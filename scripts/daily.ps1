# Daily job: pull whatever the source published, then drop anything past the freshness
# window. Windows equivalent of scripts/daily.sh.
#
#     .\scripts\daily.ps1              primary run: ingest + retention
#     .\scripts\daily.ps1 -CatchUp     ingest only, and only if today's file is missing
#
# Set INGEST_MAX_FILES to change how many of the newest pending files one run pulls.
# Default 3, the same as scripts/daily.sh -- keep the two in step.
#
# The source publishes each day's file between roughly 06:30 and 14:15 UTC and the hour
# varies, so one fixed morning slot lands before the file exists on a fair share of days.
# -CatchUp is the afternoon re-check that closes the gap within the same day; it asks one
# indexed question of the database and stops there once the day's file is in.
#
# Install as a scheduled task (run once, from an elevated PowerShell):
#     .\scripts\install-daily-task.ps1
#
# Every step is idempotent: a re-run after a failure re-processes cleanly rather than
# duplicating, so a missed day self-heals on the next run.

[CmdletBinding()]
param(
    # Ingest only, and only when today's file has not already landed. Retention is a
    # once-a-day decision and stays with the primary run: repeating it could not remove
    # anything the first pass had not already removed.
    [switch]$CatchUp
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("daily-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Write-Log($message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

$label = if ($CatchUp) { "catch-up" } else { "daily" }
Write-Log "=== $label run starting ==="

# Load configuration from .env.development. The scheduler starts with a bare environment,
# so nothing can be assumed to be inherited from an interactive shell.
#
# .env.local is then layered on top when present, for values that are true of this host
# and not of the repo -- chiefly DATABASE_URL, which names the compose hostname
# `postgres` and is therefore unreachable from a native (non-Docker) run. It is
# gitignored, so host specifics never reach the repo.
function Import-EnvFile($path) {
    Get-Content $path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { return }
        $name, $value = $line.Split("=", 2)
        # Strip trailing inline comments. Whitespace before the '#' is required, so a '#'
        # inside a secret survives. Without this, ENVIRONMENT arrived as
        # "development           # development | test | production" and Settings()
        # failed a literal_error before the run did anything at all.
        $value = ($value -replace '\s+#.*$', '').Trim()
        [Environment]::SetEnvironmentVariable($name.Trim(), $value, "Process")
    }
}

$envFile = Join-Path $repo ".env.development"
if (-not (Test-Path $envFile)) {
    Write-Log "FATAL: .env.development not found - copy .env.example and fill it in"
    exit 1
}
Import-EnvFile $envFile

$localEnv = Join-Path $repo ".env.local"
if (Test-Path $localEnv) {
    Import-EnvFile $localEnv
    Write-Log "applied host overrides from .env.local"
}

# Fail loudly here rather than several minutes into a download.
& python -c "from jobplatform_shared import get_settings; get_settings()"
if ($LASTEXITCODE -ne 0) {
    Write-Log "FATAL: configuration did not validate - see the error above"
    exit 1
}

# Run a native command, returning its exit code.
#
# stderr must not be a terminating error here. Under $ErrorActionPreference = "Stop",
# PowerShell 5.1 wraps native stderr in NativeCommandError and throws on the first line,
# which would skip the retry logic below for exactly the crashes it is meant to survive.
function Invoke-Step {
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Out-Null matters: Tee-Object passes its input down the pipeline, and anything
        # left on the pipeline becomes part of this function's return value. Without it
        # the caller gets an array of output lines with the exit code appended, and
        # `$code -eq 0` silently turns into an array filter instead of a comparison.
        # The output is not lost -- Tee-Object has already written it to $log.
        & python @Arguments 2>&1 | Tee-Object -FilePath $log -Append | Out-Null
        # Read it before anything else runs: the next command overwrites $LASTEXITCODE.
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

try {
    # 0. Catch-up guard. Exit 0 means the day is already done, and re-checking the remote
    #    listing to learn that would cost a network round trip and a sync_runs row per
    #    pass.
    #
    #    Exit 2 -- "could not tell" -- deliberately falls through to the ingest. An
    #    unknown answer must never turn into a skipped day, and the ingest is idempotent,
    #    so the worst case of guessing wrong here is one wasted directory listing.
    if ($CatchUp) {
        $code = Invoke-Step @("scripts/have_todays_file.py")
        if ($code -eq 0) {
            Write-Log "today's file is already in - nothing to do"
            Write-Log "=== $label run finished (skipped) ==="
            exit 0
        }
        if ($code -eq 2) {
            Write-Log "could not confirm today's file - ingesting anyway"
        }
    }

    # 1. Pull new jobs. discover() diffs the remote listing against our checkpoints, so a
    #    skipped or late-published day is picked up automatically rather than missed.
    # The source resets connections intermittently (~1 request in 8 from this host), and a
    # single reset anywhere in a multi-file run kills the whole run. Retrying resumes
    # rather than redoing: sync_files checkpoints each completed file, and the pipeline
    # reclaims the stale RUNNING row a killed process leaves behind.

    # Bounded to the newest few files. discover() returns every delta file the source has
    # ever published -- 82 of them -- and the pipeline walks them oldest first, so an
    # unbounded run begins on a file from months back. That file is both the most
    # expensive to process and the least useful: PURGE_AFTER_DAYS deletes anything posted
    # more than a fortnight ago, so its rows are removed the same night they are inserted.
    # On a small host it is also big enough to exhaust memory, and an OOM-killed worker
    # never finalises its sync_runs row -- the abandoned row then holds the per-source
    # lock for the full 120-minute reclaim window and blocks every later run with it.
    #
    # This was left unbounded when scripts/daily.sh was bounded, and the scheduled task
    # duly pulled all 77 pending files in a single 1h38m run. Two runners for one job is
    # already a hazard; two runners that disagree about scope is the bug that follows.
    $maxFiles = if ($env:INGEST_MAX_FILES) { $env:INGEST_MAX_FILES } else { "3" }

    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Write-Log "ingesting (attempt $attempt of $maxAttempts)..."
        $code = Invoke-Step @("scripts/ingest.py", "--trigger", "SCHEDULED", "--max-files", $maxFiles)
        if ($code -eq 0) { break }
        if ($code -eq 3) {
            # Another ingest holds the lock. Retrying is pointless -- the reclaim guard is
            # 120 minutes -- and this run has done no harm, so report and stop cleanly.
            Write-Log "another ingest is already running - nothing to do"
            Write-Log "=== $label run finished (skipped) ==="
            exit 0
        }
        if ($attempt -eq $maxAttempts) {
            throw "ingest failed after $maxAttempts attempts (last exit code $code)"
        }
        $wait = 60 * $attempt
        Write-Log "ingest failed (exit $code) - retrying in $wait s"
        Start-Sleep -Seconds $wait
    }

    # Retention is a once-a-day decision and stays with the primary run.
    if ($CatchUp) {
        Write-Log "=== $label run finished OK ==="
        exit 0
    }

    # 2. Drop jobs past the freshness window and trim old rejection records.
    #
    # Exit 2 means retention is switched off (RETENTION_MAX_POSTED_AGE_DAYS=0), which is
    # the shipped default and a legitimate choice -- this platform's rule is that jobs are
    # never deleted, only transitioned. Treating it as fatal would fail every run *after*
    # a perfectly good ingest, so it is reported and stepped over.
    Write-Log "enforcing retention..."
    $code = Invoke-Step @("scripts/enforce_retention.py", "--execute", "--yes")
    if ($code -eq 2) {
        Write-Log "retention disabled (RETENTION_MAX_POSTED_AGE_DAYS=0) - skipped"
    }
    elseif ($code -ne 0) {
        throw "retention failed with exit code $code"
    }

    Write-Log "=== $label run finished OK ==="
    exit 0
}
catch {
    Write-Log "=== $label run FAILED: $_ ==="
    exit 1
}
