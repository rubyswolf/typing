[CmdletBinding()]
param(
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDirectory = Join-Path $env:LOCALAPPDATA "HouseChordTranslator"
$installedEnvironment = Join-Path $installDirectory ".venv"
$installedPython = Join-Path $installedEnvironment "Scripts\python.exe"
$installedPythonw = Join-Path $installedEnvironment "Scripts\pythonw.exe"
$installedApplication = Join-Path $installDirectory "chord_translator.py"
$installedApi = Join-Path $installDirectory "translator_api.py"
$installedLayoutDirectory = Join-Path $installDirectory "layouts"
$installedLayout = Join-Path $installedLayoutDirectory "house_extended.json"
$installedRequirements = Join-Path $installDirectory "requirements.txt"

if (-not (Test-Path -LiteralPath $installedPythonw -PathType Leaf)) {
    throw "House Chord Translator is not installed. Run .\install.ps1 first."
}

$sourceApplication = Join-Path $sourceDirectory "chord_translator.py"
$sourceApi = Join-Path $sourceDirectory "translator_api.py"
$sourceLayout = Join-Path $sourceDirectory "layouts\house_extended.json"
$sourceRequirements = Join-Path $sourceDirectory "requirements.txt"
foreach ($sourceFile in @($sourceApplication, $sourceApi, $sourceLayout, $sourceRequirements)) {
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "Required update file is missing: $sourceFile"
    }
}

$quotedApplication = '"' + $installedApplication + '"'
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe", "pythonw.exe") -and
    $_.CommandLine -and
    $_.CommandLine.Contains($quotedApplication)
}
foreach ($process in $running) {
    if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Path $installedLayoutDirectory -Force | Out-Null
Copy-Item -LiteralPath $sourceApplication -Destination $installedApplication -Force
Copy-Item -LiteralPath $sourceApi -Destination $installedApi -Force
Copy-Item -LiteralPath $sourceLayout -Destination $installedLayout -Force
Copy-Item -LiteralPath $sourceRequirements -Destination $installedRequirements -Force

if (-not $SkipDependencies) {
    & $installedPython -m pip install --disable-pip-version-check -r $installedRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency update failed with exit code $LASTEXITCODE."
    }
}

Start-Process -FilePath $installedPythonw -ArgumentList $quotedApplication -WorkingDirectory $installDirectory -WindowStyle Hidden

$started = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    $started = [bool](Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "pythonw.exe" -and
        $_.CommandLine -and
        $_.CommandLine.Contains($quotedApplication)
    })
    if ($started) {
        break
    }
}
if (-not $started) {
    throw "Files were updated, but the translator did not restart. Run the Startup shortcut manually."
}

Write-Host "House Chord Translator updated and restarted."
Write-Host "Installed layout: $installedLayout"
