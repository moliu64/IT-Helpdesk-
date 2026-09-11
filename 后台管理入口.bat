@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ================================================
echo   IT 运维工单智能体 - 后台管理入口
echo   工单管理、运行监视、知识库和访问记录
echo   地址：http://127.0.0.1:8788/backend
echo ================================================
call "%~dp0start_backend.bat" %*
endlocal
