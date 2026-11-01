"""Meaningful integrity, provenance and query-boundary checks for the graph baseline."""
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from query_graph import query

HERE = Path(__file__).resolve().parent


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads((HERE / 'artifacts/graph.json').read_text(encoding='utf-8'))
        cls.nodes = {n['id']: n for n in cls.graph['nodes']}

    def test_unique_ids(self):
        self.assertEqual(len(self.nodes), len(self.graph['nodes']))
        self.assertEqual(len({e['id'] for e in self.graph['edges']}), len(self.graph['edges']))

    def test_all_edges_have_endpoints_and_valid_types(self):
        for edge in self.graph['edges']:
            rule = self.graph['ontology'][edge['relation']]
            self.assertEqual(self.nodes[edge['source']]['type'], rule['domain'])
            self.assertEqual(self.nodes[edge['target']]['type'], rule['range'])

    def test_every_relation_retains_evidence(self):
        for edge in self.graph['edges']:
            self.assertTrue(edge['evidence'])
            for evidence in edge['evidence']:
                self.assertTrue(evidence['locator'])
                self.assertTrue(evidence['excerpt'])
                self.assertTrue(evidence.get('file_sha256') or evidence.get('url'))

    def test_input_hashes_match_snapshots(self):
        for name, expected in self.graph['input_sha256'].items():
            self.assertEqual(hashlib.sha256((HERE / 'inputs' / name).read_bytes()).hexdigest(), expected)

    def test_derived_fields_never_silently_approved(self):
        for edge in self.graph['edges']:
            if edge['relation'].startswith('PROJECT_'):
                self.assertEqual(edge['status'], 'REVIEW_REQUIRED')

    def test_mentions_do_not_become_positive_findings(self):
        text_edges = [e for e in self.graph['edges'] if self.nodes[e['source']]['type'] == 'TextRecord']
        self.assertTrue(text_edges)
        self.assertTrue(all(e['relation'] == 'MENTIONS' and e['status'] == 'MENTION_ONLY' for e in text_edges))

    def test_no_patient_diagnosis_relation(self):
        self.assertFalse(any(e['relation'] in {'DIAGNOSED_WITH', 'HAS_DIAGNOSIS'} for e in self.graph['edges']))

    def test_filtered_query_uses_same_region(self):
        result = query(self.graph, '黄斑区出血参考病例')
        self.assertGreater(result['count'], 0)
        for candidate in result['candidates']:
            region = candidate['region_id']
            support = [e for e in candidate['path'] if e['source'] == region]
            self.assertTrue(any(e['relation'] == 'ANNOTATED_AS' and e['target'] == 'lesion:HE' for e in support))
            self.assertTrue(any(e['relation'] == 'PROJECT_LOCATION' and e['target'] == 'anatomy:MACULA' for e in support))
            self.assertTrue(candidate['review_required'])

    def test_negative_and_uncertain_queries_are_not_positive(self):
        for text in ('未见出血参考病例', '疑似微血管瘤病例', '不排除出血'):
            self.assertEqual(query(self.graph, text)['status'], 'NEEDS_CLARIFICATION')

    def test_multi_lesion_request_is_explicitly_limited(self):
        self.assertEqual(query(self.graph, '出血和硬性渗出病例')['status'], 'NEEDS_CLARIFICATION')

    def test_medication_request_is_refused(self):
        self.assertEqual(query(self.graph, '糖网病怎么治疗')['status'], 'OUT_OF_SCOPE')

    def test_unknown_query_has_no_invented_answer(self):
        self.assertEqual(query(self.graph, '青光眼手术技巧')['status'], 'NOT_FOUND')

    def test_case_trace_links_actual_data(self):
        result = query(self.graph, 'CASE_IDRiD_000055')
        self.assertEqual(result['status'], 'RECORD_TRACE')
        self.assertTrue(any(e['relation'] == 'HAS_IMAGE' for e in result['paths']))
        self.assertTrue(any(e['relation'] == 'HAS_REGION' for e in result['paths']))
        self.assertTrue(any(e['relation'] == 'ANNOTATED_AS' for e in result['paths']))

    def test_sqlite_matches_export(self):
        with sqlite3.connect(HERE / 'artifacts/graph.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0], len(self.nodes))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM edges').fetchone()[0], len(self.graph['edges']))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_no_fake_llm_or_model_training_claim(self):
        self.assertEqual(self.graph['stats']['llm_calls'], 0)
        self.assertFalse(self.graph['stats']['trained_mlp'])


if __name__ == '__main__':
    unittest.main()
