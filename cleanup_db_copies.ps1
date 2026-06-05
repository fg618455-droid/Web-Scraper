# cleanup_db_copies.ps1
# Findet alle deals.db Kopien und zeigt sie an.
# Loescht veraltete Kopien nach Bestaetigung.
#
# SICHER: loescht NIE die aktive DB in %APPDATA%\DealScraper\deals.db

$ErrorActionPreference = "Continue"

Write-Host "=== deals.db Cleanup Script ===" -ForegroundColor Cyan
Write-Host ""

# Pfade
$appDataDb  = "$env:APPDATA\DealScraper\deals.db"
$srcRoot    = "$PSScriptRoot\deals.db"
$distDb     = "$PSScriptRoot\dist\DealScraper\_internal\deals.db"
$backupDir  = "$PSScriptRoot\Backups"

# Alle gefundenen DB-Dateien auflisten
$allDbs = @()

if (Test-Path $appDataDb) {
    $f = Get-Item $appDataDb
    $allDbs += [pscustomobject]@{ Path=$f.FullName; Size=[math]::Round($f.Length/1024,1); Modified=$f.LastWriteTime; Tag="AKTIV (APPDATA) — NIEMALS loeschen" }
}

if (Test-Path $srcRoot) {
    $f = Get-Item $srcRoot
    $allDbs += [pscustomobject]@{ Path=$f.FullName; Size=[math]::Round($f.Length/1024,1); Modified=$f.LastWriteTime; Tag="Source-Mode DB (python main.py)" }
}

if (Test-Path $distDb) {
    $f = Get-Item $distDb
    $allDbs += [pscustomobject]@{ Path=$f.FullName; Size=[math]::Round($f.Length/1024,1); Modified=$f.LastWriteTime; Tag="VERALTET — dist/_internal (wird bei build ueberschrieben)" }
}

if (Test-Path $backupDir) {
    Get-ChildItem $backupDir -Filter "*.db" | ForEach-Object {
        $allDbs += [pscustomobject]@{ Path=$_.FullName; Size=[math]::Round($_.Length/1024,1); Modified=$_.LastWriteTime; Tag="Backup" }
    }
}

# Auch in APPDATA nach alten Kopien suchen
Get-ChildItem "$env:APPDATA\DealScraper\" -Filter "deals_backup_*.db" -ErrorAction SilentlyContinue | ForEach-Object {
    $allDbs += [pscustomobject]@{ Path=$_.FullName; Size=[math]::Round($_.Length/1024,1); Modified=$_.LastWriteTime; Tag="APPDATA Backup" }
}

Write-Host "Gefundene DB-Dateien:" -ForegroundColor Yellow
$allDbs | Format-Table Tag, Size, Modified, Path -AutoSize

Write-Host ""
Write-Host "Was wird geloescht (nach Bestaetigung):" -ForegroundColor Red

$toDelete = $allDbs | Where-Object { $_.Tag -like "*VERALTET*" -or ($_.Tag -eq "Backup" -and $_.Modified -lt (Get-Date).AddDays(-30)) }

if ($toDelete.Count -eq 0) {
    Write-Host "  Nichts zu loeschen — alles in Ordnung." -ForegroundColor Green
    exit 0
}

$toDelete | ForEach-Object { Write-Host "  - $($_.Path)" -ForegroundColor DarkRed }

Write-Host ""
$confirm = Read-Host "Loeschen? (j/n)"
if ($confirm -ne "j") {
    Write-Host "Abgebrochen." -ForegroundColor Gray
    exit 0
}

$toDelete | ForEach-Object {
    Remove-Item $_.Path -Force
    Write-Host "Geloescht: $($_.Path)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Fertig." -ForegroundColor Cyan
