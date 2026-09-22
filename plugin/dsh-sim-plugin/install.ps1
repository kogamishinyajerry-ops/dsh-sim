# dsh-sim-plugin intranet installer (DSH 0.1.5-rc.2, offline)
# Idempotent: safe to re-run after any `pnpm install` prunes the junctions.
# Usage (in your own PowerShell, NOT inside an agent sandbox):
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DshHome <DSH_HOME>\home -Profile web

param(
  [string]$DshHome = "<DSH_HOME>\home",
  [string]$Profile = "web",
  [string]$PluginSource = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$pluginName = "dsh-sim-plugin"
$pluginsRoot = Join-Path $DshHome "plugins"
$pluginTarget = Join-Path $pluginsRoot $pluginName
$profileDir = Join-Path $DshHome "profiles\$Profile"
$dshBin = "<DSH_HOME>\app\node_modules\@deepseek-ai\dsh\lib\bin.js"
$node = "C:\Program Files\nodejs\node.exe"

Write-Host "== dsh-sim-plugin intranet install ==" -ForegroundColor Cyan
Write-Host "  DshHome   : $DshHome"
Write-Host "  Profile   : $Profile ($profileDir)"
Write-Host "  Source    : $PluginSource"

# 1) Copy plugin to a stable home (pnpm pruning never touches <DSH_HOME>\home\plugins)
New-Item -ItemType Directory -Path $pluginsRoot -Force | Out-Null
if (Test-Path $pluginTarget) { Remove-Item $pluginTarget -Recurse -Force -Confirm:$false }
robocopy $PluginSource $pluginTarget /E /NFL /NDL /NJH /NJS /XD node_modules | Out-Null
Write-Host "  [1/4] copied to $pluginTarget"

# 2) Register with dsh's own installer (link: dep + bundles entry + profile symlink)
& $node $dshBin plugin --profile $Profile add $pluginTarget --store-dir="$DshHome\.pnpm-store"
Write-Host "  [2/4] registered via dsh plugin add (link:)"

# 3) Dependency junctions INSIDE the plugin dir (Node resolves by realpath:
#    without these, the loader cannot see @deepseek-ai/* peer packages).
$ai = Join-Path $DshHome "profiles\node_modules\@deepseek-ai"
New-Item -ItemType Directory -Path "$pluginTarget\node_modules\@deepseek-ai" -Force | Out-Null
foreach ($p in @("schemastery", "dsh-settings", "dsh-host-webserver", "cordis")) {
  $src = Join-Path $ai $p
  $dst = Join-Path "$pluginTarget\node_modules\@deepseek-ai" $p
  if (Test-Path $dst) { Remove-Item $dst -Force -Confirm:$false }
  if (Test-Path $src) {
    New-Item -ItemType Junction -Path $dst -Target $src -Force | Out-Null
    Write-Host "    junction $p"
  } else {
    Write-Host "    WARN missing $src (plugin may fail to load)" -ForegroundColor Yellow
  }
}
Write-Host "  [3/4] dependency junctions created"

# 4) Verify loader can resolve the bundle (dump-config must list sim-bridge, no errors)
$dump = & $node $dshBin --profile $Profile --dump-config 2>&1 | Out-String
if ($dump -match "sim-bridge" -and $dump -notmatch "cannot resolve profile bundle") {
  Write-Host "  [4/4] dump-config OK: sim-bridge resolvable" -ForegroundColor Green
} else {
  Write-Host "  [4/4] dump-config check FAILED — review output:" -ForegroundColor Red
  Write-Host $dump
  exit 1
}

Write-Host ""
Write-Host "Install complete. Restart dsh web:" -ForegroundColor Green
Write-Host "  Get-CimInstance Win32_Process -Filter ""Name='node.exe'"" | Where-Object { `$_.CommandLine -match 'bin\.js' } | Stop-Process -Force"
Write-Host "  `$env:DSH_HOME='$DshHome'; & '$node' '$dshBin' web"
Write-Host ""
Write-Host "Then open DSH -> right sidebar 'Sim Workbench' tab -> Settings card 'Simulation Service' -> Test connection."
