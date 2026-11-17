// Gaussian splat showpieces: photoreal 3D captures (drone or phone video)
// placed on the map as 3D Tiles. Built with pipeline/build_splat.py.
//
// Open the page with ?splat=<name> to load viewer/splats/<name>/tileset.json
// and fly to it, or ?splat=<full tileset URL>. Needs CesiumJS 1.139 or newer.
(function () {
  const M = window.MC3D;
  if (!M) return;
  const wanted = new URLSearchParams(location.search).get("splat");
  if (!wanted) return;
  const url = /^https?:|\.json$/.test(wanted) ? wanted : `splats/${wanted}/tileset.json`;

  Cesium.Cesium3DTileset.fromUrl(url, { maximumScreenSpaceError: 1 })
    .then((ts) => {
      M.viewer.scene.primitives.add(ts);
      M.splat = ts;
      // A splat is meant to be seen up close: let the camera come in to a
      // few metres, and fly there once the opening glide has finished.
      M.viewer.scene.screenSpaceCameraController.minimumZoomDistance = 3;
      const s = ts.boundingSphere;
      setTimeout(() => M.viewer.camera.flyToBoundingSphere(s, {
        duration: 3,
        offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-15), s.radius * 1.4),
      }), 1800);
    })
    .catch((e) => console.error("[splat] could not load", url, e));
})();
