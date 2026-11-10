@echo off
REM Launches Chrome with the JobSearchHeadless profile + CDP port 9111.
REM After logging into HH, leave this window open so the autopilot can attach.

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE=C:\Users\Misha\AppData\Local\JobSearchHeadless\User Data"

start "" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9111 ^
  --user-data-dir="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --window-size=1280,900 ^
  https://hh.ru/account/login
