"""ルールのパラメータ比較（学習用）。

同じ過去データに対して複数のルール設定を当てはめ、成績を一覧にする。
過学習を避けるため、候補は「意味のある少数」に絞り、結果は knowledge/learning_log.md に記録する。

  python tools/param_scan.py --start 2024-09-02
"""
import argparse
import os
import sys
import tempfile
from copy import deepcopy

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sim.config import load_config  # noqa: E402
from sim.engine import Simulator  # noqa: E402
from sim.metrics import load_nav, load_trades, compute_metrics  # noqa: E402
from run import load_prices  # noqa: E402

VARIANTS = {
    "base(現行)": {},
    "stop2.5": {"atr_stop_mult": 2.5},
    "stop4.0": {"atr_stop_mult": 4.0},
    "mom退出なし": {"exit_on_momentum": False},
    "trend退出なし": {"exit_on_trend": False},
    "退出はストップのみ": {"exit_on_momentum": False, "exit_on_trend": False},
    "高値圏10%": {"max_dist_from_high20": 0.10},
    "高値圏フィルタなし": {"max_dist_from_high20": 1.0},
    "regime100日": {"regime_sma": 100},
    "10銘柄×12%": {"max_positions": 10, "max_position_weight": 0.12},
    "リスク1.5%": {"risk_per_trade": 0.015},
    "候補20": {"top_n_candidates": 20},
    "stop4.0+退出ストップのみ": {"atr_stop_mult": 4.0, "exit_on_momentum": False, "exit_on_trend": False},
    "stop4.0+高値圏10%": {"atr_stop_mult": 4.0, "max_dist_from_high20": 0.10},
    "stop4.0+高値圏10%+退出ストップのみ": {"atr_stop_mult": 4.0, "max_dist_from_high20": 0.10, "exit_on_momentum": False, "exit_on_trend": False},
    "高値圏なし+stop2.5": {"max_dist_from_high20": 1.0, "atr_stop_mult": 2.5},
    "高値圏なし+regime100": {"max_dist_from_high20": 1.0, "regime_sma": 100},
    "高値圏なし+stop2.5+regime100": {"max_dist_from_high20": 1.0, "atr_stop_mult": 2.5, "regime_sma": 100},
    "高値圏なし+10銘柄×12%": {"max_dist_from_high20": 1.0, "max_positions": 10, "max_position_weight": 0.12},
    "高値圏なし+stop3.5": {"max_dist_from_high20": 1.0, "atr_stop_mult": 3.5},
    "業種上限なし": {"max_per_sector": 0},
    "業種上限2(現行)": {"max_per_sector": 2},
    "業種上限3": {"max_per_sector": 3},
    "業種上限4": {"max_per_sector": 4},
}


def run_variant(cfg, prices, end, overrides: dict) -> dict:
    c = deepcopy(cfg)
    c["strategy"].update(overrides)
    d = tempfile.mkdtemp(prefix="scan_")
    sim = Simulator(c, prices, d, log=lambda *a, **k: None, label="scan")
    sim.run_until(end)
    m = compute_metrics(load_nav(d), load_trades(d), cfg["benchmarks"], cfg["initial_cash"])
    t = m["trades"]
    return {
        "リターン": m["total_return"], "TOPIX": m["bench"].get(cfg["benchmarks"][0]), "最大DD": m["max_dd"],
        "シャープ": m["sharpe"], "投資比率": m["exposure_avg"], "取引数": t.get("n_closed", 0),
        "勝率": t.get("win_rate"), "PF": t.get("profit_factor"), "平均保有": t.get("avg_holding_days"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-09-02")
    ap.add_argument("--end", default=None)
    ap.add_argument("--only", default=None, help="カンマ区切りで一部の候補だけ実行")
    ap.add_argument("--full", action="store_true", help="価格キャッシュを全件取り直す")
    ap.add_argument("--tag", default="", help="出力ファイル名に付ける識別子")
    args = ap.parse_args()
    cfg = load_config()
    cfg["start_date"] = args.start
    prices, asof = load_prices(cfg, args.full)
    end = min(pd.Timestamp(args.end), asof) if args.end else asof
    names = [n for n in VARIANTS if not args.only or n in args.only.split(",")]
    rows = []
    for n in names:
        r = run_variant(cfg, prices, end, VARIANTS[n])
        r["候補"] = n
        rows.append(r)
        print(f"  {n:<28} リターン {r['リターン'] * 100:+6.1f}%  DD {r['最大DD'] * 100:6.1f}%  シャープ {r['シャープ'] or 0:5.2f}  "
              f"投資比率 {r['投資比率'] * 100:4.0f}%  取引 {r['取引数']:3d}  勝率 {(r['勝率'] or 0) * 100:3.0f}%  PF {r['PF'] or 0:4.2f}  保有 {r['平均保有'] or 0:4.0f}日", flush=True)
    df = pd.DataFrame(rows).set_index("候補")
    out = os.path.join(ROOT, "data", f"param_scan{('_' + args.tag) if args.tag else ''}.csv")
    df.to_csv(out, float_format="%.4f", encoding="utf-8-sig")
    print(f"\n期間 {args.start} → {end:%Y-%m-%d}  TOPIX {rows[0]['TOPIX'] * 100:+.1f}%  結果: {out}")


if __name__ == "__main__":
    main()
