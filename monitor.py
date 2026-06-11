#!/usr/bin/env python3
"""ひたちなか市ホームページ監視スクリプト v9.8 (農林水産省・NHK政治追加)"""

import json, os, urllib.request, xml.etree.ElementTree as ET, io, re, time
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

HITACHINAKA_KEYWORDS = ["ひたちなか", "那珂湊", "勝田", "常陸那珂"]

GIKAI_KEYWORDS = [
    "議会","議員","本会議","委員会","議案","条例","予算","決算",
    "一般質問","議長","副議長","常任委員会","特別委員会","補正予算","当初予算"
]
IMPORTANT_KEYWORDS = [
    "緊急","警報","注意報","台風","地震","津波","避難","災害",
    "入札","新規事業","計画","整備","工事","開発","方針","施策","改正","廃止"
]

# 国政・県政 RSS ソース
IBARAKI_PREF_RSS = [
    ("茨城県 注目情報",  "https://www.pref.ibaraki.jp/chumoku.xml"),
    ("茨城県 防災情報",  "https://www.pref.ibaraki.jp/bousai/bousai_rss.xml"),
]
KANTEI_RSS_URL = "https://www.kantei.go.jp/index-jnews.rdf"
SOUMU_RSS_URL  = "https://www.soumu.go.jp/news.rdf"
CAO_RSS_URL    = "https://www.cao.go.jp/bunken-suishin/rss/news.rdf"
MAFF_RSS_URL   = "https://www.maff.go.jp/rss.xml"
NHK_SEIJI_RSS_URL = "https://www.nhk.or.jp/rss/news/cat4.xml"
# Googleアラート: 環境変数 GOOGLE_ALERT_RSS_URLS にカンマ区切りでURLを設定
GOOGLE_ALERT_RSS_URLS = [u.strip() for u in os.environ.get("GOOGLE_ALERT_RSS_URLS","").split(",") if u.strip()]


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

    def get_text(self, max_chars=12000):
        seen, unique = set(), []
        for l in self._lines:
            if l not in seen:
                seen.add(l); unique.append(l)
        return "\n".join(unique)[:max_chars]

    def get_pdf_links(self): return self._pdf_links


def to_abs(href):
    if href.startswith("http"): return href
    return BASE_URL + (href if href.startswith("/") else "/" + href)


def fetch_page(url, max_pdfs=5):
    try:
        res = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        res.encoding = res.apparent_encoding or "utf-8"
        p = SmartParser(); p.feed(res.text)
        return p.get_text(), [to_abs(l) for l in p.get_pdf_links()[:max_pdfs]]
    except Exception as e:
        print(f"  ページ取得失敗: {e}"); return "", []


def fetch_pdf(pdf_url, max_bytes=4_000_000):
    """PDFテキスト抽出。max_bytes超のPDFはスキップ（デフォルト4MB）"""
    try:
        from pdfminer.high_level import extract_text
        # Content-Lengthで事前チェック
        head = requests.head(pdf_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        size = int(head.headers.get("Content-Length", 0))
        if size > max_bytes:
            print(f"  PDFスキップ（{size//1024}KB）: {pdf_url[:50]}")
            return ""
        res = requests.get(pdf_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
        if len(res.content) > max_bytes:
            return ""
        text = re.sub(r"\s+", " ", extract_text(io.BytesIO(res.content))).strip()
        return text[:5000]
    except Exception as e:
        print(f"  PDF取得失敗: {e}"); return ""


def groq_call(system, user, max_tokens=900, label=""):
    for attempt in range(5):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model":"llama-3.3-70b-versatile",
                      "messages":[{"role":"system","content":system},
                                   {"role":"user","content":user}],
                      "max_tokens":max_tokens, "temperature":0.1},
                timeout=40
            )
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            err = data.get("error",{}).get("message", str(data))
            wait = re.search(r"try again in ([\d.]+)s", err)
            wait_sec = float(wait.group(1)) + 3 if wait else 30
            print(f"  Groq待機{label}(試行{attempt+1}): {wait_sec:.0f}秒")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"  Groq例外{label}(試行{attempt+1}): {e}"); time.sleep(15)
    return ""


def ai_batch_summary(items_data):
    """複数記事を1回のAPIコールでまとめて要約"""
    if not GROQ_API_KEY:
        return {i: d["page_text"][:400] for i, d in enumerate(items_data)}

    blocks = []
    for idx, d in enumerate(items_data):
        parts = [f"タイトル: {d['title']}"]
        if d["page_text"]: parts.append(f"本文:\n{d['page_text'][:4000]}")
        for j, (url, text) in enumerate(d["pdf_list"], 1):
            parts.append(f"PDF{j}:\n{text[:1500]}" if text else f"PDF{j}: {url}（取得不可）")
        blocks.append(f"=={idx}==\n" + "\n".join(parts))

    user_prompt = f"""以下の{len(items_data)}件のひたちなか市公式情報をそれぞれ詳しく整理してください。

【絶対厳守ルール】
- ※は一切使わない / 箇条書きは「・」のみ
- 施設名・団体名・人名・地名・担当課・電話番号を省略せず正確に記載
- 日程・期限・期間・金額・数量・数値・対象者・申請条件を必ず含める
- 行政用語は平易な言葉に言い換える
- ページ本文やPDFに書かれた具体的な内容をできる限り反映する
- 各記事900文字以内

【各記事の出力形式】
==0==
1行目：何についての情報か（一文）
・詳細ポイント6〜9項目（数値・固有名詞を省略しない）

==1==
（以下同様）

【情報】
{"".join(chr(10)*2 + b for b in blocks)}"""

    result = groq_call(
        "ひたちなか市の複数の市政情報をまとめて要約するアシスタントです。固有名詞・数値・担当課情報を省略しません。",
        user_prompt, max_tokens=3500, label="[バッチ]"
    )
    if not result:
        return {i: "" for i in range(len(items_data))}

    summaries = {}
    for idx in range(len(items_data)):
        m = re.search(rf"=={idx}==\s*(.*?)(?===\d+==|$)", result, re.DOTALL)
        summaries[idx] = m.group(1).strip() if m else ""
    return summaries


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
    prompt = f"""以下の茨城新聞記事を地方議員候補者向けに詳しく整理してください。

【厳守ルール】
- ※は一切使わない / 箇条書きは「・」のみ
- 人名・地名・団体名・施設名などの固有名詞は省略せず正確に記載
- 数値・金額・日程・統計・順位は必ず含める
- 茨城県政・ひたちなか市政への影響・関連性があれば必ず補足
- 事件・事故の場合は場所・状況・被害規模を具体的に
- 800文字以内

【構成】
1行目：何についての記事か（一文）
空行
・詳細ポイントを5〜7項目（数値・固有名詞を省略しない）

タイトル: {title}
記事冒頭: {description[:1500]}"""

    result = groq_call(
        "茨城新聞記事を地方議員候補者向けに詳しく整理するアシスタントです。固有名詞・数値を省略せず記載します。",
        prompt, max_tokens=1100, label=f"[{title[:20]}]"
    )
    return result or description[:400]

def ai_digest_ibaraki(articles_with_desc):
    if not GROQ_API_KEY or not articles_with_desc:
        return ""
    lines = "\n".join([
        f"【{a['title']}】\n{a.get('desc','')[:300]}" if a.get('desc') else f"【{a['title']}】"
        for a in articles_with_desc
    ])
    prompt = f"""以下の茨城新聞の本日のニュース記事を、地方議員候補者の視点で詳しくまとめてください。

【ルール】
- 箇条書きは「・」のみ / ※不使用
- 人名・地名・数値は省略せず記載
- 県政・社会・経済の動向として重要なポイントを網羅
- 各記事について1〜2行で触れる（省略なし）
- 600文字以内

【記事一覧】
{lines}"""

    result = groq_call(
        "茨城新聞のニュースを地方議員候補者向けに詳しくまとめるアシスタントです。",
        prompt, max_tokens=900, label="[ダイジェスト]"
    )
    return result or ""


# ===== 国政・県政 RSS =====

def fetch_generic_rss(url, max_items=15):
    """汎用RSSフェッチ。RSS 1.0(名前空間あり)・RSS 2.0・Shift_JIS に対応"""
    RSS1 = "http://purl.org/rss/1.0/"
    DC   = "http://purl.org/dc/elements/1.1/"

    def ft(elem, tag):
        return (elem.findtext(tag) or
                elem.findtext(f"{{{RSS1}}}{tag}") or
                elem.findtext(f"{{{DC}}}{tag}") or "").strip()

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
        enc_m = re.search(rb'encoding=["\']([^"\']+)["\']', raw[:300])
        if enc_m:
            enc = enc_m.group(1).decode('ascii', errors='replace')
            if enc.lower() not in ('utf-8', 'utf8'):
                decoded = raw.decode(enc, errors='replace')
                raw = re.sub(r'encoding=["\'][^"\']+["\']', 'encoding="utf-8"', decoded).encode('utf-8')
        root = ET.fromstring(raw)
        # RSS 2.0 は <item>、RSS 1.0 は {RSS1}item
        elems = root.findall(".//item") or root.findall(f".//{{{RSS1}}}item")
        items = []
        for i in elems[:max_items]:
            desc = re.sub(r'<[^>]+>', '', ft(i, "description")).strip()[:300]
            items.append({
                "title":       ft(i, "title"),
                "link":        ft(i, "link"),
                "pub_date":    ft(i, "pubDate") or ft(i, "date"),
                "description": desc,
            })
        return [i for i in items if i["title"]]
    except Exception as e:
        print(f"  RSS取得失敗 ({url[:60]}): {e}")
        return []


def ai_batch_summary_national(items_data):
    """国政・県政記事を1回のAPIコールでまとめて要約（最大3件推奨）"""
    if not GROQ_API_KEY:
        return {i: "" for i in range(len(items_data))}

    blocks = []
    for idx, d in enumerate(items_data):
        max_text = d.get("_max_text", 4000)
        parts = [f"タイトル: {d['title']}（情報源: {d.get('_source','')}）"]
        if d["page_text"]: parts.append(f"本文:\n{d['page_text'][:max_text]}")
        for j, (url, text) in enumerate(d["pdf_list"], 1):
            parts.append(f"PDF{j}:\n{text[:1500]}" if text else f"PDF{j}: {url}（取得不可）")
        blocks.append(f"=={idx}==\n" + "\n".join(parts))

    user_prompt = f"""以下の{len(items_data)}件の国政・県政情報をそれぞれ詳しく整理してください。

【絶対厳守ルール】
- ※は一切使わない / 箇条書きは「・」のみ
- 機関名・担当者・人名・地名・団体名を省略せず正確に記載
- 数値・金額・期日・対象・規模を必ず含める
- 地方自治・住民生活・選挙・財政への影響を補足
- 各記事700文字以内

【各記事の出力形式】
==0==
1行目：何についての情報か（一文）
・詳細ポイント5〜7項目

==1==
（以下同様）

【情報】
{"".join(chr(10)*2 + b for b in blocks)}"""

    result = groq_call(
        "国政・県政情報を地方議員候補者向けに詳しく整理するアシスタントです。固有名詞・数値を省略しません。",
        user_prompt, max_tokens=3000, label="[国政バッチ]"
    )
    if not result:
        return {i: "" for i in range(len(items_data))}

    summaries = {}
    for idx in range(len(items_data)):
        m = re.search(rf"=={idx}==\s*(.*?)(?===\d+==|$)", result, re.DOTALL)
        summaries[idx] = m.group(1).strip() if m else ""
    return summaries


def ai_select_national_items(sources):
    """各ソースのRSSタイトル・説明からAIが重要記事を選択。{source_name: [index,...]} を返す"""
    if not GROQ_API_KEY:
        return {s["name"]: list(range(min(3, len(s["items"])))) for s in sources}

    blocks = []
    for s in sources:
        if not s["items"]: continue
        lines = "\n".join(
            f"[{i}] {item['title']}" + (f"（{item['description'][:80]}）" if item.get('description') else "")
            for i, item in enumerate(s["items"])
        )
        blocks.append(f"【{s['name']}】\n{lines}")

    if not blocks:
        return {}

    prompt = f"""以下の国政・県政ニュース一覧から、地方議員候補者にとって重要な記事を各情報源ごとに最大3件選んでください。

【選択基準】
- 地方自治・市町村行政に直接関わる制度変更・法改正
- 地方財政・補助金・交付金・予算に関わるもの
- 防災・災害対応・インフラ
- 選挙・議会制度に関するもの
- 住民生活（福祉・医療・教育・子育て）に影響するもの
- 茨城県・ひたちなか市に直接関わる内容

【出力形式】（必ずこの形式で。他の文章は不要）
茨城県 注目情報: 0,2
首相官邸: 1,3,5
総務省: 0,2,4
内閣府 地方分権改革: 0,1
農林水産省: 0,1
NHK 政治: 0,2,4

【ニュース一覧】
{"".join(chr(10)*2 + b for b in blocks)}"""

    result = groq_call(
        "国政・県政ニュースから地方議員候補者に重要な記事を選ぶアシスタントです。",
        prompt, max_tokens=300, label="[重要度選択]"
    )

    selected = {}
    for s in sources:
        # デフォルト: 先頭2件
        selected[s["name"]] = list(range(min(2, len(s["items"]))))

    if result:
        for line in result.strip().split("\n"):
            if ":" not in line: continue
            src_part, idx_part = line.split(":", 1)
            src = src_part.strip()
            try:
                indices = [int(x.strip()) for x in idx_part.split(",") if x.strip().isdigit()]
                # ソース名が一致するものに適用
                for s in sources:
                    if s["name"] == src or s["name"] in src or src in s["name"]:
                        valid = [i for i in indices if i < len(s["items"])]
                        if valid:
                            selected[s["name"]] = valid[:3]
            except Exception:
                continue

    return selected


def process_national_batch(sources):
    """AIが選んだ重要記事をページ取得→AIバッチ要約してカードを返す"""
    if not any(s["items"] for s in sources):
        return {}

    # Step1: AIで重要記事を選択
    print("  国政・県政 重要記事をAIで選択中...")
    selected_indices = ai_select_national_items(sources)
    time.sleep(5)

    # Step2: 選択された記事のページを取得
    all_items = []
    for s in sources:
        for idx in selected_indices.get(s["name"], []):
            if idx < len(s["items"]):
                all_items.append({**s["items"][idx], "_source": s["name"]})

    if not all_items:
        return {}

    # ソース別設定: 茨城県はPDF多め・テキスト多め、他は標準
    SOURCE_CONFIG = {
        "茨城県 注目情報":  {"max_pdfs": 8,  "max_text": 5000},
        "茨城県 防災情報":  {"max_pdfs": 5,  "max_text": 4000},
        "首相官邸":         {"max_pdfs": 3,  "max_text": 4000},
        "総務省":           {"max_pdfs": 8,  "max_text": 4000},
        "内閣府 地方分権改革": {"max_pdfs": 8, "max_text": 4000},
        "農林水産省":       {"max_pdfs": 8,  "max_text": 4000},
        "NHK 政治":        {"max_pdfs": 0,  "max_text": 3000},
    }

    print(f"  国政・県政 {len(all_items)}件 ページ取得中...")
    items_data = []
    for item in all_items:
        cfg = SOURCE_CONFIG.get(item["_source"], {"max_pdfs": 5, "max_text": 4000})
        page_text, pdf_links = fetch_page(item["link"], max_pdfs=cfg["max_pdfs"])
        pdf_list = [(url, fetch_pdf(url)) for url in pdf_links]
        items_data.append({
            "title": item["title"],
            "page_text": page_text,
            "pdf_list": pdf_list,
            "_source": item["_source"],
            "_max_text": cfg["max_text"],
        })
        print(f"    取得: {item['title'][:45]}")
        time.sleep(1)

    # Step3: AI詳細要約（3件ずつ分割してトークン超過を防ぐ）
    CHUNK = 3
    all_summaries = {}
    for start in range(0, len(items_data), CHUNK):
        chunk = items_data[start:start + CHUNK]
        time.sleep(5)
        print(f"  国政・県政 AI要約中 ({start+1}〜{start+len(chunk)}件目)...")
        chunk_summaries = ai_batch_summary_national(chunk)
        for local_idx, summary in chunk_summaries.items():
            all_summaries[start + local_idx] = summary
        if start + CHUNK < len(items_data):
            time.sleep(10)

    cards_by_source = {}
    for idx, item in enumerate(all_items):
        summary = all_summaries.get(idx, "")
        # AI失敗時はタイトル＋RSS descriptionをフォールバックにする
        if not summary:
            desc = item.get("description", "")
            summary = f"（AI要約失敗）{desc[:200]}" if desc else "（情報取得中）"
        card = {
            "title": item["title"], "link": item["link"],
            "summary": summary, "summary_html": summary_to_html(summary)
        }
        cards_by_source.setdefault(item["_source"], []).append(card)
    return cards_by_source


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
               ibaraki_local_cards=None, ibaraki_digest="", ibaraki_all=None,
               national_sources=None, national_cards_by_source=None):
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

    def national_section_html():
        if not national_sources:
            return '<p class="empty">本日の国政・県政情報はありません</p>'
        source_icons = {
            "茨城県 注目情報": "📌", "茨城県 防災情報": "🚨",
            "首相官邸": "🏛️", "総務省": "📋", "内閣府 地方分権改革": "🏢",
            "農林水産省": "🌾", "NHK 政治": "📺",
        }
        html = ""
        for s in national_sources:
            if not s["items"]: continue
            icon = source_icons.get(s["name"], "🔍")
            html += f'<h3 class="src-heading">{icon} {esc(s["name"])}</h3>'
            top_cards = (national_cards_by_source or {}).get(s["name"], [])
            # 上位3件はAI要約カード
            for card in top_cards:
                html += f"""
<div class="card" style="border-left:4px solid #4527A0">
  <div class="card-title"><a href="{esc(card['link'])}" target="_blank">{esc(card['title'])}</a></div>
  <div class="card-summary">{card['summary_html']}</div>
  <div class="card-meta">
    <span class="tag" style="background:#4527A0">{esc(s["name"])}</span>
    <a href="{esc(card['link'])}" target="_blank" class="src-link">公式ページを見る →</a>
  </div>
</div>"""
            # 4件目以降はリスト表示
            rest = s["items"][len(top_cards):]
            if rest:
                rows = "".join(
                    f'<tr><td><a href="{esc(i["link"])}" target="_blank">{esc(i["title"])}</a>'
                    + (f'<div class="item-desc">{esc(i["description"])}</div>' if i.get("description") else "")
                    + '</td></tr>\n'
                    for i in rest
                )
                html += f'<table><tbody>{rows}</tbody></table>'
        return html or '<p class="empty">本日の国政・県政情報はありません</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ひたちなか市・茨城新聞・国政県政 最新情報 | {date_str}</title>
<style>
:root{{--blue:#1565C0;--red:#C62828;--amber:#E65100;--gray:#546E7A;--green:#1B5E20;--purple:#4527A0;--bg:#F8F9FA}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;background:var(--bg);color:#212121;line-height:1.7;font-size:15px}}
header{{background:var(--blue);color:#fff;padding:16px 20px}}
header h1{{font-size:18px;font-weight:700}}
header .updated{{font-size:12px;opacity:.85;margin-top:4px}}
header a{{color:#fff}}
.tab-bar{{display:flex;background:#fff;border-bottom:2px solid #e0e0e0;position:sticky;top:0;z-index:10}}
.tab-btn{{flex:1;padding:12px 4px;font-size:13px;font-weight:700;border:none;background:none;cursor:pointer;color:#888;border-bottom:3px solid transparent;margin-bottom:-2px;transition:all .2s}}
.tab-btn.active{{color:var(--blue);border-bottom-color:var(--blue)}}
.tab-btn:nth-child(2).active{{color:var(--green);border-bottom-color:var(--green)}}
.tab-btn:nth-child(3).active{{color:var(--purple);border-bottom-color:var(--purple)}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}
.container{{max-width:860px;margin:0 auto;padding:16px}}
h2{{font-size:15px;font-weight:700;padding:8px 0 6px;border-bottom:2px solid currentColor;margin:20px 0 10px}}
h3.src-heading{{font-size:14px;font-weight:700;margin:16px 0 8px;color:#4527A0}}
h2.gikai{{color:#C62828}} h2.important{{color:#E65100}} h2.minor{{color:#546E7A}}
h2.ibaraki{{color:#1B5E20}} h2.ibaraki-all{{color:#2E7D32}}
h2.national{{color:#4527A0}}
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
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:12px}}
td{{padding:10px 14px;border-bottom:1px solid #eee;font-size:14px}}
td a{{color:#1565C0;text-decoration:none}}
td a:hover{{text-decoration:underline}}
.item-desc{{font-size:12px;color:#666;margin-top:3px}}
.empty{{color:#888;font-size:14px;padding:8px 0}}
footer{{text-align:center;font-size:12px;color:#888;padding:24px;margin-top:16px}}
</style>
</head>
<body>
<header>
  <h1>ひたちなか市・茨城新聞・国政県政 最新情報</h1>
  <div class="updated">最終更新: {date_str} JST</div>
</header>

<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('hitachinaka',this)">🏛️ ひたちなか市</button>
  <button class="tab-btn" onclick="switchTab('ibaraki',this)">🗞️ 茨城新聞</button>
  <button class="tab-btn" onclick="switchTab('national',this)">🏢 国政・県政</button>
</div>

<div id="hitachinaka" class="tab-content active">
<div class="container">
<h2 class="gikai">🔴 議会情報</h2>
{cards_html(gikai_cards, "#C62828", "議会")}

<h2 class="important">🟡 重要なお知らせ</h2>
{cards_html(important_cards, "#E65100", "重要")}

<h2 class="minor">⚪ その他の更新情報（24時間以内）</h2>
{minor_html()}
</div>
</div>

<div id="ibaraki" class="tab-content">
<div class="container">
<h2 class="ibaraki">📍 ひたちなか関連記事</h2>
{cards_html(ibaraki_local_cards or [], "#1B5E20", "茨城新聞")}

<h2 class="ibaraki-all">📰 本日の茨城ニュース</h2>
{ibaraki_digest_html(ibaraki_digest, ibaraki_all or [])}
</div>
</div>

<div id="national" class="tab-content">
<div class="container">
<h2 class="national">🏢 国政・県政 最新情報</h2>
{national_section_html()}
</div>
</div>

<footer>自動生成 | <a href="https://www.city.hitachinaka.lg.jp/" target="_blank">ひたちなか市公式</a> | <a href="https://ibarakinews.jp/" target="_blank">茨城新聞</a> | <a href="https://www.pref.ibaraki.jp/" target="_blank">茨城県</a> | <a href="https://www.kantei.go.jp/" target="_blank">首相官邸</a> | <a href="https://www.soumu.go.jp/" target="_blank">総務省</a> | <a href="https://www.cao.go.jp/bunken-suishin/" target="_blank">内閣府 地方分権</a> | <a href="https://www.maff.go.jp/" target="_blank">農林水産省</a> | <a href="https://www3.nhk.or.jp/news/" target="_blank">NHK</a></footer>

<script>
function switchTab(id, btn) {{
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body>
</html>"""


def process_items_batch(items, label, batch_size=3):
    """3件ずつバッチ処理（テキスト量増加によるトークン超過を防ぐ）"""
    if not items:
        return []

    print(f"  {label} {len(items)}件 ページ取得中...")
    items_data = []
    for item in items:
        # ひたちなか市はPDF5件まで
        page_text, pdf_links = fetch_page(item["link"], max_pdfs=5)
        pdf_list = [(url, fetch_pdf(url)) for url in pdf_links]
        items_data.append({"title": item["title"], "page_text": page_text, "pdf_list": pdf_list})
        print(f"    取得: {item['title'][:45]}")

    # 3件ずつに分けてAI要約
    all_summaries = {}
    for start in range(0, len(items_data), batch_size):
        chunk = items_data[start:start + batch_size]
        print(f"  {label} AI要約中 ({start+1}〜{start+len(chunk)}件目)...")
        chunk_summaries = ai_batch_summary(chunk)
        for local_idx, summary in chunk_summaries.items():
            all_summaries[start + local_idx] = summary
        if start + batch_size < len(items_data):
            time.sleep(8)

    cards = []
    for idx, item in enumerate(items):
        summary = all_summaries.get(idx, "")
        cards.append({
            "title": item["title"], "link": item["link"],
            "summary": summary, "summary_html": summary_to_html(summary), "label": label
        })
    return cards


def main():
    if not LINE_TOKEN or not LINE_USER_ID:
        print("ERROR: LINE環境変数未設定"); return

    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    now  = datetime.now(timezone.utc)

    # ===== ひたちなか市公式サイト =====
    items     = fetch_rss()
    new_items = [i for i in items if i["link"] not in seen]
    gikai     = [i for i in new_items if classify(i) == "gikai"]
    important = [i for i in new_items if classify(i) == "important"]
    minor_24h = [i for i in new_items if classify(i) == "minor" and within_24h(i.get("pub_date",""))]

    all_priority    = gikai + important
    all_cards       = process_items_batch(all_priority, "ひたちなか市")
    gikai_cards     = all_cards[:len(gikai)]
    important_cards = all_cards[len(gikai):]

    # ===== 茨城新聞 =====
    print("茨城新聞を取得中...")
    ib_items = fetch_ibaraki_rss()
    ib_new   = [i for i in ib_items if i["link"] not in seen and within_24h(i.get("pub_date",""))]
    ib_local = [i for i in ib_new if any(kw in i["title"] for kw in HITACHINAKA_KEYWORDS)]
    ib_other = [i for i in ib_new if i not in ib_local]

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

    ib_digest = ""
    if ib_other:
        print(f"  茨城新聞: og:description取得中 ({len(ib_other)}件)...")
        for item in ib_other:
            item["desc"] = fetch_og_description(item["link"])
            time.sleep(1)
        time.sleep(5)
        print(f"  茨城新聞ダイジェスト: {len(ib_other)}件")
        ib_digest = ai_digest_ibaraki(ib_other)

    # ===== 国政・県政 =====
    print("国政・県政 RSS取得中...")
    national_sources = []

    # 茨城県（3フィード）
    for name, url in IBARAKI_PREF_RSS:
        items_feed = fetch_generic_rss(url, max_items=10)
        national_sources.append({"name": name, "items": items_feed})
        print(f"  {name}: {len(items_feed)}件")

    # 首相官邸
    kantei_items = fetch_generic_rss(KANTEI_RSS_URL, max_items=10)
    national_sources.append({"name": "首相官邸", "items": kantei_items})
    print(f"  首相官邸: {len(kantei_items)}件")

    # 総務省
    soumu_items = fetch_generic_rss(SOUMU_RSS_URL, max_items=10)
    national_sources.append({"name": "総務省", "items": soumu_items})
    print(f"  総務省: {len(soumu_items)}件")

    # 内閣府 地方分権改革
    cao_items = fetch_generic_rss(CAO_RSS_URL, max_items=10)
    national_sources.append({"name": "内閣府 地方分権改革", "items": cao_items})
    print(f"  内閣府 地方分権改革: {len(cao_items)}件")

    # 農林水産省
    maff_items = fetch_generic_rss(MAFF_RSS_URL, max_items=10)
    national_sources.append({"name": "農林水産省", "items": maff_items})
    print(f"  農林水産省: {len(maff_items)}件")

    # NHK 政治
    nhk_items = fetch_generic_rss(NHK_SEIJI_RSS_URL, max_items=15)
    national_sources.append({"name": "NHK 政治", "items": nhk_items})
    print(f"  NHK 政治: {len(nhk_items)}件")

    # Googleアラート（設定済みの場合のみ）
    for alert_url in GOOGLE_ALERT_RSS_URLS:
        alert_items = fetch_generic_rss(alert_url, max_items=10)
        national_sources.append({"name": "Googleアラート", "items": alert_items})
        print(f"  Googleアラート: {len(alert_items)}件")

    # 国政・県政: 上位3件をページ取得→AI要約
    has_national = any(s["items"] for s in national_sources)
    national_cards_by_source = {}
    if has_national:
        national_cards_by_source = process_national_batch(national_sources)

    # ===== HTML生成（常時） =====
    html = build_html(
        gikai_cards, important_cards, minor_24h, now,
        ib_local_cards, ib_digest, ib_other,
        national_sources, national_cards_by_source
    )
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"✓ {HTML_FILE} 生成完了")

    for i in new_items: seen.add(i["link"])
    for i in ib_new:    seen.add(i["link"])
    save_seen(seen)

    # ===== LINE通知（本日未送信の場合のみ） =====
    jst_now  = datetime.now(timezone.utc) + timedelta(hours=9)
    today    = jst_now.strftime("%Y-%m-%d")
    sent_file = Path("last_line_sent.txt")
    if sent_file.exists() and sent_file.read_text().strip() == today:
        print(f"本日({today})のLINE送信済み。スキップします。")
        print(f"{now:%Y-%m-%d %H:%M} 完了（LINE送信スキップ）")
        return

    counts = []
    if gikai:         counts.append(f"議会{len(gikai)}件")
    if important:     counts.append(f"重要{len(important)}件")
    if minor_24h:     counts.append(f"市その他{len(minor_24h)}件")
    if ib_local:      counts.append(f"茨城新聞(ひたちなか){len(ib_local)}件")
    if ib_other:      counts.append(f"茨城新聞{len(ib_other)}件")
    if has_national:
        total_nat = sum(len(s["items"]) for s in national_sources)
        counts.append(f"国政県政{total_nat}件")
    summary_line = "・".join(counts) if counts else "更新あり"

    # 8:05 JST まで待機
    target = jst_now.replace(hour=8, minute=5, second=0, microsecond=0)
    if jst_now < target:
        wait_sec = (target - jst_now).total_seconds()
        print(f"  8:05 JST まで {wait_sec:.0f}秒 待機中...")
        time.sleep(wait_sec)

    send_time = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d %H:%M")
    msg = f"【ひたちなか市・茨城新聞・国政県政 更新情報】{send_time}\n{summary_line}\n\n{PAGES_URL}"
    send_line(msg)
    sent_file.write_text(today)
    print(f"✓ LINE送信: {msg}")
    print(f"{now:%Y-%m-%d %H:%M} 完了 — 議会:{len(gikai)} 重要:{len(important)} 軽微:{len(minor_24h)} 茨城新聞:{len(ib_new)} 国政県政:{sum(len(s['items']) for s in national_sources)}")


if __name__ == "__main__":
    main()
