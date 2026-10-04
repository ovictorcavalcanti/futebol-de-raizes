"""Leitura e serialização: dicionários prontos para JSON (docs/CONTRACT.md §3–4).

Funções puras de leitura (nada é gravado). Listas carregam os eventos de todas as
partidas numa consulta só; a home e a página da competição têm um número de
consultas fixo, que não cresce com o número de jogos.

Lançamentos cancelados nunca aparecem (só eventos visíveis). Horários em ISO
UTC com `Z` (`core.timeutils.iso_utc`); o dia segue o horário de Brasília.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Prefetch, Q, QuerySet, prefetch_related_objects
from django.db.models.functions import Coalesce

from competitions.models import Competition, Round, Stage
from core import timeutils
from core.timeutils import iso_utc
from realtime.outbox import current_cursor

from . import context, domain
from .domain import EventType, Status
from .models import Match, MatchEvent, MatchLineup, MatchLineupPlayer, MatchStat, Tie

log = logging.getLogger("fdr.selectors")

MATCH_RELATED = context.MATCH_RELATED
# Tipos que o MatchOut resumido usa: gols/anulações (placar, GoalOut), cartões,
# acréscimos (clock.stoppage_announced) e suspensão (clock.paused_at).
SUMMARY_EVENT_TYPES = (
    EventType.GOAL,
    EventType.GOAL_ANNULLED,
    EventType.YELLOW_CARD,
    EventType.RED_CARD,
    EventType.STOPPAGE_TIME,
    EventType.SUSPENDED,
    EventType.DELAYED,
)
DETAIL_PREFETCH = (
    Prefetch(
        "lineups",
        queryset=MatchLineup.objects.order_by("id").prefetch_related(
            Prefetch("entries", queryset=MatchLineupPlayer.objects.order_by("-starter", "order", "id"))
        ),
    ),
    "officials",
    "broadcasts",
    "stats",
)
LATEST_GOALS_LIMIT = 10
MATCHES_LIST_LIMIT = 500  # GET /api/matches sem filtro nenhum
# Jogo da véspera que passa da meia-noite fica na home até 2 h depois do fim.
OVERNIGHT_GRACE = timedelta(hours=2)
CLOCK_STATUSES = frozenset({Status.LIVE, Status.SUSPENDED})
CLOSED_STATUSES = (Status.FINISHED, Status.CANCELLED)
_STAT_ORDER = {key: index for index, key in enumerate(MatchStat.Key.values)}


def timezone_name() -> str:
    return settings.TIME_ZONE


def _stamp() -> dict:
    return {"server_time": iso_utc(timeutils.now()), "timezone": timezone_name()}


# --- Times, referências -----------------------------------------------------------


def serialize_team(team) -> dict:
    """TeamOut."""
    return {
        "id": team.id,
        "name": team.name,
        "short_name": team.short_name,
        "city": team.city,
        "color_primary": team.color_primary,
        "color_secondary": team.color_secondary,
        "crest_url": team.crest_src,
    }


def _competition_ref(competition) -> dict:
    return {"id": competition.id, "name": competition.name, "slug": competition.slug, "short_name": competition.short_name}


def _round_ref(round_) -> dict | None:
    if round_ is None:
        return None
    return {"id": round_.id, "number": round_.number, "name": round_.label}


def match_round(match):
    """Rodada da partida; no mata-mata sem rodada própria, a do confronto."""
    if match.round_id:
        return match.round
    if match.tie_id:
        return match.tie.round
    return None


def match_round_id(match) -> int | None:
    if match.round_id:
        return match.round_id
    return match.tie.round_id if match.tie_id else None


# Rodada efetiva nas consultas (mesma regra de `match_round`).
ROUND_EXPRESSION = Coalesce("round_id", "tie__round_id")


def round_filter(round_id: int) -> Q:
    """Partidas da rodada: a da partida ou, sem ela, a do confronto (mata-mata)."""
    return Q(round_id=round_id) | Q(round__isnull=True, tie__round_id=round_id)


def _side(match, team_id: int | None) -> str | None:
    if team_id is None:
        return None
    if team_id == match.home_team_id:
        return "home"
    if team_id == match.away_team_id:
        return "away"
    return None


# --- Linha do tempo -----------------------------------------------------------------


def chronological(rows: Iterable[MatchEvent]) -> list[MatchEvent]:
    """Lances na ordem do jogo (período, minuto, acréscimo), não na ordem em que foram
    lançados. Lance sem minuto (status, troca no intervalo) fica no instante do lance
    anterior a ele na sequência, ou no início do seu período; empate → sequência."""
    keyed = []
    last = (0, 0, 0)
    for row in sorted(rows, key=lambda row: row.sequence):
        order = domain.PERIOD_ORDER.get(row.period, 0) if row.period else last[0]
        if row.minute is not None:
            last = (order, row.minute, row.stoppage or 0)
        else:
            last = max(last, (order, 0, 0))
        keyed.append((last, row.sequence, row))
    keyed.sort(key=lambda item: item[:2])
    return [row for *_, row in keyed]


class Timeline:
    """Eventos visíveis de uma partida e o que a leitura deriva deles:
    gols anulados, placar depois de cada gol válido (na ordem do jogo)."""

    __slots__ = ("match", "rows", "ordered", "annulled", "score_after")

    def __init__(self, match, rows: Iterable[MatchEvent]):
        self.match = match
        self.rows = sorted((row for row in rows if row.voided_at is None), key=lambda row: row.sequence)
        self.ordered = chronological(self.rows)  # gol lançado com atraso entra no placar do seu minuto
        self.annulled = {
            row.annuls_event_id
            for row in self.rows
            if row.type == EventType.GOAL_ANNULLED and row.annuls_event_id is not None
        }
        self.score_after: dict[int, dict] = {}
        home = away = 0
        for row in self.ordered:
            if row.type != EventType.GOAL or row.id in self.annulled:
                continue
            if row.team_id == match.home_team_id:
                home += 1
            elif row.team_id == match.away_team_id:
                away += 1
            self.score_after[row.id] = {"home": home, "away": away}

    def valid_goals(self) -> list[MatchEvent]:
        return [row for row in self.ordered if row.id in self.score_after]


def _payload(row) -> dict:
    return row.payload if isinstance(row.payload, dict) else {}


def serialize_event(event: MatchEvent, match, *, timeline: Timeline | None = None) -> dict:
    """EventOut. `timeline` (eventos da partida) dá `annulled` e `score_after`;
    sem ela, os eventos visíveis da partida são lidos do banco."""
    if timeline is None:
        timeline = Timeline(match, MatchEvent.objects.filter(match_id=match.id, voided_at__isnull=True))
    payload = _payload(event)
    spec = domain.CATALOG.get(event.type)
    period = event.period
    domain_event = context.to_domain_event(event)
    return {
        "id": event.id,
        "sequence": event.sequence,
        "type": event.type,
        "type_label": spec.label if spec else event.type,
        "kind": spec.kind if spec else None,
        "icon": domain.event_icon(event.type, payload),
        "period": period,
        "period_label": domain.PERIOD_LABELS.get(period) if period else None,
        "period_short": domain.PERIOD_SHORT.get(period) if period else None,
        "minute": event.minute,
        "stoppage": event.stoppage,
        "minute_label": domain.format_minute(event.minute, event.stoppage),
        "team_id": event.team_id,
        "team_side": _side(match, event.team_id),
        "player": {"id": None, "name": payload.get("player")},  # jogador sem cadastro: só o nome
        "payload": dict(payload),
        "annuls_event_id": event.annuls_event_id,
        "annulled": event.type == EventType.GOAL and event.id in timeline.annulled,
        "derived": domain.derived_from(domain_event) is not None,
        "score_after": timeline.score_after.get(event.id) if event.voided_at is None else None,
        "created_at": iso_utc(event.created_at),
    }


def serialize_goal(event: MatchEvent, match, timeline: Timeline) -> dict:
    """GoalOut (resumo do gol no card)."""
    payload = _payload(event)
    return {
        "event_id": event.id,
        "match_id": match.id,
        "team_id": event.team_id,
        "team_side": _side(match, event.team_id),
        "player": payload.get("player"),
        "origin": payload.get("origin") or domain.GoalOrigin.OPEN_PLAY.value,
        "period": event.period,
        "minute": event.minute,
        "stoppage": event.stoppage,
        "minute_label": domain.format_minute(event.minute, event.stoppage),
        "score_after": timeline.score_after.get(event.id),
        "created_at": iso_utc(event.created_at),
    }


def serialize_latest_goal(event: MatchEvent, match, timeline: Timeline) -> dict:
    """LatestGoalOut = GoalOut + partida (competição, times) + time do gol."""
    goal = serialize_goal(event, match, timeline)
    home, away = serialize_team(match.home_team), serialize_team(match.away_team)
    goal["match"] = {
        "id": match.id,
        "competition": {"name": match.stage.season.competition.name, "slug": match.stage.season.competition.slug},
        "home": home,
        "away": away,
    }
    goal["team"] = home if event.team_id == match.home_team_id else away
    return goal


def goal_snapshots(match, rows: Iterable[MatchEvent]) -> dict[int, dict]:
    """Gols válidos da partida → LatestGoalOut, por id do evento (ordem de sequence).
    O caminho de escrita compara antes/depois para a mensagem `goals`."""
    timeline = Timeline(match, rows)
    return {row.id: serialize_latest_goal(row, match, timeline) for row in timeline.valid_goals()}


# --- Partida ------------------------------------------------------------------------


@dataclass(frozen=True)
class LegRow:
    """Placar de um jogo do confronto (para o agregado do TieOut)."""

    id: int
    tie_id: int
    leg: int | None
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    status: str


def _leg_rows(tie_ids: Iterable[int]) -> dict[int, list[LegRow]]:
    tie_ids = {tie_id for tie_id in tie_ids if tie_id}
    legs: dict[int, list[LegRow]] = defaultdict(list)
    if not tie_ids:
        return legs
    fields = ("id", "tie_id", "leg", "home_team_id", "away_team_id", "home_score", "away_score", "status")
    for values in Match.objects.filter(tie_id__in=tie_ids).order_by("leg", "id").values_list(*fields):
        row = LegRow(*values)
        legs[row.tie_id].append(row)
    return legs


def serialize_tie(tie, legs: Sequence, *, leg: int | None = None) -> dict:
    """TieOut (`leg` só quando embutido no MatchOut). `legs` = jogos do confronto
    (Match ou LegRow), para o agregado; jogo cancelado não conta."""
    total_a = total_b = 0
    for item in legs:
        if item.status == Status.CANCELLED:
            continue
        total_a += _goals_for(tie.team_a_id, item)
        total_b += _goals_for(tie.team_b_id, item)
    decided_by = tie.decided_by or None
    data = {"id": tie.id, "legs": tie.legs}
    if leg is not None:
        data["leg"] = leg
    data.update({
        "extra_time": tie.extra_time,
        "position": tie.position,
        "round": _round_ref(tie.round),
        "team_a": serialize_team(tie.team_a),
        "team_b": serialize_team(tie.team_b),
        "aggregate": {"team_a": total_a, "team_b": total_b},
        "winner_team_id": tie.winner_team_id,
        "decided_by": decided_by,
        "decided_by_label": domain.DECIDED_BY_LABELS.get(decided_by) if decided_by else None,
        "complete": tie.winner_team_id is not None,
    })
    return data


def _goals_for(team_id: int, leg) -> int:
    return (leg.home_score if leg.home_team_id == team_id else 0) + (leg.away_score if leg.away_team_id == team_id else 0)


def _clock(match, timeline: Timeline) -> dict | None:
    """Relógio do período (CONTRACT §3): null fora de 1T/2T/prorrogação em andamento.
    Suspenso: running=false e paused_at = horário da suspensão aberta (o front para o minuto)."""
    if match.status not in CLOCK_STATUSES or match.period not in domain.PERIOD_CLOCK:
        return None
    spec = domain.PERIOD_CLOCK[match.period]
    announced = None
    for row in timeline.rows:
        if row.type == EventType.STOPPAGE_TIME and row.period == match.period:
            minutes = _payload(row).get("minutes")
            announced = minutes if isinstance(minutes, int) else announced
    # Parado: suspensão ou relógio parado pelo operador (`clock_paused_at`, cache do serviço).
    return {
        "running": match.status == Status.LIVE and match.clock_paused_at is None,
        "offset": spec["offset"],
        "regular_end": spec["regular_end"],
        "stoppage_announced": announced,
        "paused_at": iso_utc(match.clock_paused_at),
    }


def _status_note(match, timeline: Timeline) -> str | None:
    """Observação do atraso (texto do operador), só com o jogo atrasado."""
    if match.status != Status.DELAYED:
        return None
    note = None
    for row in timeline.rows:
        if row.type == EventType.DELAYED:
            note = _payload(row).get("reason") or note
    return note


def match_winner(match) -> str | None:
    """"home" | "away" | "draw" (só encerrado; considera os pênaltis)."""
    if match.status != Status.FINISHED:
        return None
    if match.home_score != match.away_score:
        return "home" if match.home_score > match.away_score else "away"
    home_pen, away_pen = match.home_penalties, match.away_penalties
    if home_pen is not None and away_pen is not None and home_pen != away_pen:
        return "home" if home_pen > away_pen else "away"
    return "draw"


def _cards(match, timeline: Timeline) -> tuple[dict, list[dict]]:
    counts = {"home": {"yellow": 0, "red": 0}, "away": {"yellow": 0, "red": 0}}
    reds = []
    for row in timeline.rows:
        if row.type not in (EventType.YELLOW_CARD, EventType.RED_CARD):
            continue
        side = _side(match, row.team_id)
        if side is None:
            continue
        if row.type == EventType.YELLOW_CARD:
            counts[side]["yellow"] += 1
        else:
            counts[side]["red"] += 1
            reds.append({
                "team_side": side,
                "player": _payload(row).get("player"),
                "minute_label": domain.format_minute(row.minute, row.stoppage),
            })
    return counts, reds


def _lineup(lineup) -> dict:
    starters, substitutes = [], []
    for entry in lineup.entries.all():
        item = {"name": entry.name, "number": entry.number, "position": entry.position or None}
        (starters if entry.starter else substitutes).append(item)
    return {"formation": lineup.formation or None, "coach": lineup.coach or None, "starters": starters, "substitutes": substitutes}


def _detail(match, timeline: Timeline) -> dict:
    prefetch_related_objects([match], *DETAIL_PREFETCH)
    lineups = {"home": None, "away": None}
    for lineup in match.lineups.all():
        side = _side(match, lineup.team_id)
        if side:
            lineups[side] = _lineup(lineup)
    stats: dict[str, dict] = {}
    for stat in match.stats.all():
        side = _side(match, stat.team_id)
        if side is None:
            continue
        item = stats.setdefault(stat.key, {"key": stat.key, "label": stat.get_key_display(), "home": None, "away": None})
        item[side] = stat.value
    return {
        "events": [serialize_event(row, match, timeline=timeline) for row in timeline.ordered],
        "lineups": lineups,
        "officials": [
            {"role": item.role, "role_label": item.get_role_display(), "name": item.name, "state": item.state}
            for item in match.officials.all()
        ],
        "broadcasts": [
            {"name": item.name, "url": item.url, "kind": item.kind, "kind_label": item.get_kind_display()}
            for item in match.broadcasts.all()
        ],
        "stats": sorted(stats.values(), key=lambda item: _STAT_ORDER.get(item["key"], 99)),
        "attendance": match.attendance,
        "revenue_cents": match.revenue_cents,
    }


def serialize_match(match, detail: bool = False, events: Iterable[MatchEvent] | None = None, *, tie_legs: Sequence | None = None) -> dict:
    """MatchOut (resumido ou detalhe).

    `match` precisa das junções de MATCH_RELATED. `events`: eventos da partida
    (cancelados são filtrados); sem eles, são lidos do banco. `tie_legs`: jogos do
    confronto (para o agregado); sem eles, são lidos do banco.
    """
    if events is None:
        query = MatchEvent.objects.filter(match_id=match.id, voided_at__isnull=True)
        if not detail:
            query = query.filter(type__in=SUMMARY_EVENT_TYPES)
        events = query.order_by("sequence")
    timeline = Timeline(match, events)
    tie = None
    if match.tie_id:
        if tie_legs is None:
            tie_legs = _leg_rows([match.tie_id]).get(match.tie_id, [])
        tie = serialize_tie(match.tie, tie_legs, leg=match.leg)
    period = match.period
    cards, red_cards = _cards(match, timeline)
    data = {
        "id": match.id,
        "competition": _competition_ref(match.stage.season.competition),
        "stage": {"id": match.stage.id, "name": match.stage.name, "format": match.stage.format},
        "group": {"id": match.group.id, "name": match.group.name} if match.group_id else None,
        "round": _round_ref(match_round(match)),
        "kickoff_at": iso_utc(match.kickoff_at),
        "finished_at": iso_utc(match.finished_at),
        "venue": match.venue,
        "city": match.city,
        "status": match.status,
        "status_label": domain.STATUS_LABELS.get(match.status, match.status),
        "status_note": _status_note(match, timeline),
        "partial_info": match.partial_info,
        "period": period,
        "period_label": domain.PERIOD_LABELS.get(period) if period else None,
        "period_short": domain.PERIOD_SHORT.get(period) if period else None,
        "period_started_at": iso_utc(match.period_started_at),
        "clock": None if match.partial_info else _clock(match, timeline),
        "home": serialize_team(match.home_team),
        "away": serialize_team(match.away_team),
        "home_score": match.home_score,
        "away_score": match.away_score,
        "home_penalties": match.home_penalties,
        "away_penalties": match.away_penalties,
        "winner": match_winner(match),
        "version": match.version,
        "tie": tie,
        "goals": [serialize_goal(row, match, timeline) for row in timeline.valid_goals()],
        "cards": cards,
        "red_cards": red_cards,
    }
    if detail:
        data.update(_detail(match, timeline))
    return data


class _Batch:
    """Dados de várias partidas carregados de uma vez: eventos (uma consulta),
    jogos dos confrontos (uma consulta) e, no detalhe, o enriquecimento."""

    def __init__(self, matches: Sequence[Match], *, detail: bool = False):
        self.matches = list(matches)
        self.detail = detail
        ids = [match.id for match in self.matches]
        rows: dict[int, list[MatchEvent]] = defaultdict(list)
        if ids:
            query = MatchEvent.objects.filter(match_id__in=ids, voided_at__isnull=True)
            if not detail:
                query = query.filter(type__in=SUMMARY_EVENT_TYPES)
            for row in query.order_by("match_id", "sequence"):
                rows[row.match_id].append(row)
        self.rows = rows
        self.legs = _leg_rows(match.tie_id for match in self.matches)
        if detail and self.matches:
            prefetch_related_objects(self.matches, *DETAIL_PREFETCH)

    def serialize(self, match) -> dict:
        legs = self.legs.get(match.tie_id, []) if match.tie_id else None
        return serialize_match(match, self.detail, self.rows.get(match.id, []), tie_legs=legs)

    def timeline(self, match) -> Timeline:
        return Timeline(match, self.rows.get(match.id, []))

    def latest_goals(self, limit: int = LATEST_GOALS_LIMIT) -> list[dict]:
        """Os `limit` gols válidos mais recentes (created_at desc) destas partidas."""
        goals = []
        for match in self.matches:
            timeline = self.timeline(match)
            goals.extend((row, match, timeline) for row in timeline.valid_goals())
        return [serialize_latest_goal(row, match, timeline) for row, match, timeline in _latest_order(goals)[:limit]]


def _latest_order(goals: list[tuple]) -> list[tuple]:
    """Últimos gols, do mais novo ao mais antigo: entre jogos, pela hora do lançamento;
    no mesmo jogo, pelo minuto (gol lançado com atraso fica no lugar do seu minuto).
    Cada jogo mantém as posições que seus gols ocupam na ordem de lançamento."""
    goals = sorted(goals, key=lambda item: (item[0].created_at, item[0].id), reverse=True)
    by_match: dict[int, list[tuple]] = defaultdict(list)
    for item in goals:
        by_match[item[1].id].append(item)
    for items in by_match.values():
        clock = {row.id: n for n, row in enumerate(items[0][2].ordered)}  # ordem do jogo (Timeline)
        items.sort(key=lambda item: clock[item[0].id], reverse=True)
    return [by_match[item[1].id].pop(0) for item in goals]


def _with_related(matches) -> list[Match]:
    if isinstance(matches, QuerySet):
        return list(matches.select_related(*MATCH_RELATED))
    return list(matches)


def serialize_matches(matches, detail: bool = False) -> list[dict]:
    """Lista de MatchOut com UMA consulta de eventos para todas as partidas."""
    batch = _Batch(_with_related(matches), detail=detail)
    return [batch.serialize(match) for match in batch.matches]


# --- Estado para o operador ----------------------------------------------------------


def available_for(match, rows: Sequence[MatchEvent], *, legs: Sequence[Match] | None = None) -> dict:
    """Available (`domain.available_actions`) a partir dos eventos gravados."""
    if legs is None:
        legs = context.tie_legs(match)
    ctx = context.build_context(match, legs=legs)
    events = context.to_domain_events(rows)
    try:
        state = domain.derive_state(events, ctx)
    except domain.DomainError as exc:  # pragma: no cover - sequência gravada inconsistente
        log.warning("estado inválido na partida %s: %s", match.id, exc.code)
        return {"events": [], "status": []}
    return domain.available_actions(state, ctx, events)


def match_state(match, rows: Sequence[MatchEvent] | None = None) -> dict:
    """{"match": MatchOut(detalhe), "available": Available} — corpo comum das respostas
    do operador. `rows` = todos os eventos da partida (cancelados inclusive)."""
    if rows is None:
        rows = context.match_events(match)
    legs = context.tie_legs(match)
    return {"match": serialize_match(match, True, rows, tie_legs=legs), "available": available_for(match, rows, legs=legs)}


def match_detail(match_id: int) -> dict:
    """GET /api/matches/{id}. Levanta Match.DoesNotExist (404)."""
    cursor = current_cursor()
    match = Match.objects.select_related(*MATCH_RELATED).get(pk=match_id)
    return {**_stamp(), "cursor": cursor, **match_state(match)}


def post_payload(result) -> dict:
    """Corpo de POST /api/ops/matches/{id}/events a partir de `services.PostResult`."""
    match = result.match
    rows = context.match_events(match)
    timeline = Timeline(match, rows)
    by_id = {row.id: row for row in rows}
    event = by_id.get(result.event.id, result.event)
    derived = [by_id.get(row.id, row) for row in result.derived]
    return {
        "event": serialize_event(event, match, timeline=timeline),
        "derived": [serialize_event(row, match, timeline=timeline) for row in derived],
        **match_state(match, rows),
        "warnings": [{"code": warning.code, "message": warning.message} for warning in result.warnings],
        "replayed": not result.created,
    }


def status_payload(result) -> dict:
    """Corpo de POST /api/ops/matches/{id}/status a partir de `services.PostResult`."""
    payload = post_payload(result)
    return {"event": payload["event"], "match": payload["match"], "available": payload["available"], "replayed": payload["replayed"]}


def void_payload(outcome) -> dict:
    """Corpo de POST /api/ops/matches/{id}/events/{eventId}/void (`services.VoidOutcome`)."""
    return {"voided": list(outcome.voided_ids), **match_state(outcome.match), "already": outcome.already}


# --- Home ------------------------------------------------------------------------------


def _as_day(value) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timeutils.app_tz()).date()
    return timeutils.parse_day(str(value))


def day_matches_query(day: date, now: datetime) -> QuerySet:
    """Jogos do dia (Brasília): os que começam no dia e o jogo da véspera que passa
    da meia-noite — fica enquanto ao vivo/suspenso (no dia de hoje) ou até 2 h
    depois de `finished_at` (se terminou depois da meia-noite). Espelho em Python
    para uma partida só: `on_day` (mude os dois juntos)."""
    start, end = timeutils.day_bounds(day)
    previous_start = timeutils.day_bounds(day - timedelta(days=1))[0]
    overnight = Q(finished_at__gte=start, finished_at__gt=now - OVERNIGHT_GRACE)
    if day == timeutils.local_today(now):
        overnight |= Q(status__in=CLOCK_STATUSES)
    return Match.objects.filter(
        Q(kickoff_at__gte=start, kickoff_at__lt=end) | (Q(kickoff_at__gte=previous_start, kickoff_at__lt=start) & overnight)
    ).order_by("kickoff_at", "id")


def on_day(match, day: date, now: datetime, *, kickoff_at: datetime | None = None) -> bool:
    """A partida entra em `day_matches_query(day, now)`? Mesma regra, em Python, para uma
    partida só — com `kickoff_at` no lugar do gravado (ex.: o valor antigo de uma edição
    no admin, para saber se ela entrou ou saiu da home)."""
    kickoff = kickoff_at or match.kickoff_at
    start, end = timeutils.day_bounds(day)
    if start <= kickoff < end:
        return True
    previous_start = timeutils.day_bounds(day - timedelta(days=1))[0]
    if not previous_start <= kickoff < start:
        return False
    if day == timeutils.local_today(now) and match.status in CLOCK_STATUSES:
        return True
    finished = match.finished_at
    return finished is not None and finished >= start and finished > now - OVERNIGHT_GRACE


def latest_goals(day=None, now: datetime | None = None, limit: int = LATEST_GOALS_LIMIT) -> list[dict]:
    """Últimos gols válidos dos jogos do dia (LatestGoalOut), do mais novo ao mais antigo."""
    now = now or timeutils.now()
    day = _as_day(day) or timeutils.local_today(now)
    matches = list(day_matches_query(day, now).select_related(*MATCH_RELATED))
    if not matches:
        return []
    ids = [match.id for match in matches]
    rows: dict[int, list[MatchEvent]] = defaultdict(list)
    query = MatchEvent.objects.filter(
        match_id__in=ids, voided_at__isnull=True, type__in=(EventType.GOAL, EventType.GOAL_ANNULLED)
    ).order_by("match_id", "sequence")
    for row in query:
        rows[row.match_id].append(row)
    goals = []
    for match in matches:
        timeline = Timeline(match, rows.get(match.id, []))
        goals.extend((row, match, timeline) for row in timeline.valid_goals())
    return [serialize_latest_goal(row, match, timeline) for row, match, timeline in _latest_order(goals)[:limit]]


# Ordem dos jogos na home e na página da competição (static/js/match-card.js faz igual):
# com jogo ao vivo (ou atrasado, ou suspenso): ao vivo > encerrados > agendados; sem
# nenhum: agendados > encerrados. Adiados e cancelados por último; a hora desempata.
LIVE_GROUP = frozenset({Status.LIVE, Status.DELAYED, Status.SUSPENDED})
RANK_WITH_LIVE = {**dict.fromkeys(LIVE_GROUP, 0), Status.FINISHED: 1, Status.SCHEDULED: 2}
RANK_WITHOUT_LIVE = {Status.SCHEDULED: 0, Status.FINISHED: 1}


def sort_for_display(matches: list[dict]) -> list[dict]:
    """Ordena (no lugar) MatchOuts pela regra acima e devolve a mesma lista."""
    rank = RANK_WITH_LIVE if any(m["status"] in LIVE_GROUP for m in matches) else RANK_WITHOUT_LIVE
    matches.sort(key=lambda m: (rank.get(m["status"], 3), m["kickoff_at"] or "", m["id"]))
    return matches


def home_payload(day=None, now: datetime | None = None) -> dict:
    """HomeOut: jogos do dia por competição (em `position`), classificação ao vivo de
    cada fase com tabela e os 10 últimos gols válidos. `cursor` é lido ANTES do estado."""
    from standings.services import stages_standings

    cursor = current_cursor()
    now = now or timeutils.now()
    day = _as_day(day) or timeutils.local_today(now)
    batch = _Batch(list(day_matches_query(day, now).select_related(*MATCH_RELATED)))

    competitions: dict[int, dict] = {}
    stages: dict[int, dict] = {}
    stage_objects: dict[int, Stage] = {}
    for match in batch.matches:
        stage = match.stage
        competition = stage.season.competition
        entry = competitions.get(competition.id)
        if entry is None:
            entry = competitions[competition.id] = {
                "id": competition.id,
                "name": competition.name,
                "slug": competition.slug,
                "short_name": competition.short_name,
                "position": competition.position,
                "stages": [],
                "_order": (competition.position, competition.id),
            }
        block = stages.get(stage.id)
        if block is None:
            block = stages[stage.id] = {
                "id": stage.id,
                "name": stage.name,
                "format": stage.format,
                "matches": [],
                "standings": None,
                "_order": (stage.season.year, stage.position, stage.id),
            }
            stage_objects[stage.id] = stage
            entry["stages"].append(block)
        block["matches"].append(batch.serialize(match))

    with_table = [stage for stage in stage_objects.values() if stage.has_table]
    tables = stages_standings(with_table, live=True) if with_table else {}
    for stage_id, block in stages.items():
        block["standings"] = tables.get(stage_id)
    ordered = sorted(competitions.values(), key=lambda item: item.pop("_order"))
    for entry in ordered:
        entry["stages"].sort(key=lambda block: block["_order"])
        for block in entry["stages"]:
            block.pop("_order")
            sort_for_display(block["matches"])
    return {
        "date": day.isoformat(),
        **_stamp(),
        "cursor": cursor,
        "competitions": ordered,
        "latest_goals": batch.latest_goals(),
    }


# --- Competições ---------------------------------------------------------------------------


def competitions_menu() -> dict:
    """GET /api/competitions: competições em ordem, para o menu."""
    rows = Competition.objects.order_by("position", "id").values("id", "name", "slug", "short_name", "position")
    return {"competitions": list(rows)}


def _current(items: Sequence, is_open) -> Any:
    """Primeiro item com jogo ainda não encerrado; senão o último (None se vazio)."""
    for item in items:
        if is_open(item):
            return item
    return items[-1] if items else None


def serialize_tie_detail(tie, matches: Sequence[dict], legs: Sequence) -> dict:
    """TieDetailOut = TieOut sem `leg` + `matches` (MatchOut resumido)."""
    return {**serialize_tie(tie, legs), "matches": list(matches)}


def competition_payload(slug: str, stage_id: int | None = None, round_id: int | None = None) -> dict:
    """CompetitionOut. Fase e rodada exibidas: as pedidas ou as atuais (primeiras com
    jogo ainda não encerrado; senão as últimas). `current_stage_id`/`current_round_id`
    são as exibidas. Levanta Competition/Stage/Round.DoesNotExist (404)."""
    from standings.services import stage_standings

    cursor = current_cursor()
    competition = Competition.objects.get(slug=slug)
    season = competition.seasons.order_by("-year").first()
    stages: list[Stage] = []
    if season is not None:
        stages = list(
            Stage.objects.filter(season=season)
            .order_by("position", "id")
            .prefetch_related(Prefetch("rounds", queryset=Round.objects.order_by("number")))
        )
    open_pairs = set(
        Match.objects.filter(stage__in=stages)
        .exclude(status__in=CLOSED_STATUSES)
        .order_by()
        .values_list("stage_id", ROUND_EXPRESSION)
        .distinct()
    ) if stages else set()
    open_stages = {stage for stage, _ in open_pairs}

    by_id = {stage.id: stage for stage in stages}
    selected_round = None
    if round_id is not None:
        selected_round = next((rnd for stage in stages for rnd in stage.rounds.all() if rnd.id == round_id), None)
        if selected_round is None:
            raise Round.DoesNotExist(f"Rodada {round_id} não é desta competição.")
    if stage_id is not None:
        stage = by_id.get(stage_id)
        if stage is None:
            raise Stage.DoesNotExist(f"Fase {stage_id} não é desta competição.")
        if selected_round is not None and selected_round.stage_id != stage.id:
            raise Round.DoesNotExist(f"Rodada {round_id} não é desta fase.")
    elif selected_round is not None:
        stage = by_id[selected_round.stage_id]
    else:
        stage = _current(stages, lambda item: item.id in open_stages)

    data = {
        **_stamp(),
        "cursor": cursor,
        "competition": {
            "id": competition.id,
            "name": competition.name,
            "slug": competition.slug,
            "short_name": competition.short_name,
            "position": competition.position,
        },
        "season": {"id": season.id, "year": season.year} if season else None,
        "stages": [
            {
                "id": item.id,
                "name": item.name,
                "format": item.format,
                "position": item.position,
                "rounds": [_round_ref(rnd) for rnd in item.rounds.all()],
            }
            for item in stages
        ],
        "current_stage_id": stage.id if stage else None,
        "current_round_id": None,
        "stage": None,
    }
    if stage is None:
        return data

    rounds = list(stage.rounds.all())
    if selected_round is None:
        selected_round = _current(rounds, lambda rnd: (stage.id, rnd.id) in open_pairs)
    data["current_round_id"] = selected_round.id if selected_round else None

    matches_query = Match.objects.filter(stage=stage)
    ties: list[Tie] = []
    if stage.format == Stage.Format.KNOCKOUT:
        ties_query = Tie.objects.filter(stage=stage)
        if selected_round is not None:
            ties_query = ties_query.filter(round=selected_round)
        ties = list(ties_query.select_related("round", "team_a", "team_b").order_by("position", "id"))
    if selected_round is not None:
        matches_query = matches_query.filter(Q(round=selected_round) | Q(tie__in=ties))
    batch = _Batch(list(matches_query.select_related(*MATCH_RELATED).order_by("kickoff_at", "id")))
    serialized = {match.id: batch.serialize(match) for match in batch.matches}
    round_matches = [
        serialized[match.id]
        for match in batch.matches
        if selected_round is None or match_round_id(match) == selected_round.id
    ]
    tie_matches: dict[int, list[dict]] = defaultdict(list)
    for match in sorted(batch.matches, key=lambda item: (item.leg or 0, item.kickoff_at, item.id)):
        if match.tie_id:
            tie_matches[match.tie_id].append(serialized[match.id])
    data["stage"] = {
        "id": stage.id,
        "name": stage.name,
        "format": stage.format,
        "standings": stage_standings(stage, live=True) if stage.has_table else None,
        "matches": round_matches,
        "ties": [serialize_tie_detail(tie, tie_matches.get(tie.id, []), batch.legs.get(tie.id, [])) for tie in ties],
    }
    return data


def matches_list(round_id: int | None = None, date=None, status=None, stage_id: int | None = None) -> dict:
    """GET /api/matches. `date` = dia de Brasília (date ou "AAAA-MM-DD"); `status` aceita
    vários separados por vírgula. Sem filtro, no máximo MATCHES_LIST_LIMIT partidas.
    Status desconhecido ou data inválida → ValueError (a API responde 400)."""
    query = Match.objects.all()
    filtered = False
    if round_id is not None:
        query, filtered = query.filter(round_filter(round_id)), True
    if stage_id is not None:
        query, filtered = query.filter(stage_id=stage_id), True
    if date not in (None, ""):
        day = _as_day(date)
        start, end = timeutils.day_bounds(day)
        query, filtered = query.filter(kickoff_at__gte=start, kickoff_at__lt=end), True
    if status not in (None, ""):
        wanted = [item.strip() for item in (status.split(",") if isinstance(status, str) else status) if item.strip()]
        unknown = [item for item in wanted if item not in Match.Status.values]
        if unknown:
            raise ValueError(f"Status desconhecido: {', '.join(unknown)}.")
        query, filtered = query.filter(status__in=wanted), True
    query = query.order_by("kickoff_at", "id")
    if not filtered:
        query = query[:MATCHES_LIST_LIMIT]
    return {**_stamp(), "matches": serialize_matches(list(query.select_related(*MATCH_RELATED)))}


# --- Catálogo do operador -------------------------------------------------------------------


def _event_spec(spec: domain.EventSpec) -> dict:
    return {
        "type": spec.type,
        "label": spec.label,
        "kind": spec.kind,
        "icon": spec.icon,
        "minute": spec.minute,
        "periods": [str(period) for period in sorted(spec.periods, key=lambda period: domain.PERIOD_ORDER[period])],
        "fields": [
            {
                "name": item.name,
                "kind": item.kind,
                "label": item.label,
                "required": item.required,
                "choices": [[value, label] for value, label in item.choices],
            }
            for item in spec.fields
        ],
    }


def catalog_payload() -> dict:
    """GET /api/ops/catalog: tipos de evento (EventSpecOut), ações de status, períodos e status."""
    return {
        "events": [_event_spec(spec) for spec in domain.CATALOG.values()],
        "status_actions": [{"action": action.value, "label": label} for action, label in domain.STATUS_ACTION_LABELS.items()],
        "periods": [
            {"key": period.value, "label": domain.PERIOD_LABELS[period], "short": domain.PERIOD_SHORT[period]}
            for period in domain.Period
        ],
        "statuses": [{"key": status.value, "label": domain.STATUS_LABELS[status]} for status in domain.Status],
    }
