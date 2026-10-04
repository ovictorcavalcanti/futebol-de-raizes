// Testes dos módulos puros do front (sem DOM): node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  formatTime, formatClock, formatDate, dayKey, relativeDay, formatWhen, formatMoney, formatInt, minuteLabel, dayDiff,
} from '../../static/js/format.js';
import { ServerClock, liveMinute, liveMinuteLabel } from '../../static/js/clock.js';

const NBSP = / /g;

test('horários sempre em Brasília, qualquer que seja o fuso do aparelho', () => {
  // 19:30Z = 16:30 em Brasília (UTC-3, sem horário de verão)
  assert.equal(formatTime('2026-10-03T19:30:00Z'), '16:30');
  assert.equal(formatClock('2026-10-03T19:30:05Z'), '16:30:05');
  assert.equal(formatDate('2026-10-03T19:30:00Z'), 'sáb, 03/10');
  // 01:00Z do dia 4 ainda é dia 3 em Brasília
  assert.equal(dayKey('2026-10-04T01:00:00Z'), '2026-10-03');
});

test('dia relativo calculado em Brasília', () => {
  const now = Date.parse('2026-10-03T23:30:00Z'); // 20:30 de sáb em Brasília
  assert.equal(relativeDay('2026-10-04T02:00:00Z', now), 'Hoje'); // 23:00 de sábado
  assert.equal(relativeDay('2026-10-04T12:00:00Z', now), 'Amanhã');
  assert.equal(relativeDay('2026-10-02T12:00:00Z', now), 'Ontem');
  assert.equal(relativeDay('2026-10-10T12:00:00Z', now), null);
  assert.equal(dayDiff('2026-10-10T12:00:00Z', now), 7);
  assert.equal(formatWhen('2026-10-03T19:30:00Z', now), 'Hoje · 16:30');
  assert.equal(formatWhen('2026-10-07T19:30:00Z', now), 'qua, 07/10 · 16:30');
});

test('números e dinheiro em pt-BR', () => {
  assert.equal(formatInt(42318), '42.318');
  assert.equal(formatMoney(234155000).replace(NBSP, ' '), 'R$ 2.341.550,00');
  assert.equal(formatMoney(null), '');
  assert.equal(minuteLabel(45, 2), "45+2'");
  assert.equal(minuteLabel(72), "72'");
});

test('ServerClock usa o maior dos 5 últimos desvios', () => {
  const clock = new ServerClock();
  const local = 1_000_000;
  clock.sync(new Date(local + 500).toISOString(), local); // +500
  clock.sync(new Date(local + 1200).toISOString(), local); // +1200 (menos atraso de rede)
  clock.sync(new Date(local + 300).toISOString(), local); // +300
  assert.equal(clock.offset, 1200);
  for (let i = 0; i < 5; i++) clock.sync(new Date(local + 100).toISOString(), local);
  assert.equal(clock.offset, 100, 'amostras antigas saem da janela de 5');
  assert.equal(clock.sync('lixo'), null);
  assert.ok(Math.abs(clock.nowMs() - (Date.now() + 100)) < 50);
});

test('ServerClock avisa a virada do dia em Brasília', () => {
  const clock = new ServerClock();
  const realNow = Date.now;
  try {
    let fake = Date.parse('2026-10-04T02:59:59Z'); // 23:59:59 em Brasília
    Date.now = () => fake;
    const calls = [];
    const off = clock.onDayChange((day, prev) => calls.push([day, prev]));
    clock.checkDay();
    assert.deepEqual(calls, []);
    fake += 2000;
    clock.checkDay();
    assert.deepEqual(calls, [['2026-10-04', '2026-10-03']]);
    off();
  } finally {
    Date.now = realNow;
    clock.stop();
  }
});

const liveMatch = (over = {}) => ({
  status: 'live', period: 'second_half', period_short: '2T', period_started_at: '2026-10-03T20:35:00Z',
  clock: { running: true, offset: 45, regular_end: 90, stoppage_announced: 4 }, ...over,
});

test('minuto ao vivo segue o relógio do contrato', () => {
  const start = Date.parse('2026-10-03T20:35:00Z');
  assert.equal(liveMinuteLabel(liveMatch(), start + 10_000), "46'");
  assert.equal(liveMinuteLabel(liveMatch(), start + 26.5 * 60_000), "72'");
  assert.equal(liveMinuteLabel(liveMatch(), start + 46 * 60_000), "90+2'");
  assert.deepEqual(liveMinute(liveMatch(), start + 46 * 60_000), { minute: 90, stoppage: 2 });
  const firstHalf = liveMatch({ period: 'first_half', period_short: '1T', clock: { running: true, offset: 0, regular_end: 45 } });
  assert.equal(liveMinuteLabel(firstHalf, start + 46 * 60_000 + 30_000), "45+2'");
  assert.equal(liveMinuteLabel(liveMatch(), start - 30_000), "46'", 'relógio do aparelho atrasado não dá minuto negativo');
});

test('rótulos especiais: intervalo, pênaltis, suspenso e fora do ar', () => {
  assert.equal(liveMinuteLabel(liveMatch({ period: 'half_time', clock: null })), 'INT');
  assert.equal(liveMinuteLabel(liveMatch({ period: 'extra_half_time', clock: null })), 'INT');
  assert.equal(liveMinuteLabel(liveMatch({ period: 'penalties', clock: null })), 'PÊN');
  assert.equal(liveMinuteLabel(liveMatch({ clock: { running: false, offset: 45, regular_end: 90 } })), '2T');
  assert.equal(liveMinuteLabel({ status: 'finished', period: 'second_half' }), '');
  assert.equal(liveMinute(liveMatch({ clock: null })), null);
});

test('ServerClock: stop() + start() dentro do tick não deixa dois laços rodando', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
  const clock = new ServerClock();
  let ticks = 0;
  let restarted = false;
  clock.onTick(() => {
    ticks += 1;
    if (!restarted) {
      restarted = true;
      clock.stop(); // ex.: a página pausa e retoma o relógio dentro do callback
      clock.start();
    }
  });
  for (let i = 0; i < 100; i++) t.mock.timers.tick(100);
  clock.stop();
  // 10 s ⇒ ~10 ticks; com o laço duplicado seriam ~19
  assert.ok(ticks >= 9 && ticks <= 11, `ticks=${ticks}`);
});

test('formato do minuto: internacional por padrão, por tempo quando escolhido', async () => {
  const { formatMinute, minuteLabel, getMinuteFormat } = await import('../../static/js/minute-format.js');
  assert.equal(getMinuteFormat(), 'intl');
  const cases = [
    [21, null, 'first_half', "21'", "21' 1T"],
    [45, 2, 'first_half', "45+2'", "47' 1T"],
    [59, null, 'second_half', "59'", "14' 2T"],
    [90, 3, 'second_half', "90+3'", "48' 2T"],
    [95, null, 'extra_time', "95'", "5' 1TP"],
    [120, 1, 'extra_second_half', "120+1'", "16' 2TP"],
    [45, 1, 'half_time', "45+1'", "46' 1T"],
    [120, null, 'penalties', "120'", "120'"],
  ];
  for (const [minute, stoppage, period, intl, half] of cases) {
    assert.equal(formatMinute(minute, stoppage, period, 'intl'), intl);
    assert.equal(formatMinute(minute, stoppage, period, 'half'), half);
  }
  const goal = { minute: 90, stoppage: 3, period: 'second_half', minute_label: "90+3'" };
  assert.equal(minuteLabel(goal), "90+3'");
  assert.equal(minuteLabel(goal, 'half'), "48' 2T");
  const live = liveMatch({ period: 'second_half', clock: { running: true, offset: 45, regular_end: 90 }, period_started_at: new Date(Date.now() - 26.5 * 60_000).toISOString() });
  assert.equal(liveMinuteLabel(live), "72'");
  assert.equal(liveMinuteLabel(live, Date.now(), 'half'), "27' 2T");
});
