# Windows x64 self-contained ZIP, no installer and no README file.
[CmdletBinding()]
param([ValidateSet('win-x64')][string]$Runtime = 'win-x64')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$dist = Join-Path $PSScriptRoot 'dist'
$publish = Join-Path $dist $Runtime
$zip = Join-Path $dist 'WiFlow-Windows-x64-Portable-1.0.0.zip'
& (Join-Path $PSScriptRoot 'build.ps1') -Runtime $Runtime
if ($LASTEXITCODE -ne 0) { throw 'Windows publish failed' }
$required = @('WiFlow.exe','onnxruntime.dll','onnxruntime_providers_shared.dll',
    'models/captcha-common.onnx','models/captcha-digits.json','models/SOURCE.json')
foreach ($file in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $publish $file) -PathType Leaf)) {
        throw "Missing published file: $file"
    }
}
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $publish '*') -DestinationPath $zip -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) { throw 'Portable ZIP not produced' }
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $(Split-Path $zip -Leaf)" | Set-Content -LiteralPath "$zip.sha256" -Encoding ascii
Write-Host ("Windows portable ZIP: {0} ({1:N2} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
