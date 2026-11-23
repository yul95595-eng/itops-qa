# -*- coding: utf-8 -*-
# scripts/backfill_doc_registry.py
# 回填内置 Wiki 文档的登记记录。
#
# 背景:
#   data/wiki 下的文档是通过 scripts/ingest_all.py 直接灌进 Milvus 的,
#   没有走 /api/docs/upload, 因此 knowledge_docs 表里没有记录 ——
#   后果是问答审计日志里的 doc_version 永远为空(查不到文档版本)。
#
# 做法:
#   按「内容寻址」规则复算 doc_id(与 document_processor.compute_doc_id 完全一致),
#   把每篇文档登记进 knowledge_docs, version=1 / is_latest=1。
#   已登记过的直接跳过, 可重复执行(幂等)。
#
# 用法:
#   python scripts/backfill_doc_registry.py
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger
from retrieval.document_processor import compute_doc_id
from store.doc_store import KnowledgeDocStore

DOC_EXTS = (".md", ".txt", ".docx", ".pdf")


def full_md5(file_path):
    """完整文件 md5(与上传接口的 file_hash 口径一致, 32 位)"""
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main():
    store = KnowledgeDocStore()
    store.create_table()

    wiki_dir = config.WIKI_DIR
    if not os.path.isdir(wiki_dir):
        logger.error(f"Wiki 目录不存在: {wiki_dir}")
        return

    files = []
    for root, _dirs, names in os.walk(wiki_dir):
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in DOC_EXTS:
                files.append(os.path.join(root, name))
    logger.info(f"待登记文档 {len(files)} 篇 (目录: {wiki_dir})")

    added, skipped, failed = 0, 0, 0
    for path in files:
        filename = os.path.basename(path)
        try:
            doc_id = compute_doc_id(path)
            if store.get(doc_id):
                skipped += 1
                continue
            store.register(
                doc_id=doc_id,
                filename=filename,
                file_path=os.path.abspath(path),
                category="wiki",
                file_hash=full_md5(path),
                version=1,
            )
            added += 1
        except Exception as e:
            failed += 1
            logger.warning(f"登记失败 {filename}: {e}")

    logger.info(f"回填完成: 新增 {added} 篇, 已存在跳过 {skipped} 篇, 失败 {failed} 篇")
    logger.info(f"knowledge_docs 现有记录: {store.count()} 条")


if __name__ == "__main__":
    main()
