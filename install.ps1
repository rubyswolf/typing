[CmdletBinding()]
param(
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDirectory = Join-Path $env:LOCALAPPDATA "HouseChordTranslator"
$layoutDirectory = Join-Path $installDirectory "layouts"
$environmentDirectory = Join-Path $installDirectory ".venv"
$startupDirectory = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDirectory "House Chord Translator.lnk"

New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $layoutDirectory -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $sourceDirectory "chord_translator.py") -Destination $installDirectory -Force
Copy-Item -LiteralPath (Join-Path $sourceDirectory "requirements.txt") -Destination $installDirectory -Force
Copy-Item -LiteralPath (Join-Path $sourceDirectory "layouts\house_extended.json") -Destination $layoutDirectory -Force

$pythonLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    & $pythonLauncher.Source -3 -m venv $environmentDirectory
} else {
    $python = Get-Command "python.exe" -ErrorAction Stop
    & $python.Source -m venv $environmentDirectory
}

$installedPython = Join-Path $environmentDirectory "Scripts\python.exe"
$installedPythonw = Join-Path $environmentDirectory "Scripts\pythonw.exe"
$requirements = Join-Path $installDirectory "requirements.txt"
$application = Join-Path $installDirectory "chord_translator.py"

& $installedPython -m pip install --disable-pip-version-check -r $requirements

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $installedPythonw
$shortcut.Arguments = '"' + $application + '"'
$shortcut.WorkingDirectory = $installDirectory
$shortcut.Description = "House Chord Translator"
$shortcut.Save()

if (-not $NoLaunch) {
    Start-Process -FilePath $installedPythonw -ArgumentList ('"' + $application + '"') -WorkingDirectory $installDirectory -WindowStyle Hidden
}

Write-Host "House Chord Translator installed."
Write-Host "Installed at: $installDirectory"
Write-Host "Startup shortcut: $shortcutPath"
if ($NoLaunch) {
    Write-Host "It will start automatically the next time you sign in."
} else {
    Write-Host "It is running now; use the house icon in the notification area."
}
