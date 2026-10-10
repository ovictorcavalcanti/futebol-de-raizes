// Partes puras da tela do operador e do cliente da API: node --test tests/js/operator.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  effectiveMinuteMode, suggestMinute, buildEventBody, toBrasiliaInput, groupByCompetition, lineupPlayers,
  splitActions, structuralConfirm, sortByStatus, clockSetRange, editedToast,
} from '../../static/js/operator.js';
import {
  ApiError, apiFetch, getCookie, listMatches, queryString, toApiError, newIdempotencyKey, setCsrfToken,
} from '../../static/js/api.js';

const MIN = 60_000;

test('clockSetRange: faixas do acerto do relógio por tempo (com os dois da prorrogação)', () => {
  assert.deepEqual(['first_half', 'second_half', 'extra_time', 'extra_second_half'].map(clockSetRange),
    [[0, 45], [46, 90], [91, 105], [106, 120]]);
  assert.equal(clockSetRange('extra_half_time'), null);
  assert.equal(structuralConfirm('extra_half_time', {}).ok, 'Encerrar 1º tempo');
});
test('aviso da correção: diz quando o vermelho automático caiu junto', () => {
  assert.equal(editedToast("Amarelo · Alice, 10'", []), "Corrigido: Amarelo · Alice, 10'.");
  assert.equal(editedToast("Amarelo · Alice, 10'", undefined), "Corrigido: Amarelo · Alice, 10'.");
  assert.equal(editedToast("Amarelo · Alice, 10'", [7]), "Corrigido: Amarelo · Alice, 10'. O vermelho automático caiu junto.");
  assert.equal(editedToast('Amarelo', [7, 8]), 'Corrigido: Amarelo. 2 vermelhos automáticos caíram junto.');
});
const NOW = Date.parse('2026-10-03T21:00:30Z');
const spec = (type, kind = 'game', minute = 'required') => ({ type, kind, minute, label: type, fields: [] });
const GOAL = spec('goal');
const HALF_TIME = spec('half_time', 'structural', 'optional');
const MATCH_END = spec('match_end', 'structural', 'optional');
const SECOND_HALF = spec('second_half_start', 'structural', 'optional');
const SHOOTOUT = spec('shootout_kick', 'game', 'optional');
const SUSPEND = spec('suspended', 'status', 'none');

function liveMatch(period, minutesAgo, clock) {
  return {
    id: 12, status: 'live', period,
    period_started_at: new Date(NOW - minutesAgo * MIN).toISOString(),
    clock,
  };
}
const FIRST = { running: true, offset: 0, regular_end: 45, stoppage_announced: null };
const SECOND = { running: true, offset: 45, regular_end: 90, stoppage_announced: null };
const EXTRA = { running: true, offset: 90, regular_end: 120, stoppage_announced: null };

test('modo do minuto: obrigatório só com o relógio correndo (espelha domain.minute_mode)', () => {
  assert.equal(effectiveMinuteMode(GOAL, 'first_half'), 'required');
  assert.equal(effectiveMinuteMode(GOAL, 'extra_time'), 'required');
  assert.equal(effectiveMinuteMode(spec('substitution'), 'half_time'), 'optional');
  assert.equal(effectiveMinuteMode(spec('yellow_card'), 'penalties'), 'optional');
  assert.equal(effectiveMinuteMode(HALF_TIME, 'first_half'), 'optional');
  assert.equal(effectiveMinuteMode(SHOOTOUT, 'penalties'), 'optional');
  assert.equal(effectiveMinuteMode(SUSPEND, 'second_half'), 'none');
  assert.equal(effectiveMinuteMode(null, 'first_half'), 'none');
});

test('minuto sugerido pelo relógio do jogo (period_started_at + servidor)', () => {
  // 1T, 10 min e meio depois do início → 11'
  assert.deepEqual(suggestMinute(liveMatch('first_half', 10.5, FIRST), GOAL, NOW), { minute: 11, stoppage: null });
  // 1T passou dos 45 → 45+2
  assert.deepEqual(suggestMinute(liveMatch('first_half', 46.2, FIRST), GOAL, NOW), { minute: 45, stoppage: 2 });
  // 2T, 26 min depois do reinício → 72'
  assert.deepEqual(suggestMinute(liveMatch('second_half', 26.5, SECOND), GOAL, NOW), { minute: 72, stoppage: null });
  // prorrogação → 120+1
  assert.deepEqual(suggestMinute(liveMatch('extra_time', 30.4, EXTRA), GOAL, NOW), { minute: 120, stoppage: 1 });
  // acréscimo nunca passa de 30
  assert.deepEqual(suggestMinute(liveMatch('second_half', 200, SECOND), GOAL, NOW), { minute: 90, stoppage: 30 });
});

test('fim do tempo sugere o minuto final com o acréscimo jogado; início de período fica em branco', () => {
  assert.deepEqual(suggestMinute(liveMatch('first_half', 47.5, FIRST), HALF_TIME, NOW), { minute: 45, stoppage: 3 });
  assert.deepEqual(suggestMinute(liveMatch('first_half', 30, FIRST), HALF_TIME, NOW), { minute: 45, stoppage: null });
  assert.deepEqual(suggestMinute(liveMatch('second_half', 50, SECOND), MATCH_END, NOW), { minute: 90, stoppage: 6 });
  assert.equal(suggestMinute(liveMatch('second_half', 10, SECOND), spec('extra_time_start', 'structural', 'optional'), NOW), null);
  assert.equal(suggestMinute({ id: 1, status: 'scheduled', period: null, clock: null }, spec('match_start', 'structural', 'optional'), NOW), null);
  assert.equal(suggestMinute({ id: 1, status: 'live', period: 'half_time', clock: null, period_started_at: new Date(NOW).toISOString() }, SECOND_HALF, NOW), null);
});

test('sem relógio correndo não há sugestão (intervalo, pênaltis, suspenso, status)', () => {
  const halfTime = { id: 1, status: 'live', period: 'half_time', period_started_at: new Date(NOW - 5 * MIN).toISOString(), clock: null };
  assert.equal(suggestMinute(halfTime, spec('substitution'), NOW), null);
  const penalties = { id: 1, status: 'live', period: 'penalties', period_started_at: new Date(NOW).toISOString(), clock: null };
  assert.equal(suggestMinute(penalties, SHOOTOUT, NOW), null);
  const suspended = { ...liveMatch('second_half', 20, { ...SECOND, running: false }), status: 'suspended' };
  assert.equal(suggestMinute(suspended, GOAL, NOW), null);
  assert.equal(suggestMinute(liveMatch('second_half', 20, SECOND), SUSPEND, NOW), null);
});

test('corpo do lançamento: payload.*, minuto/acréscimo, vazios fora, false fica', () => {
  assert.deepEqual(
    buildEventBody('goal', [['team_id', 1], ['payload.player', 'Zé Roberto'], ['payload.origin', null], ['payload.assist', '']], { minute: 90, stoppage: 3 }),
    { type: 'goal', minute: 90, stoppage: 3, team_id: 1, payload: { player: 'Zé Roberto' } },
  );
  assert.deepEqual(
    buildEventBody('shootout_kick', [['team_id', 2], ['payload.player', 'Kayo'], ['payload.scored', false]], { minute: null, stoppage: 4 }),
    { type: 'shootout_kick', team_id: 2, payload: { player: 'Kayo', scored: false } },
  );
  assert.deepEqual(buildEventBody('goal_annulled', [['annuls_event_id', 91], ['payload.reason', 'Impedimento']], { minute: 74, stoppage: 0 }),
    { type: 'goal_annulled', minute: 74, annuls_event_id: 91, payload: { reason: 'Impedimento' } });
  assert.deepEqual(buildEventBody('match_start'), { type: 'match_start' });
});

test('datetime-local no horário de Brasília, de qualquer fuso', () => {
  assert.equal(toBrasiliaInput('2026-10-03T19:30:00Z'), '2026-10-03T16:30');
  assert.equal(toBrasiliaInput('2026-10-04T02:15:00Z'), '2026-10-03T23:15');
  assert.equal(toBrasiliaInput('lixo'), '');
});

test('partidas agrupadas por competição na ordem da API', () => {
  const pe = { id: 1, name: 'Pernambucano Raiz' };
  const copa = { id: 2, name: 'Copa Pernambuco' };
  const groups = groupByCompetition([{ id: 1, competition: pe }, { id: 2, competition: copa }, { id: 3, competition: pe }]);
  assert.deepEqual(groups.map((g) => [g.competition.name, g.matches.map((m) => m.id)]), [['Pernambucano Raiz', [1, 3]], ['Copa Pernambuco', [2]]]);
});

test('ordem por status: ao vivo > agendado > encerrado (encerrados do mais recente)', () => {
  const at = (h) => `2026-10-04T${String(h).padStart(2, '0')}:00:00Z`;
  const ids = sortByStatus([
    { id: 1, status: 'finished', kickoff_at: at(10) },
    { id: 2, status: 'scheduled', kickoff_at: at(22) },
    { id: 3, status: 'live', kickoff_at: at(19) },
    { id: 4, status: 'finished', kickoff_at: at(15) },
    { id: 5, status: 'scheduled', kickoff_at: at(20) },
    { id: 6, status: 'delayed', kickoff_at: at(18) },
  ]).map((m) => m.id);
  assert.deepEqual(ids, [6, 3, 5, 2, 4, 1]);
});

test('botões: lances de jogo primeiro, andamento do jogo (estruturais) à parte, ordem da API em cada grupo', () => {
  const kinds = {
    half_time: 'structural', match_end: 'structural', penalties_start: 'structural',
    goal: 'game', yellow_card: 'game', substitution: 'game', shootout_kick: 'game',
  };
  const specOf = (type) => ({ kind: kinds[type] || 'game' });
  // a API devolve na ordem do catálogo: estruturais antes do gol
  assert.deepEqual(splitActions(['half_time', 'goal', 'yellow_card', 'substitution', 'match_end'], specOf), {
    game: ['goal', 'yellow_card', 'substitution'],
    flow: ['half_time', 'match_end'],
  });
  assert.deepEqual(splitActions(['match_start'], () => ({ kind: 'structural' })), { game: [], flow: ['match_start'] });
  assert.deepEqual(splitActions(['penalties_start', 'shootout_kick'], specOf), { game: ['shootout_kick'], flow: ['penalties_start'] });
  assert.deepEqual(splitActions([], specOf), { game: [], flow: [] });
  // tipo desconhecido do catálogo fica nos lances (nunca some)
  assert.deepEqual(splitActions(['novo_tipo'], () => undefined), { game: ['novo_tipo'], flow: [] });
});

test('confirmação do andamento do jogo: um texto por estrutural, com o placar', () => {
  const match = { home: { name: 'Sport', short_name: 'SPT' }, away: { name: 'Náutico' }, home_score: 2, away_score: 1 };
  assert.deepEqual(structuralConfirm('match_end', match), {
    title: 'Encerrar a partida?', text: 'Sport 2 × 1 Náutico. A tabela oficial é recalculada.', ok: 'Encerrar',
  });
  assert.equal(structuralConfirm('half_time', match).title, 'Encerrar o 1º tempo?');
  assert.match(structuralConfirm('half_time', match).text, /^Sport 2 × 1 Náutico\. /);
  assert.equal(structuralConfirm('second_half_start', match).ok, 'Iniciar 2º tempo');
  assert.equal(structuralConfirm('extra_time_start', match).title, 'Iniciar a prorrogação?');
  assert.equal(structuralConfirm('penalties_start', match).title, 'Ir para os pênaltis?');
  const start = structuralConfirm('match_start', { home: { name: 'Sport' }, away: { name: 'Náutico' }, home_score: null, away_score: null });
  assert.equal(start.title, 'Iniciar a partida?');
  assert.match(start.text, /^Sport × Náutico\./);
  // estrutural novo, sem texto próprio: usa o rótulo do catálogo
  assert.deepEqual(structuralConfirm('intervalo_tecnico', match, 'Intervalo técnico'), {
    title: 'Intervalo técnico?', text: 'Sport 2 × 1 Náutico. Isto muda o período da partida.', ok: 'Confirmar',
  });
  for (const type of ['match_start', 'half_time', 'second_half_start', 'extra_time_start', 'penalties_start', 'match_end']) {
    const c = structuralConfirm(type, match);
    assert.ok(c.title.endsWith('?') && c.text && c.ok, type);
  }
});

test('jogadores da escalação: do time escolhido; no gol contra, do adversário', () => {
  const match = {
    home: { id: 1 }, away: { id: 2 },
    lineups: {
      home: { starters: [{ name: 'Zé Roberto', number: 10, position: 'MF' }], substitutes: [{ name: 'Lucas Arcanjo', number: 14, position: 'MF' }] },
      away: { starters: [{ name: 'Kayo Lima', number: 9, position: 'FW' }], substitutes: [] },
    },
  };
  assert.deepEqual(lineupPlayers(match, 1).map((p) => p.name), ['Zé Roberto', 'Lucas Arcanjo']);
  assert.deepEqual(lineupPlayers(match, 1, { opponent: true }).map((p) => p.name), ['Kayo Lima']);
  assert.deepEqual(lineupPlayers(match, null).map((p) => p.name), ['Zé Roberto', 'Lucas Arcanjo', 'Kayo Lima']);
  assert.deepEqual(lineupPlayers({ home: { id: 1 }, away: { id: 2 }, lineups: { home: null, away: null } }, 1), []);
});

/* --- api.js ------------------------------------------------------------------------------------- */

test('cookie, query string e chave de idempotência', () => {
  assert.equal(getCookie('csrftoken', 'sessionid=x; csrftoken=abc%3D123; theme=dark'), 'abc=123');
  assert.equal(getCookie('csrftoken', 'sessionid=x'), '');
  assert.equal(queryString({ date: '2026-10-03', roundId: null, live: 1, empty: '' }), '?date=2026-10-03&live=1');
  assert.equal(queryString({}), '');
  assert.equal(queryString(null), '');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  assert.match(newIdempotencyKey(), uuid);
  // fora de contexto seguro não há randomUUID: monta um v4 com getRandomValues
  assert.match(newIdempotencyKey({ getRandomValues: (a) => a.fill(255) }), uuid);
  assert.notEqual(newIdempotencyKey(), newIdempotencyKey());
});

test('erros no formato do contrato e no padrão do ninja', () => {
  const rule = toApiError(422, { code: 'confirmation_required', message: 'Há avisos', details: { codes: ['minute_decreasing'] }, warnings: [{ code: 'minute_decreasing', message: 'O minuto 70 é anterior' }] });
  assert.ok(rule instanceof ApiError);
  assert.equal(rule.code, 'confirmation_required');
  assert.equal(rule.warnings[0].code, 'minute_decreasing');
  assert.deepEqual(rule.details, { codes: ['minute_decreasing'] });
  assert.equal(toApiError(403, { detail: 'CSRF check Failed' }).code, 'csrf_failed');
  assert.equal(toApiError(404, null).code, 'not_found');
  assert.equal(toApiError(500, null).code, 'http_error');
});

test('apiFetch: JSON, mesma origem, CSRF só nos métodos que mudam estado, Idempotency-Key', async () => {
  setCsrfToken('tok-123');
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ ok: true }), { status: init.method === 'POST' ? 201 : 200, headers: { 'Content-Type': 'application/json' } });
  };
  assert.deepEqual(await apiFetch('/api/home', { query: { date: '2026-10-03' }, fetchImpl }), { ok: true });
  await apiFetch('/api/ops/matches/12/events', { method: 'POST', body: { type: 'goal' }, idempotencyKey: 'k-1', fetchImpl });
  const [get, post] = calls;
  assert.equal(get.url, '/api/home?date=2026-10-03');
  assert.equal(get.init.credentials, 'same-origin');
  assert.equal(get.init.headers['X-CSRFToken'], undefined);
  assert.equal(post.init.headers['X-CSRFToken'], 'tok-123');
  assert.equal(post.init.headers['Idempotency-Key'], 'k-1');
  assert.equal(post.init.headers['Content-Type'], 'application/json');
  assert.equal(post.init.body, JSON.stringify({ type: 'goal' }));
});

test('listMatches pagina com limit e offset', async () => {
  const urls = [];
  const fetchImpl = async (url) => { urls.push(url); return new Response('{"matches":[],"has_more":false}', { headers: { 'Content-Type': 'application/json' } }); };
  await listMatches({ date: '2026-10-03', limit: 50, offset: 100 }, { fetchImpl });
  await listMatches({ roundId: 7 }, { fetchImpl });
  assert.deepEqual(urls, ['/api/matches?date=2026-10-03&limit=50&offset=100', '/api/matches?roundId=7']);
});

test('apiFetch: erro tipado, tempo esgotado e falha de rede', async () => {
  const failing = async () => new Response(JSON.stringify({ code: 'match_not_live', message: 'A partida não está ao vivo.', details: {} }), { status: 422 });
  await assert.rejects(apiFetch('/x', { method: 'POST', body: {}, fetchImpl: failing }), (e) => e instanceof ApiError && e.status === 422 && e.code === 'match_not_live' && e.message === 'A partida não está ao vivo.');

  const hanging = (url, init) => new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))));
  await assert.rejects(apiFetch('/x', { timeout: 20, fetchImpl: hanging }), (e) => e instanceof ApiError && e.code === 'timeout' && e.isNetwork);

  const offline = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(apiFetch('/x', { fetchImpl: offline }), (e) => e.code === 'network_error' && e.status === 0);

  const html = async () => new Response('<html>', { status: 200 });
  await assert.rejects(apiFetch('/x', { fetchImpl: html }), (e) => e.code === 'invalid_response');
});

test('effectiveMinuteMode: gol tem minuto opcional em qualquer tempo (gol a confirmar)', () => {
  const goal = { type: 'goal', kind: 'game', minute: 'optional' };
  const card = { type: 'yellow_card', kind: 'game', minute: 'required' };
  for (const period of ['first_half', 'half_time', 'second_half', 'extra_half_time']) assert.equal(effectiveMinuteMode(goal, period), 'optional');
  assert.equal(effectiveMinuteMode(card, 'first_half'), 'required');
  assert.equal(effectiveMinuteMode(card, 'half_time'), 'optional');
});
