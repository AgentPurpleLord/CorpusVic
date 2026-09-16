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
  // "api" where a server can render a card on demand (the dashboard);
  // "static" on the published site, which has no server to ask and so
  // carries a preview.json beside each page anything links to.
  var STATIC = document.body.dataset.preview === "static";
  // Everything above this document's own slug, e.g. "/browse".
  // Previews work for any document under it, not just this one -- a
  // Section's "Explained in" chips point at the Bill and its
  // Explanatory Memorandum, and those are exactly the links most worth
  // previewing.
  var ROOT = BASE.slice(0, BASE.lastIndexOf("/"));
  var OPEN_DELAY = 500;   // long enough that skimming past a link doesn't trigger one
  // A card closes when the pointer has actually left it, not the instant
  // it crosses the edge. Legal text is read slowly and pointers wander,
  // and a card that vanished on a stray pixel had to be re-opened by
  // going back to the link and waiting out OPEN_DELAY again -- which
  // made the feature something to be careful around rather than
  // something to use.
  var CLOSE_DELAY = 600;  // after the pointer is genuinely away from both
  var SLACK = 40;         // px of margin around the card that still counts as "on" it
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
    (STATIC ? staticCard(target) : apiCard(target))
      .then(function (data) {
        if (seq !== requestSeq || activeLink !== a) return;  // pointer moved on before this landed
        if (!data) { hide(); return; }
        cache[href] = data;
        render(data, target.base !== BASE);
        place(a);
      })
      .catch(function () { if (seq === requestSeq) hide(); });
  }

  // The API lives under whatever path this app is served from, so the
  // "/api" belongs after that prefix rather than in front of the whole
  // address. Putting it in front asked /api/admin/browse/<slug>/preview
  // for a page mounted at /admin, which 404s -- and did, silently, for
  // as long as the dashboard has had a base path: a card that never
  // appears is indistinguishable from a link that has no card.
  function apiUrl(base, path) {
    var cut = base.indexOf("/browse/");
    var prefix = cut > 0 ? base.slice(0, cut) : "";
    return prefix + "/api" + base.slice(prefix.length) + path;
  }

  function apiCard(target) {
    var query = "section=" + encodeURIComponent(target.section) +
      "&fragment=" + encodeURIComponent(target.fragment);
    return fetch(apiUrl(target.base, "/preview?" + query))
      .then(function (res) { return res.ok ? res.json() : null; });
  }

  // One file per target page, holding every anchor within it that
  // anything links to -- so following several links into the same section
  // costs one request, and a page nothing links to costs none at all. A
  // missing file is an ordinary answer, not an error: it is what a link
  // into a document this site hasn't published looks like, and the card
  // simply doesn't appear.
  var files = {};

  function staticCard(target) {
    var url = target.base + (target.section ? "/section/" + target.section : "") + "/preview.json";
    if (!files[url]) {
      files[url] = fetch(url)
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(decryptIfGated)
        .catch(function () { return null; });
    }
    return files[url].then(function (previews) {
      return (previews && previews[target.fragment]) || null;
    });
  }

  // A preview is the provision's own words, so on a gated build it is
  // encrypted exactly as the pages are (see corpus/site_crypto.py).
  // The key is the one the unlock page already derived -- found by the
  // salt this page carries, so no passphrase is handled here and a reader
  // who hasn't unlocked simply gets no card.
  var siteKey = null;

  function decryptIfGated(payload) {
    if (!payload || !payload.iv) return payload;
    if (!siteKey) {
      var salt = document.body.dataset.siteSalt;
      var cached = null;
      try { cached = salt && sessionStorage.getItem("siteKey:" + salt); } catch (e) {}
      siteKey = cached
        ? crypto.subtle.importKey("raw", bytes(cached), { name: "AES-GCM", length: 256 }, true, ["decrypt"])
        : Promise.reject();
    }
    return siteKey
      .then(function (key) {
        return crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes(payload.iv) }, key, bytes(payload.ct));
      })
      .then(function (plain) { return JSON.parse(new TextDecoder().decode(plain)); });
  }

  function bytes(b64) {
    var binary = atob(b64), out = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
    return out;
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
    closeTimer = setTimeout(hideUnlessPointerIsNear, CLOSE_DELAY);
  }

  // Where the pointer is now, so the card can stay open while it is near
  // the card even if it has technically left it. The gap between a link
  // and the card below it is a few pixels; without this, crossing it is
  // enough to lose the card.
  var pointer = { x: -1, y: -1 };
  document.addEventListener("mousemove", function (e) {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
  }, { passive: true });

  function pointerIsNearCard() {
    if (!card.classList.contains("open")) return false;
    var r = card.getBoundingClientRect();
    return pointer.x >= r.left - SLACK && pointer.x <= r.right + SLACK
        && pointer.y >= r.top - SLACK && pointer.y <= r.bottom + SLACK;
  }

  // Checked when the delay runs out rather than when the pointer left,
  // so a pointer that wandered out and came back never loses the card.
  function hideUnlessPointerIsNear() {
    if (pointerIsNearCard()) {
      closeTimer = setTimeout(hideUnlessPointerIsNear, CLOSE_DELAY);
      return;
    }
    hide();
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
