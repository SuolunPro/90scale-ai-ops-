"""Daily digest: one line per day in logs/daily.jsonl, committed to the repo.

Keeps a 30-day operational history without 96 commits per day, and guarantees
the repository stays active (GitHub disables scheduled workflows after 60 days
without repository activity).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # allow `python ops/daily.py` from the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import ghcommit, inspector  # noqa: E402
from ops import common as C  # noqa: E402

KEEP_LINES = 400


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--root", default=".")
    ap.add_argument("--summary", default="")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    now = C.now_utc()
    doc = inspector.build_status(cfg, now, args.root)
    markdown = inspector.build_markdown(doc)
    status_dir = os.path.join(args.root, "state")
    os.makedirs(status_dir, exist_ok=True)
    with open(os.path.join(status_dir, "daily-latest.md"), "w", encoding="utf-8") as report:
        report.write(markdown)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(markdown)
    print(markdown)
    if args.report_only:
        return 0

    line = {
        "date": now.astimezone(C.BJ).date().isoformat(),
        "generated_at": doc["generated_at"], "verdict": doc["verdict"],
        "failed": doc["failed"], "warned": doc["warned"],
        "checks": {c["id"]: c["status"] for c in doc["checks"]},
    }

    os.makedirs(os.path.join(args.root, "logs"), exist_ok=True)
    path = os.path.join(args.root, "logs", "daily.jsonl")
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    lines.append(json.dumps(line, ensure_ascii=False))
    lines = lines[-KEEP_LINES:]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    for note in ghcommit.commit_files(args.root, cfg, ["logs/daily.jsonl", "state/daily-latest.md"],
                                      f"ops: daily digest {line['date']} {line['verdict']}"):
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
