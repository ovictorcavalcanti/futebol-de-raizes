// Partes puras do card de jogo (sem DOM): node --test tests/js/match_card.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeHref } from '../../static/js/match-card.js';

test('links de transmissão: só http(s); javascript:, data: e lixo viram null', () => {
  assert.equal(safeHref('https://tv.example.com/ao-vivo'), 'https://tv.example.com/ao-vivo');
  assert.equal(safeHref('http://radio.example.com'), 'http://radio.example.com/');
  assert.equal(safeHref('javascript:alert(1)'), null);
  assert.equal(safeHref(' JavaScript:alert(1)'), null);
  assert.equal(safeHref('data:text/html,<b>x</b>'), null);
  assert.equal(safeHref(''), null);
  assert.equal(safeHref(null), null);
});
