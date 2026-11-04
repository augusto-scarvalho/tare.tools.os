# One-time setup of self-updating tare.tools on this Windows machine (idempotent).
#   Tare-Serve   runs `tare serve` from the managed product (state stays in serve-state).
#   Tare-Update  runs `tare.cmd auto-update` at logon and every 5 minutes (a publish also starts it at once).
#   Tare-Publish (with -Publisher, the build machine only) checks every 2 minutes whether a main moved, publishes,
#                and starts Tare-Update here and on each -Peers user@host over ssh.
param([switch]$Publisher, [string[]]$Peers = @(), [switch]$CommandOnly)
$ErrorActionPreference = 'Stop'
$base = "$env:LOCALAPPDATA\tare.tools"
$product = "$base\product\tare.cmd"
if (-not (Test-Path $product)) { throw "Install a product bundle first: $product is missing" }
# Prefer the self-updating launcher over an older editable-install tare.exe.
# Keep all other PATH entries; repeating setup does not add another copy.
$commandDir = Split-Path -Parent $product
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\', '/') -ine $commandDir.TrimEnd('\', '/') })
$updatedPath = (@($commandDir) + $entries) -join ';'
if ($updatedPath -cne $userPath) {
    [Environment]::SetEnvironmentVariable('Path', $updatedPath, 'User')
    if (-not ('Tare.EnvironmentChange' -as [type])) {
        Add-Type -Namespace Tare -Name EnvironmentChange -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint flags, uint timeout, out System.UIntPtr result);
'@
    }
    $broadcastResult = [UIntPtr]::Zero
    [Tare.EnvironmentChange]::SendMessageTimeout([IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$broadcastResult) | Out-Null
}
$env:Path = $commandDir + ';' + (($env:Path -split ';' | Where-Object { $_ -and $_.TrimEnd('\', '/') -ine $commandDir.TrimEnd('\', '/') }) -join ';')
"tare command: $product"
if ($CommandOnly) { return }
New-Item -ItemType Directory -Force "$base\serve", "$base\serve-state" | Out-Null

Set-Content -Encoding ascii "$base\serve\tare-serve.cmd" @"
@echo off
set "TARE_STORAGE_ROOT=%LOCALAPPDATA%\tare.tools\serve-state"
call "%LOCALAPPDATA%\tare.tools\product\tare.cmd" serve --host 127.0.0.1 --port 8790 >> "%TARE_STORAGE_ROOT%\serve.log" 2>&1
"@
Set-Content -Encoding ascii "$base\serve\tare-update.cmd" @"
@echo off
call "%LOCALAPPDATA%\tare.tools\product\tare.cmd" auto-update >> "%LOCALAPPDATA%\tare.tools\product\auto-update.log" 2>&1
"@

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
function Register($name, $cmd, $triggers) {
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless cmd /c `"$cmd`""
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
    "registered $name"
}
$every = { param($minutes) $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $minutes); $t }
Register 'Tare-Update' "$base\serve\tare-update.cmd" @((New-ScheduledTaskTrigger -AtLogOn), (& $every 5))
if ($Publisher) {
    # Stable bootstrap; each invocation loads the publisher and packers from one
    # main commit, so new packaging rules travel with the published sources.
    $script = "$base\serve\tare-publish.py"
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'publish_product.py') -Destination $script -Force
    Set-Content -Encoding ascii "$base\serve\tare-publish.cmd" @"
@echo off
python -X utf8 "$script" --latest-tools $(($Peers | ForEach-Object { "--peer $_" }) -join ' ') >> "%LOCALAPPDATA%\tare.tools\publisher.log" 2>&1
"@
    Register 'Tare-Publish' "$base\serve\tare-publish.cmd" @((& $every 2))
}
