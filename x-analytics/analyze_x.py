#!/usr/bin/env python3
import os, sys, csv, datetime, requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
LINE_TOKEN     = os.environ["LINE_TOKEN"]
LINE_USER_ID   = os.environ["LINE_USER_ID"]
NOTION_TOKEN   = os.environ.get("NOTION_TOKEN", "")
NOTION_PARENT  = "38655d762728-80de-a27c-e143404406e9"
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

CSV_PATH = "x-analytics/latest.csv"

# X Analytics の列名（日英両対応）
COL_MAP = {
    "Tweet text": "text", "ツイートのテキスト": "text",
    "time": "time", "時刻": "time",
    "impressions": "impressions", "インプレッション数": "impressions",
    "engagements": "engagements", "エンゲージメント数": "engagements",
    "engagement rate": "engagement_rate", "エンゲージメント率": "engagement_rate",
    "retweets": "retweets", "リツイート": "retweets",
    "replies": "replies", "返信": "replies",
    "likes": "likes", "いいね": "likes",
}

def read_csv(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalized = {COL_MAP.get(k.strip(), k.strip()): v.strip() for k, v in row.items()}
            rows.append(normalized)
    return rows

def to_float(s):
    try: return float(str(s).replace('%','').replace(',','').strip())
    except: return 0.0

def to_int(s):
    try: return int(str(s).replace(',','').strip())
    except: return 0

def analyze(rows):
    total_imp  = sum(to_int(r.get("impressions", 0)) for r in rows)
    total_like = sum(to_int(r.get("likes", 0)) for r in rows)
    total_rep  = sum(to_int(r.get("replies", 0)) for r in rows)
    total_rt   = sum(to_int(r.get("retweets", 0)) for r in rows)
    total_eng  = sum(to_int(r.get("engagements", 0)) for r in rows)
    avg_rate   = (total_eng / total_imp * 100) if total_imp > 0 else 0.0
    top3 = sorted(rows, key=lambda r: to_float(r.get("engagement_rate", 0)), reverse=True)[:3]
    return dict(count=len(rows), impressions=total_imp, likes=total_like,
                replies=total_rep, retweets=total_rt, eng_rate=avg_rate, top3=top3)

def gemini_analyze(m, top3_texts):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    top_str = "\n".join(f"- {t[:80]}" for t in top3_texts)
    prompt = f"""Xアカウント「レイン@rain_dokugaku」（行政書士試験独学×AI活用）の今週の数字です。

投稿数:{m['count']} / インプレ:{m['impressions']:,} / いいね:{m['likes']} / リプライ:{m['replies']} / 平均エンゲ率:{m['eng_rate']:.2f}%

トップ投稿:
{top_str}

今週の評価と来週へのアドバイスを1つ、合わせて100字以内で語り口調で。"""

    res = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}],
                                    "generationConfig":{"maxOutputTokens":200}}, timeout=30)
    res.raise_for_status()
    return res.json()["candidates"][0]["content"]["parts"][0]["text"]

def send_line(text):
    requests.post("https://api.line.me/v2/bot/message/push",
                  headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
                  json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
                  timeout=10).raise_for_status()

def create_notion_page(m, ai, top3):
    if not NOTION_TOKEN:
        return
    today = datetime.date.today().strftime("%Y/%m/%d")
    top3_text = "\n".join(
        f"#{i+1} エンゲ率{top3[i].get('engagement_rate','?')} | {top3[i].get('text','')[:50]}"
        for i in range(len(top3))
    )
    body = {
        "parent": {"page_id": "38655d76-2728-80de-a27c-e143404406e9"},
        "properties": {"title": [{"type":"text","text":{"content": f"📊 週次レポート {today}"}}]},
        "children": [
            {"type":"heading_2","heading_2":{"rich_text":[{"type":"text","text":{"content":"今週の指標"}}]}},
            {"type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content":
                f"投稿数：{m['count']}件\nインプレ：{m['impressions']:,}\nいいね：{m['likes']}\nリプライ：{m['replies']}\n平均エンゲ率：{m['eng_rate']:.2f}%"
            }}]}},
            {"type":"heading_2","heading_2":{"rich_text":[{"type":"text","text":{"content":"トップ3投稿"}}]}},
            {"type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content": top3_text}}]}},
            {"type":"heading_2","heading_2":{"rich_text":[{"type":"text","text":{"content":"AI分析"}}]}},
            {"type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content": ai}}]}},
        ]
    }
    res = requests.post("https://api.notion.com/v1/pages",
                        headers={"Authorization": f"Bearer {NOTION_TOKEN}",
                                 "Notion-Version": "2022-06-28",
                                 "Content-Type": "application/json"},
                        json=body, timeout=15)
    res.raise_for_status()
    print(f"Notion page: {res.json().get('url','')}")

def save_to_supabase(m, ai, top3):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    today = datetime.date.today().strftime("%Y/%m/%d")
    top3_text = "\n".join(
        f"#{i+1} エンゲ率{top3[i].get('engagement_rate','?')} | {top3[i].get('text','')[:50]}"
        for i in range(len(top3))
    )
    content = (
        f"📊 週次レポート {today}\n\n"
        f"投稿数：{m['count']}件\n"
        f"インプレ：{m['impressions']:,}\n"
        f"いいね：{m['likes']}\n"
        f"リプライ：{m['replies']}\n"
        f"平均エンゲ率：{m['eng_rate']:.2f}%\n\n"
        f"【トップ3】\n{top3_text}\n\n"
        f"【AI分析】\n{ai}"
    )
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/x_reports",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"content": content, "posted_at": datetime.datetime.utcnow().isoformat()},
        timeout=10,
    )
    res.raise_for_status()
    print("Supabase書き込み完了")

def main():
    rows = read_csv(CSV_PATH)
    print(f"CSV読み込み: {len(rows)}件")
    m = analyze(rows)
    top3_texts = [r.get("text","") for r in m["top3"]]
    ai = gemini_analyze(m, top3_texts)

    today = datetime.date.today().strftime("%m/%d")
    line_msg = (
        f"📊 レイン週次レポート {today}\n\n"
        f"投稿数：{m['count']}件\n"
        f"インプレ：{m['impressions']:,}\n"
        f"いいね：{m['likes']}\n"
        f"リプライ：{m['replies']}\n"
        f"平均エンゲ率：{m['eng_rate']:.2f}%\n\n"
        f"【トップ投稿】\n{top3_texts[0][:60]}\n\n"
        f"【AI分析】\n{ai}"
    )
    send_line(line_msg)
    print("LINE送信完了")
    create_notion_page(m, ai, m["top3"])
    save_to_supabase(m, ai, m["top3"])
    print("完了")

if __name__ == "__main__":
    main()
