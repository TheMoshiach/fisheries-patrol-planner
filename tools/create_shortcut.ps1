# Creates a "Fisheries Patrol Planner" shortcut on the Desktop that starts the app.
$project = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "Fisheries Patrol Planner.lnk"))
$shortcut.TargetPath = Join-Path $project "run.bat"
$shortcut.WorkingDirectory = $project
$shortcut.WindowStyle = 7   # start the console window minimised
$shortcut.Description = "Start the Fisheries Patrol Planner"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$shortcut.Save()
Write-Output "Shortcut created: $($shortcut.FullName)"
