@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ================================================
echo   IT 运维工单智能体 - 一键启动入口
echo   用户入口：http://127.0.0.1:8787/
echo   后台管理：http://127.0.0.1:8787/backend
echo ================================================
call "%~dp0start.bat" %*
endlocal
