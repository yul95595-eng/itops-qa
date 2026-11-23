@echo off
chcp 65001 >nul
setlocal
title ITOps 运维智答 - 一键启动

echo ==================================================
echo    ITOps 运维智答 - 企业 IT 运维知识库助手
echo ==================================================
echo.

cd /d "%~dp0"

REM ---------- 1. 检查 Docker 是否安装 ----------
docker --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Docker
    echo.
    echo   请先安装 Docker Desktop:
    echo   https://www.docker.com/products/docker-desktop/
    echo.
    echo   安装完成后重启电脑, 再双击本文件。
    echo.
    pause
    exit /b 1
)
echo [1/5] Docker 已安装

REM ---------- 2. 检查 Docker 是否在运行 ----------
docker info >nul 2>&1
if errorlevel 1 (
    echo [错误] Docker 已安装但没有运行
    echo.
    echo   请先打开 Docker Desktop, 等左下角显示 "Engine running" 后重试。
    echo.
    pause
    exit /b 1
)
echo [2/5] Docker 正在运行

REM ---------- 3. 检查模型权重 ----------
if not exist "retrieval\models\bge-m3" (
    echo.
    echo [提示] 未找到模型权重 retrieval\models\bge-m3
    echo.
    echo   两种补齐方式（约 3GB，只需做一次）:
    echo   A. 解压别人给你的 models 压缩包, 放到 retrieval\models\ 下
    echo   B. 用命令下载:
    echo      pip install modelscope
    echo      modelscope download --model BAI/bge-m3 --local_dir retrieval/models/bge-m3
    echo      modelscope download --model BAI/bge-reranker-large --local_dir retrieval/models/bge-reranker-large
    echo.
    pause
    exit /b 1
)
echo [3/5] 模型权重已就绪

REM ---------- 4. 检查配置文件 ----------
if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo.
    echo [提示] 已为你生成 .env, 正在打开, 请把 DASHSCOPE_API_KEY 换成你自己的 Key 后保存。
    echo        保存后重新双击本文件即可。
    echo.
    notepad ".env"
    pause
    exit /b 1
)
echo [4/5] 配置文件已就绪

REM ---------- 5. 构建并启动 ----------
echo [5/5] 正在构建并启动全部服务...
echo        首次运行需要下载镜像并安装依赖, 约 5-15 分钟, 请勿关闭窗口。
echo.
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo [错误] 启动失败, 请把上方红色日志截图反馈。
    echo.
    pause
    exit /b 1
)

echo.
echo 正在等待服务就绪（首次加载模型约 1-2 分钟）...
set /a tries=0

:wait
set /a tries+=1
curl -s -o nul http://127.0.0.1:8000/health
if not errorlevel 1 goto ready
if %tries% geq 60 goto timeout
ping -n 4 127.0.0.1 >nul
goto wait

:ready
echo.
echo 服务已就绪, 正在灌入内置知识库...
echo （首次约 2-5 分钟; 已灌过会自动跳过, 只需几秒）
echo.
docker compose exec -T app python scripts/ingest_all.py
if errorlevel 1 (
    echo.
    echo [警告] 知识库灌入失败, 页面能打开但可能查不到答案。
    echo        可稍后手动重试:
    echo        docker compose exec app python scripts/ingest_all.py
    echo.
)
echo.
echo ==================================================
echo    启动成功
echo.
echo    访问地址: http://127.0.0.1:8000
echo    已自动打开浏览器
echo.
echo    关闭服务: 在本目录运行  docker compose down
echo ==================================================
echo.
start "" http://127.0.0.1:8000
pause
exit /b 0

:timeout
echo.
echo [错误] 等待超时（约 3 分钟）
echo   请查看应用日志定位原因:
echo   docker compose logs --tail 50 app
echo.
pause
exit /b 1
