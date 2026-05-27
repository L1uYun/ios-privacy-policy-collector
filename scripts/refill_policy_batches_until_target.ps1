param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [string]$Name = "stage1-refill",
    [int]$TargetPolicyDocuments = 10000,
    [int]$MinRunningFetches = 16,
    [int]$Workers = 8,
    [int]$LimitPerWorker = 300,
    [int]$PollSeconds = 120,
    [int]$MaxBatches = 40,
    [string]$Sources = "apple-search:game,apple-search:travel,apple-search:music,apple-search:video,apple-search:shopping,apple-search:sports,apple-search:food,apple-search:weather,apple-search:fitness,apple-search:education,apple-search:kids,apple-search:news,apple-search:social,apple-search:productivity",
    [string]$Proxy = "http://127.0.0.1:7890"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$eventDir = Join-Path $DataRoot "stage-monitor"
New-Item -ItemType Directory -Force -Path $eventDir | Out-Null
$eventLog = Join-Path $eventDir "$Name.events.jsonl"

function Write-Event($event, $payload = @{}) {
    $row = @{ event = $event; created_at = (Get-Date).ToUniversalTime().ToString("o") }
    foreach ($key in $payload.Keys) {
        $row[$key] = $payload[$key]
    }
    $row | ConvertTo-Json -Compress | Add-Content -Encoding utf8 $eventLog
}

function Get-QueueStats {
    $json = python (Join-Path $Repo "scripts\queue_store.py") --db (Join-Path $DataRoot "queue.sqlite") stats
    return $json | ConvertFrom-Json
}

$startedBatches = 0
Write-Event "start" @{
    target_policy_documents = $TargetPolicyDocuments
    min_running_fetches = $MinRunningFetches
    workers = $Workers
    limit_per_worker = $LimitPerWorker
    sources = $Sources
}

while ($true) {
    $stats = Get-QueueStats
    Write-Event "poll" @{
        policy_documents = $stats.policy_documents
        running_fetches = $stats.running_fetches
        pending_fetches = $stats.pending_fetches
        permanent_error_fetches = $stats.permanent_error_fetches
        started_batches = $startedBatches
    }

    if ([int]$stats.policy_documents -ge $TargetPolicyDocuments) {
        Write-Event "target_reached" @{
            policy_documents = $stats.policy_documents
            started_batches = $startedBatches
        }
        exit 0
    }

    if ($startedBatches -ge $MaxBatches) {
        Write-Event "max_batches_reached" @{
            max_batches = $MaxBatches
            policy_documents = $stats.policy_documents
            running_fetches = $stats.running_fetches
        }
        exit 1
    }

    if ([int]$stats.running_fetches -lt $MinRunningFetches) {
        $batchName = "$Name-$($startedBatches + 1)"
        Write-Event "starting_batch" @{
            batch_name = $batchName
            policy_documents = $stats.policy_documents
            running_fetches = $stats.running_fetches
        }
        $batchArgs = @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            (Join-Path $Repo "scripts\start_policy_cluster_batch.ps1"),
            "-Repo",
            $Repo,
            "-DataRoot",
            $DataRoot,
            "-Name",
            $batchName,
            "-Workers",
            $Workers,
            "-LimitPerWorker",
            $LimitPerWorker,
            "-Sources",
            $Sources,
            "-Proxy",
            $Proxy
        )
        $batchProcess = Start-Process -FilePath powershell.exe `
            -ArgumentList $batchArgs `
            -WorkingDirectory $Repo `
            -WindowStyle Hidden `
            -PassThru
        Write-Event "batch_started" @{
            batch_name = $batchName
            process_id = $batchProcess.Id
            workers = $Workers
            limit_per_worker = $LimitPerWorker
            sources = $Sources
        }
        $startedBatches += 1
    }

    Start-Sleep -Seconds $PollSeconds
}
