#!/usr/bin/env python3
"""
「口コミ投資法」監視ボット (Tier 1: 深掘り監視)

ウォッチリスト企業ごとに、
  1. 公式サイトの新商品・ニュース一覧を自動巡回して新着を検知
  2. 新着タイトルを新規キーワードとして自動でトラッキング対象に追加
  3. 各キーワード(社名+追跡中の商品名)についてGoogle Trends・
     Yahoo!リアルタイム検索(X投稿数の代替)の推移を追跡
  4. 過去の推移に対して急上昇(バズ)を検知したらDiscordに通知

X/TikTok/Instagramの公式APIは無料では使えない(有料化・アカデミック限定・
自社アカウント限定)ため使用していない。Google Trends・Yahoo!リアルタイム
検索はいずれも非公式(内部APIの直接叩き)であり、仕様変更で壊れる可能性が
ある点に留意。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "config" / "watchlist.json"
STATE_DIR = ROOT / "state"
SEEN_NEWS_PATH = STATE_DIR / "seen_news.json"
HISTORY_PATH = STATE_DIR / "history.json"
LAST_CHECKED_PATH = STATE_DIR / "last_checked.json"
BUSINESS_OVERVIEW_VERSION_PATH = STATE_DIR / "business_overview_versions.json"

EDINETDB_BASE = "https://edinetdb.jp/v1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

MAX_TRACKED_PRODUCT_KEYWORDS = 6  # 社名以外に追跡する商品キーワードの上限(API負荷抑制)
CHECK_INTERVAL_DAYS = 30  # 1社あたりのチェック間隔(月1回)
MAX_COMPANIES_PER_RUN = 10  # 1回の実行で処理する企業数の上限(将来300社規模でも1日10社に抑える)
MIN_HISTORY_FOR_SPIKE = 5  # 急上昇判定に必要な最低日数
BASELINE_WINDOW = 28  # 直近何日を基準値計算に使うか

# --- 急上昇判定の閾値 ---
TRENDS_RATIO_THRESHOLD = 2.5
TRENDS_ABS_JUMP_THRESHOLD = 40  # 0-100スケールでの絶対上昇幅
YAHOO_RATIO_THRESHOLD = 3.0


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def log(msg: str):
    print(f"[{datetime.datetime.now(JST):%H:%M:%S}] {msg}")


# ============================================================
# ニュースソース(新商品検知)
# ============================================================


@dataclass
class NewsItem:
    title: str
    url: str
    date: str
    categories: list = field(default_factory=list)


def fetch_rss_news(url: str) -> list[NewsItem]:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    text = resp.text
    items = []
    for m in re.finditer(r"<item[^>]*>(.*?)</item>", text, re.S):
        block = m.group(1)
        title_m = re.search(r"<title>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</title>", block, re.S)
        link_m = re.search(r"<link>(.*?)</link>", block, re.S)
        date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if not title_m or not link_m:
            continue
        title = nfkc(title_m.group(1) or title_m.group(2) or "")
        categories = [
            nfkc(c) for c in re.findall(r"<category><!\[CDATA\[(.*?)\]\]></category>", block)
        ]
        items.append(
            NewsItem(
                title=title,
                url=link_m.group(1).strip(),
                date=(date_m.group(1).strip() if date_m else ""),
                categories=categories,
            )
        )
    return items


def fetch_ibloom_news(url: str) -> list[NewsItem]:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    html = resp.text
    items = []
    for m in re.finditer(
        r'<li><a href="([^"]+)"[^>]*>.*?<div class="ttl">(.*?)</div>.*?<div class="date">([^<]*)</div>',
        html,
        re.S,
    ):
        link, title, date = m.groups()
        items.append(NewsItem(title=nfkc(title), url=link.strip(), date=nfkc(date)))
    return items


def fetch_jbg_topics(url: str) -> list[NewsItem]:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    html = resp.text
    items = []
    for m in re.finditer(
        r"location\.href='([^']+)'[^>]*>\s*<span class=\"news-date\">([^<]*)</span>.*?"
        r'<span class="news-title">(.*?)</span>',
        html,
        re.S,
    ):
        link, date, title = m.groups()
        title = re.sub(r"<[^>]+>", "", title)
        items.append(NewsItem(title=nfkc(title), url=link.strip(), date=nfkc(date)))
    return items


def fetch_html_regex_news(url: str, pattern: str) -> list[NewsItem]:
    """企業ごとに個別パーサー関数を書く代わりに、watchlist.json側に正規表現
    (名前付きグループ url/title/date、dateは任意)を持たせて汎用的にニュース
    一覧をパースする。企業サイトの構造が多種多様(WordPress/独自CMS/SPA等)で、
    サイトの数だけPythonの関数を増やすと保守が破綻するため、この方式にした。
    """
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    # サイトによってShift-JIS/EUC-JP等UTF-8以外のエンコーディングを使うことがあるため、
    # HTTPヘッダ/HTMLのcharset宣言からrequestsが推定したエンコーディングを尊重する
    # (決め打ちでutf-8にすると文字化けする)。
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    html = resp.text
    items = []
    for m in re.finditer(pattern, html, re.S):
        gd = m.groupdict()
        link = (gd.get("url") or "").strip()
        title = nfkc(re.sub(r"<[^>]+>", "", gd.get("title") or ""))
        date = nfkc(re.sub(r"<[^>]+>", "", gd.get("date") or ""))
        if not link or not title:
            continue
        link = urllib.parse.urljoin(url, link)  # 相対パス(../a.php等)も含めて絶対URL化
        items.append(NewsItem(title=title, url=link, date=date))
    return items


NEWS_FETCHERS = {
    "rss": fetch_rss_news,
    "html_list_ibloom": fetch_ibloom_news,
    "html_list_jbg": fetch_jbg_topics,
}


TITLE_STRIP_PATTERNS = [
    r"のお知らせ$",
    r"に関するお知らせ$",
    r"についてのお知らせ$",
    r"のご案内$",
    r"を発売$",
    r"新発売$",
    r"^【[^】]*】",
]


TAG_RE = re.compile(r"<[^>]+>")


def title_to_keyword(title: str) -> str:
    """ニュースタイトルから検索キーワードらしき部分を抽出する簡易ヒューリスティック(フォールバック用)"""
    kw = TAG_RE.sub("", title)
    for pat in TITLE_STRIP_PATTERNS:
        kw = re.sub(pat, "", kw)
    kw = kw.strip(" 　「」『』:：")
    return kw[:40] if kw else title[:40]


ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


GROWTH_CATEGORIES = ("新商品", "新店舗", "新業態", "新販路", "コラボ", "その他")


def extract_keyword_with_ai(
    title: str, company_name: str, categories: list | None = None
) -> tuple[str, str] | None:
    """Claude APIでニュースタイトルを判定し、業績インパクトが期待できる内容なら
    (種別, 検索キーワード) を返す。決算・人事・定型お知らせなど業績に直接
    関係なさそうな内容であれば None を返す(=バズ監視の対象に追加しない)。
    ANTHROPIC_API_KEY が未設定の場合は簡易ヒューリスティックにフォールバックする
    (この場合は種別を「その他」として扱う)。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        kw = title_to_keyword(title)
        return ("その他", kw) if kw else None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 50,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"企業「{company_name}」が公式サイトに出した新着情報のタイトル:\n"
                            f"「{title}」\n"
                            + (f"付随するタグ情報: {', '.join(categories)}\n" if categories else "")
                            + "\nこの内容は、売上や株価にプラスの影響を与えうる出来事ですか？\n"
                            "該当例: 新商品・新シリーズの発売、新店舗のオープン、新しい業態・"
                            "コンセプト店の展開、新しい販路への進出(コンビニ・量販店・海外展開・"
                            "ECモール出店等)、話題性のあるコラボ、SNSでバズりそうな企画・"
                            "キャンペーン。\n\n"
                            "該当する場合は次の形式で1行だけ出力してください(説明・前置き不要):\n"
                            "種別|検索キーワード\n"
                            "種別は 新商品/新店舗/新業態/新販路/コラボ/その他 のいずれか。\n"
                            "検索キーワードは、実際にSNSや検索でユーザーが使いそうな自然な言葉に"
                            "してください。固有名詞(キャラクター名・シリーズ名・店舗名)だけだと"
                            "他の意味と混ざって検索精度が落ちる場合は、商品カテゴリ語"
                            "(シール/ぬいぐるみ/フィギュア/マスキングテープ 等)を1語添えてください。"
                            "例: 「はらぺこあおむし」だけでなく「はらぺこあおむし シール」。\n\n"
                            "該当しない場合(決算発表、人事異動、システムメンテナンス、"
                            "定型の事務連絡など業績に直接関係なさそうな内容)は"
                            "「NONE」とだけ出力してください。"
                        ),
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        if not text or text.upper().startswith("NONE"):
            return None
        if "|" in text:
            category, keyword = text.split("|", 1)
            category = category.strip()
            if category not in GROWTH_CATEGORIES:
                category = "その他"
        else:
            category, keyword = "その他", text
        keyword = keyword.strip(" 　「」『』:：")[:40]
        return (category, keyword) if keyword else None
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] AI keyword extraction failed, falling back to heuristic: {type(e).__name__}: {e}")
        kw = title_to_keyword(title)
        return ("その他", kw) if kw else None


# ============================================================
# EDINET DB: 有価証券報告書「事業の内容」から主力ブランド・事業名を発見
# 新しい報告書が出た時だけ再抽出する(毎月同じ書類を読み直す無駄を避けるため、
# latest_fiscal_year が前回と変わっていない限りスキップする)
# ============================================================


def get_edinetdb_fiscal_year(edinet_code: str, api_key: str) -> int | None:
    try:
        r = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}",
            headers={"X-API-Key": api_key},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["data"].get("latest_fiscal_year")
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] EDINET DB company lookup failed: {type(e).__name__}: {e}")
        return None


def get_edinetdb_business_overview(edinet_code: str, api_key: str) -> str | None:
    """「事業の内容」は抽象的な事業区分の表現に留まることが多く、消費者向けの
    通称ブランド名(例:「アピナ」)が出てこない場合がある。一方「沿革」は
    店舗・ブランドの開業を実名で年表形式で語ることが多いため、両方を結合して
    AIに読ませることで実際のブランド名を拾いやすくする。
    """
    try:
        r = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}/text-blocks",
            headers={"X-API-Key": api_key},
            params={"full": "true", "sections": "business-overview,history"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()["data"]
        if not data:
            return None
        return "\n\n".join(f"【{item['section']}】\n{item['text']}" for item in data)
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] EDINET DB business-overview fetch failed: {type(e).__name__}: {e}")
        return None


def discover_segment_keywords_with_ai(business_overview: str, company_name: str) -> list[str]:
    """有報の「事業の内容」全文から、業績インパクトが大きい主力事業・ブランド名を
    複数抽出する(新商品ニュースの巡回では拾えない、既存の稼ぎ頭を見逃さないため)。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 200,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"企業「{company_name}」の有価証券報告書「事業の内容」「沿革」全文"
                            "(末尾に公式サイトトップページの外部リンク一覧が付くこともあります):\n"
                            f"{business_overview}\n\n"
                            "外部リンク一覧がある場合、そのリンクの説明文(alt属性等)には英語の"
                            "業種名(例:「BATTING CENTER」「BOWLING」)と固有ブランド名"
                            "(例:「iBLOOM」)が混在していることがあります。英語の一般的な業種名は"
                            "除外し、固有ブランド名だけを候補にしてください。\n\n"
                            "この中から、この企業に固有の(=これで検索すれば他社ではなくこの企業の"
                            "話題がヒットする)ブランド名・屋号・具体的な商品(シリーズ)名を、"
                            "SNSやGoogle検索で実際に使われそうな自然な言葉で最大5個抽出してください。\n\n"
                            "判定基準は「一般名詞っぽい響きかどうか」ではなく「その言葉で検索した時に"
                            "この企業の話題が中心にヒットするか、無関係な他社の話題ばかりヒットするか」"
                            "です。たとえ普通の単語の組み合わせに見えても、この企業が独自に展開する"
                            "特定の商品・店舗を指す言葉なら含めてください"
                            "(例:「3Dアイス」はGold Starが独自に販売する具体的な商品名なので含める。"
                            "一方「アイスクリーム」は業界全体を指す一般名詞なので含めない)。\n\n"
                            "【絶対に含めないもの】\n"
                            "・業界・施設タイプそのものを指す一般名詞(例:「バッティングセンター」"
                            "「ボウリング場」「蓄電池システム」)。これは特定の店舗ブランド名"
                            "(例:「アピナ」)ではなく、どの会社の同種施設・製品にも当てはまる"
                            "言葉だからです。\n"
                            "・抽象的すぎるセグメント名(例:「アミューズメント施設運営事業」)。\n\n"
                            "【含めるべきものの例】\n"
                            "・固有の屋号・ブランド名(例:「アピナ」「MOOOSH」「361°」)\n"
                            "・この企業が独自に展開する具体的な商品(シリーズ)名"
                            "(例:「3Dアイス」「3Dフルーツアイス」)\n"
                            "・企業名を含む具体的な店舗ブランド名(例:「トレーディングカードピット」)\n"
                            "・子会社名(例:「Gold Star」)\n\n"
                            "「沿革」には同じ店舗ブランドの出店(例:「アピナ○○店」)が何十件も"
                            "繰り返し登場することがありますが、それらは1つの代表的なブランド名"
                            "(例:「アピナ」)にまとめて1個としてください。個別の店舗名・地名を"
                            "そのまま列挙しないでください。\n"
                            "固有名詞が1つも見つからなければ、無理に一般名詞を出力せず"
                            "「NONE」としてください。\n"
                            "1行1キーワードで出力し、他の説明は一切不要です。"
                        ),
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if not text or text.upper().startswith("NONE"):
            return []
        keywords = [line.strip(" 　「」『』:：・-") for line in text.splitlines()]
        return [k[:40] for k in keywords if k]
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] segment keyword discovery failed: {type(e).__name__}: {e}")
        return []


def fetch_homepage_brand_links(homepage_url: str) -> str:
    """公式トップページが外部サイト(子会社・ブランドサイト)にリンクしている箇所を、
    リンクテキストやimgのalt属性と合わせて抽出する。持株会社・多角化企業のトップページは
    各ブランドサイトへの導線をバナー等で並べていることが多く、有報の本文には出てこない
    消費者向けブランド名(例:「iBLOOM」)を拾える。
    """
    try:
        resp = requests.get(homepage_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        html = resp.text
        own_domain = urllib.parse.urlparse(homepage_url).netloc
        lines = []
        for m in re.finditer(r'<a\s+[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
            url, inner = m.groups()
            domain = urllib.parse.urlparse(url).netloc
            if not domain or domain == own_domain:
                continue
            alt_m = re.search(r'alt="([^"]*)"', inner)
            label = nfkc(alt_m.group(1)) if alt_m else nfkc(re.sub(r"<[^>]+>", "", inner))
            if label:
                lines.append(f"{label} ({url})")
        seen: set[str] = set()
        uniq = []
        for l in lines:
            if l not in seen:
                seen.add(l)
                uniq.append(l)
        return "\n".join(uniq[:30])
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] homepage brand link fetch failed: {type(e).__name__}: {e}")
        return ""


def discover_new_segment_keywords(company: dict, versions: dict) -> list[str]:
    """latest_fiscal_yearが前回チェック時から変わっていれば(=新しい有報が出ていれば)
    事業内容・沿革・公式トップページの外部リンクを読み直してキーワード候補を返す。
    変わっていなければ何もしない(公式トップページの内容は有報と無関係に変わりうるが、
    ブランド構成が月次で変わることは稀なので、簡易的に同じ変更検知条件に相乗りしている)。
    """
    edinet_code = company.get("edinet_code")
    edinetdb_key = os.environ.get("EDINETDB_API_KEY")
    if not edinet_code or not edinetdb_key:
        return []
    code = company["code"]
    fy = get_edinetdb_fiscal_year(edinet_code, edinetdb_key)
    if fy is None:
        return []
    if versions.get(code) == fy:
        log(f"  [SEGMENT] business overview unchanged (FY{fy}), skipping re-extraction")
        return []
    log(f"  [SEGMENT] new filing detected (FY{versions.get(code)} -> FY{fy}), re-reading business overview")
    versions[code] = fy
    overview = get_edinetdb_business_overview(edinet_code, edinetdb_key) or ""
    homepage_url = company.get("homepage_url")
    if homepage_url:
        brand_links = fetch_homepage_brand_links(homepage_url)
        if brand_links:
            overview += "\n\n【公式サイトのトップページからリンクされている外部サイト】\n" + brand_links
    if not overview.strip():
        return []
    return discover_segment_keywords_with_ai(overview, company["name"])


# ============================================================
# Google Trends (非公式・直接API叩き)
# ============================================================


class TrendsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._bootstrapped = False

    def _bootstrap(self):
        if self._bootstrapped:
            return
        try:
            self.session.get("https://trends.google.com/?geo=JP", timeout=20)
        except Exception:
            pass
        self._bootstrapped = True

    def _get_with_retry(self, url: str, params: dict, label: str):
        for attempt in range(3):
            r = self.session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                log(f"  [WARN] {label} 429, retrying in {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            log(f"  [WARN] {label} failed: {r.status_code}")
            return None
        return None

    def get_interest(self, keyword: str, timeframe: str = "today 3-m", geo: str = "JP") -> int | None:
        self._bootstrap()
        try:
            explore_url = "https://trends.google.com/trends/api/explore"
            req_payload = {
                "comparisonItem": [{"keyword": keyword, "geo": geo, "time": timeframe}],
                "category": 0,
                "property": "",
            }
            params = {"hl": "ja", "tz": "-540", "req": json.dumps(req_payload)}
            r = self._get_with_retry(explore_url, params, f"trends explore ({keyword})")
            if r is None:
                return None
            data = json.loads(r.text[5:])
            widget = next((w for w in data["widgets"] if w["id"] == "TIMESERIES"), None)
            if widget is None:
                return None
            token = widget["token"]
            req2 = widget["request"]
            time.sleep(2.5)
            multiline_url = "https://trends.google.com/trends/api/widgetdata/multiline"
            params2 = {"hl": "ja", "tz": "-540", "req": json.dumps(req2), "token": token}
            r2 = self._get_with_retry(multiline_url, params2, f"trends multiline ({keyword})")
            if r2 is None:
                return None
            data2 = json.loads(r2.text[5:])
            timeline = data2["default"]["timelineData"]
            if not timeline:
                return None
            return int(timeline[-1]["value"][0])
        except Exception as e:  # noqa: BLE001
            log(f"  [WARN] trends error ({keyword}): {type(e).__name__}: {e}")
            return None


# ============================================================
# Yahoo!リアルタイム検索 (X投稿数の非公式代替)
# ============================================================


def get_yahoo_realtime_count(keyword: str) -> int | None:
    """投稿頻度(1時間あたりの投稿数)を「直近の投稿ボリューム」の代理指標として使う。
    タイムラインの件数は1ページあたり最大40件程度で頭打ちになるため、
    件数そのものではなく「返ってきた投稿が何分間に収まっているか」から
    密度(投稿/時間)を逆算する。値は整数(件/時間 ×10)に丸めて保存する。
    """
    try:
        url = f"https://search.yahoo.co.jp/realtime/search/{urllib.parse.quote(keyword)}"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if resp.status_code != 200:
            log(f"  [WARN] yahoo realtime failed ({keyword}): {resp.status_code}")
            return None
        resp.encoding = "utf-8"
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        timeline = data["props"]["pageProps"]["pageData"].get("timeline", {})
        entries = timeline.get("entry", [])
        count = len(entries)
        if count == 0:
            return 0
        timestamps = [e["createdAt"] for e in entries if e.get("createdAt")]
        if len(timestamps) < 2:
            return count * 10  # 1件しかない場合は概算スコアとして件数のみ使う
        span_hours = max((max(timestamps) - min(timestamps)) / 3600.0, 1 / 60)
        rate_per_hour = count / span_hours
        return round(rate_per_hour * 10)  # 少数を保持するため10倍して整数化
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] yahoo realtime error ({keyword}): {type(e).__name__}: {e}")
        return None


# ============================================================
# State
# ============================================================


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 急上昇判定
# ============================================================


def detect_spike(values_by_date: dict[str, dict], today: str) -> dict | None:
    """values_by_date: {date: {"trends": int|None, "yahoo": int|None}}"""
    dates_sorted = sorted(d for d in values_by_date if d < today)
    if len(dates_sorted) < MIN_HISTORY_FOR_SPIKE:
        return None
    recent_dates = dates_sorted[-BASELINE_WINDOW:]

    today_v = values_by_date.get(today, {})
    result = {}

    trends_hist = [values_by_date[d]["trends"] for d in recent_dates if values_by_date[d].get("trends") is not None]
    if trends_hist and today_v.get("trends") is not None:
        baseline = sorted(trends_hist)[len(trends_hist) // 2]  # median
        latest = today_v["trends"]
        ratio = latest / max(baseline, 5)
        jump = latest - baseline
        if ratio >= TRENDS_RATIO_THRESHOLD or jump >= TRENDS_ABS_JUMP_THRESHOLD:
            result["trends"] = {"latest": latest, "baseline": baseline, "ratio": round(ratio, 2)}

    yahoo_hist = [values_by_date[d]["yahoo"] for d in recent_dates if values_by_date[d].get("yahoo") is not None]
    if yahoo_hist and today_v.get("yahoo") is not None:
        baseline = sorted(yahoo_hist)[len(yahoo_hist) // 2]
        latest = today_v["yahoo"]
        ratio = latest / max(baseline, 3)
        if ratio >= YAHOO_RATIO_THRESHOLD:
            result["yahoo"] = {"latest": latest, "baseline": baseline, "ratio": round(ratio, 2)}

    return result or None


# ============================================================
# Discord
# ============================================================


def send_discord(webhook_url: str, lines: list[str]):
    content = "\n".join(lines)
    chunks = []
    cur = ""
    for line in content.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        if resp.status_code >= 300:
            print(f"[WARN] Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)


# ============================================================
# メイン
# ============================================================


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    today_date = datetime.datetime.now(JST).date()
    watchlist = load_json(WATCHLIST_PATH, [])
    seen_news = load_json(SEEN_NEWS_PATH, {})  # {code: [url, ...]}
    history = load_json(HISTORY_PATH, {})  # {code: {keyword: {date: {trends, yahoo}}}}
    tracked_keywords = load_json(STATE_DIR / "tracked_keywords.json", {})  # {code: [keyword, ...]}
    last_checked = load_json(LAST_CHECKED_PATH, {})  # {code: "YYYY-MM-DD"}
    bo_versions = load_json(BUSINESS_OVERVIEW_VERSION_PATH, {})  # {code: fiscal_year}

    # --- ローテーション選定: 1社あたり月1回、1回の実行で最大MAX_COMPANIES_PER_RUN社まで ---
    # (今は3社だが、将来300社規模になっても1日あたりの負荷を一定に保つための仕組み)
    def days_since_checked(company):
        last = last_checked.get(company["code"])
        if last is None:
            return 10**9  # 未チェックの企業を最優先
        return (today_date - datetime.date.fromisoformat(last)).days

    due = [c for c in watchlist if days_since_checked(c) >= CHECK_INTERVAL_DAYS]
    due.sort(key=days_since_checked, reverse=True)
    todays_batch = due[:MAX_COMPANIES_PER_RUN]
    skipped = len(watchlist) - len(todays_batch)
    log(
        f"[ROTATION] {len(watchlist)} companies total, {len(due)} due, "
        f"processing {len(todays_batch)} today ({skipped} skipped)"
    )

    trends_client = TrendsClient()
    discord_lines: list[str] = []
    new_product_lines: list[str] = []

    for company in todays_batch:
        code = company["code"]
        name = company["name"]
        log(f"=== {name} ({code}) ===")

        is_first_run = code not in seen_news
        seen_urls = set(seen_news.get(code, []))
        kw_list = tracked_keywords.get(code, list(company["base_keywords"]))
        for bk in company["base_keywords"]:
            if bk not in kw_list:
                kw_list.append(bk)

        # --- 主力事業・ブランド名の発見(有報が更新された時だけ実行、通常はスキップ) ---
        segment_keywords = load_json(STATE_DIR / "segment_keywords.json", {})
        company_segment_kws = segment_keywords.get(code, [])
        new_segment_kws = discover_new_segment_keywords(company, bo_versions)
        for kw in new_segment_kws:
            if kw not in company_segment_kws:
                company_segment_kws.append(kw)
                log(f"  [SEGMENT/new] discovered core keyword: {kw}")
                new_product_lines.append(f"・**{name}**[主力事業]: 「{kw}」を新たに常時監視対象に追加")
        segment_keywords[code] = company_segment_kws
        save_json(STATE_DIR / "segment_keywords.json", segment_keywords)
        core_keywords = list(company["base_keywords"]) + company_segment_kws
        for ck in core_keywords:
            if ck not in kw_list:
                kw_list.append(ck)

        # --- 新商品検知(news_source未設定企業はスキップし、キーワード追跡のみ行う) ---
        news_source = company.get("news_source")
        new_items: list[NewsItem] = []
        if news_source:
            try:
                if news_source["type"] == "html_regex":
                    items = fetch_html_regex_news(news_source["url"], news_source["pattern"])
                else:
                    fetcher = NEWS_FETCHERS.get(news_source["type"])
                    items = fetcher(news_source["url"]) if fetcher else []
                new_items = [it for it in items if it.url not in seen_urls]
            except Exception as e:  # noqa: BLE001
                log(f"  [WARN] news fetch failed: {type(e).__name__}: {e}")

        if is_first_run:
            # 初回実行: 既存の全記事を「既知」として登録するだけ(過去分をまとめて
            # 新商品通知しない)。直近数件のタイトルだけAI判定してキーワードの種にする。
            log(f"  [INIT] first run, bootstrapping {len(new_items)} existing news items silently")
            for it in new_items:
                seen_urls.add(it.url)
            for it in new_items[:MAX_TRACKED_PRODUCT_KEYWORDS]:
                result = extract_keyword_with_ai(it.title, name, it.categories)
                time.sleep(0.3)
                if result:
                    _, kw = result
                    if kw not in kw_list:
                        kw_list.append(kw)
        else:
            for it in new_items:
                seen_urls.add(it.url)
                result = extract_keyword_with_ai(it.title, name, it.categories)
                time.sleep(0.3)
                if result:
                    category, kw = result
                    log(f"  [NEW/{category}] {it.title} ({it.date}) -> keyword: {kw}")
                    if kw not in kw_list:
                        kw_list.append(kw)
                    new_product_lines.append(
                        f"・**{name}**[{category}]: {it.title} ({it.date})\n  <{it.url}>"
                    )
                else:
                    log(f"  [NEW/other] {it.title} ({it.date}) -> not growth-related, skipped")

        # 商品キーワード数を上限に丸める(社名・主力事業ブランド名は常に維持し、
        # 新商品ニュース由来のキーワードだけを直近N件に絞る)
        product_kws = [k for k in kw_list if k not in core_keywords]
        product_kws = product_kws[-MAX_TRACKED_PRODUCT_KEYWORDS:]
        kw_list = core_keywords + product_kws

        seen_news[code] = sorted(seen_urls)
        tracked_keywords[code] = kw_list
        last_checked[code] = today

        # --- 各キーワードの信号取得 ---
        company_history = history.setdefault(code, {})
        for kw in kw_list:
            log(f"  checking keyword: {kw}")
            kw_hist = company_history.setdefault(kw, {})

            trends_val = trends_client.get_interest(kw)
            time.sleep(2.5)
            yahoo_val = get_yahoo_realtime_count(kw)
            time.sleep(2.0)

            kw_hist[today] = {"trends": trends_val, "yahoo": yahoo_val}
            # 90日以上前のデータは削除(state肥大化防止)
            cutoff = (datetime.datetime.now(JST) - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
            for d in list(kw_hist.keys()):
                if d < cutoff:
                    del kw_hist[d]

            spike = detect_spike(kw_hist, today)
            if spike:
                parts = []
                if "trends" in spike:
                    s = spike["trends"]
                    parts.append(f"Google Trends {s['baseline']}→{s['latest']} ({s['ratio']}倍)")
                if "yahoo" in spike:
                    s = spike["yahoo"]
                    parts.append(
                        f"X投稿頻度(Yahoo!リアルタイム) {s['baseline']/10:.1f}→{s['latest']/10:.1f}件/時 ({s['ratio']}倍)"
                    )
                discord_lines.append(f"・**{name}** 「{kw}」: " + " / ".join(parts))

    save_json(SEEN_NEWS_PATH, seen_news)
    save_json(HISTORY_PATH, history)
    save_json(STATE_DIR / "tracked_keywords.json", tracked_keywords)
    save_json(LAST_CHECKED_PATH, last_checked)
    save_json(BUSINESS_OVERVIEW_VERSION_PATH, bo_versions)

    if not args.dry_run:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if webhook_url:
            if new_product_lines:
                send_discord(
                    webhook_url,
                    ["**\U0001f4e2 業績インパクトが期待できる新着を検知・追跡開始**"] + new_product_lines,
                )
            if discord_lines:
                send_discord(
                    webhook_url,
                    ["**\U0001f525 口コミバズ検知**"] + discord_lines,
                )
        elif new_product_lines or discord_lines:
            log("[WARN] DISCORD_WEBHOOK_URL not set, skipping notification")

    log(f"Done. new_products={len(new_product_lines)} spikes={len(discord_lines)}")


if __name__ == "__main__":
    main()
