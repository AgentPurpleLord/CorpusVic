// Hover previews. Every link on a page points either at a Section page
// (optionally with a provision's anchor) or at an index anchor, so the
// href alone says what to preview -- no data needs to be embedded in the
// page. Deliberately hover-with-a-delay rather than click: the point is
// checking what a defined term means without losing your place, and a
// card that appeared instantly would flash open every time the pointer
// crossed a link mid-sentence. It also opens on keyboard focus, where
// there's no accidental-hover problem to guard against, so a card is
// reachable without a pointer.
(function () {
  var BASE = document.body.dataset.baseUrl;
  if (!BASE) return;
  // Everything above this document's own slug, e.g. "/browse".
  // Previews work for any document under it, not just this one -- a
  // Section's "Explained in" chips point at the Bill and its
  // Explanatory Memorandum, and those are exactly the links most worth
  // previewing.
  var ROOT = BASE.slice(0, BASE.lastIndexOf("/"));
  var OPEN_DELAY = 500;   // long enough that skimming past a link doesn't trigger one
  var CLOSE_DELAY = 220;  // long enough to move the pointer from the link into the card
  var card = document.createElement("div");
  card.className = "linkpeek";
  document.body.appendChild(card);

  var cache = {};
  var openTimer = null, closeTimer = null, activeLink = null, requestSeq = 0;

  // Which link target this is, as the preview endpoint's two
  // parameters. Anything that isn't a link into this Act (the preview
  // bar's own links, an external href) returns null and is left
  // alone.
  function targetOf(a) {
    var url;
    try { url = new URL(a.getAttribute("href"), location.href); } catch (e) { return null; }
    if (url.origin !== location.origin) return null;
    var fragment = decodeURIComponent(url.hash.replace(/^#/, ""));
    if (url.pathname.slice(0, ROOT.length + 1) !== ROOT + "/") return null;
    var parts = url.pathname.slice(ROOT.length + 1).replace(/\/$/, "").split("/");
    if (parts.length === 3 && parts[1] === "section") {
      return { base: ROOT + "/" + parts[0], section: parts[2], fragment: fragment };
    }
    if (parts.length === 1 && parts[0] && fragment) {
      return { base: ROOT + "/" + parts[0], section: "", fragment: fragment };
    }
    return null;
  }

  function render(data, crossDocument) {
    card.classList.add("open");  // must be laid out before the overflow check below can measure it
    var more = data.truncated
      ? '<div class="peek-more">Continues &mdash; open the link to read the rest.</div>' : "";
    // A link into another document (a Bill clause, an EM note) is
    // named by that document as well as by the provision -- without
    // it a card reading "Clause 5" gives no clue which of the three it
    // came from.
    var subBits = [];
    if (crossDocument && data.document) subBits.push(data.document);
    if (data.subtitle) subBits.push(data.subtitle);
    var sub = subBits.length ? '<div class="peek-sub">' + escapeText(subBits.join(" \u00b7 ")) + "</div>" : "";
    card.innerHTML = '<div class="peek-title">' + escapeText(data.title) + "</div>" + sub + data.html + more;
    // A card can also overflow without the server having truncated
    // anything -- short provisions that simply wrap past its height.
    // Say so there too, so a clipped last line always reads as
    // "there's more", never as a rendering glitch.
    if (!more && card.scrollHeight > card.clientHeight) {
      card.insertAdjacentHTML("beforeend", '<div class="peek-more">Continues &mdash; scroll, or open the link.</div>');
    }
  }

  function escapeText(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  // Anchored to the link in page coordinates so the card scrolls with
  // it, flipped above when there isn't room below, and nudged back
  // inside the viewport horizontally.
  function place(a) {
    var r = a.getBoundingClientRect();
    card.style.left = "0px";
    card.style.top = "0px";
    card.classList.add("open");
    var w = card.offsetWidth, h = card.offsetHeight;
    var left = Math.min(Math.max(r.left, 8), Math.max(window.innerWidth - w - 8, 8));
    var below = r.bottom + 8;
    var top = (below + h > window.innerHeight && r.top - h - 8 > 0) ? r.top - h - 8 : below;
    card.style.left = (left + window.scrollX) + "px";
    card.style.top = (top + window.scrollY) + "px";
  }

  function show(a) {
    var target = targetOf(a);
    if (!target) return;
    var href = a.getAttribute("href");
    activeLink = a;
    var seq = ++requestSeq;
    if (cache[href]) { render(cache[href], target.base !== BASE); place(a); return; }
    card.innerHTML = '<div class="peek-loading">Loading&hellip;</div>';
    place(a);
    var query = "section=" + encodeURIComponent(target.section) + "&fragment=" + encodeURIComponent(target.fragment);
    fetch("/api" + target.base + "/preview?" + query)
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (seq !== requestSeq || activeLink !== a) return;  // pointer moved on before this landed
        if (!data) { hide(); return; }
        cache[href] = data;
        render(data, target.base !== BASE);
        place(a);
      })
      .catch(function () { if (seq === requestSeq) hide(); });
  }

  function hide() {
    card.classList.remove("open");
    activeLink = null;
    requestSeq++;
  }

  function scheduleShow(a) {
    clearTimeout(closeTimer);
    clearTimeout(openTimer);
    if (activeLink === a) return;
    openTimer = setTimeout(function () { show(a); }, OPEN_DELAY);
  }

  function scheduleHide() {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    closeTimer = setTimeout(hide, CLOSE_DELAY);
  }

  document.addEventListener("mouseover", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (a && a.closest(".page")) scheduleShow(a);
    else if (!e.target.closest || !e.target.closest(".linkpeek")) scheduleHide();
  });
  document.addEventListener("mouseout", function (e) {
    if (e.target.closest && (e.target.closest("a[href]") || e.target.closest(".linkpeek"))) scheduleHide();
  });
  card.addEventListener("mouseenter", function () { clearTimeout(closeTimer); });
  card.addEventListener("mouseleave", scheduleHide);
  document.addEventListener("focusin", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (a && a.closest(".page")) scheduleShow(a);
  });
  document.addEventListener("focusout", scheduleHide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", function () { if (activeLink) place(activeLink); }, { passive: true });
})();
