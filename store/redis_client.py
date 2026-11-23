# -*- coding: utf-8 -*-
# store/redis_client.py
# Redis 缓存客户端 —— 供 FAQ/BM25 层做答案缓存, 降低重复查询开销
#
# Redis 缓存客户端。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis
import json

from base.config import config
from base.logger import logger


class RedisClient:
    """Redis 缓存客户端: 存取 JSON 数据 + 业务级答案缓存"""

    def __init__(self):
        self.logger = logger
        try:
            self.client = redis.StrictRedis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                password=config.REDIS_PASSWORD,
                decode_responses=True,
            )
            self.logger.info("Redis 连接成功")
        except redis.ConnectionError as e:
            self.logger.error(f"Redis 连接失败: {e}")
            raise

    def set_data(self, key, value):
        """存储 JSON 数据到 Redis"""
        try:
            self.client.set(key, json.dumps(value, ensure_ascii=False))
            self.logger.info(f"存储数据到 Redis: {key}")
        except redis.RedisError as e:
            self.logger.error(f"Redis 存储失败: {e}")

    def get_data(self, key):
        """从 Redis 获取 JSON 数据"""
        try:
            data = self.client.get(key)
            return json.loads(data) if data else None
        except redis.RedisError as e:
            self.logger.error(f"Redis 获取失败: {e}")
            return None

    def get_answer(self, query, normalized=False):
        """获取查询的缓存答案
        :param normalized: 是否已用规范化 key(分词归一化后), 用于提升缓存命中率
        """
        try:
            key = f"answer:{query}" if not normalized else f"answer_norm:{query}"
            answer = self.client.get(key)
            if answer:
                self.logger.info(f"从 Redis 获取答案: {query}")
                return answer
            return None
        except redis.RedisError as e:
            self.logger.error(f"Redis 查询失败: {e}")
            return None

    def set_answer(self, query, answer, expire=3600, normalized=False):
        """缓存查询答案, 默认 1 小时过期
        :param normalized: 是否用规范化 key(分词归一化后), 让同义不同字串的问题共享缓存
        """
        try:
            key = f"answer:{query}" if not normalized else f"answer_norm:{query}"
            self.client.set(key, answer, ex=expire)
            self.logger.info(f"缓存答案: {query}")
        except redis.RedisError as e:
            self.logger.error(f"Redis 缓存答案失败: {e}")

    def delete(self, key):
        try:
            self.client.delete(key)
            self.logger.info(f"删除 Redis 数据: {key}")
        except redis.RedisError as e:
            self.logger.error(f"Redis 删除失败: {e}")


if __name__ == '__main__':
    c = RedisClient()
    c.set_data('it_ops_test', '连接正常')
    print("读回:", c.get_data('it_ops_test'))
