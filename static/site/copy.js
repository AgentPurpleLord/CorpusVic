// The "Copy section" button. Copying a provision into advice, a
// submission or an email is one of the things people most often come here
// to do, so it's a button rather than a careful drag-select that picks up
// the margin notes and loses the indentation.
//
// Two clipboard flavours are written: Word and Google Docs both prefer
// text/html and both honour margin-left in points as a real paragraph
// indent, so nesting arrives as 0 / 36 / 72pt -- half an inch a level,
// the default tab stop in either -- and stays editable as paragraphs
// rather than as a run of spaces that reflows. text/plain carries tabs
// for the same shape wherever rich text isn't wanted.
//
// One listener on the document rather than one on the button, and each
// click reads only its own section: reading on (readon.js) brings further
// sections into the page, each with its own button, and a listener bound
// at load reached only the first -- the rest did nothing, and the first
// copied every section loaded so far.
(function () {
  // Half an inch, which is what Word and Google Docs both treat as one
  // tab stop -- so "(a) is one tab in" comes out as a real paragraph
  // indent in either, not as a run of spaces that reflows on edit.
  var INDENT_PT = 36;
  var LABEL = ".prov-num, .prov-term";

  // One entry per provision, in reading order: how deep it sits, its
  // own number (or defined term), and its text with the source PDF's
  // line wraps collapsed back into running prose. Margin notes are left
  // out -- they're the amendment history printed beside the provision,
  // not part of its words.
  function provisions(section) {
    var out = [];
    section.querySelectorAll(".provisions > .prov").forEach(function (el) {
      var clone = el.cloneNode(true);
      var labelEl = clone.querySelector(LABEL);
      var label = "";
      var isTerm = false;
      if (labelEl) {
        label = labelEl.textContent.trim();
        isTerm = labelEl.classList.contains("prov-term");
        labelEl.remove();
      }
      var text = clone.textContent.replace(/\s+/g, " ").trim();
      if (!label && !text) return;
      var depth = parseInt(el.style.getPropertyValue("--depth"), 10) || 0;
      out.push({ depth: depth, label: label, text: text, term: isTerm });
    });
    return out;
  }

  // A defined term runs straight on into its own text, so it takes a
  // space -- except where that text opens with punctuation ("appear, in
  // relation to a party, ..."), which sits tight against it. Same rule
  // render_section applies when it builds the page, kept in step so the
  // copy reads exactly as the screen does.
  var TIGHT = [",", ".", ";", ":", ")", "—", "-"];

  function gap(text) {
    return !text || TIGHT.indexOf(text.charAt(0)) !== -1 ? "" : " ";
  }

  function joined(p) {
    if (!p.label) return p.text;
    if (!p.text) return p.label;
    return p.label + gap(p.text) + p.text;
  }

  function asText(heading, rows) {
    var lines = heading ? [heading, ""] : [];
    rows.forEach(function (p) {
      lines.push(new Array(p.depth + 1).join("\t") + joined(p));
    });
    return lines.join("\n");
  }

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Word and Docs both read text/html in preference to text/plain, and
  // both turn margin-left into a real indent -- which is the whole
  // reason this writes two flavours instead of one.
  function asHtml(heading, rows) {
    var parts = ['<meta charset="utf-8">'];
    if (heading) {
      parts.push('<p style="margin:0 0 8pt 0;font-weight:bold">' + esc(heading) + "</p>");
    }
    rows.forEach(function (p) {
      var indent = p.depth * INDENT_PT;
      var body = p.term && p.label
        ? "<i>" + esc(p.label) + "</i>" + gap(p.text) + esc(p.text)
        : esc(joined(p));
      parts.push(
        '<p style="margin:0 0 6pt 0;margin-left:' + indent + 'pt">' + body + "</p>"
      );
    });
    return parts.join("");
  }

  // The class as well as the words, so the button visibly changes state
  // rather than only its label. A second click restarts the clock instead
  // of being put back early by the first one's.
  function flash(btn, message, ok) {
    btn.textContent = message;
    btn.classList.toggle("copied", ok);
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {
      btn.textContent = "Copy section";
      btn.classList.remove("copied");
    }, 1800);
  }

  function done(btn, ok) {
    flash(btn, ok ? "Copied" : "Copy failed", ok);
  }

  // The modern path needs a secure context; the fallback is what runs on
  // plain http (a local preview, say) and in older browsers, and can
  // only carry the rich flavour -- so the selection is made over real
  // nodes and copied, which keeps the indents.
  function legacyCopy(html, text) {
    var holder = document.createElement("div");
    holder.setAttribute("style", "position:fixed;left:-9999px;top:0;white-space:pre-wrap");
    holder.innerHTML = html;
    document.body.appendChild(holder);
    var range = document.createRange();
    range.selectNodeContents(holder);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    sel.removeAllRanges();
    holder.remove();
    if (!ok) { window.prompt("Copy the text below", text); }
    return ok;
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".copy-section");
    if (!btn) return;
    var section = btn.closest(".reader-section") || document;
    var rows = provisions(section);
    if (!rows.length) { flash(btn, "Nothing to copy", false); return; }
    var h1 = section.querySelector("h1");
    var heading = h1 ? h1.textContent.replace(/\s+/g, " ").trim() : "";
    var html = asHtml(heading, rows);
    var text = asText(heading, rows);

    if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
      navigator.clipboard
        .write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        })])
        .then(function () { done(btn, true); })
        .catch(function () { done(btn, legacyCopy(html, text)); });
      return;
    }
    done(btn, legacyCopy(html, text));
  });
})();
