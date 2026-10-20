"""Local OCR for medical record screenshots; never treat these as fundus images."""
import json,os,re,subprocess,time
from pathlib import Path
from . import assets

def upload(filename,encoded):
    rec=assets.upload(filename,encoded)
    rec['kind']='medical_record'
    (assets.folder()/(rec['asset_id']+'.json')).write_text(json.dumps(rec,ensure_ascii=False),encoding='utf-8')
    return {'ok':True,'record':{'asset_id':rec['asset_id'],'filename':rec['filename'],'width':rec['width'],'height':rec['height'],'preview_url':'/api/records/image?id='+rec['asset_id']}}

def get(record_id):
    rec=assets.get(record_id)
    if rec.get('kind')!='medical_record':raise ValueError('此附件不是病历截图，请从病历入口重新导入')
    return rec

def recognize(record_id):
    rec=get(record_id)
    cache=assets.folder()/(record_id+'.ocr.json')
    if cache.is_file():return json.loads(cache.read_text(encoding='utf-8'))
    start=time.monotonic()
    try:
        process=subprocess.run(['tesseract',rec['original_path'],'stdout','-l','chi_sim+eng','--psm','6'],capture_output=True,timeout=30,check=True)
    except (OSError,subprocess.SubprocessError) as exc:raise ValueError('病历文字识别暂不可用，请上传清晰截图或手动录入') from exc
    text=process.stdout.decode('utf-8',errors='replace').strip()
    if len(text)<4:raise ValueError('未识别到足够文字，请更换清晰病历截图；不能据此判定没有异常')
    truncated=len(text)>6000;text=text[:6000]
    result={'ok':True,'type':'medical_record','record':{'asset_id':record_id,'filename':rec['filename'],'text':text,'engine':'Tesseract chi_sim+eng','review_required':True,'truncated':truncated,'notice':'文字为自动识别，可能存在眼别、数字或术语错误；请核对修改后再检索。'},'elapsed_ms':round((time.monotonic()-start)*1000)}
    cache.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result

def read_tool(payload,rt=None):
    try:
        record_id=payload.get('record_asset_id')
        if not re.fullmatch('[a-f0-9]{32}',str(record_id)):raise ValueError('病历资产 ID 无效')
        from . import request_scope
        request_scope.validate(payload.get('request_id'),record_id)
        service=os.environ.get('RECORD_OCR_SERVICE')
        if service:
            import urllib.request
            body=json.dumps({'asset_id':record_id}).encode()
            req=urllib.request.Request(service.rstrip('/')+'/api/records/recognize',body,{'Content-Type':'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=45) as response:return json.load(response)
        return recognize(record_id)
    except (ValueError,OSError) as exc:
        return {'ok':False,'type':'medical_record','error':{'code':'RECORD_OCR_FAILED','message':str(exc) if isinstance(exc,ValueError) else '病历识别服务不可用，请稍后重试'}}
