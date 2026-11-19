"""Fail-closed live-sale verifier for qiulaile; never infer sale from kickoff alone."""
from __future__ import annotations
import datetime as dt
import json
import urllib.request
from zoneinfo import ZoneInfo

URL = "https://qiulaile.vip/api/matches?matchType=0&mode=full"
BJ = ZoneInfo("Asia/Shanghai")
TRUE_STATES = {"on_sale", "onsale", "selling", "sale", "在售", "销售中", "开售"}
FALSE_STATES = {"停售", "已停售", "closed", "off_sale", "offsale", "未开售", "已结束"}

def _date(value):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), dt.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T"))
        return d if d.tzinfo else d.replace(tzinfo=BJ)
    except ValueError:
        return None

def _rows(data):
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        for key in ("matches", "matchList", "list", "rows", "data", "result"):
            if key in data:
                yield from _rows(data[key])

def verify(now=None, url=URL):
    now = now or dt.datetime.now(dt.timezone.utc)
    req = urllib.request.Request(url, headers={"Referer": "https://qiulaile.vip/",
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status != 200:
                return {"verified": False, "reason": f"HTTP {response.status}"}
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            return {"verified": False, "reason": "response too large"}
        doc = json.loads(raw)
    except Exception as exc:
        return {"verified": False, "reason": type(exc).__name__}
    rows = list(_rows(doc))
    for row in rows:
        ident = next((row.get(k) for k in ("matchId", "match_id", "id", "source_id") if row.get(k)), None)
        home = next((row.get(k) for k in ("homeTeam", "home_team", "homeName") if row.get(k)), None)
        away = next((row.get(k) for k in ("awayTeam", "away_team", "awayName") if row.get(k)), None)
        status = next((row.get(k) for k in ("saleStatus", "sale_status", "sellingStatus") if row.get(k) is not None), None)
        kickoff = _date(next((row.get(k) for k in ("kickoff", "matchTime", "startTime") if row.get(k)), None))
        cutoff = _date(next((row.get(k) for k in ("saleEnd", "sale_end", "stopSaleTime", "saleEndTime") if row.get(k)), None))
        # Explicit sale flag, independently parsed deadline and kickoff, and identity are mandatory.
        if not (ident and isinstance(home, str) and home.strip() and isinstance(away, str) and away.strip() and kickoff and cutoff):
            continue
        active = status is True or (isinstance(status, str) and status.strip().lower() in TRUE_STATES)
        if active and now < kickoff and now < cutoff:
            return {"verified": True, "match_id": str(ident), "home": home,
                    "away": away, "kickoff": kickoff.isoformat(), "sale_end": cutoff.isoformat()}
    return {"verified": False, "reason": f"no explicitly selling fixture with verified deadline ({len(rows)} rows)"}
