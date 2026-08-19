// Globales Eingabefeld-Verhalten (einmal in index.js installiert):
// 1) Beim Fokussieren wird der komplette Feld-Inhalt markiert -> direktes
//    Überschreiben ohne manuelles Löschen.
// 2) Hat das Feld noch keinen Platzhalter, wird der aktuelle Wert beim ersten
//    Fokus als Platzhalter gesetzt -> beim Leeren bleibt der vorherige Wert
//    grau sichtbar und kann einfach überschrieben werden.
// Opt-out pro Feld: data-no-select="true".
const SELECT_TYPES = ['text', 'number', 'search', 'tel', 'url', 'email'];

export function installInputUX() {
  document.addEventListener('focusin', (e) => {
    const el = e.target;
    if (!el || el.tagName !== 'INPUT') return;
    if (!SELECT_TYPES.includes(el.type)) return;
    if (el.readOnly || el.disabled || el.dataset.noSelect === 'true') return;
    if (!el.placeholder && el.value !== '') {
      el.placeholder = el.value;
    }
    try { el.select(); } catch { /* z.B. number-Inputs in alten Browsern */ }
  });
}

export default installInputUX;
