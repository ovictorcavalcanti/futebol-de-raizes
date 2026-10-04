/**
 * Cliente da API do Futebol de Raízes (docs/CONTRACT.md §4).
 *
 * Um fetch só para todas as páginas: JSON, cookie de sessão (same-origin),
 * header X-CSRFToken (lido do cookie csrftoken) nos métodos que mudam estado,
 * Idempotency-Key quando pedido, tempo limite com AbortController e erro tipado
 * (ApiError com code/message/details/warnings, no formato de erro da API).
 *
 * Não toca no DOM ao ser importado (os testes de node importam este módulo).
 */

export const DEFAULT_TIMEOUT = 15_000;
export const GENERIC_ERROR = 'Não deu certo agora. Tente de novo em instantes.';
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);
const CSRF_COOKIE = 'csrftoken';

let csrfFallback = '';

/** Erro de uma chamada da API, no formato do contrato: {code, message, details, warnings}. */
export class ApiError extends Error {
  /**
   * @param {{status?: number, code?: string, message?: string, details?: object, warnings?: Array<{code: string, message: string}>, body?: any}} info
   */
  constructor({ status = 0, code = 'http_error', message = GENERIC_ERROR, details = {}, warnings = [], body = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details || {};
    this.warnings = Array.isArray(warnings) ? warnings : [];
    this.body = body;
  }

  /** Falha de rede ou tempo esgotado (o pedido pode ou não ter chegado ao servidor). */
  get isNetwork() {
    return this.code === 'network_error' || this.code === 'timeout';
  }
}

/** Lê um cookie pelo nome ('' quando não existe ou não há document). */
export function getCookie(name, cookieString = typeof document !== 'undefined' ? document.cookie : '') {
  for (const part of (cookieString || '').split(';')) {
    const index = part.indexOf('=');
    if (index < 0) continue;
    if (part.slice(0, index).trim() === name) {
      try {
        return decodeURIComponent(part.slice(index + 1).trim());
      } catch {
        return part.slice(index + 1).trim();
      }
    }
  }
  return '';
}

/** Token CSRF: o cookie (fonte da verdade, muda no login) ou o último visto em /api/auth/me. */
export function csrfToken() {
  return getCookie(CSRF_COOKIE) || csrfFallback;
}

/** Guarda o csrf_token de /api/auth/me para quando o cookie não puder ser lido. */
export function setCsrfToken(token) {
  csrfFallback = typeof token === 'string' ? token : '';
}

/**
 * Chave de idempotência nova (crypto.randomUUID; fora de contexto seguro, um UUID v4
 * montado com crypto.getRandomValues).
 */
export function newIdempotencyKey(cryptoImpl = globalThis.crypto) {
  if (typeof cryptoImpl?.randomUUID === 'function') return cryptoImpl.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof cryptoImpl?.getRandomValues === 'function') cryptoImpl.getRandomValues(bytes);
  else for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Monta "?a=1&b=2" ignorando valores vazios. */
export function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value == null || value === '' || value === false) continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/**
 * Converte uma resposta de erro (corpo já lido) em ApiError.
 * Aceita o formato do contrato ({code, message, details, warnings}) e o padrão do
 * django-ninja ({detail: ...}), por exemplo a falha de CSRF.
 */
export function toApiError(status, body) {
  if (body && typeof body === 'object' && typeof body.code === 'string') {
    return new ApiError({
      status,
      code: body.code,
      message: typeof body.message === 'string' && body.message ? body.message : GENERIC_ERROR,
      details: body.details && typeof body.details === 'object' ? body.details : {},
      warnings: body.warnings,
      body,
    });
  }
  const detail = body && typeof body === 'object' ? body.detail : null;
  if (typeof detail === 'string' && /csrf/i.test(detail)) {
    return new ApiError({ status, code: 'csrf_failed', message: 'Sessão expirada. Tente de novo.', details: { detail }, body });
  }
  const byStatus = { 400: 'invalid_input', 401: 'not_authenticated', 403: 'permission_denied', 404: 'not_found', 422: 'invalid_input' };
  return new ApiError({
    status,
    code: byStatus[status] || 'http_error',
    message: typeof detail === 'string' && detail ? detail : GENERIC_ERROR,
    details: detail != null ? { detail } : {},
    body,
  });
}

/**
 * Chamada à API.
 * @param {string} path ex.: "/api/home"
 * @param {{method?: string, body?: any, query?: object, headers?: object, idempotencyKey?: string,
 *          timeout?: number, signal?: AbortSignal, fetchImpl?: typeof fetch}} [opts]
 * @returns {Promise<any>} corpo JSON (null quando vazio)
 * @throws {ApiError}
 */
export async function apiFetch(path, { method = 'GET', body, query, headers = {}, idempotencyKey, timeout = DEFAULT_TIMEOUT, signal, fetchImpl = globalThis.fetch } = {}) {
  const verb = method.toUpperCase();
  const finalHeaders = { Accept: 'application/json', ...headers };
  if (body !== undefined) finalHeaders['Content-Type'] = 'application/json';
  if (!SAFE_METHODS.has(verb)) {
    const token = csrfToken();
    if (token) finalHeaders['X-CSRFToken'] = token;
  }
  if (idempotencyKey) finalHeaders['Idempotency-Key'] = idempotencyKey;

  const controller = new AbortController();
  let timedOut = false;
  const timer = timeout > 0 ? setTimeout(() => { timedOut = true; controller.abort(); }, timeout) : 0;
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }

  const url = path + queryString(query);
  let response;
  try {
    response = await fetchImpl(url, {
      method: verb,
      headers: finalHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    });
  } catch (error) {
    if (signal?.aborted && !timedOut) throw error; // cancelado por quem chamou: repassa o AbortError
    throw new ApiError({
      code: timedOut ? 'timeout' : 'network_error',
      message: timedOut ? 'A conexão demorou demais. Tente de novo.' : GENERIC_ERROR,
    });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }

  let data = null;
  let parsed = true;
  const text = await response.text().catch(() => '');
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      parsed = false;
    }
  }
  if (!response.ok) throw toApiError(response.status, data);
  if (!parsed) throw new ApiError({ status: response.status, code: 'invalid_response' });
  return data;
}

const get = (path, query, opts) => apiFetch(path, { ...opts, query });
const post = (path, body, opts) => apiFetch(path, { ...opts, method: 'POST', body: body ?? {} });

/* --- Autenticação ---------------------------------------------------------------- */

/** GET /api/auth/me — também seta o cookie CSRF. */
export async function getMe(opts) {
  const data = await get('/api/auth/me', null, opts);
  if (data?.csrf_token) setCsrfToken(data.csrf_token);
  return data;
}
/** POST /api/auth/login — o login troca o token CSRF (a resposta pode trazê-lo). */
export async function login(username, password, opts) {
  const data = await post('/api/auth/login', { username, password }, opts);
  if (data?.csrf_token) setCsrfToken(data.csrf_token);
  return data;
}
export const logout = (opts) => post('/api/auth/logout', {}, opts);

/* --- Leitura ------------------------------------------------------------------------ */

export const getHome = (date, opts) => get('/api/home', { date }, opts);
export const getCompetitions = (opts) => get('/api/competitions', null, opts);
export const getCompetition = (slug, { stage, round } = {}, opts) => get(`/api/competitions/${encodeURIComponent(slug)}`, { stage, round }, opts);
export const getRanking = (rankingId, { live = true } = {}, opts) => get(`/api/rankings/${encodeURIComponent(rankingId)}`, { live: live ? 1 : null }, opts);
export const getStageStandings = (stageId, { live = true } = {}, opts) => get(`/api/stages/${encodeURIComponent(stageId)}/standings`, { live: live ? 1 : null }, opts);
export const listMatches = ({ roundId, date, status, stageId } = {}, opts) => get('/api/matches', { roundId, date, status, stageId }, opts);
export const getMatch = (matchId, opts) => get(`/api/matches/${encodeURIComponent(matchId)}`, null, opts);

/* --- Operação --------------------------------------------------------------------------- */

export const getCatalog = (opts) => get('/api/ops/catalog', null, opts);
export const postEvent = (matchId, body, idempotencyKey, opts) => post(`/api/ops/matches/${encodeURIComponent(matchId)}/events`, body, { ...opts, idempotencyKey });
export const editEvent = (matchId, eventId, body, opts) => post(`/api/ops/matches/${encodeURIComponent(matchId)}/events/${encodeURIComponent(eventId)}/edit`, body, opts);
export const setPartialInfo = (matchId, partialInfo, opts) => post(`/api/ops/matches/${encodeURIComponent(matchId)}/partial-info`, { partial_info: !!partialInfo }, opts);
export const voidEvent = (matchId, eventId, reason = '', opts) => post(`/api/ops/matches/${encodeURIComponent(matchId)}/events/${encodeURIComponent(eventId)}/void`, { reason }, opts);
export const changeStatus = (matchId, body, idempotencyKey, opts) => post(`/api/ops/matches/${encodeURIComponent(matchId)}/status`, body, { ...opts, idempotencyKey });
