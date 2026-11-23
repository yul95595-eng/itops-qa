# -*- coding: utf-8 -*-
# service/intent_router.py
# 意图识别与多策略路由 —— RAG 层的检索策略选择器
#
# 设计动机:
#   长尾问题形态差异大, 单一检索策略难以覆盖:
#   - 口语化/短问题: "蓝屏了咋办" —— 直接向量检索可能召回不准, 适合 HyDE 先扩写
#   - 具体故障名词: "ORA-12541 连接超时" —— 关键词特征明显, 适合关键词强化
#   - 标准陈述问题: 直接用原问题混合检索即可(Direct)
#
#   本模块用「纯规则 + 关键词」做意图识别(离线、零 LLM、零延迟), 避免为了选策略
#   反而多调一次大模型, 违背「省时省成本」的初衷。
#
# 三种检索策略(对齐业界 RAG 多策略路由的 Direct/HyDE 思路):
#   Direct   —— 原问题直接混合检索(默认)
#   HyDE     —— 先用 LLM 生成假设答案, 再用假设答案检索(提升口语化短问召回)
#   Keyword  —— 提取关键词做稀疏检索强化(具体名词类问题)
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.logger import logger


class IntentRouter:
    """意图识别器: 纯规则, 将长尾问题路由到合适的检索策略"""

    # 具体故障名词特征(这些词出现时, 关键词稀疏检索价值更高)
    KEYWORD_HINTS = [
        "错误", "报错", "失败", "无法", "不能", "连不上", "连接不上", "打不开",
        "提示", "显示", "卡", "慢", "掉线", "断网", "蓝屏", "黑屏", "死机",
        "病毒", "中毒", "重启", "闪退", "崩溃", "无响应",
    ]

    # 口语化/短问特征(问题太短或太口语, 适合 HyDE 扩写)
    SHORT_QUERY_LEN = 6  # 问题长度 <= 6 字视为短问

    def __init__(self):
        self.logger = logger

    def route(self, query):
        """
        识别意图并返回检索策略。
        :param query: 用户长尾问题
        :return: dict {"strategy": "direct"/"hyde"/"keyword", "reason": 说明}
        """
        q = (query or "").strip()
        if not q:
            return {"strategy": "direct", "reason": "空问题走默认策略"}

        # 1. 口语化短问 -> HyDE 扩写(优先级最高, 短问即使含关键词也先扩写补全语义)
        if len(q) <= self.SHORT_QUERY_LEN:
            return {"strategy": "hyde", "reason": f"问题过短({len(q)}字), HyDE 假设文档扩写"}

        # 2. 具体故障名词 -> 关键词强化
        if any(hint in q for hint in self.KEYWORD_HINTS):
            return {"strategy": "keyword", "reason": "含故障描述词, 关键词稀疏检索强化"}

        # 3. 默认 -> Direct
        return {"strategy": "direct", "reason": "标准陈述问题, 直接混合检索"}


if __name__ == "__main__":
    router = IntentRouter()
    tests = [
        "蓝屏了咋办",
        "VPN 连不上显示错误 691",
        "新员工入职需要开通哪些账号和权限",
        "电脑卡",
    ]
    for q in tests:
        r = router.route(q)
        print(f"[{q}] -> {r['strategy']} ({r['reason']})")
