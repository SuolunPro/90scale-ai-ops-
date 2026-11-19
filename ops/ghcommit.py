"""Commit small state files to this repository through the GitHub REST API.

Used because Actions workspaces are ephemeral and we deliberately avoid
`git push` races: the API returns 409 on sha mismatch, which we retry once.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from ops import common as C

API = "https://api.github.com"


def commit_files_batch(root: str, cfg: dict, files: list[str], message: str, branch: str = "main") -> list[str]:
    """单次提交多个文件（Git Data API），避免每个文件一个 commit 的噪声。"""
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "") or cfg.get("ops_repo", "")
    if not token:
        return ["本地模式：未设置 GITHUB_TOKEN，状态文件未提交"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA}

    def req(method, path, body=None):
        r = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=40) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:200]

    code, ref = req("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    if code != 200:
        return [f"读分支失败 HTTP {code}"]
    base = ref["object"]["sha"]
    code, commit = req("GET", f"/repos/{repo}/git/commits/{base}")
    if code != 200:
        return [f"读提交失败 HTTP {code}"]

    entries = []
    for rel in files:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        code, blob = req("POST", f"/repos/{repo}/git/blobs",
                         {"content": base64.b64encode(open(path, "rb").read()).decode(),
                          "encoding": "base64"})
        if code not in (200, 201):
            return [f"blob {rel} 失败 HTTP {code}"]
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    if not entries:
        return ["没有可提交的文件"]

    code, tree = req("POST", f"/repos/{repo}/git/trees",
                     {"base_tree": commit["tree"]["sha"], "tree": entries})
    if code not in (200, 201):
        return [f"建树失败 HTTP {code}"]
    code, new_commit = req("POST", f"/repos/{repo}/git/commits",
                           {"message": message, "tree": tree["sha"], "parents": [base]})
    if code not in (200, 201):
        return [f"建提交失败 HTTP {code}"]
    code, _ = req("PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
                  {"sha": new_commit["sha"], "force": False})
    return [f"batch committed {new_commit['sha'][:10]} ({len(entries)} files)"] if code in (200, 201) \
        else [f"更新分支失败 HTTP {code}"]


def delete_file(root: str, cfg: dict, rel: str, message: str) -> list[str]:
    """删除仓库中的文件（用于清理 pending 这类一次性状态）。"""
    import base64 as _b64
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "") or cfg.get("ops_repo", "")
    if not token:
        return ["本地模式：未删除远端文件"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA}
    api = f"{API}/repos/{repo}/contents/{rel}"
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=20) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        return [f"{rel}: 读取失败 HTTP {e.code}"]
    body = json.dumps({"message": message, "sha": sha, "branch": "main"}).encode()
    req = urllib.request.Request(api, data=body, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return [f"{rel}: deleted ({r.status})"]
    except urllib.error.HTTPError as e:
        return [f"{rel}: 删除失败 HTTP {e.code}"]


def commit_files(root: str, cfg: dict, files: list[str], message: str, retries: int = 2) -> list[str]:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "") or cfg.get("ops_repo", "")
    if not token:
        return ["本地模式：未设置 GITHUB_TOKEN，状态文件未提交"]
    notes = []
    for rel in files:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA}
        for attempt in range(retries + 1):
            sha = None
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        f"{API}/repos/{repo}/contents/{rel}", headers=headers), timeout=20) as r:
                    sha = json.load(r).get("sha")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    notes.append(f"{rel}: 读取 sha 失败 HTTP {e.code}")
                    break
            body = {"message": message, "branch": "main",
                    "content": base64.b64encode(open(path, "rb").read()).decode()}
            if sha:
                body["sha"] = sha
            req = urllib.request.Request(f"{API}/repos/{repo}/contents/{rel}",
                                         data=json.dumps(body).encode(), headers=headers, method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    notes.append(f"{rel}: committed ({r.status})")
                break
            except urllib.error.HTTPError as e:
                if e.code == 409 and attempt < retries:
                    time.sleep(1.5)
                    continue
                notes.append(f"{rel}: commit 失败 HTTP {e.code}")
                break
    return notes
