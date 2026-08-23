"""Bringing the branch's results onto the machine that reads them.

Driven against real git repositories built in `tmp_path`, not against fakes. The
thing being tested is almost entirely what git does with a fast-forward, a
diverged history, and a modified file - and a fake of git would only ever agree
with whatever this module already assumed. The one belief worth checking here is
the one that was wrong first: that "the checkout is dirty" and "git will refuse"
are the same statement. They are not, and only real git says so.

`conftest.no_real_git` refuses every git call for the whole suite, because the
suite runs from the repo root and `pull` writes. Each test here hands `_run` back
with a `cwd` pinned to its own throwaway clone, so nothing can reach the real
checkout even if a path is built wrong.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.web import branch_sync

TRIP = "tokyo-round-trip"

# Three sweeps and three probe observations, interleaved as the real branch has
# them. The interleaving is the point: it is what makes "6 commits behind" and
# "3 runs missing" different numbers.
CLOUD_SWEEPS = ["2026-08-22T15-31-51Z", "2026-08-22T15-36-36Z", "2026-08-22T20-30-46Z"]


def run(cwd: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> None:
    run(repo, "add", "-A")
    run(
        repo,
        "-c", "user.email=t@example.test",
        "-c", "user.name=test",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )


def sweep(repo: Path, stamp: str, legs: int = 638) -> None:
    """One committed run, in the shape the merge job commits."""
    directory = repo / "data" / "sweeps" / TRIP / stamp
    write(directory / "status.json", f'{{"state": "done", "legs_found": {legs}}}')
    write(directory / "legs.jsonl", '{"origin": "VIE"}\n')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An origin carrying three cloud sweeps, and a clone that predates them.

    Shaped like the real thing on 22 Aug: the clone has the older runs and the
    three newest exist only on the branch, with probe commits in between.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    run(origin, "init", "-b", "main")
    write(origin / "README.md", "flight watcher\n")
    # A tracked file the branch never touches again, so a local edit to it can
    # show that a dirty tree on its own does not block a fast-forward.
    write(origin / "src" / "app.py", "SPEED = 1\n")
    sweep(origin, "2026-08-21T09-55-40Z", legs=1109)
    commit(origin, "start")

    clone = tmp_path / "clone"
    run(tmp_path, "clone", str(origin), str(clone))

    # What the cloud did after this machine last looked.
    for index, stamp in enumerate(CLOUD_SWEEPS):
        sweep(origin, stamp)
        commit(origin, f"chore(data): sweep {TRIP} {stamp}")
        write(origin / "data" / "probe" / f"{index}.json", '{"observed": true}')
        commit(origin, f"chore(probe): observation {index}")
    # And one commit touching a file this machine also has, so the collision
    # case below is a real one rather than a hypothetical.
    write(origin / "README.md", "flight watcher, amended on the branch\n")
    commit(origin, "docs: amend the readme")

    monkeypatch.setattr(
        branch_sync,
        "_run",
        lambda *args: _real(clone, *args),
    )
    branch_sync.fetch()
    return clone


def _real(cwd: Path, *args: str) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def head(repo: Path) -> str:
    return _real(repo, "rev-parse", "HEAD")[1].strip()


def state(repo: Path) -> dict:
    return branch_sync.state(repo / "data", [TRIP])


# ------------------------------------------------------------------ reading


def test_the_count_is_of_runs_not_of_commits(repo):
    """Seven commits behind is three sweeps, and only one of those is the answer.

    The probe commits every two hours into the same branch, so a commit count is
    not a run count. Reporting "7 cloud runs missing" beside a picker that gains
    three would be a number nobody could reconcile, and a panel whose job is to
    be trusted cannot afford one.
    """
    found = state(repo)

    assert found["known"] is True
    assert found["behind"] == 7
    assert found["missing_count"] == 3
    assert found["missing"][TRIP] == sorted(CLOUD_SWEEPS, reverse=True)


def test_a_run_already_on_disk_is_not_reported_missing(repo):
    found = state(repo)

    assert "2026-08-21T09-55-40Z" not in found["missing"][TRIP]


def test_a_local_only_run_does_not_make_the_branch_look_behind(repo):
    """A run swept on this machine is not a run the branch is owed.

    Local sweeps land in the same directory and are frequently uncommitted - the
    checkout had two such runs on 22 Aug. The comparison is one-way on purpose.
    """
    (repo / "data" / "sweeps" / TRIP / "2026-08-21T14-22-17Z").mkdir(parents=True)

    assert state(repo)["missing_count"] == 3


def test_a_checkout_with_nothing_missing_says_so_plainly(repo):
    branch_sync.pull(repo / "data", [TRIP])

    found = state(repo)
    assert found["known"] is True
    assert found["missing_count"] == 0
    assert found["behind"] == 0


# ------------------------------------------------------------------ pulling


def test_a_clean_checkout_fast_forwards_and_the_runs_land_on_disk(repo):
    result = branch_sync.pull(repo / "data", [TRIP])

    assert result["synced"] is True
    assert result["commits"] == 7
    assert result["gained"][TRIP] == sorted(CLOUD_SWEEPS, reverse=True)
    # The directories, not just the git state: the picker reads the disk, and
    # that is the whole distance this module exists to close.
    for stamp in CLOUD_SWEEPS:
        assert (repo / "data" / "sweeps" / TRIP / stamp / "legs.jsonl").exists()


def test_pulling_twice_is_not_an_error(repo):
    branch_sync.pull(repo / "data", [TRIP])
    again = branch_sync.pull(repo / "data", [TRIP])

    assert again["synced"] is False
    assert again["already_current"] is True
    # No reason, because nothing went wrong. A reason here would be shown to
    # someone as a failure.
    assert again["reason"] == ""


def test_a_diverged_checkout_is_refused_and_nothing_moves(repo):
    """Local commits mean this cannot be a fast-forward, so it is not attempted.

    Refusing is the whole design. The alternatives - a merge commit, a rebase, an
    autostash - can each lose a local sweep, and an hour of prices cannot be
    observed again.
    """
    write(repo / "notes.md", "mine\n")
    commit(repo, "a commit the branch does not have")
    before = head(repo)

    found = state(repo)
    assert found["can_fast_forward"] is False
    assert "1 commit(s) the branch does not" in found["blocked_by"]

    result = branch_sync.pull(repo / "data", [TRIP])
    assert result["synced"] is False
    assert "Nothing has been changed" in result["reason"]
    assert head(repo) == before


def test_a_modified_file_the_pull_would_overwrite_is_refused_by_git_verbatim(repo):
    """Git decides this, and git's own sentence is what gets shown.

    It names the file. "Could not sync" would not, and this app keeps replacing
    exactly that kind of summary.
    """
    # The branch amends README.md; so does this checkout, without committing.
    write(repo / "README.md", "edited here and never committed\n")
    before = head(repo)

    result = branch_sync.pull(repo / "data", [TRIP])

    assert result["synced"] is False
    assert "README.md" in result["reason"]
    assert "nothing has been changed" in result["reason"].lower()
    assert head(repo) == before
    assert (repo / "README.md").read_text(encoding="utf-8") == (
        "edited here and never committed\n"
    )


def test_an_edit_git_would_not_touch_does_not_block_the_pull(repo):
    """A dirty tree is not a reason to refuse, and treating it as one was a bug.

    The first version of this gated on `git status` being empty at all. The cloud
    commits `data/` and the person here edits code, so that would have refused
    nearly every morning for a collision that does not exist - turning the fix
    into a second thing to work around.
    """
    write(repo / "src" / "app.py", "SPEED = 2  # mid-edit\n")
    assert state(repo)["dirty"] is True

    result = branch_sync.pull(repo / "data", [TRIP])

    assert result["synced"] is True, result["reason"]
    assert result["gained"][TRIP] == sorted(CLOUD_SWEEPS, reverse=True)
    # And the edit in progress survives it untouched.
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "SPEED = 2  # mid-edit\n"


# ------------------------------------------- when it cannot say, it says so


def test_a_checkout_without_a_remote_says_so_rather_than_nothing_missing(tmp_path, monkeypatch):
    """"Cannot tell" and "nothing missing" must not render the same.

    An empty answer here would read as a complete picker, which is the exact
    misreading this module was written to end.
    """
    lone = tmp_path / "lone"
    lone.mkdir()
    run(lone, "init", "-b", "main")
    write(lone / "README.md", "no remote\n")
    commit(lone, "start")
    monkeypatch.setattr(branch_sync, "_run", lambda *args: _real(lone, *args))

    found = branch_sync.state(lone / "data", [TRIP])

    assert found["known"] is False
    assert "no remote" in found["reason"]
    assert found["missing_count"] == 0


def test_without_git_at_all_it_says_so(tmp_path):
    """The suite-wide refusal stands in for a machine with no git."""
    found = branch_sync.state(tmp_path / "data", [TRIP])

    assert found["known"] is False
    assert found["reason"]
    assert found["can_fast_forward"] is False


def test_a_data_dir_outside_the_checkout_has_no_branch_counterpart(repo, tmp_path):
    """A `DATA_DIR` elsewhere is answerable, and the answer is "none".

    Not a failure: there genuinely are no cloud runs under a directory the branch
    does not contain.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    found = branch_sync.state(outside, [TRIP])

    assert found["known"] is True
    assert found["missing_count"] == 0
