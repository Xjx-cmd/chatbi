"""
配置管理模块

集中管理数据库连接和 LLM API 配置，所有环境变量和常量在此统一定义。

"""

import os
from dotenv import load_dotenv

load_dotenv()

# ==================== 数据库配置 ====================
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD") or "123qwe",
    "database": os.getenv("DB_NAME", "chatbi_mvp"),
    "charset": "utf8mb4",
    "autocommit": True,
    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", 5)),
    "read_timeout": int(os.getenv("DB_READ_TIMEOUT", 8)),
    "write_timeout": int(os.getenv("DB_WRITE_TIMEOUT", 8)),
}

DB_RUNTIME_CONFIG = {
    "pool_size": int(os.getenv("DB_POOL_SIZE", 5)),
    "max_overflow": int(os.getenv("DB_POOL_MAX_OVERFLOW", 5)),
    "pool_timeout": float(os.getenv("DB_POOL_TIMEOUT", 3)),
    "slow_query_threshold_ms": float(os.getenv("DB_SLOW_QUERY_THRESHOLD_MS", 200)),
}

# ++==================== LLM 配置 ====================
LLM_CONFIG = {
    "api_key": os.getenv("OPENAI_API_KEY"),
    "base_url": os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "model": os.getenv("LLM_MODEL", "qwen-plus"),
    "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
    "temperature": 0.1,
    "max_tokens": int(os.getenv("LLM_MAX_TOKENS", 4000))
}
