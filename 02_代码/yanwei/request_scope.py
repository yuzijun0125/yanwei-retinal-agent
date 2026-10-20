"""Bind medical record reads to one server-authorized Nexent request."""
import json,re,time
from pathlib import Path
from .config import default_output_dir

def register(request_id,record_asset_id):
    root=Path(default_output_dir())/'integration'/'record_scopes';root.mkdir(parents=True,exist_ok=True)
    target=root/(request_id+'.json');target.write_text(json.dumps({'record_asset_id':record_asset_id,'at':time.time()}),encoding='utf-8')

def validate(request_id,record_asset_id):
    if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):raise ValueError('病历读取缺少本轮授权，请重新发送')
    path=Path(default_output_dir())/'integration'/'record_scopes'/(request_id+'.json')
    try:scope=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,OSError) as exc:raise ValueError('本轮未授权读取病历') from exc
    if time.time()-scope['at']>300 or scope['record_asset_id']!=record_asset_id:raise ValueError('病历与本轮授权不符或已过期')
