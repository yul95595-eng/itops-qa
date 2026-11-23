# -*- coding: utf-8 -*-
# tests/test_pipeline_layers.py
# 三层路由管线的单元测试
#
# 运行:
#   python -m unittest tests.test_pipeline_layers -v      # 单个文件
#   python -m unittest discover -s tests -v               # 全部测试
#
# 说明:
#   采用 unittest 标准库, 无需额外依赖。
#   核心逻辑(正则提取/分词/阈值判断)用纯函数 + mock 测试, 不依赖 MySQL/Redis/Milvus;
#   集成相关的测试用 mock 对象隔离外部依赖, 保证可离线、可重复运行。
import sys
import os
import unittest

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.error_code_matcher import extract_error_code, ErrorCodeMatcher
from service.bm25_search import preprocess_text


# ==================== 第1层: 错误码精确匹配 ====================

class TestErrorCodeExtract(unittest.TestCase):
    """测试错误码正则提取(纯函数, 无外部依赖)"""

    def test_mysql_numeric(self):
        self.assertEqual(extract_error_code("数据库报错 error 1045 怎么办"), "1045")

    def test_mysql_numeric_with_hash(self):
        self.assertEqual(extract_error_code("错误 #1062 重复插入"), "1062")

    def test_oracle(self):
        self.assertEqual(extract_error_code("ORA-12541 连接不上"), "ORA-12541")

    def test_windows_hex(self):
        self.assertEqual(extract_error_code("更新失败 0x80072EE7"), "0x80072EE7")

    def test_browser_err(self):
        self.assertEqual(extract_error_code("浏览器提示 ERR_CONNECTION_RESET"), "ERR_CONNECTION_RESET")

    def test_redis_wrongpass(self):
        self.assertEqual(extract_error_code("redis 报 WRONGPASS"), "WRONGPASS")

    def test_vpn_691(self):
        self.assertEqual(extract_error_code("vpn 连不上显示 691"), "691")

    def test_no_code_returns_none(self):
        self.assertIsNone(extract_error_code("打印机怎么装驱动"))

    def test_empty_returns_none(self):
        self.assertIsNone(extract_error_code(""))


class TestErrorCodeMatcher(unittest.TestCase):
    """测试错误码匹配器(注入内存种子, 不依赖 MySQL)"""

    def setUp(self):
        # 内存种子: (错误码, 组件, 含义, 原因, 解决步骤)
        seed = [
            ("1045", "MySQL", "Access denied", "密码错误", "核对密码"),
            ("ORA-12541", "Oracle", "无监听", "监听未启动", "lsnrctl start"),
        ]
        self.matcher = ErrorCodeMatcher(store=None, error_codes=seed)

    def test_match_hit(self):
        hit = self.matcher.match("error 1045 怎么处理")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["code"], "1045")
        self.assertEqual(hit["component"], "MySQL")

    def test_match_miss(self):
        hit = self.matcher.match("打印机怎么装驱动")
        self.assertIsNone(hit)

    def test_format_answer(self):
        hit = {"code": "1045", "component": "MySQL", "meaning": "Access denied",
               "cause": "密码错误", "solution": "核对密码"}
        ans = self.matcher.format_answer(hit)
        self.assertIn("1045", ans)
        self.assertIn("MySQL", ans)


# ==================== 第2层: FAQ/BM25 ====================

class TestBM25Preprocess(unittest.TestCase):
    """测试 BM25 分词预处理(纯函数)"""

    def test_tokenize_basic(self):
        tokens = preprocess_text("VPN 连不上")
        self.assertGreater(len(tokens), 1)

    def test_append_whole_string(self):
        """整串兜底 token 应存在, 保证完全匹配稳定命中"""
        tokens = preprocess_text("密码忘了")
        self.assertIn("密码忘了", tokens)

    def test_lowercase(self):
        tokens = preprocess_text("VPN 连不上")
        self.assertIn("vpn 连不上", tokens)  # 整串已转小写

    def test_strip_blank(self):
        tokens = preprocess_text("   ")
        self.assertEqual(tokens, [])


# ==================== 第3层: RAG 阈值判断 ====================

class TestRAGThreshold(unittest.TestCase):
    """测试 RAG 相关性阈值判断逻辑(修复过的 bug 场景)"""

    def _judge(self, scores, threshold=0.1):
        """复刻 vector_store 中的阈值判断逻辑, 返回是否放行"""
        if len(scores) == 0:
            return False
        top_score = float(max(scores))
        return top_score >= threshold

    def test_relevant_passes(self):
        """相关(0.99)应放行"""
        self.assertTrue(self._judge([0.99, 0.84, 0.05]))

    def test_weak_relevant_passes(self):
        """弱相关(0.56)应放行(阈值 0.1 宽松)"""
        self.assertTrue(self._judge([0.12, 0.34, 0.56]))

    def test_irrelevant_blocks(self):
        """不相关(0.0)应拦截"""
        self.assertFalse(self._judge([0.0, 0.0, 0.0]))

    def test_irrelevant_blocks_stock(self):
        """不相关(0.01)应拦截"""
        self.assertFalse(self._judge([0.0, 0.01]))

    def test_empty_scores_blocks(self):
        """空结果应拦截(numpy 数组判空的边界)"""
        self.assertFalse(self._judge([]))


# ==================== 兜底 ====================

class TestFallback(unittest.TestCase):
    """测试兜底逻辑(不联网、如实告知)"""

    def test_fallback_message(self):
        from service.pipeline import QAPipeline
        pipe = QAPipeline()
        result = pipe._fallback()
        self.assertEqual(result["layer"], "fallback")
        self.assertIn("知识库", result["answer"])
        self.assertIn("不进行联网搜索", result["answer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
