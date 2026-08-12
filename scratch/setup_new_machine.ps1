# Stand up video_automation on a fresh machine.
#
#   powershell -ExecutionPolicy Bypass -File .\setup_new_machine.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_new_machine.ps1 -Dest D:\dev -WithModel
#
# Nothing is copied from the old machine. Source comes from GitHub, state from
# GCS, secrets from Secret Manager. The only thing that does not transfer is
# your gcloud login, which you redo below.

param(
  [string]$Dest = "$env:USERPROFILE",
  [switch]$WithModel,     # also pull the 97 MB depth model (only for LOCAL renders)
  [switch]$SkipDeps       # skip npm install / venv
)

$ErrorActionPreference = 'Stop'
$REPO = "https://github.com/lucasfarmer20-netizen/video_automation.git"
$BUCKET = "gs://lucas-storyboard-vault-001-483921"
$proj = Join-Path $Dest "video_automation"

function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Write-Host "MISSING: $cmd  -> $hint" -ForegroundColor Red; return $false
  }
  Write-Host "  ok: $cmd" -ForegroundColor DarkGray; return $true
}

Write-Host "`n=== prerequisites" -ForegroundColor Cyan
$ok = $true
$ok = (Need git    "https://git-scm.com/download/win")    -and $ok
$ok = (Need node   "https://nodejs.org (LTS)")            -and $ok
$ok = (Need python "https://python.org - 3.11 or newer")  -and $ok
$ok = (Need gcloud "https://cloud.google.com/sdk/docs/install") -and $ok
if (-not $ok) { Write-Host "`nInstall the missing tools, then re-run." -ForegroundColor Red; exit 1 }
# ffmpeg is a system install per CLAUDE.md -- never a pip dependency.
# NOT optional: without it 12 tests in tests/test_director.py skip and the 5
# paid-rebill tests hard-fail on WinError 2, so the money-path guards never run.
foreach ($bin in @("ffmpeg", "ffprobe")) {
  if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) {
    Write-Host "  MISSING: $bin not on PATH." -ForegroundColor Red
    Write-Host "    Install with: winget install Gyan.FFmpeg   (then reopen the shell)" -ForegroundColor Yellow
    Write-Host "    Required to run the test suite locally. Cloud Run installs its own." -ForegroundColor Yellow
    $ok = $false
  }
}
if (-not $ok) { Write-Host "`nInstall the missing tools, then re-run." -ForegroundColor Red; exit 1 }

Write-Host "`n=== clone" -ForegroundColor Cyan
if (Test-Path $proj) {
  Write-Host "  $proj already exists - pulling instead"
  git -C $proj pull --ff-only
} else {
  git clone $REPO $proj
}
Set-Location $proj

Write-Host "`n=== gcloud auth (this is the one thing that cannot be copied)" -ForegroundColor Cyan
Write-Host "  Run these yourself - they open a browser:" -ForegroundColor Yellow
Write-Host "    gcloud auth login"
Write-Host "    gcloud auth application-default login"
Write-Host "    gcloud config set project lucas-pipeline-2026-v1"
Write-Host ""
Write-Host "  Do NOT copy the service-account .json from the old machine. ADC above" -ForegroundColor Yellow
Write-Host "  replaces it, and that key should be rotated out anyway." -ForegroundColor Yellow

if (-not $SkipDeps) {
  Write-Host "`n=== frontend deps" -ForegroundColor Cyan
  Push-Location (Join-Path $proj "frontend"); npm install; Pop-Location

  Write-Host "`n=== python venv" -ForegroundColor Cyan
  python -m venv (Join-Path $proj ".venv")
  & (Join-Path $proj ".venv\Scripts\python.exe") -m pip install --upgrade pip
  & (Join-Path $proj ".venv\Scripts\pip.exe") install -r (Join-Path $proj "requirements.txt")
}

if ($WithModel) {
  Write-Host "`n=== depth model (only needed to render locally)" -ForegroundColor Cyan
  New-Item -ItemType Directory -Force (Join-Path $proj "models") | Out-Null
  gcloud storage cp "$BUCKET/models/depth_anything_v2_vits.onnx" (Join-Path $proj "models\")
}

Write-Host "`nDone. $proj" -ForegroundColor Green
Write-Host @"

The studio itself runs on Cloud Run, not here:
  https://youtube-video-pipeline-mfelaj54qa-uc.a.run.app

This checkout is for editing code and deploying:
  gcloud builds submit --config cloudbuild.yaml .

That is the only supported deploy path.
"@
