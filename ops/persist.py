"""Persist state/latest.json to the repository.

The inspect workflow runs on an ephemeral runner: without this step the status
file only survives as an artifact. We commit it when it meaningfully changed,
and at least once per hour, to keep signal without 96 commits/day.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C, ghcommit  # noqa: E402

REL = "state/latest.json"
MAX_AGE_MIN = 60


def _remote(root: str, rel: str):
    """Return (sha, doc) of the file currently on main, or (None, None)."""
    import urllib.error
    import urllib.request

    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None, None
    url = f"https://api.github.com/repos/{repo}/contents/{rel}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            doc = json.load(r)
        return doc.get("sha"), json.loads(base64.b64decode(doc["content"]).decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return None, None
    except Exception:  # noqa: BLE001
        return None, None


def should_commit(local: dict, remote: dict | None, now) -> tuple[bool, str]:
    if remote is None:
        return True, "远端还没有 state/latest.json"
    if local.get("verdict") != remote.get("verdict"):
        return True, f"判定变化 {remote.get('verdict')} -> {local.get('verdict')}"
    if local.get("signature") != remote.get("signature"):
        return True, "故障签名变化"
    if local.get("fingerprint") and not remote.get("fingerprint"):
        return True, "远端快照缺少 fingerprint，升级一次"
    if local.get("fingerprint") and remote.get("fingerprint") \
            and local["fingerprint"] != remote["fingerprint"]:
        return True, "单项检查状态变化（fingerprint 不同）"
    age = C.age_min(C.parse_ts(remote.get("generated_at")), now)
    if age is None or age >= MAX_AGE_MIN:
        return True, f"远端快照已 {age or 0:.0f} 分钟未刷新，按每小时一次刷新"
    return False, f"内容无实质变化且 {age:.0f} 分钟前刚更新，跳过提交"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--file", default=REL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    local = C.load_json(os.path.join(args.root, args.file))
    if not isinstance(local, dict):
        print("FATAL: 本地巡检结果不存在")
        return 2
    sha, remote = _remote(args.root, args.file)
    now = C.now_utc()
    ok, why = (True, "--force") if args.force else should_commit(local, remote, now)
    print(f"persist: {'COMMIT' if ok else 'SKIP'} - {why}")
    if not ok:
        return 0
    notes = ghcommit.commit_files(args.root, {"ops_repo": os.getenv("GITHUB_REPOSITORY", "")},
                                  [args.file], f"ops: status {local.get('verdict')} "
                                               f"({str(local.get('signature'))[:60]})")
    for n in notes:
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
