"""ダッシュボード（dashboard.html）の生成。

・単一の HTML ファイル（データは JSON として埋め込み）。ダブルクリックで開ける。
・グラフは Chart.js（cdnjs）を使用。オフラインでも表・数値は表示される。
"""
import html
import json
import os
import re
from datetime import datetime

import pandas as pd

from . import market
from .config import ROOT, DATA_DIR
from .metrics import load_nav, load_trades, compute_metrics, series_for_chart, clean
from .review import list_journal, params_history
from .universe import name_of, code_of, SECTORS, UNIVERSE, BENCHMARKS

BT_DIR = os.path.join(DATA_DIR, "backtest")
OUT_PATH = os.path.join(ROOT, "dashboard.html")


def md_to_html(text: str) -> str:
    out, in_ul, in_p = [], False, []

    def flush_p():
        if in_p:
            out.append("<p>" + " ".join(in_p) + "</p>")
            in_p.clear()

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", s)
        return s

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("- "):
            flush_p()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append("<li>" + inline(line[2:]) + "</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if not line:
            flush_p()
        elif line.startswith("### "):
            flush_p(); out.append("<h5>" + inline(line[4:]) + "</h5>")
        elif line.startswith("## "):
            flush_p(); out.append("<h4>" + inline(line[3:]) + "</h4>")
        elif line.startswith("# "):
            flush_p(); out.append("<h3>" + inline(line[2:]) + "</h3>")
        elif line.startswith("---"):
            flush_p(); out.append("<hr>")
        else:
            in_p.append(inline(line))
    flush_p()
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _read_jsonl(path: str, limit: int) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    return rows[-limit:][::-1]


def _positions(state: dict) -> list:
    out = []
    nav_total = None
    for t, pos in state.get("positions", {}).items():
        df = market.load_cached(t)
        last = float(df["Close"].iloc[-1]) if df is not None and len(df) else pos.get("last_close", pos["avg_price"])
        prev = float(df["Close"].iloc[-2]) if df is not None and len(df) > 1 else last
        last = pos.get("last_close", last)
        prev = pos.get("prev_close", prev)   # 帳簿側の前日終値（分割の換算後）。無ければ価格キャッシュから
        value = last * pos["shares"]
        out.append({
            "ticker": t, "code": code_of(t), "name": name_of(t),
            "sector": "指数 ETF" if pos.get("sleeve") == "core" else SECTORS.get(t, ""),
            "sleeve": pos.get("sleeve", "strategy"),
            "shares": pos["shares"], "avg_price": pos["avg_price"], "last": last, "day_change": last / prev - 1 if prev else 0,
            "value": value, "pnl": value - pos["avg_price"] * pos["shares"], "pnl_pct": last / pos["avg_price"] - 1,
            "stop": pos.get("stop_price"), "highest": pos.get("highest_close"),
            "entry_date": pos["entry_date"], "days": (pd.Timestamp.today().normalize() - pd.Timestamp(pos["entry_date"])).days,
            "reason": pos.get("reason", ""),
        })
    total = state.get("cash", 0) + sum(p["value"] for p in out)
    for p in out:
        p["weight"] = p["value"] / total if total else 0
    out.sort(key=lambda x: -x["value"])
    return out


def _pending(state: dict) -> list:
    out = []
    for o in state.get("pending_orders", []):
        out.append({"ticker": o["ticker"], "code": code_of(o["ticker"]), "name": name_of(o["ticker"]), "side": o["side"],
                    "shares": o.get("shares"), "budget": o.get("budget"), "sleeve": o.get("sleeve", "strategy"),
                    "signal_close": o.get("signal_close"), "signal_date": o.get("signal_date"),
                    "planned_stop": o.get("planned_stop"), "reason": o.get("reason", "")})
    return out


def _scheduled_cashflows(state: dict) -> list:
    """まだ反映されていない入金の予定（data/cashflows.json）。ボードに予告として出す。"""
    p = os.path.join(DATA_DIR, "cashflows.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            flows = json.load(f)
    except Exception:
        return []
    done = state.get("applied_cashflows", {})
    out = []
    for cf in flows:
        if cf.get("id") in done:
            continue
        t = cf.get("ticker")
        out.append({"date": cf.get("date"), "amount": cf.get("amount"), "sleeve": cf.get("sleeve", ""),
                    "ticker": t, "code": code_of(t) if t else "", "name": name_of(t) if t else "", "note": cf.get("note", "")})
    return out


def _trades(df: pd.DataFrame, limit: int) -> list:
    if df is None or df.empty:
        return []
    rows = df.tail(limit).iloc[::-1]
    return clean([{k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()} for r in rows.to_dict("records")])


def build_payload(cfg: dict) -> dict:
    state_path = os.path.join(DATA_DIR, "state.json")
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"cash": cfg["initial_cash"], "positions": {}, "pending_orders": []}
    nav, trades = load_nav(DATA_DIR), load_trades(DATA_DIR)
    m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
    live = {
        "metrics": m,
        "series": series_for_chart(nav, cfg["benchmarks"], cfg["initial_cash"]),
        "positions": clean(_positions(state)),
        "pending": clean(_pending(state)),
        "events": _read_jsonl(os.path.join(DATA_DIR, "events.jsonl"), 60),
        "trades": _trades(trades, 80),
        "cash": state.get("cash", cfg["initial_cash"]),
        "dividends_cum": state.get("dividends_cum", 0.0),
        "scheduled_cashflows": _scheduled_cashflows(state),
        "last_processed": state.get("last_processed_date"),
        "start_date": state.get("start_date", cfg["start_date"]),
        "regime": state.get("last_regime"),
    }
    bt = None
    sp = os.path.join(BT_DIR, "summary.json")
    if os.path.exists(sp):
        s = json.load(open(sp, encoding="utf-8"))
        bnav, btrades = load_nav(BT_DIR), load_trades(BT_DIR)
        bt = {"summary": s, "metrics": s["metrics"], "series": series_for_chart(bnav, cfg["benchmarks"], s["initial_cash"]),
              "trades": _trades(btrades, 40), "n_trades": int(len(btrades))}
    journal = [{**j, "html": md_to_html(j["text"])} for j in list_journal(8)]
    for j in journal:
        j.pop("text", None)
    return clean({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "config": {"initial_cash": cfg["initial_cash"], "strategy": cfg["strategy"], "broker": cfg["broker"], "benchmarks": cfg["benchmarks"]},
        "bench_names": {b: BENCHMARKS.get(b, b) for b in cfg["benchmarks"]},
        "universe_count": len(UNIVERSE),
        "live": live, "backtest": bt, "journal": journal, "params_history": params_history(),
    })


def build_dashboard(cfg: dict, log=print) -> str:
    payload = build_payload(cfg)
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    # 公開ページとして生成するときは検索エンジンに載せない
    robots = '<meta name="robots" content="noindex, nofollow, noarchive">\n' if os.environ.get("STOCKSIM_PUBLIC") else ""
    html_out = TEMPLATE.replace("__DATA__", data).replace("__ROBOTS__", robots)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return OUT_PATH


TEMPLATE = r"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__ROBOTS__<title>紙上運用ボード</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Shippori+Mincho:wght@700&display=swap">
<style>
:root{
  color-scheme:light;
  --bg:#f6f5f0; --surface:#fcfcfb; --surface-2:#f0efe9; --ink:#151513; --ink-2:#52514e; --muted:#898781;
  --line:#e1e0d9; --line-2:#c3c2b7; --accent:#1c5cab; --accent-soft:#e6eefb;
  --plus:#b8302f; --plus-soft:#fbe9e7; --minus:#1c5cab; --minus-soft:#e6eefb;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --bar-plus:#e34948; --bar-minus:#2a78d6;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --chip-buy:#fbe9e7; --chip-buy-ink:#8f1f1e; --chip-sell:#e6eefb; --chip-sell-ink:#123f78;
  --chip-fill:#e8f5e8; --chip-fill-ink:#0b5a0b; --chip-note:#f3f0e4; --chip-note-ink:#5c4a12;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --bg:#111110; --surface:#1a1a19; --surface-2:#232321; --ink:#f3f2ec; --ink-2:#c3c2b7; --muted:#8f8d86;
    --line:#2c2c2a; --line-2:#3e3e3a; --accent:#6da7ec; --accent-soft:#1a2a40;
    --plus:#f08a8a; --plus-soft:#3a1f1f; --minus:#6da7ec; --minus-soft:#172a44;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --bar-plus:#e66767; --bar-minus:#3987e5;
    --chip-buy:#3a1f1f; --chip-buy-ink:#f0a0a0; --chip-sell:#172a44; --chip-sell-ink:#9cc4f5;
    --chip-fill:#173117; --chip-fill-ink:#8fd48f; --chip-note:#2e2a1c; --chip-note-ink:#e2cf8a;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#111110; --surface:#1a1a19; --surface-2:#232321; --ink:#f3f2ec; --ink-2:#c3c2b7; --muted:#8f8d86;
  --line:#2c2c2a; --line-2:#3e3e3a; --accent:#6da7ec; --accent-soft:#1a2a40;
  --plus:#f08a8a; --plus-soft:#3a1f1f; --minus:#6da7ec; --minus-soft:#172a44;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --bar-plus:#e66767; --bar-minus:#3987e5;
  --chip-buy:#3a1f1f; --chip-buy-ink:#f0a0a0; --chip-sell:#172a44; --chip-sell-ink:#9cc4f5;
  --chip-fill:#173117; --chip-fill-ink:#8fd48f; --chip-note:#2e2a1c; --chip-note-ink:#e2cf8a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans JP",system-ui,-apple-system,"Segoe UI","Hiragino Sans","Yu Gothic UI",sans-serif;font-size:14px;line-height:1.6}
a{color:var(--accent)}
.page{max-width:1180px;margin:0 auto;padding:20px 20px 64px}
header.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px 24px;padding-bottom:14px;border-bottom:1px solid var(--line-2)}
.eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0 0 4px}
h1{font-family:"Shippori Mincho","Noto Serif JP","Hiragino Mincho ProN",serif;font-weight:700;font-size:30px;line-height:1.2;margin:0;letter-spacing:.02em;text-wrap:balance}
.top .meta{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;font-size:12px;color:var(--ink-2)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:500;border:1px solid var(--line-2);background:var(--surface)}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.pill.on .dot{background:var(--good)} .pill.off .dot{background:var(--warn)}
nav.nav{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;flex-wrap:wrap;gap:2px 18px;padding:10px 0;border-bottom:1px solid var(--line);font-size:13px}
nav.nav a{text-decoration:none;color:var(--ink-2);padding:2px 0;white-space:nowrap} nav.nav a:hover{color:var(--ink)}
@media (max-width:640px){nav.nav{flex-wrap:nowrap;overflow-x:auto;gap:16px;scrollbar-width:none} .page{padding:14px 14px 48px} h1{font-size:26px} .hero .big{font-size:32px}}
section{margin-top:36px}
h2{font-size:13px;font-weight:700;letter-spacing:.1em;color:var(--ink-2);margin:0 0 12px;display:flex;align-items:baseline;gap:12px}
h2 small{font-weight:400;letter-spacing:0;color:var(--muted)}
.hero{display:grid;grid-template-columns:minmax(260px,1.3fr) repeat(2,minmax(160px,1fr));gap:12px;margin-top:20px}
.hero>div{padding:18px 20px;background:var(--surface);border:1px solid var(--line);border-radius:6px}
.hero .label{font-size:12px;color:var(--muted);margin-bottom:6px}
.hero .big{font-size:40px;font-weight:700;line-height:1.1;letter-spacing:-.01em}
.hero .mid{font-size:26px;font-weight:700;line-height:1.15}
.hero .sub{font-size:13px;color:var(--ink-2);margin-top:6px}
.plus{color:var(--plus)} .minus{color:var(--minus)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}
.kpis.bt{grid-template-columns:repeat(5,1fr)}
@media (max-width:900px){.kpis.bt{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{padding:12px 14px;background:var(--surface);border:1px solid var(--line);border-radius:6px;min-width:0}
.kpi .label{font-size:11px;color:var(--muted);letter-spacing:.04em}
.kpi .value{font-size:20px;font-weight:700;margin-top:2px;line-height:1.2;white-space:nowrap}
.kpi .sub{font-size:11px;color:var(--ink-2);margin-top:2px}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px;font-size:12px;color:var(--muted)}
.filters button{font:inherit;font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line-2);background:var(--surface);color:var(--ink-2);cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--accent-soft);border-color:var(--accent);color:var(--ink);font-weight:500}
.filters button:focus-visible,details summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 16px 10px}
.panel h3{margin:0 0 6px;font-size:14px;font-weight:700}
.panel .note{font-size:12px;color:var(--muted);margin:0 0 8px}
.chart{position:relative;height:340px}
.chart.short{height:220px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:860px){.grid2{grid-template-columns:1fr}.hero{grid-template-columns:1fr}}
.empty{padding:28px 12px;text-align:center;color:var(--muted);font-size:13px;border:1px dashed var(--line-2);border-radius:6px}
details.tv{margin-top:6px;font-size:12px} details.tv summary{cursor:pointer;color:var(--ink-2)}
.tablewrap{overflow-x:auto;margin-top:6px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{font-size:11px;font-weight:500;color:var(--muted);letter-spacing:.04em;border-bottom:1px solid var(--line-2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal;min-width:260px;max-width:520px;color:var(--ink-2);font-size:12px}
tr:hover td{background:var(--surface-2)}
.chip{display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;font-weight:500;white-space:nowrap}
.chip.buy{background:var(--chip-buy);color:var(--chip-buy-ink)} .chip.sell{background:var(--chip-sell);color:var(--chip-sell-ink)}
.chip.fill{background:var(--chip-fill);color:var(--chip-fill-ink)} .chip.note{background:var(--chip-note);color:var(--chip-note-ink)}
.chip.cancel{background:var(--surface-2);color:var(--ink-2)}
.feed{display:flex;flex-direction:column;gap:0}
.feed .item{display:grid;grid-template-columns:96px 84px 1fr;gap:10px;padding:10px 4px;border-bottom:1px solid var(--line);align-items:start}
.feed .date{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;padding-top:2px}
.feed .text{font-size:13px;color:var(--ink)}
.feed .text .reason{display:block;color:var(--ink-2);font-size:12px;margin-top:2px}
@media (max-width:640px){.feed .item{grid-template-columns:1fr;gap:4px}}
.journal details{border:1px solid var(--line);border-radius:6px;background:var(--surface);margin-bottom:10px}
.journal summary{cursor:pointer;padding:10px 14px;font-weight:500;list-style:none;display:flex;gap:10px;align-items:center}
.journal summary::-webkit-details-marker{display:none}
.journal summary::before{content:"▸";color:var(--muted);font-size:12px} .journal details[open] summary::before{content:"▾"}
.journal .body{padding:0 18px 14px;max-width:72ch;font-size:13px;color:var(--ink)}
.journal .body h3{font-size:15px;margin:8px 0 4px} .journal .body h4{font-size:13px;margin:14px 0 4px;color:var(--ink-2);letter-spacing:.04em}
.journal .body ul{padding-left:18px;margin:4px 0} .journal .body li{margin:2px 0} .journal .body hr{border:0;border-top:1px solid var(--line);margin:12px 0}
.journal .body em{color:var(--muted);font-style:normal;font-size:12px}
.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 18px;font-size:13px;color:var(--ink-2)}
.rules b{color:var(--ink);font-weight:500}
footer{margin-top:48px;padding-top:14px;border-top:1px solid var(--line-2);font-size:12px;color:var(--muted);max-width:80ch}
footer p{margin:4px 0}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="page">
<header class="top">
  <div>
    <p class="eyebrow">国内株ペーパートレード ／ 楽天証券想定 ／ 実際の売買はしません</p>
    <h1>紙上運用ボード</h1>
  </div>
  <div class="meta" id="meta"></div>
</header>
<nav class="nav">
  <a href="#overview">概況</a><a href="#perf">資産推移</a><a href="#positions">保有銘柄</a><a href="#feed">売買判断・通知</a>
  <a href="#trades">取引履歴</a><a href="#journal">反省ノート</a><a href="#backtest">検証（バックテスト）</a><a href="#rules">運用ルール</a>
</nav>

<section id="overview" style="margin-top:8px">
  <div class="hero" id="hero"></div>
  <div class="kpis" id="kpis"></div>
</section>

<section id="perf">
  <h2>資産推移 <small>初期資産＝100 として指数化。ベンチマークは同じ日を 100 として比較</small></h2>
  <div class="filters" id="range"><span>期間:</span></div>
  <div class="panel">
    <h3>ポートフォリオとベンチマーク</h3>
    <div class="chart" id="navChartWrap"><canvas id="navChart"></canvas></div>
    <details class="tv"><summary>表で見る</summary><div class="tablewrap" id="navTable"></div></details>
  </div>
  <div class="grid2" style="margin-top:14px">
    <div class="panel">
      <h3>ドローダウン</h3>
      <p class="note">直近の最高資産からの下落率。損切りルールが機能しているかの目安</p>
      <div class="chart short" id="ddChartWrap"><canvas id="ddChart"></canvas></div>
    </div>
    <div class="panel">
      <h3>月次リターン</h3>
      <p class="note">ポートフォリオと TOPIX 連動 ETF の月ごとの騰落率</p>
      <div class="chart short" id="moChartWrap"><canvas id="moChart"></canvas></div>
      <details class="tv"><summary>表で見る</summary><div class="tablewrap" id="moTable"></div></details>
    </div>
  </div>
</section>

<section id="positions">
  <h2>保有銘柄 <small>評価額順。損切り目安は最高値から 3×ATR 下（日々切り上げ）。「コア」は指数の買い持ちで損切りなし</small></h2>
  <div class="tablewrap" id="positionsTable"></div>
  <div id="pendingWrap" style="margin-top:14px"></div>
</section>

<section id="feed">
  <h2>売買判断・通知 <small>「買い判断／売り判断」は翌営業日の寄付で執行される予定。理由も記録</small></h2>
  <div class="feed" id="feedList"></div>
</section>

<section id="trades">
  <h2>取引履歴 <small>約定ベース。手数料 0 円（ゼロコース／かぶミニ寄付取引）・スリッページ 0.05% を想定</small></h2>
  <div class="tablewrap" id="tradesTable"></div>
</section>

<section id="journal">
  <h2>反省ノート <small>毎週金曜と月末に自動作成。ルール変更は根拠つきで履歴に残す</small></h2>
  <div class="journal" id="journalList"></div>
  <div id="paramsWrap"></div>
</section>

<section id="backtest">
  <h2>検証（バックテスト） <small>現在のルールを過去データに当てはめた結果。将来の成績を保証するものではない</small></h2>
  <div id="btWrap"></div>
</section>

<section id="rules">
  <h2>運用ルール <small>config.json の現在値</small></h2>
  <div class="rules" id="rulesList"></div>
</section>

<footer>
  <p><b>免責:</b> このページは実在の株価データを使った投資シミュレーション（ペーパートレード）の記録です。実際の資金は一切使っておらず、証券口座にも接続していません。特定の銘柄の売買を推奨する投資助言ではありません。</p>
  <p><b>データ:</b> Yahoo Finance（yfinance 経由、15〜20 分遅延・分割/配当調整済み）。売買は翌営業日の寄付値で約定したものとして計算。配当は権利落ち日に現金として計上し（金額未発表のものは見込みで計上して確定後に精算）、株式分割は株数と単価を換算します。ベンチマークも分配金込みで比較します。税金（20.315%）は考慮していません。生の株価データは掲載していません。</p>
  <p><b>楽天証券について:</b> 手数料体系（ゼロコース・かぶミニ®）を前提として参照しているだけで、楽天証券株式会社とは無関係の個人の記録です。かぶミニ® は同社の登録商標です。</p>
  <p id="gen"></p>
</footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const L = D.live, M = L.metrics, S = L.series, BN = D.bench_names, B = D.config.benchmarks;
const FONT = '"Noto Sans JP", system-ui, sans-serif';
const $ = (s, el) => (el||document).querySelector(s);
const tok = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtN = n => Math.round(n).toLocaleString('ja-JP');
const yen = n => (n==null||isNaN(n)) ? '—' : fmtN(n) + '円';
const yenS = n => (n==null||isNaN(n)) ? '—' : (n>=0?'+':'-') + fmtN(Math.abs(n)) + '円';
const pct = (x,d) => (x==null||isNaN(x)) ? '—' : (x*100).toFixed(d==null?2:d) + '%';
const pctS = (x,d) => (x==null||isNaN(x)) ? '—' : (x>=0?'+':'-') + Math.abs(x*100).toFixed(d==null?2:d) + '%';
const cls = x => (x==null||isNaN(x)) ? '' : (x>0 ? 'plus' : (x<0 ? 'minus' : ''));
const num = (x,d) => (x==null||isNaN(x)) ? '—' : Number(x).toFixed(d==null?2:d);
const hasData = M.has_data && S.dates.length > 0;
const bname = b => (BN[b]||b) + '(' + b.split('.')[0] + ')';

// ---- ヘッダー ----
$('#meta').innerHTML =
  '<span>基準日 <b>' + esc(L.last_processed || '—') + '</b></span>' +
  '<span>運用開始 ' + esc(L.start_date) + '</span>' +
  '<span class="pill ' + (L.regime===1?'on':(L.regime===0?'off':'')) + '"><span class="dot"></span>相場環境: ' +
    (L.regime===1 ? '強気（TOPIX > 200日線）' : (L.regime===0 ? '弱気（新規買い停止）' : '未判定')) + '</span>';
$('#gen').textContent = '生成 ' + D.generated_at + ' ／ 対象ユニバース ' + D.universe_count + ' 銘柄 ／ 戦略 ' + D.config.strategy.name;

// ---- ヒーロー ----
const final = M.final, init = M.initial;
const dchg = M.daily_change||0, dpct = M.daily_change_pct||0;
$('#hero').innerHTML =
  '<div><div class="label">総資産（現金＋評価額）</div><div class="big">' + yen(final) + '</div>' +
    '<div class="sub">初期資産 ' + yen(init) + (M.flows_total ? ' ／ 追加入金 ' + yen(M.flows_total) : '') + ' ／ 現金 ' + yen(L.cash) + '（' + pct(final?L.cash/final:1,0) + '）' + (L.dividends_cum ? ' ／ 受取配当 ' + yen(L.dividends_cum) : '') + '</div></div>' +
  '<div><div class="label">前日比</div><div class="mid ' + cls(dchg) + '">' + yenS(dchg) + '</div><div class="sub ' + cls(dchg) + '">' + pctS(dpct) + '</div></div>' +
  '<div><div class="label">運用開始来の損益</div><div class="mid ' + cls(M.total_pnl) + '">' + yenS(M.total_pnl||0) + '</div><div class="sub ' + cls(M.total_return) + '">' + pctS(M.total_return) + (M.flows_total ? '（入金の影響を除く）' : '') + '</div></div>';

// ---- KPI ----
const T = M.trades || {};
const pb = M.primary_bench;
const kp = [
  {l:'運用開始来リターン', v:pctS(M.total_return), c:cls(M.total_return), s:(M.n_days||0) + ' 営業日'},
  {l:'TOPIX（同期間）', v:pctS(M.bench && M.bench[pb]), c:cls(M.bench && M.bench[pb]), s:'1306 連動ETF'},
  {l:'超過リターン', v:(M.excess==null?'—':(M.excess>=0?'+':'-')+Math.abs(M.excess*100).toFixed(2)+'pt'), c:cls(M.excess), s:'対 TOPIX'},
  {l:'最大ドローダウン', v:pct(M.max_dd), c:cls(M.max_dd), s:M.max_dd_date ? M.max_dd_date : '—'},
  {l:'シャープレシオ', v:(M.n_days||0) >= 20 ? num(M.sharpe) : '—', c:'', s:(M.n_days||0) >= 20 ? '年率換算・無リスク金利0' : '20営業日以上で表示'},
  {l:'勝率', v:T.n_closed ? pct(T.win_rate,0) : '—', c:'', s:'決済 ' + (T.n_closed||0) + ' 件'},
  {l:'プロフィットファクター', v:num(T.profit_factor), c:'', s:'総利益 ÷ 総損失'},
  {l:'投資比率', v:pct(M.exposure==null?0:M.exposure,0), c:'', s:'ルール運用 ' + (M.n_positions||0) + ' 銘柄' + (M.core_value ? ' ＋ 指数の買い持ち' : '')},
];
$('#kpis').innerHTML = kp.map(k => '<div class="kpi"><div class="label">' + k.l + '</div><div class="value ' + k.c + '">' + k.v + '</div><div class="sub">' + esc(k.s) + '</div></div>').join('');

// ---- チャート ----
const charts = {};
function mk(id, cfg){ if(charts[id]) charts[id].destroy(); if(!window.Chart) return; charts[id] = new Chart($('#'+id), cfg); }
const crosshair = {id:'crosshair', afterDraw(c){ const a = c.tooltip && c.tooltip._active; if(!a||!a.length) return; const x=a[0].element.x, ca=c.chartArea, ctx=c.ctx; ctx.save(); ctx.strokeStyle=tok('--line-2'); ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(x,ca.top); ctx.lineTo(x,ca.bottom); ctx.stroke(); ctx.restore(); }};
const endLabels = {id:'endLabels', afterDatasetsDraw(c){ const ctx=c.ctx, used=[]; c.data.datasets.forEach((ds,i)=>{ const meta=c.getDatasetMeta(i); if(meta.hidden||!meta.data.length) return; const last=meta.data[meta.data.length-1]; let y=last.y; used.sort((a,b)=>a-b); used.forEach(u=>{ if(Math.abs(u-y)<13) y=u+13; }); used.push(y); ctx.save(); ctx.font='500 11px '+FONT; ctx.fillStyle=tok('--ink-2'); ctx.textBaseline='middle'; ctx.fillText(ds.shortLabel||ds.label, last.x+6, y); ctx.restore(); }); }};
function base(yfmt, extra){
  return Object.assign({
    responsive:true, maintainAspectRatio:false, animation:false, interaction:{mode:'index', intersect:false},
    plugins:{ legend:{position:'top', align:'start', labels:{usePointStyle:true, pointStyle:'line', boxWidth:22, color:tok('--ink-2'), font:{family:FONT, size:12}}},
      tooltip:{backgroundColor:tok('--surface'), titleColor:tok('--ink'), bodyColor:tok('--ink-2'), borderColor:tok('--line-2'), borderWidth:1, padding:10, usePointStyle:true,
        titleFont:{family:FONT}, bodyFont:{family:FONT}, callbacks:{label: c => ' ' + c.dataset.label + ': ' + yfmt(c.parsed.y)}} },
    scales:{ x:{grid:{display:false}, border:{color:tok('--line-2')}, ticks:{color:tok('--muted'), maxTicksLimit:8, maxRotation:0, autoSkip:true, font:{family:FONT, size:11}}},
             y:{grid:{color:tok('--line')}, border:{display:false}, ticks:{color:tok('--muted'), font:{family:FONT, size:11}, callback:v => yfmt(v)}} },
    elements:{line:{borderWidth:2, tension:0}, point:{radius:0, hoverRadius:4, hitRadius:14}}
  }, extra||{});
}
const RANGES = [['全期間',0],['1年',252],['6ヶ月',126],['3ヶ月',63],['1ヶ月',21]];
let range = 0;
function slice(arr, n){ return n && arr.length>n ? arr.slice(arr.length-n) : arr; }
function reindex(arr){ const b = arr[0]; return b ? arr.map(v => +(v/b*100).toFixed(3)) : arr; }
function renderRange(){
  const el = $('#range'); el.querySelectorAll('button').forEach(b=>b.remove());
  RANGES.forEach(([lab,n]) => { const b=document.createElement('button'); b.textContent=lab; b.setAttribute('aria-pressed', n===range?'true':'false'); b.disabled = !hasData || (n>0 && S.dates.length<=n && n!==RANGES[1][1] && S.dates.length<22); b.onclick=()=>{ range=n; renderRange(); renderCharts(); }; el.appendChild(b); });
}
function navTable(dates, port, bench){
  let h = '<table><thead><tr><th>日付</th><th class="num">資産額</th><th class="num">ポートフォリオ</th>' + B.map(b=>'<th class="num">'+esc(bname(b))+'</th>').join('') + '</tr></thead><tbody>';
  const navs = slice(S.nav, range);
  for(let i=dates.length-1;i>=Math.max(0,dates.length-60);i--){ h += '<tr><td>'+dates[i]+'</td><td class="num">'+yen(navs[i])+'</td><td class="num">'+port[i].toFixed(2)+'</td>' + B.map(b=>'<td class="num">'+ (bench[b]?bench[b][i].toFixed(2):'—') +'</td>').join('') + '</tr>'; }
  return h + '</tbody></table>' + (dates.length>60?'<p class="note">直近60営業日のみ表示（全データは data/nav.csv）</p>':'');
}
function renderCharts(){
  if(!hasData){ ['navChartWrap','ddChartWrap','moChartWrap'].forEach(id => { $('#'+id).innerHTML = '<div class="empty">最初の営業日を処理するとグラフが表示されます</div>'; }); return; }
  const dates = slice(S.dates, range), port = reindex(slice(S.port, range));
  const bench = {}; B.forEach(b => { if(S.bench[b]) bench[b] = reindex(slice(S.bench[b], range)); });
  const colors = [tok('--s1'), tok('--s2'), tok('--s3')];
  const ds = [{label:'ポートフォリオ', shortLabel:'ポートフォリオ', data:port, borderColor:colors[0], backgroundColor:colors[0]}];
  B.forEach((b,i) => { if(bench[b]) ds.push({label:bname(b), shortLabel:BN[b]||b, data:bench[b], borderColor:colors[i+1], backgroundColor:colors[i+1], borderWidth:1.5}); });
  mk('navChart', {type:'line', data:{labels:dates, datasets:ds}, options:base(v=>Number(v).toFixed(1), {layout:{padding:{right:96}}}), plugins:[crosshair,endLabels]});
  $('#navTable').innerHTML = navTable(dates, port, bench);
  const dd = slice(S.drawdown, range);
  const c1 = tok('--s1');
  mk('ddChart', {type:'line', data:{labels:dates, datasets:[{label:'ドローダウン', data:dd, borderColor:c1, backgroundColor:c1+'22', fill:'origin'}]},
    options:(()=>{ const o=base(v=>Number(v).toFixed(1)+'%'); o.plugins.legend.display=false; o.scales.y.max=0; o.scales.y.suggestedMin=-5; return o; })(), plugins:[crosshair]});
  const mo = M.monthly||[];
  const mds = [{label:'ポートフォリオ', data:mo.map(m=>+(m.port*100).toFixed(2)), backgroundColor:tok('--s1'), borderRadius:4, borderSkipped:'start'}];
  if(mo.length && mo[0].bench!=null) mds.push({label:bname(pb), data:mo.map(m=>+(m.bench*100).toFixed(2)), backgroundColor:tok('--s2'), borderRadius:4, borderSkipped:'start'});
  mk('moChart', {type:'bar', data:{labels:mo.map(m=>m.month), datasets:mds},
    options:(()=>{ const o=base(v=>Number(v).toFixed(1)+'%'); o.interaction={mode:'index',intersect:false}; o.plugins.legend.labels.pointStyle='rect'; o.datasets={bar:{categoryPercentage:0.7, barPercentage:0.9}}; o.scales.y.grid.color=tok('--line'); return o; })()});
  $('#moTable').innerHTML = '<table><thead><tr><th>月</th><th class="num">ポートフォリオ</th><th class="num">TOPIX</th><th class="num">差</th></tr></thead><tbody>' +
    mo.slice().reverse().map(m=>'<tr><td>'+m.month+'</td><td class="num '+cls(m.port)+'">'+pctS(m.port)+'</td><td class="num '+cls(m.bench)+'">'+pctS(m.bench)+'</td><td class="num">'+(m.bench==null?'—':((m.port-m.bench)>=0?'+':'-')+Math.abs((m.port-m.bench)*100).toFixed(2)+'pt')+'</td></tr>').join('') + '</tbody></table>';
  renderBT();
}

// ---- 保有銘柄 ----
function renderPositions(){
  const P = L.positions||[];
  if(!P.length){ $('#positionsTable').innerHTML = '<div class="empty">現在、保有銘柄はありません（全額現金 ' + yen(L.cash) + '）</div>'; }
  else {
    $('#positionsTable').innerHTML = '<table><thead><tr><th>区分</th><th>コード</th><th>銘柄</th><th>業種</th><th class="num">株数</th><th class="num">取得単価</th><th class="num">現在値</th><th class="num">前日比</th><th class="num">評価額</th><th class="num">損益</th><th class="num">損益率</th><th class="num">比率</th><th class="num">損切り目安</th><th class="num">保有日数</th></tr></thead><tbody>' +
      P.map(p => '<tr><td>'+(p.sleeve==='core'?'<span class="chip note">コア</span>':'<span class="chip cancel">ルール</span>')+'</td><td>'+p.code+'</td><td title="'+esc(p.reason)+'">'+esc(p.name)+'</td><td>'+esc(p.sector)+'</td><td class="num">'+p.shares+'</td><td class="num">'+fmtN(p.avg_price)+'</td><td class="num">'+fmtN(p.last)+'</td><td class="num '+cls(p.day_change)+'">'+pctS(p.day_change,1)+'</td><td class="num">'+fmtN(p.value)+'</td><td class="num '+cls(p.pnl)+'">'+yenS(p.pnl)+'</td><td class="num '+cls(p.pnl_pct)+'">'+pctS(p.pnl_pct,1)+'</td><td class="num">'+pct(p.weight,1)+'</td><td class="num">'+(p.stop==null?'なし':fmtN(p.stop))+'</td><td class="num">'+p.days+'</td></tr>').join('') +
      '</tbody></table>';
  }
  const Q = L.pending||[];
  const SC = L.scheduled_cashflows||[];
  const sched = SC.length ? '<div class="panel" style="margin-bottom:14px"><h3>入金の予定</h3>' + SC.map(c => '<p class="note" style="color:var(--ink);font-size:13px;margin:4px 0">' + esc(c.date) + ' の寄付前に <b>' + yen(c.amount) + '</b> を追加' + (c.sleeve==='core' ? 'し、同日の寄付で ' + esc(c.name) + '(' + esc(c.code) + ') を買い付けて持ち続けます（コア）' : 'します') + '。' + esc(c.note||'') + '</p>').join('') + '</div>' : '';
  $('#pendingWrap').innerHTML = sched + (Q.length ? '<h2>翌営業日の寄付で執行予定 <small>' + Q.length + ' 件</small></h2><div class="tablewrap"><table><thead><tr><th>売買</th><th>コード</th><th>銘柄</th><th class="num">株数</th><th class="num">判断時終値</th><th class="num">概算金額</th><th class="num">想定損切り</th><th>判断日</th><th>理由</th></tr></thead><tbody>' +
    Q.map(o => '<tr><td><span class="chip '+(o.side==='BUY'?'buy':'sell')+'">'+(o.side==='BUY'?'買い':'売り')+'</span></td><td>'+o.code+'</td><td>'+esc(o.name)+'</td><td class="num">'+(o.shares==null?'金額指定':o.shares)+'</td><td class="num">'+(o.signal_close==null?'—':fmtN(o.signal_close))+'</td><td class="num">'+yen(o.shares==null?o.budget:o.shares*o.signal_close)+'</td><td class="num">'+(o.planned_stop?fmtN(o.planned_stop):'—')+'</td><td>'+esc(o.signal_date)+'</td><td class="wrap">'+esc(o.reason)+'</td></tr>').join('') + '</tbody></table></div>' : '');
}

// ---- 判断・通知 ----
const CHIP = {BUY_SIGNAL:['buy','買い判断'], SELL_SIGNAL:['sell','売り判断'], FILLED_BUY:['fill','買付約定'], FILLED_SELL:['fill','売却約定'], CANCELLED:['cancel','取消'], REVIEW:['note','反省ノート'], PARAM_CHANGE:['note','ルール変更'], INFO:['cancel','お知らせ'], DIVIDEND:['fill','配当'], SPLIT:['note','株式分割'], CASHFLOW:['note','入金']};
function renderFeed(){
  const E = (L.events||[]).filter(e => CHIP[e.type]);
  if(!E.length){ $('#feedList').innerHTML = '<div class="empty">まだ売買判断はありません。毎営業日の大引け後に判断が記録されます</div>'; return; }
  $('#feedList').innerHTML = E.map(e => { const c = CHIP[e.type]; const text = e.text || e.reason || ''; const i = text.indexOf('理由:');
    const head = i>0 ? text.slice(0,i) : text, reason = i>0 ? text.slice(i) : '';
    return '<div class="item"><div class="date">'+esc(e.date)+'</div><div><span class="chip '+c[0]+'">'+c[1]+'</span></div><div class="text">'+esc(head)+(reason?'<span class="reason">'+esc(reason)+'</span>':'')+'</div></div>'; }).join('');
}

// ---- 取引履歴 ----
function tradesTable(rows){
  if(!rows||!rows.length) return '<div class="empty">まだ約定はありません</div>';
  return '<table><thead><tr><th>日付</th><th>売買</th><th>コード</th><th>銘柄</th><th class="num">株数</th><th class="num">単価</th><th class="num">金額</th><th class="num">実現損益</th><th class="num">損益率</th><th class="num">保有日数</th><th>手仕舞い</th><th>理由</th></tr></thead><tbody>' +
    rows.map(r => '<tr><td>'+esc(r.date)+'</td><td><span class="chip '+(r.side==='BUY'?'buy':'sell')+'">'+(r.side==='BUY'?'買付':'売却')+'</span></td><td>'+esc(r.code)+'</td><td>'+esc(r.name)+'</td><td class="num">'+r.shares+'</td><td class="num">'+fmtN(r.price)+'</td><td class="num">'+fmtN(r.amount)+'</td><td class="num '+cls(r.realized_pnl)+'">'+(r.realized_pnl==null?'—':yenS(r.realized_pnl))+'</td><td class="num '+cls(r.pnl_pct)+'">'+(r.pnl_pct==null?'—':pctS(r.pnl_pct,1))+'</td><td class="num">'+(r.holding_days==null?'—':r.holding_days)+'</td><td>'+esc({stop:'ストップ',trend:'トレンド崩れ',momentum:'モメンタム失速'}[r.exit_type]||(r.side==='BUY'?'—':r.exit_type||''))+'</td><td class="wrap">'+esc(r.reason)+'</td></tr>').join('') + '</tbody></table>';
}
$('#tradesTable').innerHTML = tradesTable(L.trades);

// ---- 反省ノート ----
function renderJournal(){
  const J = D.journal||[];
  $('#journalList').innerHTML = J.length ? J.map((j,i) => '<details'+(i===0?' open':'')+'><summary>'+esc(j.date)+' <span class="chip note">'+esc(j.label)+'</span></summary><div class="body">'+j.html+'</div></details>').join('')
    : '<div class="empty">最初の反省ノートは運用開始後の金曜日（週次）に作成されます</div>';
  const H = D.params_history||[];
  $('#paramsWrap').innerHTML = H.length ? '<h2 style="margin-top:18px">ルール変更の履歴</h2><div class="tablewrap"><table><thead><tr><th>日付</th><th>種別</th><th>パラメータ</th><th class="num">変更前</th><th class="num">変更後</th><th>根拠</th></tr></thead><tbody>' +
    H.slice().reverse().map(h=>'<tr><td>'+esc(h.date)+'</td><td>'+esc(({calibration:'開始前の調整', owner_decision:'オーナーの決定', bugfix:'不具合の修正', weekly:'週次の反省', monthly:'月次の反省', manual:'臨時の反省'})[h.kind]||h.kind)+'</td><td>'+esc(h.param)+'</td><td class="num">'+esc(h.old)+'</td><td class="num">'+esc(h.new)+'</td><td class="wrap">'+esc(h.reason)+'</td></tr>').join('') + '</tbody></table></div>' : '';
}

// ---- バックテスト ----
function renderBT(){
  const BT = D.backtest;
  if(!BT){ $('#btWrap').innerHTML = '<div class="empty">python run.py backtest を実行すると検証結果が表示されます</div>'; return; }
  const m = BT.metrics, t = m.trades||{};
  const tiles = [
    {l:'検証期間', v:BT.summary.start + ' 〜 ' + BT.summary.end, c:''},
    {l:'最終資産', v:yen(m.final), c:cls(m.total_return)},
    {l:'リターン', v:pctS(m.total_return,1), c:cls(m.total_return)},
    {l:'TOPIX（同期間）', v:pctS(m.bench && m.bench[pb],1), c:cls(m.bench && m.bench[pb])},
    {l:'最大ドローダウン', v:pct(m.max_dd,1), c:cls(m.max_dd)},
    {l:'シャープレシオ', v:num(m.sharpe), c:''},
    {l:'勝率', v:t.n_closed ? pct(t.win_rate,0) + '（' + t.n_closed + '件）' : '—', c:''},
    {l:'プロフィットファクター', v:num(t.profit_factor), c:''},
    {l:'平均保有日数', v:t.avg_holding_days!=null ? Math.round(t.avg_holding_days) + '日' : '—', c:''},
    {l:'平均投資比率', v:pct(m.exposure_avg,0), c:''},
  ];
  if(!$('#btChart')){
    $('#btWrap').innerHTML = '<div class="kpis bt">' + tiles.map(k=>'<div class="kpi"><div class="label">'+k.l+'</div><div class="value '+k.c+'" style="font-size:17px">'+k.v+'</div></div>').join('') + '</div>' +
      '<div class="panel" style="margin-top:12px"><h3>検証期間の資産推移（指数化）</h3><div class="chart" id="btChartWrap"><canvas id="btChart"></canvas></div></div>' +
      '<details class="tv"><summary>検証期間の取引（直近 ' + (BT.trades||[]).length + ' 件 ／ 全 ' + BT.n_trades + ' 件）</summary><div class="tablewrap">' + tradesTable(BT.trades) + '</div></details>';
  }
  const s = BT.series; if(!s.dates.length) return;
  const colors = [tok('--s1'), tok('--s2'), tok('--s3')];
  const ds = [{label:'ポートフォリオ', data:s.port, borderColor:colors[0], backgroundColor:colors[0]}];
  B.forEach((b,i)=>{ if(s.bench[b]) ds.push({label:bname(b), shortLabel:BN[b]||b, data:s.bench[b], borderColor:colors[i+1], backgroundColor:colors[i+1], borderWidth:1.5}); });
  mk('btChart', {type:'line', data:{labels:s.dates, datasets:ds}, options:base(v=>Number(v).toFixed(1), {layout:{padding:{right:96}}}), plugins:[crosshair,endLabels]});
}

// ---- ルール ----
(function(){ const s = D.config.strategy, b = D.config.broker;
  const items = [
    ['元手', yen(M.contributed || D.config.initial_cash) + (M.flows_total ? '（初期 ' + yen(D.config.initial_cash) + ' ＋ 追加入金 ' + yen(M.flows_total) + '）。成績は入金の影響を除いて計算' : '')],
    ...((M.core_value || (L.scheduled_cashflows||[]).length) ? [['資産の構成', 'コア＝TOPIX連動ETF(1306) の買い持ち。売買ルールと損切りの対象外で、相場に居続けるための土台。サテライト＝下のルールで運用する個別株。資金管理（1%リスク・1銘柄12%）はサテライトの資産額で計算し、両者のリバランスはしない']] : []),
    ['相場環境フィルタ', 'TOPIX連動ETF(1306) が ' + s.regime_sma + '日線より上のときだけ新規買い'],
    ['銘柄選定', '終値 > ' + s.sma_fast + '日線 > ' + s.sma_slow + '日線、12-1ヶ月モメンタム上位 ' + s.top_n_candidates + ' 銘柄、20日高値の95%以上、20日平均売買代金 ' + (s.min_avg_turnover_jpy/1e8).toFixed(0) + '億円以上'],
    ['資金管理', '1トレードの想定損失 = 資産の ' + (s.risk_per_trade*100).toFixed(0) + '%（損切り幅 ' + s.atr_stop_mult + '×ATR' + s.atr_period + '）、1銘柄上限 ' + (s.max_position_weight*100).toFixed(0) + '%、最大 ' + s.max_positions + ' 銘柄、レバレッジなし'],
    ['手仕舞い', 'トレーリングストップ（最高値 − ' + s.atr_stop_mult + '×ATR）／' + s.sma_slow + '日線割れ／モメンタムがマイナス。売却後 ' + s.reentry_cooldown_days + ' 営業日は再エントリーしない'],
    ['執行', '判断は大引け後の終値、約定は翌営業日の寄付。手数料 ' + (b.commission_rate*100) + '%（' + esc(b.name) + '）、スリッページ ' + (b.slippage_rate*100).toFixed(2) + '%、最低発注額 ' + yen(b.min_order_jpy)],
    ['反省と改善', '毎週金曜・月末に反省ノートを自動作成。損切りの「往復ビンタ率」など統計的な根拠が ' + '一定件数そろった場合のみ、許容範囲内（ストップ幅 2.0〜4.0×ATR）でルールを自動調整し履歴に残す'],
  ];
  $('#rulesList').innerHTML = items.map(([k,v]) => '<div><b>' + k + '</b><br>' + v + '</div>').join('');
})();

renderRange(); renderPositions(); renderFeed(); renderJournal(); renderCharts();
try { matchMedia('(prefers-color-scheme: dark)').addEventListener('change', renderCharts); } catch(e){}
try { new MutationObserver(renderCharts).observe(document.documentElement, {attributes:true, attributeFilter:['data-theme']}); } catch(e){}
})();
</script>
"""
