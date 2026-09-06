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
MARKET_CLOSE = dtime(15, 35)


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
