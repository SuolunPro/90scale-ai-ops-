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

from ops import budget, checks, common, handoff, inspector, triage  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
