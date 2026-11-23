# -*- coding: utf-8 -*-
# scripts/demo.py
# 现场演示脚本 —— 一条命令展示三层检索路由的完整效果
#
# 用法:
#   python scripts/demo.py
#
# 作用:
#   面试演示时, 依次跑 4 类典型问题, 直观展示:
#   第1层 错误码精确匹配(零LLM) / 第2层 FAQ/BM25 / 第3层 RAG向量检索+LLM / 兜底
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.pipeline import QAPipeline


# 演示问题集: (问题, 期望命中的层, 说明)
DEMO_CASES = [
    ("数据库报错 error 1045 怎么办", "error_code", "错误码精确匹配 —— 零 LLM, 秒回标准处置"),
    ("ORA-12541 连接不上", "error_code", "错误码精确匹配 —— Oracle 监听错误"),
    ("密码忘了怎么重置", "faq", "FAQ/BM25 高频问答 —— 关键词匹配命中"),
    ("打印机卡纸了怎么办", "faq", "FAQ/BM25 高频问答"),
    ("堡垒机登录远程运维通道应该怎么使用", "rag", "RAG 向量检索 —— Milvus混合检索+rerank+千问生成"),
    ("无线AP漫游切换异常怎么排查", "rag", "RAG 向量检索 —— 长尾问题走语义检索"),
    ("今天天气怎么样", "fallback", "兜底 —— 知识库无相关内容, 如实告知不联网"),
]


def main():
    print("=" * 70)
    print("  企业内部 IT 运维知识库助手 —— 三层检索路由现场演示")
    print("=" * 70)
    print("  数据规模: 错误码 231 条 | FAQ 120 条 | Wiki 向量块 1405 个")
    print("  架构: 错误码精确匹配 → FAQ/BM25 → RAG向量检索 → 兜底")
    print("=" * 70)

    pipe = QAPipeline()

    for i, (question, expect, desc) in enumerate(DEMO_CASES, 1):
        print(f"\n{'─' * 70}")
        print(f"[{i}/{len(DEMO_CASES)}] 提问: {question}")
        print(f"      场景: {desc}")
        result = pipe.answer(question)
        layer = result.get("layer", "unknown")
        mark = "✅" if layer == expect else "⚠️"
        print(f"      {mark} 命中层: {layer}")
        answer = result.get("answer", "")
        # 只打印前 200 字, 避免刷屏
        preview = answer[:200] + ("..." if len(answer) > 200 else "")
        print(f"      回答: {preview}")
        if "sources" in result:
            print(f"      参考来源: {result['sources']}")

    print(f"\n{'=' * 70}")
    print("演示结束 ✅")
    print("=" * 70)


if __name__ == "__main__":
    main()
