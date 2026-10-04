"""Schemas de saída da API pública (`/public/v1`): a lista de permissão.

Cada classe lista, campo a campo, o que a API pública expõe. A resposta é
validada e serializada por estes schemas (Django Ninja/pydantic ignoram chaves
que não estão declaradas): um campo novo nos dicionários internos dos selectors
só aparece aqui se for declarado de propósito.

Nunca entram: origem do lançamento (`source`), autor (`created_by`), chave de
idempotência, cancelamento (`voided*`), `sequence`, `version`, `payload` bruto,
`cursor` do outbox nem as ligações internas entre eventos (`annuls_event_id`,
`derived_from_sequence`). Eventos de status e gols anulados ficam de fora da
linha do tempo (ver `public_api.api.public_events`).
"""

from __future__ import annotations

from ninja import Field, Schema

# --- Erros -----------------------------------------------------------------------------


class ErrorOut(Schema):
    code: str = Field(
        ...,
        description="invalid_api_key (401), rate_limited (429), invalid_input (400) ou not_found (404)",
    )
    message: str
    details: dict = Field(default_factory=dict)


# --- Referências -----------------------------------------------------------------------


class TeamOut(Schema):
    id: int
    name: str
    short_name: str
    city: str
    color_primary: str = Field(..., description="#RRGGBB")
    color_secondary: str = Field(..., description="#RRGGBB")
    crest_url: str = Field(..., description="URL do escudo (vazio sem escudo)")


class CompetitionRefOut(Schema):
    id: int
    name: str
    slug: str
    short_name: str


class StageRefOut(Schema):
    id: int
    name: str
    format: str = Field(..., description="league | groups | knockout")


class GroupRefOut(Schema):
    id: int
    name: str


class RoundRefOut(Schema):
    id: int
    number: int
    name: str


class ScoreOut(Schema):
    home: int
    away: int


# --- Competições -------------------------------------------------------------------------


class CompetitionItemOut(Schema):
    id: int
    name: str
    slug: str
    short_name: str
    position: int


class CompetitionsOut(Schema):
    competitions: list[CompetitionItemOut]


class SeasonOut(Schema):
    id: int
    year: int


class StageDetailOut(Schema):
    id: int
    name: str
    format: str = Field(..., description="league | groups | knockout")
    position: int
    has_standings: bool = Field(..., description="false no mata-mata (sem classificação)")
    groups: list[GroupRefOut]
    rounds: list[RoundRefOut]
    current_round_id: int | None = Field(None, description="Primeira rodada com jogo não encerrado (senão a última)")


class CompetitionDetailOut(Schema):
    competition: CompetitionItemOut
    season: SeasonOut | None = Field(None, description="Temporada mais recente (null sem temporada)")
    stages: list[StageDetailOut]
    current_stage_id: int | None = Field(None, description="Primeira fase com jogo não encerrado (senão a última)")
    current_round_id: int | None = None
    timezone: str


# --- Partidas ------------------------------------------------------------------------------


class ClockOut(Schema):
    running: bool = Field(..., description="false com o jogo suspenso")
    offset: int = Field(..., description="Minuto em que o período começa (0, 45 ou 90)")
    regular_end: int = Field(..., description="Fim regulamentar do período (45, 90 ou 120)")
    stoppage_announced: int | None = Field(None, description="Acréscimos anunciados no período")
    paused_at: str | None = Field(None, description="Suspenso: horário da suspensão (UTC); o minuto para ali")


class AggregateOut(Schema):
    team_a: int
    team_b: int


class TieOut(Schema):
    id: int
    legs: int = Field(..., description="1 = jogo único, 2 = ida e volta")
    leg: int | None = Field(None, description="Jogo desta partida no confronto (1 ou 2)")
    extra_time: bool
    position: int
    round: RoundRefOut | None = None
    team_a: TeamOut
    team_b: TeamOut
    aggregate: AggregateOut
    winner_team_id: int | None = None
    decided_by: str | None = Field(None, description="aggregate | extra_time | penalties")
    decided_by_label: str | None = None
    complete: bool


class GoalOut(Schema):
    event_id: int
    team_id: int | None = None
    team_side: str | None = Field(None, description="home | away (time beneficiado)")
    player: str | None = None
    origin: str = Field(..., description="open_play | penalty | own_goal")
    period: str | None = None
    minute: int | None = None
    stoppage: int | None = None
    minute_label: str
    score_after: ScoreOut | None = None
    created_at: str


class CardCountOut(Schema):
    yellow: int
    red: int


class CardsOut(Schema):
    home: CardCountOut
    away: CardCountOut


class RedCardOut(Schema):
    team_side: str
    player: str | None = None
    minute_label: str


class MatchOut(Schema):
    id: int
    competition: CompetitionRefOut
    stage: StageRefOut
    group: GroupRefOut | None = None
    round: RoundRefOut | None = None
    kickoff_at: str = Field(..., description="UTC (ISO 8601 com Z)")
    finished_at: str | None = None
    venue: str
    city: str
    status: str = Field(..., description="scheduled | delayed | live | finished | postponed | suspended | cancelled")
    status_label: str
    period: str | None = Field(None, description="first_half | half_time | second_half | extra_time | extra_half_time | extra_second_half | penalties")
    period_label: str | None = None
    period_short: str | None = None
    period_started_at: str | None = None
    clock: ClockOut | None = Field(None, description="Relógio do período; null fora de 1T/2T/prorrogação")
    home: TeamOut
    away: TeamOut
    home_score: int
    away_score: int
    home_penalties: int | None = None
    away_penalties: int | None = None
    winner: str | None = Field(None, description="home | away | draw (só encerrado; considera os pênaltis)")
    tie: TieOut | None = None
    goals: list[GoalOut] = Field(..., description="Só gols válidos (sem anulados), em ordem")
    cards: CardsOut
    red_cards: list[RedCardOut]


class EventOut(Schema):
    """Lance público da linha do tempo, com campos tipados no lugar do payload."""

    id: int
    type: str = Field(..., description="Tipo do catálogo com exibição pública (nunca status nem gol anulado)")
    type_label: str
    kind: str = Field(..., description="structural (início, intervalo, fim...) | game (lance)")
    period: str | None = None
    period_label: str | None = None
    minute: int | None = None
    stoppage: int | None = None
    minute_label: str = Field(..., description='Ex.: "45+2\'"; vazio sem minuto')
    team_id: int | None = None
    team_side: str | None = Field(None, description="home | away")
    player: str | None = Field(None, description="Autor do gol, cartão, pênalti perdido ou cobrança")
    origin: str | None = Field(None, description="Gol: open_play | penalty | own_goal")
    origin_label: str | None = None
    assist: str | None = Field(None, description="Gol: assistência")
    player_out: str | None = Field(None, description="Substituição: quem sai")
    player_in: str | None = Field(None, description="Substituição: quem entra")
    second_yellow: bool | None = Field(None, description="Cartão vermelho: true quando veio do 2º amarelo")
    outcome: str | None = Field(None, description="Pênalti perdido: saved | off_target | woodwork")
    outcome_label: str | None = None
    scored: bool | None = Field(None, description="Cobrança da disputa de pênaltis: convertida?")
    stoppage_minutes: int | None = Field(None, description="Acréscimos anunciados (minutos)")
    incident: str | None = Field(None, description="Revisão do VAR: lance revisado")
    decision: str | None = Field(None, description="Revisão do VAR: decisão")
    score_after: ScoreOut | None = Field(None, description="Gol: placar depois dele")
    created_at: str


class LineupPlayerOut(Schema):
    name: str
    number: int | None = None
    position: str | None = Field(None, description="GK | DF | MF | FW")


class LineupOut(Schema):
    formation: str | None = None
    coach: str | None = None
    starters: list[LineupPlayerOut]
    substitutes: list[LineupPlayerOut]


class LineupsOut(Schema):
    home: LineupOut | None = None
    away: LineupOut | None = None


class OfficialOut(Schema):
    role: str
    role_label: str
    name: str
    state: str


class BroadcastOut(Schema):
    name: str
    url: str
    kind: str
    kind_label: str


class StatOut(Schema):
    key: str
    label: str
    home: int | None = None
    away: int | None = None


class MatchDetailOut(MatchOut):
    events: list[EventOut] = Field(..., description="Linha do tempo pública, em ordem")
    lineups: LineupsOut
    officials: list[OfficialOut]
    broadcasts: list[BroadcastOut]
    stats: list[StatOut]
    attendance: int | None = None
    revenue_cents: int | None = None


class MatchesOut(Schema):
    timezone: str
    matches: list[MatchOut]


class MatchEnvelopeOut(Schema):
    timezone: str
    match: MatchDetailOut


# --- Classificação ------------------------------------------------------------------------


class PointsOut(Schema):
    win: int
    draw: int
    loss: int


class CriterionOut(Schema):
    key: str
    label: str


class LegendOut(Schema):
    name: str
    color: str
    from_: int = Field(..., alias="from")
    to: int


class ZoneOut(Schema):
    name: str
    color: str


class StandingRowOut(Schema):
    position: int
    team: TeamOut
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int = Field(..., description="Pontos já com a punição/bonificação (points_adjustment)")
    points_adjustment: int = Field(0, description="Pontos tirados (negativo) ou dados (positivo) fora de campo; 0 sem ajuste")
    yellow_cards: int
    red_cards: int
    tied: bool = Field(..., description="Empate que sobrou depois de todos os critérios")
    zone: ZoneOut | None = None
    playing: bool = Field(..., description="O time está em jogo agora")


class GroupStandingsOut(Schema):
    id: int
    name: str
    rows: list[StandingRowOut]


class PointAdjustmentOut(Schema):
    team: TeamOut
    points: int = Field(..., description="Negativo = punição (perda de pontos); positivo = bonificação")
    reason: str


class StandingsOut(Schema):
    stage_id: int
    stage_name: str
    kind: str = Field(..., description="official (só encerrados) | live (com os jogos em andamento)")
    points: PointsOut
    criteria: list[CriterionOut]
    legend: list[LegendOut]
    groups: list[GroupStandingsOut]
    adjustments: list[PointAdjustmentOut] = Field(default_factory=list, description="Punições e bonificações em pontos da fase")
