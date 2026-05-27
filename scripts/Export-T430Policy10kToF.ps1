param(
    [string]$TargetHost = "xiaolab-t430",
    [string]$PackageRoot = "F:\ios-privacy-policy-collector-10k",
    [string]$ZipPath = "F:\ios-privacy-policy-collector-10k.zip",
    [string]$RemoteRepo = "/data/xiaolab-research/ios-privacy-policy-collector",
    [string]$SnapshotName = "stage1-10k-auto-stop-reached-10000-20260525-135514"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$LogPath = "$PackageRoot.sync.log"
$ErrPath = "$PackageRoot.sync.err.log"

New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
}

Write-Log "Starting export from ${TargetHost}:$RemoteRepo"
Write-Log "PackageRoot=$PackageRoot ZipPath=$ZipPath"

$readme = @"
# iOS Privacy Policy Collector 10k Snapshot

Created from T430 project:

- Remote repo: $RemoteRepo
- Snapshot: data/stage-snapshots/$SnapshotName
- Main content: data/policy-clusters
- Expected gate: at least 10000 policy_documents

How to inspect:

1. Open `data/stage-snapshots/$SnapshotName/snapshot-manifest.json` for collection stats.
2. Browse `data/policy-clusters/`; each app/policy cluster keeps Markdown files and manifests.
3. Markdown files preserve policy page links where discovered by the collector.

The zip archive is a transport copy of this folder. The unzipped folder is already usable directly.
"@
Set-Content -LiteralPath (Join-Path $PackageRoot "README.md") -Value $readme -Encoding utf8

$remoteTarCommand = @"
cd '$RemoteRepo' && tar -cf - \
  data/policy-clusters \
  data/stage-snapshots/$SnapshotName \
  data/stage-monitor/stage1-10k-auto-stop.events.jsonl \
  data/stage-monitor/stage1-refill-10k.events.jsonl \
  data/stage-monitor/queue-backup.events.jsonl
"@

Write-Log "Streaming remote tar and extracting to package root"
ssh $TargetHost $remoteTarCommand 2>> $ErrPath | tar -xf - -C $PackageRoot
if ($LASTEXITCODE -ne 0) {
    throw "remote tar extraction failed with exit code $LASTEXITCODE. See $ErrPath"
}

Write-Log "Collecting local package stats"
$manifestPath = Join-Path $PackageRoot "data\stage-snapshots\$SnapshotName\snapshot-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Snapshot manifest missing after extraction: $manifestPath"
}
python (Join-Path $PSScriptRoot "build_review_pack.py") --package-root $PackageRoot --output-dir $PackageRoot | Out-Null
$summary = Get-Content -LiteralPath (Join-Path $PackageRoot "sample-index-summary.json") -Raw | ConvertFrom-Json
$mdCount = $summary.indexed_policy_folders
Write-Log "Extracted markdown files: $mdCount"

if (Test-Path -LiteralPath $ZipPath) {
    Write-Log "Removing previous zip: $ZipPath"
    Remove-Item -LiteralPath $ZipPath -Force
}

$parent = Split-Path -Parent $PackageRoot
$leaf = Split-Path -Leaf $PackageRoot
Write-Log "Creating zip archive with tar auto-compression"
tar -a -cf $ZipPath -C $parent $leaf
if ($LASTEXITCODE -ne 0) {
    throw "zip creation failed with exit code $LASTEXITCODE"
}

$zipInfo = Get-Item -LiteralPath $ZipPath
Write-Log "DONE ZipBytes=$($zipInfo.Length) MarkdownFiles=$mdCount"
