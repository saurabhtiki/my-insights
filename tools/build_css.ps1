# Rebuilds static/css/tailwind.css (Tailwind + DaisyUI) for the demo site.
# Run from the project folder after changing classes:   .\tools\build_css.ps1
# Needs tools\tailwindcss.exe (Tailwind v4.1.11, Windows, not in git). Get it once with:
#   curl.exe -fL -o tools\tailwindcss.exe https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.11/tailwindcss-windows-x64.exe
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tool = Join-Path $root "tools\tailwindcss.exe"
if (-not (Test-Path $tool)) {
    Write-Error "tools\tailwindcss.exe is missing. Download it with the curl line at the top of this file."
}
& $tool -i (Join-Path $root "tools\tailwind.input.css") -o (Join-Path $root "static\css\tailwind.css") --minify
if ($LASTEXITCODE -ne 0) { Write-Error "Tailwind build failed (see the message above)." }
Write-Host "Built static/css/tailwind.css"
