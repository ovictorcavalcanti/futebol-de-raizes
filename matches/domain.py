"""Regras da partida em funções puras (sem Django).

CONTRATO — os tipos e assinaturas abaixo são usados pelos serviços (matches/services.py),
pela API e pelos testes. Implementações marcadas com NotImplementedError serão
preenchidas mantendo exatamente estas assinaturas.

Convenções
----------
* Status (6): scheduled, live, finished, postponed, suspended, cancelled.
* Período (5): first_half, half_time, second_half, extra_time, penalties.
  `period` só existe com status live ou suspended (em suspenso, guarda onde parou).
* Minuto é ABSOLUTO no jogo (45+2 = minute 45, stoppage 2; 90+3; 105+1; 120+2).
  Faixas: 1T 0–45 (acréscimo só no 45); 2T 45–90 (acréscimo só no 90);
  prorrogação 90–120 (acréscimo só no 105 ou 120). Fora disso: `invalid_minute`.
* `team_id` do gol é o time BENEFICIADO; em gol contra, o jogador é do adversário.
* Jogador no MVP: `payload["player"]` (nome). Com escalação (fase 10) pode vir
  também `player_id`. Substituição usa `payload["player_out"]` e `payload["player_in"]`
  (e opcionalmente `player_out_id`/`player_in_id`).
* Período gravado no evento: eventos que ABREM período levam o período novo
  (match_start→first_half, second_half_start→second_half, extra_time_start→extra_time,
  penalties_start→penalties); half_time e match_end levam o período que FECHAM;
  eventos de jogo levam o período corrente; eventos de status levam o período
  corrente (ou None fora de jogo).
* Gol válido: tipo goal, não cancelado (voided) e não anulado por um goal_annulled
  ainda válido (não cancelado). Cobrança da disputa (shootout_kick) não é gol.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


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

# Relógio do período para o front desenhar o minuto ao vivo sem regra de negócio:
# minuto exibido = offset + minutos decorridos desde period_started_at (+1);
# passou de regular_end → "regular_end+N".
PERIOD_CLOCK = {
    Period.FIRST_HALF: {"offset": 0, "regular_end": 45},
    Period.SECOND_HALF: {"offset": 45, "regular_end": 90},
    Period.EXTRA_TIME: {"offset": 90, "regular_end": 120},
}


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


# Preenchido pela implementação: um EventSpec por EventType, com rótulos em PT-BR.
CATALOG: dict[str, EventSpec] = {}


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
    # minutos de acréscimo anunciados por período
    stoppage_announced: Mapping[str, int] = field(default_factory=dict)
    # nova data/hora pedida no último reagendamento (ISO 8601), se houver
    rescheduled_to: str | None = None
    # (ordem do período, minuto, acréscimo) do último evento de jogo com minuto
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


def player_key(team_id: int | None, player_id: int | None = None, name: str | None = None) -> str:
    """Chave estável de um jogador: por id, senão por nome normalizado (sem acento/caixa)."""
    raise NotImplementedError


def format_minute(minute: int | None, stoppage: int | None = None) -> str:
    """"45+2'", "90'", "" quando sem minuto."""
    raise NotImplementedError


def visible_events(events: Iterable[Event]) -> list[Event]:
    """Remove lançamentos cancelados; ordena por sequence."""
    raise NotImplementedError


def valid_goals(events: Iterable[Event]) -> list[Event]:
    """Gols válidos, em ordem de sequence."""
    raise NotImplementedError


def score(events: Iterable[Event], ctx: MatchContext) -> tuple[int, int]:
    """Placar (mandante, visitante) contando só gols válidos."""
    raise NotImplementedError


def derive_state(events: Iterable[Event], ctx: MatchContext) -> MatchState:
    """Refaz o estado a partir dos eventos visíveis (fold de apply_event sem
    regras brandas). Levanta DomainError se a sequência for inconsistente."""
    raise NotImplementedError


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
      invalid_status_action).
    * Regras brandas → avisos. Sem `confirm`, avisos viram
      DomainError("confirmation_required", warnings=...). Com `confirm`, o
      lançamento é aceito e os avisos voltam em ApplyResult.warnings.
      Avisos: minute_decreasing, player_sent_off, player_not_in_lineup,
      player_not_on_field, substitute_already_used.
    * `events` são os eventos já gravados (podem incluir cancelados; o domínio
      filtra). `next_sequence` default = maior sequence + 1.
    * 2º amarelo do mesmo jogador → `derived` traz o vermelho automático
      (payload {"player": ..., "reason": "second_yellow"}), sequence seguinte.
    """
    raise NotImplementedError


def check_void(events: Sequence[Event], event_id: int, ctx: MatchContext) -> VoidResult:
    """Cancela um lançamento errado (sem apagar).

    Caem junto: eventos derivados dele (payload["derived_from"] == event_id) e
    anulações que apontam para ele. O estado é refeito sem eles; se a sequência
    restante ficar inválida → DomainError("void_breaks_sequence").
    Erros: event_not_found, already_voided.
    """
    raise NotImplementedError


def available_actions(state: MatchState, ctx: MatchContext, events: Sequence[Event] = ()) -> dict:
    """O que o operador pode lançar agora: {"events": [tipos], "status": [ações]}.

    O front do operador só desenha botões a partir daqui (sem regra própria).
    """
    raise NotImplementedError


def status_action_event(action: str, *, kickoff_at: str | None = None, reason: str | None = None) -> NewEvent:
    """Converte a ação de status (/status) no evento correspondente."""
    raise NotImplementedError


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

    1. Soma os gols de cada time em todos os jogos do confronto.
    2. Todos os jogos encerrados e agregado diferente → avança quem fez mais.
    3. Agregado igual → vence a disputa de pênaltis do último jogo. Gol fora não vale.
    4. decided_by sai dos eventos do jogo decisivo: com penalties_start →
       "penalties"; só com extra_time_start → "extra_time"; senão "aggregate".
    """
    raise NotImplementedError
