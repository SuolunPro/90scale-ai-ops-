"""只读权限审计：在不触发任何生产动作的前提下，探测 token 的实际能力。

禁止：dispatch 工作流、写文件、改分支。只用 GET。
输出只包含 HTTP 状态码与仓库名，不包含任何 token 片段。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402

PROBES = [
    ("identify_user", "/user"),
    ("ops_repo", "/repos/SuolunPro/90scale-ai-ops-"),
    ("engine_repo", "/repos/SuolunPro/90scale-engine"),
    ("engine_workflows_read", "/repos/SuolunPro/90scale-engine/actions/workflows"),
    ("engine_runs_read", "/repos/SuolunPro/90scale-engine/actions/runs?per_page=1"),
    ("engine_contents_read", "/repos/SuolunPro/90scale-engine/contents/data/engine4/latest.json"),
    ("site_repo", "/repos/SuolunPro/suolunpro.github.io"),
    ("site_runs_read", "/repos/SuolunPro/suolunpro.github.io/actions/runs?per_page=1"),
    ("ops_pulls_read", "/repos/SuolunPro/90scale-ai-ops-/pulls?state=open&per_page=5"),
]


def probe(token: str, path: str) -> dict:
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            info = {}
            if path == "/user":
                doc = json.loads(body)
                info = {"login": doc.get("login"), "type": doc.get("type")}
            elif path.endswith("/pulls?state=open&per_page=5"):
                info = {"open_prs": len(json.loads(body))}
            return {"status": r.status, "ok": True, **info}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ok": False, "error": e.read().decode("utf-8", "replace")[:120]}


def main() -> int:
    token = os.getenv("ENGINE4_REPAIR_TOKEN", "")
    if not token:
        print("FATAL: 未注入 ENGINE4_REPAIR_TOKEN")
        return 2
    print(f"token length: {len(token)}")
    rows = []
    for name, path in PROBES:
        res = probe(token, path)
        rows.append({"probe": name, "path": path, **res})
        print(f"{name:26} {res['status']:>4}  {json.dumps({k: v for k, v in res.items() if k not in ('path',)}, ensure_ascii=False)[:150]}")
    C.dump_json("state/perm/audit.json", {"at": C.iso(C.now_utc()), "rows": rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
