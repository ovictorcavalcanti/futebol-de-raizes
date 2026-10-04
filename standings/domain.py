"""Classificação em funções puras (sem Django).

CONTRATO — usado por standings/services.py, admin e API. As assinaturas abaixo
são estáveis.

`compute_standings` recebe os times e as partidas do grupo e as regras da fase,
e devolve as linhas em ordem. Pontuação, critérios e legenda são dados da fase.

1. Considera as partidas encerradas; na visão ao vivo (live=True), também as em
   andamento (status live ou suspended).
2. Soma jogos, vitórias, empates, derrotas, gols pró, gols contra e cartões.
3. Calcula os pontos com a pontuação da fase e soma o ajuste do time
   (`adjustments`: punição negativa ou bonificação positiva, cadastrada na fase).
   Os pontos ajustados valem para o critério "points" e para a ordem; o confronto
   direto continua só com os resultados dos jogos.
4. Aplica os critérios na ordem configurada; cada um separa só os times que o
   anterior deixou empatados. Cada critério é uma função que recebe o BLOCO de
   times ainda empatados e devolve um valor por time (maior = melhor). Assim o
   confronto direto olha só os jogos entre os empatados (2 ou mais times).
5. Se o empate sobrar, ordena pelo nome e marca as linhas com tied=True.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby, pairwise
from types import MappingProxyType

NO_ADJUSTMENTS: Mapping[int, int] = MappingProxyType({})


@dataclass(frozen=True)
class TeamEntry:
    team_id: int
    name: str
    lot_order: int | None = None


@dataclass(frozen=True)
class MatchResult:
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    status: str  # "finished" | "live" | "suspended" | outros (ignorados)
    home_yellow: int = 0
    away_yellow: int = 0
    home_red: int = 0
    away_red: int = 0


@dataclass(frozen=True)
class Rules:
    points_win: int = 3
    points_draw: int = 1
    points_loss: int = 0
    criteria: tuple[str, ...] = ("points", "wins", "goal_difference", "goals_for", "head_to_head")


@dataclass(frozen=True)
class Row:
    team_id: int
    name: str
    position: int
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    points: int
    yellow_cards: int
    red_cards: int
    tied: bool = False
    adjustment: int = 0  # já somado em `points` (punição < 0, bonificação > 0)

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against


@dataclass(frozen=True)
class Zone:
    name: str
    color: str  # #RRGGBB
    position_from: int
    position_to: int


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str  # nome de exibição, mostrado pelas páginas na ordem configurada
    description: str
    # (bloco de linhas parciais empatadas, contexto) -> {team_id: valor}; maior = melhor
    fn: Callable[[Sequence["Row"], "CriterionContext"], Mapping[int, float]] | None = None


@dataclass(frozen=True)
class CriterionContext:
    matches: tuple[MatchResult, ...]  # partidas consideradas na visão pedida
    teams: Mapping[int, TeamEntry]
    rules: Rules


class ConfigError(ValueError):
    """Configuração de pontuação/critérios/zonas inválida."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- Visões --------------------------------------------------------------------

OFFICIAL_STATUSES: frozenset[str] = frozenset({"finished"})
LIVE_STATUSES: frozenset[str] = frozenset({"finished", "live", "suspended"})

_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def name_key(name: str) -> str:
    """Chave de ordenação por nome: sem acento e sem caixa ("Íbis" antes de "Jaguar")."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def match_points(home_score: int, away_score: int, rules: Rules) -> tuple[int, int]:
    """Pontos de (mandante, visitante) num placar, pela pontuação da fase."""
    if home_score > away_score:
        return rules.points_win, rules.points_loss
    if home_score < away_score:
        return rules.points_loss, rules.points_win
    return rules.points_draw, rules.points_draw


# --- Critérios -----------------------------------------------------------------
# Cada função recebe só o bloco ainda empatado e devolve {team_id: valor}, maior = melhor.


def _by_points(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: row.points for row in block}


def _by_wins(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: row.won for row in block}


def _by_goal_difference(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: row.goal_difference for row in block}


def _by_goals_for(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: row.goals_for for row in block}


def _by_head_to_head(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    """Pontos (pela pontuação da fase) só nos jogos entre os times do bloco."""
    points: dict[int, float] = {row.team_id: 0 for row in block}
    for match in ctx.matches:
        if match.home_team_id in points and match.away_team_id in points:
            home, away = match_points(match.home_score, match.away_score, ctx.rules)
            points[match.home_team_id] += home
            points[match.away_team_id] += away
    return points


def _by_fewer_red_cards(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: -row.red_cards for row in block}


def _by_fewer_yellow_cards(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    return {row.team_id: -row.yellow_cards for row in block}


def _by_drawing_of_lots(block: Sequence[Row], ctx: CriterionContext) -> dict[int, float]:
    """Ordem do sorteio (1 = melhor). Só separa o bloco quando TODOS os times dele
    têm `lot_order`; com algum vazio, o sorteio ainda não foi feito e nada muda."""
    orders: dict[int, float] = {}
    for row in block:
        order = ctx.teams[row.team_id].lot_order
        if order is None:
            return {row.team_id: 0 for row in block}
        orders[row.team_id] = -order
    return orders


# Catálogo: points, wins, goal_difference, goals_for, head_to_head,
# fewer_red_cards, fewer_yellow_cards, drawing_of_lots. Rótulos em PT-BR.
# As chaves e rótulos abaixo são fixos.
CRITERIA: dict[str, Criterion] = {
    "points": Criterion("points", "Pontos", "Mais pontos, pela pontuação da fase", _by_points),
    "wins": Criterion("wins", "Vitórias", "Mais vitórias", _by_wins),
    "goal_difference": Criterion("goal_difference", "Saldo de gols", "Maior saldo de gols", _by_goal_difference),
    "goals_for": Criterion("goals_for", "Gols pró", "Mais gols pró", _by_goals_for),
    "head_to_head": Criterion("head_to_head", "Confronto direto", "Mais pontos nos jogos entre os times empatados", _by_head_to_head),
    "fewer_red_cards": Criterion("fewer_red_cards", "Menos cartões vermelhos", "Menos cartões vermelhos, contados pelos eventos", _by_fewer_red_cards),
    "fewer_yellow_cards": Criterion("fewer_yellow_cards", "Menos cartões amarelos", "Menos cartões amarelos, contados pelos eventos", _by_fewer_yellow_cards),
    "drawing_of_lots": Criterion("drawing_of_lots", "Sorteio", "Ordem do sorteio", _by_drawing_of_lots),
}


# --- Classificação -------------------------------------------------------------


class _Totals:
    """Acumulador mutável de um time (evita recriar Row a cada partida)."""

    __slots__ = ("played", "won", "drawn", "lost", "goals_for", "goals_against", "yellow", "red")

    def __init__(self) -> None:
        self.played = self.won = self.drawn = self.lost = 0
        self.goals_for = self.goals_against = self.yellow = self.red = 0

    def add(self, scored: int, conceded: int, yellow: int, red: int) -> None:
        self.played += 1
        self.goals_for += scored
        self.goals_against += conceded
        self.yellow += yellow
        self.red += red
        if scored > conceded:
            self.won += 1
        elif scored < conceded:
            self.lost += 1
        else:
            self.drawn += 1


def _criterion_fn(key: str) -> Callable[[Sequence[Row], CriterionContext], Mapping[int, float]]:
    criterion = CRITERIA.get(key)
    if criterion is None or criterion.fn is None:
        raise ConfigError("criterion_unknown", f"Critério de desempate desconhecido: {key}.")
    return criterion.fn


def _split(blocks: list[list[Row]], fn: Callable[[Sequence[Row], CriterionContext], Mapping[int, float]], ctx: CriterionContext) -> list[list[Row]]:
    """Separa cada bloco empatado pelo valor do critério (maior primeiro), mantendo a ordem dos blocos."""
    result: list[list[Row]] = []
    for block in blocks:
        if len(block) < 2:
            result.append(block)
            continue
        values = fn(block, ctx)
        ordered = sorted(block, key=lambda row: values[row.team_id], reverse=True)  # estável
        result.extend(list(same) for _, same in groupby(ordered, key=lambda row: values[row.team_id]))
    return result


def compute_standings(
    teams: Sequence[TeamEntry],
    matches: Sequence[MatchResult],
    rules: Rules,
    *,
    live: bool = False,
    adjustments: Mapping[int, int] = NO_ADJUSTMENTS,
) -> list[Row]:
    """Linhas da classificação, já ordenadas e com posições 1..n.

    `adjustments` = {team_id: pontos} somados aos pontos dos jogos (negativo = punição);
    time fora de `teams` é ignorado. Todo time de `teams` ganha uma linha, mesmo sem jogos.
    Partida contra time fora de `teams` (ex.: de outro grupo, como na Copa do Nordeste)
    conta só para o time de `teams`; sem nenhum dos dois (ou de um time contra ele mesmo),
    é ignorada. Critério fora do catálogo levanta ConfigError("criterion_unknown").
    """
    # Resolve os critérios antes de tudo: configuração inválida falha sempre, mesmo
    # num grupo sem times ou quando o empate já se desfez antes do critério ruim.
    criterion_fns = [_criterion_fn(key) for key in rules.criteria]
    team_map: dict[int, TeamEntry] = {team.team_id: team for team in teams}
    counted = LIVE_STATUSES if live else OFFICIAL_STATUSES
    considered = tuple(
        match
        for match in matches
        if match.status in counted
        and match.home_team_id != match.away_team_id
        and (match.home_team_id in team_map or match.away_team_id in team_map)
    )

    totals = {team_id: _Totals() for team_id in team_map}
    for match in considered:
        if match.home_team_id in totals:
            totals[match.home_team_id].add(match.home_score, match.away_score, match.home_yellow, match.home_red)
        if match.away_team_id in totals:
            totals[match.away_team_id].add(match.away_score, match.home_score, match.away_yellow, match.away_red)

    rows = [
        Row(
            team_id=team_id,
            name=team_map[team_id].name,
            position=0,
            played=t.played,
            won=t.won,
            drawn=t.drawn,
            lost=t.lost,
            goals_for=t.goals_for,
            goals_against=t.goals_against,
            points=t.won * rules.points_win + t.drawn * rules.points_draw + t.lost * rules.points_loss + adjustments.get(team_id, 0),
            yellow_cards=t.yellow,
            red_cards=t.red,
            adjustment=adjustments.get(team_id, 0),
        )
        for team_id, t in totals.items()
    ]
    if not rows:
        return []

    ctx = CriterionContext(matches=considered, teams=team_map, rules=rules)
    blocks: list[list[Row]] = [rows]
    for fn in criterion_fns:
        if len(blocks) == len(rows):  # todos já separados
            break
        blocks = _split(blocks, fn, ctx)

    ordered: list[Row] = []
    for block in blocks:
        tied = len(block) > 1
        if tied:
            block = sorted(block, key=lambda row: (name_key(row.name), row.name, row.team_id))
        for row in block:
            ordered.append(replace(row, position=len(ordered) + 1, tied=tied))
    return ordered


# --- Configuração --------------------------------------------------------------


def validate_rules(points: tuple[int, int, int], criteria: Sequence[str], zones: Sequence[Zone]) -> None:
    """Rejeita (ConfigError) critério desconhecido, repetido ou lista vazia;
    cor fora de #RRGGBB, faixa invertida ou faixas sobrepostas; pontuação negativa.

    Códigos: criteria_empty, criterion_unknown, criterion_repeated, points_negative,
    zone_color_invalid, zone_range_invalid, zone_range_inverted, zones_overlap.
    """
    if not criteria:
        raise ConfigError("criteria_empty", "Informe ao menos um critério de desempate.")
    seen: set[str] = set()
    for key in criteria:
        if key not in CRITERIA:
            raise ConfigError("criterion_unknown", f"Critério de desempate desconhecido: {key}.")
        if key in seen:
            raise ConfigError("criterion_repeated", f"Critério de desempate repetido: {CRITERIA[key].label}.")
        seen.add(key)

    if any(value < 0 for value in points):
        raise ConfigError("points_negative", "A pontuação de vitória, empate e derrota não pode ser negativa.")

    for zone in zones:
        if not isinstance(zone.color, str) or not _HEX_COLOR.fullmatch(zone.color):
            raise ConfigError("zone_color_invalid", f"Cor da zona “{zone.name}” fora do formato #RRGGBB: {zone.color}.")
        if zone.position_from < 1:
            raise ConfigError("zone_range_invalid", f"A faixa da zona “{zone.name}” precisa começar na posição 1 ou depois.")
        if zone.position_to < zone.position_from:
            raise ConfigError(
                "zone_range_inverted",
                f"Faixa invertida na zona “{zone.name}”: {zone.position_from}ª a {zone.position_to}ª.",
            )

    ordered = sorted(zones, key=lambda zone: (zone.position_from, zone.position_to))
    for previous, current in pairwise(ordered):
        if current.position_from <= previous.position_to:
            raise ConfigError(
                "zones_overlap",
                f"As zonas “{previous.name}” e “{current.name}” se sobrepõem na {current.position_from}ª posição.",
            )


def zone_for(position: int, zones: Sequence[Zone]) -> Zone | None:
    """Zona da legenda que contém a posição (ou None)."""
    for zone in zones:
        if zone.position_from <= position <= zone.position_to:
            return zone
    return None
