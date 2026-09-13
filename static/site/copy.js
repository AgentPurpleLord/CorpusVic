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
(function () {
  var btn = document.getElementById("copy-section-btn");
  if (!btn) return;

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
  function provisions() {
    var out = [];
    document.querySelectorAll(".provisions > .prov").forEach(function (el) {
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

  function flash(message) {
    btn.textContent = message;
    setTimeout(function () { btn.textContent = "Copy section"; }, 1800);
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

  btn.addEventListener("click", function () {
    var rows = provisions();
    if (!rows.length) { flash("Nothing to copy"); return; }
    var h1 = document.querySelector(".page h1");
    var heading = h1 ? h1.textContent.replace(/\s+/g, " ").trim() : "";
    var html = asHtml(heading, rows);
    var text = asText(heading, rows);

    if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
      navigator.clipboard
        .write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        })])
        .then(function () { flash("Copied"); })
        .catch(function () { flash(legacyCopy(html, text) ? "Copied" : "Copy failed"); });
      return;
    }
    flash(legacyCopy(html, text) ? "Copied" : "Copy failed");
  });
})();
