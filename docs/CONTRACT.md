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
| `accounts/` | `User` próprio, perfis (`accounts.roles`) | grupos Operador/Administrador recriados no `migrate` (somente leitura no admin); entrar num deles marca `is_staff` |
| `competitions/` | competição, temporada, fase, critério, zona, grupo, rodada, time | fase `league` cria grupo único "Tabela"; jogador não tem cadastro (nome no lance e na escalação) |
| `matches/` | partida, evento, confronto, enriquecimento; `domain.py` (puro), `services.py` (escrita), `selectors.py` (leitura/serialização) | |
| `standings/` | `Standing` (cache), `PointAdjustment` (punição/bonificação em pontos); `domain.py` (puro), `services.py` (recalcular/ler) | |
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
    voided_ids: list[int]        # edit_event: os que caíram junto (vermelho automático); [] nos demais

def post_event(match_id: int, user, new: NewEvent, *, idempotency_key: str,
               source: str = "operator", confirm: bool = False, request=None, at=None) -> PostResult
def void_event(match_id: int, event_id: int, user, *, reason: str = "", request=None, at=None) -> VoidOutcome
     # VoidOutcome(match: Match, voided_ids: list[int], already: bool)
def change_status(match_id: int, user, action: str, *, idempotency_key: str,
                  kickoff_at: datetime | str | None = None, reason: str = "", source="operator",
                  request=None, at=None) -> PostResult
def on_match_edited(match, changed_fields, user=None, request=None) -> None   # Django Admin
def publish_match(match) -> Outbox                                          # só publica `match`
```

`at` (datetime, padrão `core.timeutils.now()`) é o `created_at`/`voided_at` gravado: só
seed, scripts e testes o passam (para datar um jogo ao vivo); a API nunca. O padrão é lido
**já com a trava** (quem esperou a trava não grava horário anterior ao do lançamento que passou antes).

Cada função, numa única transação com `core.locks.locked_atomic()`:

1. Replay: `(match_id, idempotency_key)` já existe → devolve o evento original e os derivados
   dele (`created=False`, `warnings=[]`), sem gravar nada. **A mesma chave com outro corpo
   continua sendo replay do original** (a chave identifica o pedido, não o conteúdo; vale
   também entre `/events` e `/status`).
2. Carrega partida (`select_for_update` não é necessário: a trava global serializa) e eventos.
3. Monta `MatchContext` (times, confronto com outros jogos, escalações) e chama o domínio.
4. Grava evento(s) com `sequence` = max + 1, `created_by`, `source`, `created_at = at`.
   Derivados (vermelho automático): `idempotency_key = f"{key}:auto:{n}"`, `source="system"`.
   Jogador não tem cadastro: o lance leva o **nome** (`payload.player`; substituição
   `payload.player_out`/`player_in`). Id de jogador (`NewEvent.player_id`, `payload.player_id`,
   `payload.player_out_id`, `payload.player_in_id`) → `services.InvalidInput` antes de tudo
   (`400 invalid_input`, `details.field` = `player_id` ou `payload.<chave>`).
5. Atualiza o cache da partida: status, period, period_started_at (created_at do evento
   que abriu o período, `state.period_started_seq`, **somado ao tempo parado** nas
   suspensões fechadas de `state.period_pauses`), placar, pênaltis (null sem disputa),
   finished_at (`created_at` do `match_end` visível; limpa se o fim for cancelado),
   kickoff_at (reagendamento), `version += 1` (uma vez por escrita).
6. Partida de confronto: `compute_tie_result` e grava `winner_team`/`decided_by` no `Tie`
   (limpa — `None`/`""` — quando deixa de estar completo). Se o resultado do confronto
   mudou, os outros jogos dele ganham `version += 1` e uma mensagem `match` cada.
7. Partida de grupo: `standings.services.recompute_group(group)` (oficial e ao vivo) quando
   mudou status, placar ou cartão.
8. Outbox (nesta ordem): sempre `match`; `standings` quando a partida é de grupo e a
   classificação foi recalculada (status, placar ou cartão mudaram — item 7; substituição,
   acréscimo, VAR, intervalo etc. não reenviam a mesma tabela); `goals` quando o conjunto de
   gols válidos da partida mudou (entrada, saída ou volta).
9. Auditoria: `observability.audit.record("event.create" | "event.void" | "match.status", ...)`
   com `match_id` e `data` (`type`, `period`, `minute`, `stoppage`, `team_id`, `key`, `source`,
   `derived`, `warnings`; status: `action`, `kickoff_at`, `reason`; void: `voided_ids`, `reason`).
   `on_match_edited` grava `"match.edit"` quando recebe `user`.
10. Métricas (depois do commit): `fdr_events_posted_total{type,source}` (um por linha gravada,
    derivados inclusive), `fdr_events_voided_total` (+ número de ids), `fdr_status_changes_total{action}`,
    `fdr_domain_rejections_total{code}` (quando o `DomainError` sobe).

Erros fora do domínio: partida inexistente → `Match.DoesNotExist` (404); chave de
idempotência vazia, com mais de `services.IDEMPOTENCY_KEY_MAX_LENGTH` (64) caracteres ou com o
trecho reservado `services.DERIVED_KEY_MARK` (`":auto:"`, dos derivados) ou
`source` fora de `MatchEvent.Source` → `services.InvalidInput(field, message)` (400
`invalid_input`); `user` ausente → `ValueError` (erro de programação).

Decisões do serviço:
* **Cancelar o que já está cancelado** não é erro: `VoidOutcome(already=True)` com os ids daquele
  cancelamento (mesmo `voided_at`/autor), sem gravar nem publicar nada (clique duplo inofensivo).
  O motivo (`reason`) vai só para a auditoria.
* **Confronto de ida e volta travado** (`tie_leg_locked`): com o jogo decisivo já começado
  (status fora de agendado/adiado/cancelado), o outro jogo não aceita cancelamento nem
  lançamento que mude o placar dele (o domínio refaz a volta com o placar atual da ida).
* **Reagendamento**: o payload gravado fica `{"kickoff_at": "<UTC Z>", "previous_kickoff_at":
  "<UTC Z>", "reason"?}` (sem fuso = Brasília). Cancelar reagendamento volta ao último
  reagendamento que sobrou ou, sem nenhum, ao `previous_kickoff_at` do mais antigo cancelado.
  O serviço só grava `kickoff_at` quando ele muda (não sobrescreve edição do admin).
  Data que não cabe em UTC (ex.: `9999-12-31T23:59-03:00`) → `422 invalid_payload` (`details.field` =
  `kickoff_at` no `/status`, `payload.kickoff_at` no `/events`).
* **Mensagem `goals`** — `changes[]`: gol criado nesta escrita → `added`/`null`; gol que saiu por
  anulação → `removed`/`annulled`; gol cancelado → `removed`/`voided`; gol que volta (anulação
  cancelada) → `restored`/`unvoided`. Em `removed`, `goal` é o gol como era (com o `score_after`
  daquele momento). `date`/`latest_goals` = hoje em Brasília (a lista de `GET /api/home` sem
  `date`); a mudança pode ser de jogo fora do dia (correção antiga): o front só alerta para
  gols de jogos que estão na home.
* **Admin** (`on_match_edited`): `changed_fields` = nomes (`form.changed_data`) ou `{campo: valor
  antigo}`. Refaz o cache pelos eventos (o formulário grava os campos de cache com o valor de
  quando a página abriu), `version += 1`, recalcula grupo(s) se mudou `stage/group/home_team/
  away_team` (com o valor antigo de `group`, também o grupo antigo; só com nomes, todos os grupos
  da fase atual), recalcula confronto(s) se mudou `stage/tie/leg/times`, publica `match` (+
  `standings` das fases recalculadas). Prefira salvar a partida com `update_fields` só dos campos
  editáveis. Se o confronto recalculado mudou de resultado, os
  outros jogos dele (e os do confronto antigo) ganham `version += 1` e uma mensagem `match` cada,
  como no item 6; a mensagem `match` da própria partida já sai com o confronto novo.
  `kickoff_at` editado que põe ou tira de hoje (regra de `selectors.day_matches_query`, espelhada
  em `selectors.on_day`) uma partida com gols válidos → também `goals` com `changes: []` e
  `latest_goals` refeito (sem o valor antigo — só os nomes —, publica sempre que há gols).

`DomainError` sobe para a API, que responde 422. Nada é gravado quando há erro.

Estrutura garantida pelo banco (não só pelo `clean()` do admin; seed e shell também passam por
ela), violação → `IntegrityError`: `uniq_match_tie_leg` (um jogo por confronto e ida/volta);
gatilhos de `matches/migrations/0002_structure_constraints.py` — grupo, rodada e confronto da
mesma fase da partida, fase de mata-mata ⇔ partida com confronto, fase com tabela ⇒ partida com
grupo, `leg` ≤ `ties.legs`; confronto só em mata-mata e com a rodada da fase dele; com jogos, o
confronto não muda de fase nem fica com menos jogos; grupo/rodada com partidas não mudam de
fase; formato da fase não muda deixando partidas ou confrontos inválidos — e `zone_no_overlap`
(`standing_zones`: faixas da mesma fase não se sobrepõem; exclusão GiST com `btree_gist`,
**DEFERRED**: conferida no commit, para o inline de zonas regravar várias faixas de uma vez).
No admin, o POST da partida, do confronto, da rodada (jogos e confrontos) e da fase (regras e
punições) roda inteiro com a trava de escrita (`competitions.admin.WriteLockedPostMixin`): o segundo
"Salvar" simultâneo vê o primeiro e recebe o
erro do formulário.

### Domínio da partida (matches/domain.py)

* Fluxo do serviço: `state = derive_state(events, ctx)` → `apply_event(state, events, new, ctx, confirm=...)`
  (ou `status_action_event(action, kickoff_at=, reason=)` → `apply_event`). Grave `result.event` e
  `result.derived` como vierem: período, minuto padrão dos eventos estruturais (início 0, intervalo 45,
  2T 45, prorrogação 90, pênaltis 90/120, fim 90/120), time herdado no gol anulado e payload já
  normalizado (só as chaves do tipo; com escalação, o nome canônico da escalação). Os campos
  `player_id` do domínio (`Event`, `NewEvent`, `LineupPlayer`) continuam opcionais no código puro,
  mas ninguém passa id: `context.lineups_for` monta `LineupPlayer(name, starter, number)` com
  `player_id=None` e o serviço recusa id no lançamento.
* Cancelamento: `check_void(events, event_id, ctx)` → `VoidResult(state, voided_ids)`; marque como
  cancelados **todos** os `voided_ids` (o pedido vem primeiro). Caem junto: derivados
  (`payload["derived_from_sequence"] == sequence` da origem), anulações que apontam para o gol e o
  vermelho automático cujo amarelo deixou de ser o 2º.
* Correção de lance: `check_edit(events, edited, ctx)` → `VoidResult(state, voided_ids)` com a mesma
  regra de cartões do cancelamento: o vermelho automático cujo amarelo deixou de ser o 2º cai junto
  (`voided_ids`, cancelados na mesma transação da correção e devolvidos em `PostResult.voided_ids`) e
  um amarelo que passaria a ser o 2º sem o vermelho automático → `event_not_editable` com
  `details.cause = "second_yellow_without_red"`.
* Nos dois, só conta o que muda com o cancelamento/correção (comparado por sequence com os eventos de
  antes): cartões que a correção antiga já deixou inconsistentes (vermelho automático cujo amarelo não
  é mais o 2º, 2º amarelo sem vermelho) ficam como estão — não caem nem recusam um lance sem relação.
* Vermelho automático (2º amarelo): payload `{"player", "reason": "second_yellow", "derived_from_sequence": <sequence do amarelo>}`.
  `EventOut.derived` = `domain.derived_from(event) is not None`. Ícone com variações: `domain.event_icon(type, payload)`.
* Códigos 422 além dos listados em `apply_event`: `confirmation_required` (com `warnings`),
  `unknown_event_type`, `period_mismatch` (replay), `event_not_found`, `already_voided`,
  `void_derived_event` (vermelho automático só cai com o amarelo), `void_breaks_sequence` (`details.cause` = regra violada no replay,
  ou `second_yellow_without_red` quando um amarelo passaria a ser o 2º do jogador sem o vermelho automático).
* Relógio com suspensão: `state.period_pauses` = `((seq do suspended, seq do resumed | None), ...)` do período
  corrente (zera a cada período). Ao vivo: `period_started_at` = abertura + soma de `created_at(resumed) -
  created_at(suspended)`. Suspenso (`running=false`): o minuto fica parado no `created_at` da suspensão aberta.
* Períodos: `first_half`, `half_time`, `second_half`, `extra_time` (1º tempo da prorrogação, 90–105),
  `extra_half_time` (intervalo da prorrogação), `extra_second_half` (2º tempo da prorrogação, 105–120),
  `penalties`. A prorrogação segue `extra_time_start` → `extra_half_time` → `extra_second_half_start`;
  pênaltis e fim de jogo vêm depois do 2º tempo dela.
* Minuto no formulário do operador: `EventSpec.minute == "required"` vale com o relógio correndo (1T, 2T e
  os dois tempos da prorrogação); nos intervalos e nos pênaltis é opcional. `domain.minute_mode(type, period)` devolve o que vale
  agora. Nos pênaltis, o minuto (se vier) é o do início da disputa: 120 com prorrogação, 90 sem.
* Reagendamento: `kickoff_at` em ISO 8601 com data **e** hora (só a data é recusada: `invalid_payload`).
  Sem fuso = horário de Brasília: o serviço aplica `settings.TIME_ZONE` antes de gravar `Match.kickoff_at`.
* Rótulos prontos para a API/catálogo: `STATUS_ACTION_LABELS`, `GOAL_ORIGIN_LABELS`, `PENALTY_MISS_LABELS`
  (`payload.outcome` opcional do pênalti perdido). Gols anulados: `domain.annulled_goal_ids(events)`.

Outros pontos de escrita que passam pelo mesmo núcleo:
* Django Admin: salvar fase (pontuação/critérios/punições) → `standings.services.on_stage_rules_changed(stage)`
  (valida — `ConfigError` —, recalcula e publica, sob `locked_atomic()`); salvar zona →
  `on_stage_rules_changed(stage, recalc=False)` (só publica `standings`); mata-mata: nada a fazer.
  Salvar partida (página da partida ou lista de jogos da página da rodada) →
  `matches.services.on_match_edited`; confronto alterado (página do confronto ou da rodada) →
  `on_match_edited(jogo, ["tie"])` em cada jogo; escalação/arbitragem/estatística →
  `matches.services.publish_match(match)`. Times de grupo (`GroupTeam`) →
  `standings.services.recompute_group(group)` (a leitura também calcula na hora quando o cache não
  tem os times do grupo ou tem outros ajustes de pontos). Lances não são editáveis no admin: ficam
  somente leitura dentro da partida (os cancelados não aparecem), com a caixa "Cancelar lançamento"
  por linha, que chama `void_event` (exige `matches.void_event`; erro do domínio vira mensagem).
  Lançar lance novo é só na tela do operador (`/operator.html?date=&match=`, link na partida).
  Partida com lançamentos não se apaga. A classificação (`Standing`) não tem página no admin.
* Punição/bonificação em pontos: `standings.PointAdjustment(stage, team, points ≠ 0, reason,
  created_at)` (CHECK `point_adjustment_not_zero`; o time precisa estar num grupo da fase — `clean()`
  e a lista de escolha do inline). Entra em `domain.compute_standings(..., adjustments={team_id:
  pontos})`: `points` = pontos dos jogos + ajuste (`Row.adjustment`, coluna `standings.adjustment`);
  o critério "points" e a ordem usam os pontos ajustados; o confronto direto continua só com os
  resultados. Vale nas duas visões (oficial e ao vivo).
* `seed`: usa `post_event`/`change_status` (source="script", `at=` para datar os lances); elenco de
  nomes gerado em memória (`simulate_match.roster`), sem tabela de jogadores. `seed --clear` apaga o
  que o seed criou (com a trava de escrita), inclusive as mensagens do outbox das partidas (`match`) e
  fases (`standings`) apagadas e as `goals` que citam essas partidas; usuários e auditoria ficam.
* `already_voided` não sai de `void_event` (o serviço devolve `VoidOutcome(already=True)`); só de
  `domain.check_void` chamado direto.
* Fase com critérios gravados inválidos (vazios, repetidos ou fora do catálogo) não derruba o
  lançamento: `standings.services.stage_rules` usa os critérios padrão (`Rules().criteria`) e loga
  um aviso; a leitura mostra os critérios efetivos.

### Navegação do Django Admin

* Índice só com os pontos de entrada: Competições e Times (+ usuários, perfis, chaves da API pública e
  auditoria para o Administrador). Temporada, fase, grupo, rodada, partida, confronto, escalação e
  outbox usam `competitions.admin.HiddenFromIndexMixin` (`get_model_perms` vazio fora de
  `/admin/<app>/`): somem do índice e da barra lateral, mas endereços e permissões continuam.
* Hierarquia: Competição (temporadas + painel das fases com "+ adicionar fase" → `stage/add/?season=`)
  › Temporada (fases, link "abrir") › Fase (critérios, zonas, punições, grupos com "Times do grupo (N)",
  rodadas com "Jogos da rodada (N)" ou, no mata-mata, "Confrontos e jogos") › Rodada (jogos; no
  mata-mata também os confrontos; grupo só entre os da fase, confronto só entre os da rodada; status e
  placar somente leitura) › Jogo (estrutura, cache somente leitura, lances, arbitragem, transmissões,
  estatísticas, links das escalações) › Escalação (esquema, técnico, nomes).
* Trilha (`templates/admin/fdr/change_form.html`, `HierarchyAdminMixin.breadcrumbs`): Início ›
  Competição › Temporada N › Fase › Rodada › Jogo (› Escalação); na inclusão, o pai vem do parâmetro
  GET (`?season=`, `?stage=`, `?round=`, `?match=`). "Salvar" volta para o nível de cima; cancelar
  lances fica na própria partida.

## 3. Leitura e serialização (matches/selectors.py, standings/services.py)

Toda resposta com horário traz `server_time` (ISO UTC com `Z`) e `timezone` (`"America/Sao_Paulo"`).
Respostas da home e da competição trazem `cursor` (maior id do outbox), **lido antes do estado**.

Micro-cache (`api/read.py`, cache `reads`): `GET /api/home` e `GET /api/competitions/{slug}` ficam
guardados por `settings.READ_CACHE_SECONDS` (variável `READ_CACHE_SECONDS`, padrão 5 s; `0` desliga),
chave = rota + parâmetros (dia de Brasília calculado a cada pedido; `slug`, `stage`, `round`) +
cursor do outbox. Toda escrita que publica mensagem muda o cursor e invalida na hora; o payload
guardado vale o mesmo que uma leitura nova naquele cursor (o stream entrega o resto). Acerto = 1
consulta (o cursor). `server_time`/`timezone` são sempre os da requisição. Erros (404) não entram.
Edição no admin que **não** publica mensagem (nome de time ou competição, escudo, cores) aparece
em até `READ_CACHE_SECONDS`.

### TeamOut
```json
{"id": 1, "name": "Sport Club do Recife", "short_name": "SPT", "city": "Recife",
 "color_primary": "#D7141A", "color_secondary": "#000000", "crest_url": ""}
```

### EventOut
```json
{"id": 91, "sequence": 7, "type": "goal", "type_label": "Gol", "kind": "game", "icon": "ball-penalty",
 "period": "second_half", "period_label": "2º tempo", "period_short": "2T",
 "minute": 72, "stoppage": null, "minute_label": "72'",
 "team_id": 1, "team_side": "home", "player": {"id": null, "name": "Zé Roberto"},
 "payload": {"player": "Zé Roberto", "origin": "penalty"},
 "annuls_event_id": null, "annulled": false, "derived": false,
 "score_after": {"home": 2, "away": 1},
 "created_at": "2026-10-03T21:12:09Z"}
```
`annulled` = gol anulado por anulação válida. `score_after` só em gols válidos (null nos outros).
`icon` = `domain.event_icon(type, payload)`. `player.name` = `payload.player` (null na substituição:
use `payload.player_out`/`player_in`). `player.id` é sempre `null` (jogador não tem cadastro; o
campo fica para o formato não mudar). Eventos de status também entram na linha do tempo (`kind: "status"`).
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
Sem vencedor: `winner_team_id`, `decided_by` e `decided_by_label` são `null` e `complete` é `false`.
`aggregate` soma o placar atual de todos os jogos (em andamento inclusive; cancelado não conta).

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
 "clock": {"running": true, "offset": 45, "regular_end": 90, "stoppage_announced": 4, "paused_at": null},
 "home": TeamOut, "away": TeamOut,
 "home_score": 2, "away_score": 1, "home_penalties": null, "away_penalties": null,
 "winner": null,
 "version": 17,
 "tie": TieOut | null,
 "goals": [GoalOut],
 "cards": {"home": {"yellow": 1, "red": 0}, "away": {"yellow": 2, "red": 1}},
 "red_cards": [{"team_side": "away", "player": "Fulano", "minute_label": "63'"}]}
```
* `clock` é null fora de 1T/2T/prorrogação em andamento. `running=false` em suspenso, com
  `paused_at` = `created_at` da suspensão aberta (o minuto fica parado ali: `offset` + minutos
  inteiros entre `period_started_at` e `paused_at` + 1); ao vivo, `paused_at` é null.
  `stoppage_announced` = minutos do último `stoppage_time` visível do período corrente (ou null).
* `winner`: "home" | "away" | "draw" | null (só com status finished; considera pênaltis).
* Detalhe (`GET /api/matches/:id`) e mensagem `match` do stream acrescentam:
  `"events": [EventOut]`, `"lineups": {"home": LineupOut|null, "away": LineupOut|null}`,
  `"officials": [{"role","role_label","name","state"}]`,
  `"broadcasts": [{"name","url","kind","kind_label"}]`,
  `"stats": [{"key","label","home","away"}]`, `"attendance": 42318 | null`, `"revenue_cents": 234155000 | null`.
* LineupOut: `{"formation": "4-3-3", "coach": "Fulano", "starters": [{"name","number","position"}], "substitutes": [...]}`
  (`formation`, `coach`, `number`, `position` podem ser null; `name` sempre vem — a escalação é só de nomes). `stats`: uma linha por chave, na ordem
  de `MatchStat.Key`; lado sem valor = null.
* Funções de leitura (`matches/selectors.py`, todas devolvem dict pronto): `serialize_team`,
  `serialize_event(event, match, timeline=)`, `serialize_match(match, detail=False, events=None, tie_legs=None)`,
  `serialize_matches(qs_ou_lista, detail=False)` (uma consulta de eventos para todas),
  `home_payload(day=None, now=None)`, `latest_goals(day, now, limit=10)`, `competitions_menu()`,
  `competition_payload(slug, stage_id=None, round_id=None)`, `matches_list(round_id, date, status, stage_id, *, limit=500, offset=0)`,
  `match_detail(match_id)`, `match_state(match)` → `{"match", "available"}`, `catalog_payload()` e os
  corpos das respostas do operador: `post_payload(PostResult)`, `status_payload(PostResult)`,
  `void_payload(VoidOutcome)`. Inexistente → `Model.DoesNotExist` (404); filtro inválido → `ValueError` (400).
  Classificação: `standings.services.stage_standings(stage, live=True)` / `stages_standings(stages, live=True)`.

### StageStandingsOut
```json
{"stage_id": 3, "stage_name": "1ª fase", "kind": "live",
 "points": {"win": 3, "draw": 1, "loss": 0},
 "criteria": [{"key": "points", "label": "Pontos"}, ...],
 "legend": [{"name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 4}],
 "groups": [{"id": 4, "name": "Tabela", "rows": [
   {"position": 1, "team": TeamOut, "played": 5, "won": 4, "drawn": 1, "lost": 0,
    "goals_for": 11, "goals_against": 3, "goal_difference": 8, "points": 13, "points_adjustment": 0,
    "yellow_cards": 6, "red_cards": 0, "tied": false,
    "zone": {"name": "Classificados", "color": "#1B7F3B"} | null,
    "playing": true}]}],
 "adjustments": [{"team": TeamOut, "points": -3, "reason": "escalação irregular"}]}
```
`playing` = o time está em jogo ao vivo agora (destaque visual). `points` já inclui
`points_adjustment` (soma das punições, negativas, e bonificações, positivas, do time na fase; 0
sem ajuste). `adjustments` = cada punição/bonificação da fase, na ordem de cadastro (o front marca
os pontos ajustados com "*" e lista `"Santa Cruz: −3 pts — escalação irregular"` sob a legenda).
A API pública (`/public/v1/stages/{id}/standings`) expõe os mesmos dois campos.

## 4. Rotas

Erros: `{"code": "...", "message": "...", "details": {...}}` (+ `"warnings": [{"code","message"}]` em `confirmation_required`).

Limites de acesso (com `Retry-After`): `429 login_locked` (login bloqueado por força bruta; `details.retry_after`), `429 rate_limited` (requisições por IP em `/api/`), `429 too_many_streams` e `503 stream_capacity` (conexões do stream).
`400 invalid_input` (formato), `401 not_authenticated`, `403 permission_denied`, `403 csrf_failed`, `404 not_found`, `422 <regra>`.
API pública (`/public/v1/`, `public_api/urls.py`): caminho inexistente (inclusive a raiz) → `404
not_found` em JSON (`details.path`, `details.docs`), sem exigir chave; método não aceito → `405
method_not_allowed` (`details.allowed`, header `Allow`).

Id de requisição: o header `X-Request-ID` recebido só é aceito com 1 a 64 caracteres de
`[A-Za-z0-9._:-]` (cabe em `AuditLog.request_id`); fora disso o servidor gera um (uuid4 hex). O id
usado volta no `X-Request-ID` da resposta e vai nos logs e na auditoria.

| Rota | Resposta |
| --- | --- |
| `POST /api/auth/login` `{"username","password"}` | `200 {"user": MeUser, "csrf_token": "..."}` · `401 invalid_credentials` · protegido por CSRF |
| `POST /api/auth/logout` | `200 {"ok": true}` |
| `GET /api/auth/me` | `200 {"authenticated": bool, "user": MeUser|null, "csrf_token": "...", "server_time": "...Z"}` (sempre seta o cookie CSRF; `server_time` acerta o relógio do operador já no login) |
| `POST /api/ops/matches/{id}/events` + `Idempotency-Key` | `201 {"event": EventOut, "derived": [EventOut], "match": MatchOut(detalhe), "available": Available, "warnings": [...], "replayed": false}` · replay → `200` com `"replayed": true` |
| `POST /api/ops/matches/{id}/events/{eventId}/edit` (`matches.void_event` também) `EventIn` (mesmo `type` do lance) | `200 {"event": EventOut, "derived": [], "match": MatchOut(detalhe), "available": Available, "warnings": [...], "replayed": false, "voided": [ids]}` (`voided`: os que caíram junto — o vermelho automático cujo amarelo deixou de ser o 2º; `[]` quando nada caiu) · `422 event_not_editable` |
| `POST /api/ops/matches/{id}/events/{eventId}/void` `{"reason"}` | `200 {"voided": [ids], "match": MatchOut(detalhe), "available": Available, "already": bool}` (`already`: já estava cancelado, nada mudou) |
| `POST /api/ops/matches/{id}/status` + `Idempotency-Key` `{"action","kickoff_at"?,"reason"?}` | `201 {"event": EventOut, "match": ..., "available": ..., "replayed": false}` · replay → `200` com `"replayed": true` |
| `POST /api/ops/matches/{id}/partial-info` (`matches.change_status`) `{"partial_info": bool}` | `200 {"match": MatchOut (detalhe), "available": ...}` · publica `match` só quando muda |
| `GET /api/ops/catalog` | `{"events": [EventSpecOut], "status_actions": [{"action","label"}], "periods": [{"key","label","short"}], "statuses": [{"key","label"}]}` (todos os tipos do catálogo, status inclusive) |
| `GET /api/home?date=YYYY-MM-DD` | HomeOut, `Cache-Control: no-store` (micro-cache no servidor, §3) |
| `GET /api/competitions` | `{"competitions": [{"id","name","slug","short_name","position"}]}` |
| `GET /api/competitions/{slug}?stage=&round=` | CompetitionOut (micro-cache no servidor, §3) |
| `GET /api/stages/{id}/standings?live=1` | StageStandingsOut + `server_time`, `timezone` |
| `GET /api/matches?roundId=&date=&status=&stageId=&limit=&offset=` | `{"server_time","timezone","matches": [MatchOut],"has_more": bool}` (`date` = dia de Brasília pelo `kickoff_at`; `status` aceita vários separados por vírgula; em ordem de início, com ou sem filtro, no máximo 500 por resposta: `limit` 1–500, padrão 500, `offset` 0–100000, padrão 0, fora disso `400`; `has_more` = há partidas depois desta página e a próxima, `offset` + `limit`, cabe no teto de `offset` — quem segue `has_more` nunca recebe `400`; além do teto, filtre por `date`/`roundId`) |
| `GET /api/matches/{id}` | `{"server_time","timezone","cursor","match": MatchOut(detalhe), "available": Available}` |
| `GET /api/stream?after=N` | SSE (seção 5) |

* MeUser: `{"id","username","name","roles": ["Operador"], "permissions": {"post_event": bool, "void_event": bool, "change_status": bool, "manage_users": bool, "admin_site": bool}}`.
* Available: `{"events": ["goal", ...], "status": ["suspend", ...]}` (de `domain.available_actions`).
* EventSpecOut: `{"type","label","kind","icon","minute": "required|optional|none","periods": [...], "fields": [{"name","kind","label","required","choices": [[v,l]]}]}`.
* Corpo do lançamento: `{"type","minute"?,"stoppage"?,"team_id"?,"payload"?: {},"annuls_event_id"?,"confirm"?: false,"source"?: "operator"}`.
  Jogador pelo nome no `payload`; `player_id` (ou `payload.player_id`/`player_out_id`/`player_in_id`) → `400 invalid_input`.
* HomeOut: `{"date","server_time","timezone","cursor","competitions": [{"id","name","slug","short_name","position","stages": [{"id","name","format","matches": [MatchOut],"standings": StageStandingsOut|null}]}],"latest_goals": [LatestGoalOut]}`.
  Só competições com jogo no dia, em `position`. Jogo da véspera que passa da meia-noite fica até 2h depois de `finished_at`.
  Regra exata (`selectors.day_matches_query`): começa no dia (Brasília), ou começou na véspera e
  (está ao vivo/suspenso e o dia pedido é hoje) ou (`finished_at` ≥ meia-noite do dia e `now` < `finished_at` + 2 h).
  Fases de cada competição em `position`; jogos por `kickoff_at`. `latest_goals`: os 10 gols válidos mais
  recentes (`created_at` desc) desses jogos — sem anulados, cancelados nem cobranças da disputa.
* CompetitionOut: `{"server_time","timezone","cursor","competition": {...},"season": {"id","year","end_year","label"},"stages": [{"id","name","format","position","rounds": [{"id","number","name"}]}],"current_stage_id","current_round_id","stage": {"id","name","format","standings": StageStandingsOut|null,"matches": [MatchOut da rodada],"ties": [TieDetailOut da rodada] (mata-mata; `[]` nas outras),"rankings": [RankingRef] (botão quando a página mostra esta fase)},"rankings": [RankingRef] (botão na página da competição)}`. RankingRef = `{"id","name","scope": "overall"|"custom"}`.
* `GET /api/rankings/{id}?live=1` → RankingStandingsOut: o formato de StageStandingsOut (um grupo só, `stage_id: null`, `stage_name` = nome da classificação) + `"ranking_id","scope": "overall"|"custom"|"position","stage_ids"` (fases que entram; a página busca de novo quando chega `standings` de uma delas, ou `match` de uma partida delas com status, placar, cartões ou times mudados, ou que entrou numa dessas fases ou saiu dela — o mata-mata não publica `standings`; mudança vista durante a busca é conferida quando a resposta chega) e `"group_position"` (N, só em "position"; cada linha traz `"group"`, o nome do grupo do time).
* Legenda da fase (StageStandingsOut.legend): zona condicional traz também `"condition"` (texto); a linha de quem está na faixa de posição mas fora da faixa da classificação vem com `"zone": null`.
  `current_stage_id`/`current_round_id` = fase/rodada exibidas (as pedidas ou as atuais); `season`/`stage` null sem temporada.
  Rodada de uma partida = a dela ou, no mata-mata sem rodada própria, a do confronto (`selectors.match_round`):
  vale para `MatchOut.round`, a rodada atual, `stage.matches` e o filtro `roundId` (também na API pública).
  Fase/rodada atuais = primeiras com jogo ainda não encerrado (senão a última).

### Decisões da API (`api/`)

* Montagem: `api/main.py` (NinjaAPI, `/api/docs` só para a conta de administrador, 404 para os outros, com o token CSRF no "Try it out" — recarregue a
  página depois do login), routers `api/auth.py`, `api/ops.py`, `api/read.py`; erros em
  `api/errors.py`; sessão/CSRF/permissão em `api/security.py`. As rotas devolvem os dicionários
  dos selectors como estão (os schemas de saída só documentam o 1º nível no OpenAPI).
* Mapa de erros: validação do Ninja, corpo ilegível, header ou filtro inválido e
  `services.InvalidInput` → `400 invalid_input` com `details.field` (validação do Ninja: também
  `details.errors = [{"field","message","type"}]`; `field` sem o prefixo de origem, ex.:
  `"minute"`, `"payload.player"`, `"roundId"`, `"Idempotency-Key"`); `DomainError` → `422` com
  `code`/`details`/`warnings` do domínio; `standings.domain.ConfigError` → `422` com o `code` dele;
  `Model.DoesNotExist` → `404 not_found`; rota inexistente sob `/api/` → `404 not_found` em JSON;
  método que a rota não aceita → `405 method_not_allowed` em JSON (`details.allowed`, header `Allow`).
  Corpo ilegível (JSON inválido) → `400 invalid_input` com `details.field = "body"`.
* Ordem das checagens em `/api/ops` (antes de validar o corpo): sem sessão → `401
  not_authenticated` (mesmo sem token CSRF); token CSRF ausente/vencido → `403 csrf_failed`
  (o front renova em `GET /api/auth/me`); sem a permissão da rota → `403 permission_denied`
  (`details.required_any`). Permissões: lançar `matches.post_event`, cancelar
  `matches.void_event`, status `matches.change_status`, catálogo qualquer uma das três. Tipo de
  status (`postponed`, `suspended`, `resumed`, `rescheduled`, `cancelled`) mandado por `/events`
  exige também `matches.change_status` → senão `403 permission_denied` (`required_any =
  ["matches.change_status"]`): `/events` não é atalho para quem só lança lances.
* Login: CSRF conferido explicitamente (rota anônima); `django.contrib.auth.login` troca a chave
  da sessão e o token CSRF — por isso a resposta traz o `csrf_token` novo (o cookie também muda).
  Senha errada, usuário inexistente e usuário inativo → o mesmo `401 invalid_credentials`.
  Logout sem sessão responde `{"ok": true}`. Auditoria pelos sinais de autenticação do Django
  (`observability.signals`), em qualquer porta — `/api/auth/*` e `/admin/login/`/`/admin/logout/`
  — uma linha por evento: `auth.login`, `auth.logout`, `auth.login_failed` (`data.username`, o
  usuário digitado; a senha nunca). `client.force_login` (testes) também grava `auth.login`.
  Troca da própria senha em `/admin/password_change/` → `auth.password_change`.
* `Idempotency-Key`: obrigatório em `/events` e `/status`, 1 a 64 caracteres
  (`services.IDEMPOTENCY_KEY_MAX_LENGTH`; o campo tem 80 para o sufixo `:auto:N`), sem o trecho
  reservado `:auto:` → senão `400 invalid_input` (`details.field = "Idempotency-Key"`). `source` aceita `operator`, `feed` e
  `script` (`system` é só dos derivados) → senão 400. Corpo do `/void` é opcional (`reason` até 280).
* `/void` de lançamento inexistente ou de outra partida → `404 not_found` (`details.event_id`,
  `details.match_id`), não `422 event_not_found`.
* `/api/stages/{id}/standings`: `live=1` (ou `true`/`yes`/`on`) → tabela ao vivo; sem `live`,
  vazio ou `0`/`false` → oficial; outro valor → 400. Fase de mata-mata → `404 not_found`.
* Datas (`date` da home e de `/api/matches`) só no formato `AAAA-MM-DD` → senão `400` (`details.field = "date"`).
* `Cache-Control`: `no-store` em toda resposta de dado ao vivo, de sessão e de operação (home,
  competição, classificação, partidas, `/api/auth/*`, `/api/ops/*`); `public, max-age=60` no menu
  (`GET /api/competitions`); `private, max-age=300` no catálogo (`GET /api/ops/catalog`).

## 5. Stream SSE

* `GET /api/stream?after=N` — um stream só por página, todas as competições.
* Retomada: header `Last-Event-ID` (reconexão automática) vale mais que `after`.
* Quadros: `id: <outbox.id>\nevent: <topic>\ndata: <json>\n\n`. Abre com `retry: 3000` e um `ping`.
* `ping` (sem id, fora do outbox) ao abrir e a cada 20 s: `{"server_time": "...Z"}`.
* Mensagens (estado completo, o front substitui sem merge):
  * `match`: `{"stage_id", "competition_id", "match": MatchOut(detalhe)}`
  * `standings`: `{"stage_id", "standings": StageStandingsOut(live)}`
  * `goals`: `{"date": "YYYY-MM-DD", "changes": [{"kind": "added"|"removed"|"restored", "reason": "annulled"|"voided"|"unvoided"|null, "goal": LatestGoalOut}], "latest_goals": [LatestGoalOut]}`
    (pares: `added`/null, `removed`/`annulled`, `removed`/`voided`, `restored`/`unvoided` — ver §2;
    `changes` vazio = só a lista mudou, ex.: início editado no admin).
  * Uma escrita gera, nesta ordem: `match` (+ `match` dos outros jogos do confronto quando o
    resultado dele muda), `standings` (partida de grupo com a classificação recalculada:
    status, placar ou cartão mudaram), `goals` (gols válidos mudaram; ou, no admin, o início
    editado pôs ou tirou de hoje uma partida com gols — `changes: []`).
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
