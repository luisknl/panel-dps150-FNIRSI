@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Panel DPS-150
cd /d "%~dp0"
python -c "import serial" 2>nul || python -m pip install --quiet pyserial
python panel_dps150.py %*
if errorlevel 1 (
  echo.
  echo Ocurrio un error. Revisa el mensaje de arriba.
  pause
)
