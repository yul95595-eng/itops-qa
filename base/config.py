# base/config.py
# 企业内部 IT 运维知识库助手 —— 全局配置
# 配置来源采用双层模式:
#   1. 敏感信息(API Key) 走 .env, 不硬编码、不入库
#   2. 其余参数(MySQL/Redis/Milvus/检索) 走 config.ini
import configparser
import os
from pathlib import Path

# 项目根目录: 本文件在 it_ops_qa/base/ 下, 上两级就是项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, config_file=None):
        self.PROJECT_ROOT = str(PROJECT_ROOT)
        # 目录
        self.WIKI_DIR = os.path.join(self.PROJECT_ROOT, 'data', 'wiki')
        self.ERROR_CODE_DIR = os.path.join(self.PROJECT_ROOT, 'data', 'error_codes')
        self.LOG_DIR = os.path.join(self.PROJECT_ROOT, 'logs')

        # 先加载 .env（敏感信息）
        self._load_env()

        # 再读 config.ini（结构参数）
        self.parser = configparser.ConfigParser(
            interpolation=configparser.ExtendedInterpolation()
        )
        if config_file is None:
            config_file = os.path.join(self.PROJECT_ROOT, 'config.ini')
        # 双层读取: config.ini(入库模板, 口令为占位符) -> config.local.ini(本地真实口令, 不入库)
        # parser.read 按顺序读取, 后读的同 section 同名 key 会覆盖先读的
        local_file = os.path.join(self.PROJECT_ROOT, 'config.local.ini')
        self.parser.read([config_file, local_file], encoding='utf-8')

        # ============ LLM 配置（密钥从 .env, 模型名/地址从 config.ini）============
        self.LLM_MODEL = os.getenv(
            'DASHSCOPE_MODEL',
            self.parser.get('llm', 'model', fallback='qwen-plus')
        )
        self.LLM_FLASH_MODEL = os.getenv(
            'DASHSCOPE_FLASH_MODEL',
            self.parser.get('llm', 'model', fallback='qwen-plus')
        )
        self.DASHSCOPE_API_KEY = os.getenv('DASHSCOPE_API_KEY', '')
        self.DASHSCOPE_BASE_URL = os.getenv(
            'DASHSCOPE_BASE_URL',
            self.parser.get('llm', 'dashscope_base_url',
                            fallback='https://dashscope.aliyuncs.com/compatible-mode/v1')
        )

        # ============ MySQL 配置（环境变量优先, 便于容器内指定服务名）============
        self.MYSQL_HOST = os.getenv('MYSQL_HOST', self.parser.get('mysql', 'host', fallback='localhost'))
        self.MYSQL_PORT = int(os.getenv('MYSQL_PORT', self.parser.getint('mysql', 'port', fallback=3306)))
        self.MYSQL_USER = os.getenv('MYSQL_USER', self.parser.get('mysql', 'user', fallback='root'))
        self.MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', self.parser.get('mysql', 'password', fallback=''))
        self.MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', self.parser.get('mysql', 'database', fallback='it_ops_qa'))

        # ============ Redis 配置 ============
        self.REDIS_HOST = os.getenv('REDIS_HOST', self.parser.get('redis', 'host', fallback='localhost'))
        self.REDIS_PORT = int(os.getenv('REDIS_PORT', self.parser.getint('redis', 'port', fallback=6379)))
        self.REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', self.parser.get('redis', 'password', fallback=''))
        self.REDIS_DB = int(os.getenv('REDIS_DB', self.parser.getint('redis', 'db', fallback=1)))

        # ============ Milvus 配置 ============
        self.MILVUS_HOST = os.getenv('MILVUS_HOST', self.parser.get('milvus', 'host', fallback='localhost'))
        self.MILVUS_PORT = os.getenv('MILVUS_PORT', self.parser.get('milvus', 'port', fallback='19530'))
        self.MILVUS_DATABASE_NAME = self.parser.get('milvus', 'database_name', fallback='it_ops')
        self.MILVUS_COLLECTION_NAME = self.parser.get('milvus', 'collection_name', fallback='it_ops_wiki')

        # ============ 检索参数 ============
        self.PARENT_CHUNK_SIZE = self.parser.getint('retrieval', 'parent_chunk_size', fallback=1200)
        self.CHILD_CHUNK_SIZE = self.parser.getint('retrieval', 'child_chunk_size', fallback=400)
        self.CHUNK_OVERLAP = self.parser.getint('retrieval', 'chunk_overlap', fallback=50)
        self.RETRIEVAL_K = self.parser.getint('retrieval', 'retrieval_k', fallback=5)
        self.CANDIDATE_M = self.parser.getint('retrieval', 'candidate_m', fallback=2)

        # ============ FAQ 匹配阈值 ============
        self.BM25_THRESHOLD = 0.85

        # ============ RAG 相关性阈值 ============
        # bge-reranker 精排分数阈值: 最高分低于该值判定为"未检索到相关内容", 直接兜底
        # 分数为相关性概率(0~1): 相关约 0.5~0.99, 不相关约 0.0~0.01
        # 默认 0.1: 拦截明显不相关, 放过有相关性的内容
        self.RERANK_SCORE_THRESHOLD = self.parser.getfloat(
            'retrieval', 'rerank_score_threshold', fallback=0.1)

        # ============ 应用配置 ============
        self.VALID_SOURCES = ["network", "account", "printer", "software"]
        self.SOURCE_LABELS = {
            "network": "网络与VPN",
            "account": "账号与邮箱",
            "printer": "打印机与硬件",
            "software": "软件与报错",
        }
        self.CUSTOMER_SERVICE = self.parser.get(
            'app', 'customer_service', fallback='IT服务台 400-000-0000（工作时间 9:00-18:00）')

        # ============ 知识库上传配置 ============
        # 上传文件保存目录(原文件留存, 供重新入库/审计)
        self.UPLOAD_DIR = os.path.join(self.PROJECT_ROOT, 'data', 'uploads')
        # 允许上传的扩展名(与 document_processor.document_loaders 对齐)
        self.ALLOWED_DOC_EXTS = ['.md', '.txt', '.docx', '.pdf']
        # 单文件大小上限(字节): 20MB
        self.MAX_UPLOAD_SIZE = 20 * 1024 * 1024
        # 上传文档默认分类(可自定义)
        self.DEFAULT_DOC_CATEGORY = 'custom'

        # ============ 管理端鉴权配置 ============
        # 管理口令: 保护知识库写操作(上传/删除/批量导入)与知识盲区查看。
        # 生产环境务必通过环境变量 ADMIN_TOKEN 覆盖默认值(默认值仅供本地开发)。
        # 未配置时为空串, 管理接口将全部拒绝(安全默认), 本地请在 config.local.ini 或 .env 中配置
        self.ADMIN_TOKEN = os.getenv(
            'ADMIN_TOKEN', self.parser.get('app', 'admin_token', fallback='')
        )

        # 日志文件
        self.LOG_FILE = os.path.join(self.LOG_DIR, 'app.log')

    def _load_env(self):
        """从项目根目录的 .env 加载环境变量（若存在）"""
        env_file = os.path.join(self.PROJECT_ROOT, '.env')
        if os.path.exists(env_file):
            with open(env_file, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    k, v = k.strip(), v.strip()
                    os.environ.setdefault(k, v)


config = Config()

if __name__ == '__main__':
    print('PROJECT_ROOT:', config.PROJECT_ROOT)
    print('LLM_MODEL:', config.LLM_MODEL)
    print('DASHSCOPE_API_KEY 已配置:', bool(config.DASHSCOPE_API_KEY))
    print('MYSQL_DATABASE:', config.MYSQL_DATABASE)
    print('REDIS_DB:', config.REDIS_DB)
    print('MILVUS 集合:', config.MILVUS_COLLECTION_NAME)
