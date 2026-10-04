// Reconexão do stream SSE (sem DOM, EventSource falso): node --test tests/js/stream.test.mjs
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import {
  decideReconnect, backoffDelay, streamUrl, parseEventId, createStream,
  READY, STATUS, PING_TIMEOUT, STALE_AFTER, ALIVE_GRACE,
} from '../../static/js/stream.js';

const T0 = 1_000_000;

test('45 s sem ping recria com o último id; antes disso, nada', () => {
  const base = { trigger: 'watchdog', readyState: READY.OPEN, lastContactAt: T0, openedAt: T0 };
  assert.equal(decideReconnect({ ...base, nowMs: T0 + PING_TIMEOUT }), 'none');
  assert.equal(decideReconnect({ ...base, nowMs: T0 + PING_TIMEOUT + 1 }), 'replay');
  // EventSource recém-criado ganha 45 s para receber o primeiro ping
  assert.equal(decideReconnect({ ...base, openedAt: T0 + 40_000, nowMs: T0 + 60_000 }), 'none');
  // o navegador tentando sozinho (CONNECTING) também cai na regra dos 45 s
  assert.equal(decideReconnect({ ...base, readyState: READY.CONNECTING, nowMs: T0 + 50_000 }), 'replay');
});

test('navegador desistiu (CLOSED) ou sem EventSource: recria', () => {
  assert.equal(decideReconnect({ trigger: 'error', readyState: READY.CLOSED, lastContactAt: T0, nowMs: T0 + 1_000 }), 'replay');
  assert.equal(decideReconnect({ trigger: 'watchdog', readyState: null, lastContactAt: T0, nowMs: T0 + 1_000 }), 'replay');
});

test('voltar ao primeiro plano recria, salvo conexão comprovadamente viva', () => {
  const base = { trigger: 'visible', lastContactAt: T0, openedAt: T0 };
  assert.equal(decideReconnect({ ...base, readyState: READY.OPEN, nowMs: T0 + ALIVE_GRACE - 1 }), 'none');
  assert.equal(decideReconnect({ ...base, readyState: READY.OPEN, nowMs: T0 + ALIVE_GRACE + 1 }), 'replay');
  assert.equal(decideReconnect({ ...base, readyState: READY.CONNECTING, nowMs: T0 + 1_000 }), 'replay');
});

test('mais de 5 minutos sem stream: busca o estado de novo em vez de pedir reenvio', () => {
  for (const trigger of ['watchdog', 'visible', 'error']) {
    assert.equal(decideReconnect({ trigger, readyState: READY.CLOSED, lastContactAt: T0, nowMs: T0 + STALE_AFTER }), 'replay');
    assert.equal(decideReconnect({ trigger, readyState: READY.CLOSED, lastContactAt: T0, nowMs: T0 + STALE_AFTER + 1 }), 'refetch');
  }
});

test('espera crescente, com teto e variação', () => {
  const mid = () => 0.5;
  assert.deepEqual([0, 1, 2, 3, 10].map((n) => backoffDelay(n, { random: mid })), [1_000, 2_000, 4_000, 8_000, 30_000]);
  assert.equal(backoffDelay(0, { random: () => 0 }), 800);
  assert.equal(backoffDelay(0, { random: () => 1 }), 1_200);
});

test('URL com after e id da mensagem', () => {
  assert.equal(streamUrl('/api/stream', 4182), '/api/stream?after=4182');
  assert.equal(streamUrl('/api/stream', 0), '/api/stream?after=0');
  assert.equal(streamUrl('/api/stream', null), '/api/stream');
  assert.equal(streamUrl('/api/stream', -1), '/api/stream');
  assert.equal(parseEventId('43'), 43);
  assert.equal(parseEventId(''), null);
  assert.equal(parseEventId('4a'), null);
});

/* --- createStream com EventSource falso e relógio controlado ------------------------------- */

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = READY.CONNECTING;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  close() {
    this.closed = true;
    this.readyState = READY.CLOSED;
  }
  // ajudantes do teste
  open() {
    this.readyState = READY.OPEN;
    this.onopen?.();
  }
  emit(type, data, id = '') {
    for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data), lastEventId: id });
  }
  fail(closed = false) {
    this.readyState = closed ? READY.CLOSED : READY.CONNECTING;
    this.onerror?.();
  }
}

function setup(extra = {}) {
  FakeEventSource.instances = [];
  mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  let nowMs = T0;
  const synced = [];
  const statuses = [];
  const received = [];
  const listeners = {};
  const win = {
    document: { visibilityState: 'visible', addEventListener: (t, fn) => { listeners[t] = fn; }, removeEventListener() {} },
    addEventListener: (t, fn) => { listeners[t] = fn; },
    removeEventListener() {},
  };
  const stream = createStream({
    EventSourceImpl: FakeEventSource,
    now: () => nowMs,
    win,
    clock: { sync: (t) => synced.push(t) },
    onStatus: (s) => statuses.push(s),
    handlers: {
      match: (data, id) => received.push(['match', data, id]),
      standings: (data, id) => received.push(['standings', data, id]),
      goals: (data, id) => received.push(['goals', data, id]),
    },
    ...extra,
  });
  const advance = (ms) => {
    nowMs += ms;
    mock.timers.tick(ms);
  };
  const last = () => FakeEventSource.instances.at(-1);
  return { stream, synced, statuses, received, listeners, win, advance, last };
}

test('abre no cursor, entrega por tópico, guarda o último id e acerta o relógio no ping', (t) => {
  const s = setup();
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(4182);
  assert.equal(s.last().url, '/api/stream?after=4182');
  assert.equal(s.stream.status, STATUS.CONNECTING);
  s.last().open();
  s.last().emit('ping', { server_time: '2026-10-03T21:00:00Z' });
  s.last().emit('match', { stage_id: 3, match: { id: 12 } }, '4183');
  s.last().emit('goals', { date: '2026-10-03', changes: [], latest_goals: [] }, '4184');
  assert.deepEqual(s.synced, ['2026-10-03T21:00:00Z']);
  assert.deepEqual(s.received.map(([topic, , id]) => [topic, id]), [['match', 4183], ['goals', 4184]]);
  assert.equal(s.stream.lastId, 4184);
  assert.equal(s.stream.status, STATUS.LIVE);
});

test('45 s sem ping: fecha e recria com after = último id recebido', (t) => {
  const s = setup();
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(10);
  s.last().open();
  s.last().emit('standings', { stage_id: 3, standings: {} }, '11');
  const first = s.last();
  s.advance(PING_TIMEOUT - 5_000);
  assert.equal(FakeEventSource.instances.length, 1);
  s.advance(10_000);
  assert.equal(first.closed, true);
  assert.equal(FakeEventSource.instances.length, 2);
  assert.equal(s.last().url, '/api/stream?after=11');
  assert.equal(s.stream.status, STATUS.RECONNECTING);
  // mensagem do EventSource antigo depois de fechado não conta
  first.emit('match', { match: { id: 1 } }, '99');
  assert.equal(s.stream.lastId, 11);
});

test('o navegador desistiu (CLOSED): recria depois da espera', (t) => {
  const s = setup();
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(5);
  s.last().fail(true);
  assert.equal(s.stream.status, STATUS.RECONNECTING);
  assert.equal(FakeEventSource.instances.length, 1);
  s.advance(1_300); // primeira espera: 1 s ± 20%
  assert.equal(FakeEventSource.instances.length, 2);
  assert.equal(s.last().url, '/api/stream?after=5');
  // falha transitória (CONNECTING): o navegador tenta sozinho, sem recriar
  s.last().open();
  s.last().fail(false);
  s.advance(2_000);
  assert.equal(FakeEventSource.instances.length, 2);
});

test('volta ao primeiro plano: recria se a conexão não está comprovadamente viva', (t) => {
  const s = setup();
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(1);
  s.last().open();
  s.last().emit('ping', { server_time: '2026-10-03T21:00:00Z' });
  s.advance(3_000);
  s.listeners.visibilitychange();
  assert.equal(FakeEventSource.instances.length, 1, 'ping recente: mantém');
  s.win.document.visibilityState = 'hidden';
  s.advance(30_000);
  s.listeners.visibilitychange();
  assert.equal(FakeEventSource.instances.length, 1, 'escondida: não mexe');
  s.win.document.visibilityState = 'visible';
  s.listeners.visibilitychange();
  assert.equal(FakeEventSource.instances.length, 2);
  assert.equal(s.last().url, '/api/stream?after=1');
});

test('mais de 5 min sem stream: busca o estado inteiro e reabre no cursor novo', async (t) => {
  let stale = 0;
  const s = setup({ onStale: async () => { stale += 1; return 9000; } });
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(100);
  s.last().open();
  s.last().emit('match', { match: { id: 1 } }, '101');
  // celular em segundo plano: os timers param e nada chega por 6 minutos
  s.win.document.visibilityState = 'hidden';
  s.advance(6 * 60_000);
  s.win.document.visibilityState = 'visible';
  const before = FakeEventSource.instances.length;
  s.listeners.visibilitychange();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stale, 1);
  assert.equal(FakeEventSource.instances.at(before - 1).closed, true);
  assert.equal(s.last().url, '/api/stream?after=9000');
  assert.equal(s.stream.lastId, 9000);
});

test('sem onStale, 5 min sem stream ainda pede reenvio', (t) => {
  const s = setup();
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  s.stream.start(7);
  s.last().fail(true);
  s.advance(STALE_AFTER + 60_000);
  assert.ok(FakeEventSource.instances.length >= 2);
  assert.equal(s.last().url, '/api/stream?after=7');
});

test('stop fecha a conexão e para de recriar', (t) => {
  const s = setup();
  t.after(() => mock.timers.reset());
  s.stream.start(1);
  const es = s.last();
  s.stream.stop();
  assert.equal(es.closed, true);
  assert.equal(s.stream.status, STATUS.STOPPED);
  s.advance(10 * 60_000);
  assert.equal(FakeEventSource.instances.length, 1);
});

test('busca do estado que falha (sem rede) tenta de novo com espera crescente', async (t) => {
  let calls = 0;
  let offline = true;
  const s = setup({ onStale: async () => { calls += 1; if (offline) throw new Error('offline'); return 50; } });
  t.after(() => { s.stream.stop(); mock.timers.reset(); });
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  s.stream.start(1);
  s.last().fail(true); // o servidor caiu: as recriações não abrem
  s.advance(STALE_AFTER + 5_000); // passou de 5 min sem stream
  await flush();
  assert.equal(calls, 1);
  // 40 s de checagens a cada 5 s: sem espera seriam 8 buscas; com 1 s, 2 s, 4 s, 8 s… bem menos
  for (let i = 0; i < 8; i += 1) {
    s.advance(5_000);
    await flush();
  }
  assert.ok(calls >= 2 && calls <= 6, `buscas: ${calls}`);
  offline = false;
  s.advance(60_000);
  await flush();
  assert.equal(s.last().url, '/api/stream?after=50');
});
