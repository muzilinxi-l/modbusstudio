@echo off
chcp 65001 >nul
echo ========================================================
echo       正在将 Modbus Studio 打包为独立 EXE 可执行文件
echo ========================================================
echo.

cd /d "%~dp0"

:: 检查虚拟环境
if exist ".venv\Scripts\pyinstaller.exe" (
    set "PYINSTALLER=.venv\Scripts\pyinstaller.exe"
) else (
    set "PYINSTALLER=pyinstaller"
)

echo [1/3] 开始编译打包 (单文件模式，隐藏黑色控制台窗口)...
%PYINSTALLER% --noconfirm --clean --onefile --windowed --name "ModbusStudio" --icon "app.ico" --add-data "app.ico;." --collect-all "pymodbus" modbus_studio.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [错误] 打包失败，请检查上方日志。
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [2/3] 打包完成！
echo.
echo [3/3] 生成文件位置: dist\ModbusStudio.exe
echo.
echo ========================================================
echo   打包成功！您可以直接双击 dist\ModbusStudio.exe 运行
echo ========================================================
pause
