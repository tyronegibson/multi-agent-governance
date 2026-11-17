#!/usr/bin/env python3
"""Validate a shared AI-agent pipeline file against its governance rules.

Several AI agents (and one human) edit the same JSON file. Written rules tell them
how to behave; this script checks that they actually did. Run it before saving,
or against the last two versions after a session:

    python3 validate.py grants.json                       # single-file checks
    python3 validate.py grants.json --previous backup.json  # + change checks

Exit code is 1 if any HIGH finding is present, else 0. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIDENCE = {"confirmed", "reported", "estimated"}
HUMAN_ONLY_STATUSES = {"submitted", "awarded"}
CYCLE_KEYS = ("current_cycle", "next_cycle")
DATE_KEYS = ("opens", "deadline")

# Patterns for things that must never be written into the shared file.
SECRET_PATTERNS = {
    "SSN-like number": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "EIN-like number": re.compile(r"\b\d{2}-\d{7}\b"),
    "API key (sk-...)": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "password field": re.compile(r'"(password|passwd|pwd)"\s*:\s*"[^"]+"', re.I),
}


@dataclass(frozen=True)
class Finding:
    severity: str   # "high" | "medium" | "low"
    rule: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper():6}] {self.rule:<22} {self.where}: {self.message}"


# ---------------------------------------------------------------- single-file checks

def check_file(data: dict) -> list[Finding]:
    findings: list[Finding] = []
    legends = data.get("legends", {})
    priorities = set(legends.get("priority", {}))
    statuses = set(legends.get("application_status", []))

    for g in data.get("grants", []):
        gid = g.get("id", "<no id>")
        where = f"grants[{gid}]"

        if priorities and g.get("priority") not in priorities:
            findings.append(Finding("medium", "known-priority", where,
                                    f"priority {g.get('priority')!r} is not in legends.priority"))
        if statuses and g.get("application_status") not in statuses:
            findings.append(Finding("medium", "known-status", where,
                                    f"application_status {g.get('application_status')!r} is not in legends"))

        if g.get("priority") == "archived" and not (g.get("notes") or "").strip():
            findings.append(Finding("medium", "archive-with-reason", where,
                                    "archived without an explanation in notes"))

        for ck in CYCLE_KEYS:
            cycle = g.get(ck) or {}
            dated = [k for k in DATE_KEYS if cycle.get(k)]
            if not dated:
                continue
            conf = cycle.get("confidence")
            if conf not in CONFIDENCE:
                findings.append(Finding("high", "dates-need-confidence", f"{where}.{ck}",
                                        f"has {', '.join(dated)} but confidence is {conf!r}"))
            if not g.get("sources"):
                findings.append(Finding("high", "dates-need-sources", f"{where}.{ck}",
                                        f"has {', '.join(dated)} but the grant lists no sources"))

    for n, entry in enumerate(data.get("changelog", [])):
        if not str(entry.get("by") or "").strip():
            findings.append(Finding("low", "log-author", f"changelog[{n}]",
                                    "entry has no \"by\" field naming the agent or person (schema drift?)"))

    raw = json.dumps(data, ensure_ascii=False)
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(raw):
            findings.append(Finding("high", "no-secrets", "file", f"contains a {label}"))

    return findings


# ---------------------------------------------------------------- change checks

def _by_id(items: list[dict]) -> dict[str, dict]:
    return {i["id"]: i for i in items if isinstance(i, dict) and "id" in i}


def _new_entries(old: list, new: list) -> list:
    return new[len(old):] if new[:len(old)] == old else []


def check_change(old: dict, new: dict) -> list[Finding]:
    findings: list[Finding] = []
    owner = (new.get("meta", {}).get("owner") or "").strip().lower()

    # Never delete: every grant and permit id that existed must still exist.
    old_grants, new_grants = _by_id(old.get("grants", [])), _by_id(new.get("grants", []))
    for gid in old_grants.keys() - new_grants.keys():
        findings.append(Finding("high", "never-delete", f"grants[{gid}]",
                                "was removed; archive it with a note instead"))
    old_permits = _by_id(old.get("access_and_permits", {}).get("permits", []))
    new_permits = _by_id(new.get("access_and_permits", {}).get("permits", []))
    for pid in old_permits.keys() - new_permits.keys():
        findings.append(Finding("high", "never-delete", f"permits[{pid}]",
                                "was removed; archive it with a note instead"))

    # Append-only history: earlier changelog and review entries are never rewritten.
    for key in ("changelog", "reviews"):
        o, n = old.get(key, []), new.get(key, [])
        if n[:len(o)] != o:
            findings.append(Finding("high", "append-only", key,
                                    "earlier entries were edited, reordered or removed"))

    new_log = _new_entries(old.get("changelog", []), new.get("changelog", []))
    authors = {str(e.get("by", "")).strip().lower() for e in new_log}

    # Human-only actions: only the owner can mark something submitted or awarded.
    for gid, g in new_grants.items():
        before = old_grants.get(gid, {}).get("application_status")
        after = g.get("application_status")
        if after in HUMAN_ONLY_STATUSES and after != before and owner not in authors:
            findings.append(Finding("high", "human-only-status", f"grants[{gid}]",
                                    f"changed to {after!r} in a session not logged by the owner"))

    # Every session is logged.
    body_old = {k: v for k, v in old.items() if k not in ("meta", "changelog")}
    body_new = {k: v for k, v in new.items() if k not in ("meta", "changelog")}
    if body_old != body_new:
        if not new_log:
            findings.append(Finding("high", "log-every-session", "changelog",
                                    "content changed but no changelog entry was appended"))
        if old.get("meta", {}).get("last_updated") == new.get("meta", {}).get("last_updated"):
            findings.append(Finding("low", "bump-last-updated", "meta",
                                    "content changed but meta.last_updated did not"))

    return findings


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--previous", type=Path, help="the version before this session, for change checks")
    args = ap.parse_args(argv)

    new = json.loads(args.file.read_text())
    findings = check_file(new)
    if args.previous:
        findings += check_change(json.loads(args.previous.read_text()), new)

    order = {"high": 0, "medium": 1, "low": 2}
    for f in sorted(findings, key=lambda f: order[f.severity]):
        print(f)
    high = sum(f.severity == "high" for f in findings)
    print(f"\n{len(findings)} finding(s), {high} high.")
    return 1 if high else 0


if __name__ == "__main__":
    sys.exit(main())
