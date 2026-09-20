"""Yahoo!ファイナンス掲示板の「バズっている言及」検知。

毎日、全社の掲示板の最新ページを1回だけ取得し、前回から増えた投稿だけを調べる。
- 投稿数の急増: 投稿番号の差(=前回スキャンからの投稿数)を、直近の1日あたり投稿数の
  中央値と比較する(履歴が5日分たまってから判定)。
- バズ関連語の言及: 「バズ」「品薄」「完売」「SNSで話題」等が新規投稿に出ていれば点数化する。
AIは使わない(ルールのみ。費用ゼロ)。
"""

from __future__ import annotations

import argparse
import datetime
import html
import os
import re
import statistics
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buzz_watch import (  # noqa: E402
    JST,
    STATE_DIR,
    USER_AGENT,
    WATCHLIST_PATH,
    load_json,
    log,
    save_json,
    send_discord,
)

BOARD_STATE_PATH = STATE_DIR / "board_state.json"
REQUEST_INTERVAL_SEC = 1.0
SUFFIXES = (".T", ".N", ".F")  # 東証 / 名証 / 福証(掲示板のURL接尾辞)
COUNT_HISTORY_DAYS = 28
MIN_HISTORY_DAYS = 5  # 投稿数急増の判定に必要な最低日数
VOLUME_RATIO = 3.0
VOLUME_MIN_POSTS = 15  # 急増と見なす最低の新規投稿数(閑散銘柄の揺れを除外)
ALERT_MIN_SCORE = 3
MAX_ALERT_LINES = 25
MAX_FAIL_RATIO_TO_WARN = 0.5

# 強い語(1件で2点): 実需・品薄・拡散を直接示す
STRONG_RE = re.compile(
    r"バズ|品薄|完売|売り切れ|売切れ|入手困難|爆売れ|大ヒット|行列|トレンド入り|SNS(で|に)?話題|"
    r"TikTok|ティックトック|再販|在庫(なし|切れ)|買えない|バイラル"
)
# 弱い語(1件で1点): 話題性・拡散の言及
WEAK_RE = re.compile(
    r"話題|流行|ブーム|インスタ|Instagram|YouTube|ユーチューブ|ツイッター|Twitter|インフルエンサー|口コミ|クチコミ|拡散|人気(商品|急上昇|沸騰)"
)

ARTICLE_RE = re.compile(r'<article class="_BbsItem_[^"]*">(.*?)</article>', re.S)
BODY_RE = re.compile(r'<div class="_BbsItem__body_[^"]*">(.*?)</div>', re.S)
TIME_RE = re.compile(r'<time class="_BbsItem__postDate_[^"]*">([^<]+)</time>')


def parse_posts(page_html: str, code: str, suffix: str) -> list[dict]:
    no_re = re.compile(rf'href="/quote/{re.escape(code)}{re.escape(suffix)}/forum/(\d+)"')
    posts: dict[int, dict] = {}
    for m in ARTICLE_RE.finditer(page_html):
        art = m.group(1)
        mn = no_re.search(art)
        mb = BODY_RE.search(art)
        if not mn or not mb:
            continue
        no = int(mn.group(1))
        body = html.unescape(re.sub(r"<[^>]+>", "", mb.group(1))).strip()
        mt = TIME_RE.search(art)
        posts[no] = {"no": no, "time": mt.group(1).strip() if mt else "", "body": body}
    return sorted(posts.values(), key=lambda p: p["no"], reverse=True)


def score_posts(posts: list[dict]) -> tuple[int, list[dict]]:
    score = 0
    hits = []
    for p in posts:
        if STRONG_RE.search(p["body"]):
            score += 2
            hits.append(p)
        elif WEAK_RE.search(p["body"]):
            score += 1
            hits.append(p)
    return score, hits


def fetch_board(session: requests.Session, code: str, suffix_hint: str | None) -> tuple[list[dict], str] | None:
    order = [suffix_hint] + [s for s in SUFFIXES if s != suffix_hint] if suffix_hint else list(SUFFIXES)
    for suffix in order:
        try:
            r = session.get(
                f"https://finance.yahoo.co.jp/quote/{code}{suffix}/forum",
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        except requests.RequestException as e:
            log(f"  [WARN] board fetch error {code}{suffix}: {type(e).__name__}")
            return None
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            log(f"  [WARN] board fetch {code}{suffix}: HTTP {r.status_code}")
            return None
        posts = parse_posts(r.text, code, suffix)
        if posts:
            return posts, suffix
        return None
    return None


def analyze(entry: dict, posts: list[dict], today: str) -> dict | None:
    """前回状態(entry)と最新ページの投稿から、通知に値する変化を返す。entryを更新する。"""
    max_no = posts[0]["no"]
    last_no = entry.get("last_no")
    counts: dict[str, int] = entry.setdefault("counts", {})
    if last_no is None:  # 初回は基準を記録するだけ(過去分を一斉通知しない)
        entry["last_no"] = max_no
        return None
    delta = max(max_no - last_no, 0)
    new_posts = [p for p in posts if p["no"] > last_no]
    entry["last_no"] = max_no
    if delta == 0:
        return None

    prior_days = sorted(d for d in counts if d < today)[-COUNT_HISTORY_DAYS:]
    baseline = statistics.median([counts[d] for d in prior_days]) if len(prior_days) >= MIN_HISTORY_DAYS else None
    counts[today] = counts.get(today, 0) + delta
    for d in [d for d in counts if d < (datetime.date.fromisoformat(today) - datetime.timedelta(days=COUNT_HISTORY_DAYS)).isoformat()]:
        del counts[d]

    volume_spike = baseline is not None and delta >= max(VOLUME_MIN_POSTS, VOLUME_RATIO * max(baseline, 3))
    score, hits = score_posts(new_posts)
    if not volume_spike and score < ALERT_MIN_SCORE:
        return None
    return {
        "delta": delta,
        "baseline": baseline,
        "volume_spike": volume_spike,
        "score": score + (5 if volume_spike else 0),
        "hits": hits[:2],
    }


def format_line(company: dict, suffix: str, res: dict) -> str:
    code = company["code"]
    parts = [f"新規投稿{res['delta']}件"]
    if res["volume_spike"]:
        parts.append(f"通常{res['baseline']:.0f}件/日→急増")
    if res["hits"]:
        excerpt = res["hits"][0]["body"].replace("\n", " ")
        parts.append(f"「{excerpt[:60]}…」" if len(excerpt) > 60 else f"「{excerpt}」")
    url = f"https://finance.yahoo.co.jp/quote/{code}{suffix}/forum"
    return f"・**{company['name']}**({code}): " + " / ".join(parts) + f"\n  <{url}>"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="先頭からN社だけ処理(テスト用)")
    args = parser.parse_args()

    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    watchlist = load_json(WATCHLIST_PATH, [])
    if args.limit:
        watchlist = watchlist[: args.limit]
    state = load_json(BOARD_STATE_PATH, {})
    session = requests.Session()

    alerts: list[tuple[int, str]] = []
    ok = fail = 0
    for company in watchlist:
        code = company["code"]
        entry = state.setdefault(code, {})
        result = fetch_board(session, code, entry.get("suffix"))
        time.sleep(REQUEST_INTERVAL_SEC)
        if result is None:
            fail += 1
            continue
        posts, suffix = result
        entry["suffix"] = suffix
        ok += 1
        res = analyze(entry, posts, today)
        if res:
            log(f"[BOARD] {company['name']}({code}) score={res['score']} delta={res['delta']}")
            alerts.append((res["score"], format_line(company, suffix, res)))

    log(f"[BOARD] fetched={ok} failed={fail} alerts={len(alerts)}")
    if ok == 0 and fail > 0:
        log("[WARN] board fetch failed for every company (blocked?). State not updated.")
        return
    if fail / max(ok + fail, 1) > MAX_FAIL_RATIO_TO_WARN:
        log(f"[WARN] board fetch failure ratio high: {fail}/{ok + fail}")

    save_json(BOARD_STATE_PATH, state)

    if alerts and not args.dry_run:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        alerts.sort(key=lambda x: -x[0])
        lines = ["**\U0001f5e3 Yahoo!掲示板でバズの形跡**"] + [l for _, l in alerts[:MAX_ALERT_LINES]]
        if len(alerts) > MAX_ALERT_LINES:
            lines.append(f"(ほか{len(alerts) - MAX_ALERT_LINES}社)")
        if webhook:
            send_discord(webhook, lines)
        else:
            log("[WARN] DISCORD_WEBHOOK_URL not set, skipping notification")


if __name__ == "__main__":
    main()
