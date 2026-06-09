#!/usr/bin/env python3
"""ひたちなか市ホームページ監視スクリプト v5 (GitHub Pages版)"""

import json, os, urllib.request, xml.etree.ElementTree as ET, io, re, hashlib, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import requests

RSS_URL         = "https://www.city.hitachinaka.lg.jp/news.rss"
BASE_URL        = "https://www.city.hitachinaka.lg.jp"
IBARAKI_RSS_URL = "https://ibarakinews.jp/news/hphead.rss"
PAGES_URL       = "https://oz629mutsu-lab.github.io/hitachinaka-monitor/"
LINE_TOKEN      = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID    = os.environ.get("LINE_USER_ID", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
STATE_FILE      = Path("seen.json")
HTML_FILE       = Path("docs/index.html")
ARCHIVE_DIR     = Path("docs/archive")

HITACHINAKA_KEYWORDS = ["ひたちなか", "那珂湊", "勝田", "常陸那珂"]

GIKAI_KEYWORDS = [
    "議会","議員","本会議","委員会","議案","条例","予算","決算",
    "一般質問","議長","副議長","常任委員会","特別委員会","補正予算","当初予算"
]
IMPORTANT_KEYWORDS = [
    "緊急","警報","注意報","台風","地震","津波","避難","災害",
    "入札","新規事業","計画","整備","工事","開発","方針","施策","改正","廃止"
]


class SmartParser(HTMLParser):
    SKIP_TAGS  = {"script","style","noscript","head"}
    SKIP_WORDS = {"nav","menu","header","footer","sidebar","breadcrumb",
                  "gnav","snav","pagetop","global","local","utility","tool"}

    def __init__(self):
        super().__init__()
        self._lines, self._pdf_links, self._skip = [], [], 0

    def _is_skip(self, attrs):
        d = dict(attrs)
        s = (d.get("class","") + " " + d.get("id","")).lower()
        return any(w in s for w in self.SKIP_WORDS)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS or self._is_skip(attrs): self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href","")
            if href.lower().endswith(".pdf"): self._pdf_links.append(href)

    def handle_endtag(self, tag):
        if self._skip > 0: self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            t = re.sub(r"\s+", " ", data).strip()
            if len(t) > 5: self._lines.append(t)

    def get_text(self, max_chars=4000):
        seen, unique = set(), []
        for l in self._lines:
            if l not in seen:
                seen.add(l); unique.append(l)
        return "\n".join(unique)[:max_chars]

    def get_pdf_links(self): return self._pdf_links


def to_abs(href):
    if href.startswith("http"): return href
    return BASE_URL + (href if href.startswith("/") else "/" + href)


def fetch_page(url):
    try:
        res = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        res.encoding = res.apparent_encoding or "utf-8"
        p = SmartParser(); p.feed(res.text)
        return p.get_text(), [to_abs(l) for l in p.get_pdf_links()[:3]]
    except Exception as e:
        print(f"  ページ取得失敗: {e}"); return "", []


def fetch_pdf(pdf_url):
    try:
        from pdfminer.high_level import extract_text
        res = requests.get(pdf_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        text = re.sub(r"\s+", " ", extract_text(io.BytesIO(res.content))).strip()
        return text[:2000]
    except Exception as e:
        print(f"  PDF取得失敗: {e}"); return ""


def ai_summary(title, page_text, pdf_list):
    if not GROQ_API_KEY:
        return page_text[:400]

    parts = [f"タイトル: {title}"]
    if page_text: parts.append(f"ページ本文:\n{page_text[:2000]}")
    for i, (url, text) in enumerate(pdf_list, 1):
        parts.append(f"PDF{i}内容:\n{text}" if text else f"PDF{i}: {url}（取得不可）")

    user_prompt = f"""以下のひたちなか市公式情報をウェブサイト掲載用に詳しく整理してください。

【絶対厳守ルール】
- ※は一切使わない
- 箇条書きは「・」のみ使用
- 情報を省略・要約しすぎない。元の情報に含まれる内容は漏らさず記載する
- 施設名・団体名・人名・地名などの固有名詞は必ず正確に記載する
- 対象者（年齢・資格・地域など）を具体的に記載する
- 日程・期間・締切は「令和○年○月○日」のまま省略せず記載する
- 金額・数量・定員などの数値は必ず含める
- 申込方法・問い合わせ先（電話番号・担当課）があれば記載する
- 行政用語は平易な言葉に言い換える
- 800文字以内

【構成】
1行目：何についての情報か（一文）
空行
・詳細ポイントを5〜8項目（省略なし）

【情報】
{"　".join(parts)}"""

    for attempt in range(5):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role":"system","content":"あなたはひたちなか市議会議員秘書のアシスタントです。市政情報を正確かつ詳しく、固有名詞や数値を省略せずに伝えることが仕事です。"},
                        {"role":"user","content": user_prompt}
                    ],
                    "max_tokens": 900,
                    "temperature": 0.1
                },
                timeout=40
            )
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            err = data.get("error", {}).get("message", str(data))
            # レート制限エラーから待機秒数を取得して待つ
            wait = re.search(r"try again in ([\d.]+)s", err)
            wait_sec = float(wait.group(1)) + 3 if wait else 30
            print(f"  Groq待機(試行{attempt+1}): {wait_sec:.0f}秒")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"  Groq例外(試行{attempt+1}): {e}")
            time.sleep(15)
    return "（AI要約取得失敗 — 詳細は元サイトをご確認ください）"


def load_seen():
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()

def save_seen(seen):
    STATE_FILE.write_text(json.dumps(list(seen), ensure_ascii=False, indent=2))

def fetch_rss():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        root = ET.fromstring(res.read())
    return [{"title":    i.findtext("title","").strip(),
             "link":     i.findtext("link","").strip(),
             "pub_date": i.findtext("pubDate","").strip()}
            for i in root.find("channel").findall("item")]

def classify(item):
    t = item["title"]
    if any(kw in t for kw in GIKAI_KEYWORDS):    return "gikai"
    if any(kw in t for kw in IMPORTANT_KEYWORDS): return "important"
    return "minor"

def within_24h(pub_date_str):
    try:
        return (datetime.now(timezone.utc) - parsedate_to_datetime(pub_date_str)) <= timedelta(hours=24)
    except: return True

def send_line(message):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type":"text","text":message[:4500]}]},
        timeout=30
    )


# ===== 茨城新聞 =====

def fetch_ibaraki_rss():
    req = urllib.request.Request(IBARAKI_RSS_URL, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        root = ET.fromstring(res.read())
    items = []
    for i in root.find("channel").findall("item"):
        items.append({
            "title":    i.findtext("title","").strip(),
            "link":     i.findtext("link","").strip(),
            "pub_date": i.findtext("pubDate","").strip(),
            "guid":     i.findtext("guid","").strip(),
        })
    return items

def fetch_og_description(url):
    try:
        res = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        res.encoding = res.apparent_encoding or "utf-8"
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', res.text)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', res.text)
        return m.group(1).strip() if m else ""
    except Exception as e:
        print(f"  og:description取得失敗: {e}"); return ""

def ai_summary_ibaraki(title, description):
    if not GROQ_API_KEY or not description:
        return description or "（本文取得不可）"
    prompt = f"""以下の茨城新聞記事をひたちなか市議会議員候補者の視点で簡潔に整理してください。

【厳守ルール】
- ※は一切使わない / 箇条書きは「・」のみ
- 固有名詞・地名・人名・数値は省略せず記載
- 茨城県政・地域行政への影響があれば補足
- 300文字以内

【構成】
1行目：何についての記事か（一文）
空行
・重要ポイント2〜4項目

タイトル: {title}
記事冒頭: {description[:600]}"""

    for attempt in range(5):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model":"llama-3.3-70b-versatile",
                      "messages":[{"role":"system","content":"茨城新聞記事を地方議員候補者向けに要約するアシスタントです。"},
                                   {"role":"user","content":prompt}],
                      "max_tokens":600, "temperature":0.1},
                timeout=40
            )
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            err = data.get("error",{}).get("message", str(data))
            wait = re.search(r"try again in ([\d.]+)s", err)
            wait_sec = float(wait.group(1)) + 3 if wait else 30
            print(f"  Groq待機(試行{attempt+1}): {wait_sec:.0f}秒")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"  Groq例外(試行{attempt+1}): {e}"); time.sleep(15)
    return description[:300]

def ai_digest_ibaraki(articles):
    if not GROQ_API_KEY or not articles:
        return ""
    lines = "\n".join([f"・{a['title']}" for a in articles])
    prompt = f"""以下の茨城新聞の本日のニュース見出し一覧を、地方議員候補者の視点で3〜5行にまとめてください。
県政・社会・経済の動向として押さえておくべきポイントを中心に。

【見出し一覧】
{lines}

【ルール】箇条書きは「・」のみ / ※不使用 / 300文字以内"""

    for attempt in range(5):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model":"llama-3.3-70b-versatile",
                      "messages":[{"role":"system","content":"茨城新聞のニュースダイジェストを作成するアシスタントです。"},
                                   {"role":"user","content":prompt}],
                      "max_tokens":500, "temperature":0.1},
                timeout=40
            )
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            err = data.get("error",{}).get("message", str(data))
            wait = re.search(r"try again in ([\d.]+)s", err)
            wait_sec = float(wait.group(1)) + 3 if wait else 30
            print(f"  Groqダイジェスト待機(試行{attempt+1}): {wait_sec:.0f}秒")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"  Groqダイジェスト例外(試行{attempt+1}): {e}"); time.sleep(15)
    return ""


# ===== HTML生成 =====

def esc(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

def summary_to_html(text):
    lines = text.strip().split("\n")
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("・"):
            html_parts.append(f'<li>{esc(line[1:].strip())}</li>')
        else:
            html_parts.append(f'<p>{esc(line)}</p>')
    result = []
    in_ul = False
    for part in html_parts:
        if part.startswith("<li>"):
            if not in_ul:
                result.append("<ul>"); in_ul = True
            result.append(part)
        else:
            if in_ul:
                result.append("</ul>"); in_ul = False
            result.append(part)
    if in_ul: result.append("</ul>")
    return "\n".join(result)


def build_html(gikai_cards, important_cards, minor_items, generated_at,
               ibaraki_local_cards=None, ibaraki_digest="", ibaraki_all=None):
    jst = generated_at + timedelta(hours=9)
    date_str = jst.strftime("%Y年%m月%d日 %H:%M")

    def cards_html(cards, color, label):
        if not cards: return f'<p class="empty">本日の{label}はありません</p>'
        html = ""
        for c in cards:
            html += f"""
<div class="card" style="border-left:4px solid {color}">
  <div class="card-title"><a href="{esc(c['link'])}" target="_blank">{esc(c['title'])}</a></div>
  <div class="card-summary">{c['summary_html']}</div>
  <div class="card-meta">
    <span class="tag" style="background:{color}">{label}</span>
    <a href="{esc(c['link'])}" target="_blank" class="src-link">公式ページを見る →</a>
  </div>
</div>"""
        return html

    def minor_html():
        if not minor_items: return '<p class="empty">本日の更新情報はありません</p>'
        rows = ""
        for i in minor_items:
            rows += f'<tr><td><a href="{esc(i["link"])}" target="_blank">{esc(i["title"])}</a></td></tr>\n'
        return f"<table><tbody>{rows}</tbody></table>"

    def ibaraki_digest_html(digest, all_articles):
        if not all_articles: return '<p class="empty">本日の茨城新聞ニュースはありません</p>'
        digest_block = ""
        if digest:
            digest_block = f'<div class="card" style="border-left:4px solid #33691E"><div class="card-summary">{summary_to_html(digest)}</div></div>'
        rows = "".join(
            f'<tr><td><a href="{esc(a["link"])}" target="_blank">{esc(a["title"])}</a></td></tr>\n'
            for a in all_articles
        )
        return digest_block + f'<table><tbody>{rows}</tbody></table>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ひたちなか市 最新情報 | {date_str}</title>
<style>
:root{{--blue:#1565C0;--red:#C62828;--amber:#E65100;--gray:#546E7A;--bg:#F8F9FA}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;background:var(--bg);color:#212121;line-height:1.7;font-size:15px}}
header{{background:var(--blue);color:#fff;padding:16px 20px}}
header h1{{font-size:18px;font-weight:700}}
header .updated{{font-size:12px;opacity:.85;margin-top:4px}}
.container{{max-width:860px;margin:0 auto;padding:16px}}
h2{{font-size:16px;font-weight:700;padding:10px 0 8px;border-bottom:2px solid currentColor;margin:24px 0 12px}}
h2.gikai{{color:#C62828}} h2.important{{color:#E65100}} h2.minor{{color:#546E7A}} h2.ibaraki{{color:#1B5E20}} h2.ibaraki-all{{color:#33691E}}
.card{{background:#fff;border-radius:8px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.card-title{{font-weight:700;font-size:15px;margin-bottom:8px}}
.card-title a{{color:#1565C0;text-decoration:none}}
.card-title a:hover{{text-decoration:underline}}
.card-summary p{{margin-bottom:6px;font-size:14px}}
.card-summary ul{{padding-left:1.2em;font-size:14px}}
.card-summary li{{margin-bottom:4px}}
.card-meta{{display:flex;align-items:center;gap:12px;margin-top:10px}}
.tag{{color:#fff;font-size:11px;padding:2px 8px;border-radius:12px}}
.src-link{{font-size:13px;color:#1565C0}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
td{{padding:10px 14px;border-bottom:1px solid #eee;font-size:14px}}
td a{{color:#1565C0;text-decoration:none}}
td a:hover{{text-decoration:underline}}
.empty{{color:#888;font-size:14px;padding:8px 0}}
footer{{text-align:center;font-size:12px;color:#888;padding:24px;margin-top:16px}}
</style>
</head>
<body>
<header>
  <h1>ひたちなか市・茨城県 最新情報</h1>
  <div class="updated">最終更新: {date_str} JST | <a href="https://www.city.hitachinaka.lg.jp/" style="color:#fff" target="_blank">ひたちなか市公式</a> | <a href="https://ibarakinews.jp/" style="color:#fff" target="_blank">茨城新聞</a></div>
</header>
<div class="container">

<h2 class="gikai">🔴 議会情報</h2>
{cards_html(gikai_cards, "#C62828", "議会")}

<h2 class="important">🟡 重要なお知らせ</h2>
{cards_html(important_cards, "#E65100", "重要")}

<h2 class="minor">⚪ その他の更新情報（24時間以内）</h2>
{minor_html()}

<h2 class="ibaraki">🗞️ 茨城新聞 ひたちなか関連</h2>
{cards_html(ibaraki_local_cards or [], "#1B5E20", "茨城新聞")}

<h2 class="ibaraki-all">📰 茨城新聞 本日のニュース</h2>
{ibaraki_digest_html(ibaraki_digest, ibaraki_all or [])}

</div>
<footer>自動生成 | oz629mutsu-lab / hitachinaka-monitor</footer>
</body>
</html>"""


_item_count = 0

def process_item(item, label):
    global _item_count
    if _item_count > 0:
        time.sleep(5)
    _item_count += 1
    print(f"  処理中: {item['title'][:50]}")
    page_text, pdf_links = fetch_page(item["link"])
    pdf_list = [(url, fetch_pdf(url)) for url in pdf_links]
    summary  = ai_summary(item["title"], page_text, pdf_list)
    return {
        "title":       item["title"],
        "link":        item["link"],
        "summary":     summary,
        "summary_html": summary_to_html(summary),
        "label":       label
    }


def main():
    if not LINE_TOKEN or not LINE_USER_ID:
        print("ERROR: LINE環境変数未設定"); return

    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)

    seen  = load_seen()
    now   = datetime.now(timezone.utc)

    # ===== ひたちなか市公式サイト =====
    items     = fetch_rss()
    new_items = [i for i in items if i["link"] not in seen]

    gikai     = [i for i in new_items if classify(i) == "gikai"]
    important = [i for i in new_items if classify(i) == "important"]
    minor_24h = [i for i in new_items if classify(i) == "minor" and within_24h(i.get("pub_date",""))]

    gikai_cards     = [process_item(i, "議会") for i in gikai]
    important_cards = [process_item(i, "重要") for i in important]

    # ===== 茨城新聞 =====
    print("茨城新聞を取得中...")
    ib_items     = fetch_ibaraki_rss()
    ib_new       = [i for i in ib_items if i["link"] not in seen and within_24h(i.get("pub_date",""))]
    ib_local     = [i for i in ib_new if any(kw in i["title"] for kw in HITACHINAKA_KEYWORDS)]
    ib_other     = [i for i in ib_new if i not in ib_local]

    # ひたちなか関連：og:description取得 + 個別AI要約
    ib_local_cards = []
    for item in ib_local:
        time.sleep(5)
        print(f"  茨城新聞(ひたちなか): {item['title'][:50]}")
        desc    = fetch_og_description(item["link"])
        summary = ai_summary_ibaraki(item["title"], desc)
        ib_local_cards.append({
            "title": item["title"], "link": item["link"],
            "summary": summary, "summary_html": summary_to_html(summary), "label": "茨城新聞"
        })

    # 全体ニュース：見出し一覧をまとめてAI要約（1回）
    ib_digest = ""
    if ib_other:
        time.sleep(5)
        print(f"  茨城新聞ダイジェスト: {len(ib_other)}件")
        ib_digest = ai_digest_ibaraki(ib_other)

    # HTMLを生成（更新なしでも茨城新聞があれば生成）
    has_updates = bool(gikai or important or minor_24h or ib_local or ib_other)
    if not has_updates:
        print(f"{now:%Y-%m-%d %H:%M} 新着なし"); return

    html = build_html(gikai_cards, important_cards, minor_24h, now,
                      ib_local_cards, ib_digest, ib_other)
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"✓ {HTML_FILE} 生成完了")

    # 既読登録
    for i in new_items: seen.add(i["link"])
    for i in ib_new:    seen.add(i["link"])
    save_seen(seen)

    # LINE通知
    counts = []
    if gikai:         counts.append(f"議会{len(gikai)}件")
    if important:     counts.append(f"重要{len(important)}件")
    if minor_24h:     counts.append(f"市その他{len(minor_24h)}件")
    if ib_local:      counts.append(f"茨城新聞(ひたちなか){len(ib_local)}件")
    if ib_other:      counts.append(f"茨城新聞{len(ib_other)}件")
    summary_line = "・".join(counts)

    # 8:05 JST まで待機
    jst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    target  = jst_now.replace(hour=8, minute=5, second=0, microsecond=0)
    if jst_now < target:
        wait_sec = (target - jst_now).total_seconds()
        print(f"  8:05 JST まで {wait_sec:.0f}秒 待機中...")
        time.sleep(wait_sec)

    send_time = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d %H:%M")
    msg = f"【ひたちなか市・茨城新聞 更新情報】{send_time}\n{summary_line}\n\n{PAGES_URL}"
    send_line(msg)
    print(f"✓ LINE送信: {msg}")
    print(f"{now:%Y-%m-%d %H:%M} 完了 — 議会:{len(gikai)} 重要:{len(important)} 軽微:{len(minor_24h)} 茨城新聞:{len(ib_new)}")


if __name__ == "__main__":
    main()
