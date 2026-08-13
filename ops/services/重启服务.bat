@echo off
cd /d "%~dp0"

echo ========================================
echo   Aion Chat - 重启服务
echo ========================================
echo.

echo [1/3] 停止当前进程...
:: 清理所有运行 main.py 的 Aion 后端进程（含 serve 循环已死但进程存活的僵尸）
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
ping -n 3 127.0.0.1 >nul
echo     已停止

echo [2/3] 启动服务...
schtasks /Run /TN "AionChatAutoStart" >nul 2>&1
if not errorlevel 1 goto :wait
echo     [ERROR] 计划任务 AionChatAutoStart 未找到
echo            请双击运行 ops\autostart\开机自启_安装.bat 注册任务
pause
exit /b 1

:wait
ping -n 5 127.0.0.1 >nul
echo [3/3] 验证端口...
set "NEW_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8080"') do (
    set "NEW_PID=%%a"
)
if defined NEW_PID goto :ok
echo     [WARN] 8080 尚未监听,启动可能失败
echo            请查看日志: aion-chat\data\logs\autostart.log
goto :done

:ok
echo     [OK] 服务已启动 PID=%NEW_PID%
echo     访问: http://localhost:8080

:done
echo.
echo ========================================
echo   日志: aion-chat\data\logs\autostart.log
echo ========================================
pause
