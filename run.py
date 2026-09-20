"""株式運用シミュレーション CLI。

  python run.py step                 日次処理（価格更新 → 約定 → 判断 → 記録 → 反省 → 通知 → ダッシュボード）
  python run.py backtest --start 2024-09-02   同じルールで過去を検証（data/backtest/ に出力）
  python run.py report               ダッシュボード(dashboard.html)だけ再生成
  python run.py review [--kind weekly|monthly|manual]   反省ノートを今すぐ作成
  python run.py status               現在の状態を表示
  python run.py notify-test          通知（トースト）の動作確認
  python run.py reset --yes          運用状態をアーカイブして初期化（価格キャッシュは残す）
"""
import argparse
import json
import os
import shutil
import sys
from copy import deepcopy
from datetime import datetime

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sim.config import load_config, DATA_DIR  # noqa: E402
from sim.universe import tickers, name_of, code_of  # noqa: E402
from sim import market  # noqa: E402
from sim.engine import Simulator  # noqa: E402
from sim.metrics import load_nav, load_trades, compute_metrics  # noqa: E402
from sim.notify import notify_events, toast  # noqa: E402
from sim.review import run_review, should_review  # noqa: E402
from sim.report import build_dashboard  # noqa: E402

LIVE_DIR = DATA_DIR
BT_DIR = os.path.join(DATA_DIR, "backtest")
LOG_DIR = os.path.join(ROOT, "logs")


def log(msg: str = "") -> None:
    print(msg, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"run_{datetime.now():%Y%m}.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def load_prices(cfg: dict, force_full: bool = False):
    allt = list(dict.fromkeys(tickers() + list(cfg["benchmarks"]) + [cfg["strategy"]["regime_ticker"]]))
    log("株価データを更新中…")
    prices = market.update_prices(allt, cfg["data"]["history_start"], force_full, log)
    prices = market.drop_partial_today(prices)
    prices = market.repair_glitches(prices, log)
    ref = cfg["strategy"]["regime_ticker"]
    if ref not in prices:
        raise SystemExit(f"基準銘柄 {ref} のデータが取得できません")
    asof = prices[ref].index[-1]
    stale = cfg["data"].get("stale_days", 10)
    ok = {}
    for t, df in prices.items():
        if (asof - df.index[-1]).days > stale:
            log(f"  除外（データ停止・上場廃止の可能性）: {t} {name_of(t)}")
            continue
        ok[t] = df
    log(f"  取得 {len(ok)} 銘柄、直近営業日 {asof:%Y-%m-%d}")
    return ok, asof


def summary_line(m: dict) -> str:
    return (f"総資産 {m['final']:,.0f}円（前日比 {m.get('daily_change', 0):+,.0f}円 / "
            f"運用開始来 {m['total_return'] * 100:+.2f}%）")


def cmd_step(args):
    cfg = load_config()
    prices, asof = load_prices(cfg, args.full)
    # 配当・株式分割（保有・発注中・直近に売った銘柄とベンチマークだけ調べる）
    st0 = {}
    sp = os.path.join(LIVE_DIR, "state.json")
    if os.path.exists(sp):
        with open(sp, "r", encoding="utf-8") as f:
            st0 = json.load(f)
    watch = sorted(set(st0.get("positions", {})) | {o["ticker"] for o in st0.get("pending_orders", [])}
                   | {c["ticker"] for c in st0.get("closed_positions", [])} | set(cfg["benchmarks"]))
    expected = []
    ep = os.path.join(DATA_DIR, "expected_actions.json")
    if os.path.exists(ep):
        with open(ep, "r", encoding="utf-8") as f:
            expected = json.load(f)
    actions = market.merge_expected_actions(
        market.fetch_actions(watch, st0.get("start_date", cfg["start_date"]), log), expected)
    prices = market.adjust_unadjusted_splits(prices, actions, log)
    sim = Simulator(cfg, prices, LIVE_DIR, log)
    sim.actions = actions
    cp = os.path.join(DATA_DIR, "cashflows.json")     # 追加入金の指示（オーナーの決定を記録したもの）
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            sim.cashflows = json.load(f)
    days = sim.run_until(asof)
    if days:
        log(f"処理した営業日: {', '.join(d.strftime('%m/%d') for d in days)}")
    else:
        log("新しい営業日はありません（処理済み）。")

    # 反省ノート（金曜／月末）。運用開始直後（3営業日未満）は比較できる材料がないので作らない
    due = []
    for D in days:
        for k in should_review(D, sim.tdays, cfg):
            if k not in due:
                due.append(k)
    if len(load_nav(LIVE_DIR)) >= 3:
        for kind in due:
            run_review(cfg, sim, kind, days[-1], log)
    sim.save_state()

    nav, trades = load_nav(LIVE_DIR), load_trades(LIVE_DIR)
    m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
    line = summary_line(m)
    notify_events(cfg, sim.events, line, log)
    n_dec = sum(1 for e in sim.events if e["type"] in ("BUY_SIGNAL", "SELL_SIGNAL", "FILLED_BUY", "FILLED_SELL"))
    log(f"判断・約定 {n_dec} 件を通知しました。" if n_dec else "本日は新しい売買判断はありません。")
    out = build_dashboard(cfg, log)
    log(line)
    log(f"ダッシュボード: {out}")
    for e in sim.events:
        if e["type"] in ("BUY_SIGNAL", "SELL_SIGNAL", "FILLED_BUY", "FILLED_SELL", "CANCELLED", "REVIEW", "PARAM_CHANGE",
                         "DIVIDEND", "SPLIT", "CASHFLOW"):
            log("  " + (e.get("text") or e.get("reason", "")))


def cmd_backtest(args):
    cfg = load_config()
    prices, asof = load_prices(cfg)
    cfg_bt = deepcopy(cfg)
    cfg_bt["start_date"] = args.start
    end = min(pd.Timestamp(args.end), asof) if args.end else asof
    os.makedirs(BT_DIR, exist_ok=True)
    for name in ("state.json", "trades.csv", "nav.csv", "events.jsonl", "summary.json"):
        p = os.path.join(BT_DIR, name)
        if os.path.exists(p):
            os.remove(p)  # OneDrive 配下ではフォルダ削除が拒否されることがあるためファイル単位で消す
    log(f"バックテスト: {args.start} → {end:%Y-%m-%d}（初期資産 {cfg['initial_cash']:,}円、戦略 {cfg['strategy']['name']}）")
    sim = Simulator(cfg_bt, prices, BT_DIR, log=lambda *a, **k: None, label="backtest")
    sim.run_until(end)
    nav, trades = load_nav(BT_DIR), load_trades(BT_DIR)
    m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": args.start, "end": end.strftime("%Y-%m-%d"),
        "strategy": cfg["strategy"], "broker": cfg["broker"], "initial_cash": cfg["initial_cash"],
        "metrics": m,
    }
    with open(os.path.join(BT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    t = m["trades"]
    log(f"  最終資産 {m['final']:,.0f}円  リターン {m['total_return'] * 100:+.1f}%  "
        f"TOPIX {m['bench'].get(cfg['benchmarks'][0], 0) * 100:+.1f}%  最大DD {m['max_dd'] * 100:.1f}%  "
        f"シャープ {m['sharpe'] if m['sharpe'] is None else round(m['sharpe'], 2)}")
    log(f"  取引 買{t.get('n_buys', 0)}/売{t.get('n_sells', 0)}  勝率 {t.get('win_rate', 0) * 100:.0f}%  "
        f"PF {t.get('profit_factor')}  平均保有 {t.get('avg_holding_days', 0):.0f}日")
    build_dashboard(cfg, log)


def cmd_report(args):
    cfg = load_config()
    log(f"ダッシュボード: {build_dashboard(cfg, log)}")


def cmd_review(args):
    cfg = load_config()
    prices, asof = load_prices(cfg)
    sim = Simulator(cfg, prices, LIVE_DIR, log)
    asof_done = pd.Timestamp(sim.state["last_processed_date"]) if sim.state.get("last_processed_date") else asof
    r = run_review(cfg, sim, args.kind, asof_done, log, force=True)
    sim.save_state()
    if r:
        nav, trades = load_nav(LIVE_DIR), load_trades(LIVE_DIR)
        m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
        notify_events(cfg, sim.events, summary_line(m), log)
        print(open(r["path"], encoding="utf-8").read())
    build_dashboard(cfg, log)


def cmd_status(args):
    cfg = load_config()
    nav, trades = load_nav(LIVE_DIR), load_trades(LIVE_DIR)
    m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
    print(summary_line(m))
    sp = os.path.join(LIVE_DIR, "state.json")
    if os.path.exists(sp):
        st = json.load(open(sp, encoding="utf-8"))
        print(f"処理済み: {st.get('last_processed_date')}  現金 {st['cash']:,.0f}円  保有 {len(st['positions'])} 銘柄  未約定 {len(st['pending_orders'])} 件")
        for t, p in st["positions"].items():
            lc = p.get("last_close", p["avg_price"])
            stop = f"損切{p['stop_price']:>9,.0f}" if p.get("stop_price") is not None else "コア（買い持ち）"
            print(f"  {code_of(t)} {name_of(t):<14} {p['shares']:>5}株 取得{p['avg_price']:>9,.0f} 現在{lc:>9,.0f} ({(lc / p['avg_price'] - 1) * 100:+.1f}%) {stop}")
        for o in st["pending_orders"]:
            qty = f"{o['shares']}株" if o.get("shares") is not None else f"{o.get('budget', 0):,.0f}円ぶん"
            print(f"  [未約定] {o['side']} {code_of(o['ticker'])} {name_of(o['ticker'])} {qty}（{o['signal_date']} 判断）")
    print(json.dumps({k: m[k] for k in ("total_return", "max_dd", "sharpe", "excess") if k in m}, ensure_ascii=False))


def cmd_notify_test(args):
    ok = toast("株式シミュレーション", "通知テスト: この表示が出れば Windows 通知は正常です。")
    print("トースト通知:", "OK" if ok else "失敗")


def cmd_reset(args):
    if not args.yes:
        print("運用状態を初期化するには --yes を付けてください（価格キャッシュと反省ノートはアーカイブされます）")
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    arch = os.path.join(DATA_DIR, "archive", stamp)
    os.makedirs(arch, exist_ok=True)
    for name in ("state.json", "trades.csv", "nav.csv", "events.jsonl"):  # ルール変更履歴(params_history)は資産ではなく知識なので残す
        p = os.path.join(LIVE_DIR, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(arch, name))
    jd = os.path.join(DATA_DIR, "journal")
    if os.path.isdir(jd) and os.listdir(jd):
        shutil.move(jd, os.path.join(arch, "journal"))
        os.makedirs(jd, exist_ok=True)
    nf = os.path.join(ROOT, "notifications.md")
    if os.path.exists(nf):
        shutil.move(nf, os.path.join(arch, "notifications.md"))
    print(f"初期化しました。以前の記録: {arch}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="株式運用シミュレーション")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("step", help="日次処理")
    s.add_argument("--full", action="store_true", help="価格キャッシュを全件取り直す")
    b = sub.add_parser("backtest", help="過去検証")
    b.add_argument("--start", default="2024-09-02")
    b.add_argument("--end", default=None)
    sub.add_parser("report", help="ダッシュボード再生成")
    r = sub.add_parser("review", help="反省ノートを作成")
    r.add_argument("--kind", default="manual", choices=["weekly", "monthly", "manual"])
    sub.add_parser("status", help="状態表示")
    sub.add_parser("notify-test", help="通知テスト")
    rs = sub.add_parser("reset", help="運用状態の初期化")
    rs.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    cmd = args.cmd or "step"
    {"step": cmd_step, "backtest": cmd_backtest, "report": cmd_report, "review": cmd_review,
     "status": cmd_status, "notify-test": cmd_notify_test, "reset": cmd_reset}[cmd](args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 失敗も記録してオーナーに知らせる
        log(f"エラー: {e!r}")
        try:
            toast("株式シミュレーション: エラー", str(e)[:200])
        except Exception:
            pass
        raise
