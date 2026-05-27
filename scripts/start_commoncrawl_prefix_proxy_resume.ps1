param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [string]$Name = "wave1-us-gb-jp-de-fr-cn-in-2char",
    [string]$Countries = "us,gb,jp,de,fr,cn,in",
    [string]$Index = "CC-MAIN-2026-17",
    [string]$Proxy = "http://127.0.0.1:7890",
    [int]$Timeout = 180,
    [float]$Sleep = 0.2
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$bg = Join-Path $DataRoot "commoncrawl-prefix-bg"
$db = Join-Path $DataRoot "queue.sqlite"
$outDir = Join-Path $bg $Name
$outLog = Join-Path $bg "run-prefix-$Index-$Name-proxy.out.log"
$errLog = Join-Path $bg "run-prefix-$Index-$Name-proxy.err.log"
New-Item -ItemType Directory -Force -Path $bg, $outDir | Out-Null

$command = @"
Set-Location '$Repo'
`$env:PYTHONUTF8='1'
`$env:PYTHONIOENCODING='utf-8'
python scripts\discover_commoncrawl_slug_prefixes.py --countries '$Countries' --prefix-lengths 2 --prefix-alphabet abcdefghijklmnopqrstuvwxyz --index '$Index' --limit 5000 --server-limit 5000 --fallback-server-limits 1000,500 --backend requests --timeout $Timeout --proxy '$Proxy' --sleep $Sleep --retries 3 --retry-sleep 5 --resume-progress --progress-jsonl '$bg\$Name.progress.jsonl' --summary-json '$bg\$Name.summary.json' --output-dir '$outDir' --db '$db'
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
    name = $Name
    countries = $Countries
    index = $Index
    proxy = $Proxy
    out_log = $outLog
    err_log = $errLog
    progress_jsonl = Join-Path $bg "$Name.progress.jsonl"
    summary_json = Join-Path $bg "$Name.summary.json"
} | ConvertTo-Json -Depth 3
