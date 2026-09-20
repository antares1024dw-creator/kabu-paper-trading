"""株価データの取得とキャッシュ（Yahoo Finance / yfinance）。

・日足 OHLCV（分割・配当調整済み）を data/prices/<ticker>.csv にキャッシュする
・差分更新（直近 7 日ぶんを取り直して結合）
・Yahoo Finance の日本株は 15〜20 分程度の遅延がある。日次判断用途には十分。
"""
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import DATA_DIR

JST = ZoneInfo("Asia/Tokyo")
PRICE_DIR = os.path.join(DATA_DIR, "prices")
COLS = ["Open", "High", "Low", "Close", "Volume"]

# 東証の大引け（15:30）。これより前に「今日」の足があれば場中の暫定値なので捨てる。
MARKET_CLOSE = dtime(16, 0)   # 大引け 15:30 + Yahoo の遅延（15〜20分）を見込む


def now_jst() -> datetime:
    return datetime.now(JST)


def _cache_path(ticker: str) -> str:
    return os.path.join(PRICE_DIR, f"{ticker}.csv")


def load_cached(ticker: str):
    p = _cache_path(ticker)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p, index_col=0)
    except Exception:
        return None
    if df.empty:
        return None
    df.index = pd.to_datetime(df.index).normalize()
    df.index.name = "Date"
    return df[COLS].sort_index()


def _save(ticker: str, df: pd.DataFrame) -> None:
    os.makedirs(PRICE_DIR, exist_ok=True)
    df.to_csv(_cache_path(ticker), float_format="%.4f")


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df.index.name = "Date"
    df = df[COLS].dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


def _download(tickers: list, start: str, retries: int = 3) -> dict:
    import yfinance as yf

    out = {}
    last_err = None
    raw = None
    for attempt in range(retries):
        try:
            raw = yf.download(
                tickers,
                start=start,
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
                actions=False,
            )
            break
        except Exception as e:  # ネットワーク一時障害など
            last_err = e
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"株価取得に失敗しました: {last_err}")

    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = set(raw.columns.get_level_values(0))
        for t in tickers:
            if t in lvl0:
                sub = raw[t]
                if not sub.dropna(subset=["Close"]).empty:
                    out[t] = _normalize(sub)
    else:  # 単一銘柄
        out[tickers[0]] = _normalize(raw)

    # 一時的な失敗（yfinance のキャッシュロック等）を 1 銘柄ずつ取り直す
    missing = [t for t in tickers if t not in out]
    if missing and len(missing) < len(tickers):
        time.sleep(1)
        for t in missing:
            try:
                r = yf.download(t, start=start, auto_adjust=True, threads=False, progress=False, actions=False)
                if r is not None and not r.empty:
                    if isinstance(r.columns, pd.MultiIndex):
                        r = r.droplevel(0, axis=1) if r.columns.nlevels > 1 and t in r.columns.get_level_values(0) else r
                        r.columns = [c if isinstance(c, str) else c[0] for c in r.columns]
                    out[t] = _normalize(r)
            except Exception:
                pass
    return out


def update_prices(tickers: list, history_start: str, force_full: bool = False,
                  log=print) -> dict:
    """キャッシュを差分更新して {ticker: DataFrame} を返す。"""
    os.makedirs(PRICE_DIR, exist_ok=True)
    cached = {}
    need_full, need_incr = [], []
    for t in tickers:
        df = None if force_full else load_cached(t)
        if df is None or df.empty:
            need_full.append(t)
        else:
            cached[t] = df
            need_incr.append(t)

    fetched = {}
    if need_full:
        log(f"  価格履歴を新規取得: {len(need_full)} 銘柄 (from {history_start})")
        for i in range(0, len(need_full), 40):
            fetched.update(_download(need_full[i:i + 40], history_start))
    if need_incr:
        oldest = min(cached[t].index[-1] for t in need_incr)
        start = (oldest - timedelta(days=7)).strftime("%Y-%m-%d")
        log(f"  価格を差分更新: {len(need_incr)} 銘柄 (from {start})")
        for i in range(0, len(need_incr), 60):
            fetched.update(_download(need_incr[i:i + 60], start))

    # 配当・分割のあと Yahoo は過去の価格を調整し直す。キャッシュと食い違う銘柄は全期間を取り直す
    stale = []
    for t in need_incr:
        new, old = fetched.get(t), cached.get(t)
        if new is None or old is None:
            continue
        common = old.index.intersection(new.index)
        if len(common) and ((new.loc[common, "Close"] / old.loc[common, "Close"] - 1).abs() > 0.001).any():
            stale.append(t)
    if stale:
        log(f"  過去価格が調整されたため全期間を再取得: {len(stale)} 銘柄")
        for i in range(0, len(stale), 40):
            fetched.update(_download(stale[i:i + 40], history_start))
        for t in stale:
            cached.pop(t, None)

    result = {}
    for t in tickers:
        new = fetched.get(t)
        old = cached.get(t)
        if new is not None and old is not None:
            df = pd.concat([old[old.index < new.index[0]], new])
        elif new is not None:
            df = new
        elif old is not None:
            df = old
        else:
            continue
        df = df[~df.index.duplicated(keep="last")].sort_index()
        result[t] = df
        _save(t, df)
    return result


def fetch_actions(tickers: list, start: str, log=print) -> dict:
    """配当・株式分割の一覧を取得する。{ticker: {"YYYY-MM-DD": {"dividend": 円/株, "split": 倍率}}}

    Yahoo の配当額は「現在の株数ベース」（分割調整後）で返る。失敗した銘柄は空で返す（処理は止めない）。
    """
    import yfinance as yf

    out = {}
    start_ts = pd.Timestamp(start)
    for t in tickers:
        out[t] = {}
        try:
            a = yf.Ticker(t).actions
        except Exception as e:
            log(f"  配当・分割情報の取得に失敗: {t} {e!r}")
            continue
        if a is None or len(a) == 0:
            continue
        idx = pd.to_datetime(a.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        idx = idx.normalize()
        for ts, (_, row) in zip(idx, a.iterrows()):
            if ts < start_ts:
                continue
            d = float(row.get("Dividends", 0) or 0)
            r = float(row.get("Stock Splits", 0) or 0)
            if d > 0 or (r > 0 and r != 1):
                out[t][ts.strftime("%Y-%m-%d")] = {"dividend": d, "split": r}
        _add_scheduled(out[t], a, idx, yf.Ticker(t), start_ts, t, log)
    return out


def _add_scheduled(acts: dict, hist, hist_idx, tk, start_ts, t, log) -> None:
    """Yahoo が「予定」として持っている権利落ち日・分割日を、見込みとして足す（実績が載るまでのつなぎ）。

    配当の見込み額は前年同時期の実績（分割があればその倍率で換算）。実績が載った時点で差額を精算する。
    """
    try:
        info = tk.info or {}
    except Exception as e:
        log(f"  予定情報の取得に失敗: {t} {e!r}")
        return
    # 分割の予定
    try:
        sd, sf = info.get("lastSplitDate"), info.get("lastSplitFactor")
        if sd and sf and ":" in str(sf):
            X = pd.Timestamp(int(sd), unit="s").normalize()
            a_, b_ = str(sf).split(":")
            r = float(a_) / float(b_)
            key = X.strftime("%Y-%m-%d")
            if X >= start_ts and r > 0 and r != 1 and not (acts.get(key, {}).get("split") not in (None, 0, 0.0, 1, 1.0)):
                acts.setdefault(key, {"dividend": 0.0, "split": 0.0})
                acts[key]["split"] = r
                acts[key]["split_estimated"] = True
    except Exception:
        pass
    # 配当の予定（権利落ち日だけ分かっていて金額が未掲載のとき）
    try:
        xd = info.get("exDividendDate")
        if xd:
            X = pd.Timestamp(int(xd), unit="s").normalize()
            key = X.strftime("%Y-%m-%d")
            if X >= start_ts and not acts.get(key, {}).get("dividend"):
                lo, hi = X - pd.Timedelta(days=385), X - pd.Timedelta(days=345)
                prev = [(ts, float(v)) for ts, v in zip(hist_idx, hist["Dividends"]) if lo <= ts <= hi and v > 0] if "Dividends" in hist else []
                if prev:
                    d = prev[-1][1]
                    acts.setdefault(key, {"dividend": 0.0, "split": 0.0})
                    # 予定の分割がまだ Yahoo の配当履歴に反映されていない場合は、分割後の 1 株あたりに換算する
                    if acts[key].get("split_estimated") and acts[key].get("split"):
                        d = d / acts[key]["split"]
                    acts[key]["dividend"] = d
                    acts[key]["dividend_estimated"] = True
    except Exception:
        pass


def adjust_unadjusted_splits(prices: dict, actions: dict, log=print) -> dict:
    """分割の権利落ち日に Yahoo の過去価格がまだ分割調整されていない場合、こちらで調整する。

    権利落ち日の終値が前日比でほぼ 1/倍率 になっていれば「未調整」と判断し、それ以前の株価を倍率で割る。
    これをしないと、その日だけ移動平均や ATR が壊れて誤った売りシグナルが出る。
    """
    out = dict(prices)
    for t, acts in (actions or {}).items():
        df = out.get(t)
        if df is None:
            continue
        for X, a in sorted(acts.items()):
            r = float(a.get("split") or 0)
            if not (r > 0 and r != 1):
                continue
            ts = pd.Timestamp(X)
            if ts not in df.index:
                continue
            i = df.index.get_loc(ts)
            if i == 0:
                continue
            ratio = float(df["Close"].iloc[i]) / float(df["Close"].iloc[i - 1])
            if abs(ratio * r - 1.0) < abs(ratio - 1.0):   # 1 より 1/倍率 に近い → 過去が未調整
                df = df.copy()
                m = df.index < ts
                for c in ("Open", "High", "Low", "Close"):
                    df.loc[m, c] = df.loc[m, c] / r
                df.loc[m, "Volume"] = df.loc[m, "Volume"] * r
                out[t] = df
                log(f"  分割の過去価格が未調整のため補正: {t} {X} 1→{r:g}")
    return out


def merge_expected_actions(actual: dict, expected: list) -> dict:
    """Yahoo の実績に、手入力の見込み（data/expected_actions.json）を重ねる。

    Yahoo への反映が遅れる配当（金額未発表の中間配当など）や分割で、帳簿が一時的に狂うのを防ぐ。
    実績があれば実績を優先し、見込みで計上した分は実績が出た時点で差額を精算する。
    """
    out = {t: {x: dict(a) for x, a in acts.items()} for t, acts in (actual or {}).items()}
    for e in expected or []:
        t, x = e.get("ticker"), e.get("ex_date")
        if not t or not x:
            continue
        a = out.setdefault(t, {}).setdefault(x, {"dividend": 0.0, "split": 0.0})
        if e.get("dividend") and (not a.get("dividend") or a.get("dividend_estimated")):  # 手入力は自動見込みより優先
            a["dividend"] = float(e["dividend"])
            a["dividend_estimated"] = True
        if e.get("split") and (not (a.get("split") and a["split"] != 1) or a.get("split_estimated")):
            a["split"] = float(e["split"])
            a["split_estimated"] = True
        # 分割と同じ日の配当: Yahoo は通常「分割後の 1 株あたり」で載せるが、分割前の金額で載った場合に備える。
        # 手入力の見込み（分割後ベース）に、倍率で割った値のほうが近ければ、分割前ベースと判断して換算する。
        r = float(a.get("split") or 0)
        if r > 0 and r != 1 and e.get("dividend") and a.get("dividend") and not a.get("dividend_estimated"):
            exp_d, act_d = float(e["dividend"]), float(a["dividend"])
            if abs(act_d / r - exp_d) < abs(act_d - exp_d):
                a["dividend"] = act_d / r
                a["dividend_rescaled"] = True
    return out


def repair_glitches(prices: dict, log=print, max_len: int = 5) -> dict:
    """Yahoo Finance の一時的な異常値（数日だけ価格が 1/10 や 10 倍になる等）を補正する。

    前日比で 2 倍以上／半分以下に跳び、max_len 日以内に元の水準へ戻る区間を「異常区間」とみなし、
    整数倍率（10 など）で価格を戻す。本物の株式分割は自動調整済みなので該当しない。
    """
    out = {}
    for t, df in prices.items():
        c = df["Close"].to_numpy(dtype=float)
        n = len(c)
        if n < 3:
            out[t] = df
            continue
        df = df.copy()
        i = 1
        fixed = []
        while i < n:
            prev = c[i - 1]
            ratio = c[i] / prev if prev > 0 else 1.0
            if ratio < 0.5 or ratio > 2.0:
                # 元の水準に戻る位置を探す
                j = None
                for k in range(i + 1, min(n, i + max_len + 1)):
                    if 0.7 < c[k] / prev < 1.4:
                        j = k
                        break
                if j is not None:
                    factor = prev / c[i]
                    factor = round(factor) if factor >= 2 else 1.0 / round(1.0 / factor)
                    if factor != 1.0:
                        sl = slice(i, j)
                        for col in ("Open", "High", "Low", "Close"):
                            df.iloc[sl, df.columns.get_loc(col)] = df.iloc[sl][col] * factor
                        df.iloc[sl, df.columns.get_loc("Volume")] = df.iloc[sl]["Volume"] / factor
                        c = df["Close"].to_numpy(dtype=float)
                        fixed.append(f"{df.index[i]:%Y-%m-%d}〜{df.index[j - 1]:%Y-%m-%d}×{factor:g}")
                    i = j
                    continue
            i += 1
        if fixed:
            log(f"  異常値を補正: {t} {', '.join(fixed)}")
        out[t] = df
    return out


def drop_partial_today(prices: dict, now: datetime = None) -> dict:
    """大引け前に混入した「本日の暫定足」を取り除く。"""
    now = now or now_jst()
    today = pd.Timestamp(now.date())
    if now.time() >= MARKET_CLOSE:
        return prices
    out = {}
    for t, df in prices.items():
        out[t] = df[df.index < today] if (len(df) and df.index[-1] == today) else df
    return out


def latest_common_date(prices: dict, ref_ticker: str) -> pd.Timestamp:
    """基準銘柄（ETF）の最終足の日付＝直近の完了した営業日。"""
    return prices[ref_ticker].index[-1]
