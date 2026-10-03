# Contrato de integração — Futebol de Raízes

Documento de referência para quem implementa módulos em paralelo. O plano
original está em `docs/PLANO.md`. Aqui ficam as decisões concretas de nomes,
assinaturas e formatos JSON. **Se precisar mudar algo deste contrato, mude também
este arquivo.**

## 1. Módulos e donos

| Pasta | Conteúdo | Observação |
| --- | --- | --- |
| `config/` | settings, urls, asgi | `BRAND` (logo) e `REALTIME` em settings |
| `core/` | trava de escrita (`core.locks`), datas (`core.timeutils`), páginas e `/health` | `locked_atomic()` = transação + `pg_advisory_xact_lock` |
| `accounts/` | `User` próprio, perfis (`accounts.roles`) | grupos Operador/Administrador recriados no `migrate` |
| `competitions/` | competição, temporada, fase, critério, zona, grupo, rodada, time, jogador | fase `league` cria grupo único "Tabela" |
| `matches/` | partida, evento, confronto, enriquecimento; `domain.py` (puro), `services.py` (escrita), `selectors.py` (leitura/serialização) | |
| `standings/` | `Standing` (cache); `domain.py` (puro), `services.py` (recalcular/ler) | |
| `realtime/` | `Outbox`, `outbox.enqueue`, `hub.py` (publicador), `views.stream` (SSE) | |
| `observability/` | auditoria, logs JSON, métricas `/metrics` | `audit.record(...)`, `metrics.inc(...)` |
| `public_api/` | API pública `/public/v1` (fase 12) | chave, limite, cache HTTP |
| `api/` | Ninja: `/api/auth`, `/api/ops`, leitura | `api/main.py` monta os routers |
| `templates/`, `static/` | 3 páginas + CSS/JS/ícones/sons | sem framework, sem build |

Regra de ouro: **domínio puro não importa Django**. O ORM só carrega e grava.

## 2. Caminho de escrita (matches/services.py)

```python
@dataclass
class PostResult:
    event: MatchEvent            # evento pedido (ou o original, em replay)
    derived: list[MatchEvent]    # ex.: vermelho automático
    match: Match                 # já atualizado
    created: bool                # False quando a chave de idempotência já existia
    warnings: list[DomainWarning]

def post_event(match_id: int, user, new: NewEvent, *, idempotency_key: str,
               source: str = "operator", confirm: bool = False, request=None) -> PostResult
def void_event(match_id: int, event_id: int, user, *, reason: str = "", request=None) -> VoidOutcome
     # VoidOutcome(match: Match, voided_ids: list[int], already: bool)
def change_status(match_id: int, user, action: str, *, idempotency_key: str,
                  kickoff_at: datetime | None = None, reason: str = "", source="operator", request=None) -> PostResult
```

Cada função, numa única transação com `core.locks.locked_atomic()`:

1. Replay: `(match_id, idempotency_key)` já existe → devolve o evento original (`created=False`), sem gravar nada.
2. Carrega partida (`select_for_update` não é necessário: a trava global serializa) e eventos.
3. Monta `MatchContext` (times, confronto com outros jogos, escalações) e chama o domínio.
4. Grava evento(s) com `sequence` = max + 1, `created_by`, `source`, `created_at = core.timeutils.now()`.
5. Atualiza o cache da partida: status, period, period_started_at (created_at do evento
   que abriu o período, `state.period_started_seq`, **somado ao tempo parado** nas
   suspensões fechadas de `state.period_pauses`), placar, pênaltis, finished_at (no fim
   de jogo; limpa se o fim for cancelado), kickoff_at (reagendamento), `version += 1`.
6. Partida de confronto: `compute_tie_result` e grava `winner_team`/`decided_by` no `Tie`.
7. Partida de grupo: `standings.services.recompute_group(group)` (oficial e ao vivo).
8. Outbox: sempre `match`; `standings` quando a partida é de grupo; `goals` quando o
   conjunto de gols válidos mudou (entrada, saída ou volta).
9. Auditoria: `observability.audit.record("event.create" | "event.void" | "match.status", ...)`.
10. Métricas: `fdr_events_posted_total`, `fdr_events_voided_total`, `fdr_status_changes_total`, `fdr_domain_rejections_total`.

`DomainError` sobe para a API, que responde 422. Nada é gravado quando há erro.

### Domínio da partida (matches/domain.py)

* Fluxo do serviço: `state = derive_state(events, ctx)` → `apply_event(state, events, new, ctx, confirm=...)`
  (ou `status_action_event(action, kickoff_at=, reason=)` → `apply_event`). Grave `result.event` e
  `result.derived` como vierem: período, minuto padrão dos eventos estruturais (início 0, intervalo 45,
  2T 45, prorrogação 90, pênaltis 90/120, fim 90/120), time herdado no gol anulado e payload já
  normalizado (só as chaves do tipo; com escalação, nome canônico e `player_id` completados).
* Cancelamento: `check_void(events, event_id, ctx)` → `VoidResult(state, voided_ids)`; marque como
  cancelados **todos** os `voided_ids` (o pedido vem primeiro). Caem junto: derivados
  (`payload["derived_from_sequence"] == sequence` da origem), anulações que apontam para o gol e o
  vermelho automático cujo amarelo deixou de ser o 2º.
* Vermelho automático (2º amarelo): payload `{"player", "reason": "second_yellow", "derived_from_sequence": <sequence do amarelo>}`.
  `EventOut.derived` = `domain.derived_from(event) is not None`. Ícone com variações: `domain.event_icon(type, payload)`.
* Códigos 422 além dos listados em `apply_event`: `confirmation_required` (com `warnings`),
  `unknown_event_type`, `period_mismatch` (replay), `event_not_found`, `already_voided`,
  `void_derived_event` (vermelho automático só cai com o amarelo), `void_breaks_sequence` (`details.cause` = regra violada no replay,
  ou `second_yellow_without_red` quando um amarelo passaria a ser o 2º do jogador sem o vermelho automático).
* Relógio com suspensão: `state.period_pauses` = `((seq do suspended, seq do resumed | None), ...)` do período
  corrente (zera a cada período). Ao vivo: `period_started_at` = abertura + soma de `created_at(resumed) -
  created_at(suspended)`. Suspenso (`running=false`): o minuto fica parado no `created_at` da suspensão aberta.
* Minuto no formulário do operador: `EventSpec.minute == "required"` vale com o relógio correndo (1T, 2T,
  prorrogação); no intervalo e nos pênaltis é opcional. `domain.minute_mode(type, period)` devolve o que vale
  agora. Nos pênaltis, o minuto (se vier) é o do início da disputa: 120 com prorrogação, 90 sem.
* Reagendamento: `kickoff_at` em ISO 8601 com data **e** hora (só a data é recusada: `invalid_payload`).
  Sem fuso = horário de Brasília: o serviço aplica `settings.TIME_ZONE` antes de gravar `Match.kickoff_at`.
* Rótulos prontos para a API/catálogo: `STATUS_ACTION_LABELS`, `GOAL_ORIGIN_LABELS`, `PENALTY_MISS_LABELS`
  (`payload.outcome` opcional do pênalti perdido). Gols anulados: `domain.annulled_goal_ids(events)`.

Outros pontos de escrita que passam pelo mesmo núcleo:
* Django Admin: salvar fase (pontuação/critérios) → `standings.services.on_stage_rules_changed(stage)`
  (recalcula e publica); salvar zona → só publica `standings`; salvar partida → recalcula o grupo
  se mudou algo que afeta a tabela. Eventos não são editáveis no admin (somente leitura + ação "cancelar lançamento" que chama `void_event`).
* `seed`: usa `post_event`/`change_status` (source="script").

## 3. Leitura e serialização (matches/selectors.py, standings/services.py)

Toda resposta com horário traz `server_time` (ISO UTC com `Z`) e `timezone` (`"America/Sao_Paulo"`).
Respostas da home e da competição trazem `cursor` (maior id do outbox), **lido antes do estado**.

### TeamOut
```json
{"id": 1, "name": "Sport Club do Recife", "short_name": "SPT", "city": "Recife",
 "color_primary": "#D7141A", "color_secondary": "#000000", "crest_url": ""}
```

### EventOut
```json
{"id": 91, "sequence": 7, "type": "goal", "type_label": "Gol", "kind": "game",
 "period": "second_half", "period_label": "2º tempo", "period_short": "2T",
 "minute": 72, "stoppage": null, "minute_label": "72'",
 "team_id": 1, "team_side": "home", "player": {"id": null, "name": "Zé Roberto"},
 "payload": {"player": "Zé Roberto", "origin": "penalty"},
 "annuls_event_id": null, "annulled": false, "derived": false,
 "score_after": {"home": 2, "away": 1},
 "created_at": "2026-10-03T21:12:09Z"}
```
`annulled` = gol anulado por anulação válida. `score_after` só em gols válidos (null nos outros).
Lançamentos cancelados **nunca** aparecem nas leituras (`visible_events`).

### GoalOut (resumo no card) / LatestGoalOut (home)
```json
{"event_id": 91, "match_id": 12, "team_id": 1, "team_side": "home",
 "player": "Zé Roberto", "origin": "penalty", "period": "second_half",
 "minute": 72, "stoppage": null, "minute_label": "72'",
 "score_after": {"home": 2, "away": 1}, "created_at": "2026-10-03T21:12:09Z"}
```
LatestGoalOut acrescenta `"match": {"id", "competition": {"name","slug"}, "home": TeamOut, "away": TeamOut}` e `"team": TeamOut`.

### TieOut
```json
{"id": 4, "legs": 2, "leg": 2, "extra_time": true, "position": 1,
 "round": {"id": 30, "number": 1, "name": "Semifinal"},
 "team_a": TeamOut, "team_b": TeamOut,
 "aggregate": {"team_a": 3, "team_b": 3}, "winner_team_id": 7,
 "decided_by": "penalties", "decided_by_label": "nos pênaltis", "complete": true}
```
`leg` é o jogo desta partida (só quando embutido em MatchOut). TieDetailOut (página da competição) = TieOut sem `leg` + `"matches": [MatchOut resumido]`.

### MatchOut
```json
{"id": 12,
 "competition": {"id": 1, "name": "Pernambucano Raiz", "slug": "pernambucano", "short_name": "PE Raiz"},
 "stage": {"id": 3, "name": "1ª fase", "format": "league"},
 "group": {"id": 4, "name": "Tabela"}, "round": {"id": 9, "number": 5, "name": "Rodada 5"},
 "kickoff_at": "2026-10-03T19:30:00Z", "finished_at": null,
 "venue": "Ilha do Retiro", "city": "Recife, PE",
 "status": "live", "status_label": "Ao vivo",
 "period": "second_half", "period_label": "2º tempo", "period_short": "2T",
 "period_started_at": "2026-10-03T20:35:12Z",
 "clock": {"running": true, "offset": 45, "regular_end": 90, "stoppage_announced": 4},
 "home": TeamOut, "away": TeamOut,
 "home_score": 2, "away_score": 1, "home_penalties": null, "away_penalties": null,
 "winner": null,
 "version": 17,
 "tie": TieOut | null,
 "goals": [GoalOut],
 "cards": {"home": {"yellow": 1, "red": 0}, "away": {"yellow": 2, "red": 1}},
 "red_cards": [{"team_side": "away", "player": "Fulano", "minute_label": "63'"}]}
```
* `clock` é null fora de 1T/2T/prorrogação em andamento. `running=false` em suspenso.
* `winner`: "home" | "away" | "draw" | null (só com status finished; considera pênaltis).
* Detalhe (`GET /api/matches/:id`) e mensagem `match` do stream acrescentam:
  `"events": [EventOut]`, `"lineups": {"home": LineupOut|null, "away": LineupOut|null}`,
  `"officials": [{"role","role_label","name","state"}]`,
  `"broadcasts": [{"name","url","kind","kind_label"}]`,
  `"stats": [{"key","label","home","away"}]`, `"attendance": 42318 | null`, `"revenue_cents": 234155000 | null`.
* LineupOut: `{"formation": "4-3-3", "coach": "Fulano", "starters": [{"name","number","position"}], "substitutes": [...]}`.

### StageStandingsOut
```json
{"stage_id": 3, "stage_name": "1ª fase", "kind": "live",
 "points": {"win": 3, "draw": 1, "loss": 0},
 "criteria": [{"key": "points", "label": "Pontos"}, ...],
 "legend": [{"name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 4}],
 "groups": [{"id": 4, "name": "Tabela", "rows": [
   {"position": 1, "team": TeamOut, "played": 5, "won": 4, "drawn": 1, "lost": 0,
    "goals_for": 11, "goals_against": 3, "goal_difference": 8, "points": 13,
    "yellow_cards": 6, "red_cards": 0, "tied": false,
    "zone": {"name": "Classificados", "color": "#1B7F3B"} | null,
    "playing": true}]}]}
```
`playing` = o time está em jogo ao vivo agora (destaque visual).

## 4. Rotas

Erros: `{"code": "...", "message": "...", "details": {...}}` (+ `"warnings": [{"code","message"}]` em `confirmation_required`).
`400 invalid_input` (formato), `401 not_authenticated`, `403 permission_denied`, `404 not_found`, `422 <regra>`.

| Rota | Resposta |
| --- | --- |
| `POST /api/auth/login` `{"username","password"}` | `200 {"user": MeUser}` · `401 invalid_credentials` · protegido por CSRF |
| `POST /api/auth/logout` | `200 {"ok": true}` |
| `GET /api/auth/me` | `200 {"authenticated": bool, "user": MeUser|null, "csrf_token": "..."}` (sempre seta o cookie CSRF) |
| `POST /api/ops/matches/{id}/events` + `Idempotency-Key` | `201 {"event": EventOut, "derived": [EventOut], "match": MatchOut(detalhe), "available": Available, "warnings": [...], "replayed": false}` · replay → `200` com `"replayed": true` |
| `POST /api/ops/matches/{id}/events/{eventId}/void` `{"reason"}` | `200 {"voided": [ids], "match": MatchOut(detalhe), "available": Available}` |
| `POST /api/ops/matches/{id}/status` + `Idempotency-Key` `{"action","kickoff_at"?,"reason"?}` | `201 {"event": EventOut, "match": ..., "available": ...}` |
| `GET /api/ops/catalog` | `{"events": [EventSpecOut], "status_actions": [{"action","label"}], "periods": [...], "statuses": [...]}` |
| `GET /api/home?date=YYYY-MM-DD` | HomeOut, `Cache-Control: no-store` |
| `GET /api/competitions` | `{"competitions": [{"id","name","slug","short_name","position"}]}` |
| `GET /api/competitions/{slug}?stage=&round=` | CompetitionOut |
| `GET /api/stages/{id}/standings?live=1` | StageStandingsOut + `server_time`, `timezone` |
| `GET /api/matches?roundId=&date=&status=&stageId=` | `{"server_time","timezone","matches": [MatchOut]}` |
| `GET /api/matches/{id}` | `{"server_time","timezone","cursor","match": MatchOut(detalhe), "available": Available}` |
| `GET /api/stream?after=N` | SSE (seção 5) |

* MeUser: `{"id","username","name","roles": ["Operador"], "permissions": {"post_event": bool, "void_event": bool, "change_status": bool, "manage_users": bool, "admin_site": bool}}`.
* Available: `{"events": ["goal", ...], "status": ["suspend", ...]}` (de `domain.available_actions`).
* EventSpecOut: `{"type","label","kind","icon","minute": "required|optional|none","periods": [...], "fields": [{"name","kind","label","required","choices": [[v,l]]}]}`.
* Corpo do lançamento: `{"type","minute"?,"stoppage"?,"team_id"?,"player_id"?,"payload"?: {},"annuls_event_id"?,"confirm"?: false,"source"?: "operator"}`.
* HomeOut: `{"date","server_time","timezone","cursor","competitions": [{"id","name","slug","short_name","position","stages": [{"id","name","format","matches": [MatchOut],"standings": StageStandingsOut|null}]}],"latest_goals": [LatestGoalOut]}`.
  Só competições com jogo no dia, em `position`. Jogo da véspera que passa da meia-noite fica até 2h depois de `finished_at`.
* CompetitionOut: `{"server_time","timezone","cursor","competition": {...},"season": {"id","year"},"stages": [{"id","name","format","position","rounds": [{"id","number","name"}]}],"current_stage_id","current_round_id","stage": {"id","name","format","standings": StageStandingsOut|null,"matches": [MatchOut da rodada],"ties": [TieDetailOut da rodada] (mata-mata)}}`.
  Fase/rodada atuais = primeiras com jogo ainda não encerrado (senão a última).

## 5. Stream SSE

* `GET /api/stream?after=N` — um stream só por página, todas as competições.
* Retomada: header `Last-Event-ID` (reconexão automática) vale mais que `after`.
* Quadros: `id: <outbox.id>\nevent: <topic>\ndata: <json>\n\n`. Abre com `retry: 3000` e um `ping`.
* `ping` (sem id, fora do outbox) ao abrir e a cada 20 s: `{"server_time": "...Z"}`.
* Mensagens (estado completo, o front substitui sem merge):
  * `match`: `{"stage_id", "competition_id", "match": MatchOut(detalhe)}`
  * `standings`: `{"stage_id", "standings": StageStandingsOut(live)}`
  * `goals`: `{"date": "YYYY-MM-DD", "changes": [{"kind": "added"|"removed"|"restored", "reason": "annulled"|"voided"|"unvoided"|null, "goal": LatestGoalOut}], "latest_goals": [LatestGoalOut]}`
* Hub (`realtime/hub.py`): um por processo. Busca o outbox uma vez por lote e
  distribui o mesmo quadro (já serializado) para todas as filas. Acorda no
  `on_commit` de `enqueue`, com polling de segurança. Marca `published_at`.
  Apaga mensagens com mais de 24 h (ao iniciar e a cada hora); o buffer de
  reenvio em memória segue a mesma retenção. Ao iniciar, as linhas ainda sem
  `published_at` passam a publicadas (ficam disponíveis para reenvio).
* Detalhes para o front (`stream.js`):
  * sem `after` e sem `Last-Event-ID`: só o que vier depois da conexão (sem reenvio);
  * `after` ou `Last-Event-ID` que não seja inteiro ≥ 0 → `400 {"code": "invalid_input", "details": {"field": ...}}`
    (o `EventSource` falha de vez; a página recria com `after` = último id recebido);
  * posição maior que o maior id do outbox (id de um banco recriado) vale como o maior id:
    o stream segue a partir dali em vez de ficar mudo;
  * o servidor pode encerrar o stream (conexão lenta que acumulou mensagens, falha no
    reenvio, reinício do processo): é só reconexão normal, o navegador volta com `Last-Event-ID`.

## 6. Front-end

Páginas (templates Django só para injetar a marca; dados vêm da API):
`/` (home), `/competition.html?slug=`, `/operator.html`.

Módulos ES em `static/js/`: `api.js`, `stream.js`, `render.js`, `clock.js`,
`alerts.js`, `theme.js`, `format.js`, `match-card.js`, `standings.js`,
`home.js`, `competition.js`, `operator.js`.

* Texto sempre por `textContent`/`<template>`; nunca `innerHTML` com dado da API.
* Relógio e horários em `America/Sao_Paulo` via `Intl.DateTimeFormat(..., {timeZone})`, com o desvio do servidor (maior dos 5 últimos).
* Identidade visual: ver `docs/IDENTIDADE.md`.
