#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PluginRoot,
    [Parameter(Mandatory = $true)][string]$RunnerTemp,
    [Parameter(Mandatory = $true)][string]$PythonCommand,
    [Parameter(Mandatory = $true)][string]$ClaudeCommand,
    [Parameter(Mandatory = $true)][string]$ExpectedIdentitySid,
    [ValidateSet('native', 'npm')][string]$ScenarioName,
    [Parameter(Mandatory = $true)][switch]$OrchestratorVerifiedHostedRunner
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'The Windows lifecycle gate can run only on Windows.' }
if (-not $OrchestratorVerifiedHostedRunner) { throw 'The gate must be launched by the verified hosted-runner orchestrator.' }
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -lt 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)."
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.User.Value -ne $ExpectedIdentitySid) { throw "The lifecycle gate is running as an unexpected identity: $($identity.Name)" }
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Lifecycle checks must run as a standard user.' }

$RunnerTemp = [IO.Path]::GetFullPath($RunnerTemp)
$PluginRoot = [IO.Path]::GetFullPath($PluginRoot)
$PythonCommand = [IO.Path]::GetFullPath($PythonCommand)
$ClaudeCommand = [IO.Path]::GetFullPath($ClaudeCommand)
foreach ($required in @($RunnerTemp, $PluginRoot, $PythonCommand, $ClaudeCommand)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Gate prerequisite is unavailable: $required" }
}
$env:RUNNER_TEMP = $RunnerTemp
$env:TEMP = $RunnerTemp
$env:TMP = $RunnerTemp
$env:DISABLE_AUTOUPDATER = '1'
$env:DISABLE_UPDATES = '1'
$env:MAIL_RELEASE_GATE_TESTING = 'true'
Remove-Item Env:MAIL_PYTHON -ErrorAction SilentlyContinue
Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue

$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$userProfile = [Environment]::GetFolderPath('UserProfile')
if ([string]::IsNullOrWhiteSpace($userProfile)) { throw 'Disposable user profile path could not be resolved.' }
$env:USERPROFILE = $userProfile

$claudeRoot = Join-Path $userProfile '.claude'
$claudeUserConfigPath = Join-Path $userProfile '.claude.json'
$agentRoot = Join-Path $userProfile 'mail-mcp-server'
$configDirectory = Join-Path $agentRoot 'config'
$configPath = Join-Path $configDirectory 'settings.json'
$releaseRoot = Join-Path $agentRoot 'versions'
$lifecycleLockPath = Join-Path $agentRoot '.lifecycle.lock'
$legacySkillRoot = Join-Path $claudeRoot 'skills\old-mail-provider'
$installer = Join-Path $PluginRoot 'scripts\install.ps1'
$uninstaller = Join-Path $PluginRoot 'scripts\uninstall.ps1'
$commonScript = Join-Path $PluginRoot 'scripts\windows-lifecycle-common.ps1'
$discoveryScript = Join-Path $PluginRoot 'scripts\windows-tool-discovery.ps1'
. $commonScript
. $discoveryScript
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-WindowsPowerShellScript {
    param([Parameter(Mandatory = $true)][string]$ScriptPath, [string[]]$ScriptArguments = @(), [switch]$ExpectFailure, [string]$ExpectedText = '')
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = $null
        $output = & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -File $ScriptPath @ScriptArguments 2>&1
        $exitCode = $global:LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    $text = (@($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine)
    foreach ($line in @($output)) { if ($null -ne $line) { Write-Host ([string]$line) } }
    if ($ExpectFailure) {
        if ($exitCode -eq 0) { throw "Expected script failure but it exited successfully: $ScriptPath" }
        if ($ExpectedText -and $text -notmatch [regex]::Escape($ExpectedText)) { throw "Expected failure text '$ExpectedText' was absent: $text" }
        return
    }
    if ($exitCode -ne 0) { throw "Windows PowerShell script failed with exit code $exitCode`: $ScriptPath`n$text" }
}

function Get-RegisteredPackageRoot {
    if (-not (Test-Path -LiteralPath $claudeUserConfigPath -PathType Leaf)) { throw 'Claude user configuration is missing.' }
    $payload = Get-Content -LiteralPath $claudeUserConfigPath -Raw | ConvertFrom-Json
    $servers = $payload.PSObject.Properties['mcpServers']
    if ($null -eq $servers -or $null -eq $servers.Value) { throw 'Claude user-scope MCP configuration is missing.' }
    $entry = $servers.Value.PSObject.Properties['mail-mcp']
    if ($null -eq $entry -or $null -eq $entry.Value) { throw 'Mail user-scope MCP entry is missing.' }
    $arguments = @($entry.Value.args | ForEach-Object { [string]$_ })
    $fileIndex = [Array]::IndexOf($arguments, '-File')
    if ($fileIndex -lt 0 -or $fileIndex + 1 -ge $arguments.Count) { throw 'Mail user-scope MCP entry has no -File launcher.' }
    $serverScript = [IO.Path]::GetFullPath($arguments[$fileIndex + 1])
    $root = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $serverScript)))
    if (-not $root.StartsWith(([IO.Path]::GetFullPath($releaseRoot)).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Registered runtime escaped the mail versions root: $root"
    }
    return $root
}

function Assert-UserMcpRegistered {
    param([string]$ExpectedRoot = '')
    $root = Get-RegisteredPackageRoot
    if ($ExpectedRoot -and -not [string]::Equals([IO.Path]::GetFullPath($root).TrimEnd('\'), [IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unexpected registered runtime: $root (expected $ExpectedRoot)"
    }
    $registrar = Join-Path $root 'scripts\register_claude_user_mcp.py'
    # The registrar's success message is for the console only.  Suppress it
    # from the function success stream so callers receive exactly one value:
    # the registered runtime root.  Without this, PowerShell returns an
    # Object[] containing the message and the path; a later Join-Path then
    # fails with the misleading "path's format is not supported" error.
    & $PythonCommand -B -I $registrar verify --server-name 'mail-mcp' --user-config $claudeUserConfigPath `
        --powershell-executable $windowsPowerShell --server-script (Join-Path $root 'mcp\run-server.ps1') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Claude user-scope MCP verification failed.' }
    return $root
}

function Assert-UserMcpAbsent {
    if (-not (Test-Path -LiteralPath $claudeUserConfigPath -PathType Leaf)) { return }
    $payload = Get-Content -LiteralPath $claudeUserConfigPath -Raw | ConvertFrom-Json
    $servers = $payload.PSObject.Properties['mcpServers']
    if ($null -ne $servers -and $null -ne $servers.Value -and $null -ne $servers.Value.PSObject.Properties['mail-mcp']) {
        throw 'Mail user-scope MCP entry is still present.'
    }
}

function Assert-ConfigUnchanged {
    param([string]$ExpectedHash)
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw 'Mailbox configuration was removed.' }
    if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Mailbox configuration changed during lifecycle.' }
}

if ((Test-Path -LiteralPath $configPath) -or (Test-Path -LiteralPath $claudeUserConfigPath) -or (Test-Path -LiteralPath $agentRoot)) {
    throw 'Disposable profile unexpectedly contains mail assistant state.'
}

# Exercise the same native installation path used by a normal Claude Code
# install.  The lifecycle scenarios pass an explicit fixture path to keep the
# rest of the gate deterministic; this check makes sure automatic discovery is
# also covered instead of being validated only by static string assertions.
if ($ScenarioName -eq 'native') {
    $autoClaudeRoot = Join-Path $userProfile '.local\bin'
    $autoClaudePath = Join-Path $autoClaudeRoot 'claude.exe'
    New-Item -ItemType Directory -Path $autoClaudeRoot -Force | Out-Null
    try {
        Copy-Item -LiteralPath $ClaudeCommand -Destination $autoClaudePath -Force
        $autoInvocation = Resolve-ClaudeCodeInvocation
        if ($null -eq $autoInvocation -or
            $autoInvocation.Kind -ne 'native' -or
            -not [string]::Equals(
                [IO.Path]::GetFullPath($autoInvocation.Executable),
                [IO.Path]::GetFullPath($autoClaudePath),
                [StringComparison]::OrdinalIgnoreCase
            )) {
            throw 'Automatic Claude Code discovery did not resolve the native user installation path.'
        }
    }
    finally {
        Remove-Item -LiteralPath $autoClaudePath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $autoClaudeRoot -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[gate 1/10][$ScenarioName] Parse packaged PowerShell and compile Credential Manager helper"
$parseFailures = @()
foreach ($scriptFile in (Get-ChildItem -LiteralPath $PluginRoot -Filter '*.ps1' -File -Recurse)) {
    $tokens = $null; $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$errors)
    foreach ($errorRecord in @($errors)) { $parseFailures += "$($scriptFile.FullName): $($errorRecord.Message)" }
}
if ($parseFailures.Count -gt 0) { throw "PowerShell parse failures:`n$($parseFailures -join "`n")" }
$credentialText = Get-Content -LiteralPath (Join-Path $PluginRoot 'scripts\windows-credential.ps1') -Raw
$credentialPattern = '(?ms)^[ \t]*\$credentialSource[ \t]*=[ \t]*@''\r?\n(?<source>.*?)\r?\n''@[ \t]*\r?$'
$credentialMatch = [regex]::Match($credentialText, $credentialPattern)
if (-not $credentialMatch.Success) { throw 'Unable to extract the credential helper C# source.' }
Add-Type -TypeDefinition $credentialMatch.Groups['source'].Value -Language CSharp | Out-Null

Write-Host "[gate 2/10][$ScenarioName] Verify package and MCP protocol"
& $PythonCommand -B -I (Join-Path $PluginRoot 'scripts\verify-release.py') $PluginRoot --require-windows-gate
if ($LASTEXITCODE -ne 0) { throw 'Packaged internal integrity verification failed.' }
& (Join-Path $PluginRoot 'scripts\mcp-healthcheck.ps1') -IgnoreAccountConfiguration -PythonExecutable $PythonCommand
if (-not $?) { throw 'Packaged MCP smoke test failed.' }

Write-Host "[gate 3/10][$ScenarioName] Create mailbox fixture and unrelated Claude setting"
New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
$fixture = [ordered]@{ schema_version = 1; provider = 'coremail'; transport = 'windows_simple_mapi'; username = 'ci-fixture@example.invalid'; allowed_from = @('ci-fixture@example.invalid'); sent_copy_mode = 'none'; attachment_roots = @() }
[IO.File]::WriteAllText($configPath, ($fixture | ConvertTo-Json -Depth 8), $utf8)
$fixtureHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
New-Item -ItemType Directory -Path $claudeRoot -Force | Out-Null
[IO.File]::WriteAllText($claudeUserConfigPath, ([ordered]@{ customSetting = 'preserve-me' } | ConvertTo-Json), $utf8)
$userConfigHash = (Get-FileHash -LiteralPath $claudeUserConfigPath -Algorithm SHA256).Hash
$env:CLAUDE_CONFIG_DIR = 'relative-custom-claude-root'
try {
    Invoke-WindowsPowerShellScript -ScriptPath $installer -ExpectFailure -ExpectedText 'absolute local-drive path' -ScriptArguments @('-SkipConnectionCheck', '-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "CUSTOM-$ScenarioName.log"))
}
finally { Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue }
if ((Test-Path -LiteralPath $releaseRoot) -or
    (Get-FileHash -LiteralPath $claudeUserConfigPath -Algorithm SHA256).Hash -ne $userConfigHash) {
    throw 'Rejected custom Claude root changed state.'
}
Assert-ConfigUnchanged -ExpectedHash $fixtureHash

Write-Host "[gate 4/10][$ScenarioName] Install without touching a locked legacy Skill directory"
New-Item -ItemType Directory -Path $legacySkillRoot -Force | Out-Null
$legacyMarker = Join-Path $legacySkillRoot 'legacy-marker.txt'
[IO.File]::WriteAllText($legacyMarker, 'must remain byte-for-byte unchanged', $utf8)
$legacyHash = (Get-FileHash -LiteralPath $legacyMarker -Algorithm SHA256).Hash
$legacyStream = [IO.File]::Open($legacyMarker, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {
    Invoke-WindowsPowerShellScript -ScriptPath $installer -ScriptArguments @('-SkipConnectionCheck', '-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "INSTALL-$ScenarioName.log"))
}
finally { $legacyStream.Dispose() }
$firstRoot = Assert-UserMcpRegistered
if (-not $firstRoot.StartsWith(([IO.Path]::GetFullPath($releaseRoot)).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer did not publish below the mail versions directory.' }
if (-not (Test-Path -LiteralPath (Join-Path $firstRoot 'mcp\python-runtime.json') -PathType Leaf)) { throw 'Pinned runtime descriptor is missing.' }
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
if ((Get-FileHash -LiteralPath $legacyMarker -Algorithm SHA256).Hash -ne $legacyHash) { throw 'Installer modified the legacy Skill fixture.' }

Write-Host "[gate 5/10][$ScenarioName] Publish a new immutable release while the active runtime is locked"
$lockedRuntimeFile = Join-Path $firstRoot 'README.md'
$runtimeStream = [IO.File]::Open($lockedRuntimeFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {
    Invoke-WindowsPowerShellScript -ScriptPath $installer -ScriptArguments @('-SkipConnectionCheck', '-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "REINSTALL-$ScenarioName.log"))
}
finally { $runtimeStream.Dispose() }
$secondRoot = Assert-UserMcpRegistered
if ([string]::Equals([IO.Path]::GetFullPath($firstRoot).TrimEnd('\'), [IO.Path]::GetFullPath($secondRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Locked active runtime was modified in place instead of publishing a new release.' }
if (-not (Test-Path -LiteralPath $firstRoot -PathType Container)) { throw 'Previous immutable runtime was not retained.' }
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
if ((Get-FileHash -LiteralPath $legacyMarker -Algorithm SHA256).Hash -ne $legacyHash) { throw 'Legacy Skill fixture changed during immutable upgrade.' }

Write-Host "[gate 6/10][$ScenarioName] Prove lifecycle lock contention is fail-closed"
$lockStream = [IO.File]::Open($lifecycleLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$beforeContentHash = (Get-FileHash -LiteralPath $claudeUserConfigPath -Algorithm SHA256).Hash
try {
    Invoke-WindowsPowerShellScript -ScriptPath $installer -ExpectFailure -ExpectedText 'already running' -ScriptArguments @('-SkipConnectionCheck', '-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "LOCK-$ScenarioName.log"))
}
finally { $lockStream.Dispose() }
if ((Get-FileHash -LiteralPath $claudeUserConfigPath -Algorithm SHA256).Hash -ne $beforeContentHash) { throw 'Lock contention changed Claude configuration.' }

Write-Host "[gate 7/10][$ScenarioName] Verify account transaction rollback"
$credentialScript = Join-Path $secondRoot 'scripts\windows-credential.ps1'
. $credentialScript
$credentialTarget = 'MailMcp.Coremail:rollback@example.invalid:' + [guid]::NewGuid().ToString('N')
$securePassword = ConvertTo-SecureString ('Gate9!' + [guid]::NewGuid().ToString('N')) -AsPlainText -Force
try {
    try {
        & (Join-Path $secondRoot 'scripts\setup-account.ps1') -Transport imap_smtp -Username 'rollback@example.invalid' -ImapHost 'imap.example.invalid' -SmtpHost 'smtp.example.invalid' -CredentialTarget $credentialTarget -Password $securePassword -NonInteractive -TestFailurePoint after_credential_write -LogPath (Join-Path $RunnerTemp "ACCOUNT-$ScenarioName.log")
        throw 'Injected account failure unexpectedly succeeded.'
    }
    catch { if ($_.Exception.Message -notmatch 'Injected release-gate failure') { throw } }
}
finally { $securePassword.Dispose() }
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
if (Test-CoremailCredential -Target $credentialTarget) { throw 'Failed account transaction left a credential behind.' }

Write-Host "[gate 8/10][$ScenarioName] Verify atomic same-volume publication and no elevation path"
$moveRoot = Join-Path $RunnerTemp ('move-' + [guid]::NewGuid().ToString('N'))
$moveSource = Join-Path $moveRoot 'source'; $moveDestination = Join-Path $moveRoot 'destination'
New-Item -ItemType Directory -Path $moveSource -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $moveSource 'marker.txt'), 'atomic', $utf8)
$moveLog = Join-Path $RunnerTemp "MOVE-$ScenarioName.log"
Initialize-CoremailLifecycleLog -Path $moveLog
Move-CoremailDirectoryAtomically -Source $moveSource -Destination $moveDestination -OperationLabel 'Release-gate atomic move'
if (-not (Test-Path -LiteralPath (Join-Path $moveDestination 'marker.txt') -PathType Leaf)) { throw 'Atomic move fixture failed.' }
$lifecycleSources = (Get-Content -LiteralPath (Join-Path $PluginRoot 'scripts\install.ps1') -Raw) + (Get-Content -LiteralPath (Join-Path $PluginRoot 'scripts\uninstall.ps1') -Raw) + (Get-Content -LiteralPath $commonScript -Raw)
if ($lifecycleSources -match '(?i)RunAs|icacls|Read-Host') { throw 'Lifecycle package still contains an elevation/manual prompt path.' }

Write-Host "[gate 9/10][$ScenarioName] Uninstall while the registered runtime is locked"
$registeredBeforeUninstall = Get-RegisteredPackageRoot
$lockedServer = Join-Path $registeredBeforeUninstall 'mcp\server.py'
$serverStream = [IO.File]::Open($lockedServer, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {
    Invoke-WindowsPowerShellScript -ScriptPath $uninstaller -ScriptArguments @('-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "UNINSTALL-$ScenarioName.log"))
}
finally { $serverStream.Dispose() }
Assert-UserMcpAbsent
if (-not (Test-Path -LiteralPath $registeredBeforeUninstall -PathType Container)) { throw 'Uninstall deleted the active immutable runtime.' }
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
if ((Get-FileHash -LiteralPath $legacyMarker -Algorithm SHA256).Hash -ne $legacyHash) { throw 'Uninstall modified the legacy Skill fixture.' }

Write-Host "[gate 10/10][$ScenarioName] Reinstall and idempotent removal"
Invoke-WindowsPowerShellScript -ScriptPath $installer -ScriptArguments @('-SkipConnectionCheck', '-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "FINAL-INSTALL-$ScenarioName.log"))
$finalRoot = Assert-UserMcpRegistered
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
Invoke-WindowsPowerShellScript -ScriptPath (Join-Path $finalRoot 'scripts\uninstall.ps1') -ScriptArguments @('-ClaudeCommand', $ClaudeCommand, '-LogPath', (Join-Path $RunnerTemp "FINAL-UNINSTALL-$ScenarioName.log"))
Assert-UserMcpAbsent
if (-not (Test-Path -LiteralPath $finalRoot -PathType Container)) { throw 'Final uninstall deleted the immutable runtime.' }
Assert-ConfigUnchanged -ExpectedHash $fixtureHash
Invoke-WindowsPowerShellScript -ScriptPath $uninstaller -ScriptArguments @('-ClaudeCommand', (Join-Path $RunnerTemp 'missing-claude.exe'), '-LogPath', (Join-Path $RunnerTemp "IDEMPOTENT-$ScenarioName.log"))
if ((Get-FileHash -LiteralPath $legacyMarker -Algorithm SHA256).Hash -ne $legacyHash) { throw 'Idempotent uninstall modified the legacy Skill fixture.' }

$legacyStream = $null
if (Test-Path -LiteralPath $legacyMarker) { Remove-Item -LiteralPath $legacyMarker -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath $legacySkillRoot) { Remove-Item -LiteralPath $legacySkillRoot -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "Windows PowerShell 5.1 packaged lifecycle gate passed: $ScenarioName" -ForegroundColor Green
