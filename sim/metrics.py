"""運用成績の指標計算（資産推移・ベンチマーク比較・トレード統計）。"""
import math
import os

import numpy as np
import pandas as pd

from .engine import TRADE_COLS


def clean(obj):
    """JSON 化できる形に変換（NaN→None、numpy→Python）。"""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if (obj is None or math.isnan(float(obj)) or math.isinf(float(obj))) else float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def load_nav(state_dir: str) -> pd.DataFrame:
    p = os.path.join(state_dir, "nav.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return df


def load_trades(state_dir: str) -> pd.DataFrame:
    p = os.path.join(state_dir, "trades.csv")
    if not os.path.exists(p):
        return pd.DataFrame(columns=TRADE_COLS)
    df = pd.read_csv(p)
    for c in ("realized_pnl", "pnl_pct", "holding_days", "price", "amount", "shares", "entry_price"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def flows_of(nav: pd.DataFrame) -> pd.Series:
    """日ごとの入出金（nav.csv の flow 列。無ければ 0）。"""
    if "flow" in nav:
        return pd.to_numeric(nav["flow"], errors="coerce").fillna(0.0)
    return pd.Series(0.0, index=nav.index)


def twr_index(nav: pd.DataFrame, initial_cash: float) -> pd.Series:
    """入出金の影響を除いた成績（時間加重リターン）の指数。運用開始時 = 1.0。

    入金はその日の寄付前に入ったものとして扱う: r_t = nav_t / (nav_{t-1} + flow_t) - 1
    入金が無ければ nav / 初期資産 と一致する。
    """
    navs = nav["nav"].astype(float).to_numpy()
    flows = flows_of(nav).to_numpy()
    prev, level, out = float(initial_cash), 1.0, []
    for v, f in zip(navs, flows):
        base = prev + f
        level *= (v / base) if base > 0 else 1.0
        out.append(level)
        prev = v
    return pd.Series(out, index=nav.index)


def drawdown(series: pd.Series) -> pd.Series:
    peak = series.cummax()
    return series / peak - 1.0


def _ret_since(series: pd.Series, days: int):
    """days 日前（暦日）以降のリターン。データが足りなければ None。"""
    if series.empty:
        return None
    end = series.index[-1]
    start = end - pd.Timedelta(days=days)
    if series.index[0] > start:
        return None
    base = series[series.index <= start]
    if base.empty:
        return None
    return float(series.iloc[-1] / base.iloc[-1] - 1.0)


def trade_stats(trades: pd.DataFrame) -> dict:
    out = {"n_closed": 0, "n_buys": 0, "n_sells": 0}
    if trades is None or trades.empty:
        return out
    out["n_buys"] = int((trades["side"] == "BUY").sum())
    out["n_sells"] = int((trades["side"] == "SELL").sum())
    closed = trades[trades["side"] == "SELL"].copy()
    closed = closed[closed["realized_pnl"].notna()]
    n = len(closed)
    out["n_closed"] = n
    if n == 0:
        return out
    wins = closed[closed["realized_pnl"] > 0]
    losses = closed[closed["realized_pnl"] <= 0]
    gp = float(wins["realized_pnl"].sum())
    gl = float(losses["realized_pnl"].sum())
    out.update({
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "avg_win": float(wins["realized_pnl"].mean()) if len(wins) else None,
        "avg_loss": float(losses["realized_pnl"].mean()) if len(losses) else None,
        "avg_win_pct": float(wins["pnl_pct"].mean()) if len(wins) else None,
        "avg_loss_pct": float(losses["pnl_pct"].mean()) if len(losses) else None,
        "profit_factor": (gp / abs(gl)) if gl < 0 else None,
        "expectancy": float(closed["realized_pnl"].mean()),
        "realized_total": float(closed["realized_pnl"].sum()),
        "avg_holding_days": float(closed["holding_days"].mean()),
    })
    best = closed.loc[closed["realized_pnl"].idxmax()]
    worst = closed.loc[closed["realized_pnl"].idxmin()]
    out["best"] = {"name": best["name"], "code": best["code"], "pnl": float(best["realized_pnl"]), "pnl_pct": float(best["pnl_pct"]), "date": best["date"]}
    out["worst"] = {"name": worst["name"], "code": worst["code"], "pnl": float(worst["realized_pnl"]), "pnl_pct": float(worst["pnl_pct"]), "date": worst["date"]}
    by_type = {}
    for et, g in closed.groupby(closed["exit_type"].fillna("").astype(str)):
        by_type[et or "other"] = {"n": len(g), "avg_pnl": float(g["realized_pnl"].mean()),
                                  "win_rate": float((g["realized_pnl"] > 0).mean())}
    out["by_exit_type"] = by_type
    return out


def compute_metrics(nav: pd.DataFrame, trades: pd.DataFrame, benchmarks: list, initial_cash: float) -> dict:
    m = {"initial": float(initial_cash), "has_data": not nav.empty}
    if nav.empty:
        m.update({"final": float(initial_cash), "total_return": 0.0, "bench": {}, "periods": {}, "monthly": [],
                  "trades": trade_stats(trades)})
        return clean(m)
    navs = nav["nav"].astype(float)
    flows = flows_of(nav)
    perf = twr_index(nav, initial_cash) * float(initial_cash)   # 入金が無かった場合の資産額に換算した成績
    contributed = float(initial_cash) + float(flows.sum())
    idx = navs.index
    final = float(navs.iloc[-1])
    m["contributed"] = contributed
    m["flows_total"] = float(flows.sum())
    m["core_value"] = float(pd.to_numeric(nav["core_value"], errors="coerce").fillna(0.0).iloc[-1]) if "core_value" in nav else 0.0
    m["start_date"], m["end_date"] = idx[0], idx[-1]
    m["n_days"] = len(navs)
    m["final"] = final
    m["cash"] = float(nav["cash"].iloc[-1])
    m["positions_value"] = float(nav["positions_value"].iloc[-1])
    m["n_positions"] = int(nav["n_positions"].iloc[-1])
    m["exposure"] = float(nav["exposure"].iloc[-1])
    m["regime"] = int(nav["regime"].iloc[-1]) if "regime" in nav else None
    m["total_return"] = float(perf.iloc[-1]) / initial_cash - 1.0
    m["total_pnl"] = final - contributed
    cal_days = (idx[-1] - idx[0]).days
    m["cagr"] = (float(perf.iloc[-1]) / initial_cash) ** (365.0 / cal_days) - 1.0 if cal_days >= 30 and final > 0 else None
    rets = perf.pct_change().dropna()
    if len(rets) >= 5 and rets.std() > 0:
        m["ann_vol"] = float(rets.std() * math.sqrt(252))
        m["sharpe"] = float(rets.mean() / rets.std() * math.sqrt(252))
    else:
        m["ann_vol"], m["sharpe"] = None, None
    dd = drawdown(perf)
    m["max_dd"] = float(dd.min())
    m["max_dd_date"] = dd.idxmin()
    m["current_dd"] = float(dd.iloc[-1])
    m["calmar"] = (m["cagr"] / abs(m["max_dd"])) if (m["cagr"] is not None and m["max_dd"] < 0) else None
    m["exposure_avg"] = float(nav["exposure"].mean())
    if len(navs) > 1:
        m["daily_change"] = float(navs.iloc[-1] - navs.iloc[-2] - flows.iloc[-1])
        m["daily_change_pct"] = float(perf.iloc[-1] / perf.iloc[-2] - 1.0)
    else:
        m["daily_change"], m["daily_change_pct"] = 0.0, 0.0

    # ベンチマーク（同期間）
    m["bench"] = {}
    for b in benchmarks:
        if b in nav:
            s = nav[b].astype(float)
            m["bench"][b] = float(s.iloc[-1] / s.iloc[0] - 1.0)
    primary = benchmarks[0] if benchmarks else None
    m["primary_bench"] = primary
    m["excess"] = (m["total_return"] - m["bench"][primary]) if primary in m["bench"] else None

    # 期間別リターン
    periods = {}
    for label, days in (("1週", 7), ("1ヶ月", 30), ("3ヶ月", 91), ("6ヶ月", 182), ("1年", 365)):
        r = _ret_since(perf, days)
        if r is None:
            continue
        rb = _ret_since(nav[primary].astype(float), days) if primary in nav else None
        periods[label] = {"port": r, "bench": rb}
    m["periods"] = periods

    # 月次リターン
    monthly = []
    grp = perf.groupby(perf.index.to_period("M"))
    bench_s = nav[primary].astype(float) if primary in nav else None
    prev_p, prev_b = float(initial_cash), (float(bench_s.iloc[0]) if bench_s is not None else None)
    for per, g in grp:
        last_p = float(g.iloc[-1])
        entry = {"month": str(per), "port": last_p / prev_p - 1.0}
        if bench_s is not None:
            last_b = float(bench_s[bench_s.index.to_period("M") == per].iloc[-1])
            entry["bench"] = last_b / prev_b - 1.0
            prev_b = last_b
        monthly.append(entry)
        prev_p = last_p
    m["monthly"] = monthly

    m["trades"] = trade_stats(trades)
    return clean(m)


def series_for_chart(nav: pd.DataFrame, benchmarks: list, initial_cash: float) -> dict:
    """ダッシュボード用の系列（指数化 100）。"""
    if nav.empty:
        return {"dates": [], "port": [], "bench": {}, "drawdown": [], "cash": [], "exposure": []}
    navs = nav["nav"].astype(float)
    perf = twr_index(nav, initial_cash)
    out = {
        "dates": [d.strftime("%Y-%m-%d") for d in nav.index],
        "port": [round(v * 100, 3) for v in perf],
        "nav": [round(v) for v in navs],
        "drawdown": [round(v * 100, 3) for v in drawdown(perf)],
        "exposure": [round(float(v) * 100, 1) for v in nav["exposure"]],
        "regime": [int(v) for v in nav["regime"]] if "regime" in nav else [],
        "bench": {},
    }
    for b in benchmarks:
        if b in nav:
            s = nav[b].astype(float)
            out["bench"][b] = [round(v / s.iloc[0] * 100, 3) for v in s]
    return out
