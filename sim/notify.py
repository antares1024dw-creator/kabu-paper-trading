"""オーナーへの通知（Windows トースト／notifications.md／任意でメール）。

メールは Gmail の「アプリパスワード」を環境変数（既定: STOCKSIM_SMTP_PASSWORD）に
オーナー自身が設定した場合のみ有効になる。パスワードをファイルに書かない。
"""
import html
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

from .config import ROOT

DECISION_TYPES = {"BUY_SIGNAL", "SELL_SIGNAL", "FILLED_BUY", "FILLED_SELL", "CANCELLED", "REVIEW", "PARAM_CHANGE"}
LABELS = {
    "BUY_SIGNAL": "買い判断", "SELL_SIGNAL": "売り判断", "FILLED_BUY": "買付約定", "FILLED_SELL": "売却約定",
    "CANCELLED": "取消", "REVIEW": "反省ノート", "PARAM_CHANGE": "ルール変更", "INFO": "お知らせ",
}

_TOAST_PS = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast duration="long"><visual><binding template="ToastGeneric"><text>__TITLE__</text><text>__BODY__</text></binding></visual></toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''


def toast(title: str, body: str, log=print) -> bool:
    """Windows 10/11 のトースト通知。失敗しても例外にしない。クラウド実行時は何もしない。"""
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("STOCKSIM_NO_TOAST"):
        return False
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return False
    script = _TOAST_PS.replace("__TITLE__", html.escape(title)).replace("__BODY__", html.escape(body[:400]))
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"  トースト通知に失敗: {r.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        log(f"  トースト通知に失敗: {e}")
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def append_markdown(path: str, events: list, header: str) -> None:
    lines = [f"\n## {header}\n"]
    for ev in events:
        label = LABELS.get(ev["type"], ev["type"])
        text = ev.get("text") or ev.get("reason") or ""
        lines.append(f"- **[{label}]** {ev['date']} — {text}")
    with open(path, "a", encoding="utf-8") as f:
        if os.path.getsize(path) == 0 if os.path.exists(path) else True:
            f.write("# 通知ログ（売買判断・約定・反省）\n\nこのファイルは自動生成です。新しい通知が下に追記されます。\n")
        f.write("\n".join(lines) + "\n")


def resolve_email(cfg_email: dict) -> dict:
    """送信元・宛先は環境変数（GitHub Secrets 等）があればそちらを優先する。
    アドレスを config.json（公開される可能性があるファイル）に書かなくて済むようにするため。"""
    e = dict(cfg_email or {})
    env_from = os.environ.get("STOCKSIM_EMAIL_FROM")
    env_to = os.environ.get("STOCKSIM_EMAIL_TO")
    if env_from:
        e["from"] = env_from
    if env_to:
        e["to"] = env_to
    if env_from and env_to and os.environ.get(e.get("password_env", "STOCKSIM_SMTP_PASSWORD")):
        e["enabled"] = True
    return e


def send_email(cfg_email: dict, subject: str, body: str, log=print, html_body: str = None) -> bool:
    cfg_email = resolve_email(cfg_email)
    if not cfg_email.get("enabled"):
        return False
    pw = os.environ.get(cfg_email.get("password_env", "STOCKSIM_SMTP_PASSWORD"), "")
    if not (pw and cfg_email.get("from") and cfg_email.get("to")):
        log("  メール通知: 送信元/宛先/アプリパスワード(環境変数)が未設定のためスキップ")
        return False
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg_email["from"]
        msg["To"] = cfg_email["to"]
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(cfg_email.get("smtp_host", "smtp.gmail.com"), int(cfg_email.get("smtp_port", 587)), timeout=30) as s:
            s.starttls()
            s.login(cfg_email["from"], pw)
            s.send_message(msg)
        return True
    except Exception as e:
        log(f"  メール通知に失敗: {e}")
        return False


def send_ntfy(cfg_ntfy: dict, title: str, message: str, log=print) -> bool:
    """ntfy.sh（iPhone/Android のプッシュ通知アプリ）へ送る。topic を設定した場合のみ。"""
    if not (cfg_ntfy.get("enabled") and cfg_ntfy.get("topic")):
        return False
    import base64
    import urllib.request
    url = cfg_ntfy.get("server", "https://ntfy.sh").rstrip("/") + "/" + cfg_ntfy["topic"]
    enc_title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST",
                                 headers={"Title": enc_title, "Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log(f"  ntfy 通知に失敗: {e}")
        return False


def events_html(evs: list, summary_line: str) -> str:
    rows = "".join(
        f"<li style='margin:6px 0'><b>[{LABELS.get(e['type'], e['type'])}]</b> {html.escape(e.get('text') or e.get('reason', ''))}</li>"
        for e in evs)
    return (f"<div style='font-family:sans-serif;font-size:14px;line-height:1.6'><p><b>{html.escape(summary_line)}</b></p>"
            f"<ul style='padding-left:18px'>{rows}</ul><p style='color:#888;font-size:12px'>国内株ペーパートレード（実際の売買はしていません）。詳細は dashboard.html。</p></div>")


def notify_events(cfg: dict, events: list, summary_line: str, log=print) -> None:
    """新しいイベントをオーナーに通知する。"""
    evs = [e for e in events if e["type"] in DECISION_TYPES]
    ncfg = cfg.get("notify", {})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    md_path = os.path.join(ROOT, ncfg.get("markdown_file", "notifications.md"))
    if evs:
        append_markdown(md_path, evs, f"{stamp} の通知（{len(evs)}件）")
    if not evs:
        return
    if ncfg.get("windows_toast", True):
        decisions = [e for e in evs if e["type"] in ("BUY_SIGNAL", "SELL_SIGNAL", "FILLED_BUY", "FILLED_SELL")]
        if len(decisions) <= 4:
            for e in decisions:
                title = f"{LABELS[e['type']]}: {e.get('name', '')}({e.get('code', '')})"
                toast(title, (e.get("text") or "")[:300], log)
        else:
            counts = {}
            for e in decisions:
                counts[LABELS[e["type"]]] = counts.get(LABELS[e["type"]], 0) + 1
            toast("株式シミュレーション: 本日の判断", "・".join(f"{k} {v}件" for k, v in counts.items()) + f"\n{summary_line}", log)
        for e in evs:
            if e["type"] in ("REVIEW", "PARAM_CHANGE"):
                toast(LABELS[e["type"]], (e.get("text") or "")[:300], log)
    body = summary_line + "\n\n" + "\n".join(f"[{LABELS.get(e['type'], e['type'])}] {e.get('text') or e.get('reason', '')}" for e in evs)
    if resolve_email(ncfg.get("email", {})).get("enabled"):
        send_email(ncfg.get("email", {}), f"[株シミュレーション] {stamp} 判断 {len(evs)}件", body, log, html_body=events_html(evs, summary_line))
    ntfy_cfg = dict(ncfg.get("ntfy", {}))
    if os.environ.get("STOCKSIM_NTFY_TOPIC"):  # トピック名も秘密情報として環境変数から受け取れる
        ntfy_cfg.update({"enabled": True, "topic": os.environ["STOCKSIM_NTFY_TOPIC"]})
    if ntfy_cfg.get("enabled"):
        send_ntfy(ntfy_cfg, f"株シミュレーション 判断 {len(evs)}件", body[:1500], log)
