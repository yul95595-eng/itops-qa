# -*- coding: utf-8 -*-
# retrieval/vector_store.py
# Milvus 向量存储与混合检索 —— RAG 向量检索层
#
# 方案:
#   - 嵌入模型: BGE-M3(本地), 一次编码同时产出 1024 维稠密向量 + 稀疏向量
#   - 混合检索: 稠密(语义) + 稀疏(关键词) 双路召回, WeightedRanker 加权融合
#   - 精排: bge-reranker-large 对召回父块做交叉编码重排序
#   - 父子块: 子块入库检索, 命中后回溯 parent_content 交 LLM 生成答案
#   - 无 GPU 走 CPU, 模型本地加载, 不依赖外网
import hashlib
import os
import sys

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from milvus_model.hybrid import BGEM3EmbeddingFunction
from pymilvus import MilvusClient, DataType, AnnSearchRequest, WeightedRanker
from sentence_transformers import CrossEncoder
from langchain_core.documents import Document

from base.config import config
from base.logger import logger

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# retrieval 包目录
RETRIEVAL_PATH = os.path.dirname(os.path.abspath(__file__))


def _resolve_model_path(model_dir_name):
    """
    解析模型本地路径: 从本项目 retrieval/models/<dir> 加载。
    模型需预先下载到该目录(本项目自包含, 不依赖其他项目)。
    :param model_dir_name: 模型目录名, 如 'bge-m3' / 'bge-reranker-large'
    :return: 本地模型路径
    """
    local = os.path.join(RETRIEVAL_PATH, "models", model_dir_name)
    if not os.path.isdir(local):
        raise FileNotFoundError(
            f"未找到本地模型 {model_dir_name}, 请先下载到 {local}"
        )
    return local


class VectorStore:
    """Milvus 向量存储: 集合管理 + 文档入库 + 混合检索 + 重排序"""

    def __init__(
            self,
            collection_name=config.MILVUS_COLLECTION_NAME,
            host=config.MILVUS_HOST,
            port=config.MILVUS_PORT,
            database=config.MILVUS_DATABASE_NAME,
    ):
        self.collection_name = collection_name
        self.host = host
        self.port = port
        self.database = database
        self.logger = logger

        # 设备选择: GPU > MPS > CPU
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else
            "mps" if torch.mps.is_available() else "cpu"
        )
        self.logger.info(f"使用的设备: {self.device}")

        # 重排序模型
        reranker_path = _resolve_model_path("bge-reranker-large")
        self.reranker = CrossEncoder(reranker_path, device=self.device)

        # BGE-M3 嵌入模型
        m3_path = _resolve_model_path("bge-m3")
        self.embedding_function = BGEM3EmbeddingFunction(
            model_name_or_path=m3_path,
            use_fp16=(self.device.type == "cuda"),
            device=self.device,
        )
        self.dense_dim = self.embedding_function.dim["dense"]

        # 最近一次检索的 rerank 最高分(供 pipeline 判断"没检索到" vs "检索到但不相关")
        self.last_top_score = None
        # 最近一次检索命中的父文档文件名列表(去重保序, 供问答审计日志记录"命中哪几篇文档")
        self.last_top_filenames = None

        # 连接 Milvus
        uri = f"http://{self.host}:{self.port}"
        temp_client = MilvusClient(uri=uri)
        if self.database not in temp_client.list_databases():
            temp_client.create_database(self.database)
            self.logger.info(f"已创建数据库: {self.database}")
        temp_client.close()

        self.client = MilvusClient(uri=uri, db_name=self.database)
        self._create_or_load_collection()

           #Milvus 集合的 Schema 设计
    def _create_or_load_collection(self):
        if self.client.has_collection(self.collection_name):
            # 旧 Schema(无 doc_id 字段)不支持按文档删除/过滤, 自动 drop 重建。
            # 注意: drop 后需重新执行 scripts/ingest_all.py 灌入内置 Wiki 数据。
            desc = self.client.describe_collection(self.collection_name)
            field_names = [f.get("name") for f in desc.get("fields", [])]
            if "doc_id" not in field_names:
                self.client.drop_collection(self.collection_name)
                self.logger.warning(
                    f"检测到集合 {self.collection_name} 为旧 Schema(无 doc_id), 已自动重建。"
                    f"请重新执行 python scripts/ingest_all.py 灌入内置数据"
                )

        if not self.client.has_collection(self.collection_name):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=100)
            schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=65535)
            schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=self.dense_dim)
            schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field(field_name="parent_id", datatype=DataType.VARCHAR, max_length=100)
            schema.add_field(field_name="parent_content", datatype=DataType.VARCHAR, max_length=65535)
            schema.add_field(field_name="doc_id", datatype=DataType.VARCHAR, max_length=32)
            schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=50)
            schema.add_field(field_name="filename", datatype=DataType.VARCHAR, max_length=200)
            schema.add_field(field_name="timestamp", datatype=DataType.VARCHAR, max_length=50)

            index_params = self.client.prepare_index_params()
            #- 稠密向量建 IVF_FLAT 索引（倒排文件，适合中规模、内存有限）
            index_params.add_index(
                field_name="dense_vector",
                index_name="dense_index",
                index_type="IVF_FLAT",
                metric_type="IP",
                params={"nlist": 128},
            )
            #- 稀疏向量建 SPARSE_INVERTED_INDEX（稀疏倒排索引）
            index_params.add_index(
                field_name="sparse_vector",
                index_name="sparse_index",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
                params={"drop_ratio_build": 0.2},
            )
            #- doc_id 建标量倒排索引, 支持按文档高效删除/过滤
            index_params.add_index(
                field_name="doc_id",
                index_name="doc_id_index",
                index_type="INVERTED",
            )

            self.client.create_collection(
                collection_name=self.collection_name, schema=schema, index_params=index_params
            )
            logger.info(f"已创建集合 {self.collection_name}")
        else:
            logger.info(f"已加载集合 {self.collection_name}")

        self.client.load_collection(self.collection_name)

    def add_documents(self, documents):
        """
        将文档(子块)批量入库
        :param documents: LangChain Document 列表(来自 process_documents / process_single_file)

        主键设计: md5(doc_id + parent_id + 子块序号) —— 位置寻址:
          同一文档重复入库时主键稳定, upsert 原地覆盖(幂等);
          按文档删除时用 doc_id 过滤(milvus_admin.delete_doc_vectors)。
        (旧实现 md5(正文) 会导致: 内容修改后旧块残留成幽灵数据 / 跨文档相同段落互相覆盖)
        """
        texts = [doc.page_content for doc in documents]
        embeddings = self.embedding_function(texts)

        data = []
        for i, doc in enumerate(documents):
            chunk_key = (
                f"{doc.metadata.get('doc_id', '')}|"
                f"{doc.metadata.get('parent_id', '')}|"
                f"{doc.metadata.get('id', '')}"
            )
            text_hash = hashlib.md5(chunk_key.encode("utf-8")).hexdigest()

            # 稀疏向量: 矩阵 -> Milvus 字典格式 {非零索引: 权重}
            sparse_vector = {}
            row = embeddings["sparse"][[i]]
            for idx, value in zip(row.indices, row.data):
                sparse_vector[idx] = value

            data.append({
                "id": text_hash,
                "text": doc.page_content,
                "dense_vector": embeddings["dense"][i],
                "sparse_vector": sparse_vector,
                "parent_id": doc.metadata.get("parent_id", ""),
                "parent_content": doc.metadata.get("parent_content", ""),
                "doc_id": doc.metadata.get("doc_id", ""),
                "source": doc.metadata.get("source", "unknown"),
                "filename": doc.metadata.get("filename", ""),
                "timestamp": doc.metadata.get("timestamp", ""),
            })

        if data:
            self.client.upsert(collection_name=self.collection_name, data=data)
            logger.info(f"已插入/更新 {len(data)} 条数据到集合 {self.collection_name}")

    def _doc_from_hit(self, hit):
        entity = hit.get("entity", {})
        return Document(
            page_content=entity.get("text") or "",
            metadata={
                "doc_id": entity.get("doc_id") or "",
                "parent_id": entity.get("parent_id") or "",
                "parent_content": entity.get("parent_content") or "",
                "source": entity.get("source") or "unknown",
                "filename": entity.get("filename") or "",
                "timestamp": entity.get("timestamp") or "",
                "distance": hit.get("distance"),
            },
        )

    def _get_unique_parent_docs(self, sub_chunks):
        parent_contents = set()
        unique_docs = []
        for chunk in sub_chunks:
            parent_content = chunk.metadata.get("parent_content", chunk.page_content)
            if parent_content and parent_content not in parent_contents:
                unique_docs.append(Document(page_content=parent_content, metadata=chunk.metadata))
                parent_contents.add(parent_content)
        return unique_docs

    @staticmethod
    def _collect_filenames(docs):
        """提取命中父文档的 filename 列表(去重、保持顺序), 供审计日志使用"""
        filenames = []
        for doc in docs:
            name = (doc.metadata or {}).get("filename") or ""
            if name and name not in filenames:
                filenames.append(name)
        return filenames

    def hybrid_search_with_rerank(self, query, k=config.RETRIEVAL_K, source_filter=None):
        """
        混合检索(稠密+稀疏) + 重排序, 返回 Top-M 个父文档
        :param query: 用户查询
        :param k: 混合检索召回 Top-K 子块
        :param source_filter: 分类过滤(network/account/printer/software), None 不过滤
        :return: 重排序后的父文档列表
        """
        query_embeddings = self.embedding_function([query])
        query_dense_vector = query_embeddings["dense"][0]

        sparse_query_vector = {}
        row = query_embeddings["sparse"][[0]]
        for idx, value in zip(row.indices, row.data):
            sparse_query_vector[idx] = value

        filter_expr = f"source == '{source_filter}'" if source_filter else ""

        dense_request = AnnSearchRequest(
            data=[query_dense_vector],
            anns_field="dense_vector",
            param={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=k,
            expr=filter_expr,
        )
        sparse_request = AnnSearchRequest(
            data=[sparse_query_vector],
            anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {}},
            limit=k,
            expr=filter_expr,
        )

        ranker = WeightedRanker(1.0, 0.7)  # 稠密 1.0 : 稀疏 0.7

        res = self.client.hybrid_search(
            collection_name=self.collection_name,
            reqs=[dense_request, sparse_request],
            ranker=ranker,
            limit=k,
            output_fields=["text", "doc_id", "parent_id", "parent_content", "source", "filename", "timestamp"],
        )[0]

        sub_chunks = [self._doc_from_hit(hit) for hit in res]
        parent_docs = self._get_unique_parent_docs(sub_chunks)

        if len(parent_docs) < 2:
            # 父块不足 2 个, 跳过精排(没有 rerank 分数可言)。
            # 这里必须显式重置: 否则 last_top_score / last_top_filenames 会残留上一次查询的值,
            # 导致审计日志(以及 pipeline 的缺口归因)读到"穿越"的旧分数。
            self.last_top_score = None
            self.last_top_filenames = self._collect_filenames(parent_docs[:config.CANDIDATE_M])
            return parent_docs[:config.CANDIDATE_M]

        pairs = [[query, doc.page_content] for doc in parent_docs]
        scores = self.reranker.predict(pairs)
        ranked_parent_docs = [
            doc for _, doc in sorted(zip(scores, parent_docs), key=lambda x: x[0], reverse=True)
        ]
        ranked = ranked_parent_docs[:config.CANDIDATE_M]

        # 命中父文档文件名(去重保序): 重排结束后同步维护, 保证每次 search 都是本次查询的真实值
        self.last_top_filenames = self._collect_filenames(ranked)

        # 相关性阈值判断: 最高 rerank 分数低于阈值 -> 判定为未检索到相关内容, 返回空
        # 注意: scores 是 numpy 数组, 需用 len() 判空, 用 float() 转标量
        if len(scores) == 0:
            self.last_top_score = None
            self.last_top_filenames = []
            return []
        top_score = float(max(scores))
        self.last_top_score = top_score
        if top_score < config.RERANK_SCORE_THRESHOLD:
            logger.info(
                f"RAG 检索未命中(最高 rerank 分 {top_score:.2f} < 阈值 "
                f"{config.RERANK_SCORE_THRESHOLD}), 转为兜底"
            )
            return []

        return ranked


# ---------- 共享单例 ----------
# BGE-M3 + reranker 在 CPU 上加载一次约 20 秒、占内存 2GB+,
# 问答管线与上传入库服务共用同一实例, 避免重复加载。
_shared_instance = None


def get_shared_vector_store():
    """获取进程内共享的 VectorStore 单例(线程安全由调用方保证: 检索只读可并发, 入库走单线程队列)"""
    global _shared_instance
    if _shared_instance is None:
        _shared_instance = VectorStore()
    return _shared_instance


if __name__ == "__main__":
    # 演示: 连接 + 检索
    store = VectorStore()
    results = store.hybrid_search_with_rerank("VPN 连接不上怎么办")
    for i, doc in enumerate(results):
        print(f"[{i}] source={doc.metadata.get('source')} distance={doc.metadata.get('distance')}")
        print("   ", doc.page_content[:100])
