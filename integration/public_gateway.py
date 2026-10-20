"""Visitor-only Nexent gateway; administrator credentials never reach visitors."""
import argparse
from contextlib import closing
from collections import defaultdict, deque
from http.cookies import SimpleCookie
import json, re, sqlite3, threading, time, uuid, os
from urllib.parse import urlsplit, parse_qs
import gateway
import hosting
import sys
sys.path.insert(0,str(gateway.ROOT/'02_代码'))
from yanwei import request_scope

RECORD_SERVICE=os.environ.get('RECORD_SERVICE',gateway.RETRIEVAL).rstrip('/')
DB=gateway.STATE/'visitors.sqlite'
_LIMIT=threading.BoundedSemaphore(2)
_RATE=defaultdict(deque)
_ACTIVE=set()
_LOCK=threading.Lock()

def sql(statement,args=(),one=False):
    DB.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(DB,timeout=5)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, at REAL)')
        conn.execute('CREATE TABLE IF NOT EXISTS objects (sid TEXT,kind TEXT,id TEXT,remote TEXT,PRIMARY KEY(sid,kind,id))')
        cur=conn.execute(statement,args)
        conn.commit()
        return cur.fetchone() if one else cur.fetchall()

def own(sid,kind,value,remote=None):
    sql('INSERT OR REPLACE INTO objects VALUES (?,?,?,?)',(sid,kind,str(value),remote))

def owned(sid,kind,value):
    return sql('SELECT remote FROM objects WHERE sid=? AND kind=? AND id=?',(sid,kind,str(value)),True)

class Handler(gateway.Handler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self):
        self.response_started=True
        self.close_connection=True
        super().send_header('Connection','close')
        if getattr(self,'new_session',False):
            super().send_header('Set-Cookie','yanwei_visitor='+self.sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400'+('; Secure' if __import__('os').environ.get('PUBLIC_HTTPS')=='1' else ''))
            self.new_session=False
        super().send_header('X-Content-Type-Options','nosniff')
        super().send_header('X-Frame-Options','DENY')
        super().end_headers()

    def send_header(self,name,value):
        if name.lower()=='conversation_id':
            if str(value).isdigit():own(self.sid,'conversation',self.ticket,str(value))
            value=self.ticket
        if name.lower() in {'set-cookie','location'}:return
        super().send_header(name,value)

    def auth_headers(self):
        return {'Cookie':'','Authorization':('Bearer '+hosting.settings().get('api_key','')) if getattr(self,'target','')==hosting.NORTHBOUND else ''}

    def proxy(self,base,body,override=None,enrich=None):
        self.target=base
        return super().proxy(base,body,override,enrich)

    def handle_proxy_error(self,status):
        if status>=400:
            self.json({'error':{'message':'后台服务暂不可用，请稍后重试或联系项目管理员'}},503)
            return True
        return False

    def transform_packet(self,packet):
        output=[]
        for line in packet.splitlines():
            if not line.startswith(b'data:'):continue
            try:value=json.loads(line[5:])
            except ValueError:continue
            kind=value.get('type')
            if kind in {'tool','tool_call'}:
                self.tool_started=True
            if kind=='conversation_created':
                content=value.get('content',{})
                if isinstance(content,str):
                    try:content=json.loads(content)
                    except ValueError:content={}
                rid=content.get('conversation_id') if isinstance(content,dict) else None
                if str(rid).isdigit():own(self.sid,'conversation',self.ticket,str(rid))
                value={'type':kind,'content':{'conversation_id':self.ticket}}
            elif kind=='final_answer':
                value={'type':kind,'content':value.get('content','')}
                if not getattr(self,'verified_result',False):
                    if not hasattr(self,'waiting_answers'):self.waiting_answers=[]
                    self.waiting_answers.append(value)
                    continue
            elif kind=='error':value={'type':'error','content':'本轮智能体执行失败，请稍后重试'}
            else:continue
            output.append(('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
        return b''.join(output)

    def accept_result(self,result):
        if result.get('type')=='medical_record' and result.get('ok'):
            if not owned(self.sid,'record',result.get('record',{}).get('asset_id')):raise ValueError('病历不属于当前访客')
        asset=result.get('image_asset_id')
        if asset and not owned(self.sid,'asset',asset):
            raise ValueError('本轮图片不属于当前访客会话')
        self.verified_result=True
        self.flush_answers()
        evidence=result.get('evidence') or {}
        for kind,value in [('evidence',evidence.get('evidence_id')),('trace',result.get('trace_id'))]:
            if value:own(self.sid,kind,value)
        for item in result.get('candidates',[]):
            cid=item.get('source',{}).get('case_id')
            if cid:own(self.sid,'case',cid)

    def flush_answers(self):
        for value in getattr(self,'waiting_answers',[]):
            self.wfile.write(('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
        self.waiting_answers=[]
        self.wfile.flush()

    def finish_stream(self,result_ready):
        if result_ready or not getattr(self,'tool_started',False):self.flush_answers()

    def stop_agent(self,run_id):
        gateway.upstream_json(hosting.NORTHBOUND,'/nb/v1/chat/stop/'+str(run_id),headers={'Authorization':'Bearer '+hosting.settings()['api_key']},timeout=5)

    def session(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:pass
        sid=cookie.get('yanwei_visitor')
        sid=sid.value if sid else ''
        if not re.fullmatch('[a-f0-9]{32}',sid) or not sql('SELECT sid FROM sessions WHERE sid=? AND at>?',(sid,time.time()-86400),True):
            sid=uuid.uuid4().hex;sql('INSERT INTO sessions VALUES (?,?)',(sid,time.time()));self.new_session=True
        self.sid=sid

    def dispatch(self):
        self.response_started=False
        self.new_session=False
        path=urlsplit(self.path).path
        try:
            if self.command not in {'GET','POST'}:return self.json({'error':{'message':'不支持此操作'}},405)
            if path=='/health' and self.command=='GET':return self.json({'ok':True})
            if self.command=='POST':
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=12*1024*1024:return self.json({'error':{'message':'请求为空或过大'}},413)
                raw_body=self.rfile.read(size)
                origin=self.headers.get('Origin')
                if origin and urlsplit(origin).netloc!=self.headers.get('Host'):return self.json({'error':{'message':'请从眼维页面提交'}},403)
            self.session()
            if path in {'/','/console'} and self.command=='GET':return self.file(gateway.WEB/'public-console.html')
            if path=='/static/tailwindcss-3.4.17.js' and self.command=='GET':return self.file(gateway.WEB/'tailwindcss-3.4.17.js')
            if path=='/api/integration/status' and self.command=='GET':
                return self.json({'ok':True,'hosted':True,'configured':bool(hosting.settings().get('api_key')),'agent':{'agent_id':1,'version_no':1}})
            if path=='/api/records/image' and self.command=='GET':
                value=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                if not owned(self.sid,'record',value):return self.json({'error':{'message':'病历不属于当前访客'}},403)
                return self.proxy(RECORD_SERVICE,b'')
            if path=='/api/case-image' and self.command=='GET':return self.proxy(gateway.RETRIEVAL,b'')
            if path in {'/api/asset','/api/evidence','/api/trace'} and self.command=='GET':
                kind={'/api/asset':'asset','/api/evidence':'evidence','/api/trace':'trace'}[path]
                value=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                if not owned(self.sid,kind,value):return self.json({'error':{'message':'此内容不属于当前访客或会话已过期'}},403)
                return self.proxy(gateway.RETRIEVAL,b'')
            if path not in {'/api/upload','/api/records/upload','/api/feedback','/api/agent/run'} or self.command!='POST':
                self.close_connection=True
                return self.json({'error':{'message':'访客入口不提供后台管理接口'}},404)
            if not hosting.settings().get('api_key'):return self.json({'error':{'message':'管理员尚未连接智能体，请稍后再试'}},503)
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=12*1024*1024:return self.json({'error':{'message':'请求为空或过大'}},413)
            value=json.loads(raw_body)
            if not isinstance(value,dict):raise ValueError('参数必须为 JSON 对象')
            if path=='/api/records/upload':
                result=gateway.upstream_json(RECORD_SERVICE,path,'POST',{'filename':value.get('filename'),'data':value.get('data')})
                aid=(result.get('record') or {}).get('asset_id')
                if aid:own(self.sid,'record',aid)
                return self.json(result)
            if path=='/api/upload':
                result=gateway.upstream_json(gateway.RETRIEVAL,path,'POST',{'filename':value.get('filename'),'data':value.get('data')})
                asset=(result.get('asset') or {}).get('asset_id')
                if asset:own(self.sid,'asset',asset)
                return self.json(result)
            if path=='/api/feedback':
                if not owned(self.sid,'case',value.get('case_id')) or not owned(self.sid,'trace',value.get('trace_id')):
                    return self.json({'error':{'message':'请先完成当前会话检索再反馈'}},403)
                value['idempotency_key']=self.sid+':'+str(value.get('idempotency_key',''))[:300]
                return self.json(gateway.upstream_json(gateway.RETRIEVAL,path,'POST',value))
            return self.run_visitor(value)
        except ValueError as exc:
            if self.response_started:
                self.close_connection=True
                return
            self.json({'error':{'message':str(exc)}},400)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            self.close_connection=True
        except (OSError,__import__('http.client').client.HTTPException,sqlite3.Error):
            if self.response_started:
                self.close_connection=True
                return
            self.json({'error':{'message':'服务暂不可用，请稍后重试'}},503)

    def run_visitor(self,value):
        text=value.get('query')
        if not isinstance(text,str) or not text.strip() or len(text)>8000:raise ValueError('请输入 1～8000 字的病例描述')
        asset=value.get('yanwei_asset_id')
        if asset and not owned(self.sid,'asset',asset):return self.json({'error':{'message':'图片不属于当前访客，请重新上传'}},403)
        record=value.get('record_asset_id')
        if record and not owned(self.sid,'record',record):return self.json({'error':{'message':'病历不属于当前访客，请重新导入'}},403)
        ticket=value.get('conversation_id')
        remote=None
        if ticket:
            row=owned(self.sid,'conversation',ticket)
            if not row:return self.json({'error':{'message':'会话已过期或不属于当前访客，请新建对话'}},403)
            remote=row[0]
        self.ticket=ticket or uuid.uuid4().hex
        with _LOCK:
            ip=self.client_address[0]
            now=time.monotonic();q=_RATE[ip]
            while q and now-q[0]>60:q.popleft()
            if len(q)>=12 or self.sid in _ACTIVE:return self.json({'error':{'message':'请求较多，请稍后重试'}},429)
            if not _LIMIT.acquire(False):return self.json({'error':{'message':'当前有其他检索任务，请稍后重试'}},429)
            q.append(now);_ACTIVE.add(self.sid)
        try:
            rid=uuid.uuid4().hex
            if record:request_scope.register(rid,record)
            query=text+'\n\n[YANWEI_ROUTE] '+json.dumps({'request_id':rid,'image_asset_id':asset or None,'record_asset_id':record or None},ensure_ascii=False)+'\n调用 search_cases 时逐字传入本轮 request_id 和非空 image_asset_id；根据本会话历史理解用户补充。'
            if record:query+='\n本轮仅调用 read_medical_record 识别病历截图；识别后等待用户核对，不要调用 search_cases 检索。'
            payload={'agent_name':'yanwei_integrated','query':query,'model_id':gateway.config().get('model_id')}
            if remote:payload['conversation_id']=int(remote)
            own(self.sid,'conversation',self.ticket,remote)
            return self.proxy(hosting.NORTHBOUND,json.dumps(payload,ensure_ascii=False).encode(),'/nb/v1/chat/run',rid)
        finally:
            with _LOCK:_ACTIVE.discard(self.sid);_LIMIT.release()

class PublicServer(gateway.ThreadingHTTPServer):
    """Bound connections as well as expensive agent executions."""
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(*args,**kwargs)

    def process_request(self,request,address):
        if not self.slots.acquire(False):
            try:request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:pass
            self.shutdown_request(request)
            return
        try:super().process_request(request,address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

def main():
    p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=18881);a=p.parse_args()
    server=PublicServer((a.host,a.port),Handler)
    print('眼维访客入口：http://localhost:'+str(a.port)+'/console',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
