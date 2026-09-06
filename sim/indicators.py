"""テクニカル指標。すべて「その日までのデータのみ」で計算される後方参照型（先読みなし）。"""
import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """OHLCV に指標列を追加して返す。"""
    out = df.copy()
    c, h, l, v = out["Close"], out["High"], out["Low"], out["Volume"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr"] = tr.rolling(p["atr_period"]).mean()
    out["sma_fast"] = c.rolling(p["sma_fast"]).mean()
    out["sma_slow"] = c.rolling(p["sma_slow"]).mean()
    lb, skip = p["momentum_lookback"], p["momentum_skip"]
    out["mom"] = c.shift(skip) / c.shift(lb) - 1.0
    out["ret_1m"] = c / c.shift(21) - 1.0
    out["ret_3m"] = c / c.shift(63) - 1.0
    out["turnover20"] = (c * v).rolling(20).mean()
    out["high_20"] = c.rolling(20).max()
    out["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
    return out


def regime_series(df: pd.DataFrame, sma_len: int) -> pd.Series:
    """相場環境: True=強気（終値 > 長期移動平均）。"""
    return df["Close"] > df["Close"].rolling(sma_len).mean()
