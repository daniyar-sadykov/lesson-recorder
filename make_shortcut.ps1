# Создаёт на рабочем столе ярлык "Диктофон уроков".
# Запуск один раз: powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
# Цель ярлыка pythonw.exe, поэтому чёрного окна консоли при запуске нет.

$ErrorActionPreference = 'Stop'

$pythonw = 'C:\Python314\pythonw.exe'
$script = Join-Path $PSScriptRoot 'recorder.pyw'
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop 'Диктофон уроков.lnk'

if (-not (Test-Path $pythonw)) { throw "Не найден pythonw.exe: $pythonw" }
if (-not (Test-Path $script)) { throw "Не найден recorder.pyw: $script" }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $script + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$env:SystemRoot\System32\SndVol.exe,0"
$shortcut.Description = 'Запись урока: микрофон и звук компьютера в один файл'
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Output "Ярлык создан: $link"
Write-Output "Цель: $pythonw"
Write-Output "Скрипт: $script"
