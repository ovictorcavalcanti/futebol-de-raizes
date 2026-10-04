"""Testes da classificação em funções puras (standings/domain.py), sem banco.

Os casos "reais" reproduzem tabelas finais oficiais de Copas do Mundo e Eurocopas,
para provar critérios, confronto direto e empates que sobram.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from standings.domain import (
    CRITERIA,
    ConfigError,
    CriterionContext,
    MatchResult,
    Row,
    Rules,
    TeamEntry,
    Zone,
    compute_standings,
    name_key,
    validate_rules,
    zone_for,
)

DEFAULT = ("points", "wins", "goal_difference", "goals_for", "head_to_head")


# --- Apoio -----------------------------------------------------------------------


class League:
    """Monta times e partidas pelo nome, para os testes lerem como uma súmula."""

    def __init__(self, *names: str, lots: dict[str, int] | None = None):
        lots = lots or {}
        self.ids = {name: index for index, name in enumerate(names, start=1)}
        self.teams = [TeamEntry(self.ids[name], name, lots.get(name)) for name in names]
        self.matches: list[MatchResult] = []

    def play(
        self,
        home: str,
        home_score: int,
        away_score: int,
        away: str,
        status: str = "finished",
        *,
        hy: int = 0,
        ay: int = 0,
        hr: int = 0,
        ar: int = 0,
    ) -> League:
        self.matches.append(
            MatchResult(self.ids[home], self.ids[away], home_score, away_score, status, hy, ay, hr, ar)
        )
        return self

    def table(self, criteria: tuple[str, ...] = DEFAULT, *, live: bool = False, **points: int) -> list[Row]:
        return compute_standings(self.teams, self.matches, Rules(criteria=criteria, **points), live=live)


def names(rows: list[Row]) -> list[str]:
    return [row.name for row in rows]


def line(row: Row) -> tuple:
    """(J, V, E, D, GP, GC, Pts) — como aparece numa tabela de jornal."""
    return (row.played, row.won, row.drawn, row.lost, row.goals_for, row.goals_against, row.points)


def by_name(rows: list[Row]) -> dict[str, Row]:
    return {row.name: row for row in rows}


# --- Casos reais -------------------------------------------------------------------


def wc2018_group_h() -> League:
    """Copa do Mundo 2018, Grupo H. Japão 4 amarelos, Senegal 6; Colômbia 1 vermelho (contra o Japão)."""
    return (
        League("Colombia", "Japan", "Senegal", "Poland")
        .play("Colombia", 1, 2, "Japan", hy=1, ay=1, hr=1)
        .play("Poland", 1, 2, "Senegal", hy=2, ay=1)
        .play("Japan", 2, 2, "Senegal", hy=2, ay=3)
        .play("Poland", 0, 3, "Colombia", hy=1, ay=1)
        .play("Japan", 0, 1, "Poland", hy=1, ay=0)
        .play("Senegal", 0, 1, "Colombia", hy=2, ay=3)
    )


def test_wc2018_group_h_tied_on_default_criteria():
    rows = wc2018_group_h().table()
    assert names(rows) == ["Colombia", "Japan", "Senegal", "Poland"]
    assert [row.position for row in rows] == [1, 2, 3, 4]
    assert [line(row) for row in rows] == [
        (3, 2, 0, 1, 5, 2, 6),
        (3, 1, 1, 1, 4, 4, 4),
        (3, 1, 1, 1, 4, 4, 4),
        (3, 1, 0, 2, 2, 5, 3),
    ]
    # Pontos, vitórias, saldo, gols pró e confronto direto (2–2) empatados: sobra o empate.
    assert [row.tied for row in rows] == [False, True, True, False]
    table = by_name(rows)
    assert (table["Japan"].yellow_cards, table["Senegal"].yellow_cards) == (4, 6)
    assert table["Colombia"].red_cards == 1
    assert table["Colombia"].goal_difference == 3 and table["Poland"].goal_difference == -3


def test_wc2018_group_h_fair_play_puts_japan_through():
    rows = wc2018_group_h().table(DEFAULT + ("fewer_yellow_cards",))
    assert names(rows) == ["Colombia", "Japan", "Senegal", "Poland"]
    assert not any(row.tied for row in rows)


def test_wc2018_group_h_tie_order_is_by_name_not_input_order():
    league = wc2018_group_h()
    league.teams.reverse()
    rows = league.table()
    assert names(rows)[1:3] == ["Japan", "Senegal"]
    assert rows[1].tied and rows[2].tied


def test_wc2018_group_b_spain_over_portugal_on_goals_scored():
    league = (
        League("Spain", "Portugal", "Iran", "Morocco")
        .play("Morocco", 0, 1, "Iran")
        .play("Portugal", 3, 3, "Spain")
        .play("Portugal", 1, 0, "Morocco")
        .play("Iran", 0, 1, "Spain")
        .play("Iran", 1, 1, "Portugal")
        .play("Spain", 2, 2, "Morocco")
    )
    rows = league.table(("points", "goal_difference", "goals_for"))
    assert names(rows) == ["Spain", "Portugal", "Iran", "Morocco"]
    assert [line(row) for row in rows] == [
        (3, 1, 2, 0, 6, 5, 5),
        (3, 1, 2, 0, 5, 4, 5),
        (3, 1, 1, 1, 2, 2, 4),
        (3, 0, 1, 2, 2, 4, 1),
    ]
    assert not any(row.tied for row in rows)
    # Sem gols pró, Espanha e Portugal ficam empatados e a ordem sai do nome.
    rows = league.table(("points", "goal_difference"))
    assert names(rows)[:2] == ["Portugal", "Spain"] and rows[0].tied and rows[1].tied


def test_wc2014_group_d():
    rows = (
        League("Uruguay", "Costa Rica", "England", "Italy")
        .play("Uruguay", 1, 3, "Costa Rica")
        .play("England", 1, 2, "Italy")
        .play("Uruguay", 2, 1, "England")
        .play("Italy", 0, 1, "Costa Rica")
        .play("Italy", 0, 1, "Uruguay")
        .play("Costa Rica", 0, 0, "England")
        .table()
    )
    assert names(rows) == ["Costa Rica", "Uruguay", "Italy", "England"]
    assert [row.points for row in rows] == [7, 6, 3, 1]
    assert [line(row) for row in rows] == [
        (3, 2, 1, 0, 4, 1, 7),
        (3, 2, 0, 1, 4, 4, 6),
        (3, 1, 0, 2, 2, 3, 3),
        (3, 0, 1, 2, 2, 4, 1),
    ]


def euro2012_group_a() -> League:
    return (
        League("Poland", "Greece", "Russia", "Czech Republic")
        .play("Poland", 1, 1, "Greece")
        .play("Russia", 4, 1, "Czech Republic")
        .play("Greece", 1, 2, "Czech Republic")
        .play("Poland", 1, 1, "Russia")
        .play("Czech Republic", 1, 0, "Poland")
        .play("Greece", 1, 0, "Russia")
    )


def test_euro2012_group_a_head_to_head_before_goal_difference():
    """Grécia passou à frente da Rússia (saldo melhor) pelo confronto direto (1–0)."""
    rows = euro2012_group_a().table(("points", "head_to_head", "goal_difference", "goals_for"))
    assert names(rows) == ["Czech Republic", "Greece", "Russia", "Poland"]
    assert [line(row) for row in rows] == [
        (3, 2, 0, 1, 4, 5, 6),
        (3, 1, 1, 1, 3, 3, 4),
        (3, 1, 1, 1, 5, 3, 4),
        (3, 0, 2, 1, 2, 3, 2),
    ]
    # Com o saldo antes do confronto direto, a ordem se inverte.
    rows = euro2012_group_a().table()
    assert names(rows) == ["Czech Republic", "Russia", "Greece", "Poland"]


def test_euro2004_group_c_three_team_head_to_head_left_tied():
    """Suécia, Dinamarca e Itália com 5 pontos e só empates entre si: o confronto
    direto (2 pontos cada) não separa; aqui o saldo geral decide e a ordem final é a
    da tabela oficial (a UEFA usou os gols no confronto direto, que dão a mesma ordem)."""
    league = (
        League("Denmark", "Italy", "Sweden", "Bulgaria")
        .play("Denmark", 0, 0, "Italy")
        .play("Sweden", 5, 0, "Bulgaria")
        .play("Bulgaria", 0, 2, "Denmark")
        .play("Italy", 1, 1, "Sweden")
        .play("Bulgaria", 1, 2, "Italy")
        .play("Denmark", 2, 2, "Sweden")
    )
    rows = league.table(("points", "head_to_head", "goal_difference", "goals_for"))
    assert names(rows) == ["Sweden", "Denmark", "Italy", "Bulgaria"]
    assert [line(row) for row in rows] == [
        (3, 1, 2, 0, 8, 3, 5),
        (3, 1, 2, 0, 4, 2, 5),
        (3, 1, 2, 0, 3, 2, 5),
        (3, 0, 0, 3, 1, 9, 0),
    ]
    assert not any(row.tied for row in rows)
    rows = league.table(("points", "head_to_head"))
    assert names(rows) == ["Denmark", "Italy", "Sweden", "Bulgaria"]
    assert [row.tied for row in rows] == [True, True, True, False]


# --- Confronto direto ---------------------------------------------------------------


def cycle() -> League:
    """Ciclo: Náutico vence Sport, Sport vence Santa Cruz, Santa Cruz vence Náutico;
    os três vencem o Íbis pelo mesmo placar."""
    return (
        League("Sport", "Náutico", "Santa Cruz", "Íbis", lots={"Santa Cruz": 1, "Náutico": 2, "Sport": 3, "Íbis": 4})
        .play("Náutico", 1, 0, "Sport")
        .play("Sport", 1, 0, "Santa Cruz")
        .play("Santa Cruz", 1, 0, "Náutico")
        .play("Náutico", 1, 0, "Íbis")
        .play("Sport", 1, 0, "Íbis")
        .play("Santa Cruz", 1, 0, "Íbis")
    )


def test_three_team_cycle_unresolved_stays_tied_by_name():
    rows = cycle().table()
    assert names(rows) == ["Náutico", "Santa Cruz", "Sport", "Íbis"]
    assert [row.tied for row in rows] == [True, True, True, False]
    assert [row.position for row in rows] == [1, 2, 3, 4]
    assert all(row.points == 6 for row in rows[:3])


def test_three_team_cycle_resolved_by_drawing_of_lots():
    rows = cycle().table(DEFAULT + ("drawing_of_lots",))
    assert names(rows) == ["Santa Cruz", "Náutico", "Sport", "Íbis"]
    assert not any(row.tied for row in rows)


def test_three_team_head_to_head_resolved():
    """Três times com 6 pontos; entre eles, Retrô 6, Central 3, Salgueiro 0."""
    rows = (
        League("Salgueiro", "Central", "Retrô", "Petrolina", "Afogados")
        .play("Retrô", 1, 0, "Central")
        .play("Retrô", 1, 0, "Salgueiro")
        .play("Central", 1, 0, "Salgueiro")
        .play("Petrolina", 1, 0, "Retrô")
        .play("Afogados", 1, 0, "Retrô")
        .play("Central", 1, 0, "Petrolina")
        .play("Afogados", 1, 0, "Central")
        .play("Salgueiro", 1, 0, "Petrolina")
        .play("Salgueiro", 1, 0, "Afogados")
        .play("Petrolina", 0, 0, "Afogados")
        .table(("points", "head_to_head"))
    )
    assert names(rows) == ["Afogados", "Retrô", "Central", "Salgueiro", "Petrolina"]
    assert [row.points for row in rows] == [7, 6, 6, 6, 4]
    assert not any(row.tied for row in rows)


def test_head_to_head_only_among_the_sub_block_left_tied():
    """Turno e returno. Sport, Náutico e Santa Cruz terminam com 10 pontos; o saldo
    separa o Santa Cruz. Entre Sport e Náutico, o Sport leva (4 a 1). Se o confronto
    direto olhasse os três, o Náutico (7) ficaria à frente do Sport (4)."""
    league = (
        League("Sport", "Náutico", "Santa Cruz", "Central")
        .play("Sport", 1, 0, "Náutico")
        .play("Náutico", 0, 0, "Sport")
        .play("Náutico", 1, 0, "Santa Cruz")
        .play("Santa Cruz", 0, 1, "Náutico")
        .play("Santa Cruz", 1, 0, "Sport")
        .play("Sport", 0, 1, "Santa Cruz")
        .play("Sport", 1, 0, "Central")
        .play("Central", 0, 1, "Sport")
        .play("Náutico", 1, 0, "Central")
        .play("Central", 1, 0, "Náutico")
        .play("Santa Cruz", 3, 0, "Central")
        .play("Central", 0, 0, "Santa Cruz")
    )
    rows = league.table(("points", "goal_difference", "head_to_head"))
    assert names(rows) == ["Santa Cruz", "Sport", "Náutico", "Central"]
    assert [row.points for row in rows] == [10, 10, 10, 4]
    assert [row.goal_difference for row in rows] == [3, 1, 1, -5]
    assert not any(row.tied for row in rows)
    # Com o confronto direto antes do saldo, o bloco é dos três: Náutico 7, Santa Cruz 6, Sport 4.
    rows = league.table(("points", "head_to_head", "goal_difference"))
    assert names(rows) == ["Náutico", "Santa Cruz", "Sport", "Central"]


def test_head_to_head_counts_only_matches_of_the_requested_view():
    """O jogo ao vivo entre os empatados só entra no confronto direto da visão ao vivo;
    jogo agendado nunca entra."""
    league = (
        League("Sport", "Náutico", "Central")
        .play("Sport", 1, 0, "Central")
        .play("Náutico", 1, 0, "Central")
        .play("Náutico", 2, 0, "Sport", "live")
        .play("Sport", 5, 0, "Náutico", "scheduled")
    )
    # Oficial: Sport e Náutico com 3; o 2 a 0 ao vivo e o 5 a 0 agendado não contam.
    official = league.table(("points", "head_to_head"))
    assert names(official) == ["Náutico", "Sport", "Central"]
    assert [row.tied for row in official] == [True, True, False]

    # Ao vivo: Sport e Náutico com 6; o 1 a 0 ao vivo decide o confronto direto
    # (o agendado, que daria a vaga ao Náutico, continua fora).
    league = (
        League("Náutico", "Sport", "Central", "Porto")
        .play("Náutico", 1, 0, "Central")
        .play("Náutico", 1, 0, "Porto")
        .play("Sport", 1, 0, "Porto")
        .play("Central", 1, 0, "Sport")
        .play("Sport", 1, 0, "Náutico", "live")
        .play("Náutico", 5, 0, "Sport", "scheduled")
    )
    live = league.table(("points", "head_to_head"), live=True)
    assert [(row.name, row.points) for row in live] == [("Sport", 6), ("Náutico", 6), ("Central", 3), ("Porto", 0)]
    assert not any(row.tied for row in live)
    # Oficial: Central à frente do Sport pelo confronto direto encerrado (1 a 0).
    official = league.table(("points", "head_to_head"))
    assert [(row.name, row.points) for row in official] == [("Náutico", 6), ("Central", 3), ("Sport", 3), ("Porto", 0)]
    assert not any(row.tied for row in official)


def test_head_to_head_uses_stage_points():
    """Bloco de três só no confronto direto: Sport vence o Náutico e perde do Santa Cruz;
    Náutico e Santa Cruz empatam duas vezes. Com 3/1/0, Sport 3 x Náutico 2; com
    vitória valendo 2, os dois somam 2 e o empate sobra."""
    league = (
        League("Sport", "Náutico", "Santa Cruz")
        .play("Sport", 1, 0, "Náutico")
        .play("Santa Cruz", 1, 0, "Sport")
        .play("Náutico", 0, 0, "Santa Cruz")
        .play("Santa Cruz", 0, 0, "Náutico")
    )
    rows = league.table(("head_to_head",))
    assert names(rows) == ["Santa Cruz", "Sport", "Náutico"]
    assert not any(row.tied for row in rows)
    rows = league.table(("head_to_head",), points_win=2)
    assert names(rows) == ["Santa Cruz", "Náutico", "Sport"]
    assert [row.tied for row in rows] == [False, True, True]


# --- Demais critérios --------------------------------------------------------------


def wins_league() -> League:
    return (
        League("Retrô", "Sete de Setembro", "Decisão", "Jaguar", "Maguary")
        .play("Retrô", 1, 0, "Decisão")
        .play("Retrô", 0, 5, "Jaguar")
        .play("Sete de Setembro", 0, 0, "Decisão")
        .play("Sete de Setembro", 0, 0, "Jaguar")
        .play("Sete de Setembro", 0, 0, "Maguary")
    )


def test_wins_criterion_beats_goal_difference_when_first():
    rows = wins_league().table()
    assert names(rows) == ["Jaguar", "Retrô", "Sete de Setembro", "Maguary", "Decisão"]
    assert [row.points for row in rows] == [4, 3, 3, 1, 1]
    assert not any(row.tied for row in rows)
    rows = wins_league().table(("points", "goal_difference"))
    assert names(rows) == ["Jaguar", "Sete de Setembro", "Retrô", "Maguary", "Decisão"]


def test_drawing_of_lots():
    def table(lots):
        league = League("Central", "Porto", lots=lots).play("Central", 1, 1, "Porto")
        return league.table(DEFAULT + ("drawing_of_lots",))

    rows = table({"Central": 2, "Porto": 1})
    assert names(rows) == ["Porto", "Central"] and not any(row.tied for row in rows)
    rows = table({})
    assert names(rows) == ["Central", "Porto"] and all(row.tied for row in rows)
    # Sorteio incompleto não separa ninguém.
    rows = table({"Porto": 1})
    assert names(rows) == ["Central", "Porto"] and all(row.tied for row in rows)


def test_fewer_red_cards():
    league = League("Central", "Porto").play("Central", 1, 1, "Porto", hr=1, hy=1, ay=3)
    rows = league.table(DEFAULT + ("fewer_red_cards",))
    assert names(rows) == ["Porto", "Central"]
    assert [(row.yellow_cards, row.red_cards) for row in rows] == [(3, 0), (1, 1)]
    assert not any(row.tied for row in rows)
    rows = league.table(DEFAULT + ("fewer_yellow_cards", "fewer_red_cards"))
    assert names(rows) == ["Central", "Porto"]


def test_fewer_yellow_cards_counts_only_considered_matches():
    league = (
        League("Central", "Porto")
        .play("Central", 0, 0, "Porto", hy=1, ay=2)
        .play("Porto", 0, 0, "Central", "live", hy=0, ay=5)
    )
    assert names(league.table(DEFAULT + ("fewer_yellow_cards",))) == ["Central", "Porto"]
    assert names(league.table(DEFAULT + ("fewer_yellow_cards",), live=True)) == ["Porto", "Central"]


# --- Visões, pontuação e casos de borda -------------------------------------------


def test_live_and_official_views():
    league = (
        League("Sport", "Náutico", "Santa Cruz", "Retrô")
        .play("Sport", 1, 0, "Náutico")
        .play("Santa Cruz", 2, 0, "Retrô", "live")
        .play("Náutico", 1, 1, "Santa Cruz", "suspended")
        .play("Retrô", 3, 0, "Sport", "scheduled")
        .play("Retrô", 3, 0, "Náutico", "postponed")
        .play("Sport", 3, 0, "Santa Cruz", "cancelled")
    )
    official = by_name(league.table())
    assert line(official["Sport"]) == (1, 1, 0, 0, 1, 0, 3)
    assert line(official["Santa Cruz"]) == (0, 0, 0, 0, 0, 0, 0)
    assert line(official["Retrô"]) == (0, 0, 0, 0, 0, 0, 0)

    live = league.table(live=True)
    assert names(live) == ["Santa Cruz", "Sport", "Náutico", "Retrô"]
    table = by_name(live)
    assert line(table["Santa Cruz"]) == (2, 1, 1, 0, 3, 1, 4)
    assert line(table["Náutico"]) == (2, 0, 1, 1, 1, 2, 1)
    assert line(table["Retrô"]) == (1, 0, 0, 1, 0, 2, 0)


def test_custom_points():
    league = (
        League("Sport", "Náutico", "Santa Cruz")
        .play("Sport", 1, 0, "Náutico")
        .play("Náutico", 0, 0, "Santa Cruz")
        .play("Santa Cruz", 2, 1, "Sport")
    )
    rows = by_name(league.table(points_win=2, points_draw=1, points_loss=0))
    assert (rows["Sport"].points, rows["Santa Cruz"].points, rows["Náutico"].points) == (2, 3, 1)
    rows = by_name(league.table(points_win=3, points_draw=1, points_loss=1))
    assert (rows["Sport"].points, rows["Santa Cruz"].points, rows["Náutico"].points) == (4, 4, 2)


def test_custom_points_change_order():
    league = (
        League("Sport", "Náutico", "Central", "Porto")
        .play("Sport", 1, 0, "Central")
        .play("Sport", 0, 1, "Porto")
        .play("Náutico", 0, 0, "Central")
        .play("Náutico", 0, 0, "Porto")
    )
    # 3/1/0: Sport 3 à frente do Náutico 2. Vitória valendo 1: Náutico 2 à frente do Sport 1.
    assert names(league.table(("points",))) == ["Porto", "Sport", "Náutico", "Central"]
    assert names(league.table(("points", "goal_difference"), points_win=1)) == ["Porto", "Náutico", "Sport", "Central"]


def test_teams_without_games_get_rows_sorted_by_name():
    rows = League("Náutico", "Jaguar", "Íbis", "maguary").table()
    # sem acento e sem caixa: Íbis < Jaguar < maguary < Náutico (por código, Í e m iriam para o fim)
    assert names(rows) == ["Íbis", "Jaguar", "maguary", "Náutico"]
    assert [row.position for row in rows] == [1, 2, 3, 4]
    assert all(row.tied for row in rows)
    assert all(line(row) == (0, 0, 0, 0, 0, 0, 0) for row in rows)


def test_single_team_and_empty_group():
    rows = League("Sport").table()
    assert len(rows) == 1 and rows[0].position == 1 and not rows[0].tied
    assert compute_standings([], [], Rules()) == []


def test_matches_with_outside_teams_are_ignored():
    league = League("Sport", "Náutico").play("Sport", 2, 1, "Náutico")
    league.matches.append(MatchResult(1, 99, 0, 7, "finished"))
    league.matches.append(MatchResult(99, 2, 0, 7, "finished"))
    league.matches.append(MatchResult(2, 2, 9, 9, "finished"))  # time contra ele mesmo
    table = by_name(league.table())
    assert line(table["Sport"]) == (1, 1, 0, 0, 2, 1, 3)
    assert line(table["Náutico"]) == (1, 0, 0, 1, 1, 2, 0)


def test_unknown_criterion_in_rules_raises():
    with pytest.raises(ConfigError) as error:
        League("Sport", "Náutico").table(("points", "goal_average"))
    assert error.value.code == "criterion_unknown"


def test_unknown_criterion_raises_even_without_ties_or_teams():
    """Configuração inválida falha sempre, não só quando o critério chega a ser usado."""
    decided = League("Sport", "Náutico").play("Sport", 1, 0, "Náutico")
    with pytest.raises(ConfigError) as error:
        decided.table(("points", "goal_average"))
    assert error.value.code == "criterion_unknown"
    with pytest.raises(ConfigError) as error:
        compute_standings([], [], Rules(criteria=("goal_average",)))
    assert error.value.code == "criterion_unknown"


def test_leftover_ties_are_sorted_by_name_inside_each_block():
    """Dois blocos empatados: cada um é ordenado pelo nome sem misturar com o outro."""
    rows = (
        League("Sport", "Santa Cruz", "Náutico", "Central")
        .play("Sport", 1, 1, "Náutico")
        .play("Santa Cruz", 0, 0, "Central")
        .table()
    )
    assert names(rows) == ["Náutico", "Sport", "Central", "Santa Cruz"]
    assert [row.position for row in rows] == [1, 2, 3, 4]
    assert all(row.tied for row in rows)


def test_order_does_not_depend_on_input_order():
    leagues = [wc2018_group_h(), cycle(), euro2012_group_a()]
    criteria = (DEFAULT, DEFAULT + ("fewer_yellow_cards",), DEFAULT + ("drawing_of_lots",))
    for league in leagues:
        for rules in criteria:
            expected = league.table(rules)
            for teams in itertools.permutations(league.teams):
                for matches in (league.matches, league.matches[::-1]):
                    got = compute_standings(list(teams), matches, Rules(criteria=rules))
                    assert got == expected


def test_empty_criteria_leaves_everyone_tied():
    rows = League("Sport", "Náutico").play("Sport", 5, 0, "Náutico").table(())
    assert names(rows) == ["Náutico", "Sport"] and all(row.tied for row in rows)


def test_name_key():
    assert name_key("Íbis") == "ibis"
    assert name_key("SÃO Caetano") == name_key("são caetano") == "sao caetano"
    assert name_key("Straße") == "strasse"


# --- Catálogo, configuração e zonas -----------------------------------------------


def test_catalog_keys_labels_and_functions():
    assert {key: criterion.label for key, criterion in CRITERIA.items()} == {
        "points": "Pontos",
        "wins": "Vitórias",
        "goal_difference": "Saldo de gols",
        "goals_for": "Gols pró",
        "head_to_head": "Confronto direto",
        "fewer_red_cards": "Menos cartões vermelhos",
        "fewer_yellow_cards": "Menos cartões amarelos",
        "drawing_of_lots": "Sorteio",
    }
    assert all(criterion.key == key and callable(criterion.fn) for key, criterion in CRITERIA.items())


def test_criterion_functions_follow_the_contract():
    """Cada critério recebe só o bloco empatado e devolve {team_id: valor}, maior = melhor."""
    league = (
        League("Sport", "Náutico", "Santa Cruz", lots={"Sport": 2, "Náutico": 1, "Santa Cruz": 3})
        .play("Sport", 2, 0, "Náutico", hy=1, ay=4, ar=1)
        .play("Santa Cruz", 3, 0, "Sport", hr=2)
        .play("Santa Cruz", 0, 0, "Náutico")
    )
    rows = by_name(league.table())
    block = [rows["Sport"], rows["Náutico"]]
    ids = league.ids
    ctx = CriterionContext(
        matches=tuple(league.matches),
        teams={team.team_id: team for team in league.teams},
        rules=Rules(),
    )

    def value(key: str) -> dict[int, float]:
        fn = CRITERIA[key].fn
        assert fn is not None
        return dict(fn(block, ctx))

    # Confronto direto só com Sport x Náutico (o jogo com o Santa Cruz fica fora).
    assert value("head_to_head") == {ids["Sport"]: 3, ids["Náutico"]: 0}
    assert value("points") == {ids["Sport"]: 3, ids["Náutico"]: 1}
    assert value("wins") == {ids["Sport"]: 1, ids["Náutico"]: 0}
    assert value("goal_difference") == {ids["Sport"]: -1, ids["Náutico"]: -2}
    assert value("goals_for") == {ids["Sport"]: 2, ids["Náutico"]: 0}
    assert value("fewer_yellow_cards") == {ids["Sport"]: -1, ids["Náutico"]: -4}
    assert value("fewer_red_cards") == {ids["Sport"]: 0, ids["Náutico"]: -1}
    assert value("drawing_of_lots") == {ids["Sport"]: -2, ids["Náutico"]: -1}


SEED_ZONES = (
    Zone("Classificados", "#1B7F3B", 1, 4),
    Zone("Rebaixados", "#B3261E", 17, 20),
)


def test_validate_rules_accepts_seed_config():
    assert validate_rules((3, 1, 0), list(DEFAULT), SEED_ZONES) is None
    assert validate_rules((0, 0, 0), ["drawing_of_lots"], []) is None
    # cor em minúsculas, zonas fora de ordem e encostadas também valem
    validate_rules((2, 1, 0), DEFAULT, [Zone("B", "#b3261e", 5, 5), Zone("A", "#1b7f3b", 1, 4)])


@pytest.mark.parametrize(
    ("points", "criteria", "zones", "code"),
    [
        ((3, 1, 0), [], SEED_ZONES, "criteria_empty"),
        ((3, 1, 0), ["points", "goal_average"], SEED_ZONES, "criterion_unknown"),
        ((3, 1, 0), ["points", "wins", "points"], SEED_ZONES, "criterion_repeated"),
        ((3, -1, 0), DEFAULT, SEED_ZONES, "points_negative"),
        ((3, 1, -1), DEFAULT, SEED_ZONES, "points_negative"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "1B7F3B", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3G", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3B00", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "green", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3B\n", 1, 4)], "zone_color_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3B", 4, 1)], "zone_range_inverted"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3B", 0, 4)], "zone_range_invalid"),
        ((3, 1, 0), DEFAULT, [Zone("Classificados", "#1B7F3B", 1, 4), Zone("Sul-Americana", "#1F5FA8", 4, 6)], "zones_overlap"),
        ((3, 1, 0), DEFAULT, [Zone("Rebaixados", "#B3261E", 17, 20), Zone("Meio", "#777777", 10, 18)], "zones_overlap"),
        ((3, 1, 0), DEFAULT, [Zone("Tudo", "#777777", 1, 20), Zone("Dentro", "#1B7F3B", 5, 6)], "zones_overlap"),
    ],
)
def test_validate_rules_rejects(points, criteria, zones, code):
    with pytest.raises(ConfigError) as error:
        validate_rules(points, criteria, zones)
    assert error.value.code == code
    assert error.value.message and str(error.value) == error.value.message


def test_zone_for():
    assert zone_for(1, SEED_ZONES).name == "Classificados"
    assert zone_for(4, SEED_ZONES).name == "Classificados"
    assert zone_for(5, SEED_ZONES) is None
    assert zone_for(16, SEED_ZONES) is None
    assert zone_for(17, SEED_ZONES).color == "#B3261E"
    assert zone_for(20, SEED_ZONES).name == "Rebaixados"
    assert zone_for(21, SEED_ZONES) is None
    assert zone_for(1, []) is None


def test_domain_does_not_import_django():
    source = Path(__file__).resolve().parent.parent / "standings" / "domain.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    modules += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [name for name in modules if name.split(".")[0] in {"django", "competitions", "matches", "standings"}]


# --- Punição e bonificação em pontos ----------------------------------------------------


def test_point_deduction_changes_the_order_but_not_head_to_head():
    league = (
        League("Sport", "Náutico", "Santa Cruz")
        .play("Sport", 2, 0, "Náutico")
        .play("Santa Cruz", 1, 0, "Náutico")
        .play("Sport", 1, 1, "Santa Cruz")
    )
    rows = league.table()
    assert names(rows) == ["Sport", "Santa Cruz", "Náutico"]
    assert all(row.adjustment == 0 for row in rows)

    ids = league.ids
    rows = compute_standings(league.teams, league.matches, Rules(), adjustments={ids["Sport"]: -3})
    assert names(rows) == ["Santa Cruz", "Sport", "Náutico"]
    table = by_name(rows)
    assert (table["Sport"].points, table["Sport"].adjustment) == (1, -3)
    assert (table["Santa Cruz"].points, table["Santa Cruz"].adjustment) == (4, 0)

    # Bonificação: os pontos ajustados ordenam; empatados em pontos, o confronto direto olha só
    # os jogos (Sport 1 × 1 Santa Cruz → segue empatado, ordem alfabética).
    rows = compute_standings(
        league.teams, league.matches, Rules(criteria=("points", "head_to_head")), adjustments={ids["Santa Cruz"]: 3, ids["Sport"]: 2}
    )
    assert [(row.name, row.points) for row in rows] == [("Santa Cruz", 7), ("Sport", 6), ("Náutico", 0)]
    rows = compute_standings(
        league.teams, league.matches, Rules(criteria=("points", "head_to_head")), adjustments={ids["Santa Cruz"]: 2, ids["Sport"]: 2}
    )
    assert [(row.name, row.points, row.tied) for row in rows][:2] == [("Santa Cruz", 6, True), ("Sport", 6, True)]


def test_adjustment_tie_falls_to_head_to_head_of_real_results():
    league = League("A", "B").play("A", 1, 0, "B")  # A 3, B 0
    rows = compute_standings(league.teams, league.matches, Rules(criteria=("points", "head_to_head")), adjustments={league.ids["B"]: 3})
    assert [(row.name, row.points, row.tied) for row in rows] == [("A", 3, False), ("B", 3, False)]  # A venceu o jogo
    rows = compute_standings(league.teams, league.matches, Rules(), adjustments={99: -5}, live=True)  # time fora da tabela: ignorado
    assert [row.points for row in rows] == [3, 0]


def test_adjustments_also_apply_to_teams_without_matches():
    league = League("A", "B")
    rows = compute_standings(league.teams, [], Rules(), adjustments={league.ids["B"]: -2})
    assert [(row.name, row.points, row.played, row.adjustment) for row in rows] == [("A", 0, 0, 0), ("B", -2, 0, -2)]
