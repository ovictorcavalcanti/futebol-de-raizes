/**
 * Textos da classificação (puros, sem DOM): colunas, faixa de posição da legenda e notas de
 * punição. Usados pela tabela (standings.js) e pela imagem (standings-image.js), num módulo à
 * parte para os dois não se importarem um ao outro (o collectstatic não fecha o hash de um ciclo).
 */
export const COLUMNS = [
  { key: 'points', label: 'P', title: 'Pontos', cls: 'col-pts' },
  { key: 'played', label: 'J', title: 'Jogos', cls: 'col-j' },
  { key: 'won', label: 'V', title: 'Vitórias', cls: 'col-w' },
  { key: 'drawn', label: 'E', title: 'Empates', cls: 'col-d' },
  { key: 'lost', label: 'D', title: 'Derrotas', cls: 'col-l' },
  { key: 'goals_for', label: 'GP', title: 'Gols pró', cls: 'col-gp' },
  { key: 'goals_against', label: 'GC', title: 'Gols contra', cls: 'col-gc' },
  { key: 'goal_difference', label: 'SG', title: 'Saldo de gols', cls: 'col-sg' },
];

/** Pontos de um ajuste com o sinal tipográfico: −3, +2. */
export function adjustmentLabel(points) {
  const n = Number(points) || 0;
  return n < 0 ? `−${Math.abs(n)}` : `+${n}`;
}

/** Linha da nota de punições: "Santa Cruz: −3 pts — escalação irregular". */
export function adjustmentNote(item) {
  const name = item?.team?.name || item?.team?.short_name || 'Time';
  const reason = item?.reason ? ` — ${item.reason}` : '';
  return `${name}: ${adjustmentLabel(item?.points)} pts${reason}`;
}

/** Texto do título (e do leitor de tela) da marca "*" nos pontos ajustados. */
export function adjustmentTitle(points) {
  const n = Number(points) || 0;
  const amount = Math.abs(n) === 1 ? '1 ponto' : `${Math.abs(n)} pontos`;
  return n < 0 ? `Punição: perdeu ${amount} fora de campo` : `Bonificação: ganhou ${amount} fora de campo`;
}

/** Faixa de posição de uma zona da legenda: "1º–4º", "8º". */
export function rangeLabel(item) {
  if (item.from == null) return '';
  return item.to != null && item.to !== item.from ? `${item.from}º–${item.to}º` : `${item.from}º`;
}
