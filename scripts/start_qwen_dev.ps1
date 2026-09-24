param([string]$SshHost = "75")
$ErrorActionPreference = "Stop"
if ($SshHost.StartsWith("-") -or $SshHost -match '\s') { throw "Use one approved SSH alias." }
Write-Information "Qwen9B private website: http://127.0.0.1:8090/ (keep this connection open)" -InformationAction Continue
& ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8090:127.0.0.1:8090 $SshHost
exit $LASTEXITCODE
