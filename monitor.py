#!/usr/bin/env python3
"""ひたちなか市ホームページ監視スクリプト v4"""

import json, os, urllib.request, xml.etree.ElementTree as ET, io, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import requests

RSS_URL      = "https://www.city.hitachinaka.lg.jp/news.rss"
BASE_URL     = "https://www.city.hitachinaka.lg.jp"
LINE_TOKEN   = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
STATE_FILE   = Path("seen.json")

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

    def get_text(self, max_chars=2000):
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
        p = SmartParser(); p.feed(res.text)
        return p.get_text(), [to_abs(l) for l in p.get_pdf_links()[:3]]
    except Exception as e:
        print(f"  ページ取得失敗: {e}"); return "", []


def fetch_pdf(pdf_url):
    try:
        from pdfminer.high_level import extract_text
        res = requests.get(pdf_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        text = re.sub(r"\s+", " ", extract_text(io.BytesIO(res.content))).strip()
        return text[:1000]
    except Exception as e:
        print(f"  PDF取得失敗: {e}"); return ""


def ai_summary(title, page_text, pdf_list):
    if not GROQ_API_KEY:
        return page_text[:400]

    parts = [f"タイトル: {title}"]
    if page_text: parts.append(f"ページ本文:\n{page_text[:1500]}")
    for i, (url, text) in enumerate(pdf_list, 1):
        parts.append(f"PDF{i}内容:\n{text}" if text else f"PDF{i}: {url}（取得不可）")

    user_prompt = f"""以下のひたちなか市公式情報をLINE通知用に要約してください。

【要件】
・何についての情報か冒頭に明示
・重要な数値・日程・金額・対象者を必ず含める
・市政・議会の観点から見た意義・影響を補足
・400文字以内、箇条書き

【情報】
{"　".join(parts)}"""

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role":"system","content":"あなたはひたちなか市の政治秘書のアシスタントです。"},
                    {"role":"user","content": user_prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.2
            },
            timeout=30
        )
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  Groq APIエラー: {e}"); return page_text[:400]


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

def process_important(item, label):
    print(f"  処理中: {item['title'][:50]}")
    page_text, pdf_links = fetch_page(item["link"])
    pdf_list = [(url, fetch_pdf(url)) for url in pdf_links]
    summary  = ai_summary(item["title"], page_text, pdf_list)
    send_line(f"{label}【概要】\n{item['title']}\n\n{summary}")
    send_line(f"{label}\n{item['title']}\n\n{item['link']}")

def main():
    if not LINE_TOKEN or not LINE_USER_ID:
        print("ERROR: LINE環境変数未設定"); return

    seen      = load_seen()
    items     = fetch_rss()
    new_items = [i for i in items if i["link"] not in seen]

    if not new_items:
        print(f"{datetime.now():%Y-%m-%d %H:%M} 新着なし"); return

    gikai     = [i for i in new_items if classify(i) == "gikai"]
    important = [i for i in new_items if classify(i) == "important"]
    minor_24h = [i for i in new_items if classify(i) == "minor"
                 and within_24h(i.get("pub_date",""))]

    for item in gikai:     process_important(item, "【議会情報】")
    for item in important: process_important(item, "【重要】")

    if minor_24h:
        lines = [f"【ひたちなか市 更新情報 {len(minor_24h)}件（24時間以内）】"]
        for i in minor_24h:
            lines.append(f"\n・{i['title']}\n  {i['link']}")
        send_line("\n".join(lines))

    for i in new_items: seen.add(i["link"])
    save_seen(seen)
    print(f"{datetime.now():%Y-%m-%d %H:%M} 完了 — 議会:{len(gikai)} 重要:{len(important)} 軽微:{len(minor_24h)}")

if __name__ == "__main__":
    main()
