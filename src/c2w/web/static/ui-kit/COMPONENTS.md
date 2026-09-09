# Components

Markup for every pattern in the kit. Copy the markup, keep the class names.
Nothing here needs JavaScript unless it says so.

Class names are deliberately generic — `.item`, `.group`, `.meter` — because
the kit was extracted from an app where they were `.mbx`, `.tenantblock` and
`.usage`. If your domain has better names, rename in the CSS rather than
adding a second set of classes on the elements.

---

## Page frame

```html
<header class="top">
  <div class="brand">Product name<span>what it does</span></div>
  <nav class="top">
    <a href="/" class="on">Dashboard</a>
    <a href="/things">Things</a>
  </nav>
  <div class="who">
    ada@example.com
    <form method="post" action="/logout" class="inline">
      <button class="ghost small" type="submit">Sign out</button>
    </form>
  </div>
</header>

<main>            <!-- or class="measured" for forms, "narrow" for sign-in -->
  <h1>Things</h1>
  <p class="sub">What this page is for, in one line.</p>
  ...
</main>
```

Three widths, because one does not fit three kinds of page:

| Class | Width | For |
|---|---|---|
| *(none)* | 1680px | data tables — they want the room |
| `.measured` | 1100px | forms; prose and inputs read badly at 1600px |
| `.narrow` | 440px | sign-in, single-purpose dialogs |

Second-level navigation uses the same underline idiom one level down, so the
underline always means "you are here":

```html
<div class="subnav">
  <a href="/settings" class="on">Overview</a>
  <a href="/settings/keys">Keys</a>
</div>
```

## Card

The default container. Everything lives in one.

```html
<div class="card">
  <h2>Heading</h2>
  <p class="sub">Optional standfirst.</p>
  ...
</div>
```

## Flash

The result of what you just did. Top of the page, gone on next navigation.

```html
<div class="flash ok">Saved.</div>
<div class="flash warn">Two mailboxes were skipped.</div>
<div class="flash err">Could not reach the storage account.</div>
<div class="flash">Neutral, accent-tinted.</div>
```

## Note

An explainer, **not** an alarm — default is the accent wash. `.caution`
(amber) is reserved for the irreversible.

```html
<div class="note">This runs nightly at 02:00 UTC.</div>
<div class="note caution">Permanent delete cannot be undone.</div>
```

> Why: painted in the warning palette, a permanent explanatory paragraph
> looks like something has gone wrong and gets asked about. A box that cries
> wolf on every page view teaches people to skip the one that means it.

## Pill

A state, not a label. Four fixed meanings.

```html
<span class="pill ok">running</span>
<span class="pill warn">ready, off</span>
<span class="pill err">failed</span>
<span class="pill idle">needs setup</span>
```

If a new state seems to need a fifth colour, it probably needs to collapse
into one of these instead.

## Stat tiles

```html
<div class="stats">
  <div>
    <p class="hint">Messages archived</p>
    <strong>11,526<span class="unit">msg</span></strong>
  </div>
  <div>
    <p class="hint">Failed</p>
    <strong class="bad">3</strong>
  </div>
</div>
```

The caption is not optional: `REMOVED 11,526` reads as a size as readily as a
count, and the two can differ by three orders of magnitude. The caption has a
fixed `min-height` so numbers stay aligned across a row when one caption wraps
to two lines.

## Summary grid

Like `.stats`, but for short strings — what a thing is currently wired to,
rather than how much of it there is.

```html
<div class="summarygrid">
  <div><span class="hint">Alerts go to</span><strong>ops@example.com</strong></div>
  <div><span class="hint">Bucket</span><strong>archive-eu-1</strong></div>
</div>
```

## Steps

A setup checklist that remembers what is done.

```html
<ul class="steps">
  <li>
    <span class="mark done">✓</span>
    <div><div class="lbl">Create an operator</div>
         <div class="det">ada@example.com</div></div>
    <span class="go"><a class="btn ghost small" href="#">Change</a></span>
  </li>
  <li>
    <span class="mark">2</span>
    <div><div class="lbl">Connect a tenant</div></div>
  </li>
  <li>
    <span class="mark opt">!</span>
    <div><div class="lbl">Telegram alerts</div>
         <div class="det">Optional.</div></div>
  </li>
</ul>
```

`.mark.opt` is amber, not numbered — an optional step that is not done is
worth noticing, and a number would imply it blocks the steps after it.

## Progress bar and meter

```html
<!-- plain progress -->
<div class="bar"><span style="width:64%"></span></div>
<div class="barline">
  <span><strong>3,204</strong> done</span>
  <span><strong>1,796</strong> left</span>
</div>

<!-- a meter, whose colour means something -->
<div class="meter">
  <div class="bar"><span class="bad" style="width:97%"></span></div>
  <span class="pc bad">97%</span>
  <span class="abs">48.2 GB of 50 GB</span>
</div>
```

Set `.warn` / `.bad` on **both** the fill and the percentage — the number has
to survive being read without the bar.

## Forms

```html
<div class="card">
  <form method="post">
    <div class="row">
      <div><label for="name">Name</label><input id="name" name="name"></div>
      <div><label for="region">Region</label>
           <select id="region" name="region"><option>eu-central-1</option></select></div>
      <div class="wide">
        <label for="key">Access key</label>
        <input id="key" name="key" class="mono">
        <p class="hint">Stored encrypted; shown once.</p>
      </div>
    </div>

    <div class="checkrow">
      <label><input type="checkbox" name="verify" checked> Verify after upload</label>
      <label><input type="checkbox" name="notify"> Notify on failure</label>
    </div>

    <label class="select-danger">Mode
      <select name="mode" class="live">
        <option>Dry run</option><option>Live — deletes mail</option>
      </select>
    </label>

    <div class="actions">
      <button type="submit">Save</button>
      <button type="button" class="ghost">Cancel</button>
      <button type="button" class="danger">Delete</button>
    </div>
  </form>
</div>
```

`.select-danger` is a select rather than a checkbox on purpose: an unticked
checkbox submits nothing at all, which is indistinguishable from a caller that
never mentioned the field. Forcing a choice makes "destroy" something you
picked, not something you left. Add `.live` to the select when the destructive
option is the current one.

## Data table

Always wrap a table in `.tw` — the wrapper carries the border **and** the
horizontal scroll. Without it a wide table hangs out of its card.

```html
<div class="tw stack">
  <table id="things">
    <tr>
      <th class="sortable" data-sort="name" data-type="text">Name</th>
      <th class="sortable right" data-sort="size" data-type="num">Size<span class="unit">bytes</span></th>
      <th>Status</th>
      <th class="right">Actions</th>
    </tr>
    <tr class="row" data-name="ada" data-size="80423"
        data-search="ada@example.com engineering" data-tenant="acme">
      <td data-label="Name">
        ada@example.com
        <div class="idline">6f1c…-9b2a</div>
      </td>
      <td class="right" data-label="Size">78.5 KB</td>
      <td class="statuscell" data-label="Status">
        <div class="pills"><span class="pill ok">archiving</span></div>
        <div class="det wrap">Last run finished 3m ago.</div>
      </td>
      <td data-label="">
        <div class="rowactions">
          <button class="ghost small">Run</button>
          <details class="more"><summary>More</summary>
            <div class="menu"><button class="danger small">Delete</button></div>
          </details>
        </div>
      </td>
    </tr>
  </table>
</div>
```

Add `.stack` to `.tw` when the table has too many columns to scroll sideways
usefully. Below 820px each row becomes a small card and each cell takes its
heading from `data-label` — so **every `<td>` in a `.stack` table needs a
`data-label`**, which your server already knows when it renders the row. An
action cell that should show no label gets `data-label=""`.

Cell idioms: `.right` (shrink-to-fit numerics), `.idline` (a secondary
identifier), `.statuscell` (pills then explanation), `.msgcell` (the cell that
is the point of the row), `.clip` (ellipsis rather than widening the table),
`.det` / `.det.wrap` (detail text under the value).

## Pager

```html
<div class="pager" id="things-pager" hidden>
  <label for="things-per">Rows</label>
  <select id="things-per"><option>25</option><option>50</option><option>100</option></select>
  <div class="pagebtns"></div>
  <span class="range"></span>
</div>
```

Buttons are generated by `js/data-table.js`. The container starts `hidden` so
it does not flash empty before the script runs.

## Filter chips

```html
<div class="filters">
  <button class="chip on" data-f="all">All <span class="n" data-count="all">131</span></button>
  <button class="chip" data-f="full">Full <span class="n zero" data-count="full">0</span></button>
  <div class="chip-search">
    <input id="q" placeholder="Search…">
    <button class="chip" id="qclear" hidden>Clear</button>
  </div>
</div>
```

A chip is used as both `<a>` and `<button>`, so `.chip` restates its own
background — otherwise the button rule paints every chip as if selected.
Counts are computed over **everything**, not over what survives the other
filters: a chip reading 0 because a different chip is active tells you nothing.

## Bulk bar

```html
<div class="card bulkbar" id="bulkbar" hidden>
  <div class="bulkcount"><strong id="bulkn">0</strong> selected</div>
  <div class="row">
    <div><label for="bulk-policy">Policy</label>
         <select id="bulk-policy"><option>— leave unchanged —</option></select></div>
  </div>
  <div class="actions">
    <button>Apply</button>
    <button class="ghost" id="bulkclear">Clear</button>
  </div>
</div>
```

Sticky, so the count and the action stay with the selection as you scroll.

## Item card

For a thing with too many fields to be a table row.

```html
<div class="card item" data-id="42" data-tenant="acme"
     data-search="ada@example.com" data-kind="user">
  <div class="row">
    <div><label>Policy</label><select>…</select></div>
  </div>
  <div class="item-actions">
    <span class="savemark hint"></span>
    <button class="ghost small">Run now</button>
    <p class="hint">Runs on the next sweep.</p>
  </div>
</div>
```

## Misc

```html
<p class="empty">No runs yet.</p>

<details class="quiet">
  <summary>Reference: object key format</summary>
  …
</details>

<div class="qr"><svg>…</svg></div>   <!-- always on white; inverted QRs do not scan -->
```
