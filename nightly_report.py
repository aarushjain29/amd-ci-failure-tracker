"""Post the AMD results of the vLLM nightly builds to a Microsoft Teams channel.

For the newest day on which both the upstream CI nightly (vllm/ci) and the AMD
CI nightly (vllm/amd-ci) have finished, posts one message listing the build
links, how many AMD jobs failed, and which ones (linked to their tracker issue
when one is open). Each day is posted once: posted days are recorded as
comments on a state issue in GITHUB_REPOSITORY.

Environment:
  TEAMS_WEBHOOK_URL  Teams Workflows webhook ("Send webhook alerts to a channel")
  GITHUB_TOKEN       token with issues:write on GITHUB_REPOSITORY
  GITHUB_REPOSITORY  owner/repo of this tracker
  DRY_RUN            "1" prints the message instead of posting it
"""

import json
import os
import sys
import urllib.request

import tracker as t

PIPELINES = [
    ("Upstream CI", "https://buildkite.com/vllm/ci/builds", ":amd:", 97000),
    ("AMD CI", "https://buildkite.com/vllm/amd-ci/builds", "", 15300),
]
FINISHED = {"passed", "failed", "canceled", "blocked"}
STATE_TITLE = "[Teams] nightly report log"
STATE_LABEL = "nightly-report-log"
MARKER = "<!-- nightly-report day={day} -->"
WEBHOOK = os.environ.get("TEAMS_WEBHOOK_URL", "")


def build(base, n):
    return t.bk_json(f"{base}/{n}.json")


def jobs(base, n):
    out, page = [], 1
    while True:
        d = t.bk_json(f"{base}/{n}/data/jobs?page={page}")
        if not d:
            return out
        out += d["records"]
        if not d.get("has_next_page"):
            return out
        page += 1


def newest(base, hint):
    lo = hint
    while not build(base, lo):
        lo -= 200
    step = 16
    while build(base, lo + step):
        lo += step
        step *= 2
    hi = lo + step
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if build(base, mid) else (lo, mid)
    return lo


def nightlies(base, hint, count=3, max_scan=600):
    """The newest `count` scheduled nightly builds on main, newest first."""
    found, n = [], newest(base, hint)
    for _ in range(max_scan):
        b = build(base, n)
        n -= 1
        if (
            b
            and b.get("branch_name") == "main"
            and b.get("source") == "schedule"
            and "nightly" in (b.get("message") or "").lower()
        ):
            found.append(b)
            if len(found) == count:
                break
    return found


def failed_jobs(base, b, name_filter):
    return [
        j
        for j in jobs(base, b["number"])
        if j.get("type") == "script"
        and name_filter in (j.get("name") or "")
        and not j.get("retried_in_job_uuid")
        and (
            j.get("state") == "timed_out"
            or (j.get("state") == "finished" and j.get("passed") is False)
        )
    ]


def tracker_issues():
    if not (t.REPO and t.TOKEN):
        return {}
    return {
        i["title"]: i["html_url"]
        for i in t.gh_all(
            f"/repos/{t.REPO}/issues", {"labels": t.LABEL, "state": "open"}
        )
        if "pull_request" not in i
    }


def card(sections):
    body = []
    for label, base, b, failed, issues in sections:
        n = b["number"]
        head = f"**{label}:** [{base.split('/')[-2]} #{n}]({base}/{n}/list), {len(failed)} failed"
        body.append(
            {"type": "TextBlock", "text": head, "wrap": True, "spacing": "Medium"}
        )
        for j in failed:
            name = j["name"].replace(":amd: ", "").strip()
            link = f"{base}/{n}/list?jid={j['id']}&tab=output"
            line = f"- [{name}]({link})" + (
                " (timed out)" if j.get("state") == "timed_out" else ""
            )
            if url := issues.get(t.TITLE.format(name=name)):
                line += f" · [tracker issue]({url})"
            body.append(
                {"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"}
            )
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


def state_issue():
    for i in t.gh_all(
        f"/repos/{t.REPO}/issues", {"labels": STATE_LABEL, "state": "all"}
    ):
        if i["title"] == STATE_TITLE:
            return i
    try:
        t.gh(
            "POST", f"/repos/{t.REPO}/labels", {"name": STATE_LABEL, "color": "ededed"}
        )
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
    return t.gh(
        "POST",
        f"/repos/{t.REPO}/issues",
        {
            "title": STATE_TITLE,
            "body": "Days already posted to Teams.",
            "labels": [STATE_LABEL],
        },
    )


def main():
    if not t.DRY_RUN and not (WEBHOOK and t.REPO and t.TOKEN):
        sys.exit(
            "TEAMS_WEBHOOK_URL, GITHUB_TOKEN and GITHUB_REPOSITORY are required (or set DRY_RUN=1)"
        )

    by_day = []
    for label, base, name_filter, hint in PIPELINES:
        by_day.append({b["created_at"][:10]: b for b in nightlies(base, hint)})
    days = sorted(set.intersection(*(set(d) for d in by_day)), reverse=True)
    day = next(
        (d for d in days if all(p[d].get("state") in FINISHED for p in by_day)), None
    )
    if not day:
        print("no day on which both nightlies have finished yet")
        return
    marker = MARKER.format(day=day)

    if not t.DRY_RUN:
        state = state_issue()
        posted = [
            c["body"]
            for c in t.gh_all(f"/repos/{t.REPO}/issues/{state['number']}/comments", {})
        ]
        if any(marker in c for c in posted):
            print(f"{day} already posted")
            return

    issues = tracker_issues()
    sections = []
    for (label, base, name_filter, _), builds in zip(PIPELINES, by_day):
        b = builds[day]
        sections.append((label, base, b, failed_jobs(base, b, name_filter), issues))
    message = card(sections)

    if t.DRY_RUN:
        print(f"day {day}")
        for block in message["attachments"][0]["content"]["body"]:
            print(block["text"])
        return

    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f"posted {day} to Teams: HTTP {r.status}")
    summary = ", ".join(
        f"{s[0]} #{s[2]['number']}: {len(s[3])} failed" for s in sections
    )
    t.gh(
        "POST",
        f"/repos/{t.REPO}/issues/{state['number']}/comments",
        {"body": f"{marker}\n{day}: {summary}"},
    )


if __name__ == "__main__":
    main()
