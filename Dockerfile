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

# 系统依赖: libmagic1 供 unstructured 识别文件类型, curl 供 HEALTHCHECK
# libgomp1 是 torch 运行时的 OpenMP 依赖
# 换国内 Debian 源: deb.debian.org 在国内经常卡在索引下载(实测 10 分钟无进展)
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖层: torch 走 PyPI 镜像并跳过 GPU 依赖(--no-deps 不装 nvidia-*, 只做 CPU 推理)
# 说明: 若有外网, 可换成官方 CPU 源更快更小:
#   RUN pip install --index-url https://download.pytorch.org/whl/cpu torch
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX} \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY requirements.txt ./
RUN pip install --no-deps torch \
    && pip install -r requirements.txt

# 业务代码
COPY . .

EXPOSE 8000

# 首次加载两个模型约 20~60 秒, start-period 放宽
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
