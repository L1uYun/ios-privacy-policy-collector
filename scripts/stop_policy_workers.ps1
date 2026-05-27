param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
$patterns = @(
    "scripts\run_batch.py",
    "scripts\queue_worker.py",
    "run-policy-cluster",
    "cluster-stage1-",
    "cluster-batch"
)

$processes = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    if (-not $cmd) { return $false }
    if ($cmd -notlike "*ios-privacy-policy-collector*") { return $false }
    foreach ($pattern in $patterns) {
        if ($cmd -like "*$pattern*") { return $true }
    }
    return $false
}

$stopped = @()
foreach ($process in $processes) {
    $item = [pscustomobject]@{
        process_id = $process.ProcessId
        parent_process_id = $process.ParentProcessId
        name = $process.Name
        command_line = $process.CommandLine
    }
    if (-not $WhatIfOnly) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            $item | Add-Member -NotePropertyName stopped -NotePropertyValue $true
        } catch {
            $item | Add-Member -NotePropertyName stopped -NotePropertyValue $false
            $item | Add-Member -NotePropertyName error -NotePropertyValue $_.Exception.Message
        }
    } else {
        $item | Add-Member -NotePropertyName stopped -NotePropertyValue $false
        $item | Add-Member -NotePropertyName what_if -NotePropertyValue $true
    }
    $stopped += $item
}

[pscustomobject]@{
    stopped_count = @($stopped | Where-Object { $_.stopped }).Count
    matched_count = @($stopped).Count
    what_if = [bool]$WhatIfOnly
    processes = $stopped
} | ConvertTo-Json -Depth 6
