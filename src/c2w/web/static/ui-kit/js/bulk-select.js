/* ============================================================================
   Bulk selection over a filtered list.

   The one rule that matters: selection is scoped to what the filters leave
   visible, and a hidden row is never acted on. That makes "filter, then
   Select all" the bulk edit — and guarantees nothing off-screen is ever
   caught by it. Call `refresh()` from your filter's onRender so hiding a row
   also deselects it.
   ============================================================================ */

const el = (x) => (typeof x === 'string' ? document.querySelector(x) : x);

export function bulkSelect({
  items,                 // selector for each selectable row/card
  checkbox = '.pick',    // the checkbox within one
  bar,                   // the action bar, hidden when nothing is selected
  count,                 // element showing how many are selected
  selectAll,             // buttons, all optional
  selectNone,
  selectInvert,
  clear,
  label = (n) => `Select all${n ? ` (${n})` : ''}`,
  onChange,
} = {}) {
  const barEl = bar ? el(bar) : null;
  const countEl = count ? el(count) : null;
  const allBtn = selectAll ? el(selectAll) : null;

  const boxes = () => Array.from(document.querySelectorAll(`${items} ${checkbox}`));
  const visible = () => Array.from(
    document.querySelectorAll(`${items}:not([hidden]) ${checkbox}`));
  const picked = () => boxes().filter((b) => b.checked);

  function refresh() {
    /* Anything hidden by a filter loses its tick, so the count and the action
       always mean the same set. */
    document.querySelectorAll(`${items}[hidden] ${checkbox}`)
      .forEach((cb) => { cb.checked = false; });

    const n = picked().length;
    if (countEl) countEl.textContent = n;
    if (barEl) barEl.hidden = n === 0;
    if (allBtn) {
      const vis = visible();
      const on = vis.length > 0 && vis.every((b) => b.checked);
      allBtn.classList.toggle('on', on);
      allBtn.textContent = label(vis.length);
    }
    if (onChange) onChange(picked());
  }

  boxes().forEach((cb) => cb.addEventListener('change', refresh));
  if (allBtn) allBtn.addEventListener('click', () => {
    visible().forEach((b) => { b.checked = true; }); refresh();
  });
  if (selectNone) el(selectNone).addEventListener('click', () => {
    boxes().forEach((b) => { b.checked = false; }); refresh();
  });
  if (selectInvert) el(selectInvert).addEventListener('click', () => {
    visible().forEach((b) => { b.checked = !b.checked; }); refresh();
  });
  if (clear) el(clear).addEventListener('click', () => {
    boxes().forEach((b) => { b.checked = false; }); refresh();
  });

  refresh();
  return {refresh, picked};
}
