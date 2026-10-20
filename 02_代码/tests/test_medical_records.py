"""Record-image separation, OCR failures and request authorization."""
import base64,io,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from yanwei import assets,medical_records as records,request_scope
class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patches=[patch.object(assets,'folder',return_value=self.root/'uploads'),patch.object(request_scope,'default_output_dir',return_value=str(self.root))]
        for p in self.patches:p.start()
        stream=io.BytesIO();Image.new('RGB',(500,300),'white').save(stream,'PNG');self.data=base64.b64encode(stream.getvalue()).decode()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def record(self):return records.upload('record.png',self.data)['record']['asset_id']
    def test_fundus_image_is_not_accepted_as_record(self):
        aid=assets.upload('fundus.png',self.data)['asset_id']
        with self.assertRaises(ValueError):records.get(aid)
    def test_record_preview_has_no_local_disk_paths(self):
        result=records.upload('record.png',self.data)
        self.assertNotIn('original_path',result['record']);self.assertEqual(records.get(result['record']['asset_id'])['kind'],'medical_record')
    def test_empty_ocr_does_not_mean_no_disease(self):
        aid=self.record()
        with patch.object(records.subprocess,'run',return_value=Mock(stdout=b'')):
            with self.assertRaisesRegex(ValueError,'未识别到足够文字'):records.recognize(aid)
    def test_wrong_request_scope_cannot_read_record(self):
        aid=self.record();request_scope.register('a'*32,aid)
        with self.assertRaises(ValueError):request_scope.validate('a'*32,'b'*32)
        self.assertFalse(records.read_tool({'record_asset_id':aid,'request_id':'c'*32})['ok'])
    def test_ocr_is_marked_for_human_review_and_truncation(self):
        aid=self.record()
        with patch.object(records.subprocess,'run',return_value=Mock(stdout=('右眼'*4000).encode())):result=records.recognize(aid)
        self.assertTrue(result['record']['review_required']);self.assertTrue(result['record']['truncated']);self.assertEqual(len(result['record']['text']),6000)
if __name__=='__main__':unittest.main()
