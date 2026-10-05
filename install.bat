@echo off
chcp 65001 >nul
REM =====================================================================
REM  UmbraNet - полный установщик зависимостей
REM
REM  Что делает:
REM    1. Проверяет CPython 3.10-3.14 для Windows x64
REM    2. Создаёт .venv рядом с программой
REM    3. Обновляет pip/setuptools/wheel
REM    4. Ставит ВСЕ компоненты DNS: PySide6, dnslib, requests, psutil,
REM       aioquic (DoQ), pynacl (DNSCrypt)
REM    5. DPI использует поставляемый bin\winws.exe (WinWS + WinDivert)
REM
REM  Важно:
REM    - chcp 65001 нужен, чтобы русские буквы не превращались в кракозябры.
REM    - PySide6 содержит очень длинные внутренние пути. Если программа лежит
REM      глубоко в Downloads/workspace/..., pip может упасть с Errno 2 / Long Path.
REM      Поэтому установщик при длинном пути сам перезапускается через короткий
REM      временный subst-диск.
REM =====================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "APP_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_CACHE_DIR=1"

REM ── Авто-обход Windows MAX_PATH для PySide6 ───────────────────────────
REM Если путь к папке длинный, делаем временный диск U:/V:/... на эту папку
REM и запускаем install.bat уже как U:\install.bat. Это короче, чем просить
REM пользователя включать LongPathsEnabled в реестре.
if not defined UMBRANET_SUBST_DONE (
    if not "!APP_DIR:~80,1!"=="" (
        echo.
        echo  [INFO] Путь к UmbraNet слишком длинный для установки PySide6:
        echo         !APP_DIR!
        echo  [INFO] Пробую временно запустить установку через короткий диск...
        echo.
        for %%D in (U V W X Y Z) do (
            subst %%D: "%~dp0" >nul 2>nul
            if not errorlevel 1 (
                set "UMBRANET_SUBST_DONE=1"
                pushd %%D:\
                call install.bat
                set "RC=!errorlevel!"
                popd
                subst %%D: /D >nul 2>nul
                exit /b !RC!
            )
        )
        echo  [ПРЕДУПРЕЖДЕНИЕ] Не удалось создать временный короткий диск.
        echo  Если установка PySide6 упадёт с Long Path, перенесите папку в C:\UmbraNet.
        echo.
    )
)

echo.
echo  =============================================
echo    UmbraNet ^| Полная установка зависимостей
echo  =============================================
echo.

REM ── Существующая venv имеет приоритет над системным Python ────────────
REM Проверяем именно тот интерпретатор, которым будем ставить пакеты.
REM В автоматической проверке CI UMBRANET_INSTALL_NO_PAUSE=1 отключает pause.
if exist ".venv\Scripts\python.exe" goto :check_venv
if exist ".venv" (
    echo  [ОШИБКА] Папка .venv существует, но её Python отсутствует.
    echo  Закройте UmbraNet, переименуйте .venv в .venv-old и повторите install.bat.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

REM ── Поиск Python для создания нового окружения ────────────────────────
set "SYS_PY="
set "SYS_PY_ARGS="
for /f "delims=" %%i in ('where py 2^>nul') do if not defined SYS_PY (
    set "SYS_PY=%%i"
    set "SYS_PY_ARGS=-3"
)
if not defined SYS_PY (
    for /f "delims=" %%i in ('where python 2^>nul') do if not defined SYS_PY set "SYS_PY=%%i"
)
if not defined SYS_PY (
    echo  [ОШИБКА] Python не найден.
    echo.
    echo  Установите обычный Python 3.10-3.14 для Windows x64:
    echo  https://www.python.org/downloads/
    echo  При установке включите "Add Python to PATH".
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

echo  Python для создания окружения:
"%SYS_PY%" %SYS_PY_ARGS% "%APP_DIR%tools\check_install_python.py"
if errorlevel 1 (
    echo.
    echo  [ОШИБКА] Выбранный Python не прошёл проверку выше. Установка не начата.
    echo  Нужен обычный CPython 3.10-3.14 для Windows x64.
    echo  При нескольких версиях можно заранее создать .venv нужным Python, например:
    echo    py -3.14 -m venv .venv
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

echo.
echo  Создание виртуального окружения .venv ...
"%SYS_PY%" %SYS_PY_ARGS% -m venv .venv
if errorlevel 1 (
    echo  [ОШИБКА] Не удалось создать .venv.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

:check_venv
set "PY=.venv\Scripts\python.exe"
echo.
echo  Используется Python из .venv:
"%PY%" "%APP_DIR%tools\check_install_python.py"
if errorlevel 1 (
    echo.
    echo  [ОШИБКА] Python внутри .venv несовместим или не запускается.
    echo  Смена системного Python не меняет уже созданную .venv.
    echo  Закройте UmbraNet, переименуйте .venv в .venv-old и запустите install.bat снова.
    echo  Нужен обычный CPython 3.10-3.14 для Windows x64.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

echo.
echo  Обновление pip / setuptools / wheel ...
REM Bootstrap-инструменты БЕЗ constraints.txt (пины там только для рантайма)
REM и НЕ роняем установку, если PyPI моргнул: setuptools/wheel для UmbraNet
REM не обязательны (все зависимости ставятся wheel-пакетами).
"%PY%" -m pip install --upgrade --no-cache-dir pip setuptools wheel
if errorlevel 1 (
    echo  [ПРЕДУПРЕЖДЕНИЕ] setuptools/wheel не обновились - это НЕ критично.
    echo                 Продолжаем установку зависимостей...
)

REM ── Основные и encrypted DNS зависимости ─────────────────────────────
echo.
echo  -----------------------------------------------
echo   Установка обязательных компонентов
echo   GUI + DNS + DoQ + DNSCrypt
echo  -----------------------------------------------
"%PY%" -m pip install --upgrade --no-cache-dir --only-binary=:all: -r "%APP_DIR%requirements.txt"
if errorlevel 1 (
    echo.
    echo  [ОШИБКА] Не удалось установить обязательные компоненты.
    echo.
    echo  Причина указана pip выше. Сохраните этот вывод для диагностики.
    echo  No matching distribution / ResolutionImpossible: проверьте версии пакетов
    echo  и Python. Убедитесь, что requirements.txt и constraints.txt из одной версии UmbraNet.
    echo  Если в ошибке есть "No matching distribution" или "from versions: none" - это сеть:
    echo  pip не видит PyPI. Проверьте интернет/VPN/антивирус и запустите
    echo  install.bat ещё раз.
    echo  Ошибка сети / TLS / прокси: проверьте доступ к PyPI и настройки сети.
    echo  Только при ошибке длинного пути / Errno 2 / 206 перенесите папку в C:\UmbraNet.
    echo  Текущий путь: "%CD%"
    echo.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

REM ── Проверка целостности зависимостей ────────────────────────────────
"%PY%" -m pip check
if errorlevel 1 (
    echo  [ОШИБКА] Проверка зависимостей не прошла. Сохраните сообщения pip выше.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

REM ── Проверка импортов ────────────────────────────────────────────────
echo.
echo  Проверка установленных модулей ...
"%PY%" -c "from PySide6 import QtCore, QtGui, QtWidgets; import dnslib, requests, psutil, aioquic, nacl, packaging; print('OK: all required modules imported')"
if errorlevel 1 (
    echo  [ОШИБКА] Модули установились не полностью. Повторите install.bat.
    echo  Если ошибка повторяется, удалите папку .venv и запустите install.bat заново.
    if not defined UMBRANET_INSTALL_NO_PAUSE pause
    exit /b 1
)

REM ── Итог ─────────────────────────────────────────────────────────────
echo.
echo  =============================================
echo   Готово!
echo   Установлены: UDP / DoH / DoT / DoQ / DNSCrypt + WinWS
echo   Запуск: start.bat
echo  =============================================
echo.
if not defined UMBRANET_INSTALL_NO_PAUSE pause
endlocal
exit /b 0
