# Console UI kit

A framework-agnostic extraction of the Mailbox Cold Storage console: the
stylesheet, the interaction logic, and the reasoning behind both.

Plain CSS and ES modules. No build step, no bundler, no framework, no
dependencies. Drop it into Jinja, Django, Rails, Astro, PHP or a static file
and it works; wrap it in React or Vue later if you want to.

It is built for **operator consoles** — dense tables, forms with consequences,
long-running jobs, states that mean something. It is not a marketing-site kit.

```
kit.css            import this one file
tokens.css         palette, type, elevation — retheme here and nowhere else
base.css           reset, page frame, header, nav, typography
forms.css          labels, inputs, buttons, checkrows
components.css     card pill chip flash note stats steps bar meter bulkbar
data-table.css     table, cell idioms, sortable headings, pager
narrow.css         phone rules, incl. the stacked-table pattern (load last)

js/data-table.js   live search + filter + sort + paging over rendered rows
js/autosave.js     save-on-change cards, with the repaint trap documented
js/bulk-select.js  selection scoped to what the filters leave visible
js/poll.js         watch a long-running job without treating a blip as done
js/humanize.js     byte/number/relative-time formatting

test/              node test for the data-table logic — `node data-table.test.mjs`
styleguide.html    every component rendered, in light and dark
COMPONENTS.md      markup for each pattern
UX-NOTES.md        why it is built this way. Read this one.
```

## Use it

```html
<link rel="stylesheet" href="/ui-kit/kit.css">
```

`kit.css` is `@import`s in dependency order. For production, concatenate the
six files **in that order** — tokens define what everything else resolves to,
and `narrow.css` is all overrides.

Then wire whatever behaviour a page needs:

```html
<script type="module">
  import { createDataTable } from '/ui-kit/js/data-table.js';

  createDataTable({
    table: '#things',
    rows: 'tr.row',
    search: {el: '#q', key: 'search'},          // matches row.dataset.search
    searchClear: '#qclear',
    filters: [{el: '#f-tenant', key: 'tenant'}], // matches row.dataset.tenant
    chips: [{
      selector: '.stfilter',
      match: (row, v) => v === 'all' || row.dataset.state === v,
      counts: {all: '[data-count="all"]', full: '[data-count="full"]'},
    }],
    page: {el: '#things-pager', per: '#things-per',
           storageKey: 'things.per', defaultSize: 25},
    labels: {
      noneYet: 'No things yet.',
      noneMatch: 'No thing matches these filters.',
      showing: (a, b, n) => `Showing ${a}–${b} of ${n} thing(s)`,
    },
  });
</script>
```

Rows describe themselves with `data-*` attributes, which your server already
knows when it renders them:

```html
<tr class="row" data-search="ada@example.com engineering"
                data-tenant="acme" data-state="full" data-size="80423">
```

Nothing in the kit knows what a tenant is. Filtering reads attributes.

### The other modules

```js
import { autosaveAll } from '/ui-kit/js/autosave.js';
autosaveAll('.item', {
  url: (card) => `/things/${card.dataset.id}/save.json`,
  // Repaint anything the server rendered from a field you just changed —
  // see UX-NOTES §6, this is the trap.
  onSaved: (card, d) => {
    card.querySelector('.statepill').innerHTML =
      `<span class="pill ${d.enabled ? 'ok' : 'idle'}">${d.state}</span>`;
    card.querySelector('.runbtn').disabled = !d.ready;
  },
});

import { bulkSelect } from '/ui-kit/js/bulk-select.js';
const bulk = bulkSelect({
  items: '.item', checkbox: '.pick', bar: '#bulkbar', count: '#bulkn',
  selectAll: '#selectall', selectNone: '#selectnone', clear: '#bulkclear',
});
// call bulk.refresh() from createDataTable's onRender so filtering deselects

import { poll } from '/ui-kit/js/poll.js';
poll({
  url: '/jobs/current.json',
  active: () => document.getElementById('job')?.dataset.active === 'yes',
  onTick: (d) => { bar.style.width = d.percent + '%'; },
  // default onDone reloads the page, which is usually right
});
```

## Retheming

Change `tokens.css`. Nothing else declares a literal colour, so a new palette
is that one file. Keep the roles intact:

- `--accent` paints; `--accent-ink` writes (it is the contrast-safe one for
  text); `--accent-wash` tints a background.
- Each state has an ink and a wash. The wash is **only** ever a background and
  the ink **only** ever text or a border — that is what keeps contrast safe in
  both themes without a second set of tokens.
- Dark mode is a re-tint, not a second stylesheet. Note the inversion: on a
  dark ground `--accent-ink` is *lighter* than `--accent`, because it still has
  to be the readable one.

Dark follows `prefers-color-scheme` by default. For an explicit switch, set
`data-theme="dark"` or `"light"` on `<html>`.

## Naming

Class names are generic — `.item`, `.group`, `.meter` — because in the source
app they were `.mbx`, `.tenantblock` and `.usage`. If your domain has better
names, rename in the CSS rather than putting a second set of classes on the
elements.

| Kit | Was | Is |
|---|---|---|
| `.item` | `.mbx` | one configurable thing in a list |
| `.item-form` / `.item-actions` | `.mbxform` / `.mbxrun` | its form and its action row |
| `.group` / `.group-bar` | `.tenantblock` / `.tenantbar` | a heading that groups items |
| `.meter` | `.usage` | a bar whose colour means something |
| `.summarygrid` | `.targetbox` | short strings, not numbers |
| `.summarybar` | `.storagebar` | summary left, action right |
| `.select-danger` | `.drylabel` | a select whose live value destroys |

## Tests

```bash
cd test && node data-table.test.mjs
```

Covers `pageWindow` elision, page slicing including a partial last page,
`.lastrow`, filter-resets-to-page-1, the two empty states, and sort direction
toggling with paging applied on top. It uses a small DOM stub — there is no
browser dependency.

The stylesheet is not tested. `styleguide.html` is the check: open it and look,
in both themes and at 400px wide.

## What is not here

Deliberately: no icons (the source used two arrow glyphs and text), no modal
or dialog, no toast (flashes are server-rendered at the top of the page), no
date picker, no charting. Add them in your app rather than growing the kit —
each one wants its own opinions and none of them were needed.
