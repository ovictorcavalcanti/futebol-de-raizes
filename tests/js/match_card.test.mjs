// Partes puras do card de jogo (sem DOM): node --test tests/js/match_card.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeHref, annulledGoalNote, sortEventsByClock } from '../../static/js/match-card.js';

test('links de transmissão: só http(s); javascript:, data: e lixo viram null', () => {
  assert.equal(safeHref('https://tv.example.com/ao-vivo'), 'https://tv.example.com/ao-vivo');
  assert.equal(safeHref('http://radio.example.com'), 'http://radio.example.com/');
  assert.equal(safeHref('javascript:alert(1)'), null);
  assert.equal(safeHref(' JavaScript:alert(1)'), null);
  assert.equal(safeHref('data:text/html,<b>x</b>'), null);
  assert.equal(safeHref(''), null);
  assert.equal(safeHref(null), null);
});

test('gol anulado na linha do tempo: dado factual, sem o "Oxe!" (IDENTIDADE §5)', () => {
  const note = annulledGoalNote({ minute_label: "60'", payload: { reason: 'Impedimento (VAR)' } });
  assert.equal(note, "Gol anulado aos 60' — Impedimento (VAR)");
  assert.equal(annulledGoalNote({ minute_label: "45+2'" }), "Gol anulado aos 45+2'");
  assert.equal(annulledGoalNote(null), 'Gol anulado');
  for (const text of [note, annulledGoalNote(null)]) assert.doesNotMatch(text, /Oxe/);
});

test('sortEventsByClock: ordem do jogo, não a do lançamento', () => {
  const ev = (sequence, period, minute, stoppage = null) => ({ id: sequence, sequence, period, minute, stoppage });
  const events = [
    ev(1, 'first_half', 0), ev(2, 'first_half', 40), ev(3, 'first_half', 30), ev(4, 'first_half', 45, 2),
    ev(5, 'half_time', null), ev(6, 'second_half', 45), ev(7, 'second_half', null), ev(8, 'second_half', 46),
  ];
  assert.deepEqual(sortEventsByClock(events.slice().reverse()).map((e) => e.id), [1, 3, 2, 4, 5, 6, 7, 8]);
  assert.deepEqual(sortEventsByClock(events, { desc: true }).map((e) => e.id), [8, 7, 6, 5, 4, 2, 3, 1]);
});
