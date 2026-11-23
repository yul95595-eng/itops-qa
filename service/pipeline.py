# -*- coding: utf-8 -*-
# service/pipeline.py
# 三层路由总管线 —— 把三个检索层串成完整的问答流程
#
# 路由顺序(由快到慢、由准到兜底):
#   第1层 错误码精确匹配: 正则提取错误码 -> 查 MySQL 精确命中 -> 秒回标准答案(零 LLM 零幻觉)
#   第2层 FAQ/BM25 匹配:   高频问题 BM25 关键词匹配 -> 命中即返回标准答案(带 Redis 缓存)
#   第3层 RAG 向量检索:    前两层未命中 -> Milvus 混合检索 + rerank -> 千问 LLM 生成答案
#
# 设计亮点:
#   - 能用结构化/关键词解决的, 绝不动 LLM 和向量, 省时省成本、答案更稳定
#   - 只有长尾问题才走完整的向量检索 + LLM 生成
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


class QAPipeline:
    """三层路由问答管线"""

    def __init__(self, matcher=None, bm25=None, vector_store=None, llm=None):        # 第1层: 错误码精确匹配
        self.matcher = matcher
        # 第2层: FAQ/BM25
        self.bm25 = bm25
        # 第3层: RAG 向量检索 + LLM
        self.vector_store = vector_store
        self.llm = llm

        # 延迟加载标记, 避免一次问答前全量初始化(模型加载慢)
        self._init_flags = {
            "matcher": matcher is not None,
            "bm25": bm25 is not None,
            "vector_store": vector_store is not None,
            "llm": llm is not None,
        }

    # ---------- 各层懒加载 ----------
    def _get_matcher(self):
        if self.matcher is None:
            from service.error_code_matcher import ErrorCodeMatcher
            from store.error_code_store import ErrorCodeStore
            self.matcher = ErrorCodeMatcher(store=ErrorCodeStore())
        return self.matcher

    def _get_bm25(self):
        if self.bm25 is None:
            from service.bm25_search import BM25Search
            self.bm25 = BM25Search()
        return self.bm25

    def _get_vector_store(self):
        if self.vector_store is None:
            # 用共享单例: 与上传入库服务共用一份模型, 避免重复加载(省 2GB 内存 + 20 秒)
            from retrieval.vector_store import get_shared_vector_store
            self.vector_store = get_shared_vector_store()
        return self.vector_store

    def _get_llm(self):
        if self.llm is None:
            from service.llm_client import LLMClient
            self.llm = LLMClient()
        return self.llm

    def _get_router(self):
        """意图识别路由(懒加载)"""
        if getattr(self, "router", None) is None:
            from service.intent_router import IntentRouter
            self.router = IntentRouter()
        return self.router

    def _record_gap(self, query, reason, layer="fallback", strategy="", min_score=None,
                    request_id=None):
        """记录知识缺口(答不上/检索到但不相关), 供运营补知识。失败不阻断问答主流程。"""
        try:
            from store.gap_store import get_gap_store
            get_gap_store().record(
                query, reason=reason, layer=layer, strategy=strategy, min_score=min_score,
                request_id=request_id,
            )
        except Exception as e:
            logger.warning(f"记录知识缺口失败(不影响问答): {e}")

    def _rag_retrieve(self, query):
        """
        RAG 检索(含意图路由多策略):
          1. 意图识别 -> direct / hyde / keyword
          2. HyDE 策略: 先生成假设文档再检索; 其余用原问题检索
        :return: (docs, strategy, reason)
        """
        router = self._get_router()
        route = router.route(query)
        strategy = route["strategy"]

        vector_store = self._get_vector_store()

        if strategy == "hyde":
            # HyDE: 用 LLM 扩写假设答案, 提升口语化短问召回
            llm = self._get_llm()
            try:
                hypothesis = llm.generate_hypothesis(query)
                docs = vector_store.hybrid_search_with_rerank(hypothesis)
                # HyDE 扩写后仍检索不到, 回退原问题直接检索
                if not docs:
                    logger.info("HyDE 检索为空, 回退 Direct 原问题检索")
                    docs = vector_store.hybrid_search_with_rerank(query)
                    strategy = "direct"
            except Exception as e:
                logger.warning(f"HyDE 扩写失败, 回退 Direct: {e}")
                docs = vector_store.hybrid_search_with_rerank(query)
                strategy = "direct"
        else:
            # direct / keyword: 都用原问题混合检索(关键词特征已在稀疏向量体现)
            docs = vector_store.hybrid_search_with_rerank(query)

        return docs, strategy, route["reason"]

    # ---------- 主入口 ----------
    def answer(self, query, history=None, request_id=None):
        """
        主问答入口, 依次尝试三层路由。
        :param query: 用户问题
        :param history: 历史消息列表 [{"role", "content"}, ...], 供多轮对话上下文
        :param request_id: 请求 id(路由层生成), 仅用于回传给接口层做审计追溯, 不参与任何判定
        :return: dict {layer, answer, ...}   layer 取值: error_code / faq / rag / fallback / chitchat / out_of_domain
        """
        if not query or not query.strip():
            return {"layer": "fallback", "answer": "请输入您遇到的问题。", "request_id": request_id}

        # 第-1层: 澄清轮还原 —— 上一轮是反问时, 用户回的"1"或"outlook 的"本身检索不了。
        # 回序号 -> 换成上一轮给出的候选文档标题; 补充描述 -> 拼上上一轮原问题。
        # 只改"拿去检索的 query", 不改展示给用户的内容。
        from service.slot_check import (
            CLARIFY_PREFIX, check as slot_check, entity_consistent, resolve_reply,
        )
        query = resolve_reply(query, history)

        # 槽位自检(<1ms, 纯词表)在这里算一次, 后面三处复用:
        #   FAQ 错配判定 / 第2.6层反问 / RAG 答案里的"假设声明"
        slot = slot_check(query)

        # 第0层: 域守卫 —— 闲聊/寒暄直接短路, 不进检索, 也不落知识盲区表
        from service.domain_guard import check as domain_check
        hit_chitchat, chitchat_reply = domain_check(query)
        if hit_chitchat:
            logger.info(f"[域守卫] 闲聊短路: {query}")
            return {"layer": "chitchat", "answer": chitchat_reply, "request_id": request_id}

        # 第1层: 错误码精确匹配
        try:
            matcher = self._get_matcher()
            hit = matcher.match(query)
            if hit:
                answer = matcher.format_answer(hit)
                return {
                    "layer": "error_code",
                    "answer": answer,
                    "code": hit["code"],
                    "component": hit["component"],
                    "request_id": request_id,
                }
        except Exception as e:
            logger.warning(f"错误码匹配层异常(跳过): {e}")

        # 第2层: FAQ/BM25 匹配(三级缓存)
        try:
            bm25 = self._get_bm25()
            answer, need_rag = bm25.search(query)
            if not need_rag and answer:
                # 命中之后先确认"命中的是不是同一个东西"。
                # 词面像不等于说的事相同: 问"打印机没反应"命中"U 盘插上没反应",
                # raw=7.53 / margin=0.45 双判据都过, 但答的是 U 盘。
                # margin 不能用来判这个 —— 实测"密码忘了"margin 只有 0.07 却是正确答案
                # (FAQ 里全是密码类问题, 天然分不开)。所以改用实体一致性。
                hit_q = getattr(bm25, "last_hit_question", "") or ""
                if not entity_consistent(query, hit_q) and slot["need_clarify"]:
                    logger.info(
                        f"[槽位自检] FAQ 疑似错配(命中《{hit_q}》), 改为反问澄清: {query}"
                    )
                    return {
                        "layer": "clarify",
                        "answer": self._build_clarify(slot),
                        "request_id": request_id,
                    }
                return {
                    "layer": "faq",
                    "answer": answer,
                    "cache_stats": bm25.stats.report(),
                    "request_id": request_id,
                }
        except Exception as e:
            logger.warning(f"FAQ/BM25 层异常(跳过): {e}")

        # 第2.5层: 域外判定 —— 错误码和 FAQ 都没命中, 即将进入最贵的 RAG(约 14.5 秒),
        # 此时才花一次轻量判定看是不是 IT 运维问题。
        # 放在这里而不是最前面, 是因为: 域内问题大部分被错误码/FAQ 接住或含 IT 术语,
        # 根本走不到这一行, 判定成本只落在"可能是域外"的问题上。
        # 域外问题直接拒答: 不烧重排算力, 也不落 knowledge_gaps(否则运营会去补一篇不该存在的文档)。
        from service.domain_guard import is_out_of_domain, DOMAIN_REPLY
        out_domain, via = is_out_of_domain(query, llm=self.llm)
        if out_domain:
            logger.info(f"[域判定] 域外问题拒答(via={via}): {query}")
            return {"layer": "out_of_domain", "answer": DOMAIN_REPLY, "request_id": request_id}

        # 第2.6层: 槽位自检 —— 用户在报障, 但没说清是哪个东西(如"插件坏了")。
        # 这时候检索一定是瞎猜: 稠密检索会把注意力摊到所有同类文档上, top1 分不低但可能答错
        # (实测 A04: "打印机没反应"命中"U 盘插上没反应", raw=7.53 / margin=0.45 双判据都过)。
        # 判据是纯词表(强实体/万能故障词/标题匹配), <1ms、不调模型、不查向量库;
        # 命中强实体、含具体现象词、或候选唯一时直接放行 —— 宁可漏问, 不可误拦。
        # (slot 已在入口算好, 这里只读)
        if slot["need_clarify"]:
            logger.info(f"[槽位自检] 反问澄清({slot['reason']}): {query}")
            # 故意不落 knowledge_gaps: 盲区表的判据是"能对应到一个补文档的动作",
            # 模糊提问对应不到具体文档, 记进去只会污染运营的判断。
            return {
                "layer": "clarify",
                "answer": self._build_clarify(slot),
                "request_id": request_id,
            }

        # 第3层: RAG 向量检索 + LLM 生成(含意图路由多策略)
        try:
            docs, strategy, reason = self._rag_retrieve(query)
            if not docs:
                vs = self._get_vector_store()
                top_score = getattr(vs, "last_top_score", None)
                if top_score is not None:
                    self._record_gap(
                        query, reason="rag_low", layer="rag",
                        strategy=strategy, min_score=round(top_score, 4),
                        request_id=request_id,
                    )
                else:
                    self._record_gap(query, reason="fallback", request_id=request_id)
                return self._fallback(request_id=request_id)
            llm = self._get_llm()
            contexts = [d.page_content for d in docs]
            answer = llm.generate_answer(query, contexts, history=history)
            answer = self._with_assumption(answer, slot)
            sources = list({d.metadata.get("source", "unknown") for d in docs})
            # 本次检索的真实分数与命中文档(只读透出给审计日志, 不参与任何判定)
            vs = self._get_vector_store()
            top_score = getattr(vs, "last_top_score", None)
            top_filenames = getattr(vs, "last_top_filenames", None)
            return {
                "layer": "rag",
                "answer": answer,
                "sources": sources,
                "strategy": strategy,
                "request_id": request_id,
                "top_score": round(top_score, 4) if top_score is not None else None,
                "top_filenames": top_filenames,
            }
        except Exception as e:
            # 基础设施异常(检索超时/Milvus 连不上/模型加载失败) ≠ 知识缺口。
            # 故意不传 query, 让它不落 knowledge_gaps —— 否则一次 Milvus 故障会被
            # 运营当成"知识库缺这批内容", 去补一堆根本不缺的文档。
            # 代价是这类故障的可见性只剩这条 error 日志, 排查时先看这里。
            logger.error(f"RAG 层异常(按基础设施故障处理, 不记知识缺口): {e}")
            return self._fallback(request_id=request_id)

    def _with_assumption(self, answer, slot):
        """槽位自检放行但只靠唯一候选时, 把假设说出来。

        "插件坏了"这种问题只有一个候选文档, 于是不问直接答 —— 但用户说的可能是别的插件。
        在答案前面挂一句假设, 用户能当场纠正, 比答完才发现答错代价小得多。
        """
        assumed = (slot or {}).get("assumed")
        if assumed:
            return f"（我按《{assumed}》理解您的问题，如果不对请补充说明。）\n\n{answer}"
        return answer

    def _build_clarify(self, slot):
        """澄清话术。

        开头必须是 CLARIFY_PREFIX: 下一轮 slot_check.resolve_reply 靠它认出"这是澄清轮",
        才能把用户回的"1"或"outlook 的"还原成能检索的 query。
        候选写死成 "1. 标题" 格式, 同样是为了下一轮能用正则把序号换回标题。
        """
        from service.slot_check import CLARIFY_PREFIX
        opts = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(slot["candidates"]))
        return (
            f"{CLARIFY_PREFIX}，请帮我确认是哪一种：\n\n{opts}\n\n"
            "回复序号即可；也可以直接补充具体的软件/设备名称和报错信息，我再给准确的步骤。"
        )

    def _fallback(self, query=None, request_id=None):
        """兜底: 知识库未检索到相关内容, 如实告知, 不联网、不编造

        只有 query 非空时才落 knowledge_gaps。调用方必须显式决定要不要记:
          - 检索正常返回但分数低 -> 传 query, 记缺口(真需求)
          - 链路异常/域外/闲聊     -> 不传 query, 不记(不是知识问题)
        """
        if query:
            self._record_gap(query, reason="fallback", request_id=request_id)
        return {
            "layer": "fallback",
            "answer": (
                "抱歉，知识库中暂时没有找到与您问题相关的信息，我无法回答。\n"
                "（本助手仅基于内部知识库离线回答，不进行联网搜索）\n\n"
                f"如需人工协助，可联系 IT 服务台：{config.CUSTOMER_SERVICE}"
            ),
            "request_id": request_id,
        }

    def answer_stream(self, query, history=None, request_id=None):
        """
        流式问答入口。先快速判断命中层, 再逐段产出文本。
        错误码/FAQ 层一次性产出(本身是秒回), RAG 层走 LLM 流式生成。
        :param query: 用户问题
        :param history: 历史消息列表 [{"role", "content"}, ...], 供多轮对话上下文
        :param request_id: 请求 id(路由层生成), 随 rag_meta 元信息透出, 供接口层做审计追溯
        :return: (layer, generator)  layer 取值: error_code / faq / rag / fallback / chitchat / out_of_domain
        """
        if not query or not query.strip():
            yield "fallback", "请输入您遇到的问题。"
            return

        # 第-1层: 澄清轮还原(与非流式一致)
        from service.slot_check import (
            CLARIFY_PREFIX, check as slot_check, entity_consistent, resolve_reply,
        )
        query = resolve_reply(query, history)

        # 槽位自检(<1ms)在这里算一次, 后面三处复用(与非流式一致)
        slot = slot_check(query)

        # 第0层: 域守卫 —— 闲聊/寒暄直接短路
        from service.domain_guard import check as domain_check
        hit_chitchat, chitchat_reply = domain_check(query)
        if hit_chitchat:
            logger.info(f"[域守卫] 闲聊短路(流式): {query}")
            yield "chitchat", chitchat_reply
            return

        # 第1层: 错误码精确匹配
        try:
            matcher = self._get_matcher()
            hit = matcher.match(query)
            if hit:
                answer = matcher.format_answer(hit)
                yield "error_code", answer
                return
        except Exception as e:
            logger.warning(f"错误码匹配层异常(跳过): {e}")

        # 第2层: FAQ/BM25 匹配
        try:
            bm25 = self._get_bm25()
            answer, need_rag = bm25.search(query)
            if not need_rag and answer:
                # 与非流式一致: 命中先做实体一致性检查, 主体不对就改走反问
                hit_q = getattr(bm25, "last_hit_question", "") or ""
                if not entity_consistent(query, hit_q) and slot["need_clarify"]:
                    logger.info(
                        f"[槽位自检] FAQ 疑似错配(流式, 命中《{hit_q}》), 改为反问: {query}"
                    )
                    yield "clarify", self._build_clarify(slot)
                    return
                yield "faq", answer
                return
        except Exception as e:
            logger.warning(f"FAQ/BM25 层异常(跳过): {e}")

        # 第2.5层: 域外判定(与非流式一致) —— 域外直接拒答, 不进 RAG、不落盲区表
        from service.domain_guard import is_out_of_domain, DOMAIN_REPLY
        out_domain, via = is_out_of_domain(query, llm=self.llm)
        if out_domain:
            logger.info(f"[域判定] 域外问题拒答(流式, via={via}): {query}")
            yield "out_of_domain", DOMAIN_REPLY
            return

        # 第2.6层: 槽位自检(与非流式一致) —— 报障但说不清对象, 先反问, 不进检索
        if slot["need_clarify"]:
            logger.info(f"[槽位自检] 反问澄清(流式, {slot['reason']}): {query}")
            yield "clarify", self._build_clarify(slot)
            return

        # 第3层: RAG 向量检索 + LLM 流式生成(含意图路由多策略)
        try:
            docs, strategy, reason = self._rag_retrieve(query)
            if not docs:
                # 区分两种缺口: 检索到但不相关(rerank低分) vs 根本没检索到
                vs = self._get_vector_store()
                top_score = getattr(vs, "last_top_score", None)
                if top_score is not None:
                    self._record_gap(
                        query, reason="rag_low", layer="rag",
                        strategy=strategy, min_score=round(top_score, 4),
                        request_id=request_id,
                    )
                else:
                    self._record_gap(query, reason="fallback", request_id=request_id)
                yield "fallback", self._fallback()["answer"]
                return
            llm = self._get_llm()
            contexts = [d.page_content for d in docs]
            sources = list({d.metadata.get("source", "unknown") for d in docs})
            # 本次检索的分数与命中文档(只读透出, 供接口层写审计日志), 拿不到时为 None
            vs = self._get_vector_store()
            top_score = getattr(vs, "last_top_score", None)
            top_filenames = getattr(vs, "last_top_filenames", None)
            yield "rag_meta", json.dumps({
                "sources": sources,
                "strategy": strategy,
                "request_id": request_id,
                "top_score": round(top_score, 4) if top_score is not None else None,
                "top_filenames": top_filenames,
            }, ensure_ascii=False)
            # 放行但只靠唯一候选时, 先把假设说出来(与非流式 _with_assumption 等价)
            if slot.get("assumed"):
                yield "rag", f"（我按《{slot['assumed']}》理解您的问题，如果不对请补充说明。）\n\n"
            for chunk in llm.generate_answer_stream(query, contexts, history=history):
                yield "rag", chunk
        except Exception as e:
            # 同非流式: 基础设施异常不记知识缺口。不传 query = 不落表
            logger.error(f"RAG 层异常(流式, 按基础设施故障处理, 不记知识缺口): {e}")
            yield "fallback", self._fallback()["answer"]


if __name__ == '__main__':
    pipe = QAPipeline()
    tests = [
        "数据库报错 error 1045 怎么办",   # 第1层 错误码
        "vpn 连不上显示 691",            # 第1层 错误码
        "密码忘了怎么重置",              # 第2层 FAQ
        "打印机卡纸了怎么办",            # 第2层 FAQ
        "公司新员工入职需要开通哪些账号",  # 第3层 RAG(长尾)
        "这个月业绩怎么样",              # 兜底
    ]
    for q in tests:
        print("\n" + "=" * 60)
        print(f"[提问] {q}")
        result = pipe.answer(q)
        print(f"[路由层] {result['layer']}")
        answer = result["answer"]
        print(f"[回答] {answer[:150]}{'...' if len(answer) > 150 else ''}")
