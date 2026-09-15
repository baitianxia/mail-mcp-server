#requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$SkipConnectionCheck,
    [string]$LogPath = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$packageRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$commonScript = Join-Path $PSScriptRoot 'windows-lifecycle-common.ps1'
if (-not (Test-Path -LiteralPath $commonScript -PathType Leaf)) {
    throw 'Mail assistant lifecycle support is missing.'
}
. $commonScript

$userProfile = [Environment]::GetFolderPath('UserProfile')
if ([string]::IsNullOrWhiteSpace($userProfile)) { throw 'The current Windows user profile directory could not be resolved.' }
$mailRoot = Join-Path $userProfile 'mail-mcp-server'
$configPath = Join-Path $mailRoot 'config\settings.json'
if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $logDirectory = Join-Path $mailRoot 'logs'
    $LogPath = Join-Path $logDirectory (
        'CONFIGURE-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' +
        [guid]::NewGuid().ToString('N').Substring(0, 8) + '.log'
    )
}
Initialize-CoremailLifecycleLog -Path $LogPath

function Get-RegisteredMailRuntime {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $null }
    try {
        $payload = Get-Content -LiteralPath $ConfigPath -Raw -ErrorAction Stop | ConvertFrom-Json
        $servers = $payload.PSObject.Properties['mcpServers']
        if ($null -eq $servers -or $null -eq $servers.Value) { return $null }
        $entry = $servers.Value.PSObject.Properties['mail-mcp']
        if ($null -eq $entry -or $null -eq $entry.Value) { return $null }
        $argsProperty = $entry.Value.PSObject.Properties['args']
        if ($null -eq $argsProperty -or $null -eq $argsProperty.Value) { return $null }
        $args = @($argsProperty.Value | ForEach-Object { [string]$_ })
        for ($index = 0; $index -lt $args.Count - 1; $index++) {
            if ($args[$index] -ieq '-File') {
                $serverPath = [IO.Path]::GetFullPath($args[$index + 1])
                if (-not $serverPath.EndsWith('\mcp\run-server.ps1', [StringComparison]::OrdinalIgnoreCase)) {
                    return $null
                }
                return [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $serverPath)))
            }
        }
    }
    catch { return $null }
    return $null
}

function Get-CoremailConfigCredentialTarget {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $null }
    try {
        $payload = Get-Content -LiteralPath $ConfigPath -Raw -ErrorAction Stop | ConvertFrom-Json
        $property = $payload.PSObject.Properties['credential_target']
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) { return $null }
        return [string]$property.Value
    }
    catch {
        # The account setup script will report a malformed configuration. Do
        # not guess at a credential target while preparing the replacement.
        return $null
    }
}

$previousCredentialTarget = Get-CoremailConfigCredentialTarget -ConfigPath $configPath

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Mail account configuration can run only on Windows.'
    }
    [void](Assert-CoremailSafeLocalPath -Path $userProfile -Label 'user profile')
    $claudeUserConfig = Resolve-CoremailClaudeUserConfigPath -UserProfile $userProfile
    $registeredRuntime = Get-RegisteredMailRuntime -ConfigPath $claudeUserConfig
    $versionsRoot = Join-Path $mailRoot 'versions'
    $pluginRoot = $null
    if ($registeredRuntime -and
        $registeredRuntime.StartsWith(([IO.Path]::GetFullPath($versionsRoot)).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        [void](Assert-CoremailSafeDescendantPath -Root $versionsRoot -Path $registeredRuntime -Label 'registered mail runtime')
        if (-not (Test-Path -LiteralPath (Join-Path $registeredRuntime 'scripts\setup-account.ps1') -PathType Leaf)) {
            throw "The registered mail runtime is incomplete: $registeredRuntime"
        }
        $pluginRoot = $registeredRuntime
    }
    elseif ($registeredRuntime) {
        throw "The registered mail runtime is outside the managed versions directory: $registeredRuntime"
    }
    elseif (Test-Path -LiteralPath $versionsRoot -PathType Container) {
        $candidate = Get-ChildItem -LiteralPath $versionsRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'mail-mcp-server-*' } |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($null -ne $candidate) {
            [void](Assert-CoremailSafeDescendantPath -Root $versionsRoot -Path $candidate.FullName -Label 'mail runtime candidate')
            $pluginRoot = $candidate.FullName
        }
    }
    if (-not $pluginRoot) {
        throw 'No installed mail assistant runtime was found. Run INSTALL.cmd before CONFIGURE.cmd.'
    }

    $setupScript = Join-Path $pluginRoot 'scripts\setup-account.ps1'
    $smokeTest = Join-Path $pluginRoot 'scripts\mcp-healthcheck.ps1'
    $credentialScript = Join-Path $pluginRoot 'scripts\windows-credential.ps1'
    if (-not (Test-Path -LiteralPath $setupScript -PathType Leaf)) {
        throw "Account setup script not found: $setupScript. Run INSTALL.cmd first."
    }
    if (-not (Test-Path -LiteralPath $credentialScript -PathType Leaf)) {
        throw "Credential helper not found: $credentialScript. Run INSTALL.cmd first."
    }
    if (-not (Test-Path -LiteralPath $smokeTest -PathType Leaf)) {
        throw "Installed MCP smoke test not found: $smokeTest. Run INSTALL.cmd first."
    }

    & $setupScript -LogPath $LogPath
    if (-not $?) { throw 'Account setup failed.' }
    $publishedCredentialTarget = Get-CoremailConfigCredentialTarget -ConfigPath $configPath
    if (-not $SkipConnectionCheck -and $previousCredentialTarget -and $publishedCredentialTarget -and
        -not [string]::Equals($previousCredentialTarget, $publishedCredentialTarget, [StringComparison]::Ordinal)) {
        # The replacement configuration is already published. Once the
        # account transaction has succeeded, retire the no-longer-referenced
        # target rather than retaining a stale password for rollback.
        . $credentialScript
        Remove-CoremailCredential -Target $previousCredentialTarget
        Write-CoremailLifecycleLog "RETIRED previous mail credential target=$previousCredentialTarget"
    }
    & $smokeTest
    if (-not $?) { throw 'Installed MCP smoke test failed.' }
    if (-not $SkipConnectionCheck) {
        try {
            & $smokeTest -TimeoutMilliseconds 60000 -CheckConnection
            if (-not $?) { throw 'Live mail connection smoke test failed.' }
        }
        catch {
            Write-CoremailLifecycleFailure -ErrorRecord $_ -Context 'mail connection verification'
            throw "Live mail connection verification failed. Run CONFIGURE.cmd again with the corrected password or access token. Details: $($_.Exception.Message)"
        }
    }

    Write-Host ''
    Write-Host 'Mail account configuration completed.' -ForegroundColor Green
    Write-Host "Configuration: $(Join-Path $mailRoot 'config\settings.json')"
    Write-Host "Diagnostic log: $LogPath"
    Write-Host 'Restart Claude Code, then call mail_config_reload and mail_check_connection.'
    exit 0
}
catch {
    Write-CoremailLifecycleFailure -ErrorRecord $_ -Context 'mail configuration launcher'
    Write-Host ''
    Write-Host "Account configuration stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Diagnostic log: $LogPath"
    exit 1
}
