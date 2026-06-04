$TaskName = 'pokemon-watch-bot-auto'
$null = schtasks /Query /TN $TaskName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Mode automatique actif : $TaskName"
} else {
    Write-Host 'Mode automatique inactif.'
}
