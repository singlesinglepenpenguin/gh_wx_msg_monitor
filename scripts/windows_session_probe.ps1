# Inspect only the explicitly identified acceptance-test client/debugger.
param([int]$ClientId, [int]$DebuggerId, [switch]$StopTest)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$resultPath = Join-Path $root 'data/diagnostics/windows_session_probe.json'
$client = Get-CimInstance Win32_Process -Filter "ProcessId=$ClientId"
$debugger = Get-CimInstance Win32_Process -Filter "ProcessId=$DebuggerId"
if (-not $client -or $client.Name -ne 'Weixin.exe' -or $client.ParentProcessId -ne $DebuggerId) {
    throw 'The requested client is not the specified test debugger child.'
}
if (-not $debugger -or $debugger.Name -ne 'powershell.exe' -or $debugger.CommandLine -notmatch 'chatlog_launch_') {
    throw 'The specified process is not an upstream extraction test debugger.'
}
$result = @{client_id=$ClientId; debugger_id=$DebuggerId; stopped=$false; checked_at=(Get-Date).ToString('o')}
if ($StopTest) {
    Stop-Process -Id $DebuggerId -Force
    Start-Sleep -Seconds 2
    $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$ClientId"
    if ($remaining -and $remaining.Name -eq 'Weixin.exe' -and $remaining.ParentProcessId -eq $DebuggerId) {
        Stop-Process -Id $ClientId -Force
    }
    $result.stopped = $true
} else {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class AcceptanceWindow { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int cmd); }'
    $process = Get-Process -Id $ClientId
    $result.responding = $process.Responding
    $result.window_title = $process.MainWindowTitle
    $result.window_handle = $process.MainWindowHandle.ToInt64()
    [AcceptanceWindow]::ShowWindow($process.MainWindowHandle,9) | Out-Null
    [AcceptanceWindow]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
    Start-Sleep -Seconds 1
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bitmap = New-Object System.Drawing.Bitmap($bounds.Width,$bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size)
        $bitmap.Save((Join-Path $root 'data/diagnostics/windows_live_screen.png'))
    } finally { $graphics.Dispose(); $bitmap.Dispose() }
}
$result | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $resultPath
