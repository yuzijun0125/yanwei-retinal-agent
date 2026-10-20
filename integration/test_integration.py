"""Protocol and request-isolation regressions; no external model calls."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'02_代码'))
import gateway
import bootstrap
from yanwei import integration_results as registry, mcp_tools

class IntegrationTests(unittest.TestCase):
    def handler(self):
        h=gateway.Handler.__new__(gateway.Handler)
        h.wfile=io.BytesIO();h.headers={'Host':'localhost:18880'}
        h.proxy=Mock(return_value='proxied');h.json=Mock()
        return h
    def test_agent_is_server_owned_and_request_id_is_random(self):
        h=self.handler()
        with patch.object(gateway,'config',return_value={'agent_id':6,'model_id':1,'version_no':2}):
            h.agent_run(json.dumps({'query':'左眼','agent_id':999,'model_id':999,'minio_files':['x']}))
        sent=json.loads(h.proxy.call_args.args[1])
        self.assertEqual((sent['agent_id'],sent['model_id'],sent['version_no']),(6,1,2))
        self.assertNotIn('minio_files',sent)
        self.assertTrue(registry.valid(h.proxy.call_args.kwargs['enrich']))
    def test_unconfigured_agent_does_not_run(self):
        h=self.handler()
        with patch.object(gateway,'config',return_value={}):h.agent_run('{}')
        self.assertEqual(h.json.call_args.args[1],409);h.proxy.assert_not_called()
    def test_bad_asset_rejected_before_upstream(self):
        h=self.handler()
        with patch.object(gateway,'config',return_value={'agent_id':6}),self.assertRaises(ValueError):
            h.agent_run(json.dumps({'query':'x','yanwei_asset_id':'../../secret'}))
        h.proxy.assert_not_called()
    def test_non_object_query_rejected(self):
        with self.assertRaises(ValueError):self.handler().agent_run('[]')
    def test_registry_isolated_and_expired(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(registry,'default_output_dir',return_value=folder):
            rid='a'*32;registry.save(rid,{'action':'ASK_MORE'})
            self.assertEqual(registry.read(rid)['action'],'ASK_MORE')
            with self.assertRaises(ValueError):registry.read('b'*32)
            with self.assertRaises(ValueError):registry.read('../secret')
            with patch.object(registry.time,'time',return_value=10**12):
                with self.assertRaises(ValueError):registry.read(rid)
    def test_tools_reject_non_object(self):
        self.assertFalse(mcp_tools.call_tool('search_cases',['bad'])['ok'])
    def test_upstream_list_shape(self):
        self.assertEqual(bootstrap.rows({'remote_mcp_server_list':[{'mcp_id':4}]}),[{'mcp_id':4}])
    def test_sse_utf8_boundary_and_actual_result(self):
        h=self.handler();r=Mock();conn=Mock()
        r.getheader.return_value=''
        raw=('data: '+json.dumps({'type':'final_answer','content':'右眼'},ensure_ascii=False)+'\n\n').encode()
        split=raw.index('右'.encode())+1
        r.read1.side_effect=[raw[:split],raw[split:],b'']
        with patch.object(gateway,'upstream_json',return_value={'action':'RETURN_CASES'}):
            h.stream_agent(conn,r,'a'*32)
        out=h.wfile.getvalue().decode('utf-8')
        self.assertIn('右眼',out);self.assertIn('actual-mcp-tool-result',out)
    def test_missing_registry_has_no_invented_candidates(self):
        h=self.handler();r=Mock();r.getheader.return_value='';r.read1.return_value=b''
        with patch.object(gateway,'upstream_json',side_effect=ValueError('missing')):
            h.stream_agent(Mock(),r,'a'*32)
        out=h.wfile.getvalue().decode()
        self.assertIn('yanwei_status',out);self.assertNotIn('yanwei_result',out)
    def test_wall_timeout_stops_only_response_conversation(self):
        h=self.handler();r=Mock();r.getheader.return_value='123';r.read1.return_value=b''
        with patch.object(gateway.time,'monotonic',side_effect=[0,151]),patch.object(gateway,'upstream_json') as api:
            h.stream_agent(Mock(),r,'a'*32)
        self.assertEqual(api.call_args.args[1],'/api/agent/stop/123')
        self.assertIn('yanwei_status',h.wfile.getvalue().decode())

if __name__=='__main__':unittest.main()
