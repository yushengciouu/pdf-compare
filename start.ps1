$ProjectRoot = $PSScriptRoot
$BackendDir = Join-Path $ProjectRoot "backend"
$VenvActivate = Join-Path $BackendDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $VenvActivate)) {
    Write-Host "ERROR: .venv not found." -ForegroundColor Red
    exit 1
}

$cmd = "Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force; & '$VenvActivate'; Set-Location '$BackendDir'"

Write-Host "Starting API (port 8000)..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "$cmd; uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

Write-Host ""
Write-Host "Service started!" -ForegroundColor Yellow
Write-Host "API:      http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "API Docs: http://127.0.0.1:8000/docs" -ForegroundColor Yellow
