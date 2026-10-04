// Partes puras da classificação (sem DOM): node --test tests/js/standings.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { adjustmentLabel, adjustmentNote, adjustmentTitle } from '../../static/js/standings.js';

test('pontos do ajuste com sinal tipográfico: −3 (punição) e +2 (bonificação)', () => {
  assert.equal(adjustmentLabel(-3), '−3');
  assert.equal(adjustmentLabel(2), '+2');
  assert.equal(adjustmentLabel('-1'), '−1');
});

test('nota da legenda: time, pontos e motivo', () => {
  const item = { team: { name: 'Santa Cruz', short_name: 'SCZ' }, points: -3, reason: 'escalação irregular' };
  assert.equal(adjustmentNote(item), 'Santa Cruz: −3 pts — escalação irregular');
  assert.equal(adjustmentNote({ team: { short_name: 'IBI' }, points: 1 }), 'IBI: +1 pts');
  assert.equal(adjustmentNote(null), 'Time: +0 pts');
});

test('título acessível da marca "*" nos pontos', () => {
  assert.equal(adjustmentTitle(-3), 'Punição: perdeu 3 pontos fora de campo');
  assert.equal(adjustmentTitle(-1), 'Punição: perdeu 1 ponto fora de campo');
  assert.equal(adjustmentTitle(2), 'Bonificação: ganhou 2 pontos fora de campo');
});
