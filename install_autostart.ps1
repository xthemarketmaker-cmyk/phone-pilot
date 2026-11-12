# install_autostart.ps1 - per-user Task Scheduler entry that keeps the phone chain up (no admin needed).
# Runs chain_up.py --loop 45 hidden, 30 s after this user logs on; restarts up to 3x on failure; ignores a second start.
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File C:\phonebot\live\install_autostart.ps1 [-Remove]
param([switch]$Remove)
$name = "\PhonePilot\PhonePilot chain_up"
if ($Remove) { schtasks /end /tn $name 2>$null | Out-Null; schtasks /delete /tn $name /f; exit }
$live = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
python -c "import psutil" 2>$null; if ($LASTEXITCODE -ne 0) { python -m pip install --user psutil | Out-Null }
$user = "$env:USERDOMAIN\$env:USERNAME"
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><UserId>$user</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Count>3</Count><Interval>PT1M</Interval></RestartOnFailure>
  </Settings>
  <Triggers><LogonTrigger><Delay>PT30S</Delay><UserId>$user</UserId></LogonTrigger></Triggers>
  <Actions Context="Author"><Exec><Command>$pyw</Command><Arguments>$live\chain_up.py --loop 45</Arguments><WorkingDirectory>$live</WorkingDirectory></Exec></Actions>
</Task>
"@
$tmp = Join-Path $env:TEMP "phonepilot_task.xml"
$xml | Out-File -Encoding Unicode $tmp
schtasks /create /tn $name /xml $tmp /f
Remove-Item $tmp
schtasks /run /tn $name | Out-Null
Start-Sleep 3
schtasks /query /tn $name /fo LIST | Select-String "Status"
