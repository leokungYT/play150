@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: =========================================================
:: Auto Update Script for Ranger Bot (Install to 'play150' folder)
:: =========================================================
:: URL: https://github.com/leokungYT/play150
:: =========================================================

echo.
echo ============================================
echo      Auto Update: Ranger Bot System
echo ============================================
echo.

:: Kill ADB and Python processes to prevent file locks
echo [PRE] Stopping ADB and Bot processes...
if exist "play150\adb\adb.exe" ("play150\adb\adb.exe" kill-server >nul 2>&1)
adb kill-server >nul 2>&1
taskkill /f /im adb.exe >nul 2>&1
:: adb ของโปรแกรมจำลองใช้ชื่ออื่น - ไม่ฆ่าด้วยจะล็อกไฟล์ในโฟลเดอร์ adb\ ไว้
taskkill /f /im HD-Adb.exe >nul 2>&1
taskkill /f /im nox_adb.exe >nul 2>&1
taskkill /f /im ld_adb.exe >nul 2>&1
taskkill /f /im adb_server.exe >nul 2>&1
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
timeout /t 3 /nobreak >nul

set "TARGET_FOLDER=play150"
set "REPO_URL=https://github.com/leokungYT/play150/archive/refs/heads/main.zip"
set "ZIP_NAME=play150_update.zip"
set "EXTRACT_DIR=update_temp"

:: 1. Create target folder if it doesn't exist (Always exists here)
if not exist "%TARGET_FOLDER%" (
    echo [INFO] Creating directory: %TARGET_FOLDER%
    mkdir "%TARGET_FOLDER%"
)

:: 2. Download the latest version (retry up to 3 times with curl, then fallback to PowerShell)
echo [1/5] Downloading latest version from GitHub...

set "DOWNLOAD_OK=0"

:: Try curl with retries
for /L %%i in (1,1,3) do (
    if !DOWNLOAD_OK! EQU 0 (
        echo [CURL] Attempt %%i/3...
        curl -k -L --retry 2 --retry-delay 3 --connect-timeout 15 "%REPO_URL%" -o "%ZIP_NAME%" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            if exist "%ZIP_NAME%" (
                set "DOWNLOAD_OK=1"
                echo [CURL] Download successful!
            )
        ) else (
            echo [CURL] Attempt %%i failed, retrying...
            timeout /t 3 /nobreak >nul
        )
    )
)

:: Fallback to PowerShell if curl failed
if !DOWNLOAD_OK! EQU 0 (
    echo [CURL] All attempts failed. Trying PowerShell fallback...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%REPO_URL%' -OutFile '%ZIP_NAME%' -UseBasicParsing -TimeoutSec 60; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
    if !ERRORLEVEL! EQU 0 (
        if exist "%ZIP_NAME%" (
            set "DOWNLOAD_OK=1"
            echo [PS] Download successful via PowerShell!
        )
    )
)

if !DOWNLOAD_OK! EQU 0 (
    echo.
    echo [ERROR] Download failed! Please check your internet connection.
    echo [TIP] Try: ipconfig /flushdns   then run this script again.
    pause
    exit /b 1
)

:: 3. Extract files
echo [2/5] Extracting files...
if exist "%EXTRACT_DIR%" rd /s /q "%EXTRACT_DIR%"
powershell -Command "Expand-Archive -Path '%ZIP_NAME%' -DestinationPath '%EXTRACT_DIR%' -Force"

:: Identify the source directory (GitHub zips name files like 'play150-main')
for /d %%f in ("%EXTRACT_DIR%\*") do set "SOURCE_FOLDER=%%f"

if not defined SOURCE_FOLDER (
    echo.
    echo [ERROR] Extraction failed! ZIP might be corrupt.
    pause
    exit /b 1
)

:: 4. Cleanup old img folder
echo [3/5] Cleaning old img folder (if needed)...
if exist "%TARGET_FOLDER%\img" rd /s /q "%TARGET_FOLDER%\img"

:: 5. Secure local backups + Copy new files from extracted zip 
echo [4/5] Copying new files to %TARGET_FOLDER%\...
echo ============================================
:: ลบโฟลเดอร์ backup จากไฟล์ที่โหลดมาก่อน เพื่อป้องกันไม่ให้เขียนทับของเก่า
if exist "%SOURCE_FOLDER%\backup" rd /s /q "%SOURCE_FOLDER%\backup"
if exist "%SOURCE_FOLDER%\backup-id" rd /s /q "%SOURCE_FOLDER%\backup-id"
if exist "%SOURCE_FOLDER%\lv5+" rd /s /q "%SOURCE_FOLDER%\lv5+"
if exist "%SOURCE_FOLDER%\logs" rd /s /q "%SOURCE_FOLDER%\logs"

:: adb\adb.exe มักโดนโปรแกรมจำลองล็อกไว้ (Sharing violation) - ถ้าเครื่องมี adb อยู่แล้ว
:: ข้ามไปเลย ไม่งั้นการก๊อปจะพังกลางทางแล้วไฟล์ที่เหลือ (เช่น img\) ไม่ถูกอัปเดต
if exist "%TARGET_FOLDER%\adb\adb.exe" (
    if exist "%SOURCE_FOLDER%\adb" (
        echo [SKIP] adb\ - มีอยู่แล้วในเครื่อง ข้ามการเขียนทับ
        rd /s /q "%SOURCE_FOLDER%\adb"
    )
)

:: robocopy แทน xcopy - เจอไฟล์ถูกล็อกจะ "ข้ามแล้วไปต่อ" ไม่หยุดทั้งก้อนแบบ xcopy
robocopy "%SOURCE_FOLDER%" "%TARGET_FOLDER%" /E /IS /NJH /NJS /NP /R:2 /W:1
set "RC=%ERRORLEVEL%"
echo ============================================

:: 6. Cleanup
echo [5/5] Cleaning up temporary files...
del /q "%ZIP_NAME%"
rd /s /q "%EXTRACT_DIR%"

echo.
if %RC% GEQ 8 (
    echo ============================================
    echo      [ERROR] Update INCOMPLETE ^(robocopy code %RC%^)
    echo ============================================
    echo  มีไฟล์ที่ก๊อปไม่ได้ ส่วนใหญ่เพราะโปรแกรมจำลอง/บอทยังเปิดอยู่
    echo  วิธีแก้: ปิดโปรแกรมจำลองทุกตัว + ปิดหน้าต่างบอท แล้วรันไฟล์นี้ใหม่
) else (
    echo ============================================
    echo      Update Successful ^(Saved in %TARGET_FOLDER%^)
    echo ============================================
)
echo.
pause
