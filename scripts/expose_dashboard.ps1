$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "run this script as Administrator" -ForegroundColor Red
  exit 1
}

$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
Write-Host "WSL IP: $wslIp"

netsh interface portproxy delete v4tov4 listenport=8080 listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=8080 listenaddress=0.0.0.0 connectport=8080 connectaddress=$wslIp

$rule = Get-NetFirewallRule -DisplayName "WS Trade Bot" 2>$null
if (-not $rule) {
  New-NetFirewallRule -DisplayName "WS Trade Bot" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow | Out-Null
}

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback|vEthernet" } | Select-Object -First 1).IPAddress
Write-Host "dashboard reachable on your network at: http://${lanIp}:8080" -ForegroundColor Green
Write-Host "re-run this script after each WSL/PC reboot (WSL IP changes)"
