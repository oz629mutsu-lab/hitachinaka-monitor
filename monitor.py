#!/usr/bin/env python3
"""ひたちなか市ホームページ監視スクリプト v10.0 (Gemini Flash AI・seen.json TTL追加)"""

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
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL      = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
STATE_FILE      = Path("seen.json")
SEEN_EXPIRE_DAYS = 30
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

# 補助金・助成金 RSS
SUBSIDY_RSS_SOURCES = [
    ("中小企業庁",  "https://www.chusho.meti.go.jp/rss/index.xml"),
    ("ミラサポplus", "https://mirasapo-plus.go.jp/feed/"),
]
SUBSIDY_KEYWORDS = ["補助金", "助成金", "給付金", "支援金", "交付金", "融資", "補助", "助成", "給付"]

# 省庁スクレイピング設定（月別プレスリリースページ）
MINISTRY_SCRAPE_SOURCES = [
    {
        "name": "国土交通省",
        "url_template": "https://www.mlit.go.jp/report/press/houdou{yyyymm}.html",
        "base_url": "https://www.mlit.go.jp",
        "link_pattern": r'/report/press/[a-z]+\d{2}_hh_\d+\.html',
    },
    {
        "name": "厚生労働省",
        "url_template": "https://www.mhlw.go.jp/stf/houdou/houdou_list_{yyyymm}.html",
        "base_url": "https://www.mhlw.go.jp",
        "link_pattern": r'/stf/houdou/(?:\d{7,}|newpage_\d+|0000[\w_]+)\.html',
    },
    {
        "name": "環境省",
        "url_template": "https://www.env.go.jp/press/{yyyymm}.html",
        "base_url": "https://www.env.go.jp",
        "link_pattern": r'/press/press_\d+\.html',
    },
]

# 国会会議録 検索キーワード
KOKKAI_KEYWORDS = ["ひたちなか", "那珂湊", "茨城県 地方自治"]


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
    for attempt in range(3):
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
            wait_sec = min(float(wait.group(1)) + 3, 60) if wait else 30
            print(f"  Groq待機{label}(試行{attempt+1}): {wait_sec:.0f}秒")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"  Groq例外{label}(試行{attempt+1}): {e}"); time.sleep(10)
    return ""


def gemini_call(system, user, max_tokens=900, label=""):
    """Gemini Flash (無料 1M TPM)。失敗時のみGroqにフォールバック"""
    for attempt in range(3):
        try:
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1}
            }
            res = requests.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=payload, timeout=60
            )
            data = res.json()
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            err = data.get("error", {}).get("message", str(data))
            print(f"  Geminiエラー{label}(試行{attempt+1}): {err[:120]}")
            time.sleep(5)
        except Exception as e:
            print(f"  Gemini例外{label}(試行{attempt+1}): {e}"); time.sleep(5)
    if GROQ_API_KEY:
        print(f"  Gemini全失敗 → Groqフォールバック{label}")
        return groq_call(system, user, max_tokens=max_tokens, label=label)
    return ""


def ai_call(system, user, max_tokens=900, label=""):
    """AI統合エントリ: Gemini優先 → Groqフォールバック"""
    if GEMINI_API_KEY:
        return gemini_call(system, user, max_tokens=max_tokens, label=label)
    if GROQ_API_KEY:
        return groq_call(system, user, max_tokens=max_tokens, label=label)
    return ""


def ai_batch_summary(items_data):
    """複数記事を1回のAPIコールでまとめて要約"""
    if not GEMINI_API_KEY and not GROQ_API_KEY:
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

    result = ai_call(
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
    """seen.jsonをdictで読み込む。旧list形式→dict自動変換、30日超えエントリ自動削除"""
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if isinstance(raw, list):
        return {url: today for url in raw}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRE_DAYS)).strftime("%Y-%m-%d")
    return {url: date for url, date in raw.items() if date >= cutoff}

def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2))

def mark_seen(url, seen):
    seen[url] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type":"text","text":message[:4500]}]},
        timeout=30
    )
    print(f"  LINE APIレスポンス: HTTP {res.status_code} / {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"LINE送信失敗: {res.status_code} {res.text[:200]}")


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
    if not (GEMINI_API_KEY or GROQ_API_KEY) or not description:
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

    result = ai_call(
        "茨城新聞記事を地方議員候補者向けに詳しく整理するアシスタントです。固有名詞・数値を省略せず記載します。",
        prompt, max_tokens=1100, label=f"[{title[:20]}]"
    )
    return result or description[:400]

def ai_digest_ibaraki(articles_with_desc):
    if not (GEMINI_API_KEY or GROQ_API_KEY) or not articles_with_desc:
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

    result = ai_call(
        "茨城新聞のニュースを地方議員候補者向けに詳しくまとめるアシスタントです。",
        prompt, max_tokens=900, label="[ダイジェスト]"
    )
    return result or ""


# ===== 省庁スクレイピング =====

def fetch_scraped_ministry(source_cfg, seen=None, max_items=12):
    """月別プレスリリースページをスクレイピングし、新着記事を返す"""
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    yyyymm = f"{now.year}{now.month:02d}"
    url = source_cfg["url_template"].format(yyyymm=yyyymm)
    base_url = source_cfg["base_url"]
    pattern = source_cfg["link_pattern"]

    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        res.encoding = res.apparent_encoding or "utf-8"
        seen_links = set()
        items = []
        for m in re.finditer(rf'href="({pattern})"', res.text):
            link_path = m.group(1)
            full_link = base_url + link_path if not link_path.startswith("http") else link_path
            if full_link in seen_links:
                continue
            seen_links.add(full_link)
            if seen and full_link in seen:
                continue
            # タイトルを抽出（リンク直後のテキスト）
            title_m = re.search(rf'href="{re.escape(link_path)}"[^>]*>([^<]{{5,}})', res.text)
            title = re.sub(r'\s+', ' ', title_m.group(1)).strip() if title_m else ""
            if len(title) < 5:
                continue
            items.append({"title": title, "link": full_link, "pub_date": "", "description": ""})
            if len(items) >= max_items:
                break
        return items
    except Exception as e:
        print(f"  スクレイピング失敗 ({url[:55]}): {e}")
        return []


# ===== 国会会議録API =====

def fetch_kokkai_speeches(days_back=14):
    """国会会議録APIでひたちなか・茨城関連発言を検索（過去days_back日）"""
    since = (datetime.now(timezone.utc) + timedelta(hours=9) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    speeches = []
    seen_urls = set()

    for keyword in KOKKAI_KEYWORDS:
        try:
            res = requests.get(
                "https://kokkai.ndl.go.jp/api/speech",
                params={"any": keyword, "from": since, "recordPacking": "json",
                        "maximumRecords": 4, "startRecord": 1},
                timeout=15
            )
            for rec in res.json().get("speechRecord", []):
                url = rec.get("speechURL", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                speech_text = rec.get("speech", "")
                # キーワード周辺の抜粋を作成
                idx = speech_text.find(keyword)
                if idx >= 0:
                    start = max(0, idx - 40)
                    end = min(len(speech_text), idx + 200)
                    excerpt = "…" + re.sub(r'\s+', ' ', speech_text[start:end]) + "…"
                else:
                    excerpt = re.sub(r'\s+', ' ', speech_text[:200])
                speeches.append({
                    "keyword": keyword,
                    "date": rec.get("date", ""),
                    "house": rec.get("nameOfHouse", ""),
                    "meeting": rec.get("nameOfMeeting", ""),
                    "speaker": rec.get("speaker", ""),
                    "excerpt": excerpt,
                    "link": url,
                })
            time.sleep(1)
        except Exception as e:
            print(f"  国会会議録API失敗({keyword}): {e}")

    # 日付降順でソートして最大10件
    speeches.sort(key=lambda x: x["date"], reverse=True)
    return speeches[:10]


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
    if not (GEMINI_API_KEY or GROQ_API_KEY):
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

    result = ai_call(
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


def ai_select_national_items(sources, ai_category="国政・県政"):
    """各ソースのRSSタイトル・説明からAIが重要記事を選択。{source_name: [index,...]} を返す"""
    if not (GEMINI_API_KEY or GROQ_API_KEY):
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

    prompt = f"""以下の{ai_category}ニュース一覧から、地方議員候補者にとって重要な記事を各情報源ごとに最大5件選んでください。

【選択基準】
- 地方自治・市町村行政に直接関わる制度変更・法改正
- 地方財政・補助金・交付金・予算に関わるもの
- 防災・災害対応・インフラ
- 選挙・議会制度に関するもの
- 住民生活（福祉・医療・教育・子育て）に影響するもの
- 茨城県・ひたちなか市に直接関わる内容

【出力形式】（必ずこの形式で。他の文章は不要）
ソース名A: 0,2,4
ソース名B: 1,3

【ニュース一覧】
{"".join(chr(10)*2 + b for b in blocks)}"""

    result = ai_call(
        f"{ai_category}情報を地方議員候補者向けに整理するアシスタントです。",
        prompt, max_tokens=400, label="[重要度選択]"
    )

    selected = {}
    for s in sources:
        # デフォルト: 先頭3件
        selected[s["name"]] = list(range(min(3, len(s["items"]))))

    if result:
        for line in result.strip().split("\n"):
            if ":" not in line: continue
            src_part, idx_part = line.split(":", 1)
            src = src_part.strip()
            try:
                indices = [int(x.strip()) for x in idx_part.split(",") if x.strip().isdigit()]
                for s in sources:
                    if s["name"] == src or s["name"] in src or src in s["name"]:
                        valid = [i for i in indices if i < len(s["items"])]
                        if valid:
                            selected[s["name"]] = valid[:5]
            except Exception:
                continue

    return selected


def process_national_batch(sources, ai_category="国政・県政"):
    """AIが選んだ重要記事をページ取得→AIバッチ要約してカードを返す"""
    if not any(s["items"] for s in sources):
        return {}

    # Step1: AIで重要記事を選択
    print(f"  {ai_category} 重要記事をAIで選択中...")
    selected_indices = ai_select_national_items(sources, ai_category=ai_category)
    time.sleep(5)

    # Step2: 選択された記事のページを取得
    all_items = []
    for s in sources:
        for idx in selected_indices.get(s["name"], []):
            if idx < len(s["items"]):
                all_items.append({**s["items"][idx], "_source": s["name"]})

    if not all_items:
        return {}

    # ソース別設定
    SOURCE_CONFIG = {
        "茨城県 注目情報":  {"max_pdfs": 8,  "max_text": 5000},
        "茨城県 防災情報":  {"max_pdfs": 5,  "max_text": 4000},
        "首相官邸":         {"max_pdfs": 3,  "max_text": 4000},
        "総務省":           {"max_pdfs": 8,  "max_text": 4000},
        "内閣府 地方分権改革": {"max_pdfs": 8, "max_text": 4000},
        "農林水産省":       {"max_pdfs": 8,  "max_text": 4000},
        "NHK 政治":        {"max_pdfs": 0,  "max_text": 3000},
        "国土交通省":       {"max_pdfs": 5,  "max_text": 4000},
        "厚生労働省":       {"max_pdfs": 5,  "max_text": 4000},
        "環境省":           {"max_pdfs": 3,  "max_text": 3000},
        "中小企業庁":       {"max_pdfs": 2,  "max_text": 3000},
        "ミラサポplus":     {"max_pdfs": 2,  "max_text": 3000},
        "関連情報（省庁フィルタ）": {"max_pdfs": 2, "max_text": 3000},
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


def build_html(gikai_cards, important_cards, minor_items, generated_at,  # noqa: C901
               ibaraki_local_cards=None, ibaraki_digest="", ibaraki_all=None,
               national_sources=None, national_cards_by_source=None,
               kokkai_speeches=None,
               subsidy_sources=None, subsidy_cards_by_source=None):
    jst = generated_at + timedelta(hours=9)
    date_str = jst.strftime("%-m月%-d日（%a）")

    PREF_NAMES = {"茨城県 注目情報", "茨城県 防災情報"}
    pref_sources  = [s for s in (national_sources or []) if s["name"] in PREF_NAMES]
    govt_sources  = [s for s in (national_sources or []) if s["name"] not in PREF_NAMES]

    cnt_priority = len(gikai_cards) + len(important_cards)
    cnt_hita  = cnt_priority + len(minor_items)
    cnt_ib    = len(ibaraki_local_cards or []) + len(ibaraki_all or [])
    cnt_nat   = sum(len(s["items"]) for s in govt_sources) + len(kokkai_speeches or [])
    cnt_sub   = sum(len(s["items"]) for s in (subsidy_sources or []))

    SOURCE_ICONS = {
        "茨城県 注目情報":"📌","茨城県 防災情報":"🚨",
        "首相官邸":"🏛️","総務省":"📋","内閣府 地方分権改革":"🏢",
        "農林水産省":"🌾","NHK 政治":"📺",
        "国土交通省":"🏗️","厚生労働省":"🏥","環境省":"🌿",
        "中小企業庁":"🏭","ミラサポplus":"💼","関連情報（省庁フィルタ）":"🔗",
        "Googleアラート":"🔔",
    }

    # ── HTML helpers ──

    def top_story(eyebrow, title, link, src_label, summary_html,
                  accent="#1D4ED8", grad="linear-gradient(135deg,#EFF6FF 0%,#DBEAFE 100%)",
                  shadow="rgba(29,78,216,.10)", badge_html=""):
        sum_block = f'<div class="ts-summary">{summary_html}</div>' if summary_html and summary_html.strip() else ''
        badge_block = f'{badge_html}' if badge_html else ''
        return (
            f'<div class="top-story" style="background:{grad};border-left:4px solid {accent};'
            f'box-shadow:0 2px 10px {shadow};">'
            f'<div class="ts-eyebrow" style="color:{accent}">'
            f'<span class="bar" style="background:{accent}"></span>{esc(eyebrow)}</div>'
            f'<div class="ts-title">{esc(title)}</div>'
            f'<div class="ts-meta"><span>{esc(src_label)}</span>{badge_block}</div>'
            f'{sum_block}'
            f'<a class="ts-link" href="{esc(link)}" target="_blank" rel="noopener" '
            f'style="color:{accent}">詳細を読む ›</a>'
            f'</div>'
        )

    def sec_div(label):
        return (
            f'<div class="sec-divider">'
            f'<span class="sec-divider-label">{esc(label)}</span>'
            f'</div>'
        )

    def art_row(title, link, src_label, label_html=""):
        return (
            f'<a class="article" href="{esc(link)}" target="_blank" rel="noopener">'
            f'<div class="art-body">'
            f'<div class="art-title">{label_html}{esc(title)}</div>'
            f'<div class="art-meta"><span>{esc(src_label)}</span></div>'
            f'</div>'
            f'<div class="art-chevron">›</div>'
            f'</a>'
        )

    def empty_msg(text="本日の情報はありません"):
        return f'<div class="empty-msg">{esc(text)}</div>'

    def bnav_badge(n):
        return f'<div class="bnav-badge">{n}</div>' if n else ''

    # ── Tab 1: ひたちなか市 ──
    hita_top = None
    hita_top_label = ""
    rest_gikai     = list(gikai_cards)
    rest_important = list(important_cards)

    if gikai_cards:
        hita_top = gikai_cards[0]; hita_top_label = "議会情報"; rest_gikai = gikai_cards[1:]
    elif important_cards:
        hita_top = important_cards[0]; hita_top_label = "重要なお知らせ"; rest_important = important_cards[1:]

    hita_html = ""
    if hita_top:
        hita_html += top_story(
            eyebrow=f"ひたちなか市・{hita_top_label}", title=hita_top["title"],
            link=hita_top["link"], src_label="🏛️ 市公式",
            summary_html=hita_top.get("summary_html", ""))

    if rest_gikai:
        hita_html += sec_div("議会情報")
        for c in rest_gikai:
            hita_html += art_row(c["title"], c["link"], "🏛️ 議会")

    # important_cards: show all if top was gikai, otherwise rest
    show_important = important_cards if hita_top_label != "重要なお知らせ" else rest_important
    if show_important:
        hita_html += sec_div("重要なお知らせ")
        for c in show_important:
            hita_html += art_row(c["title"], c["link"], "📌 重要",
                label_html='<span class="art-label urgent">🔴 重要</span>')

    if minor_items:
        hita_html += sec_div("その他")
        for i in minor_items:
            hita_html += art_row(i["title"], i["link"], "🏛️ 市公式")

    if not hita_html:
        hita_html = empty_msg("本日のひたちなか市情報はありません")

    # ── Tab 2: 茨城 ──
    ib_local = list(ibaraki_local_cards or [])
    ib_all   = list(ibaraki_all or [])
    ib_top   = None
    rest_ib_local = ib_local

    if ib_local:
        ib_top = ib_local[0]; rest_ib_local = ib_local[1:]
    elif ib_all:
        first = ib_all[0]
        ib_top = {"title": first["title"], "link": first["link"], "summary_html": ""}

    ib_html = ""
    if ib_top:
        ib_html += top_story(
            eyebrow="茨城新聞・今日の注目", title=ib_top["title"],
            link=ib_top["link"], src_label="🗞️ 茨城新聞",
            summary_html=ib_top.get("summary_html", ""),
            accent="#6D28D9",
            grad="linear-gradient(135deg,#F5F3FF 0%,#EDE9FE 100%)",
            shadow="rgba(109,40,217,.10)")

    if rest_ib_local:
        ib_html += sec_div("ひたちなか関連")
        for c in rest_ib_local:
            ib_html += art_row(c["title"], c["link"], "🗞️ 茨城新聞")

    local_links = {c["link"] for c in ib_local}
    top_link    = ib_top["link"] if ib_top and not ib_local else ""
    ib_others = [i for i in ib_all if i["link"] not in local_links and i["link"] != top_link]
    if ib_others:
        ib_html += sec_div("茨城新聞")
        for i in ib_others:
            ib_html += art_row(i["title"], i["link"], "🗞️ 茨城新聞")

    for s in pref_sources:
        if not s["items"]: continue
        icon = SOURCE_ICONS.get(s["name"], "📌")
        ib_html += sec_div(f"{icon} {s['name']}")
        shown = set()
        for c in (national_cards_by_source or {}).get(s["name"], []):
            ib_html += art_row(c["title"], c["link"], f"{icon} {s['name']}")
            shown.add(c["link"])
        for item in s["items"]:
            if item["link"] not in shown:
                ib_html += art_row(item["title"], item["link"], f"{icon} {s['name']}")

    if not ib_html:
        ib_html = empty_msg("本日の茨城情報はありません")

    # ── Tab 3: 国政・国会 ──
    nat_priority = ["総務省","首相官邸","NHK 政治","内閣府 地方分権改革",
                    "農林水産省","国土交通省","厚生労働省","環境省"]
    nat_top = None; nat_top_src = ""
    for sname in nat_priority:
        cs = (national_cards_by_source or {}).get(sname, [])
        if cs:
            nat_top = cs[0]; nat_top_src = sname; break
    if not nat_top:
        for s in govt_sources:
            if s["items"]:
                nat_top = {"title": s["items"][0]["title"], "link": s["items"][0]["link"], "summary_html": ""}
                nat_top_src = s["name"]; break

    nat_html = ""
    if nat_top:
        icon = SOURCE_ICONS.get(nat_top_src, "🏢")
        nat_html += top_story(
            eyebrow=f"{nat_top_src}・今日の注目", title=nat_top["title"],
            link=nat_top["link"], src_label=f"{icon} {nat_top_src}",
            summary_html=nat_top.get("summary_html", ""),
            accent="#1E40AF",
            grad="linear-gradient(135deg,#EFF6FF 0%,#DBEAFE 100%)",
            shadow="rgba(30,64,175,.10)")

    nat_top_link = nat_top["link"] if nat_top else ""
    GOVT_GROUPS_DEF = [
        ("官邸・総務・内閣府",       ["首相官邸","総務省","内閣府 地方分権改革"]),
        ("省庁（農水・国交・厚労・環境）", ["農林水産省","国土交通省","厚生労働省","環境省"]),
        ("NHK政治",                   ["NHK 政治"]),
    ]
    for grp_label, src_names in GOVT_GROUPS_DEF:
        grp_html = ""
        for sname in src_names:
            s = next((x for x in govt_sources if x["name"] == sname), None)
            if not s or not s["items"]: continue
            icon = SOURCE_ICONS.get(sname, "🏢")
            shown = set()
            for c in (national_cards_by_source or {}).get(sname, []):
                if c["link"] == nat_top_link: continue
                grp_html += art_row(c["title"], c["link"], f"{icon} {sname}")
                shown.add(c["link"])
            for item in s["items"]:
                if item["link"] not in shown and item["link"] != nat_top_link:
                    grp_html += art_row(item["title"], item["link"], f"{icon} {sname}")
        if grp_html:
            nat_html += sec_div(grp_label) + grp_html

    if kokkai_speeches:
        nat_html += sec_div("国会会議録")
        for sp in kokkai_speeches:
            dd = sp["date"].replace("-", "/") if sp.get("date") else ""
            nat_html += art_row(
                title=f'{sp["meeting"]} — {sp["speaker"]} ({dd})',
                link=sp["link"],
                src_label=f'📜 {sp.get("keyword","ひたちなか")}')

    if not nat_html:
        nat_html = empty_msg("本日の国政・国会情報はありません")

    # ── Tab 4: 補助金 ──
    sub_priority = ["中小企業庁","ミラサポplus","関連情報（省庁フィルタ）"]
    sub_top = None; sub_top_src = ""
    for sname in sub_priority:
        cs = (subsidy_cards_by_source or {}).get(sname, [])
        if cs:
            sub_top = cs[0]; sub_top_src = sname; break
    if not sub_top:
        for s in (subsidy_sources or []):
            if s["items"]:
                sub_top = {"title": s["items"][0]["title"], "link": s["items"][0]["link"], "summary_html": ""}
                sub_top_src = s["name"]; break

    sub_html = ""
    if sub_top:
        icon = SOURCE_ICONS.get(sub_top_src, "💰")
        sub_html += top_story(
            eyebrow=f"{sub_top_src}・注目の補助金", title=sub_top["title"],
            link=sub_top["link"], src_label=f"{icon} {sub_top_src}",
            summary_html=sub_top.get("summary_html", ""),
            accent="#D97706",
            grad="linear-gradient(135deg,#FFFBEB 0%,#FEF3C7 100%)",
            shadow="rgba(217,119,6,.10)")

    sub_top_link = sub_top["link"] if sub_top else ""
    sub_items_html = ""
    for sname in sub_priority:
        s = next((x for x in (subsidy_sources or []) if x["name"] == sname), None)
        if not s or not s["items"]: continue
        icon = SOURCE_ICONS.get(sname, "💰")
        shown = set()
        for c in (subsidy_cards_by_source or {}).get(sname, []):
            if c["link"] == sub_top_link: continue
            sub_items_html += art_row(c["title"], c["link"], f"{icon} {sname}")
            shown.add(c["link"])
        for item in s["items"]:
            if item["link"] not in shown and item["link"] != sub_top_link:
                sub_items_html += art_row(item["title"], item["link"], f"{icon} {sname}")
    if sub_items_html:
        sub_html += sec_div("補助金・助成金一覧") + sub_items_html

    if not sub_html:
        sub_html = empty_msg("本日の補助金情報はありません")

    # ── badge counts ──
    hita_bdg = bnav_badge(cnt_priority or "")
    sub_bdg  = bnav_badge(cnt_sub or "")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ひたちなか政治情報 | {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F2F2F7;--sheet:#FFF;
  --t:#000;--t2:#1C1C1E;--m:#3C3C43;--sub:#8E8E93;
  --b:#E5E5EA;--b2:rgba(0,0,0,.1);
  --brand:#1D4ED8;--brand-lt:#EFF6FF;--brand-md:#DBEAFE;
  --red:#DC2626;--red-lt:#FEE2E2;
}}
body{{font-family:'Noto Sans JP',-apple-system,sans-serif;background:var(--bg);
  color:var(--t);-webkit-font-smoothing:antialiased;max-width:768px;margin:0 auto;min-height:100vh}}

/* ── ヘッダー ── */
.hd{{height:52px;background:rgba(255,255,255,.92);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);border-bottom:.5px solid var(--b2);
  padding:0 16px;display:flex;justify-content:space-between;align-items:center;
  position:sticky;top:0;z-index:30}}
.hd-date{{font-size:15px;font-weight:700;color:var(--t2)}}
.hd-right{{display:flex;gap:6px}}
.chip{{font-size:13px;font-weight:700;padding:4px 10px;border-radius:99px;
  text-decoration:none;display:inline-block}}
.chip.alert{{background:var(--red-lt);color:var(--red)}}
.chip.gray{{background:var(--b);color:var(--m)}}

/* ── タブ ── */
.tab-content{{display:none;padding-bottom:88px}}
.tab-content.active{{display:block}}

/* ── ページシート（タブ内コンテンツを1枚に） ── */
.page-sheet{{background:var(--sheet);border-top:.5px solid var(--b2);border-bottom:.5px solid var(--b2);margin-top:20px}}

/* ── TOP STORY ── */
.top-story{{margin:16px 16px 0;border-radius:14px;padding:16px 16px 14px;overflow:hidden}}
.ts-eyebrow{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  display:flex;align-items:center;gap:5px;margin-bottom:9px}}
.ts-eyebrow .bar{{display:inline-block;width:14px;height:2px;border-radius:2px;background:currentColor}}
.ts-title{{font-size:20px;font-weight:800;line-height:1.45;letter-spacing:-.01em;
  color:var(--t);margin-bottom:9px}}
.ts-meta{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  font-size:13px;color:var(--sub);margin-bottom:11px}}
.ts-badge{{font-size:12px;font-weight:700;padding:2px 8px;border-radius:5px;display:inline-block}}
.ts-badge.red{{background:var(--red-lt);color:var(--red)}}
.ts-badge.blue{{background:var(--brand-lt);color:var(--brand)}}
.ts-summary{{font-size:14px;line-height:1.7;color:var(--m);
  border-left:2px solid rgba(0,0,0,.15);padding-left:11px;margin-bottom:12px}}
.ts-summary p{{margin:4px 0;color:var(--m)}}
.ts-summary ul{{padding-left:1.4em;margin:4px 0}}
.ts-summary li{{margin-bottom:3px;color:var(--m)}}
.ts-link{{font-size:14px;font-weight:700;text-decoration:none;
  display:flex;justify-content:flex-end;align-items:center;gap:2px}}

/* ── セクション区切り ── */
.sec-divider{{display:flex;align-items:center;gap:10px;padding:20px 16px 8px}}
.sec-divider-label{{font-size:12px;font-weight:700;letter-spacing:.06em;
  color:var(--sub);white-space:nowrap}}
.sec-divider::after{{content:'';flex:1;height:.5px;background:var(--b)}}

/* ── 記事行 ── */
.article{{display:flex;gap:12px;padding:13px 16px;min-height:48px;
  align-items:flex-start;text-decoration:none;
  border-top:.5px solid var(--b);
  transition:background .12s ease,transform .08s ease}}
.article:active{{background:#F2F2F7;transform:scale(.99)}}
.art-body{{flex:1;min-width:0}}
.art-title{{font-size:16px;font-weight:600;line-height:1.55;color:var(--t2);
  margin-bottom:5px;display:block;word-break:break-all}}
.art-meta{{font-size:13px;color:var(--sub);display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.art-label{{font-size:12px;font-weight:700;padding:2px 8px;border-radius:5px;
  display:inline-block;margin-right:4px}}
.art-label.urgent{{background:var(--red-lt);color:var(--red)}}
.art-badge{{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px;display:inline-block}}
.art-badge.red{{background:var(--red-lt);color:var(--red)}}
.art-badge.blue{{background:var(--brand-lt);color:var(--brand)}}
.art-chevron{{color:#C7C7CC;font-size:18px;flex-shrink:0;align-self:center;font-weight:300}}

/* ── 空メッセージ ── */
.empty-msg{{color:var(--sub);font-size:14px;text-align:center;padding:36px 16px}}

/* ── ボトムナビ ── */
.bottomnav{{position:fixed;bottom:0;left:50%;transform:translateX(-50%);
  width:100%;max-width:768px;
  background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-top:.5px solid var(--b2);display:flex;z-index:20;
  padding-bottom:env(safe-area-inset-bottom)}}
.bnav-item{{flex:1;display:flex;flex-direction:column;align-items:center;
  padding:10px 4px 12px;gap:3px;cursor:pointer;position:relative;
  -webkit-tap-highlight-color:transparent}}
.bnav-icon{{font-size:22px;line-height:1}}
.bnav-label{{font-size:10px;font-weight:500;color:var(--sub)}}
.bnav-item.active .bnav-label{{color:var(--brand);font-weight:700}}
.bnav-item.active .bnav-icon::after{{content:'';position:absolute;bottom:-4px;left:50%;
  transform:translateX(-50%);width:4px;height:4px;border-radius:50%;background:var(--brand)}}
.bnav-badge{{position:absolute;top:5px;right:calc(50% - 22px);
  background:var(--red);color:#fff;font-size:10px;font-weight:700;
  min-width:17px;height:17px;border-radius:99px;
  display:flex;align-items:center;justify-content:center;padding:0 4px}}

/* ── FAB ── */
.fab{{position:fixed;bottom:84px;right:16px;z-index:19;
  width:40px;height:40px;border-radius:50%;
  background:rgba(255,255,255,.9);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  border:.5px solid var(--b);box-shadow:0 2px 10px rgba(0,0,0,.12);
  display:flex;align-items:center;justify-content:center;
  font-size:18px;cursor:pointer;color:var(--m);
  opacity:0;pointer-events:none;transition:opacity .2s ease;
  -webkit-tap-highlight-color:transparent}}
.fab.show{{opacity:1;pointer-events:auto}}

/* PC: 最大幅制限でもきれいに */
@media(min-width:600px){{
  .top-story{{margin:20px 20px 0}}
  .sec-divider{{padding:20px 20px 8px}}
  .article{{padding:14px 20px}}
}}

/* Print */
@media print{{
  .hd,.bottomnav,.fab{{display:none!important}}
  .tab-content{{display:block!important}}
  body{{max-width:none}}
}}
</style>
</head>
<body>

<!-- ヘッダー -->
<div class="hd">
  <div class="hd-date">{date_str}</div>
  <div class="hd-right">
    <a class="chip alert" href="#tab-hita">{"⚠️ 要確認 " + str(cnt_priority) if cnt_priority else "📭 新着なし"}</a>
    <span class="chip gray">新着 {cnt_hita + cnt_ib + cnt_nat + cnt_sub}</span>
  </div>
</div>

<!-- ── ひたちなか市 ── -->
<div class="tab-content active" id="tab-hita">
  <div class="page-sheet" id="tab-hita-sheet">
{hita_html}
  </div>
</div>

<!-- ── 茨城 ── -->
<div class="tab-content" id="tab-ibaraki">
  <div class="page-sheet">
{ib_html}
  </div>
</div>

<!-- ── 国政・国会 ── -->
<div class="tab-content" id="tab-national">
  <div class="page-sheet">
{nat_html}
  </div>
</div>

<!-- ── 補助金 ── -->
<div class="tab-content" id="tab-subsidy">
  <div class="page-sheet">
{sub_html}
  </div>
</div>

<!-- ── ボトムナビ ── -->
<div class="bottomnav">
  <div class="bnav-item active" onclick="sw('tab-hita',this)">
    {hita_bdg}
    <div class="bnav-icon">🏛️</div>
    <div class="bnav-label">ひたちなか</div>
  </div>
  <div class="bnav-item" onclick="sw('tab-ibaraki',this)">
    <div class="bnav-icon">🗞️</div>
    <div class="bnav-label">茨城</div>
  </div>
  <div class="bnav-item" onclick="sw('tab-national',this)">
    <div class="bnav-icon">🏢</div>
    <div class="bnav-label">国政・国会</div>
  </div>
  <div class="bnav-item" onclick="sw('tab-subsidy',this)">
    {sub_bdg}
    <div class="bnav-icon">💰</div>
    <div class="bnav-label">補助金</div>
  </div>
</div>

<!-- ── FAB ── -->
<div class="fab" id="fab" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</div>

<script>
function sw(id,btn){{
  document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active')}});
  document.querySelectorAll('.bnav-item').forEach(function(b){{b.classList.remove('active')}});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  window.scrollTo({{top:0,behavior:'smooth'}});
  location.hash=id;
}}
(function(){{
  var h=location.hash.replace('#','');
  var ids=['tab-hita','tab-ibaraki','tab-national','tab-subsidy'];
  var idx=ids.indexOf(h);
  if(idx<0)return;
  document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active')}});
  document.querySelectorAll('.bnav-item').forEach(function(b){{b.classList.remove('active')}});
  document.getElementById(h).classList.add('active');
  document.querySelectorAll('.bnav-item')[idx].classList.add('active');
}})();
window.addEventListener('scroll',function(){{
  document.getElementById('fab').classList.toggle('show',window.scrollY>200);
}});
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

    # 省庁スクレイピング（月別プレスリリース）
    print("省庁スクレイピング中...")
    for src_cfg in MINISTRY_SCRAPE_SOURCES:
        scraped = fetch_scraped_ministry(src_cfg, seen=seen, max_items=10)
        national_sources.append({"name": src_cfg["name"], "items": scraped})
        print(f"  {src_cfg['name']}: {len(scraped)}件（新着）")

    # 国政・県政: AI要約
    has_national = any(s["items"] for s in national_sources)
    national_cards_by_source = {}
    if has_national:
        national_cards_by_source = process_national_batch(national_sources)

    # ===== 補助金・助成金情報 =====
    print("補助金・助成金 RSS取得中...")
    subsidy_sources = []
    for name, url in SUBSIDY_RSS_SOURCES:
        items_feed = fetch_generic_rss(url, max_items=15)
        subsidy_sources.append({"name": name, "items": items_feed})
        print(f"  {name}: {len(items_feed)}件")

    # 既存ソースからキーワードフィルタ
    seen_sub_links = set()
    filtered_sub = []
    for s in national_sources:
        for item in s["items"]:
            if any(kw in item["title"] for kw in SUBSIDY_KEYWORDS) and item["link"] not in seen_sub_links:
                seen_sub_links.add(item["link"])
                filtered_sub.append({**item, "_source_from": s["name"]})
    if filtered_sub:
        subsidy_sources.append({"name": "関連情報（省庁フィルタ）", "items": filtered_sub})
        print(f"  省庁フィルタ: {len(filtered_sub)}件")

    subsidy_cards_by_source = {}
    if any(s["items"] for s in subsidy_sources):
        print("補助金・助成金 AI要約中...")
        subsidy_cards_by_source = process_national_batch(subsidy_sources, ai_category="補助金・助成金")

    # 国会会議録API
    print("国会会議録API 検索中...")
    kokkai_speeches = fetch_kokkai_speeches(days_back=14)
    print(f"  国会会議録: {len(kokkai_speeches)}件")

    # ===== HTML生成（常時） =====
    html = build_html(
        gikai_cards, important_cards, minor_24h, now,
        ib_local_cards, ib_digest, ib_other,
        national_sources, national_cards_by_source,
        kokkai_speeches,
        subsidy_sources, subsidy_cards_by_source
    )
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"✓ {HTML_FILE} 生成完了")

    for i in new_items: mark_seen(i["link"], seen)
    for i in ib_new:    mark_seen(i["link"], seen)
    # スクレイピング記事もseen.jsonに追加（翌日以降の重複防止）
    for s in national_sources:
        if s["name"] in {c["name"] for c in MINISTRY_SCRAPE_SOURCES}:
            for item in s["items"]:
                mark_seen(item["link"], seen)
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

