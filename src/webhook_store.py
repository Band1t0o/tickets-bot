"""Where sweep results get sent, kept where `git add` cannot reach it.

A Discord webhook URL is a bearer token wearing a URL's clothes: whoever holds
it can post to the channel. That makes *where* it is stored the whole design.

- Not in `data/`. The scheduled workflow commits that directory so cloud runs
  can hand their findings back, and the repo is going public.
- Not in a scenario file. Those are committed too, on purpose.
- In `.secrets/`, which `.gitignore` excludes as a directory, so a new file
  added there later is covered without anyone remembering to.

The environment variable wins over the file. GitHub Actions sets it from a repo
secret and has no `.secrets/` directory at all, so saving one locally can never
change what a cloud run posts to. The file exists so that a local
`python -m src.cli sweep` notifies without exporting anything first, and so the
Sources tab has somewhere to write what you paste into it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SECRETS_DIR = Path(".secrets")
FILENAME = "discord.json"

# Discord still issues and honours discordapp.com. https only: the URL carries
# a credential, and a http:// one would put it on the wire in clear.
WEBHOOK_RE = re.compile(r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/\S+$")


def is_discord_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.match((url or "").strip()))


def _path(directory: Path | str | None = None) -> Path:
    return Path(directory or SECRETS_DIR) / FILENAME


def load_webhook(directory: Path | str | None = None) -> tuple[str | None, str]:
    """`(url, origin)` where origin is `environment`, `file` or `none`.

    Never raises: a notification is worth less than the sweep that produced it,
    so an unreadable state file loses the message and nothing else.
    """
    from_env = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if from_env:
        return from_env, "environment"

    path = _path(directory)
    if not path.exists():
        return None, "none"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        url = str(payload["discord_webhook_url"]).strip()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"[webhook] {path} is unreadable ({exc}); no notifications will be sent")
        return None, "none"
    return (url, "file") if url else (None, "none")


def save_webhook(url: str, directory: Path | str | None = None) -> Path:
    """Validated on the way in, because the failure otherwise surfaces as a
    silent 404 from Discord hours later, inside a scheduled run nobody watched."""
    url = (url or "").strip()
    if not is_discord_webhook(url):
        raise ValueError(
            "That is not a Discord webhook URL. It should look like "
            "https://discord.com/api/webhooks/<id>/<token> — copy it from the channel's "
            "Edit Channel → Integrations → Webhooks → Copy Webhook URL."
        )
    path = _path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"discord_webhook_url": url}, indent=2) + "\n", encoding="utf-8"
    )
    return path


def clear_webhook(directory: Path | str | None = None) -> bool:
    """True if a file was removed. Does not touch the environment variable —
    nothing here can, and pretending otherwise would be the lie."""
    path = _path(directory)
    if not path.exists():
        return False
    path.unlink()
    return True


def mask(url: str | None) -> str:
    """Enough to tell one webhook from another, never enough to post with.

    The id is kept because it is how you recognise which channel this is; the
    token is what actually authorises, and it is the part that is dropped.
    """
    if not url:
        return ""
    match = re.match(r"^(https://[^/]+/api/webhooks/\d+/)\S+$", url.strip())
    if not match:
        return "•" * 12
    # None of the token, not even a tail: the id already tells you which
    # channel this is, so a tail would be risk buying nothing.
    return f"{match.group(1)}{'•' * 12}"
