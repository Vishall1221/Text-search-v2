# Start existing V2 data and API. Does not import, rebuild or modify database tables.
$ErrorActionPreference = 'Stop'
$v2Root = $PSScriptRoot
$v2Python = Join-Path $v2Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $v2Python)) { throw 'V2 environment missing. Run build_v2.ps1 first.' }
Push-Location $v2Root
try {
    $details = & $v2Python -c 'import json; from config.settings import settings; from psycopg.conninfo import conninfo_to_dict; c=conninfo_to_dict(settings.database_url); print(json.dumps(dict(host=c.get(''host'',''127.0.0.1''), port=int(c.get(''port'',5432)), api_host=settings.api_host, api_port=settings.api_port)))'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to load V2 configuration.' }
    $cfg = $details | ConvertFrom-Json
    $dbRoot = Join-Path $env:USERPROFILE 'Desktop\AI-Search-DB'
    $pgBin = Join-Path $dbRoot 'postgres\Library\bin'
    & (Join-Path $pgBin 'pg_isready.exe') -h $cfg.host -p $cfg.port | Out-Null
    if ($LASTEXITCODE -ne 0) {
        if ($cfg.host -notin @('127.0.0.1', 'localhost') -or $cfg.port -ne 5432) {
            throw 'The configured PostgreSQL server is unavailable. Start that server and try again.'
        }
        & (Join-Path $pgBin 'pg_ctl.exe') -D (Join-Path $dbRoot 'data') -l (Join-Path $dbRoot 'server.log') -w start
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL could not start. Check AI-Search-DB\server.log.' }
    }
    $webHost = if ($cfg.api_host -in @('0.0.0.0', '::')) { '127.0.0.1' } else { $cfg.api_host }
    $baseUrl = 'http://{0}:{1}' -f $webHost, $cfg.api_port
    function Test-V2Ready {
        try {
            $ready = Invoke-RestMethod -Uri "$baseUrl/api/ready" -TimeoutSec 2
            return ($ready.status -eq 'ready' -and $null -ne $ready.generation)
        } catch { return $false }
    }
    if (Test-V2Ready) { Write-Host "V2 is already running: $baseUrl"; return }
    $logDir = Join-Path $v2Root 'runtime'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outLog = Join-Path $logDir "api-$stamp.out.log"
    $errLog = Join-Path $logDir "api-$stamp.err.log"
    $apiProcess = Start-Process -FilePath $v2Python -ArgumentList @('-u', '-m', 'app.start') -WorkingDirectory $v2Root -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Test-V2Ready) {
            Write-Host "V2 is running: $baseUrl" -ForegroundColor Green
            Write-Host "API process: $($apiProcess.Id). Logs: $errLog"
            Write-Host 'PostgreSQL and V2 are left running. Existing indexes were not rebuilt.'
            return
        }
        $apiProcess.Refresh()
        if ($apiProcess.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $errLog) { Get-Content -LiteralPath $errLog -Tail 15 }
    throw "V2 did not become ready. Check $errLog"
} finally { Pop-Location }
