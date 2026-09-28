// The Display menu in the header: the theme and the reading controls,
// behind one button rather than a row of them above every provision
// (issue #96), where they were the first thing on the page and pushed
// the text down. The button is hidden in the markup and shown here, so
// a reader without JavaScript is never offered a control that does
// nothing.
(function () {
  var menu = document.querySelector(".sitemenu");
  if (!menu) return;
  var btn = document.getElementById("sitemenu-btn");
  var panel = document.getElementById("sitemenu-panel");
  menu.hidden = false;

  function setOpen(open) {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  btn.addEventListener("click", function () { setOpen(panel.hidden); });
  // Stays open while its own buttons are used -- sizing the text is
  // several clicks -- and closes on anything outside it.
  document.addEventListener("click", function (event) {
    if (!panel.hidden && !menu.contains(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) {
      setOpen(false);
      btn.focus();
    }
  });
})();
