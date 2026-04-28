$ProjectRoot = $PSScriptRoot
$BackendDir = Join-Path $ProjectRoot "backend"
$VenvActivate = Join-Path $BackendDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $VenvActivate)) {
    Write-Host "ERROR: .venv not found." -ForegroundColor Red
    exit 1
}

Write-Host "[1/4] Starting Redis..." -ForegroundColor Cyan
$existing = docker ps -a --filter "name=pdf-compare-redis" --format "{{.Names}}" 2>$null
$running  = docker ps  --filter "name=pdf-compare-redis" --format "{{.Names}}" 2>$null
if ($existing -eq "pdf-compare-redis") {
    if ($running -ne "pdf-compare-redis") { docker start pdf-compare-redis | Out-Null }
    Write-Host "  -> Redis OK" -ForegroundColor Green
} else {
    docker run -d --name pdf-compare-redis -p 6379:6379 redis:7 | Out-Null
    Write-Host "  -> Redis started" -ForegroundColor Green
}

$cmd = "Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force; & '$VenvActivate'; Set-Location '$BackendDir'"

Write-Host "[2/4] Starting API (port 8000)..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "$cmd; uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
Start-Sleep -Seconds 1

Write-Host "[3/4] Starting Celery Worker..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "$cmd; celery -A app.workers.celery_app.celery_app worker --loglevel=info --pool=solo"
Start-Sleep -Seconds 1

Write-Host "[4/4] Starting Celery Beat..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "$cmd; celery -A app.workers.celery_app.celery_app beat --loglevel=info"

Write-Host ""
Write-Host "All services started!" -ForegroundColor Yellow
Write-Host "API:      http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "API Docs: http://127.0.0.1:8000/docs" -ForegroundColor Yellow
