param(
    [switch]$RegisterTask,
    [string]$BackupDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) '.private/host-backups')
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$podmanPath = Join-Path $env:LOCALAPPDATA 'Programs/Podman/podman.exe'
$taskName = 'Zoho DMARC MCP'
if ($RegisterTask) {
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute (Get-Command pwsh).Source -Argument ('-NoProfile -File "' + $PSCommandPath + '" -BackupDirectory "' + $BackupDirectory + '"')
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $owner
    $hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $hourly) -Principal $principal -Settings $settings -Description 'Start the designated DMARC cluster and export verified SQLite backups. Requires owner sign-in and an awake host.' -Force | Out-Null
    Write-Output 'Owner logon/hourly task registered.'
    exit
}

New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
$backupRoot = [IO.Path]::GetFullPath($BackupDirectory).TrimEnd('\') + '\'
$previousConnection = $env:CONTAINER_CONNECTION
$pod = $null
$status = @{state='failed'; at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); reason='host_start_or_export_failed'}
try {
    $machine = (& $podmanPath machine inspect podman-machine-dmarc | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0) { throw 'Designated machine unavailable' }
    if ($machine.State -ne 'running') {
        & $podmanPath machine start podman-machine-dmarc
        if ($LASTEXITCODE -ne 0) { throw 'Machine start failed' }
    }
    $env:CONTAINER_CONNECTION = 'podman-machine-dmarc-root'
    minikube start -p zoho-dmarc --driver=podman --container-runtime=cri-o --cpus=4 --memory=4096 --keep-context
    if ($LASTEXITCODE -ne 0) { throw 'Cluster start failed' }
    $pod = kubectl --context zoho-dmarc -n zoho-dmarc get pod -l app.kubernetes.io/instance=zoho-dmarc -o jsonpath='{.items[0].metadata.name}'
    if ($LASTEXITCODE -ne 0 -or -not $pod) { throw 'Collector Pod unavailable' }
    $items = kubectl --context zoho-dmarc -n zoho-dmarc exec $pod -c collector -- python -m zoho_dmarc backup-list | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Backup list failed' }
    if (-not $items) { throw 'No completed backup available' }
    foreach ($item in $items) {
        if ($item.file -notmatch '^dmarc-\d{8}T\d{6}Z\.sqlite$' -or $item.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid backup metadata' }
        $target = Join-Path $BackupDirectory $item.file
        if (-not (Test-Path -LiteralPath $target) -or (Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant() -ne $item.sha256) {
            $staging = $target + '.partial'
            # A relative destination avoids kubectl interpreting a Windows drive
            # colon as another remote Pod specification.
            Push-Location -LiteralPath $BackupDirectory
            try {
                kubectl --context zoho-dmarc -n zoho-dmarc cp -c collector "${pod}:/data/backups/$($item.file)" ("./" + $item.file + '.partial')
                if ($LASTEXITCODE -ne 0) { throw 'Backup transfer failed' }
            } finally { Pop-Location }
            if ((Get-FileHash -LiteralPath $staging -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw 'Backup transfer verification failed' }
            Move-Item -LiteralPath $staging -Destination $target -Force
        }
        [IO.File]::WriteAllText($target + '.sha256', $item.sha256 + "`n")
        $status = @{state='verified'; at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); file=$item.file}
    }
    Get-ChildItem -LiteralPath $BackupDirectory -File | Where-Object { $_.Name -match '^dmarc-\d{8}T\d{6}Z\.sqlite(\.sha256)?$' -and $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-30) } | ForEach-Object {
        if (-not [IO.Path]::GetFullPath($_.FullName).StartsWith($backupRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Retention target escaped backup directory' }
        Remove-Item -LiteralPath $_.FullName
    }
    Write-Output 'Completed backups exported and SHA256 verified.'
} catch {
    $status = @{state='failed'; at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); reason='host_start_or_export_failed'}
    Write-Error 'DMARC host startup or backup export failed. See private export-status.json.' -ErrorAction Continue
} finally {
    $payload = $status | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $BackupDirectory 'export-status.json'), $payload)
    if ($pod) {
        $payload | kubectl --context zoho-dmarc -n zoho-dmarc exec -i $pod -c collector -- python -c 'import json,sys,pathlib; data=json.load(sys.stdin); p=pathlib.Path("/data/backup-export.json"); t=p.with_suffix(".partial"); t.write_text(json.dumps(data)); t.replace(p)'
    }
    $env:CONTAINER_CONNECTION = $previousConnection
}
if ($status.state -ne 'verified') { exit 1 }
