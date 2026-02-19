"""配置管理模块 - 从 config.toml 加载和保存配置"""

import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # Python < 3.11 回退

CONFIG_FILE = Path(__file__).parent.parent / "config.toml"

DEFAULT_CONFIG = {
    "server": {
        "port": 8000
    },
    "aria2": {
        "rpc_url": "http://localhost",
        "rpc_port": 6800,
        "rpc_secret": "",
        "max_concurrent": 3,
        "download_dir": "./downloads",
        "aria2c_path": ""
    },
    "teldrive": {
        "api_host": "http://localhost:8080",
        "access_token": "",
        "channel_id": 0,
        "chunk_size": "500M",
        "upload_concurrency": 4,
        "random_chunk_name": True,
        "upload_dir": "",
        "target_path": "/"
    },
    "general": {
        "max_retries": 3,
        "auto_delete": True,
        "max_disk_usage": 0,
        "cpu_limit": 85
    }
}


def _format_value(v) -> str:
    """将 Python 值转为 TOML 字面量"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _write_toml(config: dict) -> str:
    """生成 TOML 文本"""
    lines = []
    section_order = ["server", "aria2", "teldrive", "general"]
    for s in config:
        if s not in section_order:
            section_order.append(s)

    for section in section_order:
        if section not in config:
            continue
        lines.append(f"[{section}]")
        for key, value in config[section].items():
            lines.append(f"{key} = {_format_value(value)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def load_config() -> dict:
    """加载配置，如果配置文件不存在则创建默认配置"""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
        # 合并默认值（确保新增字段有默认值）
        merged = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
        for section in merged:
            if section in config:
                merged[section].update(config[section])
        return merged
    except (Exception, IOError):
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """保存配置到文件（合并模式：保留未传入的 section）"""
    # 先读取现有配置，合并后再写入
    existing = load_config() if CONFIG_FILE.exists() else {}
    for section, values in config.items():
        if isinstance(values, dict):
            existing[section] = values
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(_write_toml(existing))


def get_aria2_rpc_url(config: dict) -> str:
    """获取完整的 aria2 RPC URL"""
    aria2 = config["aria2"]
    return f"{aria2['rpc_url']}:{aria2['rpc_port']}/jsonrpc"


def get_download_dir(config: dict) -> str:
    """获取下载目录的绝对路径"""
    download_dir = config["aria2"]["download_dir"]
    path = Path(download_dir)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / path
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())
