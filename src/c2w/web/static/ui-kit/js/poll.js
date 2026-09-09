/* ============================================================================
   Poll a JSON endpoint while something long-running is happening.

   The problem it solves: a job runs in a worker over minutes, so the page has
   to go and look. Without it the console shows "queued" and then, whatever
   happened, nothing at all.

   Two rules learned the hard way and encoded here:
     - A failed fetch is a blip, not a finished job. Back off and retry; never
       treat a network error as completion.
     - When it finishes, reload rather than patching the page. Everything the
       server rendered from the pre-run state is now stale, and reloading is
       the honest way to show the result.
   ============================================================================ */

export function poll({
  url,
  active = () => true,      // called before starting; skip entirely if false
  onTick,                   // (data) => void, every successful poll
  done = (d) => !d.active,  // (data) => boolean, is it finished?
  onDone,                   // (data) => void; defaults to a full reload
  interval = 3000,
  errorInterval = 5000,
  startDelay = 2000,
} = {}) {
  if (!active()) return () => {};

  let stopped = false;
  let timer = null;
  const stop = () => { stopped = true; clearTimeout(timer); };

  async function tick() {
    if (stopped) return;
    let data;
    try {
      const res = await fetch(url, {headers: {'Accept': 'application/json'}});
      if (!res.ok) throw new Error(res.status);
      data = await res.json();
    } catch (_) {
      // A blip is not a finished job.
      timer = setTimeout(tick, errorInterval);
      return;
    }
    if (stopped) return;
    if (onTick) onTick(data);
    if (!done(data)) {
      timer = setTimeout(tick, interval);
      return;
    }
    if (onDone) onDone(data);
    else window.location.reload();
  }

  timer = setTimeout(tick, startDelay);
  return stop;
}
