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
it cannot. No `--force`, no `--rebase`, no `--autostash`. A sweep is an hour of
prices that cannot be observed again, and nothing running unattended may be able
to discard one.

`take` is the one carve-out from that, and it is narrow by construction. The rule
above forbids `checkout --` because it overwrites whatever is at the path. `take`
only ever names paths that **do not exist in the working tree** - run directories
the branch has and this machine does not - and re-checks that immediately before
each one. A checkout restricted to a path with nothing at it cannot discard
anything, so the rule's reason does not reach it.

It exists because refusing to move the branch and refusing to hand over the
results are different refusals, and only the first one was ever intended. On
24 Aug a feature branch seven commits ahead of `origin/main` made `pull` refuse
correctly, and two finished cloud sweeps sat unreachable behind a sentence with
no button under it. `take` copies the directories and leaves HEAD exactly where
it was; the files land staged, for a person to commit.
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


def _in_the_way() -> list[str]:
    """Files the fast-forward would have to overwrite, so it will not run.

    `git merge --ff-only` refuses when a file it must update is also modified
    here, and it refuses before touching anything, which is right. What was
    wrong was the panel above it: `can_fast_forward` was `ahead == 0` and
    nothing else, so *Get them* was offered, git said no, and the answer came
    back as a red error on a page that had promised the opposite a second
    earlier.

    This used to be reasoned away - the cloud commits `data/`, the person here
    edits code, so the overlap does not happen. That stopped being true on
    2 Sep, when the app began publishing trip files to the branch itself. Every
    save now puts a commit on the branch touching `scenarios/<id>.json`, and the
    next edit in the narrowing panel makes that same file dirty here. The
    overlap is no longer an accident; it is the normal afternoon.

    Both sides are asked of git rather than guessed, so the answer is the one
    the merge itself would give: paths the merge must change, intersected with
    paths that differ from HEAD in this checkout. Staged or unstaged both count,
    because the merge refuses on either. Untracked files are absent from both
    lists, and correctly so - a fast-forward does not overwrite them.
    """
    changed = git("diff", "--name-only", "HEAD", CLOUD_REF)
    edited = git("diff", "--name-only", "HEAD")
    if changed is None or edited is None:
        return []
    def named(out: str) -> set[str]:
        return {line.strip() for line in out.splitlines() if line.strip()}

    return sorted(named(changed) & named(edited))


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

    # Only worth asking when there is something to fast-forward *to*. Two more
    # `git diff` calls on every page draw, to answer a question with no answer.
    in_the_way = _in_the_way() if behind else []

    return {
        "known": True,
        "reason": "",
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "in_the_way": in_the_way,
        "missing": missing,
        "missing_count": sum(len(stamps) for stamps in missing.values()),
        "can_fast_forward": ahead == 0 and not in_the_way,
        "blocked_by": _blocked_by(ahead, in_the_way),
        "ref": CLOUD_REF,
    }


def _unknown(reason: str) -> dict:
    return {
        "known": False,
        "reason": reason,
        "behind": None,
        "ahead": None,
        "dirty": None,
        "in_the_way": [],
        "missing": {},
        "missing_count": 0,
        "can_fast_forward": False,
        "blocked_by": "",
        "ref": CLOUD_REF,
    }


def _blocked_by(ahead: int, in_the_way: list[str]) -> str:
    """Why a fast-forward is not available, as a sentence to act on.

    Two different refusals, and they are not resolved the same way. Divergent
    history is about commits and wants a push or a rebase; a file open here that
    the branch also changed is about one afternoon's edit and wants a save. Both
    end at the same button underneath - the results themselves need no merge -
    so both sentences say so.

    A dirty tree on its own is still *not* a gate. `git merge --ff-only` only
    refuses over the files it would actually update, so gating on any modified
    file at all would refuse nearly every morning over a conflict that does not
    exist. `_in_the_way` asks the narrower question git asks.
    """
    if ahead:
        return (
            f"This checkout has {ahead} commit(s) the branch does not, so the cloud's "
            "results cannot be fast-forwarded in. Nothing has been changed. Push or "
            "rebase those commits, then take just the results below."
        )
    if in_the_way:
        # Named, and capped. The point is to recognise the file - usually the
        # trip you were editing a minute ago - not to read a manifest.
        shown = ", ".join(in_the_way[:3])
        rest = len(in_the_way) - 3
        return (
            f"This checkout has unsaved changes to {shown}"
            + (f" and {rest} other file(s)" if rest > 0 else "")
            + f", which {CLOUD_REF} has also changed, so git will not fast-forward "
            "over them. Nothing has been changed. Commit or revert them, or take "
            "just the results below - those need no merge."
        )
    return ""


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


def take(data_dir: Path, scenario_ids: list[str]) -> dict:
    """Copy the runs this machine is missing out of the branch, without merging.

    The answer to a refusal that is about history rather than about results. It
    checks out one path per missing run directory, so HEAD, the index's view of
    every other file, and every local commit are left exactly as they were.

    Deliberately makes no attempt to be `pull`. It brings run directories and
    nothing else: not the probe's observations, not a code change, not the trip
    files. So a checkout that has taken every run it was missing is still
    reported as diverged and still behind, because it is - and a panel that said
    otherwise would be lying about the next sweep's starting point.
    """
    fetch()
    found = state(data_dir, scenario_ids)
    if not found["known"]:
        return {"took": False, "already_current": False, "reason": found["reason"],
                "taken": {}, **_shape(found)}
    if not found["missing_count"]:
        return {"took": False, "already_current": True, "reason": "",
                "taken": {}, **_shape(found)}

    # Never None here: `missing` is only ever populated when the data directory
    # has a counterpart on the branch to compare against.
    data_root = _branch_path(data_dir)

    taken: dict[str, list[str]] = {}
    refused: list[str] = []
    for scenario_id, stamps in found["missing"].items():
        for stamp in stamps:
            # `missing` is already branch-minus-local, so this can only fire on a
            # run that appeared since `state` was computed a moment ago. Checked
            # anyway: it is the property that makes this safe, and a property
            # this module relies on is one it states rather than infers.
            if (data_dir / "sweeps" / scenario_id / stamp).exists():
                continue
            code, _, stderr = _run(
                "checkout", CLOUD_REF, "--", f"{data_root}/sweeps/{scenario_id}/{stamp}"
            )
            if code != 0:
                # Named, not counted. One run failing to copy while five succeed
                # is a different morning from none of them arriving.
                refused.append(f"{scenario_id} {stamp}: {stderr.strip() or 'git gave no reason'}")
                continue
            taken.setdefault(scenario_id, []).append(stamp)

    after = state(data_dir, scenario_ids)
    return {
        "took": bool(taken),
        "already_current": False,
        "reason": "git would not copy " + "; ".join(refused) if refused else "",
        "taken": {trip: sorted(stamps, reverse=True) for trip, stamps in taken.items()},
        **_shape(after),
    }


def _shape(found: dict) -> dict:
    """The state fields a sync result carries back, so the page can redraw from it."""
    return {
        key: found[key]
        for key in (
            "known", "behind", "ahead", "dirty", "in_the_way", "missing",
            "missing_count", "can_fast_forward", "blocked_by", "ref",
        )
    }
