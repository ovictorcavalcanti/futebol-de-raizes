"""Carga de demonstração: cinco competições (um caso de classificação em cada), clubes do
Nordeste, jogos do dia.

Uso: `python manage.py seed [--reset | --clear] [--date YYYY-MM-DD]` (padrão: hoje, horário de Brasília).

* Usuários: "admin" (Administrador) e "operador" (Operador), com as senhas de
  SEED_ADMIN_PASSWORD / SEED_OPERATOR_PASSWORD (padrão "raizes-admin-2026" /
  "raizes-operador-2026"). Usuário que já existe mantém a senha, a não ser que a
  variável de ambiente venha preenchida.
* "Pernambucano Raiz" (posição 1, pontos corridos): regras do primeiro campeonato
  (3/1/0; pontos, vitórias, saldo, gols pró, confronto direto; Classificados 1–4 e
  Rebaixados 17–20), 20 clubes, turno único de 19 rodadas: as 9 primeiras encerradas, a
  10ª no dia (2 jogos encerrados, 2 ao vivo — um no 2º tempo, com gol anulado pelo VAR e
  2º amarelo, outro no 1º tempo — e o resto mais tarde), as demais agendadas.
* "Copa Pernambuco" (posição 2, grupos + mata-mata): fase de grupos (A e B, encerrada),
  semifinais de ida e volta com prorrogação (uma decidida na prorrogação, outra no
  agregado) e a final, em jogo único sem prorrogação, no dia.
* "Copa do Nordeste Raiz" (posição 3, jogos entre grupos): grupos A e B em que cada time
  enfrenta os quatro do outro grupo (como na Copa do Nordeste); 3 rodadas encerradas e a
  4ª no dia. Cada jogo conta para a tabela de cada time, no grupo dele.
* "Copa do Interior" (posição 4, grupos + "posição nos grupos"): grupos A, B e C; o 1º de
  cada grupo e o melhor 2º (classificação "Melhores segundos", zona condicional) avançam.
  2 rodadas encerradas e a 3ª no dia; o Grupo C fica com dois empates em todos os
  critérios (posições 1, 1, 3, 3).
* "Série A2 Raiz" (posição 5, turnos + classificação geral): 1º turno encerrado, 2º turno
  com 2 rodadas encerradas e a 3ª no dia, "Classificação geral" somando os dois turnos e
  uma punição de 3 pontos no 2º turno.

Horários: todo jogo encerrado ou agendado fica num dos horários de SLOTS (15h, 16h, 18h30,
19h, 20h, 20h30, 21h; nada de madrugada). Os jogos do dia que já deveriam ter terminado vão
para os horários de hoje que já passaram (ou de ontem) e os que ainda vão começar, para os
próximos (ou de amanhã). Só os jogos ao vivo começam "agora", e só quando isso cai entre
15h e 22h30: de madrugada ou de manhã não há jogo ao vivo (eles ficam agendados).

Todo lance e toda mudança de status passam pelos serviços (`matches.services`, origem
"script", com `at=` para datar os lances e o relógio dos jogos ao vivo fazer sentido).
Jogadores não têm cadastro: o elenco de cada time é uma lista de nomes gerada em memória
(`simulate_match.roster`), usada nas escalações e nos lances.

Com `--reset`, apaga antes o que o seed criou (em ordem segura de chaves estrangeiras),
mantendo os usuários; sem ele, recusa rodar sobre dados do seed já existentes. Com
`--clear`, só apaga (não recria): competições do seed com temporadas, fases, grupos,
rodadas, partidas, lances, confrontos, escalações, punições, a classificação e as
mensagens do outbox dessas partidas e fases; os times do seed que não são usados fora
dele; usuários e auditoria ficam.
"""

from __future__ import annotations

import math
import os
import random
import time as clock_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group as AuthGroup
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError

from accounts.roles import ADMINISTRATOR, OPERATOR, sync_roles
from competitions.models import (
    Competition,
    Group,
    GroupTeam,
    Round,
    Season,
    Stage,
    StageCriterion,
    StandingZone,
    Team,
)
from core import timeutils
from core.locks import locked_atomic
from matches.management.commands.simulate_match import (
    FIRST_NAMES,
    SURNAMES,
    VAR_INCIDENTS,
    MatchClock,
    Play,
    PlayRunner,
    Poster,
    Squad,
    SquadPlayer,
    pick_eleven,
    quiet_audit_log,
    random_plays,
    roster,
)
from matches.domain import Period
from matches.models import (
    Match,
    MatchBroadcast,
    MatchEvent,
    MatchLineup,
    MatchLineupPlayer,
    MatchOfficial,
    MatchStat,
    Tie,
)
from realtime.models import Outbox
from standings.models import PointAdjustment, Ranking, RankingCriterion, RankingZone, Standing

SEED_SLUGS = ("pernambucano-raiz", "copa-pernambuco", "copa-do-nordeste-raiz", "copa-do-interior", "serie-a2-raiz")
FIRST_CHAMPIONSHIP_CRITERIA = ("points", "wins", "goal_difference", "goals_for", "head_to_head")
RNG_SEED = 2026
TODAY_ROUND = 10  # rodada do Pernambucano no dia (as anteriores, encerradas)
# horários dos jogos: nada de madrugada (os ao vivo começam "agora", dentro de LIVE_WINDOW)
SLOTS = (time(15, 0), time(16, 0), time(18, 30), time(19, 0), time(20, 0), time(20, 30), time(21, 0))
LEAGUE_SLOTS = (time(15, 0), time(16, 0), time(18, 30), time(20, 0), time(21, 0))  # 5 jogos por dia
MIDWEEK_SLOTS = (time(19, 0), time(20, 30), time(21, 0), time(19, 0))
LIVE_WINDOW = (time(15, 0), time(22, 30))  # jogo ao vivo só começa nesse intervalo
GAME_LENGTH = timedelta(minutes=125)  # do início ao fim, com intervalo e acréscimos
PER_SLOT = 2  # jogos da mesma competição no mesmo horário
FORMATIONS = {"4-3-3": (4, 3, 3), "4-4-2": (4, 4, 2), "4-2-3-1": (4, 5, 1), "3-5-2": (3, 5, 2), "4-1-4-1": (4, 5, 1)}


@dataclass(frozen=True)
class Club:
    name: str
    short: str
    city: str
    primary: str
    secondary: str
    venue: str
    venue_city: str
    size: int  # 3 = grande, 2 = médio, 1 = pequeno (público e renda)


PERNAMBUCANO_CLUBS = (
    Club("Sport", "SPT", "Recife", "#D7141A", "#000000", "Ilha do Retiro", "Recife, PE", 3),
    Club("Náutico", "NAU", "Recife", "#C8102E", "#FFFFFF", "Aflitos", "Recife, PE", 3),
    Club("Santa Cruz", "SCZ", "Recife", "#000000", "#E30613", "Arruda", "Recife, PE", 3),
    Club("Retrô", "RET", "Camaragibe", "#00539F", "#F58025", "Arena de Pernambuco", "São Lourenço da Mata, PE", 2),
    Club("Central", "CEN", "Caruaru", "#1B1B1B", "#FFFFFF", "Lacerdão", "Caruaru, PE", 2),
    Club("Salgueiro", "SAL", "Salgueiro", "#B5121B", "#FFFFFF", "Cornélio de Barros", "Salgueiro, PE", 2),
    Club("Afogados da Ingazeira", "AFO", "Afogados da Ingazeira", "#0B3D91", "#FFFFFF", "Vianão", "Afogados da Ingazeira, PE", 1),
    Club("Petrolina", "PET", "Petrolina", "#E30613", "#0B3D91", "Paulo Coelho", "Petrolina, PE", 1),
    Club("Maguary", "MAG", "Bonito", "#0057A8", "#FFFFFF", "Estádio Municipal de Bonito", "Bonito, PE", 1),
    Club("Vitória das Tabocas", "VIT", "Vitória de Santo Antão", "#E03A3E", "#FFFFFF", "Carneirão", "Vitória de Santo Antão, PE", 1),
    Club("Íbis", "IBI", "Paulista", "#000000", "#D7141A", "Ademir Cunha", "Paulista, PE", 1),
    Club("Jaguar", "JAG", "Jaboatão dos Guararapes", "#F2B705", "#000000", "Estádio Municipal de Jaboatão", "Jaboatão dos Guararapes, PE", 1),
    Club("Decisão", "DEC", "Goiana", "#003F87", "#E30613", "Estádio Municipal de Goiana", "Goiana, PE", 1),
    Club("Belo Jardim", "BEL", "Belo Jardim", "#0E8A4A", "#FFFFFF", "Mendonção", "Belo Jardim, PE", 1),
    Club("América-PE", "AME", "Recife", "#00843D", "#FFFFFF", "Ademir Cunha", "Paulista, PE", 1),
    Club("Porto", "POR", "Caruaru", "#1D4E9E", "#FFFFFF", "Lacerdão", "Caruaru, PE", 1),
    Club("Serra Talhada", "SER", "Serra Talhada", "#7A1F1F", "#F2B705", "Pereirão", "Serra Talhada, PE", 1),
    Club("Flamengo de Arcoverde", "FLA", "Arcoverde", "#C8102E", "#000000", "Áureo Bradley", "Arcoverde, PE", 1),
    Club("Ypiranga", "YPI", "Santa Cruz do Capibaribe", "#006B3F", "#F2B705", "Limeirão", "Santa Cruz do Capibaribe, PE", 1),
    Club("Atlético Pernambucano", "ATL", "Carpina", "#B71C1C", "#000000", "Estádio Municipal de Carpina", "Carpina, PE", 1),
)

COPA_CLUBS = (
    Club("Vera Cruz", "VER", "Vitória de Santo Antão", "#0E6B3A", "#FFFFFF", "Carneirão", "Vitória de Santo Antão, PE", 1),
    Club("Sete de Setembro", "SET", "Garanhuns", "#12306B", "#FFFFFF", "Estádio Municipal de Garanhuns", "Garanhuns, PE", 1),
    Club("Centro Limoeirense", "CLI", "Limoeiro", "#C8102E", "#FFFFFF", "Estádio Municipal de Limoeiro", "Limoeiro, PE", 1),
    Club("Cabense", "CAB", "Cabo de Santo Agostinho", "#1B1B1B", "#F2B705", "Estádio Municipal do Cabo", "Cabo de Santo Agostinho, PE", 1),
    Club("Barreiros", "BAR", "Barreiros", "#0057A8", "#F2B705", "Estádio Municipal de Barreiros", "Barreiros, PE", 1),
    Club("Arcoverde", "ARC", "Arcoverde", "#F2B705", "#12306B", "Áureo Bradley", "Arcoverde, PE", 1),
    Club("Unibol", "UNI", "Paulista", "#E87722", "#1B1B1B", "Ademir Cunha", "Paulista, PE", 1),
    Club("Torre", "TOR", "Recife", "#7A1F1F", "#FFFFFF", "Estádio Municipal do Recife", "Recife, PE", 1),
)

NORDESTE_CLUBS = (
    Club("Bahia", "BAH", "Salvador", "#0055A4", "#E30613", "Fonte Nova", "Salvador, BA", 3),
    Club("Vitória", "VTR", "Salvador", "#E30613", "#000000", "Barradão", "Salvador, BA", 3),
    Club("Fortaleza", "FOR", "Fortaleza", "#0B3D91", "#E30613", "Castelão", "Fortaleza, CE", 3),
    Club("Ceará", "CEA", "Fortaleza", "#000000", "#FFFFFF", "Presidente Vargas", "Fortaleza, CE", 3),
    Club("CRB", "CRB", "Maceió", "#E30613", "#FFFFFF", "Rei Pelé", "Maceió, AL", 2),
    Club("CSA", "CSA", "Maceió", "#0057A8", "#FFFFFF", "Rei Pelé", "Maceió, AL", 2),
    Club("ABC", "ABC", "Natal", "#000000", "#FFFFFF", "Frasqueirão", "Natal, RN", 2),
    Club("Botafogo-PB", "BOT", "João Pessoa", "#000000", "#FFFFFF", "Almeidão", "João Pessoa, PB", 2),
)
ALL_CLUBS = (*PERNAMBUCANO_CLUBS, *COPA_CLUBS, *NORDESTE_CLUBS)

COACH_FIRST_NAMES = ("Givanildo", "Waldemar", "Severino", "Evaristo", "Nelson", "Arnaldo", "Ivan", "Gilberto", "Roberval", "Ademir", "Hélcio", "Josias")
STATES = ("PE", "PE", "PE", "PB", "AL", "RN", "BA", "CE", "SE")
BROADCASTERS = (
    ("TV Frevo", MatchBroadcast.Kind.OPEN_TV, "https://tvfrevo.example/ao-vivo"),
    ("Raízes Play", MatchBroadcast.Kind.STREAMING, "https://play.futeboldaraizes.example/jogos/{match}"),
    ("Canal Maracatu Sports", MatchBroadcast.Kind.PAY_TV, "https://maracatusports.example/futebol"),
    ("Rádio Capibaribe", MatchBroadcast.Kind.RADIO, "https://radiocapibaribe.example/ouvir"),
)


def poisson(rng: random.Random, mean: float, cap: int = 5) -> int:
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return min(k, cap)
        k += 1


def round_robin(teams: list) -> list[list[tuple]]:
    """Turno único pelo método do círculo, alternando mando."""
    count = len(teams)
    fixed, rotating = teams[0], list(teams[1:])
    rounds = []
    for number in range(count - 1):
        current = [fixed, *rotating]
        pairs = []
        for i in range(count // 2):
            first, second = current[i], current[count - 1 - i]
            pairs.append((first, second) if (number + i) % 2 == 0 else (second, first))
        rounds.append(pairs)
        rotating = rotating[-1:] + rotating[:-1]
    return rounds


def floor_to(moment: datetime, minutes: int) -> datetime:
    local = moment.astimezone(timeutils.app_tz())
    floored = local.replace(minute=local.minute - local.minute % minutes, second=0, microsecond=0)
    return floored.astimezone(moment.tzinfo)


def ceil_to(moment: datetime, minutes: int) -> datetime:
    floored = floor_to(moment, minutes)
    return floored if floored == moment else floored + timedelta(minutes=minutes)


def local_dt(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=timeutils.app_tz())


def purge_outbox_for(match_ids: set[int], stage_ids: set[int]) -> int:
    """Apaga do outbox as mensagens das partidas (`match`) e fases (`standings`) dadas e
    as listas de gols (`goals`) que citam alguma das partidas. Precisa da trava de escrita."""
    if not match_ids and not stage_ids:
        return 0
    doomed = set(
        Outbox.objects.filter(topic=Outbox.Topic.MATCH, payload__match__id__in=list(match_ids)).values_list("id", flat=True)
    ) | set(Outbox.objects.filter(topic=Outbox.Topic.STANDINGS, payload__stage_id__in=list(stage_ids)).values_list("id", flat=True))
    for row_id, payload in Outbox.objects.filter(topic=Outbox.Topic.GOALS).values_list("id", "payload").iterator():
        goals = [change.get("goal") or {} for change in payload.get("changes") or []] + list(payload.get("latest_goals") or [])
        if any(isinstance(goal, dict) and goal.get("match_id") in match_ids for goal in goals):
            doomed.add(row_id)
    deleted, _ = Outbox.objects.filter(id__in=doomed).delete()
    return deleted


class Seeder:
    def __init__(self, day: date, out):
        self.day = day
        self.out = out
        self.rng = random.Random(RNG_SEED)
        now = timeutils.now()
        # Instante de referência dos jogos do dia: agora (se o dia é hoje) ou 17h40 do dia pedido.
        self.ref_now = now if day == timeutils.local_today(now) else local_dt(day, time(17, 40))
        self.day_start, self.day_end = timeutils.day_bounds(day)
        self.teams: dict[str, Team] = {}
        self.clubs: dict[int, Club] = {}
        self.players: dict[int, list[SquadPlayer]] = {}  # elenco em memória (sem cadastro)
        self.operator = None
        self.counts = {"matches": 0, "events": 0}
        self.live: list[Match] = []  # jogos deixados ao vivo (para o aviso final)

    # --- Apagar -------------------------------------------------------------------------------

    @staticmethod
    def has_seed_data() -> bool:
        return Competition.objects.filter(slug__in=SEED_SLUGS).exists()

    def reset(self) -> dict[str, int]:
        """Apaga o que o seed criou (mantém usuários e auditoria) e devolve as contagens.

        Também tira do outbox as mensagens das partidas e fases apagadas (`match` e
        `standings`) e as listas de gols (`goals`) que citam essas partidas — com a
        trava de escrita, para não cruzar com um lançamento: uma página que reconecte
        não recebe de novo uma partida que não existe mais."""
        with locked_atomic():
            competitions = Competition.objects.filter(slug__in=SEED_SLUGS)
            matches = Match.objects.filter(stage__season__competition__in=competitions)
            match_ids = set(matches.values_list("id", flat=True))
            stage_ids = set(Stage.objects.filter(season__competition__in=competitions).values_list("id", flat=True))
            counts = {"competitions": competitions.count(), "matches": len(match_ids), "outbox": 0, "teams": 0}
            events = MatchEvent.objects.filter(match__in=matches)
            counts["events"] = events.count()
            events.update(annuls_event=None)  # gol anulado aponta para o gol (PROTECT)
            events.delete()
            matches.delete()  # escalações, arbitragem, transmissões e estatísticas vão junto
            Tie.objects.filter(stage__season__competition__in=competitions).delete()
            competitions.delete()  # temporadas, fases, critérios, zonas, punições, grupos, rodadas, classificação
            counts["outbox"] = purge_outbox_for(match_ids, stage_ids)
            kept = []
            for team in Team.objects.filter(name__in=[club.name for club in ALL_CLUBS]):
                try:
                    with transaction.atomic():
                        team.delete()
                        counts["teams"] += 1
                except ProtectedError:
                    kept.append(team.name)  # usado fora do seed: fica (e é reaproveitado)
        if kept:
            self.out.write(f"Times mantidos (usados fora do seed): {', '.join(sorted(kept))}.")
        return counts

    # --- Usuários ------------------------------------------------------------------------------

    def ensure_users(self) -> list[tuple[str, str, str | None]]:
        sync_roles()
        User = get_user_model()
        result = []
        specs = (
            ("admin", ADMINISTRATOR, "SEED_ADMIN_PASSWORD", "raizes-admin-2026", "Administração", "Raízes"),
            ("operador", OPERATOR, "SEED_OPERATOR_PASSWORD", "raizes-operador-2026", "Operador", "de Jogo"),
        )
        for username, role, env_name, default, first_name, last_name in specs:
            from_env = os.environ.get(env_name) or None
            user, created = User.objects.get_or_create(
                username=username, defaults={"first_name": first_name, "last_name": last_name, "is_staff": True}
            )
            password = None
            if created or from_env:
                password = from_env or default
                user.set_password(password)
            user.is_staff = True
            user.is_active = True
            user.save()
            user.groups.add(AuthGroup.objects.get(name=role))
            result.append((username, role, password))
            if username == "operador":
                self.operator = user
        return result

    # --- Times e elencos --------------------------------------------------------------------

    def ensure_teams(self) -> None:
        for club in ALL_CLUBS:
            team, _ = Team.objects.update_or_create(
                name=club.name,
                defaults={
                    "short_name": club.short,
                    "city": club.city,
                    "color_primary": club.primary,
                    "color_secondary": club.secondary,
                },
            )
            self.teams[club.name] = team
            self.clubs[team.pk] = club
            self.players[team.pk] = roster(team)  # nomes em memória: jogador não tem cadastro

    def coach(self, team: Team) -> str:
        rng = random.Random(f"tecnico:{team.name}")
        return f"{rng.choice(COACH_FIRST_NAMES)} {rng.choice(SURNAMES)}"

    # --- Estrutura ------------------------------------------------------------------------------

    @staticmethod
    def stage_rules(stage: Stage, zones: list[tuple[str, str, int, int]]) -> None:
        StageCriterion.objects.bulk_create(
            StageCriterion(stage=stage, position=n, key=key) for n, key in enumerate(FIRST_CHAMPIONSHIP_CRITERIA, start=1)
        )
        StandingZone.objects.bulk_create(
            StandingZone(stage=stage, name=name, color=color, position_from=start, position_to=end) for name, color, start, end in zones
        )

    def venue(self, team: Team) -> tuple[str, str]:
        club = self.clubs[team.pk]
        return club.venue, club.venue_city

    def create_match(self, stage: Stage, home: Team, away: Team, kickoff: datetime, *, group=None, round_=None, tie=None, leg=None, venue=None) -> Match:
        place, city = venue or self.venue(home)
        self.counts["matches"] += 1
        return Match.objects.create(
            stage=stage, group=group, round=round_, tie=tie, leg=leg, home_team=home, away_team=away,
            kickoff_at=kickoff, venue=place, city=city,
        )

    # --- Lançar jogos -------------------------------------------------------------------------

    def poster(self, match: Match) -> Poster:
        match = Match.objects.select_related("home_team", "away_team").get(pk=match.pk)
        squads = {"home": Squad.for_match(match, match.home_team), "away": Squad.for_match(match, match.away_team)}
        return Poster(match, self.operator, squads, key_prefix=f"seed-{match.pk}")

    def play(self, match: Match, plays: list[Play], start: datetime, *, first_stoppage: int, second_stoppage: int, until: datetime | None = None, scale: float = 1.0) -> Poster:
        poster = self.poster(match)
        clock = MatchClock(start, first_stoppage, second_stoppage, scale=scale)
        PlayRunner(poster, self.rng, clock).run(sorted(plays, key=Play.sort_key), until=until)
        self.counts["events"] += poster.n
        return poster

    def play_finished(self, match: Match, home_goals: int, away_goals: int, start: datetime, *, scale: float = 1.0, subs=(0, 2), tail: list[Play] | None = None) -> Poster:
        """Jogo inteiro com o placar pedido. `tail` troca o fim de jogo do tempo normal
        (ex.: prorrogação)."""
        s1, s2 = self.rng.randint(1, 4), self.rng.randint(3, 6)
        plays = random_plays(
            self.rng, home_goals, away_goals, first_stoppage=s1, second_stoppage=s2, substitutions=subs, yellow_cards=(1, 5)
        )
        if tail is not None:
            plays = [play for play in plays if play.kind != "match_end"] + tail
        return self.play(match, plays, start, first_stoppage=s1, second_stoppage=s2, scale=scale)

    def random_score(self) -> tuple[int, int]:
        return poisson(self.rng, 1.45), poisson(self.rng, 1.1)

    # --- Enriquecimento -----------------------------------------------------------------------

    def add_lineup(self, match: Match, team: Team) -> None:
        formation = self.rng.choice(tuple(FORMATIONS))
        starters, bench = pick_eleven(self.players[team.pk], FORMATIONS[formation])
        lineup = MatchLineup.objects.create(match=match, team=team, formation=formation, coach=self.coach(team))
        MatchLineupPlayer.objects.bulk_create(
            [
                MatchLineupPlayer(lineup=lineup, name=p.name, number=p.number, position=p.position, starter=True, order=n)
                for n, p in enumerate(starters, start=1)
            ]
            + [
                MatchLineupPlayer(lineup=lineup, name=p.name, number=p.number, position=p.position, starter=False, order=n)
                for n, p in enumerate(bench[:5], start=1)
            ]
        )

    def add_officials_and_broadcasts(self, match: Match) -> None:
        rng = self.rng

        def person() -> str:
            return f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)} {rng.choice(SURNAMES)}"

        MatchOfficial.objects.bulk_create(
            [
                MatchOfficial(match=match, role=MatchOfficial.Role.REFEREE, name=person(), state=rng.choice(STATES), order=1),
                MatchOfficial(match=match, role=MatchOfficial.Role.ASSISTANT, name=person(), state=rng.choice(STATES), order=2),
                MatchOfficial(match=match, role=MatchOfficial.Role.ASSISTANT, name=person(), state=rng.choice(STATES), order=3),
                MatchOfficial(match=match, role=MatchOfficial.Role.FOURTH, name=person(), state="PE", order=4),
                MatchOfficial(match=match, role=MatchOfficial.Role.VAR, name=person(), state=rng.choice(STATES), order=5),
            ]
        )
        chosen = [BROADCASTERS[1], rng.choice((BROADCASTERS[0], BROADCASTERS[2])), BROADCASTERS[3]]
        MatchBroadcast.objects.bulk_create(
            MatchBroadcast(match=match, name=name, kind=kind, url=url.format(match=match.pk), order=n)
            for n, (name, kind, url) in enumerate(chosen, start=1)
        )

    def add_crowd(self, match: Match) -> None:
        size = self.clubs[match.home_team_id].size
        low, high, ticket = {3: (16000, 38000, (40, 70)), 2: (4000, 12000, (25, 40)), 1: (800, 4500, (15, 30))}[size]
        attendance = self.rng.randint(low, high)
        revenue = attendance * self.rng.randint(*ticket) * 100 + self.rng.randint(0, 99) * 100
        Match.objects.filter(pk=match.pk).update(attendance=attendance, revenue_cents=revenue)

    def add_stats(self, match: Match, home_goals: int, away_goals: int, fraction: float = 1.0) -> None:
        rng = self.rng
        possession = rng.randint(38, 64)
        rows = []
        for team, goals, share in ((match.home_team, home_goals, possession), (match.away_team, away_goals, 100 - possession)):
            shots = max(goals + 1, round(rng.randint(6, 18) * fraction))
            on_target = min(shots, max(goals, round(shots * rng.uniform(0.3, 0.55))))
            values = {
                MatchStat.Key.POSSESSION: share,
                MatchStat.Key.SHOTS: shots,
                MatchStat.Key.SHOTS_ON_TARGET: on_target,
                MatchStat.Key.CORNERS: round(rng.randint(2, 9) * fraction),
                MatchStat.Key.FOULS: round(rng.randint(9, 19) * fraction),
                MatchStat.Key.OFFSIDES: round(rng.randint(0, 5) * fraction),
                MatchStat.Key.SAVES: round(rng.randint(1, 6) * fraction),
            }
            rows.extend(MatchStat(match=match, team=team, key=key, value=value) for key, value in values.items())
        MatchStat.objects.bulk_create(rows)

    # --- Pernambucano Raiz -------------------------------------------------------------------

    def pernambucano(self) -> None:
        competition = Competition.objects.create(name="Pernambucano Raiz", slug="pernambucano-raiz", short_name="PE Raiz", position=1)
        season = Season.objects.create(competition=competition, year=self.day.year)
        stage = Stage.objects.create(
            season=season, name="Fase única", position=1, format=Stage.Format.LEAGUE, points_win=3, points_draw=1, points_loss=0
        )
        self.stage_rules(stage, [("Classificados", "#1B7F3B", 1, 4), ("Rebaixados", "#B3261E", 17, 20)])
        group = stage.groups.get()
        teams = [self.teams[club.name] for club in PERNAMBUCANO_CLUBS]
        GroupTeam.objects.bulk_create(GroupTeam(group=group, team=team) for team in teams)
        rounds = [Round(stage=stage, number=n, name=f"Rodada {n}") for n in range(1, len(teams))]
        Round.objects.bulk_create(rounds)
        rounds = list(Round.objects.filter(stage=stage).order_by("number"))

        schedule = round_robin(teams)
        sport, nautico, santa = self.teams["Sport"], self.teams["Náutico"], self.teams["Santa Cruz"]
        classic = next(n for n, pairs in enumerate(schedule) if any({home.pk, away.pk} == {sport.pk, nautico.pk} for home, away in pairs))
        schedule[classic], schedule[TODAY_ROUND - 1] = schedule[TODAY_ROUND - 1], schedule[classic]
        today = [(sport, nautico) if {home.pk, away.pk} == {sport.pk, nautico.pk} else (home, away) for home, away in schedule[TODAY_ROUND - 1]]
        schedule[TODAY_ROUND - 1] = today

        for number, pairs in enumerate(schedule, start=1):
            round_ = rounds[number - 1]
            if number < TODAY_ROUND:
                self.league_past_round(stage, group, round_, pairs, number)
            elif number == TODAY_ROUND:
                self.league_today(stage, group, round_, pairs, sport, nautico, santa)
            else:
                self.league_future_round(stage, group, round_, pairs, number)

    def league_day(self, number: int, index: int) -> date:
        """Sábado e domingo de cada rodada (5 jogos por dia), uma rodada por semana."""
        weeks = number - TODAY_ROUND
        return self.day + timedelta(days=7 * weeks - (1 if index < 5 else 0))

    def league_past_round(self, stage, group, round_, pairs, number: int) -> None:
        for index, (home, away) in enumerate(pairs):
            kickoff = local_dt(self.league_day(number, index), LEAGUE_SLOTS[index % 5])
            match = self.create_match(stage, home, away, kickoff, group=group, round_=round_)
            home_goals, away_goals = self.random_score()
            self.play_finished(match, home_goals, away_goals, kickoff + timedelta(minutes=self.rng.randint(0, 3)))

    def league_future_round(self, stage, group, round_, pairs, number: int) -> None:
        for index, (home, away) in enumerate(pairs):
            kickoff = local_dt(self.league_day(number, index), LEAGUE_SLOTS[index % 5])
            self.create_match(stage, home, away, kickoff, group=group, round_=round_)

    # --- Horários ------------------------------------------------------------------------------

    def slots(self, day: date) -> list[datetime]:
        return [local_dt(day, at) for at in SLOTS]

    @staticmethod
    def fill(pool: list[datetime], count: int) -> list[datetime]:
        """Os `count` primeiros horários da lista, com até PER_SLOT jogos em cada."""
        return [kickoff for kickoff in pool for _ in range(PER_SLOT)][:count]

    def finished_kickoffs(self, count: int) -> list[datetime]:
        """Jogos do dia já encerrados: os horários de hoje que já terminaram, do mais recente
        para trás (até PER_SLOT jogos em cada); faltando, os de ontem."""
        today = [kickoff for kickoff in self.slots(self.day) if kickoff + GAME_LENGTH <= self.ref_now]
        pool = sorted(today, reverse=True) + sorted(self.slots(self.day - timedelta(days=1)), reverse=True)
        return sorted(self.fill(pool, count))

    def scheduled_kickoffs(self, count: int) -> list[datetime]:
        """Jogos do dia que ainda vão começar: os próximos horários de hoje (a partir de 20 min
        depois de agora), até PER_SLOT jogos em cada; faltando, os dos dias seguintes."""
        soon = self.ref_now + timedelta(minutes=20)
        pool = [kickoff for kickoff in self.slots(self.day) if kickoff >= soon]
        for ahead in (1, 2):
            pool += self.slots(self.day + timedelta(days=ahead))
        return self.fill(pool, count)

    def final_kickoff(self) -> datetime:
        """A final: o último horário de hoje que ainda não começou (ou 16h de amanhã)."""
        soon = self.ref_now + timedelta(minutes=20)
        today = [kickoff for kickoff in self.slots(self.day) if kickoff >= soon]
        return today[-1] if today else local_dt(self.day + timedelta(days=1), time(16, 0))

    def live_allowed(self) -> bool:
        """Jogo ao vivo começa "agora" (o do 2º tempo, ~90 min atrás; o do 1º, ~25 min atrás):
        só quando os dois começam dentro de LIVE_WINDOW — de madrugada ou de manhã, não há."""
        low, high = (local_dt(self.day, at) for at in LIVE_WINDOW)
        return low <= self.ref_now - timedelta(minutes=90) and self.ref_now - timedelta(minutes=24) <= high

    def league_today(self, stage, group, round_, pairs, sport, nautico, santa) -> None:
        live_late = next(pair for pair in pairs if pair == (sport, nautico))
        live_early = next(pair for pair in pairs if santa in pair)
        others = [pair for pair in pairs if pair not in (live_late, live_early)]
        finished, scheduled = others[:2], others[2:]
        if not self.live_allowed():  # de madrugada ou de manhã: os dois jogos ficam para mais tarde
            self.league_scheduled(stage, group, round_, [live_late, live_early, *scheduled])
            for (home, away), kickoff in zip(finished, self.finished_kickoffs(len(finished)), strict=True):
                self.league_finished_today(stage, group, round_, home, away, kickoff)
            return

        # Encerrados mais cedo (num horário de hoje que já terminou, ou de ontem).
        for (home, away), kickoff in zip(finished, self.finished_kickoffs(len(finished)), strict=True):
            self.league_finished_today(stage, group, round_, home, away, kickoff)

        # Ao vivo no 2º tempo (~70'): Sport × Náutico, com gol anulado pelo VAR e 2º amarelo.
        second_half = self.ref_now - timedelta(minutes=25, seconds=20)
        start = second_half - timedelta(minutes=15 + 45 + 3)
        match = self.create_match(stage, sport, nautico, floor_to(start, 5), group=group, round_=round_)
        self.enrich(match, lineups=True, stats=(2, 1, 0.75))
        plays = [
            Play(Period.FIRST_HALF, 0, "match_start"),
            Play(Period.FIRST_HALF, 12, "yellow", "away"),
            Play(Period.FIRST_HALF, 18, "goal", "home"),
            Play(Period.FIRST_HALF, 31, "penalty_goal", "away"),
            Play(Period.FIRST_HALF, 38, "yellow", "home"),
            Play(Period.FIRST_HALF, 45, "stoppage", extra={"minutes": 3}),
            Play(Period.FIRST_HALF, 45, "yellow", "away", stoppage=2, extra={"tag": "expulso"}),
            Play(Period.FIRST_HALF, 45, "half_time", stoppage=3),
            Play(Period.SECOND_HALF, 45, "second_half_start"),
            Play(Period.SECOND_HALF, 52, "goal", "home"),
            Play(Period.SECOND_HALF, 57, "sub", "away"),
            Play(Period.SECOND_HALF, 58, "goal", "away", extra={"tag": "anulado"}),
            Play(Period.SECOND_HALF, 60, "var_annul", "away", extra={"annul": "anulado", "var": VAR_INCIDENTS[0]}),
            Play(Period.SECOND_HALF, 62, "sub", "home"),
            Play(Period.SECOND_HALF, 62, "sub", "home"),
            Play(Period.SECOND_HALF, 66, "yellow", "away", extra={"tag": "expulso"}),  # 2º amarelo: vermelho automático
            Play(Period.SECOND_HALF, 68, "sub", "away"),
        ]
        self.play(match, plays, start, first_stoppage=3, second_stoppage=5, until=self.ref_now)
        self.live.append(match)

        # Ao vivo no 1º tempo (~25'): o jogo do Santa Cruz.
        home, away = live_early
        santa_side = "home" if home == santa else "away"
        other_side = "away" if santa_side == "home" else "home"
        start = self.ref_now - timedelta(minutes=24, seconds=30)
        match = self.create_match(stage, home, away, floor_to(start, 5), group=group, round_=round_)
        self.enrich(match, lineups=True, stats=(int(santa_side == "home"), int(santa_side == "away"), 0.3))
        plays = [
            Play(Period.FIRST_HALF, 0, "match_start"),
            Play(Period.FIRST_HALF, 9, "yellow", other_side),
            Play(Period.FIRST_HALF, 17, "goal", santa_side),
            Play(Period.FIRST_HALF, 22, "yellow", santa_side),
        ]
        self.play(match, plays, start, first_stoppage=2, second_stoppage=4, until=self.ref_now)
        self.live.append(match)

        # Mais tarde (nos próximos horários), dois jogos por horário.
        self.league_scheduled(stage, group, round_, scheduled)

    def league_finished_today(self, stage, group, round_, home, away, kickoff) -> None:
        match = self.create_match(stage, home, away, kickoff, group=group, round_=round_)
        home_goals, away_goals = self.random_score()
        self.enrich(match, lineups=True, stats=(home_goals, away_goals, 1.0))
        self.play_finished(match, home_goals, away_goals, kickoff + timedelta(minutes=2), subs=(3, 5))

    def league_scheduled(self, stage, group, round_, pairs) -> None:
        for (home, away), kickoff in zip(pairs, self.scheduled_kickoffs(len(pairs)), strict=True):
            match = self.create_match(stage, home, away, kickoff, group=group, round_=round_)
            self.enrich(match)

    def enrich(self, match: Match, *, lineups: bool = False, stats: tuple[int, int, float] | None = None) -> None:
        """Ficha do jogo do dia: arbitragem e transmissões; com bola rolando, escalações,
        público, renda e estatísticas. Gravado antes dos lances (que validam a escalação)."""
        self.add_officials_and_broadcasts(match)
        if lineups:
            self.add_lineup(match, match.home_team)
            self.add_lineup(match, match.away_team)
            self.add_crowd(match)
        if stats is not None:
            self.add_stats(match, *stats)

    # --- Copa Pernambuco ------------------------------------------------------------------------

    def copa(self) -> None:
        competition = Competition.objects.create(name="Copa Pernambuco", slug="copa-pernambuco", short_name="Copa PE", position=2)
        season = Season.objects.create(competition=competition, year=self.day.year)
        groups_stage = Stage.objects.create(season=season, name="Fase de grupos", position=1, format=Stage.Format.GROUPS)
        self.stage_rules(groups_stage, [("Classificados", "#1B7F3B", 1, 2)])
        knockout = Stage.objects.create(season=season, name="Mata-mata", position=2, format=Stage.Format.KNOCKOUT)
        teams = [self.teams[club.name] for club in COPA_CLUBS]
        groups = {
            "A": Group.objects.create(stage=groups_stage, name="Grupo A"),
            "B": Group.objects.create(stage=groups_stage, name="Grupo B"),
        }
        members = {"A": teams[:4], "B": teams[4:]}
        GroupTeam.objects.bulk_create(GroupTeam(group=groups[key], team=team) for key in groups for team in members[key])
        rounds = [Round.objects.create(stage=groups_stage, number=n, name=f"Rodada {n}") for n in (1, 2, 3)]

        for key, group in groups.items():
            for number, pairs in enumerate(round_robin(members[key]), start=1):
                match_day = self.day - timedelta(days=63 - 7 * (number - 1))
                for index, (home, away) in enumerate(pairs):
                    kickoff = local_dt(match_day, time(19, 0) if index == 0 else time(21, 0))
                    match = self.create_match(groups_stage, home, away, kickoff, group=group, round_=rounds[number - 1])
                    home_goals, away_goals = self.random_score()
                    self.play_finished(match, home_goals, away_goals, kickoff + timedelta(minutes=self.rng.randint(0, 3)))

        def top_two(group: Group) -> list[Team]:
            rows = Standing.objects.filter(group=group, kind=Standing.Kind.OFFICIAL).select_related("team").order_by("position")[:2]
            return [row.team for row in rows]

        first_a, second_a = top_two(groups["A"])
        first_b, second_b = top_two(groups["B"])
        semifinal = Round.objects.create(stage=knockout, number=1, name="Semifinal")
        final_round = Round.objects.create(stage=knockout, number=2, name="Final")

        # Semifinal 1: decidida na prorrogação do jogo de volta.
        tie_one = Tie.objects.create(stage=knockout, round=semifinal, position=1, legs=2, extra_time=True, team_a=second_b, team_b=first_a)
        # Semifinal 2: decidida no agregado.
        tie_two = Tie.objects.create(stage=knockout, round=semifinal, position=2, legs=2, extra_time=True, team_a=second_a, team_b=first_b)
        first_leg, second_leg = self.day - timedelta(days=21), self.day - timedelta(days=14)
        for tie, kickoff_time, legs in (
            (tie_one, time(19, 0), ((1, 0), (1, 0, "extra"))),
            (tie_two, time(21, 0), ((0, 2), (1, 1, None))),
        ):
            kickoff = local_dt(first_leg, kickoff_time)
            match = self.create_match(knockout, tie.team_a, tie.team_b, kickoff, round_=semifinal, tie=tie, leg=1)
            self.play_finished(match, legs[0][0], legs[0][1], kickoff + timedelta(minutes=2))
            kickoff = local_dt(second_leg, kickoff_time)
            match = self.create_match(knockout, tie.team_b, tie.team_a, kickoff, round_=semifinal, tie=tie, leg=2)
            home_goals, away_goals, decided = legs[1]
            tail = None
            if decided == "extra":
                tail = [
                    Play(Period.EXTRA_TIME, 90, "extra_time_start"),
                    Play(Period.EXTRA_TIME, 104, "goal", "home"),
                    Play(Period.EXTRA_TIME, 105, "extra_half_time"),
                    Play(Period.EXTRA_SECOND_HALF, 105, "extra_second_half_start"),
                    Play(Period.EXTRA_SECOND_HALF, 120, "stoppage", extra={"minutes": 1}),
                    Play(Period.EXTRA_SECOND_HALF, 120, "match_end", stoppage=1),
                ]
            self.play_finished(match, home_goals, away_goals, kickoff + timedelta(minutes=2), tail=tail)

        tie_one.refresh_from_db()
        tie_two.refresh_from_db()
        if not (tie_one.winner_team_id and tie_two.winner_team_id):  # pragma: no cover - roteiro fixo
            raise CommandError("As semifinais deveriam ter vencedor.")
        final = Tie.objects.create(
            stage=knockout, round=final_round, position=1, legs=1, extra_time=False,
            team_a=tie_one.winner_team, team_b=tie_two.winner_team,
        )
        kickoff = self.final_kickoff()
        match = self.create_match(
            knockout, final.team_a, final.team_b, kickoff, round_=final_round, tie=final, leg=1,
            venue=("Arena de Pernambuco", "São Lourenço da Mata, PE"),
        )
        self.enrich(match)


    # --- Competições com outros casos de classificação ---------------------------------------

    def new_season(self, name: str, slug: str, short_name: str, position: int) -> Season:
        competition = Competition.objects.create(name=name, slug=slug, short_name=short_name, position=position)
        return Season.objects.create(competition=competition, year=self.day.year)

    @staticmethod
    def new_rounds(stage: Stage, count: int) -> list[Round]:
        return [Round.objects.create(stage=stage, number=n, name=f"Rodada {n}") for n in range(1, count + 1)]

    @staticmethod
    def ranking_rules(ranking: Ranking, criteria: tuple[str, ...], zones: list[tuple[str, str, int, int]]) -> None:
        RankingCriterion.objects.bulk_create(RankingCriterion(ranking=ranking, position=n, key=key) for n, key in enumerate(criteria, start=1))
        RankingZone.objects.bulk_create(
            RankingZone(ranking=ranking, name=name, color=color, position_from=start, position_to=end) for name, color, start, end in zones
        )

    def midweek(self, weeks_ago: int, index: int) -> datetime:
        """Jogo de rodada passada, numa quarta-feira à noite (MIDWEEK_SLOTS)."""
        return local_dt(self.day - timedelta(days=7 * weeks_ago + 3), MIDWEEK_SLOTS[index % len(MIDWEEK_SLOTS)])

    def past_match(self, stage, home, away, kickoff, *, group, round_, score=None) -> None:
        match = self.create_match(stage, home, away, kickoff, group=group, round_=round_)
        home_goals, away_goals = score if score is not None else self.random_score()
        self.play_finished(match, home_goals, away_goals, kickoff + timedelta(minutes=self.rng.randint(0, 3)))

    def scheduled_round(self, stage, round_, pairs, group_of) -> None:
        """Rodada do dia: os jogos nos próximos horários (dois por horário)."""
        for (home, away), kickoff in zip(pairs, self.scheduled_kickoffs(len(pairs)), strict=True):
            match = self.create_match(stage, home, away, kickoff, group=group_of(home), round_=round_)
            self.enrich(match)

    def nordeste(self) -> None:
        """Jogos entre grupos (como na Copa do Nordeste): cada time do Grupo A enfrenta os
        quatro do Grupo B; o jogo conta para a tabela de cada time, no grupo dele."""
        season = self.new_season("Copa do Nordeste Raiz", "copa-do-nordeste-raiz", "Nordeste", 3)
        stage = Stage.objects.create(season=season, name="Fase de grupos", position=1, format=Stage.Format.GROUPS)
        self.stage_rules(stage, [("Classificados", "#1B7F3B", 1, 2)])
        teams = [self.teams[club.name] for club in NORDESTE_CLUBS]
        sides = {"A": teams[0::2], "B": teams[1::2]}
        groups = {key: Group.objects.create(stage=stage, name=f"Grupo {key}") for key in sides}
        GroupTeam.objects.bulk_create(GroupTeam(group=groups[key], team=team) for key in sides for team in sides[key])
        group_of = {team.pk: groups[key] for key in sides for team in sides[key]}  # o jogo fica no grupo do mandante
        rounds = self.new_rounds(stage, 4)
        for number, round_ in enumerate(rounds, start=1):
            pairs = []
            for index, team in enumerate(sides["A"]):
                rival = sides["B"][(index + number - 1) % 4]
                pairs.append((team, rival) if (index + number) % 2 == 0 else (rival, team))
            if number < len(rounds):
                for index, (home, away) in enumerate(pairs):
                    self.past_match(stage, home, away, self.midweek(len(rounds) - number, index), group=group_of[home.pk], round_=round_)
            else:
                self.scheduled_round(stage, round_, pairs, lambda team: group_of[team.pk])

    def interior(self) -> None:
        """Grupos e "posição nos grupos": o 1º de cada grupo e o melhor 2º (classificação
        "Melhores segundos", zona condicional) avançam. O Grupo C fica com dois empates em
        todos os critérios: posições exibidas 1, 1, 3, 3."""
        season = self.new_season("Copa do Interior", "copa-do-interior", "Interior", 4)
        stage = Stage.objects.create(season=season, name="Fase de grupos", position=1, format=Stage.Format.GROUPS)
        self.stage_rules(stage, [("Classificados", "#1B7F3B", 1, 1)])
        ranking = Ranking.objects.create(season=season, name="Melhores segundos", scope=Ranking.Scope.POSITION, position=1, group_position=2)
        ranking.stages.add(stage)
        ranking.show_on_stages.add(stage)
        self.ranking_rules(ranking, ("points", "wins", "goal_difference", "goals_for"), [("Classificado", "#1B7F3B", 1, 1)])
        StandingZone.objects.create(
            stage=stage, name="Melhor 2º", color="#2E7D9A", position_from=2, position_to=2,
            ranking=ranking, ranking_position_from=1, ranking_position_to=1,
        )
        teams = [self.teams[club.name] for club in PERNAMBUCANO_CLUBS[3:15]]
        members = {"A": teams[0:4], "B": teams[4:8], "C": teams[8:12]}
        groups = {key: Group.objects.create(stage=stage, name=f"Grupo {key}") for key in members}
        GroupTeam.objects.bulk_create(GroupTeam(group=groups[key], team=team) for key in members for team in members[key])
        rounds = self.new_rounds(stage, 3)
        # Grupo C: 1×1 e 0×0, depois 0×0 e 0×0 — dois blocos empatados em tudo (1, 1, 3, 3)
        tied = {1: ((1, 1), (0, 0)), 2: ((0, 0), (0, 0))}
        today = []
        for slot, (key, group) in enumerate(groups.items()):
            for number, pairs in enumerate(round_robin(members[key]), start=1):
                if number == len(rounds):
                    today.extend(pairs)
                    continue
                for index, (home, away) in enumerate(pairs):
                    score = tied[number][index] if key == "C" else None
                    kickoff = self.midweek(len(rounds) - number, 2 * slot + index)
                    self.past_match(stage, home, away, kickoff, group=group, round_=rounds[number - 1], score=score)
        group_of = {team.pk: groups[key] for key in members for team in members[key]}
        self.scheduled_round(stage, rounds[-1], today, lambda team: group_of[team.pk])

    def serie_a2(self) -> None:
        """Dois turnos e a "Classificação geral", que soma os dois (botão na página da
        competição); uma punição de 3 pontos no 2º turno."""
        season = self.new_season("Série A2 Raiz", "serie-a2-raiz", "A2", 5)
        teams = [self.teams[club.name] for club in COPA_CLUBS[:6]]
        turns = [
            Stage.objects.create(season=season, name=f"{n}º turno", position=n, format=Stage.Format.LEAGUE, points_win=3, points_draw=1, points_loss=0)
            for n in (1, 2)
        ]
        for stage in turns:
            self.stage_rules(stage, [("Vencedor do turno", "#1B7F3B", 1, 1)])
            GroupTeam.objects.bulk_create(GroupTeam(group=stage.groups.get(), team=team) for team in teams)
        ranking = Ranking.objects.create(season=season, name="Classificação geral", scope=Ranking.Scope.OVERALL, position=1, show_on_competition=True)
        ranking.stages.add(*turns)
        self.ranking_rules(ranking, FIRST_CHAMPIONSHIP_CRITERIA, [("Acesso", "#1B7F3B", 1, 2), ("Rebaixado", "#B3261E", 6, 6)])
        # punição antes dos jogos do 2º turno: cada lançamento recalcula o grupo já com ela
        PointAdjustment.objects.create(stage=turns[1], team=teams[-1], points=-3, reason="escalação irregular")
        schedule = round_robin(teams)  # 5 rodadas por turno
        today_round = 3  # do 2º turno
        for turn, stage in enumerate(turns, start=1):
            group = stage.groups.get()
            rounds = self.new_rounds(stage, len(schedule))
            for number, pairs in enumerate(schedule, start=1):
                round_ = rounds[number - 1]
                if turn == 2:
                    pairs = [(away, home) for home, away in pairs]  # returno: mando invertido
                if turn == 1 or number < today_round:
                    weeks_ago = (today_round - number) + (len(schedule) if turn == 1 else 0)
                    for index, (home, away) in enumerate(pairs):
                        self.past_match(stage, home, away, self.midweek(weeks_ago, index), group=group, round_=round_)
                elif number == today_round:
                    self.scheduled_round(stage, round_, pairs, lambda team, group=group: group)
                else:
                    day = self.day + timedelta(days=7 * (number - today_round) - 3)
                    for index, (home, away) in enumerate(pairs):
                        self.create_match(stage, home, away, local_dt(day, MIDWEEK_SLOTS[index]), group=group, round_=round_)


class Command(BaseCommand):
    help = "Carga de demonstração: cinco competições (pontos corridos, grupos, jogos entre grupos, turnos com classificação geral), jogos encerrados, ao vivo e do dia."

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--reset", action="store_true", help="Apaga os dados do seed antes e recria (mantém os usuários).")
        modes.add_argument(
            "--clear", action="store_true", help="Só apaga os dados do seed (competições, jogos, times do seed), sem recriar; mantém os usuários."
        )
        parser.add_argument("--date", default=None, help="Dia dos jogos ao vivo, YYYY-MM-DD (padrão: hoje em Brasília).")

    def handle(self, *args, reset=False, clear=False, date=None, **options):
        try:
            day = timeutils.parse_day(date) or timeutils.local_today()
        except ValueError as exc:
            raise CommandError("Use --date no formato YYYY-MM-DD.") from exc
        started = clock_time.monotonic()
        seeder = Seeder(day, self.stdout)
        if clear:
            if not seeder.has_seed_data():
                self.stdout.write("Nada a apagar: não há dados do seed.")
                return
            counts = seeder.reset()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dados do seed apagados: {counts['competitions']} competições, {counts['matches']} partidas, "
                    f"{counts['events']} lançamentos, {counts['teams']} times e {counts['outbox']} mensagens do outbox. "
                    "Usuários e auditoria mantidos."
                )
            )
            return
        with quiet_audit_log(), transaction.atomic():
            if seeder.has_seed_data():
                if not reset:
                    raise CommandError("Os dados do seed já existem. Rode com --reset para recriá-los (ou --clear para só apagar).")
                seeder.reset()
            users = seeder.ensure_users()
            seeder.ensure_teams()
            seeder.pernambucano()
            seeder.copa()
            seeder.nordeste()
            seeder.interior()
            seeder.serie_a2()
        elapsed = clock_time.monotonic() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"Seed pronto para {day.isoformat()}: {seeder.counts['matches']} partidas, "
                f"{seeder.counts['events']} lançamentos pelos serviços, em {elapsed:.1f} s."
            )
        )
        for username, role, password in users:
            shown = password if password else "(mantida: já existia)"
            self.stdout.write(f"  {role}: usuário “{username}”, senha {shown}")
        for match in seeder.live:
            self.stdout.write(
                f"  Ao vivo: partida {match.pk}, {match.home_team.name} × {match.away_team.name} "
                f"(continue com `python manage.py simulate_match {match.pk}`)"
            )
