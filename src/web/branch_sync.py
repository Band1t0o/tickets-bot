"""Bringing the branch's results onto the machine that reads them.

The cloud sweep commits to `origin/main`; this app lists `data/sweeps/<id>/` off
the working tree. Nothing ever joined the two. On 22 Aug that meant three
finished tokyo-round-trip sweeps - including a deep run of 64 searches and 638
flights - sat on the branch while the Results picker showed the previous day's
local runs and gave no hint that anything was missing. The checkout was six
commits behind and nothing on screen could say so.

It is the same shape as the failure `cloud_runs.py` was written for: a run that
worked, and a screen with no way to tell. So it keeps the same discipline. No
call here raises past the boundary; no git, no remote, or a ref that cannot be
resolved all mean *cannot say*, and a panel that cannot say must say that rather
than draw a confident empty list.

What it will not do matters as much as what it does. This moves the checkout by
fast-forward only, and refuses - with a sentence, for a person to act on - when
it cannot. No `--force`, no `--rebase`, no `--autostash`, no `checkout --`. A
sweep is an hour of prices that cannot be observed again, and nothing running
unattended may be able to discard one.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

# The branch the scheduled sweep runs from, and commits its results to.
CLOUD_REF = os.getenv("CLOUD_REF", "origin/main")

# How stale the last fetch may be before a read refreshes it in the background.
# Generous: the thing being watched commits a few times an hour at most, and the
# rule this obeys is that rendering a page never waits on a remote.
FETCH_EVERY_SECONDS = 60

_fetched_at: float | None = None
_fetch_lock = threading.Lock()
_fetching = False


def _run(*args: str) -> tuple[int, str, str]:
    """A git command as (returncode, stdout, stderr). Never raises.

    Returns 127 with the reason as stderr when git itself cannot be run, so a
    checkout without git looks like a failed command rather than an exception.
    """
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"git did not answer: {exc}"
    return done.returncode, done.stdout, done.stderr


def git(*args: str) -> str | None:
    """Read-only git output, or None if the question cannot be answered.

    A repo without a remote, a checkout without git, a ref that was never
    fetched: all of them mean "cannot say", and the callers are written to
    report that rather than to guess.
    """
    code, out, _ = _run(*args)
    return out if code == 0 else None


def remote() -> str | None:
    """The first configured remote, or None if there is none."""
    names = (git("remote") or "").split()
    return names[0] if names else None


def fetch() -> None:
    """Bring `CLOUD_REF` up to date. Best effort; a failure here is not an error.

    Everything else reads the last fetch rather than the network, so that opening
    a page never blocks on a remote. That makes every answer only as fresh as
    this call, which is why the page is told when it last ran.
    """
    global _fetched_at
    name = remote()
    if name:
        _run("fetch", name, "--quiet")
    _fetched_at = time.monotonic()


def fetched_recently() -> bool:
    return _fetched_at is not None and time.monotonic() - _fetched_at < FETCH_EVERY_SECONDS


def fetch_in_background(force: bool = False) -> bool:
    """Refresh the fetch off the request thread if it has gone stale.

    Returns whether a fetch was started. One at a time: this is called from page
    load and from tab switches, and a pile of overlapping `git fetch` processes
    against one repo is a lock fight rather than freshness.
    """
    global _fetching
    with _fetch_lock:
        if _fetching or (fetched_recently() and not force):
            return False
        _fetching = True

    def work() -> None:
        global _fetching
        try:
            fetch()
        finally:
            with _fetch_lock:
                _fetching = False

    threading.Thread(target=work, daemon=True).start()
    return True


def _count(spec: str) -> int | None:
    out = git("rev-list", "--count", spec)
    try:
        return int((out or "").strip())
    except ValueError:
        return None


def _branch_path(data_dir: Path) -> str | None:
    """`data_dir` as the branch names it, or None if it is not in this repo.

    A `DATA_DIR` pointed outside the checkout has no counterpart on the branch,
    which is a real answer rather than a failure: there are no cloud runs there
    to be missing.
    """
    top = git("rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        return data_dir.resolve().relative_to(Path(top.strip()).resolve()).as_posix()
    except ValueError:
        return None


def _branch_stamps(data_root: str, scenario_id: str) -> set[str]:
    """Run directories the branch holds for one trip."""
    out = git(
        "ls-tree", "-d", "--name-only",
        f"{CLOUD_REF}:{data_root}/sweeps/{scenario_id}",
    )
    if out is None:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _local_stamps(data_dir: Path, scenario_id: str) -> set[str]:
    root = data_dir / "sweeps" / scenario_id
    try:
        return {p.name for p in root.iterdir() if p.is_dir()}
    except OSError:
        return set()


def state(data_dir: Path, scenario_ids: list[str]) -> dict:
    """How this checkout stands against the branch the cloud commits to.

    `missing_count` is the number the page shows, and it is deliberately not
    `behind`. The probe commits every two hours, so "six commits behind" on
    22 Aug was three sweeps and three probe observations; only one of those two
    numbers answers "how many runs am I not seeing".
    """
    if git("rev-parse", "--git-dir") is None:
        return _unknown(
            "This folder is not a git checkout, so the app cannot tell whether the "
            "cloud has results it does not."
        )
    if remote() is None:
        return _unknown(
            "This checkout has no remote, so there is no branch to read cloud "
            "results from."
        )
    if git("rev-parse", "--verify", "--quiet", CLOUD_REF) is None:
        return _unknown(
            f"{CLOUD_REF} has never been fetched here, so the app cannot see what "
            "the cloud has committed."
        )

    behind = _count(f"HEAD..{CLOUD_REF}")
    ahead = _count(f"{CLOUD_REF}..HEAD")
    if behind is None or ahead is None:
        return _unknown(f"git could not compare this checkout with {CLOUD_REF}.")

    dirty = bool((git("status", "--porcelain", "--untracked-files=no") or "").strip())
    data_root = _branch_path(data_dir)
    missing: dict[str, list[str]] = {}
    if data_root is not None:
        for scenario_id in scenario_ids:
            gap = _branch_stamps(data_root, scenario_id) - _local_stamps(
                data_dir, scenario_id
            )
            if gap:
                missing[scenario_id] = sorted(gap, reverse=True)

    return {
        "known": True,
        "reason": "",
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "missing": missing,
        "missing_count": sum(len(stamps) for stamps in missing.values()),
        "can_fast_forward": ahead == 0,
        "blocked_by": _blocked_by(ahead),
        "ref": CLOUD_REF,
    }


def _unknown(reason: str) -> dict:
    return {
        "known": False,
        "reason": reason,
        "behind": None,
        "ahead": None,
        "dirty": None,
        "missing": {},
        "missing_count": 0,
        "can_fast_forward": False,
        "blocked_by": "",
        "ref": CLOUD_REF,
    }


def _blocked_by(ahead: int) -> str:
    """Why a fast-forward is not available, as a sentence to act on.

    Only divergence is decided here. A dirty tree is deliberately *not* a gate:
    `git merge --ff-only` refuses on its own, and only when the files it would
    update are among the modified ones. Since the cloud commits `data/` and the
    person here edits code, gating on any dirty file at all would refuse nearly
    every morning for a conflict that does not exist. Git makes that call
    exactly, and `pull` reports whatever it says.
    """
    if not ahead:
        return ""
    return (
        f"This checkout has {ahead} commit(s) the branch does not, so the cloud's "
        "results cannot be fast-forwarded in. Nothing has been changed. Push or "
        "rebase those commits, then try again."
    )


def pull(data_dir: Path, scenario_ids: list[str]) -> dict:
    """Fast-forward this checkout onto the branch, or refuse and say why.

    Synchronous, unlike the reads: someone pressed a button and is waiting for
    the answer. Fetches first, because a decision taken against a fortnight-old
    ref is not an answer - the same reason `run-cloud` fetches before it compares
    trips.
    """
    fetch()
    before = state(data_dir, scenario_ids)
    if not before["known"]:
        return {"synced": False, "reason": before["reason"], "gained": {}, **_shape(before)}
    if before["blocked_by"]:
        return {"synced": False, "reason": before["blocked_by"], "gained": {}, **_shape(before)}
    if not before["behind"]:
        return {
            "synced": False,
            "already_current": True,
            "reason": "",
            "gained": {},
            **_shape(before),
        }

    # --ff-only is the whole safety story. A merge commit, a rebase or an
    # autostash could each lose a local sweep, and this runs on a button press
    # with nobody reading git's output.
    code, _, stderr = _run("merge", "--ff-only", CLOUD_REF)
    after = state(data_dir, scenario_ids)
    if code != 0:
        return {
            "synced": False,
            "already_current": False,
            # Verbatim, because git names the files. "Your local changes to
            # src/web/app.py would be overwritten" is actionable; "could not
            # sync" is the kind of summary this whole app keeps replacing.
            "reason": "git would not fast-forward, and nothing has been changed. "
            + (stderr.strip() or "It gave no reason."),
            "gained": {},
            **_shape(after),
        }

    gained = {
        scenario_id: sorted(
            set(stamps) - set(after["missing"].get(scenario_id, [])), reverse=True
        )
        for scenario_id, stamps in before["missing"].items()
    }
    return {
        "synced": True,
        "already_current": False,
        "reason": "",
        "commits": before["behind"],
        "gained": {trip: stamps for trip, stamps in gained.items() if stamps},
        **_shape(after),
    }


def _shape(found: dict) -> dict:
    """The state fields a sync result carries back, so the page can redraw from it."""
    return {
        key: found[key]
        for key in (
            "known", "behind", "ahead", "dirty", "missing", "missing_count",
            "can_fast_forward", "blocked_by", "ref",
        )
    }
