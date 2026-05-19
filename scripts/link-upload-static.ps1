# Junction upload folders to the main TheNetworkerDev static tree (same files on disk).
$ErrorActionPreference = "Stop"
$MaintStatic = Join-Path $PSScriptRoot "..\app\static"
$MainStatic = Resolve-Path (Join-Path $PSScriptRoot "..\..\TheNetworkerDev\app\static")
$dirs = @("meeting_group_images", "event_images", "user_images")
foreach ($name in $dirs) {
    $link = Join-Path $MaintStatic $name
    $target = Join-Path $MainStatic $name
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
    }
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Write-Host ("OK (already linked): " + $name)
            continue
        }
        Write-Warning ("Skip " + $name + " - folder exists and is not a junction. Remove or rename it first.")
        continue
    }
    cmd /c mklink /J "$link" "$target" | Out-Null
    Write-Host ("Linked " + $name + " to " + $target)
}
