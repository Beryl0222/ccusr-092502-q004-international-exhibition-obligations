"""服务启动入口：python3 -m src.app

环境变量：
  CHAIN_STORE_PATH  事件日志路径（默认 data/eventlog.jsonl，留空为纯内存）
  CHAIN_HOST        监听地址（默认 127.0.0.1）
  CHAIN_PORT        端口（默认 8080）
"""
from __future__ import annotations

import os

from src.api import serve

if __name__ == "__main__":
    store_path = os.environ.get("CHAIN_STORE_PATH", "data/eventlog.jsonl")
    host = os.environ.get("CHAIN_HOST", "127.0.0.1")
    port = int(os.environ.get("CHAIN_PORT", "8080"))
    serve(store_path if store_path else None, port=port, host=host)
