param(
    [int]$TimeoutMilliseconds = 15000,
    [switch]$CheckConnection,
    [switch]$IgnoreAccountConfiguration,
    [string]$PythonExecutable = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ($CheckConnection -and $IgnoreAccountConfiguration) {
    throw 'CheckConnection and IgnoreAccountConfiguration cannot be used together.'
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The launcher smoke test must run on Windows.'
}

$mcpRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\mcp'))
$runnerPath = Join-Path $mcpRoot 'run-server.ps1'
$serverPath = Join-Path $mcpRoot 'server.py'
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
        throw "MCP launcher not found: $runnerPath"
    }
}
else {
    $PythonExecutable = [IO.Path]::GetFullPath($PythonExecutable)
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "Explicit smoke-test Python executable not found: $PythonExecutable"
    }
    if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
        throw "MCP server not found: $serverPath"
    }
}

function ConvertTo-RequestJson {
    param([object]$Value)
    return ($Value | ConvertTo-Json -Depth 24 -Compress)
}

function Get-OptionalJsonProperty {
    param(
        [object]$Value,
        [string]$Name
    )
    if ($null -eq $Value) { return $null }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Read-ServerResponse {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$Timeout
    )
    $task = $Process.StandardOutput.ReadLineAsync()
    if (-not $task.Wait($Timeout)) {
        throw "Timed out after $Timeout ms waiting for an MCP response."
    }
    $line = $task.Result
    if ([string]::IsNullOrWhiteSpace($line)) {
        $stderr = $Process.StandardError.ReadToEnd()
        throw "MCP server returned no JSON response. stderr: $stderr"
    }
    return ($line | ConvertFrom-Json)
}

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $startInfo.FileName = 'powershell.exe'
    $escapedRunnerPath = $runnerPath.Replace('"', '\"')
    $startInfo.Arguments = "-NoLogo -NoProfile -NonInteractive -File `"$escapedRunnerPath`""
}
else {
    $startInfo.FileName = $PythonExecutable
    $escapedServerPath = $serverPath.Replace('"', '\"')
    $startInfo.Arguments = "-B -I `"$escapedServerPath`""
}
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
$startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
if ($IgnoreAccountConfiguration) {
    $isolatedProfile = Join-Path (
        [IO.Path]::GetTempPath()
    ) ('mail-smoke-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $isolatedProfile -Force | Out-Null
    $startInfo.EnvironmentVariables['USERPROFILE'] = $isolatedProfile
    $startInfo.EnvironmentVariables['CLAUDE_PROJECT_DIR'] = $isolatedProfile
}

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo

try {
    if (-not $process.Start()) { throw 'Failed to start the MCP launcher.' }

    $process.StandardInput.WriteLine((ConvertTo-RequestJson ([ordered]@{
        jsonrpc = '2.0'
        id = 1
        method = 'initialize'
        params = [ordered]@{
            protocolVersion = '2024-11-05'
            capabilities = [ordered]@{}
            clientInfo = [ordered]@{ name = 'mail-smoke-test'; version = '0.9.0' }
        }
    })))
    $process.StandardInput.Flush()
    $initialize = Read-ServerResponse -Process $process -Timeout $TimeoutMilliseconds
    $initializeId = Get-OptionalJsonProperty -Value $initialize -Name 'id'
    $initializeResult = Get-OptionalJsonProperty -Value $initialize -Name 'result'
    $serverInfo = Get-OptionalJsonProperty -Value $initializeResult -Name 'serverInfo'
    $initializeServerName = Get-OptionalJsonProperty -Value $serverInfo -Name 'name'
    if ($initializeId -ne 1 -or $initializeServerName -ne 'mail-mcp-server') {
        $responseId = if ($null -ne $initializeId) { [string]$initializeId } else { '<missing>' }
        $serverName = if ($null -ne $initializeServerName) { [string]$initializeServerName } else { '<missing>' }
        $initializeError = Get-OptionalJsonProperty -Value $initialize -Name 'error'
        $initializeErrorCode = Get-OptionalJsonProperty -Value $initializeError -Name 'code'
        $initializeErrorMessage = Get-OptionalJsonProperty -Value $initializeError -Name 'message'
        $errorCode = if ($null -ne $initializeErrorCode) { [string]$initializeErrorCode } else { '<none>' }
        $errorMessage = if ($null -ne $initializeErrorMessage) { [string]$initializeErrorMessage } else { '<none>' }
        throw "Unexpected initialize response: id=$responseId server=$serverName error=$errorCode message=$errorMessage"
    }

    $process.StandardInput.WriteLine((ConvertTo-RequestJson ([ordered]@{
        jsonrpc = '2.0'
        method = 'notifications/initialized'
        params = [ordered]@{}
    })))
    $process.StandardInput.WriteLine((ConvertTo-RequestJson ([ordered]@{
        jsonrpc = '2.0'
        id = 2
        method = 'tools/list'
        params = [ordered]@{}
    })))
    $process.StandardInput.Flush()
    $toolList = Read-ServerResponse -Process $process -Timeout $TimeoutMilliseconds
    $toolNames = @($toolList.result.tools | ForEach-Object { $_.name })
    $expectedTools = @(
        'mail_config_status',
        'mail_configure',
        'mail_config_reload',
        'mail_connection_status',
        'mail_discover_local',
        'mail_check_connection',
        'mail_list_folders',
        'mail_search',
        'mail_get_message',
        'mail_set_seen',
        'mail_set_flags',
        'mail_copy_message',
        'mail_move_message',
        'mail_delete_message',
        'mail_manage_folder',
        'mail_get_raw_message',
        'mail_download_attachment',
        'mail_update_draft',
        'mail_watch_folder',
        'mail_prepare_message',
        'mail_save_draft',
        'mail_send_prepared'
    )
    foreach ($toolName in $expectedTools) {
        if ($toolName -notin $toolNames) { throw "Missing MCP tool: $toolName" }
    }

    $process.StandardInput.WriteLine((ConvertTo-RequestJson ([ordered]@{
        jsonrpc = '2.0'
        id = 3
        method = 'tools/call'
        params = [ordered]@{
            name = 'mail_connection_status'
            arguments = [ordered]@{}
        }
    })))
    $process.StandardInput.Flush()
    $status = Read-ServerResponse -Process $process -Timeout $TimeoutMilliseconds
    if ($status.id -ne 3 -or $status.result.isError) {
        throw 'The offline connection-status tool failed.'
    }

    if ($CheckConnection) {
        $process.StandardInput.WriteLine((ConvertTo-RequestJson ([ordered]@{
            jsonrpc = '2.0'
            id = 4
            method = 'tools/call'
            params = [ordered]@{
                name = 'mail_check_connection'
                arguments = [ordered]@{}
            }
        })))
        $process.StandardInput.Flush()
        $connection = Read-ServerResponse -Process $process -Timeout $TimeoutMilliseconds
        if ($connection.id -ne 4) {
            throw 'The live connection check returned an unexpected response.'
        }
        if ($connection.result.isError) {
            $detail = [string]$connection.result.content[0].text
            throw "The live mail connection check failed: $detail"
        }
        Write-Host 'Live mail transport check passed.'
    }

    Write-Host "Mail MCP smoke test passed. Tools: $($toolNames.Count)"
}
finally {
    try { $process.StandardInput.Close() } catch { }
    if (-not $process.HasExited) {
        if (-not $process.WaitForExit(3000)) { $process.Kill() }
    }
    $process.Dispose()
}
