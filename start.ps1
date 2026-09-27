$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Python environment not found. Follow the setup instructions in README.md.'
}
& $projectPython manage.py migrate
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
& $projectPython manage.py runserver 127.0.0.1:8000
