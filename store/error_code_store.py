# -*- coding: utf-8 -*-
# store/error_code_store.py
# MySQL 错误码入库 —— 精确匹配层的数据底座
#
# 设计动机:
#   错误码是运维知识库中"有标准答案"的那类数据, 适合走结构化表精确查询(零 LLM、零幻觉)。
#   本模块把内置的 29 条错误码种子数据批量写入 MySQL 的 error_codes 表,
#   并提供精确查询接口, 供 error_code_matcher 命中后取回标准答案。
#
# 表结构:
#   error_codes(code, component, meaning, cause, solution, source)
#   - code:      错误码标识(如 1045 / ORA-12541 / 0x80072EE7), 唯一索引
#   - component: 所属组件(MySQL/Redis/Windows/网络/Oracle)
#   - meaning:   含义
#   - cause:     常见原因
#   - solution:  解决步骤
#   - source:    数据来源(builtin 内置 / 后续可扩展官方文档)
import os
import sys

# 将项目根目录加入模块搜索路径, 保证直接运行本文件也能导入 base / core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dbutils.pooled_db import PooledDB
from contextlib import contextmanager

from base.config import config
from base.logger import logger


class ErrorCodeStore:
    """MySQL 错误码存储: 建表 + 批量入库 + 精确查询"""

    def __init__(self):
        self.logger = logger
        self.table = "error_codes"
        # 先确保数据库存在(库不存在时连接池初始化会失败)
        self.ensure_database()
        try:
            self.pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=2,
                maxcached=5,
                blocking=True,
                ping=1,  # 每次借连接自动检活, 防止 Web 长连接下拿死连接崩溃
                host=config.MYSQL_HOST,
                port=config.MYSQL_PORT,
                user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,
                database=config.MYSQL_DATABASE,
                charset="utf8mb4",
            )
            self.logger.info("MySQL 错误码存储连接池创建成功")
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
        """确保目标数据库存在(首次运行时库可能还没建)"""
        try:
            # 不指定 database 临时连接, 创建库
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
        """创建 error_codes 表(若不存在)。唯一键为 (code, component), 允许不同组件存在同名错误码。"""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.table}` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `code` VARCHAR(64) NOT NULL COMMENT '错误码标识',
            `component` VARCHAR(32) NOT NULL COMMENT '所属组件',
            `meaning` VARCHAR(255) NOT NULL COMMENT '含义',
            `cause` VARCHAR(500) DEFAULT '' COMMENT '常见原因',
            `solution` TEXT COMMENT '解决步骤',
            `source` VARCHAR(32) DEFAULT 'builtin' COMMENT '数据来源',
            UNIQUE KEY `uk_code_component` (`code`, `component`),
            KEY `idx_component` (`component`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='IT运维错误码表';
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(ddl)
            self.logger.info(f"表 {self.table} 创建/校验成功")
        except pymysql.Error as e:
            self.logger.error(f"建表失败: {e}")
            raise

    def ingest(self, error_codes=None, source="builtin"):
        """
        批量写入错误码(upsert, 按 code 唯一键去重, 重复运行不产生脏数据)
        :param error_codes: [(code, component, meaning, cause, solution), ...]
                            默认使用「内置种子 + 大规模扩展表」的完整数据
        :param source: 数据来源(builtin 内置 / custom 企业导入)
        :return: 写入条数
        """
        if error_codes is None:
            from data.seed.error_code_seed import get_all_error_codes
            error_codes = get_all_error_codes()

        sql = f"""
        INSERT INTO `{self.table}` (code, component, meaning, cause, solution, source)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            component = VALUES(component),
            meaning = VALUES(meaning),
            cause = VALUES(cause),
            solution = VALUES(solution),
            source = VALUES(source)
        """
        count = 0
        try:
            with self.get_db_cursor() as (conn, cursor):
                for rec in error_codes:
                    code, component, meaning, cause, solution = rec
                    cursor.execute(sql, (code, component, meaning, cause, solution, source))
                    count += 1
            self.logger.info(f"错误码入库完成, 共 {count} 条 (source={source})")
        except pymysql.Error as e:
            self.logger.error(f"错误码入库失败: {e}")
            raise
        return count

    def query(self, code: str, component: str = None):
        """
        精确查询某错误码的完整记录
        :param code: 错误码标识(不区分大小写)
        :param component: 可选, 指定组件以消歧义(不同组件可能同名错误码)
        :return: dict 或 None
        """
        if component:
            sql = f"""
            SELECT code, component, meaning, cause, solution, source
            FROM `{self.table}` WHERE code = %s AND component = %s LIMIT 1
            """
            params = (code, component)
        else:
            sql = f"""
            SELECT code, component, meaning, cause, solution, source
            FROM `{self.table}` WHERE code = %s LIMIT 1
            """
            params = (code,)
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, params)
                row = cursor.fetchone()
            if not row:
                return None
            return {
                "code": row[0],
                "component": row[1],
                "meaning": row[2],
                "cause": row[3],
                "solution": row[4],
                "source": row[5],
            }
        except pymysql.Error as e:
            self.logger.error(f"查询错误码失败: {e}")
            return None

    def count(self):
        """统计当前错误码条数"""
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
            self.logger.info("MySQL 错误码连接池已关闭")
        except (AttributeError, pymysql.Error) as e:
            self.logger.info(f"连接池关闭(可忽略): {e}")


if __name__ == "__main__":
    store = ErrorCodeStore()
    store.ensure_database()
    store.create_table()
    n = store.ingest()
    print(f"入库 {n} 条, 当前表内共 {store.count()} 条")
    # 抽查一条
    hit = store.query("1045")
    print("抽查 1045 ->", hit)
    store.close()
