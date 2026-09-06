"""設定ファイル（config.json）の読み書きと既定値。"""
import json
import os
from copy import deepcopy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")

DEFAULT_CONFIG = {
    "initial_cash": 1_000_000,
    "start_date": "2026-09-04",
    "broker": {
        "name": "楽天証券（ゼロコース／かぶミニ寄付取引を想定）",
        "commission_rate": 0.0,
        "slippage_rate": 0.0005,
        "lot_size": 1,
        "min_order_jpy": 20_000,
        "fill_timeout_days": 3,
    },
    "strategy": {
        "name": "trend_momentum_v1.1",
        "max_positions": 10,
        "risk_per_trade": 0.01,
        "max_position_weight": 0.12,
        "atr_period": 20,
        "atr_stop_mult": 3.0,
        "sma_fast": 50,
        "sma_slow": 200,
        "momentum_lookback": 252,
        "momentum_skip": 21,
        "top_n_candidates": 20,
        "max_per_sector": 4,
        "max_dist_from_high20": 0.05,
        "min_avg_turnover_jpy": 500_000_000,
        "reentry_cooldown_days": 10,
        "exit_on_trend": True,
        "exit_on_momentum": True,
        "regime_ticker": "1306.T",
        "regime_sma": 200,
    },
    "benchmarks": ["1306.T", "1321.T"],
    "notify": {
        "windows_toast": True,
        "markdown_file": "notifications.md",
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "from": "",
            "to": "",
            "password_env": "STOCKSIM_SMTP_PASSWORD",
        },
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""},
    },
    "review": {
        "weekly_weekday": 4,
        "auto_tune": True,
        "tune_min_trades": 6,
        "tune_cooldown_days": 20,
        "ai_review": {"enabled": False, "command": "claude"},
    },
    "data": {
        "history_start": "2019-06-01",
        "stale_days": 10,
    },
}


def _merge(base, override):
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = CONFIG_PATH) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    else:
        user = {}
    cfg = _merge(DEFAULT_CONFIG, user)
    if not os.path.exists(path):
        save_config(cfg, path)
    return cfg


def save_config(cfg: dict, path: str = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
