// Historical mode: a provision's wordings as a timeline, oldest on the
// left and newest on the right, two at a time. See
// html_view.render_history for the markup this takes over.
//
// Everything a reader can ask for is already in the page, rendered by the
// server or the static build, so this only decides what is visible. That
// is what lets the same page work on the static archive, and without
// script at all, where the wordings are a plain list in <details>.
//
// Listeners are on the document rather than on each chip, because reading
// on (readon.js) appends further provisions after the page has loaded and
// each may carry a history of its own.
(function () {
  function setup(history) {
    if (history.dataset.ready) return;
    history.dataset.ready = "1";
    history.classList.add("hist-js");
    history.open = true;
    var panels = history.querySelectorAll(".hist-panel");
    var nav = document.createElement("div");
    nav.className = "hist-nav";
    nav.innerHTML =
      '<button type="button" class="hist-step" data-step="-1" aria-label="Earlier wording">&#9664; Earlier</button>' +
      '<div class="hist-dots" role="group" aria-label="Wordings"></div>' +
      '<button type="button" class="hist-step" data-step="1" aria-label="Later wording">Later &#9654;</button>';
    var dots = nav.querySelector(".hist-dots");
    panels.forEach(function (panel, n) {
      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "hist-dot";
      dot.dataset.go = String(n);
      dot.setAttribute("aria-label", panel.getAttribute("aria-label") || "Wording " + (n + 1));
      dots.appendChild(dot);
    });
    history.insertBefore(nav, history.querySelector(".hist-track"));
    // The left pane starts on the wording before the one this page
    // carries, with that one on the right -- "what did this used to
    // say?" is the question the chip was pressed to answer.
    var at = parseInt(history.dataset.at, 10);
    show(history, (isNaN(at) ? panels.length - 1 : at) - 1);
  }

  function narrow() {
    return window.innerWidth <= 720;
  }

  // `focus` is the left of the two panes shown side by side, or the one
  // pane a phone has room for.
  function show(history, focus) {
    var panels = history.querySelectorAll(".hist-panel");
    var last = narrow() ? panels.length - 1 : panels.length - 2;
    focus = Math.max(0, Math.min(focus, last));
    history.dataset.focus = String(focus);
    var shown = function (n) { return n === focus || (!narrow() && n === focus + 1); };
    panels.forEach(function (panel, n) {
      panel.classList.toggle("hist-shown", shown(n));
      panel.classList.toggle("hist-focus", n === focus);
    });
    history.querySelectorAll(".hist-dot").forEach(function (dot, n) {
      dot.classList.toggle("on", shown(n));
    });
    var steps = history.querySelectorAll(".hist-step");
    steps[0].disabled = focus <= 0;
    steps[1].disabled = focus >= last;
  }

  function step(history, by) {
    show(history, parseInt(history.dataset.focus, 10) + by);
  }

  function toggle(chip) {
    var history = document.getElementById(chip.getAttribute("aria-controls"));
    var article = chip.closest(".reader-section");
    if (!history || !article) return;
    setup(history);
    var on = !article.classList.contains("historical");
    article.classList.toggle("historical", on);
    chip.setAttribute("aria-expanded", on ? "true" : "false");
  }

  function compare(button) {
    var panel = button.closest(".hist-panel");
    var pressed = button.getAttribute("aria-pressed") === "true";
    var view = pressed ? "text" : button.dataset.view;
    panel.querySelectorAll(".hist-cmp").forEach(function (b) {
      b.setAttribute("aria-pressed", !pressed && b === button ? "true" : "false");
    });
    panel.querySelectorAll(".hist-body").forEach(function (body) {
      body.hidden = body.dataset.view !== view;
    });
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest(".history-chip, .hist-step, .hist-dot, .hist-cmp");
    if (!target) return;
    var history = target.closest(".history");
    if (target.classList.contains("history-chip")) toggle(target);
    else if (target.classList.contains("hist-cmp")) compare(target);
    else if (target.classList.contains("hist-step")) step(history, parseInt(target.dataset.step, 10));
    else show(history, parseInt(target.dataset.go, 10));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    var history = event.target.closest && event.target.closest(".history.hist-js");
    if (!history) return;
    event.preventDefault();
    step(history, event.key === "ArrowLeft" ? -1 : 1);
  });

  // A ghost page (a provision since repealed) has nothing but its
  // history to show, so it opens in historical mode.
  document.querySelectorAll(".reader-section.historical .history").forEach(setup);
})();
