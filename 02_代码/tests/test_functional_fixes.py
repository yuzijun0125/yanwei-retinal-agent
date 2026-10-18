"""Business regressions for clarification, contracts, persistence and provenance."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yanwei import mcp_tools as m, pipeline, config


class FunctionalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = m.Runtime()

    def run_query(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            return pipeline.Pipeline(rt=self.rt, trace_dir=folder).run(text, save_artifacts=False, **kwargs)

    def test_short_eye_asks_and_cap_is_final(self):
        self.assertEqual(self.run_query("左眼")["action"], "ASK_MORE")
        result = self.run_query("眼底出血", clarify_round=2)
        self.assertEqual(result["action"], "ABSTAIN")
        self.assertEqual(result["decided_by"], "clarify_cap")

    def test_text_flow_still_returns_real_cases(self):
        result = self.run_query("右眼黄斑区散在微血管瘤")
        self.assertEqual(result["action"], "RETURN_CASES")
        self.assertTrue(result["evidence"]["basis"])

    def test_invalid_topk_is_user_error(self):
        for value in [-1, 0, True, {}, "bad", 31]:
            with self.subTest(value=value):
                r = m.search_cases({"query_text": "右眼黄斑区出血", "topk": value}, self.rt)
                self.assertEqual(r["error"]["code"], "BAD_INPUT")

    def test_uncertainty_survives_into_visible_evidence(self):
        r = self.run_query("左眼疑似黄斑区中度出血")
        self.assertTrue(r["evidence"]["query_uncertain"])
        self.assertIn("不确定", r["evidence"]["html"])

    def test_changed_threshold_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(config.default_config().dir)
            for f in source.glob("*.yaml"):
                shutil.copy2(f, folder)
            before = config.Config(folder)
            file = Path(folder) / "thresholds.yaml"
            file.write_text(file.read_text(encoding="utf-8").replace("return_cases: 0.80", "return_cases: 0.81"), encoding="utf-8")
            after = config.Config(folder)
            self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_empty_dataset_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(m.ToolError):
                m.Runtime(data_dir=folder)

    def test_concurrent_feedback_is_atomic(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(m, "default_output_dir", return_value=folder):
            payload = {"case_id": "CASE_IDRiD_000028", "verdict": "ACCEPT", "idempotency_key": "one"}
            with ThreadPoolExecutor(max_workers=8) as pool:
                replies = list(pool.map(lambda _: m.record_feedback(payload, self.rt), range(8)))
            self.assertTrue(all(r["ok"] for r in replies), replies)
            self.assertEqual(sum(r["stored"] for r in replies), 1)
            rows = (Path(folder) / "反馈/feedback.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["idempotency_key"], "one")

    def test_timeout_never_retries_a_still_running_tool(self):
        finished = threading.Event()
        calls = []
        def slow(*args):
            calls.append(1)
            time.sleep(.08)
            finished.set()
            return {"ok": True}
        class FastTimeout:
            max_tool_retries = 1
            def timeout_ms(self, name):
                return 5
        p = pipeline.Pipeline(rt=self.rt)
        p.cfg = FastTimeout()
        with patch.object(m, "call_tool", side_effect=slow):
            r, _ = p._call_tool("fixture", {}, pipeline.Guard(p.cfg))
            self.assertEqual(r["error"]["code"], "TIMEOUT")
            busy, _ = p._call_tool("fixture", {}, pipeline.Guard(p.cfg))
            self.assertEqual(busy["error"]["code"], "TOOL_BUSY")
            self.assertTrue(finished.wait(1))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
