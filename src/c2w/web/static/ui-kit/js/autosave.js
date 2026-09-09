/* ============================================================================
   Save-on-change for a card of settings.

   Why it exists: the page it came from had a Save button per card, each of
   which redirected — so configuring the second card silently discarded
   whatever you had set on the first. Autosaving removes the class of bug
   entirely.

   THE TRAP, and the reason `onSaved` exists. The card saves without a
   reload, so *everything the server rendered from the pre-save state is now
   wrong* and has to be corrected by hand. The one that got missed in the
   original was a Run button: switching a row on left the button disabled
   until the next full page load, and a disabled button does nothing when
   clicked and says nothing about why. When you add a field, ask what else on
   the card was rendered from it.
   ============================================================================ */

const el = (x) => (typeof x === 'string' ? document.querySelector(x) : x);

export function autosave(card, {
  url,                       // string, or (card) => string
  form = '.item-form',
  marker = '.savemark',
  debounce = 600,
  clearAfter = 2000,
  onSaved,                   // (card, data) => void — repaint derived UI here
  onError,
} = {}) {
  const node = el(card);
  if (!node) return;
  const formEl = node.querySelector(form);
  if (!formEl) return;
  let timer = null;

  function mark(text, cls) {
    const m = node.querySelector(marker);
    if (m) { m.textContent = text; m.className = `savemark hint ${cls || ''}`.trim(); }
  }

  async function save() {
    const target = typeof url === 'function' ? url(node) : url;
    mark('saving…', '');
    let data;
    try {
      const res = await fetch(target, {method: 'POST', body: new FormData(formEl)});
      data = await res.json();
    } catch (_) {
      /* The failure is the message that has to persist and be readable. */
      mark('not saved — check the connection', 'warn');
      if (onError) onError(node, _);
      return;
    }
    mark(data.ok ? 'saved' : (data.message || 'not saved'), data.ok ? 'ok' : 'warn');
    if (onSaved) onSaved(node, data);
    /* Quiet on success: success is the normal case, and a green flash on
       every keystroke is noise. A failure stays up until it is fixed. */
    if (data.ok && clearAfter) setTimeout(() => mark('', ''), clearAfter);
  }

  /* A select or a checkbox is a decision — save it at once. Typing is not
     finished until it pauses, so text debounces. */
  node.addEventListener('change', save);
  node.addEventListener('input', (e) => {
    const t = e.target;
    if (t.tagName !== 'INPUT' || t.type === 'checkbox' || t.type === 'radio') return;
    clearTimeout(timer);
    timer = setTimeout(save, debounce);
  });
  /* Enter in a text field submits. Route it to the same path rather than
     letting the browser navigate away. */
  formEl.addEventListener('submit', (e) => { e.preventDefault(); save(); });

  return {save};
}

/* Wire every card matching a selector. */
export function autosaveAll(selector, opts) {
  return Array.from(document.querySelectorAll(selector)).map((c) => autosave(c, opts));
}
