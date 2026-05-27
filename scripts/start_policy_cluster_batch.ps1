param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [string]$Name = "stage1-10k",
    [int]$Workers = 8,
    [int]$LimitPerWorker = 250,
    [string]$Countries = "",
    [string]$Sources = "apple-search:*",
    [string]$Proxy = "http://127.0.0.1:7890",
    [int]$MinPolicyChars = 500,
    [int]$Timeout = 45,
    [int]$JsTimeout = 30
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$logRoot = Join-Path $DataRoot "policy-batch-bg"
$batchName = "cluster-$Name-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$batchDir = Join-Path $logRoot $batchName
$outLog = Join-Path $logRoot "$batchName.out.log"
$errLog = Join-Path $logRoot "$batchName.err.log"
$summary = Join-Path $logRoot "$batchName.summary.json"

New-Item -ItemType Directory -Force -Path $batchDir | Out-Null

$countryArg = ""
if (-not [string]::IsNullOrWhiteSpace($Countries)) {
    $countryArg = "--countries '$Countries'"
}

$command = @"
Set-Location '$Repo'
`$env:PYTHONUTF8='1'
`$env:PYTHONIOENCODING='utf-8'
python scripts\run_batch.py --db '$DataRoot\queue.sqlite' --output-dir '$DataRoot\policy-clusters' --log-dir '$batchDir' --workers $Workers --limit-per-worker $LimitPerWorker --worker-prefix '$batchName' --active-only $countryArg --sources '$Sources' --claim-order newest --min-policy-chars $MinPolicyChars --proxy '$Proxy' --timeout $Timeout --fallback-timeout 8 --max-attempts 2 --cluster-max-depth 1 --cluster-max-docs 8 --cluster-min-chars 200 --js-timeout $JsTimeout --js-wait-ms 1000 --poll-seconds 30 --summary-json '$summary'
"@

$process = Start-Process -FilePath powershell.exe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
    -WorkingDirectory $Repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

[pscustomobject]@{
    process_id = $process.Id
    batch_name = $batchName
    workers = $Workers
    limit_per_worker = $LimitPerWorker
    countries = $Countries
    sources = $Sources
    log_dir = $batchDir
    out_log = $outLog
    err_log = $errLog
    summary_json = $summary
} | ConvertTo-Json -Depth 3
