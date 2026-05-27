param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [int]$TargetPolicyDocuments = 10000,
    [int]$PollSeconds = 60,
    [string]$Name = "stage1-10k-auto-stop",
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$logRoot = Join-Path $DataRoot "stage-monitor"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$statusJson = Join-Path $DataRoot "milestone-status.json"
$eventsJsonl = Join-Path $logRoot "$Name.events.jsonl"

function Write-EventJson {
    param([hashtable]$Payload)
    $Payload["created_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $Payload | ConvertTo-Json -Depth 8 -Compress | Add-Content -Path $eventsJsonl -Encoding utf8
}

function Get-PolicyDocumentCount {
    Set-Location $Repo
    python scripts\milestone_status.py --db "$DataRoot\queue.sqlite" --output-json $statusJson | Out-Null
    $status = Get-Content $statusJson -Raw | ConvertFrom-Json
    return [int]$status.stats.policy_documents
}

while ($true) {
    $count = Get-PolicyDocumentCount
    Write-EventJson @{
        event = "poll"
        target_policy_documents = $TargetPolicyDocuments
        policy_documents = $count
    }

    if ($count -ge $TargetPolicyDocuments) {
        $snapshotName = "$Name-reached-$TargetPolicyDocuments-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Set-Location $Repo
        python scripts\freeze_stage_snapshot.py `
            --db "$DataRoot\queue.sqlite" `
            --data-root $DataRoot `
            --name $snapshotName | Out-File -FilePath (Join-Path $logRoot "$snapshotName.snapshot.log") -Encoding utf8

        $stopLog = Join-Path $logRoot "$snapshotName.stop-policy-workers.json"
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_policy_workers.ps1 `
            -Repo $Repo `
            -DataRoot $DataRoot | Out-File -FilePath $stopLog -Encoding utf8

        $requeueLog = Join-Path $logRoot "$snapshotName.requeue-running.json"
        python scripts\queue_store.py --db "$DataRoot\queue.sqlite" requeue-running |
            Out-File -FilePath $requeueLog -Encoding utf8

        Write-EventJson @{
            event = "target_reached"
            target_policy_documents = $TargetPolicyDocuments
            policy_documents = $count
            snapshot_name = $snapshotName
            stop_log = $stopLog
            requeue_log = $requeueLog
        }
        break
    }

    if ($Once) {
        break
    }
    Start-Sleep -Seconds $PollSeconds
}
