"""Administrator-only enrollment of a server-side Nexent API key."""
import json,os,threading
from gateway import STATE,NEXENT,upstream_json
SECRET=STATE/'hosting.secret.json'
NORTHBOUND=os.environ.get('NEXENT_NORTHBOUND_UPSTREAM','http://127.0.0.1:5013').rstrip('/')

def settings():
    try:return json.loads(SECRET.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

_LOCK=threading.Lock()

def enable(headers):
    with _LOCK:return _enable(headers)

def _enable(headers):
    profile=upstream_json(NEXENT,'/api/user/current_user_info',headers=headers)
    user=(profile.get('data') or {}).get('user') or {}
    uid=user.get('id') or user.get('user_id')
    if not uid:raise ValueError('请先在本机管理入口登录 Nexent')
    old=settings()
    if old.get('api_key'):
        upstream_json(NORTHBOUND,'/nb/v1/agents/yanwei_integrated',headers={'Authorization':'Bearer '+old['api_key']},timeout=15)
        return {'ok':True,'configured':True,'reused':True}
    from urllib.parse import urlencode
    data=upstream_json(NEXENT,'/api/user/tokens?'+urlencode({'user_id':uid}),headers=headers).get('data',[])
    if isinstance(data,dict):data=data.get('tokens',data.get('items',[]))
    # Never rotate or revoke an existing user's key.
    if data:
        token=next((t for t in data if t.get('can_copy') and '*' not in t.get('access_key','')),None)
        if not token:raise ValueError('现有 API Key 不允许读取，请使用有权限的管理员登录；未重置任何密钥')
    else:
        token=upstream_json(NEXENT,'/api/user/tokens','POST',{},headers).get('data',{})
    key=token.get('access_key')
    if not key:raise ValueError('未取得服务端 API Key，未启用访客入口')
    upstream_json(NORTHBOUND,'/nb/v1/agents/yanwei_integrated',headers={'Authorization':'Bearer '+key},timeout=15)
    STATE.mkdir(parents=True,exist_ok=True)
    tmp=SECRET.with_suffix('.tmp');tmp.write_text(json.dumps({'api_key':key,'agent_name':'yanwei_integrated'},ensure_ascii=False),encoding='utf-8')
    try:tmp.chmod(0o600)
    except OSError:pass
    tmp.replace(SECRET)
    return {'ok':True,'configured':True,'reused':bool(data)}
