/**
 * Tema claro (padrão) e escuro. O script inline do <head> (base.html) aplica o
 * tema salvo antes da primeira pintura; aqui ficam o botão e a sincronização.
 */

export const THEME_KEY = 'fdr-theme';
const DARK_THEME_COLOR = '#0C1322';

function readSaved() {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function save(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* navegação privada ou armazenamento bloqueado: segue sem guardar */
  }
}

/** @returns {'light'|'dark'} */
export function getTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

// Botão de alternância: o nome acessível fica fixo ("Tema escuro") e o estado vai
// em aria-pressed — trocar o rótulo junto faria o leitor de tela anunciar o contrário.
function paintToggles(theme) {
  for (const btn of document.querySelectorAll('[data-theme-toggle]')) {
    btn.setAttribute('aria-pressed', String(theme === 'dark'));
    btn.title = theme === 'dark' ? 'Voltar ao tema claro' : 'Usar tema escuro';
  }
}

/**
 * Aplica o tema: data-theme no <html>, meta theme-color e estado dos botões.
 * @param {'light'|'dark'} theme
 * @param {{persist?: boolean}} [opts]
 */
export function setTheme(theme, { persist = true } = {}) {
  const value = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = value;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', value === 'dark' ? (meta.dataset.dark || DARK_THEME_COLOR) : (meta.dataset.light || meta.content));
  paintToggles(value);
  if (persist) save(value);
  document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: value } }));
}

/** Alterna claro/escuro. */
export function toggleTheme() {
  setTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

/** Liga os botões [data-theme-toggle] e sincroniza entre abas. Idempotente. */
export function initTheme() {
  const root = document.documentElement;
  if (root.dataset.themeReady) return;
  root.dataset.themeReady = '1';
  const saved = readSaved();
  if (saved === 'dark' || saved === 'light') {
    if (saved !== getTheme()) setTheme(saved, { persist: false });
  }
  paintToggles(getTheme());
  document.addEventListener('click', (event) => {
    const btn = event.target instanceof Element ? event.target.closest('[data-theme-toggle]') : null;
    if (btn) toggleTheme();
  });
  window.addEventListener('storage', (event) => {
    if (event.key === THEME_KEY && (event.newValue === 'dark' || event.newValue === 'light')) {
      setTheme(event.newValue, { persist: false });
    }
  });
}

initTheme();
