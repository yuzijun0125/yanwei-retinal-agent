param([switch]$Diagnose,[switch]$Stop)
$ErrorActionPreference='Stop'
$projectPath=$PSScriptRoot
$nativeDocker=Get-Command docker -ErrorAction SilentlyContinue
function Invoke-ProjectDocker {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$DockerArgs)
    if($nativeDocker){ & $nativeDocker.Source @DockerArgs }
    else { & wsl.exe -e docker @DockerArgs }
    if($LASTEXITCODE -ne 0){throw "Docker command failed (exit $LASTEXITCODE)."}
}
try {
    Invoke-ProjectDocker info --format '{{.OSType}} {{.Architecture}}' | Out-Host
    Invoke-ProjectDocker compose version | Out-Host
} catch { Write-Host '请先安装并启动 Docker Desktop（Linux 容器，启用 WSL2），然后重试。';throw }
if($nativeDocker){$mountPath=$projectPath} else {$mountPath=(& wsl.exe -e wslpath -a -u $projectPath).Trim()}
$envPath=Join-Path $projectPath 'deploy\.env'
$composeFile=if($nativeDocker){Join-Path $projectPath 'deploy\compose.yml'}else{"$mountPath/deploy/compose.yml"}
$dockerEnv=if($nativeDocker){$envPath}else{"$mountPath/deploy/.env"}
$pythonImage=if($env:PYTHON_IMAGE){$env:PYTHON_IMAGE}else{'docker.m.daocloud.io/library/python:3.11-slim'}
if(!(Test-Path -LiteralPath $envPath)){
    if($Stop){Write-Host '尚未初始化，无需停止。';exit 0}
    Invoke-ProjectDocker pull $pythonImage
    Invoke-ProjectDocker run --rm -v "${mountPath}:/app" -w /app -e "PYTHON_IMAGE=$pythonImage" $pythonImage python integration/deployment.py init
}
$composeArgs=@('compose','--project-name','yanwei-local','--env-file',$dockerEnv,'-f',$composeFile)
if($Stop){Invoke-ProjectDocker @composeArgs stop;Write-Host '已停止本项目，数据保留。';exit 0}
Invoke-ProjectDocker @composeArgs config --quiet
if($Diagnose){
    Invoke-ProjectDocker @composeArgs ps
    Invoke-ProjectDocker @composeArgs logs --tail 60 yanwei-gateway yanwei-public yanwei-record-ocr yanwei-retrieval nexent-runtime
    Write-Host '诊断日志请先去除模型凭据、Cookie和患者信息后再对外发送。'
    exit 0
}
$info=Invoke-ProjectDocker info --format '{{json .}}' | ConvertFrom-Json
if($info.OSType -ne 'linux' -or $info.Architecture -notin @('x86_64','amd64')){throw '当前交付支持 Linux 容器 amd64；此 Docker 架构未验证。'}
if([double]$info.MemTotal -lt 6GB){throw 'Docker 分配内存不足 6GB，请先调整 Docker/WSL 内存。建议为本项目分配 8GB 以上。'}
$portLine=Get-Content -LiteralPath $envPath | Where-Object {$_ -match '^YANWEI_PORT='} | Select-Object -First 1
$projectPort=if($portLine){[int]($portLine -split '=',2)[1]}else{18880}
$listener=Get-NetTCPConnection -LocalPort $projectPort -State Listen -ErrorAction SilentlyContinue
if($listener){
    $ownService=Invoke-ProjectDocker @composeArgs ps --status running -q yanwei-gateway
    if(!$ownService){throw "端口 $projectPort 已占用。请编辑 deploy/.env 中 YANWEI_PORT 为一个空闲端口后重试。"}
}
Write-Host '首次启动需下载镜像并构建 CPU 图文环境，可能需要较长时间；后续复用缓存。'
Invoke-ProjectDocker @composeArgs up -d --build
$portLine=Get-Content -LiteralPath $envPath | Where-Object {$_ -match '^YANWEI_PORT='} | Select-Object -First 1
$projectPort=if($portLine){[int]($portLine -split '=',2)[1]}else{18880}
$deadline=(Get-Date).AddMinutes(6)
$ready=$false
while((Get-Date) -lt $deadline){
    try{
        $status=Invoke-RestMethod "http://127.0.0.1:${projectPort}/api/integration/status" -TimeoutSec 12
        if($status.services.retrieval -and $status.services.nexent){$ready=$true;break}
    }catch{}
    Start-Sleep -Seconds 3
}
if(!$ready){throw '服务尚未就绪。请运行 诊断项目.cmd 查看启动状态，等待模型加载后重试。'}
Start-Process "http://localhost:${projectPort}/hosting-admin"
Write-Host '已打开托管管理页：管理员配置并授权一次；访客使用端口 18881，无需 Nexent 账号。'
