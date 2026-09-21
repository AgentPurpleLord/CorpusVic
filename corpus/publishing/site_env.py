"""
The public site's passphrase, and where it is kept.

One file, `deploy/site.env`, read by two different things for two
different reasons: the live site checks a visitor's passphrase against it
at run time, and the archive build encrypts every page with it. Both need
it, and neither should import the other to get it, so it lives here.

A shell variable lives as long as the shell, which is how the gate came
off the published site once already: the build was carried over to the
server but the passphrase was not, and an ungated build is not an error
-- it is a supported configuration, so nothing complained.
"""
import os
from pathlib import Path

from corpus import PROJECT_ROOT

# From the package root, not by counting `.parent`s from this file: this
# module moved into corpus/publishing/ and the count did not follow it, so
# the passphrase was looked for at corpus/deploy/site.env and never found.
# An ungated build is a supported configuration, so nothing complained --
# the gate simply came off.
SITE_ENV_FILE = PROJECT_ROOT / "deploy" / "site.env"
VARIABLE = "SITE_PASSWORD"


def password_from_env_file(path: "Path | None" = None) -> "str | None":
    """SITE_PASSWORD as recorded on this machine, or None."""
    path = Path(path) if path else SITE_ENV_FILE
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == VARIABLE:
            return value.strip().strip("'\"") or None
    return None


def site_password(path: "Path | None" = None) -> "str | None":
    """The passphrase, from the environment first and the file second.

    The environment wins so that a one-off run can override what is on
    disk without editing it; the file is what survives a reboot."""
    return os.environ.get(VARIABLE) or password_from_env_file(path)
