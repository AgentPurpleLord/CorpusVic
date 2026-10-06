// A chart, full size, over the page (see html_view._figure_html, which
// sets it small). Delegated from the document so charts added by reading
// on (readon.js) open too.
(function () {
  let dialog = null, opener = null;

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "figure-dialog";
    dialog.innerHTML = '<button type="button" class="figure-dialog-close" aria-label="Close">&times;</button><div></div>';
    dialog.querySelector(".figure-dialog-close").addEventListener("click", () => dialog.close());
    // A click on the backdrop lands on the dialog itself, not its content.
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { if (opener) opener.focus(); });
    document.body.appendChild(dialog);
  }

  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest(".figure-zoom");
    if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    if (typeof HTMLDialogElement === "undefined") return;  // the link opens the image
    e.preventDefault();
    if (!dialog) build();
    opener = link;
    const picture = link.querySelector("picture").cloneNode(true);
    const img = picture.querySelector("img");
    img.removeAttribute("loading");
    // Its own size, not the card's.
    img.removeAttribute("style");
    const holder = dialog.querySelector("div");
    holder.replaceChildren(picture);
    dialog.setAttribute("aria-label", img.alt || "Chart");
    dialog.showModal();
    dialog.querySelector(".figure-dialog-close").focus();
  });
})();
