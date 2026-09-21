// Reading on: the next provision arrives under this one as you reach the
// end of it, to the end of the Division, and the address bar follows.
//
// Why it does not change what a crawler or a reader without JavaScript
// gets: every provision is still its own URL serving its own complete
// page, rendered by the server exactly as before. This only ever *adds*
// what was already a click away through the "next" link it reads the
// address from -- so what is indexed at a URL and what is read at it stay
// the same document.
//
// Bounded by the Division (see render_section's own note on scope)
// because an Act is written in Divisions and their ends are the author's
// own. Past one, reading on is a decision rather than a scroll, so it is
// a link.
(function () {
  var main = document.querySelector(".reader-main");
  var first = document.querySelector(".reader-section");
  if (!main || !first || !first.dataset.scope) return;
  // Respect someone who has asked not to be moved around.
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var nav = main.querySelector(".section-nav");
  var scope = first.dataset.scope;
  var loading = false;
  var finished = false;
  // Every provision on the page, in order, with the address it came from.
  var loaded = [{ el: first, url: location.pathname + location.search }];

  function nextHref() {
    // The last provision's own "next" is the one to follow -- taken from
    // the page it arrived on, not from the first one, or reading on would
    // fetch the same provision forever.
    var last = loaded[loaded.length - 1].el;
    var link = last.dataset.nextHref;
    return link || null;
  }

  function markNext(section, doc) {
    var link = doc.querySelector(".section-nav .nav-next");
    if (link) section.dataset.nextHref = link.getAttribute("href");
  }
  markNext(first, document);

  function endOfScope(label) {
    var end = document.createElement("p");
    end.className = "read-on-end";
    end.textContent = label ? "End of " + label : "End of this Division";
    main.insertBefore(end, nav);
    finished = true;
  }

  async function readOn() {
    if (loading || finished) return;
    var href = nextHref();
    if (!href) { finished = true; return; }
    loading = true;
    try {
      var res = await fetch(href, { credentials: "same-origin" });
      if (!res.ok) { finished = true; return; }
      var doc = new DOMParser().parseFromString(await res.text(), "text/html");
      var section = doc.querySelector(".reader-section");
      if (!section) { finished = true; return; }
      // The Division's own end. The provision is not appended: it belongs
      // to the next Division, and its "next" link is already below.
      if (section.dataset.scope !== scope) {
        endOfScope(first.dataset.scopeLabel);
        return;
      }
      markNext(section, doc);
      var imported = document.importNode(section, true);
      main.insertBefore(imported, nav);
      loaded.push({ el: imported, url: href });
      watch(imported);
      // The nav below belongs to whatever is last on the page, not to
      // whatever was served: left alone it offered to take a reader
      // "next" to the provision already above it.
      var freshNav = doc.querySelector(".section-nav");
      if (freshNav) nav.innerHTML = freshNav.innerHTML;
    } catch (e) {
      // Offline, or the page moved. The "next" link below still works.
      finished = true;
    } finally {
      loading = false;
    }
  }

  // Which provision is being read, so the address bar names it. replaceState
  // rather than pushState: scrolling is not navigation, and filling someone's
  // Back button with provisions they scrolled past would trap them on the page.
  function showing(entry) {
    if (location.pathname === entry.url.split("#")[0]) return;
    history.replaceState(null, "", entry.url);
    if (entry.el.dataset.title) document.title = entry.el.dataset.title;
  }

  var seen = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      var entry = loaded.find(function (l) { return l.el === e.target; });
      if (entry) showing(entry);
    });
  }, { rootMargin: "-25% 0px -70% 0px" });

  function watch(el) { seen.observe(el); }
  watch(first);

  // Fetch the next one while there is still a screenful to read, so it is
  // already there by the time the reader arrives at the bottom.
  var ahead = new IntersectionObserver(function (entries) {
    if (entries.some(function (e) { return e.isIntersecting; })) readOn();
  }, { rootMargin: "0px 0px 600px 0px" });
  ahead.observe(nav);
})();
