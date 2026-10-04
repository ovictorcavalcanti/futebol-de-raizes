"""Rotas de operação (`/api/ops`): lançar evento, cancelar lançamento, mudar status
e o catálogo da tela do operador.

Camada fina sobre `matches.services` (escrita) e `matches.selectors` (corpo das
respostas). Exigem sessão, token CSRF e a permissão da rota (`OperatorAuth`):
sem login 401, sem permissão 403. Lançamento e status exigem `Idempotency-Key`:
a mesma chave devolve o lançamento original (200, `replayed: true`).
"""

from __future__ import annotations

from ninja import Header, Router
from ninja.responses import codes_4xx

from matches import selectors, services
from matches.domain import CATALOG, DomainError, NewEvent

from .errors import ApiError, invalid_input
from .responses import CATALOG_CACHE, respond
from .schemas import CatalogOut, ErrorOut, EventIn, PostEventOut, StatusIn, StatusOut, VoidIn, VoidOut
from .security import CHANGE_STATUS, OPS_PERMISSIONS, POST_EVENT, VOID_EVENT, OperatorAuth

router = Router(tags=["Operação"])

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_DESCRIPTION = (
    f"Chave do pedido (1 a {services.IDEMPOTENCY_KEY_MAX_LENGTH} caracteres; o front usa crypto.randomUUID()). "
    "A mesma chave devolve o lançamento original."
)


def idempotency_key(value: str | None) -> str:
    """Header obrigatório: 1..IDEMPOTENCY_KEY_MAX_LENGTH caracteres → senão 400."""
    key = (value or "").strip()
    if not key:
        raise invalid_input(IDEMPOTENCY_HEADER, "Informe o header Idempotency-Key.")
    if len(key) > services.IDEMPOTENCY_KEY_MAX_LENGTH:
        raise invalid_input(
            IDEMPOTENCY_HEADER,
            f"O header Idempotency-Key tem no máximo {services.IDEMPOTENCY_KEY_MAX_LENGTH} caracteres.",
        )
    if services.DERIVED_KEY_MARK in key:
        raise invalid_input(IDEMPOTENCY_HEADER, f'O header Idempotency-Key não pode conter "{services.DERIVED_KEY_MARK}".')
    return key


def require_status_permission(request, event_type: str) -> None:
    """Evento de status (adiar, suspender, cancelar...) lançado por `/events` exige
    também a permissão de mudar status: `/events` não é atalho para quem só lança lances."""
    spec = CATALOG.get(event_type)
    if spec is not None and spec.kind == "status" and not request.user.has_perm(CHANGE_STATUS):
        raise ApiError(
            403,
            "permission_denied",
            "Seu perfil não tem permissão para mudar o status da partida.",
            {"required_any": [CHANGE_STATUS], "type": event_type},
        )


@router.post(
    "/matches/{match_id}/events",
    auth=OperatorAuth(POST_EVENT),
    response={201: PostEventOut, 200: PostEventOut, codes_4xx: ErrorOut},
    summary="Lança um evento",
)
def post_event(
    request,
    match_id: int,
    data: EventIn,
    key: str | None = Header(None, alias=IDEMPOTENCY_HEADER, description=IDEMPOTENCY_DESCRIPTION),
):
    """201 no lançamento novo; 200 com `replayed: true` quando a chave já existia.
    Avisos sem `confirm` → 422 `confirmation_required` (com `warnings`). Tipo de status
    (`postponed`, `cancelled`...) exige também `matches.change_status` (senão 403)."""
    key = idempotency_key(key)
    require_status_permission(request, data.type)
    new = NewEvent(
        type=data.type,
        minute=data.minute,
        stoppage=data.stoppage,
        team_id=data.team_id,
        player_id=data.player_id,
        payload=data.payload or {},
        annuls_event_id=data.annuls_event_id,
    )
    result = services.post_event(
        match_id, request.user, new, idempotency_key=key, source=data.source, confirm=data.confirm, request=request
    )
    return respond(selectors.post_payload(result), status=201 if result.created else 200)


@router.post(
    "/matches/{match_id}/events/{event_id}/edit",
    auth=OperatorAuth(POST_EVENT),
    response={200: PostEventOut, codes_4xx: ErrorOut},
    summary="Corrige os dados de um lance",
)
def edit_event(request, match_id: int, event_id: int, data: EventIn):
    """Corrige minuto, time, jogador e detalhes de um lance, no mesmo lugar da sequência.
    Exige também `matches.void_event` (é uma correção). Regra violada → 422; tipo
    diferente do lance, andamento ou status → 422 `event_not_editable`."""
    if not request.user.has_perm(VOID_EVENT):
        raise ApiError(403, "permission_denied", "Seu perfil não pode corrigir lançamentos.", {"required": [VOID_EVENT]})
    new = NewEvent(
        type=data.type,
        minute=data.minute,
        stoppage=data.stoppage,
        team_id=data.team_id,
        player_id=data.player_id,
        payload=data.payload or {},
        annuls_event_id=data.annuls_event_id,
    )
    try:
        result = services.edit_event(match_id, event_id, request.user, new, confirm=data.confirm, request=request)
    except DomainError as exc:
        if exc.code != "event_not_found":
            raise
        raise ApiError(404, "not_found", exc.message, {"event_id": event_id, "match_id": match_id}) from exc
    payload = selectors.post_payload(result)
    payload["replayed"] = False
    return respond(payload)


@router.post(
    "/matches/{match_id}/events/{event_id}/void",
    auth=OperatorAuth(VOID_EVENT),
    response={200: VoidOut, codes_4xx: ErrorOut},
    summary="Cancela um lançamento errado",
)
def void_event(request, match_id: int, event_id: int, data: VoidIn | None = None):
    """Cai junto o que depende dele (vermelho automático, anulação do gol).
    Já cancelado → 200 com `already: true`. Lançamento de outra partida → 404."""
    try:
        outcome = services.void_event(match_id, event_id, request.user, reason=data.reason if data else "", request=request)
    except DomainError as exc:
        if exc.code != "event_not_found":
            raise
        raise ApiError(404, "not_found", exc.message, {"event_id": event_id, "match_id": match_id}) from exc
    return respond(selectors.void_payload(outcome))


@router.post(
    "/matches/{match_id}/status",
    auth=OperatorAuth(CHANGE_STATUS),
    response={201: StatusOut, 200: StatusOut, codes_4xx: ErrorOut},
    summary="Adia, suspende, retoma, reagenda ou cancela",
)
def change_status(
    request,
    match_id: int,
    data: StatusIn,
    key: str | None = Header(None, alias=IDEMPOTENCY_HEADER, description=IDEMPOTENCY_DESCRIPTION),
):
    """201 na mudança nova; 200 com `replayed: true` quando a chave já existia."""
    key = idempotency_key(key)
    result = services.change_status(
        match_id,
        request.user,
        data.action,
        idempotency_key=key,
        kickoff_at=data.kickoff_at or None,
        reason=data.reason or "",
        request=request,
    )
    return respond(selectors.status_payload(result), status=201 if result.created else 200)


@router.get(
    "/catalog",
    auth=OperatorAuth(*OPS_PERMISSIONS),
    response={200: CatalogOut, codes_4xx: ErrorOut},
    summary="Catálogo de eventos, ações de status, períodos e status",
)
def catalog(request):
    return respond(selectors.catalog_payload(), cache=CATALOG_CACHE)
