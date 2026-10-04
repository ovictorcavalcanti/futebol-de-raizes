/**
 * Relógio do servidor e minuto ao vivo.
 *
 * A hora vem do servidor, não do aparelho: offset = server_time − Date.now().
 * Guardamos os 5 últimos desvios e usamos o MAIOR — é o da mensagem que menos
 * demorou na rede (atraso de rede só diminui o desvio medido).
 */
import { dayKey, formatClock } from './format.js';

export class ServerClock {
  #samples = [];
  #maxSamples;
  #offset = 0;
  #timer = 0;
  #generation = 0;
  #tickers = new Set();
  #dayListeners = new Set();
  #lastDay = '';

  /** @param {{samples?: number}} [opts] */
  constructor({ samples = 5 } = {}) {
    this.#maxSamples = samples;
  }

  /**
   * Registra uma amostra de hora do servidor (ISO com Z), recebida em localMs.
   * @returns {number|null} o desvio desta amostra (ms) ou null se inválida
   */
  sync(serverTime, localMs = Date.now()) {
    const serverMs = typeof serverTime === 'number' ? serverTime : Date.parse(serverTime);
    if (!Number.isFinite(serverMs)) return null;
    const offset = serverMs - localMs;
    this.#samples.push(offset);
    if (this.#samples.length > this.#maxSamples) this.#samples.shift();
    this.#offset = Math.max(...this.#samples);
    return offset;
  }

  /** Desvio em uso (ms). */
  get offset() {
    return this.#offset;
  }

  /** Já recebeu ao menos uma hora do servidor? */
  get synced() {
    return this.#samples.length > 0;
  }

  /** Agora corrigido, em ms. */
  nowMs() {
    return Date.now() + this.#offset;
  }

  /** Agora corrigido, como Date. */
  now() {
    return new Date(this.nowMs());
  }

  /** Dia de hoje em Brasília ("AAAA-MM-DD"). */
  today() {
    return dayKey(this.nowMs());
  }

  /**
   * Chama cb(nowMs) a cada segundo, alinhado à virada do segundo.
   * @returns {() => void} cancela a inscrição
   */
  onTick(cb) {
    this.#tickers.add(cb);
    this.start();
    return () => {
      this.#tickers.delete(cb);
      if (!this.#tickers.size && !this.#dayListeners.size) this.stop();
    };
  }

  /**
   * Chama cb(novoDia, diaAnterior) quando o dia vira em Brasília.
   * @returns {() => void}
   */
  onDayChange(cb) {
    this.#dayListeners.add(cb);
    if (!this.#lastDay) this.#lastDay = this.today();
    this.start();
    return () => {
      this.#dayListeners.delete(cb);
      if (!this.#tickers.size && !this.#dayListeners.size) this.stop();
    };
  }

  /** Confere a virada do dia agora (use ao voltar ao primeiro plano). */
  checkDay() {
    const day = this.today();
    if (this.#lastDay && day !== this.#lastDay) {
      const previous = this.#lastDay;
      this.#lastDay = day;
      for (const cb of this.#dayListeners) cb(day, previous);
    } else if (!this.#lastDay) {
      this.#lastDay = day;
    }
  }

  start() {
    if (this.#timer) return;
    // cada start() abre uma "geração": um laço antigo (stop + start dentro de um
    // callback) percebe que foi substituído e não agenda de novo — nunca dois laços
    const generation = ++this.#generation;
    const schedule = () => {
      const delay = 1000 - (this.nowMs() % 1000) + 5;
      this.#timer = setTimeout(() => {
        if (generation !== this.#generation) return;
        this.#tick();
        if (generation === this.#generation) schedule();
      }, delay);
    };
    schedule();
  }

  stop() {
    clearTimeout(this.#timer);
    this.#timer = 0;
    this.#generation += 1;
  }

  #tick() {
    const now = this.nowMs();
    for (const cb of this.#tickers) cb(now);
    if (this.#dayListeners.size) this.checkDay();
  }
}

/**
 * Liga um <time> à hora de Brasília do relógio (o relógio do cabeçalho).
 * @param {HTMLTimeElement} el
 * @param {ServerClock} clock
 * @returns {() => void}
 */
export function mountClock(el, clock) {
  const paint = (nowMs) => {
    const text = formatClock(nowMs);
    if (el.textContent !== text) el.textContent = text;
    el.dateTime = new Date(nowMs).toISOString();
  };
  paint(clock.nowMs());
  return clock.onTick(paint);
}

const MAX_STOPPAGE = 30;

/**
 * Minuto corrente de um jogo com relógio correndo (CONTRACT: match.clock).
 * minuto = clock.offset + minutos inteiros desde period_started_at + 1;
 * passou de regular_end → acréscimo.
 * @returns {{minute: number, stoppage: number|null}|null}
 */
export function liveMinute(match, nowMs = Date.now()) {
  const clock = match?.clock;
  if (!clock || !match.period_started_at) return null;
  // Relógio parado (suspensão ou operador): o minuto fica no instante em que parou.
  const at = clock.running ? nowMs : Date.parse(clock.paused_at || '');
  const started = Date.parse(match.period_started_at);
  if (!Number.isFinite(started) || !Number.isFinite(at)) return null;
  const elapsed = Math.max(0, Math.floor((at - started) / 60_000));
  const minute = clock.offset + elapsed + 1;
  if (minute > clock.regular_end) {
    return { minute: clock.regular_end, stoppage: Math.min(minute - clock.regular_end, MAX_STOPPAGE) };
  }
  return { minute, stoppage: null };
}

/**
 * Rótulo do minuto ao vivo: "72'", "45+2'", "INT" (intervalo), "PÊN" (pênaltis).
 * Vazio quando o jogo não está ao vivo.
 */
export function liveMinuteLabel(match, nowMs = Date.now()) {
  if (!match || match.status !== 'live' || match.partial_info) return '';
  if (match.period === 'half_time') return 'INT';
  if (match.period === 'penalties') return 'PÊN';
  const m = liveMinute(match, nowMs);
  if (!m) return match.period_short || '';
  return m.stoppage ? `${m.minute}+${m.stoppage}'` : `${m.minute}'`;
}
