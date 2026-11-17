// Street View stops: opens Google's own 360° Street View panorama of a
// place in an overlay, through the Maps Embed API (free, no usage limit).
// The imagery stays in Google's official viewer, as its terms require; it
// is never copied onto the 3D scene.
//
//   MC3D.streetView.open(spots, index, onClose)
//     spots: [{ lat, lng, heading, pitch?, kicker?, title }]
//     ‹ and › step through the list, so a whole tour can be walked.
//   MC3D.streetView.available  false without a Google key
//
// The key must have "Maps Embed API" enabled and allowed (see README).
/* global MC3D */
(function () {
  const M = window.MC3D;
  if (!M) return;
  const KEY = new URLSearchParams(location.search).get("gkey") ||
    (window.MC3D_CONFIG && window.MC3D_CONFIG.googleKey) || "";

  const el = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const root = el("div", "sv", document.body);
  root.hidden = true;
  const panel = el("div", "sv-panel", root);
  const head = el("div", "sv-head", panel);
  const names = el("div", "sv-names", head);
  const kicker = el("div", "sv-kicker", names);
  const title = el("div", "sv-title", names);
  const closeBtn = el("button", "sv-close", head, "✕");
  closeBtn.title = "Back to the 3D view";
  closeBtn.setAttribute("aria-label", "Back to the 3D view");
  const frameBox = el("div", "sv-frame", panel);
  const foot = el("div", "sv-foot", panel);
  const prevBtn = el("button", "sv-step", foot, "‹");
  prevBtn.setAttribute("aria-label", "Previous place");
  const count = el("span", "sv-count", foot);
  const maps = el("a", "sv-maps", foot, "Open in Google Maps");
  maps.target = "_blank";
  maps.rel = "noopener";
  const nextBtn = el("button", "sv-step", foot, "›");
  nextBtn.setAttribute("aria-label", "Next place");

  let spots = [], index = 0, done = null, frame = null;
  const fix = (v) => (+v).toFixed(6);

  function embedUrl(s) {
    const q = new URLSearchParams({
      key: KEY,
      location: `${fix(s.lat)},${fix(s.lng)}`,
      heading: Math.round(((s.heading || 0) % 360 + 360) % 360),
      pitch: Math.round(s.pitch || 0),
      fov: 90,
    });
    return `https://www.google.com/maps/embed/v1/streetview?${q}`;
  }
  function mapsUrl(s) {
    return "https://www.google.com/maps/@?api=1&map_action=pano"
      + `&viewpoint=${fix(s.lat)},${fix(s.lng)}&heading=${Math.round(s.heading || 0)}&pitch=${Math.round(s.pitch || 0)}`;
  }

  function show(k) {
    index = (k + spots.length) % spots.length;
    const s = spots[index];
    kicker.textContent = s.kicker || "Street View";
    title.textContent = s.title || "";
    count.textContent = spots.length > 1 ? `${index + 1} / ${spots.length}` : "";
    prevBtn.hidden = nextBtn.hidden = spots.length < 2;
    maps.href = mapsUrl(s);
    // A fresh iframe per place: changing src keeps the old panorama's
    // history and can flash it.
    if (frame) frame.remove();
    frame = el("iframe", "", frameBox);
    frame.title = `Street View: ${s.title || ""}`;
    frame.allow = "fullscreen; accelerometer; gyroscope";
    frame.allowFullscreen = true;
    // The key is limited to this site's address, so Google must see the
    // full page address, not just the domain.
    frame.referrerPolicy = "no-referrer-when-downgrade";
    frame.src = embedUrl(s);
  }

  function open(list, k, onClose) {
    if (!KEY || !list || !list.length) return;
    spots = list;
    done = onClose || null;
    show(k || 0);
    root.hidden = false;
    document.body.classList.add("street-view");
  }
  function close() {
    if (root.hidden) return;
    root.hidden = true;
    document.body.classList.remove("street-view");
    if (frame) { frame.remove(); frame = null; }
    const cb = done;
    done = null;
    if (cb) cb();
  }

  closeBtn.onclick = close;
  prevBtn.onclick = () => show(index - 1);
  nextBtn.onclick = () => show(index + 1);
  root.addEventListener("pointerdown", (e) => { if (e.target === root) close(); });
  // While the panorama is open, Escape closes it rather than the tour
  // behind it, and the arrow keys step through the places.
  window.addEventListener("keydown", (e) => {
    if (root.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft" && spots.length > 1) show(index - 1);
    else if (e.key === "ArrowRight" && spots.length > 1) show(index + 1);
    else if (e.key !== " ") return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);

  // A small "pegman" for the tour bars.
  const ICON = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><circle cx="10" cy="4.2" r="2.6"/><path d="M6.6 8.2h6.8l-.9 5.6h-1.6l-.5 4.4H9.6l-.5-4.4H7.5z"/></svg>';

  M.streetView = { available: !!KEY, open, close, ICON, get isOpen() { return !root.hidden; } };
})();
