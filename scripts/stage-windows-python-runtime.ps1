[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$sourceRoot = (Get-Item -LiteralPath $Source -Force).FullName
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Python source directory is not available: $sourceRoot"
}

if (Test-Path -LiteralPath $Destination) {
    Remove-Item -LiteralPath $Destination -Recurse -Force
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$destinationRoot = (Get-Item -LiteralPath $Destination -Force).FullName

$excludedDirectories = @(
    '__pycache__', '.pytest_cache', '.mypy_cache', '.tox',
    'doc', 'docs', 'ensurepip', 'idlelib', 'include', 'includes',
    'lib2to3', 'libs', 'site-packages', 'scripts', 'tcl', 'test',
    'tests', 'tools', 'turtledemo', 'venv'
)

function Test-ExcludedRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $parts = $RelativePath -split '[\\/]'
    foreach ($part in $parts) {
        if ($excludedDirectories -contains $part.ToLowerInvariant()) { return $true }
    }
    return $false
}

function Copy-RuntimeFile {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$File,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $target = Join-Path $destinationRoot ($RelativePath -replace '/', '\')
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $File.FullName -Destination $target -Force
}

$python = Get-ChildItem -LiteralPath $sourceRoot -File -Force |
    Where-Object { $_.Name -ieq 'python.exe' } |
    Select-Object -First 1
if ($null -eq $python) { throw 'The Python source directory has no python.exe.' }
Copy-RuntimeFile -File $python -RelativePath 'python.exe'

# Keep only runtime DLLs beside python.exe.  The standard library extensions
# and their private DLL dependencies live under DLLs and are selected below.
$rootDlls = @(Get-ChildItem -LiteralPath $sourceRoot -File -Filter '*.dll' -Force)
foreach ($dll in $rootDlls) {
    Copy-RuntimeFile -File $dll -RelativePath $dll.Name
}

$license = Get-ChildItem -LiteralPath $sourceRoot -File -Force |
    Where-Object { $_.Name -ieq 'LICENSE.txt' } |
    Select-Object -First 1
if ($null -eq $license) { throw 'The Python source directory has no LICENSE.txt.' }
Copy-RuntimeFile -File $license -RelativePath 'LICENSE.txt'

$libRoot = Join-Path $sourceRoot 'Lib'
if (-not (Test-Path -LiteralPath $libRoot -PathType Container)) {
    throw "The Python source directory has no Lib directory: $libRoot"
}
foreach ($file in Get-ChildItem -LiteralPath $libRoot -File -Recurse -Force) {
    $relative = $file.FullName.Substring($sourceRoot.Length + 1) -replace '\\', '/'
    if (Test-ExcludedRelativePath -RelativePath $relative) { continue }
    if (@('.pyc', '.pyo', '.pyi') -contains $file.Extension.ToLowerInvariant()) { continue }
    Copy-RuntimeFile -File $file -RelativePath $relative
}

$dllRoot = Join-Path $sourceRoot 'DLLs'
if (-not (Test-Path -LiteralPath $dllRoot -PathType Container)) {
    throw "The Python source directory has no DLLs directory: $dllRoot"
}
foreach ($file in Get-ChildItem -LiteralPath $dllRoot -File -Recurse -Force) {
    $relative = $file.FullName.Substring($sourceRoot.Length + 1) -replace '\\', '/'
    if (Test-ExcludedRelativePath -RelativePath $relative) { continue }
    if ($file.Extension.ToLowerInvariant() -notin @('.dll', '.pyd')) { continue }
    if ($file.Extension.ToLowerInvariant() -eq '.pyd' -and $file.BaseName -match '(?i)test') { continue }
    Copy-RuntimeFile -File $file -RelativePath $relative
}

$stagedFiles = @(Get-ChildItem -LiteralPath $destinationRoot -File -Recurse -Force)
$forbidden = @($stagedFiles | Where-Object {
    $relative = $_.FullName.Substring($destinationRoot.Length + 1) -replace '\\', '/'
    (Test-ExcludedRelativePath -RelativePath $relative) -or
    $_.Extension.ToLowerInvariant() -in @('.h', '.lib', '.pdb', '.pyc', '.pyo', '.pyi')
})
if ($forbidden.Count -gt 0) {
    throw "The staged runtime contains development or test files: $($forbidden[0].FullName)"
}

Write-Host "Staged minimal Windows Python runtime: $($stagedFiles.Count) files"
