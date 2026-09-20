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
        self.actions = {}   # 配当・分割（ライブ運用のみ使用。run.py が設定する）
        self.cashflows = []  # 追加入金の指示（ライブ運用のみ使用。data/cashflows.json を run.py が読み込む）

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

    # --- コア（指数 ETF の買い持ち）とサテライト（ルール運用）を分けて扱う ---
    def strategy_positions(self) -> dict:
        return {t: p for t, p in self.state["positions"].items() if p.get("sleeve") != "core"}

    def core_value(self, D) -> float:
        return sum(p["shares"] * self.last_close(t, D)
                   for t, p in self.state["positions"].items() if p.get("sleeve") == "core")

    def core_reserved(self) -> float:
        """コアの買付待ちで確保してある現金。"""
        return sum(float(o.get("budget", 0)) for o in self.state["pending_orders"] if o.get("sleeve") == "core")

    def strategy_equity(self, D) -> float:
        """ルール運用（サテライト）の資金管理に使う資産額。コアの評価額と買付待ち資金は含めない。"""
        return self.equity(D) - self.core_value(D) - self.core_reserved()

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

    # ---------- 配当・株式分割（ライブ運用のみ） ----------
    def apply_corporate_actions(self, D):
        """権利落ち日の処理。バックテストは調整済み価格の上で売買するため配当は価格に含まれており、何もしない。

        ライブでは約定・高値・損切りラインを「その時点の実際の株価」で記録しているので、
          分割: 株数×倍率、単価・高値・損切り・ATR÷倍率
          配当: 株数×配当を現金に計上し、高値・損切りラインを配当落ちの分だけ引き下げる
        を行う。positions の basis_asof（約定を記録したときのデータ基準日）より後の権利落ちだけが対象。
        """
        if self.label != "live" or not self.actions:
            return
        st = self.state
        applied = st.setdefault("applied_actions", {})
        tax = float(self.broker.get("dividend_tax_rate", 0.0))
        Ds = _d(D)
        for t in sorted(self.actions):
            for X in sorted(self.actions[t]):
                if X > Ds:
                    continue
                a = self.actions[t][X]
                pos = st["positions"].get(t)
                held = bool(pos) and pos.get("basis_asof", pos["entry_date"]) < X
                closed = [c for c in st.get("closed_positions", [])
                          if c["ticker"] == t and c.get("basis_asof", c["entry_date"]) < X <= c["exit_date"]]
                is_bench = t in self.benchmarks and t in st.get("bench_base", {}) and st.get("start_date", "") < X
                pend = [o for o in st["pending_orders"] if o["ticker"] == t and o.get("signal_date", "") < X]

                # --- 分割 ---
                r = float(a.get("split") or 0)
                ksp = f"{t}|{X}|split"
                if r > 0 and r != 1 and ksp not in applied and (held or is_bench or pend):
                    if held:
                        new_sh = pos["shares"] * r
                        whole = int(new_sh + 1e-9)
                        ref = (pos.get("last_close") or pos["avg_price"]) / r
                        if new_sh - whole > 1e-9:
                            st["cash"] += (new_sh - whole) * ref  # 端数株は現金で精算
                        old_sh = pos["shares"]
                        pos["shares"] = whole
                        for k in ("avg_price", "entry_price", "highest_close", "stop_price", "atr", "last_close"):
                            if pos.get(k) is not None:
                                pos[k] = pos[k] / r
                        stop_txt = f"、損切り目安 {pos['stop_price']:,.0f}円" if pos.get("stop_price") is not None else ""
                        self.event(D, "SPLIT", ticker=t, ratio=r, ex_date=X, shares_before=old_sh, shares_after=whole,
                                   estimated=bool(a.get("split_estimated")),
                                   text=f"【株式分割】{name_of(t)}({code_of(t)}) 1株→{r:g}株（権利落ち {X}）。"
                                        f"保有 {old_sh}株→{whole}株、取得単価 {pos['avg_price']:,.1f}円{stop_txt}に換算。資産額は変わりません")
                    for o in pend:
                        if "shares" not in o:      # コアの買付は金額指定なので換算不要
                            continue
                        o["shares"] = int(o["shares"] * r + 1e-9)
                        for k in ("signal_close", "planned_stop", "atr"):
                            if o.get(k) is not None:
                                o[k] = o[k] / r
                    if is_bench:
                        st["bench_base"][t] = st["bench_base"][t] / r
                        if t in st.get("bench_last", {}):
                            st["bench_last"][t] = st["bench_last"][t] / r
                    applied[ksp] = {"split": r, "date": Ds, "estimated": bool(a.get("split_estimated"))}

                # --- 配当 ---
                d = float(a.get("dividend") or 0)
                if d <= 0:
                    continue
                kdv = f"{t}|{X}|div"
                est = bool(a.get("dividend_estimated"))
                rec = applied.get(kdv)
                if rec is None:
                    if held:
                        raw_prev = pos.get("last_close") or pos["avg_price"]
                        f = min(1.0, max(0.5, 1.0 - d / raw_prev))
                        amount = pos["shares"] * d * (1.0 - tax)
                        st["cash"] += amount
                        if pos.get("hc_date", pos["entry_date"]) < X:
                            pos["highest_close"] *= f
                        if pos.get("stop_date", pos["entry_date"]) < X and pos.get("stop_price") is not None:
                            pos["stop_price"] *= f
                        pos["dividends"] = pos.get("dividends", 0.0) + amount
                        st["dividends_cum"] = st.get("dividends_cum", 0.0) + amount
                        applied[kdv] = {"dividend": d, "shares": pos["shares"], "estimated": est, "date": Ds}
                        note = "、見込み額。確定後に差額を精算" if est else ""
                        stop_txt = (f"。配当落ちに合わせて損切り目安を {pos['stop_price']:,.0f}円に調整"
                                    if pos.get("stop_price") is not None else "")
                        self.event(D, "DIVIDEND", ticker=t, ex_date=X, per_share=d, shares=pos["shares"],
                                   amount=round(amount), estimated=est,
                                   text=f"【配当】{name_of(t)}({code_of(t)}) 1株 {d:g}円 × {pos['shares']}株 = {amount:,.0f}円を現金に計上"
                                        f"（権利落ち {X}{note}）{stop_txt}")
                    elif closed:
                        sh = sum(c["shares"] for c in closed)
                        amount = sh * d * (1.0 - tax)
                        st["cash"] += amount
                        st["dividends_cum"] = st.get("dividends_cum", 0.0) + amount
                        applied[kdv] = {"dividend": d, "shares": sh, "estimated": est, "date": Ds}
                        self.event(D, "DIVIDEND", ticker=t, ex_date=X, per_share=d, shares=sh, amount=round(amount), estimated=est,
                                   text=f"【配当】{name_of(t)}({code_of(t)}) 売却済みだが権利確定時に保有していた {sh}株ぶん {amount:,.0f}円を計上（権利落ち {X}）")
                elif rec.get("estimated") and not est:
                    diff = (d - rec["dividend"]) * rec.get("shares", 0) * (1.0 - tax)
                    if abs(diff) >= 0.5:
                        st["cash"] += diff
                        st["dividends_cum"] = st.get("dividends_cum", 0.0) + diff
                        self.event(D, "DIVIDEND", ticker=t, ex_date=X, per_share=d, shares=rec.get("shares", 0), amount=round(diff),
                                   text=f"【配当の確定】{name_of(t)}({code_of(t)}) 見込み {rec['dividend']:g}円 → 確定 {d:g}円。差額 {diff:+,.0f}円を精算")
                    rec.update({"dividend": d, "estimated": False})

                # ベンチマーク指数の基準値は、その ETF を保有しているかに関係なく分配金込み（トータルリターン）に直す
                kbn = f"{t}|{X}|bench"
                if is_bench and kbn not in applied and st.get("bench_last", {}).get(t):
                    fb = min(1.0, max(0.5, 1.0 - d / st["bench_last"][t]))
                    st["bench_base"][t] *= fb
                    applied[kbn] = {"dividend": d, "date": Ds}

    # ---------- 追加入金（ライブ運用のみ） ----------
    def apply_cashflows(self, D) -> float:
        """data/cashflows.json の指示を 1 回だけ反映する。入金はその日の寄付前に入ったものとして扱う。

        sleeve が "core" の入金は、同じ日の寄付で指定の ETF を金額ぶん買い付ける（以後は買い持ち）。
        戻り値はその日の入出金の合計（nav.csv の flow 列に記録し、成績計算から入金の影響を除くために使う）。
        """
        if self.label != "live" or not self.cashflows:
            return 0.0
        st = self.state
        done = st.setdefault("applied_cashflows", {})
        Ds = _d(D)
        total = 0.0
        for cf in self.cashflows:
            cid, date, amount = cf.get("id"), cf.get("date"), float(cf.get("amount") or 0)
            if not cid or not date or cid in done or date > Ds or amount == 0:
                continue
            if amount < 0 and st["cash"] + amount < 0:
                self.log(f"  出金 {amount:,.0f}円 は現金不足のため見送り（id={cid}）")
                continue
            st["cash"] += amount
            st["contributed"] = st.get("contributed", st["initial_cash"]) + amount
            total += amount
            done[cid] = {"date": Ds, "amount": amount}
            core = cf.get("sleeve") == "core" and cf.get("ticker")
            self.event(D, "CASHFLOW", amount=round(amount), sleeve=cf.get("sleeve", ""),
                       text=f"【入金】{amount:,.0f}円を追加（仮想の資金）。累計の元手は {st['contributed']:,.0f}円。"
                            + (f"全額を {name_of(cf['ticker'])}({code_of(cf['ticker'])}) の買い持ち（コア）に充てる。" if core else "")
                            + (cf.get("note") or ""))
            if core:
                decided = pd.Timestamp(cf.get("decided") or date)
                prior = [ts for ts in self.tdays if ts <= decided and ts < D]
                st["pending_orders"].append({
                    "ticker": cf["ticker"], "side": "BUY", "sleeve": "core", "budget": amount,
                    "lot": int(cf.get("lot", 1) or 1), "signal_date": _d(prior[-1]) if prior else Ds,
                    "reason": cf.get("reason") or "コア（指数 ETF の買い持ち）。売買ルールの対象外で、損切りも利確もしない",
                })
        return total

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
                if self.pending_days(o, D) >= self.broker.get("fill_timeout_days", 3) and o.get("sleeve") != "core":
                    self.event(D, "CANCELLED", ticker=t, side=o["side"], shares=o.get("shares"),
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
                # 売却後に遅れて反映される配当を受け取れるよう、直近の決済を控えておく
                cp = st.setdefault("closed_positions", [])
                cp.append({"ticker": t, "shares": shares, "entry_date": pos["entry_date"],
                           "basis_asof": pos.get("basis_asof", pos["entry_date"]), "exit_date": _d(D)})
                st["closed_positions"] = [c for c in cp if (pd.Timestamp(D) - pd.Timestamp(c["exit_date"])).days <= 150]
                del st["positions"][t]
                st["cooldown"][t] = _d(D)
                self.event(D, "FILLED_SELL", ticker=t, shares=shares, price=round(price, 1), amount=round(amount),
                           realized_pnl=round(pnl), pnl_pct=round(pnl / (pos["avg_price"] * shares), 4),
                           holding_days=hold, exit_type=o.get("exit_type", ""), reason=o["reason"],
                           text=f"【売却約定】{name_of(t)}({code_of(t)}) {shares}株 @{price:,.1f}円 "
                                f"実現損益 {pnl:+,.0f}円（{pnl / (pos['avg_price'] * shares) * 100:+.1f}%、{hold}日保有）")
            elif o.get("sleeve") == "core":
                # コア: 金額指定で指数 ETF を買い、以後は持ち続ける（枠数・業種上限・損切りの対象外）
                price = b["Open"] * (1 + slip)
                lot = int(o.get("lot", 1)) or 1
                budget = min(float(o.get("budget", 0)), st["cash"])
                shares = int(budget / (price * (1 + comm_rate)) / lot) * lot
                if shares < lot:
                    self.event(D, "CANCELLED", ticker=t, side="BUY", shares=0, reason="資金不足のためコアの買付を取消")
                    continue
                amount = price * shares
                comm = amount * comm_rate
                st["cash"] -= amount + comm
                st["trade_seq"] += 1
                cur = st["positions"].get(t)
                if cur and cur.get("sleeve") == "core":   # 買い増し
                    tot = cur["shares"] + shares
                    cur["avg_price"] = (cur["avg_price"] * cur["shares"] + price * shares) / tot
                    cur["shares"] = tot
                    cur["cost"] = cur.get("cost", 0.0) + amount + comm
                else:
                    st["positions"][t] = {
                        "shares": shares, "avg_price": price, "cost": amount + comm,
                        "entry_date": _d(D), "entry_price": price, "highest_close": price,
                        "stop_price": None, "atr": None, "sleeve": "core", "last_close": price,
                        "signal_date": o["signal_date"], "reason": o["reason"],
                        "hc_date": _d(D), "stop_date": _d(D), "basis_asof": _d(self.tdays[-1]),
                    }
                row = {
                    "id": st["trade_seq"], "date": _d(D), "ticker": t, "code": code_of(t), "name": name_of(t),
                    "side": "BUY", "shares": shares, "price": round(price, 1), "amount": round(amount),
                    "commission": round(comm), "realized_pnl": "", "pnl_pct": "", "holding_days": "",
                    "entry_price": round(price, 1), "exit_type": "", "signal_date": o["signal_date"], "reason": o["reason"],
                }
                self._append_csv("trades.csv", TRADE_COLS, row)
                self.event(D, "FILLED_BUY", ticker=t, shares=shares, price=round(price, 1), amount=round(amount),
                           sleeve="core", reason=o["reason"],
                           text=f"【コア買付】{name_of(t)}({code_of(t)}) {shares}口 @{price:,.1f}円（{amount:,.0f}円）。"
                                f"指数の買い持ち枠。損切りはせず持ち続ける")
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
                    "hc_date": _d(D), "stop_date": _d(D),
                    "basis_asof": _d(self.tdays[-1]),  # この約定を記録したときの価格データの基準日
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
            if "last_close" in pos:
                pos["prev_close"] = pos["last_close"]   # 前日比の表示用（分割・配当の換算後の値で持つ）
            if pos.get("sleeve") == "core":     # コアは買い持ち。損切りラインを持たない
                pos["last_close"] = b["Close"]
                continue
            if b["Close"] > pos["highest_close"]:
                pos["highest_close"] = b["Close"]
                pos["hc_date"] = _d(D)
            atr = b["atr"] if not np.isnan(b.get("atr", float("nan"))) else pos["atr"]
            pos["atr"] = atr
            new_stop = pos["highest_close"] - self.p["atr_stop_mult"] * atr
            if new_stop > pos.get("stop_price", float("-inf")):  # ストップは切り上げのみ
                pos["stop_price"] = new_stop
                pos["stop_date"] = _d(D)
            pos["last_close"] = b["Close"]

    def _ensure_csv_columns(self, name, cols, defaults):
        """列を足したとき、既存の CSV を新しい列構成に書き直す（値はそのまま、足した列は既定値）。"""
        p = self._path(name)
        if not os.path.exists(p):
            return
        with open(p, "r", newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames == cols:
                return
            rows = list(rd)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: (r[c] if r.get(c) not in (None, "") else defaults.get(c, "")) for c in cols})

    def record_nav(self, D, flow: float = 0.0):
        st = self.state
        pv = self.positions_value(D)
        nav = st["cash"] + pv
        row = {
            "date": _d(D), "cash": round(st["cash"]), "positions_value": round(pv), "nav": round(nav),
            "n_positions": len(self.strategy_positions()), "exposure": round(pv / nav, 4) if nav else 0,
            "regime": int(self.regime_on(D)),
            "flow": round(flow), "core_value": round(self.core_value(D)),
        }
        # ベンチマークは「運用開始日＝100」の指数として記録する（生の株価データを記録・公開しないため）
        base = st.setdefault("bench_base", {})
        blast = st.setdefault("bench_last", {})
        for bt in self.benchmarks:
            c = self.last_close(bt, D)
            if not np.isnan(c):
                blast[bt] = c
            if bt not in base and not np.isnan(c):
                base[bt] = c
            row[bt] = round(c / base[bt] * 100, 4) if bt in base and not np.isnan(c) else ""
        cols = ["date", "cash", "positions_value", "nav", "n_positions", "exposure", "regime"] + self.benchmarks
        if self.label == "live":   # flow=入出金、core_value=コアの評価額。成績は入出金の影響を除いて計算する
            cols = cols + ["flow", "core_value"]
            self._ensure_csv_columns("nav.csv", cols, {"flow": "0", "core_value": "0"})
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
        equity = self.strategy_equity(D)          # 資金管理はルール運用ぶんの資産で行う（コアは別枠）
        strat_pos = self.strategy_positions()
        strat_orders = [o for o in st["pending_orders"] if o.get("sleeve") != "core"]

        exits = strategy.evaluate_exits(strat_pos, bars, self.p)
        for s in exits:
            s["signal_date"] = _d(D)
            st["pending_orders"].append(s)
            self.event(D, "SELL_SIGNAL", ticker=s["ticker"], shares=s["shares"], signal_close=round(s["signal_close"], 1),
                       exit_type=s["exit_type"], reason=s["reason"],
                       text=f"【売り判断】{name_of(s['ticker'])}({code_of(s['ticker'])}) {s['shares']}株 → 翌営業日の寄付で売却。理由: {s['reason']}")

        pending_buy_cost = sum(o["shares"] * o["signal_close"] for o in strat_orders if o["side"] == "BUY")
        exiting = {s["ticker"] for s in exits}
        positions_for_entry = {t: p for t, p in strat_pos.items() if t not in exiting}
        cash_available = st["cash"] - pending_buy_cost - self.core_reserved()
        entries = strategy.evaluate_entries(bars, positions_for_entry, strat_orders + exits, cash_available, equity,
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
        self.apply_corporate_actions(D)
        flow = self.apply_cashflows(D)
        self.fill_orders(D)
        self.update_positions(D)
        regime, exits, entries = self.generate_signals(D)
        nav = self.record_nav(D, flow)
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
        done = []
        for D in days:
            if self.label == "live":
                # 保有・発注中の銘柄の当日足が欠けている日は処理を見送る（古い終値で評価・判定しないため）。3日待っても無ければ進める
                need = set(self.state["positions"]) | {o["ticker"] for o in self.state["pending_orders"]}
                missing = sorted(t for t in need if self.idx.get(t, {}).get(D) is None)
                if missing and (asof - D).days < 3:
                    self.log(f"  {_d(D)} は {', '.join(missing)} の株価が未取得のため処理を見送り（次回の実行で処理）")
                    break
            self.process_day(D)
            done.append(D)
        self.save_state()
        return done
