"""Authenticated, session-scoped review audit. No automatic training or publication."""
import hashlib,hmac,json,secrets,sqlite3,time,uuid
from pathlib import Path
from contextlib import closing

def connect(state):
    state.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(state/'clinical-review.sqlite',timeout=10)
    c.execute('CREATE TABLE IF NOT EXISTS snapshots (sid TEXT, trace TEXT, payload TEXT, PRIMARY KEY(sid,trace))')
    c.execute('CREATE TABLE IF NOT EXISTS logins (sid TEXT PRIMARY KEY, reviewer TEXT, expires REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS login_codes (sid TEXT PRIMARY KEY, fingerprint TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY,sid TEXT,trace TEXT,case_id TEXT,request_key TEXT,payload TEXT, UNIQUE(sid,request_key))')
    return c

def provision(state):
    """Local team invitation. Never served over public HTTP or packaged with source."""
    path=state/'reviewer.secret.json'
    if not path.exists():
        token=secrets.token_urlsafe(24)
        path.write_text(json.dumps({'name':'团队医生复核员','invitation_code':token},ensure_ascii=False),encoding='utf-8')
    return path

def login(state,sid,code):
    path=state/'reviewer.secret.json'
    if not path.exists():raise PermissionError('管理员尚未配置复核授权')
    account=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(code,str) or not hmac.compare_digest(code,account['invitation_code']):
        raise PermissionError('复核授权码不正确')
    with closing(connect(state)) as c:
        c.execute('INSERT OR REPLACE INTO logins VALUES (?,?,?)',(sid,account['name'],-1))
        c.execute('INSERT OR REPLACE INTO login_codes VALUES (?,?)',(sid,hashlib.sha256(code.encode()).hexdigest()));c.commit()
    return {'ok':True,'reviewer':account['name'],'expires_in':None,'permanent':True}

def remember(state,sid,result):
    trace=result.get('trace_id')
    if not trace:return
    # Persist only the authoritative tool result, not a browser-provided baseline.
    with closing(connect(state)) as c:
        c.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',(sid,trace,json.dumps(result,ensure_ascii=False)));c.commit()

def reviewer(c,sid):
    row=c.execute('SELECT reviewer FROM logins WHERE sid=? AND (expires=-1 OR expires>?)',(sid,time.time())).fetchone()
    if not row:raise PermissionError('请先输入团队授权码，获得长期复核权限')
    stored=c.execute('SELECT fingerprint FROM login_codes WHERE sid=?',(sid,)).fetchone()
    db=Path(c.execute('PRAGMA database_list').fetchone()[2])
    try:account=json.loads((db.parent/'reviewer.secret.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise PermissionError('团队授权已撤销，请联系管理员')
    if not stored or not hmac.compare_digest(stored[0],hashlib.sha256(account['invitation_code'].encode()).hexdigest()):
        raise PermissionError('授权码已更换，请重新授权')
    return row[0]

def permanent(state,sid):
    with closing(connect(state)) as c:
        try:reviewer(c,sid)
        except PermissionError:return False
        return c.execute('SELECT expires FROM logins WHERE sid=?',(sid,)).fetchone()[0]==-1

def save(state,sid,data):
    trace=data.get('trace_id');case=data.get('case_id');key=data.get('request_key')
    if not all(isinstance(v,str) and 0<len(v)<=180 for v in (trace,case,key)):raise ValueError('请选择本轮候选并重新提交')
    verdict=data.get('verdict')
    if verdict not in ('ACCEPT','REJECT','CORRECT','UNKNOWN'):raise ValueError('无效的复核结论')
    reason=data.get('reason','');corrections=data.get('corrections',{})
    if not isinstance(reason,str) or len(reason)>3000:raise ValueError('修改依据限3000字')
    if verdict in ('CORRECT','REJECT') and not reason.strip():raise ValueError('纠正或不采纳时请填写依据')
    allowed={'eye','lesion','location','severity','modality','interpretation'}
    if not isinstance(corrections,dict) or set(corrections)-allowed:raise ValueError('修改字段无效')
    if any(not isinstance(v,str) or len(v)>1500 for v in corrections.values()):raise ValueError('修改内容过长')
    if verdict=='CORRECT' and not any(v.strip() for v in corrections.values()):raise ValueError('请填写至少一项修改')
    with closing(connect(state)) as c:
        name=reviewer(c,sid)
        row=c.execute('SELECT payload FROM snapshots WHERE sid=? AND trace=?',(sid,trace)).fetchone()
        if not row:raise ValueError('未找到本会话的原始检索结果，请重新检索')
        baseline=json.loads(row[0]);candidate=next((x for x in baseline.get('candidates',[]) if x.get('source',{}).get('case_id')==case),None)
        if not candidate:raise PermissionError('该病例不是本轮返回的候选')
        content={'trace_id':trace,'case_id':case,'verdict':verdict,'reason':reason,'corrections':corrections}
        fingerprint=hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        old=c.execute('SELECT payload FROM reviews WHERE sid=? AND request_key=?',(sid,key)).fetchone()
        if old:
            rec=json.loads(old[0])
            if rec['fingerprint']!=fingerprint:raise ValueError('提交编号重复但内容不同，请重新提交')
            return {'ok':True,'record':rec,'duplicate':True}
        rec={**content,'record_id':'REV-'+uuid.uuid4().hex,'reviewer':name,'at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             'status':'PENDING_TEAM_REVIEW','fingerprint':fingerprint,'baseline':baseline,'candidate_baseline':candidate,
             'summary':'已记录人工复核，待团队审核；尚未用于训练或更新公共知识库。'}
        c.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?)',(rec['record_id'],sid,trace,case,key,json.dumps(rec,ensure_ascii=False)));c.commit()
    return {'ok':True,'record':rec,'duplicate':False}

def history(state,sid,filters=None):
    filters=filters or {}
    q=str(filters.get('q',''))[:150].strip()
    verdict=str(filters.get('verdict',''))
    try:page=max(1,int(filters.get('page',1)))
    except (ValueError,TypeError):raise ValueError('页码不正确')
    if verdict and verdict not in ('ACCEPT','REJECT','CORRECT','UNKNOWN'):raise ValueError('结论筛选无效')
    with closing(connect(state)) as c:
        name=reviewer(c,sid)
        # Authenticated team account history survives visitor-cookie renewal.
        clauses=["json_extract(payload,'$.reviewer')=?"];args=[name]
        if q:
            clauses.append("(instr(case_id,?)>0 OR instr(json_extract(payload,'$.reason'),?)>0 OR instr(id,?)>0)");args.extend([q,q,q])
        if verdict:clauses.append("json_extract(payload,'$.verdict')=?");args.append(verdict)
        where=' AND '.join(clauses)
        total=c.execute('SELECT COUNT(*) FROM reviews WHERE '+where,args).fetchone()[0]
        rows=c.execute('SELECT payload FROM reviews WHERE '+where+' ORDER BY rowid DESC LIMIT 20 OFFSET ?',args+[(page-1)*20]).fetchall()
    return {'ok':True,'records':[json.loads(r[0]) for r in rows],'total':total,'page':page,'page_size':20,'reviewer':name}

def legacy_history(state,sid,path,filters=None):
    filters=filters or {}
    with closing(connect(state)) as c:name=reviewer(c,sid)
    try:page=max(1,int(filters.get('page',1)))
    except (ValueError,TypeError):raise ValueError('页码不正确')
    q=str(filters.get('q',''))[:150].strip();verdict=filters.get('verdict','');records=[]
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:r=json.loads(line)
                except ValueError:continue
                if not isinstance(r,dict):continue
                if verdict and r.get('verdict')!=verdict:continue
                if q and not any(q in str(r.get(k,'')) for k in ('case_id','record_id','note')):continue
                records.append({'record_id':r.get('record_id'),'case_id':r.get('case_id'),'verdict':r.get('verdict'),
                    'at':r.get('at'),'reason':r.get('note',''),'reviewer':'旧版普通反馈（身份未经验证）','corrections':{},
                    'summary':'旧版反馈只保存结论和备注，不含逐项修正，也不代表经过医生审核。',
                    'provenance':r.get('provenance'),'legacy':True})
    records.reverse()
    return {'ok':True,'records':records[(page-1)*20:page*20],'total':len(records),'page':page,'page_size':20,'reviewer':name,'legacy':True}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Create a local team reviewer invitation without publishing its code.')
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parents[1]/'04_产物/integration')
    args = parser.parse_args()
    print('Local reviewer configuration:', provision(args.state))
