@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ============================================================
rem  Build Android debug APK
rem  Output: android\app\build\outputs\apk\debug\app-debug.apk
rem ============================================================

echo.
echo === Stock Screener - Android build ===
echo.

rem ---------- locate JDK ----------
set "JDK="
if defined JAVA_HOME if exist "%JAVA_HOME%\bin\javac.exe" set "JDK=%JAVA_HOME%"
if defined JDK goto jdk_ok

for /d %%D in ("%LOCALAPPDATA%\jdk*") do if exist "%%D\bin\javac.exe" set "JDK=%%D"
if defined JDK goto jdk_ok

for /d %%D in ("C:\Program Files\Java\jdk*") do if exist "%%D\bin\javac.exe" set "JDK=%%D"
if defined JDK goto jdk_ok

for /d %%D in ("C:\Program Files\Eclipse Adoptium\jdk*") do if exist "%%D\bin\javac.exe" set "JDK=%%D"
if defined JDK goto jdk_ok

for /d %%D in ("D:\Java\jdk*") do if exist "%%D\bin\javac.exe" set "JDK=%%D"
if defined JDK goto jdk_ok

echo [ERROR] JDK 17 not found. Set JAVA_HOME or install JDK 17.
goto done

:jdk_ok
set "JAVA_HOME=%JDK%"
echo [JDK] %JDK%

rem ---------- locate Android SDK ----------
set "SDK="
if defined ANDROID_HOME if exist "%ANDROID_HOME%\platforms" set "SDK=%ANDROID_HOME%"
if defined SDK goto sdk_ok
if defined ANDROID_SDK_ROOT if exist "%ANDROID_SDK_ROOT%\platforms" set "SDK=%ANDROID_SDK_ROOT%"
if defined SDK goto sdk_ok
if exist "%LOCALAPPDATA%\Android\Sdk\platforms" set "SDK=%LOCALAPPDATA%\Android\Sdk"
if defined SDK goto sdk_ok
if exist "D:\Android\Sdk\platforms" set "SDK=D:\Android\Sdk"
if defined SDK goto sdk_ok

echo [ERROR] Android SDK not found. Set ANDROID_HOME.
goto done

:sdk_ok
echo [SDK] %SDK%
call :write_local_props "%SDK%"

rem ---------- locate Gradle ----------
set "GRADLE="
if defined GRADLE_HOME if exist "%GRADLE_HOME%\bin\gradle.bat" set "GRADLE=%GRADLE_HOME%\bin\gradle.bat"
if defined GRADLE goto gradle_ok
if exist "%USERPROFILE%\gradle-8.7\bin\gradle.bat" set "GRADLE=%USERPROFILE%\gradle-8.7\bin\gradle.bat"
if defined GRADLE goto gradle_ok

for /d %%D in ("%USERPROFILE%\gradle-*") do if exist "%%D\bin\gradle.bat" set "GRADLE=%%D\bin\gradle.bat"
if defined GRADLE goto gradle_ok

for /d %%D in ("C:\Gradle\gradle-*") do if exist "%%D\bin\gradle.bat" set "GRADLE=%%D\bin\gradle.bat"
if defined GRADLE goto gradle_ok

where gradle >nul 2>nul
if %errorlevel%==0 set "GRADLE=gradle"
if defined GRADLE goto gradle_ok

echo [ERROR] Gradle 8.7 not found. Set GRADLE_HOME.
goto done

:gradle_ok
echo [GRADLE] %GRADLE%

rem ---------- build ----------
cd /d "%~dp0android"
call "%GRADLE%" assembleDebug
if not %errorlevel%==0 goto build_fail

echo.
echo [OK] APK: android\app\build\outputs\apk\debug\app-debug.apk
if not exist "%~dp0release" mkdir "%~dp0release"
copy /y "app\build\outputs\apk\debug\app-debug.apk" "%~dp0release\app-debug.apk" >nul
echo [OK] Copied to release\app-debug.apk
goto done

:build_fail
echo.
echo [FAILED] Gradle build failed. See messages above.
goto done

:done
echo.
pause
endlocal
exit /b

:write_local_props
set "SDKPATH=%~1"
set "SDKPATH=%SDKPATH:\=\\%"
> "%~dp0android\local.properties" echo sdk.dir=%SDKPATH%
exit /b
