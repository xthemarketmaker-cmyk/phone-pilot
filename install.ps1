# install.ps1 - install or repair the Phone Pilot tools in C:\phonebot\live.
# Downloads each file from https://raw.githubusercontent.com/xthemarketmaker-cmyk/phone-pilot/<Ref>/<file>, checks it
# against the SHA-256 hash written below, and only then copies it into place. Any mismatch stops the install and
# nothing is changed. Never creates or overwrites config.json.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Ref <commit-sha|main>] [-Dest C:\phonebot\live]
#   testing:  -LocalDir <folder with the files>   or   -BaseUrl <url prefix ending before the file name>
param(
  [string]$Ref = "main",
  [string]$Dest = "C:\phonebot\live",
  [string]$BaseUrl = "",
  [string]$LocalDir = ""
)
$ErrorActionPreference = "Stop"
$Hashes = [ordered]@{
    "act.py" = "2b69a7b888ac21300789d0cd3337286c55dd3ca018b2ce17ed1bad3458cdbf47"
    "autolock.py" = "d8d32d1158964b86e5086889587eafbde71df33685eda1ea97ed61d62f181924"
    "chain_up.py" = "a7c6a4a434b9a1ebde09d7f414b6028126e065235f676d5631f6982ba53c7e9e"
    "config.example.json" = "2498579b8c56a7439098af788572f0d109f35cbe52304649a1cd5443c6819bac"
    "do.py" = "2f815878bbbdce82e66319afe123ece3eb8881c5f4bf18b07ebeecb61fc361e2"
    "doctor.py" = "7be802f0319f5b40d65af4ea1e39a16d97c15f455793e0c4d539dc0267394c25"
    "install_autostart.ps1" = "f76d68397ad9e59ee573eccf72600bc226dacb8dc9554b64c122bcf17ac71aee"
    "phone_view.py" = "aa5fd825bf70fd028128b22093ea407c5e5717ab697eaf5c370e676e94b66866"
    "pp.py" = "a56ee64fd25503f482094b35196648870a390dd4680a75141e2fa76aa024dd19"
}
function Fail($msg) { Write-Host "INSTALL STOPPED: $msg" -ForegroundColor Red; Write-Host "Nothing in $Dest was changed."; exit 1 }

if (-not $BaseUrl) { $BaseUrl = "https://raw.githubusercontent.com/xthemarketmaker-cmyk/phone-pilot/$Ref" }
$BaseUrl = $BaseUrl.TrimEnd("/")
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$stage = Join-Path $env:TEMP ("pp-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $stage | Out-Null
try {
  foreach ($f in $Hashes.Keys) {
    $out = Join-Path $stage $f
    if ($LocalDir) {
      $src = Join-Path $LocalDir $f
      if (-not (Test-Path $src)) { Fail "$f is missing in $LocalDir" }
      Copy-Item $src $out
    } else {
      try { Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$f" -OutFile $out -TimeoutSec 60 }
      catch { Fail "could not download $f from $BaseUrl ($($_.Exception.Message)). Check the internet connection and the -Ref value." }
    }
    $got = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $Hashes[$f]) { Fail "$f does not match its expected SHA-256 (got $got, expected $($Hashes[$f])). The download may be damaged or the file was changed; use a pinned -Ref that matches this installer." }
    Write-Host ("ok      {0,-22} {1}" -f $f, $got.Substring(0, 12))
  }
  New-Item -ItemType Directory -Force $Dest | Out-Null
  $base = Split-Path $Dest -Parent
  foreach ($d in "logs", "locks") { New-Item -ItemType Directory -Force (Join-Path $base $d) | Out-Null }
  foreach ($f in $Hashes.Keys) {
    if ($f -eq "config.json") { continue }   # never shipped, never overwritten
    Copy-Item (Join-Path $stage $f) (Join-Path $Dest $f) -Force
  }
  # re-check what landed
  foreach ($f in $Hashes.Keys) {
    if ((Get-FileHash (Join-Path $Dest $f) -Algorithm SHA256).Hash.ToLower() -ne $Hashes[$f]) { Write-Host "INSTALL STOPPED: $f changed while copying" -ForegroundColor Red; exit 1 }
  }
  $cfg = Join-Path $base "config.json"
  Write-Host ""
  Write-Host "Installed $($Hashes.Count) files into $Dest (ref $Ref), all SHA-256 verified."
  if (Test-Path $cfg) { Write-Host "Kept your existing $cfg." } else { Write-Host "No $cfg yet: copy $Dest\config.example.json there and fill in your phone." }
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { Write-Host "Python not found: winget install -e --id Python.Python.3.12, then python -m pip install pillow psutil" }
  Write-Host "Next: python $Dest\doctor.py"
} finally {
  Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
