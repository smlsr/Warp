#!/usr/bin/env python3
"""Build docs/config.html: every `.warp/config.yaml` key in one grid, with its default, the values it takes,
and what it does.

The rows come from the key table in docs/CONFIG.md, so the page never says something CONFIG.md does not.
Change a key there, then run this script.

  python3 scripts/config_doc.py            write docs/config.html
  python3 scripts/config_doc.py --check    exit 1 when docs/config.html is not current

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "CONFIG.md"
TARGET = ROOT / "docs" / "config.html"

# The order of the sections on the page, and the keys in each. A key not listed here fails --check.
SECTIONS = [
    ("Run and state", "Where the run keeps its state, which repos and paths it covers, and how it is published.", [
        "stateDir", "run", "scanFolder", "gateOrder", "startAfter", "paths", "repo", "multiRepo", "splitRepos", "siblingRoot", "siblingRepos", "repoRuns", "externalRepos",
        "baseFixOwner", "checkpointMinutes", "reportOnComplete", "reportPath"]),
    ("Agents and models", "How many Shuttles run, where they run, and which model each one uses.", [
        "runner", "launch", "worktreeRoot", "subagentVm", "memoryCheck", "cloudSnapshot", "maxAgents", "maxInProgress",
        "model", "modelS", "modelM", "modelL", "modelXL", "fixModel", "fixModelAfter", "staleMinutes", "maxRecoveries",
        "maxSpawnsPerTicket", "maxSpawnsPerHour", "requireCursorApiKey"]),
    ("Merging", "What has to pass before a merge, how the merge is made, and what is published after it.", [
        "autoMergeSizes", "checkCommand", "mergeCheck", "mergeCheckMinutes", "maxMergesPerPass", "mergeMethod", "stateInMerge",
        "publishOnChange", "pushStateNew", "deleteBranchOnMerge", "mergeQueue", "appendOnlyPaths", "maxFixAttempts",
        "respectMergeWindows", "mergeWindows"]),
    ("Cursor Bugbot", "When Bugbot is required, how it is asked, and how long Warp waits for it.", [
        "bugbotRequired", "bugbotManual", "bugbotTrigger", "bugbotStartMinutes", "bugbotPollSeconds"]),
    ("Stalls and repair", "When a ticket counts as stalled or stuck, and how often Warp tries to repair it.", [
        "stuckAfterMinutes", "stallImplementingMinutes", "stallPrOpenMinutes", "stallReviewingMinutes",
        "stallFixingMinutes", "stallReadyMinutes", "stallMergingMinutes", "stallQueuedMinutes", "stallApprovalMinutes",
        "stallMinutes", "ciStartGraceMinutes", "maxStallFixes", "repairSweepMinutes", "launchGraceMinutes", "autoScan", "autoScanResets", "baseCheckSeconds",
        "stallNoPushMinutes", "alarmRepairMinutes",
        "maxAlarmRepairs"]),
    ("Slack, Teams and the listener", "Where Herald posts, what it posts, and how the channel listener paces itself.", [
        "messenger", "notify", "slackChannel", "teamsChannel", "slackMcp", "teamsMcp", "projectName",
        "statusDigestMinutes", "pollSeconds", "reviewPollSeconds", "listenerFastSeconds", "listenerSlowSeconds", "listenerOrphanMinutes",
        "listenerSilentMinutes", "listenerMaxMinutes", "listenerMaxReads", "listenerModel", "notifyAllow",
        "autoAllowTools"]),
    ("Jira", "How tickets are matched to Jira issues, and which statuses Warp moves them through.", [
        "jiraProject", "jiraKeyPrefixes", "jiraKeyMap", "jiraMcp", "jiraSite", "jiraTransition", "jiraInProgressStatus",
        "jiraInReviewStatus", "jiraQaReadyStatus", "jiraDoneStatus", "jiraDoneOnManualMerge", "jiraRestoreOnRelease",
        "jiraExternalIdField", "jiraMatchOnText", "jiraWriteExternalId", "jiraExternalIdFieldName",
        "jiraCreateExternalIdField", "jiraExternalIdFallback"]),
    ("Git provider", "Where pull requests are opened, and which branch tickets start from.", [
        "gitProvider", "githubMcp", "bitbucketMcp", "ghCli", "pushMerge", "baseBranch"]),
]

# The values a key takes, when it is a fixed set. Everything else is read from its default.
ENUMS = {
    "gateOrder": ["strict", "deps"],
    "runner": ["cloud", "local", "auto"],
    "launch": ["worktree", "agent"],
    "messenger": ["slack", "teams", "both"],
    "notify": ["verbose", "quiet"],
    "bugbotTrigger": ["auto", "comment", "manual", "shuttle"],
    "mergeCheck": ["auto", "off"],
    "mergeMethod": ["merge", "squash", "rebase"],
    "gitProvider": ["auto", "github", "bitbucket"],
    "jiraExternalIdFallback": ["label", "remote-link", "none"],
    "autoMergeSizes": ["S", "M", "L", "XL"],  # several at once; the defaults are S and M
}
LISTS = {"appendOnlyPaths", "paths", "jiraKeyPrefixes", "mergeWindows", "notifyAllow", "startAfter"}
MAPS = {"siblingRepos", "repoRuns", "externalRepos", "jiraKeyMap"}
MODELS = {"model", "modelS", "modelM", "modelL", "modelXL", "fixModel", "listenerModel"}


def rows(text: str) -> list[dict]:
    """The key table in CONFIG.md: `| Key | Default | Meaning |` down to the first line that is not a row."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "| Key | Default | Meaning |")
    out = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) != 3:
            continue
        for key in (part.strip(" `") for part in cells[0].split(",")):
            if key:
                out.append({"key": key, "default": cells[1], "meaning": cells[2]})
    return out


def inline(text: str) -> str:
    """CONFIG.md's inline Markdown as HTML: code, links, bold. Everything else is escaped."""
    parts = re.split(r"(`[^`]*`)", text.replace("\\|", "|"))
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            out.append("<code>%s</code>" % html.escape(part[1:-1]))
            continue
        chunk = html.escape(part)
        chunk = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: '<a href="%s">%s</a>' % (_link(m.group(2)), m.group(1)), chunk)
        chunk = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", chunk)
        out.append(chunk)
    return "".join(out)


def _link(target: str) -> str:
    target = html.unescape(target)
    if target.endswith(".md") or ".md#" in target:
        return html.escape(target)
    return html.escape(target)


def plain_default(cell: str) -> str:
    match = re.match(r"^`([^`]*)`$", cell.strip())
    return match.group(1) if match else cell.strip()


def values(key: str, default_cell: str) -> tuple[str, list[str]]:
    """(kind, choices): what a key takes, shown as chips."""
    raw = plain_default(default_cell)
    if key in ENUMS:
        return "choice", ENUMS[key]
    if raw in {"true", "false"}:
        return "switch", ["true", "false"]
    if key in MAPS:
        return "map", ["{repo: git url}" if key == "siblingRepos" else "{plan or run: value}"]
    if key in LISTS:
        return "list", ["comma-separated"]
    if key in MODELS:
        return "model", ["Cursor model slug"]
    unit = next((u for suffix, u in (("Minutes", "minutes"), ("Seconds", "seconds"), ("Hours", "hours")) if key.endswith(suffix)), "")
    if re.fullmatch(r"\d+", raw):
        return "number", [unit or "whole number"]
    if unit:
        return "number", [unit]
    return "text", []


def section_of(key: str) -> str:
    for title, _blurb, keys in SECTIONS:
        if key in keys:
            return title
    return ""


def render(text: str, version: str) -> str:
    table = rows(text)
    by_key = {row["key"]: row for row in table}
    blocks = []
    nav = []
    for title, blurb, keys in SECTIONS:
        anchor = re.sub(r"[^a-z]+", "-", title.lower()).strip("-")
        present = [by_key[k] for k in keys if k in by_key]
        nav.append('<a href="#%s">%s <span>%d</span></a>' % (anchor, html.escape(title), len(present)))
        body = []
        for row in present:
            kind, choices = values(row["key"], row["default"])
            default_plain = plain_default(row["default"])
            defaults = {part.strip() for part in default_plain.split(",")} if kind == "choice" else {default_plain}
            shown_default = '<span class="none">empty</span>' if default_plain in {'""', "empty", "{}"} else inline(row["default"])
            if default_plain == "{}":
                shown_default = '<span class="none">empty map</span>'
            elif default_plain in {"unset", "none"}:
                shown_default = '<span class="none">%s</span>' % default_plain
            chips = "".join(
                '<span class="chip%s">%s</span>' % (" is-default" if choice in defaults else "", html.escape(choice))
                for choice in choices
            )
            search = " ".join([row["key"], default_plain, " ".join(choices), re.sub(r"[`*\\]", "", row["meaning"])]).lower()
            body.append(
                '<tr data-search="%s">'
                '<td class="key"><code id="%s">%s</code></td>'
                '<td class="default">%s</td>'
                '<td class="values"><span class="kind">%s</span>%s</td>'
                '<td class="meaning">%s</td>'
                "</tr>" % (html.escape(search, quote=True), html.escape(row["key"]), html.escape(row["key"]),
                           shown_default, kind, chips, inline(row["meaning"]))
            )
        blocks.append(
            '<section id="%s"><header><h2>%s</h2><p>%s</p></header>'
            '<table><thead><tr><th>Key</th><th>Default</th><th>Values</th><th>What it does</th></tr></thead>'
            "<tbody>%s</tbody></table></section>" % (anchor, html.escape(title), html.escape(blurb), "".join(body))
        )
    count = sum(1 for row in table if section_of(row["key"]))
    return TEMPLATE.replace("{{VERSION}}", html.escape(version)).replace("{{COUNT}}", str(count)).replace(
        "{{NAV}}", "".join(nav)).replace("{{SECTIONS}}", "".join(blocks))


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Warp configuration</title>
<!-- Generated from docs/CONFIG.md by scripts/config_doc.py. Do not edit by hand. -->
<style>
:root {
  --bg: #f7f5f0; --paper: #fffdf8; --ink: #1d1c1a; --muted: #6b675f; --rule: #e4dfd4; --accent: #8a5a2b;
  --chip: #efe9dc; --chip-ink: #3d3a34; --def: #1f4d3a; --def-bg: #dcebe2; --code: #f1ece1; --row: #fbf8f2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141412; --paper: #1b1a18; --ink: #ece8df; --muted: #a29d92; --rule: #2e2c28; --accent: #d6a46a;
    --chip: #2a2824; --chip-ink: #ddd7cb; --def: #bfe3cf; --def-bg: #22392e; --code: #24221f; --row: #1f1e1b;
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 8rem; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 0 28px 64px; }
.masthead { padding-top: 48px; padding-bottom: 22px; }
a { color: var(--accent); }
.eyebrow { font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--accent); font-weight: 600; }
h1 { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif; font-weight: 500; font-size: 40px;
  letter-spacing: -.01em; margin: 6px 0 8px; }
.lede { color: var(--muted); max-width: 780px; margin: 0; }
.lede code, td code { font: 12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code);
  padding: 1px 5px; border-radius: 4px; }
.bar { position: sticky; top: 0; z-index: 5; background: var(--bg);
  box-shadow: 0 6px 12px -10px rgba(0,0,0,.35); border-bottom: 1px solid var(--rule); padding: 12px 0; }
.bar .wrap { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding-bottom: 0; }
.bar input { flex: 0 1 300px; min-width: 200px; font: inherit; padding: 8px 12px; border-radius: 8px;
  border: 1px solid var(--rule); background: var(--paper); color: var(--ink); }
.bar input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
nav { display: flex; flex-wrap: wrap; gap: 6px; }
nav a { font-size: 13px; color: var(--ink); text-decoration: none; padding: 5px 10px; border-radius: 999px;
  border: 1px solid var(--rule); background: var(--paper); white-space: nowrap; }
nav a:hover { border-color: var(--accent); }
nav a span { color: var(--muted); margin-left: 4px; font-variant-numeric: tabular-nums; }
section { margin-top: 36px; }
section header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
h2 { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif; font-weight: 500; font-size: 24px; margin: 0; }
section header p { margin: 0; color: var(--muted); font-size: 14px; }
table { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--paper);
  border: 1px solid var(--rule); border-radius: 12px; overflow: hidden; }
th { text-align: left; font-size: 11.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
  font-weight: 600; padding: 10px 14px; border-bottom: 1px solid var(--rule); background: var(--row); }
td { padding: 12px 14px; border-bottom: 1px solid var(--rule); vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--row); }
td.key { width: 17%; }
td.key code { background: none; padding: 0; font-weight: 600; font-size: 13.5px; color: var(--ink); }
td.default { width: 13%; color: var(--muted); font-size: 13.5px; }
td.values { width: 17%; }
td.meaning { font-size: 14px; }
td.meaning a { color: var(--accent); }
.kind { display: block; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 5px; }
.chip { display: inline-block; font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--chip);
  color: var(--chip-ink); padding: 3px 7px; border-radius: 6px; margin: 0 4px 4px 0; }
.chip.is-default { background: var(--def-bg); color: var(--def); font-weight: 600; }
.chip.is-default::after { content: " default"; font: 10px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: .04em; opacity: .8; }
.none { font-style: italic; }
.empty { display: none; color: var(--muted); padding: 40px 0; text-align: center; }
footer { margin-top: 48px; color: var(--muted); font-size: 13px; border-top: 1px solid var(--rule); padding-top: 16px; }
@media (max-width: 860px) {
  table, thead, tbody, tr, td { display: block; width: 100% !important; }
  thead { display: none; }
  tbody tr { border-bottom: 1px solid var(--rule); padding: 10px 0; }
  td { border: 0; padding: 4px 14px; }
  h1 { font-size: 30px; }
  .wrap { padding-left: 16px; padding-right: 16px; }
  .bar input { flex: 1 1 100%; min-width: 0; }
  nav { flex-wrap: nowrap; overflow-x: auto; width: 100%; padding-bottom: 2px; }
}
</style>
</head>
<body>
<div class="wrap masthead">
  <div class="eyebrow">Warp v{{VERSION}} · configuration</div>
  <h1>Every setting in <code>.warp/config.yaml</code></h1>
  <p class="lede">{{COUNT}} keys, grouped by what they control. The highlighted value is the default. Edit
  <code>.warp/config.yaml</code>, then <code>/warp-resume</code>; agents re-read it at each launch.
  The full text is in <a href="CONFIG.md">CONFIG.md</a>, which this page is built from.</p>
</div>
<div class="bar"><div class="wrap">
  <input id="q" type="search" placeholder="Filter: a key, a value, a word…" aria-label="Filter settings">
  <nav>{{NAV}}</nav>
</div></div>
<main class="wrap">
{{SECTIONS}}
<p class="empty" id="empty">No setting matches that filter.</p>
<footer>Generated from <code>docs/CONFIG.md</code> by <code>scripts/config_doc.py</code>. Change a key there and run the script; the
tests fail when this page is out of date.</footer>
</main>
<script>
(function () {
  var q = document.getElementById("q"), empty = document.getElementById("empty"), bar = document.querySelector(".bar");
  function pad() { document.documentElement.style.scrollPaddingTop = (bar.offsetHeight + 16) + "px"; }
  pad();
  window.addEventListener("resize", pad);
  q.addEventListener("input", function () {
    var words = q.value.toLowerCase().split(/\\s+/).filter(Boolean), shown = 0;
    document.querySelectorAll("section").forEach(function (sec) {
      var any = 0;
      sec.querySelectorAll("tbody tr").forEach(function (tr) {
        var text = tr.getAttribute("data-search"), hit = words.every(function (w) { return text.indexOf(w) >= 0; });
        tr.style.display = hit ? "" : "none";
        if (hit) { any++; }
      });
      sec.style.display = any ? "" : "none";
      shown += any;
    });
    empty.style.display = shown ? "none" : "block";
  });
})();
</script>
</body>
</html>
"""


def build() -> str:
    version = (ROOT / "VERSION").read_text().strip()
    return render(SOURCE.read_text(), version)


def malformed(text: str) -> list[int]:
    """Line numbers of key-table rows that do not split into three cells (a bare `|` inside a code span)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "| Key | Default | Meaning |")
    bad = []
    for i, line in enumerate(lines[start + 2:], start + 3):
        if not line.startswith("|"):
            break
        if len(re.split(r"(?<!\\)\|", line.strip())) - 2 != 3:
            bad.append(i)
    return bad


def unplaced(text: str) -> list[str]:
    return sorted({row["key"] for row in rows(text)} - {key for _t, _b, keys in SECTIONS for key in keys})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build docs/config.html from docs/CONFIG.md",
                                     epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/config.html is not current")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    broken = malformed(SOURCE.read_text())
    if broken:
        print("config-doc: docs/CONFIG.md rows that do not have three cells (escape a | inside code as \\|): line %s"
              % ", ".join(map(str, broken)))
        return 1
    missing = unplaced(SOURCE.read_text())
    if missing:
        print("config-doc: keys with no section in scripts/config_doc.py: %s" % ", ".join(missing))
        return 1
    page = build()
    if args.check:
        current = TARGET.read_text() if TARGET.is_file() else ""
        if current != page:
            print("config-doc: docs/config.html is out of date. Run python3 scripts/config_doc.py")
            return 1
        print("config-doc: docs/config.html is current")
        return 0
    TARGET.write_text(page)
    print("config-doc: wrote %s (%d keys)" % (TARGET.relative_to(ROOT), len(rows(SOURCE.read_text()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
