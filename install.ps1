<#
  Mesh Warp installer for GIMP 3.x on Windows.

  Usage:
    .\install.ps1                          # use files next to this script (or a zip next to it)
    .\install.ps1 -Source mesh-warp.zip    # local zip
    .\install.ps1 -Source C:\folder        # folder containing mesh-warp.py + mesh-warp.dll
    .\install.ps1 -Source https://.../mesh-warp.zip
    .\install.ps1 -GimpVersion 3.2
    .\install.ps1 -GeglDir "D:\GIMPPortable\...\gegl-0.4\plug-ins"   # portable GIMP
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$GimpVersion = "3.2",
    [string]$GeglDir,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$PluginDir = Join-Path $env:APPDATA "GIMP\$GimpVersion\plug-ins\mesh-warp"
if (-not $GeglDir) { $GeglDir = Join-Path $env:LOCALAPPDATA "gegl-0.4\plug-ins" }

if (Get-Process -Name "gimp*" -ErrorAction SilentlyContinue) {
    Write-Warning "GIMP is running. Close it first, otherwise the DLL may be locked."
}

if ($Uninstall) {
    Remove-Item -Recurse -Force $PluginDir -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $GeglDir "mesh-warp.dll") -ErrorAction SilentlyContinue
    Write-Host "Removed Mesh Warp."
    return
}

# Default source: the folder this script lives in
if (-not $Source) { $Source = $PSScriptRoot }

# Work in a fresh temp directory (downloads/extracts are untrusted data)
$Work = Join-Path ([IO.Path]::GetTempPath()) ("meshwarp-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Work | Out-Null

try {
    $SearchRoot = $Source

    if ($Source -match '^https?://') {
        $zip = Join-Path $Work "download.zip"
        Write-Host "Downloading $Source ..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Source -OutFile $zip -UseBasicParsing
        $Source = $zip
    }

    if (Test-Path $Source -PathType Leaf) {
        if ($Source -notmatch '\.zip$') { throw "Source file must be a .zip: $Source" }
        $extract = Join-Path $Work "extracted"
        Expand-Archive -LiteralPath $Source -DestinationPath $extract -Force
        $SearchRoot = $extract
    } elseif (Test-Path $Source -PathType Container) {
        $SearchRoot = $Source
        # No loose files next to the script? Try a zip sitting there.
        if (-not (Get-ChildItem $SearchRoot -Recurse -Filter "mesh-warp.dll" -ErrorAction SilentlyContinue)) {
            $z = Get-ChildItem $SearchRoot -Filter "*.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($z) {
                $extract = Join-Path $Work "extracted"
                Expand-Archive -LiteralPath $z.FullName -DestinationPath $extract -Force
                $SearchRoot = $extract
            }
        }
    } else {
        throw "Source not found: $Source"
    }

    $py  = Get-ChildItem $SearchRoot -Recurse -Filter "mesh-warp.py"  | Select-Object -First 1
    $dll = Get-ChildItem $SearchRoot -Recurse -Filter "mesh-warp.dll" | Select-Object -First 1

    if (-not $py)  { throw "mesh-warp.py not found in $SearchRoot" }
    if (-not $dll) {
        $c = Get-ChildItem $SearchRoot -Recurse -Filter "mesh-warp.c" | Select-Object -First 1
        if (-not $c) { throw "mesh-warp.dll not found in $SearchRoot (and no mesh-warp.c to build from)." }

        Write-Host "==> No pre-built mesh-warp.dll found - building from source..."
        $mingw = "C:\msys64\mingw64\bin"
        if (Test-Path $mingw) { $env:PATH = "$mingw;$env:PATH" }
        if (-not (Get-Command gcc -ErrorAction SilentlyContinue)) {
            throw "gcc not found. Install MSYS2 and run in MinGW64: pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-pkg-config mingw-w64-x86_64-gegl  (then re-run), or supply a zip containing mesh-warp.dll."
        }
        & pkg-config --exists gegl-0.4
        if ($LASTEXITCODE -ne 0) { throw "gegl-0.4 dev files not found (pacman -S mingw-w64-x86_64-gegl)." }

        $out = Join-Path $Work "mesh-warp.dll"
        $flags = (& pkg-config --cflags --libs gegl-0.4 glib-2.0) -split '\s+' | Where-Object { $_ }
        & gcc -O2 -shared $c.FullName -o $out @flags
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "Build failed." }
        $dll = Get-Item $out
        Write-Host "    Built: $out"
    }

    New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
    New-Item -ItemType Directory -Force -Path $GeglDir   | Out-Null

    Copy-Item -LiteralPath $py.FullName  -Destination (Join-Path $PluginDir "mesh-warp.py")  -Force
    Copy-Item -LiteralPath $dll.FullName -Destination (Join-Path $GeglDir  "mesh-warp.dll") -Force

    Write-Host ""
    Write-Host "Installed:"
    Write-Host "  Python plugin : $PluginDir\mesh-warp.py"
    Write-Host "  GEGL op (DLL) : $GeglDir\mesh-warp.dll"
    Write-Host ""
    Write-Host "Restart GIMP, then open Filters > Deform (or Distorts) > Mesh Warp."
    Write-Host "If the filter does not appear, check Windows > Dockable Dialogs > Error Console for dlopen/symbol errors."
    Write-Host "Optional: install NumPy in GIMP's Python for faster puppet mode."
}
finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
