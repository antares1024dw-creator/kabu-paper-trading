# 株式運用シミュレーション（紙上運用ボード）

国内株を**実在の株価データ**で毎営業日「買う／売る」を判断し、**実際のお金は使わずに**運用したものとして記録・通知・振り返りを行うアプリです。
楽天証券の手数料体系（国内株ゼロコース＝手数料 0 円、かぶミニ®の寄付取引＝1 株単位・手数料 0 円）を想定しています。
証券口座には接続せず、注文も出しません。実際の投資との違いは「お金を使わない」ことだけです。

- 初期資産: **1,000,000 円**（`config.json` の `initial_cash`）。2026-09-24 に仮想の追加入金 500,000 円（`data/cashflows.json`）
- 資産の構成: **コア**＝TOPIX 連動 ETF(1306) の買い持ち 50 万円、**サテライト**＝下のルールで運用する個別株 100 万円
- 対象: 東証プライム主要 120 銘柄（`sim/universe.py`）＋ベンチマーク（TOPIX 連動 ETF 1306・日経 225 連動 ETF 1321）
- 判断: 毎営業日の大引け後（終値ベース）。約定は翌営業日の寄付値（先読みなし）
- 通知: Windows 通知（トースト）＋ `notifications.md` への追記（任意でメール）
- 可視化: `dashboard.html`（ダブルクリックで開く。資産推移・保有・判断ログ・取引・反省ノート・検証）
- 反省: 毎週金曜と月末に「反省ノート」を自動作成し、根拠が揃ったときだけルールを許容範囲内で自動調整

## 1. 仕組み（毎営業日 16:05 の処理）

```
価格更新(Yahoo Finance) → 異常値補正 → 未約定注文を当日寄付で約定 → 保有銘柄の損切りラインを更新
→ 売買判断(終値) → 資産を記録 → (金曜/月末) 反省ノート → 通知 → dashboard.html 生成
```

売買ルール（`trend_momentum_v1`、詳細は `sim/strategy.py`）:

| 項目 | ルール |
|---|---|
| 相場環境 | TOPIX 連動 ETF が 200 日線より上のときだけ新規買い（下なら現金で待つ） |
| 銘柄選定 | 終値 > 50 日線 > 200 日線、12-1 ヶ月モメンタム上位、20 日高値圏、20 日平均売買代金 5 億円以上 |
| 資金管理 | 1 トレードの想定損失 = 資産の 1%（損切り幅 = 3×ATR）、1 銘柄上限 12%、最大 10 銘柄、同一業種は最大 4 銘柄、レバレッジなし |
| 手仕舞い | トレーリングストップ（最高値 − 3×ATR、切り上げのみ）／200 日線割れ／モメンタム失速 |
| 執行 | 判断は終値、約定は翌営業日寄付、手数料 0 円、スリッページ 0.05%、税金は未考慮 |
| 資産の構成 | コア（指数 ETF の買い持ち）は売買ルール・枠数・業種上限・損切りの対象外。資金管理はサテライトの資産額で計算し、両者のリバランスはしない |
| 入金 | `data/cashflows.json` に日付・金額・充て先を書くと、その日の寄付前に入金として反映する。成績は入金の影響を除いた時間加重リターンで計算（`data/nav.csv` の `flow` 列） |
| 配当・分割 | 権利落ち日に「株数×配当」を現金へ計上し、損切りラインを配当落ちぶん引き下げる。分割は株数・単価を換算。Yahoo への反映が遅い分は `data/expected_actions.json` の見込みで計上し、確定後に差額を精算 |

## 2. 使い方

### クラウドで自動実行（推奨・PC の電源不要）

`.github/workflows/daily.yml` により、GitHub Actions が平日 16:13（日本時間）に日次処理を実行し、記録をコミットし、
`dashboard.html` を GitHub Pages に公開します（検索エンジンには載せない noindex 付き）。

- 通知をメールで受け取る場合は、リポジトリの Settings → Secrets and variables → Actions に
  `STOCKSIM_EMAIL_FROM`（Gmail アドレス）、`STOCKSIM_EMAIL_TO`（宛先）、`STOCKSIM_SMTP_PASSWORD`（Gmail のアプリパスワード）を登録します。
- iPhone のプッシュ通知（ntfy）を使う場合は `STOCKSIM_NTFY_TOPIC` を登録します。
- 秘密情報はすべて Secrets に置き、ファイルには書きません。公開されるファイルに個人情報は含まれません。
- 生の株価データ（`data/prices/`）はリポジトリに含めず、毎回取得し直します。

### PC で手動実行する場合

Python 3.12 を入れ、フォルダで次を実行します（Windows の例）。

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py -m pip install -r requirements.txt      # 初回のみ
& $py run.py step            # 日次処理（手動実行）
& $py run.py status          # 現在の資産・保有・未約定を表示
& $py run.py report          # dashboard.html だけ再生成
& $py run.py review          # 反省ノートを今すぐ作る
& $py run.py backtest --start 2024-09-02   # 現在のルールで過去を検証（data/backtest/）
& $py tools\param_scan.py    # ルールの候補を比較（学習用）
& $py run.py notify-test     # 通知の動作確認
& $py run.py reset --yes     # 運用状態を初期化（記録は data/archive/ に退避）
```

### PC のタスクスケジューラで自動実行する場合（クラウド実行と併用しない）

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
```

タスクスケジューラに「株シミュレーション日次」が登録されます。PC がオフだった日は次回起動時に実行され、未処理の営業日をまとめて処理します。
解除は `Unregister-ScheduledTask -TaskName "株シミュレーション日次" -Confirm:$false`。
クラウド実行と両方を動かすと記録が食い違うので、どちらか一方にしてください。

### 通知と iPhone での確認

- **claude.ai のアーティファクト「紙上運用ボード」**: ダッシュボードを claude.ai 上に公開したもの。iPhone の Claude アプリ／ブラウザから閲覧できます。
  更新は Claude のセッション（手動、または Claude デスクトップアプリのスケジュールタスク）で `dashboard.html` を再公開して行います。
- **Windows 通知**: 買い判断／売り判断／約定／反省ノート作成時にトーストを表示（`config.json` → `notify.windows_toast`）
- **notifications.md**: すべての判断が理由つきで追記されます（OneDrive 経由でスマホからも読めます）
- **メール（任意・iPhone 向け）**: Gmail で受け取る場合は `config.json` の `notify.email` に `enabled: true`、`from`／`to` を設定し、
  Google アカウントで発行した**アプリパスワード**を環境変数 `STOCKSIM_SMTP_PASSWORD` に **オーナー自身が** 設定してください
  （パスワードはファイルに書かない／AI に渡さない）。判断の一覧が HTML メールで届きます。
- **プッシュ通知（任意）**: iPhone に「ntfy」アプリを入れ、推測されにくいトピック名を決めて `notify.ntfy` に `enabled: true`／`topic` を設定すると、
  判断のたびにプッシュ通知が届きます（ntfy.sh は公開サービスなのでトピック名は長くランダムに）。

## 3. ファイル

```
run.py                日次処理などの CLI
config.json           初期資産・ルール・通知設定（自動調整で書き換わることがある）
dashboard.html        ダッシュボード（自動生成）
notifications.md      通知ログ（自動追記）
sim/                  エンジン（market: 価格取得, strategy: ルール, engine: 約定・台帳,
                      metrics: 指標, review: 反省ノート, notify: 通知, report: ダッシュボード）
data/state.json       現金・保有・未約定注文
data/trades.csv       約定履歴      data/nav.csv 日次資産推移      data/events.jsonl 判断ログ
data/journal/         反省ノート    data/params_history.jsonl ルール変更履歴
data/cashflows.json   入金の指示    data/expected_actions.json 配当・分割の見込み
data/backtest/        検証結果      data/prices/ 価格キャッシュ
knowledge/            学習ログ（投資家としての学び・検証結果の記録）
logs/                 実行ログ
```

## 4. 法令・データ・免責

- これは**シミュレーション**です。証券口座に接続せず、注文も出しません。金銭の移動は一切ありません。
- 株価は Yahoo Finance（yfinance ライブラリ経由、15〜20 分遅延、分割・配当調整済み）を**個人の非商用利用**として取得しています。データの再配布はしません。
- 特定銘柄の売買を勧める**投資助言ではありません**。オーナーが実際に投資する場合は、自身の判断と責任で行ってください。
- 楽天証券のサイトやアカウントには一切アクセスしません。手数料体系のみ参考にしています。
