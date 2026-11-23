# -*- coding: utf-8 -*-
# retrieval/milvus_admin.py
# Milvus 轻量管理工具 —— 只连接 Milvus, 不加载嵌入/重排模型
#
# 使用场景:
#   删除文档时(DELETE /api/docs/{doc_id})只需要按 doc_id 过滤删除向量,
#   不需要加载 BGE-M3 / reranker(加载一次 20 秒 + 2GB 内存), 所以单独走这里。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymilvus import MilvusClient

from base.config import config
from base.logger import logger


def _get_client():
    uri = f"http://{config.MILVUS_HOST}:{config.MILVUS_PORT}"
    return MilvusClient(uri=uri, db_name=config.MILVUS_DATABASE_NAME)


def delete_doc_vectors(doc_id, collection_name=config.MILVUS_COLLECTION_NAME):
    """
    按文档 ID 删除其全部向量块(父子块都带 doc_id, 一条 expr 全删)。
    :param doc_id: 文档 ID(md5 前 16 位)
    :return: 是否成功
    """
    try:
        client = _get_client()
        if not client.has_collection(collection_name):
            return True
        client.delete(collection_name=collection_name, filter=f"doc_id == '{doc_id}'")
        client.close()
        logger.info(f"已删除文档 {doc_id} 的全部向量块")
        return True
    except Exception as e:
        logger.error(f"删除文档向量失败 {doc_id}: {e}")
        return False


def count_doc_vectors(doc_id, collection_name=config.MILVUS_COLLECTION_NAME, strong=True):
    """
    统计某文档当前的向量块数量(用于删除后校验/列表展示)。
    :param strong: 用 Strong 一致性读最新视图。删除后立刻计数时, 默认一致性级别
                   可能读到秒级延迟前的旧数据, 造成"删了却没删掉"的误报。
    """
    try:
        client = _get_client()
        if not client.has_collection(collection_name):
            return 0
        # 确保集合已加载(刚重建/重启后的集合可能未 load, load 是幂等操作)
        try:
            client.load_collection(collection_name)
        except Exception:
            pass
        kwargs = {"consistency_level": "Strong"} if strong else {}
        res = client.query(
            collection_name=collection_name,
            filter=f"doc_id == '{doc_id}'",
            output_fields=["count(*)"],
            **kwargs,
        )
        client.close()
        if res:
            return res[0].get("count(*)", 0)
        return 0
    except TypeError:
        # 旧版 pymilvus 的 query 不支持 consistency_level, 退回默认一致性
        try:
            client = _get_client()
            res = client.query(
                collection_name=collection_name,
                filter=f"doc_id == '{doc_id}'",
                output_fields=["count(*)"],
            )
            client.close()
            return res[0].get("count(*)", 0) if res else 0
        except Exception as e:
            logger.error(f"统计文档向量失败 {doc_id}: {e}")
            return -1
    except Exception as e:
        logger.error(f"统计文档向量失败 {doc_id}: {e}")
        return -1


if __name__ == "__main__":
    # 用法: python retrieval/milvus_admin.py <doc_id>
    if len(sys.argv) > 1:
        print("块数量:", count_doc_vectors(sys.argv[1]))
    else:
        print("用法: python retrieval/milvus_admin.py <doc_id>")
