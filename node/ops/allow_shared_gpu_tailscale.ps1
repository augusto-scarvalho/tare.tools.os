# Run in elevated Windows PowerShell on the GPU node. Leave global firewall policy intact.
$ErrorActionPreference = 'Stop'
$name = 'TareSharedGpuTailscale'
$creator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
$existing = Get-NetFirewallHyperVRule -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    if (((@($existing.LocalPorts) | Sort-Object) -join ',') -ne '8080,8188' -or
        ($existing.RemoteAddresses -join ',') -notin @('100.64.0.0/10','100.64.0.0/255.192.0.0') -or
        $existing.VMCreatorId -ne $creator -or
        $existing.Protocol.ToString() -notin @('TCP','6') -or
        $existing.Action.ToString() -notin @('Allow','2') -or
        $existing.Direction.ToString() -notin @('Inbound','1')) {
        throw 'Existing shared-GPU rule differs; inspect it before changing it.'
    }
    $existing
} else {
    New-NetFirewallHyperVRule -Name $name `
        -DisplayName 'tare.tools shared GPU (Tailscale)' `
        -Direction Inbound -VMCreatorId $creator -Protocol TCP `
        -LocalPorts 8080,8188 -RemoteAddresses '100.64.0.0/10' -Action Allow
}
