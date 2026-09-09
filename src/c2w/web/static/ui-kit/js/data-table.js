/* ============================================================================
   Data table — live search, filter, sort and paging over rows already in the
   page.

   The premise: the server renders the whole list once, and everything after
   that is choosing which rows may be shown. With a few hundred rows, waiting
   for a round trip on every keystroke was the actual problem being solved —
   not rendering cost.

   Know the limit. This is right up to a few thousand rows. Past that the
   page weight, not the filtering, becomes the problem and you want the server
   paging instead. The give-away is time-to-first-byte, not sluggish typing.

   Rows describe themselves with data attributes, which the server already
   knows when it renders them:

     <tr class="row" data-search="ada@example.com engineering"
                     data-tenant="acme" data-state="full" data-size="80423">

   Filtering reads those attributes; nothing here knows what a tenant is.
   ============================================================================ */

const noop = () => {};
const el = (x) => (typeof x === 'string' ? document.querySelector(x) : x);

/* First page, last page, and the current page's neighbours. 131 rows at five
   a page is 27 buttons, which is a worse way to find a row than the list. */
export function pageWindow(current, total) {
  const out = [];
  for (let p = 1; p <= total; p++) {
    if (p === 1 || p === total || Math.abs(p - current) <= 1) out.push(p);
    else if (out[out.length - 1] !== null) out.push(null);
  }
  return out;
}

export function createDataTable(options) {
  const root = el(options.table);
  if (!root) return null;

  const container = options.rowContainer ? el(options.rowContainer) : root;
  const all = Array.from(container.querySelectorAll(options.rows));
  if (!all.length && !options.allowEmpty) return null;
  const body = all.length ? all[0].parentNode : null;

  const search = options.search ? el(options.search.el || options.search) : null;
  const searchKey = (options.search && options.search.key) || 'search';
  const clearBtn = options.searchClear ? el(options.searchClear) : null;

  const filters = (options.filters || [])
    .map((f) => ({el: el(f.el), key: f.key, match: f.match}))
    .filter((f) => f.el);

  const chipGroups = (options.chips || []).map((g) => ({
    els: Array.from(document.querySelectorAll(g.selector)),
    match: g.match,
    value: g.initial || 'all',
    counts: g.counts || null,
  }));

  const labels = Object.assign({
    noneYet: 'Nothing here yet.',
    noneMatch: 'Nothing matches these filters.',
    showing: (from, to, total) => `Showing ${from}–${to} of ${total}`,
  }, options.labels || {});

  /* ---- paging ------------------------------------------------------------
     Optional: without a `page` option every matching row is shown. */
  const pageCfg = options.page || null;
  const pager = pageCfg ? el(pageCfg.el) : null;
  const perSelect = pageCfg && pageCfg.per ? el(pageCfg.per) : null;
  const buttons = pager ? pager.querySelector('.pagebtns') : null;
  const range = pager ? pager.querySelector('.range') : null;
  const storageKey = pageCfg ? pageCfg.storageKey : null;

  let rows = all;          // what the filters left, in the current sort order
  let page = 1;
  let sortKey = (options.sort && options.sort.key) || null;
  let sortDir = (options.sort && options.sort.dir === 'asc') ? 1 : -1;

  /* A remembered page size, because a list that resets to 25 on every visit
     is one the operator has to re-configure before they can read it. */
  if (perSelect && storageKey) {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved && Array.from(perSelect.options).some((o) => o.value === saved)) {
        perSelect.value = saved;
      }
    } catch (_) { /* a private window has no storage; the default is fine */ }
  }

  const size = () => (perSelect ? parseInt(perSelect.value, 10) : 0) ||
                     (pageCfg && pageCfg.defaultSize) || rows.length || 1;
  const pageCount = () => Math.max(1, Math.ceil(rows.length / size()));

  function pageButton(label, target, disabled, current) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.disabled = !!disabled;
    if (current) { b.className = 'on'; b.setAttribute('aria-current', 'page'); }
    b.addEventListener('click', () => { page = target; render(); });
    buttons.appendChild(b);
  }

  function render() {
    let from = 0, to = rows.length;
    if (pageCfg) {
      page = Math.min(Math.max(1, page), pageCount());
      from = (page - 1) * size();
      to = Math.min(from + size(), rows.length);
    }

    rows.forEach((row, i) => {
      row.hidden = i < from || i >= to;
      /* `tr:last-child td` drops the rule under the final row, which once a
         page ends mid-table is a hidden one — leaving the visible last row
         with a stray border. */
      row.classList.toggle('lastrow', i === to - 1);
    });

    if (buttons) {
      buttons.textContent = '';
      if (pageCount() > 1) {
        pageButton('‹ Prev', page - 1, page === 1);
        for (const p of pageWindow(page, pageCount())) {
          if (p === null) {
            const gap = document.createElement('span');
            gap.className = 'gap';
            gap.textContent = '…';
            buttons.appendChild(gap);
          } else {
            pageButton(String(p), p, false, p === page);
          }
        }
        pageButton('Next ›', page + 1, page === pageCount());
      }
    }

    if (range) {
      /* "Nothing here yet" is a different statement from "nothing matches
         what you asked for", and only one of them is ever true. */
      range.textContent = rows.length
        ? labels.showing(from + 1, to, rows.length)
        : (all.length ? labels.noneMatch : labels.noneYet);
    }
    if (pager) pager.hidden = false;
    (options.onRender || noop)({visible: rows.length, total: all.length, page});
  }

  function apply() {
    const needle = search ? (search.value || '').trim().toLowerCase() : '';

    rows = all.filter((row) => {
      const d = row.dataset;
      if (needle && !(d[searchKey] || '').includes(needle)) return false;
      for (const f of filters) {
        const v = f.el.value;
        if (!v) continue;
        if (f.match ? !f.match(row, v) : d[f.key] !== v) return false;
      }
      for (const g of chipGroups) {
        if (!g.match(row, g.value)) return false;
      }
      return true;
    });

    /* Counts are computed over everything, not over what survives — a chip
       that says 0 because another chip is active tells you nothing. */
    for (const g of chipGroups) {
      if (!g.counts) continue;
      for (const [key, node] of Object.entries(g.counts)) {
        const target = el(node);
        if (!target) continue;
        const n = all.filter((row) => g.match(row, key)).length;
        target.textContent = n;
        target.classList.toggle('zero', n === 0);
      }
    }

    /* Rows the filters dropped are hidden outright; the pager only ever hides
       rows that did match, which is what keeps "showing 1-25 of N" honest. */
    for (const row of all) row.hidden = true;
    if (clearBtn) clearBtn.hidden = !needle;
    page = 1;
    render();
  }

  function sortBy(key, type) {
    sortDir = sortKey === key ? -sortDir : (type === 'text' ? 1 : -1);
    sortKey = key;
    const ordered = all.slice().sort((a, b) => {
      const x = a.dataset[key], y = b.dataset[key];
      const cmp = type === 'num'
        ? (Number(x) || 0) - (Number(y) || 0)
        : String(x ?? '').localeCompare(String(y ?? ''));
      return cmp * sortDir;
    });
    /* Reorder the DOM, then keep `all` in the same order so paging slices the
       sorted list rather than the original one. */
    for (const row of ordered) body.appendChild(row);
    all.length = 0;
    all.push(...ordered);
    root.querySelectorAll('th.sortable').forEach((th) => {
      const active = th.dataset.sort === key;
      th.classList.toggle('sorted', active);
      th.dataset.dir = active ? (sortDir > 0 ? 'asc' : 'desc') : '';
    });
    apply();
  }

  /* ---- wiring ----------------------------------------------------------- */
  root.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => sortBy(th.dataset.sort, th.dataset.type));
  });
  if (search) search.addEventListener('input', apply);
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      search.value = ''; apply(); search.focus();
    });
  }
  filters.forEach((f) => f.el.addEventListener('change', apply));
  chipGroups.forEach((g) => {
    g.els.forEach((btn) => {
      btn.addEventListener('click', () => {
        g.value = btn.dataset.f;
        g.els.forEach((b) => b.classList.toggle('on', b === btn));
        apply();
      });
    });
  });
  if (perSelect) {
    perSelect.addEventListener('change', () => {
      try { if (storageKey) localStorage.setItem(storageKey, perSelect.value); }
      catch (_) { /* nothing to do */ }
      page = 1;
      render();
    });
  }

  apply();

  return {
    refresh: apply,
    render,
    sortBy,
    get visible() { return rows.slice(); },
    get total() { return all.length; },
  };
}
