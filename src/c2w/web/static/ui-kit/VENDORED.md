# Vendored: Console UI kit

Copied here **unmodified** from the kit the customer supplied. Do not edit
these files: change `../app.css` for anything application-specific, and
`tokens.css` only when retheming (which is the one file a retheme touches).

Re-sync from source:

```sh
cp /path/to/ui-kit/*.css      src/c2w/web/static/ui-kit/
cp /path/to/ui-kit/js/*.js    src/c2w/web/static/ui-kit/js/
```

Then diff to confirm nothing local was lost:

```sh
diff -r /path/to/ui-kit src/c2w/web/static/ui-kit --exclude VENDORED.md
```

## What c2w uses

| File | Used by |
|---|---|
| `kit.css` | every page, via `base.html` and `login.html` |
| `js/data-table.js` | the audit log — search, chips and paging over rendered rows |
| `js/poll.js` | the sync page — watches the queue without reading a failed request as "finished" |
| `js/humanize.js` | the sync page, so a figure updated by script matches the server's formatting |
| `js/autosave.js` | not yet used. The settings page posts a row at a time |
| `js/bulk-select.js` | not yet used. There are no bulk operations |

The calls table is **not** driven by `data-table.js`, deliberately: it is
paged and filtered on the server. The kit's own guidance (UX-NOTES §8) is that
in-page filtering is right up to a few thousand rows and that past that the
page weight, not the filtering, is the problem — and these buckets hold
millions of recordings.

## Its test

`test/data-table.test.mjs` needs Node:

```sh
cd src/c2w/web/static/ui-kit/test && node data-table.test.mjs
```

`styleguide.html` from the source kit is the check on the stylesheet: open it
and look, in both themes and at 400px wide.
