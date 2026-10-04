"""Tabela de jogos em JSON para uma fase com tabela: pontos corridos ou grupos (admin da fase).

Formato — os participantes (opcional: os que ainda não estão na fase) e as rodadas:

    Pontos corridos:
    {"times": ["SPT", "NAU", "SCZ", "RET"],
     "rodadas": [
       {"numero": 1, "nome": "1ª rodada", "jogos": [
         {"mandante": "SPT", "visitante": "NAU", "data": "2027-01-15 19:00",
          "local": "Ilha do Retiro", "cidade": "Recife"},
         {"mandante": "Santa Cruz", "visitante": {"id": 42}, "data": "2027-01-16 16:00"}
       ]}
     ]}

    Grupos:
    {"grupos": {"A": ["SPT", "NAU"], "B": ["SCZ", "RET"]},
     "rodadas": [{"numero": 1, "jogos": [{"mandante": "SPT", "visitante": "SCZ", "data": "..."}]}]}

(Uma lista de rodadas direto também vale; chaves em inglês também: teams, groups,
rounds, number, name, matches, home, away, kickoff, venue, city.) `data` é o horário
de Brasília.

Time: `{"id": N}`, o nome completo ou a sigla. A sigla é procurada primeiro entre os
times da fase, depois no cadastro; sigla ou nome com mais de um time é recusado, com
os candidatos.

Participantes = times que já estão na fase + os de `times`/`grupos` (que entram na
tabela; grupo novo é criado). Jogo com time fora dos participantes, ou time em dois
jogos da mesma rodada, é recusado. Na fase de grupos, times de grupos diferentes
podem se enfrentar (ex.: Copa do Nordeste); o jogo fica no grupo do mandante.

Rodada que já existe (pelo número) recebe os jogos; jogo que já existe na rodada
(mesmos mandante e visitante) é pulado.

`parse_table` só lê e confere (nada gravado: dá para usar no clean do formulário);
`apply_table` grava o plano. Erros: `TableImportError` com uma mensagem por problema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from django.db import transaction

from core import timeutils

from .models import Group, GroupTeam, Round, Stage, Team

KEYS = {
    "times": "teams", "teams": "teams",
    "grupos": "groups", "groups": "groups",
    "rodadas": "rounds", "rounds": "rounds",
    "numero": "number", "número": "number", "number": "number",
    "nome": "name", "name": "name",
    "jogos": "matches", "matches": "matches",
    "mandante": "home", "home": "home",
    "visitante": "away", "away": "away",
    "data": "kickoff", "kickoff": "kickoff",
    "local": "venue", "estadio": "venue", "estádio": "venue", "venue": "venue",
    "cidade": "city", "city": "city",
}
TOP_KEYS = {"teams", "groups", "rounds"}
ROUND_KEYS = {"number", "name", "matches"}
MATCH_KEYS = {"home", "away", "kickoff", "venue", "city"}
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
LEAGUE_GROUP = "Tabela"


class TableImportError(Exception):
    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


@dataclass
class MatchPlan:
    home: Team
    away: Team
    kickoff: datetime
    venue: str = ""
    city: str = ""


@dataclass
class RoundPlan:
    number: int
    name: str
    matches: list[MatchPlan] = field(default_factory=list)


@dataclass
class TablePlan:
    rounds: list[RoundPlan]
    entries: dict[int, tuple[Team, str]] = field(default_factory=dict)  # time novo na fase → (time, grupo)

    @property
    def match_count(self) -> int:
        return sum(len(item.matches) for item in self.rounds)


@dataclass
class TableResult:
    rounds_created: int = 0
    matches_created: int = 0
    matches_skipped: int = 0
    teams_added: list[str] = field(default_factory=list)
    groups_created: list[str] = field(default_factory=list)


def _normalize(obj: dict, allowed: set[str], where: str, errors: list[str]) -> dict:
    out = {}
    for key, value in obj.items():
        name = KEYS.get(str(key).strip().lower())
        if name not in allowed:
            errors.append(f"{where}: chave desconhecida {key}.")
            continue
        out[name] = value
    return out


class _Teams:
    """Resolve id, nome ou sigla; a sigla vale primeiro entre os times da fase."""

    def __init__(self, stage: Stage | None):
        self.all = list(Team.objects.all())
        self.by_id = {team.id: team for team in self.all}
        self.group_of: dict[int, str] = {}  # times que já estão na fase → grupo
        if stage and stage.pk:
            self.group_of = dict(GroupTeam.objects.filter(group__stage=stage).values_list("team_id", "group__name"))
        self.stage_teams = [team for team in self.all if team.id in self.group_of]

    @staticmethod
    def _label(teams: list[Team]) -> str:
        return ", ".join(f"{team.name} (id {team.id})" for team in teams)

    def resolve(self, ref, where: str, errors: list[str]) -> Team | None:
        if isinstance(ref, dict) and set(ref) == {"id"}:
            ref = ref["id"]
        if isinstance(ref, int) and not isinstance(ref, bool):
            team = self.by_id.get(ref)
            if team is None:
                errors.append(f"{where}: não há time com id {ref}.")
            return team
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f"{where}: informe o time (sigla, nome ou {{\"id\": N}}).")
            return None
        text = ref.strip()
        named = [team for team in self.all if team.name.casefold() == text.casefold()]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            errors.append(f'{where}: há mais de um time chamado "{text}" ({self._label(named)}); use {{"id": N}}.')
            return None
        for pool in (self.stage_teams, self.all):
            coded = [team for team in pool if team.short_name.upper() == text.upper()]
            if len(coded) == 1:
                return coded[0]
            if len(coded) > 1:
                errors.append(f"{where}: a sigla {text.upper()} é de mais de um time ({self._label(coded)}); use o nome ou o id.")
                return None
        errors.append(f'{where}: time "{text}" não cadastrado (cadastre antes, ex.: manage.py import_teams).')
        return None


def _kickoff(value, where: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f'{where}: informe a data e a hora (ex.: "2027-01-15 19:00").')
        return None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timeutils.app_tz())  # horário de Brasília
    errors.append(f'{where}: data "{value}" fora do formato AAAA-MM-DD HH:MM.')
    return None


def _text(value, limit: int, label: str, where: str, errors: list[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append(f"{where}: {label} precisa ser texto.")
        return ""
    if len(value.strip()) > limit:
        errors.append(f"{where}: {label} tem no máximo {limit} caracteres.")
    return value.strip()


def _participants(top: dict, fmt: str, teams: _Teams, errors: list[str]) -> dict[int, tuple[Team, str]]:
    """Times declarados em `times` (pontos corridos) ou `grupos` (grupos) que ainda não
    estão na fase → {time: (time, grupo)}. Time já em outro grupo da fase é recusado."""
    declared: list[tuple[str, object, str]] = []  # (onde, referência, grupo)
    if fmt == Stage.Format.LEAGUE:
        if "groups" in top:
            errors.append('Pontos corridos não tem grupos: use "times" para os participantes.')
        raw = top.get("teams", [])
        if not isinstance(raw, list):
            errors.append('"times" precisa ser uma lista de times.')
            raw = []
        declared = [(f"Times, item {n}", ref, LEAGUE_GROUP) for n, ref in enumerate(raw, start=1)]
    else:
        if "teams" in top:
            errors.append('Fase de grupos: use "grupos" ({"A": [...], "B": [...]}) para os participantes.')
        raw = top.get("groups", {})
        if not isinstance(raw, dict) or not all(isinstance(v, list) for v in raw.values()):
            errors.append('"grupos" precisa ser um objeto: {"A": ["SPT", "NAU"], "B": [...]}.')
            raw = {}
        for name, refs in raw.items():
            if not str(name).strip() or len(str(name).strip()) > Group._meta.get_field("name").max_length:
                errors.append(f'Grupo "{name}": nome vazio ou longo demais.')
                continue
            declared += [(f"Grupo {name}, item {n}", ref, str(name).strip()) for n, ref in enumerate(refs, start=1)]
    entries: dict[int, tuple[Team, str]] = {}
    for where, ref, group in declared:
        team = teams.resolve(ref, where, errors)
        if team is None:
            continue
        current = teams.group_of.get(team.id) or (entries[team.id][1] if team.id in entries else None)
        if current is not None and current.casefold() != group.casefold():
            errors.append(f"{where}: {team.name} já está no grupo {current}.")
        elif current is None:
            entries[team.id] = (team, group)
    return entries


def parse_table(text: str, stage: Stage | None, fmt: str | None = None) -> TablePlan:
    """Lê e confere o JSON (sem gravar). `stage` pode ser None (fase ainda não salva);
    `fmt` = formato da fase (padrão: o da fase; sem fase, pontos corridos)."""
    fmt = fmt or (stage.format if stage else Stage.Format.LEAGUE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TableImportError([f"JSON inválido (linha {exc.lineno}, coluna {exc.colno}): {exc.msg}."]) from exc
    errors: list[str] = []
    teams = _Teams(stage)
    top = _normalize(data, TOP_KEYS, "Tabela", errors) if isinstance(data, dict) else {"rounds": data}
    entries = _participants(top, fmt, teams, errors)
    in_championship = set(teams.group_of) | set(entries)
    data = top.get("rounds")
    if not isinstance(data, list) or not data:
        raise TableImportError(errors or ['A tabela deve ter a lista "rodadas", cada uma com os seus "jogos".'])
    rounds: list[RoundPlan] = []
    seen_numbers: set[int] = set()
    for index, raw_round in enumerate(data, start=1):
        where = f"Rodada {index}"
        if not isinstance(raw_round, dict):
            errors.append(f"{where}: cada rodada é um objeto com numero e jogos.")
            continue
        item = _normalize(raw_round, ROUND_KEYS, where, errors)
        number = item.get("number", index)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            errors.append(f"{where}: numero precisa ser um inteiro a partir de 1.")
            continue
        where = f"Rodada {number}"
        if number in seen_numbers:
            errors.append(f"{where}: rodada repetida no arquivo.")
            continue
        seen_numbers.add(number)
        plan = RoundPlan(number, _text(item.get("name"), Round._meta.get_field("name").max_length, "nome", where, errors))
        matches = item.get("matches")
        if not isinstance(matches, list) or not matches:
            errors.append(f"{where}: informe os jogos da rodada.")
            continue
        playing: set[int] = set()
        for m_index, raw_match in enumerate(matches, start=1):
            m_where = f"{where}, jogo {m_index}"
            if not isinstance(raw_match, dict):
                errors.append(f"{m_where}: cada jogo é um objeto com mandante, visitante e data.")
                continue
            entry = _normalize(raw_match, MATCH_KEYS, m_where, errors)
            home = teams.resolve(entry.get("home"), f"{m_where} (mandante)", errors)
            away = teams.resolve(entry.get("away"), f"{m_where} (visitante)", errors)
            kickoff = _kickoff(entry.get("kickoff"), m_where, errors)
            venue = _text(entry.get("venue"), 120, "local", m_where, errors)
            city = _text(entry.get("city"), 80, "cidade", m_where, errors)
            if not (home and away and kickoff):
                continue
            if home.id == away.id:
                errors.append(f"{m_where}: mandante e visitante são o mesmo time ({home.name}).")
                continue
            outside = [team.name for team in (home, away) if team.id not in in_championship]
            if outside:
                where_to = '"times"' if fmt == Stage.Format.LEAGUE else '"grupos"'
                verb = "estão" if len(outside) > 1 else "está"
                errors.append(f"{m_where}: {' e '.join(outside)} não {verb} no campeonato (inclua em {where_to} ou na tabela da fase).")
                continue
            twice = [team.name for team in (home, away) if team.id in playing]
            playing.update((home.id, away.id))  # mesmo recusado: uma 3ª aparição também é apontada
            if twice:
                errors.append(f"{m_where}: {' e '.join(twice)} já {'jogam' if len(twice) > 1 else 'joga'} nesta rodada.")
                continue
            plan.matches.append(MatchPlan(home, away, kickoff, venue, city))
        rounds.append(plan)
    if errors:
        raise TableImportError(errors)
    return TablePlan(rounds, entries)


def apply_table(stage: Stage, plan: TablePlan) -> TableResult:
    """Grava o plano na fase: participantes novos (e grupos novos), rodadas e jogos.
    O jogo fica no grupo do mandante."""
    from matches.models import Match

    if not stage.has_table:
        raise TableImportError(["A tabela em JSON é só para fase de pontos corridos ou de grupos."])
    result = TableResult()
    with transaction.atomic():
        groups = {group.name.casefold(): group for group in Group.objects.filter(stage=stage)}
        if stage.format == Stage.Format.LEAGUE and not groups:
            groups[LEAGUE_GROUP.casefold()] = Group.objects.create(stage=stage, name=LEAGUE_GROUP)
        group_of = {item.team_id: item.group for item in GroupTeam.objects.filter(group__stage=stage).select_related("group")}
        for team, group_name in plan.entries.values():
            if team.id in group_of:
                continue
            if stage.format == Stage.Format.LEAGUE:
                group = next(iter(groups.values()))  # o grupo único da fase
            else:
                group = groups.get(group_name.casefold())
                if group is None:
                    group = groups[group_name.casefold()] = Group.objects.create(stage=stage, name=group_name)
                    result.groups_created.append(group_name)
            GroupTeam.objects.create(group=group, team=team)
            group_of[team.id] = group
            result.teams_added.append(team.name)
        rounds = {rnd.number: rnd for rnd in Round.objects.filter(stage=stage)}
        for round_plan in plan.rounds:
            rnd = rounds.get(round_plan.number)
            if rnd is None:
                rnd = Round.objects.create(stage=stage, number=round_plan.number, name=round_plan.name)
                rounds[rnd.number] = rnd
                result.rounds_created += 1
            elif round_plan.name and not rnd.name:
                rnd.name = round_plan.name
                rnd.save(update_fields=["name"])
            existing = set(Match.objects.filter(stage=stage, round=rnd).values_list("home_team_id", "away_team_id"))
            for item in round_plan.matches:
                if (item.home.id, item.away.id) in existing:
                    result.matches_skipped += 1
                    continue
                match = Match(
                    stage=stage, group=group_of[item.home.id], round=rnd, home_team=item.home, away_team=item.away,
                    kickoff_at=item.kickoff, venue=item.venue, city=item.city,
                )
                match.full_clean()
                match.save()
                result.matches_created += 1
    return result
