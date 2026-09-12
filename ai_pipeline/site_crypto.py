"""
Password-gates the published static site (see export_static_site.py) by
encrypting each page at build time, so a testing passphrase can be handed
out before the site is meant to be public.

GitHub Pages serves plain files and has no server-side login, so a gate
that merely *hides* content with JavaScript would be decorative: the real
text would still sit in the HTML for anyone who viewed source, fetched
the page with curl, or read it out of a cache. Instead each page is
encrypted here, at build time, and what gets published is the ciphertext
plus a small unlock page. The passphrase never appears in the repository
or in the output -- it comes from a GitHub Actions secret (see
.github/workflows/pages.yml) -- and the content genuinely cannot be read
without it.

What this is and isn't:
  - AES-256-GCM, with the key derived by PBKDF2-HMAC-SHA256 at the same
    200,000 iterations dashboard.py already uses for its own login. Both
    are standard primitives the browser implements natively (WebCrypto),
    so nothing cryptographic is hand-rolled on either side.
  - GCM's authentication tag doubles as the password check: a wrong
    passphrase fails to decrypt rather than producing garbage, so the
    unlock page needs no separate "is this right?" value that would give
    an attacker something cheaper to test against.
  - One salt per build, so unlocking once derives a key that works for
    every page; the derived key (not the passphrase) is cached in
    sessionStorage, which is per-tab and cleared when the tab closes.
  - It is *shared-secret* protection, not per-user access control.
    Everyone gets the same passphrase, anyone holding it can decrypt and
    redistribute the plaintext, and the ciphertext is public, so an
    offline guessing attack is possible -- use a long passphrase. This is
    sized for "not ready to be read yet", not for protecting secrets.
"""
import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Matches dashboard.py's own _PBKDF2_ITERATIONS: one project, one answer
# to "how hard should deriving a key from a password be".
PBKDF2_ITERATIONS = 200_000

_SALT_BYTES = 16
_IV_BYTES = 12  # 96 bits, the size AES-GCM is specified around


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


class SiteGate:
    """One build's worth of gating: a salt, the key derived from the
    passphrase once, and the page wrapper. Reused across every page so a
    reader who unlocks one has unlocked all of them."""

    def __init__(self, password: str, iterations: int = PBKDF2_ITERATIONS):
        if not password:
            raise ValueError("A gate needs a passphrase -- build without one for an open site.")
        self.salt = os.urandom(_SALT_BYTES)
        self.iterations = iterations
        self.key = derive_key(password, self.salt, iterations)

    def encrypt(self, plaintext: str) -> dict:
        """{"iv", "ct"}, both base64. A fresh IV per page, as AES-GCM
        requires -- reusing one across pages under the same key would
        leak the relationship between them."""
        iv = os.urandom(_IV_BYTES)
        ciphertext = AESGCM(self.key).encrypt(iv, plaintext.encode("utf-8"), None)
        return {"iv": _b64(iv), "ct": _b64(ciphertext)}

    def wrap(self, page_html: str) -> str:
        """The whole page, encrypted, inside the unlock shell -- the shell
        deliberately carries nothing derived from the page it holds (not
        even its title), so the published file set gives away no more
        than how many pages exist."""
        payload = {"salt": _b64(self.salt), "iterations": self.iterations, **self.encrypt(page_html)}
        # Base64 contains no "<", so it can never close the script element
        # early; json.dumps handles the rest of the escaping.
        return _GATE_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload))


ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


# The unlock page. Self-contained on purpose: no external stylesheet or
# font, so it renders identically whether or not anything else loads, and
# no request that would tell a third party the site exists. Honours the
# same localStorage["reviewTheme"] the rest of the GUI uses, so a
# dark-mode reader doesn't get a white flash on the way in.
_GATE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Password required</title>
<script>
  try { if (localStorage.getItem("reviewTheme") === "dark") document.documentElement.dataset.theme = "dark"; } catch (e) {}
</script>
<style>
  :root {
    color-scheme: light;
    --bg: #f3f2f2; --panel: #eae9e9; --fg: #201e1d; --accent: #ec3013;
    --border: color-mix(in srgb, #201e1d 40%, transparent);
    --muted: #605d5d;
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #120e0e; --panel: #1c1717; --fg: #eeeaea; --accent: #ff7f67;
    --border: #413b3b; --muted: #969191;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px; background: var(--bg); color: var(--fg);
    font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; line-height: 1.55;
  }
  form { width: 320px; max-width: 100%; }
  h1 { font-size: 19px; margin: 0 0 6px; }
  p { font-size: 13px; color: var(--muted); margin: 0 0 16px; }
  input {
    width: 100%; padding: 8px 10px; font: inherit; font-size: 14px;
    background: var(--panel); color: var(--fg);
    border: 1px solid var(--border); border-radius: 0;
  }
  input:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; border-color: var(--accent); }
  button {
    width: 100%; margin-top: 10px; padding: 8px 12px; font: inherit; font-size: 14px;
    cursor: pointer; border: 0; border-radius: 0;
    background: var(--accent); color: var(--bg);
  }
  button:disabled { opacity: 0.5; cursor: progress; }
  #err { color: var(--accent); font-size: 12.5px; min-height: 17px; margin-top: 8px; }
</style>
</head>
<body>
<form id="f">
  <h1>This site isn't public yet</h1>
  <p>It's being tested. Enter the passphrase you were given to read it.</p>
  <input type="password" id="pw" autocomplete="current-password" aria-label="Passphrase" autofocus>
  <button type="submit">View the site</button>
  <div id="err" role="alert"></div>
</form>
<script type="application/json" id="payload">__PAYLOAD__</script>
<script>
(function () {
  var P = JSON.parse(document.getElementById("payload").textContent);
  var form = document.getElementById("f");
  var input = document.getElementById("pw");
  var err = document.getElementById("err");
  var button = form.querySelector("button");
  // Keyed by salt, so a rebuild (which draws a new salt) doesn't leave a
  // stale key behind that would fail to decrypt the new pages.
  var CACHE_KEY = "siteKey:" + P.salt;

  function bytes(b64) {
    var bin = atob(b64), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function b64(buffer) {
    var b = new Uint8Array(buffer), bin = "";
    for (var i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
    return btoa(bin);
  }

  function deriveKey(password) {
    return crypto.subtle
      .importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"])
      .then(function (base) {
        return crypto.subtle.deriveKey(
          { name: "PBKDF2", salt: bytes(P.salt), iterations: P.iterations, hash: "SHA-256" },
          base, { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
      });
  }

  // Rejects on a wrong key: AES-GCM checks its own authentication tag,
  // so there's no separate password check to get wrong.
  function decryptPage(key) {
    return crypto.subtle
      .decrypt({ name: "AES-GCM", iv: bytes(P.iv) }, key, bytes(P.ct))
      .then(function (plain) { return new TextDecoder().decode(plain); });
  }

  // Replaces this document wholesale rather than injecting into it, so
  // the real page's own <head>, styles and scripts run exactly as they
  // would have if it had been served directly.
  //
  // Deferred until the load event, because document.open() does nothing
  // at all while the parser is still running -- an unlock from the
  // cached key below can otherwise resolve mid-parse and silently leave
  // the gate on screen, which is exactly what it used to do.
  function show(html) {
    whenLoaded(function () {
      document.open();
      document.write(html);
      document.close();
    });
  }

  function whenLoaded(fn) {
    if (document.readyState === "complete") fn();
    else window.addEventListener("load", fn, { once: true });
  }

  function remember(rawKey) {
    try { sessionStorage.setItem(CACHE_KEY, b64(rawKey)); } catch (e) {}
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    err.textContent = "";
    button.disabled = true;
    button.textContent = "Unlocking\\u2026";
    var derived;
    deriveKey(input.value)
      .then(function (key) {
        derived = key;
        return crypto.subtle.exportKey("raw", key);
      })
      .then(function (raw) {
        return decryptPage(derived).then(function (html) {
          // Cached before the document is replaced -- afterwards there is
          // no page left to run the rest of this handler in.
          remember(raw);
          show(html);
        });
      })
      .catch(function () {
        err.textContent = "That passphrase didn't work.";
        button.disabled = false;
        button.textContent = "View the site";
        input.select();
      });
  });

  var cached = null;
  try { cached = sessionStorage.getItem(CACHE_KEY); } catch (e) {}
  if (cached) {
    // Hidden rather than removed: this page is about to be replaced by
    // the real one, and flashing "this site isn't public yet" on every
    // link a reader follows would be worse than a blank moment. Put
    // back if the cached key turns out not to work.
    form.style.visibility = "hidden";
    crypto.subtle
      .importKey("raw", bytes(cached), { name: "AES-GCM", length: 256 }, true, ["decrypt"])
      .then(decryptPage)
      .then(show)
      .catch(function () {
        try { sessionStorage.removeItem(CACHE_KEY); } catch (e) {}
        form.style.visibility = "";
        input.focus();
      });
  }
})();
</script>
</body>
</html>
"""
