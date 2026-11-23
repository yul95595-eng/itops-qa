# -*- coding: utf-8 -*-
# service/error_code_matcher.py
# 错误码精确匹配层 —— 本项目区别于通用 RAG 的核心亮点
#
# 设计动机:
#   运维场景中, 用户常直接抛出固定格式的错误码(如 "ORA-12541", "error 1045",
#   "0x80072EE7")。这类问题有官方标准答案, 且"精确匹配"远比向量检索可靠。
#   通用 RAG 会把错误码当普通词做向量检索, 容易搜不准;
#   本模块先用正则提取错误码, 再走结构化表精确查询, 命中即秒回, 零 LLM 调用。
import os
import re
import sys

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.logger import logger

# 常见错误码的正则模式（按优先级匹配）
_PATTERNS = [
    # Windows 十六进制错误码: 0x80072EE7
    re.compile(r'0x[0-9A-Fa-f]{8}', re.IGNORECASE),
    # 浏览器/网络错误: ERR_CONNECTION_RESET / ERR_NAME_NOT_RESOLVED
    re.compile(r'ERR_[A-Z_]+', re.IGNORECASE),
    # Redis 错误: WRONGPASS / LOADING / OOM / READONLY
    re.compile(r'\b(WRONGPASS|LOADING|READONLY|NOAUTH|OOM|MISCONF|BUSYKEY)\b', re.IGNORECASE),
    # Oracle 风格: ORA-12541（尾部不加 \b, 因为后面可能紧跟中文）
    re.compile(r'\bORA-\d{4,5}', re.IGNORECASE),
    # 带提示词的数字错误码: "error 1045" / "错误 691" / "#1045"
    re.compile(r'(?:error|错误|err)\s*[#]?(\d{3,5})', re.IGNORECASE),
    # 纯数字错误码(3~5位, 单独出现): "691" / "1045"
    re.compile(r'\b(\d{3,5})\b', re.IGNORECASE),
]


def extract_error_code(query: str) -> str:
    """
    从用户输入中提取错误码标识。
    :param query: 用户原始输入
    :return: 提取到的错误码(标准化后)字符串, 未匹配到返回 None
    """
    if not query:
        return None
    for pattern in _PATTERNS:
        m = pattern.search(query)
        if m:
            code = m.group(1) if m.lastindex else m.group(0)
            return _normalize_code(code)
    return None


def _normalize_code(code: str) -> str:
    """统一错误码标准化: 十六进制保留小写 0x 前缀, 其余大写"""
    code = code.strip()
    if code.lower().startswith("0x"):
        return "0x" + code[2:].upper()
    return code.upper()


def _load_seed_error_codes():
    """
    加载内置错误码种子数据 data/seed/error_code_seed.py。

    优先按包路径导入(from data.seed.error_code_seed)。
    若失败(例如从 service/ 目录单独运行、编辑器的 source root 没包含项目根、
    或环境中存在同名 data 包冲突), 则按文件绝对路径直接加载,
    完全不依赖 sys.path, 也不需要在此处写裸模块名 import。
    """
    try:
        from data.seed.error_code_seed import ERROR_CODES
        return ERROR_CODES
    except ImportError:
        pass

    # 兜底: 按文件路径加载, 彻底绕开包名/sys.path 问题
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    seed_path = os.path.join(root, "data", "seed", "error_code_seed.py")
    spec = importlib.util.spec_from_file_location("_it_ops_error_code_seed", seed_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ERROR_CODES


class ErrorCodeMatcher:
    """
    错误码精确匹配器: 正则提取 + 结构化表精确查询。
    数据源优先查 MySQL(ErrorCodeStore.query), 连接失败或无记录时回退到内置种子数据(内存)。
    """

    def __init__(self, store=None, error_codes=None):
        """
        :param store: ErrorCodeStore 实例。传入则走 MySQL 精确查询, 否则回退内存种子。
        :param error_codes: 内存种子列表, 作为 MySQL 不可用时的兜底。
        """
        self.store = store  # 可为 None(回退内存)
        # 内存兜底索引
        if error_codes is None:
            error_codes = _load_seed_error_codes()
        self._index = {}
        for rec in error_codes:
            code = _normalize_code(rec[0])
            self._index.setdefault(code, []).append(rec)

    def _query_mysql(self, code: str):
        """查 MySQL 表, 返回记录 dict 或 None(失败/无记录)"""
        if self.store is None:
            return None
        try:
            return self.store.query(code)
        except Exception as e:
            logger.warning(f"MySQL 错误码查询失败, 回退内存: {e}")
            return None

    def match(self, query: str):
        """
        匹配用户输入中的错误码。
        :param query: 用户输入
        :return: 命中则返回 dict(命中结果), 未命中返回 None
        """
        code = extract_error_code(query)
        if not code:
            return None

        # 1. 优先查 MySQL
        rec = self._query_mysql(code)
        if rec:
            return {
                "matched": True,
                "code": rec["code"],
                "component": rec["component"],
                "meaning": rec["meaning"],
                "cause": rec["cause"],
                "solution": rec["solution"],
                "backend": "mysql",
            }

        # 2. 回退内存种子
        if code in self._index:
            r = self._index[code][0]
            return {
                "matched": True,
                "code": r[0],
                "component": r[1],
                "meaning": r[2],
                "cause": r[3],
                "solution": r[4],
                "backend": "memory",
            }
        return None

    def format_answer(self, hit: dict) -> str:
        """把命中结果格式化成给用户的答案文本"""
        return (
            f"检测到错误码【{hit['code']}】（{hit['component']}）\n"
            f"含义：{hit['meaning']}\n"
            f"常见原因：{hit['cause']}\n"
            f"解决步骤：\n{hit['solution']}"
        )


if __name__ == '__main__':
    # 接入 MySQL 数据源(连接失败自动回退内存种子)
    try:
        from store.error_code_store import ErrorCodeStore
        store = ErrorCodeStore()
    except Exception as e:
        print(f"MySQL 不可用, 回退内存种子: {e}")
        store = None

    matcher = ErrorCodeMatcher(store=store)
    tests = [
        "数据库报错 error 1045 怎么办",
        "mysql 1049 unknown database",
        "电脑更新失败 0x80072EE7",
        "浏览器提示 ERR_CONNECTION_RESET",
        "redis 报 WRONGPASS",
        "vpn 连不上显示 691",
        "帮我查一下 ORA-12541",
        "打印机怎么装驱动",   # 无错误码, 应返回 None
    ]
    for q in tests:
        hit = matcher.match(q)
        if hit:
            print(f"[命中] {q!r}\n  -> {hit['code']} ({hit['component']}) [backend={hit['backend']}]: {hit['meaning']}")
        else:
            print(f"[未命中] {q!r}")
    if store:
        store.close()
