# UX notes

The reasoning behind the kit. Most of these are things that were built the
obvious way first, went wrong in a specific way, and were rebuilt. The
conclusions are cheap to carry into a new project; rediscovering them is not.

---

## 1. Colour is a vocabulary, not decoration

Four state colours — accent, good, warn, danger — and each one means exactly
one thing everywhere it appears:

- **accent** — "this is the current/active/primary one"
- **good / warn / danger** — state, and only state

The corollary is the rule that is easy to break: *never use a state colour
decoratively.* The original had an explanatory paragraph on the settings page
painted in the warning palette. It was not a warning. It sat there on every
page view, and people asked what was wrong. Worse, it devalued the amber that
did mean something. `.note` is now accent-tinted by default and `.note.caution`
is the opt-in.

If you find yourself wanting a fifth state colour, the state probably wants to
collapse into one of the four instead.

## 2. Say what the number counts

`REMOVED 11,526` reads as a size as readily as a count, and in that app the two
differed by three orders of magnitude. Every stat tile carries a caption, and
the caption has a fixed `min-height` so a row of tiles keeps its numbers
aligned when one caption wraps.

Same reason `th .unit` puts the unit *under* the column name rather than beside
it — beside it, the column grows to fit the word.

Use `font-variant-numeric: tabular-nums` on anything that updates in place. A
counter whose digits change width jitters while it climbs.

## 3. "Nothing yet" and "nothing matches" are different sentences

Only one of them is ever true, and the difference is the whole message. A list
that says "No mailboxes yet" while a filter is active is actively lying to
someone who is about to go and look for a missing mailbox.

`data-table.js` distinguishes them by comparing the filtered set against the
full set, not against zero.

## 4. Filter, sort and page belong to each other

They are one controller, not three. Filtering changes the set that paging
slices; sorting changes the order it slices in. Wired separately they drift:
you page to 3, filter down to four rows, and the table is empty because page 3
of one page is nothing.

The rule that keeps `showing 1–25 of N` honest: **rows the filters dropped are
hidden outright; the pager only ever hides rows that did match.** Two different
reasons to be hidden, one attribute, so the order of operations matters.

## 5. Selection is scoped to what you can see

The most dangerous thing in a bulk-edit UI is a selected row that scrolled out
of existence behind a filter. So: hiding a row clears its tick, and Select
all / Invert act on visible rows only.

That single rule makes "filter, then Select all" *the* bulk edit idiom, and
guarantees nothing off-screen is ever caught by it.

## 6. Autosave, and then repaint what the server rendered

Per-card Save buttons that each redirect will silently discard the other cards'
edits. Autosave removes the bug class.

But it creates a subtler one, and this is the note to re-read when adding a
field: **the card saved without a reload, so everything the server rendered
from the pre-save state is now stale.** In the original, switching a mailbox on
left its "Run now" button disabled until the next full page load — and a
disabled button does nothing when clicked and says nothing about why. Anything
derived from a field has to be repainted in the save callback.

Feedback is asymmetric on purpose: success is quiet and clears itself after two
seconds, because success is the normal case and a green flash per keystroke is
noise. Failure persists until it is fixed.

A `change` (select, checkbox) saves at once; typing debounces, because typing
is not finished until it pauses.

## 7. A blip is not a completed job

When polling a long-running job, a failed fetch must never be read as "done".
Back off and retry. The original polls every 3s, and 5s after an error.

When it genuinely finishes, reload rather than patching the page. Everything
rendered from the pre-run state is stale, and the honest way to show a dozen
changed rows is to show them.

## 8. Do the work in the page, until you cannot

With a few hundred rows already rendered, a round trip per keystroke *was* the
problem. Filtering client-side over `data-*` attributes made the list usable.

Know where this stops. It is right up to a few thousand rows; past that the
page weight — not the filtering — is the problem, and the server should page.
The give-away is time-to-first-byte, not sluggish typing.

Remember the page size in `localStorage`, wrapped in try/catch (a private
window has no storage). A list that resets to 25 every visit is one the
operator has to re-configure before they can read it.

## 9. On a phone, a wide table is not a table

The default — scroll the whole table sideways as one piece — is right up to
about six columns. Past that it is a sideways scroll into blank space: rows
with height and nothing in them.

`.tw.stack` turns each row into a small card below 820px, each cell labelled
from its `data-label`. It costs one attribute per cell, which the server
already knows. It is the single highest-value rule in the stylesheet.

Related, all learned from the same phone:

- Identifiers inside a cell get `white-space:nowrap` and an ellipsis. `code`
  breaks anywhere, which is right in prose and turns a 36-character GUID into
  five stacked characters per line in a table.
- Sentences inside a cell must opt back *in* to wrapping (`.det.wrap`), and be
  sized in `ch`, or one long message drags the table hundreds of pixels wide.
- Row actions wrap to a second line rather than widening the table, with the
  destructive ones folded behind a `<details>` — which expands **in place**,
  because `.tw` scrolls and a scrolling ancestor clips an absolutely
  positioned dropdown no matter what its z-index says.

## 10. The CSS traps worth knowing

These cost real debugging time:

- **`min-width:0` on grid and flex children.** Without it a track cannot shrink
  below its content, and one unbreakable number widens the entire page instead
  of shrinking. It appears on `.row>*`, `.stats>div`, `.growcol`, `.barline>span`.
- **`overflow-x:clip` on `body`, not `hidden`.** `hidden` makes body a scroll
  container, which silently breaks `position:sticky` on everything inside it.
- **`.wide` must be reset to one column on narrow screens.** An item spanning
  two columns of a one-column grid creates an implicit second column, and the
  whole page scrolls sideways.
- **`tr:last-child td{border-bottom:0}` fights paging.** Once a page ends
  mid-table the last child is a *hidden* row, leaving the visible last row with
  a stray border. The pager sets `.lastrow` on the last visible row.
- **A `<button>` rule with a background will paint your chips.** `.chip` is used
  as both `<a>` and `<button>`; it has to restate its own background or the
  whole filter row renders as though every option were selected.
- **`display:contents` on a `<form>` inside a flex row**, so a wrapping form
  does not become a flex item and break button spacing.

## 11. Two accessibility habits that cost nothing

`:focus-visible` outlines are defined once, globally, for every interactive
element — and never removed. Keyboard operation of a console is not a nice-to-
have when the mouse hand is holding a phone with the on-call rota on it.

Every transition is wrapped in `@media (prefers-reduced-motion:reduce)`.

## 12. System fonts

No font CDN. An internal tool has no guarantee of reaching one, and a silent
fallback to Times on an ops dashboard is worse than plainly choosing the system
stack in the first place.
