"""Ponte entre o ORM e o domínio puro da partida (matches/domain.py).

O domínio não importa Django: aqui as linhas do banco viram `domain.Event`,
`domain.MatchContext` (times, confronto com os outros jogos, escalações) e
`domain.TieInfo`/`TieLeg`. Usado pelo caminho de escrita (matches/services.py) e
pelas leituras que precisam do estado (matches/selectors.py → `available`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from . import domain
from .models import Match, MatchEvent, MatchLineupPlayer

# Junções de toda leitura/escrita de partida (MatchOut precisa de todas).
MATCH_RELATED = (
    "stage__season__competition",
    "group",
    "round",
    "tie__round",
    "tie__team_a",
    "tie__team_b",
    "home_team",
    "away_team",
)

# Status em que o jogo decisivo do confronto ainda não começou.
NOT_STARTED = frozenset({domain.Status.SCHEDULED, domain.Status.POSTPONED, domain.Status.CANCELLED})


def to_domain_event(row: MatchEvent) -> domain.Event:
    """Linha gravada → evento do domínio (cancelado vira `voided=True`)."""
    return domain.Event(
        sequence=row.sequence,
        type=row.type,
        period=row.period,
        minute=row.minute,
        stoppage=row.stoppage,
        team_id=row.team_id,
        player_id=row.player_id,
        payload=row.payload if isinstance(row.payload, dict) else {},
        annuls_event_id=row.annuls_event_id,
        id=row.id,
        voided=row.voided_at is not None,
        created_at=row.created_at,
    )


def to_domain_events(rows: Iterable[MatchEvent]) -> list[domain.Event]:
    return [to_domain_event(row) for row in rows]


def match_events(match: Match) -> list[MatchEvent]:
    """Todos os eventos da partida (inclusive cancelados), em ordem de sequence."""
    return list(MatchEvent.objects.filter(match=match).order_by("sequence"))


def tie_legs(match: Match) -> list[Match]:
    """Jogos do confronto da partida (ela inclusive), em ordem de jogo; [] fora do mata-mata."""
    if not match.tie_id:
        return []
    others = list(Match.objects.filter(tie_id=match.tie_id).exclude(pk=match.pk))
    return sorted([match, *others], key=lambda leg: (leg.leg or 0, leg.pk))


def tie_context(match: Match, legs: Sequence[Match]) -> domain.TieContext | None:
    """Confronto visto do jogo `match`, com o placar atual dos outros jogos (sem os cancelados)."""
    tie = match.tie if match.tie_id else None
    if tie is None:
        return None
    others = tuple(
        domain.LegScore(
            home_team_id=leg.home_team_id,
            away_team_id=leg.away_team_id,
            home_score=leg.home_score,
            away_score=leg.away_score,
            finished=leg.status == domain.Status.FINISHED,
        )
        for leg in legs
        if leg.pk != match.pk and leg.status != domain.Status.CANCELLED
    )
    return domain.TieContext(
        legs=tie.legs,
        extra_time=tie.extra_time,
        leg=match.leg or 1,
        team_a_id=tie.team_a_id,
        team_b_id=tie.team_b_id,
        other_legs=others,
    )


def lineups_for(match: Match) -> dict[int, tuple[domain.LineupPlayer, ...]]:
    """Escalações da partida por time (só times com jogadores escalados)."""
    entries = (
        MatchLineupPlayer.objects.filter(lineup__match=match)
        .select_related("lineup", "player")
        .order_by("lineup_id", "-starter", "order", "id")
    )
    lineups: dict[int, list[domain.LineupPlayer]] = defaultdict(list)
    for entry in entries:
        name = entry.display_name
        if not name and entry.player_id is None:
            continue
        lineups[entry.lineup.team_id].append(
            domain.LineupPlayer(name=name, starter=entry.starter, player_id=entry.player_id, number=entry.number)
        )
    return {team_id: tuple(players) for team_id, players in lineups.items()}


def build_context(match: Match, *, legs: Sequence[Match] | None = None) -> domain.MatchContext:
    """MatchContext da partida: times, confronto (com os outros jogos) e escalações."""
    if legs is None:
        legs = tie_legs(match)
    return domain.MatchContext(
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
        tie=tie_context(match, legs),
        lineups=lineups_for(match),
    )


def decisive_leg(legs: Sequence[Match], tie) -> Match | None:
    return next((leg for leg in legs if leg.leg == tie.legs), None)


def tie_leg_locked(match: Match, legs: Sequence[Match]) -> bool:
    """O jogo de ida está travado: o jogo decisivo (volta) já começou.

    O domínio refaz a volta com o placar atual da ida; corrigir a ida depois que a
    volta começou deixaria a sequência da volta inválida (ex.: prorrogação aceita
    com o agregado que não existe mais).
    """
    tie = match.tie if match.tie_id else None
    if tie is None or tie.legs < 2 or match.leg == tie.legs:
        return False
    decisive = decisive_leg(legs, tie)
    return decisive is not None and decisive.status not in NOT_STARTED


def tie_info(tie) -> domain.TieInfo:
    return domain.TieInfo(legs=tie.legs, extra_time=tie.extra_time, team_a_id=tie.team_a_id, team_b_id=tie.team_b_id)


def tie_leg(leg: Match) -> domain.TieLeg:
    return domain.TieLeg(
        leg=leg.leg or 1,
        home_team_id=leg.home_team_id,
        away_team_id=leg.away_team_id,
        status=leg.status,
        home_score=leg.home_score,
        away_score=leg.away_score,
        home_penalties=leg.home_penalties,
        away_penalties=leg.away_penalties,
    )
