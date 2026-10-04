// Partes puras do card de jogo (sem DOM): node --test tests/js/match_card.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeHref, annulledGoalNote, sortEventsByClock, sortMatchesForDisplay, groupMatchesByGroup } from '../../static/js/match-card.js';

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

test('sortMatchesForDisplay: ao vivo > encerrados > agendados; sem ao vivo, agendados > encerrados', () => {
  const m = (id, status, hour) => ({ id, status, kickoff_at: `2026-10-04T${String(hour).padStart(2, '0')}:00:00Z` });
  const withLive = [m(1, 'scheduled', 20), m(2, 'finished', 16), m(3, 'live', 18), m(4, 'finished', 14), m(5, 'postponed', 10)];
  assert.deepEqual(sortMatchesForDisplay(withLive).map((x) => x.id), [3, 4, 2, 1, 5]);
  const noLive = [m(1, 'scheduled', 20), m(2, 'finished', 16), m(4, 'finished', 14), m(6, 'scheduled', 19)];
  assert.deepEqual(sortMatchesForDisplay(noLive).map((x) => x.id), [6, 1, 4, 2]);
  assert.deepEqual(sortMatchesForDisplay([m(7, 'delayed', 21), m(2, 'finished', 16)]).map((x) => x.id), [7, 2]); // atrasado conta como ao vivo
});

test('groupMatchesByGroup: um bloco por grupo, pela ordem do nome, cada um na ordem da home', () => {
  const m = (id, group, status, hour) => ({ id, group, status, kickoff_at: `2026-10-04T${String(hour).padStart(2, '0')}:00:00Z` });
  const a = { id: 1, name: 'Grupo A' };
  const b = { id: 2, name: 'Grupo B' };
  const g10 = { id: 3, name: 'Grupo 10' };
  const g2 = { id: 4, name: 'Grupo 2' };
  const out = groupMatchesByGroup([m(1, b, 'scheduled', 20), m(2, a, 'finished', 16), m(3, b, 'live', 18), m(4, a, 'scheduled', 19)]);
  assert.deepEqual(out.map((g) => g.group.name), ['Grupo A', 'Grupo B']);
  assert.deepEqual(out.map((g) => g.matches.map((x) => x.id)), [[4, 2], [3, 1]]);
  assert.deepEqual(groupMatchesByGroup([m(1, g10, 'scheduled', 20), m(2, g2, 'scheduled', 20)]).map((g) => g.group.name), ['Grupo 2', 'Grupo 10']);
});
