#!/usr/bin/env python3
import json, os, urllib.request, xml.etree.ElementTree as ET, io, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime

RSS_URL = "https://www.city.hitachinaka.lg.jp/news.rss"
BASE_URL = "https://www.city.hitachinaka.lg.jp"
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
STATE_FILE = Path("seen.json")

GIKAI_KEYWORDS = [
    "議会","議員","本会議","委員会","議案","条例","予算",
    "決算","一般質問","議長","副議長","常任委員会","特別委員会","補正予算","当初予算"
]
IMPORTANT_KEYWORDS = [
    "緊急","警報","注意報","台風","地震","津波","避難","災害",
    "入札","新規事業","計画","整備","工事","開発","方針","施策","改正","廃止"
]


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text, self._pdf_links = [], []
        self._skip = False
        self._skip_tags = {"script","style","nav","header","footer","noscript"}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in self._skip_tags:
            self._skip = True
        if tag == "a":
            href = d.get("href","")
            if href.lower().endswith(".pdf"):
                self._pdf_links.append(href)

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if len(t) > 4:
                self._text.append(t)

    def get_text(self, max_chars=1200):
        return "\n".join(self._text)[:max_chars]

    def get_pdf_links(self):
        return self._pdf_links


def abs_url(href):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return BASE_URL + "/" + href


def fetch_page(url):
    """HTMLページのテキストとPDFリンクを取得"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            html = res.read().decode("utf-8", errors="ignore")
        p = PageParser()
        p.feed(html)
        return p.get_text(), [abs_url(l) for l in p.get_pdf_links()[:3]]
    except Exception:
        return "", []


def fetch_pdf_text(pdf_url):
    """PDFのテキストを抽出"""
    try:
        from pdfminer.high_level import extract_text
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as res:
            data = res.read()
        text = extract_text(io.BytesIO(data)).strip()
        # 空白・改行を整理
        text = re.sub(r"\s+", " ", text)
        return text[:400]
    except Exception:
        return ""


def build_summary(item):
    """ページ・PDFを取得して概要メッセージを作成"""
    page_text, pdf_links = fetch_page(item["link"])
    parts = []
    if page_text:
        parts.append(page_text)
    if pdf_links:
        parts.append(f"\n【関連PDF {len(pdf_links)}件】")
        for url in pdf_links:
            pdf_text = fetch_pdf_text(url)
            if pdf_text:
                parts.append(f"{url}\n▶ {pdf_text}")
            else:
                parts.append(url)
    return "\n".join(parts)[:2000] if parts else "（本文を取得できませんでした）"


def load_seen():
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(list(seen), ensure_ascii=False, indent=2))


def fetch_rss():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        root = ET.fromstring(res.read())
    items = []
    for i in root.find("channel").findall("item"):
        items.append({
            "title":    i.findtext("title","").strip(),
            "link":     i.findtext("link","").strip(),
            "pub_date": i.findtext("pubDate","").strip(),
        })
    return items


def classify(item):
    text = item["title"]
    if any(kw in text for kw in GIKAI_KEYWORDS): return "gikai"
    if any(kw in text for kw in IMPORTANT_KEYWORDS): return "important"
    return "minor"


def is_within_24h(pub_date_str):
    """pubDateが24時間以内かどうか"""
    try:
        pub_dt = parsedate_to_datetime(pub_date_str)
        now = datetime.now(timezone.utc)
        return (now - pub_dt) <= timedelta(hours=24)
    except Exception:
        return True  # パース失敗時は含める


def send_line(message):
    payload = json.dumps({
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message}]
    }).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"}
    )
    urllib.request.urlopen(req, timeout=30)


def main():
    if not LINE_TOKEN or not LINE_USER_ID:
        print("ERROR: 環境変数未設定")
        return

    seen = load_seen()
    items = fetch_rss()
    new_items = [i for i in items if i["link"] not in seen]

    if not new_items:
        print(f"{datetime.now():%Y-%m-%d %H:%M} 新着なし")
        return

    gikai = [i for i in new_items if classify(i) == "gikai"]
    important = [i for i in new_items if classify(i) == "important"]
    minor_24h = [i for i in new_items
                 if classify(i) == "minor" and is_within_24h(i.get("pub_date",""))]

    # 議会関連：概要 → 通知 の順に送信
    for item in gikai:
        summary = build_summary(item)
        send_line(f"【議会情報 概要】\n{item['title']}\n\n{summary}")
        send_line(f"【議会情報】\n{item['title']}\n\n{item['link']}")
        print(f"議会: {item['title']}")

    # 重要：概要 → 通知 の順に送信
    for item in important:
        summary = build_summary(item)
        send_line(f"【重要 概要】\n{item['title']}\n\n{summary}")
        send_line(f"【重要なお知らせ】\n{item['title']}\n\n{item['link']}")
        print(f"重要: {item['title']}")

    # 軽微（24時間以内）：URLつきでまとめて1通
    if minor_24h:
        lines = [f"【ひたちなか市 更新情報 {len(minor_24h)}件（24時間以内）】"]
        for item in minor_24h:
            lines.append(f"\n・{item['title']}\n  {item['link']}")
        send_line("\n".join(lines)[:4500])
        print(f"軽微: {len(minor_24h)}件")

    for item in new_items:
        seen.add(item["link"])
    save_seen(seen)

    total = len(gikai) + len(important) + len(minor_24h)
    print(f"{datetime.now():%Y-%m-%d %H:%M} 完了 議会:{len(gikai)} 重要:{len(important)} 軽微:{len(minor_24h)}")


if __name__ == "__main__":
    main()
