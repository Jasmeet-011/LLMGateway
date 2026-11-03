# Runs the five checks from CLAUDE.md in order, from the repo root.
# Stops at the first failure with that command's non-zero exit code; prints PASS
# only when every check passed.

$steps = @(
    @('uv', 'sync', '--locked'),
    @('uv', 'run', 'pytest'),
    @('uv', 'run', 'ruff', 'check'),
    @('uv', 'run', 'ruff', 'format', '--check'),
    @('uv', 'run', 'mypy')
)

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    foreach ($step in $steps) {
        $command = $step -join ' '
        Write-Host "==> $command"
        & $step[0] $step[1..($step.Count - 1)]
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAIL: $command (exit code $LASTEXITCODE)"
            exit $LASTEXITCODE
        }
    }
}
finally {
    Pop-Location
}

Write-Host 'PASS'
