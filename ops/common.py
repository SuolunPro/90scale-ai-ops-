"""Shared helpers for the 90scale AI ops kit. Standard library only.

Runs unchanged on ubuntu-latest (GitHub Actions, Python 3.12) and on Windows.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

BJ = dt.timezone(dt.timedelta(hours=8))
UA = "90scale-ai-ops/1.0 (+https://github.com/SuolunPro/90scale-ai-ops-)"
SITE = "https://suolunpro.github.io"


# ---------------------------------------------------------------- time helpers
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(raw) -> dt.datetime | None:
    """Parse an ISO-ish timestamp that may be naive (assumed Beijing time)."""
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=BJ)
    return d.astimezone(dt.timezone.utc)


def age_min(ts: dt.datetime | None, ref: dt.datetime | None = None) -> float | None:
    if ts is None:
        return None
    return round(((ref or now_utc()) - ts).total_seconds() / 60.0, 1)


def in_sale_window(now: dt.datetime | None = None) -> bool:
    """Sale/prediction window in Beijing time: 09:00-22:45."""
    bj = (now or now_utc()).astimezone(BJ)
    return (9, 0) <= (bj.hour, bj.minute) <= (22, 45)


# ---------------------------------------------------------------- http helpers
def http_get(url: str, timeout: float = 20.0, retries: int = 2, headers: dict | None = None) -> dict:
    """GET a URL with retries; never raises. Returns a result dict."""
    h = {"User-Agent": UA, "Accept": "*/*", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    if headers:
        h.update(headers)
    last = ""
    for attempt in range(retries + 1):
        started = time.time()
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return {
                    "url": url,
                    "status": r.status,
                    "bytes": len(body),
                    "body": body,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "error": None,
                }
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return {
                "url": url,
                "status": e.code,
                "bytes": len(body),
                "body": body,
                "sha256": hashlib.sha256(body).hexdigest(),
                "elapsed_ms": int((time.time() - started) * 1000),
                "error": f"HTTP {e.code}",
            }
        except Exception as e:  # noqa: BLE001 - network layer, record and retry
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {
        "url": url,
        "status": 0,
        "bytes": 0,
        "body": b"",
        "sha256": "",
        "elapsed_ms": 0,
        "error": last or "unknown error",
    }


def as_json(res: dict):
    """Decode a successful JSON response body."""
    if res.get("status") != 200 or not res.get("body"):
        return None
    try:
        return json.loads(res["body"].decode("utf-8", "replace"))
    except ValueError:
        return None


# ------------------------------------------------------------------ file / hash
def load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dump_json(path: str, obj) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def git_blob_sha(content: bytes) -> str:
    """Git blob object id of a file body; lets us compare a served file with the repo copy."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(content))
    h.update(content)
    return h.hexdigest()


# --------------------------------------------------------------------- results
def result(check_id: str, title: str, status: str, severity: str, detail: str, **extra) -> dict:
    out = {
        "id": check_id,
        "title": title,
        "status": status,  # ok | warn | fail | skip
        "severity": severity,  # info | warn | critical
        "detail": detail,
    }
    out.update(extra)
    return out


def verdict_of(checks: list) -> str:
    codes = {c["status"] for c in checks}
    if "fail" in codes:
        return "FAIL"
    if "warn" in codes:
        return "WARN"
    if "ok" in codes:
        return "OK"
    return "UNKNOWN"
