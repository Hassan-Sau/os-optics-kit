/* Loaded first by every /engine-view/<snapshot_id>/ page. It records whether the page's own scripts and styles
 * loaded and whether starting the engine viewer raised an error, so the host page can tell a failed start (which it
 * abandons and retries) from a scene that is still loading (which it keeps waiting for). It changes nothing else.
 */
(function () {
  "use strict";
  var boot = { failed: null };
  window.__osOpticsKitBoot = boot;
  window.addEventListener("error", function (event) {
    if (boot.failed !== null || window.__opensciViewer) {
      return;
    }
    var target = event.target;
    if (target && target !== window && (target.tagName === "SCRIPT" || target.tagName === "LINK")) {
      // A script or stylesheet that did not load (network error, HTTP error status or a refused content type).
      boot.failed = { kind: "asset", url: String(target.src || target.href || "") };
    } else {
      // An uncaught error while the engine viewer starts: it will never show the scene or report a failure itself.
      boot.failed = { kind: "script", message: String(event.message || "") };
    }
  }, true);
})();
