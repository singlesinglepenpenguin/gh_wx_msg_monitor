# Acceptance driver: interacts only with the clean test package's own windows.
param([ValidateSet('Capture','Click')][string]$Action='Capture', [int]$X, [int]$Y)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$session = Get-Content -Encoding UTF8 (Join-Path $root 'data/diagnostics/cold_setup_session.json') | ConvertFrom-Json
$binary = Join-Path $session.package 'GroupMessageMonitor.exe'
$process = Get-Process GroupMessageMonitor | Where-Object { $_.Path -eq $binary -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $process) { throw 'Clean-package GUI is absent' }
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class GuiAcceptance { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h,IntPtr after,int x,int y,int cx,int cy,uint flags); [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y); [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint x,uint y,uint data,UIntPtr extra); [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h,uint attribute,[Out] int[] rect,int size); }'
[GuiAcceptance]::SetProcessDPIAware() | Out-Null
$handle = $process.MainWindowHandle
[GuiAcceptance]::SetWindowPos($handle,[IntPtr](-1),0,0,0,0,3) | Out-Null
[GuiAcceptance]::SetForegroundWindow($handle) | Out-Null
Start-Sleep -Milliseconds 250
$rect = New-Object int[] 4
if ([GuiAcceptance]::DwmGetWindowAttribute($handle,9,$rect,16) -ne 0) { throw 'Test window frame unavailable' }
if ($Action -eq 'Click') {
    if ($X -lt 0 -or $Y -lt 0 -or $X -ge ($rect[2]-$rect[0]) -or $Y -ge ($rect[3]-$rect[1])) { throw 'Point outside test window' }
    [GuiAcceptance]::SetCursorPos(($rect[0]+$X),($rect[1]+$Y)) | Out-Null
    [GuiAcceptance]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
    [GuiAcceptance]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
    Start-Sleep -Milliseconds 350
}
$image = New-Object Drawing.Bitmap(($rect[2]-$rect[0]),($rect[3]-$rect[1]))
$graphics = [Drawing.Graphics]::FromImage($image)
try { $graphics.CopyFromScreen($rect[0],$rect[1],0,0,$image.Size); $image.Save((Join-Path $root 'data/diagnostics/cold_setup_gui.png')) }
finally { $graphics.Dispose(); $image.Dispose() }
[GuiAcceptance]::SetWindowPos($handle,[IntPtr](-2),0,0,0,0,3) | Out-Null
