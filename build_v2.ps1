# build_v2.ps1 - builds Text Search V2 in your own ai_search database. Safe to run again.
#   Run from PowerShell:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\Text-Search-v2\build_v2.ps1"
# Touches only: the search_v2 schema in ai_search, Text-Search-v2\.venv, Text-Search-v2\models, Text-Search-v2\config\.env.
# V1's tables (public.cms, public.scene_embeddings) and V1's .venv are not changed.

$ErrorActionPreference = "Stop"
$V2      = $PSScriptRoot
$DB      = Join-Path $env:USERPROFILE "Desktop\AI-Search-DB"
$Bin     = Join-Path $DB "postgres\Library\bin"
$Movies  = Join-Path $env:USERPROFILE "Desktop\Datas\output"
$Python  = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
$VPy     = Join-Path $V2 ".venv\Scripts\python.exe"
$Psql    = Join-Path $Bin "psql.exe"
$Conn    = @("-h", "127.0.0.1", "-p", "5432", "-U", "ai_search", "-d", "ai_search", "-v", "ON_ERROR_STOP=1")

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

Step "1/6  PostgreSQL ($DB)"
& (Join-Path $Bin "pg_isready.exe") -h 127.0.0.1 -p 5432 | Out-Null
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $Bin "pg_ctl.exe") -D (Join-Path $DB "data") -l (Join-Path $DB "server.log") -w start
    Check "Starting PostgreSQL (see $DB\server.log)"
}
& $Psql @Conn -tAc "SELECT 'connected; pgvector ' || extversion FROM pg_extension WHERE extname = 'vector'"
Check "Connecting to ai_search"

Step "2/6  Python environment for V2 (Text-Search-v2\.venv)"
if (-not (Test-Path $VPy)) {
    if (-not (Test-Path $Python)) { throw "Python 3.11 not found at $Python" }
    & $Python -m venv (Join-Path $V2 ".venv"); Check "Creating .venv"
}
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $V2 "requirements.txt"); Check "Installing requirements"

Step "3/6  config\.env"
$envFile = Join-Path $V2 "config\.env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $V2 "config\.env.example") $envFile; Write-Host "created from .env.example" }
else { Write-Host "kept your existing config\.env" }

Set-Location $V2
Step "4/6  Embedding model bge-small-en-v1.5 (about 130 MB, downloaded once)"
if (-not (Test-Path (Join-Path $V2 "models\bge-small-en-v1.5\onnx\model.onnx"))) {
    & $VPy -m scripts.download_model bge-small-en-v1.5; Check "Model download"
} else { Write-Host "already downloaded" }

Step "5/6  Validate and build the index from $Movies"
& $VPy -m app.ingest --data $Movies --dry-run; Check "Validation (dry run)"
& $VPy -m app.ingest --data $Movies; Check "Building the index"

Step "6/6  Named views + check"
& $Psql @Conn -q -c @"
CREATE OR REPLACE VIEW search_v2.cms_v2 AS
  SELECT a.* FROM search_v2.asset_snapshot a
  WHERE a.generation_id = (SELECT generation_id FROM search_v2.active_generation);
CREATE OR REPLACE VIEW search_v2.scene_embeddings_v2 AS
  SELECT d.doc_id, d.asset_id, d.language, d.asset_key, d.scene_id, d.kind, d.passage_no, d.text, d.embedding
  FROM search_v2.search_doc d
  WHERE d.scope = 'scene' AND d.generation_id = (SELECT generation_id FROM search_v2.active_generation);
"@
Check "Creating views"
& $Psql @Conn -c @"
SELECT 'search_v2.cms_v2 (titles)' AS what, count(*) AS rows, NULL::bigint AS with_vector FROM search_v2.cms_v2
UNION ALL
SELECT 'search_v2.scene_embeddings_v2 (' || kind || ')', count(*), count(embedding) FROM search_v2.scene_embeddings_v2 GROUP BY kind
UNION ALL
SELECT 'title vectors (search_doc, catalog)', count(*), count(embedding) FROM search_v2.search_doc
  WHERE scope = 'catalog' AND generation_id = (SELECT generation_id FROM search_v2.active_generation);
"@
Check "Final check"
Write-Host "`nDone. PostgreSQL is left running. Start the search app with:  .\.venv\Scripts\python.exe -m app.start   (http://127.0.0.1:8001)" -ForegroundColor Green
