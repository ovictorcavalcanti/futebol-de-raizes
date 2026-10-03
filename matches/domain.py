"""Regras da partida em funções puras (sem Django).

CONTRATO — os tipos e assinaturas abaixo são usados pelos serviços (matches/services.py),
pela API e pelos testes. As assinaturas são estáveis; campos novos só entram com default.

Convenções
----------
* Status (6): scheduled, live, finished, postponed, suspended, cancelled.
* Período (5): first_half, half_time, second_half, extra_time, penalties.
  `period` só existe com status live ou suspended (em suspenso, guarda onde parou).
* Minuto é ABSOLUTO no jogo (45+2 = minute 45, stoppage 2; 90+3; 105+1; 120+2).
  Faixas: 1T 0–45 (acréscimo só no 45); 2T 45–90 (acréscimo só no 90);
  prorrogação 90–120 (acréscimo só no 105 ou 120). Fora disso: `invalid_minute`.
  Intervalo: minuto opcional; se vier, é 45 (com ou sem acréscimo). Pênaltis:
  minuto opcional; se vier, é o do início da disputa (90 sem prorrogação, 120 com).
  Acréscimo vai de 1 a 30 (0 = sem acréscimo). `EventSpec.minute == "required"`
  vale nos períodos com relógio; no intervalo e nos pênaltis o minuto é opcional
  (`minute_mode(tipo, período)` diz o que vale agora).
* `team_id` do gol é o time BENEFICIADO; em gol contra, o jogador é do adversário.
* Jogador no MVP: `payload["player"]` (nome). Com escalação (fase 10) pode vir
  também `player_id`. Substituição usa `payload["player_out"]` e `payload["player_in"]`
  (e opcionalmente `player_out_id`/`player_in_id`). Com escalação, o domínio troca o
  nome digitado pelo nome da escalação e completa o id que faltar.
* Período gravado no evento: eventos que ABREM período levam o período novo
  (match_start→first_half, second_half_start→second_half, extra_time_start→extra_time,
  penalties_start→penalties); half_time e match_end levam o período que FECHAM;
  eventos de jogo levam o período corrente; eventos de status levam o período
  corrente (ou None fora de jogo).
* Gol válido: tipo goal, não cancelado (voided) e não anulado por um goal_annulled
  ainda válido (não cancelado). Cobrança da disputa (shootout_kick) não é gol.
* Evento derivado (vermelho automático do 2º amarelo) leva no payload
  `derived_from_sequence` = sequence do evento de origem. Cancelar a origem
  cancela o derivado (check_void).

Máquina de estados (status e período mudam só por eventos)
----------------------------------------------------------
* Estrutura: match_start (agendado → ao vivo/1T) · half_time (1T → intervalo) ·
  second_half_start (intervalo → 2T) · extra_time_start (2T → prorrogação, só no jogo
  decisivo do confronto com prorrogação e agregado igual) · penalties_start (agregado
  igual no jogo decisivo: do 2T sem prorrogação, ou da prorrogação) · match_end (2T,
  prorrogação ou pênaltis → encerrado). Fora de ordem: `invalid_transition`
  (prorrogação e pênaltis: `extra_time_not_allowed` / `penalties_not_allowed`).
* Jogo decisivo com agregado igual: fim de jogo no 2T → `tie_level_requires_extra_time`
  (com prorrogação) ou `tie_level_requires_penalties`; na prorrogação →
  `tie_level_requires_penalties`; nos pênaltis com placar igual → `penalties_level`.
  Agregado = gols dos outros jogos + placar atual; gol fora não desempata.
* Status (ações do operador): adiar (agendado → adiado) · suspender (ao vivo →
  suspenso, período mantido; a parada entra em `period_pauses`, para o relógio
  descontar) · retomar (suspenso → ao vivo, mesmo período) ·
  reagendar (adiado/agendado → agendado, exige `payload.kickoff_at` ISO 8601 com
  data e hora; sem fuso = horário de Brasília, que o serviço aplica) ·
  cancelar (agendado/adiado/suspenso → cancelado). Fora disso: `invalid_status_action`.
* Evento de jogo com status diferente de ao vivo: `match_not_live`; fora dos
  períodos do catálogo: `invalid_period_for_event`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import lru_cache


# --- Enumerações ------------------------------------------------------------------


class Status(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class Period(StrEnum):
    FIRST_HALF = "first_half"
    HALF_TIME = "half_time"
    SECOND_HALF = "second_half"
    EXTRA_TIME = "extra_time"
    PENALTIES = "penalties"


class EventType(StrEnum):
    # estrutura do jogo
    MATCH_START = "match_start"
    HALF_TIME = "half_time"  # fim do 1T = intervalo (um evento só)
    SECOND_HALF_START = "second_half_start"
    EXTRA_TIME_START = "extra_time_start"
    PENALTIES_START = "penalties_start"
    MATCH_END = "match_end"
    # lances
    GOAL = "goal"
    GOAL_ANNULLED = "goal_annulled"
    PENALTY_AWARDED = "penalty_awarded"
    PENALTY_MISSED = "penalty_missed"
    VAR_REVIEW = "var_review"
    SUBSTITUTION = "substitution"
    YELLOW_CARD = "yellow_card"
    RED_CARD = "red_card"
    STOPPAGE_TIME = "stoppage_time"
    SHOOTOUT_KICK = "shootout_kick"
    # status (ações do operador, também registradas como fatos)
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"


class StatusAction(StrEnum):
    POSTPONE = "postpone"
    SUSPEND = "suspend"
    RESUME = "resume"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"


STATUS_ACTION_EVENT: dict[str, str] = {
    StatusAction.POSTPONE: EventType.POSTPONED,
    StatusAction.SUSPEND: EventType.SUSPENDED,
    StatusAction.RESUME: EventType.RESUMED,
    StatusAction.RESCHEDULE: EventType.RESCHEDULED,
    StatusAction.CANCEL: EventType.CANCELLED,
}


class GoalOrigin(StrEnum):
    OPEN_PLAY = "open_play"
    PENALTY = "penalty"
    OWN_GOAL = "own_goal"


class DecidedBy(StrEnum):
    AGGREGATE = "aggregate"
    EXTRA_TIME = "extra_time"
    PENALTIES = "penalties"


STATUS_LABELS = {
    Status.SCHEDULED: "Agendado",
    Status.LIVE: "Ao vivo",
    Status.FINISHED: "Encerrado",
    Status.POSTPONED: "Adiado",
    Status.SUSPENDED: "Suspenso",
    Status.CANCELLED: "Cancelado",
}

PERIOD_LABELS = {
    Period.FIRST_HALF: "1º tempo",
    Period.HALF_TIME: "Intervalo",
    Period.SECOND_HALF: "2º tempo",
    Period.EXTRA_TIME: "Prorrogação",
    Period.PENALTIES: "Pênaltis",
}

PERIOD_SHORT = {
    Period.FIRST_HALF: "1T",
    Period.HALF_TIME: "INT",
    Period.SECOND_HALF: "2T",
    Period.EXTRA_TIME: "PRO",
    Period.PENALTIES: "PÊN",
}

DECIDED_BY_LABELS = {
    DecidedBy.AGGREGATE: "no agregado",
    DecidedBy.EXTRA_TIME: "na prorrogação",
    DecidedBy.PENALTIES: "nos pênaltis",
}

# Rótulos dos botões de status (GET /api/ops/catalog → status_actions).
STATUS_ACTION_LABELS = {
    StatusAction.POSTPONE: "Adiar",
    StatusAction.SUSPEND: "Suspender",
    StatusAction.RESUME: "Retomar",
    StatusAction.RESCHEDULE: "Reagendar",
    StatusAction.CANCEL: "Cancelar",
}

GOAL_ORIGIN_LABELS = {
    GoalOrigin.OPEN_PLAY: "Jogada",
    GoalOrigin.PENALTY: "Pênalti",
    GoalOrigin.OWN_GOAL: "Contra",
}

# Resultado opcional do pênalti perdido (payload["outcome"]).
PENALTY_MISS_LABELS = {
    "saved": "Defendido",
    "off_target": "Para fora",
    "woodwork": "Na trave",
}

# Ordem dos períodos, para comparar instantes do jogo (last_clock).
PERIOD_ORDER = {
    Period.FIRST_HALF: 1,
    Period.HALF_TIME: 2,
    Period.SECOND_HALF: 3,
    Period.EXTRA_TIME: 4,
    Period.PENALTIES: 5,
}

# Relógio do período para o front desenhar o minuto ao vivo sem regra de negócio:
# minuto exibido = offset + minutos decorridos desde period_started_at (+1);
# passou de regular_end → "regular_end+N".
PERIOD_CLOCK = {
    Period.FIRST_HALF: {"offset": 0, "regular_end": 45},
    Period.SECOND_HALF: {"offset": 45, "regular_end": 90},
    Period.EXTRA_TIME: {"offset": 90, "regular_end": 120},
}

MAX_STOPPAGE = 30  # acréscimo máximo (minuto 45+30) e maior anúncio de acréscimos
SECOND_YELLOW = "second_yellow"  # payload["reason"] do vermelho automático
NAME_MAX_LENGTH = 80  # nomes de jogador (igual ao modelo Player)
TEXT_MAX_LENGTH = 280  # motivos, lance revisado, decisão


# --- Catálogo de eventos ------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """Dado exigido por um tipo de evento.

    kind: team | player | text | choice | int | bool | event_ref | datetime
    `name` é a chave no corpo do lançamento: "team_id", "annuls_event_id" ou
    "payload.<chave>" (ex.: "payload.player", "payload.origin").
    """

    name: str
    kind: str
    label: str
    required: bool = True
    choices: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EventSpec:
    type: str
    label: str
    kind: str  # "structural" | "game" | "status"
    fields: tuple[FieldSpec, ...] = ()
    periods: frozenset[str] = frozenset()  # onde o evento de jogo pode ocorrer
    minute: str = "required"  # "required" | "optional" | "none"
    public: bool = True  # aparece na API pública (fase 12)?
    icon: str = ""  # nome do ícone no sprite do front


_CLOCK_PERIODS = frozenset({Period.FIRST_HALF, Period.SECOND_HALF, Period.EXTRA_TIME})
_PLAY_AND_INTERVAL = _CLOCK_PERIODS | {Period.HALF_TIME}
_ANY_PERIOD = frozenset(Period)

_F_TEAM = FieldSpec("team_id", "team", "Time")
_F_PLAYER = FieldSpec("payload.player", "player", "Jogador")
_F_REASON_OPTIONAL = FieldSpec("payload.reason", "text", "Motivo", required=False)


def _structural(type_: EventType, label: str, icon: str) -> EventSpec:
    # Minuto opcional: sem ele, o domínio usa o minuto natural da transição.
    return EventSpec(type_.value, label, "structural", minute="optional", icon=icon)


def _status(type_: EventType, label: str, icon: str, *fields: FieldSpec) -> EventSpec:
    # Status não é lance: não leva minuto e não aparece na API pública.
    return EventSpec(type_.value, label, "status", fields, minute="none", public=False, icon=icon)


def _game(type_: EventType, label: str, icon: str, periods: frozenset[str], *fields: FieldSpec, **extra) -> EventSpec:
    return EventSpec(type_.value, label, "game", fields, periods, icon=icon, **extra)


CATALOG: dict[str, EventSpec] = {
    spec.type: spec
    for spec in (
        _structural(EventType.MATCH_START, "Início de jogo", "whistle"),
        _structural(EventType.HALF_TIME, "Fim do 1º tempo", "whistle"),
        _structural(EventType.SECOND_HALF_START, "Início do 2º tempo", "whistle"),
        _structural(EventType.EXTRA_TIME_START, "Início da prorrogação", "whistle"),
        _structural(EventType.PENALTIES_START, "Início dos pênaltis", "ball-penalty"),
        _structural(EventType.MATCH_END, "Fim de jogo", "flag"),
        _game(
            EventType.GOAL, "Gol", "ball", _CLOCK_PERIODS,
            FieldSpec("team_id", "team", "Time beneficiado"),
            _F_PLAYER,
            FieldSpec(
                "payload.origin", "choice", "Origem", required=False,
                choices=tuple((origin.value, label) for origin, label in GOAL_ORIGIN_LABELS.items()),
            ),
            FieldSpec("payload.assist", "player", "Assistência", required=False),
        ),
        _game(
            EventType.GOAL_ANNULLED, "Gol anulado", "ball-x", _PLAY_AND_INTERVAL,
            FieldSpec("annuls_event_id", "event_ref", "Gol anulado", required=False),
            FieldSpec("team_id", "team", "Time", required=False),
            FieldSpec("payload.reason", "text", "Motivo"),
            public=False,
        ),
        _game(EventType.PENALTY_AWARDED, "Pênalti marcado", "ball-penalty", _CLOCK_PERIODS,
              FieldSpec("team_id", "team", "Time a favor")),
        _game(
            EventType.PENALTY_MISSED, "Pênalti perdido", "x-circle", _CLOCK_PERIODS,
            _F_TEAM,
            FieldSpec("payload.player", "player", "Cobrador"),
            FieldSpec("payload.outcome", "choice", "Resultado", required=False,
                      choices=tuple(PENALTY_MISS_LABELS.items())),
        ),
        _game(
            EventType.VAR_REVIEW, "Revisão do VAR", "var", _CLOCK_PERIODS,
            FieldSpec("team_id", "team", "Time", required=False),
            FieldSpec("payload.incident", "text", "Lance revisado"),
            FieldSpec("payload.decision", "text", "Decisão"),
        ),
        _game(
            EventType.SUBSTITUTION, "Substituição", "sub", _PLAY_AND_INTERVAL,
            _F_TEAM,
            FieldSpec("payload.player_out", "player", "Sai"),
            FieldSpec("payload.player_in", "player", "Entra"),
        ),
        _game(EventType.YELLOW_CARD, "Cartão amarelo", "card-yellow", _ANY_PERIOD, _F_TEAM, _F_PLAYER),
        _game(EventType.RED_CARD, "Cartão vermelho", "card-red", _ANY_PERIOD, _F_TEAM, _F_PLAYER),
        _game(EventType.STOPPAGE_TIME, "Acréscimos", "clock", _CLOCK_PERIODS,
              FieldSpec("payload.minutes", "int", "Minutos de acréscimo")),
        _game(
            EventType.SHOOTOUT_KICK, "Cobrança de pênalti", "target", frozenset({Period.PENALTIES}),
            _F_TEAM,
            FieldSpec("payload.player", "player", "Cobrador"),
            FieldSpec("payload.scored", "bool", "Convertida"),
            minute="optional",
        ),
        _status(EventType.POSTPONED, "Adiado", "calendar", _F_REASON_OPTIONAL),
        _status(EventType.SUSPENDED, "Suspenso", "pause", _F_REASON_OPTIONAL),
        _status(EventType.RESUMED, "Retomado", "play"),
        _status(
            EventType.RESCHEDULED, "Reagendado", "calendar",
            FieldSpec("payload.kickoff_at", "datetime", "Nova data e hora"),
            _F_REASON_OPTIONAL,
        ),
        _status(EventType.CANCELLED, "Cancelado", "x-circle", _F_REASON_OPTIONAL),
    )
}


def event_icon(event_type: str, payload: Mapping | None = None) -> str:
    """Ícone de um evento já gravado, com as variações que o payload pede
    (gol de pênalti/contra, vermelho do 2º amarelo, cobrança perdida)."""
    payload = payload if isinstance(payload, Mapping) else {}
    if event_type == EventType.GOAL:
        origin = payload.get("origin")
        if origin == GoalOrigin.PENALTY:
            return "ball-penalty"
        if origin == GoalOrigin.OWN_GOAL:
            return "ball-own"
    elif event_type == EventType.RED_CARD and payload.get("reason") == SECOND_YELLOW:
        return "card-second-yellow"
    elif event_type == EventType.SHOOTOUT_KICK and payload.get("scored") is False:
        return "x-circle"
    spec = CATALOG.get(event_type)
    return spec.icon if spec else ""


def minute_mode(event_type: str, period: str | None) -> str:
    """Exigência do minuto para lançar `event_type` com a partida em `period`:
    "required" | "optional" | "none". Lance que exige minuto com o relógio
    correndo (1T, 2T, prorrogação) o dispensa no intervalo e nos pênaltis."""
    spec = CATALOG.get(event_type)
    if spec is None:
        return "none"
    if spec.kind == "game" and spec.minute == "required" and period not in _CLOCK_PERIODS:
        return "optional"
    return spec.minute


# --- Dados que o domínio recebe ---------------------------------------------------


@dataclass(frozen=True)
class Event:
    """Evento já gravado, como o domínio o enxerga."""

    sequence: int
    type: str
    period: str | None = None
    minute: int | None = None
    stoppage: int | None = None
    team_id: int | None = None
    player_id: int | None = None
    payload: Mapping = field(default_factory=dict)
    annuls_event_id: int | None = None
    id: int | None = None
    voided: bool = False
    created_at: datetime | None = None


@dataclass(frozen=True)
class NewEvent:
    """Lançamento pedido pelo operador (período é atribuído pelo domínio)."""

    type: str
    minute: int | None = None
    stoppage: int | None = None
    team_id: int | None = None
    player_id: int | None = None
    payload: Mapping = field(default_factory=dict)
    annuls_event_id: int | None = None


@dataclass(frozen=True)
class LegScore:
    """Outro jogo do mesmo confronto (para o agregado)."""

    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    finished: bool = True


@dataclass(frozen=True)
class TieContext:
    legs: int  # 1 ou 2
    extra_time: bool
    leg: int  # jogo desta partida (1 ou 2)
    team_a_id: int
    team_b_id: int
    other_legs: tuple[LegScore, ...] = ()

    @property
    def decisive(self) -> bool:
        return self.leg == self.legs


@dataclass(frozen=True)
class LineupPlayer:
    name: str
    starter: bool
    player_id: int | None = None
    number: int | None = None


@dataclass(frozen=True)
class MatchContext:
    home_team_id: int
    away_team_id: int
    tie: TieContext | None = None
    # team_id → jogadores escalados; vazio = sem escalação (não valida em campo)
    lineups: Mapping[int, tuple[LineupPlayer, ...]] = field(default_factory=dict)


# --- Estado e resultado -------------------------------------------------------------


@dataclass(frozen=True)
class MatchState:
    status: str = Status.SCHEDULED
    period: str | None = None
    home_score: int = 0
    away_score: int = 0
    home_penalties: int | None = None
    away_penalties: int | None = None
    # sequência do evento que abriu o período corrente (o serviço converte em horário)
    period_started_seq: int | None = None
    # suspensões do período corrente: (sequence do suspended, sequence do resumed ou
    # None enquanto suspenso). O serviço desconta esses intervalos do relógio
    # (created_at): period_started_at = abertura do período + tempo parado.
    period_pauses: tuple[tuple[int, int | None], ...] = ()
    # minutos de acréscimo anunciados por período
    stoppage_announced: Mapping[str, int] = field(default_factory=dict)
    # nova data/hora pedida no último reagendamento (ISO 8601), se houver
    rescheduled_to: str | None = None
    # (ordem do período, minuto, acréscimo) mais adiantado entre os eventos de jogo
    # com minuto — base do aviso minute_decreasing
    last_clock: tuple[int, int, int] | None = None
    # controle de quem está em campo / cartões; chaves = player_key(...)
    yellow_cards: Mapping[str, int] = field(default_factory=dict)
    sent_off: frozenset[str] = frozenset()
    subbed_off: frozenset[str] = frozenset()
    subbed_on: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DomainWarning:
    code: str
    message: str


@dataclass(frozen=True)
class ApplyResult:
    state: MatchState
    event: Event  # o novo evento, com sequence e period atribuídos
    derived: tuple[Event, ...] = ()  # ex.: vermelho automático do 2º amarelo
    warnings: tuple[DomainWarning, ...] = ()


@dataclass(frozen=True)
class VoidResult:
    state: MatchState  # estado depois de cancelar
    voided_ids: tuple[int, ...]  # o evento pedido + derivados/anulações que caem junto


class DomainError(Exception):
    """Regra violada. `code` vai para a resposta 422."""

    def __init__(self, code: str, message: str, details: Mapping | None = None, warnings: Sequence[DomainWarning] = ()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.warnings = tuple(warnings)


# --- Funções puras --------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def player_key(team_id: int | None, player_id: int | None = None, name: str | None = None) -> str:
    """Chave estável de um jogador: por id, senão por nome normalizado (sem acento/caixa).

    "id:<n>" quando há id; senão "t<time>:<nome normalizado>" (o time entra porque
    nomes se repetem entre elencos).
    """
    if player_id is not None:
        return f"id:{player_id}"
    normalized = _normalize_name(name) if name else ""
    if not normalized:
        raise ValueError("player_key precisa de player_id ou de nome.")
    return f"t{team_id}:{normalized}"


def format_minute(minute: int | None, stoppage: int | None = None) -> str:
    """"45+2'", "90'", "" quando sem minuto."""
    if minute is None:
        return ""
    if stoppage:
        return f"{minute}+{stoppage}'"
    return f"{minute}'"


def visible_events(events: Iterable[Event]) -> list[Event]:
    """Remove lançamentos cancelados; ordena por sequence."""
    return sorted((event for event in events if not event.voided), key=lambda event: event.sequence)


def annulled_goal_ids(events: Iterable[Event]) -> set[int]:
    """Ids dos gols anulados por um goal_annulled ainda válido (não cancelado)."""
    return {
        event.annuls_event_id
        for event in events
        if not event.voided and event.type == EventType.GOAL_ANNULLED and event.annuls_event_id is not None
    }


def valid_goals(events: Iterable[Event]) -> list[Event]:
    """Gols válidos, em ordem de sequence."""
    visible = visible_events(events)
    annulled = annulled_goal_ids(visible)
    return [event for event in visible if event.type == EventType.GOAL and (event.id is None or event.id not in annulled)]


def score(events: Iterable[Event], ctx: MatchContext) -> tuple[int, int]:
    """Placar (mandante, visitante) contando só gols válidos."""
    home = away = 0
    for goal in valid_goals(events):
        if goal.team_id == ctx.home_team_id:
            home += 1
        elif goal.team_id == ctx.away_team_id:
            away += 1
    return home, away


def derived_from(event: Event) -> int | None:
    """Sequence do evento de origem de um evento derivado (ou None)."""
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    origin = payload.get("derived_from_sequence")
    return origin if isinstance(origin, int) and not isinstance(origin, bool) else None


def derive_state(events: Iterable[Event], ctx: MatchContext) -> MatchState:
    """Refaz o estado a partir dos eventos visíveis (fold de apply_event sem
    regras brandas). Levanta DomainError se a sequência for inconsistente.

    Além das regras de apply_event, confere o período gravado em cada evento
    (`period_mismatch` quando difere do período que a sequência produz).
    Eventos derivados já gravados entram como eventos comuns (o 2º amarelo não
    gera outro vermelho no replay).
    """
    return _replay(events, ctx).state


def apply_event(
    state: MatchState,
    events: Sequence[Event],
    new: NewEvent,
    ctx: MatchContext,
    *,
    confirm: bool = False,
    next_sequence: int | None = None,
) -> ApplyResult:
    """Valida `new` contra o estado e devolve o novo estado.

    * Regras duras → DomainError(code) (ex.: match_not_live, invalid_transition,
      annul_target_invalid, team_required, team_not_in_match, player_required,
      invalid_period_for_event, invalid_minute, extra_time_not_allowed,
      penalties_not_allowed, tie_level_requires_extra_time,
      tie_level_requires_penalties, penalties_level, invalid_payload,
      invalid_status_action, unknown_event_type).
    * Regras brandas → avisos. Sem `confirm`, avisos viram
      DomainError("confirmation_required", warnings=...). Com `confirm`, o
      lançamento é aceito e os avisos voltam em ApplyResult.warnings.
      Avisos: minute_decreasing, player_sent_off, player_not_in_lineup,
      player_not_on_field, substitute_already_used.
    * `state` deve ser o de derive_state(events); `events` são os eventos já
      gravados (podem incluir cancelados; o domínio filtra). `next_sequence`
      default = maior sequence + 1. O resultado é igual a derive_state(events +
      [event] + derived).
    * O evento devolvido traz período, minuto padrão (eventos estruturais), time
      (gol anulado herda o do gol) e payload normalizado (só as chaves do tipo).
    * 2º amarelo do mesmo jogador → `derived` traz o vermelho automático
      (payload {"player": ..., "reason": "second_yellow", "derived_from_sequence":
      <sequence do amarelo>}), na sequence seguinte, mesmo time/período/minuto.
    """
    sequence = next_sequence if next_sequence is not None else max((event.sequence for event in events), default=0) + 1
    history = _History.of(visible_events(events))
    step = _step(state, new, ctx, history, sequence=sequence, collect=True)
    if step.warnings and not confirm:
        raise DomainError(
            "confirmation_required",
            "Há avisos neste lançamento: confirme para lançar mesmo assim.",
            {"codes": [warning.code for warning in step.warnings]},
            warnings=step.warnings,
        )
    derived: tuple[Event, ...] = ()
    new_state = step.state
    if step.second_yellow:
        yellow = step.event
        name = yellow.payload.get("player")
        red = NewEvent(
            type=EventType.RED_CARD,
            minute=yellow.minute,
            stoppage=yellow.stoppage,
            team_id=yellow.team_id,
            player_id=yellow.player_id,
            payload={"player": name} if name else {},
        )
        red_step = _step(new_state, red, ctx, history, sequence=sequence + 1, collect=False)
        payload = {**red_step.event.payload, "reason": SECOND_YELLOW, "derived_from_sequence": yellow.sequence}
        derived = (replace(red_step.event, payload=payload),)
        new_state = red_step.state
    return ApplyResult(new_state, step.event, derived, step.warnings)


def check_void(events: Sequence[Event], event_id: int, ctx: MatchContext) -> VoidResult:
    """Cancela um lançamento errado (sem apagar).

    Caem junto: eventos derivados dele (payload["derived_from_sequence"] == sua
    sequence) e anulações que apontam para ele (annuls_event_id == seu id), em
    cascata. Também cai o vermelho automático cujo amarelo de origem deixou de ser
    o 2º do jogador (ex.: cancelar o 1º amarelo). O estado é refeito sem eles; se a
    sequência restante ficar inválida → DomainError("void_breaks_sequence"), com a
    regra violada em details["cause"] (inclusive "second_yellow_without_red": um
    amarelo que passaria a ser o 2º do jogador sem ter o vermelho automático).
    Erros: event_not_found, already_voided, void_derived_event (o vermelho
    automático só cai junto com o amarelo de origem).
    `voided_ids` começa pelo evento pedido; os demais seguem a ordem de sequence.
    """
    events = list(events)
    target = next((event for event in events if event.id == event_id), None)
    if target is None:
        raise DomainError("event_not_found", "Lançamento não encontrado nesta partida.", {"event_id": event_id})
    if target.voided:
        raise DomainError("already_voided", "Este lançamento já foi cancelado.", {"event_id": event_id})
    visible = visible_events(events)
    origin = derived_from(target)
    if origin is not None and any(event.sequence == origin for event in visible):
        raise DomainError(
            "void_derived_event",
            "Este vermelho saiu do 2º amarelo: cancele o cartão amarelo de origem.",
            {"event_id": event_id, "origin_sequence": origin},
        )

    dropped = _cascade(visible, {target.sequence})
    while True:
        remaining = [event for event in visible if event.sequence not in dropped]
        try:
            replay = _replay(remaining, ctx)
        except DomainError as exc:
            raise DomainError(
                "void_breaks_sequence",
                f"Cancelar este lançamento deixa a sequência inválida: {exc.message}",
                {"event_id": event_id, "cause": exc.code, **exc.details},
            ) from exc
        orphans = {
            event.sequence
            for event in remaining
            if _is_second_yellow_red(event) and derived_from(event) not in replay.second_yellows
        }
        if not orphans:
            break
        dropped = _cascade(visible, dropped | orphans)

    # Um amarelo que passa a ser o 2º do jogador (ex.: cancelar o 2º com um 3º já
    # lançado, ou um vermelho direto anterior) ficaria sem o vermelho automático.
    with_red = {derived_from(event) for event in remaining if _is_second_yellow_red(event)}
    missing = sorted(replay.second_yellows - with_red)
    if missing:
        raise DomainError(
            "void_breaks_sequence",
            f"Cancelar este lançamento faz do amarelo #{missing[0]} o 2º do jogador, sem o vermelho "
            "automático: cancele esse amarelo antes e lance-o de novo depois.",
            {"event_id": event_id, "cause": "second_yellow_without_red", "sequence": missing[0]},
        )

    others = tuple(event.id for event in visible if event.sequence in dropped and event.id is not None and event.id != event_id)
    return VoidResult(replay.state, (event_id, *others))


def available_actions(state: MatchState, ctx: MatchContext, events: Sequence[Event] = ()) -> dict:
    """O que o operador pode lançar agora: {"events": [tipos], "status": [ações]}.

    O front do operador só desenha botões a partir daqui (sem regra própria).
    Ordem de tela: eventos estruturais primeiro, depois os lances mais comuns.
    Usa as mesmas regras de apply_event (ex.: 2T do jogo decisivo com agregado
    igual → sem match_end, com extra_time_start ou penalties_start). `events` fica
    reservado (as regras atuais dependem só do estado e do contexto).
    """
    allowed: list[str] = []
    if state.status in (Status.SCHEDULED, Status.LIVE):
        allowed.extend(
            event_type.value
            for event_type in _STRUCTURAL_ORDER
            if not isinstance(_plan_structural(state, event_type, ctx), DomainError)
        )
    if state.status == Status.LIVE:
        allowed.extend(event_type.value for event_type in _GAME_ORDER if state.period in CATALOG[event_type].periods)
    status = [
        action.value
        for action in _STATUS_ORDER
        if not isinstance(_plan_status(state, STATUS_ACTION_EVENT[action]), DomainError)
    ]
    return {"events": allowed, "status": status}


def status_action_event(action: str, *, kickoff_at: str | None = None, reason: str | None = None) -> NewEvent:
    """Converte a ação de status (/status) no evento correspondente.

    Ação desconhecida → invalid_status_action; reagendar sem kickoff_at (ou com
    data inválida) → invalid_payload. `kickoff_at` aceita texto ISO 8601 ou datetime
    e só vale para reagendar.
    """
    event_type = STATUS_ACTION_EVENT.get(action) if isinstance(action, str) else None
    if event_type is None:
        raise DomainError("invalid_status_action", "Ação de status desconhecida.", {"action": action})
    payload: dict[str, str] = {}
    if event_type == EventType.RESCHEDULED:
        if kickoff_at is None or kickoff_at == "":
            raise DomainError("invalid_payload", "Informe a nova data e hora do jogo.", {"field": "kickoff_at"})
        payload["kickoff_at"] = _iso_datetime(kickoff_at, "kickoff_at")
    text = _text({"reason": reason}, "reason", "payload.reason", required=False)
    if text:
        payload["reason"] = text
    return NewEvent(type=event_type, payload=payload)


# --- Mata-mata -------------------------------------------------------------------------


@dataclass(frozen=True)
class TieInfo:
    legs: int
    extra_time: bool
    team_a_id: int
    team_b_id: int


@dataclass(frozen=True)
class TieLeg:
    leg: int
    home_team_id: int
    away_team_id: int
    status: str
    home_score: int = 0
    away_score: int = 0
    home_penalties: int | None = None
    away_penalties: int | None = None


@dataclass(frozen=True)
class TieResult:
    aggregate_a: int
    aggregate_b: int
    winner_team_id: int | None
    decided_by: str | None  # DecidedBy ou None
    complete: bool  # todos os jogos encerrados e vencedor definido


def compute_tie_result(tie: TieInfo, legs: Sequence[TieLeg], decisive_event_types: Iterable[str]) -> TieResult:
    """Agregado (inclui jogos em andamento), vencedor e forma da decisão.

    1. Soma os gols de cada time em todos os jogos do confronto (jogo cancelado
       não conta).
    2. Todos os jogos encerrados e agregado diferente → avança quem fez mais.
    3. Agregado igual → vence a disputa de pênaltis do último jogo. Gol fora não vale.
    4. decided_by sai dos eventos VISÍVEIS (não cancelados) do jogo decisivo: com
       penalties_start → "penalties"; só com extra_time_start → "extra_time";
       senão "aggregate".
       Agregado igual decidido nos pênaltis é sempre "penalties".
    Sem vencedor definido (jogo pendente, pênaltis ausentes ou iguais):
    winner_team_id=None, decided_by=None, complete=False.
    """
    aggregate_a = aggregate_b = 0
    for leg in legs:
        if leg.status == Status.CANCELLED:
            continue
        aggregate_a += _goals_of(tie.team_a_id, leg.home_team_id, leg.away_team_id, leg.home_score, leg.away_score)
        aggregate_b += _goals_of(tie.team_b_id, leg.home_team_id, leg.away_team_id, leg.home_score, leg.away_score)

    finished = {leg.leg: leg for leg in legs if leg.status == Status.FINISHED}
    if any(number not in finished for number in range(1, tie.legs + 1)):
        return TieResult(aggregate_a, aggregate_b, None, None, False)

    if aggregate_a != aggregate_b:
        types = set(decisive_event_types)
        if EventType.PENALTIES_START in types:
            decided_by = DecidedBy.PENALTIES
        elif EventType.EXTRA_TIME_START in types:
            decided_by = DecidedBy.EXTRA_TIME
        else:
            decided_by = DecidedBy.AGGREGATE
        winner = tie.team_a_id if aggregate_a > aggregate_b else tie.team_b_id
        return TieResult(aggregate_a, aggregate_b, winner, decided_by.value, True)

    last = finished[tie.legs]
    home_pen, away_pen = last.home_penalties, last.away_penalties
    if home_pen is None or away_pen is None or home_pen == away_pen:
        return TieResult(aggregate_a, aggregate_b, None, None, False)
    winner = last.home_team_id if home_pen > away_pen else last.away_team_id
    return TieResult(aggregate_a, aggregate_b, winner, DecidedBy.PENALTIES.value, True)


# --- Implementação interna ----------------------------------------------------------

_STRUCTURAL_ORDER = (
    EventType.MATCH_START,
    EventType.HALF_TIME,
    EventType.SECOND_HALF_START,
    EventType.EXTRA_TIME_START,
    EventType.PENALTIES_START,
    EventType.MATCH_END,
)
_GAME_ORDER = (
    EventType.GOAL,
    EventType.SHOOTOUT_KICK,
    EventType.PENALTY_AWARDED,
    EventType.PENALTY_MISSED,
    EventType.YELLOW_CARD,
    EventType.RED_CARD,
    EventType.SUBSTITUTION,
    EventType.VAR_REVIEW,
    EventType.STOPPAGE_TIME,
    EventType.GOAL_ANNULLED,
)
_STATUS_ORDER = (
    StatusAction.RESUME,
    StatusAction.SUSPEND,
    StatusAction.POSTPONE,
    StatusAction.RESCHEDULE,
    StatusAction.CANCEL,
)
_STATUS_VERBS = {
    EventType.POSTPONED: "adiar",
    EventType.SUSPENDED: "suspender",
    EventType.RESUMED: "retomar",
    EventType.RESCHEDULED: "reagendar",
    EventType.CANCELLED: "cancelar",
}
_ORDER_PERIOD = {order: period for period, order in PERIOD_ORDER.items()}
# "no 1º tempo", "na prorrogação"... (mensagens com a preposição certa)
_PERIOD_IN = {
    Period.FIRST_HALF: "no 1º tempo",
    Period.HALF_TIME: "no intervalo",
    Period.SECOND_HALF: "no 2º tempo",
    Period.EXTRA_TIME: "na prorrogação",
    Period.PENALTIES: "nos pênaltis",
}
_ISO_DATETIME_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
# (mínimo, máximo, minutos que aceitam acréscimo); pênaltis: ver _shootout_minutes.
_MINUTE_RULES: dict[str, tuple[int, int, tuple[int, ...]]] = {
    Period.FIRST_HALF: (0, 45, (45,)),
    Period.HALF_TIME: (45, 45, (45,)),
    Period.SECOND_HALF: (45, 90, (90,)),
    Period.EXTRA_TIME: (90, 120, (105, 120)),
    Period.PENALTIES: (90, 120, ()),
}


def _error(code: str, message: str, **details) -> DomainError:
    return DomainError(code, message, details)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class _Transition:
    status: str
    period: str | None  # período do estado depois do evento
    event_period: str | None  # período gravado no evento
    minute: int | None = None  # minuto natural do evento estrutural
    stoppage_ok: bool = False  # o evento fecha um período (aceita acréscimo)


@dataclass
class _History:
    """O que a validação precisa saber dos eventos visíveis anteriores."""

    goals: dict[int, Event] = field(default_factory=dict)  # gols visíveis por id
    annulled: set[int] = field(default_factory=set)  # ids de gols com anulação válida
    second_yellows: set[int] = field(default_factory=set)  # sequences de 2º amarelo

    @classmethod
    def of(cls, visible: Iterable[Event]) -> _History:
        history = cls()
        for event in visible:
            history.observe(event)
        return history

    def observe(self, event: Event) -> None:
        if event.type == EventType.GOAL and event.id is not None:
            self.goals[event.id] = event
        elif event.type == EventType.GOAL_ANNULLED and event.annuls_event_id is not None:
            self.annulled.add(event.annuls_event_id)


@dataclass(frozen=True)
class _Step:
    state: MatchState
    event: Event
    warnings: tuple[DomainWarning, ...] = ()
    second_yellow: bool = False


@dataclass(frozen=True)
class _Replay:
    state: MatchState
    second_yellows: frozenset[int]


def _replay(events: Iterable[Event], ctx: MatchContext) -> _Replay:
    state = MatchState()
    history = _History()
    for stored in visible_events(events):
        new = NewEvent(
            type=stored.type,
            minute=stored.minute,
            stoppage=stored.stoppage,
            team_id=stored.team_id,
            player_id=stored.player_id,
            payload=stored.payload,
            annuls_event_id=stored.annuls_event_id,
        )
        step = _step(state, new, ctx, history, sequence=stored.sequence, collect=False)
        if stored.period is not None and stored.period != step.event.period:
            raise _error(
                "period_mismatch",
                f"O lance #{stored.sequence} está gravado em outro período.",
                sequence=stored.sequence,
                stored=stored.period,
                expected=step.event.period,
            )
        state = step.state
        history.observe(stored)
        if step.second_yellow:
            history.second_yellows.add(stored.sequence)
    return _Replay(state, frozenset(history.second_yellows))


def _cascade(visible: Sequence[Event], sequences: set[int]) -> set[int]:
    """Fecha o conjunto de eventos a cancelar: derivados e anulações, em cascata.

    `visible` está em ordem de sequence e todo dependente vem depois da origem,
    então uma passada basta.
    """
    dropped = set(sequences)
    dropped_ids = {event.id for event in visible if event.sequence in dropped and event.id is not None}
    for event in visible:
        if event.sequence in dropped:
            continue
        origin = derived_from(event)
        annuls_dropped = event.type == EventType.GOAL_ANNULLED and event.annuls_event_id in dropped_ids
        if (origin is not None and origin in dropped) or annuls_dropped:
            dropped.add(event.sequence)
            if event.id is not None:
                dropped_ids.add(event.id)
    return dropped


def _is_second_yellow_red(event: Event) -> bool:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    return event.type == EventType.RED_CARD and payload.get("reason") == SECOND_YELLOW and derived_from(event) is not None


def _goals_of(team_id: int, home_team_id: int, away_team_id: int, home_score: int, away_score: int) -> int:
    return (home_score if home_team_id == team_id else 0) + (away_score if away_team_id == team_id else 0)


def _aggregate(state: MatchState, ctx: MatchContext) -> tuple[int, int]:
    tie = ctx.tie
    assert tie is not None
    legs = (*tie.other_legs, LegScore(ctx.home_team_id, ctx.away_team_id, state.home_score, state.away_score))
    total_a = sum(_goals_of(tie.team_a_id, leg.home_team_id, leg.away_team_id, leg.home_score, leg.away_score) for leg in legs)
    total_b = sum(_goals_of(tie.team_b_id, leg.home_team_id, leg.away_team_id, leg.home_score, leg.away_score) for leg in legs)
    return total_a, total_b


def _decisive_level(state: MatchState, ctx: MatchContext) -> bool:
    """Jogo decisivo de confronto com o agregado igual."""
    if ctx.tie is None or not ctx.tie.decisive:
        return False
    total_a, total_b = _aggregate(state, ctx)
    return total_a == total_b


def _transition_error(state: MatchState, event_type: str) -> DomainError:
    label = CATALOG[event_type].label
    where = STATUS_LABELS.get(state.status, state.status)
    if state.period:
        where = f"{where} ({PERIOD_LABELS.get(state.period, state.period)})"
    return _error(
        "invalid_transition",
        f"{label} não é permitido agora: o jogo está {where.lower()}.",
        status=state.status,
        period=state.period,
        type=event_type,
    )


def _tie_block(state: MatchState, ctx: MatchContext, *, what: str) -> str | None:
    """Motivo comum de recusa de prorrogação/pênaltis (ou None)."""
    tie = ctx.tie
    if tie is None:
        return f"Só jogo de mata-mata tem {what}."
    if not tie.decisive:
        return f"Só o jogo decisivo do confronto tem {what}."
    if not _decisive_level(state, ctx):
        return f"Só há {what} com o agregado empatado."
    return None


def _plan_structural(state: MatchState, event_type: str, ctx: MatchContext) -> _Transition | DomainError:
    live = state.status == Status.LIVE
    period = state.period
    if event_type == EventType.MATCH_START:
        if state.status != Status.SCHEDULED:
            return _transition_error(state, event_type)
        return _Transition(Status.LIVE, Period.FIRST_HALF, Period.FIRST_HALF, 0)
    if event_type == EventType.HALF_TIME:
        if not (live and period == Period.FIRST_HALF):
            return _transition_error(state, event_type)
        return _Transition(Status.LIVE, Period.HALF_TIME, Period.FIRST_HALF, 45, True)
    if event_type == EventType.SECOND_HALF_START:
        if not (live and period == Period.HALF_TIME):
            return _transition_error(state, event_type)
        return _Transition(Status.LIVE, Period.SECOND_HALF, Period.SECOND_HALF, 45)
    if event_type == EventType.EXTRA_TIME_START:
        if not (live and period == Period.SECOND_HALF):
            reason = "A prorrogação só começa ao fim do 2º tempo."
        elif ctx.tie is not None and ctx.tie.decisive and not ctx.tie.extra_time:
            reason = "Este confronto não tem prorrogação: o empate vai direto aos pênaltis."
        else:
            reason = _tie_block(state, ctx, what="prorrogação")
        if reason:
            return _error("extra_time_not_allowed", reason, status=state.status, period=period)
        return _Transition(Status.LIVE, Period.EXTRA_TIME, Period.EXTRA_TIME, 90, True)
    if event_type == EventType.PENALTIES_START:
        reason = _tie_block(state, ctx, what="disputa de pênaltis")
        if not live:
            reason = "Os pênaltis só começam com o jogo em andamento."
        elif reason is None:
            expected = Period.EXTRA_TIME if ctx.tie.extra_time else Period.SECOND_HALF
            if period != expected:
                reason = (
                    "Este confronto tem prorrogação: os pênaltis vêm ao fim dela."
                    if ctx.tie.extra_time
                    else "Os pênaltis começam ao fim do 2º tempo."
                )
        if reason:
            return _error("penalties_not_allowed", reason, status=state.status, period=period)
        return _Transition(Status.LIVE, Period.PENALTIES, Period.PENALTIES, 90 if period == Period.SECOND_HALF else 120, True)
    if event_type == EventType.MATCH_END:
        if not live or period not in (Period.SECOND_HALF, Period.EXTRA_TIME, Period.PENALTIES):
            return _transition_error(state, event_type)
        if period == Period.PENALTIES:
            if (state.home_penalties or 0) == (state.away_penalties or 0):
                return _error(
                    "penalties_level",
                    "A disputa de pênaltis está empatada: o jogo só acaba com um vencedor.",
                    home_penalties=state.home_penalties,
                    away_penalties=state.away_penalties,
                )
            end_minute = 120 if ctx.tie is not None and ctx.tie.extra_time else 90
        elif _decisive_level(state, ctx):
            if period == Period.SECOND_HALF and ctx.tie.extra_time:
                return _error("tie_level_requires_extra_time", "O agregado está empatado: o jogo vai para a prorrogação.")
            return _error("tie_level_requires_penalties", "O agregado está empatado: o jogo vai para os pênaltis.")
        else:
            end_minute = 90 if period == Period.SECOND_HALF else 120
        return _Transition(Status.FINISHED, None, period, end_minute, True)
    raise AssertionError(f"evento estrutural desconhecido: {event_type}")


def _plan_status(state: MatchState, event_type: str) -> _Transition | DomainError:
    current = state.status
    if event_type == EventType.POSTPONED and current == Status.SCHEDULED:
        return _Transition(Status.POSTPONED, None, None)
    if event_type == EventType.SUSPENDED and current == Status.LIVE:
        return _Transition(Status.SUSPENDED, state.period, state.period)
    if event_type == EventType.RESUMED and current == Status.SUSPENDED:
        return _Transition(Status.LIVE, state.period, state.period)
    if event_type == EventType.RESCHEDULED and current in (Status.SCHEDULED, Status.POSTPONED):
        return _Transition(Status.SCHEDULED, None, None)
    if event_type == EventType.CANCELLED and current in (Status.SCHEDULED, Status.POSTPONED, Status.SUSPENDED):
        return _Transition(Status.CANCELLED, None, state.period)
    message = f"Não é possível {_STATUS_VERBS[event_type]} um jogo {STATUS_LABELS.get(current, current).lower()}."
    if event_type == EventType.CANCELLED and current == Status.LIVE:
        message += " Suspenda o jogo antes de cancelar."
    return _error("invalid_status_action", message, status=current, type=event_type)


def _step(
    state: MatchState,
    new: NewEvent,
    ctx: MatchContext,
    history: _History,
    *,
    sequence: int,
    collect: bool,
) -> _Step:
    spec = CATALOG.get(new.type) if isinstance(new.type, str) else None
    if spec is None:
        raise _error("unknown_event_type", "Tipo de evento desconhecido.", type=new.type)
    if spec.kind == "structural":
        return _step_structural(state, new, spec, ctx, sequence, collect)
    if spec.kind == "status":
        return _step_status(state, new, spec, sequence)
    return _step_game(state, new, spec, ctx, history, sequence, collect)


def _normalized_stoppage(stoppage) -> int | None:
    if stoppage is None or (_is_int(stoppage) and stoppage == 0):
        return None
    if not _is_int(stoppage) or not 1 <= stoppage <= MAX_STOPPAGE:
        raise _error("invalid_minute", f"Acréscimo vai de 1 a {MAX_STOPPAGE} minutos.", stoppage=stoppage)
    return stoppage


def _minute_decreasing(state: MatchState, period: str, minute: int, stoppage: int | None) -> DomainWarning | None:
    """Aviso quando o instante informado é anterior ao lance mais adiantado."""
    last = state.last_clock
    if last is None or (PERIOD_ORDER[period], minute, stoppage or 0) >= last:
        return None
    last_label = f"{PERIOD_SHORT[_ORDER_PERIOD[last[0]]]} {format_minute(last[1], last[2])}"
    return DomainWarning(
        "minute_decreasing",
        f"O minuto {format_minute(minute, stoppage)} é anterior ao do último lance ({last_label}).",
    )


def _step_structural(
    state: MatchState, new: NewEvent, spec: EventSpec, ctx: MatchContext, sequence: int, collect: bool
) -> _Step:
    plan = _plan_structural(state, spec.type, ctx)
    if isinstance(plan, DomainError):
        raise plan
    minute, stoppage = new.minute, _normalized_stoppage(new.stoppage)
    warnings: tuple[DomainWarning, ...] = ()
    if minute is None:
        if stoppage is not None:
            raise _error("invalid_minute", "Acréscimo sem minuto.", stoppage=stoppage)
        minute = plan.minute  # minuto natural: não é comparado com os lances
    elif not _is_int(minute) or minute != plan.minute or (stoppage is not None and not plan.stoppage_ok):
        expected = f"{plan.minute}+N'" if plan.stoppage_ok else f"{plan.minute}'"
        raise _error(
            "invalid_minute",
            f"{spec.label} acontece no minuto {plan.minute}' (aceita {expected}).",
            minute=minute,
            stoppage=stoppage,
        )
    elif collect:
        # Minuto informado à mão (ex.: fim do 1T em 45+2 depois de um lance em 45+4).
        # Evento que fecha período compara no período que ele fecha.
        closing = state.period if plan.stoppage_ok else plan.event_period
        warning = _minute_decreasing(state, closing, minute, stoppage)
        warnings = (warning,) if warning else ()
    changes: dict = {
        "status": plan.status,
        "period": plan.period,
        "period_started_seq": sequence if plan.period is not None else None,
        "period_pauses": (),
    }
    if spec.type == EventType.PENALTIES_START:
        changes.update(home_penalties=0, away_penalties=0)
    event = Event(sequence, spec.type, plan.event_period, minute, stoppage, payload={})
    return _Step(replace(state, **changes), event, warnings)


def _step_status(state: MatchState, new: NewEvent, spec: EventSpec, sequence: int) -> _Step:
    plan = _plan_status(state, spec.type)
    if isinstance(plan, DomainError):
        raise plan
    if new.minute is not None or _normalized_stoppage(new.stoppage) is not None:
        raise _error("invalid_minute", "Mudança de status não leva minuto.", minute=new.minute)
    raw = _payload_of(new)
    payload: dict[str, str] = {}
    changes: dict = {"status": plan.status, "period": plan.period}
    if plan.period is None:
        changes.update(period_started_seq=None, period_pauses=())
    elif spec.type == EventType.SUSPENDED:
        changes["period_pauses"] = (*state.period_pauses, (sequence, None))
    elif spec.type == EventType.RESUMED and state.period_pauses and state.period_pauses[-1][1] is None:
        changes["period_pauses"] = (*state.period_pauses[:-1], (state.period_pauses[-1][0], sequence))
    if spec.type == EventType.RESCHEDULED:
        value = raw.get("kickoff_at")
        if value is None or value == "":
            raise _error("invalid_payload", "Informe a nova data e hora do jogo.", field="payload.kickoff_at")
        payload["kickoff_at"] = changes["rescheduled_to"] = _iso_datetime(value, "payload.kickoff_at")
    if spec.type != EventType.RESUMED:
        reason = _text(raw, "reason", "payload.reason", required=False)
        if reason:
            payload["reason"] = reason
    event = Event(sequence, spec.type, plan.event_period, payload=payload)
    return _Step(replace(state, **changes), event)


def _iso_datetime(value, field_name: str) -> str:
    """Data e hora em ISO 8601 (com fuso ou sem). Só a data (que viraria meia-noite)
    ou formatos compactos são recusados."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and _ISO_DATETIME_PREFIX.match(value.strip()):
        try:
            return datetime.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise _error(
        "invalid_payload",
        "Data e hora inválidas: use o formato ISO 8601 (ex.: 2026-10-10T19:30:00-03:00).",
        field=field_name,
    )


# --- Lances (eventos de jogo) ---------------------------------------------------------


@dataclass(frozen=True)
class _PlayerRef:
    team_id: int
    key: str
    name: str | None
    player_id: int | None
    lineup: tuple[LineupPlayer, ...] | None  # escalação do time do jogador (None = sem)
    entry: LineupPlayer | None  # o jogador na escalação, se achado

    @property
    def label(self) -> str:
        return self.name or f"Jogador #{self.player_id}"

    def named(self, prefix: str = "player") -> dict:
        data: dict = {}
        if self.name:
            data[prefix] = self.name
        return data


@dataclass
class _Input:
    state: MatchState
    new: NewEvent
    ctx: MatchContext
    history: _History
    payload: Mapping
    period: str
    collect: bool
    warnings: list[DomainWarning] = field(default_factory=list)

    def warn(self, code: str, message: str) -> None:
        if self.collect:
            self.warnings.append(DomainWarning(code, message))


@dataclass
class _Outcome:
    team_id: int | None = None
    player_id: int | None = None
    payload: dict = field(default_factory=dict)
    annuls_event_id: int | None = None
    changes: dict = field(default_factory=dict)
    second_yellow: bool = False


def _payload_of(new: NewEvent) -> Mapping:
    payload = new.payload
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise _error("invalid_payload", "Os dados do lançamento precisam ser um objeto.", field="payload")
    return payload


def _text(payload: Mapping, key: str, field_name: str, *, required: bool, missing: str = "Preencha o campo.") -> str | None:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _error("invalid_payload", missing, field=field_name)
        return None
    if not isinstance(value, str):
        raise _error("invalid_payload", "Texto inválido.", field=field_name)
    value = value.strip()
    if len(value) > TEXT_MAX_LENGTH:
        raise _error("invalid_payload", f"Use no máximo {TEXT_MAX_LENGTH} caracteres.", field=field_name)
    return value


def _clean_name(value, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("invalid_payload", "Nome de jogador inválido.", field=field_name)
    name = " ".join(value.split())
    if len(name) > NAME_MAX_LENGTH:
        raise _error("invalid_payload", f"Nome com mais de {NAME_MAX_LENGTH} caracteres.", field=field_name)
    return name or None


def _optional_id(value, field_name: str) -> int | None:
    if value is None:
        return None
    if not _is_int(value) or value < 1:
        raise _error("invalid_payload", "Identificador de jogador inválido.", field=field_name)
    return value


def _team(i: _Input, *, required: bool = True) -> int | None:
    team_id = i.new.team_id
    if team_id is None:
        if required:
            raise _error("team_required", "Informe o time.", field="team_id")
        return None
    if isinstance(team_id, bool) or team_id not in (i.ctx.home_team_id, i.ctx.away_team_id):
        raise _error("team_not_in_match", "O time informado não joga esta partida.", team_id=team_id)
    return team_id


def _opponent(ctx: MatchContext, team_id: int) -> int:
    return ctx.away_team_id if team_id == ctx.home_team_id else ctx.home_team_id


def _side(ctx: MatchContext, team_id: int | None, suffix: str = "score") -> str | None:
    if team_id == ctx.home_team_id:
        return f"home_{suffix}"
    if team_id == ctx.away_team_id:
        return f"away_{suffix}"
    return None


def _find_in_lineup(lineup: tuple[LineupPlayer, ...], player_id: int | None, name: str | None) -> LineupPlayer | None:
    if player_id is not None:
        for entry in lineup:
            if entry.player_id == player_id:
                return entry
    if name:
        wanted = _normalize_name(name)
        for entry in lineup:
            if entry.name and _normalize_name(entry.name) == wanted:
                return entry
    return None


def _player(ctx: MatchContext, team_id: int, raw_name, raw_id, field_name: str) -> _PlayerRef:
    name = _clean_name(raw_name, field_name)
    player_id = _optional_id(raw_id, field_name)
    if name is None and player_id is None:
        raise _error("player_required", "Informe o jogador.", field=field_name)
    lineup = ctx.lineups.get(team_id) or None
    entry = _find_in_lineup(lineup, player_id, name) if lineup else None
    if entry is None:
        return _PlayerRef(team_id, player_key(team_id, player_id, name), name, player_id, lineup, None)
    key = player_key(team_id, entry.player_id, entry.name)
    resolved_id = entry.player_id if entry.player_id is not None else player_id
    return _PlayerRef(team_id, key, entry.name or name, resolved_id, lineup, entry)


def _is_on_field(state: MatchState, ref: _PlayerRef) -> bool:
    if ref.key in state.subbed_off or ref.key in state.sent_off:
        return False
    if ref.lineup is None:
        return True  # sem escalação, só sabemos quem saiu
    return ref.entry is not None and (ref.entry.starter or ref.key in state.subbed_on)


def _check_player(i: _Input, ref: _PlayerRef, *, on_field: bool) -> None:
    """Avisos sobre o jogador: expulso, fora da escalação, fora de campo."""
    if ref.key in i.state.sent_off:
        i.warn("player_sent_off", f"{ref.label} já foi expulso.")
    elif ref.lineup is not None and ref.entry is None:
        i.warn("player_not_in_lineup", f"{ref.label} não está na escalação do time.")
    elif on_field and not _is_on_field(i.state, ref):
        i.warn("player_not_on_field", f"{ref.label} não está em campo.")


def _check_incoming(i: _Input, ref: _PlayerRef) -> None:
    """Avisos sobre quem entra numa substituição."""
    if ref.key in i.state.sent_off:
        i.warn("player_sent_off", f"{ref.label} já foi expulso.")
    elif ref.lineup is not None and ref.entry is None:
        i.warn("player_not_in_lineup", f"{ref.label} não está na escalação do time.")
    elif ref.key in i.state.subbed_off:
        i.warn("substitute_already_used", f"{ref.label} já saiu do jogo e não pode voltar.")
    elif ref.key in i.state.subbed_on or (ref.entry is not None and ref.entry.starter):
        i.warn("substitute_already_used", f"{ref.label} já está em campo.")


def _on_goal(i: _Input) -> _Outcome:
    team = _team(i)
    origin = i.payload.get("origin") or GoalOrigin.OPEN_PLAY
    if not isinstance(origin, str) or origin not in GOAL_ORIGIN_LABELS:
        raise _error("invalid_payload", "Origem do gol inválida.", field="payload.origin")
    origin = GoalOrigin(origin)
    scorer_team = _opponent(i.ctx, team) if origin == GoalOrigin.OWN_GOAL else team
    scorer = _player(i.ctx, scorer_team, i.payload.get("player"), i.new.player_id, "payload.player")
    assist = _clean_name(i.payload.get("assist"), "payload.assist")
    if assist and origin == GoalOrigin.OWN_GOAL:
        raise _error("invalid_payload", "Gol contra não tem assistência.", field="payload.assist")
    _check_player(i, scorer, on_field=True)
    payload = {**scorer.named(), "origin": origin.value}
    if assist:
        payload["assist"] = assist
    side = _side(i.ctx, team)
    return _Outcome(team, scorer.player_id, payload, changes={side: getattr(i.state, side) + 1})


def _on_goal_annulled(i: _Input) -> _Outcome:
    reason = _text(i.payload, "reason", "payload.reason", required=True, missing="Informe o motivo da anulação.")
    target_id = i.new.annuls_event_id
    if target_id is None:
        return _Outcome(_team(i), payload={"reason": reason})
    target = i.history.goals.get(target_id) if _is_int(target_id) else None
    if target is None:
        raise _error("annul_target_invalid", "O lance indicado não é um gol desta partida.", annuls_event_id=target_id)
    if target_id in i.history.annulled:
        raise _error("annul_target_invalid", "Este gol já foi anulado.", annuls_event_id=target_id)
    if i.new.team_id is not None and i.new.team_id != target.team_id:
        raise _error("annul_target_invalid", "O time informado não é o do gol anulado.", annuls_event_id=target_id)
    target_payload = target.payload if isinstance(target.payload, Mapping) else {}
    payload = {"reason": reason}
    if target_payload.get("player"):
        payload["player"] = target_payload["player"]
    side = _side(i.ctx, target.team_id)
    changes = {side: getattr(i.state, side) - 1} if side else {}
    return _Outcome(target.team_id, target.player_id, payload, target_id, changes)


def _on_penalty_awarded(i: _Input) -> _Outcome:
    return _Outcome(_team(i))


def _on_penalty_missed(i: _Input) -> _Outcome:
    team = _team(i)
    taker = _player(i.ctx, team, i.payload.get("player"), i.new.player_id, "payload.player")
    outcome = i.payload.get("outcome") or None
    if outcome is not None and (not isinstance(outcome, str) or outcome not in PENALTY_MISS_LABELS):
        raise _error("invalid_payload", "Resultado do pênalti inválido.", field="payload.outcome")
    _check_player(i, taker, on_field=True)
    payload = taker.named()
    if outcome:
        payload["outcome"] = outcome
    return _Outcome(team, taker.player_id, payload)


def _on_var_review(i: _Input) -> _Outcome:
    team = _team(i, required=False)
    incident = _text(i.payload, "incident", "payload.incident", required=True, missing="Informe o lance revisado.")
    decision = _text(i.payload, "decision", "payload.decision", required=True, missing="Informe a decisão do VAR.")
    return _Outcome(team, payload={"incident": incident, "decision": decision})


def _on_substitution(i: _Input) -> _Outcome:
    team = _team(i)
    leaving = _player(i.ctx, team, i.payload.get("player_out"), i.payload.get("player_out_id"), "payload.player_out")
    entering = _player(i.ctx, team, i.payload.get("player_in"), i.payload.get("player_in_id"), "payload.player_in")
    if leaving.key == entering.key:
        raise _error("invalid_payload", "Quem sai e quem entra precisam ser jogadores diferentes.", field="payload.player_in")
    _check_player(i, leaving, on_field=True)
    _check_incoming(i, entering)
    payload = {**leaving.named("player_out"), **entering.named("player_in")}
    if leaving.player_id is not None:
        payload["player_out_id"] = leaving.player_id
    if entering.player_id is not None:
        payload["player_in_id"] = entering.player_id
    changes = {
        "subbed_off": i.state.subbed_off | {leaving.key},
        "subbed_on": i.state.subbed_on | {entering.key},
    }
    return _Outcome(team, payload=payload, changes=changes)


def _carded(i: _Input) -> tuple[int, _PlayerRef]:
    team = _team(i)
    ref = _player(i.ctx, team, i.payload.get("player"), i.new.player_id, "payload.player")
    _check_player(i, ref, on_field=False)  # cartão vale para quem está no banco
    return team, ref


def _on_yellow_card(i: _Input) -> _Outcome:
    team, ref = _carded(i)
    count = i.state.yellow_cards.get(ref.key, 0) + 1
    second = count == 2 and ref.key not in i.state.sent_off
    changes = {"yellow_cards": {**i.state.yellow_cards, ref.key: count}}
    return _Outcome(team, ref.player_id, ref.named(), changes=changes, second_yellow=second)


def _on_red_card(i: _Input) -> _Outcome:
    team, ref = _carded(i)
    return _Outcome(team, ref.player_id, ref.named(), changes={"sent_off": i.state.sent_off | {ref.key}})


def _on_stoppage_time(i: _Input) -> _Outcome:
    minutes = i.payload.get("minutes")
    if isinstance(minutes, str) and minutes.strip().isdigit():
        minutes = int(minutes)
    if not _is_int(minutes) or not 1 <= minutes <= MAX_STOPPAGE:
        raise _error("invalid_payload", f"Acréscimos vão de 1 a {MAX_STOPPAGE} minutos.", field="payload.minutes")
    announced = {**i.state.stoppage_announced, str(i.period): minutes}
    return _Outcome(payload={"minutes": minutes}, changes={"stoppage_announced": announced})


def _on_shootout_kick(i: _Input) -> _Outcome:
    team = _team(i)
    taker = _player(i.ctx, team, i.payload.get("player"), i.new.player_id, "payload.player")
    scored = i.payload.get("scored")
    if isinstance(scored, str) and scored.strip().lower() in ("true", "false"):
        scored = scored.strip().lower() == "true"
    if not isinstance(scored, bool):
        raise _error("invalid_payload", "Informe se a cobrança foi convertida.", field="payload.scored")
    _check_player(i, taker, on_field=True)
    changes = {}
    if scored:
        side = _side(i.ctx, team, "penalties")
        changes[side] = (getattr(i.state, side) or 0) + 1
    return _Outcome(team, taker.player_id, {**taker.named(), "scored": scored}, changes=changes)


_GAME_HANDLERS: dict[str, Callable[[_Input], _Outcome]] = {
    EventType.GOAL: _on_goal,
    EventType.GOAL_ANNULLED: _on_goal_annulled,
    EventType.PENALTY_AWARDED: _on_penalty_awarded,
    EventType.PENALTY_MISSED: _on_penalty_missed,
    EventType.VAR_REVIEW: _on_var_review,
    EventType.SUBSTITUTION: _on_substitution,
    EventType.YELLOW_CARD: _on_yellow_card,
    EventType.RED_CARD: _on_red_card,
    EventType.STOPPAGE_TIME: _on_stoppage_time,
    EventType.SHOOTOUT_KICK: _on_shootout_kick,
}


def _shootout_minutes(ctx: MatchContext) -> tuple[int, ...]:
    """Minuto da disputa de pênaltis: o do início dela (120 com prorrogação, 90 sem)."""
    if ctx.tie is None:
        return (90, 120)
    return (120,) if ctx.tie.extra_time else (90,)


def _game_minute(new: NewEvent, period: str, ctx: MatchContext) -> tuple[int | None, int | None]:
    minute, stoppage = new.minute, _normalized_stoppage(new.stoppage)
    if minute is None:
        if stoppage is not None:
            raise _error("invalid_minute", "Acréscimo sem minuto.", stoppage=stoppage)
        if period in _CLOCK_PERIODS:
            raise _error("invalid_minute", "Informe o minuto do lance.", period=period)
        return None, None
    low, high, stoppage_at = _MINUTE_RULES[period]
    where = _PERIOD_IN[period].capitalize()
    if not _is_int(minute):
        raise _error("invalid_minute", "Minuto inválido.", minute=minute)
    if period == Period.PENALTIES:
        allowed = _shootout_minutes(ctx)
        if minute not in allowed or stoppage is not None:
            options = " ou ".join(f"{value}'" for value in allowed)
            raise _error(
                "invalid_minute", f"{where}, o minuto (se informado) é {options}.", minute=minute, period=period
            )
    elif not low <= minute <= high:
        span = f"é {low}" if low == high else f"vai de {low} a {high}"
        raise _error("invalid_minute", f"{where}, o minuto {span}.", minute=minute, period=period)
    elif stoppage is not None and minute not in stoppage_at:
        options = " ou ".join(f"{value}'" for value in stoppage_at)
        raise _error("invalid_minute", f"{where}, acréscimo só no minuto {options}.", minute=minute, stoppage=stoppage)
    return minute, stoppage


def _step_game(
    state: MatchState,
    new: NewEvent,
    spec: EventSpec,
    ctx: MatchContext,
    history: _History,
    sequence: int,
    collect: bool,
) -> _Step:
    if state.status != Status.LIVE:
        raise _error("match_not_live", "O jogo não está em andamento.", status=state.status, type=spec.type)
    period = state.period
    if period not in spec.periods:
        what = spec.label[0].lower() + spec.label[1:]
        raise _error(
            "invalid_period_for_event",
            f"Não é possível lançar {what} {_PERIOD_IN.get(period, 'neste período')}.",
            period=period,
            type=spec.type,
        )
    minute, stoppage = _game_minute(new, period, ctx)
    if new.player_id is not None:
        _optional_id(new.player_id, "player_id")
    i = _Input(state, new, ctx, history, _payload_of(new), period, collect)
    outcome = _GAME_HANDLERS[spec.type](i)
    changes = outcome.changes
    if minute is not None:
        clock = (PERIOD_ORDER[period], minute, stoppage or 0)
        last = state.last_clock
        warning = _minute_decreasing(state, period, minute, stoppage) if collect else None
        if warning:
            i.warnings.insert(0, warning)
        changes = {**changes, "last_clock": clock if last is None else max(clock, last)}
    event = Event(
        sequence,
        spec.type,
        period,
        minute,
        stoppage,
        outcome.team_id,
        outcome.player_id,
        outcome.payload,
        outcome.annuls_event_id,
    )
    return _Step(replace(state, **changes), event, tuple(i.warnings), outcome.second_yellow)
