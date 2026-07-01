@echo off
REM qlit-flet Windows 打包脚本
REM 产物：dist\QLIT养成教育助手\QLIT养成教育助手.exe
setlocal
cd /d "%~dp0"

set APP_NAME=QLIT养成教育助手

echo ==^> 激活 venv
if exist .venv\Scripts\activate.bat (
  call .venv\Scripts\activate.bat
) else (
  echo X 未找到 .venv，请先：python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
  exit /b 1
)

REM ── 1. 校验 mitmdump ─────────────────────────────
echo ==^> 校验 mitmdump
where mitmdump >nul 2>nul
if errorlevel 1 (
  echo X 未找到 mitmdump，请先安装：
  echo    winget install mitmproxy
  echo    或 choco install mitmproxy
  exit /b 1
)
for /f "delims=" %%i in ('where mitmdump') do set MITM_PATH=%%i
echo   找到：%MITM_PATH%

REM ── 2. flet pack ─────────────────────────────────
echo ==^> flet pack
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

flet pack app.py ^
  --name "%APP_NAME%" ^
  --product-name "%APP_NAME%" ^
  --file-version "1.0.0.0" ^
  --product-version "1.0.0" ^
  --company-name "Qlit" ^
  --copyright "Copyright (c) qlit-flet contributors"

REM ── 3. 验证 ──────────────────────────────────────
echo ==^> 验证
if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
  echo X 打包失败：未找到 dist\%APP_NAME%\%APP_NAME%.exe
  exit /b 1
)

echo.
echo V 完成：%CD%\dist\%APP_NAME%\
echo.
echo 使用说明：
echo   1. 双击 %APP_NAME%.exe 启动（SmartScreen 拦就点"仍要运行"）
echo   2. 抓凭据会触发 netsh winhttp set proxy（需管理员）
echo   3. CA 证书用 certutil -addstore Root，不需要管理员
echo   4. 手动退出微信 ^> 重开 ^> 打开校园系统公众号 ^> 完成登录

endlocal
