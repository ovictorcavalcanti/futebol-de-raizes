/**
 * Formatação de datas, horas e números — sempre no horário de Brasília,
 * para qualquer visitante, em qualquer fuso (Intl com timeZone fixo).
 */

export const TIME_ZONE = 'America/Sao_Paulo';
export const LOCALE = 'pt-BR';

const dtf = (opts, locale = LOCALE) => new Intl.DateTimeFormat(locale, { timeZone: TIME_ZONE, ...opts });

const F_TIME = dtf({ hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const F_CLOCK = dtf({ hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const F_WEEKDAY = dtf({ weekday: 'short' });
const F_DAY_MONTH = dtf({ day: '2-digit', month: '2-digit' });
const F_LONG = dtf({ weekday: 'long', day: 'numeric', month: 'long' });
const F_KEY = dtf({ year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA'); // AAAA-MM-DD
const N_INT = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const N_BRL = new Intl.NumberFormat(LOCALE, { style: 'currency', currency: 'BRL' });

const DAY_MS = 86_400_000;

/**
 * Converte Date | número (ms) | string ISO em Date; inválido → null.
 * @returns {Date|null}
 */
export function toDate(value) {
  if (value == null || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "16:30" */
export function formatTime(value) {
  const d = toDate(value);
  return d ? F_TIME.format(d) : '';
}

/** "16:30:05" */
export function formatClock(value) {
  const d = toDate(value);
  return d ? F_CLOCK.format(d) : '';
}

/** "sáb, 03/10" */
export function formatDate(value) {
  const d = toDate(value);
  if (!d) return '';
  const weekday = F_WEEKDAY.format(d).replace(/\.$/, '');
  return `${weekday}, ${F_DAY_MONTH.format(d)}`;
}

/** "sábado, 3 de outubro" */
export function formatDateLong(value) {
  const d = toDate(value);
  return d ? F_LONG.format(d) : '';
}

/** Dia no horário de Brasília, "AAAA-MM-DD" (o mesmo formato do filtro ?date= da API). */
export function dayKey(value) {
  const d = toDate(value);
  return d ? F_KEY.format(d) : '';
}

/** Diferença em dias de calendário (Brasília) entre value e now: 0 hoje, 1 amanhã, -1 ontem. */
export function dayDiff(value, now = Date.now()) {
  const a = dayKey(value);
  const b = dayKey(now);
  if (!a || !b) return null;
  return Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / DAY_MS);
}

/** "Hoje" | "Amanhã" | "Ontem" | null */
export function relativeDay(value, now = Date.now()) {
  const diff = dayDiff(value, now);
  return { 0: 'Hoje', 1: 'Amanhã', [-1]: 'Ontem' }[diff] ?? null;
}

/** "Hoje" ou "sáb, 03/10" */
export function formatDay(value, now = Date.now()) {
  return relativeDay(value, now) ?? formatDate(value);
}

/** "Hoje · 16:30" ou "sáb, 03/10 · 16:30" */
export function formatWhen(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '';
  return `${formatDay(d, now)} · ${formatTime(d)}`;
}

/** Inteiro com separador pt-BR: 42318 → "42.318" */
export function formatInt(n) {
  return n == null || Number.isNaN(Number(n)) ? '' : N_INT.format(Number(n));
}

/** Dinheiro em centavos → "R$ 2.341.550,00" */
export function formatMoney(cents) {
  return cents == null || Number.isNaN(Number(cents)) ? '' : N_BRL.format(Number(cents) / 100);
}

/** Placar "2 × 1" */
export function formatScore(home, away) {
  return `${home ?? 0} × ${away ?? 0}`;
}

/** Minuto do lance: (45, 2) → "45+2'" ; (72) → "72'" */
export function minuteLabel(minute, stoppage = null) {
  if (minute == null) return '';
  return stoppage ? `${minute}+${stoppage}'` : `${minute}'`;
}

/** Ordinal masculino: 1 → "1º" */
export function ordinal(n) {
  return `${n}º`;
}
