# Sistema de Jogos — plano de implementação

Oct 3, 2026 · @Casalzão

## Resumo e premissas

O desenho já separa bem domínio, partida, eventos e status; o plano mantém essa estrutura e corrige dois pontos de risco.

1. O caminho de escrita grava no banco antes de avisar o front.
2. Placar e classificação viram projeções calculadas a partir dos eventos.

O back vem inteiro antes do front: fases 0 a 6, com usuários e permissões, partidas, eventos, classificação, mata-mata e tempo real. O front, nas fases 7 e 8, é HTML e JS puro; o CSS fica para uma etapa à parte. A API pública fica fora do MVP.

O plano parte destas decisões e premissas:

- Back em Python com Django e PostgreSQL.
- Um único serviço (monólito modular) e um único banco.
- Eventos lançados por operador logado, com permissão para isso.
- Tempo real por Server-Sent Events (SSE), sem biblioteca no navegador.
- O “dia”, o relógio e os horários dos jogos seguem o horário de Brasília, configurado no back.
- Últimos gols e alertas de gol valem para todos os jogos do dia; os alertas só funcionam com a home aberta.
- No mata-mata, operador ou administrador cadastra os confrontos da rodada seguinte; o sistema só apura quem avançou.

## Stack

A stack é Python com Django e PostgreSQL. Os dois requisitos novos, usuários com permissões e configuração no back, já vêm prontos no Django; em Node ou FastAPI, seriam a maior parte do trabalho inicial.

| Critério | Django (Python) | Fastify (Node.js) | FastAPI (Python) |
| --- | --- | --- | --- |
| Usuários, grupos e permissões | Prontos no framework | Montados com bibliotecas | Montados com bibliotecas |
| Telas de cadastro e de configuração | Django Admin, pronto | Escritas à mão | Escritas à mão ou com pacote de terceiros |
| Testar o back antes de existir front | Django Admin e documentação interativa da API | Documentação interativa, via plugin | Documentação interativa, pronta |
| Transações e travas no PostgreSQL | ORM e `transaction.atomic`; a trava é uma chamada SQL | SQL direto ou query builder | SQLAlchemy |
| Tempo real por SSE | Funciona em modo assíncrono; é a parte menos natural | Natural | Natural |
| Mesma linguagem do front | Não | Sim | Não |

- O custo do Django é ter duas linguagens e um stream SSE menos natural. Pesa pouco aqui: o front não tem regra de negócio, e o stream é uma rota só.
- A API usa Django Ninja: validação por tipos e documentação interativa em `/api/docs`. Django REST Framework é a alternativa mais tradicional.
- O stream SSE é uma view assíncrona do próprio Django, servida por um servidor ASGI como o uvicorn.
- O usuário é um modelo próprio, criado na fase 0; trocar isso depois de ter dados é trabalhoso.
- As regras continuam em funções puras, sem importar nada do Django; o ORM só carrega e grava.
- Os testes usam pytest e pytest-django.

## Melhorias de arquitetura

A mudança mais importante é a ordem do fluxo 6: gravar primeiro, avisar depois.

1. **Gravar antes de publicar.** No fluxo 6, o front é atualizado antes do banco e há dois caminhos de gravação. Se a gravação falhar, o front mostra um gol que não existe. Proposta: uma transação grava evento, partida e classificação; a mensagem sai só depois do commit.
2. **Outbox para a mensagem.** A mesma transação grava a mensagem em uma tabela `outbox`; um publicador lê e envia ao front. Nada se perde se o processo cair entre gravar e publicar. A tabela também reenvia mensagens a quem reconecta.
3. **Duas portas sobre o mesmo núcleo.** Operação (escrita, autenticada) e leitura (home e página da competição). As duas usam as mesmas regras e o mesmo banco. A API pública fica fora do MVP e volta depois como terceira porta, só com serializadores novos.
4. **Front sem regra de negócio.** Ordem, critérios, zonas e cores da classificação chegam prontos do back. As páginas só desenham; mudar uma regra não exige mexer no front.
5. **Origem dos eventos explícita.** O desenho começa em “Back-end → Add evento” sem dizer quem lança. O endpoint exige operador logado e com permissão, e recebe uma chave de idempotência e um campo de origem. Cada lançamento guarda quem o fez. Um clique duplo não vira dois gols, e um feed de provedor pode entrar depois pelo mesmo caminho.
6. **Tempo real por SSE.** O fluxo é só do servidor para o navegador, que é o caso do SSE. O `EventSource` reconecta sozinho e informa a última mensagem recebida. Não exige biblioteca, o que combina com o front em JS puro.
7. **Validação em duas camadas.** A camada HTTP valida formato e autenticação. As regras de negócio ficam em funções puras do domínio, chamadas por qualquer entrada: tela do operador, Django Admin, script ou teste.
8. **Uma trava de escrita.** Toda transação que grava no `outbox` pega antes uma trava global do Postgres (`pg_advisory_xact_lock`) e a segura até o commit. Os lançamentos entram em fila: sequência, placar, classificação e últimos gols ficam coerentes, e a ordem dos ids do `outbox` é a dos commits. Nesse volume, a fila não pesa.
9. **Monólito modular.** Módulos por pasta (usuários, competições, partidas, eventos, classificação e tempo real) em um único processo. Microsserviços e filas não se pagam nesse tamanho; o outbox já deixa a porta aberta.

## Melhorias de domínio e dados

O modelo fica mais simples quando os eventos são fatos imutáveis e todo o resto é derivado deles.

1. **Placar derivado.** O placar é a contagem dos gols válidos, recalculada a cada lançamento; ninguém edita o placar direto. As colunas de placar na partida são um cache gravado na mesma transação.
2. **Gol anulado não é lançamento errado.** Gol anulado é um fato do jogo: fica na linha do tempo e aponta para o gol original, quando ele chegou a ser lançado. Erro do operador é correção de dado: o evento é marcado como cancelado, some das linhas do tempo e fica só na auditoria. Nenhum evento é apagado ou editado.
3. **Status e período como dois campos.** O desenho já separa os dois; falta fixar as transições válidas em uma máquina de estados. O período muda como consequência de eventos, nunca por edição manual.
4. **Eventos que faltam ou se sobrepõem.** Faltam início de jogo, fim de jogo, início da prorrogação, início da disputa de pênaltis e cada cobrança. “Fim do 1T” e “Intervalo” são o mesmo momento: um evento só, que coloca o período em intervalo. “Pênalti” precisa virar pênalti marcado, gol de pênalti e pênalti perdido.
5. **Tempo do evento em três partes.** Período, minuto e acréscimo (45+2), mais um número de sequência por partida. Um minuto sozinho não ordena dois eventos do mesmo minuto nem distingue 45+2 de 47.
6. **Classificação recalculada, não incrementada.** Somar três pontos a cada vitória quebra com gol anulado e correção. Recalcular o grupo inteiro a partir das partidas é barato e sempre dá o mesmo resultado. A mesma função gera a tabela oficial (só encerrados) e a tabela ao vivo.
7. **Critérios e legenda configuráveis.** Pontuação, ordem dos critérios de desempate e zonas da legenda (nome, cor e faixa de posições) variam por competição. Ficam como dados da fase, editáveis no back; o código só conhece o catálogo de critérios.
8. **Times e jogadores.** “Times” está no título da seção 2, mas não no desenho. Entram `teams`, a participação do time no grupo e, mais tarde, `players` para escalação e autoria dos eventos.
9. **Rodada pertence à fase, não ao grupo.** A rodada 1 de uma fase de grupos vale para todos os grupos. Grupo e rodada viram duas referências independentes da partida.
10. **Formato na fase.** Pontos corridos, grupos ou mata-mata. Fase com tabela sempre tem ao menos um grupo: pontos corridos ganha um grupo único automático, e a classificação tem um caminho de código só. Mata-mata não tem grupo nem tabela; tem confronto, de jogo único ou de ida e volta, com agregado.
11. **Partida sem redundância.** Competição e temporada saem da partida: ela aponta para fase, grupo e rodada, e o resto vem por junção. Faltam no modelo data e hora (em UTC), estádio e placar dos pênaltis.
12. **Núcleo e enriquecimento.** Transmissão, árbitros, público, renda, escalação e estatísticas vão para tabelas próprias e para uma fase posterior. Renda fica em centavos, como inteiro. Estatísticas derivadas de eventos (gols, cartões) não se misturam com as digitadas (posse, finalizações).

## Arquitetura alvo

Um único processo Django expõe duas portas de API, o Django Admin e um stream; toda escrita passa pelo mesmo núcleo de domínio.

&#91;embedded content: arquitetura alvo · 3 telas, 1 processo, 1 banco\]

A escrita entra pela API de operação ou pelo Django Admin e passa pelo domínio. A API de leitura e o stream só entregam às páginas o que já foi gravado.

### Caminho de escrita de um evento

1. A tela do operador envia `POST /api/ops/matches/:id/events` com a chave de idempotência.
2. A camada HTTP valida sessão, permissão e formato.
3. A transação abre e pega a trava de escrita.
4. `apply_event` valida as regras e devolve o novo estado, ou rejeita com o motivo.
5. A transação grava evento, partida, classificação do grupo ou confronto, e mensagens no `outbox`.
6. Depois do commit, o publicador envia as mensagens aos navegadores conectados.

### Organização do código

```
config/         settings, urls, asgi
accounts/       usuário próprio, grupos e permissões
competitions/   competições, temporadas, fases, grupos, rodadas, times
matches/        partidas, eventos, confrontos; regras puras em domain.py
standings/      critérios, zonas, classificação; regras puras em domain.py
realtime/       outbox, publicador e stream SSE
api/            rotas de autenticação, operação e leitura
static/         index.html, competition.html, operator.html, js/, sounds/
tests/          testes do domínio e das rotas
```

## Modelo de dados

Quinze tabelas formam o núcleo, contando a de usuários; grupos e permissões vêm prontos do Django. O enriquecimento entra depois, em tabelas próprias.

| Tabela | O que guarda | Campos principais |
| --- | --- | --- |
| `users` | Usuário do back; grupos e permissões vêm do Django | `username`, `email`, `password`, `is_active` |
| `competitions` | Competição | `name`, `slug`, `position` |
| `seasons` | Temporada de uma competição | `competition_id`, `year` |
| `stages` | Fase da temporada, com sua pontuação | `season_id`, `name`, `position`, `format`, `points_win`, `points_draw`, `points_loss` |
| `stage_criteria` | Critério de desempate da fase, em ordem | `stage_id`, `position`, `key` |
| `standing_zones` | Zona da legenda da classificação | `stage_id`, `name`, `color`, `position_from`, `position_to` |
| `groups` | Grupo da fase (único em pontos corridos) | `stage_id`, `name` |
| `rounds` | Rodada da fase | `stage_id`, `number`, `name` |
| `teams` | Time | `name`, `short_name` |
| `group_teams` | Times de cada grupo | `group_id`, `team_id`, `lot_order` |
| `ties` | Confronto de mata-mata, de um ou dois jogos | `stage_id`, `round_id`, `position`, `legs`, `extra_time`, `team_a_id`, `team_b_id`, `winner_team_id`, `decided_by` |
| `matches` | Partida | `stage_id`, `group_id`, `round_id`, `tie_id`, `leg`, `home_team_id`, `away_team_id`, `kickoff_at`, `finished_at`, `venue`, `status`, `period`, `home_score`, `away_score`, `home_penalties`, `away_penalties`, `version` |
| `match_events` | Evento da partida, imutável | `match_id`, `sequence`, `type`, `period`, `minute`, `stoppage`, `team_id`, `player_id`, `payload`, `annuls_event_id`, `voided_at`, `voided_by`, `idempotency_key`, `source`, `created_by`, `created_at` |
| `standings` | Linha da classificação (cache) | `group_id`, `team_id`, `kind`, `position`, `played`, `won`, `drawn`, `lost`, `goals_for`, `goals_against`, `points`, `tied` |
| `outbox` | Mensagem a publicar | `topic`, `payload`, `created_at`, `published_at` |

Restrições que sustentam as regras:

- `match_events` tem `UNIQUE (match_id, sequence)` e `UNIQUE (match_id, idempotency_key)`.
- `match_events.created_at` guarda o horário do lançamento; ordena os últimos gols e data os alertas.
- `kickoff_at` e `finished_at` são `timestamptz`, sempre em UTC; `finished_at` é gravado na mesma transação do fim de jogo. O “dia” da home é calculado no horário de Brasília, o fuso configurado no back.
- `period` só é preenchido com a partida ao vivo ou suspensa.
- Grupo e rodada de uma partida precisam ser da mesma fase que ela.
- Partida de mata-mata aponta para um confronto e não tem grupo; partida de grupo não tem confronto.
- `ties.legs` vale 1 ou 2: jogo único ou ida e volta. O confronto não aceita mais partidas do que isso.
- `ties.extra_time` diz se o jogo decisivo tem prorrogação antes dos pênaltis; é obrigatório no cadastro.
- `ties.decided_by` vale `aggregate`, `extra_time` ou `penalties`.
- `standings.kind` separa a tabela oficial da tabela ao vivo; a tabela inteira pode ser apagada e reconstruída.
- `stage_criteria` não repete critério nem posição na mesma fase; cada chave precisa existir no catálogo do código.
- `standing_zones.color` só aceita o formato `#RRGGBB`; as faixas de posição de uma mesma fase não se sobrepõem.
- `group_teams.lot_order` guarda o resultado de um sorteio e fica vazio até ser necessário.
- `created_by` e `voided_by` apontam para `users`; nenhum lançamento fica sem autor.
- `player_id` fica vazio no MVP; até a fase 10, o nome do jogador vai em `payload`.

Depois do MVP entram `players`, `match_lineups`, `match_officials`, `match_broadcasts` e `match_stats`, além de público e renda como colunas de `matches`.

## Regras de domínio

As regras vivem em quatro funções puras, testáveis sem banco: `apply_event`, `visible_events`, `compute_standings` e `compute_tie_result`.

### Status e período

&#91;embedded content: máquina de estados · 6 status, 5 períodos\]

Início de jogo leva a ao vivo e fim de jogo leva a encerrado; adiar, suspender, retomar, reagendar e cancelar são ações do operador. Cancelado recebe de agendado, adiado e suspenso. Em suspenso, o período guarda onde o jogo parou. No jogo decisivo do mata-mata, com o confronto empatado, o 2T leva à prorrogação ou direto aos pênaltis, conforme o confronto.

### Catálogo de eventos

Cada tipo declara os dados que exige, em que período pode ocorrer e o efeito no estado.

| Evento | Dados | Efeito |
| --- | --- | --- |
| Início de jogo | Nenhum | Status vai para ao vivo; período, para 1T |
| Gol | Time, jogador, origem (jogada, pênalti ou contra) | Placar recalculado |
| Gol anulado | Motivo e o gol original, se já lançado | Placar recalculado sem o gol |
| Pênalti marcado | Time | Nenhum |
| Pênalti perdido | Time e jogador | Nenhum |
| Revisão do VAR | Lance revisado e decisão | Nenhum |
| Substituição | Time, quem sai e quem entra | Atualiza quem está em campo |
| Cartão amarelo | Time e jogador | Segundo amarelo exige o vermelho |
| Cartão vermelho | Time e jogador | Jogador fora de campo |
| Acréscimos | Minutos | Informa o acréscimo do período |
| Fim do 1T, início do 2T, início da prorrogação, início dos pênaltis | Nenhum | Período avança na ordem do diagrama |
| Cobrança na disputa de pênaltis | Time, jogador, convertida ou não | Placar dos pênaltis |
| Fim de jogo | Nenhum | Status vai para encerrado; tabela oficial recalculada |

- Regras duras rejeitam o lançamento: evento de jogo com a partida fora de ao vivo, transição fora do diagrama, gol anulado apontando para algo que não é gol.
- Regras brandas avisam e aceitam com confirmação: jogador fora da escalação, minuto menor que o do evento anterior.
- `visible_events` remove os lançamentos cancelados de toda leitura; o filtro por público só volta com a API pública.
- Gol válido é o que não teve o lançamento cancelado nem foi anulado por um lançamento ainda válido. Placar, últimos gols e alertas de gol usam essa regra; cobrança da disputa de pênaltis não conta como gol.

### Classificação

`compute_standings` recebe as partidas do grupo e as regras da fase e devolve as linhas em ordem. Pontuação, critérios e legenda são dados da fase, não código.

1. Considera as partidas encerradas; na visão ao vivo, também as em andamento.
2. Soma jogos, vitórias, empates, derrotas, gols pró, gols contra e cartões de cada time.
3. Calcula os pontos com a pontuação da fase.
4. Aplica os critérios na ordem configurada; cada um separa só os times que o anterior deixou empatados.
5. Se o empate sobrar, ordena pelo nome e marca as linhas como empatadas.

Cada critério é uma função que recebe o bloco de times ainda empatados e devolve um valor por time. Esse formato acomoda o confronto direto, que olha só os jogos entre os empatados. Cada critério tem também um nome de exibição, que as páginas mostram na ordem configurada.

| Critério | O que decide |
| --- | --- |
| `points` | Mais pontos, pela pontuação da fase |
| `wins` | Mais vitórias |
| `goal_difference` | Maior saldo de gols |
| `goals_for` | Mais gols pró |
| `head_to_head` | Mais pontos nos jogos entre os times empatados, sejam dois ou mais |
| `fewer_red_cards` | Menos cartões vermelhos, contados pelos eventos |
| `fewer_yellow_cards` | Menos cartões amarelos, contados pelos eventos |
| `drawing_of_lots` | Ordem do sorteio, guardada em `group_teams.lot_order` |

A legenda é uma lista de zonas da fase, cada uma com nome, cor e faixa de posições. A zona de cada linha sai da posição e vale para todos os grupos da fase.

Regras do primeiro campeonato, como entram no seed: pontos, vitórias, saldo de gols, gols pró e confronto direto, nessa ordem. Depois, mudam pelo Django Admin.

```json
{
  "points": { "win": 3, "draw": 1, "loss": 0 },
  "criteria": ["points", "wins", "goal_difference", "goals_for", "head_to_head"],
  "zones": [
    { "name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 4 },
    { "name": "Rebaixados", "color": "#B3261E", "from": 17, "to": 20 }
  ]
}
```

- Critério desconhecido, repetido ou lista vazia: a configuração é rejeitada.
- Cor fora do formato `#RRGGBB`, faixa invertida ou faixas sobrepostas: a configuração é rejeitada.
- Mudar pontuação ou critério recalcula as tabelas da fase; mudar zona ou cor não recalcula nada.
- Nos dois casos, as páginas recebem a classificação nova pelo stream.
- Vaga decidida entre grupos, como a de melhor terceiro colocado, fica fora do MVP.

### Mata-mata

`compute_tie_result` recebe o confronto, os jogos dele e os eventos do jogo decisivo e devolve o agregado, o vencedor e a forma da decisão.

1. Soma os gols dos jogos do confronto: um jogo ou ida e volta, conforme o confronto.
2. Com todos os jogos encerrados e agregado diferente, avança quem fez mais gols.
3. Com agregado igual, avança quem venceu a disputa de pênaltis do último jogo. Gol fora de casa não desempata.
4. Grava no confronto o vencedor e a forma da decisão, tirada dos eventos do jogo decisivo: com início dos pênaltis, `penalties`; só com início da prorrogação, `extra_time`; sem eles, `aggregate`.

- No jogo decisivo, com o agregado igual ao fim do 2T, o fim de jogo é rejeitado: o jogo vai à prorrogação, se o confronto tiver prorrogação, ou direto aos pênaltis.
- Com o agregado ainda igual ao fim da prorrogação, o fim de jogo também é rejeitado, e o jogo vai aos pênaltis. Na disputa, o fim de jogo exige placar de pênaltis diferente.
- Início da prorrogação só é aceito no 2T do jogo decisivo, com o agregado igual e o confronto com prorrogação. Início dos pênaltis só é aceito com o agregado igual: no 2T, se o confronto não tem prorrogação; na prorrogação, se tem.
- Gols da prorrogação entram no placar; as cobranças da disputa de pênaltis ficam em um placar à parte.
- Fase de mata-mata não tem grupo nem classificação; as páginas mostram os confrontos de cada rodada, com agregado e vencedor.
- O campeonato tem confrontos de jogo único e de ida e volta; o número de jogos e a prorrogação são dados de cada confronto.
- Operador ou administrador cadastra os confrontos da rodada seguinte; o sistema só apura quem avançou.

## Usuários e permissões

São dois perfis. O Operador faz todo o trabalho do dia a dia; o Administrador faz o mesmo e é o único que gerencia usuários e níveis de acesso.

| Ação | Operador | Administrador |
| --- | --- | --- |
| Lançar evento, cancelar lançamento e mudar status | Sim | Sim |
| Cadastrar competições, fases, grupos, rodadas, times, partidas e confrontos | Sim | Sim |
| Configurar critérios, zonas e cores da classificação | Sim | Sim |
| Criar usuários e definir níveis de acesso | Não | Sim |

- Os perfis são grupos do Django; cada ação é uma permissão, e o perfil só reúne permissões.
- O login é por sessão, com cookie e proteção CSRF; as senhas ficam com o hash padrão do Django.
- O Django Admin mostra a cada usuário só o que o perfil dele permite.
- Todo lançamento e todo cancelamento guardam o autor.

## API

São quatro grupos de rotas: autenticação, operação, leitura e tempo real.

| Grupo | Rota | Uso |
| --- | --- | --- |
| Autenticação | `POST /api/auth/login` | Abre a sessão do operador |
| Autenticação | `POST /api/auth/logout` | Encerra a sessão |
| Autenticação | `GET /api/auth/me` | Usuário logado e suas permissões |
| Operação | `POST /api/ops/matches/:id/events` | Lança evento; exige `Idempotency-Key` |
| Operação | `POST /api/ops/matches/:id/events/:eventId/void` | Cancela um lançamento errado |
| Operação | `POST /api/ops/matches/:id/status` | Adia, suspende, retoma, cancela ou reagenda |
| Leitura | `GET /api/home?date=` | Jogos do dia por competição, últimos gols e hora do servidor |
| Leitura | `GET /api/competitions` | Competições em ordem, para o menu |
| Leitura | `GET /api/competitions/:slug` | Página da competição: fases, fase e rodada atuais, classificação e jogos |
| Leitura | `GET /api/stages/:id/standings?live=1` | Classificação de todos os grupos de uma fase, com critérios e legenda |
| Leitura | `GET /api/matches?roundId=&date=&status=` | Lista de partidas |
| Leitura | `GET /api/matches/:id` | Partida com todos os eventos |
| Tempo real | `GET /api/stream?after=` | Stream SSE único, de todas as competições; retoma por `Last-Event-ID` ou pelo cursor em `after` |

- Cadastros e regras da classificação são feitos no Django Admin: competições, fases, grupos, rodadas, times, partidas e confrontos. O seed carrega duas competições; a primeira traz as regras do primeiro campeonato.
- Rotas de operação exigem sessão e permissão: sem login, `401`; sem permissão, `403`. A sessão usa cookie, com proteção CSRF.
- Regra violada responde `422` com o código da regra; chave de idempotência repetida devolve a resposta original.
- A documentação interativa da API fica em `/api/docs`; é por ela que o back é exercitado antes de existir front.

### Respostas de leitura

- `GET /api/home` devolve só as competições com jogo no dia, na ordem de `competitions.position`. Sem `date`, vale o dia de hoje no horário de Brasília.
- Jogos do dia são os que começam no dia, mais o jogo da véspera que passa da meia-noite: ele fica na resposta até duas horas depois do fim, pelo `finished_at`.
- Cada competição da resposta traz os jogos do dia e a classificação da fase desses jogos; fase de mata-mata não traz classificação.
- Partida de mata-mata leva o confronto junto: agregado, vencedor e forma da decisão. Vale para as leituras e para a mensagem `match`.
- A resposta da classificação tem três partes: `criteria` (nomes em ordem), `legend` (zonas com nome e cor) e `groups` (linhas, cada uma com sua zona).
- `latest_goals` traz os dez gols válidos mais recentes dos jogos do dia, do mais novo ao mais antigo. Cada gol leva id do evento, jogo, time, jogador, origem, período, minuto, acréscimo, placar depois do gol e horário do lançamento.
- `server_time` é um instante UTC em ISO 8601; `timezone` é o nome IANA do fuso, `America/Sao_Paulo`. Toda leitura com horário de jogo devolve `timezone`, e o filtro `date` segue o dia de Brasília.
- `cursor` é o maior id do `outbox`; com o `outbox` vazio, vale 0. O servidor lê o `cursor` primeiro e o estado depois; assim nenhuma mensagem se perde entre a resposta e o stream. `GET /api/competitions/:slug` devolve o mesmo campo.
- `GET /api/home` responde com `Cache-Control: no-store`.

### Mensagens do stream

- São três tipos de mensagem, cada uma com o estado completo: `match`, com a partida e os lances dela; `standings`, com a classificação ao vivo da fase; e `goals`, com a lista de últimos gols, a mesma de `GET /api/home` sem `date`. As duas primeiras levam o id da fase.
- A mensagem `goals` diz o que mudou: o gol que entrou, ou o gol que saiu por anulação ou cancelamento. É ela que dispara os alertas de gol.
- O front substitui o estado pelo da mensagem, sem lógica de merge.
- O id de cada mensagem é o id da linha no `outbox`; a trava de escrita faz a ordem dos ids ser a dos commits.
- Na reconexão automática do navegador, o servidor reenvia o que veio depois de `Last-Event-ID`. Na primeira conexão e quando a página recria o `EventSource`, reenvia o que veio depois de `after`: o `cursor` ou o id da última mensagem recebida. Se vierem os dois, vale `Last-Event-ID`.
- O stream envia um `ping` com a hora do servidor ao abrir e a cada 20 segundos. Ele mantém a conexão aberta e acerta o relógio; não tem id nem passa pelo `outbox`.
- O `outbox` guarda as mensagens por 24 horas.
- O publicador vive no processo do Django. O back roda com um processo ASGI só, atrás de um proxy sem buffer e com HTTPS.
- Cada página abre um stream só; um por competição esbarraria no limite de conexões do navegador.

## Front-end em HTML e JS, sem CSS

Três páginas estáticas servidas pelo próprio back, sem framework e sem etapa de build: a home, a página da competição e a tela do operador. O front só começa depois do back, e o CSS é uma etapa à parte. Jogo e classificação não têm página própria.

| Página | Mostra | Dados |
| --- | --- | --- |
| `index.html` (home) | Menu, relógio, últimos gols e os jogos do dia por competição, com a classificação ao lado | `GET /api/competitions`, `GET /api/home` e o stream |
| `competition.html` | Jogos por rodada e classificação com legenda; no mata-mata, os confrontos | `GET /api/competitions/:slug` e o stream |
| `operator.html` | Login, lançamento de eventos, mudança de status, cancelamento de lançamento | Rotas `/api/auth` e `/api/ops`; `GET /api/matches` e `GET /api/matches/:id` |

### Blocos da home

&#91;embedded content: home · relógio, últimos gols e uma seção por competição com jogo no dia\]

A home tem menu, relógio, últimos gols e uma seção para cada competição com jogo no dia. O desenho mostra a disposição pedida para a etapa de CSS: classificação à direita dos jogos, no mesmo topo do primeiro jogo. A posição do relógio e dos últimos gols é só sugestão.

| Bloco | Elementos | Dados |
| --- | --- | --- |
| 1. Menu de competições | Um `<nav>` com um link por competição, para a página dela | `GET /api/competitions` |
| 2. Relógio | Um `<time>` com a hora de Brasília, atualizada a cada segundo, e o rótulo “horário de Brasília” | `server_time` e `timezone` de `GET /api/home`; `ping` do stream |
| 3. Últimos gols | Um `<ol>` com os dez gols mais recentes do dia: minuto, jogador, time e placar. Acima dele, o aviso de gol e os botões “ativar notificações” e “ativar som” | `latest_goals` de `GET /api/home`; mensagens `goals` do stream |
| 4. Competição | Uma `<section>` por competição com jogo no dia; `<h2>` com nome e fase | `GET /api/home` |
| 5. Jogos do dia | Uma `<table>` com uma linha por jogo: horário, times, placar e status | `GET /api/home` |
| 6. Lances do jogo | Acordeão: um `<details>` em cada jogo, com a linha do tempo em `<ol>` | `GET /api/matches/:id`, ao abrir |
| 7. Classificação | Uma `<table>` por grupo, com `<caption>`; cada linha leva a cor e o nome da zona | `GET /api/home` |
| 8. Legenda e critérios | Lista das zonas, com amostra de cor e nome; `<ol>` com os critérios de desempate | `GET /api/home` |

- A home mostra só os jogos do dia; competição sem jogo no dia não aparece.
- Sem jogo no dia, a home mostra o menu, o relógio e uma mensagem; o bloco de últimos gols não aparece.
- As seções seguem a ordem de `competitions.position` e saem do mesmo `<template>`.
- Os lances ficam fechados; o usuário abre o acordeão do jogo que quiser.
- A classificação ao lado é a da fase dos jogos do dia, ao vivo; jogo de mata-mata não tem classificação ao lado.
- A cor da legenda aparece sem CSS: um `<svg>` pequeno com um `<rect>` cujo atributo `fill` vem da API.
- A cor nunca é o único sinal: o nome da zona vai em texto, na linha e na legenda.

### Relógio e últimos gols

- O relógio mostra a hora de Brasília para qualquer visitante, mesmo em outro fuso; os horários dos jogos seguem a mesma regra.
- A hora vem do servidor, não do aparelho. Ao receber `server_time`, a home calcula o desvio: hora do servidor menos hora do aparelho, com sinal.
- A cada `ping`, a home refaz a conta e usa o maior desvio dos cinco últimos: é o da mensagem que menos demorou na rede. Assim um `ping` atrasado não atrasa o relógio.
- Na virada do dia, a home busca os jogos de novo. Ao voltar ao primeiro plano, confere se o dia virou.
- A lista traz os dez gols mais recentes dos jogos do dia, de todas as competições; sem gol ainda, mostra uma mensagem.
- Gol anulado ou lançamento cancelado sai da lista na hora; cobrança da disputa de pênaltis não entra.

### Alertas de gol

Cada gol de um jogo do dia gera três alertas na home: aviso na página, som e notificação do sistema.

| Alerta | Como aparece | Exige |
| --- | --- | --- |
| Aviso na página | Texto com time, placar, jogador e minuto, acima dos últimos gols, em região `aria-live` | Nada; vale para todo visitante |
| Som | Um `<audio>` curto, tocado a cada gol | Um clique em “ativar som” |
| Notificação do sistema | `Notification` do navegador, visível com a home em outra aba | Permissão, pedida em “ativar notificações”; HTTPS; só no computador |

- Todo gol do dia gera alerta, de qualquer jogo ou competição; o visitante não escolhe.
- Os alertas só existem com a home aberta: a página da competição não alerta. Alerta com o site fechado exigiria push, com service worker e Web Push, e ficou fora do plano.
- O navegador só libera som depois de um gesto do usuário. O botão “ativar som” toca o som uma vez: serve de amostra e libera o áudio.
- Os dois botões ligam e desligam; a escolha fica guardada no navegador. Se o navegador recusar o som depois de recarregar a página, o botão volta a “ativar som”.
- No celular, `new Notification()` não funciona, e o navegador suspende a página em segundo plano. Lá valem o aviso na página e o som, com a home na tela; o botão de notificações só aparece onde `new Notification()` funciona.
- A home só gera alerta para gol que chega pelo stream; os gols da lista inicial contam como já alertados. Cada gol gera um só alerta; a home confere pelo id do evento.
- Gol lançado há mais de dois minutos, pela hora do servidor, entra na lista sem alerta.
- Gol anulado ou lançamento cancelado gera um aviso de correção na página, sem som; a home fecha a notificação do gol e abre a de correção. Anulação cancelada devolve o gol à lista, sem alerta.
- O som é um arquivo curto em `static/sounds/`; escolher o arquivo faz parte da fase 8.

Fontes dos limites de navegador: [MDN, construtor `Notification()`](https://developer.mozilla.org/docs/Web/API/notification/Notification), [MDN, uso da API de notificações](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API), [Chrome, política de autoplay](https://developer.chrome.com/blog/autoplay), [WebKit, Web Push no iOS e iPadOS](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/), [Chrome, ciclo de vida da página](https://developer.chrome.com/docs/web-platform/page-lifecycle-api) e [HTML Standard, server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html).

### Página da competição

Reúne tudo de uma competição: os jogos de todas as rodadas e a classificação completa, com os mesmos blocos de jogos, lances, classificação e legenda.

- O endereço leva o `slug`: `competition.html?slug=`. O menu da home aponta para ele.
- A página abre na fase e na rodada atuais: as primeiras com jogo ainda não encerrado.
- Botões de rodada anterior e próxima trocam os jogos; um `<select>` troca de fase.
- Em fase de mata-mata, saem os confrontos de cada rodada, com agregado e vencedor, no lugar da classificação.

### Layout: etapa de CSS

O layout fica para a etapa de CSS, a discutir depois do front. Um requisito já está registrado: classificação à direita dos jogos de cada competição, com o topo alinhado ao do primeiro jogo. Até lá, o HTML sai em ordem de leitura, jogos e depois classificação, sem tabela de layout.

### Implementação

- Módulos ES com `<script type="module">`: `api.js` (fetch), `stream.js` (EventSource), `render.js` (DOM), `clock.js` (relógio), `alerts.js` (alertas de gol) e um arquivo por página.
- A home e a página da competição seguem o mesmo ciclo: busca o estado, desenha e assina o stream a partir do `cursor`. Cada mensagem substitui o estado do jogo, da fase ou da lista de gols e redesenha o trecho afetado.
- A página recria o `EventSource` em três casos: ao voltar ao primeiro plano, ao ficar 45 segundos sem `ping` e quando o navegador desiste da conexão. Ao recriar, envia em `after` o id da última mensagem recebida. O celular derruba a conexão em segundo plano.
- Depois de cinco minutos sem stream, a página busca o estado de novo em vez de pedir o reenvio.
- Sem CSS, a legibilidade vem da semântica: `<nav>`, `<section>`, `<table>`, `<caption>`, `<details>`, `<ol>`, `<time>` e `<form>` com validação nativa.
- Texto entra por `textContent` ou `<template>`, nunca por `innerHTML` com dado da API.
- O aviso de gol fica em uma região `aria-live`, para o leitor de tela anunciar; o relógio fica fora dela, para não ser lido a cada segundo.
- A tela do operador mostra só as ações que `GET /api/auth/me` permite. Ela não assina o stream: busca a partida de novo depois de cada envio.
- Na tela do operador, campos aparecem e somem com `hidden` e confirmações usam `<dialog>`.
- Cada envio do operador leva o token CSRF e uma chave gerada com `crypto.randomUUID()`; o botão fica desabilitado até a resposta.

## Fases de implementação

O back vem inteiro antes do front: fases 0 a 6. O MVP fecha na fase 8; o CSS é uma etapa à parte, e a API pública ficou para a fase 12.

| Etapa | Fase | Entrega | Pronto quando |
| --- | --- | --- | --- |
| Back | 0. Fundação | Projeto Django, Postgres via Docker Compose, usuário próprio, migrações, pytest e `GET /health` | As migrações rodam do zero e o healthcheck responde |
| Back | 1. Usuários e permissões | Login por sessão, grupos Operador e Administrador, permissões por ação e Django Admin | Um operador autentica e é barrado na gestão de usuários, exclusiva do administrador; testes cobrem `401` e `403` |
| Back | 2. Estrutura e partidas | Modelos de competição a partida, cadastro no Django Admin, seed com duas competições, rotas de leitura: menu, página da competição, jogos do dia e lista de partidas, com hora do servidor e fuso | As rotas devolvem os jogos do dia por competição; um teste cobre a virada do dia no horário de Brasília, com um jogo que passa da meia-noite |
| Back | 3. Eventos e estado | Catálogo de tipos, máquina de estados, `apply_event`, placar derivado, lista de últimos gols, idempotência, trava de escrita, gol anulado, cancelamento de lançamento, mudança de status e leitura da partida com os lances | Um jogo inteiro é lançado pela API, com gol anulado, e o placar fecha; o gol anulado sai dos últimos gols; transições inválidas e usuário sem permissão falham em teste |
| Back | 4. Classificação | `compute_standings` com critérios e zonas configuráveis no Django Admin, confronto direto, recálculo na transação, visões oficial e ao vivo | A tabela final de um campeonato real é reproduzida em teste; um gol anulado desfaz a mudança na tabela ao vivo; trocar critério ou cor muda a resposta da API |
| Back | 5. Mata-mata | Fases eliminatórias, confrontos de um ou dois jogos, agregado, prorrogação opcional por confronto, pênaltis e `compute_tie_result` | Dois confrontos são lançados de ponta a ponta pela API: um de ida e volta com prorrogação e outro de jogo único direto nos pênaltis. O vencedor sai certo, e o fim de jogo com o confronto empatado é rejeitado |
| Back | 6. Tempo real | Outbox, publicador, `cursor` nas leituras e stream SSE com retomada, mensagens de gol e `ping`; ambiente de teste com HTTPS, proxy sem buffer e um processo ASGI só | Dois clientes conectados recebem o mesmo evento; derrubar a conexão não perde nem duplica mensagens; gol anulado gera uma mensagem `goals` com o gol que saiu |
| Front | 7. Tela do operador | `operator.html`: login, lançamento de eventos, mudança de status e cancelamento | Um operador lança um jogo inteiro pela tela, sem chamar a API à mão |
| Front | 8. Home e página da competição | `index.html` com menu, relógio, últimos gols, jogos do dia com classificação e legenda, e alertas de gol; `competition.html` com rodadas, classificação e confrontos do mata-mata; lances em acordeão e stream | Duas abas acompanham um jogo ao vivo sem recarregar, na home e na página da competição; um gol lançado mostra o aviso na página, e toca o som e gera a notificação em quem ativou |
| Front | 9. CSS | Etapa à parte, a discutir; requisito já registrado: classificação à direita dos jogos, topo alinhado ao do primeiro jogo | A definir na própria etapa |
| Depois | 10. Enriquecimento | Jogadores, escalação, árbitros, transmissão, público, renda e estatísticas | Substituição e cartão validam contra quem está em campo |
| Depois | 11. Operação | Adiamento e suspensão completos, auditoria, logs estruturados e métricas | Toda ação de operador aparece na auditoria, com autor e horário |
| Depois | 12. API pública | Rotas `/public/v1`, serializadores com lista de permissão, cache HTTP, chave, limite de uso e documentação OpenAPI | Um teste de contrato prova que gol anulado e tipos internos não aparecem |

A ordem é proposital: o domínio fica correto e testado antes do tempo real, que é só transporte. As fases 3 a 5 concentram o risco; vale escrever os testes do domínio antes das rotas. Enquanto não há front, o back é exercitado por testes, pela documentação interativa da API e pelo Django Admin.

## Decisões

Dezoito decisões estão tomadas e nenhuma pergunta ficou em aberto; o back pode começar pela fase 0.

| Tema | Decisão |
| --- | --- |
| Stack | Python com Django e PostgreSQL |
| Lançamento de eventos | Operador logado na tela do operador, com permissão; exige módulo de usuários |
| Perfis | Operador e Administrador; só o Administrador gerencia usuários e níveis de acesso |
| Ordem de trabalho | Back inteiro primeiro, depois front; CSS em etapa à parte |
| Home | Apenas os jogos do dia, com a classificação de cada competição ao lado |
| Páginas | Competição tem página com jogos e classificação; há um menu de competições; jogo e classificação não têm página |
| Lances do jogo | Na home, em acordeão aberto pelo usuário |
| Fuso e relógio | O dia segue o horário de Brasília; a home mostra um relógio com a hora atual |
| Últimos gols | A home tem uma seção de últimos gols |
| Alertas de gol | Cada gol gera notificação e alerta sonoro |
| Gols que alertam | Todo gol do dia; o visitante não escolhe jogos nem competições |
| Alcance dos alertas | Só com a home aberta; sem push com o site fechado |
| Critérios de desempate | Configurados no back; ordem inicial: pontos, vitórias, saldo de gols, gols pró, confronto direto |
| Confronto direto | Vale para qualquer número de times empatados |
| Mata-mata | Faz parte do MVP; há confrontos de jogo único e de ida e volta; gol fora não vale |
| Prorrogação | Pode haver ou não; o sistema guarda isso em cada confronto |
| Avanço na chave | Operador e Administrador cadastram os confrontos da rodada seguinte |
| API pública | Fora do MVP |
