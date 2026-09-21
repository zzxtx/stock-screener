@echo off
chcp 65001 >nul
rem ============================================================
rem  构建 Android Debug APK
rem
rem  前置条件：
rem    - JDK 17，并设置 JAVA_HOME
rem    - Android SDK（platform 34 + build-tools 34.0.0），
rem      并设置 ANDROID_HOME（或 ANDROID_SDK_ROOT）
rem    - Gradle 8.7，加入 PATH，或设置 GRADLE_HOME
rem
rem  产物：android\app\build\outputs\apk\debug\app-debug.apk
rem ============================================================
setlocal

if not defined JAVA_HOME (
    echo [错误] 未设置 JAVA_HOME，请安装 JDK 17 并设置该环境变量。
    exit /b 1
)

if not defined ANDROID_HOME (
    if defined ANDROID_SDK_ROOT (
        set "ANDROID_HOME=%ANDROID_SDK_ROOT%"
    ) else (
        echo [错误] 未设置 ANDROID_HOME 或 ANDROID_SDK_ROOT。
        exit /b 1
    )
)

cd /d "%~dp0android"

where gradle >nul 2>nul
if %errorlevel%==0 (
    call gradle assembleDebug --no-daemon
    goto :check
)

if defined GRADLE_HOME (
    call "%GRADLE_HOME%\bin\gradle.bat" assembleDebug --no-daemon
    goto :check
)

echo [错误] 找不到 gradle。请将 Gradle 8.7 加入 PATH，或设置 GRADLE_HOME。
exit /b 1

:check
if errorlevel 1 (
    echo.
    echo [失败] 构建未通过，请检查上方 Gradle 输出。
    exit /b 1
)

echo.
echo [完成] APK 位于 android\app\build\outputs\apk\debug\app-debug.apk
endlocal
