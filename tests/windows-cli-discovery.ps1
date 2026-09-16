#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$NativeClaude,
    [Parameter(Mandatory = $true)][string]$LegacyNpmClaude,
    [Parameter(Mandatory = $true)][string]$NodeExecutable,
    [Parameter(Mandatory = $true)][string]$EvidencePath
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows is required.' }
. (Join-Path $PSScriptRoot '..\scripts\windows-tool-discovery.ps1')
. (Join-Path $PSScriptRoot '..\scripts\windows-lifecycle-common.ps1')

$fixtureRoot = Join-Path $env:RUNNER_TEMP ('mail-cli-links-' + [guid]::NewGuid().ToString('N'))
$nativeLink = Join-Path $fixtureRoot 'WinGet Links\claude.exe'
$desktopLink = Join-Path $fixtureRoot 'Microsoft\WindowsApps\claude.exe'
$nodeLinkRoot = Join-Path $fixtureRoot 'nvm4w\nodejs'
$npmLinkRoot = Join-Path $fixtureRoot 'Roaming npm'
$previousPath = [string]$env:Path
$report = New-Object System.Collections.ArrayList
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Assert-SamePath {
    param([string]$Actual, [string]$Expected, [string]$Label)
    if (-not [string]::Equals($Actual, $Expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label resolved to '$Actual', expected '$Expected'."
    }
}

try {
    $nativeTarget = Resolve-CoremailExternalFilePath -Path $NativeClaude
    $nodeTarget = Resolve-CoremailExternalFilePath -Path $NodeExecutable
    New-Item -ItemType Directory -Path (Split-Path -Parent $nativeLink), (Split-Path -Parent $desktopLink), (Split-Path -Parent $nodeLinkRoot) -Force | Out-Null
    New-Item -ItemType SymbolicLink -Path $nativeLink -Target $nativeTarget | Out-Null
    New-Item -ItemType SymbolicLink -Path $desktopLink -Target $nativeTarget | Out-Null
    New-Item -ItemType Junction -Path $nodeLinkRoot -Target (Split-Path -Parent $NodeExecutable) | Out-Null
    New-Item -ItemType Junction -Path $npmLinkRoot -Target (Split-Path -Parent $LegacyNpmClaude) | Out-Null

    # Prove these are real reparse points rejected by the old path predicate.
    if (Test-CoremailPathChainSafe -Path $nativeLink) { throw 'The WinGet fixture is not a real file symlink.' }
    if (Test-CoremailPathChainSafe -Path (Join-Path $nodeLinkRoot 'node.exe')) { throw 'The NVM fixture is not a real directory junction.' }

    $nativeInvocation = Resolve-ClaudeCodeInvocation -ExplicitPath $nativeLink -Diagnostics $report
    if ($null -eq $nativeInvocation) { throw 'The WinGet file symlink was rejected.' }
    Assert-SamePath -Actual $nativeInvocation.Executable -Expected $nativeTarget -Label 'WinGet link'
    Invoke-CoremailClaudeChecked -Invocation $nativeInvocation -Arguments @('--version') -Label 'Linked native CLI'
    [void]$report.Add('PASS: native file symlink resolves and runs')

    # Match the reported legacy Claude 2.1.84 plus NVM Node combination. The
    # npm prefix has no adjacent node.exe, so node must resolve through PATH's
    # directory junction rather than accidentally using a copied fixture.
    $env:Path = $npmLinkRoot + ';' + $nodeLinkRoot + ';' + $previousPath
    $npmCommand = Join-Path $npmLinkRoot 'claude.cmd'
    $version = ((& $npmCommand --version 2>&1) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^2\.1\.84(?:\s|$)') {
        throw "The linked legacy npm fixture cannot run in the shell: $version"
    }
    foreach ($explicit in @($npmCommand, $LegacyNpmClaude, '')) {
        $npmInvocation = Resolve-ClaudeCodeInvocation -ExplicitPath $explicit -Diagnostics $report
        if ($null -eq $npmInvocation -or $npmInvocation.Kind -ne 'npm' -or $npmInvocation.NpmBinKind -ne 'node') {
            throw "The legacy npm/NVM fixture was not resolved: $explicit"
        }
        Assert-SamePath -Actual $npmInvocation.Executable -Expected $nodeTarget -Label 'NVM node.exe'
        Invoke-CoremailClaudeChecked -Invocation $npmInvocation -Arguments @('--version') -Label 'Linked legacy npm CLI'
    }
    [void]$report.Add('PASS: Claude 2.1.84 through npm prefix and NVM Node junction, explicit and automatic')

    $invalidRoot = Join-Path $fixtureRoot 'invalid'
    New-Item -ItemType Directory -Path $invalidRoot -Force | Out-Null
    $invalidExe = Join-Path $invalidRoot 'claude.exe'
    [IO.File]::WriteAllText($invalidExe, 'not a PE', $utf8)
    if ($null -ne (Resolve-ClaudeCodeInvocation -ExplicitPath $invalidExe -Diagnostics $report)) { throw 'A non-PE candidate was accepted.' }
    if ($null -ne (Resolve-ClaudeCodeInvocation -ExplicitPath $desktopLink -Diagnostics $report)) { throw 'A WindowsApps alias was accepted.' }
    if ($null -ne (Resolve-ClaudeCodeInvocation -ExplicitPath (Join-Path $fixtureRoot 'missing.exe') -Diagnostics $report)) { throw 'An invalid explicit path fell back to PATH.' }
    [void]$report.Add('PASS: invalid PE, WindowsApps alias, and invalid explicit path remain rejected')
}
finally {
    $env:Path = $previousPath
    [IO.File]::WriteAllText($EvidencePath, ($report -join [Environment]::NewLine), $utf8)
    # Remove only the links first; never recursively remove their external
    # fixture targets (the real Claude/Node installs are reused later).
    foreach ($link in @($nativeLink, $desktopLink)) {
        if (Test-Path -LiteralPath $link) { [IO.File]::Delete($link) }
    }
    foreach ($link in @($nodeLinkRoot, $npmLinkRoot)) {
        if (Test-Path -LiteralPath $link) { [IO.Directory]::Delete($link) }
    }
    if (Test-Path -LiteralPath $fixtureRoot) { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force }
}
Write-Host 'Windows CLI discovery link regression passed.' -ForegroundColor Green
