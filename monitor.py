#!/usr/bin/env python3
"""ひたちなか市ホームページ監視スクリプト"""

import json
import os
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# 設定
RSS_URL = "https://www.city.hitachinaka.lg.jp/news.rss"
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
STATE_FILE = Path("seen.json")

# 議会関連キーワード（最優先）
GIKAI_KEYWORDS = [
    "議会", "議員", "本会議", "委員会", "議案", "条例", "予算",
    "決算", "一般質問", "議長", "副議長", "常任委員会", "特別委員会",
    "補正予算", "当初予算"
]

# 重要キーワード（行政・事業・緊急系に絞る）
IMPORTANT_KEYWORDS = [
    "緊急", "警報", "注意報", "台風", "地震", "津波",
    "避難", "災害", "入札", "新規事業", "計画", "整備",
    "工事", "開発", "方針", "施策", "改正", "廃止"
]


def load_seen() -> set:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f, ensure_ascii=False, indent=2)


def fetch_rss() -> list[dict]:
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        content = res.read()
    root = ET.fromstring(content)
    channel = root.find("channel")
    items = []
    for item in channel.findall("item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        pub_date = item.findtext("pubDate", "").strip()
        description = item.findtext("description", "").strip()
        items.append({
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": description,
        })
    return items


def classify(item: dict) -> str:
    text = item["title"] + " " + item["description"]
    if any(kw in text for kw in GIKAI_KEYWORDS):
        return "gikai"
    if any(kw in text for kw in IMPORTANT_KEYWORDS):
        return "important"
    return "minor"


def send_line(message: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message}]
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.status


def main():
    if not LINE_TOKEN or not LINE_USER_ID:
        print("ERROR: 環境変数 LINE_TOKEN / LINE_USER_ID が未設定")
        return

    seen = load_seen()
    items = fetch_rss()

    new_items = [item for item in items if item["link"] not in seen]

    if not new_items:
        print(f"{datetime.now():%Y-%m-%d %H:%M} 新着なし")
        return

    gikai_items = [i for i in new_items if classify(i) == "gikai"]
    important_items = [i for i in new_items if classify(i) == "important"]
    minor_items = [i for i in new_items if classify(i) == "minor"]

    # 議会関連：1件ずつ通知
    for item in gikai_items:
        msg = f"【議会情報】\n{item['title']}\n\n{item['link']}"
        send_line(msg)
        print(f"議会: {item['title']}")

    # 重要：1件ずつ通知
    for item in important_items:
        msg = f"【重要なお知らせ】\n{item['title']}\n\n{item['link']}"
        send_line(msg)
        print(f"重要: {item['title']}")

    # 軽微：まとめて1通
    if minor_items:
        lines = [f"【ひたちなか市 更新情報 {len(minor_items)}件】"]
        for item in minor_items:
            lines.append(f"・{item['title']}")
        lines.append(f"\n詳細: https://www.city.hitachinaka.lg.jp/newslist.html")
        send_line("\n".join(lines))
        print(f"軽微: {len(minor_items)}件まとめて送信")

    # 既読として保存
    for item in new_items:
        seen.add(item["link"])
    save_seen(seen)

    print(f"{datetime.now():%Y-%m-%d %H:%M} 完了 - 議会:{len(gikai_items)} 重要:{len(important_items)} 軽微:{len(minor_items)}")


if __name__ == "__main__":
    main()
