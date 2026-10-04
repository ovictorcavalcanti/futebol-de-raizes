"""Classificação: recalcular o cache (`Standing`) e ler a tabela pronta para a API.

`recompute_group` refaz as duas visões (oficial e ao vivo) de um grupo a partir das
partidas, dos cartões visíveis e das punições/bonificações da fase (`PointAdjustment`);
roda dentro da transação do lançamento. A leitura
(`stage_standings`) devolve o StageStandingsOut do CONTRACT: critérios na ordem
configurada, legenda, zona de cada linha e quem está em campo agora (`playing`).

Fase com critérios inválidos gravados (ex.: chave fora do catálogo inserida por
fora do admin) não derruba o lançamento: vale a lista padrão de critérios
(`Rules().criteria`, já validada) e fica um aviso no log.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence

from django.db.models import Count, Q

from competitions.models import Group, GroupTeam, Stage, StageCriterion, StandingZone, Team
from core.locks import locked_atomic
from matches.domain import EventType
from matches.models import Match, MatchEvent
from realtime.outbox import enqueue

from . import domain
from .domain import CRITERIA, ConfigError, Rules, TeamEntry, Zone, compute_standings, validate_rules, zone_for
from .models import PointAdjustment, Ranking, Standing

log = logging.getLogger("fdr.standings")

COUNTED_STATUSES = tuple(sorted(domain.LIVE_STATUSES))  # finished, live, suspended
PLAYING_STATUSES = (Match.Status.LIVE, Match.Status.SUSPENDED)
CARD_TYPES = (EventType.YELLOW_CARD, EventType.RED_CARD)
DEFAULT_CRITERIA = Rules().criteria


# --- Regras da fase ------------------------------------------------------------------


def _points(stage: Stage) -> tuple[int, int, int]:
    return stage.points_win, stage.points_draw, stage.points_loss


def stage_zones(stage: Stage) -> list[Zone]:
    return [
        Zone(zone.name, zone.color, zone.position_from, zone.position_to)
        for zone in stage.zones.order_by("position_from", "id")
    ]


def stage_rules(stage: Stage, criteria: Sequence[str] | None = None) -> Rules:
    """Pontuação e critérios da fase. Critérios inválidos (vazios, repetidos ou fora do
    catálogo) → critérios padrão, com aviso no log (o lançamento não pode cair)."""
    if criteria is None:
        criteria = list(stage.criteria.order_by("position").values_list("key", flat=True))
    keys = tuple(criteria)
    try:
        validate_rules(_points(stage), keys, ())
    except ConfigError as exc:
        log.warning(
            "critérios inválidos na fase %s (%s): usando os critérios padrão",
            stage.pk,
            exc.code,
            extra={"stage_id": stage.pk, "code": exc.code},
        )
        keys = DEFAULT_CRITERIA
    return Rules(points_win=stage.points_win, points_draw=stage.points_draw, points_loss=stage.points_loss, criteria=keys)


def validate_stage(stage: Stage) -> None:
    """Valida pontuação, critérios e zonas gravados da fase (levanta ConfigError)."""
    criteria = list(stage.criteria.order_by("position").values_list("key", flat=True))
    validate_rules(_points(stage), criteria, stage_zones(stage))


def stage_adjustments(stage_id: int) -> dict[int, int]:
    """{team_id: soma dos pontos ajustados} da fase (punição < 0, bonificação > 0)."""
    totals: dict[int, int] = defaultdict(int)
    for team_id, points in PointAdjustment.objects.filter(stage_id=stage_id).values_list("team_id", "points"):
        totals[team_id] += points
    return dict(totals)


# --- Recalcular ----------------------------------------------------------------------


def _results(query) -> list[domain.MatchResult]:
    """Resultados (placar, status e cartões) das partidas de `query` (contadas na tabela)."""
    matches = list(query.values_list("id", "home_team_id", "away_team_id", "home_score", "away_score", "status"))
    cards: dict[tuple[int, int, str], int] = {}
    if matches:
        rows = (
            MatchEvent.objects.filter(
                match_id__in=[row[0] for row in matches],
                voided_at__isnull=True,
                type__in=CARD_TYPES,
                team__isnull=False,
            )
            .values("match_id", "team_id", "type")
            .annotate(total=Count("id"))
            .order_by()
        )
        cards = {(row["match_id"], row["team_id"], row["type"]): row["total"] for row in rows}
    return [
        domain.MatchResult(
            home_team_id=home,
            away_team_id=away,
            home_score=home_score,
            away_score=away_score,
            status=status,
            home_yellow=cards.get((match_id, home, EventType.YELLOW_CARD), 0),
            away_yellow=cards.get((match_id, away, EventType.YELLOW_CARD), 0),
            home_red=cards.get((match_id, home, EventType.RED_CARD), 0),
            away_red=cards.get((match_id, away, EventType.RED_CARD), 0),
        )
        for match_id, home, away, home_score, away_score, status in matches
    ]


def _group_inputs(group: Group, teams: Sequence[GroupTeam]) -> tuple[list[TeamEntry], list[domain.MatchResult]]:
    entries = [TeamEntry(item.team_id, item.team.name, item.lot_order) for item in teams]
    team_ids = [item.team_id for item in teams]
    # Os jogos dos times do grupo na fase, contra qualquer adversário: jogo entre grupos
    # (ex.: Copa do Nordeste) conta para os dois grupos.
    group_matches = Match.objects.filter(stage_id=group.stage_id, status__in=COUNTED_STATUSES).filter(
        Q(group=group) | Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids)
    )
    return entries, _results(group_matches)


def compute_group(
    group: Group,
    *,
    rules: Rules | None = None,
    teams: Sequence[GroupTeam] | None = None,
    adjustments: dict[int, int] | None = None,
) -> dict[str, list[domain.Row]]:
    """Linhas das duas visões do grupo, sem gravar: {"official": [...], "live": [...]}."""
    if rules is None:
        rules = stage_rules(group.stage)
    if teams is None:
        teams = list(GroupTeam.objects.filter(group=group).select_related("team"))
    if adjustments is None:
        adjustments = stage_adjustments(group.stage_id)
    entries, results = _group_inputs(group, teams)
    return {
        Standing.Kind.OFFICIAL: compute_standings(entries, results, rules, live=False, adjustments=adjustments),
        Standing.Kind.LIVE: compute_standings(entries, results, rules, live=True, adjustments=adjustments),
    }


def groups_of_match(match) -> list[Group]:
    """Grupos cuja tabela o jogo afeta: o do jogo e os dos dois times na fase (jogo entre
    grupos conta para os dois)."""
    if not match.stage.has_table:
        return []
    return list(
        Group.objects.filter(stage_id=match.stage_id)
        .filter(Q(id=match.group_id) | Q(group_teams__team_id__in=[match.home_team_id, match.away_team_id]))
        .select_related("stage")
        .distinct()
    )


def recompute_group(group: Group) -> dict[str, list[domain.Row]]:
    """Refaz o cache do grupo (as duas visões): apaga e regrava as linhas.
    Chamado dentro da transação de escrita (mesmo commit do lançamento)."""
    tables = compute_group(group)
    Standing.objects.filter(group=group).delete()
    Standing.objects.bulk_create(
        [
            Standing(
                group=group,
                team_id=row.team_id,
                kind=kind,
                position=row.position,
                played=row.played,
                won=row.won,
                drawn=row.drawn,
                lost=row.lost,
                goals_for=row.goals_for,
                goals_against=row.goals_against,
                points=row.points,
                adjustment=row.adjustment,
                yellow_cards=row.yellow_cards,
                red_cards=row.red_cards,
                tied=row.tied,
            )
            for kind, rows in tables.items()
            for row in rows
        ]
    )
    return tables


def recompute_stage(stage: Stage) -> None:
    """Refaz o cache de todos os grupos da fase."""
    for group in Group.objects.filter(stage=stage).select_related("stage"):
        recompute_group(group)


def on_stage_rules_changed(stage: Stage, recalc: bool = True) -> None:
    """Fase salva no admin. Valida as regras (ConfigError), recalcula os grupos
    quando pontuação, critérios ou punições/bonificações mudaram (`recalc`) e publica
    a classificação nova (mudar só zona ou cor não recalcula, mas publica). Mata-mata
    não tem tabela: nada a fazer."""
    if not stage.has_table:
        return
    validate_stage(stage)
    with locked_atomic():
        if recalc:
            recompute_stage(stage)
        enqueue("standings", standings_message(stage))


def standings_message(stage: Stage) -> dict:
    """Mensagem `standings` do stream: {"stage_id", "standings": StageStandingsOut(ao vivo)}."""
    return {"stage_id": stage.id, "standings": stage_standings(stage, live=True)}


# --- Ler ------------------------------------------------------------------------------


def _row_out(row, team, zones: Sequence[Zone], playing: set[int], qualified: dict | None = None) -> dict:
    """Linha da tabela. `qualified` = {(da, até): {times}} das zonas condicionais: quem está
    na faixa da zona mas fora desse conjunto fica sem a cor."""
    from matches.selectors import serialize_team

    zone = zone_for(row.position, zones)
    if zone is not None and qualified and (zone.position_from, zone.position_to) in qualified:
        if row.team_id not in qualified[(zone.position_from, zone.position_to)]:
            zone = None
    return {
        "position": row.position,
        "team": serialize_team(team),
        "played": row.played,
        "won": row.won,
        "drawn": row.drawn,
        "lost": row.lost,
        "goals_for": row.goals_for,
        "goals_against": row.goals_against,
        "goal_difference": row.goals_for - row.goals_against,
        "points": row.points,
        "points_adjustment": row.adjustment,
        "yellow_cards": row.yellow_cards,
        "red_cards": row.red_cards,
        "tied": row.tied,
        "zone": {"name": zone.name, "color": zone.color} if zone else None,
        "playing": row.team_id in playing,
    }


def stages_standings(stages: Iterable[Stage], live: bool = True, conditional: bool = True) -> dict[int, dict]:
    """StageStandingsOut de várias fases com um número fixo de consultas
    (critérios, zonas, grupos, times, linhas e jogos em andamento). `conditional=False`
    ignora as zonas condicionais (usado pela própria classificação de que elas dependem)."""
    stages = [stage for stage in stages]
    if not stages:
        return {}
    ids = [stage.id for stage in stages]
    kind = Standing.Kind.LIVE.value if live else Standing.Kind.OFFICIAL.value

    criteria: dict[int, list[str]] = defaultdict(list)
    for stage_id, key in StageCriterion.objects.filter(stage_id__in=ids).order_by("stage_id", "position").values_list("stage_id", "key"):
        criteria[stage_id].append(key)
    zones: dict[int, list[Zone]] = defaultdict(list)
    conditions: dict[int, list[StandingZone]] = defaultdict(list)
    for zone in StandingZone.objects.filter(stage_id__in=ids).select_related("ranking").order_by("stage_id", "position_from", "id"):
        zones[zone.stage_id].append(Zone(zone.name, zone.color, zone.position_from, zone.position_to))
        if conditional and zone.ranking_id:
            conditions[zone.stage_id].append(zone)
    groups: dict[int, list[Group]] = defaultdict(list)
    for group in Group.objects.filter(stage_id__in=ids).order_by("stage_id", "name", "id"):
        groups[group.stage_id].append(group)
    members: dict[int, list[GroupTeam]] = defaultdict(list)
    for item in GroupTeam.objects.filter(group__stage_id__in=ids).select_related("team"):
        members[item.group_id].append(item)
    cached: dict[int, list[Standing]] = defaultdict(list)
    for row in Standing.objects.filter(group__stage_id__in=ids, kind=kind).select_related("team").order_by("group_id", "position"):
        cached[row.group_id].append(row)
    adjustments: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    adjustment_rows: dict[int, list[PointAdjustment]] = defaultdict(list)
    for item in PointAdjustment.objects.filter(stage_id__in=ids).select_related("team").order_by("stage_id", "created_at", "id"):
        adjustments[item.stage_id][item.team_id] += item.points
        adjustment_rows[item.stage_id].append(item)
    playing: dict[int, set[int]] = defaultdict(set)
    for stage_id, home, away in Match.objects.filter(stage_id__in=ids, status__in=PLAYING_STATUSES).values_list(
        "stage_id", "home_team_id", "away_team_id"
    ):
        playing[stage_id].update((home, away))

    from matches.selectors import serialize_team

    result = {}
    for stage in stages:
        rules = stage_rules(stage, criteria.get(stage.id, []))
        stage_zones_ = zones.get(stage.id, [])
        stage_adjustments_ = dict(adjustments.get(stage.id, {}))
        qualified = _conditional_zones(conditions.get(stage.id, []), live)
        conditions_text = {
            (z.position_from, z.position_to): f"só quem estiver do {z.ranking_position_from}º ao {z.ranking_position_to}º em “{z.ranking.name}”"
            for z in conditions.get(stage.id, [])
        }
        out_groups = []
        for group in groups.get(stage.id, []):
            teams = members.get(group.id, [])
            rows = cached.get(group.id, [])
            stale = {row.team_id for row in rows} != {item.team_id for item in teams} or any(
                row.adjustment != stage_adjustments_.get(row.team_id, 0) for row in rows
            )
            if stale:
                # Sem cache (ou cache de outro elenco ou de outros ajustes): calcula na hora, sem gravar.
                group.stage = stage
                team_map = {item.team_id: item.team for item in teams}
                computed = compute_group(group, rules=rules, teams=teams, adjustments=stage_adjustments_)[kind]
                rows_out = [_row_out(row, team_map[row.team_id], stage_zones_, playing[stage.id], qualified) for row in computed]
            else:
                rows_out = [_row_out(row, row.team, stage_zones_, playing[stage.id], qualified) for row in rows]
            out_groups.append({"id": group.id, "name": group.name, "rows": rows_out})
        result[stage.id] = {
            "stage_id": stage.id,
            "stage_name": stage.name,
            "kind": kind,
            "points": {"win": stage.points_win, "draw": stage.points_draw, "loss": stage.points_loss},
            "criteria": [{"key": key, "label": CRITERIA[key].label} for key in rules.criteria],
            "legend": [
                {
                    "name": zone.name, "color": zone.color, "from": zone.position_from, "to": zone.position_to,
                    # só nas zonas condicionais: "só quem estiver do 1º ao 4º em “Melhores terceiros”"
                    **({"condition": conditions_text[key]} if (key := (zone.position_from, zone.position_to)) in conditions_text else {}),
                }
                for zone in stage_zones_
            ],
            "groups": out_groups,
            "adjustments": [
                {"team": serialize_team(item.team), "points": item.points, "reason": item.reason}
                for item in adjustment_rows.get(stage.id, [])
            ],
        }
    return result


def stage_standings(stage: Stage, live: bool = True) -> dict:
    """StageStandingsOut da fase (ao vivo por padrão; `live=False` = oficial)."""
    return stages_standings([stage], live=live)[stage.id]


# --- Classificações gerais e personalizadas (Ranking) ---------------------------------


def ranking_rules(ranking: Ranking) -> Rules:
    keys = [item.key for item in sorted(ranking.criteria.all(), key=lambda item: item.position)]
    return Rules(
        points_win=ranking.points_win,
        points_draw=ranking.points_draw,
        points_loss=ranking.points_loss,
        criteria=tuple(keys) or DEFAULT_CRITERIA,
    )


def _conditional_zones(zones: Sequence[StandingZone], live: bool) -> dict[tuple[int, int], set[int]]:
    """{(da, até): times dentro da faixa da classificação} de cada zona condicional."""
    out: dict[tuple[int, int], set[int]] = {}
    for zone in zones:
        ranking = Ranking.objects.prefetch_related("criteria", "zones").get(pk=zone.ranking_id)
        rows = ranking_standings(ranking, live=live)["groups"][0]["rows"]
        low, high = zone.ranking_position_from or 1, zone.ranking_position_to or len(rows)
        out[(zone.position_from, zone.position_to)] = {row["team"]["id"] for row in rows if low <= row["position"] <= high}
    return out


def _position_inputs(ranking: Ranking, stage_ids: list[int], live: bool):
    """Posição nos grupos: quem está na posição N de cada grupo da (única) fase e os jogos
    que contam (sem os jogos contra quem passa do tamanho do menor grupo, se pedido)."""
    stage = Stage.objects.filter(id__in=stage_ids).first()
    if stage is None or not ranking.group_position:
        return [], {}, Match.objects.none()
    table = stages_standings([stage], live=live, conditional=False)[stage.id]
    groups = [group for group in table["groups"] if group["rows"]]
    smallest = min((len(group["rows"]) for group in groups), default=0)
    chosen, group_of, extra = [], {}, set()
    for group in groups:
        for row in group["rows"]:
            if row["position"] == ranking.group_position:
                chosen.append(row["team"]["id"])
                group_of[row["team"]["id"]] = group["name"]
            if ranking.skip_extra_teams and row["position"] > smallest:
                extra.add(row["team"]["id"])
    extra -= set(chosen)
    matches = Match.objects.filter(stage=stage, status__in=COUNTED_STATUSES)
    if extra:
        matches = matches.exclude(home_team_id__in=extra).exclude(away_team_id__in=extra)
    return chosen, group_of, matches


def ranking_standings(ranking: Ranking, live: bool = True) -> dict:
    """RankingStandingsOut: mesmo formato de StageStandingsOut (um grupo só), calculado
    na hora. Geral: todos os times das fases marcadas (tabelas e mata-mata); personalizada:
    só os times escolhidos. Somam todos os jogos dessas fases (mata-mata inclusive) e as
    punições/bonificações delas."""
    from matches.selectors import serialize_team

    stage_ids = list(ranking.stages.order_by("position", "id").values_list("id", flat=True))
    group_of: dict[int, str] = {}
    matches = Match.objects.filter(stage_id__in=stage_ids, status__in=COUNTED_STATUSES)
    if ranking.scope == Ranking.Scope.POSITION:
        chosen, group_of, matches = _position_inputs(ranking, stage_ids, live)
        teams = list(Team.objects.filter(id__in=chosen))
    elif ranking.scope == Ranking.Scope.CUSTOM:
        teams = list(ranking.teams.all())
    else:
        ids = set(GroupTeam.objects.filter(group__stage_id__in=stage_ids).values_list("team_id", flat=True))
        for home, away in Match.objects.filter(stage_id__in=stage_ids).values_list("home_team_id", "away_team_id"):
            ids.update((home, away))
        teams = list(Team.objects.filter(id__in=ids))
    by_id = {team.id: team for team in teams}
    entries = [TeamEntry(team.id, team.name, None) for team in teams]
    # O confronto direto (se estiver nos critérios) só olha jogos entre os times da tabela.
    results = _results(matches)
    adjustments: dict[int, int] = defaultdict(int)
    for team_id, points in PointAdjustment.objects.filter(stage_id__in=stage_ids, team_id__in=by_id).values_list("team_id", "points"):
        adjustments[team_id] += points
    rules = ranking_rules(ranking)
    zones = [Zone(z.name, z.color, z.position_from, z.position_to) for z in ranking.zones.order_by("position_from", "id")]
    playing = set()
    for home, away in Match.objects.filter(stage_id__in=stage_ids, status__in=PLAYING_STATUSES).values_list("home_team_id", "away_team_id"):
        playing.update((home, away))
    rows = compute_standings(entries, results, rules, live=live, adjustments=dict(adjustments))
    kind = Standing.Kind.LIVE.value if live else Standing.Kind.OFFICIAL.value
    return {
        "ranking_id": ranking.id,
        "stage_id": None,
        "stage_name": ranking.name,
        "scope": ranking.scope,
        "stage_ids": stage_ids,
        "kind": kind,
        "points": {"win": rules.points_win, "draw": rules.points_draw, "loss": rules.points_loss},
        "criteria": [{"key": key, "label": CRITERIA[key].label} for key in rules.criteria],
        "legend": [{"name": z.name, "color": z.color, "from": z.position_from, "to": z.position_to} for z in zones],
        "groups": [{"id": None, "name": ranking.name, "rows": [
            {**_row_out(row, by_id[row.team_id], zones, playing), "group": group_of.get(row.team_id)} for row in rows
        ]}],
        "group_position": ranking.group_position if ranking.scope == Ranking.Scope.POSITION else None,
        "adjustments": [
            {"team": serialize_team(by_id[team_id]), "points": points, "reason": ""}
            for team_id, points in adjustments.items()
            if points
        ],
    }
