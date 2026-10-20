"""Visitor isolation tests against a real local HTTP server, with fixture upstreams."""
import http.client,json,tempfile,threading,unittest,socket,socketserver
from pathlib import Path
from unittest.mock import patch
import public_gateway as public
from unittest.mock import Mock

class VisitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.dbpatch=patch.object(public,'DB',Path(cls.temp.name)/'visitors.sqlite');cls.dbpatch.start()
        cls.secret=patch.object(public.hosting,'settings',return_value={'api_key':'fixture-secret-not-a-real-key'});cls.secret.start()
        cls.server=public.PublicServer(('127.0.0.1',0),public.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.dbpatch.stop();cls.secret.stop();cls.temp.cleanup()
    def req(self,path,method='GET',body=None,cookie=None,origin=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Content-Type':'application/json'}
        if cookie:headers['Cookie']=cookie
        if origin:headers['Origin']=origin
        c.request(method,path,json.dumps(body) if body is not None else None,headers)
        r=c.getresponse();raw=r.read();out=(r.status,dict(r.getheaders()),raw);c.close();return out
    def visitor(self):
        status,headers,raw=self.req('/api/integration/status')
        self.assertEqual(status,200);self.assertNotIn(b'fixture-secret',raw)
        return headers['Set-Cookie'].split(';')[0]
    def test_no_nexent_login_needed_for_page(self):
        status,headers,raw=self.req('/console');self.assertEqual(status,200)
        self.assertIn('眼维'.encode(),raw);self.assertIn('HttpOnly',headers['Set-Cookie'])
    def test_management_routes_never_proxied(self):
        for path in ['/zh','/integration','/hosting-admin','/mcp','/api/user/tokens','/api/integration/models','/api/agent/list']:
            self.assertEqual(self.req(path)[0],404,path)
    def test_foreign_origin_rejected(self):
        self.assertEqual(self.req('/api/agent/run','POST',{'query':'左眼'},origin='https://foreign.example')[0],403)
    def test_unconfigured_service_fails_honestly(self):
        with patch.object(public.hosting,'settings',return_value={}):
            self.assertEqual(self.req('/api/agent/run','POST',{'query':'左眼'})[0],503)
    def test_uploaded_asset_is_bound_to_visitor(self):
        first=self.visitor();second=self.visitor();aid='a'*32
        with patch.object(public.gateway,'upstream_json',return_value={'ok':True,'asset':{'asset_id':aid}}):
            self.assertEqual(self.req('/api/upload','POST',{'filename':'x.jpg','data':'fixture'},first)[0],200)
        self.assertEqual(self.req('/api/asset?id='+aid,cookie=second)[0],403)
        self.assertEqual(self.req('/api/agent/run','POST',{'query':'右眼','yanwei_asset_id':aid},second)[0],403)
    def test_conversation_isolation_rejects_guessed_numeric_id(self):
        first=self.visitor();second=self.visitor();sid=first.split('=')[1]
        public.own(sid,'conversation','b'*32,'999')
        for cid in ['b'*32,999]:
            self.assertEqual(self.req('/api/agent/run','POST',{'query':'继续','conversation_id':cid},second)[0],403)
    def test_record_is_bound_to_visitor(self):
        first=self.visitor();second=self.visitor();aid='e'*32
        with patch.object(public.gateway,'upstream_json',return_value={'ok':True,'record':{'asset_id':aid}}):
            self.assertEqual(self.req('/api/records/upload','POST',{'filename':'record.png','data':'fixture'},first)[0],200)
        self.assertEqual(self.req('/api/records/image?id='+aid,cookie=second)[0],403)
        self.assertEqual(self.req('/api/agent/run','POST',{'query':'读取病历','record_asset_id':aid},second)[0],403)
    def test_record_run_uses_server_agent_and_registers_scope(self):
        h=public.Handler.__new__(public.Handler);h.sid='fixture-owner';h.client_address=('fixture-address',0)
        h.proxy=Mock();h.json=Mock();aid='f'*32;public.own(h.sid,'record',aid)
        with patch.object(public.request_scope,'register') as scope,patch.object(public.gateway,'config',return_value={'model_id':1}):
            h.run_visitor({'query':'识别病历','record_asset_id':aid,'model_id':999,'agent_name':'foreign-agent'})
        sent=json.loads(h.proxy.call_args.args[1])
        self.assertEqual(sent['agent_name'],'yanwei_integrated');self.assertEqual(sent['model_id'],1)
        self.assertEqual(scope.call_args.args[1],aid)
    def test_evidence_and_feedback_require_ownership(self):
        cookie=self.visitor()
        self.assertEqual(self.req('/api/evidence?id='+'d'*32,cookie=cookie)[0],403)
        self.assertEqual(self.req('/api/feedback','POST',{'case_id':'CASE_X','trace_id':'TRACE_X'},cookie)[0],403)
    def test_client_authentication_is_not_forwarded(self):
        h=public.Handler.__new__(public.Handler);h.target=public.hosting.NORTHBOUND
        self.assertEqual(h.auth_headers()['Cookie'],'')
        self.assertEqual(h.auth_headers()['Authorization'],'Bearer fixture-secret-not-a-real-key')
        h.target=public.gateway.RETRIEVAL;self.assertEqual(h.auth_headers()['Authorization'],'')
    def test_connection_limit_rejects_excess_clients(self):
        class Hold(socketserver.BaseRequestHandler):
            def handle(self):self.request.recv(1)
        server=public.PublicServer(('127.0.0.1',0),Hold)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        sockets=[]
        try:
            for _ in range(8):
                s=socket.create_connection(('127.0.0.1',server.server_port),timeout=5)
                sockets.append(s)
            with socket.create_connection(('127.0.0.1',server.server_port),timeout=5) as extra:
                self.assertIn(b'503 Service Unavailable',extra.recv(256))
        finally:
            for s in sockets:s.close()
            server.shutdown();server.server_close()
    def test_internal_events_removed(self):
        h=public.Handler.__new__(public.Handler)
        self.assertEqual(h.transform_packet(b'data: {"type":"execution_logs","content":"private"}'),b'')
    def test_non_object_body_is_controlled(self):
        self.assertEqual(self.req('/api/agent/run','POST',[])[0],400)

if __name__=='__main__':unittest.main()
