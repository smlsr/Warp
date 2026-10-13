#!/usr/bin/env python3
"""/warp-from-jira: build the plan files /warp-scan reads from Jira issues that carry given labels.

Warp has no Jira credentials. `plan` writes the query to .warp/jira-pull.json and prints what
to call; the agent runs searchJiraIssuesUsingJql on the Atlassian server named jiraMcp and saves
every page; `build` turns the saved issues into a plan folder and prints `next: /warp-scan <folder>`.

  jira_pull.py plan version:1.5.5,ui
  jira_pull.py plan version:1.5.5 --folder plans/v155 --project WAR --match all --jql "type != Bug"
  jira_pull.py build                       # reads .warp/jira-pull/issues.json
  jira_pull.py build --results pages/ --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plansim  # noqa: E402

REQUEST = "jira-pull.json"
RESULTS = "jira-pull/issues.json"
DEFAULT_PARENT = "warp-plans"
FIELDS = ["summary", "description", "labels", "status", "priority", "issuetype", "components",
          "issuelinks", "parent", "timeoriginalestimate", "fixVersions"]
EXTERNAL_NAMES = {"externalid", "externalkey", "planid", "ticketid"}
POINTS_NAMES = {"storypoints", "storypointestimate"}
BLOCKED_BY = {"is blocked by", "depends on", "is dependent on"}
DONE_NAMES = {"done", "closed", "resolved", "complete", "completed"}
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
REF_RE = re.compile(r"(?<![\w:/-])(?:([a-z][a-z0-9-]*):)?([A-Z][A-Z0-9_]*-\d+)\b")
H_RE = re.compile(r"(?im)^(?:h[1-6]\.\s*|#{1,6}\s+)(.+?)\s*$")
SIZE_HOURS = plansim.SIZE_HOURS

HELP = """\
/warp-from-jira <label>[,<label>…]: Jira issues with those labels become a plan folder for /warp-scan.

plan  <labels>     write .warp/jira-pull.json (the JQL, the fields, the folder) and print the calls
      --folder F   the plan folder (default warp-plans/<labels>); refused when it already has a plan
      --project K  limit to one Jira project (default jiraProject); --any-project searches them all
      --match all  every label must be on the issue (default any: labels in (...))
      --jql TEXT   one more JQL clause, ANDed (for example "type != Bug")
      --force      rebuild a folder that already has a plan
build              read the saved issues and write the folder
      --results P  the saved search: a file or a folder of page files (default .warp/jira-pull/issues.json)
      --dry-run    print what would be written; write nothing
      --force      same as on plan

The search transcript is what searchJiraIssuesUsingJql returned: {"issues": [...]} for one page,
{"pages": [<response>, ...]} for several, a list of either, or a folder of page files. Add
"names": {"customfield_10050": "External ID"} when you have the field names.

From each issue (labels and description sections are the ones Warp's own Jira import writes):
  plan id    External ID field (jiraExternalIdField, else a field named External ID / External Key /
             Plan ID / Ticket ID), else label warp:<id> or ext:<id>, else a [ID] summary prefix,
             else the Jira key
  size       label size:S|M|L|XL, else the Size section, else the original estimate, else story
             points (<=2 S, <=5 M, <=8 L, else XL), else M (reported)
  hours      the original estimate when set, else the size (S 4, M 7, L 11, XL 16)
  blockers   "is blocked by" / "depends on" links and the Blocked by section; run:id stays a
             cross-run blocker; a blocker that is Done in Jira is met; one outside the labels and
             not Done is left out and reported
  locks      the Locks section (one path per line); none is reported
  gate       label gate:<key> (gates are blocking; order G-number, then name)
  lane, repo label lane:<x>, repo:<x>; module label area:<x>, else the first component
  acceptance the Acceptance (criteria) section, one per line
Done issues and epics are left out.

Writes <folder>/plan/schedule.json (build-schedule/2, planned with maxAgents slots, gateOrder and
autoMergeSizes from .warp/config.yaml), <folder>/plan/FROM_JIRA.md (the query and every warning),
<folder>/jira/issues.json (the issues as read) and .warp/jira-map.json (plan id -> Jira key).
Nothing is written to Jira.
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


# ------------------------------------------------------------------ config and paths
def read_config(root: Path) -> dict:
    path = root / ".warp" / "config.yaml"
    if not path.is_file():
        return {}
    import prompt_gate

    return prompt_gate.yaml_scalars(path.read_text())


def auto_sizes(cfg: dict) -> list:
    raw = cfg.get("autoMergeSizes")
    if raw is None or raw == "":
        return ["S", "M"]
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[,\s\[\]\"']+", str(raw))
    return [s.strip().upper() for s in items if s.strip().upper() in SIZE_HOURS]


def split_labels(raw) -> list:
    items = []
    for part in raw if isinstance(raw, list) else [raw]:
        items += [x.strip() for x in re.split(r"[,\s]+", str(part or "")) if x.strip()]
    return list(dict.fromkeys(items))


def slug(labels: list) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(labels))).strip("-") or "jira"


def has_plan(folder: Path) -> list:
    """Plan files /warp-scan would read in this folder."""
    if not folder.is_dir():
        return []
    import scan

    found = scan.scan(folder.parent if folder.parent.exists() else folder, folder)
    return [rel for k in ("warp", "schedules", "plans", "jira") for rel in found[k]]


def q(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(labels: list, match: str, project: Optional[str], extra: str) -> str:
    parts = []
    if project:
        parts.append(f"project = {q(project)}")
    if match == "all":
        parts += [f"labels = {q(l)}" for l in labels]
    else:
        parts.append("labels in (" + ", ".join(q(l) for l in labels) + ")")
    if extra and extra.strip():
        parts.append(f"({extra.strip()})")
    return " AND ".join(parts) + " ORDER BY key ASC"


# ------------------------------------------------------------------ plan
def cmd_plan(args, root: Path) -> int:
    labels = split_labels(args.labels)
    if not labels:
        print("refuse: name at least one label, for example /warp-from-jira version:1.5.5")
        return 2
    cfg = read_config(root)
    project = None if args.any_project else (args.project or str(cfg.get("jiraProject") or "").strip() or None)
    jql = build_jql(labels, args.match, project, args.jql or "")
    folder = Path(args.folder) if args.folder else Path(DEFAULT_PARENT) / slug(labels)
    existing = has_plan(root / folder)
    if existing and not args.force:
        print(f"refuse: {folder} already has a plan ({', '.join(existing[:3])}). /warp-scan {folder} reads it. "
              f"--force rebuilds it from Jira.")
        return 3
    fields = list(FIELDS)
    ext = str(cfg.get("jiraExternalIdField") or "").strip()
    names_needed = False
    if ext.startswith("customfield_"):
        fields.append(ext)
    elif ext:
        names_needed = True
    request = {"labels": labels, "match": args.match, "project": project, "extraJql": args.jql or "", "jql": jql,
               "fields": ["*all"] if names_needed else fields, "expandNames": names_needed, "maxResults": 100,
               "folder": str(folder), "force": bool(args.force), "results": f".warp/{RESULTS}",
               "server": str(cfg.get("jiraMcp") or "atlassian"), "cloudId": str(cfg.get("jiraSite") or "") or None,
               "createdAt": utcnow()}
    warp = root / ".warp"
    warp.mkdir(parents=True, exist_ok=True)
    (warp / REQUEST).write_text(json.dumps(request, indent=2) + "\n")
    print(f"from-jira: labels {', '.join(labels)} ({'every label' if args.match == 'all' else 'any label'})"
          + (f", project {project}" if project else ", every project"))
    print(f"jql: {jql}")
    print(f"fields: {', '.join(request['fields'])}" + (" (expand names: jiraExternalIdField is a field name)" if names_needed else ""))
    print(f"folder: {folder}" + (" (rebuilt)" if existing else " (new)"))
    print(f"request: .warp/{REQUEST}")
    print(f"next: on {request['server']}, cloudId from " + ("jiraSite" if request["cloudId"] else "getAccessibleAtlassianResources")
          + f"; searchJiraIssuesUsingJql with that jql and fields, maxResults 100, every page (nextPageToken) saved to .warp/{RESULTS}")
    print("then: python3 " + str(Path(__file__).resolve()) + " build")
    return 0


# ------------------------------------------------------------------ reading what Jira returned
def adf_text(node, depth: int = 0) -> str:
    """Atlassian Document Format to text: headings become `## Heading`, list items and paragraphs lines."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n, depth) for n in node)
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("type")
    kids = node.get("content") or []
    if kind == "text":
        text = node.get("text") or ""
        if any(m.get("type") == "code" for m in node.get("marks") or []):
            text = "`" + text + "`"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "heading":
        return "## " + adf_text(kids, depth).strip() + "\n"
    if kind in ("paragraph",):
        return adf_text(kids, depth).strip() + "\n"
    if kind in ("bulletList", "orderedList", "taskList"):
        return "".join(adf_text(k, depth + 1) for k in kids)
    if kind in ("listItem", "taskItem"):
        body = adf_text(kids, depth).strip().replace("\n", " ")
        return "- " + body + "\n"
    if kind == "codeBlock":
        return adf_text(kids, depth).rstrip() + "\n"
    if kind in ("inlineCard", "blockCard"):
        return str((node.get("attrs") or {}).get("url") or "")
    if kind == "mention":
        return str((node.get("attrs") or {}).get("text") or "")
    return adf_text(kids, depth)


def text_of(desc) -> str:
    if isinstance(desc, (dict, list)):
        return adf_text(desc).strip()
    return str(desc or "").strip()


def sections(text: str) -> dict:
    found = list(H_RE.finditer(text or ""))
    out = {}
    for n, m in enumerate(found):
        end = found[n + 1].start() if n + 1 < len(found) else len(text)
        out.setdefault(m.group(1).strip().strip("*").strip().casefold(), text[m.end():end].strip())
    return out


def lines_of(body: str) -> list:
    rows = []
    for line in (body or "").splitlines():
        t = re.sub(r"^\s*(?:[*\-#]+|\d+[.)])\s*", "", line).strip()
        if t and not re.fullmatch(r"[|\s-]*", t):
            rows.append(t)
    return rows


def is_none(text: str) -> bool:
    return str(text).strip().strip(".").casefold() in {"", "none", "n/a", "na", "-", "—", "nothing"}


def clean_path(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^\{\{(.*)\}\}$", r"\1", t)
    t = t.strip("`").strip()
    t = re.sub(r"\s+[—–-]\s+.*$", "", t)  # "path — why"
    return t.strip()


def values(raw) -> list:
    if raw is None or isinstance(raw, bool):
        return []
    if isinstance(raw, dict):
        out = []
        for k in ("value", "name", "key", "id"):
            if raw.get(k) not in (None, ""):
                out.append(str(raw[k]).strip())
                break
        return out
    if isinstance(raw, list):
        out = []
        for r in raw:
            out += values(r)
        return out
    t = str(raw).strip()
    return [t] if t else []


def size_of(text) -> Optional[str]:
    t = str(text or "").strip().upper()
    t = re.sub(r"^SIZE\s*:\s*", "", t)
    m = re.match(r"^(XL|S|M|L)\b", t)
    if m:
        return m.group(1)
    words = {"SMALL": "S", "MEDIUM": "M", "LARGE": "L", "XLARGE": "XL", "X-LARGE": "XL", "EXTRA LARGE": "XL"}
    for w, s in words.items():
        if t.startswith(w):
            return s
    return None


def size_from_hours(h: float) -> str:
    return "S" if h <= 4 else "M" if h <= 7 else "L" if h <= 11 else "XL"


def status_of(fields: dict) -> tuple:
    st = fields.get("status")
    if isinstance(st, dict):
        name = str(st.get("name") or "")
        cat = str(((st.get("statusCategory") or {}).get("key")) or "")
        return name, cat == "done" or name.casefold() in DONE_NAMES
    name = str(st or "")
    return name, name.casefold() in DONE_NAMES


def load_results(path: Path) -> tuple:
    """(issues, names) from a saved search: one response, {"pages": [...]}, a list, or a folder."""
    docs = []
    if path.is_dir():
        for p in sorted(path.glob("*.json")):
            docs.append(json.loads(p.read_text()))
    else:
        docs.append(json.loads(path.read_text()))
    issues, names = {}, {}

    def take(doc):
        if isinstance(doc, list):
            for d in doc:
                take(d)
            return
        if not isinstance(doc, dict):
            return
        if isinstance(doc.get("names"), dict):
            names.update({str(k): str(v) for k, v in doc["names"].items()})
        for key in ("pages", "responses", "results"):
            if isinstance(doc.get(key), list):
                take(doc[key])
        for key in ("issues", "values"):
            if isinstance(doc.get(key), list):
                for it in doc[key]:
                    if isinstance(it, dict) and (it.get("key") or (it.get("fields") or {}).get("key")):
                        k = str(it.get("key") or it["fields"]["key"])
                        if isinstance(it.get("names"), dict):
                            names.update({str(a): str(b) for a, b in it["names"].items()})
                        issues[k] = it  # a later copy (getJiraIssue) wins
        if doc.get("key") and isinstance(doc.get("fields"), dict):
            issues[str(doc["key"])] = doc

    for d in docs:
        take(d)
    return list(issues.values()), names


def external_id(issue: dict, fields: dict, names: dict, cfg: dict, labels: list, summary: str, key: str) -> tuple:
    conf = str(cfg.get("jiraExternalIdField") or "").strip()
    by_name = {norm(v): k for k, v in names.items()}
    tried = []
    if conf:
        tried.append(conf if conf.startswith("customfield_") else by_name.get(norm(conf), conf))
    tried += [fid for fid, nm in names.items() if norm(nm) in EXTERNAL_NAMES]
    for fid in tried:
        got = values(fields.get(fid))
        if got:
            return got[0], "field " + (names.get(fid) or fid)
    for cf in issue.get("customFieldValues") or fields.get("customFieldValues") or []:
        if isinstance(cf, dict) and norm(cf.get("fieldName")) in EXTERNAL_NAMES and values(cf.get("value")):
            return values(cf.get("value"))[0], "field " + str(cf.get("fieldName"))
    for raw in (issue.get("externalId"), fields.get("externalId")):
        if values(raw):
            return values(raw)[0], "externalId"
    for lab in labels:
        m = re.match(r"(?i)^(?:warp|ext):(.+)$", lab)
        if m and m.group(1).strip():
            return m.group(1).strip(), "label " + lab.split(":", 1)[0].lower() + ":"
    m = re.match(r"^\s*\[([A-Za-z][A-Za-z0-9_]*-\d+)\]", summary or "")
    if m and m.group(1).upper() != key:
        return m.group(1), "summary prefix"
    return key, "Jira key"


def read_issue(issue: dict, names: dict, cfg: dict) -> dict:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else issue
    key = str(issue.get("key") or fields.get("key") or "").strip().upper()
    summary = str(fields.get("summary") or "")
    labels = [str(l) for l in fields.get("labels") or []]
    status, done = status_of(fields)
    itype = fields.get("issuetype")
    itype = str(itype.get("name") if isinstance(itype, dict) else itype or "")
    text = text_of(fields.get("description"))
    sec = sections(text)
    tid, id_from = external_id(issue, fields, names, cfg, labels, summary, key)
    summary_clean = re.sub(r"^\s*\[" + re.escape(tid) + r"\]\s*", "", summary).strip() or summary

    lab = {}
    for l in labels:
        if ":" in l:
            k, v = l.split(":", 1)
            lab.setdefault(k.strip().lower(), v.strip())
    # size and hours
    est = fields.get("timeoriginalestimate")
    est_h = round(float(est) / 3600.0, 2) if isinstance(est, (int, float)) and est > 0 else None
    points = None
    for fid, nm in names.items():
        if norm(nm) in POINTS_NAMES and isinstance(fields.get(fid), (int, float)):
            points = float(fields[fid])
    size, size_from = None, None
    if size_of(lab.get("size")):
        size, size_from = size_of(lab.get("size")), "label"
    elif sec.get("size") and lines_of(sec["size"]) and size_of(lines_of(sec["size"])[0]):
        size, size_from = size_of(lines_of(sec["size"])[0]), "Size section"
    elif est_h:
        size, size_from = size_from_hours(est_h), "original estimate"
    elif points is not None:
        size = "S" if points <= 2 else "M" if points <= 5 else "L" if points <= 8 else "XL"
        size_from = "story points"
    else:
        size, size_from = "M", None
    hours = est_h or SIZE_HOURS[size]
    # locks
    lock_body = sec.get("locks") or sec.get("lock paths") or ""
    locks = [] if is_none(lock_body) else [clean_path(x) for x in lines_of(lock_body) if not is_none(x)]
    locks = [x for x in dict.fromkeys(locks) if x]
    # blockers
    refs = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        typ = link.get("type") or {}
        if link.get("inwardIssue"):
            rel, other = str(typ.get("inward") or ""), link["inwardIssue"]
        elif link.get("outwardIssue"):
            rel, other = str(typ.get("outward") or ""), link["outwardIssue"]
        else:
            continue
        if not rel and str(typ.get("name") or "").casefold() == "blocks":
            rel = "is blocked by" if link.get("inwardIssue") else "blocks"
        if rel.strip().casefold() in BLOCKED_BY:
            ofields = other.get("fields") or {}
            oname, odone = status_of(ofields)
            refs.append({"ref": str(other.get("key") or "").upper(), "run": None, "status": oname, "done": odone, "via": "link"})
    blocked = sec.get("blocked by") or sec.get("blockers") or sec.get("depends on") or ""
    if not is_none(blocked):
        for m in REF_RE.finditer(blocked):
            refs.append({"ref": m.group(2), "run": m.group(1), "status": None, "done": False, "via": "Blocked by"})
    acs = lines_of(sec.get("acceptance") or sec.get("acceptance criteria") or "")
    gate = lab.get("gate") or (lines_of(sec.get("gate") or "")[:1] or [None])[0]
    gate = re.sub(r"\s.*$", "", gate).strip() if gate else None
    comps = values(fields.get("components"))
    return {
        "key": key, "id": tid, "idFrom": id_from, "summary": summary_clean, "type": itype, "status": status, "done": done,
        "labels": labels, "size": size, "sizeFrom": size_from, "hours": hours, "locks": locks, "refs": refs, "acs": acs,
        "gate": gate or None, "lane": lab.get("lane") or None, "repo": lab.get("repo") or None,
        "module": lab.get("area") or (comps[0] if comps else None), "layer": lab.get("layer") or None,
        "priority": (values(fields.get("priority")) or [None])[0], "parent": (values(fields.get("parent")) or [None])[0],
        "description": text,
    }


# ------------------------------------------------------------------ build
def load_request(root: Path) -> dict:
    path = root / ".warp" / REQUEST
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def matches(row: dict, labels: list, match: str) -> bool:
    if not labels:
        return True
    have = {l.casefold() for l in row["labels"]}
    want = [l.casefold() for l in labels]
    return all(w in have for w in want) if match == "all" else any(w in have for w in want)


def assemble(rows: list, req: dict, cfg: dict) -> tuple:
    """(schedule, report lines, warnings, kept rows). Raises ValueError on a cycle."""
    notes, warns = [], []
    labels, match = req.get("labels") or [], req.get("match") or "any"
    done_keys = {r["key"] for r in rows if r["done"]}
    left_done = [r for r in rows if r["done"]]
    epics = [r for r in rows if r["type"].casefold() == "epic" and not r["done"]]
    other = [r for r in rows if not r["done"] and r not in epics and not matches(r, labels, match)]
    kept = [r for r in rows if not r["done"] and r not in epics and r not in other]
    if left_done:
        notes.append(f"{len(left_done)} done in Jira, left out: " + ", ".join(sorted((r["key"] for r in left_done), key=plansim.natkey)))
    if epics:
        notes.append(f"{len(epics)} epic(s) left out: " + ", ".join(r["key"] for r in epics))
    if other:
        notes.append(f"{len(other)} without the labels, left out: " + ", ".join(r["key"] for r in other))
    # ids: unique
    seen = {}
    for r in kept:
        if r["id"] in seen and r["id"] != r["key"]:
            warns.append(f"{r['key']} and {seen[r['id']]} both say plan id {r['id']}: {r['key']} keeps its Jira key as its id")
            r["id"], r["idFrom"] = r["key"], "Jira key (duplicate plan id)"
        seen[r["id"]] = r["key"]
    by_key = {r["key"]: r for r in kept}
    by_id = {r["id"]: r for r in kept}
    outside = []
    for r in kept:
        after = []
        for ref in r["refs"]:
            if ref["run"]:
                after.append(f"{ref['run']}:{ref['ref']}")
                continue
            hit = by_key.get(ref["ref"].upper()) or by_id.get(ref["ref"])
            if hit:
                if hit is not r:
                    after.append(hit["id"])
            elif ref["done"] or ref["ref"].upper() in done_keys:
                continue
            else:
                outside.append(f"{r['id']} ← {ref['ref']}" + (f" ({ref['status']})" if ref["status"] else ""))
        r["after"] = list(dict.fromkeys(after))
    if outside:
        warns.append("blockers outside these labels and not Done, left out (add the label, or write <run>:<id> in Blocked by): "
                     + ", ".join(outside))
    nosize = [r["id"] for r in kept if not r["sizeFrom"]]
    if nosize:
        warns.append(f"{len(nosize)} without a size (label size:S|M|L|XL, Size section, original estimate or story points), planned as M: "
                     + ", ".join(nosize))
    nolocks = [r["id"] for r in kept if not r["locks"]]
    if nolocks:
        warns.append(f"{len(nolocks)} without locks (a Locks section, one path per line); Warp cannot keep them apart: " + ", ".join(nolocks))
    if not kept:
        return None, notes, warns, kept

    gkeys = plansim.gate_keys(r["gate"] for r in kept if r["gate"])
    gates = [{"key": k, "members": sorted([r["id"] for r in kept if r["gate"] == k], key=plansim.natkey), "priority": False} for k in gkeys]
    order = "deps" if str(cfg.get("gateOrder") or "").strip().lower() == "deps" else "strict"
    slots = int(str(cfg.get("maxAgents") or "18").strip() or 18) if str(cfg.get("maxAgents") or "18").strip().isdigit() else 18
    autos = cfg["_autoMergeSizes"] if isinstance(cfg.get("_autoMergeSizes"), list) else auto_sizes(cfg)
    sim_in = [{"id": r["id"], "hours": r["hours"], "size": r["size"], "after": r["after"], "locks": r["locks"], "gate": r["gate"]} for r in kept]
    sim = plansim.plan(sim_in, gates, slots=slots, auto_sizes=autos, approval=0.5, strict=order == "strict")
    by_slots = {}
    for k in sorted({1, 2, 4, 6, 8, 12, 18, 24, slots}):
        by_slots[str(k)] = round(plansim.plan(sim_in, gates, slots=k, auto_sizes=autos, approval=0.5, strict=order == "strict")["finish"], 2)
    S = sim["tickets"]
    anc = {}

    def A(i):
        if i not in anc:
            anc[i] = set()
            for d in by_id[i]["after"]:
                if d in by_id:
                    anc[i] |= {d} | A(d)
        return anc[i]

    first_gate = {}
    for g in gates:
        for i in set(g["members"]).union(*[A(m) for m in g["members"]]):
            first_gate.setdefault(i, g["key"])
    tickets = []
    for r in sorted(kept, key=lambda r: (S[r["id"]]["start"], S[r["id"]]["slot"])):
        s = S[r["id"]]
        files = [p for p in r["locks"] if not p.endswith("/")]
        tickets.append({
            "id": r["id"], "jira": r["key"], "summary": r["summary"], "module": r["module"], "layer": r["layer"], "lane": r["lane"],
            "wave": s["wave"], "repo": r["repo"], "file": files[0] if len(r["locks"]) == 1 and files else None,
            "size": r["size"], "h": r["hours"], "rank": s["rank"], "latest_start": round(s["latest_start"], 2), "critical": s["critical"],
            "priority_gate": s["priority_gate"], "slot": s["slot"], "start": round(s["start"], 2), "merge": round(s["merge"], 2),
            "ready": round(s["ready"], 2), "waited": round(s["waited"], 2), "why": s["why"],
            "after": r["after"], "after_other_modules": [a for a in r["after"] if ":" in a], "unlocks": s["unlocks"],
            "gate": r["gate"] or "", "first_gate": first_gate.get(r["id"], ""), "checks": [], "acs": r["acs"],
            "locks": r["locks"], "locks_more": 0, "lock_pairs": [], "screens": [], "mock": None,
        })
    out_gates = []
    for g in gates:
        behind = set(g["members"]).union(*[A(m) for m in g["members"]])
        out_gates.append({"key": g["key"], "name": g["key"], "blocking": True, "scope": "everyone", "members": g["members"],
                          "behind": len(behind), "at": round(sim["gate_at"].get(g["key"], 0.0), 2),
                          "closes_with": sim["closes_with"].get(g["key"], []), "checks": [], "if_red": None, "on_green": None,
                          "sibling": None, "siblings": [], "priority": False, "starts": [], "old_reached": None})
    steps = [{"n": n, "t": round(s["t"], 2), "merges": s["merges"], "starts": s["starts"], "queued": s["queued"], "held": s["held"],
              "running": s["running"], "free": s["free"], "gates": s["gates"]} for n, s in enumerate(sim["steps"])]
    repos = sorted({r["repo"] for r in kept if r["repo"]})
    lanes = sorted({r["lane"] for r in kept if r["lane"]}, key=plansim.natkey)
    finish = sim["finish"]
    sched = {
        "schema": "build-schedule/2",
        "program": "+".join(labels) or "jira",
        "rebuilt": utcnow(),
        "source": {"from": "jira", "tool": "/warp-from-jira (scripts/jira_pull.py)", "labels": labels, "match": match,
                   "project": req.get("project"), "jql": req.get("jql"), "issues": len(rows), "generated": utcnow()},
        "model": {"orchestrators": 1, "slots": slots,
                  "merge": f"continuous: each PR merges when green; sizes outside autoMergeSizes ({', '.join(autos) or 'none'}) wait 0.5 h for approval",
                  "clock": "estimate hours (S 4 · M 7 · L 11 · XL 16, or the issue's original estimate), continuous",
                  "priority": "critical chain first, then rank (ascending latest_start), then natural id"
                              + ("; gate_order strict: a gate block starts when every earlier block has merged" if order == "strict" and gates else ""),
                  "gate_order": order},
        "numbers": {"tickets": len(kept), "waves": sim["waves"], "agent_hours": round(sim["agent_hours"], 2), "finish_h": round(finish, 2),
                    "critical_path_h": round(sim["cp_len"], 2), "critical_tickets": len(sim["chain"]), "peak": sim["peak"],
                    "utilisation": round(sim["agent_hours"] / (slots * finish), 3) if finish else 0,
                    "delayed_by_cap": sum(1 for t in tickets if t["waited"] > 0), "steps": len(steps),
                    "max_queue": max((len(s["queued"]) for s in steps), default=0), "finish_by_slots": by_slots, "lock_pairs": 0, "old_plan": None},
        "critical_chain": sim["chain"],
        "lanes": lanes,
        "repos": repos,
        "gates": out_gates,
        "lock_pairs": [],
        "tickets": tickets,
        "steps": steps,
        "fields": {"note": "build-schedule/2. Written by /warp-from-jira from Jira issues; `jira` is each ticket's issue key and `acs` its acceptance criteria."},
    }
    ids_from = {}
    for r in kept:
        ids_from[r["idFrom"].split(" (")[0]] = ids_from.get(r["idFrom"].split(" (")[0], 0) + 1
    notes.append("plan ids: " + ", ".join(f"{n} from {k}" for k, n in sorted(ids_from.items(), key=lambda x: -x[1])))
    return sched, notes, warns, kept


def summary_md(sched: dict, req: dict, notes: list, warns: list, kept: list, folder: Path) -> str:
    n = sched["numbers"]
    o = [f"# {sched['program']} — plan from Jira", "",
         f"Written by `/warp-from-jira` at {sched['rebuilt']}. Rebuild: `/warp-from-jira {','.join(req.get('labels') or [])} --force`. "
         f"Run: `/warp-scan {folder}`.", "",
         f"- JQL: `{req.get('jql')}`",
         f"- {n['tickets']} tickets · {n['agent_hours']} agent-hours · finish T+{n['finish_h']}h at {sched['model']['slots']} slots · "
         f"peak {n['peak']} · critical chain {n['critical_path_h']}h over {n['critical_tickets']} tickets · gate order {sched['model']['gate_order']}",
         "- ★ " + (" → ".join(sched["critical_chain"]) or "—"), ""]
    if notes or warns:
        o += ["## Notes", ""] + [f"- {x}" for x in notes] + [f"- **warn:** {x}" for x in warns] + [""]
    if sched["gates"]:
        o += ["## Gates", "", "| Gate | Members | Reached |", "|---|---|---|"]
        o += [f"| {g['key']} | {', '.join(g['members'])} | T+{g['at']}h |" for g in sched["gates"]] + [""]
    o += ["## Tickets", "", "| id | Jira | size | h | gate | after | locks | start → merge | summary |", "|---|---|---|---|---|---|---|---|---|"]
    for t in sorted(sched["tickets"], key=lambda t: plansim.natkey(t["id"])):
        o.append(f"| {'★ ' if t['critical'] else ''}{t['id']} | {t['jira']} | {t['size']} | {t['h']:g} | {t['gate'] or '—'} | "
                 f"{', '.join(t['after']) or '—'} | {len(t['locks'])} | T+{t['start']:g} → T+{t['merge']:g} | {t['summary'].replace('|', '/')} |")
    return "\n".join(o) + "\n"


def cmd_build(args, root: Path) -> int:
    req = load_request(root)
    if args.labels:
        req["labels"] = split_labels(args.labels)
    if args.folder:
        req["folder"] = args.folder
    labels = req.get("labels") or []
    folder = Path(req.get("folder") or (Path(DEFAULT_PARENT) / slug(labels)))
    results = Path(args.results) if args.results else root / ".warp" / RESULTS
    if not results.is_absolute() and not results.exists():
        results = root / results
    if not results.exists():
        print(f"refuse: no saved search at {results}. Run jira_pull.py plan, then searchJiraIssuesUsingJql, and save every page there.")
        return 2
    try:
        raw, names = load_results(results)
    except (OSError, json.JSONDecodeError) as e:
        print(f"refuse: {results} is not JSON ({e})")
        return 2
    if not raw:
        print(f"refuse: {results} holds no issues. jql: {req.get('jql') or '(none saved)'}. Check the labels and the project.")
        return 2
    cfg = read_config(root)
    yaml_path = root / ".warp" / "config.yaml"
    if yaml_path.is_file():
        import beam

        cfg["_autoMergeSizes"] = [s for s in beam.resolve_auto_merge_sizes(None, yaml_path) if s in SIZE_HOURS]
    rows = [read_issue(i, names, cfg) for i in raw]
    try:
        sched, notes, warns, kept = assemble(rows, req, cfg)
    except ValueError as e:
        print(f"refuse: {e}. Fix the links in Jira, then build again.")
        return 4
    print(f"jira: {len(rows)} issues read from {results}")
    for x in notes:
        print(f"note: {x}")
    if sched is None:
        print("refuse: no open issue is left to plan.")
        return 2
    n = sched["numbers"]
    print(f"plan: {n['tickets']} tickets · {len(sched['gates'])} gates" + (f" ({', '.join(g['key'] for g in sched['gates'])})" if sched["gates"] else "")
          + f" · {n['agent_hours']:g} agent-hours · finish T+{n['finish_h']:g}h at {sched['model']['slots']} slots · peak {n['peak']}"
          + f" · critical chain {n['critical_tickets']} tickets · gate order {sched['model']['gate_order']}")
    for x in warns:
        print(f"warn: {x}")
    existing = has_plan(root / folder)
    if existing and not (args.force or req.get("force")):
        print(f"refuse: {folder} already has a plan ({', '.join(existing[:3])}). --force rebuilds it.")
        return 3
    files = [folder / "plan/schedule.json", folder / "plan/FROM_JIRA.md", folder / "jira/issues.json", Path(".warp/jira-map.json")]
    if args.dry_run:
        print("dry run: would write " + ", ".join(str(f) for f in files))
        return 0
    (root / folder / "plan").mkdir(parents=True, exist_ok=True)
    (root / folder / "jira").mkdir(parents=True, exist_ok=True)
    (root / folder / "plan/schedule.json").write_text(json.dumps(sched, indent=2, ensure_ascii=False) + "\n")
    (root / folder / "plan/FROM_JIRA.md").write_text(summary_md(sched, req, notes, warns, kept, folder))
    issues_doc = {"source": sched["source"], "issues": [{k: r[k] for k in ("id", "key", "idFrom", "summary", "type", "status", "labels", "size", "sizeFrom",
                                                                          "hours", "gate", "lane", "repo", "module", "locks", "after", "acs", "priority", "parent", "description")}
                                                         for r in sorted(kept, key=lambda r: plansim.natkey(r["id"]))]}
    (root / folder / "jira/issues.json").write_text(json.dumps(issues_doc, indent=2, ensure_ascii=False) + "\n")
    import jira_sync

    for r in kept:
        jira_sync.remember_map(root / ".warp" / "beam.json", r["id"], r["key"], source="export", confidence="high")
    print("wrote: " + ", ".join(str(f) for f in files) + f" ({len(kept)} keys)")
    print(f"next: /warp-scan {folder}")
    return 0


def main(argv=None) -> int:
    import usage

    ap = argparse.ArgumentParser(prog="jira_pull.py", description="/warp-from-jira: a plan folder from Jira issues with given labels",
                                 epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="write the query and print the Jira calls")
    p.add_argument("labels", nargs="+")
    p.add_argument("--folder")
    p.add_argument("--project")
    p.add_argument("--any-project", action="store_true")
    p.add_argument("--match", choices=["any", "all"], default="any")
    p.add_argument("--jql", default="")
    p.add_argument("--force", action="store_true")
    b = sub.add_parser("build", help="turn the saved search into the plan folder")
    b.add_argument("--results")
    b.add_argument("--labels")
    b.add_argument("--folder")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--force", action="store_true")
    args = ap.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    return cmd_plan(args, root) if args.cmd == "plan" else cmd_build(args, root)


if __name__ == "__main__":
    sys.exit(main())
