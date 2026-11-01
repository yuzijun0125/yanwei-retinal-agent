import json,tempfile,unittest
from pathlib import Path
import clinical_review as review

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.state=Path(self.tmp.name)
        path=review.provision(self.state);self.code=json.loads(path.read_text(encoding='utf-8'))['invitation_code']
        self.original={'trace_id':'TRACE_1','candidates':[{'source':{'case_id':'CASE_1'},'profile':{'eye':'OS'}}],'provenance':{'model_ver':'fixture'}}
        review.remember(self.state,'alice',self.original)
        self.data={'trace_id':'TRACE_1','case_id':'CASE_1','request_key':'once','verdict':'CORRECT','reason':'对照原始报告','corrections':{'eye':'OD'}}
    def test_auth_required(self):
        with self.assertRaises(PermissionError):review.save(self.state,'alice',self.data)
        with self.assertRaises(PermissionError):review.login(self.state,'alice','invalid')

    def test_permanent_login_and_rotation_revokes_it(self):
        from unittest.mock import patch
        result=review.login(self.state,'alice',self.code)
        self.assertTrue(result['permanent']);self.assertIsNone(result['expires_in'])
        with patch.object(review.time,'time',return_value=10**12):
            self.assertTrue(review.permanent(self.state,'alice'))
            self.assertEqual(review.history(self.state,'alice')['total'],0)
        (self.state/'reviewer.secret.json').write_text(json.dumps({'name':'团队医生复核员','invitation_code':'replacement'}),encoding='utf-8')
        self.assertFalse(review.permanent(self.state,'alice'))
        with self.assertRaises(PermissionError):review.history(self.state,'alice')
    def test_audited_original_and_idempotency(self):
        review.login(self.state,'alice',self.code)
        first=review.save(self.state,'alice',{**self.data,'baseline':{'forged':True}})
        self.assertEqual(first['record']['baseline'],self.original)
        self.assertEqual(first['record']['status'],'PENDING_TEAM_REVIEW')
        self.assertTrue(review.save(self.state,'alice',self.data)['duplicate'])
        with self.assertRaises(ValueError):review.save(self.state,'alice',{**self.data,'reason':'changed'})
        self.assertEqual(len(review.history(self.state,'alice')['records']),1)
    def test_scope_and_candidate(self):
        review.login(self.state,'bob',self.code)
        with self.assertRaises(ValueError):review.save(self.state,'bob',self.data)
        review.login(self.state,'alice',self.code)
        with self.assertRaises(PermissionError):review.save(self.state,'alice',{**self.data,'case_id':'FOREIGN'})
        self.assertEqual(review.history(self.state,'bob')['records'],[])
    def test_validation_and_expiry(self):
        review.login(self.state,'alice',self.code)
        for replacement in [{'reason':''},{'corrections':{}},{'corrections':{'unknown':'x'}},{'corrections':{'eye':7}}]:
            with self.assertRaises(ValueError):review.save(self.state,'alice',{**self.data,**replacement})
        from contextlib import closing
        with closing(review.connect(self.state)) as c:c.execute('UPDATE logins SET expires=0');c.commit()
        with self.assertRaises(PermissionError):review.history(self.state,'alice')

    def test_team_history_survives_session_change_and_filters(self):
        review.login(self.state,'alice',self.code)
        saved=review.save(self.state,'alice',self.data)['record']
        with self.assertRaises(PermissionError):review.history(self.state,'new-session')
        review.login(self.state,'new-session',self.code)
        self.assertEqual(review.history(self.state,'new-session')['total'],1)
        self.assertEqual(review.history(self.state,'new-session',{'q':saved['record_id']})['total'],1)
        self.assertEqual(review.history(self.state,'new-session',{'verdict':'REJECT'})['total'],0)
        self.assertEqual(review.history(self.state,'new-session',{'q':'not-present'})['total'],0)
        self.assertEqual(review.history(self.state,'new-session',{'page':2})['records'],[])
        with self.assertRaises(ValueError):review.history(self.state,'new-session',{'page':'invalid'})

    def test_legacy_feedback_is_read_only_unverified_and_authenticated(self):
        path=self.state/'legacy.jsonl'
        path.write_text(json.dumps({'record_id':'OLD1','case_id':'CASE_OLD','verdict':'ACCEPT','note':'旧备注','idempotency_key':'private-session'})+'\n',encoding='utf-8')
        with self.assertRaises(PermissionError):review.legacy_history(self.state,'alice',path)
        review.login(self.state,'alice',self.code)
        data=review.legacy_history(self.state,'alice',path,{'q':'CASE_OLD'})
        self.assertEqual(data['total'],1)
        self.assertTrue(data['records'][0]['legacy'])
        self.assertNotIn('idempotency_key',data['records'][0])
        self.assertEqual(review.history(self.state,'alice')['total'],0)

    def test_release_gate_case_scope_and_rollback(self):
        import review_release as release
        review.login(self.state,'alice',self.code)
        rec=review.save(self.state,'alice',self.data)['record']
        first=release.prepare(self.state,rec['record_id'],'复核说明一，测试用')
        self.assertNotIn('reviewed_context',release.enrich(self.state,dict(self.original)))
        with self.assertRaises(ValueError):release.activate(self.state,first['version'],{},True)
        report={'version':first['version'],'passed':True,'dataset_id':'fixture-not-clinical','metrics':{'test':1}}
        with self.assertRaises(ValueError):release.activate(self.state,first['version'],report,False)
        release.activate(self.state,first['version'],report,True)
        self.assertEqual(release.enrich(self.state,dict(self.original))['reviewed_context']['notes'][0]['lesson'],'复核说明一，测试用')
        self.assertNotIn('reviewed_context',release.enrich(self.state,{'candidates':[{'source':{'case_id':'FOREIGN'}}]}))
        second=release.prepare(self.state,rec['record_id'],'复核说明二，测试用')
        release.activate(self.state,second['version'],{**report,'version':second['version']},True)
        release.rollback(self.state,first['version'])
        self.assertEqual(release.active(self.state)['version'],first['version'])

if __name__=='__main__':unittest.main()
