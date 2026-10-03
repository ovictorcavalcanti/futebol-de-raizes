// Regras dos alertas de gol (sem DOM): node --test tests/js/alerts.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GoalAlertTracker, isRecent, notificationSupport, isMobileDevice, notificationText, ALERT_WINDOW } from '../../static/js/alerts.js';

const SERVER_NOW = Date.parse('2026-10-03T21:15:00Z');
const at = (msBefore) => new Date(SERVER_NOW - msBefore).toISOString();
const goal = (id, msBefore = 10_000, extra = {}) => ({
  event_id: id,
  match_id: 12,
  team_side: 'home',
  player: 'Zé Roberto',
  minute_label: "72'",
  score_after: { home: 2, away: 1 },
  created_at: at(msBefore),
  match: { id: 12, home: { name: 'Sport Club do Recife', short_name: 'SPT' }, away: { name: 'Clube Náutico Capibaribe', short_name: 'NAU' } },
  team: { name: 'Sport Club do Recife', short_name: 'SPT' },
  ...extra,
});
const actions = (decisions) => decisions.map((d) => d.action);

test('gols da lista inicial contam como já alertados', () => {
  const tracker = new GoalAlertTracker([goal(1), goal(2)]);
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: goal(1) }, { kind: 'added', goal: goal(3) }], SERVER_NOW)), ['skip', 'alert']);
});

test('cada gol alerta uma vez só (pelo id do evento)', () => {
  const tracker = new GoalAlertTracker();
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: goal(7) }], SERVER_NOW)), ['alert']);
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: goal(7) }], SERVER_NOW)), ['skip']);
  assert.equal(tracker.hasSeen(7), true);
});

test('gol lançado há mais de 2 minutos (hora do servidor) entra sem alerta', () => {
  const tracker = new GoalAlertTracker();
  const decisions = tracker.decide([
    { kind: 'added', goal: goal(10, ALERT_WINDOW) }, // exatamente 2 min: ainda alerta
    { kind: 'added', goal: goal(11, ALERT_WINDOW + 1) },
    { kind: 'added', goal: goal(12, -5_000) }, // relógio do servidor um pouco atrás do lançamento
    { kind: 'added', goal: goal(13, 0, { created_at: 'lixo' }) },
  ], SERVER_NOW);
  assert.deepEqual(actions(decisions), ['alert', 'silent', 'alert', 'silent']);
  // a regra usa o relógio do servidor que a página passa, não o do aparelho
  assert.equal(isRecent(goal(14, 60_000), SERVER_NOW), true);
  assert.equal(isRecent(goal(14, 60_000), SERVER_NOW + 3 * 60_000), false);
});

test('gol anulado ou cancelado gera correção (uma vez) e a volta não alerta', () => {
  const tracker = new GoalAlertTracker();
  tracker.decide([{ kind: 'added', goal: goal(20) }], SERVER_NOW);
  const removed = tracker.decide([{ kind: 'removed', reason: 'voided', goal: goal(20) }], SERVER_NOW);
  assert.deepEqual(removed.map((d) => [d.action, d.reason]), [['correction', 'voided']]);
  assert.deepEqual(actions(tracker.decide([{ kind: 'removed', reason: 'voided', goal: goal(20) }], SERVER_NOW)), ['skip']);
  assert.deepEqual(actions(tracker.decide([{ kind: 'restored', reason: 'unvoided', goal: goal(20) }], SERVER_NOW)), ['restore']);
  // a volta não reabre o alerta, nem se a mensagem vier como "added"
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: goal(20) }], SERVER_NOW)), ['skip']);
  // anulado de novo depois de voltar: nova correção
  assert.deepEqual(actions(tracker.decide([{ kind: 'removed', reason: 'annulled', goal: goal(20) }], SERVER_NOW)), ['correction']);
});

test('correção de gol da lista inicial e motivo padrão', () => {
  const tracker = new GoalAlertTracker([goal(30)]);
  const [d] = tracker.decide([{ kind: 'removed', reason: null, goal: goal(30) }], SERVER_NOW);
  assert.equal(d.action, 'correction');
  assert.equal(d.reason, 'annulled');
});

test('mudanças sem gol ou de tipo desconhecido são ignoradas', () => {
  const tracker = new GoalAlertTracker();
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: {} }, { kind: 'moved', goal: goal(40) }], SERVER_NOW)), ['skip', 'skip']);
  assert.deepEqual(tracker.decide(undefined, SERVER_NOW), []);
});

test('reset da lista (virada do dia, volta depois de 5 min) não alerta', () => {
  const tracker = new GoalAlertTracker();
  tracker.markSeen([goal(50), goal(51)]);
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: goal(51) }, { kind: 'added', goal: goal(52) }], SERVER_NOW)), ['skip', 'alert']);
});

/* --- Suporte a notificações ------------------------------------------------------------------ */

function fakeWindow({ permission = 'default', throws = false, ua = 'Mozilla/5.0 (X11; Linux x86_64) Chrome/140', mobile, secure = true, touch = false } = {}) {
  const created = [];
  function Notification(title, opts) {
    if (throws) throw new TypeError('Illegal constructor');
    const n = { title, opts, closed: false, close() { this.closed = true; } };
    created.push(n);
    return n;
  }
  Notification.permission = permission;
  const win = { Notification, isSecureContext: secure, navigator: { userAgent: ua, maxTouchPoints: touch ? 5 : 0 } };
  if (mobile !== undefined) win.navigator.userAgentData = { mobile };
  if (touch) win.ontouchstart = null;
  return { win, created };
}

test('notificações: só onde new Notification() funciona (computador, HTTPS)', () => {
  const desktop = fakeWindow();
  assert.deepEqual(notificationSupport(desktop.win), { available: true, reason: null });
  assert.equal(desktop.created.length, 1, 'testa o construtor sem permissão');
  assert.equal(desktop.created[0].closed, true);

  const granted = fakeWindow({ permission: 'granted' });
  assert.equal(notificationSupport(granted.win).available, true);
  assert.equal(granted.created.length, 0, 'com permissão não cria notificação de teste');

  assert.deepEqual(notificationSupport({ navigator: {} }), { available: false, reason: 'unsupported' });
  assert.equal(notificationSupport(fakeWindow({ secure: false }).win).reason, 'insecure');
  assert.equal(notificationSupport({ isSecureContext: false, navigator: { userAgent: 'Chrome' } }).reason, 'insecure');
  assert.equal(notificationSupport(fakeWindow({ secure: false, mobile: true }).win).reason, 'mobile');
  assert.equal(notificationSupport(fakeWindow({ throws: true }).win).reason, 'mobile');
  assert.equal(notificationSupport(fakeWindow({ mobile: true }).win).reason, 'mobile');
  assert.equal(notificationSupport(fakeWindow({ ua: 'Mozilla/5.0 (Linux; Android 14) Mobile Safari' }).win).reason, 'mobile');
});

test('celular: userAgentData.mobile, user agent e iPadOS que se diz Mac', () => {
  assert.equal(isMobileDevice(fakeWindow({ mobile: true }).win), true);
  assert.equal(isMobileDevice(fakeWindow({ ua: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)' }).win), true);
  assert.equal(isMobileDevice(fakeWindow({ ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', touch: true }).win), true);
  assert.equal(isMobileDevice(fakeWindow({ ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)' }).win), false);
  // notebook com tela de toque continua computador
  assert.equal(isMobileDevice(fakeWindow({ ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140', mobile: false, touch: true }).win), false);
});

test('texto da notificação: gol e correção', () => {
  const g = goal(60);
  assert.deepEqual(notificationText(g), { title: 'É gol! SPT 2 × 1 NAU', body: "Zé Roberto (72') · Sport Club do Recife" });
  const fix = notificationText(g, { kind: 'correction', reason: 'annulled' });
  assert.equal(fix.title, 'Oxe! Gol anulado.');
  assert.match(fix.body, /Zé Roberto \(72'\).*não vale mais\. Lance corrigido pelo operador\./);
  assert.equal(notificationText(g, { kind: 'correction', reason: 'voided' }).title, 'Lance corrigido.');
});

test('só alerta gol de jogo que está na home (correção antiga de outro dia não alerta)', () => {
  const tracker = new GoalAlertTracker();
  const onHome = (g) => g.match_id === 12;
  const old = goal(70, 1_000, { match_id: 99 });
  assert.deepEqual(actions(tracker.decide([{ kind: 'added', goal: old }, { kind: 'added', goal: goal(71) }], SERVER_NOW, { isRelevant: onHome })), ['skip', 'alert']);
  assert.deepEqual(actions(tracker.decide([{ kind: 'removed', reason: 'voided', goal: old }], SERVER_NOW, { isRelevant: onHome })), ['skip']);
  assert.deepEqual(actions(tracker.decide([{ kind: 'removed', reason: 'voided', goal: goal(71) }], SERVER_NOW, { isRelevant: onHome })), ['correction']);
});
