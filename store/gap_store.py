# -*- coding: utf-8 -*-
# store/gap_store.py
# 知识缺口登记表 —— 知识库"自我进化"的数据底座（MySQL）
#
# 设计动机:
#   员工问了、但知识库答不上的问题，恰恰是最有价值的产出。
#   如果这些"答不上"只散落在日志里，企业就永远不知道下一批该写什么文档。
#   把三类"未命中"集中落一张表，再在管理页聚类展示，就形成了闭环:
#     用户提问 → 检索未命中 → 记录缺口 → 运营看到清单 → 补文档/FAQ → 缺口关闭
#
# 记录的三类缺口(触发点都来自问答管线):
#   1. fallback   : 三层路由全未命中(错误码无/FAQ无/RAG检索为空) —— 知识里确实没有
#   2. rag_low    : RAG 检索到但 rerank 最高分低于阈值, 被判"不相关"返回兜底 —— 有疑似但不相关
#   3. feedback   : 用户对答案点了"没用" —— 答案存在但用户不满意(可能是答非所问/过时/错误)
#
# 关键设计:
#   - query_hash = md5(去空白后的问题原文), 作幂等主键:
#       同一问题被反复问(常见于"确实没人管"的长尾问题), 只记一条, count 累加。
#       运营按 count 排序就能直接看出"最该优先补的知识"。
#   - 记录 source(命中层/检索策略) 与最低分, 方便事后归因是"没检索到"还是"检索到了但不相关"。
import hashlib
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dbutils.pooled_db import PooledDB
from contextlib import contextmanager

from base.config import config
from base.logger import logger


def _query_hash(query: str) -> str:
    """问题归一化哈希: 去首尾空白 + 压缩连续空白后取 md5, 作为幂等主键"""
    normalized = " ".join((query or "").split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


class KnowledgeGapStore:
    """MySQL 知识缺口登记表: 记录 + 聚类统计 + 关闭缺口"""

    def __init__(self):
        self.logger = logger
        self.table = "knowledge_gaps"
        self.ensure_database()
        try:
            self.pool = PooledDB(
                creator=pymysql,
                maxconnections=5,
                mincached=1,
                maxcached=3,
                blocking=True,
                ping=1,
                host=config.MYSQL_HOST,
                port=config.MYSQL_PORT,
                user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,
                database=config.MYSQL_DATABASE,
                charset="utf8mb4",
            )
            self.logger.info("MySQL 知识缺口存储连接池创建成功")
        except pymysql.Error as e:
            self.logger.error(f"MySQL 连接失败: {e}")
            raise

    @contextmanager
    def get_db_cursor(self):
        conn = None
        cursor = None
        try:
            conn = self.pool.connection()
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

    def ensure_database(self):
        try:
            conn = pymysql.connect(
                host=config.MYSQL_HOST,
                port=config.MYSQL_PORT,
                user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,
                charset="utf8mb4",
            )
            cursor = conn.cursor()
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.MYSQL_DATABASE}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            conn.commit()
            cursor.close()
            conn.close()
        except pymysql.Error as e:
            self.logger.error(f"创建数据库失败: {e}")
            raise

    def create_table(self):
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.table}` (
            `query_hash` VARCHAR(32) NOT NULL COMMENT '问题归一化md5(幂等主键)',
            `query` VARCHAR(500) NOT NULL COMMENT '用户原始问题',
            `reason` VARCHAR(16) NOT NULL COMMENT '缺口类型: fallback/rag_low/feedback',
            `layer` VARCHAR(32) DEFAULT '' COMMENT '最终命中层(如 error_code/faq/rag/fallback)',
            `strategy` VARCHAR(32) DEFAULT '' COMMENT 'RAG检索策略(direct/hyde/keyword)',
            `min_score` FLOAT DEFAULT NULL COMMENT 'rerank最高分(rag_low时记录,用于归因)',
            `last_request_id` VARCHAR(32) DEFAULT NULL COMMENT '最近一次问答的request_id(关联qa_audit_log, 用于事后还原答案)',
            `hit_count` INT DEFAULT 1 COMMENT '同一问题被问次数',
            `status` VARCHAR(16) DEFAULT 'open' COMMENT 'open已开/resolved已补知识',
            `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`query_hash`),
            KEY `idx_reason` (`reason`),
            KEY `idx_status` (`status`),
            KEY `idx_count` (`hit_count`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识缺口登记表(答不上/不满意的提问聚合)';
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(ddl)
            self.logger.info(f"表 {self.table} 创建/校验成功")
        except pymysql.Error as e:
            self.logger.error(f"建表失败: {e}")
            raise

        # 兼容已存在的旧表: 补 last_request_id 列(1060=列已存在则跳过)
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(
                    f"ALTER TABLE `{self.table}` ADD COLUMN `last_request_id` VARCHAR(32) "
                    f"DEFAULT NULL COMMENT '最近一次问答的request_id(关联qa_audit_log, 用于事后还原答案)'"
                )
        except pymysql.Error as e:
            if e.args[0] != 1060:
                self.logger.warning(f"补充 last_request_id 列失败(不影响主流程): {e}")

    def record(self, query, reason, layer="", strategy="", min_score=None, request_id=None):
        """
        记录一条知识缺口(幂等):
          - 已存在同问题 -> hit_count + 1
          - 不存在 -> 新增一条
        :param reason: fallback / rag_low / feedback
        :param request_id: 本次问答的 request_id, 用于关联 qa_audit_log 还原答案
        """
        query = (query or "").strip()
        if not query:
            return False
        qh = _query_hash(query)
        sql = f"""
        INSERT INTO `{self.table}`
            (query_hash, query, reason, layer, strategy, min_score, last_request_id, hit_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
        ON DUPLICATE KEY UPDATE
            hit_count = hit_count + 1,
            last_request_id = COALESCE(VALUES(last_request_id), last_request_id),
            updated_at = CURRENT_TIMESTAMP
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(
                    sql,
                    (qh, query[:500], reason, layer[:32], strategy[:32], min_score,
                     (request_id or "")[:32] or None),
                )
            return True
        except pymysql.Error as e:
            self.logger.error(f"记录知识缺口失败: {e}")
            return False

    def list_gaps(self, reason=None, limit=100):
        """列出缺口, 按被问次数倒序(运营最该优先补的排前面)"""
        sql = f"SELECT * FROM `{self.table}`"
        params = ()
        if reason:
            sql += " WHERE reason = %s"
            params = (reason,)
        sql += " ORDER BY hit_count DESC, updated_at DESC LIMIT %s"
        params = params + (limit,)
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        except pymysql.Error as e:
            self.logger.error(f"查询知识缺口失败: {e}")
            return []

    def resolve(self, query_hash):
        """关闭缺口: 补了知识后标记 resolved"""
        sql = f"UPDATE `{self.table}` SET status = 'resolved' WHERE query_hash = %s"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (query_hash,))
            return True
        except pymysql.Error as e:
            self.logger.error(f"关闭缺口失败: {e}")
            return False

    def count(self, reason=None):
        sql = f"SELECT COUNT(*) FROM `{self.table}`"
        params = ()
        if reason:
            sql += " WHERE reason = %s"
            params = (reason,)
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, params)
                return cursor.fetchone()[0]
        except pymysql.Error as e:
            self.logger.error(f"统计缺口失败: {e}")
            return -1

    def close(self):
        try:
            self.pool.close()
        except Exception:
            pass

    @staticmethod
    def _row_to_dict(row):
        cols = ["query_hash", "query", "reason", "layer", "strategy",
                "min_score", "hit_count", "status", "created_at", "updated_at"]
        return {c: (v.isoformat() if isinstance(v, datetime) else v) for c, v in zip(cols, row)}


# 模块级单例(懒加载)
_gap_store = None


def get_gap_store():
    global _gap_store
    if _gap_store is None:
        _gap_store = KnowledgeGapStore()
        _gap_store.create_table()
    return _gap_store


if __name__ == "__main__":
    store = get_gap_store()
    print("当前缺口数:", store.count())
    for g in store.list_gaps():
        print(f"  [{g['reason']}] x{g['hit_count']} {g['query']}")
