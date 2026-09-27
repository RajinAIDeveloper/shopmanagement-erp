# Production launcher for Windows hosts: applies migrations, collects static files, starts the WSGI server
# and the reservation-expiry worker. Configure the environment first (see .env.example and README → Deployment).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $PSScriptRoot '.runtime\python\python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment not found. Follow the setup instructions in README.md.' }
if (-not $env:DJANGO_SECRET_KEY) { throw 'DJANGO_SECRET_KEY is not set. Refusing to start a production server with the development key.' }
if (-not $env:DJANGO_DEBUG) { $env:DJANGO_DEBUG = '0' }
if (-not $env:DJANGO_ALLOWED_HOSTS) { throw 'DJANGO_ALLOWED_HOSTS is not set (comma-separated hostnames).' }
$listen = if ($env:ALTAB_LISTEN) { $env:ALTAB_LISTEN } else { '127.0.0.1:8000' }
$threads = if ($env:ALTAB_THREADS) { $env:ALTAB_THREADS } else { '8' }

if ($env:DJANGO_HTTPS -eq '0') {
    Write-Warning 'DJANGO_HTTPS=0: cookies are not marked Secure and HSTS is off. Only use this on a firewalled LAN with no internet exposure.'
    & $python manage.py check --deploy --fail-level ERROR
} else {
    & $python manage.py check --deploy --fail-level WARNING
}
if ($LASTEXITCODE -ne 0) { throw 'Deployment checks failed.' }
& $python manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
& $python manage.py collectstatic --noinput --clear
if ($LASTEXITCODE -ne 0) { throw 'collectstatic failed.' }

$worker = Start-Process -FilePath $python -ArgumentList 'manage.py', 'expire_reservations', '--watch' -NoNewWindow -PassThru
try {
    Write-Host "Altab Tech serving on http://$listen (reservation worker PID $($worker.Id)). Press Ctrl+C to stop."
    & $python -m waitress --listen=$listen --threads=$threads config.wsgi:application
} finally {
    if (-not $worker.HasExited) { Stop-Process -Id $worker.Id -Force }
}
