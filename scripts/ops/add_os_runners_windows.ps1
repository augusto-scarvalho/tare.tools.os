# Two more tare.tools.os runners on the node (aaaaa-desktop-2, -3), copies of C:\actions-runner, as Windows
# services under NETWORK SERVICE. The registration token comes on stdin (docs/operations/INFRASTRUCTURE_CI_RUNNERS_NODE_AAAAA.md,
# item 5). Run elevated (an administrator over SSH is).
$token = [Console]::In.ReadLine()  # through stdin: never on this disk or in a command line
if (-not $token) { 'no registration token on stdin'; exit 1 }
$src = 'C:\actions-runner'
foreach ($n in 2, 3) {
    $dir = "C:\actions-runners\tare.tools.os-$n"
    if (Test-Path "$dir\.runner") { "skip $dir (already configured)"; continue }
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }  # a partial copy from an attempt that never registered
    New-Item -ItemType Directory -Force $dir | Out-Null
    foreach ($item in 'bin', 'externals') {
        # bin and externals are links to the version folders (bin.2.337.0); Windows will not traverse them for
        # another process (ERROR 448), and robocopy then retries for ever. Copy the target folder itself.
        $target = @((Get-Item "$src\$item").Target)[0]
        $from = if ($target) { $target } else { "$src\$item" }
        robocopy $from "$dir\$item" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { "  [$n] copy of $item failed (robocopy $LASTEXITCODE)"; exit 1 }
    }
    foreach ($f in 'config.cmd', 'run.cmd', 'run-helper.cmd.template', 'run-helper.sh.template', '.env') {
        if (Test-Path "$src\$f") { Copy-Item "$src\$f" "$dir\$f" -Force }
    }
    # The CI's learned test map, so the first runs here are scoped like the original runner's; each learns on its own.
    $tool = "$dir\_work\_tool\tare-ci"
    New-Item -ItemType Directory -Force $tool | Out-Null
    Copy-Item "$src\_work\_tool\tare-ci\test-map-windows-host*.json" $tool -ErrorAction SilentlyContinue
    Push-Location $dir
    $out = & cmd.exe /c "config.cmd --unattended --url https://github.com/augusto-scarvalho/tare.tools.os --token $token --name aaaaa-desktop-$n --labels aaaaa --work _work --runasservice" 2>&1
    $out | Select-Object -Last 6 | ForEach-Object { "  [$n] $_" }
    Pop-Location
}
Get-CimInstance Win32_Service | Where-Object { $_.PathName -like '*tare.tools.os-*' -or $_.PathName -like 'C:\actions-runner\*' -or $_.PathName -like '"C:\actions-runner\*' } |
    ForEach-Object { $_.Name + ' ' + $_.State + ' ' + $_.StartName }
