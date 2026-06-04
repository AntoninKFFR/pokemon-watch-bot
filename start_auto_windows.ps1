$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'pokemon-watch-bot-auto'
$PythonExe = Join-Path $ScriptDir '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonExe)) {
    $PythonExe = 'python'
}
$Command = "cmd /c cd /d `"$ScriptDir`" && `"$PythonExe`" main.py >> bot.log 2>&1"
schtasks /Create /F /SC HOURLY /MO 1 /TN $TaskName /TR $Command | Out-Null
Write-Host "Tâche planifiée créée : $TaskName"
