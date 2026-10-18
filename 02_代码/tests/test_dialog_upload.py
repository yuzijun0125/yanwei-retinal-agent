import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from yanwei import assets, dialog, evidence_store, mcp_tools as m, pipeline, multimodal


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mod in (assets, dialog, evidence_store, m, pipeline):
            if hasattr(mod, 'default_output_dir'):
                p = patch.object(mod, 'default_output_dir', return_value=self.temp.name)
                p.start()
                self.addCleanup(p.stop)

    def test_short_turns_accumulate_to_real_result(self):
        first = dialog.respond({'text': '左眼'})
        self.assertEqual(first['result']['action'], 'ASK_MORE')
        second = dialog.respond({'text': '黄斑区散在微血管瘤', 'session_id': first['session_id']})
        self.assertEqual(second['result']['action'], 'RETURN_CASES')
        # A complete second phrase must retain explicitly supplied eye context.
        self.assertEqual(second['result']['query']['codes']['eye'], 'OS')
        self.assertTrue(second['result']['evidence']['view_url'])
        self.assertNotIn('html', second['result']['evidence'])

    def test_eye_change_needs_confirmation(self):
        first = dialog.respond({'text': '右眼黄斑区散在微血管瘤'})
        second = dialog.respond({'text': '左眼', 'session_id': first['session_id']})
        self.assertNotIn('result', second)
        third = dialog.respond({'text': '是', 'session_id': first['session_id']})
        self.assertEqual(third['result']['query']['codes']['eye'], 'OS')

    def test_upload_preserves_original_and_bounds_preview(self):
        b = io.BytesIO()
        Image.new('RGB', (1800, 1000), 'red').save(b, 'PNG')
        raw = b.getvalue()
        record = assets.upload('test.png', base64.b64encode(raw).decode())
        self.assertEqual(Path(record['original_path']).read_bytes(), raw)
        with Image.open(record['analysis_path']) as image:
            self.assertLessEqual(max(image.size), 1024)
        self.assertEqual(assets.get(record['asset_id'])['sha256'], record['sha256'])

    def test_corrupt_image_and_unknown_asset_rejected(self):
        with self.assertRaises(ValueError):
            assets.upload('bad.jpg', base64.b64encode(b'not an image').decode())
        with self.assertRaises(ValueError):
            dialog.respond({'text': '左眼', 'asset_id': '../test'})

    def test_complete_flow_validates_before_running(self):
        for query in [[], 123, {'bad': True}]:
            result = m.search_cases({'query_text': query})
            self.assertEqual(result['error']['code'], 'BAD_INPUT')
        self.assertEqual(m.complete_flow({'clarify_round': 'x'})['error']['code'], 'BAD_INPUT')

    def test_image_hints_require_explicit_lesion_confirmation(self):
        image = Path(__file__).parent / 'fixtures' / 'bad_images' / 'ok_clean.png'
        with patch.object(multimodal, 'status', return_value={'ready':False, 'reason':'test legacy fallback'}):
            result = m.complete_flow({'query_text': '左眼黄斑区', 'image_path': str(image)})
        self.assertEqual(result['action'], 'ASK_MORE')
        self.assertIn('人工确认', result['action_reason'])

    def test_semantic_evidence_gallery_keeps_model_provenance(self):
        info={'used':True,'ready':True,'encoder':'FLAIR','model_revision':'test-revision'}
        hits={'IMG_IDRiD_000055':{'rank':1,'cosine':.95,'image_cosine':.96,'text_cosine':.8}}
        with patch.object(multimodal,'recall',return_value=(hits,info)):
            result=m.complete_flow({'query_text':'右眼黄斑区散在微血管瘤'})
        self.assertEqual(result['action'],'RETURN_CASES')
        self.assertEqual(result['evidence']['provenance']['model_ver'],'FLAIR@test-revisio')
        card=evidence_store.read(result['evidence']['evidence_id'])
        self.assertIn('整图相似候选对照',card['html'])
        self.assertIn('width:100%',card['html'])


if __name__ == '__main__':
    unittest.main()
