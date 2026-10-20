"""Run real Linux CPU retrieval over HTTP with shipped weights and sample."""
import base64,json,sys,threading,time,urllib.request
from pathlib import Path
from yanwei import server
start=time.monotonic()
httpd=server.serve('127.0.0.1',0)
threading.Thread(target=httpd.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(httpd.server_address[1])
def api(path,data=None):
    body=json.dumps(data,ensure_ascii=False).encode() if data is not None else None
    req=urllib.request.Request(base+path,body,{'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=90) as response:return json.load(response)
try:
    image=Path('/app/01_数据集/图像/IDRiD/原始图像/Testing set/IDRiD_55.jpg')
    if not image.is_file():
        image=next(Path('/app/01_数据集').rglob('IDRiD_55.jpg'))
    asset=api('/api/upload',{'filename':'IDRiD_55.jpg','data':base64.b64encode(image.read_bytes()).decode()})['asset']['asset_id']
    rid='b'*32
    result=api('/api/tool',{'name':'search_cases','arguments':{'query_text':'右眼黄斑区散在出血','image_asset_id':asset,'request_id':rid,'complete_flow':True,'topk':3}})
    assert result.get('ok'),result.get('error')
    assert result.get('multimodal',{}).get('used'),result.get('multimodal')
    assert result['multimodal']['query_kind']=='image_text'
    actual=api('/api/integration/result?id='+rid)
    assert actual['trace_id']==result['trace_id']
    with urllib.request.urlopen(base+result['candidates'][0]['image_url'],timeout=10) as response:
        assert response.status==200 and response.read(2)==b'\xff\xd8'
    print(json.dumps({'ok':True,'environment':'Linux Docker CPU / shipped weights','action':result['action'],'candidate_count':len(result['candidates']),'query_kind':result['multimodal']['query_kind'],'encoder':result['multimodal']['encoder'],'tool_elapsed_ms':result['elapsed_ms'],'startup_and_flow_ms':round((time.monotonic()-start)*1000),'registry_matched':True,'image_http_ok':True},ensure_ascii=False),flush=True)
finally:
    httpd.shutdown();httpd.server_close()
