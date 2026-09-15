@echo off
SET TARGET=C:\Data\vsCodeProjects\r33_master_slave\gui_ota_server
SET BUILDS=C:\Data\vsCodeProjects\r33_master_slave\builds

IF NOT EXIST "%TARGET%" mkdir "%TARGET%"

robocopy "%~dp0" "%TARGET%" /E /XF deploy.bat /XD __pycache__ .git
IF %ERRORLEVEL% GEQ 8 ( echo [ERROR] robocopy failed. & exit /b 1 )

IF EXIST "%BUILDS%\firmwareServer.py" (
    copy /Y "%BUILDS%\firmwareServer.py" "%TARGET%\firmwareServer.py"
    echo Copied firmwareServer.py from builds.
) ELSE (
    echo [WARN] firmwareServer.py not found in builds\ — place it manually.