"""Putting a trip on the branch the cloud sweeps.

Driven against a real git repository in `tmp_path`, for the same reason
`test_branch_sync` is: what is being tested is mostly what git says about a blob
that is on the branch, absent from it, or the same on both sides - and a fake of
git would only ever agree with whatever this module already assumed.

`gh` is the one thing that is faked, at `cloud_runs.gh`, the single boundary the
suite already closes. Nothing here may reach GitHub, and every test asserts on
the request that would have been sent rather than on a reply.

The belief worth checking hardest is the one that is invisible on Linux: this
checkout has `core.autocrlf` on, so the trip file is CRLF on disk and LF in the
tree. Publishing the bytes off the disk would look right, commit a whole-file
diff on every save, and never once match what the branch already had.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from src.web import branch_sync, cloud_runs, publish

# Captured before any test stubs it, for the one test that is about `_target`
# itself rather than about a caller of it - the same trick `test_cloud_runs`
# uses to get at the real `gh`.
REAL_TARGET = publish._target

TRIP = "japan-philippines"
PATH = f"scenarios/{TRIP}.json"
SCHEMA = publish.SCHEMA

# The trip format the branch is running, as far as these tests are concerned.
# Only the field *names* are read - by `ast`, never executed - so this is the
# shortest thing that carries them.
CURRENT_SCHEMA = """
@dataclass
class Stop:
    airports: list[str]
    stay_days: tuple[int, int]
    label: str = ""
    arrive_via: str | None = None


@dataclass
class Scenario:
    id: str
    name: str = ""
    stops: list[Stop] = ()
    enabled: bool = True
"""

# The same file one release older: no `arrive_via` on a stop. What a checkout
# ahead of the branch is publishing into.
OLDER_SCHEMA = CURRENT_SCHEMA.replace('    arrive_via: str | None = None\n', "")

# Two versions of one trip, as the app writes them: LF, trailing newline.
ON_BRANCH = '{\n  "id": "japan-philippines",\n  "stops": ["NRT", "MNL"]\n}\n'
NARROWED = '{\n  "id": "japan-philippines",\n  "stops": ["NRT", "CEB"]\n}\n'


def run(cwd: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"


def _real(cwd: Path, *args: str) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def commit(repo: Path, message: str) -> None:
    run(repo, "add", "-A")
    run(
        repo,
        "-c", "user.email=t@example.test",
        "-c", "user.name=test",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )


def write(path: Path, text: str, newline: str = "\n") -> None:
    """Write a trip file, spelling the line endings out.

    `newline="\\r\\n"` is what `save_scenario` really produces on this machine,
    and it is the case the module exists to get right.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone whose `origin/main` carries one trip, and the calls gh would get.

    `_target` is stubbed rather than the remote URL faked: the clone has to fetch
    from a path on disk, and a remote spelled `https://github.com/...` so that
    the real `_target` could parse it would send `fetch` to the network. What
    that stub stands in for is tested on its own below.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    run(origin, "init", "-b", "main")
    write(origin / PATH, ON_BRANCH)
    # The branch carries the code that reads its trips, and since 2 Sep the
    # publish is checked against it. An origin without one is not a repository
    # this app would ever publish to, and every test here would be testing the
    # refusal rather than the thing it names.
    write(origin / SCHEMA, CURRENT_SCHEMA)
    commit(origin, "the trip as the cloud has it, and the code that reads it")

    clone = tmp_path / "clone"
    run(tmp_path, "clone", str(origin), str(clone))
    monkeypatch.setattr(branch_sync, "_run", lambda *args: _real(clone, *args))
    monkeypatch.setattr(publish, "_target", lambda: ("origin", "main", "test/tickets"))
    return clone


@pytest.fixture
def sent(monkeypatch):
    """Every `gh` call the module makes, as the list of its arguments."""
    calls: list[tuple[str, ...]] = []

    def fake(*args: str, timeout: int = 30) -> str:
        calls.append(args)
        return '{"commit": {"html_url": "https://github.com/test/tickets/commit/abc"}}'

    monkeypatch.setattr(cloud_runs, "gh", fake)
    return calls


def field(call: tuple[str, ...], name: str) -> str | None:
    """The value of one `-f key=value` in a gh invocation, or None if absent."""
    for argument in call:
        if argument.startswith(f"{name}="):
            return argument[len(name) + 1:]
    return None


def content_of(call: tuple[str, ...]) -> bytes:
    return base64.b64decode(field(call, "content"))


def blob_on_branch(repo: Path) -> str:
    return _real(repo, "rev-parse", f"origin/main:{PATH}")[1].strip()


# ------------------------------------------------------------------ writing


def test_a_trip_the_branch_has_never_seen_is_sent_without_a_sha(repo, sent):
    """A create and a replace are the same call with one field between them.

    Sending an empty `sha` is how the API is told the file already exists, so a
    new trip has to leave it off entirely rather than send "".
    """
    write(repo / "scenarios" / "hokkaido.json", NARROWED, newline="\r\n")
    answer = publish.publish_trip(repo / "scenarios", "hokkaido")

    assert answer["published"] is True
    assert answer["reason"] == ""
    assert len(sent) == 1
    call = sent[0]
    assert call[:4] == ("api", "repos/test/tickets/contents/scenarios/hokkaido.json",
                        "-X", "PUT")
    assert field(call, "branch") == "main"
    assert field(call, "sha") is None


def test_the_bytes_sent_are_the_ones_git_would_store(repo, sent):
    """The file on disk is CRLF and the branch's copy is LF.

    `save_scenario` writes through Python's text layer, so on this machine every
    trip file has CRLF endings while `core.autocrlf` stores LF. Publishing the
    disk's bytes would commit a diff on every single line of every save.
    """
    write(repo / PATH, NARROWED, newline="\r\n")
    assert b"\r\n" in (repo / PATH).read_bytes()

    publish.publish_trip(repo / "scenarios", TRIP)

    assert content_of(sent[0]) == NARROWED.encode("utf-8")
    assert b"\r" not in content_of(sent[0])


def test_a_trip_the_branch_already_has_is_replaced_with_its_own_sha(repo, sent):
    """The sha is the branch's blob, which is what makes a race refuse.

    Sending anything else - or nothing - would let this overwrite a version of
    the trip it never read.
    """
    write(repo / PATH, NARROWED, newline="\r\n")
    publish.publish_trip(repo / "scenarios", TRIP)

    assert field(sent[0], "sha") == blob_on_branch(repo)


def test_a_trip_the_branch_already_matches_is_not_committed_again(repo, sent):
    """Saving a trip that changed nothing must not put a commit on the branch.

    This is the CRLF trap the other way round: the disk's bytes never equal the
    branch's, so a module comparing them would publish on every save, for ever,
    and each commit would be an empty diff dressed as a change.
    """
    write(repo / PATH, ON_BRANCH, newline="\r\n")
    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["already_current"] is True
    assert answer["published"] is False
    assert sent == []


# ------------------------------------------- what the branch's code can read
#
# The 2 Sep failure, and the reason this section exists. The app writes trip
# files in the schema of the code it is running; the branch reads them with the
# code *it* is running. A checkout several commits ahead publishes a trip the
# branch cannot load - and because the sweep loads the whole directory at once,
# that is not one trip lost but every trip, on every scheduled run, until
# somebody looks at Actions.


@pytest.fixture
def branch_runs(repo, tmp_path):
    """Change the code the branch runs, and let the clone see that it changed.

    The fetch matters: everything here reads `origin/main` out of the clone, and
    a schema committed to the origin that nobody fetched is a schema the module
    under test cannot see.
    """
    def set_schema(source: str) -> None:
        origin = tmp_path / "origin"
        write(origin / SCHEMA, source)
        commit(origin, "the code the branch runs, one release older")
        branch_sync.fetch()

    return set_schema


def test_a_trip_the_branch_could_not_load_is_refused_before_it_is_sent(repo, sent):
    """One unreadable trip stops every trip, so this cannot be a warning.

    `Scenario.from_dict` refuses unknown fields outright, and the sweep's plan
    step loads them all in one glob - so publishing a trip with a field the
    branch has never heard of takes down the nightly sweep of trips nobody
    touched.
    """
    write(repo / PATH, '{"id": "x", "stops": [], "preferences": [], "probe_extra": {}}')

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is False
    assert "preferences" in answer["reason"]
    assert "probe_extra" in answer["reason"]
    assert sent == [], "nothing may be sent: the branch is what is being protected"


def test_a_trip_the_branch_can_read_is_published_as_before(repo, sent):
    """The guard must not refuse the ordinary case, which is every other save."""
    write(repo / PATH, '{"id": "x", "name": "X", "stops": [], "enabled": true}')

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is True
    assert answer["reason"] == ""
    assert len(sent) == 1


def test_a_field_the_branch_would_drop_from_a_stop_is_refused_too(repo, sent, branch_runs):
    """A stop field the branch does not know is not an error there - it is
    silently dropped, and the cloud then sweeps a trip that is not the one on
    screen. Worse than the loud version: the two files are byte-identical, so
    `_cloud_state` compares them and reports that everything agrees."""
    branch_runs(OLDER_SCHEMA)
    write(repo / PATH,
          '{"id": "x", "name": "X", "stops": [{"airports": ["NRT"], "stay_days": [9, 11],'
          ' "arrive_via": "KIX"}]}')

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is False
    assert "arrive_via" in answer["reason"]
    assert sent == []


def test_a_branch_whose_schema_cannot_be_read_is_not_published_to(repo, sent, branch_runs):
    """"Cannot check" is not "fine". Publishing blind is the thing that broke."""
    branch_runs("x = 1  # no Scenario class here at all")
    write(repo / PATH, NARROWED, newline="\r\n")

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is False
    assert "cannot read the trip format" in answer["reason"]
    assert sent == []


# ------------------------------------------------------------------ removing


def test_a_deleted_trip_is_taken_off_the_branch(repo, sent):
    """Deleting a trip here has to stop the night sweep planning it.

    The absence is the instruction: `publish_trip` is given a name whose file is
    gone, and the branch still has one.
    """
    (repo / PATH).unlink()
    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["removed"] is True
    call = sent[0]
    assert call[:4] == ("api", f"repos/test/tickets/contents/{PATH}", "-X", "DELETE")
    assert field(call, "sha") == blob_on_branch(repo)


def test_a_trip_neither_side_has_is_not_an_error(repo, sent):
    """Deleting a trip that was never published is already the wanted state."""
    answer = publish.publish_trip(repo / "scenarios", "never-existed")

    assert answer["already_current"] is True
    assert answer["reason"] == ""
    assert sent == []


# ------------------------------------------------------- when it cannot say


def test_gh_refusing_is_carried_back_whole_and_nothing_claims_to_have_landed(repo, monkeypatch):
    """GitHub's own sentence is the part worth reading.

    "409: does not match" names a file that moved on the branch and "403" names
    a rule about who may write to it; a summary of either is a summary of the
    only useful half.
    """
    def refuse(*args, **kwargs):
        raise cloud_runs.CloudError("gh failed: HTTP 409: sha does not match")

    monkeypatch.setattr(cloud_runs, "gh", refuse)
    write(repo / PATH, NARROWED, newline="\r\n")

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is False
    assert "409" in answer["reason"]
    assert "does not match" in answer["reason"]


def test_a_checkout_with_no_github_remote_says_so_rather_than_trying(repo, sent, monkeypatch):
    """The real `_target` against a clone of a directory: there is no repo to write to.

    It must answer, not raise, and it must not reach for `gh` on the way.
    """
    # The real one, put back over the fixture's stub. Not `monkeypatch.undo()`:
    # that would also lift `no_real_gh` and `no_real_git`, and a test that
    # accidentally reaches a real remote is the thing they exist to stop.
    monkeypatch.setattr(publish, "_target", REAL_TARGET)

    answer = publish.publish_trip(repo / "scenarios", TRIP)

    assert answer["published"] is False
    assert "remote" in answer["reason"]
    assert sent == []


def test_a_trip_outside_the_checkout_has_nowhere_on_the_branch_to_go(repo, sent, tmp_path):
    """`SCENARIO_DIR` can point anywhere; the branch only names paths in the repo."""
    outside = tmp_path / "elsewhere"
    write(outside / f"{TRIP}.json", NARROWED)

    answer = publish.publish_trip(outside, TRIP)

    assert answer["published"] is False
    assert "checkout" in answer["reason"]
    assert sent == []


# --------------------------------------------------------- reading the remote


@pytest.mark.parametrize("url", [
    "https://github.com/Band1t0o/tickets-bot.git",
    "https://github.com/Band1t0o/tickets-bot",
    "git@github.com:Band1t0o/tickets-bot.git",
    "ssh://git@github.com/Band1t0o/tickets-bot.git",
])
def test_the_repository_is_read_out_of_the_remote_url(url, monkeypatch):
    """Both spellings of a GitHub remote name the same repository.

    Read from the remote rather than asked of `gh repo view`, so this is the
    place the two spellings have to be understood.
    """
    monkeypatch.setattr(branch_sync, "remote", lambda: "origin")
    monkeypatch.setattr(branch_sync, "git", lambda *args: url)

    assert publish._target() == ("origin", "main", "Band1t0o/tickets-bot")


def test_a_remote_that_is_not_github_is_not_guessed_at(monkeypatch):
    monkeypatch.setattr(branch_sync, "remote", lambda: "origin")
    monkeypatch.setattr(branch_sync, "git", lambda *args: "/srv/mirrors/tickets.git")

    assert publish._target() is None
