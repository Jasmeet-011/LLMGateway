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
        # $? also catches a command that could not start (uv missing), where
        # $LASTEXITCODE is not set.
        if (-not $? -or $LASTEXITCODE -ne 0) {
            $code = if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }
            Write-Host "FAIL: $command (exit code $code)"
            exit $code
        }
    }
}
finally {
    Pop-Location
}

Write-Host 'PASS'
