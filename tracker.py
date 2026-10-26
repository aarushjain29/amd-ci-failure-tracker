"""Open or update a GitHub issue for every :amd: job that fails on vLLM main.

Scans recent builds of https://buildkite.com/vllm/ci (public, no token), keeps
main-branch builds, and for each :amd: job whose final attempt failed or timed
out, adds a comment to the open issue "[AMD CI] <job name>" (creating it if
needed). Each failure is recorded once, so the script is safe to rerun.

Environment:
  GITHUB_TOKEN       token with issues:write on GITHUB_REPOSITORY
  GITHUB_REPOSITORY  owner/repo that receives the issues
  LOOKBACK_HOURS     how far back to scan builds (default 6)
  BRANCHES           comma-separated Buildkite branches (default "main")
  BUILD_HINT         a recent build number to start the search from
  DRY_RUN            "1" prints what would be filed instead of filing it
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

BK = "https://buildkite.com/vllm/ci/builds"
BK_JOB_LOG = "https://buildkite.com/organizations/vllm/pipelines/ci/builds/{n}/jobs/{jid}/download.txt"
GH = "https://api.github.com"
LABEL = "amd-ci-failure"
TITLE = "[AMD CI] {name}"
MARKER = "<!-- amd-ci-failure build={n} job={jid} -->"

LOOKBACK = timedelta(hours=float(os.environ.get("LOOKBACK_HOURS", "6")))
BRANCHES = set(os.environ.get("BRANCHES", "main").split(","))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _get(url, accept="application/json", timeout=60):
    req = urllib.request.Request(url, headers={"Accept": accept})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except (OSError, TimeoutError):
            continue
    return None


def bk_json(url):
    body = _get(url)
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return None


def build(n):
    return bk_json(f"{BK}/{n}.json")


def jobs(n):
    out, page = [], 1
    while True:
        d = bk_json(f"{BK}/{n}/data/jobs?page={page}")
        if not d:
            return out
        out += d["records"]
        if not d.get("has_next_page"):
            return out
        page += 1


def newest_build(hint):
    lo = hint
    while not build(lo):
        lo -= 500
    step = 64
    while build(lo + step):
        lo += step
        step *= 2
    hi = lo + step
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if build(mid) else (lo, mid)
    return lo


def recent_builds(newest):
    cutoff = datetime.now(UTC) - LOOKBACK
    n = newest
    while True:
        b = build(n)
        n -= 1
        if not b:
            continue
        if datetime.fromisoformat(b["created_at"]) < cutoff:
            return
        if b.get("branch_name") in BRANCHES:
            yield b


def failed_amd_jobs(b):
    for j in jobs(b["number"]):
        name = j.get("name") or ""
        if ":amd:" not in name or j.get("retried_in_job_uuid"):
            continue
        if j.get("state") == "timed_out" or (
            j.get("state") == "finished" and j.get("passed") is False
        ):
            yield j


def failure_summary(n, j):
    raw = _get(BK_JOB_LOG.format(n=n, jid=j["id"]), accept="text/plain", timeout=120)
    text = (raw or b"").decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-9;]*m|\x1b_bk;t=\d+\x07|_bk;t=\d+|\x07|\r", "", text)
    lines = text.splitlines()
    end = next(
        (
            i
            for i, l in enumerate(lines)
            if re.search(
                r"Received cancellation signal|^user command error|buildkite-agent artifact upload",
                l,
            )
        ),
        len(lines) - 1,
    )
    lines = lines[: end + 1]

    failed, current = [], None
    for line in lines:
        # pytest -s prints "path::test <test output...>" then PASSED/FAILED on a later line.
        if m := re.match(r"^(\S+\.py::\S+)", line):
            current = m.group(1)
        if m := re.match(r"^(FAILED|ERROR) (\S+::\S+)(.*)$", line):
            entry = m.group(2) + m.group(3)[:200]
        elif current and re.search(r"(FAILED|ERROR)\s*$", line):
            entry = current
        else:
            if current and re.search(r"(PASSED|SKIPPED|XFAIL|XPASS)\s*$", line):
                current = None
            continue
        if not any(f.split(" ")[0] == entry.split(" ")[0] for f in failed):
            failed.append(entry)
        current = None
    if current and j.get("state") == "timed_out":
        failed.append(f"{current} (running when the step timed out)")

    noise = re.compile(r"queue-intercept|Extension modules:|^\s*$")
    tail = [l[:300] for l in lines if not noise.search(l)][-25:]
    return failed[:30], tail


def comment_body(b, j, failed, tail):
    n, jid = b["number"], j["id"]
    state = (
        "timed out"
        if j.get("state") == "timed_out"
        else f"failed (exit {j.get('exit_status')})"
    )
    lines = [
        MARKER.format(n=n, jid=jid),
        (
            f"**{state}** in [build {n}]({BK}/{n}/list?jid={jid}&tab=output)"
            f" on `{b.get('branch_name')}` @ `{(b.get('commit_id') or '')[:10]}`"
            f" ({b.get('created_at', '')[:16].replace('T', ' ')} UTC)"
        ),
        "",
        f"Build: {(b.get('message') or '').splitlines()[0][:120] if b.get('message') else ''}",
    ]
    if j.get("soft_failed"):
        lines.append("Soft-failed (optional step).")
    if failed:
        lines += ["", "Failed tests:", "```", *failed, "```"]
    else:
        lines += [
            "",
            "No pytest FAILED/ERROR lines found. Last log lines:",
            "```",
            *tail,
            "```",
        ]
    return "\n".join(lines)


def gh(method, path, data=None, params=None):
    url = f"{GH}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def gh_all(path, params):
    out, page = [], 1
    while True:
        batch = gh("GET", path, params={**params, "per_page": 100, "page": page})
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def ensure_label():
    try:
        gh(
            "POST",
            f"/repos/{REPO}/labels",
            {
                "name": LABEL,
                "color": "d73a4a",
                "description": "AMD job failing on vLLM main",
            },
        )
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise


def main():
    if not DRY_RUN and not (REPO and TOKEN):
        sys.exit("GITHUB_REPOSITORY and GITHUB_TOKEN are required (or set DRY_RUN=1)")

    if DRY_RUN:
        issues = {}
    else:
        ensure_label()
        issues = {
            i["title"]: i
            for i in gh_all(f"/repos/{REPO}/issues", {"labels": LABEL, "state": "open"})
            if "pull_request" not in i
        }
    seen = {}

    def recorded(issue):
        if issue["number"] not in seen:
            bodies = [issue.get("body") or ""] + [
                c["body"]
                for c in gh_all(f"/repos/{REPO}/issues/{issue['number']}/comments", {})
            ]
            seen[issue["number"]] = "\n".join(bodies)
        return seen[issue["number"]]

    newest = newest_build(int(os.environ.get("BUILD_HINT", "97000")))
    builds = list(recent_builds(newest))
    print(
        f"newest build {newest}; {len(builds)} builds on {sorted(BRANCHES)} in the last {LOOKBACK}"
    )

    with ThreadPoolExecutor(8) as ex:
        failures = [
            (b, j)
            for b, js in zip(builds, ex.map(lambda b: list(failed_amd_jobs(b)), builds))
            for j in js
        ]
    print(f"{len(failures)} failed :amd: jobs")

    for b, j in sorted(failures, key=lambda bj: bj[0]["number"]):
        title = TITLE.format(name=j["name"].replace(":amd: ", "").strip())
        marker = MARKER.format(n=b["number"], jid=j["id"])
        issue = issues.get(title)
        if issue and marker in recorded(issue):
            continue
        body = comment_body(b, j, *failure_summary(b["number"], j))
        if DRY_RUN:
            print(f"\n=== {'comment on' if issue else 'new issue'}: {title}\n{body}")
            if not issue:
                issue = issues[title] = {"number": -len(issues) - 1}
                seen[issue["number"]] = ""
            seen[issue["number"]] += "\n" + body
            continue
        if issue:
            gh(
                "POST",
                f"/repos/{REPO}/issues/{issue['number']}/comments",
                {"body": body},
            )
            seen[issue["number"]] += "\n" + body
            print(f"commented on #{issue['number']}: {title} (build {b['number']})")
        else:
            issue = gh(
                "POST",
                f"/repos/{REPO}/issues",
                {"title": title, "body": body, "labels": [LABEL]},
            )
            issues[title] = issue
            seen[issue["number"]] = body
            print(f"opened #{issue['number']}: {title} (build {b['number']})")


if __name__ == "__main__":
    main()
