/** Shared, text-only interface translation; raw data and SQL remain canonical. */
import spanish from './messages.es.js';

/** Resolve the server-selected interface language without reading session credentials. */
export function getLanguage() {
  return typeof document !== 'undefined' &&
    document.documentElement?.lang?.toLowerCase().startsWith('es')
    ? 'es'
    : 'en';
}

/** Return an explicit formatting locale while keeping calendar arithmetic in UTC. */
export const getLocale = () => (getLanguage() === 'es' ? 'es-ES' : 'en-US');

/** Translate authored interface text and interpolate named values without interpreting HTML. */
export function t(message, parameters = {}) {
  const source = String(message);
  const translated =
    getLanguage() === 'es' && Object.hasOwn(spanish, source) ? spanish[source] : source;
  return translated.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (placeholder, name) =>
    Object.hasOwn(parameters, name) ? String(parameters[name] ?? '') : placeholder,
  );
}

/** Preserve the current application route when the native language form reloads the page. */
export function bindLanguageSwitcher() {
  if (typeof document === 'undefined') return;
  const form = document.getElementById('language-form');
  const select = document.getElementById('language-select');
  const next = document.getElementById('language-next');
  if (!form || !select || !next) return;
  form.classList.add('is-enhanced');
  const target = () => {
    next.value = location.pathname + location.search + location.hash;
  };
  form.addEventListener('submit', target);
  select.addEventListener('change', () => {
    target();
    form.requestSubmit();
  });
}
