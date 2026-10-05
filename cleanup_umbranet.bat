@echo off
chcp 65001 >nul
REM =====================================================================
REM  UmbraNet - аварийная очистка после удаления/зависания
REM
REM  Делает:
REM    1) удаляет автозапуск UmbraNet из Планировщика задач и HKCU\Run;
REM    2) завершает процессы UmbraNet/DPI/watchdog из этой папки;
REM    3) сбрасывает DNS Windows на авто (DHCP);
REM    4) пытается остановить WinDivert-драйвер, если он остался висеть.
REM
REM  Запускать от имени администратора.
REM =====================================================================
setlocal EnableExtensions

net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Запрос прав администратора...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo  === UmbraNet cleanup ===

echo.
echo  [1/4] Удаляю автозапуск...
schtasks /Delete /TN "UmbraNet_Autostart" /F >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "UmbraNet" /f >nul 2>&1

echo  [2/4] Завершаю процессы UmbraNet/DPI/watchdog...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = (Resolve-Path '%~dp0').Path.TrimEnd('\');" ^
  "$procs = Get-CimInstance Win32_Process | Where-Object {" ^
  "  ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) -or" ^
  "  ($_.CommandLine -and $_.CommandLine -like ('*' + $root + '*') -and ($_.Name -match '^(python|pythonw|winws|e1-spike)')) -or" ^
  "  ($_.Name -in @('winws.exe','e1-spike.exe') -and $_.ExecutablePath -and $_.ExecutablePath -like '*\UmbraNet\bin\*')" ^
  "};" ^
  "$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo  [3/4] Сбрасываю DNS на авто...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'};" ^
  "foreach ($a in $adapters) {" ^
  "  Set-DnsClientServerAddress -InterfaceAlias $a.Name -ResetServerAddresses -ErrorAction SilentlyContinue;" ^
  "}" >nul 2>&1

echo  [4/4] Останавливаю возможный WinDivert-драйвер...
for %%S in (WinDivert WinDivert14 WinDivert64) do (
    sc stop %%S >nul 2>&1
)

echo.
echo  Готово. Если папка UmbraNet всё ещё не удаляется — перезагрузите ПК и удалите её снова.
echo.
pause
endlocal
