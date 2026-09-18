// hermex-push has no dashboard UI. The SPA loads every plugin's entry script and
// flags one that registers nothing, so register an empty component.
(function () {
  var sdk = window.__HERMES_PLUGINS__;
  if (sdk && typeof sdk.register === "function") {
    sdk.register("hermex-push", function HermexPush() { return null; });
  }
})();
