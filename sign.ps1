<#
.SYNOPSIS
    Build kopy.exe and sign it with a self-signed code-signing certificate.

.DESCRIPTION
    First run creates the certificate in Cert:\CurrentUser\My. Later runs reuse it,
    so every build is signed by the same publisher. The public certificate is
    exported next to the exe as kopy-signing.cer: give that file to IT to deploy
    (Group Policy) into "Trusted Root Certification Authorities" AND
    "Trusted Publishers" on the machines that will run kopy.

.EXAMPLE
    .\sign.ps1 -Subject "CN=Contoso IT Tools"
#>
param(
    # Publisher name shown in Windows' signature details. Keep it stable.
    [string]$Subject = "CN=kopy Code Signing",
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [int]$Years = 3,
    # Sign the existing exe without rebuilding.
    [switch]$NoBuild,
    # Also trust the certificate for the current user on THIS machine (for testing).
    [switch]$TrustLocally
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $NoBuild) {
    cargo build --release
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
}
$exe = Join-Path $PSScriptRoot 'target\release\kopy.exe'
if (-not (Test-Path $exe)) { throw "not found: $exe (build first)" }

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq $Subject -and $_.NotAfter -gt (Get-Date) -and $_.HasPrivateKey } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1

if (-not $cert) {
    Write-Host "Creating code-signing certificate '$Subject' (valid $Years years)..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
        -NotAfter (Get-Date).AddYears($Years)
}
Write-Host "Certificate: $($cert.Subject)  thumbprint $($cert.Thumbprint)  expires $($cert.NotAfter.ToString('yyyy-MM-dd'))"

$cer = Join-Path (Split-Path $exe) 'kopy-signing.cer'
Export-Certificate -Cert $cert -FilePath $cer | Out-Null

if ($TrustLocally) {
    # Adding to Root shows a Windows confirmation dialog.
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
    Write-Host "Trusted locally for the current user."
}

$sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer $TimestampServer
if ($sig.SignerCertificate.Thumbprint -ne $cert.Thumbprint) {
    throw "signing failed: $($sig.Status) $($sig.StatusMessage)"
}
if (-not $sig.TimeStamperCertificate) {
    throw "signed but not timestamped (is $TimestampServer reachable?); the signature would expire with the certificate"
}

# On machines that don't trust the certificate yet, status is UnknownError: that is
# expected and means "signed, but by a publisher this machine doesn't know".
$check = Get-AuthenticodeSignature $exe
Write-Host ""
Write-Host "Signed:      $exe"
Write-Host "Status:      $($check.Status)"
Write-Host "Timestamped: $($check.TimeStamperCertificate.Subject)"
Write-Host "Public cert: $cer"
if ($check.Status -ne 'Valid') {
    Write-Host "(Not trusted on this machine yet - deploy kopy-signing.cer, or rerun with -TrustLocally to test.)"
}
