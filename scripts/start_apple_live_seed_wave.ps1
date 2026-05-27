param(
    [string]$Repo = "E:\CodexRemote\ios-privacy-policy-collector",
    [string]$DataRoot = "F:\ios-privacy-policy-collector\data",
    [string]$Name = "stage1-live-high-yield",
    [string]$Countries = "us,gb,ca,au,de,fr,jp,kr,cn,in,br,mx,es,it,nl,se,sg,ch,at,no,dk,fi,ie,nz",
    [string[]]$Terms = @(
        "game",
        "travel",
        "music",
        "video",
        "shopping",
        "sports",
        "food",
        "weather",
        "fitness",
        "education",
        "kids",
        "news",
        "social",
        "productivity",
        "photo"
    ),
    [string]$Proxy = "http://127.0.0.1:7890",
    [int]$SearchLimit = 200,
    [int]$BatchSize = 100,
    [int]$Timeout = 60
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$seedRoot = Join-Path $DataRoot "seed-waves"
$waveDir = Join-Path $seedRoot $Name
$db = Join-Path $DataRoot "queue.sqlite"
New-Item -ItemType Directory -Force -Path $waveDir | Out-Null

$termArgs = ($Terms | ForEach-Object { "--term `"$_`"" }) -join " "
$sourceArgs = ($Terms | ForEach-Object { "apple-search:$_" }) -join ","
$command = @"
Set-Location '$Repo'
`$env:PYTHONUTF8='1'
`$env:PYTHONIOENCODING='utf-8'
python scripts\discover_apple_live_seeds.py --countries '$Countries' --charts none $termArgs --search-limit $SearchLimit --timeout $Timeout --proxy '$Proxy' --sleep 0.05 --progress-every 25 --resume-progress --incremental-import --progress-jsonl '$waveDir\$Name.progress.jsonl' --output-csv '$waveDir\$Name.seeds.csv' --summary-json '$waveDir\$Name.summary.json' --db '$db'
python scripts\validate_itunes_seeds.py --db '$db' --batch-size $BatchSize --timeout $Timeout --proxy '$Proxy' --sleep 0.05 --progress-every 2000 --output-json '$waveDir\$Name.validation.json' --output-csv '$waveDir\$Name.validation.csv'
python scripts\export_seed_dump.py --db '$db' --sources '$sourceArgs' --status active --output-csv '$waveDir\$Name.active-seeds.csv' --summary-json '$waveDir\$Name.active-seeds.summary.json'
"@

$outLog = Join-Path $waveDir "$Name.out.log"
$errLog = Join-Path $waveDir "$Name.err.log"
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
    terms = $Terms
    sources = $sourceArgs
    out_log = $outLog
    err_log = $errLog
    summary_json = Join-Path $waveDir "$Name.summary.json"
    validation_json = Join-Path $waveDir "$Name.validation.json"
} | ConvertTo-Json -Depth 4
