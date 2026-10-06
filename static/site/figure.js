// A chart, enlarged over the page (see html_view._figure_html, which sets
// it small). It opens fitted to the screen -- opened at its own pixel
// size, the tallest Bail Act chart ran off it -- and zooms from there.
// Delegated from the document so charts added by reading on (readon.js)
// open too.
(function () {
  const STEP = 1.25, MAX = 4, FIT_CAP = 1.5;
  let dialog, stage, frame, img, level, opener;
  let scale = 1, fitScale = 1, atFit = true, natW = 1, natH = 1;
  const pointers = new Map();
  let pinch = null, drag = null, lastTap = 0;

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "figure-dialog";
    dialog.innerHTML =
      '<div class="figure-toolbar">' +
      '<button type="button" data-zoom="out" aria-label="Zoom out">&minus;</button>' +
      '<span class="figure-level" aria-live="polite"></span>' +
      '<button type="button" data-zoom="in" aria-label="Zoom in">+</button>' +
      '<button type="button" data-zoom="fit">Fit</button>' +
      '<button type="button" class="figure-dialog-close" aria-label="Close">&times;</button>' +
      '</div><div class="figure-stage"><div class="figure-frame"></div></div>';
    stage = dialog.querySelector(".figure-stage");
    frame = dialog.querySelector(".figure-frame");
    level = dialog.querySelector(".figure-level");
    dialog.querySelector(".figure-dialog-close").addEventListener("click", () => dialog.close());
    dialog.querySelector(".figure-toolbar").addEventListener("click", (e) => {
      const which = e.target.dataset && e.target.dataset.zoom;
      if (which === "in") zoomAt(scale * STEP);
      else if (which === "out") zoomAt(scale / STEP);
      else if (which === "fit") fit();
    });
    // A click on the backdrop lands on the dialog itself.
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { if (opener) opener.focus(); });
    dialog.addEventListener("keydown", (e) => {
      if (e.key === "+" || e.key === "=") { zoomAt(scale * STEP); e.preventDefault(); }
      else if (e.key === "-") { zoomAt(scale / STEP); e.preventDefault(); }
      else if (e.key === "0") { fit(); e.preventDefault(); }
    });
    // Ctrl/Cmd + wheel is also what a trackpad pinch sends.
    stage.addEventListener("wheel", (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      zoomAt(scale * Math.exp(-e.deltaY * 0.0025), e.clientX, e.clientY);
    }, { passive: false });
    stage.addEventListener("dblclick", (e) => toggle(e.clientX, e.clientY));
    stage.addEventListener("pointerdown", down);
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", up);
    stage.addEventListener("pointercancel", up);
    window.addEventListener("resize", () => { if (dialog.open && atFit) fit(); });
    document.body.appendChild(dialog);
  }

  function apply() {
    img.style.width = `${Math.round(natW * scale)}px`;
    level.textContent = `${Math.round(scale * 100)}%`;
    const zoomed = img.offsetWidth > stage.clientWidth || img.offsetHeight > stage.clientHeight;
    stage.classList.toggle("zoomed", zoomed);
  }

  function fit() {
    const w = stage.clientWidth - 32, h = stage.clientHeight - 32;
    fitScale = Math.min(w / natW, h / natH, FIT_CAP);
    scale = fitScale;
    atFit = true;
    apply();
  }

  // To `next`, keeping the point under (x, y) -- the middle of the view
  // when not given -- where it is.
  function zoomAt(next, x, y) {
    next = Math.min(Math.max(next, Math.min(fitScale, 1)), MAX);
    const box = stage.getBoundingClientRect();
    const px = (x === undefined ? box.left + box.width / 2 : x) - box.left;
    const py = (y === undefined ? box.top + box.height / 2 : y) - box.top;
    const before = img.getBoundingClientRect();
    const fx = (box.left + px - before.left) / before.width;
    const fy = (box.top + py - before.top) / before.height;
    scale = next;
    atFit = Math.abs(scale - fitScale) < 0.001;
    apply();
    const after = img.getBoundingClientRect();
    stage.scrollLeft += after.left + fx * after.width - (box.left + px);
    stage.scrollTop += after.top + fy * after.height - (box.top + py);
  }

  function toggle(x, y) {
    if (atFit) zoomAt(Math.max(1, fitScale * 2), x, y);
    else fit();
  }

  function down(e) {
    stage.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale };
      drag = null;
    } else if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, left: stage.scrollLeft, top: stage.scrollTop };
      if (e.pointerType === "touch") {
        // A double tap, which with touch-action off is ours to notice.
        const now = Date.now();
        if (now - lastTap < 300) { toggle(e.clientX, e.clientY); lastTap = 0; }
        else lastTap = now;
      }
    }
  }

  function move(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      zoomAt(pinch.scale * Math.hypot(a.x - b.x, a.y - b.y) / pinch.dist, (a.x + b.x) / 2, (a.y + b.y) / 2);
    } else if (drag) {
      stage.scrollLeft = drag.left - (e.clientX - drag.x);
      stage.scrollTop = drag.top - (e.clientY - drag.y);
    }
  }

  function up(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!pointers.size) drag = null;
  }

  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest(".figure-zoom");
    if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    if (typeof HTMLDialogElement === "undefined") return;  // the link opens the image
    e.preventDefault();
    if (!dialog) build();
    opener = link;
    const picture = link.querySelector("picture").cloneNode(true);
    img = picture.querySelector("img");
    img.removeAttribute("loading");
    img.draggable = false;
    natW = +img.getAttribute("width") || img.naturalWidth || 1;
    natH = +img.getAttribute("height") || img.naturalHeight || 1;
    frame.replaceChildren(picture);
    dialog.setAttribute("aria-label", img.alt || "Chart");
    dialog.showModal();
    stage.scrollTop = stage.scrollLeft = 0;
    fit();
    dialog.querySelector(".figure-dialog-close").focus();
  });
})();
