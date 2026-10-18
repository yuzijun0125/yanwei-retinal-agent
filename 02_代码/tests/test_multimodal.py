from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from yanwei import multimodal as mm


class MultimodalTests(unittest.TestCase):
    def test_legacy_path_is_mapped_but_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            result=mm.resolve_image(root,{'raw_path':'01_数据集本体/图像/IDRiD/a.jpg'})
            self.assertEqual(result,Path(root)/'图像'/'IDRiD'/'a.jpg')
            with self.assertRaises(ValueError):
                mm.resolve_image(root,{'raw_path':'../../private.jpg'})

    def test_english_mapping_does_not_invent_translation(self):
        self.assertIn('hemorrhages',mm.english_query({'lesion':'HE','location':'MACULA'}))
        self.assertIsNone(mm.english_query({'eye':'OS'}))
        self.assertIsNone(mm.english_query({'lesion':'HE'},['lesion']))

    def test_disabled_model_returns_explicit_fallback(self):
        with patch.object(mm,'status',return_value={'ready':False,'reason':'offline'}):
            hits,info=mm.recall('test.jpg',{'lesion':'HE'})
        self.assertEqual(hits,{})
        self.assertFalse(info['used'])
        self.assertEqual(info['reason'],'offline')

    def test_reference_crop_uses_xywh_and_rejects_bad_coordinates(self):
        from PIL import Image
        import io
        with tempfile.TemporaryDirectory() as root:
            Image.new('RGB',(200,100),'red').save(Path(root)/'case.jpg')
            result=mm.thumbnail(root,{'raw_path':'case.jpg'},'[10,20,30,40]')
            with Image.open(io.BytesIO(result)) as crop:
                self.assertEqual(crop.size,(30,40))
            with self.assertRaises(ValueError):
                mm.thumbnail(root,{'raw_path':'case.jpg'},'[-1,20,30,40]')


if __name__=='__main__':
    unittest.main()
