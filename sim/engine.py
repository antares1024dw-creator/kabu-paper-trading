"""シミュレーション・エンジン（台帳・約定・日次処理）。

状態は state_dir 配下に保存する:
  state.json   現金・保有・未約定注文・処理済み日付
  trades.csv   約定履歴
  nav.csv      日次の資産推移（ベンチマーク終値つき）
  events.jsonl 判断・約定・お知らせのログ（通知の元データ）
"""
import csv
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

from . import strategy
from .indicators import compute_indicators
from .universe import name_of, code_of

BAR_FIELDS = ("Open", "High", "Low", "Close", "Volume", "atr", "sma_fast", "sma_slow",
              "mom", "ret_1m", "ret_3m", "turnover20", "high_20", "vol20")

TRADE_COLS = ["id", "date", "ticker", "code", "name", "side", "shares", "price", "amount",
              "commission", "realized_pnl", "pnl_pct", "holding_days", "entry_price",
              "exit_type", "signal_date", "reason"]


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


class Simulator:
    def __init__(self, cfg: dict, prices: dict, state_dir: str, log=print, label: str = "live"):
        self.cfg = cfg
        self.p = cfg["strategy"]
        self.broker = cfg["broker"]
        self.state_dir = state_dir
        self.log = log
        self.label = label
        os.makedirs(state_dir, exist_ok=True)

        self.regime_ticker = self.p["regime_ticker"]
        self.benchmarks = list(cfg["benchmarks"])
        self.universe = [t for t in prices if t not in self.benchmarks]

        # 指標を事前計算し、高速アクセス用に numpy 配列へ
        self.arr, self.idx = {}, {}
        for t, df in prices.items():
            ind = compute_indicators(df, self.p) if t in self.universe else df.copy()
            if t == self.regime_ticker:
                ind["regime_sma"] = ind["Close"].rolling(self.p["regime_sma"]).mean()
            self.arr[t] = {c: ind[c].to_numpy(dtype=float) for c in ind.columns}
            self.idx[t] = {ts: i for i, ts in enumerate(ind.index)}
        self.tdays = list(prices[self.regime_ticker].index)
        self.tday_ord = {ts: i for i, ts in enumerate(self.tdays)}

        self.state = self._load_state()
        self.events = []

    # ---------- 状態 ----------
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def _load_state(self) -> dict:
        p = self._path("state.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        start = pd.Timestamp(self.cfg["start_date"])
        prev = [ts for ts in self.tdays if ts < start]
        return {
            "initial_cash": float(self.cfg["initial_cash"]),
            "cash": float(self.cfg["initial_cash"]),
            "positions": {},
            "pending_orders": [],
            "last_processed_date": _d(prev[-1]) if prev else None,
            "start_date": _d(start),
            "trade_seq": 0,
            "cooldown": {},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    def save_state(self):
        with open(self._path("state.json"), "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ---------- データアクセス ----------
    def bar(self, t: str, D) -> dict:
        i = self.idx.get(t, {}).get(D)
        if i is None:
            return None
        a = self.arr[t]
        return {k: float(a[k][i]) for k in BAR_FIELDS if k in a}

    def regime_on(self, D) -> bool:
        i = self.idx[self.regime_ticker].get(D)
        if i is None:
            return False
        a = self.arr[self.regime_ticker]
        sma = a["regime_sma"][i]
        return bool(not np.isnan(sma) and a["Close"][i] > sma)

    def last_close(self, t: str, D) -> float:
        """D 以前の直近終値（休場・欠損対策）。"""
        idx = self.idx.get(t)
        if not idx:
            return float("nan")
        if D in idx:
            return float(self.arr[t]["Close"][idx[D]])
        keys = [ts for ts in idx if ts <= D]
        return float(self.arr[t]["Close"][idx[max(keys)]]) if keys else float("nan")

    def positions_value(self, D) -> float:
        return sum(pos["shares"] * self.last_close(t, D) for t, pos in self.state["positions"].items())

    def equity(self, D) -> float:
        return self.state["cash"] + self.positions_value(D)

    # ---------- 記録 ----------
    def _append_csv(self, name, cols, row):
        p = self._path(name)
        new = not os.path.exists(p)
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow({c: row.get(c, "") for c in cols})

    def event(self, D, etype: str, **kw):
        ev = {"date": _d(D), "type": etype, "ts": datetime.now().isoformat(timespec="seconds")}
        ev.update(kw)
        if "ticker" in ev and "name" not in ev:
            ev["name"] = name_of(ev["ticker"])
            ev["code"] = code_of(ev["ticker"])
        self.events.append(ev)
        with open(self._path("events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    # ---------- 日次処理 ----------
    def pending_days(self, order, D) -> int:
        s = pd.Timestamp(order["signal_date"])
        return self.tday_ord.get(D, 0) - self.tday_ord.get(s, 0)

    def fill_orders(self, D):
        st = self.state
        keep = []
        slip = self.broker.get("slippage_rate", 0.0)
        comm_rate = self.broker.get("commission_rate", 0.0)
        # 売りを先に処理して現金を確保
        orders = sorted(st["pending_orders"], key=lambda o: 0 if o["side"] == "SELL" else 1)
        for o in orders:
            t = o["ticker"]
            b = self.bar(t, D)
            if b is None or np.isnan(b["Open"]):
                if self.pending_days(o, D) >= self.broker.get("fill_timeout_days", 3):
                    self.event(D, "CANCELLED", ticker=t, side=o["side"], shares=o["shares"],
                               reason="約定できる価格が取得できなかったため取消")
                else:
                    keep.append(o)
                continue
            if o["side"] == "SELL":
                pos = st["positions"].get(t)
                if not pos:
                    continue
                price = b["Open"] * (1 - slip)
                shares = pos["shares"]
                amount = price * shares
                comm = amount * comm_rate
                pnl = amount - comm - pos["avg_price"] * shares
                hold = (pd.Timestamp(D) - pd.Timestamp(pos["entry_date"])).days
                st["cash"] += amount - comm
                st["trade_seq"] += 1
                row = {
                    "id": st["trade_seq"], "date": _d(D), "ticker": t, "code": code_of(t), "name": name_of(t),
                    "side": "SELL", "shares": shares, "price": round(price, 1), "amount": round(amount),
                    "commission": round(comm), "realized_pnl": round(pnl), "pnl_pct": round(pnl / (pos["avg_price"] * shares), 4),
                    "holding_days": hold, "entry_price": round(pos["avg_price"], 1), "exit_type": o.get("exit_type", ""),
                    "signal_date": o["signal_date"], "reason": o["reason"],
                }
                self._append_csv("trades.csv", TRADE_COLS, row)
                del st["positions"][t]
                st["cooldown"][t] = _d(D)
                self.event(D, "FILLED_SELL", ticker=t, shares=shares, price=round(price, 1), amount=round(amount),
                           realized_pnl=round(pnl), pnl_pct=round(pnl / (pos["avg_price"] * shares), 4),
                           holding_days=hold, exit_type=o.get("exit_type", ""), reason=o["reason"],
                           text=f"【売却約定】{name_of(t)}({code_of(t)}) {shares}株 @{price:,.1f}円 "
                                f"実現損益 {pnl:+,.0f}円（{pnl / (pos['avg_price'] * shares) * 100:+.1f}%、{hold}日保有）")
            else:
                price = b["Open"] * (1 + slip)
                shares = int(o["shares"])
                lot = int(self.broker.get("lot_size", 1)) or 1
                max_afford = int(st["cash"] / (price * (1 + comm_rate)))
                if max_afford < shares:
                    shares = (max_afford // lot) * lot
                if shares < lot or shares * price < self.broker.get("min_order_jpy", 0):
                    self.event(D, "CANCELLED", ticker=t, side="BUY", shares=o["shares"],
                               reason="現金不足のため買い注文を取消")
                    continue
                amount = price * shares
                comm = amount * comm_rate
                st["cash"] -= amount + comm
                st["trade_seq"] += 1
                atr = o.get("atr") or (b["atr"] if not np.isnan(b.get("atr", float("nan"))) else price * 0.02)
                st["positions"][t] = {
                    "shares": shares, "avg_price": price, "cost": amount + comm,
                    "entry_date": _d(D), "entry_price": price, "highest_close": price,
                    "stop_price": price - self.p["atr_stop_mult"] * atr, "atr": atr,
                    "signal_date": o["signal_date"], "reason": o["reason"],
                }
                row = {
                    "id": st["trade_seq"], "date": _d(D), "ticker": t, "code": code_of(t), "name": name_of(t),
                    "side": "BUY", "shares": shares, "price": round(price, 1), "amount": round(amount),
                    "commission": round(comm), "realized_pnl": "", "pnl_pct": "", "holding_days": "",
                    "entry_price": round(price, 1), "exit_type": "", "signal_date": o["signal_date"], "reason": o["reason"],
                }
                self._append_csv("trades.csv", TRADE_COLS, row)
                self.event(D, "FILLED_BUY", ticker=t, shares=shares, price=round(price, 1), amount=round(amount),
                           stop_price=round(st["positions"][t]["stop_price"], 1), reason=o["reason"],
                           text=f"【買付約定】{name_of(t)}({code_of(t)}) {shares}株 @{price:,.1f}円（{amount:,.0f}円）"
                                f" 損切り目安 {st['positions'][t]['stop_price']:,.0f}円")
        st["pending_orders"] = keep

    def update_positions(self, D):
        for t, pos in self.state["positions"].items():
            b = self.bar(t, D)
            if b is None:
                continue
            if b["Close"] > pos["highest_close"]:
                pos["highest_close"] = b["Close"]
            atr = b["atr"] if not np.isnan(b.get("atr", float("nan"))) else pos["atr"]
            pos["atr"] = atr
            new_stop = pos["highest_close"] - self.p["atr_stop_mult"] * atr
            pos["stop_price"] = max(pos.get("stop_price", new_stop), new_stop)  # ストップは切り上げのみ
            pos["last_close"] = b["Close"]

    def record_nav(self, D):
        st = self.state
        pv = self.positions_value(D)
        nav = st["cash"] + pv
        row = {
            "date": _d(D), "cash": round(st["cash"]), "positions_value": round(pv), "nav": round(nav),
            "n_positions": len(st["positions"]), "exposure": round(pv / nav, 4) if nav else 0,
            "regime": int(self.regime_on(D)),
        }
        # ベンチマークは「運用開始日＝100」の指数として記録する（生の株価データを記録・公開しないため）
        base = st.setdefault("bench_base", {})
        for bt in self.benchmarks:
            c = self.last_close(bt, D)
            if bt not in base and not np.isnan(c):
                base[bt] = c
            row[bt] = round(c / base[bt] * 100, 4) if bt in base and not np.isnan(c) else ""
        cols = ["date", "cash", "positions_value", "nav", "n_positions", "exposure", "regime"] + self.benchmarks
        self._append_csv("nav.csv", cols, row)
        return row

    def active_cooldown(self, D) -> set:
        out = set()
        n = self.p.get("reentry_cooldown_days", 0)
        for t, d in list(self.state["cooldown"].items()):
            since = self.tday_ord.get(D, 0) - self.tday_ord.get(pd.Timestamp(d), 0)
            if since < n:
                out.add(t)
            else:
                del self.state["cooldown"][t]
        return out

    def generate_signals(self, D):
        st = self.state
        bars = {t: b for t in self.universe if (b := self.bar(t, D)) is not None}
        regime = self.regime_on(D)
        equity = self.equity(D)

        exits = strategy.evaluate_exits(st["positions"], bars, self.p)
        for s in exits:
            s["signal_date"] = _d(D)
            st["pending_orders"].append(s)
            self.event(D, "SELL_SIGNAL", ticker=s["ticker"], shares=s["shares"], signal_close=round(s["signal_close"], 1),
                       exit_type=s["exit_type"], reason=s["reason"],
                       text=f"【売り判断】{name_of(s['ticker'])}({code_of(s['ticker'])}) {s['shares']}株 → 翌営業日の寄付で売却。理由: {s['reason']}")

        pending_buy_cost = sum(o["shares"] * o["signal_close"] for o in st["pending_orders"] if o["side"] == "BUY")
        exiting = {s["ticker"] for s in exits}
        positions_for_entry = {t: p for t, p in st["positions"].items() if t not in exiting}
        cash_available = st["cash"] - pending_buy_cost
        entries = strategy.evaluate_entries(bars, positions_for_entry, st["pending_orders"], cash_available, equity,
                                            self.p, self.broker, regime, self.active_cooldown(D) | exiting, {})
        for s in entries:
            s["signal_date"] = _d(D)
            st["pending_orders"].append(s)
            self.event(D, "BUY_SIGNAL", ticker=s["ticker"], shares=s["shares"], signal_close=round(s["signal_close"], 1),
                       planned_stop=round(s["planned_stop"], 1), reason=s["reason"],
                       text=f"【買い判断】{name_of(s['ticker'])}({code_of(s['ticker'])}) {s['shares']}株（約 {s['shares'] * s['signal_close']:,.0f}円）"
                            f" → 翌営業日の寄付で買付。理由: {s['reason']}")
        return regime, exits, entries

    def process_day(self, D):
        self.fill_orders(D)
        self.update_positions(D)
        regime, exits, entries = self.generate_signals(D)
        nav = self.record_nav(D)
        self.state["last_processed_date"] = _d(D)
        self.state["last_regime"] = int(regime)
        return nav

    def run_until(self, asof) -> list:
        """未処理の営業日を順に処理する。処理した日付のリストを返す。"""
        asof = pd.Timestamp(asof)
        last = self.state.get("last_processed_date")
        last_ts = pd.Timestamp(last) if last else None
        days = [d for d in self.tdays if d <= asof and (last_ts is None or d > last_ts)]
        if last_ts is None:
            days = [d for d in days if d >= pd.Timestamp(self.state["start_date"])]
        for D in days:
            self.process_day(D)
        self.save_state()
        return days
