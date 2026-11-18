"""状态文件回滚：把 state 下的文件恢复成某个历史提交的内容，并生成一次新提交。

只动本仓库的 state/ 文件，不碰生产站点、不碰冻结数据。每次回滚都有提交记录可追溯。

    python ops/rollback.py --file state/latest.json --list
    python ops/rollback.py --file state/latest.json --to <commit_sha>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402


def _req(method, path, token, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def history(repo: str, token: str, path: str, limit: int = 10):
    code, doc = _req("GET", f"/repos/{repo}/commits?path={path}&per_page={limit}", token)
    if code != 200:
        return []
    return [(c["sha"], c["commit"]["committer"]["date"], c["commit"]["message"].splitlines()[0])
            for c in doc]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "SuolunPro/90scale-ai-ops-"))
    ap.add_argument("--file", default="state/latest.json")
    ap.add_argument("--to", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    token = os.getenv("OPS_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("FATAL: 缺少 token")
        return 2

    rows = history(args.repo, token, args.file)
    if args.list or not args.to:
        for sha, at, msg in rows:
            print(f"{sha[:10]} {at} {msg[:70]}")
        return 0

    code, doc = _req("GET", f"/repos/{args.repo}/contents/{args.file}?ref={args.to}", token)
    if code != 200:
        print(f"FATAL: 读取 {args.file}@{args.to[:10]} 失败 HTTP {code}")
        return 1
    content = base64.b64decode(doc["content"])

    code, cur = _req("GET", f"/repos/{args.repo}/contents/{args.file}", token)
    sha = cur.get("sha") if code == 200 else None
    body = {"message": f"ops: rollback {args.file} to {args.to[:10]}",
            "branch": "main",
            "content": base64.b64encode(content).decode()}
    if sha:
        body["sha"] = sha
    code, out = _req("PUT", f"/repos/{args.repo}/contents/{args.file}", token, body)
    print(f"rollback {args.file} -> {args.to[:10]}: HTTP {code}")
    return 0 if code in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
