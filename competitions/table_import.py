"""Tabela de jogos em JSON para uma fase de pontos corridos (admin da fase).

Formato — as rodadas da fase, cada uma com os seus jogos:

    {"rodadas": [
      {"numero": 1, "nome": "1ª rodada",
       "jogos": [
         {"mandante": "SPT", "visitante": "NAU", "data": "2027-01-15 19:00",
          "local": "Ilha do Retiro", "cidade": "Recife"},
         {"mandante": "Santa Cruz", "visitante": {"id": 42}, "data": "2027-01-16 16:00"}
       ]}
    ]}

(Uma lista de rodadas direto também vale; chaves em inglês também: rounds, number,
name, matches, home, away, kickoff, venue, city.) `data` é o horário de Brasília.

Time: `{"id": N}`, o nome completo ou a sigla. A sigla é procurada primeiro entre os
times da fase (grupo "Tabela"), depois no cadastro; sigla ou nome com mais de um time
é recusado, com os candidatos. Rodada que já existe (pelo número) recebe os jogos;
jogo que já existe na rodada (mesmos mandante e visitante) é pulado. Time que joga e
ainda não está na tabela da fase entra nela.

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
ROUND_KEYS = {"number", "name", "matches"}
MATCH_KEYS = {"home", "away", "kickoff", "venue", "city"}
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


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

    @property
    def match_count(self) -> int:
        return sum(len(item.matches) for item in self.rounds)


@dataclass
class TableResult:
    rounds_created: int = 0
    matches_created: int = 0
    matches_skipped: int = 0
    teams_added: list[str] = field(default_factory=list)


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
        in_stage = set(GroupTeam.objects.filter(group__stage=stage).values_list("team_id", flat=True)) if stage and stage.pk else set()
        self.stage_teams = [team for team in self.all if team.id in in_stage]

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


def parse_table(text: str, stage: Stage | None) -> TablePlan:
    """Lê e confere o JSON (sem gravar). `stage` pode ser None (fase ainda não salva)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TableImportError([f"JSON inválido (linha {exc.lineno}, coluna {exc.colno}): {exc.msg}."]) from exc
    errors: list[str] = []
    if isinstance(data, dict):
        data = _normalize(data, {"rounds"}, "Tabela", errors).get("rounds")
    if not isinstance(data, list) or not data:
        raise TableImportError(errors or ['A tabela deve ter a lista "rodadas", cada uma com os seus "jogos".'])
    teams = _Teams(stage)
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
        pairs: set[tuple[int, int]] = set()
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
            if (home.id, away.id) in pairs:
                errors.append(f"{m_where}: {home.name} × {away.name} repetido na rodada.")
                continue
            twice = {home.id, away.id} & playing
            if twice:
                name = home.name if home.id in twice else away.name
                errors.append(f"{m_where}: {name} joga duas vezes na rodada.")
                continue
            pairs.add((home.id, away.id))
            playing.update((home.id, away.id))
            plan.matches.append(MatchPlan(home, away, kickoff, venue, city))
        rounds.append(plan)
    if errors:
        raise TableImportError(errors)
    return TablePlan(rounds)


def apply_table(stage: Stage, plan: TablePlan) -> TableResult:
    """Grava o plano na fase (pontos corridos): rodadas, times na tabela e jogos."""
    from matches.models import Match

    if stage.format != Stage.Format.LEAGUE:
        raise TableImportError(["A tabela em JSON é só para fase de pontos corridos."])
    result = TableResult()
    with transaction.atomic():
        group = Group.objects.filter(stage=stage).order_by("id").first() or Group.objects.create(stage=stage, name="Tabela")
        in_table = set(GroupTeam.objects.filter(group__stage=stage).values_list("team_id", flat=True))
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
                for team in (item.home, item.away):
                    if team.id not in in_table:
                        GroupTeam.objects.create(group=group, team=team)
                        in_table.add(team.id)
                        result.teams_added.append(team.name)
                match = Match(
                    stage=stage, group=group, round=rnd, home_team=item.home, away_team=item.away,
                    kickoff_at=item.kickoff, venue=item.venue, city=item.city,
                )
                match.full_clean()
                match.save()
                result.matches_created += 1
    return result
