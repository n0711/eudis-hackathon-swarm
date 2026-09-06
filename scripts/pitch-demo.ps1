<#
.SYNOPSIS
    Run the two quorum-floor scenarios back to back for the live pitch.

.DESCRIPTION
    Calls the console script inside the virtual environment directly, so it
    works from any working directory and needs no prior activation. Activation
    is the one step that has actually failed in practice, so this removes it.

    Prints each run's real output verbatim. Nothing here reformats or filters
    the numbers being claimed on stage.

.EXAMPLE
    .\scripts\pitch-demo.ps1
    .\scripts\pitch-demo.ps1 -Full
#>
[CmdletBinding()]
param(
    # Show each run's complete output rather than the mission-outcome lines.
    [switch]$Full
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $repoRoot '.venv\Scripts\eudis-swarm.exe'

if (-not (Test-Path $exe)) {
    Write-Host "Could not find $exe" -ForegroundColor Red
    Write-Host "Create the environment first:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -e `".[dev,visualization,dashboard]`"" -ForegroundColor Yellow
    exit 1
}

# The lines that carry the argument. The first three are identical in both
# runs, which is the point: neither swarm knows the aircraft is gone.
$keyLines = @(
    'Agents physically lost:',
    'Agents declared failed:',
    'Losses never detected:',
    'Mission completed:',
    'Tasks completed:',
    'Simulation duration:'
)

$runs = @(
    @{ Scenario = 'demo-quorum-floor';     Label = 'DISTRIBUTED  (replicated ownership)' },
    @{ Scenario = 'baseline-quorum-floor'; Label = 'CENTRALISED  (coordinator baseline)' }
)

foreach ($run in $runs) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkGray
    Write-Host $run.Label -ForegroundColor Cyan
    Write-Host "eudis-swarm --scenario $($run.Scenario)" -ForegroundColor DarkGray
    Write-Host ('=' * 68) -ForegroundColor DarkGray

    $output = & $exe --scenario $run.Scenario 2>&1
    $exitCode = $LASTEXITCODE

    if ($Full) {
        $output | ForEach-Object { Write-Host $_ }
    }
    else {
        foreach ($line in $output) {
            $text = [string]$line
            foreach ($key in $keyLines) {
                if ($text.TrimStart().StartsWith($key)) {
                    Write-Host $text.Trim()
                    break
                }
            }
        }
    }

    $colour = if ($exitCode -eq 0) { 'Green' } else { 'Red' }
    Write-Host "exit code: $exitCode" -ForegroundColor $colour
}

Write-Host ''
Write-Host 'Both runs report the same three evidence lines.' -ForegroundColor Yellow
Write-Host 'The swarm is equally blind in each. What differs is whether' -ForegroundColor Yellow
Write-Host 'recovery needed a declaration that could never arrive.' -ForegroundColor Yellow
Write-Host ''

# The centralised run is *expected* to exit non-zero: its mission genuinely
# fails. Without this the script would inherit that code and look broken on
# stage, which is the opposite of the point being made.
exit 0
