"""Offline self-tests for the ops kit: no network, no secrets.

Run:  python ops/selftest.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import budget, checks, common, dedupe, handoff, inspector, persist, recovery, task_runner, triage, watchdog  # noqa: E402


class TestBudget(unittest.TestCase):
    def test_cost_deepseek_chat_list_price(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        self.assertAlmostEqual(budget.cost_of("deepseek-chat", usage), 10.0, places=6)

    def test_cost_small_call(self):
        usage = {"prompt_tokens": 1500, "completion_tokens": 400}
        cost = budget.cost_of("deepseek-chat", usage)
        self.assertAlmostEqual(cost, 1500 / 1e6 * 2.0 + 400 / 1e6 * 8.0, places=6)
        self.assertLess(cost, 0.01)

    def test_hard_stop_when_exhausted(self):
        ledger = {"month": "2026-11", "entries": [{"cost_cny": 9.98}, {"cost_cny": 0.05}]}
        ok, why = budget.can_call(ledger, 10.0)
        self.assertFalse(ok)
        self.assertIn("预算", why)

    def test_reserve_blocks_small_remainder(self):
        ledger = {"month": "2026-11", "entries": [{"cost_cny": 9.9}]}
        ok, _ = budget.can_call(ledger, 10.0, reserve_cny=0.3)
        self.assertFalse(ok)

    def test_summary_math(self):
        ledger = {"month": "2026-11", "entries": [{"cost_cny": 0.25}, {"cost_cny": 0.5}]}
        s = budget.summary(ledger, 10.0)
        self.assertEqual(s["spent_cny"], 0.75)
        self.assertEqual(s["remaining_cny"], 9.25)
        self.assertEqual(s["calls"], 2)
        self.assertFalse(s["exhausted"])

    def test_month_key_beijing(self):
        # 2026-11-30 17:00 UTC == 2026-12-01 01:00 Beijing
        now = dt.datetime(2026, 11, 30, 17, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(budget.month_key(now), "2026-12")


class TestCommon(unittest.TestCase):
    def test_verdict_precedence(self):
        self.assertEqual(common.verdict_of([{"status": "ok"}]), "OK")
        self.assertEqual(common.verdict_of([{"status": "ok"}, {"status": "warn"}]), "WARN")
        self.assertEqual(common.verdict_of([{"status": "warn"}, {"status": "fail"}]), "FAIL")
        self.assertEqual(common.verdict_of([{"status": "skip"}]), "UNKNOWN")

    def test_git_blob_sha_known_value(self):
        self.assertEqual(common.git_blob_sha(b"abc"),
                         "f2ba8f84ab5c1bce84a7b441cb1959cfc7093b7f")

    def test_parse_naive_timestamp_is_beijing(self):
        ts = common.parse_ts("2026-11-19 06:30")
        self.assertEqual(common.iso(ts), "2026-11-18T22:30:00Z")

    def test_sale_window_boundaries(self):
        bj = common.BJ
        self.assertTrue(common.in_sale_window(dt.datetime(2026, 11, 18, 9, 0, tzinfo=bj)))
        self.assertTrue(common.in_sale_window(dt.datetime(2026, 11, 18, 22, 45, tzinfo=bj)))
        self.assertFalse(common.in_sale_window(dt.datetime(2026, 11, 18, 22, 46, tzinfo=bj)))
        self.assertFalse(common.in_sale_window(dt.datetime(2026, 11, 18, 3, 0, tzinfo=bj)))


class TestTriageGuards(unittest.TestCase):
    def test_parse_plain_json(self):
        self.assertEqual(triage._parse_json_text('{"a":1}'), {"a": 1})

    def test_parse_fenced_json(self):
        self.assertEqual(triage._parse_json_text('```json\n{"a":2}\n```'), {"a": 2})

    def test_parse_garbage_returns_raw(self):
        self.assertIn("raw", triage._parse_json_text("not json"))

    def test_dedupe_hit_inside_window(self):
        now = dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.timezone.utc)
        ledger = {"entries": [{"at": "2026-11-18T05:30:00+00:00",
                               "note": "triage:FAIL:sources"}]}
        self.assertTrue(triage._dedupe_hit(ledger, "sources", 120, now))
        self.assertFalse(triage._dedupe_hit(ledger, "sources", 15, now))
        self.assertFalse(triage._dedupe_hit(ledger, "settlement", 120, now))

    def test_daily_cap(self):
        now = dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.timezone.utc)
        ledger = {"entries": [{"at": "2026-11-18T0%d:00:00+00:00" % i} for i in range(5)]}
        self.assertEqual(triage._today_entries(ledger, now), 5)


class TestConfig(unittest.TestCase):
    def test_config_shape(self):
        cfg = common.load_json(os.path.join(ROOT, "config", "ops.json"))
        self.assertIsNotNone(cfg)
        for key in ("site", "site_repo", "thresholds", "probes", "budget"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["budget"]["cap_cny"], 10.0)
        ids = {p["id"] for p in cfg["probes"]}
        self.assertTrue({"sporttery", "qiulaile", "fotmob", "okooo"} <= ids)

    def test_every_check_registered_once(self):
        ids = [f.__name__ for f in checks.CHECKS]
        self.assertEqual(len(ids), len(set(ids)))
        for f in checks.CHECKS:
            self.assertTrue(callable(f))


class TestMarkdown(unittest.TestCase):
    def test_render(self):
        doc = {
            "verdict": "WARN", "generated_at": "2026-11-18T06:00:00Z", "in_sale_window": True,
            "failed": [], "warned": ["sources"],
            "budget": {"spent_cny": 0.01, "cap_cny": 10.0, "remaining_cny": 9.99, "calls": 1},
            "checks": [{"id": "sources", "title": "采集链路可用性", "status": "warn",
                        "detail": "澳客 down | 备注", "probes": [
                            {"name": "澳客竞彩", "status": "down", "http": 405, "bytes": 2657, "elapsed_ms": 132}]}],
        }
        md = inspector.build_markdown(doc)
        self.assertIn("WARN", md)
        self.assertIn("澳客竞彩", md)
        self.assertIn("\\|", md)  # pipes in detail are escaped


class TestProbeTemplating(unittest.TestCase):
    def test_template(self):
        ctx = {"now": dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.timezone.utc), "cfg": {}}
        url = checks._probe_url("https://x/{today}/{today_compact}", ctx)
        self.assertEqual(url, "https://x/2026-11-18/20261118")


class TestHandoff(unittest.TestCase):
    def setUp(self):
        # 用固定目录而不是 mkdtemp：某些 Windows 沙箱会拒绝对 mkdtemp 新建目录再建子目录
        self.tmp = os.path.join(ROOT, ".selftest-tmp", self._testMethodName)
        os.makedirs(self.tmp, exist_ok=True)
        self.now = dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_task_and_validate(self):
        doc = handoff.new_task(self.tmp, "确认澳客 405", created_by="gpt",
                               priority="P1", now=self.now)
        self.assertRegex(doc["id"], r"^TASK-20261118-\d{3}$")
        self.assertEqual(doc["status"], "queued")
        self.assertEqual(handoff.validate_task(doc), [])

    def test_task_ids_increment(self):
        a = handoff.new_task(self.tmp, "a", now=self.now)
        b = handoff.new_task(self.tmp, "b", now=self.now)
        self.assertNotEqual(a["id"], b["id"])
        self.assertTrue(b["id"].endswith("002"))

    def test_write_scope_rejected(self):
        with self.assertRaises(ValueError):
            handoff.new_task(self.tmp, "改算法", scope="write", now=self.now)

    def test_claim_then_complete(self):
        task = handoff.new_task(self.tmp, "查结算", now=self.now)
        handoff.claim(self.tmp, task["id"], by="deepseek", now=self.now)
        with self.assertRaises(ValueError):
            handoff.claim(self.tmp, task["id"], now=self.now)
        rep = handoff.complete(self.tmp, task["id"], "OK", "已确认",
                               findings=["f1"], evidence=["state/latest.json"],
                               next_actions=["n1"], now=self.now)
        self.assertTrue(rep["report_id"].startswith("RPT-20261118-"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "reports", rep["report_id"] + ".md")))
        self.assertEqual(handoff.load_task(self.tmp, task["id"])["status"], "done")

    def test_board_renders(self):
        handoff.new_task(self.tmp, "任务A", now=self.now)
        md = handoff.board(self.tmp)
        self.assertIn("TASK-20261118-001", md)
        self.assertIn("任务A", md)


class TestSourcesGeoDowngrade(unittest.TestCase):
    """境外 runner 访问中国站点被拒时，不应误报 FAIL（否则会白烧 AI 预算）。"""

    def setUp(self):
        self.cfg = {
            "probes": [
                {"id": "sporttery", "name": "体彩官方接口", "url": "https://a/{today}",
                 "required": True, "cn_only": True, "min_bytes": 1},
                {"id": "qiulaile", "name": "球来了镜像", "url": "https://b/{today}",
                 "required": False, "cn_only": True, "min_bytes": 1},
            ],
            "thresholds": {"probe_timeout_s": 5},
        }
        self.ctx = {"cfg": self.cfg, "now": dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.timezone.utc)}
        self._orig = checks.http_get

    def tearDown(self):
        checks.http_get = self._orig
        os.environ.pop("GITHUB_ACTIONS", None)

    def _stub(self, codes):
        def fake(url, timeout=20, retries=2, headers=None):
            code = codes.get("a" if "//a/" in url else "b", 200)
            body = b"x" * 10 if code == 200 else b""
            return {"url": url, "status": code, "bytes": len(body), "body": body,
                    "sha256": "", "elapsed_ms": 1, "error": None if code == 200 else f"HTTP {code}"}
        checks.http_get = fake

    def test_offshore_runner_downgrades_cn_only_failure_to_skip(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self._stub({"a": 403, "b": 200})
        res = checks.check_sources(self.ctx)
        self.assertEqual(res["status"], "skip")
        self.assertIn("境外 runner", res["detail"])

    def test_all_cn_sources_down_still_fails(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self._stub({"a": 403, "b": 0})
        res = checks.check_sources(self.ctx)
        self.assertEqual(res["status"], "fail")

    def test_local_china_runner_fails_on_required(self):
        os.environ.pop("GITHUB_ACTIONS", None)
        self._stub({"a": 500, "b": 200})
        res = checks.check_sources(self.ctx)
        self.assertEqual(res["status"], "fail")


class TestExecutorGuards(unittest.TestCase):
    def test_rejects_write_scope(self):
        ok, why = task_runner.validate_spec({"scope": "write", "action": "site_probe"})
        self.assertFalse(ok)
        self.assertIn("read-only", why)

    def test_rejects_unknown_action(self):
        ok, why = task_runner.validate_spec({"scope": "read-only", "action": "rm -rf /"})
        self.assertFalse(ok)
        self.assertIn("未知 action", why)

    def test_rejects_missing_action(self):
        ok, why = task_runner.validate_spec({"scope": "read-only"})
        self.assertFalse(ok)

    def test_accepts_known_action(self):
        ok, _ = task_runner.validate_spec({"scope": "read-only", "action": "site_probe"})
        self.assertTrue(ok)

    def test_site_probe_uses_only_safe_params(self):
        orig = checks.http_get if hasattr(checks, "http_get") else None
        from ops import common
        old = common.http_get
        try:
            common.http_get = lambda url, **kw: {"status": 200, "bytes": 123, "body": b"vip-code",
                                                 "elapsed_ms": 5, "error": None, "sha256": ""}
            ctx = {"site": "https://example.test"}
            out = task_runner.site_probe(ctx, {"url": "https://example.test/"}, dt.datetime.now(dt.timezone.utc))
            self.assertTrue(out["ok"])
            self.assertEqual(out["data"]["bytes"], 123)
        finally:
            common.http_get = old

    def test_param_newline_rejected(self):
        from ops import common
        old = common.http_get
        try:
            common.http_get = lambda url, **kw: {"status": 200, "bytes": 1, "body": b"", "elapsed_ms": 1,
                                                 "error": None, "sha256": ""}
            with self.assertRaises(ValueError):
                task_runner.site_probe({"site": "x"}, {"url": "https://a/\nhttps://b"}, dt.datetime.now(dt.timezone.utc))
        finally:
            common.http_get = old


class TestRecoveryPlansV13(unittest.TestCase):
    """V1.3：运维动作与代码修改分离，停售保护按计划类别判定。"""

    def setUp(self):
        self.cfg = common.load_json(os.path.join(ROOT, "config", "ops.json"))
        self.ok_status = {"checks": [{"id": "engine_runs", "status": "warn"},
                                     {"id": "engine_fresh", "status": "warn"},
                                     {"id": "site_data_sync", "status": "ok"},
                                     {"id": "sources", "status": "warn"}]}

    def _now(self, hh, mm=0):
        return dt.datetime(2026, 11, 19, hh, mm, tzinfo=common.BJ).astimezone(dt.timezone.utc)

    def test_plans_present(self):
        plans = recovery.plans(self.cfg)
        for name in ("engine_missed_run", "site_sync_failure", "collector_failure", "code_fix"):
            self.assertIn(name, plans)

    def test_engine_recovery_allowed_outside_window(self):
        p = recovery.preflight(self.cfg, "engine_missed_run", recovery.CONFIRM_PHRASE,
                               self._now(3), [], self.ok_status)
        self.assertEqual(p, [])

    def test_engine_recovery_allowed_inside_window(self):
        # allow_in_sale=true：窗口内也能救，这是对旧设计反向限制的修正
        p = recovery.preflight(self.cfg, "engine_missed_run", recovery.CONFIRM_PHRASE,
                               self._now(12), [], self.ok_status)
        self.assertEqual(p, [])

    def test_collector_report_blocked_in_window(self):
        p = recovery.preflight(self.cfg, "collector_failure", recovery.CONFIRM_PHRASE,
                               self._now(12), [], self.ok_status)
        self.assertTrue(any("停售保护" in x for x in p))

    def test_precondition_not_met_refused(self):
        ok_status = {"checks": [{"id": "engine_runs", "status": "ok"},
                                {"id": "engine_fresh", "status": "ok"}]}
        p = recovery.preflight(self.cfg, "engine_missed_run", recovery.CONFIRM_PHRASE,
                               self._now(3), [], ok_status)
        self.assertTrue(any("前置条件" in x for x in p))

    def test_code_fix_refused_by_recovery_channel(self):
        p = recovery.preflight(self.cfg, "code_fix", recovery.CONFIRM_PHRASE,
                               self._now(3), [], self.ok_status)
        self.assertTrue(any("fix_pr" in x for x in p))

    def test_wrong_confirm_refused(self):
        p = recovery.preflight(self.cfg, "engine_missed_run", "yes", self._now(3), [], self.ok_status)
        self.assertTrue(any("授权串" in x for x in p))

    def test_rate_limit_per_plan(self):
        now = self._now(3)
        hist = [{"plan": "engine_missed_run", "allowed": True,
                 "at": (now - dt.timedelta(minutes=5)).isoformat()}]
        p = recovery.preflight(self.cfg, "engine_missed_run", recovery.CONFIRM_PHRASE, now, hist, self.ok_status)
        self.assertTrue(any("上限" in x for x in p))

    def test_writes_frozen_data_never_allowed(self):
        cfg = {"recovery": {"enabled": True}, "recovery_plans": {
            "bad": {"class": "operational", "action": "dispatch_engine4",
                    "writes_frozen_data": True, "allow_in_sale": True}}}
        p = recovery.preflight(cfg, "bad", recovery.CONFIRM_PHRASE, self._now(3), [], self.ok_status)
        self.assertTrue(any("冻结" in x for x in p))

    def test_unknown_plan_refused(self):
        p = recovery.preflight(self.cfg, "rm_rf", recovery.CONFIRM_PHRASE, self._now(3), [], self.ok_status)
        self.assertTrue(any("未定义" in x for x in p))


class TestPersist(unittest.TestCase):
    def test_first_time_commits(self):
        ok, _ = persist.should_commit({"verdict": "OK"}, None, dt.datetime.now(dt.timezone.utc))
        self.assertTrue(ok)

    def test_unchanged_and_fresh_skips(self):
        now = dt.datetime(2026, 11, 19, 0, 0, tzinfo=dt.timezone.utc)
        local = {"verdict": "OK", "signature": "OK", "generated_at": now.isoformat()}
        remote = {"verdict": "OK", "signature": "OK",
                  "generated_at": (now - dt.timedelta(minutes=5)).isoformat()}
        ok, why = persist.should_commit(local, remote, now)
        self.assertFalse(ok)
        self.assertIn("跳过", why)

    def test_signature_change_commits(self):
        now = dt.datetime(2026, 11, 19, 0, 0, tzinfo=dt.timezone.utc)
        local = {"verdict": "WARN", "signature": "sources", "generated_at": now.isoformat()}
        remote = {"verdict": "OK", "signature": "OK", "generated_at": now.isoformat()}
        ok, why = persist.should_commit(local, remote, now)
        self.assertTrue(ok)
        self.assertIn("判定变化", why)

    def test_fingerprint_change_commits(self):
        now = dt.datetime(2026, 11, 19, 0, 0, tzinfo=dt.timezone.utc)
        local = {"verdict": "WARN", "signature": "sources", "generated_at": now.isoformat(),
                 "fingerprint": "a=ok,b=skip"}
        remote = {"verdict": "WARN", "signature": "sources", "generated_at": now.isoformat(),
                  "fingerprint": "a=ok,b=warn"}
        ok, why = persist.should_commit(local, remote, now)
        self.assertTrue(ok)
        self.assertIn("fingerprint", why)

    def test_stale_forces_commit(self):
        now = dt.datetime(2026, 11, 19, 0, 0, tzinfo=dt.timezone.utc)
        local = {"verdict": "OK", "signature": "OK", "generated_at": now.isoformat()}
        remote = {"verdict": "OK", "signature": "OK",
                  "generated_at": (now - dt.timedelta(minutes=90)).isoformat()}
        ok, _ = persist.should_commit(local, remote, now)
        self.assertTrue(ok)


class TestBudgetConcurrency(unittest.TestCase):
    def test_merge_keeps_both_sides(self):
        local = {"entries": [{"at": "t2", "note": "n2", "cost_cny": 0.01}]}
        remote = {"entries": [{"at": "t1", "note": "n1", "cost_cny": 0.02}]}
        merged = budget.merge_entries(local, remote)
        self.assertEqual(len(merged["entries"]), 2)
        self.assertEqual([e["at"] for e in merged["entries"]], ["t1", "t2"])

    def test_merge_dedupes_identical_entry(self):
        e = {"at": "t1", "note": "n1", "cost_cny": 0.02}
        merged = budget.merge_entries({"entries": [e]}, {"entries": [dict(e)]})
        self.assertEqual(len(merged["entries"]), 1)

    def test_merge_total_not_lost(self):
        local = {"entries": [{"at": "t1", "note": "a", "cost_cny": 0.1},
                             {"at": "t2", "note": "b", "cost_cny": 0.2}]}
        remote = {"entries": [{"at": "t2", "note": "b", "cost_cny": 0.2},
                              {"at": "t3", "note": "c", "cost_cny": 0.3}]}
        merged = budget.merge_entries(local, remote)
        self.assertAlmostEqual(sum(e["cost_cny"] for e in merged["entries"]), 0.6)


class TestStopSaleWindowConfig(unittest.TestCase):
    def test_window_is_config_driven(self):
        cfg = {"recovery": {"stop_sale_window_bj": ["10:00", "11:00"]}}
        inside = dt.datetime(2026, 11, 19, 10, 30, tzinfo=common.BJ)
        outside = dt.datetime(2026, 11, 19, 9, 0, tzinfo=common.BJ)
        self.assertTrue(recovery.in_stop_sale_window(inside, cfg))
        self.assertFalse(recovery.in_stop_sale_window(outside, cfg))

    def test_default_window_boundaries(self):
        self.assertTrue(recovery.in_stop_sale_window(dt.datetime(2026, 11, 19, 8, 45, tzinfo=common.BJ), {}))
        self.assertTrue(recovery.in_stop_sale_window(dt.datetime(2026, 11, 19, 22, 45, tzinfo=common.BJ), {}))
        self.assertFalse(recovery.in_stop_sale_window(dt.datetime(2026, 11, 19, 8, 44, tzinfo=common.BJ), {}))


class TestDedupe(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 19, 2, 0, tzinfo=dt.timezone.utc)

    def _run(self, rid, minutes_ago, event="schedule"):
        return {"id": rid, "event": event,
                "created_at": (self.NOW - dt.timedelta(minutes=minutes_ago)).isoformat()}

    def test_recent_other_run_skips(self):
        ok, why = dedupe.decide([self._run(1, 2)], "99", self.NOW, 5)
        self.assertFalse(ok)
        self.assertIn("已有运行", why)

    def test_old_run_proceeds(self):
        ok, _ = dedupe.decide([self._run(1, 30)], "99", self.NOW, 5)
        self.assertTrue(ok)

    def test_self_is_ignored(self):
        ok, _ = dedupe.decide([self._run(99, 0)], "99", self.NOW, 5)
        self.assertTrue(ok)

    def test_empty_history_proceeds(self):
        ok, _ = dedupe.decide([], "99", self.NOW, 5)
        self.assertTrue(ok)


class TestWatchdogDecide(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 19, 2, 0, tzinfo=dt.timezone.utc)
    CFG = {"enabled": True, "cooldown_min": 90, "max_per_day": 3,
           "require_no_active_run": True, "block_in_sale_window": True,
           "manual_override_in_sale": True, "allow_in_sale_when_stale": True}

    def ctx(self, **kw):
        base = {"now": self.NOW, "cfg": self.CFG, "missed": ["12:12"], "active_runs": [],
                "in_sale_window": False, "engine_status": "warn", "fresh_status": "warn",
                "last_auto_age_min": 200, "today_auto_count": 0}
        base.update(kw)
        return base

    def test_trigger_when_all_conditions_met(self):
        d = watchdog.decide(self.ctx())
        self.assertTrue(d["should_trigger"], d["blocked_by"])

    def test_active_run_blocks(self):
        d = watchdog.decide(self.ctx(active_runs=[{"id": 5}]))
        self.assertFalse(d["should_trigger"])
        self.assertTrue(any("运行中" in x for x in d["blocked_by"]))

    def test_no_missed_blocks(self):
        d = watchdog.decide(self.ctx(missed=[]))
        self.assertFalse(d["should_trigger"])

    def test_sale_window_blocks(self):
        d = watchdog.decide(self.ctx(in_sale_window=True))
        self.assertFalse(d["should_trigger"])
        self.assertTrue(any("停售" in x for x in d["blocked_by"]))

    def test_manual_override_in_sale(self):
        d = watchdog.decide(self.ctx(in_sale_window=True, manual=True))
        self.assertTrue(d["should_trigger"], d["blocked_by"])
        self.assertTrue(any("sale_window" in x for x in d["overrides"]))

    def test_manual_cannot_override_active_run(self):
        d = watchdog.decide(self.ctx(in_sale_window=True, manual=True, active_runs=[{"id": 1}]))
        self.assertFalse(d["should_trigger"])

    def test_auto_allowed_in_sale_when_stale(self):
        d = watchdog.decide(self.ctx(in_sale_window=True, fresh_status="fail",
                                     engine_status="warn"))
        self.assertTrue(d["should_trigger"], d["blocked_by"])
        self.assertTrue(any("stale" in x or "过期" in x for x in d["overrides"]))

    def test_auto_blocked_in_sale_when_not_stale(self):
        d = watchdog.decide(self.ctx(in_sale_window=True, fresh_status="warn",
                                     engine_status="warn"))
        self.assertFalse(d["should_trigger"])

    def test_fresh_data_blocks(self):
        d = watchdog.decide(self.ctx(engine_status="ok", fresh_status="ok"))
        self.assertFalse(d["should_trigger"])
        self.assertTrue(any("新鲜" in x for x in d["blocked_by"]))

    def test_cooldown_blocks(self):
        d = watchdog.decide(self.ctx(last_auto_age_min=10))
        self.assertFalse(d["should_trigger"])
        self.assertTrue(any("冷却" in x for x in d["blocked_by"]))

    def test_daily_cap_blocks(self):
        d = watchdog.decide(self.ctx(today_auto_count=3))
        self.assertFalse(d["should_trigger"])
        self.assertTrue(any("上限" in x for x in d["blocked_by"]))

    def test_disabled_blocks(self):
        d = watchdog.decide(self.ctx(cfg={"enabled": False}))
        self.assertFalse(d["should_trigger"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
