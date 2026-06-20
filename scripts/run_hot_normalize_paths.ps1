param(
    [int] $WatchSeconds = 1500,
    [int] $IntervalSeconds = 30
)

$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonScript = Join-Path $ScriptRoot 'keep_codex_fast.py'
$BackupRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex\codex-backups\keep-codex-fast-hot-latest'
$LogRoot = Join-Path $BackupRoot 'logs'
$LogFile = Join-Path $LogRoot ('hot-normalize-{0:yyyyMMdd}.log' -f (Get-Date))

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}

if (-not $python) {
    throw 'Python was not found on PATH.'
}

$arguments = @()
if ($python.Name -ieq 'py.exe' -or $python.Name -ieq 'py') {
    $arguments += '-3'
}

$arguments += '-u'
$arguments += @(
    $PythonScript,
    '--apply',
    '--hot-normalize-paths',
    '--hot-normalize-watch-seconds',
    [string] $WatchSeconds,
    '--hot-normalize-interval-seconds',
    [string] $IntervalSeconds,
    '--backup-root',
    $BackupRoot
)

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$timestamp] starting hot normalize: watch=$WatchSeconds interval=$IntervalSeconds backup=$BackupRoot" | Add-Content -Path $LogFile -Encoding UTF8

& $python.Source @arguments *>> $LogFile
$exitCode = $LASTEXITCODE

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$timestamp] finished hot normalize: exit=$exitCode" | Add-Content -Path $LogFile -Encoding UTF8

exit $exitCode
