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


def build_html(gikai_cards, important_cards, minor_items, generated_at,
               ibaraki_local_cards=None, ibaraki_digest="", ibaraki_all=None,
               national_sources=None, national_cards_by_source=None,
               kokkai_speeches=None,
               subsidy_sources=None, subsidy_cards_by_source=None):
    jst = generated_at + timedelta(hours=9)
    date_str = jst.strftime("%Y年%m月%d日 %H:%M")

    PREF_NAMES = {"茨城県 注目情報", "茨城県 防災情報"}
    pref_sources = [s for s in (national_sources or []) if s["name"] in PREF_NAMES]
    govt_sources = [s for s in (national_sources or []) if s["name"] not in PREF_NAMES]

    cnt_hita = len(gikai_cards) + len(important_cards) + len(minor_items)
    cnt_ib   = len(ibaraki_local_cards or []) + len(ibaraki_all or [])
    cnt_pref = sum(len(s["items"]) for s in pref_sources)
    cnt_nat  = sum(len(s["items"]) for s in govt_sources)
    cnt_sub  = sum(len(s["items"]) for s in (subsidy_sources or []))
    cnt_kok  = len(kokkai_speeches or [])

    def badge(n, bg):
        return f'<span class="tbadge" style="background:{bg}">{n}</span>' if n else ''

    _ci = [0]
    def card(title, link, summary_html, accent, tag, tag_bg, extra=""):
        i = _ci[0]; _ci[0] += 1
        cid = f"c{i}"
        has = bool(summary_html and summary_html.strip())
        hcls = "card-head ex" if has else "card-head"
        hx   = f' data-t="{cid}" onclick="tgl(this)"' if has else ''
        chev = '<span class="chev">›</span>' if has else ''
        body = f'<div class="card-body" id="{cid}" hidden>{summary_html}</div>' if has else ''
        return f'''<div class="card" style="--ac:{accent}">
  <div class="{hcls}"{hx}>
    <div class="ctitle"><a href="{esc(link)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{esc(title)}</a></div>
    <div class="cacts">{chev}<a href="{esc(link)}" target="_blank" rel="noopener" class="bout" onclick="event.stopPropagation()">↗</a></div>
  </div>
  <div class="cmeta"><span class="ctag" style="background:{tag_bg}">{esc(tag)}</span>{extra}</div>
  {body}
</div>'''

    def cards(lst, accent, tag, tag_bg, empty=""):
        if not lst:
            return f'<p class="empty">{esc(empty or f"本日の{tag}はありません")}</p>'
        return "".join(card(c["title"], c["link"], c["summary_html"], accent, tag, tag_bg) for c in lst)

    def minor_html():
        if not minor_items: return '<p class="empty">本日の更新情報はありません</p>'
        return '<div class="lbox">' + "".join(
            f'<div class="lrow"><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a></div>'
            for i in minor_items
        ) + '</div>'

    def ib_html(digest, arts):
        if not arts: return '<p class="empty">本日の茨城新聞ニュースはありません</p>'
        d = f'<div class="digest">{summary_to_html(digest)}</div>' if digest else ''
        return d + '<div class="lbox">' + "".join(
            f'<div class="lrow"><a href="{esc(a["link"])}" target="_blank" rel="noopener">{esc(a["title"])}</a></div>'
            for a in arts
        ) + '</div>'

    SOURCE_ICONS = {
        "茨城県 注目情報":"📌","茨城県 防災情報":"🚨",
        "首相官邸":"🏛️","総務省":"📋","内閣府 地方分権改革":"🏢",
        "農林水産省":"🌾","NHK 政治":"📺",
        "国土交通省":"🏗️","厚生労働省":"🏥","環境省":"🌿",
        "中小企業庁":"🏭","ミラサポplus":"💼","関連情報（省庁フィルタ）":"🔗",
        "Googleアラート":"🔔",
    }

    def src_group_html(group_defs, src_list, cards_map):
        if not src_list: return '<p class="empty">本日の情報はありません</p>'
        src_map = {s["name"]: s for s in src_list}
        html = ""; shown = set()
        for grp_id, grp_label, grp_color, grp_names in group_defs:
            total = sum(len(src_map[n]["items"]) for n in grp_names if n in src_map and src_map[n]["items"])
            if total == 0: continue
            shown.update(grp_names)
            open_attr = ' open' if not html else ''
            inner = ""
            for sname in grp_names:
                s = src_map.get(sname)
                if not s or not s["items"]: continue
                icon = SOURCE_ICONS.get(sname, "🔍")
                top_cards = cards_map.get(sname, [])
                rest = s["items"][len(top_cards):]
                inner += f'<div class="srcblk"><div class="srclbl">{icon} {esc(sname)}</div>'
                for c in top_cards:
                    inner += card(c["title"], c["link"], c["summary_html"], grp_color, sname, grp_color,
                                  extra='<span class="aibadge">✦ AI要約</span>')
                if rest:
                    inner += '<div class="lbox">'
                    for item in rest:
                        desc = f'<span class="ldesc">{esc(item["description"][:60])}…</span>' if item.get("description") else ""
                        inner += f'<div class="lrow"><a href="{esc(item["link"])}" target="_blank" rel="noopener">{esc(item["title"])}</a>{desc}</div>'
                    inner += '</div>'
                inner += '</div>'
            html += f'<details class="acc"{open_attr}><summary class="acc-s" style="--ac:{grp_color}"><span class="acc-l">{grp_label}</span><span class="acc-c">{total}件</span><span class="acc-v">›</span></summary><div class="acc-b">{inner}</div></details>'
        for s in src_list:
            if s["name"] not in shown and s["items"]:
                icon = SOURCE_ICONS.get(s["name"], "🔍"); color = "#546E7A"
                top_cards = cards_map.get(s["name"], [])
                inner = f'<div class="srcblk"><div class="srclbl">{icon} {esc(s["name"])}</div>'
                for c in top_cards:
                    inner += card(c["title"], c["link"], c["summary_html"], color, s["name"], color,
                                  extra='<span class="aibadge">✦ AI要約</span>')
                rest = s["items"][len(top_cards):]
                if rest:
                    inner += '<div class="lbox">' + "".join(
                        f'<div class="lrow"><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a></div>'
                        for i in rest
                    ) + '</div>'
                inner += '</div>'
                html += f'<details class="acc"><summary class="acc-s" style="--ac:{color}"><span class="acc-l">{icon} {esc(s["name"])}</span><span class="acc-c">{len(s["items"])}件</span><span class="acc-v">›</span></summary><div class="acc-b">{inner}</div></details>'
        return html or '<p class="empty">本日の情報はありません</p>'

    PREF_GROUPS = [
        ("pc", "📌 注目情報",  "#1B5E20", ["茨城県 注目情報"]),
        ("pb", "🚨 防災情報",  "#C62828", ["茨城県 防災情報"]),
    ]
    GOVT_GROUPS = [
        ("kg", "🏛️ 官邸・総務・内閣府", "#1565C0", ["首相官邸","総務省","内閣府 地方分権改革"]),
        ("mn", "🏗️ 省庁（農水・国交・厚労・環境）", "#4527A0", ["農林水産省","国土交通省","厚生労働省","環境省"]),
        ("md", "📺 NHK政治", "#C62828", ["NHK 政治"]),
    ]
    SUBSIDY_GROUPS = [
        ("sm", "🏭 中小企業庁",    "#5D4037", ["中小企業庁"]),
        ("mp", "💼 ミラサポplus", "#6D4C41", ["ミラサポplus"]),
        ("sf", "🔗 省庁フィルタ",  "#8D6E63", ["関連情報（省庁フィルタ）"]),
    ]

    def kokkai_html():
        if not kokkai_speeches:
            return '<p class="empty">過去14日間のひたちなか・茨城関連発言はありません</p>'
        html = ""
        for sp in kokkai_speeches:
            dd = sp["date"].replace("-", "/") if sp["date"] else ""
            extra = f'<span class="mpill">{esc(sp["house"])}</span><span class="mpill">{esc(sp["meeting"])}</span><span class="mpill">{esc(sp["speaker"])}</span>'
            sm = f'<p class="excerpt">{esc(sp["excerpt"])}</p>'
            html += card(f'{sp["meeting"]} — {sp["speaker"]} ({dd})',
                         sp["link"], sm, "#00695C", sp["keyword"], "#00695C", extra=extra)
        return html

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ひたちなか政治情報ダッシュボード | {date_str}</title>
<style>
:root{{
  --bg:#F3F4F6;--s:#fff;--s2:#F9FAFB;--b:#E5E7EB;--b2:#F0F2F5;
  --t:#111827;--t2:#374151;--m:#6B7280;--f:#9CA3AF;
  --sh1:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
  --sh2:0 4px 14px rgba(0,0,0,.11),0 2px 4px rgba(0,0,0,.06);
  --r:10px;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;background:var(--bg);color:var(--t);line-height:1.75;font-size:15px;-webkit-font-smoothing:antialiased}}

/* Header */
header{{background:linear-gradient(150deg,#0A1628 0%,#162B50 55%,#0A1628 100%);color:#fff;border-bottom:1px solid rgba(255,255,255,.06)}}
.hi{{max-width:900px;margin:0 auto;padding:16px 18px 13px}}
.hb{{display:flex;align-items:center;gap:10px;margin-bottom:7px}}
.hl{{width:34px;height:34px;background:linear-gradient(135deg,#3B82F6,#1D4ED8);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 2px 8px rgba(59,130,246,.3);flex-shrink:0}}
.ht{{font-size:17px;font-weight:800;letter-spacing:-.3px;line-height:1.2}}
.hm{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.hbg{{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.15);border-radius:20px;font-size:11px;padding:3px 10px;color:rgba(255,255,255,.8)}}
.hbg.live{{background:rgba(52,211,153,.15);border-color:rgba(52,211,153,.3);color:#6EE7B7}}

/* Tab bar */
.tb{{background:var(--s);border-bottom:2px solid var(--b);position:sticky;top:0;z-index:100;box-shadow:0 2px 10px rgba(0,0,0,.05)}}
.tbi{{max-width:900px;margin:0 auto;display:flex;overflow-x:auto;scrollbar-width:none}}
.tbi::-webkit-scrollbar{{display:none}}
.tbtn{{flex-shrink:0;padding:12px 14px;font-size:13px;font-weight:700;border:none;background:none;cursor:pointer;color:var(--m);border-bottom:3px solid transparent;margin-bottom:-2px;display:flex;align-items:center;gap:5px;white-space:nowrap;transition:color .18s,background .15s,border-color .18s;letter-spacing:-.1px}}
.tbtn:hover{{color:var(--t2);background:var(--b2)}}
.tbtn.active{{color:var(--tc,#1565C0);border-bottom-color:var(--tc,#1565C0);background:rgba(0,0,0,.015)}}
.tbadge{{font-size:10px;font-weight:800;padding:1px 6px;border-radius:10px;color:#fff;min-width:16px;text-align:center;line-height:1.5}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}

/* Container */
.wrap{{max-width:900px;margin:0 auto;padding:16px 15px 36px}}

/* Section head */
.sh{{display:flex;align-items:center;gap:8px;margin:24px 0 11px;padding-bottom:9px;border-bottom:2px solid currentColor}}
.sh h2{{font-size:12px;font-weight:800;letter-spacing:.8px;text-transform:uppercase}}
.sc{{font-size:10px;font-weight:800;padding:1px 7px;border-radius:10px;background:currentColor;color:#fff;opacity:.9;line-height:1.6}}

/* Card */
.card{{background:var(--s);border-radius:var(--r);margin-bottom:9px;box-shadow:var(--sh1);border-left:4px solid var(--ac,#ccc);overflow:hidden;transition:box-shadow .18s,transform .15s}}
.card:hover{{box-shadow:var(--sh2);transform:translateY(-1px)}}
.card-head{{padding:13px 14px 7px;display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.card-head.ex{{cursor:pointer}}
.card-head.ex:hover{{background:var(--s2)}}
.card-head.ex.expanded .chev{{transform:rotate(90deg);color:var(--ac)}}
.ctitle{{font-weight:700;font-size:14px;line-height:1.55;flex:1;min-width:0}}
.ctitle a{{color:var(--t);text-decoration:none;word-break:break-all}}
.ctitle a:hover{{color:#1565C0;text-decoration:underline}}
.cacts{{display:flex;align-items:center;gap:6px;flex-shrink:0;padding-top:1px}}
.chev{{font-size:19px;color:var(--f);transition:transform .22s,color .22s;line-height:1;flex-shrink:0;user-select:none}}
.bout{{font-size:11px;color:#1565C0;text-decoration:none;border:1px solid #BFDBFE;border-radius:6px;padding:3px 8px;background:#EFF6FF;white-space:nowrap;transition:background .15s;flex-shrink:0}}
.bout:hover{{background:#DBEAFE}}
.cmeta{{padding:0 14px 9px;display:flex;align-items:center;gap:5px;flex-wrap:wrap}}
.ctag{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:12px;color:#fff;letter-spacing:.2px;flex-shrink:0}}
.aibadge{{font-size:10px;font-weight:700;color:#7C3AED;background:#EDE9FE;padding:2px 7px;border-radius:10px;flex-shrink:0}}
.mpill{{font-size:11px;color:var(--m);background:var(--b2);padding:2px 7px;border-radius:8px;border:1px solid var(--b)}}
.card-body{{padding:4px 14px 14px;font-size:14px;line-height:1.78;border-top:1px solid var(--b)}}
.card-body p{{margin:8px 0 4px;color:var(--t2)}}
.card-body ul{{padding-left:1.3em;margin:6px 0}}
.card-body li{{margin-bottom:5px;color:var(--t2)}}
.excerpt{{color:var(--t2);font-style:italic;border-left:3px solid var(--b);padding-left:10px;margin:8px 0;font-size:13px}}

/* List box */
.lbox{{background:var(--s);border-radius:var(--r);overflow:hidden;box-shadow:var(--sh1);margin-bottom:9px}}
.lrow{{padding:10px 14px;border-bottom:1px solid var(--b);font-size:13px;display:flex;flex-direction:column;gap:3px}}
.lrow:last-child{{border-bottom:none}}
.lrow a{{color:var(--t);text-decoration:none;font-weight:600;line-height:1.5}}
.lrow a:hover{{color:#1565C0}}
.ldesc{{font-size:11px;color:var(--f)}}

/* Digest */
.digest{{background:linear-gradient(135deg,#F0FFF4,#ECFDF5);border:1px solid #86EFAC;border-radius:var(--r);padding:14px 16px;margin-bottom:10px;font-size:14px;line-height:1.78}}
.digest p{{margin-bottom:6px;color:var(--t2)}}
.digest ul{{padding-left:1.3em}}
.digest li{{margin-bottom:4px;color:var(--t2)}}

/* Accordion */
.acc{{background:var(--s);border-radius:var(--r);margin-bottom:8px;box-shadow:var(--sh1);border-left:4px solid var(--ac,#999);overflow:hidden}}
.acc-s{{list-style:none;padding:13px 15px;cursor:pointer;display:flex;align-items:center;gap:10px;user-select:none;font-weight:700;font-size:14px;transition:background .15s}}
.acc-s::-webkit-details-marker{{display:none}}
.acc-s:hover{{background:var(--s2)}}
.acc-l{{flex:1;color:var(--ac,#333)}}
.acc-c{{font-size:10px;font-weight:800;color:var(--ac);background:var(--b2);padding:2px 8px;border-radius:10px;border:1px solid var(--b)}}
.acc-v{{color:var(--ac,#999);font-size:17px;transition:transform .22s;line-height:1;flex-shrink:0}}
details[open] .acc-v{{transform:rotate(90deg)}}
.acc-b{{padding:0 10px 10px}}
.srcblk{{margin-top:8px}}
.srclbl{{font-size:11px;font-weight:800;color:var(--m);letter-spacing:.8px;text-transform:uppercase;padding:5px 4px 4px}}

/* Search */
.sbar{{padding:10px 0 4px}}
.sinput{{width:100%;padding:9px 14px;border:1px solid var(--b);border-radius:8px;font-size:14px;background:var(--s);color:var(--t);transition:border-color .15s,box-shadow .15s}}
.sinput:focus{{outline:none;border-color:#93C5FD;box-shadow:0 0 0 3px rgba(147,197,253,.2)}}
.hint{{font-size:12px;color:var(--f);margin-bottom:12px;padding:0 2px}}
.empty{{color:var(--m);font-size:13px;padding:20px 0;text-align:center}}

/* Footer */
footer{{background:#0A1628;color:rgba(255,255,255,.45);font-size:12px;padding:22px 20px;text-align:center;line-height:2.2}}
footer a{{color:rgba(255,255,255,.55);text-decoration:none}}
footer a:hover{{color:#fff}}

/* Mobile */
@media(max-width:600px){{
  .ht{{font-size:15px}}
  .card-head{{flex-direction:column;gap:5px}}
  .cacts{{align-self:flex-start}}
  .tbtn{{padding:11px 11px;font-size:12px}}
  .tbadge{{font-size:9px;padding:1px 5px}}
}}

/* Print */
@media print{{
  .tb,.cacts,.sbar{{display:none!important}}
  .tab-content{{display:block!important}}
  .card-body{{display:block!important}}
  .acc{{box-shadow:none;border:1px solid #ddd}}
  body{{font-size:12px}}
}}
</style>
</head>
<body>

<header>
<div class="hi">
  <div class="hb">
    <div class="hl">🗾</div>
    <h1 class="ht">ひたちなか政治情報ダッシュボード</h1>
  </div>
  <div class="hm">
    <span class="hbg live">● LIVE</span>
    <span class="hbg">更新: {date_str} JST</span>
    <span class="hbg">AI: Gemini Flash 自動要約</span>
  </div>
</div>
</header>

<div class="tb">
<div class="tbi">
  <button class="tbtn active" data-tc="#1565C0" onclick="sw('hitachinaka',this)">🏛️ ひたちなか市{badge(cnt_hita,"#1565C0")}</button>
  <button class="tbtn" data-tc="#2E7D32" onclick="sw('ibaraki',this)">🗞️ 茨城新聞{badge(cnt_ib,"#2E7D32")}</button>
  <button class="tbtn" data-tc="#1B5E20" onclick="sw('pref',this)">🌿 茨城県政{badge(cnt_pref,"#1B5E20")}</button>
  <button class="tbtn" data-tc="#4527A0" onclick="sw('national',this)">🏢 国政・省庁{badge(cnt_nat,"#4527A0")}</button>
  <button class="tbtn" data-tc="#5D4037" onclick="sw('subsidy',this)">💰 補助金{badge(cnt_sub,"#5D4037")}</button>
  <button class="tbtn" data-tc="#00695C" onclick="sw('kokkai',this)">📜 国会{badge(cnt_kok,"#00695C")}</button>
</div>
</div>

<!-- ひたちなか市タブ -->
<div id="hitachinaka" class="tab-content active">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 このタブ内を検索..." oninput="filt(this)" aria-label="検索"></div>

  <div class="sh" style="color:#C62828">
    <h2>🔴 議会情報</h2>
    <span class="sc" style="background:#C62828">{len(gikai_cards)}</span>
  </div>
  {cards(gikai_cards,"#C62828","議会","#C62828")}

  <div class="sh" style="color:#E65100">
    <h2>🟡 重要なお知らせ</h2>
    <span class="sc" style="background:#E65100">{len(important_cards)}</span>
  </div>
  {cards(important_cards,"#E65100","重要","#E65100")}

  <div class="sh" style="color:#546E7A">
    <h2>📋 その他（24時間以内）</h2>
    <span class="sc" style="background:#546E7A">{len(minor_items)}</span>
  </div>
  {minor_html()}
</div>
</div>

<!-- 茨城新聞タブ -->
<div id="ibaraki" class="tab-content">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 このタブ内を検索..." oninput="filt(this)" aria-label="検索"></div>

  <div class="sh" style="color:#1B5E20">
    <h2>📍 ひたちなか・那珂湊 関連</h2>
    <span class="sc" style="background:#1B5E20">{len(ibaraki_local_cards or [])}</span>
  </div>
  {cards(ibaraki_local_cards or [],"#1B5E20","茨城新聞","#1B5E20","本日のひたちなか関連記事はありません")}

  <div class="sh" style="color:#2E7D32">
    <h2>📰 本日の茨城ニュース</h2>
    <span class="sc" style="background:#2E7D32">{len(ibaraki_all or [])}</span>
  </div>
  {ib_html(ibaraki_digest, ibaraki_all or [])}
</div>
</div>

<!-- 茨城県政タブ -->
<div id="pref" class="tab-content">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 このタブ内を検索..." oninput="filt(this)" aria-label="検索"></div>
  <p class="hint">▶ をクリックして展開 ／ タイトル行をタップして AI 要約を表示</p>
  {src_group_html(PREF_GROUPS, pref_sources, national_cards_by_source or {})}
</div>
</div>

<!-- 国政・省庁タブ -->
<div id="national" class="tab-content">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 このタブ内を検索..." oninput="filt(this)" aria-label="検索"></div>
  <p class="hint">▶ をクリックして展開 ／ タイトル行をタップして AI 要約を表示</p>
  {src_group_html(GOVT_GROUPS, govt_sources, national_cards_by_source or {})}
</div>
</div>

<!-- 補助金・助成金タブ -->
<div id="subsidy" class="tab-content">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 補助金名・対象を検索..." oninput="filt(this)" aria-label="検索"></div>
  <p class="hint">中小企業庁・ミラサポplusの補助金・助成金情報 ／ タイトル行をタップして AI 要約を表示</p>
  {src_group_html(SUBSIDY_GROUPS, subsidy_sources or [], subsidy_cards_by_source or {})}
</div>
</div>

<!-- 国会会議録タブ -->
<div id="kokkai" class="tab-content">
<div class="wrap">
  <div class="sbar"><input class="sinput" type="search" placeholder="🔍 このタブ内を検索..." oninput="filt(this)" aria-label="検索"></div>
  <div class="sh" style="color:#00695C">
    <h2>📜 国会会議録（過去14日間）</h2>
    <span class="sc" style="background:#00695C">{cnt_kok}</span>
  </div>
  <p class="hint">キーワード: ひたちなか / 那珂湊 / 茨城県 地方自治</p>
  {kokkai_html()}
</div>
</div>

<footer>
  <div>自動生成ダッシュボード &nbsp;|&nbsp; {date_str} JST &nbsp;|&nbsp; Powered by Gemini Flash</div>
  <div style="margin-top:8px">
    <a href="https://www.city.hitachinaka.lg.jp/" target="_blank">ひたちなか市</a> ·
    <a href="https://ibarakinews.jp/" target="_blank">茨城新聞</a> ·
    <a href="https://www.pref.ibaraki.jp/" target="_blank">茨城県</a> ·
    <a href="https://www.kantei.go.jp/" target="_blank">首相官邸</a> ·
    <a href="https://www.soumu.go.jp/" target="_blank">総務省</a> ·
    <a href="https://www.chusho.meti.go.jp/" target="_blank">中小企業庁</a> ·
    <a href="https://mirasapo-plus.go.jp/" target="_blank">ミラサポplus</a>
  </div>
</footer>

<script>
var TABS = ['hitachinaka','ibaraki','pref','national','subsidy','kokkai'];

function sw(id, btn) {{
  document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active')}});
  document.querySelectorAll('.tbtn').forEach(function(b){{b.classList.remove('active');b.style.removeProperty('--tc')}});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  if(btn.dataset.tc) btn.style.setProperty('--tc', btn.dataset.tc);
  location.hash = id;
}}

(function(){{
  // 最初のアクティブボタンにカラー適用
  var first = document.querySelectorAll('.tbtn')[0];
  if(first && first.dataset.tc) first.style.setProperty('--tc', first.dataset.tc);
  var h = location.hash.replace('#','');
  if(!h) return;
  var el = document.getElementById(h);
  if(!el) return;
  document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active')}});
  document.querySelectorAll('.tbtn').forEach(function(b){{b.classList.remove('active');b.style.removeProperty('--tc')}});
  el.classList.add('active');
  var idx = TABS.indexOf(h);
  if(idx >= 0) {{
    var btn = document.querySelectorAll('.tbtn')[idx];
    btn.classList.add('active');
    if(btn.dataset.tc) btn.style.setProperty('--tc', btn.dataset.tc);
  }}
}})();

function tgl(head) {{
  var id = head.dataset.t;
  var body = document.getElementById(id);
  var isHidden = body.hidden;
  body.hidden = !isHidden;
  head.classList.toggle('expanded', isHidden);
}}

function filt(input) {{
  var q = input.value.toLowerCase().trim();
  var wrap = input.closest('.wrap');
  wrap.querySelectorAll('.card,.lrow,.acc').forEach(function(el) {{
    el.style.display = (!q || el.textContent.toLowerCase().includes(q)) ? '' : 'none';
  }});
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

