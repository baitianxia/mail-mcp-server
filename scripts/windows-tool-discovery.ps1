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

    # Checking only the final file is insufficient: a junction in a parent
    # node_modules directory can redirect an otherwise ordinary-looking Claude
    # launcher or package. Walk every existing component before Resolve-Path
    # follows anything. Discovery is Windows-only, so local-drive paths are
    # required just as they are for the lifecycle scripts.
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

function Test-CoremailNativeCliExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        if ($fullPath -match '(?i)\\Microsoft\\WindowsApps\\') {
            # Windows app-execution aliases can resolve as claude.exe but may
            # launch Claude Desktop rather than the Claude Code CLI.
            return $false
        }
        if (-not (Test-CoremailPathChainSafe -Path $fullPath)) { return $false }
        $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
        if ($item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        return (Test-CoremailPortableExecutable -Path $fullPath)
    }
    catch { return $false }
}

function Resolve-NpmClaudeInvocation {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$CommandPath)

    if (-not (Test-CoremailPathChainSafe -Path $CommandPath) -or
        -not (Test-Path -LiteralPath $CommandPath -PathType Leaf) -or
        [IO.Path]::GetExtension($CommandPath) -ine '.cmd') {
        return $null
    }

    try {
        $resolvedCommand = (Resolve-Path -LiteralPath $CommandPath -ErrorAction Stop).Path
        if (-not (Test-CoremailPathChainSafe -Path $resolvedCommand)) { return $null }
        $commandItem = Get-Item -LiteralPath $resolvedCommand -Force -ErrorAction Stop
        if (($commandItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $null
        }
        $commandRoot = Split-Path -Parent $resolvedCommand
        $packageRootCandidate = Join-Path $commandRoot 'node_modules\@anthropic-ai\claude-code'
        if (-not (Test-CoremailPathChainSafe -Path $packageRootCandidate) -or
            -not (Test-Path -LiteralPath $packageRootCandidate -PathType Container)) {
            return $null
        }
        $packageRoot = (Resolve-Path -LiteralPath $packageRootCandidate -ErrorAction Stop).Path
        if (-not (Test-CoremailPathChainSafe -Path $packageRoot)) { return $null }
        $packagePath = Join-Path $packageRoot 'package.json'
        if (-not (Test-CoremailPathChainSafe -Path $packagePath)) { return $null }
        $package = Get-Content -LiteralPath $packagePath -Raw -ErrorAction Stop |
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
        if (-not (Test-CoremailPathChainSafe -Path $cliPath)) { return $null }

        $cliItem = Get-Item -LiteralPath $cliPath -Force -ErrorAction Stop
        if (($cliItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $null
        }
        $cliExtension = [IO.Path]::GetExtension($cliPath)
        if ($cliExtension -ieq '.exe') {
            # Current npm releases replace their declared bin/claude.exe stub
            # with the platform-native PE during postinstall. Execute that
            # declared binary directly; passing it to node.exe is incorrect.
            if (-not (Test-CoremailNativeCliExecutable -Path $cliPath)) { return $null }
            return [pscustomobject]@{
                CommandPath = $resolvedCommand
                Executable = $cliPath
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
            $resolvedNode = (Resolve-Path -LiteralPath $nodeCandidate -ErrorAction Stop).Path
            if (-not (Test-CoremailPathChainSafe -Path $resolvedNode)) { continue }
            if ([IO.Path]::GetExtension($resolvedNode) -ine '.exe' -or
                -not (Test-CoremailNativeCliExecutable -Path $resolvedNode)) { continue }
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
        return $null
    }
    return $null
}

function Resolve-ClaudeCodeInvocation {
    [CmdletBinding()]
    param([string]$ExplicitPath = '')

    $candidates = New-Object System.Collections.ArrayList
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
        if (-not (Test-CoremailPathChainSafe -Path $candidate)) { continue }
        try { $resolvedCandidate = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path }
        catch { continue }
        if (-not (Test-CoremailPathChainSafe -Path $resolvedCandidate)) { continue }
        $extension = [IO.Path]::GetExtension($resolvedCandidate)
        if ($extension -ieq '.exe') {
            if (-not (Test-CoremailNativeCliExecutable -Path $resolvedCandidate)) {
                continue
            }
            return [pscustomobject]@{
                CommandPath = $resolvedCandidate
                Executable = $resolvedCandidate
                Prefix = [string[]]@()
                Kind = 'native'
            }
        }
        if ($extension -ieq '.cmd') {
            $npmInvocation = Resolve-NpmClaudeInvocation -CommandPath $resolvedCandidate
            if ($null -ne $npmInvocation) { return $npmInvocation }
        }
    }
    return $null
}
