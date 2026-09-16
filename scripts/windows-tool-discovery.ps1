#requires -Version 5.1

Set-StrictMode -Version 2.0

function Test-CoremailPortableExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    try {
        $stream = [IO.File]::Open(
            [IO.Path]::GetFullPath($Path),
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        if ($stream.Length -lt 70) { return $false }
        $dosHeader = New-Object byte[] 64
        if ($stream.Read($dosHeader, 0, $dosHeader.Length) -ne $dosHeader.Length -or
            $dosHeader[0] -ne 0x4D -or $dosHeader[1] -ne 0x5A) {
            return $false
        }
        $peOffset = [BitConverter]::ToInt32($dosHeader, 0x3C)
        if ($peOffset -lt 64 -or $peOffset -gt ($stream.Length - 6)) { return $false }
        $stream.Position = $peOffset
        $peHeader = New-Object byte[] 6
        if ($stream.Read($peHeader, 0, $peHeader.Length) -ne $peHeader.Length) {
            return $false
        }
        return ($peHeader[0] -eq 0x50 -and
            $peHeader[1] -eq 0x45 -and
            $peHeader[2] -eq 0 -and
            $peHeader[3] -eq 0)
    }
    catch { return $false }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Test-CoremailPathChainSafe {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    # Package, configuration, and lifecycle paths are fail-closed: checking
    # only the final file is insufficient because a junction in a parent
    # directory can redirect an otherwise ordinary-looking path. External
    # Claude/Node installations use Resolve-CoremailExternalFilePath instead,
    # because WinGet and NVM legitimately expose links. Discovery is
    # Windows-only, so local-drive paths are required here.
    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        if ($fullPath -notmatch '^[A-Za-z]:\\') { return $false }
        $rootPath = [IO.Path]::GetPathRoot($fullPath)
        $root = $rootPath.TrimEnd('\')
        if (Test-Path -LiteralPath $rootPath) {
            $rootItem = Get-Item -LiteralPath $rootPath -Force -ErrorAction Stop
            if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
        }
        $cursor = $fullPath.TrimEnd('\')
        while ($true) {
            if (Test-Path -LiteralPath $cursor) {
                $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    return $false
                }
            }
            if ([string]::Equals($cursor, $root, [StringComparison]::OrdinalIgnoreCase)) {
                break
            }
            $parent = Split-Path -Parent $cursor
            if ([string]::IsNullOrWhiteSpace($parent) -or
                [string]::Equals($parent, $cursor, [StringComparison]::OrdinalIgnoreCase)) {
                return $false
            }
            $cursor = $parent.TrimEnd('\')
        }
        return $true
    }
    catch { return $false }
}

function Resolve-CoremailExternalFilePath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    # External developer tools may legitimately use a symlink or junction
    # (WinGet Links, NVM, and npm bin trees do this).  The package/config
    # boundary still rejects reparse points; for an external executable we
    # open the file and ask Windows for the final path before validating it.
    $fullPath = [IO.Path]::GetFullPath($Path)
    if ($fullPath -notmatch '^[A-Za-z]:\\') {
        throw "external executable is not an absolute local-drive path: $Path"
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "external executable does not exist: $Path"
    }

    if ($null -eq ('MailMcp.ExternalFilePath' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Text;
using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;

namespace MailMcp {
    public static class ExternalFilePath {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(
            SafeFileHandle hFile,
            StringBuilder lpszFilePath,
            uint cchFilePath,
            uint dwFlags);

        public static string GetFinalPath(SafeFileHandle handle) {
            uint capacity = 512;
            for (int attempt = 0; attempt < 5; attempt++) {
                var buffer = new StringBuilder((int)capacity);
                uint length = GetFinalPathNameByHandle(handle, buffer, capacity, 0);
                if (length == 0) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (length < capacity) {
                    return buffer.ToString();
                }
                capacity = length + 1;
            }
            throw new IOException("The final external executable path is too long.");
        }
    }
}
'@ -Language CSharp
    }

    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $fullPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        )
        $finalPath = [MailMcp.ExternalFilePath]::GetFinalPath($stream.SafeFileHandle)
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }

    if ($finalPath.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "external executable resolves to a UNC path: $Path"
    }
    if ($finalPath.StartsWith('\\?\', [StringComparison]::OrdinalIgnoreCase)) {
        $finalPath = $finalPath.Substring(4)
    }
    if ($finalPath -notmatch '^[A-Za-z]:\\') {
        throw "external executable resolved to an unsupported path: $Path"
    }
    return [IO.Path]::GetFullPath($finalPath)
}

function Test-CoremailNativeCliExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        if ($fullPath -match '(?i)\\(?:Microsoft\\WindowsApps|Program Files\\WindowsApps)\\') {
            # Windows app-execution aliases can resolve as claude.exe but may
            # launch Claude Desktop rather than the Claude Code CLI.
            return $null
        }
        $finalPath = Resolve-CoremailExternalFilePath -Path $fullPath
        if ($finalPath -match '(?i)\\(?:Microsoft\\WindowsApps|Program Files\\WindowsApps)\\') {
            return $null
        }
        if (-not (Test-CoremailPathChainSafe -Path $finalPath) -or
            -not (Test-CoremailPortableExecutable -Path $finalPath)) {
            return $null
        }
        return $finalPath
    }
    catch { return $null }
}

function Resolve-NpmClaudeInvocation {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$CommandPath)

    try {
        $resolvedCommand = Resolve-CoremailExternalFilePath -Path $CommandPath
        if ([IO.Path]::GetExtension($resolvedCommand) -ine '.cmd') { return $null }
        $commandRoot = Split-Path -Parent $resolvedCommand
        $packageRootCandidate = Join-Path $commandRoot 'node_modules\@anthropic-ai\claude-code'
        if (-not (Test-Path -LiteralPath $packageRootCandidate -PathType Container)) {
            return $null
        }
        $packageRoot = [IO.Path]::GetFullPath($packageRootCandidate).TrimEnd('\')
        $packagePath = Join-Path $packageRoot 'package.json'
        $resolvedPackagePath = Resolve-CoremailExternalFilePath -Path $packagePath
        $utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
        $package = [IO.File]::ReadAllText($resolvedPackagePath, $utf8Strict) |
            ConvertFrom-Json -ErrorAction Stop
        if ([string]$package.name -ne '@anthropic-ai/claude-code') {
            return $null
        }

        $binPath = ''
        if ($package.bin -is [string]) {
            $binPath = [string]$package.bin
        }
        elseif ($null -ne $package.bin) {
            $claudeBin = $package.bin.PSObject.Properties['claude']
            if ($null -ne $claudeBin) { $binPath = [string]$claudeBin.Value }
        }
        if ([string]::IsNullOrWhiteSpace($binPath) -or [IO.Path]::IsPathRooted($binPath)) {
            return $null
        }
        $packageRootFull = [IO.Path]::GetFullPath($packageRoot).TrimEnd('\')
        $cliPath = [IO.Path]::GetFullPath((Join-Path $packageRootFull $binPath))
        if (-not $cliPath.StartsWith(
            $packageRootFull + '\',
            [StringComparison]::OrdinalIgnoreCase
        ) -or -not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
            return $null
        }
        $resolvedCliPath = Resolve-CoremailExternalFilePath -Path $cliPath
        $cliExtension = [IO.Path]::GetExtension($resolvedCliPath)
        if ($cliExtension -ieq '.exe') {
            # Current npm releases replace their declared bin/claude.exe stub
            # with the platform-native PE during postinstall. Execute that
            # declared binary directly; passing it to node.exe is incorrect.
            $nativeCli = Test-CoremailNativeCliExecutable -Path $resolvedCliPath
            if ([string]::IsNullOrWhiteSpace([string]$nativeCli)) { return $null }
            return [pscustomobject]@{
                CommandPath = $resolvedCommand
                Executable = $nativeCli
                Prefix = [string[]]@()
                Kind = 'npm'
                NpmBinKind = 'native'
            }
        }
        if ($cliExtension -notin @('.js', '.cjs', '.mjs')) { return $null }

        $nodeCandidates = @((Join-Path $commandRoot 'node.exe'))
        $nodeCommand = Get-Command 'node.exe' -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $nodeCommand) {
            $nodeCandidates += if ($nodeCommand.Source) { $nodeCommand.Source } else { $nodeCommand.Path }
        }
        foreach ($nodeCandidate in $nodeCandidates) {
            if ([string]::IsNullOrWhiteSpace($nodeCandidate) -or
                -not (Test-Path -LiteralPath $nodeCandidate -PathType Leaf)) { continue }
            try { $resolvedNode = Resolve-CoremailExternalFilePath -Path $nodeCandidate }
            catch { continue }
            if ([IO.Path]::GetExtension($resolvedNode) -ine '.exe' -or
                [string]::IsNullOrWhiteSpace([string](Test-CoremailNativeCliExecutable -Path $resolvedNode))) { continue }
            return [pscustomobject]@{
                CommandPath = $resolvedCommand
                Executable = $resolvedNode
                Prefix = [string[]]@($cliPath)
                Kind = 'npm'
                NpmBinKind = 'node'
            }
        }
    }
    catch {
        throw
    }
    return $null
}

function Resolve-ClaudeCodeInvocation {
    [CmdletBinding()]
    param(
        [string]$ExplicitPath = '',
        [System.Collections.IList]$Diagnostics = $null
    )

    $candidates = New-Object System.Collections.ArrayList
    function Add-ClaudeDiagnostic {
        param([string]$Message)
        if ($null -ne $Diagnostics -and -not [string]::IsNullOrWhiteSpace($Message)) {
            [void]$Diagnostics.Add($Message)
        }
    }
    function Add-ClaudeCandidate {
        param([string]$Candidate)
        if ([string]::IsNullOrWhiteSpace($Candidate)) { return }
        foreach ($existing in $candidates) {
            if ([string]::Equals([string]$existing, $Candidate, [StringComparison]::OrdinalIgnoreCase)) {
                return
            }
        }
        [void]$candidates.Add($Candidate)
    }
    function Add-ClaudeCandidatesFromDirectory {
        param([string]$Directory)
        if ([string]::IsNullOrWhiteSpace($Directory)) { return }
        foreach ($name in @('claude.exe', 'claude.cmd')) {
            try { Add-ClaudeCandidate -Candidate (Join-Path $Directory $name) }
            catch { }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        Add-ClaudeCandidate -Candidate $ExplicitPath
    }
    else {
        # Match the discovery baseline used by the other Windows Claude
        # integrations: query the native Windows resolver first so every PATH
        # result is considered, including WinGet links and .cmd shims that
        # PowerShell may not return as the first command.
        $whereExe = Join-Path $env:SystemRoot 'System32\where.exe'
        if (Test-Path -LiteralPath $whereExe -PathType Leaf) {
            try {
                $whereResults = @(& $whereExe 'claude' 2>$null)
                foreach ($whereResult in $whereResults) {
                    Add-ClaudeCandidate -Candidate ([string]$whereResult).Trim()
                }
            }
            catch { }
        }
        foreach ($commandName in @('claude.exe', 'claude.cmd')) {
            $commands = @(Get-Command $commandName -CommandType Application -All -ErrorAction SilentlyContinue)
            foreach ($command in $commands) {
                $resolved = if ($command.Source) { $command.Source } else { $command.Path }
                Add-ClaudeCandidate -Candidate $resolved
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            # Native installer and legacy per-user npm installation.
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:USERPROFILE '.local\bin')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:USERPROFILE '.claude\local')
        }
        if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
            # The standard Windows global npm prefix.
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:APPDATA 'npm')
        }
        if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            # WinGet's portable Claude Code package normally exposes this
            # user-level link. Older native installs use the Programs paths.
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:LOCALAPPDATA 'Programs\claude')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:LOCALAPPDATA 'Programs\claude\bin')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:LOCALAPPDATA 'Programs\ClaudeCode')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $env:LOCALAPPDATA 'Programs\ClaudeCode\bin')

            # Some WinGet versions leave the portable executable in the
            # package cache without creating a Links entry. Search only the
            # exact Anthropic package prefix; every candidate still passes the
            # complete reparse-point and executable checks below.
            $wingetPackages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
            if (Test-Path -LiteralPath $wingetPackages -PathType Container) {
                try {
                    $packageDirectories = @(Get-ChildItem -LiteralPath $wingetPackages -Directory -Filter 'Anthropic.ClaudeCode*' -ErrorAction SilentlyContinue |
                        Sort-Object LastWriteTime -Descending)
                    foreach ($packageDirectory in $packageDirectories) {
                        Add-ClaudeCandidatesFromDirectory -Directory $packageDirectory.FullName
                        Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $packageDirectory.FullName 'bin')
                    }
                }
                catch { }
            }
        }
        foreach ($programRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            if ([string]::IsNullOrWhiteSpace($programRoot)) { continue }
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $programRoot 'ClaudeCode')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $programRoot 'Programs\ClaudeCode')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $programRoot 'Claude')
            Add-ClaudeCandidatesFromDirectory -Directory (Join-Path $programRoot 'Programs\Claude')
        }
        # Explorer-launched .cmd files can inherit a stale process PATH after a
        # Claude installation. Include current, user, and machine PATH values.
        foreach ($pathValue in @(
            $env:Path,
            [Environment]::GetEnvironmentVariable('Path', 'User'),
            [Environment]::GetEnvironmentVariable('Path', 'Machine')
        )) {
            if ([string]::IsNullOrWhiteSpace($pathValue)) { continue }
            foreach ($directory in $pathValue -split ';') {
                Add-ClaudeCandidatesFromDirectory -Directory $directory.Trim()
            }
        }
    }

    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate) -or
            -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $extension = [IO.Path]::GetExtension($candidate)
        if ($extension -ieq '.exe') {
            $nativeCli = Test-CoremailNativeCliExecutable -Path $candidate
            if (-not [string]::IsNullOrWhiteSpace([string]$nativeCli)) {
                Add-ClaudeDiagnostic -Message ("accepted native candidate: {0} -> {1}" -f $candidate, $nativeCli)
                return [pscustomobject]@{
                    CommandPath = $candidate
                    Executable = $nativeCli
                    Prefix = [string[]]@()
                    Kind = 'native'
                }
            }
            Add-ClaudeDiagnostic -Message ("rejected native candidate: {0} (not a usable local Claude Code PE or final path)" -f $candidate)
        }
        if ($extension -ieq '.cmd') {
            try {
                $npmInvocation = Resolve-NpmClaudeInvocation -CommandPath $candidate
                if ($null -ne $npmInvocation) {
                    Add-ClaudeDiagnostic -Message ("accepted npm candidate: {0} -> {1}" -f $candidate, $npmInvocation.Executable)
                    return $npmInvocation
                }
                Add-ClaudeDiagnostic -Message ("rejected npm candidate: {0} (package identity, bin, Node, or PE validation failed)" -f $candidate)
            }
            catch {
                Add-ClaudeDiagnostic -Message ("rejected npm candidate: {0} ({1})" -f $candidate, $_.Exception.Message)
            }
        }
        if ($extension -notin @('.exe', '.cmd')) {
            Add-ClaudeDiagnostic -Message ("ignored candidate with unsupported extension: {0}" -f $candidate)
        }
    }
    Add-ClaudeDiagnostic -Message 'no usable Claude Code CLI candidate remained after validation'
    return $null
}
