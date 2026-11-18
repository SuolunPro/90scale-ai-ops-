"""生产代码修复通道：测试通过 → 独立分支 → 提交 → 开 PR（等人工审核，绝不合并）。

与 ops/recovery.py 的区别：
  recovery  = 运维动作（只触发已有工作流，不碰代码）
  fix_pr    = 代码修改（必须 PR，必须人工审核）

用法（在仓库根目录）：
    python ops/fix_pr.py --branch fix/collector-timeout --title "fix: 采集超时重试" \
        --files ops/checks.py,config/ops.json --body "说明"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402
from ops.pr import create_pr  # noqa: E402


def run_tests(root: str) -> tuple[bool, str]:
    proc = subprocess.run([sys.executable, os.path.join(root, "ops", "selftest.py")],
                          cwd=root, capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.splitlines()[-6:])
    return proc.returncode == 0, tail


def open_pr_from_branch(repo: str, token: str, head: str, title: str, body: str,
                        base: str = "main") -> dict:
    """分支已有提交时，只开 PR（不重复提交内容）。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls",
        data=json.dumps({"title": title, "head": head, "base": base, "body": body}).encode(),
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
            return {"ok": True, "pr": d.get("html_url"), "number": d.get("number")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code,
                "detail": e.read().decode("utf-8", "replace")[:200],
                "compare": f"https://github.com/{repo}/compare/{base}...{head}?expand=1"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "SuolunPro/90scale-ai-ops-"))
    ap.add_argument("--branch", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="由 AI 运维生成的修复 PR，需人工审核。")
    ap.add_argument("--files", default="", help="逗号分隔，相对仓库根目录")
    ap.add_argument("--head-branch", default="", help="分支已有提交时：直接用它开 PR")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    files = [p.strip() for p in args.files.split(",") if p.strip()]
    for rel in files:
        if not os.path.exists(os.path.join(args.root, rel)):
            print(f"FATAL: 文件不存在 {rel}")
            return 2
    if not files and not args.head_branch:
        print("FATAL: 需要 --files 或 --head-branch")
        return 2

    ok, tail = (True, "跳过测试") if args.skip_tests else run_tests(args.root)
    print("测试结果：", "PASS" if ok else "FAIL")
    print(tail)
    if not ok:
        print("REFUSED: 测试未通过，不创建 PR")
        return 1

    body = (f"{args.body}\n\n### 自动化测试\n\n```\n{tail}\n```\n\n"
            f"本 PR 由 AI 运维生成，**不会自动合并**，请人工审核后处理。\n文件：{files}")
    if args.dry_run:
        print(f"[dry] branch={args.branch} files={files}")
        return 0

    # 同仓库 PR 用 GITHUB_TOKEN 即可（需要 workflow 里声明 pull-requests: write）
    same_repo = args.repo == os.getenv("GITHUB_REPOSITORY", "")
    token = (os.getenv("GITHUB_TOKEN") if same_repo else "") or os.getenv("OPS_TOKEN") \
        or os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("FATAL: 缺少 OPS_TOKEN")
        return 2
    if args.head_branch:
        out = open_pr_from_branch(args.repo, token, args.head_branch, args.title, body)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    payload = [(rel, open(os.path.join(args.root, rel), "rb").read()) for rel in files]
    out = create_pr(args.repo, token, args.branch, args.title, body, payload)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
