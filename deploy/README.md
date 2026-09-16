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
sudo mkdir -p /opt/corpusvic
sudo git clone https://github.com/AgentPurpleLord/CorpusVic.git /opt/corpusvic
cd /opt/corpusvic
```

(Or `git pull` there if it's already cloned.)

## 2. Python environment

```bash
sudo apt update
sudo apt install -y python3-venv
cd /opt/corpusvic
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements-site.txt
```

`requirements-site.txt` rather than `requirements-gui.txt`: this server
both runs the admin tool and builds the published site, and the site
build needs one library more (see the file's own comment). It includes
the GUI requirements, so this one command covers both.

Python 3.11 or newer. `python3 --version` on Ubuntu 24.04 is 3.12, which
is fine; on an older release install `python3.11` first.

## 3. A dedicated, unprivileged user to run it as

```bash
sudo useradd --system --home /opt/corpusvic --shell /usr/sbin/nologin dashboard
sudo chown -R dashboard:dashboard /opt/corpusvic
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

## 7. Run the public site

The public side is an application, not a directory of files:
`public.py` reads the same database the admin tool writes, so a review
decision is on the site the moment it is saved. There is no build step
between the two to have not run.

Put the passphrase in place first. Without it `public.py` refuses to
start rather than serving the corpus to anyone who asks:

```bash
sudo cp deploy/site.env.example deploy/site.env
sudo nano deploy/site.env            # SITE_PASSWORD=...
sudo chmod 600 deploy/site.env
sudo chown dashboard:dashboard deploy/site.env
```

This is a third password, separate from the other two: `SITE_PASSWORD`
opens the public site, `DASHBOARD_PASSWORD` opens the admin tool, and the
deploy key pushes to GitHub. Do not reuse one for another.

It is worth knowing what it now buys you. The passphrase is what session
cookies are signed with, so changing it and restarting the service ends
every session that exists. If it ever leaks, that is a five-second
problem rather than a thirty-day one -- which was not true of the old
gate, where published pages and a leaked passphrase could never be
recalled.

```bash
sudo cp deploy/public.service /etc/systemd/system/corpusvic-public.service
sudo systemctl daemon-reload
sudo systemctl enable --now corpusvic-public
sudo systemctl status corpusvic-public     # confirm it's "active (running)"
```

**Check it before Caddy points at it.** It is on loopback until you
change the Caddyfile, so this is the moment to be sure it is not open:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/                       # 303 -> /login
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/browse/crimes-act/     # 303 -> /login
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/search?q=anything  # 401
curl -s http://127.0.0.1:8001/robots.txt                                              # Disallow: /
```

A `200` on any of the first three means the site is open. Stop and find
out why before going further.

### Choosing what is on the public site

Nothing is, until you say so. Each card on the dashboard carries a badge
reading **On the public site** or **Not published**; click it to change
it. The decision is per *work*, so an Act and all its reprints go
together, and it lives in `data/legislation.db` -- so it travels to
GitHub with your review work on the next push rather than living only on
this machine.

The first time the dashboard starts after this feature lands it records
every document already parsed as published, so nothing that was on the
site disappears. Anything parsed after that starts off until you publish
it.

### The search index

Search reads `data/search.db`, which is built from the parses and the
review database. It is gitignored and rebuilt from scratch in a few
seconds, so it is never something to back up or push.

The dashboard builds it: **Rebuild search index**, beside the archive
button, and automatically whenever you publish or withdraw a work. The
row beside the button says whether what is there still matches the data.
Build it once now:

```bash
cd /opt/corpusvic
sudo -u dashboard .venv/bin/python -m corpus.search --build
```

Until it exists the site works normally and the search page says the
index has not been built yet.

### The offline archive

`export_static_site.py` still exists and still writes `_site/`. It is no
longer how the site is served -- it is a snapshot that survives this
server, and it is the rollback if the live site ever misbehaves (see the
commented-out block in `deploy/Caddyfile.example`).

```bash
cd /opt/corpusvic
sudo -u dashboard .venv/bin/python export_static_site.py --out _site
```

About three minutes for the whole corpus. It publishes the same works the
live site does; `--include-unpublished` builds an archive of everything
held instead. It keeps the old encryption gate, because a static host has
no server to check a passphrase -- there, encrypting the pages is the
only honest answer.

Overnight, if you would rather not remember:

```bash
sudo tee /etc/systemd/system/corpus-site.service >/dev/null <<'UNIT'
[Unit]
Description=Rebuild the offline Corpus archive
[Service]
Type=oneshot
User=dashboard
WorkingDirectory=/opt/corpusvic
ExecStart=/opt/corpusvic/.venv/bin/python export_static_site.py --out _site
UNIT

sudo tee /etc/systemd/system/corpus-site.timer >/dev/null <<'UNIT'
[Unit]
Description=Rebuild the offline Corpus archive daily
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now corpus-site.timer
```

The GitHub Pages workflow builds the same archive off this server
entirely. Worth keeping for exactly that reason -- it is a copy of the
corpus that does not depend on one VPS -- but it needs `SITE_PASSWORD` as
a repository secret, or it publishes openly whatever it builds.

## 8. Let the server push your review work back to GitHub

Reviewing on the server writes to `data/legislation.db` there, and that
database is the review work. Until it is pushed it exists on one disk.

Give the server its own deploy key with write access. Generate it **on
the server**: the private half should never exist anywhere else, which
means never pasting it into a chat, a password manager shared with
anything, or the repository itself.

```bash
sudo -u dashboard mkdir -p /opt/corpusvic/.ssh
sudo -u dashboard chmod 700 /opt/corpusvic/.ssh
sudo -u dashboard ssh-keygen -t ed25519 -C "corpusvic-vps" \
  -f /opt/corpusvic/.ssh/id_ed25519 -N ""
```

No passphrase (`-N ""`), because nothing can type one: the pushes are
made by a service account with no shell. What stands in for it is that
the key is useless without the server, is scoped to this one repository,
and can be revoked from GitHub in a click.

Teach the server who GitHub is, or the first push fails on host
verification with no one there to answer the prompt:

```bash
sudo -u dashboard sh -c 'ssh-keyscan github.com >> /opt/corpusvic/.ssh/known_hosts'
sudo -u dashboard ssh-keygen -lf /opt/corpusvic/.ssh/known_hosts
```

That second line prints the fingerprints just written. Check them against
GitHub's own published list (docs.github.com, "GitHub's SSH key
fingerprints") before going further -- ssh-keyscan trusts whatever
answers, so this is the step that makes it trust the right thing.

Now the public half, which is the part that leaves the machine:

```bash
sudo cat /opt/corpusvic/.ssh/id_ed25519.pub
```

Add it to the repository on GitHub under **Settings → Deploy keys → Add
deploy key**, with **"Allow write access" ticked**. A deploy key belongs
to exactly one repository -- GitHub refuses a public key already used as
a deploy key on another -- so if it is rejected as already in use, that
is what happened.

Check it before relying on it:

```bash
sudo -u dashboard ssh -T git@github.com
# "Hi AgentPurpleLord/CorpusVic! You've successfully authenticated, but
#  GitHub does not provide shell access." -- the named repository, and
#  the refusal of shell access, are both correct.
```

Then point the checkout at SSH and tell git who it is:

```bash
cd /opt/corpusvic
sudo -u dashboard git remote set-url origin git@github.com:AgentPurpleLord/CorpusVic.git
sudo -u dashboard git config user.name "Corpus VPS"
sudo -u dashboard git config user.email "you@example.com"
# The database is opened in WAL mode, so a recent write can still be
# sitting in the -wal file rather than the committed one. This hook
# checkpoints it on every commit so you never push a half-written
# database.
sudo -u dashboard cp deploy/pre-commit.hook.example .git/hooks/pre-commit
sudo -u dashboard chmod +x .git/hooks/pre-commit
```

Confirm the whole path works while nothing is at stake:

```bash
sudo -u dashboard git -C /opt/corpusvic push --dry-run
```

After that, git is a row of buttons. The dashboard at `/admin` carries a
strip along the top saying how the review work stands against GitHub --
how much is unpushed, whether the remote has moved on -- and the things
you can do about it. Each button says in its tooltip why it is greyed
out when it is, because a button that does nothing when clicked reads as
a broken page.

| Button | What it does | What it refuses |
| --- | --- | --- |
| **Refresh** | Re-reads this checkout and fetches the remote | -- |
| **Push to GitHub** | Commits what changed under `data/` and pushes it | When the remote is ahead. It never forces |
| **Pull** | Fast-forwards onto what the remote has | When there are uncommitted changes, or the two have diverged |
| **Save locally** | Commits without pushing | -- |
| **Discard...** | Throws away uncommitted changes under `data/` | Unless you type the word out |

Only `data/` is ever staged, so an edit left in the working tree on the
server stays there. The database's write-ahead log is checkpointed
first, every time, so what is committed is the whole state rather than
whatever had been folded into the file so far.

**Pull is fast-forward only, and that is not a limitation to work
around.** `data/legislation.db` is synced as one whole file: git cannot
merge two versions of it, and the merge it would otherwise attempt ends
in a conflict on a binary file that nobody can resolve. So a checkout
that has diverged -- commits here that GitHub doesn't have, and commits
there this server doesn't -- stops and says so. One side has to be
chosen, deliberately, on a machine where you can see what each contains.

**Save locally** exists for the case Push cannot help with: a remote
that isn't answering. The work still lands in a commit that survives a
restart, and the push can follow whenever the network does.

**Discard** is the only button here that destroys anything, so it is the
only one that asks for more than a click, and the only one that keeps a
copy: the database is written to `_backups/legislation-<timestamp>.db`
before anything is restored. Untracked files under `data/` -- a PDF just
uploaded, a parse not yet committed -- are left alone, because they have
no committed version to go back to.

### When a pull brings new code

A pull that changes a `.py` file changes nothing about what is running:
this process imported its code at startup and cannot reload it. The
dashboard notices -- it compares the commit it started on against the
one now checked out -- and says so in a strip of its own, with a
**Restart the dashboard** button.

That button works by exiting, and letting systemd start the service
again. So it is offered only where systemd will actually do that: the
unit file is read and its `Restart=` checked, and if it says `no`, or
this process was not started by systemd at all, the button is disabled
and says why rather than stopping a service nothing would bring back.
`deploy/dashboard.service` ships with `Restart=on-failure`, which is
enough.

**The button restarts the dashboard only.** There are two services now,
and the public site imports the same `corpus/` code -- so a pull that
touched it leaves the public side running the old version with nothing
saying so. Restart both:

```bash
sudo systemctl restart dashboard corpusvic-public
```

### Rebuilding the archive and the search index

Neither of these is how the public site gets its content any more -- it
reads the database directly, so a review decision is live the moment it
is saved.

**Rebuild the public site**, in the row under the sync strip, builds the
offline archive into `_site/` (see "The offline archive" above). It runs
the script with no passphrase argument, so the script takes
`SITE_PASSWORD` from `deploy/site.env` and refuses outright to replace a
gated build with an open one. Taking the gate off stays a deliberate
command (`--no-password`) rather than something a button can do by
accident.

**Rebuild search index**, beside it, rebuilds `data/search.db`. That one
does affect the live site: search cannot find what is not indexed. It
happens automatically when you publish or withdraw a work, and the row
beside the button says whether the index still matches the data.

The equivalents by hand, if you would rather, or if a button is telling
you something you want to look at directly:

```bash
cd /opt/corpusvic
sudo -u dashboard git add data/
sudo -u dashboard git commit -m "Review progress"
sudo -u dashboard git push
sudo -u dashboard git pull --ff-only
sudo systemctl restart dashboard
sudo -u dashboard .venv/bin/python export_static_site.py --out _site
```

To revoke the key later -- a rebuilt server, a suspicion, or just
tidying -- delete it under Settings → Deploy keys. That is the whole
revocation: nothing else on GitHub trusts it.

One warning, and it is the same one as anywhere else in this project:
`data/legislation.db` is synced as a whole file, not merged. If you
review on the server and also on your laptop without pulling in between,
whichever pushes last wins and the other's work is gone. Once the server
is where you review, review only there.

## 9. Firewall

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

## 10. Verify

Visit `https://corpusvic.au/admin` from any browser. You should land on
the login page over a valid HTTPS connection, at `/admin/login`. Log in
with the credentials you set in step 4 (or the placeholder, which then
forces you to set a real one immediately), and you arrive at the
dashboard; "Review" on any document opens the review GUI at
`/admin/review/<document>/`.

Then `https://corpusvic.au/` itself: you should get the unlock page, and
the passphrase from `deploy/site.env` should get you to the list of
published works. Search from the box in the header of any page.

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

## When it doesn't load

Work down this list; each step tells you which layer to stop at.

**Is anything listening?** On the server:

```bash
sudo ss -tlnp | grep -E ':(80|443|8000)\b'
sudo systemctl status caddy dashboard --no-pager
```

**Does the site work, ignoring DNS entirely?** This is the decisive one:
it asks the server for the site by name, over loopback, so nothing about
the domain or the network is involved.

```bash
curl -sI -H 'Host: corpusvic.au' http://127.0.0.1/ | head -1
curl -sI -H 'Host: corpusvic.au' http://127.0.0.1/admin | head -1
```

A 200 (or a 308 for `/admin`) means the server is fine and the problem is
DNS, a firewall, or the certificate. A 404 for the first means `_site` was
never built (step 7). A 502 for the second means Caddy is up but the
dashboard is not.

**Does the name point here?** From your own machine, not the server:

```bash
dig +short corpusvic.au        # expect the VPS's own IP
```

If it returns something else, that is the answer, and nothing on the
server can fix it.

### Getting the public key out of a web console

A provider's browser console usually will not let you copy text out of
it, which makes an 80-character public key hard to get to GitHub. You do
not have to retype it: the server is already serving files over HTTP, so
let it serve this one for a minute.

```bash
sudo cp /opt/corpusvic/.ssh/id_ed25519.pub /opt/corpusvic/_site/key.txt
```

Open `https://corpusvic.au/key.txt` in an ordinary browser, copy it from
there into GitHub's Deploy keys page, then put it back:

```bash
sudo rm /opt/corpusvic/_site/key.txt
```

A public key is public, so this gives nothing away -- but remove it
anyway, because a file nobody meant to publish is worth not leaving
published. Only ever the `.pub`. The private half never leaves the
server.

If the site is behind its passphrase, this still works: the gate encrypts
the pages the build writes, and a file copied in beside them is served as
it is.

### If you cannot SSH to the server at all

Worth fixing before anything else -- every operation here is easier over
SSH than through a console, and this guide assumes one. Add your own
machine's public key to the instance, either through the provider's
control panel or, from the console:

```bash
sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh
sudo nano /root/.ssh/authorized_keys     # paste your laptop's id_*.pub
sudo chmod 600 /root/.ssh/authorized_keys
```

Pasting *into* a web console usually works even where copying out does
not, which is the direction that matters here.

### "The authenticity of host 'github.com' can't be established"

Not about your deploy key. ssh is asking whether the host answering is
really GitHub, which it asks once per machine and would ask with the key
perfectly installed.

Answering it deliberately means comparing the fingerprint it prints
against the list GitHub publishes (docs.github.com, "GitHub's SSH key
fingerprints") and typing `yes`. That is the honest way, and worth the
minute.

Accepting it on first sight instead is what `ssh-keyscan` does, and what
the dashboard's own git calls do (`StrictHostKeyChecking=accept-new` --
see corpus/sync.py). It trusts whatever answers the first time and
refuses any change afterwards, which is weaker than checking but far
stronger than turning the check off:

```bash
sudo -u dashboard sh -c 'ssh-keyscan github.com >> /opt/corpusvic/.ssh/known_hosts'
```

### "fatal: detected dubious ownership"

git refusing to work in a repository owned by somebody other than the
user running it. The checkout belongs to `dashboard` (step 3), so this is
what you get running git as yourself or under plain `sudo`.

Run it as the owner, which is what every git command in this guide does:

```bash
sudo -u dashboard git -C /opt/corpusvic fetch
```

**Do not take git's own advice here.** It suggests adding a
`safe.directory` exception, which gets the command through and leaves
every file it writes owned by whoever ran it -- a `.git/index` or a
freshly fetched pack that the service can then no longer write. The
failure surfaces later, somewhere else, as a dashboard that cannot commit.

If that has already happened -- an earlier `sudo git pull`, say -- put the
ownership back:

```bash
sudo chown -R dashboard:dashboard /opt/corpusvic
sudo -u dashboard git -C /opt/corpusvic status      # expect it to just work
```

Worth checking for stragglers, since one root-owned file is enough to
break a commit:

```bash
sudo find /opt/corpusvic ! -user dashboard -print -quit
```

### The admin page says a sync or API call was "not found"

A `git pull` without the restart after it. The two halves of the
dashboard update at different moments: `static/dashboard.html` is served
off disk on every request, so the browser has the new page the instant
the pull lands, while the routes it calls live in a process that started
before it. The new page asks for a route the old process has never heard
of and gets a 404.

```bash
sudo systemctl restart dashboard
```

To tell this apart from a route that genuinely is not there, ask the
service directly:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/admin/api/sync/status
```

`401` means the route exists and is only asking who you are, which is the
answer you want. `404` after a restart means the pull did not bring the
code -- check `git -C /opt/corpusvic log --oneline -1` against GitHub.

Worth the habit: any pull that touched a `.py` file needs the restart,
and one that touched `corpus/`, `static/site/` or the data needs the site
rebuilt as well. Both lines are in "Updating later" above.

### "Permission denied (publickey)" / "Could not read from remote repository"

The same mistake as dubious ownership, one step further along: git run as
the wrong user. The deploy key lives in `/opt/corpusvic/.ssh/`, which is
`dashboard`'s home, so ssh run as root looks in `/root/.ssh/`, finds
nothing GitHub accepts, and is refused.

If `sudo -u dashboard git -C /opt/corpusvic pull` works and a bare
`git fetch` does not, that is the whole diagnosis -- the second one is
root.

```bash
sudo -u dashboard git -C /opt/corpusvic fetch
```

Adding a `safe.directory` exception for root does not fix this and makes
it worse: it gets root past the ownership check so that it can go on to
write root-owned files into a checkout the service has to be able to
write. If you added one, take it back out and put the ownership right:

```bash
sudo git config --global --unset-all safe.directory
sudo chown -R dashboard:dashboard /opt/corpusvic
```

To confirm which key ssh is actually offering:

```bash
sudo -u dashboard ssh -T git@github.com    # names the repository if the key is right
sudo ssh -T git@github.com                 # "Permission denied" -- expected, root has no key
```

The second line failing is correct, not a problem to fix. Root is not
meant to be able to push this repository; `dashboard` is.

### Two traps worth knowing before you start

**The bare IP will not load, even when everything is right.** The site
block matches on hostname, so a request carrying `Host: 203.0.113.10`
matches nothing and Caddy answers 404. `deploy/Caddyfile.example` has an
optional `:80` block for reaching it by IP while DNS is still settling --
it serves the published site only, never `/admin`, because there is no
certificate for an IP address and a login has no business crossing the
network in the clear.

**A proxying CDN in front of the domain stops the certificate issuing.**
If the A record points at Cloudflare (or anything similar) rather than
straight at the VPS, Let's Encrypt's HTTP challenge is answered by the CDN
instead of by Caddy, and Caddy never gets a certificate -- so HTTPS fails
in a way that looks like Caddy being broken. You can tell from `dig`: an
address that is not your server's is a proxy in front of it.

Either point the record straight at the VPS (in Cloudflare's terms,
"DNS only" -- the grey cloud) and let Caddy hold the certificate, or keep
the proxy and give Caddy a certificate some other way, which means an
origin certificate from the CDN or a DNS-01 challenge. The first is
simpler and is what the rest of this guide assumes.

## If the site was open when it should not have been

Much better than it used to be. Change `SITE_PASSWORD` in
`deploy/site.env` and restart:

```bash
sudo systemctl restart corpusvic-public
```

That closes the site *and* ends every session that exists, because the
signing key for session cookies is derived from the passphrase. Anyone
who was already inside is outside again.

If the *archive* was built and published open -- on GitHub Pages, say --
that is the older, harder problem, and rebuilding it with the passphrase
only closes it to new readers. Two things it does not undo, and both are
worth a few minutes:

**Anything already crawled.** An open build published thousands of
provisions of mostly unchecked text. Check what is out there:

```
site:corpusvic.au
```

in a search engine. Where there are results, ask for removal rather than
waiting -- Google Search Console's Removals tool, Bing Webmaster Tools'
equivalent -- because a crawler will otherwise keep serving its snapshot
long after the pages behind it stopped answering. A gated rebuild also
publishes a Disallow-everything robots.txt, which stops the *next* crawl
but does nothing about a cached one.

**Anyone who read it.** There is no log of that unless Caddy's access log
was on, and nothing to be done about it either way. Worth knowing rather
than worth acting on: what was published was the parser's reading of
public legislation, with the disclaimers already on every page.

## If you already deployed under the old path

A server set up before the repository was renamed has everything under
`/opt/vic-legislation-parser`. Moving it is a few minutes; rebuilding the
box is not worth it, and carries one risk that rebuilding usually hides:
Let's Encrypt allows only a handful of certificates per week for the same
set of domain names, so a fresh server that re-requests one can find
itself rate-limited and left without HTTPS for days. Nothing about the
move touches the certificate Caddy already holds.

```bash
# The service's WorkingDirectory is the old path, so stop it first.
sudo systemctl stop dashboard

# Move the checkout, and the service user's home along with it -- its
# home is the checkout, and that is where the deploy key lives.
sudo mv /opt/vic-legislation-parser /opt/corpusvic
sudo usermod -d /opt/corpusvic dashboard

# Point git at the renamed repository. GitHub redirects the old name
# indefinitely, so this is tidiness rather than repair -- but a remote
# that names the repository is one less thing resting on a redirect.
cd /opt/corpusvic
sudo -u dashboard git remote set-url origin git@github.com:AgentPurpleLord/CorpusVic.git
sudo -u dashboard git pull

# Rebuild the virtualenv. A venv is not relocatable: every console script
# in .venv/bin carries the old absolute path in its shebang, so pip stops
# working the moment the directory moves. The service itself would
# survive -- it runs .venv/bin/python3, which is a symlink -- which is
# exactly what makes this worth doing deliberately rather than
# discovering later, at the next upgrade.
sudo rm -rf .venv
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements-site.txt
sudo chown -R dashboard:dashboard /opt/corpusvic

# Re-point the unit files in place, rather than copying the repo's over
# them: yours may carry an IP allowlist or anything else you added.
sudo sed -i 's|/opt/vic-legislation-parser|/opt/corpusvic|g' \
    /etc/systemd/system/dashboard.service \
    /etc/caddy/Caddyfile
# And the nightly site rebuild, if you made one.
sudo sed -i 's|/opt/vic-legislation-parser|/opt/corpusvic|g' \
    /etc/systemd/system/corpus-site.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl start dashboard
sudo systemctl reload caddy
```

Then check, in this order -- the first failure tells you which step to
look at:

```bash
# If the site is gated, its front page is the unlock page rather than the
# index -- this is the one that catches a passphrase having gone missing.
curl -s https://corpusvic.au/ | grep -c 'id="payload"'   # 1 when gated, 0 when open
curl -s https://corpusvic.au/robots.txt                 # Disallow: / unless you asked otherwise
sudo systemctl status dashboard          # active (running)
grep -r /opt/vic-legislation-parser /etc/systemd/system /etc/caddy   # expect nothing
curl -sI https://corpusvic.au/admin | head -1                        # 308 to /admin/
curl -sI https://corpusvic.au/ | head -1                             # 200, the published site
sudo -u dashboard git -C /opt/corpusvic push --dry-run               # the deploy key still works
```

Starting fresh instead is the better call only if the box has drifted --
changes you made while getting it working and can no longer enumerate.
Nothing on it is irreplaceable as long as `data/legislation.db` has been
pushed: the site rebuilds in about three minutes, the password is one
file, and the deploy key takes a minute to reissue.

## Updating later

Most of this is now a button on `/admin` -- **Pull**, then **Restart the
dashboard** if it brought new code (see "After that, git is a row of
buttons" above). What is left for a terminal is a dependency change and
restarting the *public* service, neither of which the dashboard has any
business doing as itself.

```bash
cd /opt/corpusvic
# As the owner of the checkout, so nothing ends up root-owned and
# unwritable by the service afterwards.
sudo -u dashboard git pull
sudo .venv/bin/pip install -r requirements-site.txt   # in case dependencies changed
# Both services: the public site imports the same corpus/ code, so a pull
# that touched it leaves that side running the old version.
sudo systemctl restart dashboard corpusvic-public
# Only for the offline archive -- the live site is already up to date.
sudo -u dashboard .venv/bin/python export_static_site.py --out _site
```

Restarting the service does not lose review progress or the login
credential you've set -- both live in `data/` and `.dashboard_auth.json`
respectively, neither of which this restart touches.

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
sqlite3 /opt/corpusvic/data/legislation.db ".backup /path/to/backup/legislation-$(date +%F).db"
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
git clone https://github.com/AgentPurpleLord/CorpusVic.git
cd CorpusVic
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
