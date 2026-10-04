"""Simula uma partida ao vivo, lance a lance, pelos serviços de escrita.

Uso: `python manage.py simulate_match <match_id> [--speed N] [--seed N] [--until-minute M] [--user operador]`

Leva a partida do estado em que está até o fim (ou até o minuto `--until-minute`),
lançando lances plausíveis — gols, cartões, substituições, acréscimos, VAR — com
`matches.services` (origem "script"), como o operador do seed. O tempo corre em
escala: `--speed` = minutos de jogo por minuto real (padrão 30: um jogo em ~4 min).
Com `--speed 1`, o relógio das páginas acompanha o minuto dos lances. No mata-mata
decide sozinho entre fim de jogo, prorrogação e pênaltis (`domain.available_actions`).

Este módulo também guarda as peças que o seed usa para lançar jogos inteiros com
horários do passado (`at=`): `Squad` (quem está em campo), `Poster` (lança e
imprime), `Play`/`MatchClock` (roteiro e relógio de um jogo), `PlayRunner` (lança o roteiro)
e `roster` (elenco de nomes plausíveis, em memória: jogadores não têm cadastro).
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from competitions.models import Team
from core import timeutils
from matches import context, domain, services
from matches.domain import EventType, NewEvent, Period, Status
from matches.models import Match, MatchLineupPlayer

GOAL_WEIGHTS = {"FW": 6.0, "MF": 3.0, "DF": 1.0, "GK": 0.0}
CARD_WEIGHTS = {"DF": 4.0, "MF": 4.0, "FW": 2.0, "GK": 0.4}
SUB_OUT_WEIGHTS = {"FW": 4.0, "MF": 4.0, "DF": 2.0, "GK": 0.0}
SHOOTOUT_ORDER = {"FW": 0, "MF": 1, "DF": 2, "GK": 3}
MAX_SUBSTITUTIONS = 5
PERIOD_INDEX = domain.PERIOD_ORDER

FIRST_NAMES = (
    "Matheus", "Gabriel", "Lucas", "Rafael", "Thiago", "Felipe", "Bruno", "Diego", "Igor", "Caio", "Vinícius",
    "Rodrigo", "André", "Leandro", "Wellington", "Jefferson", "Anderson", "Everton", "Renan", "Marcos", "Paulo",
    "Danilo", "Hugo", "Samuel", "Ramon", "Gustavo", "Luan", "Kauã", "Arthur", "Davi", "Pedro", "João", "Cícero",
    "Josué", "Edson", "Fábio", "Márcio", "Wagner", "Erick", "Iago", "Wesley", "Alisson", "Robson", "Elias",
    "Jonas", "Ítalo", "Jadson", "Wanderson", "Elton", "Patrick", "Rômulo", "Talles", "Yuri", "Douglas", "Emerson",
)
SURNAMES = (
    "Silva", "Santos", "Oliveira", "Souza", "Lima", "Pereira", "Ferreira", "Costa", "Rodrigues", "Almeida",
    "Nascimento", "Araújo", "Barbosa", "Cavalcanti", "Albuquerque", "Melo", "Ribeiro", "Carvalho", "Gomes",
    "Freitas", "Monteiro", "Bezerra", "Tavares", "Lins", "Moura", "Pessoa", "Cordeiro", "Batista", "Brandão",
    "Siqueira", "Veloso", "Queiroz", "Rocha", "Farias", "Leite", "Macedo", "Galvão", "Pontes", "Marinho", "Aragão",
)
NICKNAMES = (
    "Dudu", "Biel", "Netinho", "Thiaguinho", "Pedrinho", "Juninho", "Zé Roberto", "Toinho", "Ciço", "Galego",
    "Nino", "Didi", "Tonhão", "Caíque", "Juca", "Tita", "Bilu", "Nem", "Léo Paraíba", "Jajá",
)
ROSTER = (  # posição e número de cada um dos 18 jogadores do elenco
    ("GK", 1), ("DF", 2), ("DF", 3), ("DF", 4), ("MF", 5), ("DF", 6), ("FW", 7), ("MF", 8), ("FW", 9),
    ("MF", 10), ("FW", 11), ("GK", 12), ("DF", 13), ("DF", 14), ("MF", 15), ("MF", 16), ("FW", 17), ("FW", 18),
)

VAR_INCIDENTS = (
    ("Possível impedimento no lance do gol", "Gol anulado: impedimento", "Impedimento (VAR)"),
    ("Possível falta no início da jogada", "Gol anulado: falta no ataque", "Falta no ataque (VAR)"),
    ("Possível toque de mão do atacante", "Gol anulado: mão na bola", "Mão na bola (VAR)"),
)


@contextmanager
def quiet_audit_log():
    """A auditoria continua gravada no banco; só a linha de log de cada lance some do terminal."""
    logger = logging.getLogger("fdr.audit")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


def sleep(seconds: float) -> None:
    """Pausa entre minutos de jogo (os testes trocam por uma função vazia)."""
    if seconds > 0:
        time.sleep(seconds)


# --- Elenco em campo ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SquadPlayer:
    """Jogador do elenco (em memória): jogadores não têm cadastro, só nome."""

    name: str
    position: str = ""
    number: int | None = None


def roster(team: Team) -> list[SquadPlayer]:
    """Elenco de 18 nomes plausíveis do time, sempre o mesmo para o mesmo nome de time
    (sorteio com semente fixa), com posição e número. Nada é gravado no banco."""
    rng = random.Random(f"elenco:{team.name}")
    used: set[str] = set()
    players = []
    for position, number in ROSTER:
        for _ in range(50):
            name = rng.choice(NICKNAMES) if rng.random() < 0.22 else f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
            if name not in used:
                break
        used.add(name)
        players.append(SquadPlayer(name, position, number))
    return players


class Squad:
    """Elenco de um time na partida: titulares, banco, quem está em campo e cartões.

    As chaves de jogador são as do domínio (`domain.player_key`), para conferir com o
    estado derivado dos eventos ao retomar uma partida já começada."""

    def __init__(self, team: Team, starters: Sequence[SquadPlayer], bench: Sequence[SquadPlayer]):
        self.team = team
        self.starters = list(starters)
        self.bench = list(bench)
        self.on_field = list(starters)
        self.available_bench = list(bench)
        self.yellow: set[str] = set()
        self.sent_off: set[str] = set()
        self.substitutions = 0
        self.tags: dict[str, SquadPlayer] = {}

    @property
    def team_id(self) -> int:
        return self.team.pk

    def key(self, player: SquadPlayer) -> str:
        return domain.player_key(self.team_id, None, player.name)

    @classmethod
    def for_match(cls, match: Match, team: Team, rng: random.Random | None = None) -> Squad:
        """Escalação da partida; sem ela, um elenco de nomes gerados (`roster`, 4-4-2)."""
        entries = list(MatchLineupPlayer.objects.filter(lineup__match=match, lineup__team=team).order_by("-starter", "order", "id"))
        if entries:
            starters = [cls._from_entry(entry) for entry in entries if entry.starter]
            bench = [cls._from_entry(entry) for entry in entries if not entry.starter]
            return cls(team, starters, bench)
        starters, bench = pick_eleven(roster(team), (4, 4, 2))
        return cls(team, starters, bench)

    @staticmethod
    def _from_entry(entry: MatchLineupPlayer) -> SquadPlayer:
        return SquadPlayer(entry.name, entry.position, entry.number)

    def sync(self, state: domain.MatchState) -> None:
        """Ajusta quem está em campo pelo estado derivado dos eventos já lançados."""
        subbed_off, subbed_on, sent_off = state.subbed_off, state.subbed_on, state.sent_off
        everyone = self.starters + self.bench
        self.on_field = [
            p for p in everyone
            if (p in self.starters or self.key(p) in subbed_on) and self.key(p) not in subbed_off and self.key(p) not in sent_off
        ]
        self.available_bench = [
            p for p in self.bench if self.key(p) not in subbed_on and self.key(p) not in subbed_off and self.key(p) not in sent_off
        ]
        keys = {self.key(p) for p in everyone}
        self.yellow = {key for key, count in state.yellow_cards.items() if count >= 1 and key in keys}
        self.sent_off = {key for key in sent_off if key in keys}
        self.substitutions = len([key for key in subbed_on if key in keys])

    # --- escolhas ---

    def _weighted(self, rng: random.Random, people: Sequence[SquadPlayer], weights: dict[str, float]) -> SquadPlayer | None:
        options = [(p, weights.get(p.position, 1.0)) for p in people]
        options = [(p, w) for p, w in options if w > 0] or [(p, 1.0) for p in people]
        if not options:
            return None
        total = sum(w for _, w in options)
        roll = rng.uniform(0, total)
        for person, weight in options:
            roll -= weight
            if roll <= 0:
                return person
        return options[-1][0]

    def scorer(self, rng: random.Random) -> SquadPlayer | None:
        return self._weighted(rng, self.on_field, GOAL_WEIGHTS)

    def defender(self, rng: random.Random) -> SquadPlayer | None:
        return self._weighted(rng, self.on_field, {"DF": 5.0, "MF": 1.0, "GK": 0.5, "FW": 0.2})

    def assistant(self, rng: random.Random, scorer: SquadPlayer | None) -> SquadPlayer | None:
        people = [p for p in self.on_field if p != scorer]
        return self._weighted(rng, people, {"MF": 5.0, "FW": 3.0, "DF": 1.5, "GK": 0.0}) if people else None

    def carded(self, rng: random.Random, *, fresh: bool = True) -> SquadPlayer | None:
        people = [p for p in self.on_field if not fresh or self.key(p) not in self.yellow]
        return self._weighted(rng, people or self.on_field, CARD_WEIGHTS)

    def substitution(self, rng: random.Random) -> tuple[SquadPlayer, SquadPlayer] | None:
        if self.substitutions >= MAX_SUBSTITUTIONS or not self.available_bench:
            return None
        protected = set(self.tags.values())  # quem ainda tem lance marcado no roteiro fica em campo
        candidates = [p for p in self.on_field if p not in protected]
        booked_first = [p for p in candidates if self.key(p) in self.yellow and p.position != "GK"]
        leaving = rng.choice(booked_first) if booked_first and rng.random() < 0.5 else self._weighted(rng, candidates, SUB_OUT_WEIGHTS)
        if leaving is None:
            return None
        same = [p for p in self.available_bench if p.position == leaving.position]
        outfield = [p for p in self.available_bench if p.position != "GK"]
        entering = rng.choice(same or outfield or self.available_bench)
        return leaving, entering

    def shootout_takers(self) -> list[SquadPlayer]:
        return sorted(self.on_field, key=lambda p: (SHOOTOUT_ORDER.get(p.position, 1), -(p.number or 0)))

    # --- efeitos (espelham o domínio) ---

    def booked(self, player: SquadPlayer) -> None:
        key = self.key(player)
        if key in self.yellow:
            self.sent(player)
        self.yellow.add(key)

    def sent(self, player: SquadPlayer) -> None:
        self.sent_off.add(self.key(player))
        self.on_field = [p for p in self.on_field if p != player]

    def substituted(self, leaving: SquadPlayer, entering: SquadPlayer) -> None:
        self.on_field = [entering if p == leaving else p for p in self.on_field]
        self.available_bench = [p for p in self.available_bench if p != entering]
        self.substitutions += 1


def pick_eleven(players: Sequence, shape: tuple[int, int, int]) -> tuple[list, list]:
    """Onze titulares num esquema (defensores, meias, atacantes) e até 7 reservas.

    `players` são objetos com `.position` ("GK", "DF", "MF", "FW"); na falta de uma
    posição, completa com quem sobrar."""
    by_position: dict[str, list] = {"GK": [], "DF": [], "MF": [], "FW": []}
    others = []
    for player in players:
        by_position.get(player.position, others).append(player)
    wanted = {"GK": 1, "DF": shape[0], "MF": shape[1], "FW": shape[2]}
    starters = []
    for position, count in wanted.items():
        starters.extend(by_position[position][:count])
    rest = [p for p in players if p not in starters]
    while len(starters) < 11 and rest:
        starters.append(rest.pop(0))
    bench_gk = [p for p in rest if p.position == "GK"][:1]
    bench = bench_gk + [p for p in rest if p not in bench_gk][: 7 - len(bench_gk)]
    return starters, bench


# --- Lançar pelos serviços ------------------------------------------------------------------


class Poster:
    """Lança eventos e ações de status de uma partida pelos serviços (origem "script")
    e, com `out`, imprime cada lance."""

    def __init__(self, match: Match, user, squads: dict[str, Squad], *, key_prefix: str, out=None, confirm: bool = False):
        self.match = match
        self.match_id = match.pk
        self.user = user
        self.squads = squads  # {"home": Squad, "away": Squad}
        self.key_prefix = key_prefix
        self.out = out
        self.confirm = confirm
        self.n = 0
        self.score = (match.home_score, match.away_score)

    def _key(self) -> str:
        self.n += 1
        return f"{self.key_prefix}:{self.n}"

    def side_of(self, team_id: int | None) -> str | None:
        if team_id == self.squads["home"].team_id:
            return "home"
        if team_id == self.squads["away"].team_id:
            return "away"
        return None

    def post(
        self,
        type_: str,
        *,
        at: datetime | None = None,
        minute: int | None = None,
        stoppage: int | None = None,
        side: str | None = None,
        player: SquadPlayer | None = None,
        payload: dict | None = None,
        annuls_event_id: int | None = None,
    ) -> services.PostResult:
        payload = dict(payload or {})
        if player is not None and type_ != EventType.SUBSTITUTION:
            payload.setdefault("player", player.name)
        new = NewEvent(
            type=type_,
            minute=minute,
            stoppage=stoppage or None,
            team_id=self.squads[side].team_id if side else None,
            payload=payload,
            annuls_event_id=annuls_event_id,
        )
        result = services.post_event(
            self.match_id, self.user, new, idempotency_key=self._key(), source="script", confirm=self.confirm, at=at
        )
        self._report(result)
        return result

    def status(self, action: str, *, at: datetime | None = None, **kwargs) -> services.PostResult:
        result = services.change_status(self.match_id, self.user, action, idempotency_key=self._key(), source="script", at=at, **kwargs)
        self._report(result)
        return result

    def _report(self, result: services.PostResult) -> None:
        self.match = result.match
        self.score = (result.match.home_score, result.match.away_score)
        if self.out is None:
            return
        for event in (result.event, *result.derived):
            self.out.write(describe(event, result.match, self))


def describe(event, match: Match, poster: Poster | None = None) -> str:
    """Uma linha legível do lance (para o terminal)."""
    spec = domain.CATALOG.get(event.type)
    label = spec.label if spec else event.type
    minute = domain.format_minute(event.minute, event.stoppage) or "   "
    team = ""
    side = poster.side_of(event.team_id) if poster else None
    if side:
        team = poster.squads[side].team.short_name
    payload = event.payload if isinstance(event.payload, dict) else {}
    who = payload.get("player") or ""
    if event.type == EventType.SUBSTITUTION:
        who = f"sai {payload.get('player_out')}, entra {payload.get('player_in')}"
    elif event.type == EventType.STOPPAGE_TIME:
        who = f"+{payload.get('minutes')}"
    elif event.type == EventType.VAR_REVIEW:
        who = f"{payload.get('incident')} → {payload.get('decision')}"
    elif event.type == EventType.SHOOTOUT_KICK:
        who = f"{who} ({'converteu' if payload.get('scored') else 'perdeu'})"
    if payload.get("reason") == domain.SECOND_YELLOW:
        who = f"{who} (2º amarelo)"
    score = f"{match.home_team.short_name} {match.home_score} × {match.away_score} {match.away_team.short_name}"
    if match.home_penalties is not None:
        score += f" (pên. {match.home_penalties} × {match.away_penalties})"
    parts = [f"{minute:>6}", label]
    if team:
        parts.append(team)
    if who:
        parts.append(who)
    return f"{'  '.join(parts)}  [{score}]"


# --- Roteiro de um jogo (seed) --------------------------------------------------------------


@dataclass
class Play:
    """Um passo do roteiro: estrutural ("match_start", "half_time"...) ou lance
    ("goal", "penalty_goal", "penalty_miss", "own_goal", "yellow", "red", "sub",
    "stoppage", "var_annul"). Quem participa é escolhido na hora de lançar, com o
    elenco em campo naquele momento."""

    period: str
    minute: int
    kind: str
    side: str | None = None
    stoppage: int = 0
    extra: dict = field(default_factory=dict)

    def sort_key(self) -> tuple:
        priority = {"match_start": -2, "second_half_start": -2, "extra_time_start": -2, "penalties_start": -2}.get(self.kind, 0)
        if self.kind in ("half_time", "match_end"):
            priority = 9
        return (PERIOD_INDEX[self.period], self.minute, self.stoppage, priority)


class MatchClock:
    """Instantes reais de um jogo a partir do início (para datar os lances com `at`).

    `scale` < 1 encurta o jogo (quando não cabe no tempo disponível)."""

    def __init__(self, start: datetime, first_stoppage: int, second_stoppage: int, *, interval: int = 15, scale: float = 1.0, et_stoppage: int = 1):
        self.start = start
        self.scale = scale
        self.half_time = start + self._minutes(45 + first_stoppage)
        self.second_half = self.half_time + self._minutes(interval)
        self.regular_end = self.second_half + self._minutes(45 + second_stoppage)
        self.extra_time = self.regular_end + self._minutes(5)
        self.extra_end = self.extra_time + self._minutes(32 + et_stoppage)
        self.penalties = self.extra_end + self._minutes(3)

    def _minutes(self, value: float) -> timedelta:
        return timedelta(minutes=value * self.scale)

    def at(self, play: Play, seconds: int = 0) -> datetime:
        jitter = timedelta(seconds=seconds * self.scale)
        if play.kind == "match_start":
            return self.start
        if play.kind == "half_time":
            return self.half_time
        if play.kind == "second_half_start":
            return self.second_half
        if play.kind == "extra_time_start":
            return self.extra_time
        if play.kind == "penalties_start":
            return self.penalties
        if play.kind == "match_end":
            return {Period.SECOND_HALF: self.regular_end, Period.EXTRA_TIME: self.extra_end}.get(play.period, self.penalties + self._minutes(1.5 * (play.minute + 1)))
        if play.period == Period.FIRST_HALF:
            return self.start + self._minutes(play.minute + play.stoppage) + jitter
        if play.period == Period.SECOND_HALF:
            return self.second_half + self._minutes(play.minute - 45 + play.stoppage) + jitter
        if play.period == Period.EXTRA_TIME:
            pause = 2 if play.minute > 105 or (play.minute == 105 and play.stoppage) else 0
            return self.extra_time + self._minutes(play.minute - 90 + play.stoppage + pause) + jitter
        return self.penalties + self._minutes(1.5 * (play.minute + 1))


def random_plays(
    rng: random.Random,
    home_goals: int,
    away_goals: int,
    *,
    first_stoppage: int,
    second_stoppage: int,
    yellow_cards: tuple[int, int] = (2, 6),
    substitutions: tuple[int, int] = (2, 4),
    red_chance: float = 0.06,
) -> list[Play]:
    """Roteiro aleatório (e plausível) do tempo normal com o placar pedido."""
    plays = [
        Play(Period.FIRST_HALF, 0, "match_start"),
        Play(Period.FIRST_HALF, 45, "stoppage", extra={"minutes": first_stoppage}),
        Play(Period.FIRST_HALF, 45, "half_time", stoppage=first_stoppage),
        Play(Period.SECOND_HALF, 45, "second_half_start"),
        Play(Period.SECOND_HALF, 90, "stoppage", extra={"minutes": second_stoppage}),
        Play(Period.SECOND_HALF, 90, "match_end", stoppage=second_stoppage),
    ]

    def instant(lo: int = 1, hi: int = 90) -> tuple[str, int, int]:
        minute = rng.randint(lo, hi)
        if minute <= 45:
            stoppage = rng.randint(1, first_stoppage) if minute == 45 and rng.random() < 0.5 else 0
            return Period.FIRST_HALF, minute, stoppage
        stoppage = rng.randint(1, second_stoppage) if minute == 90 and rng.random() < 0.6 else 0
        return Period.SECOND_HALF, minute, stoppage

    for side, goals in (("home", home_goals), ("away", away_goals)):
        for _ in range(goals):
            period, minute, stoppage = instant()
            roll = rng.random()
            kind = "goal" if roll < 0.84 else "penalty_goal" if roll < 0.96 else "own_goal"
            plays.append(Play(period, minute, kind, side, stoppage))
    if rng.random() < 0.12:
        period, minute, stoppage = instant(10, 85)
        plays.append(Play(period, minute, "penalty_miss", rng.choice(("home", "away")), stoppage))
    for _ in range(rng.randint(*yellow_cards)):
        period, minute, stoppage = instant(8, 90)
        plays.append(Play(period, minute, "yellow", rng.choice(("home", "away")), stoppage))
    if rng.random() < red_chance:
        period, minute, stoppage = instant(30, 88)
        plays.append(Play(period, minute, "red", rng.choice(("home", "away")), stoppage))
    for side in ("home", "away"):
        count = rng.randint(*substitutions)
        windows = sorted(rng.sample(range(56, 88), k=min(3, count)))
        for n in range(count):
            plays.append(Play(Period.SECOND_HALF, windows[n % len(windows)], "sub", side))
    return sorted(plays, key=Play.sort_key)


class PlayRunner:
    """Lança um roteiro (`Play`s em ordem) pelos serviços, escolhendo os jogadores
    com o elenco em campo a cada momento."""

    def __init__(self, poster: Poster, rng: random.Random, clock: MatchClock | None = None):
        self.poster = poster
        self.rng = rng
        self.clock = clock
        self.goal_tags: dict[str, int] = {}
        self.last_at: datetime | None = None

    def squad(self, side: str) -> Squad:
        return self.poster.squads[side]

    def _at(self, play: Play) -> datetime | None:
        if self.clock is None:
            return None
        moment = self.clock.at(play, seconds=self.rng.randint(4, 50))
        if self.last_at is not None and moment <= self.last_at:
            moment = self.last_at + timedelta(seconds=1)
        self.last_at = moment
        return moment

    def run(self, plays: Iterable[Play], until: datetime | None = None) -> None:
        for play in plays:
            if until is not None and self.clock is not None and self.clock.at(play) > until:
                break
            self.play(play)

    def play(self, play: Play) -> services.PostResult | None:
        at = self._at(play)
        post = self.poster.post
        kind, side, minute, stoppage = play.kind, play.side, play.minute, play.stoppage
        if kind == "match_start":
            return post(EventType.MATCH_START, at=at)
        if kind == "half_time":
            return post(EventType.HALF_TIME, at=at, minute=45, stoppage=stoppage)
        if kind == "second_half_start":
            return post(EventType.SECOND_HALF_START, at=at)
        if kind == "extra_time_start":
            return post(EventType.EXTRA_TIME_START, at=at)
        if kind == "penalties_start":
            return post(EventType.PENALTIES_START, at=at)
        if kind == "match_end":
            end_minute = {Period.SECOND_HALF: 90, Period.EXTRA_TIME: 120}.get(play.period)
            return post(EventType.MATCH_END, at=at, minute=end_minute, stoppage=stoppage if end_minute else None)
        if kind == "stoppage":
            return post(EventType.STOPPAGE_TIME, at=at, minute=minute, payload={"minutes": play.extra["minutes"]})
        squad = self.squad(side)
        if kind in ("goal", "penalty_goal"):
            scorer = self._tagged(squad, play) or squad.scorer(self.rng)
            payload = {"origin": "penalty" if kind == "penalty_goal" else "open_play"}
            if kind == "penalty_goal":
                post(EventType.PENALTY_AWARDED, at=at, minute=minute, stoppage=stoppage, side=side)
                at = self._at(play)
            elif self.rng.random() < 0.7:
                helper = squad.assistant(self.rng, scorer)
                if helper is not None:
                    payload["assist"] = helper.name
            result = post(EventType.GOAL, at=at, minute=minute, stoppage=stoppage, side=side, player=scorer, payload=payload)
            if play.extra.get("tag"):
                self.goal_tags[play.extra["tag"]] = result.event.id
            return result
        if kind == "own_goal":
            other = self.squad("away" if side == "home" else "home")
            return post(EventType.GOAL, at=at, minute=minute, stoppage=stoppage, side=side, player=other.defender(self.rng), payload={"origin": "own_goal"})
        if kind == "penalty_miss":
            post(EventType.PENALTY_AWARDED, at=at, minute=minute, stoppage=stoppage, side=side)
            taker = squad.scorer(self.rng)
            outcome = self.rng.choice(tuple(domain.PENALTY_MISS_LABELS))
            return post(EventType.PENALTY_MISSED, at=self._at(play), minute=minute, stoppage=stoppage, side=side, player=taker, payload={"outcome": outcome})
        if kind == "var_annul":
            incident, decision, reason = play.extra.get("var") or self.rng.choice(VAR_INCIDENTS)
            target = self.goal_tags[play.extra["annul"]]
            post(EventType.VAR_REVIEW, at=at, minute=minute, stoppage=stoppage, side=side, payload={"incident": incident, "decision": decision})
            return post(EventType.GOAL_ANNULLED, at=self._at(play), minute=minute, stoppage=stoppage, annuls_event_id=target, payload={"reason": reason})
        if kind == "yellow":
            player = self._tagged(squad, play) or squad.carded(self.rng)
            if play.extra.get("tag"):
                squad.tags[play.extra["tag"]] = player
            result = post(EventType.YELLOW_CARD, at=at, minute=minute, stoppage=stoppage, side=side, player=player)
            squad.booked(player)
            return result
        if kind == "red":
            player = squad.carded(self.rng)
            result = post(EventType.RED_CARD, at=at, minute=minute, stoppage=stoppage, side=side, player=player)
            squad.sent(player)
            return result
        if kind == "sub":
            change = squad.substitution(self.rng)
            if change is None:
                return None
            leaving, entering = change
            payload = {"player_out": leaving.name, "player_in": entering.name}
            result = post(EventType.SUBSTITUTION, at=at, minute=minute, stoppage=stoppage, side=side, payload=payload)
            squad.substituted(leaving, entering)
            return result
        raise ValueError(f"passo de roteiro desconhecido: {kind}")

    @staticmethod
    def _tagged(squad: Squad, play: Play) -> SquadPlayer | None:
        tag = play.extra.get("tag")
        player = squad.tags.get(tag) if tag else None
        return player if player is not None and player in squad.on_field else None


def shootout_plays(
    rng: random.Random,
    first: str,
    *,
    conversion: float = 0.76,
    scored: dict[str, int] | None = None,
    taken: dict[str, int] | None = None,
) -> list[Play]:
    """Cobranças até haver vencedor (5 para cada; depois, alternadas). `scored`/`taken`
    continuam uma disputa já começada (`first` = quem bateu a primeira)."""
    second = "away" if first == "home" else "home"
    scored = {first: 0, second: 0, **(scored or {})}
    taken = {first: 0, second: 0, **(taken or {})}
    plays: list[Play] = []

    def decided() -> bool:
        if taken[first] <= 5 and taken[second] <= 5 and not (taken[first] == taken[second] == 5):
            left_first, left_second = 5 - taken[first], 5 - taken[second]
            return scored[first] + left_first < scored[second] or scored[second] + left_second < scored[first]
        return taken[first] == taken[second] and scored[first] != scored[second]

    side = first if taken[first] == taken[second] else second
    while not decided():
        goal = rng.random() < conversion
        plays.append(Play(Period.PENALTIES, len(plays), "kick", side, extra={"scored": goal}))
        taken[side] += 1
        scored[side] += int(goal)
        side = second if side == first else first
    return plays


# --- Simulação ao vivo -------------------------------------------------------------------------


class LiveSimulation:
    """Leva uma partida do estado atual até o fim, minuto a minuto, em tempo escalado."""

    GOAL_RATE = {"home": 1.45 / 90, "away": 1.15 / 90}
    YELLOW_RATE = 2.1 / 90
    RED_RATE = 0.08 / 90
    PENALTY_RATE = 0.12 / 90
    VAR_ANNUL_CHANCE = 0.08

    def __init__(self, match_id: int, user, *, speed: float = 30.0, seed: int | None = None, until_minute: int | None = None, out=None):
        self.match_id = match_id
        self.user = user
        self.speed = speed
        self.rng = random.Random(seed)
        self.until_minute = until_minute
        self.out = out
        self.minute_pause = 60.0 / speed
        match = self.load_match()
        squads = {
            "home": Squad.for_match(match, match.home_team, self.rng),
            "away": Squad.for_match(match, match.away_team, self.rng),
        }
        self.poster = Poster(match, user, squads, key_prefix=f"sim-{uuid.uuid4().hex[:12]}", out=out, confirm=True)
        self.runner = PlayRunner(self.poster, self.rng)
        self.stopped = False
        self.stoppage_used: dict[str, int] = {}

    def headline(self) -> str:
        match = self.poster.match
        situation = match.get_status_display().lower()
        if match.period:
            situation += f", {match.get_period_display().lower()}"
        return (
            f"Simulando {match.home_team.name} × {match.away_team.name} (partida {match.pk}, {situation}) "
            f"a {self.speed:g} minuto(s) de jogo por minuto real."
        )

    # --- estado ---

    def load_match(self) -> Match:
        return Match.objects.select_related(*context.MATCH_RELATED).get(pk=self.match_id)

    def state(self) -> tuple[Match, domain.MatchState, domain.MatchContext, list]:
        match = self.load_match()
        rows = context.match_events(match)
        ctx = context.build_context(match)
        events = context.to_domain_events(rows)
        return match, domain.derive_state(events, ctx), ctx, rows

    def current_minute(self, match: Match, state: domain.MatchState, rows) -> int:
        """Minuto em que a simulação continua: o do último lance do período ou, com o
        relógio correndo, o minuto que as páginas mostram (limitado ao fim do período)."""
        clock = domain.PERIOD_CLOCK.get(state.period)
        if clock is None:
            return 0
        minutes = [row.minute for row in rows if row.voided_at is None and row.period == state.period and row.minute is not None]
        minute = max([clock["offset"], *minutes])
        if match.period_started_at is not None:
            elapsed = int((timeutils.now() - match.period_started_at).total_seconds() // 60)
            minute = max(minute, min(clock["offset"] + elapsed, clock["regular_end"]))
        return minute

    # --- laço ---

    def ensure_playable(self) -> domain.MatchState:
        """Estado atual da partida; partida encerrada, cancelada ou adiada → CommandError."""
        _match, state, _ctx, _rows = self.state()
        if state.status == Status.FINISHED:
            raise CommandError("A partida já terminou.")
        if state.status == Status.CANCELLED:
            raise CommandError("A partida foi cancelada.")
        if state.status == Status.POSTPONED:
            raise CommandError("A partida está adiada: reagende-a antes de simular.")
        return state

    def run(self) -> Match:
        state = self.ensure_playable()
        for squad in self.poster.squads.values():
            squad.sync(state)
        if state.status == Status.SUSPENDED:
            self.poster.status("resume")
        if state.status == Status.SCHEDULED:
            self.poster.post(EventType.MATCH_START)
        while not self.stopped:
            match, state, ctx, rows = self.state()
            if state.status != Status.LIVE:
                break
            period = state.period
            if period == Period.FIRST_HALF:
                self.play_period(Period.FIRST_HALF, self.current_minute(match, state, rows), 45)
                if not self.stopped:
                    self.poster.post(EventType.HALF_TIME, minute=45, stoppage=self.stoppage_used.get(Period.FIRST_HALF))
            elif period == Period.HALF_TIME:
                if self._reached(45):
                    break
                self.wait(15)
                if self.rng.random() < 0.5:
                    self.half_time_substitution()
                self.poster.post(EventType.SECOND_HALF_START)
            elif period == Period.SECOND_HALF:
                self.play_period(Period.SECOND_HALF, self.current_minute(match, state, rows), 90)
                if not self.stopped:
                    self.end_of(Period.SECOND_HALF, 90)
            elif period == Period.EXTRA_TIME:
                self.play_period(Period.EXTRA_TIME, self.current_minute(match, state, rows), 120)
                if not self.stopped:
                    self.end_of(Period.EXTRA_TIME, 120)
            elif period == Period.PENALTIES:
                if self._reached(120):
                    break
                self.shootout(rows)
                self.poster.post(EventType.MATCH_END)
        return self.load_match()

    def _reached(self, minute: int) -> bool:
        if self.until_minute is not None and minute >= self.until_minute:
            self.stopped = True
        return self.stopped

    def wait(self, game_minutes: float) -> None:
        sleep(game_minutes * self.minute_pause)

    def play_period(self, period: str, start: int, end: int) -> None:
        """Minutos `start+1 .. end` e os acréscimos anunciados. Com `--until-minute`,
        para ao chegar nele (antes dos acréscimos, se ele for o fim do período)."""
        minute = start
        while minute < end:
            if self._reached(minute):
                return
            minute += 1
            self.wait(1)
            self.minute_events(period, minute, 0)
        if self._reached(end):
            return
        extra = self.rng.randint(1, 4) if end == 45 else self.rng.randint(2, 7) if end == 90 else self.rng.randint(1, 3)
        self.poster.post(EventType.STOPPAGE_TIME, minute=end, payload={"minutes": extra})
        for added in range(1, extra + 1):
            self.wait(1)
            self.minute_events(period, end, added)
        self.stoppage_used[period] = extra

    def minute_events(self, period: str, minute: int, stoppage: int) -> None:
        rng = self.rng
        for side in ("home", "away"):
            squad = self.poster.squads[side]
            if not squad.on_field:
                continue
            if rng.random() < self.GOAL_RATE[side]:
                self.goal(period, minute, stoppage, side)
            if rng.random() < self.PENALTY_RATE:
                kind = "penalty_goal" if rng.random() < 0.75 else "penalty_miss"
                self.runner.play(Play(period, minute, kind, side, stoppage))
            if rng.random() < self.YELLOW_RATE:
                fresh = rng.random() > 0.08  # de vez em quando, o 2º amarelo
                player = squad.carded(rng, fresh=fresh)
                if player is not None:
                    self.poster.post(EventType.YELLOW_CARD, minute=minute, stoppage=stoppage, side=side, player=player)
                    squad.booked(player)
            if rng.random() < self.RED_RATE:
                self.runner.play(Play(period, minute, "red", side, stoppage))
            if period != Period.FIRST_HALF and not stoppage and minute >= 56 and rng.random() < 0.06:
                self.runner.play(Play(period, minute, "sub", side, stoppage))

    def goal(self, period: str, minute: int, stoppage: int, side: str) -> None:
        tag = f"g{self.poster.n}"
        kind = "own_goal" if self.rng.random() < 0.04 else "goal"
        if kind == "own_goal":
            self.runner.play(Play(period, minute, kind, side, stoppage))
            return
        self.runner.play(Play(period, minute, "goal", side, stoppage, extra={"tag": tag}))
        if self.rng.random() < self.VAR_ANNUL_CHANCE:
            self.wait(1)
            self.runner.play(Play(period, minute, "var_annul", side, stoppage, extra={"annul": tag}))

    def half_time_substitution(self) -> None:
        side = self.rng.choice(("home", "away"))
        self.runner.play(Play(Period.HALF_TIME, 45, "sub", side))

    def end_of(self, period: str, regular_end: int) -> None:
        """Fim do 2º tempo ou da prorrogação: fim de jogo, prorrogação ou pênaltis."""
        match, state, ctx, _rows = self.state()
        available = domain.available_actions(state, ctx)["events"]
        stoppage = self.stoppage_used.get(period)
        if EventType.MATCH_END in available:
            self.poster.post(EventType.MATCH_END, minute=regular_end, stoppage=stoppage)
        elif EventType.EXTRA_TIME_START in available:
            if self._reached(regular_end):
                return
            self.wait(5)
            self.poster.post(EventType.EXTRA_TIME_START)
        elif EventType.PENALTIES_START in available:
            if self._reached(regular_end):
                return
            self.wait(3)
            self.poster.post(EventType.PENALTIES_START)
        else:  # pragma: no cover - o domínio sempre oferece uma saída
            raise CommandError(f"Nenhuma ação de fim disponível: {available}")

    def shootout(self, rows) -> None:
        """Disputa de pênaltis (continua a que já começou, se for o caso)."""
        kicks_done = [row for row in rows if row.voided_at is None and row.type == EventType.SHOOTOUT_KICK]
        sides = [self.poster.side_of(row.team_id) for row in kicks_done]
        first = sides[0] if sides else self.rng.choice(("home", "away"))
        taken = {side: sides.count(side) for side in ("home", "away")}
        scored = {
            side: sum(1 for row, kicker in zip(kicks_done, sides, strict=True) if kicker == side and row.payload.get("scored") is True)
            for side in ("home", "away")
        }
        kicks = shootout_plays(self.rng, first, scored=scored, taken=taken)
        takers = {side: self.poster.squads[side].shootout_takers() for side in ("home", "away")}
        for kick in kicks:
            self.wait(1.5)
            people = takers[kick.side]
            taker = people[taken[kick.side] % len(people)] if people else None
            taken[kick.side] += 1
            self.poster.post(EventType.SHOOTOUT_KICK, side=kick.side, player=taker, payload={"scored": kick.extra["scored"]})


# --- Comando --------------------------------------------------------------------------------


class Command(BaseCommand):
    help = (
        "Simula uma partida ao vivo pelos serviços (origem script): leva do estado atual até o fim "
        "(ou até --until-minute), em tempo escalado (--speed = minutos de jogo por minuto real)."
    )

    def add_arguments(self, parser):
        parser.add_argument("match_id", type=int, help="Id da partida.")
        parser.add_argument("--speed", type=float, default=30.0, help="Minutos de jogo por minuto real (padrão 30; 1 = tempo real).")
        parser.add_argument("--seed", type=int, default=None, help="Semente do sorteio dos lances (repetível).")
        parser.add_argument("--until-minute", type=int, default=None, help="Para ao chegar neste minuto de jogo (a partida fica ao vivo).")
        parser.add_argument("--user", default="operador", help="Usuário que lança (padrão: o operador do seed).")

    def handle(self, *args, match_id, speed, seed, until_minute, user, **options):
        if speed <= 0:
            raise CommandError("--speed precisa ser maior que zero.")
        if until_minute is not None and not 1 <= until_minute <= 120:
            raise CommandError("--until-minute vai de 1 a 120.")
        operator = get_user_model().objects.filter(username=user).first()
        if operator is None:
            raise CommandError(f"Usuário “{user}” não existe (rode `python manage.py seed` ou use --user).")
        if not Match.objects.filter(pk=match_id).exists():
            raise CommandError(f"Partida {match_id} não existe.")
        simulation = LiveSimulation(match_id, operator, speed=speed, seed=seed, until_minute=until_minute, out=self.stdout)
        simulation.ensure_playable()
        self.stdout.write(simulation.headline())
        try:
            with quiet_audit_log():
                final = simulation.run()
        except domain.DomainError as exc:
            # Ex.: o operador mudou a partida durante a simulação (encerrou, suspendeu...).
            raise CommandError(f"Lance recusado ({exc.code}): {exc.message}") from exc
        except KeyboardInterrupt:
            # Cada lance é uma transação: o que já foi lançado fica gravado e publicado.
            self.stdout.write(self.style.WARNING("Simulação interrompida: os lances já lançados ficam gravados."))
            final = simulation.load_match()
        status = final.get_status_display().lower()
        self.stdout.write(
            self.style.SUCCESS(
                f"Fim da simulação: {final.home_team.name} {final.home_score} × {final.away_score} {final.away_team.name} ({status})."
            )
        )
