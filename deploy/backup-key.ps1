param(
    [string]$Directory = (Join-Path $env:USERPROFILE '.config\arkham-backup'),
    [string]$Age = 'age',
    [string]$AgeKeygen = 'age-keygen'
)
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$destination = [IO.Path]::GetFullPath($Directory)
if ($destination.StartsWith($repository + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The recovery key must be stored outside the repository.'
}
if (Test-Path -LiteralPath $destination) { throw 'Choose a new private directory; existing key material is never replaced.' }
New-Item -ItemType Directory -Path $destination | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $destination /inheritance:r /grant:r "*${sid}:(OI)(CI)(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not apply a private user ACL.' }
$identity = Join-Path $destination 'recovery.agekey'
$recipientPath = Join-Path $destination 'recipient.txt'
& $AgeKeygen --output $identity 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Recovery key generation failed.' }
$recipient = (& $AgeKeygen -y $identity).Trim()
if ($LASTEXITCODE -ne 0 -or $recipient -notmatch '^age1[a-z0-9]{58}$') { throw 'Invalid public recipient.' }
[IO.File]::WriteAllText($recipientPath, $recipient + "`n", [Text.UTF8Encoding]::new($false))
$challenge = Join-Path $destination 'challenge.bin'
$encrypted = Join-Path $destination 'challenge.age'
$decrypted = Join-Path $destination 'challenge.restored'
try {
    $bytes = [byte[]]::new(128)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    [IO.File]::WriteAllBytes($challenge, $bytes)
    & $Age --encrypt --recipient $recipient --output $encrypted $challenge
    if ($LASTEXITCODE -ne 0) { throw 'Local encryption challenge failed.' }
    & $Age --decrypt --identity $identity --output $decrypted $encrypted
    if ($LASTEXITCODE -ne 0 -or (Get-FileHash -LiteralPath $challenge).Hash -ne (Get-FileHash -LiteralPath $decrypted).Hash) {
        throw 'Local recovery-key challenge failed.'
    }
    # Only paths and the public recipient are emitted. The private key never leaves this file.
    @{ recipient = $recipient; private_key = $identity; local_challenge = 'passed'; transfer_private_key = $false } | ConvertTo-Json -Compress
} finally {
    foreach ($path in @($challenge, $encrypted, $decrypted)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}
