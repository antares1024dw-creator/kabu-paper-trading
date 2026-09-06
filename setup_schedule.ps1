# Windows タスクスケジューラに「平日 16:05 に日次処理を実行」するタスクを登録する
# 使い方:  powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
# 解除:    Unregister-ScheduledTask -TaskName "株シミュレーション日次" -Confirm:$false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "run_daily.ps1"
$taskName = "株シミュレーション日次"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
# 東証の大引け（15:30）後、Yahoo Finance に終値が反映される時間を見て 16:05 に実行
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 16:05
# -WakeToRun: スリープ中なら 16:05 に自動で復帰して実行する（電源設定「スリープ解除タイマーの許可」が有効な場合のみ）
# -AllowStartIfOnBatteries: バッテリー駆動中でも実行する（実行自体は軽いので許可）
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
  -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
  -Description "国内株ペーパートレードの日次処理（価格更新・売買判断・通知・ダッシュボード生成）" -Force | Out-Null
Write-Host "登録しました: $taskName（平日 16:05、PC がオフだった場合は次回起動時に実行）"
Write-Host "今すぐ試すには: Start-ScheduledTask -TaskName `"$taskName`""
