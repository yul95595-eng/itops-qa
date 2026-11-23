# -*- coding: utf-8 -*-
# service/knowledge_ingest.py
# 知识文档异步入库服务 —— 上传接口的"后台工人"
#
# 设计要点:
#   1. 必须异步: CPU 上 BGE-M3 向量化是分钟级, 同步做接口必超时。
#      上传接口登记完(knowledge_docs 一条 pending 记录)立即返回, 工人慢慢干。
#   2. 必须串行: 单线程线程池(max_workers=1)。
#      两个入库任务并发跑, CPU 和内存会被两个 BGE-M3 一起打爆;
#      串行队列既保护机器, 也让进度条语义清晰(一次只有一个在处理)。
#   3. 状态落库: 每一步(解析/向量化/完成/失败)都写回 knowledge_docs,
#      前端轮询 GET /api/docs/{doc_id} 拿状态画进度条, 进程重启也不丢账。
#   4. 失败可重试: 失败只记 error_msg, 调 POST /api/docs/{doc_id}/reindex 重跑。
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.logger import logger

# 可恢复的中间状态(仅用于展示), 提交去重靠内存 in-flight 集合而非状态字段
RUNNING_STATUSES = ("pending", "parsing", "embedding")


class KnowledgeIngestService:
    """文档入库后台服务: 串行队列 + 状态机落库"""

    def __init__(self, vector_store=None):
        # 单线程池: 所有入库任务排队串行执行
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-ingest")
        # VectorStore(含 BGE-M3/reranker)用共享单例, 与问答管线共用一份模型
        self._vector_store = vector_store
        self._vs_lock = threading.Lock()
        # 在途任务集合(内存): 判断是否重复提交。
        # 不用 status 字段判断 —— 进程崩溃会留下卡住的 pending/embedding 状态,
        # 用状态判断会导致崩溃后的文档永远无法重试; 内存集合重启即清零, 无此问题。
        self._inflight = set()
        self._submit_lock = threading.Lock()

    def submit(self, doc_id):
        """
        提交一个入库任务(立即返回, 后台执行)。
        :param doc_id: 文档 ID
        :return: (ok, message)  ok=False 表示任务未被接收(不存在/已在队列中)
        """
        from store.doc_store import KnowledgeDocStore
        store = KnowledgeDocStore()
        try:
            doc = store.get(doc_id)
            if not doc:
                return False, f"文档不存在: {doc_id}"
            with self._submit_lock:
                if doc_id in self._inflight:
                    return False, "该文档已在入库队列中, 请等待完成"
                self._inflight.add(doc_id)
            # 置为 pending 并入队
            store.update_status(doc_id, "pending", error_msg="")
            self._executor.submit(self._run, doc_id)
            logger.info(f"入库任务已提交: {doc_id} ({doc['filename']})")
            return True, "任务已提交"
        except Exception as e:
            self._inflight.discard(doc_id)
            raise e
        finally:
            store.close()

    def _run(self, doc_id):
        """工人线程包装: 执行完(无论成败)把 doc_id 移出在途集合"""
        try:
            self._process(doc_id)
        finally:
            with self._submit_lock:
                self._inflight.discard(doc_id)

    def _get_vector_store(self):
        if self._vector_store is None:
            with self._vs_lock:
                if self._vector_store is None:
                    from retrieval.vector_store import get_shared_vector_store
                    self._vector_store = get_shared_vector_store()
        return self._vector_store

    def _process(self, doc_id):
        """真正的入库流程(在工人线程里跑): 解析 -> 切块 -> 向量化 -> 写 Milvus"""
        from store.doc_store import KnowledgeDocStore
        from retrieval.document_processor import process_single_file

        store = KnowledgeDocStore()
        try:
            doc = store.get(doc_id)
            if not doc:
                logger.error(f"入库中止: 文档不存在 {doc_id}")
                return
            file_path, category = doc["file_path"], doc["category"]

            # ---- 阶段1: 解析 + 切块 ----
            store.update_status(doc_id, "parsing")
            chunks = process_single_file(file_path, doc_id=doc_id, source=category)
            logger.info(f"[{doc_id}] 解析切块完成: {len(chunks)} 个子块")

            # ---- 阶段2: 向量化 + 写入 Milvus ----
            store.update_status(doc_id, "embedding")
            vector_store = self._get_vector_store()
            vector_store.add_documents(chunks)

            # ---- 阶段3: 完成 ----
            store.update_status(doc_id, "done", chunk_count=len(chunks), error_msg="")
            logger.info(f"[{doc_id}] 入库完成: {doc['filename']} -> {len(chunks)} 块")

        except Exception as e:
            logger.error(f"[{doc_id}] 入库失败: {e}")
            try:
                store.update_status(doc_id, "failed", error_msg=str(e))
            except Exception as e2:
                logger.error(f"[{doc_id}] 失败状态回写也失败: {e2}")
        finally:
            store.close()


# ---------- 进程级单例 ----------
_service_instance = None
_service_lock = threading.Lock()


def get_ingest_service():
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = KnowledgeIngestService()
    return _service_instance


if __name__ == "__main__":
    # 用法: python service/knowledge_ingest.py <doc_id>  (同步执行一次入库, 调试用)
    if len(sys.argv) > 1:
        svc = KnowledgeIngestService()
        svc._process(sys.argv[1])
    else:
        print("用法: python service/knowledge_ingest.py <doc_id>")
