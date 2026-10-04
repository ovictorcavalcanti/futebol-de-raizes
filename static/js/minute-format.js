/**
 * Formato do minuto nas páginas públicas. Padrão: o internacional dos registros
 * ("72'", "45+2'", "90+3'"). Opcional: por tempo ("27' 2T", "47' 1T", "48' 2T").
 * A escolha é do visitante, fica no aparelho e vale em todas as abas; o operador
 * lança sempre no formato internacional.
 */

export const MINUTE_FORMAT_KEY = 'fdr-minute-format';

// período → [minuto em que o tempo começa, rótulo]; o intervalo conta como o fim do tempo anterior
const HALVES = {
  first_half: [0, '1T'],
  half_time: [0, '1T'],
  second_half: [45, '2T'],
  extra_time: [90, '1TP'],
  extra_half_time: [90, '1TP'],
  extra_second_half: [105, '2TP'],
};

function readSaved() {
  try {
    return localStorage.getItem(MINUTE_FORMAT_KEY) === 'half' ? 'half' : 'intl';
  } catch {
    return 'intl';
  }
}

let current = readSaved();

/** @returns {'intl'|'half'} */
export function getMinuteFormat() {
  return current;
}

/**
 * Minuto absoluto (+ acréscimo) no formato pedido. "half": minutos desde o início do
 * tempo, acréscimo somado (45+2 → "47' 1T"; 59 → "14' 2T"). Sem tempo de relógio
 * (ex.: pênaltis) → formato internacional.
 */
export function formatMinute(minute, stoppage, period, format = current) {
  if (minute == null) return '';
  const half = HALVES[period];
  if (format !== 'half' || !half) return stoppage ? `${minute}+${stoppage}'` : `${minute}'`;
  return `${minute - half[0] + (stoppage || 0)}' ${half[1]}`;
}

/** Rótulo do minuto de um lance/gol ({minute, stoppage, period, minute_label}). */
export function minuteLabel(item, format = current) {
  if (!item) return '';
  if (format !== 'half') return item.minute_label ?? formatMinute(item.minute, item.stoppage, item.period, 'intl');
  return formatMinute(item.minute, item.stoppage, item.period, 'half') || item.minute_label || '';
}

function paintToggles() {
  for (const btn of document.querySelectorAll('[data-minute-format-toggle]')) {
    btn.setAttribute('aria-pressed', String(current === 'half'));
    btn.title = current === 'half' ? "Ver minutos no formato internacional (45+2')" : "Ver minutos por tempo (47' 1T)";
  }
}

/** Troca o formato, guarda a escolha e avisa as páginas (evento `minuteformatchange`). */
export function setMinuteFormat(format, { persist = true } = {}) {
  current = format === 'half' ? 'half' : 'intl';
  if (persist) {
    try {
      localStorage.setItem(MINUTE_FORMAT_KEY, current);
    } catch {
      /* armazenamento bloqueado: vale só nesta página */
    }
  }
  paintToggles();
  document.dispatchEvent(new CustomEvent('minuteformatchange', { detail: { format: current } }));
}

function init() {
  if (typeof document === 'undefined' || document.documentElement.dataset.minuteFormatReady) return;
  document.documentElement.dataset.minuteFormatReady = '1';
  const ready = () => paintToggles();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true });
  else ready();
  document.addEventListener('click', (event) => {
    const btn = event.target instanceof Element ? event.target.closest('[data-minute-format-toggle]') : null;
    if (btn) setMinuteFormat(current === 'half' ? 'intl' : 'half');
  });
  window.addEventListener('storage', (event) => {
    if (event.key === MINUTE_FORMAT_KEY) setMinuteFormat(event.newValue, { persist: false });
  });
}

init();
