"""Enrollment must not rotate existing API keys or return secrets to browsers."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import hosting

class HostingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'hosting.secret.json'
        self.patches=[patch.object(hosting,'STATE',Path(self.temp.name)),patch.object(hosting,'SECRET',self.path)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_cached_key_still_requires_admin_login(self):
        self.path.write_text('{"api_key":"fixture-existing"}',encoding='utf-8')
        with patch.object(hosting,'upstream_json',return_value={'data':None}):
            with self.assertRaises((ValueError,AttributeError)):hosting.enable({})
    def test_existing_key_reused_without_rotation(self):
        calls=[]
        def api(base,path,*args,**kwargs):
            calls.append((path,args))
            if 'current_user' in path:return {'data':{'user':{'id':'owner'}}}
            if '/tokens?' in path:return {'data':[{'access_key':'fixture-existing','can_copy':True}]}
            return {'name':'yanwei_integrated'}
        with patch.object(hosting,'upstream_json',side_effect=api):result=hosting.enable({'Cookie':'fixture'})
        self.assertTrue(result['reused']);self.assertNotIn('fixture-existing',json.dumps(result))
        self.assertFalse(any(p=='/api/user/tokens' for p,a in calls))
    def test_masked_key_does_not_trigger_rotation(self):
        def api(base,path,*args,**kwargs):
            if 'current_user' in path:return {'data':{'user':{'id':'owner'}}}
            if '/tokens?' in path:return {'data':[{'access_key':'masked***','can_copy':False}]}
            raise AssertionError('Unexpected key creation')
        with patch.object(hosting,'upstream_json',side_effect=api):
            with self.assertRaises(ValueError):hosting.enable({})
        self.assertFalse(self.path.exists())
    def test_key_creation_only_when_no_active_keys(self):
        def api(base,path,*args,**kwargs):
            if 'current_user' in path:return {'data':{'user':{'id':'owner'}}}
            if '/tokens?' in path:return {'data':[]}
            if path=='/api/user/tokens':return {'data':{'access_key':'fixture-new'}}
            return {'name':'yanwei_integrated'}
        with patch.object(hosting,'upstream_json',side_effect=api):result=hosting.enable({})
        self.assertTrue(result['configured']);self.assertNotIn('fixture-new',json.dumps(result))
        self.assertEqual(json.loads(self.path.read_text())['api_key'],'fixture-new')

if __name__=='__main__':unittest.main()
