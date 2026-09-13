// The reading controls on a section page: how large the legislative text
// is set, and whether the amendment-history notes are shown beside it.
//
// Both are the reader's own preference and both are remembered, because
// they are properties of how someone reads rather than of the page they
// happen to be on -- having to re-enlarge the text on every provision
// would make the control useless to the person who most needs it. They
// are written to <html> as data attributes, which is where reader.css
// picks them up (nothing here sets a style directly), and read from
// localStorage alongside the theme.
(function () {
  var controls = document.querySelector(".readerctl");
  if (!controls) return;

  var SIZE_KEY = "readerProvSize";
  var NOTES_KEY = "readerNotes";
  var STEPS = 5;              // reader.css defines the five sizes
  var DEFAULT_STEP = 2;       // 19px: where the page sits with no preference
  var root = document.documentElement;
  var smaller = document.getElementById("reader-smaller");
  var bigger = document.getElementById("reader-bigger");
  var notesBtn = document.getElementById("reader-notes");

  function stored(key, fallback) {
    try {
      var value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (e) {
      return fallback;
    }
  }

  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) {}
  }

  // The bar is markup rather than something built here, so that a reader
  // without JavaScript is never shown buttons that do nothing; it is
  // hidden until this runs and can make them work.
  controls.hidden = false;

  var step = parseInt(stored(SIZE_KEY, DEFAULT_STEP), 10);
  if (!(step >= 0 && step < STEPS)) step = DEFAULT_STEP;

  function paintSize() {
    root.dataset.provSize = step;
    // Disabled at the ends rather than silently ignoring the click: the
    // range is deliberate (Butterick's 15-25px), and a button that does
    // nothing should say so.
    smaller.disabled = step === 0;
    bigger.disabled = step === STEPS - 1;
  }

  function resize(by) {
    step = Math.min(STEPS - 1, Math.max(0, step + by));
    remember(SIZE_KEY, step);
    paintSize();
  }

  var notes = stored(NOTES_KEY, "on") !== "off";

  function paintNotes() {
    if (notes) delete root.dataset.notes;
    else root.dataset.notes = "off";
    notesBtn.setAttribute("aria-pressed", notes ? "true" : "false");
    notesBtn.textContent = notes ? "Notes on" : "Text only";
    notesBtn.title = notes
      ? "Hide the amendment-history notes"
      : "Show the amendment-history notes";
  }

  paintSize();
  paintNotes();
  smaller.addEventListener("click", function () { resize(-1); });
  bigger.addEventListener("click", function () { resize(1); });
  notesBtn.addEventListener("click", function () {
    notes = !notes;
    remember(NOTES_KEY, notes ? "on" : "off");
    paintNotes();
  });
})();
