"""Schemas da API (Django Ninja).

Entrada: validam o FORMATO (tipos, tamanhos, valores fechados) → 400
`invalid_input`. As regras do jogo ficam no domínio (422).

Saída: documentam no OpenAPI (`/api/docs`) o primeiro nível das respostas; os
objetos aninhados (MatchOut, EventOut, StageStandingsOut...) seguem o
docs/CONTRACT.md §3 e saem como os dicionários prontos dos selectors.
"""

from __future__ import annotations

from typing import Any, Literal

from ninja import Field, Schema

from matches.domain import TEXT_MAX_LENGTH

# --- Erros -----------------------------------------------------------------------------


class WarningOut(Schema):
    code: str
    message: str


class ErrorOut(Schema):
    code: str = Field(..., description="invalid_input, not_authenticated, permission_denied, csrf_failed, not_found ou o código da regra (422)")
    message: str
    details: dict[str, Any]
    warnings: list[WarningOut] | None = Field(None, description="Só em confirmation_required")


# --- Autenticação -------------------------------------------------------------------------


class LoginIn(Schema):
    username: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1, max_length=4096)


class PermissionsOut(Schema):
    post_event: bool
    void_event: bool
    change_status: bool
    manage_users: bool
    admin_site: bool


class MeUser(Schema):
    id: int
    username: str
    name: str
    roles: list[str]
    permissions: PermissionsOut


class LoginOut(Schema):
    user: MeUser
    csrf_token: str = Field(..., description="Token CSRF novo (o login troca o token)")


class MeOut(Schema):
    authenticated: bool
    user: MeUser | None
    csrf_token: str
    server_time: str


class OkOut(Schema):
    ok: bool


# --- Operação ----------------------------------------------------------------------------


class EventIn(Schema):
    """Corpo do lançamento (CONTRACT §4)."""

    type: str = Field(..., min_length=1, max_length=40, description="Tipo do catálogo (GET /api/ops/catalog)")
    minute: int | None = None
    stoppage: int | None = None
    team_id: int | None = None
    player_id: int | None = Field(
        None,
        description="Não use: jogadores não têm cadastro (enviar → 400 invalid_input). O nome vai em payload.player.",
        json_schema_extra={"deprecated": True},
    )
    payload: dict[str, Any] | None = Field(
        None, description="Campos do tipo (catálogo), ex.: {\"player\": \"Zé Roberto\", \"origin\": \"penalty\"}"
    )
    annuls_event_id: int | None = None
    confirm: bool = Field(False, description="Lança mesmo com avisos (422 confirmation_required)")
    source: Literal["operator", "feed", "script"] = "operator"


class StatusIn(Schema):
    action: str = Field(..., min_length=1, max_length=40, description="postpone, suspend, resume, reschedule ou cancel")
    kickoff_at: str | None = Field(None, max_length=64, description="Reagendar: ISO 8601 com data e hora (sem fuso = Brasília)")
    reason: str | None = Field(None, max_length=TEXT_MAX_LENGTH)


class PartialInfoIn(Schema):
    partial_info: bool = Field(..., description="true: jogo com informações parciais (sem relógio público)")


class MatchStateOut(Schema):
    match: dict[str, Any] = Field(..., description="MatchOut (detalhe)")
    available: "AvailableOut"


class VoidIn(Schema):
    reason: str = Field("", max_length=TEXT_MAX_LENGTH)


class AvailableOut(Schema):
    events: list[str]
    status: list[str]


class PostEventOut(Schema):
    event: dict[str, Any] = Field(..., description="EventOut")
    derived: list[dict[str, Any]] = Field(..., description="[EventOut] (ex.: vermelho automático)")
    match: dict[str, Any] = Field(..., description="MatchOut (detalhe)")
    available: AvailableOut
    warnings: list[WarningOut]
    replayed: bool


class StatusOut(Schema):
    event: dict[str, Any] = Field(..., description="EventOut")
    match: dict[str, Any] = Field(..., description="MatchOut (detalhe)")
    available: AvailableOut
    replayed: bool


class VoidOut(Schema):
    voided: list[int]
    match: dict[str, Any] = Field(..., description="MatchOut (detalhe)")
    available: AvailableOut
    already: bool


class CatalogOut(Schema):
    events: list[dict[str, Any]] = Field(..., description="[EventSpecOut]")
    status_actions: list[dict[str, Any]]
    periods: list[dict[str, Any]]
    statuses: list[dict[str, Any]]


# --- Leitura -----------------------------------------------------------------------------


class Stamped(Schema):
    server_time: str = Field(..., description="Instante UTC (ISO 8601 com Z)")
    timezone: str = Field(..., description="America/Sao_Paulo")


class HomeOut(Stamped):
    date: str
    cursor: int
    competitions: list[dict[str, Any]]
    latest_goals: list[dict[str, Any]] = Field(..., description="[LatestGoalOut]")


class CompetitionRefOut(Schema):
    id: int
    name: str
    slug: str
    short_name: str
    position: int


class CompetitionsOut(Schema):
    competitions: list[CompetitionRefOut]


class CompetitionOut(Stamped):
    cursor: int
    competition: CompetitionRefOut
    season: dict[str, Any] | None
    stages: list[dict[str, Any]]
    current_stage_id: int | None
    current_round_id: int | None
    stage: dict[str, Any] | None
    rankings: list[dict[str, Any]] = Field(default_factory=list, description="[RankingRef] com botão na página da competição")


class StandingsOut(Stamped):
    stage_id: int
    stage_name: str
    kind: Literal["live", "official"]
    points: dict[str, int]
    criteria: list[dict[str, Any]]
    legend: list[dict[str, Any]]
    groups: list[dict[str, Any]]


class RankingStandingsOut(Stamped):
    ranking_id: int
    stage_name: str = Field(..., description="Nome da classificação")
    scope: Literal["overall", "custom"]
    stage_ids: list[int] = Field(..., description="Fases que entram (para atualizar ao vivo)")
    kind: Literal["live", "official"]
    points: dict[str, int]
    criteria: list[dict[str, Any]]
    legend: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]


class MatchesOut(Stamped):
    matches: list[dict[str, Any]] = Field(..., description="[MatchOut]")


class MatchDetailOut(Stamped):
    cursor: int
    match: dict[str, Any] = Field(..., description="MatchOut (detalhe)")
    available: AvailableOut
