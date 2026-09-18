$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent

$keygen = (Get-Command ssh-keygen -ErrorAction SilentlyContinue).Source
if (-not $keygen) {
    $keygen = "C:\Program Files\Git\usr\bin\ssh-keygen.exe"
}
if (-not (Test-Path $keygen)) {
    throw "ssh-keygen not found - install Git for Windows or enable the OpenSSH Client optional feature"
}

$ssh = (Get-Command ssh -ErrorAction SilentlyContinue).Source
if (-not $ssh) {
    $ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
}

$sshDir = Join-Path $env:USERPROFILE ".ssh"
$key = Join-Path $sshDir "id_ed25519"

if (-not (Test-Path $key)) {
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    & $keygen -t ed25519 -C "ws-trade-bot-autoupdate" -f $key -N '""'
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
} else {
    Write-Host "using existing key $key"
}

$origin = git -C $repoRoot remote get-url origin
if ($origin -notmatch "github\.com[:/]([^/]+/[^/]+?)(\.git)?$") {
    throw "cannot parse origin url: $origin"
}
$slug = $Matches[1]

Write-Host ""
Write-Host "1. open https://github.com/$slug/settings/keys/new"
Write-Host "2. paste this PUBLIC key, leave 'Allow write access' UNCHECKED:"
Write-Host ""
Write-Host (Get-Content "$key.pub" -Raw)
Write-Host ""
Read-Host "press ENTER after adding the deploy key"

& $ssh -o StrictHostKeyChecking=accept-new -T git@github.com | Out-Null

git -C $repoRoot remote set-url origin "git@github.com:$slug.git"
git -C $repoRoot fetch origin
if ($LASTEXITCODE -ne 0) { throw "git fetch over SSH failed" }
Write-Host "remote switched to SSH and fetch verified"
