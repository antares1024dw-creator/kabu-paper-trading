# 日次実行スクリプト（タスクスケジューラから呼ばれる）
# 価格更新 → 約定 → 売買判断 → 記録 → 反省ノート → 通知 → ダッシュボード生成 を行う
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
$log = Join-Path $root "logs\scheduler.log"
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start ===" | Out-File -FilePath $log -Append -Encoding utf8
& $py run.py step 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') end (exit $LASTEXITCODE) ===" | Out-File -FilePath $log -Append -Encoding utf8
