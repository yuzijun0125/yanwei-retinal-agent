#Requires -RunAsAdministrator
$ErrorActionPreference='Stop'
$ruleName='Yanwei-Visitor-18881-LocalSubnet'
if(!(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)){
 New-NetFirewallRule -Name $ruleName -DisplayName 'Yanwei visitor demo (local subnet)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18881 -RemoteAddress LocalSubnet -Profile Any | Out-Null
}
$envFile=Join-Path $PSScriptRoot 'deploy/.env'
if(Test-Path -LiteralPath $envFile){
 $content=Get-Content -LiteralPath $envFile -Raw
 if($content -match '(?m)^YANWEI_PUBLIC_BIND='){$content=$content -replace '(?m)^YANWEI_PUBLIC_BIND=.*$','YANWEI_PUBLIC_BIND=0.0.0.0'}else{$content+="`nYANWEI_PUBLIC_BIND=0.0.0.0`n"}
 [IO.File]::WriteAllText($envFile,$content,[Text.UTF8Encoding]::new($false))
 Write-Host 'Docker 部署配置已更新。请重新运行 START.cmd 使端口映射生效。'
}
Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notmatch '^(127\.|169\.254\.|172\.)'} | ForEach-Object {Write-Host ('同网访客地址：http://'+$_.IPAddress+':18881/console')}
Write-Host '仅放行访客端口；校园网或访客 Wi-Fi 的客户端隔离仍可能阻止互访。'
