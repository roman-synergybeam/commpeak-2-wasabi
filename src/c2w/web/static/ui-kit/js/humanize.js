/* ============================================================================
   Formatting helpers.
   These exist because a number rendered one way on the server and another way
   in the browser is a bug report waiting to happen: the same mailbox reads
   "1.4 GB" on load and "1.40 GiB" after an autosave. Mirror whatever your
   server-side formatter does, and use only this on the client.
   ============================================================================ */

/* Bytes at 1024, one decimal from MB up. Below a megabyte a decimal is noise. */
export function humanBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return (i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}

/* Thousands separators, and tabular-nums in the CSS, so a column of figures
   lines up and a counter does not jitter as it climbs. */
export function humanNumber(n) {
  return (Number(n) || 0).toLocaleString();
}

/* "1 mailbox" / "2 mailboxes". Pass the plural where English will not just
   take an s. */
export function plural(n, one, many) {
  return `${humanNumber(n)} ${n === 1 ? one : (many || one + 's')}`;
}

/* Short relative time. Deliberately coarse: on an ops screen "3m ago" is the
   useful precision, and anything finer redraws for no reason. */
export function ago(iso) {
  const then = new Date(iso).getTime();
  if (!then) return '';
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.round(m / 60);
  if (h < 48) return h + 'h ago';
  return Math.round(h / 24) + 'd ago';
}
