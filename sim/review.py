"""反省ノート（週次・月次）の自動生成と、ガードレール付きのルール調整。

反省の流れ
  1. 期間の成績をベンチマーク（TOPIX ETF）と比較する
  2. 期間中に決済したトレードを 1 件ずつ振り返る（損切り後に株価が戻った「往復ビンタ」も検出）
  3. ルールに照らして「良かった点」「悪かった点」「仮説」「次のアクション」を書く
  4. 十分な件数の証拠があるときだけ、許容範囲内でパラメータを調整し、履歴を残す
  5. （任意）Claude CLI があれば AI による深い考察を追記する
"""
import json
import os
import shutil
import subprocess
from datetime import datetime

import pandas as pd

from .config import DATA_DIR, save_config
from .metrics import load_nav, load_trades, compute_metrics, _ret_since, drawdown
from .strategy import fmt_pct, fmt_yen
from .universe import name_of, code_of, SECTORS

JOURNAL_DIR = os.path.join(DATA_DIR, "journal")
PARAMS_HISTORY = os.path.join(DATA_DIR, "params_history.jsonl")
KNOWLEDGE_LOG = os.path.join(os.path.dirname(DATA_DIR), "knowledge", "learning_log.md")

TUNE_BOUNDS = {"atr_stop_mult": (2.0, 4.0)}
KIND_LABEL = {"weekly": "週次", "monthly": "月次", "manual": "臨時"}
KIND_DAYS = {"weekly": 7, "monthly": 31, "manual": 7}


def should_review(D: pd.Timestamp, tdays: list, cfg: dict) -> list:
    kinds = []
    if D.weekday() == cfg["review"].get("weekly_weekday", 4):
        kinds.append("weekly")
    i = tdays.index(D) if D in tdays else -1
    if i >= 0 and (i + 1 >= len(tdays) or tdays[i + 1].month != D.month):
        kinds.append("monthly")
    return kinds


def journal_path(asof: str, kind: str) -> str:
    return os.path.join(JOURNAL_DIR, f"{asof}_{kind}.md")


def whipsaw_analysis(trades: pd.DataFrame, sim, asof: pd.Timestamp, lookback_tdays: int = 60, after: int = 10) -> dict:
    """損切り後 after 営業日で株価が売値を上回ったトレードを数える。"""
    out = {"n_stop": 0, "n_whipsaw": 0, "items": []}
    if trades is None or trades.empty:
        return out
    closed = trades[(trades["side"] == "SELL") & (trades["exit_type"].astype(str) == "stop")]
    if closed.empty:
        return out
    ord_asof = sim.tday_ord.get(asof, len(sim.tdays) - 1)
    for _, r in closed.iterrows():
        d = pd.Timestamp(r["date"])
        if ord_asof - sim.tday_ord.get(d, ord_asof) > lookback_tdays:
            continue
        t = r["ticker"]
        idx = sim.idx.get(t, {})
        i = idx.get(d)
        if i is None:
            continue
        j = i + after
        closes = sim.arr[t]["Close"]
        if j >= len(closes):
            continue  # まだ after 日経っていない
        later = float(closes[j])
        out["n_stop"] += 1
        rebound = later / float(r["price"]) - 1.0
        if rebound > 0.0:
            out["n_whipsaw"] += 1
        out["items"].append({"name": r["name"], "code": r["code"], "date": r["date"], "exit_price": float(r["price"]),
                             "later_price": later, "rebound": rebound, "pnl": float(r["realized_pnl"])})
    return out


def _last_param_change() -> dict:
    if not os.path.exists(PARAMS_HISTORY):
        return None
    last = None
    with open(PARAMS_HISTORY, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line)
    return last


def propose_tuning(cfg: dict, whip: dict, stats: dict, asof: pd.Timestamp) -> tuple:
    """(変更リスト, 見送り理由) を返す。"""
    rv = cfg["review"]
    p = cfg["strategy"]
    if not rv.get("auto_tune", True):
        return [], "自動調整は無効（config.review.auto_tune=false）"
    last = _last_param_change()
    if last:
        since = (asof - pd.Timestamp(last["date"])).days
        if since < rv.get("tune_cooldown_days", 20) * 1.4:
            return [], f"前回のルール変更（{last['date']}）から日が浅いため、効果を見極め中"
    min_n = rv.get("tune_min_trades", 6)
    changes = []
    if whip["n_stop"] >= min_n:
        ratio = whip["n_whipsaw"] / whip["n_stop"]
        lo, hi = TUNE_BOUNDS["atr_stop_mult"]
        if ratio >= 0.6 and p["atr_stop_mult"] + 0.5 <= hi:
            changes.append({"param": "atr_stop_mult", "old": p["atr_stop_mult"], "new": round(p["atr_stop_mult"] + 0.5, 1),
                            "reason": f"直近60営業日の損切り {whip['n_stop']} 件のうち {whip['n_whipsaw']} 件で株価が10営業日後に売値を上回った（往復ビンタ率 {ratio * 100:.0f}%）。ストップ幅を広げて早すぎる損切りを減らす。"})
        elif ratio <= 0.2 and stats.get("by_exit_type", {}).get("stop", {}).get("avg_pnl", 0) < 0 and p["atr_stop_mult"] - 0.5 >= lo:
            avg = stats["by_exit_type"]["stop"]["avg_pnl"]
            if abs(avg) > cfg["initial_cash"] * p["risk_per_trade"] * 1.5:
                changes.append({"param": "atr_stop_mult", "old": p["atr_stop_mult"], "new": round(p["atr_stop_mult"] - 0.5, 1),
                                "reason": f"損切り後に株価が戻るケースは少なく（{ratio * 100:.0f}%）、一方で損切り1回あたりの平均損失 {avg:,.0f}円 が想定リスク（資産の{p['risk_per_trade'] * 100:.0f}%）を大きく超えている。ストップ幅を狭めて1回の損失を抑える。"})
    if not changes:
        return [], (f"損切りトレードが {whip['n_stop']} 件で、判断に必要な {min_n} 件に達していない" if whip["n_stop"] < min_n
                    else "統計上、変更を正当化できる偏りは見られない")
    return changes, ""


def apply_tuning(cfg: dict, changes: list, asof: str, kind: str, log=print) -> None:
    for ch in changes:
        cfg["strategy"][ch["param"]] = ch["new"]
        rec = {"date": asof, "kind": kind, **ch}
        with open(PARAMS_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"  ルール変更: {ch['param']} {ch['old']} → {ch['new']}")
    save_config(cfg)


def ai_review(cfg: dict, context: str, log=print):
    ai = cfg["review"].get("ai_review", {})
    if not ai.get("enabled"):
        return None
    cmd = shutil.which(ai.get("command", "claude"))
    if not cmd:
        log("  AI考察: Claude CLI が見つからないためスキップ")
        return None
    prompt = ("あなたは日本株のトレンドフォロー運用を行う投資家です。以下はペーパートレードの成績データです。"
              "ルール違反や改善余地を率直に指摘し、次週に検証すべき仮説を3つ、日本語で簡潔に書いてください。"
              "投資助言ではなくシミュレーションの振り返りです。\n\n" + context)
    try:
        r = subprocess.run([cmd, "-p", prompt], capture_output=True, text=True, timeout=240, encoding="utf-8")
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception as e:
        log(f"  AI考察に失敗: {e}")
        return None


def _md_trade(r) -> str:
    pnl = r["realized_pnl"]
    return (f"- {r['date']} 売却 {r['name']}({r['code']}) {int(r['shares'])}株 @{r['price']:,.0f}円 "
            f"→ 損益 **{pnl:+,.0f}円（{r['pnl_pct'] * 100:+.1f}%）** {int(r['holding_days'])}日保有。{str(r['reason'])[:90]}…")


def run_review(cfg: dict, sim, kind: str, asof: pd.Timestamp, log=print, force: bool = False) -> dict:
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    asof_s = asof.strftime("%Y-%m-%d")
    path = journal_path(asof_s, kind)
    if os.path.exists(path) and not force:
        return None
    nav = load_nav(sim.state_dir)
    trades = load_trades(sim.state_dir)
    m = compute_metrics(nav, trades, cfg["benchmarks"], cfg["initial_cash"])
    p = cfg["strategy"]
    days = KIND_DAYS[kind]
    navs = nav["nav"].astype(float) if not nav.empty else pd.Series(dtype=float)
    primary = cfg["benchmarks"][0]
    pr = _ret_since(navs, days) if not nav.empty else None
    br = _ret_since(nav[primary].astype(float), days) if (not nav.empty and primary in nav) else None
    period_trades = pd.DataFrame()
    if not trades.empty:
        td = pd.to_datetime(trades["date"])
        period_trades = trades[td > asof - pd.Timedelta(days=days)]
    closed = period_trades[period_trades["side"] == "SELL"] if not period_trades.empty else pd.DataFrame()
    buys = period_trades[period_trades["side"] == "BUY"] if not period_trades.empty else pd.DataFrame()
    whip = whipsaw_analysis(trades, sim, asof)
    stats = m.get("trades", {})
    regime = bool(m.get("regime"))
    positions = sim.state["positions"]

    # 期間中の平均投資比率
    exp_period = None
    if not nav.empty:
        sub = nav[nav.index > asof - pd.Timedelta(days=days)]
        if not sub.empty:
            exp_period = float(sub["exposure"].mean())

    good, bad, hyp, learn = [], [], [], []
    if pr is not None and br is not None:
        diff = pr - br
        if diff >= 0.005:
            good.append(f"期間リターン {fmt_pct(pr)} は TOPIX の {fmt_pct(br)} を {diff * 100:+.1f}pt 上回った。")
        elif diff <= -0.005:
            bad.append(f"期間リターン {fmt_pct(pr)} は TOPIX の {fmt_pct(br)} を {abs(diff) * 100:.1f}pt 下回った。")
        else:
            good.append(f"期間リターン {fmt_pct(pr)} は TOPIX（{fmt_pct(br)}）とほぼ同等だった。")
    elif not nav.empty:
        good.append("運用開始直後のため、期間比較はまだできない。")
    n_pending_buy = sum(1 for o in sim.state.get("pending_orders", []) if o["side"] == "BUY")
    if len(nav) < 5:
        good.append(f"運用開始直後。ルール通りに候補を選び、{n_pending_buy} 件の買い注文が翌営業日の寄付で執行される予定。" if n_pending_buy
                    else "運用開始直後。判断材料（営業日数・トレード件数）を蓄積する段階。")
    elif regime:
        if exp_period is not None and exp_period < 0.5:
            bad.append(f"TOPIX が 200 日線の上にある強気局面なのに、平均投資比率が {exp_period * 100:.0f}% と低い。候補が少ないなら「20日高値の95%以上」「売買代金5億円以上」のフィルタが厳しすぎる可能性。")
            hyp.append("仮説: 高値圏条件を 95%→90% に緩めると投資比率は上がるが、押し目買いになりトレンド確認が弱まる。バックテストで損益比を比較してから決める。")
        elif exp_period is not None:
            good.append(f"強気局面で投資比率 {exp_period * 100:.0f}% を維持できた（ルール通り）。")
    else:
        good.append("TOPIX が 200 日線を下回る弱気局面のため、ルール通り新規買いを停止中。保有銘柄はトレーリングストップで管理する。")
        learn.append("弱気局面での現金保有は「守り」。次の強気転換（TOPIX が 200 日線を上抜け）を見逃さないよう、毎日の相場環境判定を確認する。")
    if whip["n_stop"] >= 3:
        ratio = whip["n_whipsaw"] / whip["n_stop"]
        if ratio >= 0.5:
            bad.append(f"損切り後に株価が戻る「往復ビンタ」が {whip['n_whipsaw']}/{whip['n_stop']} 件。ストップ幅（{p['atr_stop_mult']}×ATR）が浅い可能性。")
            hyp.append(f"仮説: ストップを {p['atr_stop_mult'] + 0.5}×ATR に広げると往復ビンタは減るが、1回あたりの損失は増える。損益比（プロフィットファクター）で判断する。")
        else:
            good.append(f"損切り後に株価が戻ったケースは {whip['n_whipsaw']}/{whip['n_stop']} 件に留まり、ストップ幅は概ね機能している。")
    if not closed.empty:
        big = closed[closed["realized_pnl"] < -cfg["initial_cash"] * p["risk_per_trade"] * 1.5]
        for _, r in big.iterrows():
            bad.append(f"{r['name']}({r['code']}) の損失 {r['realized_pnl']:,.0f}円 は想定リスク（資産の{p['risk_per_trade'] * 100:.0f}%）を大きく超えた。寄付のギャップダウンか、ATR 急拡大が原因と考えられる。")
            learn.append("決算発表や重要イベントの前後はギャップが大きくなる。保有銘柄の決算日を把握し、決算またぎのリスク管理（ポジション縮小）を検討する。")
    sectors = {}
    for t in positions:
        s = SECTORS.get(t, "その他")
        sectors[s] = sectors.get(s, 0) + 1
    for s, n in sectors.items():
        if n >= 3:
            bad.append(f"同一業種（{s}）に {n} 銘柄が集中している。相場のテーマが崩れたとき同時に損失が出るリスク。")
            hyp.append("仮説: 業種あたり最大 2 銘柄の制限を加えると、分散は改善するがモメンタム上位の取りこぼしが出る。")
    if m.get("current_dd") is not None and m["current_dd"] < -0.05:
        bad.append(f"ピークから {m['current_dd'] * 100:.1f}% のドローダウン中。ルール通りストップで損失を限定し、感情で早売り・ナンピンをしない。")
    if stats.get("n_closed", 0) >= 10:
        wr, pf = stats.get("win_rate"), stats.get("profit_factor")
        if pf is not None and pf < 1.0:
            bad.append(f"決済 {stats['n_closed']} 件のプロフィットファクターが {pf:.2f} と 1 を下回り、戦略が機能していない。エントリー条件と損切り幅を根本から見直す。")
        elif pf is not None and pf >= 1.5:
            good.append(f"決済 {stats['n_closed']} 件でプロフィットファクター {pf:.2f}、勝率 {wr * 100:.0f}%。トレンドフォローらしく「小さく負けて大きく勝つ」形になっている。")
        else:
            good.append(f"決済 {stats['n_closed']} 件、勝率 {wr * 100:.0f}%、プロフィットファクター {pf:.2f}（{'合格' if pf and pf >= 1 else '要改善'}）。")
    if not good and not bad:
        good.append("特筆すべき問題はなく、ルール通りに運用できた。")
    if not learn:
        learn.append("保有銘柄と候補上位の直近ニュース・決算日を確認し、ルール外の判断材料が必要か検討する（学習ログに記録）。")

    changes, skip_reason = propose_tuning(cfg, whip, stats, asof)

    # ---- Markdown ----
    L = []
    L.append(f"# 反省ノート（{KIND_LABEL[kind]}）{asof_s}\n")
    L.append("## 1. 結果")
    L.append(f"- 総資産: {m['final']:,.0f}円（運用開始来 {fmt_pct(m['total_return'])}、TOPIX {fmt_pct(m['bench'].get(primary)) if m['bench'] else 'n/a'}）")
    if pr is not None:
        L.append(f"- 直近{days}日: ポートフォリオ {fmt_pct(pr)} / TOPIX {fmt_pct(br) if br is not None else 'n/a'}")
    L.append(f"- 最大ドローダウン（開始来）: {fmt_pct(m['max_dd'], signed=False) if m.get('max_dd') is not None else 'n/a'}、現在 {fmt_pct(m['current_dd'], signed=False) if m.get('current_dd') is not None else 'n/a'}")
    L.append(f"- 保有 {len(positions)} 銘柄 / 現金比率 {(1 - m['exposure']) * 100 if m.get('exposure') is not None else 100:.0f}% / 相場環境: {'強気（TOPIX > 200日線）' if regime else '弱気（TOPIX < 200日線）'}")
    if stats.get("n_closed"):
        L.append(f"- 決済累計 {stats['n_closed']} 件、勝率 {stats['win_rate'] * 100:.0f}%、プロフィットファクター {stats['profit_factor']:.2f}" if stats.get("profit_factor") else f"- 決済累計 {stats['n_closed']} 件、勝率 {stats['win_rate'] * 100:.0f}%")
    L.append("")
    L.append("## 2. 期間中の売買")
    L.append(f"- 買付 {len(buys)} 件、売却 {len(closed)} 件" + (f"（勝ち {(closed['realized_pnl'] > 0).sum()} / 負け {(closed['realized_pnl'] <= 0).sum()}）" if not closed.empty else ""))
    for _, r in closed.iterrows():
        L.append(_md_trade(r))
    for _, r in buys.iterrows():
        L.append(f"- {r['date']} 買付 {r['name']}({r['code']}) {int(r['shares'])}株 @{r['price']:,.0f}円")
    if whip["items"]:
        L.append(f"- 損切り後10営業日の追跡: {whip['n_stop']} 件中 {whip['n_whipsaw']} 件で株価が売値を上回った")
    L.append("")
    L.append("## 3. 良かった点")
    L += [f"- {g}" for g in good]
    L.append("")
    L.append("## 4. 悪かった点・反省")
    L += [f"- {b}" for b in bad] if bad else ["- 特になし"]
    L.append("")
    L.append("## 5. 仮説（次に検証すること）")
    L += [f"- {h}" for h in hyp] if hyp else ["- 現行ルールを継続し、判断材料（トレード件数）を蓄積する。"]
    L.append("")
    L.append("## 6. 学習事項")
    L += [f"- {x}" for x in learn]
    L.append("")
    L.append("## 7. 次のアクション（ルール変更）")
    if changes:
        for ch in changes:
            L.append(f"- **{ch['param']}: {ch['old']} → {ch['new']}** — {ch['reason']}")
        apply_tuning(cfg, changes, asof_s, kind, log)
    else:
        L.append(f"- 変更なし（{skip_reason}）")
    L.append("")
    L.append("## 8. 現在の保有")
    if positions:
        for t, pos in positions.items():
            lc = pos.get("last_close", pos["avg_price"])
            L.append(f"- {name_of(t)}({code_of(t)}) {pos['shares']}株 取得 {pos['avg_price']:,.0f}円 → 現在 {lc:,.0f}円（{(lc / pos['avg_price'] - 1) * 100:+.1f}%）損切り目安 {pos['stop_price']:,.0f}円")
    else:
        L.append("- なし（全額現金）")

    context = "\n".join(L)
    ai_text = ai_review(cfg, context, log)
    if ai_text:
        L.append("\n## 9. AI による考察\n")
        L.append(ai_text)
    L.append(f"\n---\n_自動生成 {datetime.now().strftime('%Y-%m-%d %H:%M')} / 戦略 {p['name']} / これは実際の投資ではなくシミュレーションの振り返りです。_\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    summary = (f"{KIND_LABEL[kind]}の反省ノートを作成。総資産 {m['final']:,.0f}円（開始来 {fmt_pct(m['total_return'])}）。"
               + (f"良かった点: {good[0]}" if good else "") + (f" 反省: {bad[0]}" if bad else "")
               + (f" ルール変更: " + ", ".join(f"{c['param']} {c['old']}→{c['new']}" for c in changes) if changes else ""))
    ev = sim.event(asof, "REVIEW", kind=kind, path=os.path.relpath(path, os.path.dirname(DATA_DIR)), text=summary)
    for ch in changes:
        sim.event(asof, "PARAM_CHANGE", param=ch["param"], old=ch["old"], new=ch["new"], reason=ch["reason"],
                  text=f"ルール変更 {ch['param']}: {ch['old']} → {ch['new']}。{ch['reason']}")
    log(f"  反省ノート: {path}")
    return {"path": path, "summary": summary, "changes": changes, "event": ev}


def list_journal(limit: int = 12) -> list:
    if not os.path.isdir(JOURNAL_DIR):
        return []
    files = sorted((f for f in os.listdir(JOURNAL_DIR) if f.endswith(".md")), reverse=True)[:limit]
    out = []
    for fn in files:
        with open(os.path.join(JOURNAL_DIR, fn), "r", encoding="utf-8") as f:
            text = f.read()
        date, kind = fn[:-3].split("_", 1)
        out.append({"file": fn, "date": date, "kind": kind, "label": KIND_LABEL.get(kind, kind), "text": text})
    return out


def params_history() -> list:
    if not os.path.exists(PARAMS_HISTORY):
        return []
    with open(PARAMS_HISTORY, "r", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]
