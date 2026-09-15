# Deploying the admin tool to a VPS, with HTTPS

Puts the dashboard and the review GUI at **corpusvic.au/admin**, behind a
login, with the published site served from the same hostname's root.

This runs `dashboard.py` as a systemd service bound to `127.0.0.1` only,
with [Caddy](https://caddyserver.com/) as the internet-facing reverse
proxy in front of it. Caddy gets you real, auto-renewing HTTPS (via
Let's Encrypt) for free with essentially no TLS configuration of your
own, and the dashboard process itself never listens on a public
interface directly -- only Caddy does.

Written for Ubuntu/Debian. Adjust package-manager commands for another
distro; the systemd unit and Caddy config are distro-agnostic.

Prerequisite: a domain (or subdomain) with its DNS **A** record pointed
at this server's public IP. Let's Encrypt needs that to issue a
certificate, and it needs to already be resolving before you start Caddy.

## 1. Get the code onto the server

```bash
sudo mkdir -p /opt/vic-legislation-parser
sudo git clone <your-repo-url> /opt/vic-legislation-parser
cd /opt/vic-legislation-parser
```

(Or `git pull` there if it's already cloned.)

## 2. Python environment

```bash
sudo apt update
sudo apt install -y python3-venv
cd /opt/vic-legislation-parser
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements-gui.txt
```

## 3. A dedicated, unprivileged user to run it as

```bash
sudo useradd --system --home /opt/vic-legislation-parser --shell /usr/sbin/nologin dashboard
sudo chown -R dashboard:dashboard /opt/vic-legislation-parser
```

## 4. The admin password

This is **its own password**, set by you and used nowhere else. In
particular it is not `SITE_PASSWORD`, the repository secret that gates
the published site behind an unlock page: that one is shared with anyone
you give reading access to, and this one opens a tool that can upload
PDFs and run the pipeline. Never make them the same.


```bash
sudo cp deploy/dashboard.env.example deploy/dashboard.env
sudo nano deploy/dashboard.env   # fill in DASHBOARD_USERNAME / DASHBOARD_PASSWORD, or leave blank
sudo chmod 600 deploy/dashboard.env
sudo chown dashboard:dashboard deploy/dashboard.env
```

If you skip this step entirely, the dashboard starts on its built-in
placeholder login and forces a password change on first login instead --
see `dashboard.py`'s module docstring. Either way, whatever password ends
up in effect is never stored in this repo: what is stored on the server
is a PBKDF2 hash of it, in `.dashboard_auth.json`.

## 5. systemd service

```bash
sudo cp deploy/dashboard.service /etc/systemd/system/dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard
sudo systemctl status dashboard   # confirm it's "active (running)"
```

`journalctl -u dashboard -f` tails its logs.

## 6. Caddy (reverse proxy + automatic HTTPS)

These are Caddy's own official apt-repo install commands as of this
writing; if they've changed, https://caddyserver.com/docs/install has
the current version -- I couldn't reach that page live to double-check
it from this session, so treat this block as "very likely still
correct" rather than freshly verified:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

Edit `/etc/caddy/Caddyfile` (see `deploy/Caddyfile.example` in this repo
for the two-line contents) to point at your actual domain, then:

```bash
sudo systemctl reload caddy
```

## 7. Firewall

Only 80 (ACME challenge + HTTP->HTTPS redirect, which Caddy does
automatically) and 443 (HTTPS) need to be open to the internet. Port 8000
(the dashboard itself) should stay loopback-only -- it already is,
because of `--host 127.0.0.1` in the systemd unit; just don't also open
it in the firewall.

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow OpenSSH   # don't lock yourself out over SSH
sudo ufw enable
```

## 8. Verify

Visit `https://corpusvic.au/admin` from any browser. You should land on
the login page over a valid HTTPS connection, at `/admin/login`. Log in
with the credentials you set in step 4 (or the placeholder, which then
forces you to set a real one immediately), and you arrive at the
dashboard; "Review" on any document opens the review GUI at
`/admin/review/<document>/`.

Worth confirming while you are there, because both are what keep the
admin tool from leaking onto the public side:

```bash
# The app answers only under /admin -- nothing of it at the domain root.
curl -sI https://corpusvic.au/login | head -1        # expect 404
# And its session cookie is scoped to /admin, so it is never sent with a
# request for a published page.
curl -si https://corpusvic.au/admin/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"...","password":"..."}' | grep -i set-cookie   # expect Path=/admin; Secure
```

## Updating later

```bash
cd /opt/vic-legislation-parser
sudo git pull
sudo .venv/bin/pip install -r requirements-gui.txt   # in case dependencies changed
sudo systemctl restart dashboard
```

Restarting the service does not lose review progress or the login
credential you've set -- both live in `data/` and `.dashboard_auth.json`
respectively, neither of which this restarts touches.

## Backing up your review progress

All the review work a human has actually done -- accepted/flagged
pieces, link annotations, the correction log -- lives in one file:
`data/legislation.db` (SQLite), alongside `data/parsed/<act>.json`
(the raw parse those rows are keyed against). Both are committed to git
(see the next section) -- that's now the primary way this data travels
and gets backed up. Between commits, or as extra insurance before
anything risky (an OS upgrade, a migration, a `git pull` across a major
version bump), back the live file up directly too:

```bash
# A safe way to copy a live SQLite file without risking a torn read
sqlite3 /opt/vic-legislation-parser/data/legislation.db ".backup /path/to/backup/legislation-$(date +%F).db"
```

## Working from a remote dev environment

`data/legislation.db` and `data/parsed/<act>.json` are committed to
git as a pair (see `.gitignore`'s own comment on why they're kept
together): review.py's verified rows are keyed by a *positional* index
into that exact parse, so a clone that had the DB but regenerated
`parsed` from a different parser version could silently misalign
verified content with the wrong provisions. Committing both together
means a fresh clone -- a temporary cloud dev environment (Codespaces, a
VS Code remote container, a throwaway VM), say -- can start reviewing
immediately:

```bash
git clone <repo-url>
cd vic-legislation-parser
pip install -r requirements-gui.txt
python review.py criminal-procedure-act   # your review progress is already there
```

No pipeline re-run needed, and none of the drift risk a re-run would
otherwise carry.

Before committing any change to `data/legislation.db`, checkpoint it
first -- it's opened in WAL mode, so a recent write can sit in the
(gitignored) `-wal` file rather than the main one yet:

```bash
python checkpoint_db.py
```

To do this automatically on every commit instead of remembering it,
install the tracked hook template once per environment (git hooks
don't travel with a clone):

```bash
cp deploy/pre-commit.hook.example .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

When you're done working in a temporary environment, just commit and
push `data/legislation.db` (the hook above handles checkpointing) so
the progress travels back with you -- and `git pull` it into your other
environments to pick it up there. This is a single-file, whole-database
sync, not a real merge: if you review from two environments without
pulling in between, whichever you push last wins and the other's
progress is overwritten, so pull before you start a session, and avoid
leaving two environments mid-review at once.
