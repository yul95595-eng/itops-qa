# -*- coding: utf-8 -*-
# store/faq_store.py
# FAQ 存储 —— BM25 高频问答层的数据底座（MySQL）
#
# 设计动机:
#   高频 FAQ 有标准答案, 适合存结构化表, 由 BM25 层快速匹配, 省去 LLM 和向量检索开销。
#   本模块负责 FAQ 的建表 + 批量入库 + 查询, 写法对齐 error_code_store 的连接池模式。
#
# 表结构:
#   faq(id, question, answer, category, source)
#   - question: 高频问题(标准化问法)
#   - answer:   标准处置答案
#   - category: 所属知识域(network/account/printer/software)
#   - source:   数据来源(builtin 内置)
import os
import sys

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dbutils.pooled_db import PooledDB
from contextlib import contextmanager

from base.config import config
from base.logger import logger


class FAQStore:
    """MySQL FAQ 存储: 建表 + 批量入库 + 查询"""

    def __init__(self):
        self.logger = logger
        self.table = "faq"
        self.ensure_database()
        try:
            self.pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=2,
                maxcached=5,
                blocking=True,
                ping=1,
                host=config.MYSQL_HOST,
                port=config.MYSQL_PORT,
                user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,
                database=config.MYSQL_DATABASE,
                charset="utf8mb4",
            )
            self.logger.info("MySQL FAQ 存储连接池创建成功")
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
        except Exception as e:
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def ensure_database(self):
        """确保目标数据库存在"""
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
            self.logger.info(f"数据库 {config.MYSQL_DATABASE} 就绪")
        except pymysql.Error as e:
            self.logger.error(f"创建数据库失败: {e}")
            raise

    def create_table(self):
        """创建 faq 表(若不存在)。question 唯一键防重复。"""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.table}` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `question` VARCHAR(255) NOT NULL COMMENT '高频问题',
            `answer` TEXT COMMENT '标准答案',
            `category` VARCHAR(32) DEFAULT '' COMMENT '所属知识域',
            `source` VARCHAR(32) DEFAULT 'builtin' COMMENT '数据来源',
            UNIQUE KEY `uk_question` (`question`),
            KEY `idx_category` (`category`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='IT运维FAQ表';
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(ddl)
            self.logger.info(f"表 {self.table} 创建/校验成功")
        except pymysql.Error as e:
            self.logger.error(f"建表失败: {e}")
            raise

    def ingest(self, faq_list=None, source="builtin"):
        """
        批量写入 FAQ(upsert, 按 question 唯一键去重)
        :param faq_list: [(question, answer, category), ...], 默认使用内置种子
        :param source: 数据来源(builtin 内置 / custom 企业导入), 用于区分内置与企业知识
        :return: 写入条数
        """
        if faq_list is None:
            from data.seed.faq_seed import get_faq_list
            faq_list = get_faq_list()

        sql = f"""
        INSERT INTO `{self.table}` (question, answer, category, source)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            answer = VALUES(answer),
            category = VALUES(category),
            source = VALUES(source)
        """
        count = 0
        try:
            with self.get_db_cursor() as (conn, cursor):
                for question, answer, category in faq_list:
                    cursor.execute(sql, (question, answer, category, source))
                    count += 1
            self.logger.info(f"FAQ 入库完成, 共 {count} 条 (source={source})")
        except pymysql.Error as e:
            self.logger.error(f"FAQ 入库失败: {e}")
            raise
        return count

    def fetch_all_questions(self):
        """返回所有问题字符串列表(供 BM25 建索引)"""
        sql = f"SELECT question FROM `{self.table}` ORDER BY id"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql)
                rows = cursor.fetchall()
            return [r[0] for r in rows]
        except pymysql.Error as e:
            self.logger.error(f"查询问题列表失败: {e}")
            return []

    def fetch_answer(self, question):
        """按问题精确取答案"""
        sql = f"SELECT answer FROM `{self.table}` WHERE question = %s LIMIT 1"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (question,))
                row = cursor.fetchone()
            return row[0] if row else None
        except pymysql.Error as e:
            self.logger.error(f"查询答案失败: {e}")
            return None

    def count(self):
        """统计 FAQ 条数"""
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(f"SELECT COUNT(*) FROM `{self.table}`")
                return cursor.fetchone()[0]
        except pymysql.Error as e:
            self.logger.error(f"统计失败: {e}")
            return 0

    def close(self):
        try:
            self.pool.close()
            self.logger.info("MySQL FAQ 连接池已关闭")
        except (AttributeError, pymysql.Error) as e:
            self.logger.info(f"连接池关闭(可忽略): {e}")


if __name__ == "__main__":
    store = FAQStore()
    store.create_table()
    n = store.ingest()
    print(f"入库 {n} 条, 当前表内共 {store.count()} 条")
    # 抽查
    print("问题总数:", len(store.fetch_all_questions()))
    print("抽查答案:", store.fetch_answer("VPN 连接不上怎么办"))
    store.close()
