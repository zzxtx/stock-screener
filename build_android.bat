@echo off
setlocal
chcp 65001 >nul

rem ============================================================
rem  Build Android Debug APK
rem
rem  Auto-detects JDK 17 / Android SDK / Gradle 8.7 on this machine.
rem  Existing JAVA_HOME / ANDROID_HOME / GRADLE_HOME take priority.
rem
rem  Output: android\app\build\outputs\apk\debug\app-debug.apk
rem ============================================================

set "JDK="
set "SDK="
set "GRADLE="

echo.
echo === Stock Screener - Android build ===
echo.

rem ---------- 1. JDK ----------
if defined JAVA_HOME set "JDK=%JAVA_HOME%"
if defined JDK goto jdk_ok

if exist "%LOCALAPPDATA%\jdk-17.0.20.1+1\bin\java.exe" set "JDK=%LOCALAPPDATA%\jdk-17.0.20.1+1"
if defined JDK goto jdk_ok

for /d %%J in ("C:\Program Files\Java\jdk*") do if not defined JDK if exist "%%~J\bin\java.exe" set "JDK=%%~J"
if defined JDK goto jdk_ok

for /d %%J in ("C:\Program Files\Eclipse Adoptium\jdk*") do if not defined JDK if exist "%%~J\bin\java.exe" set "JDK=%%~J"
if defined JDK goto jdk_ok

for /d %%J in ("D:\Java\jdk*") do if not defined JDK if exist "%%~J\bin\java.exe" set "JDK=%%~J"
if defined JDK goto jdk_ok

echo [ERROR] JDK 17 not found.
echo         Install JDK 17, or set JAVA_HOME manually and retry.
goto done

:jdk_ok
set "JAVA_HOME=%JDK%"
echo [JDK]    %JAVA_HOME%

rem ---------- 2. Android SDK ----------
if defined ANDROID_HOME set "SDK=%ANDROID_HOME%"
if defined SDK goto sdk_ok

if defined ANDROID_SDK_ROOT set "SDK=%ANDROID_SDK_ROOT%"
if defined SDK goto sdk_ok

if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools" set "SDK=%LOCALAPPDATA%\Android\Sdk"
if defined SDK goto sdk_ok

if exist "D:\Android\Sdk\platform-tools" set "SDK=D:\Android\Sdk"
if defined SDK goto sdk_ok

echo [ERROR] Android SDK not found.
echo         Set ANDROID_HOME to the SDK folder and retry.
goto done

:sdk_ok
set "ANDROID_HOME=%SDK%"
echo [SDK]    %ANDROID_HOME%

rem ---------- 3. Gradle ----------
if defined GRADLE_HOME set "GRADLE=%GRADLE_HOME%\bin\gradle.bat"
if defined GRADLE goto gradle_ok

if exist "%USERPROFILE%\gradle-8.7\bin\gradle.bat" set "GRADLE=%USERPROFILE%\gradle-8.7\bin\gradle.bat"
if defined GRADLE goto gradle_ok

for /d %%G in ("%USERPROFILE%\gradle-*") do if not defined GRADLE if exist "%%~G\bin\gradle.bat" set "GRADLE=%%~G\bin\gradle.bat"
if defined GRADLE goto gradle_ok

for /d %%G in ("C:\Gradle\gradle-*") do if not defined GRADLE if exist "%%~G\bin\gradle.bat" set "GRADLE=%%~G\bin\gradle.bat"
if defined GRADLE goto gradle_ok

where gradle >nul 2>nul
if %errorlevel%==0 set "GRADLE=gradle"
if defined GRADLE goto gradle_ok

echo [ERROR] Gradle 8.7 not found.
echo         Set GRADLE_HOME to the Gradle folder and retry.
goto done

:gradle_ok
echo [GRADLE] %GRADLE%

rem ---------- 4. Build ----------
cd /d "%~dp0android"

call "%GRADLE%" assembleDebug --no-daemon
if not %errorlevel%==0 goto build_fail

echo.
echo [OK] APK built at:
echo      android\app\build\outputs\apk\debug\app-debug.apk
goto done

:build_fail
echo.
echo [FAILED] Build did not pass. Check the Gradle output above.
goto done

:done
echo.
pause
endlocal
