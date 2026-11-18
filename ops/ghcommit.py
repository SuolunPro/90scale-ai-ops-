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
