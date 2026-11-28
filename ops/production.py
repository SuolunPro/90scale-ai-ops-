"""Read-only adapters for the public production runner and its published evidence."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import re
from urllib.parse import urlencode

from ops import common as C

PUBLIC = "SuolunPro/suolunpro.github.io"
WRITER = "build-engine4.yml"
AUDIT = "public-refresh-audit.yml"
SETTLEMENT = "supabase-settlement-publisher.yml"
STAGING = "https://zssourkgabcxibggwhqm.supabase.co/functions/v1/ninety-public-settlement-staging-v1"


def published_result_evidence(day):
    doc = C.as_json(C.http_get(f"{STAGING}?sale_day={day}", timeout=10, retries=0))
    if not isinstance(doc, dict) or doc.get("sale_day") != day or doc.get("mode") != "verified_staging_not_official":
        return {}
    rows = doc.get("results") or []
    counts = Counter(str(r.get("match_no")) for r in rows)
    return {str(r["match_no"]): r for r in rows if counts[str(r.get("match_no"))] == 1 and "match_no" in r}


def same_result_fixture(fixture, evidence):
    pairs = [(fixture.get(a), evidence.get(b)) for a, b in
             (("source_id", "qiulaile_id"), ("fotmob_match_id", "fotmob_match_id"))
             if fixture.get(a) and evidence.get(b)]
    return bool(pairs) and all(str(a) == str(b) for a, b in pairs)


def api(repo, path, token=None):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return C.as_json(C.http_get(f"https://api.github.com/repos/{repo}/{path}",
                               timeout=12, retries=0, headers=headers))


def raw(repo, path, ref="main", token=None):
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    headers = None
    if token:
        url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw+json"}
    return C.http_get(url, timeout=12, retries=0, headers=headers)


def runs(repo, workflow, start, token=None):
    prefix = f"actions/workflows/{workflow}/runs" if workflow else "actions/runs"
    query = {"per_page": 100, "created": ">=" + C.iso(start)}
    records = {}
    for page in range(1, 11):
        doc = api(repo, prefix + "?" + urlencode({**query, "page": page}), token)
        if not isinstance(doc, dict) or not isinstance(doc.get("workflow_runs"), list):
            return None
        rows = doc["workflow_runs"]
        records.update({r["id"]: r for r in rows})
        if len(rows) < 100:
            return list(records.values())
    return None


def outcome(check, title, status, detail, **extra):
    severity = {"FAIL": "critical", "WARN": "warn"}.get(status, "info")
    return C.result(check, title, status, severity, detail, **extra)


def daily_cutoff(local):
    return local.replace(hour=23 if local.weekday() >= 5 else 22, minute=0, second=0, microsecond=0)


def sale_state(now, receipt):
    local = now.astimezone(C.BJ)
    end = daily_cutoff(local)
    if local >= end:
        return "CLOSED"
    if not receipt or receipt.get("error"):
        return "UNKNOWN"
    age = C.age_min(C.parse_ts(receipt.get("checked_at")), now)
    if age is None or not 0 <= age <= 45:
        return "UNKNOWN"
    live = receipt.get("verified_live") or {}
    if any((C.parse_ts(row[4]) or now) > now for row in live.values() if len(row) >= 5):
        return "OPEN"
    if live or ((receipt.get("integrity") or {}).get("source") or {}).get("official_confirmed"):
        return "CLOSED"
    return "UNKNOWN"


def inventory(doc, day):
    rows = [row for row in doc.get("matches", []) if row.get("sale_day", doc.get("sale_day")) == day]
    nums = [str(row.get("match_no", "")) for row in rows]
    counts = Counter(nums)
    return {
        "numbers": nums, "ordered": nums == sorted(nums),
        "duplicates": sorted(n for n, count in counts.items() if count > 1),
        "invalid": sorted(n for n in counts if not re.fullmatch(r"[0-9]{3}", n)),
        "pending": [n for n, row in zip(nums, rows) if not row.get("prediction")
                    or row.get("collection_status") == "PENDING_DATA"
                    or row["prediction"].get("status") == "PENDING_DATA"],
    }


def numbers(quotes, length=3, minimum=0):
    try:
        return len(quotes) == length and all(minimum < float(q) < 10000 for q in quotes)
    except (TypeError, ValueError):
        return False


def quote_state(pair, captured, now, kickoff, *, asian=False, open_for_refresh=True):
    def valid(value):
        if not isinstance(value, str):
            return False
        if not asian:
            return numbers(value.split("/"), minimum=1)
        parts = value.split()
        return (len(parts) == 3 and numbers([parts[0], parts[2]], length=2)
                and bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?", parts[1])))

    if not all(valid(pair.get(field)) for field in ("initial", "current")):
        return "missing"
    if captured and kickoff and captured >= kickoff:
        return "invalid"
    if open_for_refresh and captured and kickoff and now < kickoff and now - captured > timedelta(hours=3):
        return "stale"
    if not captured:
        return "unverified"
    return "available"


def fixture_sources(row, now, matching_failure=False, cutoff=None):
    market = row.get("market") or {}
    betfair = row.get("bifa") or {}
    xg = row.get("fotmob_xg") or {}
    kickoff = C.parse_ts(row.get("kickoff"))
    sale_day = C.parse_ts(row.get("sale_day"))
    if cutoff is None and sale_day:
        cutoff = daily_cutoff(sale_day.astimezone(C.BJ))
    open_for_refresh = not (row.get("frozen") or row.get("frozen_at") or row.get("settlement")
                            or (cutoff and now >= cutoff))
    market_at = C.parse_ts(market.get("captured_at"))
    bf_at = C.parse_ts(betfair.get("captured_at"))
    bf_rows = betfair.get("index_rows") or []
    xg_reason = str(xg.get("reason", ""))
    bf_status = "missing"
    if betfair:
        bf_status = "unverified"
        if kickoff and bf_at and bf_at >= kickoff:
            bf_status = "invalid"
        elif (betfair.get("prematch_verified") is True and kickoff and bf_at
              and len(bf_rows) == 3 and all(isinstance(r.get("bfMarketIndex"), (int, float)) for r in bf_rows)):
            bf_status = ("stale" if open_for_refresh and now < kickoff and now - bf_at > timedelta(hours=3)
                         else "available")
    market_options = {"open_for_refresh": open_for_refresh}
    return {
        "体彩普通SP": "available" if numbers(row.get("spf"), minimum=1) else "missing",
        "体彩让球SP": "available" if numbers(row.get("rqspf"), minimum=1) and row.get("handicap") is not None else "missing",
        "William Hill": quote_state(market.get("william") or {}, market_at, now, kickoff, **market_options),
        "澳门亚盘及水位": quote_state((market.get("asia") or {}).get("澳门") or {}, market_at, now, kickoff, asian=True, **market_options),
        "澳门大小球及水位": quote_state((market.get("over_under") or {}).get("澳门") or {}, market_at, now, kickoff, asian=True, **market_options),
        "必发指数": bf_status,
        "FotMob赛事匹配": ("available" if row.get("fotmob_match_id") else
                           "match_failed" if "匹配" in xg_reason or "映射" in xg_reason else "missing"),
        "FotMob xG": ("available" if xg.get("status") == "AVAILABLE"
                      and numbers([xg.get("home_lambda"), xg.get("away_lambda")], length=2) else "missing"),
        "赛事身份核验": "match_failed" if matching_failure else "available" if row.get("source_id") else "unverified",
    }


def settlement_state(row, now, overdue_hours, evidence=None):
    verification = row.get("result_verification") or {}
    settlement = row.get("settlement") or {}
    event = row.get("event_status") or row.get("match_status") or {}
    if not isinstance(event, dict):
        event = {"status": event}
    statuses = {str(value).upper() for value in (row.get("status"), verification.get("status"),
                verification.get("event_status"), event.get("status"), settlement.get("status"))}
    current = statuses | {str(event.get("code", "")).upper()}
    source = str(event.get("source") or verification.get("source") or "").lower()
    official = (event.get("official_confirmed") is True or verification.get("official_confirmed") is True
                or source in {"sporttery", "sporttery_official", "sporttery.cn", "体彩官方"})
    if settlement.get("ft") and "VERIFIED" in statuses:
        return "SETTLED"
    if official and current & {"VOID", "OFFICIAL_VOID", "作废"}:
        return "VOID"
    if current & {"POSTPONED", "PP", "延期"}:
        return "POSTPONED"
    if current & {"CANCELLED", "CANCELED", "取消"}:
        return "CANCELLED"
    if current & {"ABANDONED", "ABD", "腰斩"}:
        return "ABANDONED"
    kickoff = C.parse_ts(row.get("kickoff"))
    if kickoff and now < kickoff:
        return "NOT_STARTED"
    if current & {"IN_PROGRESS", "LIVE", "HT"}:
        return "IN_PROGRESS"
    evidence = evidence or {}
    finished_independent = (same_result_fixture(row, evidence)
                            and evidence.get("independent_verified") is True and evidence.get("ft"))
    if finished_independent or current & {"FT", "FINISHED", "COMPLETED", "PENDING_SETTLEMENT", "完赛"}:
        finished = C.parse_ts(event.get("finished_at") or verification.get("finished_at")
                              or (evidence.get("calculated_at") if finished_independent else None))
        if finished is None and kickoff:
            finished = kickoff + timedelta(hours=2)
        return "OVERDUE" if finished and now - finished >= timedelta(hours=overdue_hours) else "FINISHED_PENDING"
    return "UNKNOWN"


def receipt_recovery(receipts, main, pages, sync_status):
    """Require an accepted public action followed by matching published data."""
    if not receipts:
        return "UNKNOWN", []
    recovered, unresolved = [], []
    for proof in receipts:
        slot = C.parse_ts(proof.get("slot"))
        day = slot.astimezone(C.BJ).date().isoformat() if slot else None
        if not day or day != main.get("sale_day") or main.get("sale_day") != pages.get("sale_day"):
            continue
        req = proof.get("betfair_repair") or {}
        integrity = proof.get("integrity") or {}
        refresh = proof.get("decision") == "dispatched" and proof.get("dispatch_receipt")
        publication = integrity.get("sync_requested") and integrity.get("sync_result") not in (None, "", "dispatch failed")
        bf_requested = req.get("accepted") is True and str(req.get("requested", "")).startswith(day + ":")
        if not (refresh or publication or bf_requested):
            continue
        deployed_at = C.parse_ts(pages.get("generated_at"))
        checked = C.parse_ts(proof.get("checked_at"))
        observed = set(inventory(pages, day)["numbers"]) - set(inventory(pages, day)["pending"])
        expected = {str(row[0]) for row in (proof.get("verified_live") or {}).values() if row}
        requested = set(req["requested"].split(":", 1)[1].split(",")) if bf_requested else set()
        accepted_bf = {str(m.get("match_no")) for m in pages.get("matches", [])
                       if fixture_sources(m, deployed_at or checked)["必发指数"] in {"available", "stale"}}
        verified = (sync_status == "PASS" and checked and deployed_at
                    and (not refresh or (deployed_at >= max(slot, checked) and expected and expected <= observed))
                    and (not bf_requested or (deployed_at >= checked and requested <= accepted_bf))
                    and (not publication or deployed_at >= slot))
        record = {"audit_id": str(proof["run_id"]), "published_at": pages.get("generated_at"),
                  "refresh": proof.get("dispatch_receipt") if refresh else None,
                  "requested_betfair": sorted(requested), "missing_betfair": sorted(requested - accepted_bf),
                  "missing_forecast": sorted(expected - observed)}
        (recovered if verified else unresolved).append(record)
    return ("WARN" if unresolved else "RECOVERED" if recovered else "SKIP",
            {"recovered": recovered, "unverified": unresolved})


def audit(cfg, now, root="."):
    from ops import handoff

    day = now.astimezone(C.BJ).date()
    start = now.astimezone(C.BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    token = os.getenv("ENGINE4_READ_TOKEN")
    private = cfg["private_audit_repo"]
    site = cfg["site"]
    raw_at = C.iso(now)
    actions_link = f"https://github.com/{PUBLIC}/actions"
    data = "data/engine4"
    docs, runs_by = {}, {}
    paths = ["latest", *[f"sales/{day + timedelta(days=delta)}" for delta in (-2, -1, 0, 1)]]
    page_url = lambda name: f"{site}/{data}/{name}.json"
    with ThreadPoolExecutor(max_workers=14) as pool:
        requests = {name: pool.submit(C.http_get, page_url(name) + "?ops=" + str(int(now.timestamp())),
                                     timeout=14, retries=0) for name in paths}
        head = pool.submit(api, PUBLIC, "git/ref/heads/main")
        intelligence_request = pool.submit(raw, PUBLIC, f"{data}/intelligence/{day}.json")
        staging = {name: pool.submit(published_result_evidence, name.split("/")[-1])
                   for name in paths[1:-1]}
        run_requests = {name: pool.submit(runs, PUBLIC, name, start) for name in (WRITER, AUDIT, SETTLEMENT)}
        private_runs = pool.submit(runs, private, None, start, token) if token else None
        for name, req in requests.items():
            docs[name] = C.as_json(req.result()) or {}
        for name, req in run_requests.items():
            runs_by[name] = req.result()
        ref_doc = head.result() or {}
        ref = (ref_doc.get("object") or {}).get("sha")
        latest, main = docs["latest"], {}
        same_day = str(latest.get("sale_day") or day)
        main_names = ["latest", f"sales/{same_day}"]
        if f"sales/{same_day}" not in docs and re.fullmatch(r"\d{4}-\d{2}-\d{2}", same_day):
            docs[f"sales/{same_day}"] = C.as_json(C.http_get(page_url(f"sales/{same_day}"), timeout=12, retries=0)) or {}
        if ref and re.fullmatch(r"\d{4}-\d{2}-\d{2}", same_day):
            requested = {name: pool.submit(raw, PUBLIC, f"{data}/{name}.json", ref) for name in main_names}
            main = {name: C.as_json(future.result()) or {} for name, future in requested.items()}
        audit_runs = [r for r in (runs_by[AUDIT] or []) if r.get("conclusion") == "success"]
        ordered = sorted(audit_runs, key=lambda r: r["created_at"], reverse=True)[:12]
        receipts = []
        if token:
            incoming = [pool.submit(raw, private, f"diagnostics/engine4-refresh/{r['id']}-{r.get('run_attempt', 1)}.json",
                                    token=token) for r in ordered]
            receipts = [d for f in incoming if (d := C.as_json(f.result())) and not d.get("error")]
        residual = private_runs.result() if private_runs else None
        result_evidence = {name: future.result() for name, future in staging.items()}
        intelligence = C.as_json(intelligence_request.result()) or {}

    receipt = receipts[0] if receipts else {}
    source = ((receipt.get("integrity") or {}).get("source") or {})
    source_link = f"{actions_link}/runs/{receipt['run_id']}" if receipt else f"https://github.com/{PUBLIC}/actions/workflows/{AUDIT}"
    sale = sale_state(now, receipt)
    writer_runs = runs_by[WRITER]
    counts = Counter(("running" if r.get("status") != "completed" else r.get("conclusion") or "unknown")
                     for r in writer_runs or [])
    successful = [r for r in writer_runs or [] if r.get("conclusion") == "success"]
    latest_success = max(successful, key=lambda r: r["updated_at"], default={})
    generated = C.parse_ts((main.get("latest") or {}).get("generated_at"))
    main_fresh = (generated and generated <= now and (main.get("latest") or {}).get("matches"))
    slot = C.parse_ts(receipt.get("slot"))
    missed = (sale == "OPEN" and slot and now - slot >= timedelta(minutes=25)
              and not receipt.get("busy") and writer_runs is not None
              and not counts["running"] and main_fresh and generated < slot)
    eng_status = "FAIL" if missed else "PASS" if writer_runs is not None and main_fresh else "UNKNOWN"
    fresh_status = "SKIP" if sale == "CLOSED" else "FAIL" if missed else (
        "PASS" if sale == "OPEN" and main_fresh and slot and generated >= slot else "UNKNOWN")
    history = [r for r in writer_runs or [] if r.get("conclusion") in {"failure", "cancelled", "timed_out"}]
    out = [
        outcome("engine_runs", "公开主引擎", eng_status,
                f"今日 {len(writer_runs)} 次，成功 {counts['success']}、失败 {counts['failure'] + counts['timed_out']}、取消 {counts['cancelled']}、运行或排队 {counts['running']}。最近成功结束 {latest_success.get('updated_at') or '未知'}"
                if writer_runs is not None else "Actions 运行记录不可用",
                runs_today=len(writer_runs) if writer_runs is not None else None,
                counts=dict(counts), last_success=latest_success.get("updated_at"), link=latest_success.get("html_url") or f"https://github.com/{PUBLIC}/actions/workflows/{WRITER}", historical_failures=[r.get("html_url") for r in history],
                checked_at=raw_at),
        outcome("engine_fresh", "在售预测刷新", fresh_status,
                f"销售窗口 {sale}，公开 main 快照 {C.iso(generated) if generated else '未知'}，最近有证据的生产计划点 {receipt.get('slot') or '未知'}",
                sale_state=sale, generated_at=C.iso(generated) if generated else None,
                receipt_at=receipt.get("checked_at"), link=source_link),
    ]

    inventories = {name: inventory(doc, same_day) for name, doc in
                   {**docs, **{"main/" + k: v for k, v in main.items()}}.items()
                   if doc.get("sale_day") == same_day}
    official = set(map(str, source.get("official_open") or []))
    board = set(map(str, source.get("board_open") or []))
    expected = {str(row[0]) for item in receipts
                if ((item.get("integrity") or {}).get("sale_day")) == same_day
                for row in (item.get("verified_live") or {}).values() if row}
    if (receipt.get("integrity") or {}).get("sale_day") == same_day:
        expected |= official
    defects = {name: {**rows, "missing": sorted(expected - set(rows["numbers"]))}
               for name, rows in inventories.items()}
    pool_issue = any(r["missing"] or r["duplicates"] or r["invalid"] or not r["ordered"] for r in defects.values())
    full_latest = (inventories.get("latest") or {}).get("numbers", [])
    if any(rows["numbers"] != full_latest for name, rows in inventories.items() if name != "latest"):
        pool_issue = True
    if (latest.get("metrics") or {}).get("total") not in (None, len(full_latest)):
        pool_issue = True
    latest_n = len(full_latest)
    pending = (inventories.get("latest") or {}).get("pending", [])
    pool_state = ("FAIL" if pool_issue else "WARN" if pending else "PASS"
                  if latest_n and source.get("official_confirmed") and sale == "OPEN"
                  and (receipt.get("integrity") or {}).get("sale_day") == same_day else "UNKNOWN")
    out.append(outcome("pool_coverage", "赛事池完整性", pool_state,
                       f"{same_day} 正式站 {latest_n} 场，待预测 {len(pending)} 场，已取证在售赛号 {len(expected)} 个"
                       + ("，存在赛号差异、重复或乱序" if pool_issue else "")
                       + ("" if source.get("official_confirmed") else "，体彩官方名单待确认"),
                       inventory=defects, official=sorted(official), board=sorted(board),
                       official_confirmed=bool(source.get("official_confirmed")), link=source_link,
                       checked_at=receipt.get("checked_at")))

    rejections = {str(r.get("number")) for r in source.get("rejections", []) if r.get("reason") == "IDENTITY_CONFLICT"}
    coverage = {}
    proven_cutoffs = {}
    for proof in receipts:
        receipt_day = (proof.get("integrity") or {}).get("sale_day")
        for item in (proof.get("verified_live") or {}).values():
            if len(item) >= 5 and (cutoff := C.parse_ts(item[4])):
                proven_cutoffs.setdefault((receipt_day, str(item[0])), cutoff)
    source_tables = {"latest": latest, **{name: doc for name, doc in docs.items()
                    if name in {f"sales/{day}", f"sales/{day + timedelta(days=1)}"}
                    and doc.get("sale_day") != latest.get("sale_day")}}
    for name, doc in source_tables.items():
        for row in doc.get("matches", []):
            fixture = f"{doc['sale_day']} / {row.get('match_no')}"
            number = str(row.get("match_no"))
            cutoff = proven_cutoffs.get((doc["sale_day"], number))
            freshness_row = {**row, "sale_day": row.get("sale_day") or doc["sale_day"]}
            for field, state in fixture_sources(freshness_row, now,
                                               name == "latest" and number in rejections, cutoff).items():
                coverage.setdefault(field, {}).setdefault(state, []).append(fixture)
    bf = coverage.get("必发指数", {})
    src_state = ("FAIL" if latest_n and not (bf.get("available") or bf.get("stale"))
                 else "WARN" if any(v.get("match_failed") for v in coverage.values())
                 or any(bf.get(s) for s in ("missing", "unverified", "invalid"))
                 or (sale == "OPEN" and any(coverage.get(k, {}).get(s) for k in
                     ("体彩普通SP", "体彩让球SP", "William Hill", "澳门亚盘及水位")
                     for s in ("missing", "stale", "invalid")))
                 else "PASS" if coverage else "UNKNOWN")
    current_bf = [item for item in (bf.get("missing", []) + bf.get("invalid", []) + bf.get("unverified", []))
                  if item.startswith(str(day) + " / ")]
    betfair_receipt = receipt.get("betfair") or {}
    ticket_bf = {
        "frozen_missing": current_bf if sale == "CLOSED" else [],
        "eligible_missing": (betfair_receipt.get("private") or {}).get("eligible_missing", []) if sale == "OPEN" else [],
        "publication_issues": {k: x["betfair_lost_after_prediction"] for k, x in
                               (receipt.get("snapshot_issues") or {}).items()
                               if x.get("betfair_lost_after_prediction")},
    }
    out.append(outcome("sources", "逐场采集与匹配", src_state,
                       "统计来自正式快照。HTTP 可达性独立记录，资料时效以采集时间为准。"
                       if coverage else "没有可核验的逐场快照", coverage=coverage, betfair_breakdown=ticket_bf,
                       link=page_url("latest"), checked_at=latest.get("generated_at")))

    settled = {status: [] for status in ("SETTLED", "NOT_STARTED", "IN_PROGRESS", "FINISHED_PENDING",
                                        "OVERDUE", "POSTPONED", "CANCELLED", "ABANDONED", "VOID", "UNKNOWN")}
    for name in paths:
        if not name.startswith("sales/") or name.split("/")[-1] > str(day):
            continue
        for row in docs[name].get("matches", []):
            evidence = (result_evidence.get(name) or {}).get(str(row.get("match_no")))
            state = settlement_state(row, now, cfg["thresholds"]["settlement_overdue_fail_h"], evidence)
            settled.setdefault(state, []).append(f"{name.split('/')[-1]} / {row.get('match_no')}")
    settlement_runs = runs_by[SETTLEMENT] or []
    last_settlement = max(settlement_runs, key=lambda r: r["created_at"], default={})
    settle_status = ("FAIL" if settled.get("OVERDUE") else "UNKNOWN"
                     if settled.get("UNKNOWN") or not any(settled.values()) or runs_by[SETTLEMENT] is None
                     or any(not docs[name] for name in paths[1:-1]) else "PASS")
    states_cn = {"SETTLED": "已核验", "NOT_STARTED": "未开赛", "IN_PROGRESS": "比赛中",
                 "FINISHED_PENDING": "完赛待核验", "OVERDUE": "确认为完赛且超时",
                 "POSTPONED": "延期", "CANCELLED": "取消", "ABANDONED": "腰斩",
                 "VOID": "官方作废", "UNKNOWN": "状态待核实"}
    out.append(outcome("settlement", "独立赛果核验及异常赛事", settle_status,
                       "；".join(f"{states_cn[k]} {len(v)} 场" for k, v in settled.items()),
                       states=settled, publisher=last_settlement.get("html_url"), checked_at=raw_at,
                       link=f"https://github.com/{PUBLIC}/actions/workflows/{SETTLEMENT}"))

    matched = {name: bool(main.get(name) and docs.get(name) and main[name] == docs[name]) for name in main_names}
    if all(matched.values()) and generated and generated <= now and latest_n and not pool_issue:
        sync_status = "PASS"
    elif all(main.get(n) and docs.get(n) for n in main_names):
        deployed = C.parse_ts(latest.get("generated_at"))
        stale_known = (generated and deployed and generated > deployed
                       and now - generated >= timedelta(minutes=cfg["thresholds"]["sync_lag_fail_min"]))
        sync_status = "FAIL" if pool_issue or stale_known else "WARN"
    else:
        sync_status = "UNKNOWN"
    out.append(outcome("site_data_sync", "公开 main 与正式站发布", sync_status,
                       "；".join(f"{name} {'一致' if match else '未通过比对'}" for name, match in matched.items()),
                       comparisons=matched, ref=ref, link=page_url("latest"),
                       compared_paths={name: [f"https://github.com/{PUBLIC}/blob/{ref}/{data}/{name}.json", page_url(name)]
                                       for name in main_names},
                       generated_at=latest.get("generated_at"), checked_at=raw_at))

    rec_status, proof = receipt_recovery(receipts, main.get("latest") or {}, latest, sync_status)
    out.append(outcome("self_heal", "公开自检自愈", rec_status,
                       f"公开审计回执 {len(receipts)} 份，恢复证据 {len(proof.get('recovered', [])) if proof else 0} 项",
                       recovery=proof, publication_status=receipt.get("publication_status"),
                       snapshot_issues=receipt.get("snapshot_issues"), betfair=receipt.get("betfair"),
                       receipt_at=receipt.get("checked_at"), link=source_link))
    out.append(outcome("independent_review", "旧独立复核归档", "RETIRED",
                       "旧日期文件仅供历史查阅，正式赛果沿用独立结算发布器"))
    out.append(outcome("experimental", "三核及试验引擎", "SKIP",
                       "研究和影子产物不参与正式预测可用性判定"))
    intelligence_states = Counter(m.get("status", "unknown") for m in intelligence.get("matches", []))
    out.append(outcome("intelligence", "公开 FotMob 情报影子采集", "SKIP",
                       f"影子产物 {sum(intelligence_states.values())} 场，逐场状态 {dict(intelligence_states)}。",
                       checked_at=intelligence.get("generated_at") or intelligence.get("collected_at"),
                       link=f"https://github.com/{PUBLIC}/blob/main/{data}/intelligence/{day}.json"))

    tasks = handoff.list_tasks(root)
    task_counts = Counter(t.get("status", "unknown") for t in tasks)
    out.append(outcome("handoff", "双AI工单", "PASS",
                       f"待领取 {task_counts['queued']}，处理中 {task_counts['claimed']}，累计完成 {task_counts['done']}。按当前故障建单。",
                       counts=dict(task_counts), checked_at=raw_at,
                       link=f"https://github.com/{cfg['ops_repo']}/tree/main/tasks"))

    private_counts = Counter(("running" if r.get("status") != "completed" else r.get("conclusion") or "unknown")
                             for r in residual or [])
    private_link = f"https://github.com/{private}/actions"
    residual_workflows = Counter(r.get("path") for r in residual or [] if r.get("conclusion") != "skipped")
    out.append(outcome("private_audit", "私人仓库残留运行与额度", "SKIP" if residual is not None else "UNKNOWN",
                       (f"今日 {len(residual)} 条运行记录，其中跳过 {private_counts['skipped']} 条。实际计费分钟待账单核实。"
                        if residual is not None else "私人运行记录缺少权限或不可读，实际计费分钟也未核实。"),
                       counts=dict(private_counts), residual_workflows=dict(residual_workflows), billed_minutes=None,
                       checked_at=raw_at, link=private_link))
    return out
