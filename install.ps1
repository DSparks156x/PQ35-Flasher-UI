# PQ35 Flasher UI installer for Windows.
#
#   irm https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.ps1 | iex
#
# or run .\install.ps1 from a checkout. Re-run it any time to update.
# Installs uv (per-user), a private 64-bit Python for the app, a private 32-bit
# Python for J2534 adapters, and (if you say yes) Start menu and desktop
# shortcuts. No admin needed. Set PQ35_YES=1 to accept every question.

$ErrorActionPreference = "Stop"
$Repo = "DSparks156x/PQ35-Flasher-UI"
$InstallDir = Join-Path $env:LOCALAPPDATA "PQ35-Flasher-UI"

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Ask($question) {
    if ($env:PQ35_YES) { return $true }
    try { $answer = Read-Host "$question [Y/n]" } catch { return $true }
    return -not ($answer -match '^\s*[Nn]')
}

# 1. Find or fetch the app.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { $null }
if ($here -and (Test-Path (Join-Path $here "pyproject.toml"))) {
    $AppDir = $here
    Step "Using this checkout: $AppDir"
    if ((Test-Path (Join-Path $AppDir ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        git -C $AppDir submodule update --init --recursive
    }
} else {
    $AppDir = Join-Path $InstallDir "app"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        if (Test-Path (Join-Path $AppDir ".git")) {
            Step "Updating $AppDir"
            git -C $AppDir pull --ff-only
            git -C $AppDir submodule update --init --recursive
        } else {
            Step "Downloading into $AppDir"
            New-Item -ItemType Directory -Force $InstallDir | Out-Null
            git clone --recurse-submodules "https://github.com/$Repo.git" $AppDir
        }
    } else {
        # No git: fetch zips of the app and of the exact flasher commit it pins.
        Step "Downloading into $AppDir (git not found, using zip downloads)"
        $tmp = Join-Path $env:TEMP ("pq35-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Force $tmp | Out-Null
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile "$tmp\app.zip"
        $sub = Invoke-RestMethod "https://api.github.com/repos/$Repo/contents/flasher?ref=main"
        $flasherRepo = ($sub.submodule_git_url -replace '^https://github.com/', '') -replace '\.git$', ''
        Invoke-WebRequest "https://github.com/$flasherRepo/archive/$($sub.sha).zip" -OutFile "$tmp\flasher.zip"
        Expand-Archive "$tmp\app.zip" "$tmp\app"
        Expand-Archive "$tmp\flasher.zip" "$tmp\flasher"
        if (Test-Path $AppDir) {
            # Keep the venv and local settings across updates.
            Get-ChildItem $AppDir -Exclude ".venv", ".python32" | Remove-Item -Recurse -Force
        }
        New-Item -ItemType Directory -Force $AppDir | Out-Null
        Copy-Item -Recurse -Force "$((Get-ChildItem "$tmp\app")[0].FullName)\*" $AppDir
        $flasherDir = Join-Path $AppDir "flasher"
        if (Test-Path $flasherDir) { Remove-Item -Recurse -Force $flasherDir }
        Copy-Item -Recurse "$((Get-ChildItem "$tmp\flasher")[0].FullName)" $flasherDir
        Remove-Item -Recurse -Force $tmp
    }
}
if (-not (Test-Path (Join-Path $AppDir "flasher\runner.py"))) {
    throw "The flasher submodule is missing from $AppDir\flasher."
}

# 2. uv.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Step "Installing uv"
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction Stop
}
$uv = $uv.Source

# 3. App environment (64-bit Python + PySide6 + CAN backends).
Step "Installing the app (this takes a minute the first time)"
& $uv sync --project $AppDir --frozen 2>$null
if ($LASTEXITCODE -ne 0) { & $uv sync --project $AppDir }
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# 4. 32-bit Python for J2534 adapters (stdlib only, nothing to install into it).
Step "Installing 32-bit Python for J2534 adapters"
& $uv python install --no-bin cpython-3.12-windows-x86-none
$py32 = (& $uv python find cpython-3.12-windows-x86-none).Trim()
if ($py32 -and (Test-Path $py32)) {
    Set-Content -Path (Join-Path $AppDir ".python32") -Value $py32 -Encoding UTF8 -NoNewline
} else {
    Write-Warning "Couldn't set up 32-bit Python. J2534 adapters won't work; others will."
}

# 5. Shortcuts, if wanted.
$pythonw = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$shell = New-Object -ComObject WScript.Shell
$targets = @()
if (Ask "Add PQ35 Flasher to the Start menu?") {
    $targets += Join-Path ([Environment]::GetFolderPath("Programs")) "PQ35 Flasher.lnk"
}
if (Ask "Add a desktop shortcut?") {
    $targets += Join-Path ([Environment]::GetFolderPath("Desktop")) "PQ35 Flasher.lnk"
}
foreach ($path in $targets) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "-m pq35ui"
    $link.WorkingDirectory = $AppDir
    $link.Description = "PQ35 Flasher"
    $link.Save()
}

Write-Host ""
Write-Host "PQ35 Flasher is installed." -ForegroundColor Green
Write-Host "You can always start it with: $(Join-Path $AppDir 'run.cmd')"
Write-Host "Adapters: J2534 needs the adapter vendor's driver. CANable/candleLight (gs_usb) needs no driver."
