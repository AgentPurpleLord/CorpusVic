// The dark/light toggle's button wiring. The half that has to run
// before the first paint -- so a dark-mode reader never gets a white
// flash -- is inline in page.html's <head> instead. Both halves read and
// write the same localStorage key static/review.html does, so toggling
// the theme in the review GUI and clicking through to a page keeps it.
(function () {
  var btn = document.getElementById("theme-toggle-btn");
  function paint() {
    var dark = document.documentElement.dataset.theme === "dark";
    btn.innerHTML = dark ? "&#9728;&#65039;" : "&#127769;";
    btn.title = dark ? "Switch to light mode" : "Switch to dark mode";
  }
  paint();
  btn.onclick = function () {
    var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    if (next === "dark") document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("reviewTheme", next); } catch (e) {}
    paint();
  };
})();
