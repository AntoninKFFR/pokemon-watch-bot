$ErrorActionPreference = 'Stop'
$TaskName = 'pokemon-watch-bot-auto'
$null = schtasks /Query /TN $TaskName 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Tâche planifiée absente.'
    exit 0
}
schtasks /Delete /F /TN $TaskName | Out-Null
Write-Host "Tâche planifiée supprimée : $TaskName"
