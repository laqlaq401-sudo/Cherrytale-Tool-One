@echo off
chcp 65001 >nul
title 米娅小助手 - 一键打包发布助手

cd /d "%~dp0"

echo =====================================================================
echo           米娅小助手 - 自动化打包发布中心
echo =====================================================================
echo.

set "PY_EXE="
rem 2026-10-06 双端统一到 Python 3.11（Chaquopy 只支持到 3.11）。
rem 优先级：本工程 .venv > 用户级 3.11 启动器 > PATH 兜底。
rem 注意：.venv 现在是指向 %USERPROFILE%\.venvs\cherrytale311 的目录联接（junction）。
if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "%LOCALAPPDATA%\Python\bin\python3.11.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Python\bin\python3.11.exe"
) else (
    set "PY_EXE=python"
)

for /f "delims=" %%v in ('"%PY_EXE%" tools/release.py current-version 2^>nul') do set "CUR_VER=%%v"
if "%CUR_VER%"=="" set "CUR_VER=未知"

for /f "delims=" %%v in ('"%PY_EXE%" tools/release.py calc-bump z 2^>nul') do set "PREVIEW_Z=%%v"
for /f "delims=" %%v in ('"%PY_EXE%" tools/release.py calc-bump y 2^>nul') do set "PREVIEW_Y=%%v"
for /f "delims=" %%v in ('"%PY_EXE%" tools/release.py calc-bump x 2^>nul') do set "PREVIEW_X=%%v"

echo [当前版本号] %CUR_VER%
echo.

echo ---------------------------------------------------------------------
echo 【步骤 1/2】请选择版本号更新方式 [x.y.z]：
echo   [1] 更新 Patch [z]  : 修复 Bug / 日常微调    [%CUR_VER% -^> %PREVIEW_Z%]
echo   [2] 更新 Minor [y]  : 新增功能 / 模块更新    [%CUR_VER% -^> %PREVIEW_Y%]
echo   [3] 更新 Major [x]  : 重大重构 / 架构升级    [%CUR_VER% -^> %PREVIEW_X%]
echo   [4] 手动输入版本号  : 自定义版本字符串 [例如 1.0.0]
echo   [0] 不更新版本号    : 使用当前版本 %CUR_VER% 直接进入打包
echo ---------------------------------------------------------------------
set /p BUMP_CHOICE="请输入选项 [0-4, 默认 1]: "
if "%BUMP_CHOICE%"=="" set "BUMP_CHOICE=1"

if "%BUMP_CHOICE%"=="1" goto do_bump_z
if "%BUMP_CHOICE%"=="2" goto do_bump_y
if "%BUMP_CHOICE%"=="3" goto do_bump_x
if "%BUMP_CHOICE%"=="4" goto do_bump_custom
if "%BUMP_CHOICE%"=="0" goto do_bump_skip

echo [ERROR] 无效选项！
goto on_error

:do_bump_z
echo.
echo 正在递增 Patch [z] 版本号...
"%PY_EXE%" tools/release.py bump-part z
if errorlevel 1 goto on_error
goto after_bump

:do_bump_y
echo.
echo 正在递增 Minor [y] 版本号...
"%PY_EXE%" tools/release.py bump-part y
if errorlevel 1 goto on_error
goto after_bump

:do_bump_x
echo.
echo 正在递增 Major [x] 版本号...
"%PY_EXE%" tools/release.py bump-part x
if errorlevel 1 goto on_error
goto after_bump

:do_bump_custom
echo.
set /p CUSTOM_VER="请输入新的版本号: "
if "%CUSTOM_VER%"=="" (
    echo [ERROR] 版本号不能为空！
    goto on_error
)
"%PY_EXE%" tools/release.py bump "%CUSTOM_VER%"
if errorlevel 1 goto on_error
goto after_bump

:do_bump_skip
echo.
echo [INFO] 保持当前版本号不变。
goto after_bump

:after_bump
echo.
for /f "delims=" %%v in ('"%PY_EXE%" tools/release.py current-version 2^>nul') do set "ACTIVE_VER=%%v"
if "%ACTIVE_VER%"=="" set "ACTIVE_VER=%CUR_VER%"

echo ---------------------------------------------------------------------
echo 【步骤 2/2】请选择打包目标 [当前发布版本: %ACTIVE_VER%]：
echo   [1] 一键双端完整封包 [PC 端 EXE/ZIP + Android 端 APK, 推荐]
echo   [2] 仅打包 PC 端 [EXE + 33张运行时配置表 + 发布 ZIP 归档包]
echo   [3] 仅打包 Android 端 [APK 安装包 + 资产同步]
echo   [4] 仅进行环境体检与核心回归单测 [不执行封包]
echo   [0] 取消并退出
echo ---------------------------------------------------------------------
set /p BUILD_CHOICE="请输入选项 [0-4, 默认 1]: "
if "%BUILD_CHOICE%"=="" set "BUILD_CHOICE=1"

if "%BUILD_CHOICE%"=="1" goto do_build_all
if "%BUILD_CHOICE%"=="2" goto do_build_pc
if "%BUILD_CHOICE%"=="3" goto do_build_android
if "%BUILD_CHOICE%"=="4" goto do_build_test
if "%BUILD_CHOICE%"=="0" goto do_build_cancel

echo [ERROR] 无效选项！
goto on_error

:do_build_all
echo.
echo 开始执行一键双端完整封包流水线...
"%PY_EXE%" tools/release.py all
if errorlevel 1 goto on_error
goto on_success

:do_build_pc
echo.
echo 开始执行 PC 端打包与 ZIP 压缩...
"%PY_EXE%" tools/release.py pc
if errorlevel 1 goto on_error
goto on_success

:do_build_android
echo.
echo 开始执行 Android 端资产同步与 Gradle 封包...
"%PY_EXE%" tools/release.py android
if errorlevel 1 goto on_error
goto on_success

:do_build_test
echo.
echo 开始执行环境依赖体检与快速回归单测...
"%PY_EXE%" tools/release.py check-env
if errorlevel 1 goto on_error
"%PY_EXE%" tools/release.py test
if errorlevel 1 goto on_error
goto on_success

:do_build_cancel
echo 操作已取消。
goto on_exit

:on_success
echo.
echo =====================================================================
echo 操作全部顺利完成！发布产物位于 dist/ 目录：
echo    PC ZIP 包 : dist\CherrytaleTool-%ACTIVE_VER%-windows-x64.zip
echo    Android   : dist\android\CherrytaleTool-%ACTIVE_VER%.apk
echo =====================================================================
goto on_exit

:on_error
echo.
echo 打包流程遇到错误，请查看上方输出信息。
echo.

:on_exit
echo.
echo 按任意键退出...
pause >nul
