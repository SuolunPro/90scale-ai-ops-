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

from ops import checks, ghcommit  # noqa: E402
from ops import common as C  # noqa: E402

KEEP_LINES = 400


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    now = C.now_utc()
    results = checks.run_all(cfg, now=now)
    verdict = C.verdict_of(results)
    failed = [c["id"] for c in results if c["status"] == "fail"]
    warned = [c["id"] for c in results if c["status"] == "warn"]

    line = {
        "date": now.astimezone(C.BJ).strftime("%Y-%m-%d"),
        "generated_at": C.iso(now),
        "verdict": verdict,
        "failed": failed,
        "warned": warned,
        "checks": {c["id"]: c["status"] for c in results},
        "site_bytes": next((c.get("prod_bytes") for c in results if c["id"] == "site_data_sync"), None),
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

    C.dump_json(os.path.join(args.root, "state", "latest.json"),
                {"schema": 1, "generated_at": line["generated_at"], "verdict": verdict,
                 "signature": ",".join(sorted(failed)) or ",".join(sorted(warned)) or "OK",
                 "failed": failed, "warned": warned, "checks": results,
                 "budget": {"spent_cny": None}})

    print(json.dumps(line, ensure_ascii=False))
    for note in ghcommit.commit_files(args.root, cfg, ["logs/daily.jsonl", "state/latest.json"],
                                      f"ops: daily digest {line['date']} {verdict}"):
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
