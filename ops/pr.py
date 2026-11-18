"""代码修复走独立 PR，不直接改 main。

流程：建分支 → 提交文件 → 开 PR（需要 Pull requests: write）。
若 token 没有开 PR 的权限，则保留分支并打印 compare 链接，由人点一下开 PR。

这份脚本只用于**运维仓库自身**的代码修复；正式网站/私人引擎的代码一律不动。
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


def _req(method, path, token, body=None, timeout=60):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def create_pr(repo: str, token: str, branch: str, title: str, body: str,
              files: list[tuple[str, bytes]], base: str = "main") -> dict:
    code, ref = _req("GET", f"/repos/{repo}/git/ref/heads/{base}", token)
    if code != 200:
        return {"ok": False, "step": "read_base", "http": code, "detail": ref}
    base_sha = ref["object"]["sha"]
    code, commit = _req("GET", f"/repos/{repo}/git/commits/{base_sha}", token)
    if code != 200:
        return {"ok": False, "step": "read_commit", "http": code}

    entries = []
    for path, content in files:
        code, blob = _req("POST", f"/repos/{repo}/git/blobs", token,
                          {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        if code not in (200, 201):
            return {"ok": False, "step": f"blob:{path}", "http": code, "detail": blob}
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    code, tree = _req("POST", f"/repos/{repo}/git/trees", token,
                      {"base_tree": commit["tree"]["sha"], "tree": entries})
    if code not in (200, 201):
        return {"ok": False, "step": "tree", "http": code, "detail": tree}

    code, new_commit = _req("POST", f"/repos/{repo}/git/commits", token,
                            {"message": title, "tree": tree["sha"], "parents": [base_sha]})
    if code not in (200, 201):
        return {"ok": False, "step": "commit", "http": code, "detail": new_commit}

    code, newref = _req("POST", f"/repos/{repo}/git/refs", token,
                        {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]})
    if code not in (200, 201):
        return {"ok": False, "step": "branch", "http": code, "detail": newref}

    code, pr = _req("POST", f"/repos/{repo}/pulls", token,
                    {"title": title, "head": branch, "base": base, "body": body})
    if code in (200, 201):
        return {"ok": True, "pr": pr.get("html_url"), "number": pr.get("number"),
                "commit": new_commit["sha"]}
    return {"ok": False, "step": "pull_request", "http": code, "detail": pr,
            "branch": branch, "commit": new_commit["sha"],
            "compare": f"https://github.com/{repo}/compare/{base}...{branch}?expand=1"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="SuolunPro/90scale-ai-ops-")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="由 AI 运维自动生成的修复 PR，等待人工审核。")
    ap.add_argument("--files", required=True, help="逗号分隔的本地文件路径")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    token = os.getenv("OPS_TOKEN", "")
    if not token:
        print("FATAL: 缺少 OPS_TOKEN")
        return 2
    files = []
    for p in args.files.split(","):
        p = p.strip()
        full = os.path.join(args.root, p)
        with open(full, "rb") as f:
            files.append((p, f.read()))
    out = create_pr(args.repo, token, args.branch, args.title, args.body, files)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
