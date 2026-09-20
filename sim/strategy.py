"""売買ルール（trend_momentum_v1.1）。数値は config.json が正。

考え方（トレンドフォロー × モメンタム × リスク管理）
  1. 相場環境フィルタ: TOPIX ETF が 200 日線より上のときだけ新規買い
  2. 銘柄選定: 上昇トレンド（終値 > 50日線 > 200日線）かつ 12-1 ヶ月モメンタム上位、
     20 日高値圏、十分な売買代金
  3. 資金管理: 1 トレードの想定損失 = 資産の 1%（損切り幅 = 3×ATR）、
     1 銘柄の上限 = 資産の 12%、最大 10 銘柄、同一業種 4 銘柄まで、レバレッジなし
  4. 手仕舞い: トレーリングストップ（最高値 − 3×ATR）／200日線割れ／モメンタム失速

全ての判断は「その日の終値」で行い、約定は「翌営業日の寄付」で行う（先読みなし）。
"""
import math

from .universe import SECTORS, group_of


def fmt_yen(x: float) -> str:
    return f"{x:,.0f}円"


def fmt_pct(x: float, signed: bool = True) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def size_position(close: float, atr: float, equity: float, cash_available: float, p: dict, broker: dict):
    """株数を決める。戻り値: (株数, 説明) 。買えない場合は (0, 理由)。"""
    lot = int(broker.get("lot_size", 1)) or 1
    stop_dist = p["atr_stop_mult"] * atr
    if stop_dist <= 0:
        return 0, "ATR が計算できない"
    risk_budget = equity * p["risk_per_trade"]
    risk_shares = int(risk_budget / stop_dist)
    cap_shares = int(equity * p["max_position_weight"] / close)
    cash_shares = int(cash_available / (close * (1 + broker.get("slippage_rate", 0))))
    shares = min(risk_shares, cap_shares, cash_shares)
    shares = (shares // lot) * lot
    if shares < lot:
        why = []
        if risk_shares < lot:
            why.append("値動きが大きすぎてリスク許容内で買えない")
        if cap_shares < lot:
            why.append("1銘柄上限に収まらない")
        if cash_shares < lot:
            why.append("現金不足")
        return 0, "・".join(why) or "株数が0"
    if shares * close < broker.get("min_order_jpy", 0):
        return 0, "最低発注額未満"
    binding = "リスク許容" if shares == risk_shares else ("1銘柄上限" if shares == cap_shares else "現金残高")
    return shares, binding


def evaluate_exits(positions: dict, bars: dict, p: dict) -> list:
    """保有銘柄ごとに手仕舞い判定。bars[ticker] は当日の指標付き足（dict）。"""
    signals = []
    for t, pos in positions.items():
        b = bars.get(t)
        if b is None:
            continue
        close = b["Close"]
        reasons = []
        exit_type = None
        stop = pos.get("stop_price")
        if _ok(stop) and close < stop:
            reasons.append(
                f"トレーリングストップ抵触: 終値 {fmt_yen(close)} < 損切りライン {fmt_yen(stop)}"
                f"（最高値 {fmt_yen(pos['highest_close'])} から {p['atr_stop_mult']}×ATR 下落）"
            )
            exit_type = "stop"
        if p.get("exit_on_trend", True) and _ok(b.get("sma_slow")) and close < b["sma_slow"]:
            reasons.append(f"{p['sma_slow']}日移動平均線（{fmt_yen(b['sma_slow'])}）を下回り、長期トレンドが崩れた")
            exit_type = exit_type or "trend"
        if p.get("exit_on_momentum", True) and _ok(b.get("mom")) and b["mom"] < 0:
            reasons.append(f"12ヶ月モメンタムがマイナス（{fmt_pct(b['mom'])}）に転じた")
            exit_type = exit_type or "momentum"
        if reasons:
            pnl_pct = close / pos["avg_price"] - 1.0
            signals.append({
                "ticker": t,
                "side": "SELL",
                "shares": pos["shares"],
                "signal_close": close,
                "exit_type": exit_type,
                "reason": "；".join(reasons) + f"。現在の含み損益 {fmt_pct(pnl_pct)}",
            })
    return signals


def rank_candidates(bars: dict, p: dict, exclude: set) -> list:
    """新規買い候補を作る（フィルタ → モメンタム順）。戻り値は (ticker, bar, rank) のリスト。"""
    rows = []
    for t, b in bars.items():
        if t in exclude:
            continue
        need = ("Close", "atr", "sma_fast", "sma_slow", "mom", "turnover20", "high_20")
        if not all(_ok(b.get(k)) for k in need):
            continue
        if not (b["Close"] > b["sma_fast"] > b["sma_slow"]):
            continue
        if b["mom"] <= 0:
            continue
        if b["turnover20"] < p["min_avg_turnover_jpy"]:
            continue
        if b["Close"] < b["high_20"] * (1.0 - p.get("max_dist_from_high20", 0.05)):  # 高値から離れた押し目は待つ
            continue
        rows.append((t, b))
    rows.sort(key=lambda x: x[1]["mom"], reverse=True)
    return [(t, b, i + 1) for i, (t, b) in enumerate(rows)]


def evaluate_entries(bars: dict, positions: dict, pending: list, cash_available: float,
                     equity: float, p: dict, broker: dict, regime_on: bool,
                     cooldown: set, names: dict) -> list:
    """新規買いシグナル。"""
    if not regime_on:
        return []
    held = set(positions) | {o["ticker"] for o in pending} | set(cooldown)
    slots = p["max_positions"] - len(positions) - sum(1 for o in pending if o["side"] == "BUY")
    if slots <= 0:
        return []
    ranked = rank_candidates(bars, p, held)
    signals = []
    cash_left = cash_available
    # 業種の集中を避ける（同一業種の保有＋発注予定を数える）
    max_sector = p.get("max_per_sector", 0)
    sector_count = {}
    max_group = p.get("max_per_group", 0)      # 大分類（金融など）の上限。0=無効
    max_new = p.get("max_new_per_day", 0)      # 1日に出す新規買いの上限（分散エントリー）。0=無効
    group_count = {}
    for t in list(positions) + [o["ticker"] for o in pending if o["side"] == "BUY"]:
        s = SECTORS.get(t, "その他")
        sector_count[s] = sector_count.get(s, 0) + 1
        group_count[group_of(t)] = group_count.get(group_of(t), 0) + 1
    for t, b, rank in ranked[: p["top_n_candidates"]]:
        if slots <= 0 or cash_left <= 0:
            break
        sec = SECTORS.get(t, "その他")
        if max_sector and sector_count.get(sec, 0) >= max_sector:
            continue
        if max_group and group_count.get(group_of(t), 0) >= max_group:
            continue
        if max_new and len(signals) >= max_new:
            break
        shares, note = size_position(b["Close"], b["atr"], equity, cash_left, p, broker)
        if shares <= 0:
            continue
        sector_count[sec] = sector_count.get(sec, 0) + 1
        group_count[group_of(t)] = group_count.get(group_of(t), 0) + 1
        cost = shares * b["Close"]
        stop = b["Close"] - p["atr_stop_mult"] * b["atr"]
        weight = cost / equity
        reason = (
            f"12ヶ月モメンタム {fmt_pct(b['mom'])}（候補内 {rank} 位）、"
            f"株価 {fmt_yen(b['Close'])} > 50日線 {fmt_yen(b['sma_fast'])} > 200日線 {fmt_yen(b['sma_slow'])} の上昇トレンド、"
            f"20日高値圏（直近1ヶ月 {fmt_pct(b['ret_1m'])}）、業種 {sec}（同業種 {sector_count[sec]} 銘柄目）。"
            f"想定損切り {fmt_yen(stop)}（{p['atr_stop_mult']}×ATR={fmt_yen(p['atr_stop_mult'] * b['atr'])} 下）、"
            f"資産の {p['risk_per_trade'] * 100:.0f}% をリスク上限として {shares} 株（評価 {weight * 100:.1f}%、{note}で決定）"
        )
        signals.append({
            "ticker": t,
            "side": "BUY",
            "shares": shares,
            "signal_close": b["Close"],
            "planned_stop": stop,
            "atr": b["atr"],
            "reason": reason,
        })
        cash_left -= cost
        slots -= 1
    return signals
