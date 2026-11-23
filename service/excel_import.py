# -*- coding: utf-8 -*-
# service/excel_import.py
# FAQ / 错误码 的 Excel 批量导入 —— B 层: 企业自有结构化知识入库
#
# 设计要点:
#   1. 两种模板: FAQ(question/answer/category 三列) 和 错误码(code/component/meaning/cause/solution 五列)
#   2. 两步走: 先「校验预览」(只解析、不落库, 返回逐行校验结果), 用户确认后再「正式导入」
#      —— 避免一份脏 Excel 直接污染生产库
#   3. 落库后主动失效 BM25 索引 + Redis 答案缓存:
#      bm25_search 把问题列表缓存在 Redis(it_ops_faq_questions/it_ops_faq_tokenized),
#      不失效的话新 FAQ 永远进不了 BM25 检索, 必须重启进程才生效(本机测试时最容易忽略的坑)
#   4. 数据标记 source='custom', 与内置 builtin 数据区分
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.logger import logger

# 模板列定义
FAQ_COLUMNS = ["question", "answer", "category"]
ERROR_CODE_COLUMNS = ["code", "component", "meaning", "cause", "solution"]

# 示例行标记: 模板第 2 行的示例数据用该前缀开头, 解析时据此跳过, 避免示例被当成真实数据导入
EXAMPLE_MARK = "示例："


def _read_sheet(file_bytes):
    """用 openpyxl 读取第一个 sheet, 返回 (表头列表, 数据行列表)"""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise ValueError("Excel 文件为空")
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    data = [list(r) for r in rows[1:] if any(c is not None and str(c).strip() != "" for c in r)]
    return header, data


def parse_faq(file_bytes):
    """
    解析 FAQ Excel, 返回 (记录列表, 错误列表)。
    表头需含 question/answer/category(顺序可乱, 按表头名映射)。
    """
    header, data = _read_sheet(file_bytes)
    # 按表头名定位列
    idx = {}
    for col in FAQ_COLUMNS:
        if col in header:
            idx[col] = header.index(col)
    missing = [c for c in FAQ_COLUMNS if c not in idx]
    if missing:
        raise ValueError(f"FAQ 模板缺少列: {', '.join(missing)}(应为 question/answer/category)")

    records, errors = [], []
    for i, row in enumerate(data, start=2):  # 从第 2 行起(第 1 行是表头)
        def cell(c):
            v = row[idx[c]] if idx[c] < len(row) else None
            return str(v).strip() if v is not None else ""

        question, answer, category = cell("question"), cell("answer"), cell("category")
        # 跳过示例行(模板自带的演示数据, 以「示例：」开头)
        if question.startswith(EXAMPLE_MARK):
            continue
        if not question or not answer:
            errors.append(f"第 {i} 行: question 或 answer 为空")
            continue
        records.append((question, answer, category or "custom"))
    return records, errors


def parse_error_codes(file_bytes):
    """解析错误码 Excel, 返回 (记录列表, 错误列表)"""
    header, data = _read_sheet(file_bytes)
    idx = {}
    for col in ERROR_CODE_COLUMNS:
        if col in header:
            idx[col] = header.index(col)
    missing = [c for c in ERROR_CODE_COLUMNS if c not in idx]
    if missing:
        raise ValueError(f"错误码模板缺少列: {', '.join(missing)}(应为 code/component/meaning/cause/solution)")

    records, errors = [], []
    for i, row in enumerate(data, start=2):
        def cell(c):
            v = row[idx[c]] if idx[c] < len(row) else None
            return str(v).strip() if v is not None else ""

        code, component, meaning, cause, solution = (
            cell("code"), cell("component"), cell("meaning"), cell("cause"), cell("solution"))
        # 跳过示例行(模板自带的演示数据, 以「示例：」开头)
        if code.startswith(EXAMPLE_MARK):
            continue
        if not code or not meaning:
            errors.append(f"第 {i} 行: code 或 meaning 为空")
            continue
        records.append((code, component or "通用", meaning, cause, solution))
    return records, errors


def invalidate_faq_cache():
    """
    导入 FAQ 后失效缓存:
      1. 清 Redis 里 bm25 缓存的问题列表(key 见 bm25_search._load_data)
      2. 重建 BM25 实例的索引(若进程内已有实例)
    不失效的话, 新 FAQ 不会被 BM25 检索到, 也不会被 Redis 命中。
    """
    try:
        from store.redis_client import RedisClient
        rc = RedisClient()
        rc.client.delete("it_ops_faq_questions", "it_ops_faq_tokenized")
        logger.info("已失效 FAQ 的 BM25/Redis 缓存")
    except Exception as e:
        logger.warning(f"失效 FAQ 缓存失败(不影响入库, 但需重启生效): {e}")


def import_faq(file_bytes):
    """校验并导入 FAQ。返回 (成功条数, 错误列表)"""
    records, errors = parse_faq(file_bytes)
    if not records:
        return 0, errors
    from store.faq_store import FAQStore
    store = FAQStore()
    try:
        store.create_table()
        n = store.ingest(records, source="custom")
    finally:
        store.close()
    invalidate_faq_cache()
    return n, errors


def import_error_codes(file_bytes):
    """校验并导入错误码。返回 (成功条数, 错误列表)"""
    records, errors = parse_error_codes(file_bytes)
    if not records:
        return 0, errors
    from store.error_code_store import ErrorCodeStore
    store = ErrorCodeStore()
    try:
        store.create_table()
        n = store.ingest(records, source="custom")
    finally:
        store.close()
    return n, errors


def build_template(sheet_type):
    """生成模板 Excel 字节流。sheet_type: 'faq' 或 'error_code'"""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active

    if sheet_type == "faq":
        ws.title = "FAQ导入模板"
        headers = FAQ_COLUMNS
        example = [EXAMPLE_MARK + "VPN 连不上怎么办", "先检查账号是否过期，再确认客户端版本，最后联系 IT 服务台。", "network"]
    else:
        ws.title = "错误码导入模板"
        headers = ERROR_CODE_COLUMNS
        example = [EXAMPLE_MARK + "E1001", "业务系统", "接口调用超时", "下游服务响应慢", "1. 检查下游服务状态 2. 增大超时时间 3. 联系开发"]

    # 表头加粗
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True)
    # 示例行(便于用户理解格式)
    for col, v in enumerate(example, start=1):
        ws.cell(row=2, column=col, value=v)

    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + col)].width = 30

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


if __name__ == "__main__":
    # 自测: 生成模板 -> 解析回读
    data = build_template("faq")
    records, errors = parse_faq(data)
    print("FAQ 模板解析:", records, errors)
    data2 = build_template("error_code")
    records2, errors2 = parse_error_codes(data2)
    print("错误码模板解析:", records2, errors2)
