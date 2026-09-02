"""Putting the trip you just saved onto the branch the cloud actually sweeps.

The app has been able to *see* this gap since `_cloud_state` was written, and
could do nothing about it. Saving a trip writes `scenarios/<id>.json` in the
working tree; the night sweep and every cloud run plan from the copy committed
to `CLOUD_REF`. Between the two sits a commit and a push that only a person at a
terminal could make - and on a checkout that is usually on a feature branch,
"push" does not even reach the branch being swept. So every new trip ended the
same way: a refusal with a correct sentence under it, and an errand.

It is the same shape as `branch_sync.take`. The results were on the branch and
unreachable from the app; here the trip is on the machine and unreachable from
the branch. Both are one narrow, named path across a boundary the app can
otherwise only describe.

Narrow, and the narrowness is the safety story. This writes **one file per
call**, always `scenarios/<id>.json`, through the GitHub contents API rather
than through the checkout. That is deliberate:

- it never touches HEAD, the index, the working tree, or any local commit, so
  nothing here can lose a sweep - the rule `branch_sync` is built around;
- it does not care which branch is checked out or how dirty it is, which is the
  case that made this necessary rather than merely convenient;
- it is scoped to one path, so a race with the probe committing to `main` is not
  a race at all: the API only refuses if that *same file* moved underneath us,
  and then it refuses with a sentence rather than overwriting.

What it will not do: it will not push code, results, or anything else; it will
not force; and it will not invent a trip on the branch that is not on this disk.
When the file is gone locally, the same call removes it from the branch - a
deleted trip that carries on being swept every night is the same lie the other
direction.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
from pathlib import Path

from . import branch_sync, cloud_runs

# github.com/<owner>/<repo>, in either the https or the ssh spelling.
REPO_URL = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

# Where the branch keeps the definition of what a trip file may contain.
SCHEMA = "src/scenario.py"


def _target() -> tuple[str, str, str] | None:
    """(remote, branch, owner/repo) for `CLOUD_REF`, or None if it cannot be told.

    `origin/main` is two facts spelled as one string, and both are needed: the
    branch to commit to, and the repository to commit it in. The repository is
    read from the remote's URL rather than from `gh repo view`, which would be a
    second subprocess to learn something the checkout already knows.
    """
    remote = branch_sync.remote()
    if remote is None:
        return None
    ref = branch_sync.CLOUD_REF
    branch = ref[len(remote) + 1:] if ref.startswith(f"{remote}/") else ref
    if not branch:
        return None
    found = REPO_URL.search((branch_sync.git("remote", "get-url", remote) or "").strip())
    if found is None:
        return None
    return remote, branch, f"{found['owner']}/{found['repo']}"


def _repo_path(local: Path) -> str | None:
    """`local` as the branch names it, or None when it is not in this checkout.

    Same answer, and the same reason, as `branch_sync._branch_path`: a
    `SCENARIO_DIR` pointed outside the repository has no counterpart on the
    branch, and that is a fact to report rather than a failure to hide.
    """
    top = branch_sync.git("rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        return local.resolve().relative_to(Path(top.strip()).resolve()).as_posix()
    except ValueError:
        return None


def _blob_sha(content: bytes) -> str:
    """The git object id `content` would be stored under.

    Computed here rather than shelled out to `git hash-object`, because it is
    four lines of a documented format and the alternative is a subprocess per
    save. What it is for is the comparison below: the contents API wants the sha
    of the file it is replacing, and the same number says whether there is
    anything to replace at all.
    """
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def _content(local: Path) -> bytes:
    """The trip file as the branch would store it: UTF-8, LF, whatever the disk says.

    `core.autocrlf` is on for this checkout, so `save_scenario` writes CRLF and
    git stores LF. Publishing the bytes off the disk would commit the CRLF copy,
    which is a real diff on every save and, worse, a blob sha that never matches
    the branch's - so nothing would ever look "already published" and each save
    would flip the file back and forth.
    """
    return local.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


def _branch_schema() -> dict[str, set[str]] | None:
    """The field names the branch's own `Scenario` and `Stop` declare.

    Read out of `CLOUD_REF:src/scenario.py` with `ast`, because the question is
    about *that* code and this process is running different code. Nothing is
    executed: the file is parsed and the annotated class attributes are read,
    which is exactly what `@dataclass` turns into `__dataclass_fields__` - the
    set `from_dict` subtracts the payload from.

    None when it cannot be told, which the caller treats as a refusal rather
    than as permission. A publish that cannot be checked is the one that took
    the branch down.
    """
    source = branch_sync.git("show", f"{branch_sync.CLOUD_REF}:{SCHEMA}")
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found = {
        node.name: {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in ("Scenario", "Stop")
    }
    if not found.get("Scenario") or not found.get("Stop"):
        return None
    return found


def _unreadable_on_branch(content: bytes) -> str:
    """Why the branch's code could not read this trip, or "" when it can.

    The failure this exists for, in full, from 2 Sep 2026. The app writes trip
    files in the schema of the code it is running; the branch reads them with
    the code *it* is running, and on a checkout that is several commits ahead
    those are not the same schema. `jak-filipiny.json` went up carrying three
    fields the branch had never heard of, and `Scenario.from_dict` refuses
    unknown fields outright - so the plan job died in a traceback.

    What made it expensive rather than annoying is that the sweep loads the
    whole directory at once. One unreadable trip is every trip: the nightly
    sweep of a perfectly good trip nobody had touched would have swept nothing
    that night, and a sweep that swept nothing looks exactly like a quiet day.

    Both levels are checked, and they fail differently. An unknown field on the
    trip *raises* there. An unknown field on a stop is silently dropped, which
    is worse in its own way: the branch would sweep a trip that is not the one
    on screen, and `_cloud_state` would compare the two files, find them
    identical, and say everything agrees.

    Not checked: fields the branch has and this app does not. Publishing an
    older shape to a newer branch is the reverse drift, `from_dict` fills most
    of it from defaults, and guessing which of those defaults it supplies would
    mean modelling the branch's loader rather than reading its fields.
    """
    known = _branch_schema()
    if known is None:
        return (
            f"This app cannot read the trip format {branch_sync.CLOUD_REF} uses, so it "
            "cannot tell whether the cloud would be able to load this trip at all. "
            "Publishing it blind is how one trip stopped every sweep on 2 Sep."
        )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "This trip file is not readable JSON, so nothing should carry it further."

    unknown = set(payload) - known["Scenario"]
    for stop in payload.get("stops") or []:
        if isinstance(stop, dict):
            unknown |= set(stop) - known["Stop"]
    if not unknown:
        return ""
    return (
        f"{branch_sync.CLOUD_REF} runs an older copy of this app: its trip format does "
        f"not know {', '.join(sorted(unknown))}. Publishing this trip would stop the "
        f"cloud sweeping *any* trip, because the sweep loads them all at once. Merge "
        f"this app's code into {branch_sync.CLOUD_REF} first."
    )


def _commit_url(raw: str) -> str:
    try:
        return json.loads(raw).get("commit", {}).get("html_url", "") or ""
    except (json.JSONDecodeError, AttributeError):
        return ""


def _answer(**fields) -> dict:
    return {
        "published": False,
        "removed": False,
        "already_current": False,
        "reason": "",
        "path": "",
        "url": "",
        "ref": branch_sync.CLOUD_REF,
        **fields,
    }


def publish_trip(scenario_dir: Path, scenario_id: str) -> dict:
    """Commit this trip to the branch the cloud sweeps, and say what happened.

    Never raises, like everything else that stands between this app and a remote:
    no git, no remote, no `gh`, or a GitHub that refuses are all *cannot say*,
    carried back as a sentence for the page to print. The caller decides whether
    that is worth interrupting someone over; on a save it is not, and on a run
    that is about to sweep the wrong trip it is.
    """
    local = Path(scenario_dir) / f"{scenario_id}.json"
    target = _target()
    if target is None:
        return _answer(
            reason="This checkout has no GitHub remote the app can recognise, so "
            "there is no branch to publish the trip to."
        )
    _remote, branch, repo = target

    path = _repo_path(local)
    if path is None:
        return _answer(
            reason="This trip is not saved inside the git checkout, so it has no "
            "place on the branch the cloud reads."
        )

    # The sha the contents API replaces, read from the last fetch. Refreshed
    # first for the same reason `run-cloud` fetches before it compares trips: a
    # decision taken against a fortnight-old ref is not a decision.
    #
    # `fetched_recently` is the module's own answer to "fresh enough", and every
    # panel already trusts it; borrowing it here keeps a save from spending two
    # network round trips when the page fetched a moment ago. A sha that goes
    # stale inside that minute is not a silent wrong answer either - GitHub
    # refuses the write and names the mismatch.
    if not branch_sync.fetched_recently():
        branch_sync.fetch()
    on_branch = branch_sync.git(
        "rev-parse", "--verify", "--quiet", f"{branch_sync.CLOUD_REF}:{path}"
    )
    sha = (on_branch or "").strip()

    if not local.exists():
        if not sha:
            return _answer(already_current=True, path=path)
        return _remove(repo, branch, path, sha, scenario_id)
    return _put(repo, branch, path, sha, scenario_id, _content(local))


def _put(repo: str, branch: str, path: str, sha: str, scenario_id: str,
         content: bytes) -> dict:
    if sha and sha == _blob_sha(content):
        # Already the trip the cloud would run. Worth answering plainly rather
        # than committing an empty change: a save that alters nothing about the
        # trip - a rename, a re-open - must not put a commit on the branch.
        return _answer(already_current=True, path=path)

    # Checked after "nothing to do" and before anything is sent. A trip already
    # on the branch byte for byte is one the branch is evidently living with,
    # and refusing to leave it alone would be a refusal to do nothing.
    refusal = _unreadable_on_branch(content)
    if refusal:
        return _answer(reason=refusal, path=path)

    command = [
        "api", f"repos/{repo}/contents/{path}",
        "-X", "PUT",
        "-f", f"message=chore(trip): {scenario_id} as saved in the app",
        "-f", f"content={base64.b64encode(content).decode('ascii')}",
        "-f", f"branch={branch}",
    ]
    # Present for an edit, absent for a new trip. Sending an empty one is how the
    # API is told "this file does not exist yet", and sending a stale one is how
    # it is told to refuse - which is exactly the protection wanted here.
    if sha:
        command += ["-f", f"sha={sha}"]
    return _send(command, path, published=True)


def _remove(repo: str, branch: str, path: str, sha: str, scenario_id: str) -> dict:
    command = [
        "api", f"repos/{repo}/contents/{path}",
        "-X", "DELETE",
        "-f", f"message=chore(trip): stop sweeping {scenario_id}",
        "-f", f"sha={sha}",
        "-f", f"branch={branch}",
    ]
    return _send(command, path, removed=True)


def _send(command: list[str], path: str, **outcome) -> dict:
    try:
        raw = cloud_runs.gh(*command, timeout=60)
    except cloud_runs.CloudError as exc:
        # gh's own sentence, kept whole. "HTTP 409: does not match" names a file
        # that moved on the branch, and "HTTP 403" names a rule about who may
        # write to it; a summary of either is a summary of the only part worth
        # reading.
        return _answer(reason=f"The trip was not published. {exc}", path=path)

    # The branch has moved and every panel in the app reads the last fetch, so
    # without this the page would go on saying the trip is not on the branch
    # until something else happened to fetch.
    branch_sync.fetch()
    return _answer(path=path, url=_commit_url(raw), **outcome)
