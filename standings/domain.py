"""Classificação em funções puras (sem Django).

CONTRATO — usado por standings/services.py, admin e API. Implementações marcadas
com NotImplementedError serão preenchidas mantendo estas assinaturas.

`compute_standings` recebe os times e as partidas do grupo e as regras da fase,
e devolve as linhas em ordem. Pontuação, critérios e legenda são dados da fase.

1. Considera as partidas encerradas; na visão ao vivo (live=True), também as em
   andamento (status live ou suspended).
2. Soma jogos, vitórias, empates, derrotas, gols pró, gols contra e cartões.
3. Calcula os pontos com a pontuação da fase.
4. Aplica os critérios na ordem configurada; cada um separa só os times que o
   anterior deixou empatados. Cada critério é uma função que recebe o BLOCO de
   times ainda empatados e devolve um valor por time (maior = melhor). Assim o
   confronto direto olha só os jogos entre os empatados (2 ou mais times).
5. Se o empate sobrar, ordena pelo nome e marca as linhas com tied=True.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


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


# Catálogo: points, wins, goal_difference, goals_for, head_to_head,
# fewer_red_cards, fewer_yellow_cards, drawing_of_lots. Rótulos em PT-BR.
# As chaves e rótulos abaixo são fixos; a implementação preenche `fn`.
CRITERIA: dict[str, Criterion] = {
    "points": Criterion("points", "Pontos", "Mais pontos, pela pontuação da fase"),
    "wins": Criterion("wins", "Vitórias", "Mais vitórias"),
    "goal_difference": Criterion("goal_difference", "Saldo de gols", "Maior saldo de gols"),
    "goals_for": Criterion("goals_for", "Gols pró", "Mais gols pró"),
    "head_to_head": Criterion("head_to_head", "Confronto direto", "Mais pontos nos jogos entre os times empatados"),
    "fewer_red_cards": Criterion("fewer_red_cards", "Menos cartões vermelhos", "Menos cartões vermelhos, contados pelos eventos"),
    "fewer_yellow_cards": Criterion("fewer_yellow_cards", "Menos cartões amarelos", "Menos cartões amarelos, contados pelos eventos"),
    "drawing_of_lots": Criterion("drawing_of_lots", "Sorteio", "Ordem do sorteio"),
}


def compute_standings(teams: Sequence[TeamEntry], matches: Sequence[MatchResult], rules: Rules, *, live: bool = False) -> list[Row]:
    raise NotImplementedError


def validate_rules(points: tuple[int, int, int], criteria: Sequence[str], zones: Sequence[Zone]) -> None:
    """Rejeita (ConfigError) critério desconhecido, repetido ou lista vazia;
    cor fora de #RRGGBB, faixa invertida ou faixas sobrepostas; pontuação negativa."""
    raise NotImplementedError


def zone_for(position: int, zones: Sequence[Zone]) -> Zone | None:
    raise NotImplementedError
