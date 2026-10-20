#Requires -RunAsAdministrator
Remove-NetFirewallRule -Name 'Yanwei-Visitor-18881-LocalSubnet' -ErrorAction SilentlyContinue
Write-Host '已移除本项目创建的局域网防火墙规则。Docker 场景请将 deploy/.env 中 YANWEI_PUBLIC_BIND 改回 127.0.0.1 并重启。'
