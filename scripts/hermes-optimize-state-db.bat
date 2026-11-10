@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

rem ============================================================================
rem  Обслуживание state.db Hermes: компактная раскладка FTS + VACUUM.
rem
rem  Зачем. state.db = 1.54 ГБ, из них ~81% — поисковые индексы, и ~367 МБ это
rem  ДУБЛИ текста сообщений в messages_fts_content (183 МБ) и
rem  messages_fts_trigram_content (183 МБ). Индекс собран в старой, некомпактной
rem  раскладке (fts_optimize_available=1), поэтому Hermes сам ругается:
rem  STATE_DB_SIZE_WARN_BYTES = 1 ГиБ. Собственный апдейтер Hermes (07-10-2026)
rem  оценивает выигрыш так: «frees ~60% of state.db — about 0.9 GB».
rem  optimize-storage пересобирает индекс на компактную раскладку, дубли уходят.
rem
rem  Побочный эффект, ради которого это и делается: после каждого ГРЯЗНОГО
rem  выхода гейтвей на старте гоняет PRAGMA quick_check(1) по всей базе и
rem  блокируется на этом (`state_db_unclean_integrity_check`, лиз 900 с) —
rem  всё это время Telegram, api_server и cron не подняты. Чем меньше база,
rem  тем короче окно. Плюс именно из-за размера падал pre-flight десктопного
rem  апдейтера (см. skill hermes-ops).
rem
rem  Запускать ДВОЙНЫМ КЛИКОМ из Проводника (или из обычного cmd).
rem  НЕ запускать из-под агента WorkBuddy: там дерево процессов убивается в
rem  момент завершения вызова, и гейтвей останется лежать. Проверено: даже
rem  сырой subprocess.Popen с DETACHED_PROCESS не выживает, а
rem  CREATE_BREAKAWAY_FROM_JOB отклоняется с WinError 5.
rem
rem  Требование: Desktop-приложение Hermes (Hermes.exe) должно быть закрыто —
rem  оно тоже держит state.db, и оптимизация откажется работать.
rem
rem  Порядок: проверка -> замер -> stop -> копия -> optimize -> restart
rem  -> ПРОВЕРКА, что гейтвей поднялся (иначе fallback на "gateway start").
rem  Данные не теряются: optimize-storage документирован как resumable.
rem ============================================================================

set "HH=%LOCALAPPDATA%\hermes"
set "HERMES=%HH%\bin\hermes.exe"
set "DB=%HH%\state.db"
set "PYTHONIOENCODING=utf-8"
set "GWSTATE=unknown"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
if "%STAMP%"=="" set "STAMP=backup"
set "BACKUP=%HH%\backups\state.db.manual-%STAMP%"

echo.
echo ============================================================
echo   Hermes: оптимизация state.db
echo ============================================================
echo.

if not exist "%HERMES%" (
  echo   [ОШИБКА] Не найден hermes.exe:
  echo   %HERMES%
  echo.
  pause
  exit /b 1
)

echo [1/6] Проверяю, закрыт ли Desktop-приложение...
tasklist /FI "IMAGENAME eq Hermes.exe" 2>nul | find /I "Hermes.exe" >nul
if not errorlevel 1 (
  echo.
  echo   [СТОП] Запущено Desktop-приложение Hermes ^(Hermes.exe^).
  echo   Оно держит state.db, оптимизация откажется работать.
  echo   Закрой приложение полностью и запусти этот файл снова.
  echo.
  pause
  exit /b 1
)
echo       Desktop закрыт. ОК.

for %%A in ("%DB%") do set /a "SZ=%%~zA/1048576"
echo.
echo [2/6] Текущий размер state.db: %SZ% МБ

echo.
echo [3/6] Останавливаю гейтвей...
"%HERMES%" gateway stop
timeout /t 3 /nobreak >nul

echo.
echo [4/6] Резервная копия: %BACKUP%
if not exist "%HH%\backups" mkdir "%HH%\backups"
copy /Y "%DB%" "%BACKUP%" >nul
if errorlevel 1 (
  echo       [СТОП] Не удалось сделать копию. Ничего не менял, поднимаю гейтвей.
  "%HERMES%" gateway restart
  pause
  exit /b 1
)
echo       Копия готова.

echo.
echo [5/6] Оптимизирую. На 1.4 ГБ это займёт несколько минут, прогресс пойдёт ниже...
"%HERMES%" sessions optimize-storage -y
set "OPT=%ERRORLEVEL%"

echo.
echo [6/6] Поднимаю гейтвей обратно...
"%HERMES%" gateway restart
timeout /t 6 /nobreak >nul
call :gwstate
if /I "%GWSTATE%"=="running" goto :gw_done
echo       Не поднялся ^(%GWSTATE%^), пробую "gateway start"...
"%HERMES%" gateway start
timeout /t 6 /nobreak >nul
call :gwstate
:gw_done
echo       Состояние гейтвея: %GWSTATE%
"%HERMES%" gateway status

for %%A in ("%DB%") do set /a "SZ2=%%~zA/1048576"
echo.
echo ============================================================
echo   Было:  %SZ% МБ
echo   Стало: %SZ2% МБ
echo   Код возврата optimize-storage: %OPT%   ^(0 = успех^)
echo   Гейтвей: %GWSTATE%
echo   Копия: %BACKUP%
echo ============================================================
echo.
if /I not "%GWSTATE%"=="running" (
  echo   [!] ГЕЙТВЕЙ НЕ ПОДНЯЛСЯ. Запусти вручную из этого же окна:
  echo        hermes gateway restart
  echo.
)
echo   Проверь в Telegram, что бот отвечает ^(/kanban^).
echo   Копию можно удалить, когда убедишься, что всё в порядке.
echo.
pause
exit /b 0

:gwstate
rem Читаем gateway_state.json без пайпа (не ломается о quoting в for/f).
set "GWSTATE=unknown"
powershell -NoProfile -Command "try{(Get-Content -LiteralPath '%HH%\gateway_state.json' -Raw | ConvertFrom-Json).gateway_state}catch{'unknown'}" > "%TEMP%\hermes_gwstate.txt" 2>nul
if exist "%TEMP%\hermes_gwstate.txt" set /p GWSTATE=<"%TEMP%\hermes_gwstate.txt"
if "%GWSTATE%"=="" set "GWSTATE=unknown"
exit /b 0
