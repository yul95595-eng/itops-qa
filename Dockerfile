# ============================================================
# IT 运维知识库助手 —— 应用镜像
#
# 分层策略:
#   1. 系统依赖(libmagic/curl) 与 pip 依赖各自成层, 改业务代码不触发重装
#   2. torch 固定装 CPU 版, 避免 pip 默认拉 CUDA 版(镜像会多出 2GB+)
#   3. BGE-M3 / bge-reranker 权重不打进镜像(2GB+), 运行时由 volume 挂载
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# libmagic1: unstructured 识别文件类型所需; curl: 供 HEALTHCHECK 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖层: 先装 CPU 版 torch, 再装 requirements(已满足的 torch 会被跳过)
# pip 走国内镜像源, 显著加速; 需要官方源可 --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX} \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY requirements.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r requirements.txt

# 业务代码
COPY . .

EXPOSE 8000

# 首次加载两个模型约 20~60 秒, start-period 放宽
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
