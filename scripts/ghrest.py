#!/usr/bin/env python3
"""GitHub over REST first; GraphQL only as a bonus (1.7.15).

`gh pr view`, `gh pr list` and `gh pr merge` spend the GraphQL quota (5000 points an hour). On Forge-QA run core the
read of about 160 pull requests each wake ran it to 0 and merges stalled. Everything here is the REST API through
`gh api`, which has its own quota, and the provider read is incremental:

  read      GET  /repos/:o/:r/pulls/N  + GET /commits/<head>/check-runs + GET /commits/<head>/status
  list      GET  /repos/:o/:r/pulls?state=all&sort=updated&direction=desc&per_page=100   (stops at the last cursor)
  merge     PUT  /repos/:o/:r/pulls/N/merge  merge_method=<squash|merge|rebase>  sha=<reviewed head> (the guard)
  retarget  PATCH /repos/:o/:r/pulls/N  base=<branch>
  cancel    POST /repos/:o/:r/actions/runs/<id>/cancel      (hung CI; never rerun)
  comment   POST /repos/:o/:r/issues/N/comments  body=...

Every call takes `runner` (provider._run by default), so tests replace the network with a function. A GraphQL
rate-limit error from any `gh` call is recognised by `rate_limited` and the caller retries here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

CURSOR_FILE = "pulls-cursor.json"
_RATE = re.compile(r"(api rate limit|rate limit exceeded|secondary rate limit|graphql.*rate|was submitted too quickly|"
                   r"abuse detection|HTTP 403.*rate|You have exceeded a secondary rate limit)", re.I)


def rate_limited(text: str) -> bool:
    return bool(_RATE.search(str(text or "")))


def _runner(runner: Optional[Callable]) -> Callable:
    if runner is not None:
        return runner
    import provider

    return provider._run


def slug_of(root: Path, url: str = "") -> str:
    match = re.search(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/pull/\d+)?/?$", str(url or ""))
    if match:
        return "%s/%s" % (match.group(1), match.group(2))
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/pull/\d+", str(url or ""))
    if match:
        return "%s/%s" % (match.group(1), match.group(2))
    try:
        import herald_fmt
        import provider

        return provider.github_slug(herald_fmt.git(Path(root), "remote", "get-url", "origin") or "") or ""
    except Exception:
        return ""


def number_of(pr) -> str:
    text = str(pr or "")
    if text.isdigit():
        return text
    match = re.search(r"/pull/(\d+)", text)
    return match.group(1) if match else ""


def call(root: Path, method: str, path: str, fields: Optional[dict] = None, runner: Optional[Callable] = None,
         timeout: int = 20) -> tuple:
    """(ok, doc, text). `fields` go as -f (strings) or -F (numbers and booleans)."""
    cmd = ["gh", "api", "-X", method.upper(), path, "-H", "Accept: application/vnd.github+json"]
    for key, value in (fields or {}).items():
        if isinstance(value, bool) or isinstance(value, int):
            cmd += ["-F", "%s=%s" % (key, str(value).lower() if isinstance(value, bool) else value)]
        else:
            cmd += ["-f", "%s=%s" % (key, value)]
    try:
        done = _runner(runner)(cmd, Path(root) if root else None)
    except TypeError:
        done = _runner(runner)(cmd)
    except Exception as exc:
        return False, None, str(exc)
    text = ((getattr(done, "stdout", "") or "") + "\n" + (getattr(done, "stderr", "") or "")).strip()
    if getattr(done, "returncode", 1) != 0:
        return False, None, text
    try:
        doc = json.loads(getattr(done, "stdout", "") or "null")
    except ValueError:
        doc = None
    return True, doc, text


# ---- reads ---------------------------------------------------------------------------------------------------------


def _rollup(root: Path, slug: str, sha: str, runner) -> list:
    """check-runs and commit statuses as the rows gh's statusCheckRollup has (name/conclusion/status, context/state)."""
    rows = []
    if not sha:
        return rows
    ok, doc, _ = call(root, "GET", "repos/%s/commits/%s/check-runs?per_page=100" % (slug, sha), runner=runner)
    for run in (doc or {}).get("check_runs") or [] if ok and isinstance(doc, dict) else []:
        if isinstance(run, dict):
            rows.append({"__typename": "CheckRun", "name": run.get("name") or "",
                         "status": str(run.get("status") or "").upper(),
                         "conclusion": str(run.get("conclusion") or "").upper(),
                         "startedAt": run.get("started_at"), "completedAt": run.get("completed_at"),
                         "detailsUrl": run.get("details_url") or run.get("html_url") or "",
                         "app": ((run.get("app") or {}).get("slug") if isinstance(run.get("app"), dict) else ""),
                         "id": run.get("id")})
    ok, doc, _ = call(root, "GET", "repos/%s/commits/%s/status" % (slug, sha), runner=runner)
    for st in (doc or {}).get("statuses") or [] if ok and isinstance(doc, dict) else []:
        if isinstance(st, dict):
            rows.append({"__typename": "StatusContext", "context": st.get("context") or "",
                         "state": str(st.get("state") or "").upper(), "targetUrl": st.get("target_url") or ""})
    return rows


def _mergeable(pull: dict) -> tuple:
    value = pull.get("mergeable")
    mergeable = "MERGEABLE" if value is True else ("CONFLICTING" if value is False else "UNKNOWN")
    state = str(pull.get("mergeable_state") or "unknown").upper()
    return mergeable, state


def pr_doc(root: Path, pr, runner: Optional[Callable] = None) -> Optional[dict]:
    """The pull request as the JSON `gh pr view --json …` gives, built from REST. None when REST cannot answer."""
    number = number_of(pr)
    slug = slug_of(root, str(pr))
    if not number or not slug:
        return None
    ok, pull, _ = call(root, "GET", "repos/%s/pulls/%s" % (slug, number), runner=runner)
    if not ok or not isinstance(pull, dict):
        return None
    return doc_from_pull(root, slug, pull, runner=runner)


def doc_from_pull(root: Path, slug: str, pull: dict, runner: Optional[Callable] = None, checks: bool = True) -> dict:
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    sha = str(head.get("sha") or "")
    mergeable, merge_state = _mergeable(pull)
    if pull.get("merged") or pull.get("merged_at"):
        state = "MERGED"
    else:
        state = "OPEN" if str(pull.get("state") or "").lower() == "open" else "CLOSED"
    doc = {
        "url": pull.get("html_url") or "https://github.com/%s/pull/%s" % (slug, pull.get("number")),
        "number": pull.get("number"),
        "headRefOid": sha,
        "headRefName": head.get("ref") or "",
        "baseRefName": base.get("ref") or "",
        "statusCheckRollup": _rollup(root, slug, sha, runner) if checks and state == "OPEN" else [],
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "state": state,
        "createdAt": pull.get("created_at") or "",
        "mergedAt": pull.get("merged_at") or "",
        "updatedAt": pull.get("updated_at") or "",
        "closedAt": pull.get("closed_at") or "",
        "author": ((pull.get("user") or {}).get("login") if isinstance(pull.get("user"), dict) else "") or "",
        "draft": bool(pull.get("draft")),
        "commits": [],
        "latestReviews": [],
        "via": "rest",
    }
    if pull.get("merge_commit_sha") and state == "MERGED":
        doc["mergeCommit"] = {"oid": pull["merge_commit_sha"]}
    elif pull.get("merge_commit_sha"):
        doc["testMergeSha"] = pull["merge_commit_sha"]  # the CI merge ref (refs/pull/N/merge)
    return doc


def changed_pulls(root: Path, slug: str, since: str, runner: Optional[Callable] = None, pages: int = 5) -> tuple:
    """({number: pull}, newest updated_at) for pull requests updated after `since` (all of them on the first read,
    up to `pages` pages of 100). One call per page instead of one per pull request."""
    out: dict = {}
    newest = since or ""
    for page in range(1, max(1, pages) + 1):
        ok, rows, _ = call(root, "GET", "repos/%s/pulls?state=all&sort=updated&direction=desc&per_page=100&page=%d"
                           % (slug, page), runner=runner)
        if not ok or not isinstance(rows, list):
            if page == 1:
                return None, since
            break
        stop = False
        for pull in rows:
            if not isinstance(pull, dict):
                continue
            updated = str(pull.get("updated_at") or "")
            if since and updated and updated <= since:
                stop = True
                break
            out[str(pull.get("number"))] = pull
            if updated > newest:
                newest = updated
        if stop or len(rows) < 100:
            break
    return out, newest


# ---- writes ----------------------------------------------------------------------------------------------------------


def merge(root: Path, pr, sha: str = "", method: str = "squash", runner: Optional[Callable] = None,
          title: str = "") -> tuple:
    """("merged", merge sha) | ("rejected", why). `sha` is the guard: GitHub refuses (409) when the head moved."""
    number = number_of(pr)
    slug = slug_of(root, str(pr))
    if not number or not slug:
        return "rejected", "no GitHub pull request number or repository for %s" % pr
    fields = {"merge_method": method if method in {"squash", "merge", "rebase"} else "squash"}
    if sha:
        fields["sha"] = sha
    if title:
        fields["commit_title"] = title
    ok, doc, text = call(root, "PUT", "repos/%s/pulls/%s/merge" % (slug, number), fields, runner=runner, timeout=60)
    if ok and isinstance(doc, dict) and doc.get("merged"):
        return "merged", str(doc.get("sha") or "merged")
    if "409" in text or "Head branch was modified" in text:
        return "rejected", "the head moved past %s (sha guard); not merged" % (sha[:12] or "?")
    return "rejected", (text or "merge refused").splitlines()[-1][:300]


def refused(detail: str) -> bool:
    """GitHub answered the merge with a definite no: not mergeable (405), head moved (409), or protection."""
    text = str(detail or "")
    low = text.lower()
    return ("sha guard" in low or "not mergeable" in low or "405" in text or "409" in text
            or "required status check" in low or "protected branch" in low or "review required" in low)


def retarget(root: Path, pr, base: str = "main", runner: Optional[Callable] = None) -> tuple:
    number = number_of(pr)
    slug = slug_of(root, str(pr))
    if not number or not slug:
        return False, "no pull request number"
    ok, _doc, text = call(root, "PATCH", "repos/%s/pulls/%s" % (slug, number), {"base": base}, runner=runner)
    return ok, "" if ok else (text.splitlines() or ["?"])[-1]


def comment(root: Path, pr, body: str, runner: Optional[Callable] = None) -> bool:
    number = number_of(pr)
    slug = slug_of(root, str(pr))
    if not number or not slug:
        return False
    ok, _doc, _ = call(root, "POST", "repos/%s/issues/%s/comments" % (slug, number), {"body": body}, runner=runner)
    return ok


def cancel_run(root: Path, slug: str, run_id, runner: Optional[Callable] = None) -> bool:
    ok, _doc, _ = call(root, "POST", "repos/%s/actions/runs/%s/cancel" % (slug, run_id), runner=runner)
    return ok


def runs_for(root: Path, slug: str, sha: str = "", branch: str = "", status: str = "",
             runner: Optional[Callable] = None) -> list:
    query = ["per_page=100"]
    if sha:
        query.append("head_sha=%s" % sha)
    if branch:
        query.append("branch=%s" % branch)
    if status:
        query.append("status=%s" % status)
    ok, doc, _ = call(root, "GET", "repos/%s/actions/runs?%s" % (slug, "&".join(query)), runner=runner)
    return list((doc or {}).get("workflow_runs") or []) if ok and isinstance(doc, dict) else []


def jobs_for(root: Path, slug: str, run_id, runner: Optional[Callable] = None) -> list:
    ok, doc, _ = call(root, "GET", "repos/%s/actions/runs/%s/jobs?per_page=100" % (slug, run_id), runner=runner)
    return list((doc or {}).get("jobs") or []) if ok and isinstance(doc, dict) else []


def merged_since(root: Path, slug: str, since: str, runner: Optional[Callable] = None) -> list:
    """Pull requests merged into any base after `since` (ISO time), newest first, from the incremental list."""
    rows, _newest = changed_pulls(root, slug, "", runner=runner, pages=3)
    out = [p for p in (rows or {}).values() if isinstance(p, dict) and p.get("merged_at") and str(p["merged_at"]) >= since]
    return sorted(out, key=lambda p: str(p.get("merged_at")), reverse=True)


# ---- the incremental provider read ----------------------------------------------------------------------------------


def _cursor_path(beam_path: Path, name: str = "") -> Path:
    return Path(beam_path).parent / (CURSOR_FILE if not name else CURSOR_FILE.replace(".json", "-%s.json" % name))


def _load_cursor(beam_path: Path, name: str = "") -> dict:
    try:
        doc = json.loads(_cursor_path(beam_path, name).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _older(cached: Optional[dict], refresh_s: float, now_s: float) -> bool:
    """A cached snapshot older than refresh_s is read again: main moving can make a pull request dirty or behind
    without changing its updated_at."""
    try:
        return now_s - float((cached or {}).get("_readAt") or 0) >= refresh_s
    except (TypeError, ValueError):
        return True


def incremental(beam_path: Path, root: Path, urls: list, fetch: Callable, runner: Optional[Callable] = None,
                budget_s: float = 45.0, clock: Optional[Callable] = None, refresh_s: float = 900.0,
                name: str = "") -> tuple:
    """({url: snapshot}, stats). Only pull requests whose updated_at changed since the last pass are read again, plus
    those whose last snapshot still had CI or Bugbot pending (a check finishing does not touch updated_at), and
    any never read. The rest reuse the cached snapshot. Stops reading at `budget_s`; what is left waits a pass.
    `fetch(url)` reads one pull request (provider.fetch_pr_state)."""
    import time

    tick = clock or time.monotonic
    began = tick()
    now_s = time.time()
    cache_doc = _load_cursor(beam_path, name)
    snaps = cache_doc.get("snaps") if isinstance(cache_doc.get("snaps"), dict) else {}
    slug = slug_of(root, urls[0] if urls else "")
    changed, newest = (None, cache_doc.get("since") or "")
    if slug:
        changed, newest = changed_pulls(root, slug, str(cache_doc.get("since") or ""), runner=runner)
    out: dict = {}
    stats = {"read": 0, "cached": 0, "deferred": 0, "listed": len(changed or {}) if changed is not None else -1}
    order = []
    for url in urls:
        number = number_of(url)
        cached = snaps.get(url)
        dirty = (changed is None or cached is None or number in (changed or {})
                 or str((cached or {}).get("ci") or "") in {"pending", "running", "queued", "absent", ""}
                 or str((cached or {}).get("bugbot") or "") in {"pending", "running"}
                 or str((cached or {}).get("mergeable") or "").upper() == "UNKNOWN"
                 or _older(cached, refresh_s, now_s))
        order.append((0 if cached is None else (1 if dirty else 2), url, dirty))
    for _rank, url, dirty in sorted(order):
        cached = snaps.get(url)
        if not dirty and cached is not None:
            out[url] = cached
            stats["cached"] += 1
            continue
        if tick() - began > budget_s and cached is not None:
            out[url] = cached
            stats["deferred"] += 1
            continue
        snap = fetch(url)
        if snap:
            out[url] = snap
            snaps[url] = dict(snap, _readAt=now_s)
            stats["read"] += 1
        elif cached is not None:
            out[url] = cached
            stats["cached"] += 1
    keep = set(urls)
    cache_doc = {"since": newest if changed is not None else cache_doc.get("since") or "",
                 "snaps": {u: s for u, s in snaps.items() if u in keep}}
    try:
        _cursor_path(beam_path, name).write_text(json.dumps(cache_doc) + "\n")
    except OSError:
        pass
    return out, stats


def update_branch(root: Path, pr, head: str = "", runner: Optional[Callable] = None) -> tuple:
    """(True, "") | (False, why): GitHub merges the base into the pull request branch itself (1.7.25), with the head as
    the guard. No agent and no clone: one server-side commit, then CI runs on the new head."""
    number = number_of(pr)
    slug = slug_of(root, str(pr))
    if not number or not slug:
        return False, "no GitHub pull request number or repository for %s" % pr
    fields = {"expected_head_sha": head} if head else {}
    ok, _doc, text = call(root, "PUT", "repos/%s/pulls/%s/update-branch" % (slug, number), fields, runner=runner, timeout=60)
    return ok, "" if ok else (text.splitlines() or ["update-branch refused"])[-1][:300]
