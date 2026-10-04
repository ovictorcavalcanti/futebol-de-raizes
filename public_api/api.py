"""API pública (fase 12), montada em `/public/v1`.

Terceira porta sobre o mesmo núcleo: só leitura, com serializadores novos
(`public_api.schemas`, a lista de permissão), chave (`X-API-Key`), limite de uso
por chave, cache HTTP (ETag/304) e documentação OpenAPI em `/public/v1/docs`
(o esquema em `/public/v1/openapi.json`; os dois abrem sem chave).

Linha do tempo pública: só eventos visíveis (nunca os cancelados) cujo tipo tem
`CATALOG[tipo].public` — fora os de status e o gol anulado — e nunca o gol que foi
anulado. Cada lance sai com campos tipados no lugar do payload bruto.
"""

from __future__ import annotations

import datetime as dt
import re
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from ninja import NinjaAPI, Query
from ninja.errors import AuthenticationError, HttpError, Throttled, ValidationError

from competitions.models import Competition, Group, Round, Stage
from core import timeutils
from core.timeutils import iso_utc
from matches import domain, selectors
from matches.domain import EventType
from matches.models import Match, MatchEvent
from observability.metrics import metrics
from standings.services import stage_standings

from . import cache, throttle
from .auth import HEADER, ApiKeyAuth
from .schemas import (
    CompetitionDetailOut,
    CompetitionsOut,
    ErrorOut,
    MatchEnvelopeOut,
    MatchesOut,
    StandingsOut,
)

MATCHES_LIMIT = selectors.MATCHES_LIST_LIMIT  # no máximo 500 partidas por lista
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

DESCRIPTION = f"""
API pública do **Futebol de Raízes**: competições, partidas com os lances e classificação.

* **Chave**: mande o header `{HEADER}` em toda requisição. Sem chave, chave inválida ou
  desativada → `401 invalid_api_key`.
* **Limite de uso**: requisições por chave por minuto (janela fixa). Toda resposta traz
  `X-RateLimit-Limit`, `X-RateLimit-Remaining` e `X-RateLimit-Reset` (instante Unix, em
  segundos, em que a janela reabre). Passou do limite → `429 rate_limited` com `Retry-After`.
* **Cache HTTP**: respostas 200 trazem `ETag` e `Cache-Control: public, max-age=N`. Repita o
  pedido com `If-None-Match: <ETag>` → `304 Not Modified` sem corpo quando nada mudou
  (conta no limite de uso).
* **Horários** em UTC (ISO 8601 com `Z`); o "dia" (`date`) segue o horário de Brasília
  (`timezone`).
* **Lances**: só os lances públicos — sem eventos de status, sem gols anulados e sem
  lançamentos cancelados.
* **Erros**: `{{"code", "message", "details"}}` — 400 `invalid_input`, 401 `invalid_api_key`,
  404 `not_found` (também caminho inexistente sob `/public/v1/`), 405 `method_not_allowed`,
  429 `rate_limited`.
"""

public_api = NinjaAPI(
    title="Futebol de Raízes · API pública",
    version="1.0.0",
    description=DESCRIPTION,
    urls_namespace="public_v1",
    docs_url="/docs",
    openapi_url="/openapi.json",
    auth=ApiKeyAuth(),
    throttle=throttle.ApiKeyThrottle(),
)

ERRORS = {400: ErrorOut, 401: ErrorOut, 404: ErrorOut, 429: ErrorOut}


def responses(schema) -> dict:
    return {200: schema, 304: None, **ERRORS}


# --- Erros -----------------------------------------------------------------------------


class PublicError(Exception):
    """Erro das rotas públicas → `{"code", "message", "details"}`."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def invalid_input(field: str, message: str) -> PublicError:
    return PublicError(400, "invalid_input", message, {"field": field})


def error_response(request, status: int, code: str, message: str, details: dict | None = None):
    body = {"code": code, "message": message, "details": details or {}}
    return public_api.create_response(request, body, status=status)


_NOT_FOUND = (
    (Match.DoesNotExist, "Partida não encontrada."),
    (Competition.DoesNotExist, "Competição não encontrada."),
    (Stage.DoesNotExist, "Fase não encontrada."),
)
_HTTP_CODES = {400: "invalid_input", 401: "invalid_api_key", 404: "not_found", 405: "method_not_allowed", 429: "rate_limited"}


def _plain_error(status: int, code: str, message: str, details: dict) -> JsonResponse:
    """Erro no formato da API pública fora do Ninja (sem chave nem limite de uso)."""
    response = JsonResponse({"code": code, "message": message, "details": details}, status=status)
    response["Cache-Control"] = cache.NO_STORE
    metrics.inc("fdr_public_api_requests_total", endpoint=code, status=status)
    return response


@csrf_exempt
def not_found_view(request, rest: str = ""):
    """Caminho inexistente sob `/public/v1/`: 404 no formato de erro da API pública
    (não a página HTML do Django). Não exige chave nem conta no limite de uso."""
    return _plain_error(
        404, "not_found", "Rota da API pública não encontrada.", {"path": request.path, "docs": "/public/v1/docs"}
    )


def json_errors(view):
    """Envolve a view de um caminho do Ninja: 404 (ex.: a raiz `/public/v1/`) e 405
    (método não aceito) que o Ninja/Django respondem em HTML ou texto puro saem no
    formato de erro da API pública."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except Http404:
            return not_found_view(request)
        if "application/json" in response.get("Content-Type", ""):
            return response
        if response.status_code == 405:
            allowed = response.get("Allow", "")
            methods = [method.strip() for method in allowed.split(",") if method.strip()]
            json_response = _plain_error(405, "method_not_allowed", "Método não permitido.", {"allowed": methods})
            json_response["Allow"] = allowed
            return json_response
        if response.status_code == 404:
            return not_found_view(request)
        return response

    return wrapper


@public_api.exception_handler(PublicError)
def _on_public_error(request, exc: PublicError):
    return error_response(request, exc.status, exc.code, exc.message, exc.details)


@public_api.exception_handler(AuthenticationError)
def _on_authentication_error(request, exc):
    return error_response(
        request, 401, "invalid_api_key", f"Chave da API ausente, inválida ou desativada: envie o header {HEADER}."
    )


@public_api.exception_handler(Throttled)
def _on_throttled(request, exc: Throttled):
    state = getattr(request, "rate_limit", None)
    retry_after = state.retry_after if state else throttle.WINDOW_SECONDS
    details = {"limit": state.limit, "retry_after": retry_after} if state else {"retry_after": retry_after}
    response = error_response(
        request, 429, "rate_limited", f"Limite de uso da chave excedido: tente de novo em {retry_after} s.", details
    )
    response["Retry-After"] = str(retry_after)
    return response


@public_api.exception_handler(ValidationError)
def _on_validation_error(request, exc: ValidationError):
    # loc = ("query", "round_id"): sai o prefixo de origem.
    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "query",
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors
    ]
    details = {"field": errors[0]["field"], "errors": errors} if errors else {"errors": []}
    return error_response(request, 400, "invalid_input", "Parâmetro inválido: confira a requisição.", details)


@public_api.exception_handler(ObjectDoesNotExist)
def _on_not_found(request, exc: ObjectDoesNotExist):
    message = next((text for kind, text in _NOT_FOUND if isinstance(exc, kind)), "Não encontrado.")
    return error_response(request, 404, "not_found", message)


@public_api.exception_handler(Http404)
def _on_http404(request, exc):
    return error_response(request, 404, "not_found", "Não encontrado.")


@public_api.exception_handler(HttpError)
def _on_http_error(request, exc: HttpError):
    status = exc.status_code
    return error_response(request, status, _HTTP_CODES.get(status, "http_error"), str(exc))


# --- Resposta: cache HTTP, limite de uso e métricas -------------------------------------


def public_response(run):
    """Envolve a execução de cada rota (autenticação, limite de uso, rota e
    serialização acontecem dentro de `run`) e acerta a resposta final: ETag/304 e
    Cache-Control, headers X-RateLimit-* e a métrica de requisições."""

    @wraps(run)
    def wrapper(request, *args, **kwargs):
        response = cache.finalize(request, run(request, *args, **kwargs))
        throttle.apply_headers(response, getattr(request, "rate_limit", None))
        route = getattr(request, "resolver_match", None)
        endpoint = (route.url_name if route is not None else None) or ""
        metrics.inc("fdr_public_api_requests_total", endpoint=endpoint, status=response.status_code)
        return response

    return wrapper


public_api.add_decorator(public_response, mode="view")


# --- Lances públicos -----------------------------------------------------------------


# Campos tipados que cada tipo de lance expõe a partir do payload (o resto do payload
# — ids de jogador, motivos, `derived_from_sequence` — nunca sai).
_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    EventType.GOAL: ("player", "origin", "assist"),
    EventType.PENALTY_MISSED: ("player", "outcome"),
    EventType.VAR_REVIEW: ("incident", "decision"),
    EventType.SUBSTITUTION: ("player_out", "player_in"),
    EventType.YELLOW_CARD: ("player",),
    EventType.RED_CARD: ("player",),
    EventType.STOPPAGE_TIME: ("minutes",),
    EventType.SHOOTOUT_KICK: ("player", "scored"),
}
_RENAMED = {"minutes": "stoppage_minutes"}
_TYPED_FIELDS = (
    "player", "origin", "origin_label", "assist", "player_out", "player_in", "second_yellow",
    "outcome", "outcome_label", "scored", "stoppage_minutes", "incident", "decision",
)


def is_public(row: MatchEvent, timeline: selectors.Timeline) -> bool:
    """Lance visível com exibição pública: nunca status, gol anulado, cancelado nem gol que foi anulado."""
    spec = domain.CATALOG.get(row.type)
    if spec is None or not spec.public or row.voided_at is not None:
        return False
    return not (row.type == EventType.GOAL and row.id in timeline.annulled)


def serialize_public_event(row: MatchEvent, match: Match, timeline: selectors.Timeline) -> dict:
    """EventOut público: campos tipados por tipo de lance, sem o payload bruto."""
    spec = domain.CATALOG[row.type]
    payload = row.payload if isinstance(row.payload, dict) else {}
    typed: dict = dict.fromkeys(_TYPED_FIELDS)
    for key in _PAYLOAD_FIELDS.get(row.type, ()):
        typed[_RENAMED.get(key, key)] = payload.get(key)
    if row.type == EventType.GOAL:
        typed["origin"] = typed["origin"] or domain.GoalOrigin.OPEN_PLAY.value
        typed["origin_label"] = domain.GOAL_ORIGIN_LABELS.get(typed["origin"])
    elif row.type == EventType.PENALTY_MISSED and typed["outcome"]:
        typed["outcome_label"] = domain.PENALTY_MISS_LABELS.get(typed["outcome"])
    elif row.type == EventType.RED_CARD:
        typed["second_yellow"] = payload.get("reason") == domain.SECOND_YELLOW
    period = row.period
    team_side = "home" if row.team_id == match.home_team_id else "away" if row.team_id == match.away_team_id else None
    return {
        "id": row.id,
        "type": row.type,
        "type_label": spec.label,
        "kind": spec.kind,
        "period": period,
        "period_label": domain.PERIOD_LABELS.get(period) if period else None,
        "minute": row.minute,
        "stoppage": row.stoppage,
        "minute_label": domain.format_minute(row.minute, row.stoppage),
        "team_id": row.team_id,
        "team_side": team_side,
        **typed,
        "score_after": timeline.score_after.get(row.id),
        "created_at": iso_utc(row.created_at),
    }


def public_events(match: Match, rows) -> list[dict]:
    """Linha do tempo pública da partida, em ordem (`rows` = eventos da partida)."""
    timeline = selectors.Timeline(match, rows)
    return [serialize_public_event(row, match, timeline) for row in timeline.rows if is_public(row, timeline)]


# --- Filtros ---------------------------------------------------------------------------


def parse_day(value: str | None) -> dt.date | None:
    """"AAAA-MM-DD" (dia de Brasília) → date; vazio → None; outro formato → 400."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        if not _DAY.fullmatch(text):
            raise ValueError(text)
        return dt.date.fromisoformat(text)
    except ValueError:
        raise invalid_input("date", "Data inválida: use o formato AAAA-MM-DD.") from None


def parse_statuses(value: str | None) -> list[str]:
    """Um ou vários status separados por vírgula; desconhecido → 400."""
    wanted = [item.strip() for item in (value or "").split(",") if item.strip()]
    unknown = [item for item in wanted if item not in Match.Status.values]
    if unknown:
        raise invalid_input("status", f"Status desconhecido: {', '.join(unknown)}. Use: {', '.join(Match.Status.values)}.")
    return wanted


def _first_open(items, is_open):
    """Primeiro item com jogo ainda não encerrado; senão o último (None se vazio)."""
    return next((item for item in items if is_open(item)), items[-1] if items else None)


# --- Rotas -----------------------------------------------------------------------------


@public_api.get("/competitions", response=responses(CompetitionsOut), summary="Competições, em ordem", tags=["Competições"])
def competitions(request):
    return selectors.competitions_menu()


@public_api.get(
    "/competitions/{slug}",
    response=responses(CompetitionDetailOut),
    summary="Competição: temporada atual, fases, grupos e rodadas",
    tags=["Competições"],
)
def competition_detail(request, slug: str):
    competition = Competition.objects.get(slug=slug)
    season = competition.seasons.order_by("-year").first()
    stages: list[Stage] = []
    if season is not None:
        stages = list(
            Stage.objects.filter(season=season)
            .order_by("position", "id")
            .prefetch_related(
                Prefetch("rounds", queryset=Round.objects.order_by("number")),
                Prefetch("groups", queryset=Group.objects.order_by("name", "id")),
            )
        )
    open_pairs = set(
        Match.objects.filter(stage__in=stages)
        .exclude(status__in=selectors.CLOSED_STATUSES)
        .order_by()
        .values_list("stage_id", selectors.ROUND_EXPRESSION)
        .distinct()
    ) if stages else set()
    open_stages = {stage_id for stage_id, _ in open_pairs}

    stage_items = []
    for stage in stages:
        rounds = list(stage.rounds.all())
        current = _first_open(rounds, lambda rnd, stage=stage: (stage.id, rnd.id) in open_pairs)
        stage_items.append({
            "id": stage.id,
            "name": stage.name,
            "format": stage.format,
            "position": stage.position,
            "has_standings": stage.has_table,
            "groups": [{"id": group.id, "name": group.name} for group in stage.groups.all()] if stage.has_table else [],
            "rounds": [{"id": rnd.id, "number": rnd.number, "name": rnd.label} for rnd in rounds],
            "current_round_id": current.id if current else None,
        })
    current_stage = _first_open(stage_items, lambda item: item["id"] in open_stages)
    return {
        "competition": {
            "id": competition.id,
            "name": competition.name,
            "slug": competition.slug,
            "short_name": competition.short_name,
            "position": competition.position,
        },
        "season": {"id": season.id, "year": season.year} if season else None,
        "stages": stage_items,
        "current_stage_id": current_stage["id"] if current_stage else None,
        "current_round_id": current_stage["current_round_id"] if current_stage else None,
        "timezone": selectors.timezone_name(),
    }


@public_api.get("/matches", response=responses(MatchesOut), summary="Partidas (resumo, sem os lances)", tags=["Partidas"])
def matches(
    request,
    date: str | None = Query(None, description="Dia de Brasília pelo início do jogo (AAAA-MM-DD)"),
    competition: str | None = Query(None, description="Slug da competição"),
    status: str | None = Query(None, description="Um ou vários status separados por vírgula (ex.: live,suspended)"),
    round_id: int | None = Query(None, description="Id da rodada (GET /competitions/{slug})"),
):
    """Em ordem de início; no máximo 500 partidas por resposta (use os filtros)."""
    query = Match.objects.all()
    day = parse_day(date)
    if day is not None:
        start, end = timeutils.day_bounds(day)
        query = query.filter(kickoff_at__gte=start, kickoff_at__lt=end)
    if competition:
        competition_id = Competition.objects.filter(slug=competition).values_list("id", flat=True).first()
        if competition_id is None:
            raise PublicError(404, "not_found", "Competição não encontrada.", {"field": "competition"})
        query = query.filter(stage__season__competition_id=competition_id)
    statuses = parse_statuses(status)
    if statuses:
        query = query.filter(status__in=statuses)
    if round_id is not None:
        query = query.filter(selectors.round_filter(round_id))
    rows = list(query.select_related(*selectors.MATCH_RELATED).order_by("kickoff_at", "id")[:MATCHES_LIMIT])
    return {"timezone": selectors.timezone_name(), "matches": selectors.serialize_matches(rows)}


@public_api.get(
    "/matches/{match_id}",
    response=responses(MatchEnvelopeOut),
    summary="Partida com os lances públicos, escalações, arbitragem e estatísticas",
    tags=["Partidas"],
)
def match_detail(request, match_id: int):
    match = Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=match_id)
    rows = list(MatchEvent.objects.filter(match_id=match.id, voided_at__isnull=True).order_by("sequence"))
    data = selectors.serialize_match(match, detail=True, events=rows)
    data["events"] = public_events(match, rows)
    return {"timezone": selectors.timezone_name(), "match": data}


@public_api.get(
    "/stages/{stage_id}/standings",
    response=responses(StandingsOut),
    by_alias=True,
    summary="Classificação da fase (oficial; ao vivo com live=true)",
    tags=["Classificação"],
)
def standings(request, stage_id: int, live: bool = Query(False, description="true = tabela ao vivo (com os jogos em andamento)")):
    stage = Stage.objects.get(pk=stage_id)
    if not stage.has_table:
        raise PublicError(404, "not_found", "Fase de mata-mata não tem classificação.", {"stage_id": stage_id})
    return stage_standings(stage, live=live)
