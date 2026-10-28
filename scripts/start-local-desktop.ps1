param(
    [string]$DataDirectory,
    [switch]$CheckOnly
)

# ASCII source supports Windows PowerShell 5.1 without a BOM.
# This launcher activates a local build; it never submits a model request.
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskDesktopName = (-join @([char]0x53F8, [char]0x5FC6)) + '.exe'
$taskDesktop = Join-Path $taskRoot $taskDesktopName
$taskActivationPath = Join-Path $taskRoot '.siyi-desktop.local.json'

function Assert-UnlinkedPath([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw ('Linked/reparse paths are not accepted: ' + $cursor)
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Read-LocalJson([string]$Path) {
    Assert-UnlinkedPath $Path
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-NewLocalText([string]$Path, [string]$Text) {
    Assert-UnlinkedPath $Path
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
}

Assert-UnlinkedPath $taskRoot
if (-not (Test-Path -LiteralPath $taskActivationPath)) {
    throw 'Local desktop is not activated. Build a complete runtime, then run scripts/activate-local-desktop.ps1. No fallback to an old executable is allowed.'
}
$taskActivation = Read-LocalJson $taskActivationPath
if ($taskActivation.schema_version -ne 1 -or $taskActivation.product_version -notmatch '^16\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$' -or $taskActivation.runtime_commit -notmatch '^[a-fA-F0-9]{40}$' -or $taskActivation.data_owner -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') {
    throw 'Invalid local activation identity.'
}
$taskSourceVersion = (Get-Content -LiteralPath (Join-Path $taskRoot 'VERSION') -Raw -Encoding UTF8).Trim()
if ($taskSourceVersion -cne $taskActivation.product_version) {
    throw 'Source VERSION differs from the activated runtime. Build and activate the intended version first.'
}
$taskSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($entry in @($taskActivation.files)) {
    $relative = ([string]$entry.path).Replace('/', '\')
    if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|\\)\.\.?($|\\)' -or $relative.Contains(':') -or -not ($relative -ieq $taskDesktopName -or $relative -ieq 'agent-backend.exe' -or $relative.StartsWith('_internal\', [StringComparison]::OrdinalIgnoreCase))) {
        throw 'Activation inventory contains an invalid runtime path.'
    }
    if (-not $taskSeen.Add($relative) -or $entry.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or [long]$entry.bytes -lt 0) {
        throw 'Activation inventory contains a duplicate or invalid file identity.'
    }
    $absolute = [IO.Path]::GetFullPath((Join-Path $taskRoot $relative))
    if (-not $absolute.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Activation inventory escaped the application directory.'
    }
    Assert-UnlinkedPath $absolute
    if ((Get-Item -LiteralPath $absolute -Force).Length -ne [long]$entry.bytes -or (Get-FileHash -LiteralPath $absolute -Algorithm SHA256).Hash -ine $entry.sha256) {
        throw ('Activated runtime file changed: ' + $relative + '. Rebuild and activate the complete runtime, not one component.')
    }
}
foreach ($required in @($taskDesktopName, 'agent-backend.exe', '_internal\build-info.json')) {
    if (-not $taskSeen.Contains($required)) { throw ('Activation inventory lacks required file: ' + $required) }
}
Assert-UnlinkedPath (Join-Path $taskRoot '_internal')
foreach ($directory in @(Get-ChildItem -LiteralPath (Join-Path $taskRoot '_internal') -Directory -Recurse -Force)) {
    Assert-UnlinkedPath $directory.FullName
}
if ((@(Get-ChildItem -LiteralPath (Join-Path $taskRoot '_internal') -File -Recurse -Force).Count + 2) -ne $taskSeen.Count) {
    throw 'The runtime contains unrecorded files. Activate a clean, complete build.'
}
$taskBuild = Read-LocalJson (Join-Path $taskRoot '_internal\build-info.json')
if ($taskBuild.product_version -cne $taskActivation.product_version -or $taskBuild.git_commit -ine $taskActivation.runtime_commit) {
    throw 'Embedded build identity differs from the activation manifest.'
}

if (-not $DataDirectory) {
    $taskDataName = -join @([char]0x53F8, [char]0x5FC6, [char]0x6570, [char]0x636E)
    $DataDirectory = Join-Path (Join-Path (Split-Path -Parent $taskRoot) $taskDataName) 'v16.0.0'
}
$taskData = [IO.Path]::GetFullPath($DataDirectory)
$taskDataParent = [IO.Path]::GetDirectoryName($taskData)
if (-not $taskDataParent -or $taskData -ieq [IO.Path]::GetPathRoot($taskData) -or $taskData -ieq $taskRoot -or $taskData.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $taskRoot.StartsWith($taskData + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use a dedicated data directory outside the application checkout.'
}
Assert-UnlinkedPath $taskData
$taskOwnerPath = Join-Path $taskData 'local-profile-owner.json'
$taskProviderPath = Join-Path $taskData 'config\provider-settings.json'
$taskEnvPath = Join-Path $taskData 'config\desktop.env'
$taskFresh = -not (Test-Path -LiteralPath $taskData)
if (-not $taskFresh) {
    if (-not (Test-Path -LiteralPath $taskOwnerPath)) { throw 'Existing data directory has no local v16 owner. Refusing to adopt, migrate or overwrite it.' }
    $taskOwner = Read-LocalJson $taskOwnerPath
    if ($taskOwner.schema_version -ne 1 -or $taskOwner.owner -cne $taskActivation.data_owner -or $taskOwner.data_root -ine $taskData) {
        throw 'Existing data directory owner differs. No data was changed.'
    }
    Assert-UnlinkedPath $taskProviderPath
    Assert-UnlinkedPath $taskEnvPath
    if (-not (Test-Path -LiteralPath $taskProviderPath) -or -not (Test-Path -LiteralPath $taskEnvPath)) {
        throw 'Owned profile configuration is incomplete. Refusing a silent fallback to another configuration.'
    }
    $taskProvider = Read-LocalJson $taskProviderPath
    if ($taskProvider.provider_id -notin @('deepseek','ollama','openai_compatible','mock')) { throw 'Invalid saved Provider. Nothing was launched.' }
}
$taskFolders = @('data','logs','backups','state','artifacts','user-assets','extensions','workspaces','kokoro','voice\models','home','home\Desktop','home\AppData\Local','home\AppData\Roaming','temp','cache','cache\huggingface','cache\torch','webview2','launch-records','config')
foreach ($folder in $taskFolders) { Assert-UnlinkedPath (Join-Path $taskData $folder) }

# The product mutex is shared by older SiYi versions. Never terminate them.
$taskRunning = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @($taskDesktopName, 'Agent.exe', 'agent-backend.exe') })
$taskOwnDesktop = @($taskRunning | Where-Object { $_.Name -eq $taskDesktopName -and $_.ExecutablePath -ieq $taskDesktop })
$taskForeign = @($taskRunning | Where-Object {
    -not ($_.Name -eq $taskDesktopName -and $_.ExecutablePath -ieq $taskDesktop) -and
    -not ($taskOwnDesktop.Count -eq 1 -and $_.Name -eq 'agent-backend.exe' -and $_.ExecutablePath -ieq (Join-Path $taskRoot 'agent-backend.exe') -and $_.ParentProcessId -eq $taskOwnDesktop[0].ProcessId)
})
if ($taskOwnDesktop.Count -eq 1 -and $taskForeign.Count -eq 0) {
    # A bare EXE can use legacy defaults. Same executable path is not proof of
    # the independent profile: require our PID + creation-time launch receipt.
    $taskOwnProfileRunning = $false
    $taskLaunchRecords = Join-Path $taskData 'launch-records'
    if (-not $taskFresh -and (Test-Path -LiteralPath $taskLaunchRecords)) {
        foreach ($taskRecordFile in @(Get-ChildItem -LiteralPath $taskLaunchRecords -File -Filter 'launch-*.json' | Sort-Object LastWriteTime -Descending | Select-Object -First 100)) {
            try {
                $taskRecord = Read-LocalJson $taskRecordFile.FullName
                $taskRecordedStart = [DateTimeOffset]::Parse($taskRecord.desktop_start_time_utc)
                $taskCurrentStart = [DateTimeOffset]::new($taskOwnDesktop[0].CreationDate.ToUniversalTime())
                if ($taskRecord.desktop_pid -eq $taskOwnDesktop[0].ProcessId -and $taskRecord.data_owner -ceq $taskActivation.data_owner -and $taskRecord.data_root -ieq $taskData -and $taskRecord.executable -ieq $taskDesktop -and $taskRecord.compiled_runtime_commit -ieq $taskActivation.runtime_commit -and [Math]::Abs(($taskRecordedStart - $taskCurrentStart).TotalMilliseconds) -le 1) {
                    $taskOwnProfileRunning = $true
                    break
                }
            } catch {
                # Invalid/unrelated records cannot establish profile ownership.
            }
        }
    }
    if ($taskOwnProfileRunning) {
        Write-Host 'The activated local desktop is already running with this owned profile. No process was stopped and no new task was sent.'
        exit 0
    }
    throw 'An executable at this path is running without a matching owned-profile launch receipt. Exit it normally, then launch through this script. No process was stopped.'
}
if ($taskRunning.Count -gt 0) { throw 'Another SiYi/Agent instance is running. Save work and exit it normally before launching this desktop. No process was stopped.' }
try {
    $taskProductMutex = [Threading.Mutex]::OpenExisting('io.github.aureliuswu.agent-sim')
    $taskProductMutex.Dispose()
    throw 'Another SiYi product mutex exists. Exit that application normally first.'
} catch [Threading.WaitHandleCannotBeOpenedException] {
    # Missing is expected. Never create or acquire the product mutex here.
}
if ($CheckOnly) {
    Write-Host ('READY: ' + $taskSeen.Count + ' activated runtime files, v' + $taskActivation.product_version + ', source ' + $taskActivation.runtime_commit + '.')
    Write-Host ('Profile: ' + $(if ($taskFresh) { 'will create a fresh owned directory' } else { 'existing owned directory verified' }) + '. CheckOnly created no files and launched no process.')
    exit 0
}

if ($taskFresh) {
    # Initialize secret-free profile metadata atomically, before application start.
    # Both exact paths are verified within the intended dedicated data parent.
    Assert-UnlinkedPath $taskDataParent
    if (-not (Test-Path -LiteralPath $taskDataParent)) { [void](New-Item -ItemType Directory -Path $taskDataParent) }
    $taskStage = $taskData + '.initializing-' + [Guid]::NewGuid().ToString('N')
    if (-not $taskStage.StartsWith($taskData + '.initializing-', [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetDirectoryName($taskStage) -ine $taskDataParent) { throw 'Invalid profile staging path.' }
    Assert-UnlinkedPath $taskStage
    [void](New-Item -ItemType Directory -Path $taskStage)
    foreach ($folder in $taskFolders) { [void](New-Item -ItemType Directory -Path (Join-Path $taskStage $folder) -Force) }
    $taskOwner = [ordered]@{ schema_version=1; owner=$taskActivation.data_owner; data_root=$taskData; created_at_utc=[DateTime]::UtcNow.ToString('o'); initialized_runtime_commit=$taskActivation.runtime_commit }
    Write-NewLocalText (Join-Path $taskStage 'local-profile-owner.json') ($taskOwner | ConvertTo-Json)
    $taskProvider = [ordered]@{provider_id='deepseek';base_url='https://api.deepseek.com';model='deepseek-flash';timeout_seconds=90;max_tokens=8192;max_retries=0;allow_tools=$true;allow_streaming=$true}
    Write-NewLocalText (Join-Path $taskStage 'config\provider-settings.json') ($taskProvider | ConvertTo-Json)
    Write-NewLocalText (Join-Path $taskStage 'config\desktop.env') '# No credentials are copied here. This profile starts independently of older data.'
    Assert-UnlinkedPath $taskStage
    Assert-UnlinkedPath $taskData
    [IO.Directory]::Move($taskStage, $taskData)
} else {
    foreach ($folder in $taskFolders) {
        $path = Join-Path $taskData $folder
        if (-not (Test-Path -LiteralPath $path)) { [void](New-Item -ItemType Directory -Path $path) }
    }
}

# Only this new child's environment is changed. Never write user/system settings.
$taskStart = [Diagnostics.ProcessStartInfo]::new()
$taskStart.FileName = $taskDesktop
$taskStart.WorkingDirectory = $taskRoot
$taskStart.UseShellExecute = $false
$taskStart.EnvironmentVariables.Clear()
foreach ($name in @('SystemRoot','WINDIR','COMSPEC','PATHEXT','PATH','PROGRAMDATA','ALLUSERSPROFILE','PROGRAMFILES','PROGRAMFILES(X86)','COMMONPROGRAMFILES','COMMONPROGRAMFILES(X86)','USERNAME','USERDOMAIN','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE','PROCESSOR_IDENTIFIER','OS','SESSIONNAME','PSModulePath')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($value) { $taskStart.EnvironmentVariables[$name] = $value }
}
$taskEnv = @{
    'AGENT_DATA_ROOT'=$taskData
    'AGENT_DESKTOP_DATA_DIRECTORY'=$taskData
    'AGENT_DATABASE_PATH'=(Join-Path $taskData 'data\agent.db')
    'AGENT_LOG_PATH'=(Join-Path $taskData 'logs\agent.log')
    'AGENT_EXTENSION_DIRECTORY'=(Join-Path $taskData 'extensions')
    'AGENT_ENV_FILE'=$taskEnvPath
    'AGENT_PROVIDER_CONFIG_PATH'=$taskProviderPath
    'AGENT_DEPLOYMENT_MODE'='desktop_local'
    'AGENT_RUNTIME_ENV'='production'
    'AGENT_BIND_HOST'='127.0.0.1'
    'AGENT_MODEL_NAME'='deepseek-flash'
    'AGENT_MODEL_LIGHT_NAME'='deepseek-flash'
    'AGENT_MODEL_MEDIUM_NAME'='deepseek-flash'
    'AGENT_MODEL_STRONG_NAME'='deepseek-flash'
    'AGENT_MODEL_MAX_RETRIES'='0'
    'AGENT_MODEL_ESCALATION_ENABLED'='false'
    'AGENT_NETWORK_MAX_REDIRECTS'='0'
    'HOME'=(Join-Path $taskData 'home')
    'USERPROFILE'=(Join-Path $taskData 'home')
    'APPDATA'=(Join-Path $taskData 'home\AppData\Roaming')
    'LOCALAPPDATA'=(Join-Path $taskData 'home\AppData\Local')
    'TEMP'=(Join-Path $taskData 'temp')
    'TMP'=(Join-Path $taskData 'temp')
    'XDG_CACHE_HOME'=(Join-Path $taskData 'cache')
    'HF_HOME'=(Join-Path $taskData 'cache\huggingface')
    'TORCH_HOME'=(Join-Path $taskData 'cache\torch')
    'WEBVIEW2_USER_DATA_FOLDER'=(Join-Path $taskData 'webview2')
    'PYTHONDONTWRITEBYTECODE'='1'
}
foreach ($entry in $taskEnv.GetEnumerator()) { $taskStart.EnvironmentVariables[$entry.Key] = $entry.Value }
$taskChild = [Diagnostics.Process]::Start($taskStart)
$taskReceipt = [ordered]@{
    status='USER_TEST_BASELINE_LAUNCHED_NOT_FORMAL_ACCEPTANCE'
    product_version=$taskActivation.product_version
    compiled_runtime_commit=$taskActivation.runtime_commit
    data_owner=$taskActivation.data_owner
    desktop_pid=$taskChild.Id
    desktop_start_time_utc=$taskChild.StartTime.ToUniversalTime().ToString('o')
    launched_at_utc=[DateTime]::UtcNow.ToString('o')
    executable=$taskDesktop
    data_root=$taskData
    provider_id=$taskProvider.provider_id
    paid_validation_performed=$false
}
$taskReceiptPath = Join-Path $taskData ('launch-records\launch-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.json')
Write-NewLocalText $taskReceiptPath ($taskReceipt | ConvertTo-Json)
Write-Host ('SiYi v' + $taskActivation.product_version + ' USER TEST BASELINE / source ' + $taskActivation.runtime_commit.Substring(0,7))
Write-Host ('Started desktop PID ' + $taskChild.Id + '; independent data: ' + $taskData)
Write-Host 'No model test was submitted by the launcher. Your own cloud tasks or resumed app work may incur provider charges.'
Write-Host 'Windows SiYi credentials remain shared with this Windows account; this launcher does not read, copy or modify them.'
