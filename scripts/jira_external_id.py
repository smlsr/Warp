#!/usr/bin/env python3
"""Write plan ids into Jira's External ID field for tickets that are already mapped.

Warp has no Jira credentials. Dry-run lists each pair. --apply --yes prints one
MUST DO block. The agent calls editJiraIssue, then jira_sync.py record-external-id.
jiraWriteExternalId makes /warp-scan and /warp-jira-check queue the same block
without --yes. This command works either way once --apply --yes is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jira_match

HELP = """
examples:
  python3 scripts/jira_external_id.py ?
  python3 scripts/jira_external_id.py
  python3 scripts/jira_external_id.py --ticket WV-01
  python3 scripts/jira_external_id.py --apply --yes
  python3 scripts/jira_external_id.py --results seen.json
  python3 scripts/jira_external_id.py --results edits.json --apply --yes
  python3 scripts/jira_sync.py record-external-id --results edits.json

Dry-run is the default. Each line is WV-01 -> WAR-1: External ID currently
<value|empty|unknown|field missing> -> would set WV-01. --apply --yes queues
editJiraIssue for every mapped ticket whose External ID is not yet confirmed
equal to the plan id. A key that came from the external-id search is already
equal and is skipped. A ticket with no confirmed key is skipped. A second run
sees jira.externalIdWritten and does not send the edit again. --force queues
those tickets again. An equal value is not an error. --force-external-id
replaces a different non-empty value and shows before and after.

The field is discovered from getJiraProjectIssueTypesMetadata and confirmed
with getJiraIssueEditmeta on each issue. A missing or read-only field is
skipped. Nothing posts a Jira comment. editJiraIssue is a write and needs
/warp-allow-notify --with-jira.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main(argv: list[str] | None = None) -> int:
    import usage

    parser = argparse.ArgumentParser(
        description="Write mapped plan ids into the Jira External ID field",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--ticket", help="one plan id, for example WV-01")
    parser.add_argument("--results", help="issue transcript or editJiraIssue results")
    parser.add_argument("--apply", action="store_true", help="queue editJiraIssue for each pair that should change")
    parser.add_argument("--yes", action="store_true", help="confirm --apply")
    parser.add_argument("--dry-run", action="store_true", help="list pairs and write nothing")
    parser.add_argument("--force", action="store_true", help="queue tickets that were already recorded")
    parser.add_argument("--force-external-id", action="store_true", help="replace a different non-empty External ID")
    args = parser.parse_args(usage.normalize_argv(argv, {"--apply"}))
    beam = Path(args.beam)
    if not beam.is_absolute():
        beam = Path.cwd() / beam
    only = {args.ticket} if args.ticket else None
    must = bool(args.apply and args.yes and not args.dry_run)
    if args.results:
        try:
            data = json.loads(Path(args.results).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"jira: could not read results: {exc}")
            return 2
        if not isinstance(data, dict):
            print("jira: the transcript must be a JSON object")
            return 2
        print(
            jira_match.record_external_id(
                beam,
                data,
                force=bool(args.force),
                force_value=bool(args.force_external_id),
                save=must,
            )
        )
        if must and not (data.get("edits") or []):
            print(
                jira_match.external_id_backfill(
                    beam,
                    mode="must",
                    only_ids=only,
                    force=bool(args.force),
                    force_value=bool(args.force_external_id),
                )
            )
        return 0
    print(
        jira_match.external_id_backfill(
            beam,
            mode="must" if must else "dry",
            only_ids=only,
            force=bool(args.force),
            force_value=bool(args.force_external_id),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
