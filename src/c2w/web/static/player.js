/* Playback volume, and the sliders that set it.
 *
 * The preference was being stored and then ignored: nothing read it back, so
 * every recording still started at whatever the browser felt like. This
 * applies it.
 *
 * Above 100% an <audio> element cannot help -- HTMLMediaElement.volume is
 * clamped to 1.0 and silently refuses anything larger. Call recordings are
 * genuinely quiet (narrowband, agc'd, often a speakerphone across a room), so
 * the ceiling here is 200% and everything over 100% goes through a Web Audio
 * GainNode instead. That graph is built lazily on first play, because a
 * suspended AudioContext created at page load is one more thing browsers
 * warn about and Safari refuses outright without a gesture.
 */
(function () {
  "use strict";

  var CTX = null;      // one AudioContext for the page; several is a leak
  var wired = new WeakMap();

  function pref() {
    var el = document.documentElement;
    var v = parseInt(el.getAttribute("data-volume"), 10);
    return isFinite(v) ? Math.max(0, Math.min(200, v)) : 100;
  }

  function context() {
    if (CTX) return CTX;
    var C = window.AudioContext || window.webkitAudioContext;
    if (!C) return null;            // no Web Audio: we stay in the 0-100 range
    try { CTX = new C(); } catch (e) { CTX = null; }
    return CTX;
  }

  /* Can this element legally go through a Web Audio graph?
   *
   * Only if the media is same-origin. Routing cross-origin media through
   * createMediaElementSource does not fail loudly -- the spec taints the graph
   * and it outputs **silence**. So the element played, the progress bar moved,
   * and nothing came out; downloading the same file and opening it in another
   * application worked perfectly, which is a maddening pair of symptoms.
   *
   * `/api/v1/recordings/<id>/stream` is same-origin but 302s to a presigned
   * archive URL, so `src` is the wrong thing to test. `currentSrc` is the URL
   * actually loaded, after redirects, which is the one that decides.
   */
  function sameOrigin(audio) {
    var url = audio.currentSrc || audio.src;
    if (!url) return false;              // not loaded yet: assume it is not
    try {
      return new URL(url, location.href).origin === location.origin;
    } catch (e) {
      return false;
    }
  }

  /* Cross-origin media may go through the graph only if it was fetched with
     CORS and the fetch succeeded. There is no API that reports "CORS was
     approved", but there does not need to be one: with crossOrigin set, a
     media element that failed CORS never reaches readyState >= 1 at all. So
     having metadata *is* the proof. */
  function corsApproved(audio) {
    return audio.crossOrigin === "anonymous" && audio.readyState >= 1;
  }

  function canAmplify(audio) {
    return sameOrigin(audio) || corsApproved(audio);
  }

  /* Route an element through a GainNode so it can exceed unity.
     Returns the node, or null when that cannot be done safely -- in which
     case the caller falls back to element.volume and cannot go past 100%. */
  function amplifier(audio) {
    if (wired.has(audio)) return wired.get(audio);
    var ctx = context();
    if (!ctx) return null;
    if (!canAmplify(audio)) {
      // Silence is worse than quiet. Remembered so the check is not repeated
      // on every timeupdate.
      wired.set(audio, null);
      return null;
    }
    try {
      var src = ctx.createMediaElementSource(audio);
      var gain = ctx.createGain();
      src.connect(gain);
      gain.connect(ctx.destination);
      wired.set(audio, gain);
      return gain;
    } catch (e) {
      // Already routed, or the browser refused. Either way, keep the audio.
      wired.set(audio, null);
      return null;
    }
  }

  function apply(audio, percent) {
    if (percent <= 100) {
      audio.volume = percent / 100;
      var g = wired.get(audio);
      if (g) g.gain.value = 1;
      return;
    }
    var gain = amplifier(audio);
    if (gain) {
      audio.volume = 1;
      gain.gain.value = percent / 100;
    } else {
      audio.volume = 1;   // as loud as this browser will go
    }
  }

  function attach(audio) {
    if (audio.dataset.c2wVolume === "1") return;
    audio.dataset.c2wVolume = "1";
    apply(audio, pref());
    // A gesture has happened by the time play fires, so this is where a
    // suspended context is allowed to start.
    audio.addEventListener("play", function () {
      if (CTX && CTX.state === "suspended") CTX.resume();
      apply(audio, pref());
    });
    // By this point `currentSrc` is the post-redirect URL, so the same-origin
    // question can finally be answered. Before it, the graph would have been
    // built on a guess.
    audio.addEventListener("loadedmetadata", function () {
      wired.delete(audio);
      apply(audio, pref());
    });
    /* If asking for CORS is what stopped the media loading, drop the request
       and load it again without. Quiet audio beats no audio, and a bucket
       whose CORS policy has been changed or removed must not silence
       playback -- which is exactly the failure this whole file exists to
       stop happening a second time. */
    audio.addEventListener("error", function () {
      if (audio.crossOrigin !== "anonymous" || audio.dataset.c2wRetried === "1") return;
      audio.dataset.c2wRetried = "1";
      audio.removeAttribute("crossorigin");
      wired.delete(audio);
      audio.load();
    });
  }

  function attachAll(root) {
    var list = (root || document).querySelectorAll("audio");
    for (var i = 0; i < list.length; i++) attach(list[i]);
  }

  /* The slider's own fill. The native control paints up to the middle of the
     thumb, which makes a maximum look short of the end; app.css draws the
     track from --fill instead. */
  function paint(input) {
    var min = parseFloat(input.min || 0);
    var max = parseFloat(input.max || 100);
    var val = parseFloat(input.value);
    var pct = max > min ? ((val - min) / (max - min)) * 100 : 100;
    input.style.setProperty("--fill", pct.toFixed(2) + "%");
  }

  function attachSliders(root) {
    var list = (root || document).querySelectorAll('input[type="range"]');
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      paint(el);
      if (el.dataset.c2wRange === "1") continue;
      el.dataset.c2wRange = "1";
      el.addEventListener("input", function (e) {
        paint(e.target);
        // A volume slider should be audible while you drag it, not after a
        // round trip to the server.
        if (e.target.id === "volume") {
          document.documentElement.setAttribute("data-volume", e.target.value);
          var players = document.querySelectorAll("audio");
          for (var j = 0; j < players.length; j++) apply(players[j], pref());
        }
      });
    }
  }

  function init(root) { attachAll(root); attachSliders(root); }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  } else {
    init(document);
  }
  // htmx swaps content in without a page load, so newly arrived players and
  // sliders would otherwise be inert.
  document.body && document.body.addEventListener("htmx:afterSwap", function (e) {
    init(e.target);
  });
})();
