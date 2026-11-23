# -*- coding: utf-8 -*-
# scripts/ingest_all.py
# 一键入库脚本: 串联「MySQL 错误码入库」+「Wiki 文档切块 -> Milvus 向量入库」
#
# 用法:
#   python scripts/ingest_all.py            # 幂等入库(Milvus 已有向量则跳过向量化)
#   python scripts/ingest_all.py --force    # 强制重新向量化
#
# 流程:
#   1. MySQL: 建库 -> 建 error_codes 表 -> 批量写入内置错误码
#   2. Milvus: 加载 data/wiki 的 Markdown -> 父子块切分 -> BGE-M3 向量化入库
import argparse
import os
import sys

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


def ingest_error_codes():
    """MySQL 错误码入库"""
    logger.info("=" * 60)
    logger.info("[1/3] 开始 MySQL 错误码入库")
    from store.error_code_store import ErrorCodeStore
    store = ErrorCodeStore()  # 内部已自动 ensure_database
    try:
        store.create_table()
        n = store.ingest()
        logger.info(f"错误码入库完成: 写入 {n} 条, 表内共 {store.count()} 条")
    finally:
        store.close()


def ingest_faq():
    """MySQL FAQ 入库"""
    logger.info("=" * 60)
    logger.info("[2/3] 开始 MySQL FAQ 入库")
    from store.faq_store import FAQStore
    store = FAQStore()
    try:
        store.create_table()
        n = store.ingest()
        logger.info(f"FAQ 入库完成: 写入 {n} 条, 表内共 {store.count()} 条")
    finally:
        store.close()


def _vector_count():
    """直接查 Milvus 集合行数(不加载嵌入模型); 任何异常按 0 处理, 触发全量入库"""
    try:
        from pymilvus import MilvusClient
        client = MilvusClient(uri=f"http://{config.MILVUS_HOST}:{config.MILVUS_PORT}")
        try:
            if not client.has_collection(config.MILVUS_COLLECTION_NAME):
                return 0
            stats = client.get_collection_stats(config.MILVUS_COLLECTION_NAME)
            return int(stats.get("row_count", 0) or 0)
        finally:
            client.close()
    except Exception as e:
        logger.warning(f"查询 Milvus 集合行数失败(按 0 处理): {e}")
        return 0


def ingest_wiki(force=False):
    """Wiki 文档切块 -> Milvus 向量入库"""
    logger.info("=" * 60)
    logger.info("[3/3] 开始 Wiki 文档向量入库")

    existing = _vector_count()
    if existing > 0 and not force:
        logger.info(f"Milvus 已有 {existing} 条向量, 跳过向量化(需要重灌请加 --force)")
        return

    from retrieval.document_processor import process_documents
    from retrieval.vector_store import VectorStore

    chunks = process_documents(config.WIKI_DIR)
    logger.info(f"文档切块完成: 共 {len(chunks)} 个子块")

    if not chunks:
        logger.warning("未切分出任何子块, 请检查 data/wiki 目录是否有文档")
        return

    vector_store = VectorStore()
    vector_store.add_documents(chunks)
    logger.info("Wiki 文档向量入库完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="一键入库: 错误码 + FAQ + Wiki 向量")
    parser.add_argument("--force", action="store_true",
                        help="Milvus 已有向量时仍强制重新向量化")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("IT 运维知识库助手 —— 一键入库开始")
    ingest_error_codes()
    ingest_faq()
    ingest_wiki(force=args.force)
    logger.info("=" * 60)
    logger.info("全部入库完成 ✅")
