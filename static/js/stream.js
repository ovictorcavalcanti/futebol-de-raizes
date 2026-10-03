/**
 * Stream ao vivo (SSE) do Futebol de Raízes — um EventSource por página em
 * /api/stream?after=<cursor> (docs/CONTRACT.md §5, docs/PLANO.md "Implementação").
 *
 * * Guarda o id da última mensagem recebida; cada tópico (match, standings, goals,
 *   ping) tem o seu handler; todo ping acerta o ServerClock.
 * * Recria o EventSource (com after = último id) ao voltar ao primeiro plano, depois
 *   de 45 s sem ping e quando o navegador desiste da conexão (readyState CLOSED).
 * * Depois de 5 minutos sem stream, não pede reenvio: chama onStale() para a página
 *   buscar o estado inteiro de novo (e reabrir com o cursor novo).
 * * Expõe o estado da conexão para o aviso "Reconectando ao vivo…".
 *
 * As decisões de reconexão são funções puras exportadas (testadas com node --test);
 * o módulo não toca no DOM ao ser importado.
 */

export const PING_TIMEOUT = 45_000; // sem ping (nem mensagem) por 45 s → recria
export const STALE_AFTER = 5 * 60_000; // sem stream por 5 min → busca o estado de novo
export const ALIVE_GRACE = 25_000; // ao voltar ao primeiro plano: conexão aberta com contato há < 25 s segue
export const WATCHDOG_INTERVAL = 5_000;
export const TOPICS = Object.freeze(['match', 'standings', 'goals']);

export const STATUS = Object.freeze({
  CONNECTING: 'connecting',
  LIVE: 'live',
  RECONNECTING: 'reconnecting',
  STOPPED: 'stopped',
});

// valores de EventSource.readyState (também sem EventSource no ambiente, como no node)
export const READY = Object.freeze({ CONNECTING: 0, OPEN: 1, CLOSED: 2 });

/**
 * O que fazer com a conexão agora (função pura).
 *
 * @param {object} s
 * @param {'watchdog'|'visible'|'error'} s.trigger o que motivou a checagem
 * @param {number|null} s.readyState readyState do EventSource atual (null = nenhum aberto)
 * @param {number} s.lastContactAt último contato real com o servidor (ping, mensagem, abertura
 *   ou o estado buscado na API) — base da regra dos 5 minutos
 * @param {number} s.openedAt quando o EventSource atual foi criado — base dos 45 s sem ping
 * @param {number} s.nowMs
 * @returns {'none'|'replay'|'refetch'} replay = recriar com after = último id;
 *   refetch = buscar o estado inteiro (passou de 5 min sem stream)
 */
export function decideReconnect({ trigger, readyState, lastContactAt, openedAt = 0, nowMs }) {
  const sinceContact = nowMs - lastContactAt;
  let recreate = false;
  if (readyState == null || readyState === READY.CLOSED) {
    recreate = true;
  } else if (trigger === 'visible') {
    // ao voltar ao primeiro plano, recria — a não ser que a conexão esteja
    // comprovadamente viva (aberta e com ping recente)
    recreate = !(readyState === READY.OPEN && sinceContact < ALIVE_GRACE);
  } else {
    recreate = nowMs - Math.max(lastContactAt, openedAt) > PING_TIMEOUT;
  }
  if (!recreate) return 'none';
  return sinceContact > STALE_AFTER ? 'refetch' : 'replay';
}

/**
 * Espera antes de recriar depois que o navegador desistiu (falhas seguidas):
 * 1 s, 2 s, 4 s… até 30 s, com variação de ±20% para não sincronizar clientes.
 */
export function backoffDelay(attempt, { base = 1_000, max = 30_000, random = Math.random } = {}) {
  const raw = Math.min(max, base * 2 ** Math.max(0, attempt));
  return Math.round(raw * (0.8 + 0.4 * random()));
}

/** URL do stream com a posição de retomada (o cursor da leitura ou o último id recebido). */
export function streamUrl(base = '/api/stream', after = null) {
  if (after == null || after === '' || !Number.isFinite(Number(after)) || Number(after) < 0) return base;
  const sep = base.includes('?') ? '&' : '?';
  return `${base}${sep}after=${Math.trunc(Number(after))}`;
}

/** Id da mensagem SSE como inteiro (ou null). */
export function parseEventId(raw) {
  if (raw == null || raw === '') return null;
  const text = String(raw).trim();
  return /^\d+$/.test(text) ? Number(text) : null;
}

/**
 * Cria o stream da página.
 *
 * @param {object} opts
 * @param {Record<string, (data: any, id: number|null) => void>} [opts.handlers] match, standings, goals, ping
 * @param {(status: string) => void} [opts.onStatus] connecting | live | reconnecting | stopped
 * @param {() => (Promise<number|null|void>|number|null|void)} [opts.onStale] busca o estado inteiro;
 *   devolve (ou resolve com) o cursor novo. Sem onStale, a página só pede reenvio.
 * @param {{sync: (serverTime: string) => any}} [opts.clock] ServerClock (cada ping acerta o relógio)
 * @param {string} [opts.url]
 * @param {typeof EventSource} [opts.EventSourceImpl]
 * @param {() => number} [opts.now]
 * @param {any} [opts.win] window (eventos visibilitychange/online/pagehide/pageshow)
 */
export function createStream({
  handlers = {},
  onStatus = null,
  onStale = null,
  clock = null,
  url = '/api/stream',
  EventSourceImpl = globalThis.EventSource,
  now = () => Date.now(),
  win = typeof window !== 'undefined' ? window : null,
  watchdogMs = WATCHDOG_INTERVAL,
} = {}) {
  let source = null;
  let lastId = null;
  let lastContactAt = 0;
  let openedAt = 0;
  let status = STATUS.STOPPED;
  let failures = 0;
  let retryTimer = 0;
  let watchdog = 0;
  let refetching = false;
  let refetchNotBefore = 0; // espera crescente quando a busca do estado falha (sem rede)
  let running = false;

  const setStatus = (next) => {
    if (status === next) return;
    status = next;
    onStatus?.(next);
  };

  const contact = () => {
    lastContactAt = now();
    failures = 0;
    setStatus(STATUS.LIVE);
  };

  const safeCall = (fn, ...args) => {
    try {
      fn?.(...args);
    } catch (error) {
      // um desenho com defeito não derruba o stream; o erro aparece no console
      if (typeof globalThis.reportError === 'function') globalThis.reportError(error);
      else setTimeout(() => { throw error; });
    }
  };

  const parse = (event) => {
    try {
      return JSON.parse(event.data);
    } catch {
      return null;
    }
  };

  function close() {
    clearTimeout(retryTimer);
    retryTimer = 0;
    if (source) {
      source.onopen = source.onerror = null;
      source.close();
      source = null;
    }
  }

  function open() {
    close();
    if (!running || typeof EventSourceImpl !== 'function') return;
    openedAt = now();
    const es = new EventSourceImpl(streamUrl(url, lastId), { withCredentials: false });
    source = es;
    es.onopen = () => { if (source === es) contact(); };
    es.onerror = () => {
      if (source !== es) return;
      setStatus(STATUS.RECONNECTING);
      if (es.readyState === READY.CLOSED) {
        // o navegador desistiu (ex.: 400/503): recria com espera crescente
        const delay = backoffDelay(failures++);
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => check('error'), delay);
      }
      // CONNECTING: o navegador está tentando sozinho (retry: 3000, com Last-Event-ID)
    };
    es.addEventListener('ping', (event) => {
      if (source !== es) return;
      contact();
      const data = parse(event);
      if (data?.server_time) safeCall(() => clock?.sync(data.server_time));
      safeCall(handlers.ping, data, null);
    });
    for (const topic of TOPICS) {
      es.addEventListener(topic, (event) => {
        if (source !== es) return;
        const id = parseEventId(event.lastEventId);
        if (id != null) lastId = id;
        contact();
        const data = parse(event);
        if (data) safeCall(handlers[topic], data, id);
      });
    }
  }

  async function refetch() {
    if (refetching) return;
    refetching = true;
    close();
    setStatus(STATUS.RECONNECTING);
    try {
      const cursor = await onStale();
      if (!running) return;
      lastContactAt = now();
      failures = 0;
      refetchNotBefore = 0;
      if (cursor != null) lastId = cursor;
      open();
    } catch {
      // sem rede ainda: uma checagem seguinte tenta de novo, com espera crescente
      refetchNotBefore = now() + backoffDelay(failures++);
    } finally {
      refetching = false;
    }
  }

  function check(trigger) {
    if (!running || refetching) return;
    if (trigger === 'watchdog' && retryTimer) return; // já há uma recriação agendada
    const decision = decideReconnect({
      trigger,
      readyState: source ? source.readyState : null,
      lastContactAt,
      openedAt,
      nowMs: now(),
    });
    if (decision === 'none') return;
    if (decision === 'refetch' && typeof onStale === 'function') {
      if (now() >= refetchNotBefore) refetch();
      return;
    }
    setStatus(STATUS.RECONNECTING);
    open();
  }

  const onVisibility = () => {
    if (win?.document && win.document.visibilityState === 'hidden') return;
    check('visible');
  };
  const onOnline = () => check('visible');
  const onPageHide = (event) => {
    // libera a conexão (e o bfcache); a volta recria pelo pageshow
    if (event?.persisted) close();
  };
  const onPageShow = (event) => { if (event?.persisted) check('visible'); };

  function listen(on) {
    if (!win?.addEventListener) return;
    const method = on ? 'addEventListener' : 'removeEventListener';
    win.document?.[method]('visibilitychange', onVisibility);
    win[method]('online', onOnline);
    win[method]('pagehide', onPageHide);
    win[method]('pageshow', onPageShow);
  }

  return {
    /** Abre o stream a partir do cursor da leitura (o estado acabou de ser buscado). */
    start(after = null) {
      if (!running) {
        running = true;
        listen(true);
        watchdog = setInterval(() => check('watchdog'), watchdogMs);
      }
      if (after != null) lastId = after;
      lastContactAt = now();
      failures = 0;
      setStatus(STATUS.CONNECTING);
      open();
    },
    /** Reabre a partir de um cursor novo (depois de a página buscar o estado de novo). */
    restart(after = null) {
      this.start(after);
    },
    /** Força a checagem como se a página tivesse voltado ao primeiro plano. */
    reconnect() {
      check('visible');
    },
    stop() {
      running = false;
      clearInterval(watchdog);
      watchdog = 0;
      listen(false);
      close();
      setStatus(STATUS.STOPPED);
    },
    get lastId() {
      return lastId;
    },
    get status() {
      return status;
    },
  };
}

/**
 * Liga o aviso "Reconectando ao vivo…" (#live-status) ao estado do stream.
 * Só aparece depois de `delay` ms sem conexão — mais que o `retry: 3000` do servidor,
 * para não piscar numa reconexão normal (o servidor pode encerrar o stream).
 * @param {HTMLElement|null} el
 * @returns {(status: string) => void} passe como onStatus do createStream
 */
export function liveStatusIndicator(el, { delay = 4_000 } = {}) {
  let timer = 0;
  return (status) => {
    if (!el) return;
    clearTimeout(timer);
    if (status === STATUS.RECONNECTING) {
      timer = setTimeout(() => { el.hidden = false; }, delay);
    } else {
      el.hidden = true;
    }
  };
}
