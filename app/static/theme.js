// Runs before the page draws so a saved theme doesn't flash the default colors first.
// The real preference lives on the server (it follows you between devices); this is a cache.
(function () {
  try {
    var t = JSON.parse(localStorage.getItem("budget-theme") || "{}");
    var d = document.documentElement;
    if (t.theme) d.setAttribute("data-theme", t.theme);
    if (t.accent) d.setAttribute("data-accent", t.accent);
  } catch (e) { /* private mode etc: defaults are fine */ }
})();
