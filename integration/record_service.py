"""Private screenshot upload/OCR service, called by Nexent MCP tools."""
import argparse,json,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from yanwei import medical_records
slots=threading.BoundedSemaphore(2)
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data,status=200):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/health':return self.reply({'ok':True})
        u=urlsplit(self.path)
        if u.path!='/api/records/image':return self.reply({'error':'Not found'},404)
        try:record=medical_records.get(parse_qs(u.query).get('id',[''])[0]);raw=__import__('pathlib').Path(record['analysis_path']).read_bytes()
        except (ValueError,OSError):return self.reply({'error':'Not found'},404)
        self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        if not slots.acquire(False):return self.reply({'error':'OCR busy'},429)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=12*1024*1024:return self.reply({'error':'Invalid size'},413)
            value=json.loads(self.rfile.read(size))
            if not isinstance(value,dict):raise ValueError('参数必须是 JSON 对象')
            if self.path=='/api/records/upload':result=medical_records.upload(value.get('filename'),value.get('data'))
            elif self.path=='/api/records/recognize':result=medical_records.recognize(value.get('asset_id'))
            else:return self.reply({'error':'Not found'},404)
            self.reply(result)
        except (ValueError,OSError) as exc:self.reply({'ok':False,'error':{'message':str(exc)}},400)
        finally:slots.release()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--host',default='0.0.0.0');p.add_argument('--port',type=int,default=8236);a=p.parse_args();ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
