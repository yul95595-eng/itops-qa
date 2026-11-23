# base/logger.py
# 统一日志配置 —— 输出到 stdout（和 print 同管道, 顺序一致）
import logging
import os
import sys
from base.config import config


def setup_logger(name="ItOpsQA", level=logging.INFO):
    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复添加 handler
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出到 stdout（与 print 顺序一致, 避免 PyCharm 里 stderr 渲染错位）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件日志
    os.makedirs(config.LOG_DIR, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(config.LOG_DIR, "app.log"), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
