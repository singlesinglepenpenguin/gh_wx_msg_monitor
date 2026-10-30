# Synthetic equivalence test only: compiles the bundled C# but never starts Weixin.
$ErrorActionPreference = 'Stop'
$sourcePath = Join-Path $PSScriptRoot '../vendor/wechat_native/scripts/windows_wechat_get_key.ps1'
$source = [IO.File]::ReadAllText((Resolve-Path $sourcePath))
$code = [regex]::Match($source, '(?s)\$DebugApiCode = @''\r?\n(.*?)\r?\n''@')
if (-not $code.Success) { throw 'Bundled C# source was not found' }
Add-Type -TypeDefinition $code.Groups[1].Value -Language CSharp
$method = [DebugApiWx.KeyExtractor].GetMethod('DerivePageKey', [Reflection.BindingFlags]'NonPublic,Static')
if (-not $method) { throw 'DerivePageKey not found' }
$results = @()
foreach ($case in 0,1) {
    $password = [byte[]](0..31 | ForEach-Object { if ($case -eq 0) { $_ } else { 255 - $_ } })
    $salt = [byte[]](0..15 | ForEach-Object { if ($case -eq 0) { $_ } else { 0 } })
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $actual = [byte[]]$method.Invoke($null, [object[]]@($password,$salt))
    $nativeMs = $timer.Elapsed.TotalMilliseconds
    $timer.Restart()
    $kdf = [Security.Cryptography.Rfc2898DeriveBytes]::new($password,$salt,256000,[Security.Cryptography.HashAlgorithmName]::SHA512)
    try { $expected = $kdf.GetBytes(32) } finally { $kdf.Dispose() }
    $managedMs = $timer.Elapsed.TotalMilliseconds
    if ([Convert]::ToBase64String($actual) -ne [Convert]::ToBase64String($expected)) {
        throw "Native and original PBKDF2 differ for synthetic case $case"
    }
    $results += [pscustomobject]@{ case=$case; equal=$true; native_ms=[math]::Round($nativeMs,1); original_ms=[math]::Round($managedMs,1) }
}
[pscustomobject]@{ synthetic_only=$true; results=$results } | ConvertTo-Json -Depth 3
