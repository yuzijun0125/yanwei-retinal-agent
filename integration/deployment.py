"""Docker-only deployment helpers; run with the small Python bootstrap image."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
DEPLOY=ROOT/'deploy'


def token(secret,role):
    def enc(data):
        return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).rstrip(b'=')
    now=int(time.time())
    raw=enc({'alg':'HS256','typ':'JWT'})+b'.'+enc({'role':role,'iss':'supabase','iat':now,'exp':now+10*365*86400})
    return (raw+b'.'+base64.urlsafe_b64encode(hmac.new(secret.encode(),raw,hashlib.sha256).digest()).rstrip(b'=')).decode()


def initialize(port=18880):
    dest=DEPLOY/'.env'
    if dest.exists():
        print('保留本机已有配置，不轮换密码、不覆盖模型配置。')
        return
    lines=(DEPLOY/'nexent.env.example').read_text(encoding='utf-8').splitlines()
    current={}
    for line in lines:
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key,value=line.split('=',1);current[key]=value.strip('"')
    jwt=secrets.token_hex(32)
    for key in ('ELASTIC_PASSWORD','NEXENT_POSTGRES_PASSWORD','MINIO_ROOT_PASSWORD','MINIO_SECRET_KEY',
                'SUPABASE_POSTGRES_PASSWORD','DASHBOARD_PASSWORD','NEXENT_SUPER_ADMIN_PASSWORD','INVITE_CODE'):
        current[key]=secrets.token_hex(20)
    current.update({'APP_VERSION':'v2.6.0','YANWEI_PORT':str(port),
        'YANWEI_PUBLIC_PORT':'18881','YANWEI_PUBLIC_BIND':'127.0.0.1','PUBLIC_HTTPS':'0',
        'PYTHON_IMAGE':os.environ.get('PYTHON_IMAGE','docker.m.daocloud.io/library/python:3.11-slim'),
        'NEXENT_IMAGE':'ccr.ccs.tencentyun.com/nexent-hub/nexent:v2.6.0',
        'NEXENT_WEB_IMAGE':'ccr.ccs.tencentyun.com/nexent-hub/nexent-web:v2.6.0',
        'ELASTICSEARCH_IMAGE':'elastic.m.daocloud.io/elasticsearch/elasticsearch:8.17.4',
        'POSTGRESQL_IMAGE':'docker.m.daocloud.io/postgres:15-alpine',
        'REDIS_IMAGE':'docker.m.daocloud.io/redis:alpine',
        'MINIO_IMAGE':'quay.m.daocloud.io/minio/minio:RELEASE.2023-12-20T01-00-02Z',
        'SUPABASE_KONG':'docker.m.daocloud.io/kong:2.8.1',
        'SUPABASE_GOTRUE':'docker.m.daocloud.io/supabase/gotrue:v2.170.0',
        'SUPABASE_DB':'docker.m.daocloud.io/supabase/postgres:15.8.1.060',
        'JWT_SECRET':jwt,'SUPABASE_KEY':token(jwt,'anon'),'SERVICE_ROLE_KEY':token(jwt,'service_role'),
        'MINIO_ACCESS_KEY':'yw'+secrets.token_hex(8),'MINIO_ROOT_USER':'yanwei',
        'SITE_URL':f'http://localhost:{port}','OAUTH_CALLBACK_BASE_URL':f'http://localhost:{port}',
        'CAS_CALLBACK_BASE_URL':f'http://localhost:{port}','CAS_ENABLED':'false','CAS_LOGIN_MODE':'disabled',
        'OAUTH_LOGIN_MODE':'disabled','MODEL_ENGINE_ENABLED':'false','MINIO_SECURE':'false',
        'NEXENT_SANDBOX_MODE':'disabled','NEXENT_SANDBOX_ENABLED':'false',
        'ENABLE_TERMINAL':'false','ES_JAVA_OPTS':'-Xms512m -Xmx512m',
        'ENABLE_EMAIL_AUTOCONFIRM':'true','ENABLE_EMAIL_SIGNUP':'true','DISABLE_SIGNUP':'false',
        'ENABLE_UPLOAD_IMAGE':'false','INVITE_EXPIRE_TIME':'86400',
        'NEXENT_POSTGRES_MIGRATION_MODE':'migrate',
        'NO_PROXY':'localhost,127.0.0.1,nexent-config,nexent-runtime,nexent-mcp,nexent-web,nexent-postgresql,nexent-minio,nexent-supabase-db,nexent-supabase-kong,yanwei-retrieval'})
    # No user/provider credential is read from another installation.
    for key in ('APPID','TOKEN','GITHUB_OAUTH_CLIENT_ID','GITHUB_OAUTH_CLIENT_SECRET',
                'SSH_PRIVATE_KEY_PATH','CAS_SERVER_URL','CAS_INTERNAL_SERVER_URL'):
        current[key]=''
    # Host-generic paths are not needed; all database state uses project-scoped named volumes.
    current['ROOT_DIR']='.'
    dest.write_text('\n'.join(k+'='+str(v) for k,v in current.items())+'\n',encoding='utf-8')
    try:dest.chmod(0o600)
    except OSError:pass
    info=DEPLOY/'本机首次登录信息.txt'
    info.write_text('仅供本机管理员查看，不随项目分发。\n'
        f'入口：http://localhost:{port}/integration\n'
        f'平台注册邀请码：{current["INVITE_CODE"]}\n'
        f'初始超级管理员密码：{current["NEXENT_SUPER_ADMIN_PASSWORD"]}\n'
        '请先在平台注册/登录并配置语言模型，再回到首次设置初始化眼维。\n',encoding='utf-8')
    (ROOT/'04_产物/integration').mkdir(parents=True,exist_ok=True)
    print('已生成本机独立配置。首次登录信息：deploy/本机首次登录信息.txt（不要分发）。')


def wait_ready(port,timeout):
    base=f'http://127.0.0.1:{port}'
    end=time.monotonic()+timeout
    last='服务启动中'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic()<end:
        try:
            with opener.open(base+'/api/integration/status',timeout=10) as r:data=json.load(r)
            services=data.get('services',{})
            if services.get('nexent') and services.get('retrieval'):
                print('服务就绪：'+base+'/integration');return 0
            last=json.dumps(services,ensure_ascii=False)
        except (OSError,ValueError):pass
        print('正在等待：'+last,flush=True)
        time.sleep(5)
    print('等待超时。请运行“一键诊断”，查看容器日志和模型/索引状态。');return 1


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['init','wait']);ap.add_argument('--port',type=int,default=18880);ap.add_argument('--timeout',type=int,default=360)
    args=ap.parse_args()
    if args.action=='init':initialize(args.port)
    else:sys.exit(wait_ready(args.port,args.timeout))
