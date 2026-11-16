// Guided tours: one shared menu ("Tours" in the panel) and one shared player
// (caption, play/pause, progress bar, close) used by every tour.
//
// A tour registers itself with
//   MC3D.tours.register({ id, label, start(), stop() })
// and gets a button in the menu. Only one tour runs at a time. While a tour
// runs, <body> has the class "touring", which hides the normal controls.
// Opening the page with ?tour=<id> starts that tour straight away.
(function () {
  const M = window.MC3D;
  if (!M) return;

  const tours = new Map();
  let active = null;
  const group = document.getElementById("tours-group");
  const row = document.getElementById("tours");
  const wanted = new URLSearchParams(location.search).get("tour");

  function stop() {
    if (!active) return;
    const t = active;
    active = null;
    document.body.classList.remove("touring");
    hud.hide();
    t.stop();
  }

  function start(id) {
    const t = tours.get(id);
    if (!t) return;
    stop();
    active = t;
    document.body.classList.add("touring");
    t.start();
  }

  function register(t) {
    tours.set(t.id, t);
    const b = document.createElement("button");
    b.className = "tour-btn";
    b.textContent = t.label;
    b.onclick = () => start(t.id);
    row.appendChild(b);
    group.hidden = false;
    if (wanted === t.id) setTimeout(() => start(t.id), 1200);
  }

  // ---- Player ---------------------------------------------------------
  const el = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const root = el("div", "tour-hud", document.body);
  root.hidden = true;
  const caption = el("div", "tour-caption", root);
  const kicker = el("div", "tour-kicker", caption);
  const title = el("div", "tour-title", caption);
  const note = el("div", "tour-note", caption);
  const bar = el("div", "tour-bar", root);
  const playBtn = el("button", "tour-play", bar);
  playBtn.setAttribute("aria-label", "Play or pause");
  const track = el("div", "tour-track", bar);
  const rail = el("div", "tour-rail", track);
  const fill = el("div", "tour-fill", rail);
  const marks = el("div", "tour-marks", rail);
  const closeBtn = el("button", "tour-close", bar, "✕");
  closeBtn.setAttribute("aria-label", "Close the tour");
  closeBtn.title = "Close the tour";
  const tip = el("div", "tour-tip", root);
  const endCard = el("div", "tour-end", root);
  endCard.hidden = true;
  const endKicker = el("div", "tour-kicker", endCard);
  const endTitle = el("div", "tour-title", endCard);
  const endNote = el("div", "tour-note", endCard);
  const endRow = el("div", "tour-end-row", endCard);
  const againBtn = el("button", "on", endRow, "Watch again");
  const exploreBtn = el("button", "", endRow, "Explore Monaco");

  const ICON_PLAY = '<svg width="18" height="18" viewBox="0 0 18 18"><path d="M5 3 L15 9 L5 15 Z" fill="currentColor"/></svg>';
  const ICON_PAUSE = '<svg width="18" height="18" viewBox="0 0 18 18"><path d="M4 3h3.5v12H4zM10.5 3H14v12h-3.5z" fill="currentColor"/></svg>';
  let handlers = {};
  let captionKey = "";

  playBtn.onclick = () => handlers.onToggle && handlers.onToggle();
  closeBtn.onclick = stop;
  exploreBtn.onclick = stop;
  againBtn.onclick = () => { endCard.hidden = true; if (handlers.onReplay) handlers.onReplay(); };

  // Tap or drag on the progress bar to jump.
  function seek(e) {
    const r = rail.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    if (handlers.onSeek) handlers.onSeek(f);
  }
  track.addEventListener("pointerdown", (e) => {
    track.setPointerCapture(e.pointerId);
    seek(e);
    const move = (ev) => seek(ev);
    const up = () => {
      track.removeEventListener("pointermove", move);
      track.removeEventListener("pointerup", up);
      track.removeEventListener("pointercancel", up);
    };
    track.addEventListener("pointermove", move);
    track.addEventListener("pointerup", up);
    track.addEventListener("pointercancel", up);
  });
  window.addEventListener("keydown", (e) => {
    if (!active) return;
    if (e.key === "Escape") stop();
    if (e.key === " ") { e.preventDefault(); if (handlers.onToggle) handlers.onToggle(); }
  });

  const hud = {
    root,
    // h: { onToggle, onSeek(fraction), onReplay, marks: [fractions 0..1] }
    show(h) {
      handlers = h || {};
      marks.textContent = "";
      for (const f of handlers.marks || []) el("i", "", marks).style.left = `${f * 100}%`;
      endCard.hidden = true;
      root.classList.remove("ended");
      root.hidden = false;
      hud.progress(0);
      hud.playing(false);
      hud.tip("");
    },
    hide() { root.hidden = true; handlers = {}; captionKey = ""; },
    // Change the caption with a short fade; does nothing if it is the same.
    caption(k, t, n) {
      const key = `${k}|${t}|${n}`;
      if (key === captionKey) return;
      captionKey = key;
      caption.classList.remove("in");
      void caption.offsetWidth;          // restart the animation
      kicker.textContent = k || "";
      title.textContent = t || "";
      note.textContent = n || "";
      caption.classList.add("in");
    },
    progress(f) { fill.style.width = `${Math.min(1, Math.max(0, f)) * 100}%`; },
    playing(on) {
      playBtn.innerHTML = on ? ICON_PAUSE : ICON_PLAY;
      playBtn.title = on ? "Pause" : "Play";
      root.classList.toggle("playing", on);
    },
    tip(text) { tip.textContent = text || ""; tip.classList.toggle("show", !!text); },
    finish(k, t, n) {
      endKicker.textContent = k || "";
      endTitle.textContent = t || "";
      endNote.textContent = n || "";
      endCard.hidden = false;
      root.classList.add("ended");
      hud.playing(false);
      hud.tip("");
    },
  };

  M.tours = { register, start, stop, hud, isActive: (id) => !!active && active.id === id };
})();
