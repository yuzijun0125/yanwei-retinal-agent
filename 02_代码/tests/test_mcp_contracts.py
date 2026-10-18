"""Contract regressions for Nexent candidate transport and compact evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yanwei import mcp_tools as tools, mcp_stdio, evidence_store


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.query = tools.extract_medical_entities({"text": "右眼黄斑区散在微血管瘤"})
        cls.search = tools.search_cases({"query_text": "右眼黄斑区散在微血管瘤", "topk": 3})

    def test_object_array_and_json_strings_have_same_verdict(self):
        values = [self.search, self.search["candidates"], json.dumps(self.search),
                  json.dumps(self.search["candidates"])]
        results = [tools.verify_medical_logic({"query": self.query, "candidates": x}) for x in values]
        self.assertTrue(all(x["ok"] for x in results))
        self.assertEqual(len({(x["verdict"], x["sufficiency"]) for x in results}), 1)

    def test_bad_candidate_shapes_are_bad_input(self):
        for value in ["not JSON", "null", "1", ["wrong"], {"wrong": []}, 42]:
            with self.subTest(value=value):
                result = tools.verify_medical_logic({"query": self.query, "candidates": value})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "BAD_INPUT")

    def test_evidence_accepts_legacy_json(self):
        logic = tools.verify_medical_logic({"query": self.query, "candidates": self.search})
        result = tools.build_evidence_chain({"query": self.query, "candidates": json.dumps(self.search),
                                             "logic_result": logic})
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "RETURN_CASES")

    def test_bad_evidence_candidates_do_not_become_internal_error(self):
        logic = tools.verify_medical_logic({"query": self.query, "candidates": self.search})
        result = tools.build_evidence_chain({"candidates": "broken", "logic_result": logic})
        self.assertEqual(result["error"]["code"], "BAD_INPUT")

    def test_schemas_have_explicit_object_type(self):
        for name in ["verify_medical_logic", "build_evidence_chain"]:
            schema = next(x for x in mcp_stdio.TOOL_SCHEMAS if x["name"] == name)
            self.assertEqual(schema["inputSchema"]["properties"]["candidates"]["type"], "object")

    def test_compact_mcp_retains_full_evidence_in_store(self):
        logic = tools.verify_medical_logic({"query": self.query, "candidates": self.search})
        arguments = {"query": self.query, "candidates": self.search, "logic_result": logic}
        with tempfile.TemporaryDirectory() as directory, patch.object(evidence_store, "folder", return_value=Path(directory)):
            response = mcp_stdio.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "build_evidence_chain", "arguments": arguments}})
            compact = response["result"]["structuredContent"]
            self.assertNotIn("html", compact)
            full = evidence_store.read(compact["evidence_id"])
            self.assertIn("html", full)
            for field in ["action", "basis", "provenance", "logic"]:
                self.assertEqual(full[field], compact[field])
            self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))
            arguments["include_html"] = True
            legacy = mcp_stdio.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "build_evidence_chain", "arguments": arguments}})
            self.assertIn("html", legacy["result"]["structuredContent"])

    def test_evidence_id_cannot_traverse(self):
        self.assertIsNone(evidence_store.read("../secret"))

    def test_nexent_all_upstream_outputs_are_json_text(self):
        query = json.dumps(self.query)
        candidates = json.dumps(self.search)
        logic = tools.verify_medical_logic({"query": query, "candidates": candidates})
        self.assertTrue(logic["ok"])
        result = tools.build_evidence_chain({"query": query, "candidates": candidates,
                                              "logic_result": json.dumps(logic)})
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "RETURN_CASES")

    def test_invalid_query_and_logic_are_bad_input(self):
        result = tools.verify_medical_logic({"query": "broken", "candidates": self.search})
        self.assertEqual(result["error"]["code"], "BAD_INPUT")
        result = tools.build_evidence_chain({"logic_result": "broken", "candidates": self.search})
        self.assertEqual(result["error"]["code"], "BAD_INPUT")

    def test_evidence_write_failure_returns_explicit_error(self):
        logic = tools.verify_medical_logic({"query": self.query, "candidates": self.search})
        with patch.object(evidence_store, "save", side_effect=OSError("unavailable")):
            response = mcp_stdio.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "build_evidence_chain", "arguments": {
                    "query": self.query, "candidates": self.search, "logic_result": logic}}})
        result = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(result["error"]["code"], "ARTIFACT_WRITE_FAILED")


if __name__ == "__main__": unittest.main()
