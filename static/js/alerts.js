/**
 * Alertas de gol da home (docs/PLANO.md "Alertas de gol").
 *
 * Cada gol que chega pelo stream (mensagem `goals`, changes[].kind = "added") gera
 * três alertas: aviso na página (região aria-live acima dos últimos gols), som
 * (para quem ativou) e notificação do sistema (para quem ativou, só no computador).
 *
 * * Cada gol alerta uma vez só (pelo id do evento); os gols da lista inicial contam
 *   como já alertados. Só alertam gols de jogos que estão na home (a mensagem pode
 *   trazer a correção de um jogo antigo).
 * * Gol lançado há mais de 2 minutos, pela hora do SERVIDOR, entra na lista sem alerta.
 * * "removed" (anulado ou cancelado): aviso de correção na página, sem som; fecha a
 *   notificação do gol e abre a de correção. "restored": volta à lista, sem alerta.
 *
 * A regra de decisão (GoalAlertTracker) é pura e testada com node --test; o resto
 * (DOM, áudio, Notification) só roda quando createGoalAlerts() é chamado.
 */
import { showToast } from './render.js';
import { createGoalAlert } from './match-card.js';
import { formatScore } from './format.js';

export const ALERT_WINDOW = 2 * 60_000;
export const SOUND_KEY = 'fdr-sound';
export const NOTIFY_KEY = 'fdr-notifications';

/* ==========================================================================
   Decisão (pura)
   ========================================================================== */

/**
 * Decide o que cada mudança da mensagem `goals` provoca.
 * Ações: 'alert' (aviso + som + notificação), 'silent' (entra na lista sem alerta),
 * 'correction' (aviso de correção), 'restore' (volta à lista sem alerta), 'skip' (repetida).
 */
export class GoalAlertTracker {
  #seen = new Set();
  #removed = new Set();

  /** @param {Array<{event_id: number}>} [initialGoals] gols já na tela: contam como alertados */
  constructor(initialGoals = []) {
    this.markSeen(initialGoals);
  }

  /** Marca gols como já alertados (lista inicial ou lista buscada de novo). */
  markSeen(goals = []) {
    for (const goal of goals) {
      if (goal?.event_id != null) this.#seen.add(goal.event_id);
    }
  }

  /** O gol já foi alertado (ou estava na lista inicial)? */
  hasSeen(eventId) {
    return this.#seen.has(eventId);
  }

  /**
   * @param {Array<{kind: string, reason?: string|null, goal: object}>} changes
   * @param {number} serverNowMs agora pelo relógio do servidor (ServerClock#nowMs)
   * @param {{isRelevant?: (goal: object) => boolean}} [opts] gol de jogo que está na home?
   *   (a mensagem pode trazer correção de jogo fora do dia: registra, mas não alerta)
   * @returns {Array<{action: string, goal: object, reason: string|null}>}
   */
  decide(changes = [], serverNowMs = Date.now(), { isRelevant = null } = {}) {
    return (changes || []).map((change) => {
      const goal = change?.goal;
      const id = goal?.event_id;
      const reason = change?.reason ?? null;
      if (id == null) return { action: 'skip', goal, reason };
      const relevant = typeof isRelevant !== 'function' || isRelevant(goal);
      switch (change.kind) {
        case 'added': {
          if (this.#seen.has(id)) return { action: 'skip', goal, reason };
          this.#seen.add(id);
          this.#removed.delete(id);
          if (!relevant) return { action: 'skip', goal, reason };
          return { action: isRecent(goal, serverNowMs) ? 'alert' : 'silent', goal, reason };
        }
        case 'removed': {
          if (this.#removed.has(id)) return { action: 'skip', goal, reason };
          this.#removed.add(id);
          this.#seen.add(id); // se voltar, volta sem alerta
          if (!relevant) return { action: 'skip', goal, reason };
          return { action: 'correction', goal, reason: reason || 'annulled' };
        }
        case 'restored': {
          this.#removed.delete(id);
          this.#seen.add(id);
          return { action: 'restore', goal, reason };
        }
        default:
          return { action: 'skip', goal, reason };
      }
    });
  }
}

/** Gol lançado há no máximo 2 minutos (hora do servidor)? Horário inválido → não alerta. */
export function isRecent(goal, serverNowMs, windowMs = ALERT_WINDOW) {
  const created = Date.parse(goal?.created_at ?? '');
  if (!Number.isFinite(created)) return false;
  return serverNowMs - created <= windowMs;
}

/* ==========================================================================
   Suporte do navegador
   ========================================================================== */

/** Celular/tablet? (userAgentData.mobile; senão o user agent; iPadOS se apresenta como Mac com toque). */
export function isMobileDevice(win = globalThis) {
  const nav = win?.navigator || {};
  if (typeof nav.userAgentData?.mobile === 'boolean' && nav.userAgentData.mobile) return true;
  const ua = String(nav.userAgent || '');
  if (/Android|iPhone|iPad|iPod|Mobile|Silk|Kindle|Opera Mini/i.test(ua)) return true;
  return /Macintosh/.test(ua) && (nav.maxTouchPoints || 0) > 1 && 'ontouchstart' in (win || {});
}

/**
 * Onde `new Notification()` funciona: API presente, contexto seguro (HTTPS ou
 * localhost) e computador. Sem permissão concedida, testa o construtor (no celular
 * ele lança TypeError); com permissão, não testa para não mostrar nada.
 * @returns {{available: boolean, reason: null|'unsupported'|'insecure'|'mobile'}}
 */
export function notificationSupport(win = globalThis) {
  const NotificationImpl = win?.Notification;
  // celular primeiro: lá o aviso de HTTPS não ajudaria (new Notification() não funciona)
  if (isMobileDevice(win)) return { available: false, reason: 'mobile' };
  if (win?.isSecureContext === false) return { available: false, reason: 'insecure' };
  if (typeof NotificationImpl !== 'function') return { available: false, reason: 'unsupported' };
  if (NotificationImpl.permission !== 'granted') {
    try {
      const probe = new NotificationImpl('', { silent: true, tag: 'fdr-probe' });
      probe.onerror = null;
      probe.close?.();
    } catch {
      return { available: false, reason: 'mobile' };
    }
  }
  return { available: true, reason: null };
}

/* ==========================================================================
   Textos
   ========================================================================== */

const teamName = (team) => team?.name || team?.short_name || '';
const shortName = (team) => team?.short_name || team?.name || '';

/** Título e corpo da notificação do sistema (gol ou correção). */
export function notificationText(goal, { kind = 'goal', reason = '' } = {}) {
  const m = goal?.match || {};
  const s = goal?.score_after || { home: 0, away: 0 };
  const who = `${goal?.player || 'Gol'}${goal?.minute_label ? ` (${goal.minute_label})` : ''}`;
  if (kind === 'goal') {
    return {
      title: `É gol! ${shortName(m.home)} ${formatScore(s.home, s.away)} ${shortName(m.away)}`,
      body: `${who} · ${teamName(goal?.team)}`,
    };
  }
  return {
    title: reason === 'voided' ? 'Lance corrigido.' : 'Oxe! Gol anulado.',
    body: `Gol de ${who} em ${teamName(m.home)} × ${teamName(m.away)} não vale mais. Lance corrigido pelo operador.`,
  };
}

/* ==========================================================================
   Controlador da home (DOM)
   ========================================================================== */

function storageGet(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
  } catch {
    /* navegação privada ou armazenamento bloqueado: segue sem guardar */
  }
}

/**
 * Liga os alertas da home.
 * @param {object} opts
 * @param {HTMLElement} opts.region #goal-alert (aria-live)
 * @param {HTMLButtonElement} [opts.soundButton] #toggle-sound
 * @param {HTMLButtonElement} [opts.notifyButton] #toggle-notifications
 * @param {HTMLAudioElement} [opts.audio] #goal-sound
 * @param {HTMLElement} [opts.hint] aviso de que as notificações exigem HTTPS
 * @param {() => number} opts.serverNow relógio do servidor
 * @param {Array} [opts.initialGoals] latest_goals da carga inicial
 * @param {(goal: object) => boolean} [opts.isRelevant] o gol é de um jogo que está na home?
 * @param {string} [opts.iconUrl] ícone da notificação
 */
export function createGoalAlerts({
  region,
  soundButton = null,
  notifyButton = null,
  audio = null,
  hint = null,
  serverNow = () => Date.now(),
  initialGoals = [],
  isRelevant = null,
  iconUrl = '',
  maxVisible = 3,
  ttl = 120_000,
  win = window,
} = {}) {
  const tracker = new GoalAlertTracker(initialGoals);
  const notifications = new Map(); // event_id → Notification
  let soundOn = false;
  let notifyOn = false;

  /* --- Som ------------------------------------------------------------------------ */
  const paintSound = () => soundButton?.setAttribute('aria-pressed', String(soundOn));

  function playSound() {
    if (!audio) return Promise.reject(new Error('sem áudio'));
    try {
      audio.currentTime = 0;
    } catch {
      /* ainda sem metadados */
    }
    const played = audio.play();
    return played && typeof played.then === 'function' ? played : Promise.resolve();
  }

  if (soundButton && audio) {
    // a escolha fica guardada; se o navegador já avisa que vai recusar, o botão volta a "Ativar som"
    const policy = typeof win.navigator?.getAutoplayPolicy === 'function' ? safePolicy(win.navigator, audio) : 'allowed';
    soundOn = storageGet(SOUND_KEY) === 'on' && policy === 'allowed';
    paintSound();
    soundButton.addEventListener('click', () => {
      if (soundOn) {
        soundOn = false;
        audio.pause();
        storageSet(SOUND_KEY, 'off');
        paintSound();
        return;
      }
      // o clique toca o som uma vez: amostra e liberação do áudio
      playSound().then(() => {
        soundOn = true;
        storageSet(SOUND_KEY, 'on');
        paintSound();
      }).catch(() => {
        showToast('O navegador não liberou o som agora. Tente de novo.', { kind: 'warn' });
      });
    });
  } else if (soundButton) {
    soundButton.hidden = true;
  }

  /* --- Notificações ---------------------------------------------------------------- */
  const support = notificationSupport(win);
  const paintNotify = () => notifyButton?.setAttribute('aria-pressed', String(notifyOn));
  if (notifyButton) {
    notifyButton.hidden = !support.available;
    if (hint) hint.hidden = support.reason !== 'insecure';
    if (support.available) {
      notifyOn = storageGet(NOTIFY_KEY) === 'on' && win.Notification.permission === 'granted';
      paintNotify();
      notifyButton.addEventListener('click', async () => {
        if (notifyOn) {
          notifyOn = false;
          storageSet(NOTIFY_KEY, 'off');
          paintNotify();
          return;
        }
        const permission = await requestPermission(win.Notification);
        if (permission === 'granted') {
          notifyOn = true;
          storageSet(NOTIFY_KEY, 'on');
          paintNotify();
        } else {
          showToast('As notificações estão bloqueadas neste navegador. Libere nas configurações do site.', { kind: 'warn', timeout: 7000 });
        }
      });
    }
  }

  function notify(goal, kind, reason) {
    if (!notifyOn || win.Notification?.permission !== 'granted') return;
    const { title, body } = notificationText(goal, { kind, reason });
    try {
      const n = new win.Notification(title, { body, tag: `fdr-${kind}-${goal.event_id}`, icon: iconUrl || undefined, lang: 'pt-BR' });
      n.onclick = () => {
        try {
          win.focus();
        } catch {
          /* janela já em foco */
        }
        n.close();
      };
      if (kind === 'goal') notifications.set(goal.event_id, n);
    } catch {
      /* o navegador recusou: segue com o aviso na página */
    }
  }

  /* --- Aviso na página ------------------------------------------------------------------ */
  function pageAlert(goal, kind, reason) {
    if (!region) return;
    if (kind === 'correction') {
      // como na notificação: o "É gol!" que não vale mais sai e a correção entra no lugar
      for (const old of [...region.children]) {
        if (old.dataset?.eventId === String(goal.event_id)) old.remove();
      }
    }
    const el = createGoalAlert(goal, { kind, reason });
    region.prepend(el);
    while (region.childElementCount > maxVisible) region.lastElementChild.remove();
    if (ttl > 0) setTimeout(() => el.remove(), ttl);
  }

  function apply(decision) {
    const { action, goal, reason } = decision;
    if (action === 'alert') {
      pageAlert(goal, 'goal');
      if (soundOn) {
        playSound().catch((error) => {
          // o navegador recusou o som depois de recarregar: o botão volta a "Ativar som"
          if (error?.name === 'NotAllowedError') {
            soundOn = false;
            paintSound();
          }
        });
      }
      notify(goal, 'goal');
    } else if (action === 'correction') {
      pageAlert(goal, 'correction', reason);
      notifications.get(goal.event_id)?.close();
      notifications.delete(goal.event_id);
      notify(goal, 'correction', reason);
    }
  }

  return {
    tracker,
    /** Lista buscada de novo (carga, virada do dia, volta depois de 5 min): sem alertas. */
    reset(goals = []) {
      tracker.markSeen(goals);
    },
    /**
     * Mensagem `goals` do stream.
     * @returns {Array} as decisões (a home usa para destacar os gols novos na lista)
     */
    handle(message) {
      const decisions = tracker.decide(message?.changes || [], serverNow(), { isRelevant });
      for (const decision of decisions) apply(decision);
      return decisions;
    },
    get soundEnabled() {
      return soundOn;
    },
    get notificationsEnabled() {
      return notifyOn;
    },
  };
}

function safePolicy(nav, audio) {
  try {
    return nav.getAutoplayPolicy(audio);
  } catch {
    return 'allowed';
  }
}

function requestPermission(NotificationImpl) {
  if (NotificationImpl.permission === 'granted' || NotificationImpl.permission === 'denied') {
    return Promise.resolve(NotificationImpl.permission);
  }
  return new Promise((resolve) => {
    // Safari antigo usa callback; os demais, Promise
    const result = NotificationImpl.requestPermission(resolve);
    if (result && typeof result.then === 'function') result.then(resolve, () => resolve('denied'));
  });
}
