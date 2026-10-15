// Partes puras da classificação (sem DOM): node --test tests/js/standings.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { adjustmentLabel, adjustmentNote, adjustmentTitle } from '../../static/js/standings.js';
import { IMAGE_COLUMNS, performance, cellText, imageHeading, imageFileName, stampText } from '../../static/js/standings-image.js';

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

// imagem da classificação (standings-image.js)
test('imagem: colunas #, Time, P, J, V, E, D, GP, GC, SG e %', () => {
  assert.deepEqual(IMAGE_COLUMNS.map((c) => c.label), ['P', 'J', 'V', 'E', 'D', 'GP', 'GC', 'SG', '%']);
  assert.equal(IMAGE_COLUMNS.at(-1).title, 'Aproveitamento');
});

test('aproveitamento com uma casa: pontos ganhos em campo sobre os disputados, sem a punição', () => {
  assert.equal(performance({ points: 7, played: 3 }, { win: 3 }), 77.8);
  assert.equal(performance({ points: 4, played: 3, points_adjustment: -3 }, { win: 3 }), 77.8);
  assert.equal(performance({ points: 0, played: 0 }, { win: 3 }), 0);
  assert.equal(performance({ points: 4, played: 2 }, { win: 2 }), 100);
  assert.equal(cellText({ points: 7, played: 3 }, 'performance', { win: 3 }), '77,8');
  assert.equal(cellText({ points: 9, played: 3 }, 'performance', { win: 3 }), '100,0');
  assert.equal(cellText({ points: 0, played: 3 }, 'performance', { win: 3 }), '0,0');
  assert.equal(cellText({ goal_difference: 3 }, 'goal_difference'), '+3');
  assert.equal(cellText({ goal_difference: -2 }, 'goal_difference'), '-2');
});

test('imagem: título com competição e temporada; fase e grupo na linha de baixo', () => {
  const standings = { stage_name: '1ª fase' };
  assert.deepEqual(imageHeading(standings, { name: 'Grupo A' }, { competition: 'Copa Pernambuco', season: '2026' }),
    { title: 'Copa Pernambuco 2026', sub: '1ª fase · Grupo A' });
  // grupo único ("Tabela") e classificação geral (grupo com o nome dela) não repetem nada
  assert.equal(imageHeading(standings, { name: 'Tabela' }, { competition: 'Copa' }).sub, '1ª fase');
  assert.equal(imageHeading({ stage_name: 'Geral' }, { name: 'Geral' }, { competition: 'Copa' }).sub, 'Geral');
  assert.equal(imageFileName({ title: 'Copa Pernambuco 2026', sub: '1ª fase · Grupo A' }), 'classificacao-copa-pernambuco-2026-1a-fase-grupo-a.png');
});

test('imagem: data e hora de Brasília', () => {
  assert.equal(stampText(new Date('2026-10-16T01:30:00Z')), 'Atualizada em 15/10/2026 às 22:30');
});
