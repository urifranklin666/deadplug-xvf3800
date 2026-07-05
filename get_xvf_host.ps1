# Fetch the win32 xvf_host control tool from Seeed's repo into
# host_control\win32\ (vendored; not committed to this repo).
# Requires the x86 VC++ 2015-2022 redistributable to run xvf_host.exe.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$tmp = Join-Path $env:TEMP ("xvf_" + [guid]::NewGuid().ToString('N'))
git clone --depth 1 --filter=blob:none --sparse `
  https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git $tmp
git -C $tmp sparse-checkout set host_control/win32
New-Item -ItemType Directory -Force host_control | Out-Null
if (Test-Path host_control\win32) { Remove-Item -Recurse -Force host_control\win32 }
Copy-Item -Recurse "$tmp\host_control\win32" host_control\
Remove-Item -Recurse -Force $tmp
Write-Host "installed: host_control\win32\xvf_host.exe"
