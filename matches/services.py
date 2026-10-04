"""Caminho de escrita da partida: lançar evento, cancelar lançamento, mudar status.

Cada função roda numa única transação com a trava global de escrita
(`core.locks.locked_atomic`):

1. Replay idempotente: `(partida, chave)` já gravada → devolve o evento original e
   os derivados dele (`created=False`), sem gravar nada. A mesma chave com outro
   corpo continua sendo replay do original (a chave identifica o pedido, não o conteúdo).
2. Carrega a partida (com as junções) e os eventos; monta o `MatchContext`.
3. Chama o domínio (`derive_state` → `apply_event` / `check_void`).
4. Grava o evento e os derivados (sequence max + 1…, autor, origem, `created_at`).
5. Atualiza o cache da partida a partir do estado novo (`version += 1`).
6. Confronto: `compute_tie_result` e grava vencedor/forma da decisão no `Tie`.
7. Partida de grupo: recalcula a classificação (oficial e ao vivo) quando mudou
   status, placar ou cartão.
8. Outbox: `match` sempre; `standings` quando a classificação foi recalculada (item 7);
   `goals` quando o conjunto de gols válidos da partida mudou.
9. Auditoria. Métricas depois do commit.

`DomainError` sobe para a API (422) e é contado em `fdr_domain_rejections_total`;
nada é gravado. Partida inexistente → `Match.DoesNotExist` (404). Formato inválido
(chave de idempotência, origem) → `InvalidInput` (400).

`at` (datetime, padrão `core.timeutils.now()`) é o `created_at` gravado. Só scripts,
seed e testes o passam (para datar um jogo ao vivo e o relógio fazer sentido); a API nunca.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from competitions.models import Group
from core import timeutils
from core.locks import locked_atomic
from observability import audit
from observability.metrics import metrics
from realtime.outbox import enqueue
from standings import services as standings_services

from . import context, domain, selectors
from .domain import DomainError, DomainWarning, EventType, NewEvent, Status
from .models import Match, MatchEvent, Tie

log = logging.getLogger("fdr.services")

IDEMPOTENCY_KEY_MAX_LENGTH = 64  # sobra espaço para o sufixo ":auto:N" dos derivados (campo: 80)
DERIVED_KEY_MARK = ":auto:"  # chave dos derivados: f"{chave}:auto:{n}" (reservado: o cliente não usa)
SOURCES = frozenset(MatchEvent.Source.values)
CARD_TYPES = frozenset({EventType.YELLOW_CARD, EventType.RED_CARD})
# Campos da partida (Django Admin) que mudam a classificação ou o confronto.
TABLE_FIELDS = frozenset({"stage", "group", "home_team", "away_team"})
TIE_FIELDS = frozenset({"stage", "tie", "leg", "home_team", "away_team"})
CACHE_FIELDS = [
    "status",
    "period",
    "period_started_at",
    "home_score",
    "away_score",
    "home_penalties",
    "away_penalties",
    "finished_at",
    "version",
]


class InvalidInput(ValueError):
    """Formato inválido (a API responde 400 invalid_input)."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass
class PostResult:
    event: MatchEvent  # evento pedido (ou o original, em replay)
    derived: list[MatchEvent]  # ex.: vermelho automático
    match: Match  # já atualizado
    created: bool  # False quando a chave de idempotência já existia
    warnings: list[DomainWarning] = field(default_factory=list)


@dataclass
class VoidOutcome:
    match: Match
    voided_ids: list[int]  # o pedido primeiro, depois os que caíram junto
    already: bool  # o lançamento já estava cancelado: nada gravado, ids do cancelamento original


# --- API pública do módulo -------------------------------------------------------------


def post_event(
    match_id: int,
    user,
    new: NewEvent,
    *,
    idempotency_key: str,
    source: str = "operator",
    confirm: bool = False,
    request=None,
    at: datetime | None = None,
) -> PostResult:
    """Lança um evento (lance, estrutural ou status). Ver o docstring do módulo.

    Jogador não tem cadastro: id de jogador (`player_id`, `payload.player_id`,
    `payload.player_out_id`, `payload.player_in_id`) → `InvalidInput` (400); o nome vai em
    `payload.player` (substituição: `payload.player_out`/`payload.player_in`)."""
    _check_no_player_ids(new)
    return _counting_rejections(
        lambda: _post(match_id, user, lambda: new, key=idempotency_key, source=source, confirm=confirm, request=request, at=at, action=None)
    )


def change_status(
    match_id: int,
    user,
    action: str,
    *,
    idempotency_key: str,
    kickoff_at: datetime | str | None = None,
    reason: str = "",
    source: str = "operator",
    request=None,
    at: datetime | None = None,
) -> PostResult:
    """Adia, suspende, retoma, reagenda (`kickoff_at`; sem fuso = Brasília) ou cancela."""

    def build() -> NewEvent:
        return domain.status_action_event(action, kickoff_at=kickoff_at, reason=reason or None)

    return _counting_rejections(
        lambda: _post(match_id, user, build, key=idempotency_key, source=source, confirm=True, request=request, at=at, action=action)
    )


def void_event(match_id: int, event_id: int, user, *, reason: str = "", request=None, at: datetime | None = None) -> VoidOutcome:
    """Cancela um lançamento errado e o que cai junto (derivados, anulações).

    Lançamento já cancelado → `VoidOutcome(already=True)` com os ids daquele
    cancelamento, sem gravar nada (clique duplo é inofensivo). No confronto de ida
    e volta, com o jogo decisivo já começado, cancelar no outro jogo →
    `DomainError("tie_leg_locked")`.
    """
    return _counting_rejections(lambda: _void(match_id, event_id, user, reason=reason, request=request, at=at))


def on_match_edited(match: Match, changed_fields, user=None, request=None) -> None:
    """Partida salva no Django Admin.

    `changed_fields`: nomes dos campos alterados (ex.: `form.changed_data`) ou um
    dicionário {campo: valor antigo}. Com o valor antigo de `group`/`tie`, o grupo e
    o confronto antigos também são recalculados; só com os nomes, recalcula todos os
    grupos da fase atual da partida. Refaz o cache a partir dos eventos quando os
    times ou o confronto mudam, recalcula classificação/confronto, `version += 1`
    e publica `match` (+ `standings`) sob a trava. Início (`kickoff_at`) editado que
    põe ou tira de hoje uma partida com gols válidos → também `goals` (`changes` vazio,
    `latest_goals` refeito): os últimos gols da home não ficam velhos até recarregar.
    """
    previous = dict(changed_fields) if isinstance(changed_fields, Mapping) else {}
    names = {_field_name(name) for name in changed_fields}
    with locked_atomic():
        work = _Work.load(match.pk)
        match = work.match
        # O formulário do admin grava também os campos de cache com o valor de quando
        # a página abriu: refazer o cache pelos eventos desfaz um placar velho.
        work.refresh_from_events()
        match.version += 1
        match.save(update_fields=CACHE_FIELDS)

        stages = {match.stage_id: match.stage} if match.stage.has_table else {}
        if names & TABLE_FIELDS:
            groups = set()
            if match.group_id:
                groups.add(match.group_id)
            old_group = _pk(previous.get("group"))
            if old_group:
                groups.add(old_group)
            elif "group" in names or "stage" in names:
                groups.update(Group.objects.filter(stage_id=match.stage_id).values_list("id", flat=True))
            for group in Group.objects.filter(id__in=groups).select_related("stage"):
                standings_services.recompute_group(group)
                if group.stage.has_table:
                    stages[group.stage_id] = group.stage
        other_legs: list[Match] = []
        if names & TIE_FIELDS:
            # O confronto da partida pelo próprio `match.tie` (a mensagem `match` sai com
            # o vencedor novo); o confronto antigo, se a partida mudou de confronto.
            if match.tie_id and _recompute_tie(match.tie, legs=work.legs, current=match, current_rows=work.rows):
                other_legs.extend(leg for leg in work.legs if leg.pk != match.pk)
            old_tie = _pk(previous.get("tie"))
            if old_tie and old_tie != match.tie_id:
                tie = Tie.objects.filter(pk=old_tie).first()
                if tie is not None and _recompute_tie(tie):
                    other_legs.extend(Match.objects.filter(tie_id=old_tie).exclude(pk=match.pk))
        _bump_versions(other_legs)
        if user is not None:
            audit.record(
                "match.edit", actor=user, obj=match, match_id=match.id, data={"fields": sorted(names)}, request=request
            )
        work.enqueue_match()
        _enqueue_legs(other_legs)
        if names & TABLE_FIELDS:
            for stage in stages.values():
                enqueue("standings", standings_services.standings_message(stage))
        if "kickoff_at" in names and work.left_or_joined_today(previous.get("kickoff_at")):
            work.enqueue_latest_goals()


def publish_match(match: Match):
    """Publica a partida (`match`, detalhe) sem mudar nada — ex.: escalação, arbitragem
    ou estatísticas editadas no admin. Devolve a linha do outbox."""
    with locked_atomic():
        work = _Work.load(match.pk)
        return work.enqueue_match()


# --- Núcleo ---------------------------------------------------------------------------


def _counting_rejections(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except DomainError as exc:
        metrics.inc("fdr_domain_rejections_total", code=exc.code)
        raise


def _check_write(user, key: str | None, source: str | None) -> None:
    if user is None or getattr(user, "pk", None) is None:
        raise ValueError("Todo lançamento precisa de um autor (usuário salvo).")
    if key is not None:
        if not isinstance(key, str) or not key.strip():
            raise InvalidInput("idempotency_key", "Informe a chave de idempotência (Idempotency-Key).")
        if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            raise InvalidInput("idempotency_key", f"A chave de idempotência tem no máximo {IDEMPOTENCY_KEY_MAX_LENGTH} caracteres.")
        if DERIVED_KEY_MARK in key:
            # Reservado para os derivados: senão a chave do cliente colidiria com a do
            # vermelho automático de outro lançamento (replay errado ou erro de unicidade).
            raise InvalidInput("idempotency_key", f"A chave de idempotência não pode conter \"{DERIVED_KEY_MARK}\".")
    if source is not None and source not in SOURCES:
        raise InvalidInput("source", f"Origem inválida: use {', '.join(sorted(SOURCES))}.")


PLAYER_ID_KEYS = ("player_id", "player_out_id", "player_in_id")


def _check_no_player_ids(new: NewEvent) -> None:
    """Jogadores não têm cadastro: lançamento com id de jogador é recusado (400)."""
    message = "Jogadores não têm cadastro: informe o nome (payload.player; na substituição, payload.player_out e payload.player_in)."
    if new.player_id is not None:
        raise InvalidInput("player_id", message)
    payload = new.payload if isinstance(new.payload, Mapping) else {}
    for key in PLAYER_ID_KEYS:
        if key in payload:
            raise InvalidInput(f"payload.{key}", message)


def _replay(match_id: int, key: str) -> PostResult | None:
    original = MatchEvent.objects.filter(match_id=match_id, idempotency_key=key).first()
    if original is None:
        return None
    derived = list(
        MatchEvent.objects.filter(match_id=match_id, idempotency_key__startswith=f"{key}{DERIVED_KEY_MARK}").order_by("sequence")
    )
    match = Match.objects.select_related(*context.MATCH_RELATED).get(pk=match_id)
    return PostResult(event=original, derived=derived, match=match, created=False, warnings=[])


def _post(match_id, user, build: Callable[[], NewEvent], *, key, source, confirm, request, at, action) -> PostResult:
    _check_write(user, key, source)
    key = key.strip()
    with locked_atomic():
        replayed = _replay(match_id, key)
        if replayed is not None:
            return replayed
        # Horário lido já com a trava: `created_at` segue a ordem de `sequence`
        # (quem esperou a trava não grava um horário anterior ao do lançamento que passou antes).
        at = at or timeutils.now()
        work = _Work.load(match_id)
        new = build()
        events = work.domain_events()
        state = domain.derive_state(events, work.ctx)
        result = domain.apply_event(state, events, new, work.ctx, confirm=confirm)
        work.check_tie_lock(state, result.state, new.type)
        rows = work.persist(result, user=user, key=key, source=source, at=at, kickoff_field="kickoff_at" if action else "payload.kickoff_at")
        work.apply_state(result.state, rescheduled=rows[0] if rows[0].type == EventType.RESCHEDULED else None)
        work.finish(changed_types={row.type for row in rows}, created_ids={row.id for row in rows}, removed_reason="annulled")
        event = rows[0]
        data = {
            "type": event.type,
            "period": event.period,
            "minute": event.minute,
            "stoppage": event.stoppage,
            "team_id": event.team_id,
            "key": key,
            "source": source,
            "derived": [row.id for row in rows[1:]],
            "warnings": [warning.code for warning in result.warnings],
        }
        if action is not None:
            data["action"] = action
            if event.payload.get("kickoff_at"):
                data["kickoff_at"] = event.payload["kickoff_at"]
            if event.payload.get("reason"):
                data["reason"] = event.payload["reason"]
        audit.record(
            "match.status" if action is not None else "event.create",
            actor=user,
            obj=event,
            match_id=work.match.id,
            data=data,
            request=request,
        )
    if action is not None:
        metrics.inc("fdr_status_changes_total", action=action)
    else:
        for row in rows:
            metrics.inc("fdr_events_posted_total", type=row.type, source=row.source)
    return PostResult(event=rows[0], derived=rows[1:], match=work.match, created=True, warnings=list(result.warnings))


def _void(match_id, event_id, user, *, reason, request, at) -> VoidOutcome:
    _check_write(user, None, None)
    with locked_atomic():
        at = at or timeutils.now()  # com a trava (ver _post)
        work = _Work.load(match_id)
        target = next((row for row in work.rows if row.id == event_id), None)
        if target is not None and target.voided_at is not None:
            together = [
                row.id
                for row in work.rows
                if row.voided_at == target.voided_at and row.voided_by_id == target.voided_by_id and row.id != target.id
            ]
            return VoidOutcome(match=work.match, voided_ids=[target.id, *together], already=True)
        if target is not None and context.tie_leg_locked(work.match, work.legs):
            raise DomainError(
                "tie_leg_locked",
                "O jogo decisivo do confronto já começou: o jogo de ida não pode mais ser corrigido.",
                {"event_id": event_id, "tie_id": work.match.tie_id},
            )
        result = domain.check_void(work.domain_events(), event_id, work.ctx)
        voided_ids = list(result.voided_ids)
        MatchEvent.objects.filter(id__in=voided_ids).update(voided_at=at, voided_by=user)
        voided_rows = []
        for row in work.rows:
            if row.id in voided_ids:
                row.voided_at, row.voided_by = at, user
                voided_rows.append(row)
        work.apply_state(result.state, voided=voided_rows)
        work.finish(changed_types={row.type for row in voided_rows}, created_ids=set(), removed_reason="voided")
        target = next(row for row in voided_rows if row.id == event_id)
        audit.record(
            "event.void",
            actor=user,
            obj=target,
            match_id=work.match.id,
            data={"type": target.type, "minute": target.minute, "voided_ids": voided_ids, "reason": reason or ""},
            request=request,
        )
    metrics.inc("fdr_events_voided_total", amount=len(voided_ids))
    return VoidOutcome(match=work.match, voided_ids=voided_ids, already=False)


class _Work:
    """Uma escrita em andamento numa partida (dentro da trava)."""

    def __init__(self, match: Match):
        self.match = match
        self.rows: list[MatchEvent] = context.match_events(match)
        self.legs: list[Match] = context.tie_legs(match)
        self.ctx = context.build_context(match, legs=self.legs)
        self.before = (match.status, match.home_score, match.away_score)
        self.goals_before = selectors.goal_snapshots(match, self.rows)

    @classmethod
    def load(cls, match_id: int) -> _Work:
        match = Match.objects.select_related(*context.MATCH_RELATED).get(pk=match_id)
        if match.group_id:
            match.group.stage = match.stage  # recompute_group usa group.stage sem outra consulta
        return cls(match)

    def domain_events(self) -> list[domain.Event]:
        return context.to_domain_events(self.rows)

    def check_tie_lock(self, before: domain.MatchState, after: domain.MatchState, event_type: str) -> None:
        """No jogo de ida, com a volta já começada, nada que mude o placar da ida
        (o domínio refaz a volta com o placar atual da ida)."""
        cancelled = Status.CANCELLED
        changed = (before.home_score, before.away_score) != (after.home_score, after.away_score) or (
            (before.status == cancelled) != (after.status == cancelled)  # jogo cancelado sai do agregado
        )
        if changed and context.tie_leg_locked(self.match, self.legs):
            raise DomainError(
                "tie_leg_locked",
                "O jogo decisivo do confronto já começou: o placar do jogo de ida não pode mais mudar.",
                {"tie_id": self.match.tie_id, "type": event_type},
            )

    # --- gravação ---

    def persist(
        self, result: domain.ApplyResult, *, user, key: str, source: str, at: datetime, kickoff_field: str = "kickoff_at"
    ) -> list[MatchEvent]:
        rows = []
        for index, event in enumerate((result.event, *result.derived)):
            payload = dict(event.payload)
            if event.type == EventType.RESCHEDULED:
                payload["kickoff_at"] = _kickoff_utc(payload["kickoff_at"], kickoff_field)
                payload["previous_kickoff_at"] = timeutils.iso_utc(self.match.kickoff_at)
            rows.append(
                MatchEvent(
                    match=self.match,
                    sequence=event.sequence,
                    type=str(event.type),
                    period=_text(event.period),
                    minute=event.minute,
                    stoppage=event.stoppage,
                    team_id=event.team_id,
                    payload=payload,
                    annuls_event_id=event.annuls_event_id,
                    idempotency_key=key if index == 0 else f"{key}{DERIVED_KEY_MARK}{index}",
                    source=source if index == 0 else MatchEvent.Source.SYSTEM,
                    created_by=user,
                    created_at=at,
                )
            )
        MatchEvent.objects.bulk_create(rows)
        self.rows.extend(rows)
        return rows

    def apply_state(self, state: domain.MatchState, *, rescheduled: MatchEvent | None = None, voided: Sequence[MatchEvent] = ()) -> None:
        """Cache da partida a partir do estado (status, período, relógio, placar,
        pênaltis, fim, início reagendado) e `version += 1`."""
        match = self.match
        by_seq = {row.sequence: row for row in self.rows}
        match.status = str(state.status)
        match.period = _text(state.period)
        match.period_started_at = _period_started_at(state, by_seq)
        match.home_score = state.home_score
        match.away_score = state.away_score
        match.home_penalties = state.home_penalties
        match.away_penalties = state.away_penalties
        match.finished_at = self._finished_at(state)
        fields = list(CACHE_FIELDS)
        kickoff = _kickoff_after(state, rescheduled, voided)
        if kickoff is not None:
            match.kickoff_at = kickoff
            fields.append("kickoff_at")  # só quando muda: não sobrescreve edição do admin
        match.version += 1
        match.save(update_fields=fields)

    def _finished_at(self, state: domain.MatchState) -> datetime | None:
        if state.status != Status.FINISHED:
            return None
        ends = [row for row in self.rows if row.voided_at is None and row.type == EventType.MATCH_END]
        return max(ends, key=lambda row: row.sequence).created_at if ends else self.match.finished_at

    def refresh_from_events(self) -> None:
        """Refaz o cache a partir dos eventos (times/confronto editados no admin)."""
        self.ctx = context.build_context(self.match, legs=self.legs)
        try:
            state = domain.derive_state(self.domain_events(), self.ctx)
        except DomainError as exc:
            log.warning(
                "partida %s editada deixou os eventos inconsistentes (%s): cache mantido",
                self.match.id,
                exc.code,
                extra={"match_id": self.match.id, "code": exc.code},
            )
            return
        match = self.match
        by_seq = {row.sequence: row for row in self.rows}
        match.status, match.period = str(state.status), _text(state.period)
        match.period_started_at = _period_started_at(state, by_seq)
        match.home_score, match.away_score = state.home_score, state.away_score
        match.home_penalties, match.away_penalties = state.home_penalties, state.away_penalties
        match.finished_at = self._finished_at(state)

    # --- consequências ---

    def finish(self, *, changed_types: set[str], created_ids: set[int], removed_reason: str) -> None:
        """Confronto, classificação e outbox depois de gravar."""
        match = self.match
        other_legs: list[Match] = []
        if match.tie_id and _recompute_tie(match.tie, legs=self.legs, current=match, current_rows=self.rows):
            other_legs = [leg for leg in self.legs if leg.pk != match.pk]
            _bump_versions(other_legs)
        after = (match.status, match.home_score, match.away_score)
        # A tabela (e o `playing`, que só depende do status) só muda com status, placar
        # ou cartão: sem recálculo, a mensagem `standings` seria idêntica à anterior.
        recomputed = bool(match.group_id) and (after != self.before or bool(changed_types & CARD_TYPES))
        if recomputed:
            standings_services.recompute_group(match.group)

        self.enqueue_match()
        _enqueue_legs(other_legs)
        if recomputed:
            enqueue("standings", standings_services.standings_message(match.stage))
        self.enqueue_goals(created_ids, removed_reason)

    def enqueue_match(self):
        return enqueue("match", _match_message(self.match, self.rows, self.legs))

    def left_or_joined_today(self, old_kickoff) -> bool:
        """Início editado no admin: a partida (com gols válidos) entrou ou saiu dos jogos de
        hoje? Então os últimos gols da home mudaram. Sem o valor antigo, considera que sim."""
        if not selectors.goal_snapshots(self.match, self.rows):
            return False
        now = timeutils.now()
        today = timeutils.local_today(now)
        if old_kickoff in (None, ""):
            return True
        was_today = selectors.on_day(self.match, today, now, kickoff_at=_aware(old_kickoff))
        return was_today != selectors.on_day(self.match, today, now)

    def enqueue_latest_goals(self, changes: Sequence[dict] = ()) -> None:
        """Mensagem `goals` com os últimos gols de hoje (`changes` vazio: só a lista mudou)."""
        now = timeutils.now()
        today = timeutils.local_today(now)
        enqueue("goals", {"date": today.isoformat(), "changes": list(changes), "latest_goals": selectors.latest_goals(today, now)})

    def enqueue_goals(self, created_ids: set[int], removed_reason: str) -> None:
        after = selectors.goal_snapshots(self.match, self.rows)
        changes = []
        for event_id, goal in after.items():
            if event_id not in self.goals_before:
                added = event_id in created_ids
                changes.append({"kind": "added" if added else "restored", "reason": None if added else "unvoided", "goal": goal})
        for event_id, goal in self.goals_before.items():
            if event_id not in after:
                changes.append({"kind": "removed", "reason": removed_reason, "goal": goal})
        if changes:
            self.enqueue_latest_goals(changes)


def _match_message(match: Match, rows: Iterable[MatchEvent], legs: Sequence[Match]) -> dict:
    return {
        "stage_id": match.stage_id,
        "competition_id": match.stage.season.competition_id,
        "match": selectors.serialize_match(match, True, rows, tie_legs=legs or None),
    }


def _bump_versions(legs: Sequence[Match]) -> None:
    """Outros jogos de um confronto cujo resultado mudou: `version += 1` (o TieOut deles mudou)."""
    for leg in legs:
        leg.version += 1
        leg.save(update_fields=["version"])


def _enqueue_legs(legs: Sequence[Match]) -> None:
    """Mensagem `match` (detalhe) de cada jogo, relido com as junções e o confronto já gravado."""
    for leg in legs:
        full = Match.objects.select_related(*context.MATCH_RELATED).get(pk=leg.pk)
        enqueue("match", _match_message(full, context.match_events(full), context.tie_legs(full)))


def _recompute_tie(tie: Tie, *, legs: Sequence[Match] | None = None, current: Match | None = None, current_rows=None) -> bool:
    """Apura o confronto e grava vencedor/forma da decisão (limpa quando deixa de estar
    completo). Devolve True se mudou."""
    if legs is None:
        legs = list(Match.objects.filter(tie=tie).order_by("leg", "id"))
    legs = [current if current is not None and leg.pk == current.pk else leg for leg in legs]
    decisive = context.decisive_leg(legs, tie)
    types: set[str] = set()
    all_finished = all(any(leg.leg == number and leg.status == Status.FINISHED for leg in legs) for number in range(1, tie.legs + 1))
    if decisive is not None and all_finished:
        if current is not None and decisive.pk == current.pk and current_rows is not None:
            types = {row.type for row in current_rows if row.voided_at is None}
        else:
            types = set(
                MatchEvent.objects.filter(match=decisive, voided_at__isnull=True).values_list("type", flat=True).distinct()
            )
    result = domain.compute_tie_result(context.tie_info(tie), [context.tie_leg(leg) for leg in legs if leg.leg], types)
    winner = result.winner_team_id if result.complete else None
    decided_by = (result.decided_by or "") if result.complete else ""
    if (tie.winner_team_id, tie.decided_by) == (winner, decided_by):
        return False
    tie.winner_team_id = winner
    tie.decided_by = decided_by
    tie.save(update_fields=["winner_team", "decided_by"])
    return True


def _kickoff_after(state: domain.MatchState, rescheduled: MatchEvent | None, voided: Sequence[MatchEvent]) -> datetime | None:
    """Novo início da partida: o do reagendamento lançado agora; ao cancelar
    reagendamento(s), o do último reagendamento que sobrou ou, sem nenhum, o
    `previous_kickoff_at` guardado no mais antigo dos cancelados. None = não muda."""
    if rescheduled is not None:
        return _aware(rescheduled.payload["kickoff_at"])
    dropped = sorted((row for row in voided if row.type == EventType.RESCHEDULED), key=lambda row: row.sequence)
    if not dropped:
        return None
    if state.rescheduled_to:
        return _aware(state.rescheduled_to)
    previous = dropped[0].payload.get("previous_kickoff_at")
    return _aware(previous) if previous else None


def _period_started_at(state: domain.MatchState, by_seq: Mapping[int, MatchEvent]) -> datetime | None:
    """Abertura do período + tempo parado nas suspensões já retomadas (o relógio do
    front desconta a parada). Suspensão aberta não entra: o front para no `paused_at`."""
    if state.period_started_seq is None or state.period_started_seq not in by_seq:
        return None
    started = by_seq[state.period_started_seq].created_at
    for suspended_seq, resumed_seq in state.period_pauses:
        if resumed_seq is None or suspended_seq not in by_seq or resumed_seq not in by_seq:
            continue
        started += by_seq[resumed_seq].created_at - by_seq[suspended_seq].created_at
    return started


def _kickoff_utc(value: str | datetime, field_name: str) -> str:
    """Novo início (sem fuso = Brasília) em ISO UTC com Z. Data que não cabe em UTC
    (ex.: 9999-12-31T23:59-03:00) → `invalid_payload`, não um erro 500."""
    try:
        return timeutils.iso_utc(_aware(value))
    except (OverflowError, ValueError) as exc:
        raise DomainError(
            "invalid_payload", "Data e hora fora do intervalo aceito.", {"field": field_name}
        ) from exc


def _aware(value: str | datetime) -> datetime:
    """ISO 8601 (ou datetime) → datetime com fuso; sem fuso = horário de Brasília."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timeutils.app_tz())
    return moment


def _text(value) -> str | None:
    """Enum do domínio (StrEnum) → str simples para o ORM/JSON."""
    return str(value) if value is not None else None


def _field_name(name: str) -> str:
    return name[:-3] if name.endswith("_id") else name


def _pk(value) -> int | None:
    if value is None or value == "":
        return None
    return getattr(value, "pk", value)


__all__ = [
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "InvalidInput",
    "PostResult",
    "VoidOutcome",
    "change_status",
    "on_match_edited",
    "post_event",
    "publish_match",
    "void_event",
]
