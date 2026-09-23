// Historical mode: a provision's wordings as a timeline, oldest on the
// left and newest on the right, two to a screen, moved through sideways.
// See html_view.render_history for the markup this takes over.
//
// Everything a reader can ask for is already in the page, rendered by the
// server or the static build, so this only decides what is visible. That
// is what lets the same page work on the static archive, and without
// script at all, where the wordings are a plain list in <details>.
//
// Two ways in: a provision's own History chip, and the History control in
// the reading bar, which turns the mode on for every provision that has a
// history -- including ones reading on (readon.js) brings in later, which
// is why arrivals are announced and handled here, and why the click
// listeners sit on the document rather than on each chip.
(function () {
  var root = document.documentElement;
  var KEY = "readerHistory";
  var modeBtn = document.getElementById("reader-history");
  var mode = stored() === "on";

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function remember(value) {
    try { localStorage.setItem(KEY, value); } catch (e) {}
  }

  function narrow() {
    return window.innerWidth <= 720;
  }
  function panelsOf(history) { return history.querySelectorAll(".hist-panel"); }
  function trackOf(history) { return history.querySelector(".hist-track"); }
  function lastFocus(history) {
    var count = panelsOf(history).length;
    return narrow() ? count - 1 : count - 2;
  }

  // A ghost -- a provision since repealed -- has no text of its own, only
  // its history, so nothing may switch it out of historical mode: there
  // would be nothing left on its page.
  function isGhost(article) {
    return !article.querySelector(":scope > .provisions");
  }

  function setup(history) {
    if (history.dataset.ready) return;
    history.dataset.ready = "1";
    history.classList.add("hist-js");
    history.open = true;
    var nav = document.createElement("div");
    nav.className = "hist-nav";
    nav.innerHTML =
      '<button type="button" class="hist-step" data-step="-1" aria-label="Earlier wording">&#9664; Earlier</button>' +
      '<div class="hist-dots" role="group" aria-label="Wordings"></div>' +
      '<button type="button" class="hist-step" data-step="1" aria-label="Later wording">Later &#9654;</button>';
    var dots = nav.querySelector(".hist-dots");
    panelsOf(history).forEach(function (panel, n) {
      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "hist-dot";
      dot.dataset.go = String(n);
      dot.setAttribute("aria-label", panel.getAttribute("aria-label") || "Wording " + (n + 1));
      dots.appendChild(dot);
    });
    history.insertBefore(nav, trackOf(history));
    trackOf(history).addEventListener("scroll", function () { follow(history); }, { passive: true });
    // Starts on the wording before the one this page carries, with that
    // one beside it -- "what did this used to say?" is the question the
    // mode was turned on to answer.
    var at = parseInt(history.dataset.at, 10);
    history.dataset.focus = String((isNaN(at) ? panelsOf(history).length - 1 : at) - 1);
  }

  // The dots and the step buttons for a focus, without moving anything.
  // `focus` is the left of the two wordings on screen, or the one a phone
  // has room for.
  function paint(history, focus) {
    var last = lastFocus(history);
    focus = Math.max(0, Math.min(focus, last));
    history.dataset.focus = String(focus);
    var shown = function (n) { return n === focus || (!narrow() && n === focus + 1); };
    panelsOf(history).forEach(function (panel, n) {
      panel.classList.toggle("hist-focus", n === focus);
    });
    history.querySelectorAll(".hist-dot").forEach(function (dot, n) {
      dot.classList.toggle("on", shown(n));
    });
    var steps = history.querySelectorAll(".hist-step");
    steps[0].disabled = focus <= 0;
    steps[1].disabled = focus >= last;
    return focus;
  }

  function show(history, focus, smooth) {
    focus = paint(history, focus);
    var track = trackOf(history);
    var panel = panelsOf(history)[focus];
    if (!panel) return;
    // Measured on screen rather than with offsetLeft, which counts from
    // whichever ancestor happens to be positioned, not from the track.
    var left = panel.getBoundingClientRect().left - track.getBoundingClientRect().left + track.scrollLeft;
    track.scrollTo({ left: left, behavior: smooth ? "smooth" : "auto" });
  }

  // A swipe moves the strip itself, so the dots follow wherever it came
  // to rest rather than the other way round.
  function follow(history) {
    if (history.dataset.following) return;
    history.dataset.following = "1";
    requestAnimationFrame(function () {
      delete history.dataset.following;
      var base = trackOf(history).getBoundingClientRect().left;
      var nearest = 0, gap = Infinity;
      panelsOf(history).forEach(function (panel, n) {
        var d = Math.abs(panel.getBoundingClientRect().left - base);
        if (d < gap) { gap = d; nearest = n; }
      });
      paint(history, nearest);
    });
  }

  function step(history, by) {
    show(history, parseInt(history.dataset.focus, 10) + by, true);
  }

  function setOpen(article, on) {
    var history = article.querySelector(".history");
    if (!history) return;
    if (!on && isGhost(article)) return;
    article.classList.toggle("historical", on);
    var chip = article.querySelector(".history-chip");
    if (chip) chip.setAttribute("aria-expanded", on ? "true" : "false");
    if (!on) return;
    // After the class, not before: until the article is historical the
    // strip is display:none, has no width, and cannot be scrolled to the
    // right wording.
    setup(history);
    show(history, parseInt(history.dataset.focus, 10), false);
  }

  // What a provision should look like when it first appears, on load or
  // by reading on.
  function prepare(article) {
    if (article.querySelector(".history") && (mode || isGhost(article))) setOpen(article, true);
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

  function paintMode() {
    if (mode) root.dataset.history = "on";
    else delete root.dataset.history;
    if (!modeBtn) return;
    modeBtn.setAttribute("aria-pressed", mode ? "true" : "false");
    modeBtn.textContent = mode ? "History on" : "History off";
    modeBtn.title = mode
      ? "Show each provision's current text"
      : "Show how each provision has read across the versions held here";
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest(".history-chip, .hist-step, .hist-dot, .hist-cmp");
    if (!target) return;
    var history = target.closest(".history");
    if (target.classList.contains("history-chip")) {
      var article = target.closest(".reader-section");
      if (article) setOpen(article, !article.classList.contains("historical"));
    } else if (target.classList.contains("hist-cmp")) compare(target);
    else if (target.classList.contains("hist-step")) step(history, parseInt(target.dataset.step, 10));
    else show(history, parseInt(target.dataset.go, 10), true);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    var history = event.target.closest && event.target.closest(".history.hist-js");
    if (!history) return;
    event.preventDefault();
    step(history, event.key === "ArrowLeft" ? -1 : 1);
  });

  // Two wordings across becomes one on a phone, which changes where the
  // last stop is.
  window.addEventListener("resize", function () {
    document.querySelectorAll(".history.hist-js").forEach(function (history) {
      show(history, parseInt(history.dataset.focus, 10), false);
    });
  }, { passive: true });

  if (modeBtn) {
    modeBtn.addEventListener("click", function () {
      mode = !mode;
      remember(mode ? "on" : "off");
      paintMode();
      document.querySelectorAll(".reader-section").forEach(function (article) {
        if (article.querySelector(".history")) setOpen(article, mode || isGhost(article));
      });
    });
  }

  document.addEventListener("readon:arrived", function (event) { prepare(event.detail); });
  paintMode();
  document.querySelectorAll(".reader-section").forEach(prepare);
})();
