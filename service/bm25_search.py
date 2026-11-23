# -*- coding: utf-8 -*-
# service/bm25_search.py
# FAQ 层 —— 三级缓存架构 + BM25 关键词匹配引擎
#
# 设计动机:
#   高频问题(占咨询量的大头)不应每次都走昂贵的向量检索 + LLM 生成。
#   本模块实现「三级缓存」递进, 从快到慢、从便宜到贵:
#
#     第1级 Redis 热点缓存:  按「分词归一化 key」缓存标准答案, 命中毫秒级返回, 零计算
#     第2级 MySQL FAQ 精确匹配: 未命中 Redis 时, 查结构化 FAQ 表(标准问法精确命中)
#     第3级 BM25 关键词匹配:  前两级都未命中, 用 jieba 分词 + BM25 打分找最相似 FAQ
#
#   三级都未命中 -> 返回 need_rag=True, 降级到 RAG 向量检索层。
#
#   缓存命中率: 本模块内置 CacheStats 计数器, 统计「被三级缓存接住的问题占比」,
#   即没有流到 LLM/向量检索的比例 —— 这是量化「缓存降本」的核心指标。
#
# 返回约定: (answer, need_rag)
#   - 命中:   (答案字符串, False)    False 表示不需要走 RAG 向量检索
#   - 未命中: (None, True)          True  表示需要降级到 RAG 向量检索层
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from base.config import config
from base.logger import logger
from store.redis_client import RedisClient
from store.faq_store import FAQStore


class CacheStats:
    """三级缓存命中统计器, 用于量化「缓存命中率」"""

    def __init__(self):
        self.total = 0          # 进入 FAQ 层的总请求数
        self.redis_hits = 0     # 第1级 Redis 命中数
        self.mysql_hits = 0     # 第2级 MySQL 精确匹配命中数
        self.bm25_hits = 0      # 第3级 BM25 关键词命中数

    def record(self, level):
        """记录一次命中, level 取值: redis/mysql/bm25"""
        self.total += 1
        if level == "redis":
            self.redis_hits += 1
        elif level == "mysql":
            self.mysql_hits += 1
        elif level == "bm25":
            self.bm25_hits += 1

    def record_miss(self):
        """记录一次未命中(会降级到 RAG)"""
        self.total += 1

    @property
    def cache_hit_rate(self):
        """缓存命中率 = 三级缓存接住的比例(没有流到 LLM/向量检索)"""
        if self.total == 0:
            return 0.0
        return (self.redis_hits + self.mysql_hits + self.bm25_hits) / self.total

    def report(self):
        """生成可读的命中率报告"""
        return {
            "total": self.total,
            "redis_hits": self.redis_hits,
            "mysql_hits": self.mysql_hits,
            "bm25_hits": self.bm25_hits,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
        }


def preprocess_text(text):
    """
    jieba 分词并转小写。
    为缓解口语/标准问法的分词差异(如"连不上" vs "连接/不上"), 额外补充整串作为兜底 token,
    使完全同字串的问题能稳定命中。
    """
    try:
        tokens = jieba.lcut(text.lower())
    except AttributeError:
        tokens = []
    # 去空白, 并追加整串(强化完全匹配)
    tokens = [t for t in tokens if t.strip()]
    if text and text.strip():
        tokens.append(text.strip().lower())
    return tokens


class BM25Search:
    """FAQ 三级缓存 + BM25 匹配: 分词索引 + 双判据阈值 + 命中率统计"""

    def __init__(self, redis_client=None, faq_store=None):
        self.logger = logger
        self.redis_client = redis_client if redis_client else RedisClient()
        self.faq_store = faq_store if faq_store else FAQStore()

        self.bm25 = None
        self.original_questions = None
        self.tokenized_questions = None

        # 三级缓存命中统计器
        self.stats = CacheStats()

        # 最近一次命中的"相对优势"和"命中的标准问题", 只读透出给上层做可信度判断,
        # 不参与本层判定。上层用它做实体一致性检查(见 pipeline 第2层)。
        self.last_margin = 1.0
        self.last_hit_question = ""
        # 归一化 query -> 命中的标准问题。缓存命中时靠它还原"当初命中的是哪条 FAQ",
        # 否则拿不到标准问题就无法判断是否错配。进程内有效, 冷启动后为空(可接受)。
        self._hit_map = {}

        self._load_data()

    def _load_data(self):
        """从 MySQL 加载 FAQ 问题并建立 BM25 索引(问题列表可缓存到 Redis)"""
        original_key = "it_ops_faq_questions"
        tokenized_key = "it_ops_faq_tokenized"

        self.original_questions = self.redis_client.get_data(original_key)
        tokenized = self.redis_client.get_data(tokenized_key)

        if not self.original_questions or not tokenized:
            self.original_questions = self.faq_store.fetch_all_questions()
            if not self.original_questions:
                self.logger.warning("未从 MySQL 加载到 FAQ 问题")
                return
            tokenized = [preprocess_text(q) for q in self.original_questions]
            # 缓存到 Redis, 下次免重查 MySQL
            self.redis_client.set_data(original_key, self.original_questions)
            self.redis_client.set_data(tokenized_key, tokenized)

        self.tokenized_questions = tokenized
        self.bm25 = BM25Okapi(self.tokenized_questions)
        self.logger.info(f"BM25 索引初始化完成, 共 {len(self.original_questions)} 条 FAQ")

    def search(self, query, min_score=5.0, margin_ratio=0.3):
        """
        三级缓存递进检索最匹配的 FAQ:
          第1级: Redis 热点缓存(分词归一化 key, 同义问题共享缓存)
          第2级: MySQL FAQ 表精确匹配(标准问法一字不差)
          第3级: BM25 关键词匹配(双判据: 绝对分 raw>=min_score 且 相对优势 margin>=margin_ratio)
        :return: (answer, need_rag)  命中返回(答案, False), 未命中返回(None, True)
        """
        if not query or not isinstance(query, str):
            return None, True

        self.last_margin = 1.0
        self.last_hit_question = ""

        # ---- 第1级: Redis 热点缓存(按归一化 key, 提升缓存命中率) ----
        norm_key = " ".join(preprocess_text(query))
        cache_answer = self.redis_client.get_answer(norm_key, normalized=True)
        if cache_answer:
            self.stats.record("redis")
            # 还原当初命中的是哪条 FAQ, 上层要靠它做实体一致性检查。
            # 先查进程内字典, 再查 Redis 里的伴生 key(冷启动后内存里没有)。
            # 两者都拿不到时保持空串, 上层会退化为"高置信"放行 —— 宁可漏检, 不能凭空调数据。
            self.last_hit_question = (
                self._hit_map.get(norm_key)
                or self.redis_client.get_answer(f"src:{norm_key}", normalized=True)
                or ""
            )
            self.logger.info(f"[三级缓存] 第1级 Redis 命中: {query}")
            return cache_answer, False

        # ---- 第2级: MySQL FAQ 精确匹配(标准问法) ----
        exact_answer = self.faq_store.fetch_answer(query.strip())
        if exact_answer:
            self.stats.record("mysql")
            self.redis_client.set_answer(norm_key, exact_answer, normalized=True)
            self.logger.info(f"[三级缓存] 第2级 MySQL 精确命中: {query}")
            return exact_answer, False

        # ---- 第3级: BM25 关键词匹配 ----
        try:
            query_tokens = preprocess_text(query)
            scores = self.bm25.get_scores(query_tokens)

            # 取 top1 和 top2
            order = np.argsort(scores)[::-1]
            best_idx = order[0]
            best_score = scores[best_idx]
            second_score = scores[order[1]] if len(order) > 1 else 0.0

            # 相对优势: top1 相对 top2 的领先幅度
            margin = (best_score - second_score) / best_score if best_score > 0 else 0.0

            # 完全字串命中(整串兜底 token 起作用)时, 直接放行
            # 必须加长度门槛: 否则"慢""卡""登录"这类两字词是任意 FAQ 的子串,
            # 会跳过分数和 margin 直接返回一条不相干的 FAQ —— 不兜底、直接答错。
            exact = (
                len(query.strip()) >= 8
                and query.strip().lower() in (self.original_questions[best_idx].strip().lower())
            )

            if (best_score >= min_score and margin >= margin_ratio) or exact:
                original_question = self.original_questions[best_idx]
                answer = self.redis_client.get_answer(original_question)
                if not answer:
                    answer = self.faq_store.fetch_answer(original_question)
                if answer:
                    self.stats.record("bm25")
                    # 透出本次命中的"相对优势"和"命中的标准问题", 供上层判断这条答案可不可信
                    # (只读, 不参与本层判定)。margin 小 = top1 只是微弱领先 top2, 常见于
                    # 共享了几个字面词的错配: 问"打印机没反应"命中"U 盘插上没反应", margin 仅 0.45。
                    self.last_margin = margin
                    self.last_hit_question = original_question
                    self._hit_map[norm_key] = original_question
                    # 伴生 key: 让"命中的标准问题"跟着答案一起缓存, 否则冷启动后
                    # 缓存命中的问题拿不到来源, 实体一致性检查就永远跳过。
                    self.redis_client.set_answer(
                        f"src:{norm_key}", original_question, normalized=True)
                    # 双写缓存: 原始 key + 归一化 key, 下次同义问题直接走第1级
                    self.redis_client.set_answer(original_question, answer)
                    self.redis_client.set_answer(norm_key, answer, normalized=True)
                    self.logger.info(
                        f"[三级缓存] 第3级 BM25 命中, raw={best_score:.2f} margin={margin:.2f} -> {original_question}"
                    )
                    return answer, False

            self.stats.record_miss()
            self.logger.info(
                f"BM25 未命中, raw={best_score:.2f} margin={margin:.2f} (需 raw>={min_score} 且 margin>={margin_ratio})"
            )
            return None, True
        except Exception as e:
            self.logger.error(f"BM25 搜索失败: {e}")
            return None, True


if __name__ == '__main__':
    search = BM25Search()
    tests = [
        "VPN 连不上",
        "密码忘了",
        "打印机卡纸",
        "电脑蓝屏",
        "这个月业绩怎么样",  # 无关问题, 应未命中
    ]
    for q in tests:
        answer, need_rag = search.search(q)
        print(f"\n[Q] {q}  -> need_rag={need_rag}")
        if answer:
            print(f"[A] {answer[:60]}...")
