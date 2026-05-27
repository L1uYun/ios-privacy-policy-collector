param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [string]$WaveName = "stage1-live-high-yield-20260525",
    [string]$BatchName = "stage1-live-expanded-policy",
    [string]$Sources = "apple-search:game,apple-search:travel,apple-search:music,apple-search:video,apple-search:shopping,apple-search:sports,apple-search:food,apple-search:weather,apple-search:fitness,apple-search:education,apple-search:kids,apple-search:news,apple-search:social,apple-search:productivity",
    [int]$Workers = 8,
    [int]$LimitPerWorker = 300,
    [int]$PollSeconds = 60,
    [int]$MaxWaitMinutes = 240
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$waveDir = Join-Path (Join-Path $DataRoot "seed-waves") $WaveName
$activeSummary = Join-Path $waveDir "$WaveName.active-seeds.summary.json"
$watchLog = Join-Path $waveDir "$WaveName.policy-watcher.events.jsonl"
$deadline = (Get-Date).AddMinutes($MaxWaitMinutes)

function Write-Event($event, $payload = @{}) {
    $row = @{ event = $event; created_at = (Get-Date).ToUniversalTime().ToString("o") }
    foreach ($key in $payload.Keys) {
        $row[$key] = $payload[$key]
    }
    $row | ConvertTo-Json -Compress | Add-Content -Encoding utf8 $watchLog
}

Write-Event "wait_start" @{ active_summary = $activeSummary; sources = $Sources }
while ((Get-Date) -lt $deadline) {
    if (Test-Path $activeSummary) {
        $summary = Get-Content $activeSummary -Raw | ConvertFrom-Json
        Write-Event "seed_wave_ready" @{ rows = $summary.rows; unique_app_ids = $summary.unique_app_ids; output_csv = $summary.output_csv }
        $batch = powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo "scripts\start_policy_cluster_batch.ps1") `
            -Repo $Repo `
            -DataRoot $DataRoot `
            -Name $BatchName `
            -Workers $Workers `
            -LimitPerWorker $LimitPerWorker `
            -Countries "" `
            -Sources $Sources
        Write-Event "policy_batch_started" @{ batch = $batch }
        exit 0
    }
    Write-Event "waiting" @{ active_summary_exists = $false }
    Start-Sleep -Seconds $PollSeconds
}

Write-Event "timeout" @{ max_wait_minutes = $MaxWaitMinutes }
exit 1
