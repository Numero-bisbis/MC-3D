// Guided tour of Le Rocher (Monaco-Ville), the old town on the Rock.
//
// Plugs into the shared tours menu (tours.js): MC3D.tours.register(...).
// The visitor steps through the stops with Back / Next on a caption card;
// at each stop the camera slowly circles the landmark until they touch the
// globe. Heights (h) are metres above sea level; heading is the direction
// the camera looks, pitch its tilt, range its distance from the landmark.
/* global Cesium, MC3D */
(function () {
  const STOPS = [
    {
      title: "Le Rocher",
      text: "Monaco-Ville, the old town, sits on a rock 60 metres above the sea. "
        + "François Grimaldi took its fortress in 1297, disguised as a monk; "
        + "his family still reigns today.",
      lon: 7.4225, lat: 43.7310, h: 50, range: 1100, pitch: -24, heading: 40, duration: 5,
    },
    {
      title: "The Prince's Palace",
      text: "Home of the Grimaldis, the Palace began as a Genoese fortress in 1215. "
        + "The Changing of the Guard takes place on the square in front every day at 11:55.",
      lon: 7.41982, lat: 43.73126, h: 62, range: 450, pitch: -24, heading: 260,
    },
    {
      title: "Place du Palais",
      text: "The palace square, lined with old cannons. From its ramparts you look "
        + "straight down on Port Hercule and across to Monte-Carlo.",
      lon: 7.42138, lat: 43.73090, h: 60, range: 480, pitch: -16, heading: 35,
    },
    {
      title: "Monaco Cathedral",
      text: "Built from 1875 in white stone from La Turbie, the Cathedral of Our Lady "
        + "Immaculate is where Princess Grace and Prince Rainier III are buried.",
      lon: 7.42265, lat: 43.73025, h: 60, range: 390, pitch: -26, heading: 290,
    },
    {
      title: "Saint-Martin Gardens",
      text: "Pine-shaded paths and lookouts along the cliff top, with the open "
        + "Mediterranean below.",
      lon: 7.42428, lat: 43.73004, h: 50, range: 480, pitch: -20, heading: 350,
    },
    {
      title: "Oceanographic Museum",
      text: "Opened in 1910 by Prince Albert I, the explorer prince. Its façade rises "
        + "straight out of the cliff, 85 metres above the sea. Jacques-Yves Cousteau "
        + "directed it from 1957 to 1988.",
      lon: 7.42551, lat: 43.73068, h: 40, range: 540, pitch: -12, heading: 330,
    },
    {
      title: "Fort Antoine",
      text: "An early 18th-century fort at the tip of the Rock, now an open-air "
        + "theatre for summer evenings by the sea.",
      lon: 7.42794, lat: 43.73308, h: 35, range: 450, pitch: -24, heading: 240,
    },
    {
      title: "Back to the view",
      text: "That's the Rock. Explore freely, or pick a place or another tour.",
      lon: 7.4230, lat: 43.7340, h: 50, range: 1600, pitch: -28, heading: 220, duration: 5,
    },
  ];

  // Caption card.
  const card = document.createElement("div");
  card.className = "rt-card";
  card.hidden = true;
  card.innerHTML = `
    <div class="rt-head"><span class="rt-step"></span>
      <button class="rt-close" aria-label="End tour" title="End tour">✕</button></div>
    <div class="rt-title"></div>
    <div class="rt-text"></div>
    <div class="rt-buttons"><button class="rt-prev">Back</button><button class="rt-next">Next</button></div>`;
  document.body.appendChild(card);
  const $ = (s) => card.querySelector(s);

  let running = false, index = 0, seq = 0, endDrift = null, unframe = null;

  function viewer() { return MC3D.viewer; }

  // Keep the landmark in the free part of the screen, between whatever
  // panel is still showing at the top and the card at the bottom, by
  // shifting the projection rather than the camera.
  function frameAboveCard() {
    const v = viewer(), f = v.camera.frustum;
    const remove = v.scene.preRender.addEventListener(() => {
      if (!(f instanceof Cesium.PerspectiveFrustum)) return;
      const H = v.scene.canvas.clientHeight;
      const panel = document.querySelector(".panel");
      const top = panel && panel.offsetParent ? Math.max(panel.getBoundingClientRect().bottom, 0) : 0;
      const bottom = card.hidden ? H : Math.min(card.getBoundingClientRect().top, H);
      const k = Cesium.Math.clamp((H / 2 - (top + bottom) / 2) / (H / 2), -0.5, 0.5);
      const want = -k * f.near * Math.tan(f.fovy / 2);
      if (Math.abs(f.yOffset - want) > 1e-9) f.yOffset = want;
    });
    return () => { remove(); f.yOffset = 0; };
  }

  function target(s) {
    return Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.h + MC3D.GEOID_N);
  }

  function offset(s, heading) {
    return new Cesium.HeadingPitchRange(
      heading ?? Cesium.Math.toRadians(s.heading), Cesium.Math.toRadians(s.pitch), s.range);
  }

  // Slowly circle the landmark. A touch, click or scroll on the globe hands
  // the camera back to the visitor.
  function drift(s) {
    const v = viewer(), cam = v.camera, canvas = v.scene.canvas;
    const t = target(s);
    let heading = Cesium.Math.toRadians(s.heading), last = performance.now();
    const speed = Cesium.Math.toRadians(2.5);  // per second
    const remove = v.scene.preRender.addEventListener(() => {
      const now = performance.now();
      heading += speed * Math.min(now - last, 100) / 1000;
      last = now;
      cam.lookAt(t, offset(s, heading));
    });
    const end = () => {
      remove();
      cam.lookAtTransform(Cesium.Matrix4.IDENTITY);
      canvas.removeEventListener("pointerdown", end);
      canvas.removeEventListener("wheel", end);
      if (endDrift === end) endDrift = null;
    };
    canvas.addEventListener("pointerdown", end);
    canvas.addEventListener("wheel", end);
    return end;
  }

  function show(k) {
    if (!running) return;
    if (k >= STOPS.length) { MC3D.tours.stop ? MC3D.tours.stop() : stop(); return; }
    index = Math.max(0, k);
    const mine = ++seq;
    if (endDrift) endDrift();
    const s = STOPS[index];
    $(".rt-step").textContent = `${index + 1} / ${STOPS.length}`;
    $(".rt-title").textContent = s.title;
    $(".rt-text").textContent = s.text;
    $(".rt-prev").hidden = index === 0;
    $(".rt-next").textContent = index === STOPS.length - 1 ? "Finish" : "Next";
    card.hidden = false;
    viewer().camera.flyToBoundingSphere(new Cesium.BoundingSphere(target(s), 1), {
      offset: offset(s),
      duration: s.duration ?? 4,
      complete: () => { if (running && mine === seq) endDrift = drift(s); },
    });
  }

  function start() {
    running = true;
    unframe = frameAboveCard();
    show(0);
  }

  function stop() {
    if (!running) return;
    running = false;
    seq++;
    if (endDrift) endDrift();
    if (unframe) { unframe(); unframe = null; }
    const cam = viewer().camera;
    cam.cancelFlight();
    cam.lookAtTransform(Cesium.Matrix4.IDENTITY);
    card.hidden = true;
  }

  $(".rt-prev").onclick = () => show(index - 1);
  $(".rt-next").onclick = () => show(index + 1);
  $(".rt-close").onclick = () => (MC3D.tours.stop ? MC3D.tours.stop() : stop());

  MC3D.tours.register({ id: "rocher", label: "Le Rocher tour", start, stop });
})();
