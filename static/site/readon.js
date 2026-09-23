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
  var MARGIN = { next: 1200, prev: 600 };

  // Every provision on the page, in reading order, with the address it
  // came from. loaded[0] is the topmost.
  var loaded = [{ el: first, url: location.pathname + location.search }];
  var busy = { next: false, prev: false };
  var done = { next: false, prev: false };
  // The provision in front of the reader, and whether they have moved at
  // all yet. Both are read where something is put in above them.
  var reading = first;
  var scrolled = false;

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

  // Where a provision sits: every structural level above it, outermost
  // first, as render_section wrote them (see html_view._scope_label).
  function scopes(el) {
    try {
      return JSON.parse(el.dataset.scopes || "[]");
    } catch (e) {
      return [];
    }
  }

  // A Part's or a Division's end is marked where it falls rather than
  // stopping the read: an Act is written in Parts and Divisions and it is
  // worth seeing one end, but a reader going on to the next is reading,
  // not navigating. What begins is announced too -- a heading is how the
  // printed Act says a new Part has started, and without one a reader
  // scrolling past the boundary has only the breadcrumb to tell them.
  //
  // Levels are told apart by their own ids, not by their printed labels:
  // every Part of an Act has a Division 1, so labels repeat and comparing
  // them would miss the break between one Part's last Division and the
  // next Part's first.
  function transition(before, after) {
    if (!before || !after) return null;
    var ending = scopes(before);
    var starting = scopes(after);
    var common = 0;
    while (common < ending.length && common < starting.length
           && ending[common].id === starting[common].id) common++;
    if (common === ending.length && common === starting.length) return null;

    var out = document.createDocumentFragment();
    // Innermost first, which is the order they actually end in: a
    // Division closes, and the Part it is the last Division of closes
    // with it.
    for (var i = ending.length - 1; i >= common; i--) {
      var mark = document.createElement("p");
      mark.className = "read-on-end";
      mark.textContent = "End of " + ending[i].label;
      out.appendChild(mark);
    }
    // Outermost first, the order the Act prints them in.
    for (var j = common; j < starting.length; j++) {
      var head = document.createElement("header");
      head.className = "read-on-scope";
      var label = document.createElement("p");
      label.className = "scope-label";
      label.textContent = starting[j].label;
      head.appendChild(label);
      if (starting[j].heading) {
        var heading = document.createElement("h2");
        heading.className = "scope-heading";
        heading.textContent = starting[j].heading;
        head.appendChild(heading);
      }
      out.appendChild(head);
    }
    return out.childNodes.length ? out : null;
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
        var breakAfter = transition(from.el, arriving);
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
        //
        // Measured on the provision being read rather than on the
        // document's height: the height moves when anything at all on the
        // page reflows -- a font arriving, an image, a details opening --
        // and a correction computed from it then moves the reader by
        // whatever that was. Where the thing under their eye was, and
        // where it ended up, is the only measurement that cannot drift.
        // reader.css turns the browser's own scroll anchoring off over
        // this, so this correction is the only one applied.
        var anchor = (reading && main.contains(reading)) ? reading : from.el;
        var was = anchor.getBoundingClientRect().top;
        var breakBefore = transition(arriving, from.el);
        main.insertBefore(arriving, from.el);
        if (breakBefore) main.insertBefore(breakBefore, from.el);
        loaded.unshift({ el: arriving, url: href });
        var moved = anchor.getBoundingClientRect().top - was;
        if (moved) window.scrollTo(0, window.scrollY + moved);
      }
      watch(arriving);
      // Anything that sets a provision up on load has to hear about the
      // ones that arrive afterwards too. history.js did not, so a repealed
      // provision scrolled into showed its plain fallback list rather than
      // its timeline.
      document.dispatchEvent(new CustomEvent("readon:arrived", { detail: arriving }));
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
    // Nothing is fetched upwards until the reader has moved. Landing on a
    // provision from the contents used to put three more above it at the
    // moment of arrival -- compensated, but still the whole page shifting
    // under someone who has just started reading. They are asked for the
    // moment a scroll shows the reader is going somewhere.
    if (where === "prev" && !scrolled) return false;
    var el = edge(where).el;
    var box = el.getBoundingClientRect();
    return where === "next"
      ? box.bottom < window.innerHeight + MARGIN.next
      : box.top > -MARGIN.prev;
  }

  // Which provision is being read, so the address bar names it -- and
  // what a load above the viewport is measured against (see extend).
  // replaceState rather than pushState: scrolling is not navigation, and
  // filling someone's Back button with provisions they scrolled past
  // would trap them on the page.
  function showing(entry) {
    reading = entry.el;
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

  window.addEventListener("scroll", function () {
    scrolled = true;
    check();
  }, { passive: true });
  window.addEventListener("resize", check, { passive: true });
  // A provision opened part-way through an Act has nothing under it, and
  // the reader who came from the contents wanted to land inside the Act
  // rather than at the end of an excerpt. What comes before it waits for
  // them to scroll (see nearEnd).
  check();
})();
