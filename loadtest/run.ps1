<#
Runs one pre-registered load-test run and saves its raw output (M18, D-119, D-121).

    .\loadtest\run.ps1 -Run m4-on-r1             # a run from loadtest/runs.json
    .\loadtest\run.ps1 -Run m4-on-r1 -Rerun 1    # only after a defect is logged (D-121)
    .\loadtest\run.ps1 -Run m4-on-r1 -Smoke      # 2 virtual users for 5 s; not a result

The stack must already be up with the load-test override:
    docker compose -f compose.yaml -f loadtest/compose.loadtest.yaml up --build -d --wait

For one run it: recreates `api` and `direct-mock` with the run's Compose files (a fresh
process: closed breakers, the mock's seed sequence from the start, an empty key cache);
makes an API key whose tenant is the run id (not for the direct mock); runs k6 in the
`k6` service with the key in its environment; waits 5 s for the usage writer; saves
docs/results/raw/<id>/: records.jsonl, summary.json, k6.txt (k6's stdout and stderr), api.log, usage.tsv (when the
catalog says so) and run.json. It stops if the key appears in any saved file.

Standby guard (D-121, amended after the first pass, which ran with the laptop in Modern Standby):
it refuses to start while the machine is in Modern Standby, holds a power request that keeps the
display on while it runs, and lists in run.json every Kernel-Power standby event (506 entering,
507 exiting) between the run's start and end; if there is one, it stops with an error after
saving the output, so the run is kept and recorded as disturbed.
#>
param(
    [Parameter(Mandatory = $true)][string]$Run,
    [int]$Rerun = 0,
    [switch]$Smoke
)

$ErrorActionPreference = 'Stop'
$K6Image = 'grafana/k6:2.3.0@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8'

function Invoke-Native {
    # Runs a native command, returns its stdout lines, and stops on a non-zero exit.
    param([string]$Exe, [string[]]$Arguments)
    $output = & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe exited with $LASTEXITCODE"
    }
    return $output
}

function Get-StandbyEvents {
    # Kernel-Power 506 (entering Modern Standby) and 507 (exiting), oldest first.
    param([Nullable[datetime]]$Start, [Nullable[datetime]]$End, [int]$Newest = 0)
    $filter = @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power'; Id = 506, 507 }
    if ($null -ne $Start) { $filter.StartTime = $Start }
    if ($null -ne $End) { $filter.EndTime = $End }
    $query = @{ FilterHashtable = $filter; ErrorAction = 'Stop' }
    if ($Newest -gt 0) { $query.MaxEvents = $Newest }
    try {
        $found = @(Get-WinEvent @query)
    }
    catch {
        # "No events were found" is an answer; any other failure (no access to the log) is not.
        if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { return @() }
        throw
    }
    return @($found | Sort-Object TimeCreated)
}

# SetThreadExecutionState: a power request bound to this thread, held until it is reset.
Add-Type -Namespace LgwLoad -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$ES_CONTINUOUS = [uint32]'0x80000000'
$ES_SYSTEM_REQUIRED = [uint32]'0x00000001'
$ES_DISPLAY_REQUIRED = [uint32]'0x00000002'

function Write-LfFile {
    param([string]$Path, [object[]]$Lines)
    $text = if ($null -eq $Lines -or $Lines.Count -eq 0) { '' } else { ($Lines -join "`n") + "`n" }
    [System.IO.File]::WriteAllText($Path, $text, [System.Text.UTF8Encoding]::new($false))
}

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
$powerHeld = $false
try {
    $catalog = Get-Content -Raw -Path 'loadtest/runs.json' | ConvertFrom-Json
    $entry = $catalog | Where-Object { $_.id -eq $Run }
    if ($null -eq $entry) {
        throw "unknown run id '$Run': see loadtest/runs.json"
    }
    if ($Smoke -and $Rerun -gt 0) {
        throw 'a smoke run is not a rerun'
    }
    $id = $entry.id
    if ($Rerun -gt 0) { $id = "$id-rerun$Rerun" }
    if ($Smoke) { $id = "smoke-$id-$((Get-Date).ToUniversalTime().ToString('HHmmss'))" }
    $out = Join-Path 'docs/results/raw' $id
    if (Test-Path $out) {
        throw "$out exists: every run keeps its output, and a rerun gets its own id (D-121)"
    }

    # Refuse to start in Modern Standby: its latest 506 or 507 event is a 506 (D-121).
    $last = @(Get-StandbyEvents -Newest 1)
    if ($last.Count -eq 1 -and $last[0].Id -eq 506) {
        $since = $last[0].TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        throw "the machine has been in Modern Standby since $since; wake it before a run (D-121)"
    }
    # 0x80000003: keep the system and the display on until the reset in the finally below.
    if ([LgwLoad.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_DISPLAY_REQUIRED) -eq 0) {
        throw 'SetThreadExecutionState refused the power request'
    }
    $powerHeld = $true
    $startedLocal = Get-Date

    $files = @('compose', '-f', 'compose.yaml', '-f', 'loadtest/compose.loadtest.yaml')
    foreach ($override in $entry.overrides) { $files += @('-f', $override) }

    $started = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    Invoke-Native 'docker' ($files + @('up', '-d', '--wait', '--force-recreate', '--no-deps', 'api', 'direct-mock')) | Out-Null

    $keyId = $null
    $key = ''
    if ($entry.target -eq 'api') {
        $created = Invoke-Native 'docker' ($files + @('exec', '-T', 'api', 'python', '-m', 'llmgateway.keys', 'create', '--tenant', $id))
        $key = @($created | Where-Object { $_ -match '^lgw_[0-9a-f]{16}_' })
        if ($key.Count -ne 1) { throw 'the key CLI did not print exactly one key' }
        $key = $key[0].Trim()
        $keyId = $key.Split('_')[1]
    }

    New-Item -ItemType Directory -Path $out | Out-Null
    $target = "http://$($entry.target):8000"
    $alias = if ($null -eq $entry.alias) { '' } else { $entry.alias }
    # --no-usage-report: k6 sends no anonymous usage statistics from these runs.
    $k6Args = @('run', '-q', '--no-usage-report', '--log-format', 'json',
        '--console-output', "/out/$id/records.jsonl",
        '--summary-export', "/out/$id/summary.json")
    if ($Smoke) { $k6Args += @('--vus', '2', '--duration', '5s') }
    $k6Args += "/scripts/$($entry.script)"

    $env:LGW_KEY = $key
    try {
        $runArgs = $files + @('run', '--rm', '--no-deps', '-e', 'LGW_KEY',
            '-e', "TARGET=$target", '-e', "ALIAS=$alias", '-e', "RUN_ID=$id",
            '-e', "MEASUREMENT=$($entry.measurement)", 'k6') + $k6Args
        # stderr too: k6 logs its own warnings there (D-122, amended), and they belong
        # with the run. Each stderr line arrives as an ErrorRecord; "$_" is its text.
        $k6Text = & docker @runArgs 2>&1 | ForEach-Object { "$_" }
        $k6Exit = $LASTEXITCODE
    }
    finally {
        Remove-Item Env:LGW_KEY -ErrorAction SilentlyContinue
    }
    Write-LfFile (Join-Path $out 'k6.txt') $k6Text

    # The usage writer batches off the response path (D-099); give it time to drain.
    Start-Sleep -Seconds 5
    $apiLog = Invoke-Native 'docker' ($files + @('logs', '--no-color', '--no-log-prefix', 'api'))
    Write-LfFile (Join-Path $out 'api.log') $apiLog

    if ($entry.usage) {
        # One parameterized query: the tenant is a bind parameter, never pasted into the SQL.
        $sql = 'SELECT outcome, provider_instance, count(*) FROM usage_records ' +
            'WHERE tenant_id = $1 GROUP BY 1, 2 ORDER BY 1, 2 ' + "\bind '$id' \g"
        $usage = $sql | & docker @($files + @('exec', '-T', 'postgres', 'psql', '-U', 'llmgateway',
                    '-d', 'llmgateway', '-X', '-q', '-A', '-t', '-F', "`t", '-v', 'ON_ERROR_STOP=1', '-f', '-'))
        if ($LASTEXITCODE -ne 0) { throw "psql exited with $LASTEXITCODE" }
        Write-LfFile (Join-Path $out 'usage.tsv') @($usage | Where-Object { $_ -ne '' })
    }

    $endedLocal = Get-Date
    $ended = $endedLocal.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    # Any standby event during the run (D-121); [ordered] keeps the keys in this order in JSON.
    $powerEvents = @(Get-StandbyEvents -Start $startedLocal -End $endedLocal | ForEach-Object {
            [ordered]@{ utc = $_.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ'); id = $_.Id }
        })
    $commit = (Invoke-Native 'git' @('rev-parse', 'HEAD')).Trim()
    $dirty = [bool](Invoke-Native 'git' @('status', '--porcelain', '--', 'src', 'config', 'loadtest', 'compose.yaml', 'Dockerfile'))
    $imageId = (Invoke-Native 'docker' @('image', 'inspect', 'llmgateway:local', '--format', '{{.Id}}')).Trim()
    $command = ".\loadtest\run.ps1 -Run $Run" + $(if ($Rerun -gt 0) { " -Rerun $Rerun" } else { '' }) + $(if ($Smoke) { ' -Smoke' } else { '' })
    $meta = [ordered]@{
        id              = $id
        catalog_id      = $entry.id
        measurement     = $entry.measurement
        smoke           = [bool]$Smoke
        rerun           = $Rerun
        command         = $command
        script          = $entry.script
        target          = $target
        alias           = $entry.alias
        compose_files   = @($files | Where-Object { $_ -ne '-f' -and $_ -ne 'compose' })
        k6_image        = $K6Image
        k6_args         = $k6Args
        k6_exit_code    = $k6Exit
        api_image_id    = $imageId
        git_commit      = $commit
        git_dirty       = $dirty
        key_id          = $keyId
        started_utc     = $started
        ended_utc       = $ended
        power_events    = $powerEvents
    }
    Write-LfFile (Join-Path $out 'run.json') @(($meta | ConvertTo-Json -Depth 4).Replace("`r`n", "`n"))

    if ($key -ne '') {
        foreach ($file in Get-ChildItem -File -Path $out) {
            # An empty file reads as $null.
            $text = [System.IO.File]::ReadAllText($file.FullName)
            if ($text.Contains($key)) {
                throw "the run's key is in $($file.Name): do not commit $out"
            }
        }
    }
    if ($powerEvents.Count -gt 0) {
        $listed = ($powerEvents | ForEach-Object { "$($_.id) at $($_.utc)" }) -join ', '
        throw "standby events during the run ($listed); the output is kept in $out and the run is disturbed (D-121)"
    }
    if ($k6Exit -ne 0) {
        throw "k6 exited with $k6Exit; the output is kept in $out"
    }
    Write-Host "done: $out"
}
finally {
    if ($powerHeld) {
        # Back to ES_CONTINUOUS alone: the power request ends.
        [LgwLoad.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    }
    Pop-Location
}
