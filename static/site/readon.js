// Reading on: an Act read the way it is written, as one continuous
// document, with the address bar naming whichever provision is in front
// of you. Reaching the end of one brings the next; reaching the top
// brings the one before. It runs to the ends of the Act.
//
// Why it does not change what a crawler or a reader without JavaScript
// gets: every provision is still its own URL serving its own complete
// page, rendered by the server exactly as before. This only ever *adds*
// what was already a click away through the "next" and "previous" links
// it reads the addresses from -- so what is indexed at a URL and what is
// read at it stay the same document.
(function () {
  var main = document.querySelector(".reader-main");
  var first = document.querySelector(".reader-section");
  if (!main || !first) return;
  // Respect someone who has asked not to be moved around.
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var nav = main.querySelector(".section-nav");
  if (!nav) return;

  // How many provisions to keep loaded past each end. Reading on used to
  // fetch one at a time, and only once the bottom was within 600px: a
  // provision can be three lines, so a reader scrolling at any pace
  // arrived at the end before the next had been asked for, and watched it
  // appear. A buffer is filled until it is deep enough, which means a
  // scroll has several provisions of runway rather than one.
  var BUFFER = 3;
  // Far enough out that the buffer starts refilling while there is still
  // most of a screen to read.
  var MARGIN = 1200;

  // Every provision on the page, in reading order, with the address it
  // came from. loaded[0] is the topmost.
  var loaded = [{ el: first, url: location.pathname + location.search }];
  var busy = { next: false, prev: false };
  var done = { next: false, prev: false };

  function edge(where) {
    return where === "next" ? loaded[loaded.length - 1] : loaded[0];
  }

  // The address to follow from the provision at one end. Taken from the
  // page each provision arrived on, not from the one first served, or
  // reading on would fetch the same provision for ever.
  function hrefFrom(where) {
    return edge(where).el.dataset[where === "next" ? "nextHref" : "prevHref"] || null;
  }

  function markLinks(section, doc) {
    var next = doc.querySelector(".section-nav .nav-next");
    var prev = doc.querySelector(".section-nav .nav-prev");
    if (next) section.dataset.nextHref = next.getAttribute("href");
    if (prev) section.dataset.prevHref = prev.getAttribute("href");
  }
  markLinks(first, document);

  // A Division's end is marked where it falls rather than stopping the
  // read: an Act is written in Divisions and it is worth seeing one end,
  // but a reader going on to the next is reading, not navigating.
  //
  // Told apart by the scope's own id, not by its printed label: every
  // Part of an Act has a Division 1, so labels repeat and comparing them
  // would miss the break between one Part's last Division and the next
  // Part's first.
  function divisionBreak(before, after) {
    if (!before || !after) return null;
    if (before.dataset.scope === after.dataset.scope) return null;
    var label = before.dataset.scopeLabel;
    if (!label) return null;
    var mark = document.createElement("p");
    mark.className = "read-on-end";
    mark.textContent = "End of " + label;
    return mark;
  }

  async function fetchSection(href) {
    var res = await fetch(href, { credentials: "same-origin" });
    if (!res.ok) return null;
    var doc = new DOMParser().parseFromString(await res.text(), "text/html");
    var section = doc.querySelector(".reader-section");
    return section ? { section: section, doc: doc } : null;
  }

  async function extend(where) {
    if (busy[where] || done[where]) return false;
    var href = hrefFrom(where);
    if (!href) { done[where] = true; return false; }
    busy[where] = true;
    try {
      var got = await fetchSection(href);
      if (!got) { done[where] = true; return false; }
      var from = edge(where);
      markLinks(got.section, got.doc);
      var arriving = document.importNode(got.section, true);

      if (where === "next") {
        var breakAfter = divisionBreak(from.el, arriving);
        if (breakAfter) main.insertBefore(breakAfter, nav);
        main.insertBefore(arriving, nav);
        loaded.push({ el: arriving, url: href });
        // The nav below belongs to whatever is last on the page, not to
        // whatever was served: left alone it offered to take a reader
        // "next" to the provision already above it.
        var freshNav = got.doc.querySelector(".section-nav");
        if (freshNav) nav.innerHTML = freshNav.innerHTML;
      } else {
        // Putting something above the viewport moves everything below it
        // down, so the page would jump out from under the reader.
        // Measured and compensated rather than left to the browser's own
        // scroll anchoring, which not every browser does and none does
        // identically.
        var before = document.documentElement.scrollHeight;
        var breakBefore = divisionBreak(arriving, from.el);
        main.insertBefore(arriving, from.el);
        if (breakBefore) main.insertBefore(breakBefore, from.el);
        loaded.unshift({ el: arriving, url: href });
        window.scrollBy(0, document.documentElement.scrollHeight - before);
      }
      watch(arriving);
      return true;
    } catch (e) {
      // Offline, or the page moved. The links below still work.
      done[where] = true;
      return false;
    } finally {
      busy[where] = false;
    }
  }

  // Fill until there are BUFFER provisions past the one being read, or
  // until that end of the Act is reached. One at a time, because each
  // fetch is what says where the next one is.
  async function fill(where) {
    for (var i = 0; i < BUFFER; i++) {
      if (!nearEnd(where)) return;
      if (!await extend(where)) return;
    }
  }

  function nearEnd(where) {
    var el = edge(where).el;
    var box = el.getBoundingClientRect();
    return where === "next"
      ? box.bottom < window.innerHeight + MARGIN
      : box.top > -MARGIN;
  }

  // Which provision is being read, so the address bar names it.
  // replaceState rather than pushState: scrolling is not navigation, and
  // filling someone's Back button with provisions they scrolled past
  // would trap them on the page.
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

  var pending = false;
  function check() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      fill("next");
      fill("prev");
    });
  }

  window.addEventListener("scroll", check, { passive: true });
  window.addEventListener("resize", check, { passive: true });
  // A provision opened part-way through an Act has nothing above it, and
  // the reader who came from the contents wanted to land inside the Act
  // rather than at the top of an excerpt.
  check();
})();
