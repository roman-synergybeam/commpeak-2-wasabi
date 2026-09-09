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

  /* Route an element through a GainNode so it can exceed unity.
     Returns the node, or null when Web Audio is unavailable -- in which case
     the caller falls back to element.volume and simply cannot go past 100%. */
  function amplifier(audio) {
    if (wired.has(audio)) return wired.get(audio);
    var ctx = context();
    if (!ctx) return null;
    try {
      var src = ctx.createMediaElementSource(audio);
      var gain = ctx.createGain();
      src.connect(gain);
      gain.connect(ctx.destination);
      wired.set(audio, gain);
      return gain;
    } catch (e) {
      // createMediaElementSource throws if the element is already routed, or
      // if the media is cross-origin without CORS. Presigned Wasabi URLs are
      // another origin, so this is a real path, not a theoretical one: fall
      // back to element volume rather than losing audio altogether.
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
