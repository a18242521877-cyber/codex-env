@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py
  goto done
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run.py
  goto done
)
python run.py
:done
if errorlevel 1 (
  echo.
  echo 启动失败。请先安装 Python 3.11 或更高版本，并在此目录执行：
  echo   py -3 -m venv .venv
  echo   .venv\Scripts\activate
  echo   pip install -r requirements-windows.txt
  pause
)
