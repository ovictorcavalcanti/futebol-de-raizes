"""Rotas de leitura (sem login): home, menu, competição, classificação e partidas.

Camada fina sobre `matches.selectors` e `standings.services`: os dicionários
saem como estão (docs/CONTRACT.md §3–4). Dado ao vivo responde com
`Cache-Control: no-store`; o menu de competições aceita um cache curto.
Filtro com formato inválido → 400 `invalid_input` (`details.field`).

Micro-cache (`settings.READ_CACHE_SECONDS`, padrão 5 s; 0 desliga) nas duas leituras
mais quentes, home e competição, no cache `reads`: chave = (rota, parâmetros, cursor do
outbox). O cursor é lido antes do estado, e toda escrita publica mensagem, que muda o
cursor: o que sai do cache vale o mesmo que uma leitura nova feita naquele cursor (o
stream entrega o resto). `server_time`/`timezone` são sempre os da requisição. Só a
edição no admin que não publica mensagem (nome de time ou competição, escudo) espera
o vencimento.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Callable

from django.conf import settings
from django.core.cache import caches
from ninja import Query, Router
from ninja.responses import codes_4xx

from competitions.models import Stage
from core import timeutils
from matches import selectors
from realtime.outbox import current_cursor
from standings.models import Ranking
from standings.services import ranking_standings, stage_standings

from .errors import ApiError, invalid_input
from .responses import MENU_CACHE, respond
from .schemas import (
    CompetitionOut,
    CompetitionsOut,
    ErrorOut,
    HomeOut,
    MatchDetailOut,
    MatchesOut,
    StandingsOut,
    RankingStandingsOut,
)

router = Router(tags=["Leitura"])

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def parse_day(value: str | None, field: str = "date") -> dt.date | None:
    """"AAAA-MM-DD" (dia de Brasília) → date; vazio → None; outro formato → 400."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if not _DAY.fullmatch(text):
            raise ValueError(text)
        return dt.date.fromisoformat(text)
    except ValueError:
        raise invalid_input(field, "Data inválida: use o formato AAAA-MM-DD.") from None


def parse_flag(value: str | None, field: str) -> bool:
    """`live=1` → True; ausente, vazio ou `0` → False; outro valor → 400."""
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise invalid_input(field, f"Valor inválido para {field}: use 1 ou 0.")


def stamp() -> dict:
    return {"server_time": timeutils.iso_utc(timeutils.now()), "timezone": selectors.timezone_name()}


READ_CACHE = "reads"


def _cache_key(kind: str, parts: tuple, cursor: int) -> str:
    digest = hashlib.sha1(repr(parts).encode(), usedforsecurity=False).hexdigest()
    return f"fdr:read:{kind}:{digest}:{cursor}"


def cached_read(kind: str, parts: tuple, build: Callable[[], dict]) -> dict:
    """Payload de `build()` (que traz `cursor`, lido antes do estado), guardado por
    `READ_CACHE_SECONDS` sob (kind, parts, cursor). Acerto = 1 consulta (o cursor).
    O dicionário guardado nunca é mexido: devolve uma cópia rasa com `server_time` novo.
    Erros (ex.: 404) sobem de `build()` e não entram no cache."""
    ttl = float(getattr(settings, "READ_CACHE_SECONDS", 0) or 0)
    if ttl <= 0:
        return build()
    store = caches[READ_CACHE]
    payload = store.get(_cache_key(kind, parts, current_cursor()))
    if payload is None:
        payload = build()
        # Sob o cursor do próprio payload (≥ o da chave e lido antes do estado): o
        # próximo pedido no mesmo cursor já acerta.
        store.set(_cache_key(kind, parts, payload["cursor"]), payload, timeout=ttl)
    return {**payload, **stamp()}


@router.get("/home", response={200: HomeOut, codes_4xx: ErrorOut}, summary="Jogos do dia por competição e últimos gols")
def home(request, date: str | None = Query(None, description="Dia de Brasília (AAAA-MM-DD); sem ele, hoje")):
    # O dia é calculado a cada pedido: a virada da meia-noite muda a chave do cache.
    day = parse_day(date) or timeutils.local_today()
    return respond(cached_read("home", (day.isoformat(),), lambda: selectors.home_payload(day=day)))


@router.get("/competitions", response=CompetitionsOut, summary="Competições em ordem, para o menu")
def competitions(request):
    return respond(selectors.competitions_menu(), cache=MENU_CACHE)


@router.get(
    "/competitions/{slug}",
    response={200: CompetitionOut, codes_4xx: ErrorOut},
    summary="Página da competição: fases, fase e rodada atuais, classificação e jogos",
)
def competition(
    request,
    slug: str,
    stage_id: int | None = Query(None, alias="stage", description="Fase exibida (padrão: a atual)"),
    round_id: int | None = Query(None, alias="round", description="Rodada exibida (padrão: a atual)"),
):
    return respond(
        cached_read(
            "competition",
            # sem rodada pedida, a atual depende do dia (calendário): o dia entra na chave
            (slug, stage_id, round_id, None if round_id else timeutils.local_today().isoformat()),
            lambda: selectors.competition_payload(slug, stage_id=stage_id, round_id=round_id),
        )
    )


@router.get(
    "/stages/{stage_id}/standings",
    response={200: StandingsOut, codes_4xx: ErrorOut},
    summary="Classificação de todos os grupos da fase, com critérios e legenda",
)
def standings(request, stage_id: int, live: str | None = Query(None, description="1 = tabela ao vivo; sem ele, a oficial")):
    is_live = parse_flag(live, "live")
    stage = Stage.objects.get(pk=stage_id)
    if not stage.has_table:
        raise ApiError(404, "not_found", "Fase de mata-mata não tem classificação.", {"stage_id": stage_id})
    return respond({**stamp(), **stage_standings(stage, live=is_live)})


@router.get(
    "/rankings/{ranking_id}",
    response={200: RankingStandingsOut, codes_4xx: ErrorOut},
    summary="Classificação geral do torneio ou personalizada (mesmo formato da classificação da fase)",
)
def ranking(request, ranking_id: int, live: str | None = Query(None, description="1 = ao vivo; sem ele, a oficial")):
    is_live = parse_flag(live, "live")
    item = Ranking.objects.prefetch_related("criteria", "zones").get(pk=ranking_id)
    return respond({**stamp(), **ranking_standings(item, live=is_live)})


@router.get("/matches", response={200: MatchesOut, codes_4xx: ErrorOut}, summary="Lista de partidas")
def matches(
    request,
    round_id: int | None = Query(None, alias="roundId"),
    date: str | None = Query(None, description="Dia de Brasília pelo kickoff_at (AAAA-MM-DD)"),
    status: str | None = Query(None, description="Um ou vários status separados por vírgula"),
    stage_id: int | None = Query(None, alias="stageId"),
):
    """Sem filtro, no máximo 500 partidas."""
    day = parse_day(date)
    try:
        payload = selectors.matches_list(round_id=round_id, date=day, status=status, stage_id=stage_id)
    except ValueError as exc:  # status desconhecido
        raise invalid_input("status", str(exc)) from exc
    return respond(payload)


@router.get("/matches/{match_id}", response={200: MatchDetailOut, codes_4xx: ErrorOut}, summary="Partida com todos os lances")
def match(request, match_id: int):
    return respond(selectors.match_detail(match_id))
