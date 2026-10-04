/**
 * Utilitários mínimos de DOM do Futebol de Raízes.
 *
 * Regra da casa: dado da API entra só por textContent/atributos — nunca por
 * innerHTML. Estes helpers garantem isso: strings viram nós de texto.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Cria um elemento HTML.
 * @param {string} tag
 * @param {Record<string, any>|null} [attrs] class, text, dataset, style (objeto), on<Evento>, demais atributos
 * @param {...(Node|string|number|null|false|Array)} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

/**
 * Cria um elemento SVG (mesma assinatura de h()).
 * @returns {SVGElement}
 */
export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

/** Aplica atributos com as mesmas convenções de h(). */
export function setAttrs(el, attrs) {
  if (!attrs) return el;
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class' || key === 'className') {
      const cls = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
      if (cls) el.setAttribute('class', cls);
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (key === 'dataset') {
      for (const [k, v] of Object.entries(value)) if (v != null) el.dataset[k] = String(v);
    } else if (key === 'style' && typeof value === 'object') {
      for (const [prop, v] of Object.entries(value)) if (v != null && v !== '') el.style.setProperty(prop, String(v));
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      el.setAttribute(key, '');
    } else {
      el.setAttribute(key, String(value));
    }
  }
  return el;
}

/** Acrescenta filhos (achata arrays; ignora null/false; string vira texto). */
export function append(parent, children) {
  for (const child of children) {
    if (child == null || child === false || child === '') continue;
    if (Array.isArray(child)) append(parent, child);
    else if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
  return parent;
}

/** Esvazia um elemento. */
export function clear(el) {
  el.replaceChildren();
  return el;
}

/** Troca o texto só quando mudou (evita trabalho de layout a cada segundo). */
export function setText(el, text) {
  const value = text == null ? '' : String(text);
  if (el && el.textContent !== value) el.textContent = value;
  return el;
}

/**
 * <svg><use href="#id"/></svg>
 * @param {string} href ex.: "#i-ball"
 * @param {Record<string, any>} [attrs]
 */
export function svgUse(href, attrs = {}) {
  const svg = s('svg', { 'aria-hidden': 'true', focusable: 'false', ...attrs });
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', href);
  svg.appendChild(use);
  return svg;
}

/**
 * Clona um <template> e devolve o primeiro elemento (ou o fragmento, se houver vários).
 * @param {string|HTMLTemplateElement} tpl id do template (sem #) ou o próprio elemento
 * @returns {HTMLElement|DocumentFragment}
 */
export function cloneTemplate(tpl) {
  const template = typeof tpl === 'string' ? document.getElementById(tpl) : tpl;
  if (!(template instanceof HTMLTemplateElement)) throw new Error(`Template não encontrado: ${tpl}`);
  const fragment = template.content.cloneNode(true);
  return fragment.childElementCount === 1 ? fragment.firstElementChild : fragment;
}

/** Primeiro elemento com data-hook="name" dentro de root. */
export function hook(root, name) {
  return root.querySelector(`[data-hook="${name}"]`);
}

/** Mapa {nome: elemento} de todos os data-hook dentro de root (o primeiro de cada nome). */
export function hooks(root) {
  const map = {};
  for (const el of root.querySelectorAll('[data-hook]')) {
    const name = el.dataset.hook;
    if (!(name in map)) map[name] = el;
  }
  return map;
}

/**
 * Mostra um aviso passageiro na região #toasts (definida em base.html).
 * @param {string} text
 * @param {{kind?: 'info'|'ok'|'error'|'warn', timeout?: number, action?: {label: string, onClick: Function}}} [opts]
 * @returns {{close: () => void}}
 */
export function showToast(text, { kind = 'info', timeout = 4500, action } = {}) {
  const region = document.getElementById('toasts');
  if (!region) return { close() {} };
  const iconName = { ok: 'check', error: 'alert', warn: 'alert', info: 'info' }[kind] || 'info';
  const el = h('div', { class: ['toast', kind !== 'info' && `toast--${kind}`], role: kind === 'error' ? 'alert' : 'status' },
    svgUse(`#i-${iconName}`, { class: 'icon' }),
    h('span', { class: 'toast__text', text }),
  );
  if (action) {
    el.appendChild(h('button', { type: 'button', class: 'btn btn--ghost btn--sm', text: action.label, onClick: () => { action.onClick(); close(); } }));
  }
  const closeBtn = h('button', { type: 'button', class: 'btn btn--ghost btn--icon btn--sm', 'aria-label': 'Fechar aviso' }, svgUse('#i-close', { class: 'icon' }));
  el.appendChild(closeBtn);
  let timer = 0;
  function close() {
    clearTimeout(timer);
    if (!el.isConnected) return;
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 260);
  }
  closeBtn.addEventListener('click', close);
  region.appendChild(el);
  if (timeout > 0) timer = setTimeout(close, timeout);
  return { close };
}

/**
 * Preenche o menu de competições do cabeçalho (#competitions-nav).
 * @param {HTMLElement} list o <ul data-hook="competition-links">
 * @param {Array<{slug: string, name: string, short_name?: string}>} competitions
 * @param {{activeSlug?: string, liveSlugs?: Set<string>, href?: (c) => string}} [opts]
 */
export function renderCompetitionNav(list, competitions, { activeSlug = null, liveSlugs = null, href = null } = {}) {
  const items = competitions.map((c) => {
    const link = h('a', {
      class: 'comp-nav__link',
      href: href ? href(c) : `/competition.html?slug=${encodeURIComponent(c.slug)}`,
      'aria-current': c.slug === activeSlug ? 'page' : null,
      title: c.short_name && c.short_name !== c.name ? c.name : null,
    }, c.name);
    if (liveSlugs?.has(c.slug)) {
      link.append(h('span', { class: 'comp-nav__live', 'aria-hidden': 'true' }), h('span', { class: 'visually-hidden', text: '(jogo ao vivo)' }));
    }
    return h('li', { class: 'comp-nav__item' }, link);
  });
  list.replaceChildren(...items);
  list.closest('nav')?.removeAttribute('aria-busy');
  return list;
}
