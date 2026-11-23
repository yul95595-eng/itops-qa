# -*- coding: utf-8 -*-
# store/doc_store.py
# 上传知识文档的登记表 —— 知识库可维护性的数据底座（MySQL）
#
# 设计动机:
#   让企业把自己整理的运维文档灌进知识库, 必须回答三个问题:
#     1. 传了什么?        -> 每篇文档一条记录(文件名/分类/块数/时间)
#     2. 处理到哪一步了?   -> 状态机 pending -> parsing -> embedding -> done / failed
#     3. 怎么删干净?       -> doc_id 是向量库/Milvus 的删除依据, 一条记录对应一批向量块
#
# 表结构:
#   knowledge_docs(doc_id, filename, file_path, category, file_hash, status,
#                  chunk_count, error_msg, tenant_id, created_at, updated_at)
#   - doc_id:    文件内容 md5 前 16 位(内容寻址, 同一文件重复上传得到同一 ID -> 天然幂等)
#   - file_hash: 文件全量 md5(查重: 重复上传直接拒绝, 不浪费 CPU 重算向量)
#   - tenant_id: 多租户预留字段, 当前私有化部署恒为 'default'
import os
import sys
from datetime import datetime

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dbutils.pooled_db import PooledDB
from contextlib import contextmanager

from base.config import config
from base.logger import logger

# 文档状态机
DOC_STATUS = ("pending", "parsing", "embedding", "done", "failed")


class KnowledgeDocStore:
    """MySQL 知识文档登记表: 建表 + 登记 + 状态流转 + 查询 + 删除"""

    def __init__(self):
        self.logger = logger
        self.table = "knowledge_docs"
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
            self.logger.info("MySQL 知识文档存储连接池创建成功")
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
        except pymysql.Error as e:
            self.logger.error(f"创建数据库失败: {e}")
            raise

    def create_table(self):
        """创建 knowledge_docs 表(若不存在)"""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.table}` (
            `doc_id` VARCHAR(32) NOT NULL COMMENT '文档ID(文件内容md5前16位)',
            `filename` VARCHAR(255) NOT NULL COMMENT '原始文件名',
            `file_path` VARCHAR(500) NOT NULL COMMENT '服务器存储路径',
            `category` VARCHAR(32) DEFAULT 'custom' COMMENT '知识分类',
            `file_hash` VARCHAR(32) NOT NULL COMMENT '文件全量md5(查重)',
            `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/parsing/embedding/done/failed',
            `chunk_count` INT DEFAULT 0 COMMENT '切分出的子块数',
            `error_msg` VARCHAR(1000) DEFAULT '' COMMENT '失败原因',
            `tenant_id` VARCHAR(32) DEFAULT 'default' COMMENT '租户(私有化部署恒为default)',
            `version` INT DEFAULT 1 COMMENT '同名文档的版本号(首次上传为1)',
            `is_latest` TINYINT DEFAULT 1 COMMENT '是否当前版本: 1当前/0已被同名新版取代',
            `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`doc_id`),
            KEY `idx_status` (`status`),
            KEY `idx_category` (`category`),
            KEY `idx_hash` (`file_hash`),
            KEY `idx_filename_latest` (`filename`, `is_latest`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='上传知识文档登记表';
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(ddl)
            self.logger.info(f"表 {self.table} 创建/校验成功")
        except pymysql.Error as e:
            self.logger.error(f"建表失败: {e}")
            raise

        # 兼容已存在的旧表: 补版本相关列(1060=列已存在, 跳过)
        for ddl_add in (
            "ADD COLUMN `version` INT DEFAULT 1 COMMENT '同名文档的版本号(首次上传为1)'",
            "ADD COLUMN `is_latest` TINYINT DEFAULT 1 COMMENT '是否当前版本: 1当前/0已被同名新版取代'",
        ):
            try:
                with self.get_db_cursor() as (conn, cursor):
                    cursor.execute(f"ALTER TABLE `{self.table}` {ddl_add}")
            except pymysql.Error as e:
                if e.args[0] != 1060:
                    self.logger.warning(f"补充版本列失败(不影响主流程): {e}")

    def register(self, doc_id, filename, file_path, category, file_hash, version=1):
        """
        登记一篇上传文档(状态 pending)。
        若 doc_id 已存在(同内容文件曾上传过), 抛出 ValueError 由接口层返回 409。
        :param version: 同名文档的版本号, 首次上传为 1, 同名新版本由调用方传入旧版本+1
        """
        sql = f"""
        INSERT INTO `{self.table}` (doc_id, filename, file_path, category, file_hash, status, version, is_latest)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s, 1)
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (doc_id, filename, file_path, category, file_hash, version))
            self.logger.info(f"文档登记成功: {filename} (doc_id={doc_id}, v{version})")
        except pymysql.err.IntegrityError:
            raise ValueError(f"文档已存在(doc_id={doc_id}), 请勿重复上传")
        except pymysql.Error as e:
            self.logger.error(f"文档登记失败: {e}")
            raise

    def find_latest_by_filename(self, filename):
        """
        查同名文档的当前版本记录(is_latest=1), 无则 None。用于同名新版本的覆盖处理。

        兼容两种取名口径: 向量库 metadata 里的 filename 不带扩展名(如「蓝屏排查」),
        登记表里存的是完整文件名(如「蓝屏排查.md」), 因此同时按等值和「原名.扩展名」匹配。
        """
        sql = f"""
        SELECT * FROM `{self.table}`
        WHERE (filename = %s OR filename LIKE %s) AND is_latest = 1
        ORDER BY version DESC LIMIT 1
        """
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (filename, f"{filename}.%"))
                row = cursor.fetchone()
            return self._row_to_dict(row, cursor.description) if row else None
        except pymysql.Error as e:
            self.logger.error(f"按文件名查当前版本失败: {e}")
            return None

    def mark_superseded(self, doc_id):
        """把旧版本标记为非当前版本(保留记录便于追溯, 向量由调用方删除)"""
        sql = f"UPDATE `{self.table}` SET is_latest = 0 WHERE doc_id = %s"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (doc_id,))
            self.logger.info(f"旧版本已下架(is_latest=0): {doc_id}")
            return True
        except pymysql.Error as e:
            self.logger.error(f"标记旧版本下架失败: {e}")
            return False

    def update_status(self, doc_id, status, chunk_count=None, error_msg=None):
        """更新状态机(只能沿 DOC_STATUS 定义的状态流转)"""
        if status not in DOC_STATUS:
            raise ValueError(f"非法状态: {status}")
        sets = ["status = %s", "updated_at = %s"]
        params = [status, datetime.now()]
        if chunk_count is not None:
            sets.append("chunk_count = %s")
            params.append(chunk_count)
        if error_msg is not None:
            sets.append("error_msg = %s")
            params.append(error_msg[:1000])
        params.append(doc_id)
        sql = f"UPDATE `{self.table}` SET {', '.join(sets)} WHERE doc_id = %s"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, params)
        except pymysql.Error as e:
            self.logger.error(f"更新文档状态失败 {doc_id}: {e}")
            raise

    def get(self, doc_id):
        """查单篇文档, 返回 dict 或 None"""
        sql = f"SELECT * FROM `{self.table}` WHERE doc_id = %s"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (doc_id,))
                row = cursor.fetchone()
            return self._row_to_dict(row, cursor.description) if row else None
        except pymysql.Error as e:
            self.logger.error(f"查询文档失败 {doc_id}: {e}")
            return None

    def list_docs(self, category=None):
        """列出全部(或某分类)上传文档, 按创建时间倒序"""
        sql = f"SELECT * FROM `{self.table}`"
        params = ()
        if category:
            sql += " WHERE category = %s"
            params = (category,)
        sql += " ORDER BY created_at DESC"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                desc = cursor.description
            return [self._row_to_dict(r, desc) for r in rows]
        except pymysql.Error as e:
            self.logger.error(f"查询文档列表失败: {e}")
            return []

    def find_by_hash(self, file_hash):
        """按文件 hash 查重, 返回已存在的记录或 None"""
        sql = f"SELECT * FROM `{self.table}` WHERE file_hash = %s LIMIT 1"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (file_hash,))
                row = cursor.fetchone()
            return self._row_to_dict(row, cursor.description) if row else None
        except pymysql.Error as e:
            self.logger.error(f"按hash查重失败: {e}")
            return None

    def delete(self, doc_id):
        """删除登记记录(向量块的删除由调用方配合 milvus_admin 完成, 保证三处一致)"""
        sql = f"DELETE FROM `{self.table}` WHERE doc_id = %s"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql, (doc_id,))
            self.logger.info(f"文档登记记录已删除: {doc_id}")
            return True
        except pymysql.Error as e:
            self.logger.error(f"删除文档记录失败 {doc_id}: {e}")
            return False

    def count(self):
        sql = f"SELECT COUNT(*) FROM `{self.table}`"
        try:
            with self.get_db_cursor() as (conn, cursor):
                cursor.execute(sql)
                return cursor.fetchone()[0]
        except pymysql.Error as e:
            self.logger.error(f"统计文档数失败: {e}")
            return -1

    def close(self):
        try:
            self.pool.close()
        except Exception:
            pass

    @staticmethod
    def _row_to_dict(row, description=None):
        """
        行转字典。
        优先用 cursor.description 动态取列名 —— 表结构加字段时不会错位;
        未传 description 时退回固定列序(仅作兜底)。
        """
        if description:
            cols = [d[0] for d in description]
        else:
            cols = ["doc_id", "filename", "file_path", "category", "file_hash",
                    "status", "chunk_count", "error_msg", "tenant_id",
                    "version", "is_latest", "created_at", "updated_at"]
        return {c: (v.isoformat() if isinstance(v, datetime) else v) for c, v in zip(cols, row)}


if __name__ == "__main__":
    store = KnowledgeDocStore()
    store.create_table()
    print("当前登记文档数:", store.count())
    for d in store.list_docs():
        print(f"  [{d['status']}] {d['filename']} ({d['chunk_count']} 块, doc_id={d['doc_id']})")
