# -*- coding: utf-8 -*-
# store/qa_log.py
# 问答审计日志 —— 每次问答落一行, 让"这一次到底发生了什么"可事后追溯(MySQL)
#
# 设计动机:
#   用户反馈"刚才那题答错了"时, 光看应用日志很难还原现场:
#   走的哪一层? RAG 检索最高分多少? 命中了哪几篇文档? 最终答了什么?
#   把这几项集中落一张表, 就能按 request_id 精确回查某一次问答, 也能按层/时间做统计归因
#   (是"检索没检到"还是"检到了但模型答歪了")。
#
# 关键设计:
#   - request_id: 由路由层生成并回传前端, 是串起"用户反馈 → 审计记录"的唯一线索
#   - answer 只存 md5 摘要, 不存全文(答案里可能含用户数据, 全文进审计表有合规风险)
#   - 只记不改: 本模块不参与任何业务判定(阈值/路由/检索参数一概不碰),
#     所有写入都用 try/except 包住, 失败只 logger.warning —— 绝不能因为写日志失败影响正常问答
import hashlib
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dbutils.pooled_db import PooledDB

from base.config import config
from base.logger import logger

TABLE = "qa_audit_log"

# 连接池(懒创建): 首次写入时初始化; 库由其他 store(启动时) / docker 初始化脚本创建
_pool = None
# 建表标记: 首次写入前确保表存在, 之后不再重复执行 DDL
_table_ready = False


def _get_pool():
    """获取(懒创建)MySQL 连接池, 与 gap_store / error_code_store 同一套连接参数"""
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=5,
            mincached=1,
            maxcached=3,
            blocking=True,
            ping=1,  # 每次借连接自动检活, 防止 Web 长连接下拿死连接
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            charset="utf8mb4",
        )
    return _pool


@contextmanager
def _get_db_cursor():
    """借还连接: 正常提交, 异常回滚, 最后统一关闭游标与连接"""
    conn = None
    cursor = None
    try:
        conn = _get_pool().connection()
        cursor = conn.cursor()
        yield conn, cursor
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def ensure_table():
    """
    建表(幂等)。失败只记 warning 并返回 False, 不抛异常 —— 审计日志不能阻断问答主流程。
    :return: True 表已就绪 / False 建表失败(或连不上 MySQL)
    """
    global _table_ready
    if _table_ready:
        return True

    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE}` (
        `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
        `request_id` VARCHAR(32) DEFAULT NULL COMMENT '请求id(路由层生成, 回传前端)',
        `query` TEXT COMMENT '用户原始问题',
        `layer` VARCHAR(16) DEFAULT NULL COMMENT '最终命中层: error_code/faq/rag/fallback',
        `strategy` VARCHAR(16) DEFAULT NULL COMMENT 'RAG检索策略: direct/hyde/keyword',
        `top_score` DECIMAL(6,4) DEFAULT NULL COMMENT 'rerank最高分(未精排/非RAG层为NULL)',
        `top_filenames` VARCHAR(512) DEFAULT NULL COMMENT '命中父文档文件名(英文逗号拼接,最多8个)',
        `doc_version` INT DEFAULT NULL COMMENT '命中文档(top1)的版本号, 用于追责时确认依据的是哪一版',
        `answer_snippet` VARCHAR(1200) DEFAULT NULL COMMENT '答案正文前1200字(事后还原用)',
        `answer_hash` VARCHAR(32) DEFAULT NULL COMMENT '答案md5摘要(用于去重/校验)',
        `source` VARCHAR(32) DEFAULT 'web' COMMENT '入口来源',
        `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',
        KEY `idx_created` (`created_at`),
        KEY `idx_layer` (`layer`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='问答审计日志(每次问答一条)';
    """
    try:
        with _get_db_cursor() as (conn, cursor):
            cursor.execute(ddl)
        # 兼容已存在的旧表: 补 doc_version 列(1060=列已存在, 跳过)
        try:
            with _get_db_cursor() as (conn, cursor):
                cursor.execute(
                    f"ALTER TABLE `{TABLE}` ADD COLUMN `doc_version` INT DEFAULT NULL "
                    f"COMMENT '命中文档(top1)的版本号, 用于追责时确认依据的是哪一版'"
                )
        except Exception as e:
            if getattr(e, "args", [None])[0] != 1060:
                logger.warning(f"补充 doc_version 列失败(不影响问答): {e}")
        _table_ready = True
        logger.info(f"表 {TABLE} 创建/校验成功")
        return True
    except Exception as e:
        logger.warning(f"创建表 {TABLE} 失败(不影响问答): {e}")
        return False


def _normalize_filenames(top_filenames):
    """文件名归一化: list 取前 8 个用英文逗号拼接, 超长截断到 500 字符; None 保持 NULL"""
    if top_filenames is None:
        return None
    if isinstance(top_filenames, (list, tuple)):
        names = [str(n) for n in top_filenames[:8] if n]
        if not names:
            return None
        text = ",".join(names)
    else:
        text = str(top_filenames)
    return text[:500] or None


def log_qa(request_id, query, layer, strategy=None, top_score=None,
           top_filenames=None, answer=None, source="web", doc_version=None):
    """
    记录一条问答审计日志(只写不改, 失败不影响问答)。

    :param request_id: 请求 id(路由层生成)
    :param query: 用户原始问题
    :param layer: 最终命中层 error_code / faq / rag / fallback
    :param strategy: RAG 检索策略 direct / hyde / keyword, 非 RAG 层传 None
    :param top_score: rerank 最高分, 无精排/非 RAG 层传 None
    :param top_filenames: 命中父文档文件名列表, 只取前 8 个, None 写 NULL
    :param doc_version: 命中文档(top1)的当前版本号, 非 RAG 层传 None
    :param answer: 答案原文, 存前 1200 字正文 + md5 摘要(用于事后还原与校验)
    :param source: 入口来源, 默认 web
    :return: True 写入成功 / False 写入失败(已记 warning)
    """
    try:
        if not ensure_table():
            return False

        answer_hash = hashlib.md5(answer.encode("utf-8")).hexdigest() if answer else None
        # 答案正文前 1200 字用于事后还原(超长截断); 全量答案不落库, 避免日志表膨胀
        answer_snippet = (answer or "")[:1200] or None
        # DECIMAL(6,4): 统一保留 4 位小数, 与知识缺口表里的 min_score 口径一致
        score = round(float(top_score), 4) if top_score is not None else None

        sql = f"""
        INSERT INTO `{TABLE}`
            (request_id, query, layer, strategy, top_score, top_filenames, doc_version, answer_snippet, answer_hash, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        with _get_db_cursor() as (conn, cursor):
            cursor.execute(sql, (
                (request_id or "")[:32] or None,
                query or "",
                (layer or "")[:16] or None,
                (strategy or "")[:16] or None,
                score,
                _normalize_filenames(top_filenames),
                int(doc_version) if doc_version is not None else None,
                answer_snippet,
                answer_hash,
                (source or "web")[:32],
            ))
        return True
    except Exception as e:
        logger.warning(f"写问答审计日志失败(不影响问答): {e}")
        return False
