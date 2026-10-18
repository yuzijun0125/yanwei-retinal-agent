"""P02: contradictory eye/modality queries must not silently select one value."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from yanwei import mcp_tools as m, pipeline, dialog, assets, evidence_store, multimodal


class InputConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = m.Runtime.get()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mod in (m, pipeline, dialog, assets, evidence_store):
            p = patch.object(mod, 'default_output_dir', return_value=self.temp.name)
            p.start()
            self.addCleanup(p.stop)

    def test_conflict_stops_before_vector_search(self):
        for text in ('左眼和右眼黄斑区散在出血', '双眼黄斑区出血',
                     '左眼黄斑区出血；右眼黄斑区微血管瘤', '眼底彩照和超广角右眼黄斑区出血'):
            with self.subTest(text=text), patch.object(multimodal, 'recall') as recall:
                result = m.complete_flow({'query_text': text})
                self.assertEqual(result['action'], 'ASK_MORE')
                self.assertEqual(result['candidates'], [])
                self.assertTrue(result['query']['input_conflicts'])
                recall.assert_not_called()

    def test_raw_search_and_manual_logic_do_not_bypass_gate(self):
        text = '左眼和右眼黄斑区出血'
        query = m.extract_medical_entities({'text': text}, self.rt)
        self.assertIsNone(query['codes']['eye'])
        self.assertEqual(query['active_codes']['eye'], ['OD', 'OS'])
        raw = m.search_cases({'query_text': text}, self.rt)
        self.assertEqual(raw['action'], 'ASK_MORE')
        logic = m.verify_medical_logic({'query': query, 'candidates': []}, self.rt)
        self.assertEqual(m.decide_action(self.rt.cfg, logic, query['codes'])['action'], 'ASK_MORE')

    def test_explicit_corrections_and_exclusions(self):
        for text in ('不是左眼，是右眼黄斑区出血', '左眼，不对，是右眼黄斑区出血',
                     '左眼黄斑区出血，更正为右眼', '不查左眼，只查右眼黄斑区出血'):
            with self.subTest(text=text):
                result = m.parse_entities(self.rt, text)
                self.assertEqual(result['codes']['eye'], 'OD')
                self.assertFalse(result['input_conflicts'])

    def test_repetition_same_eye_is_not_conflict(self):
        self.assertFalse(m.parse_entities(self.rt, '左眼黄斑区出血，左眼')['input_conflicts'])

    def test_cap_abstains_without_candidates(self):
        r = m.complete_flow({'query_text': '左眼和右眼黄斑区出血', 'clarify_round': 2})
        self.assertEqual(r['action'], 'ABSTAIN')
        self.assertEqual(r['candidates'], [])

    def test_dialogue_selection_keeps_original_details(self):
        first = dialog.respond({'text': '左眼和右眼黄斑区散在出血'})
        self.assertEqual(first['result']['action'], 'ASK_MORE')
        second = dialog.respond({'text': '只查右眼', 'session_id': first['session_id']})
        query = second['result']['query']
        self.assertEqual(query['codes']['eye'], 'OD')
        self.assertEqual(query['codes']['location'], 'MACULA')
        self.assertEqual(query['codes']['lesion'], 'HE')
        self.assertFalse(query['input_conflicts'])
        self.assertEqual(second['result']['action'], 'RETURN_CASES')


if __name__ == '__main__':
    unittest.main()
