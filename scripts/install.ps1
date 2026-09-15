#requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Reconfigure,
    [switch]$SkipConnectionCheck,
    # Kept as a diagnostic override for publisher-side tests.  A user install
    # never falls back to PATH: the package must carry payload/runtime/python.exe.
    [string]$PythonExecutable = '',
    [string]$ClaudeCommand = '',
    [string]$LogPath = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$commonScript = Join-Path $PSScriptRoot 'windows-lifecycle-common.ps1'
$discoveryScript = Join-Path $PSScriptRoot 'windows-tool-discovery.ps1'
$credentialScript = Join-Path $PSScriptRoot 'windows-credential.ps1'
if (-not (Test-Path -LiteralPath $commonScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $discoveryScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $credentialScript -PathType Leaf)) {
    throw 'Mail assistant lifecycle support scripts are missing.'
}
. $commonScript
. $discoveryScript
. $credentialScript

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $logDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'mail-mcp-server\logs'
    $LogPath = Join-Path $logDirectory (
        'INSTALL-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' +
        [guid]::NewGuid().ToString('N').Substring(0, 8) + '.log'
    )
}
Initialize-CoremailLifecycleLog -Path $LogPath

$mcpServerName = 'mail-mcp'
$packageName = 'mail-mcp-server'
$expectedVersion = '0.9.0'
$userProfile = $null
$stateRoot = $null
$agentRoot = $null
$releaseRoot = $null
$stagingRoot = $null
$activeRoot = $null
$stageRoot = $null
$claudeInvocation = $null
$pythonRuntime = $null
$claudeUserConfigPath = $null
$lifecycleLockPath = $null
$lifecycleLockStream = $null
$claudeUserConfigSnapshot = $null
$claudeUserConfigMutationStarted = $false
$registrationCommitted = $false
$configPath = $null
$configSnapshot = $null
$configMutationStarted = $false
$configCommitted = $false
$credentialMutationStarted = $false
$previousCredentialTarget = $null
$newCredentialTarget = $null
$installationCommitted = $false
$preserveStage = $false
$sourceVersion = $null
$sourceCommit = $null
$deploymentPath = $null

function Write-Step {
    param([int]$Number, [string]$Message)
    Write-Host ''
    Write-Host "[$Number/6] $Message" -ForegroundColor Cyan
    Write-CoremailLifecycleLog "STEP $Number/6: $Message"
}

function Get-CoremailManifestVersion {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifestPath = Join-Path $Root '.claude-plugin\plugin.json'
    try { $manifest = (Get-Content -LiteralPath $manifestPath -Raw -ErrorAction Stop | ConvertFrom-Json) }
    catch { throw "Plugin manifest is unavailable or invalid: $manifestPath ($($_.Exception.Message))" }
    if ([string]$manifest.name -ne $packageName -or [string]::IsNullOrWhiteSpace([string]$manifest.version)) {
        throw "Unexpected mail package identity: $manifestPath"
    }
    return [string]$manifest.version
}

function Get-CoremailConfigCredentialTarget {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $payload = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $payload.PSObject.Properties['credential_target'] -and
            -not [string]::IsNullOrWhiteSpace([string]$payload.credential_target)) {
            return [string]$payload.credential_target
        }
    }
    catch {
        # The account setup process performs the authoritative validation.  A
        # malformed old file must not make the installer log or expose any
        # value from it; return no target and let setup report the field error.
    }
    return $null
}

function Invoke-PinnedPython {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$Label = 'Python helper',
        [string]$CapturePath = ''
    )
    Invoke-CoremailExternalChecked -Executable ([string]$pythonRuntime.executable) `
        -Arguments $Arguments -CapturePath $CapturePath -Label $Label
}

function Assert-CoremailRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowPythonRuntime
    )
    $verifier = Join-Path $Root 'scripts\verify-release.py'
    $arguments = @('-B', '-I', $verifier, $Root, '--require-windows-gate')
    if ($AllowPythonRuntime) { $arguments += '--allow-python-runtime' }
    Invoke-PinnedPython -Arguments $arguments -Label 'mail release verifier'
}

function Get-BundledPythonRuntime {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifestPath = Join-Path $Root 'payload\runtime\runtime-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "The mail package has no bundled runtime manifest: $manifestPath"
    }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
    catch { throw "The bundled mail runtime manifest is invalid: $($_.Exception.Message)" }
    if ([int]$manifest.schema_version -ne 1 -or [string]$manifest.kind -ne 'python' -or
        -not [bool]$manifest.bundled -or [string]$manifest.status -ne 'verified' -or
        [int]$manifest.pointer_bits -ne 64 -or
        [string]$manifest.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$' -or
        [string]$manifest.executable_relative_path -ne 'payload/runtime/python.exe' -or
        [string]$manifest.target.system -ne 'windows' -or [string]$manifest.target.machine -ne 'x64') {
        throw 'The package does not contain a verified Windows x64 Python runtime.'
    }
    $executable = Join-Path $Root 'payload\runtime\python.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "The bundled Python executable is missing: $executable"
    }
    $executable = [IO.Path]::GetFullPath($executable)
    $expectedHash = [string]$manifest.executable_sha256
    $actualHash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash
    if ($expectedHash -notmatch '^[0-9a-fA-F]{64}$' -or $actualHash -ine $expectedHash) {
        throw 'The bundled Python executable hash does not match the release manifest.'
    }
    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $override = [IO.Path]::GetFullPath($PythonExecutable)
        if (-not [string]::Equals($override, $executable, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'PythonExecutable cannot replace the package-bundled runtime.'
        }
    }
    return [pscustomobject]@{
        executable = $executable
        executable_sha256 = $actualHash.ToLowerInvariant()
        version = [string]$manifest.version
        pointer_bits = 64
        bundled = $true
    }
}

function Get-CoremailClaudePowerShellPath {
    $path = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Windows PowerShell is unavailable: $path" }
    return [IO.Path]::GetFullPath($path)
}

function Invoke-CoremailUserMcpRegistration {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('register', 'unregister')][string]$Operation,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$UserConfig,
        [Parameter(Mandatory = $true)][string]$BackupPath
    )
    $registrar = Join-Path $Root 'scripts\register_claude_user_mcp.py'
    if (-not (Test-Path -LiteralPath $registrar -PathType Leaf)) { throw "The mail MCP registrar is missing: $registrar" }
    $arguments = @(
        '-B', '-I', $registrar, $Operation,
        '--claude-executable', [string]$claudeInvocation.Executable,
        '--server-name', $mcpServerName,
        '--user-config', $UserConfig,
        '--backup', $BackupPath
    )
    foreach ($prefix in @($claudeInvocation.Prefix)) { $arguments += @('--claude-prefix', [string]$prefix) }
    if ($Operation -eq 'register') {
        $arguments += @(
            '--powershell-executable', (Get-CoremailClaudePowerShellPath),
            '--server-script', (Join-Path $Root 'mcp\run-server.ps1')
        )
    }
    $autoUpdaterWasPresent = Test-Path -LiteralPath 'Env:DISABLE_AUTOUPDATER'
    $updatesWerePresent = Test-Path -LiteralPath 'Env:DISABLE_UPDATES'
    $previousAutoUpdater = [string]$env:DISABLE_AUTOUPDATER
    $previousUpdates = [string]$env:DISABLE_UPDATES
    try {
        $env:DISABLE_AUTOUPDATER = '1'
        $env:DISABLE_UPDATES = '1'
        $label = if ($Operation -eq 'register') { 'Claude user-scope MCP registration' } else { 'Claude user-scope MCP removal' }
        Invoke-PinnedPython -Arguments $arguments -Label $label
    }
    finally {
        if ($autoUpdaterWasPresent) { $env:DISABLE_AUTOUPDATER = $previousAutoUpdater }
        else { Remove-Item Env:DISABLE_AUTOUPDATER -ErrorAction SilentlyContinue }
        if ($updatesWerePresent) { $env:DISABLE_UPDATES = $previousUpdates }
        else { Remove-Item Env:DISABLE_UPDATES -ErrorAction SilentlyContinue }
    }
}

function Copy-CoremailPluginTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $descriptor = [IO.Path]::GetFullPath((Join-Path $Source 'mcp\python-runtime.json'))
    foreach ($item in @(Get-ChildItem -LiteralPath $Source -Force -ErrorAction Stop)) {
        if ([string]::Equals([IO.Path]::GetFullPath($item.FullName), $descriptor, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        Copy-Item -LiteralPath $item.FullName -Destination $Destination -Recurse -Force
    }
}

function Write-PythonRuntimeDescriptor {
    param(
        [Parameter(Mandatory = $true)][string]$PluginRoot,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string]$RuntimeExecutable,
        [string]$PinnedExecutablePath = ''
    )
    $descriptorScript = Join-Path $PluginRoot 'mcp\describe-python.py'
    $probePath = $OutputPath + '.probe-' + [guid]::NewGuid().ToString('N')
    try {
        Invoke-CoremailExternalChecked -Executable $RuntimeExecutable `
            -Arguments @('-B', '-I', $descriptorScript, '--output', $probePath) `
            -Label 'Bundled Python runtime descriptor'
        $descriptor = Get-Content -LiteralPath $probePath -Raw | ConvertFrom-Json
        if ([int]$descriptor.schema_version -ne 1 -or [string]$descriptor.kind -ne 'python' -or
            -not [bool]$descriptor.bundled -or [int]$descriptor.pointer_bits -ne 64 -or
            [string]$descriptor.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
            throw 'The bundled Python runtime descriptor did not report a verified 64-bit Python interpreter.'
        }
        if (-not [string]::IsNullOrWhiteSpace($PinnedExecutablePath)) {
            $descriptor.executable = [IO.Path]::GetFullPath($PinnedExecutablePath)
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($OutputPath, ($descriptor | ConvertTo-Json -Depth 10), $utf8)
    }
    finally {
        if (Test-Path -LiteralPath $probePath -PathType Leaf) {
            Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-CoremailBuildIdentity {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifestPath = Join-Path $Root 'release-manifest.json'
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
    catch { throw "Release manifest is unavailable or invalid: $manifestPath" }
    $commit = [string]$manifest.source_commit
    if ([int]$manifest.schema_version -ne 1 -or [string]$manifest.version -ne $expectedVersion -or
        $commit -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'The package is not a valid Windows-gated mail release.'
    }
    return $commit
}

function Get-CoremailDeploymentPath {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeExecutable,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    # Hash the bundled executable content rather than a descriptor containing
    # a temporary absolute path, so reinstalling the same package/runtime can
    # reuse the immutable version directory.
    $runtimeHash = (Get-FileHash -LiteralPath $RuntimeExecutable -Algorithm SHA256).Hash.ToLowerInvariant().Substring(0, 12)
    $leaf = 'mail-mcp-server-{0}-{1}-{2}' -f $sourceVersion, $Commit.Substring(0, 12).ToLowerInvariant(), $runtimeHash
    $candidate = Join-Path $Parent $leaf
    [void](Assert-CoremailSafeDescendantPath -Root $stateRoot -Path $candidate -Label 'mail release path')
    return $candidate
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'This installer can run only on Windows.' }
    Write-Step 1 'Checking the gated package and local prerequisites'
    $userProfile = [Environment]::GetFolderPath('UserProfile')
    if ([string]::IsNullOrWhiteSpace($userProfile)) { throw 'The current Windows user profile directory could not be resolved.' }
    $stateRoot = $userProfile
    [void](Assert-CoremailSafeLocalPath -Path $stateRoot -Label 'user profile')

    $claudeUserConfigPath = Resolve-CoremailClaudeUserConfigPath -UserProfile $userProfile
    # The target machine must not supply or download the runtime.  Resolve and
    # hash the executable shipped in payload before invoking any package code.
    $pythonRuntime = Get-BundledPythonRuntime -Root $sourceRoot

    $sourceVersion = Get-CoremailManifestVersion -Root $sourceRoot
    if ($sourceVersion -ne $expectedVersion) { throw "The package version is $sourceVersion; expected $expectedVersion." }
    $sourceCommit = Get-CoremailBuildIdentity -Root $sourceRoot
    Assert-CoremailRelease -Root $sourceRoot
    # Only run a package-provided helper after the release manifest, complete
    # file set, and runtime hash have passed the gated integrity check.  The
    # native checks above intentionally do not execute arbitrary package code.
    Invoke-PinnedPython -Arguments @('-B', '-I', (Join-Path $sourceRoot 'mcp\check-python.py')) -Label 'Bundled Python runtime check'

    $claudeInvocation = Resolve-ClaudeCodeInvocation -ExplicitPath $ClaudeCommand
    if ($null -eq $claudeInvocation) {
        throw 'Claude Code CLI was not found. Start `claude --version` in a new PowerShell window, or rerun INSTALL.cmd with the CLI path as its first argument (for example: INSTALL.cmd "C:\Users\<user>\.local\bin\claude.exe"). The installer uses the existing CLI and does not install or repair Claude Code; the Claude Desktop app alone is not a CLI.'
    }
    $claudeVersion = Get-CoremailClaudeVersion -Invocation $claudeInvocation -Label 'Claude Code version probe'
    $claudeVersionDisplay = '<unreported>'
    if ($null -ne $claudeVersion.Version) { $claudeVersionDisplay = [string]$claudeVersion.Version }
    elseif ($claudeVersion.Text) { $claudeVersionDisplay = [string]$claudeVersion.Text }
    Invoke-CoremailClaudeChecked -Invocation $claudeInvocation -Arguments @('mcp', '--help') `
        -Label 'Claude MCP capability probe' -QuietOnSuccess
    Write-Host "Mail assistant package version: $sourceVersion"
    Write-Host "Pinned Python: $($pythonRuntime.executable) ($($pythonRuntime.version), $($pythonRuntime.pointer_bits)-bit)"
    Write-Host "Claude Code: $($claudeInvocation.CommandPath) ($($claudeInvocation.Kind), $claudeVersionDisplay); user MCP config: $claudeUserConfigPath"

    Write-Step 2 'Acquiring the mail assistant lock and staging an isolated version'
    $agentRoot = Join-Path $userProfile 'mail-mcp-server'
    $releaseRoot = Join-Path $agentRoot 'versions'
    $stagingRoot = Join-Path $agentRoot 'staging'
    $lifecycleLockPath = Join-Path $agentRoot '.lifecycle.lock'
    foreach ($path in @($agentRoot, $releaseRoot, $stagingRoot, $lifecycleLockPath)) {
        [void](Assert-CoremailSafeDescendantPath -Root $userProfile -Path $path -Label 'mail lifecycle path')
    }
    $lifecycleLockStream = Enter-CoremailLifecycleLock -Path $lifecycleLockPath
    New-Item -ItemType Directory -Path $releaseRoot, $stagingRoot -Force | Out-Null
    $stageRoot = Join-Path $stagingRoot ('mail-mcp-server-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    $stagePlugin = Join-Path $stageRoot 'runtime'
    Copy-CoremailPluginTree -Source $sourceRoot -Destination $stagePlugin
    Get-ChildItem -LiteralPath $stagePlugin -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    Assert-CoremailRelease -Root $stagePlugin
    $stagedPython = Join-Path $stagePlugin 'payload\runtime\python.exe'
    if (-not (Test-Path -LiteralPath $stagedPython -PathType Leaf)) {
        throw "The staged mail package is missing its bundled Python runtime: $stagedPython"
    }
    Write-PythonRuntimeDescriptor -PluginRoot $stagePlugin `
        -OutputPath (Join-Path $stagePlugin 'mcp\python-runtime.json') `
        -RuntimeExecutable $stagedPython
    $pythonRuntime = Get-Content -LiteralPath (Join-Path $stagePlugin 'mcp\python-runtime.json') -Raw | ConvertFrom-Json
    $pythonRuntime.executable = [IO.Path]::GetFullPath([string]$pythonRuntime.executable)
    Assert-CoremailRelease -Root $stagePlugin -AllowPythonRuntime
    & (Join-Path $stagePlugin 'scripts\mcp-healthcheck.ps1') -IgnoreAccountConfiguration -PythonExecutable $pythonRuntime.executable
    if (-not $?) { throw 'Staged MCP smoke test failed.' }

    Write-Step 3 'Publishing an immutable mail assistant runtime release'
    $deploymentPath = Get-CoremailDeploymentPath -RuntimeExecutable (Join-Path $stagePlugin 'payload\runtime\python.exe') -Commit $sourceCommit -Parent $releaseRoot
    $activeRoot = $deploymentPath
    $reuseExisting = $false
    if (Test-CoremailDirectoryPresent -Path $deploymentPath) {
        try {
            Assert-CoremailRelease -Root $deploymentPath -AllowPythonRuntime
            $existingDescriptor = Get-Content -LiteralPath (Join-Path $deploymentPath 'mcp\python-runtime.json') -Raw | ConvertFrom-Json
            $existingDescriptor.executable = [IO.Path]::GetFullPath([string]$existingDescriptor.executable)
            $pythonRuntime = $existingDescriptor
            & (Join-Path $deploymentPath 'scripts\mcp-healthcheck.ps1') -IgnoreAccountConfiguration -PythonExecutable $pythonRuntime.executable
            if (-not $?) { throw 'Existing runtime smoke test failed.' }
            $reuseExisting = $true
            Write-Host "Verified immutable runtime already exists; reusing: $deploymentPath"
            Write-CoremailLifecycleLog "IMMUTABLE RELEASE REUSED path=$deploymentPath"
        }
        catch {
            $activeRoot = Join-Path $releaseRoot ('mail-mcp-server-' + $sourceVersion + '-' + [guid]::NewGuid().ToString('N'))
            [void](Assert-CoremailSafeDescendantPath -Root $userProfile -Path $activeRoot -Label 'replacement mail release path')
            Write-Warning "An existing release identity was not reusable; publishing a separate immutable directory: $activeRoot"
            Write-CoremailLifecycleLog "IMMUTABLE RELEASE IDENTITY NOT REUSED path=$deploymentPath; replacement=$activeRoot; reason=$($_.Exception.Message)"
        }
    }
    if (-not $reuseExisting) {
        try {
            # Rewrite the descriptor while the tree is still in staging.  It
            # records the destination path that will exist after the atomic
            # move, so no post-publication mutation of an immutable release is
            # needed.
            Write-PythonRuntimeDescriptor -PluginRoot $stagePlugin `
                -OutputPath (Join-Path $stagePlugin 'mcp\python-runtime.json') `
                -RuntimeExecutable $stagedPython `
                -PinnedExecutablePath (Join-Path $activeRoot 'payload\runtime\python.exe')
            Move-CoremailDirectoryAtomically -Source $stagePlugin -Destination $activeRoot -OperationLabel 'Publishing the immutable mail runtime'
            $stagePlugin = $null
            $pythonRuntime = Get-Content -LiteralPath (Join-Path $activeRoot 'mcp\python-runtime.json') -Raw | ConvertFrom-Json
            $pythonRuntime.executable = [IO.Path]::GetFullPath([string]$pythonRuntime.executable)
        }
        catch {
            # The move helper deliberately leaves an ambiguous or locked
            # source untouched. Keep that staging directory for diagnosis
            # instead of deleting the only recoverable copy in finally.
            $preserveStage = $true
            throw
        }
        Write-CoremailLifecycleLog "IMMUTABLE RELEASE PUBLISHED path=$activeRoot"
    }
    Assert-CoremailRelease -Root $activeRoot -AllowPythonRuntime
    & (Join-Path $activeRoot 'scripts\mcp-healthcheck.ps1') -IgnoreAccountConfiguration -PythonExecutable $pythonRuntime.executable
    if (-not $?) { throw 'Published MCP smoke test failed.' }

    Write-Step 4 'Registering and verifying mail-mcp in Claude user scope'
    $claudeUserConfigSnapshot = Save-CoremailFileSnapshot -Path $claudeUserConfigPath -BackupDirectory $stageRoot -Label 'claude-user-config'
    $claudeUserConfigMutationStarted = $true
    Invoke-CoremailUserMcpRegistration -Operation register -Root $activeRoot -UserConfig $claudeUserConfigPath `
        -BackupPath (Join-Path $stageRoot 'claude-user-config-registrar.backup')
    $registrationCommitted = $true
    Write-Host "Claude Code user-scope MCP registered: $mcpServerName" -ForegroundColor Green

    Write-Step 5 'Preserving or configuring the mailbox account'
    $configRoot = Join-Path $agentRoot 'config'
    $configPath = Join-Path $configRoot 'settings.json'
    [void](Assert-CoremailSafeDescendantPath -Root $userProfile -Path $configPath -Label 'mail account configuration')
    New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
    if ($Reconfigure -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        $configSnapshot = Save-CoremailFileSnapshot -Path $configPath -BackupDirectory $stageRoot -Label 'mail-settings'
        $configMutationStarted = $true
        $previousCredentialTarget = Get-CoremailConfigCredentialTarget -Path $configPath
        $credentialMutationStarted = $true
        & (Join-Path $activeRoot 'scripts\setup-account.ps1') -LogPath $LogPath -LifecycleLockAlreadyHeld
        if (-not $?) { throw 'Account setup failed.' }
        $publishedCredentialTarget = Get-CoremailConfigCredentialTarget -Path $configPath
        if ($publishedCredentialTarget -and
            -not [string]::Equals($publishedCredentialTarget, $previousCredentialTarget, [StringComparison]::Ordinal)) {
            $newCredentialTarget = $publishedCredentialTarget
        }
        $configCommitted = $true
    }
    else { Write-Host "Existing non-secret account configuration preserved: $configPath" }

    Write-Step 6 'Verifying the installed MCP server and writing the result'
    $smokeTest = Join-Path $activeRoot 'scripts\mcp-healthcheck.ps1'
    & $smokeTest
    if (-not $?) { throw 'Installed MCP smoke test failed.' }
    $connectionVerified = $false
    if (-not $SkipConnectionCheck -and (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        try {
            & $smokeTest -TimeoutMilliseconds 60000 -CheckConnection
            if (-not $?) { throw 'Live mail connection smoke test failed.' }
            $connectionVerified = $true
        }
        catch {
            Write-Warning "The package is installed, but the live transport check did not pass: $($_.Exception.Message)"
            Write-CoremailLifecycleLog "WARNING live connection check failed: $($_.Exception.Message)"
        }
    }
    $summaryDirectory = Join-Path $agentRoot 'logs'
    New-Item -ItemType Directory -Path $summaryDirectory -Force | Out-Null
    $summaryPath = Join-Path $summaryDirectory 'INSTALLATION.txt'
    [void](Assert-CoremailSafeDescendantPath -Root $userProfile -Path $summaryPath -Label 'mail installation summary')
    $connectionText = if ($connectionVerified) { 'verified' } else { 'not verified' }
    $summary = @"
邮件助手安装

Version: $sourceVersion
MCP runtime: $activeRoot
Runtime root: $agentRoot
Claude MCP server: $mcpServerName (user scope; registered and verified)
Claude user MCP configuration: $claudeUserConfigPath
Python: $($pythonRuntime.executable)
Python SHA-256: $($pythonRuntime.executable_sha256)
Configuration: $configPath
Live connection: $connectionText
Lifecycle log: $LogPath

No Claude Skill directory is required or modified. Restart Claude Code, then
describe the mailbox task in natural language using the mail-mcp tools.
"@
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($summaryPath, $summary, $utf8)
    Write-CoremailLifecycleLog 'INSTALLATION COMMITTED'
    $installationCommitted = $true
    Write-Host ''
    Write-Host '邮件助手安装/升级已完成。' -ForegroundColor Green
    Write-Host "Installation summary: $summaryPath"
    Write-Host "Diagnostic log: $LogPath"
    Write-Host 'Restart Claude Code, then describe the mailbox task in natural language.'
    exit 0
}
catch {
    $installError = $_
    Write-CoremailLifecycleFailure -ErrorRecord $installError -Context 'installation'
    $rollbackFailed = $false
    # Registration and account configuration remain provisional until the
    # installation reaches the explicit commit point above.  If a later smoke,
    # configuration, or summary step fails, restore both snapshots so the
    # previous active version and settings stay usable.
    # Capture a newly published credential target before restoring the old
    # settings file; after restoration the target would no longer be visible.
    if ($credentialMutationStarted -and -not $newCredentialTarget -and $configPath) {
        $candidateTarget = Get-CoremailConfigCredentialTarget -Path $configPath
        if ($candidateTarget -and
            -not [string]::Equals($candidateTarget, $previousCredentialTarget, [StringComparison]::Ordinal)) {
            $newCredentialTarget = $candidateTarget
        }
    }
    if (-not $installationCommitted -and $configMutationStarted -and $null -ne $configSnapshot) {
        try {
            Restore-CoremailFileSnapshot -Destination ([string]$configSnapshot.Path) `
                -WasPresent ([bool]$configSnapshot.WasPresent) -BackupPath ([string]$configSnapshot.BackupPath)
            Write-CoremailLifecycleLog 'ROLLBACK restored mail account configuration'
        }
        catch {
            $rollbackFailed = $true
            $preserveStage = $true
            Write-CoremailLifecycleFailure -ErrorRecord $_ -Context 'installation account configuration rollback'
        }
    }
    if (-not $installationCommitted -and $credentialMutationStarted) {
        # Account setup normally rolls back its own unpublished credential. If
        # a later installer step fails after configuration publication, remove
        # only the newly generated target before restoring the previous file.
        # Never enumerate or touch credentials belonging to another target.
        if (-not $newCredentialTarget -and $configPath) {
            $candidateTarget = Get-CoremailConfigCredentialTarget -Path $configPath
            if ($candidateTarget -and
                -not [string]::Equals($candidateTarget, $previousCredentialTarget, [StringComparison]::Ordinal)) {
                $newCredentialTarget = $candidateTarget
            }
        }
        if ($newCredentialTarget) {
            try {
                Remove-CoremailCredential -Target $newCredentialTarget
                Write-CoremailLifecycleLog 'ROLLBACK removed newly created mail credential'
            }
            catch {
                $rollbackFailed = $true
                $preserveStage = $true
                Write-CoremailLifecycleFailure -ErrorRecord $_ -Context 'installation credential rollback'
            }
        }
    }
    if (-not $installationCommitted -and $claudeUserConfigMutationStarted -and $null -ne $claudeUserConfigSnapshot) {
        try {
            Restore-CoremailFileSnapshot -Destination ([string]$claudeUserConfigSnapshot.Path) `
                -WasPresent ([bool]$claudeUserConfigSnapshot.WasPresent) -BackupPath ([string]$claudeUserConfigSnapshot.BackupPath)
            Write-CoremailLifecycleLog 'ROLLBACK restored Claude user MCP configuration'
        }
        catch {
            $rollbackFailed = $true
            $preserveStage = $true
            Write-CoremailLifecycleFailure -ErrorRecord $_ -Context 'installation user config rollback'
        }
    }
    Write-Host ''
    Write-Host "Setup stopped safely: $($installError.Exception.Message)" -ForegroundColor Red
    if ($rollbackFailed) {
        Write-Host 'Automatic rollback encountered an error; the staging directory and diagnostic log were retained for recovery.' -ForegroundColor Red
    }
    if ($activeRoot -and (Test-Path -LiteralPath $activeRoot -PathType Container)) {
        Write-Host "Any published immutable runtime remains intact at: $activeRoot"
    }
    if ($preserveStage -and $stageRoot -and (Test-Path -LiteralPath $stageRoot -PathType Container)) {
        Write-Host "The staged runtime was retained for safe diagnosis at: $stageRoot"
    }
    Write-Host "Diagnostic log: $LogPath"
    exit 1
}
finally {
    Exit-CoremailLifecycleLock -Stream $lifecycleLockStream -Path $lifecycleLockPath
    if (-not $preserveStage -and $stageRoot -and (Test-Path -LiteralPath $stageRoot -PathType Container)) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
