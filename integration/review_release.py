"""Local operator only: reviewed retrieval context, version gates and rollback.

Does not train models or rewrite medical facts. Public routes cannot publish.
"""
import argparse,hashlib,json,os,time,uuid
from contextlib import closing
from pathlib import Path
import clinical_review

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp-'+uuid.uuid4().hex)
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,path)

def prepare(state,record_id,lesson):
    if not lesson.strip() or len(lesson)>1500:raise ValueError('请填写已脱敏、可用于后续检索的复核结论（1—1500字）')
    with closing(clinical_review.connect(state)) as c:
        row=c.execute('SELECT payload FROM reviews WHERE id=?',(record_id,)).fetchone()
    if not row:raise ValueError('复核记录不存在')
    rec=json.loads(row[0]);version='review-'+uuid.uuid4().hex
    value={'version':version,'status':'PREPARED_NOT_ACTIVE','case_id':rec['case_id'],'review_id':record_id,
           'lesson':lesson,'at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'scope':'case_specific_reference_not_diagnosis'}
    atomic(state/'review-releases'/f'{version}.json',value)
    return value

def activate(state,version,report,clinician_approved):
    # Strict version IDs prevent traversal through the local CLI too.
    import re
    if not re.fullmatch(r'review-[a-f0-9]{32}',version):raise ValueError('版本编号不合法')
    path=state/'review-releases'/f'{version}.json'
    value=json.loads(path.read_text(encoding='utf-8'))
    if not clinician_approved:raise ValueError('需要团队确认医生审核和脱敏已完成')
    if report.get('version')!=version or report.get('passed') is not True or not report.get('dataset_id') or not report.get('metrics'):
        raise ValueError('评测报告须对应此版本，并包含passed=true、dataset_id和metrics')
    value['status']='ACTIVE_REVIEWED_CONTEXT';value['evaluation']=report
    value['evaluation_note']='Team-supplied evaluation; not independently certified by software.'
    atomic(path,value)
    old=active(state)
    entries=[x for x in old.get('entries',[]) if x['case_id']!=value['case_id']]+[value]
    release={'version':version,'entries':entries,'previous':old.get('version')}
    atomic(state/'review-releases'/f'{version}.release.json',release)
    atomic(state/'review-active.json',release)
    return release

def active(state):
    path=state/'review-active.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version':None,'entries':[]}

def rollback(state,version):
    import re
    if not re.fullmatch(r'review-[a-f0-9]{32}',version):raise ValueError('版本编号不合法')
    value=json.loads((state/'review-releases'/f'{version}.release.json').read_text(encoding='utf-8'))
    atomic(state/'review-active.json',value)

def enrich(state,result):
    release=active(state)
    cases={c.get('source',{}).get('case_id') for c in result.get('candidates',[])}
    notes=[{'case_id':x['case_id'],'lesson':x['lesson'],'version':x['version']} for x in release.get('entries',[]) if x['case_id'] in cases]
    if notes:
        result['reviewed_context']={'release':release['version'],'notes':notes,
             'notice':'团队审核后的该参考病例复核说明，不改变原始标注，不构成新患者诊断。'}
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,default=Path(__file__).resolve().parents[1]/'04_产物/integration')
    sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('prepare');q.add_argument('record_id');q.add_argument('--lesson',required=True)
    q=sub.add_parser('activate');q.add_argument('version');q.add_argument('--evaluation',required=True,type=Path);q.add_argument('--clinician-approved',action='store_true')
    q=sub.add_parser('rollback');q.add_argument('version')
    args=p.parse_args()
    if args.command=='prepare':out=prepare(args.state,args.record_id,args.lesson);print(out['version'])
    elif args.command=='activate':out=activate(args.state,args.version,json.loads(args.evaluation.read_text(encoding='utf-8')),args.clinician_approved);print('Activated',out['version'])
    else:rollback(args.state,args.version);print('Rolled back',args.version)
