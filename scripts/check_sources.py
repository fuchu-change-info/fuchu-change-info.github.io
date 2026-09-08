#!/usr/bin/env python3
"""府中町の公式資料が更新されていないかを確認するスクリプト。

sources.json に列挙した公式ページ / PDF を取得し、本文テキストを正規化して
snapshots/ に保存する。前回のスナップショットと比べて変化があれば、
何がどう変わったかを update-report.md に書き出す。

  python scripts/check_sources.py            変化を確認するだけ（変化ありなら終了コード 1）
  python scripts/check_sources.py --accept   確認済みとしてスナップショットを更新する
  python scripts/check_sources.py --stamp    index.html の「出典の最終確認日」を today に更新

このスクリプト自身はページ本文（index.html）を書き換えない。
本文をどう直すかは必ず人が判断する。
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.json"
SNAPSHOT_DIR = ROOT / "snapshots"
LOCK_FILE = ROOT / "sources.lock.json"
REPORT_FILE = ROOT / "update-report.md"
INDEX_FILE = ROOT / "index.html"

TIMEOUT = 60
SKIP_TAGS = {"script", "style", "noscript", "template"}


class TextExtractor(HTMLParser):
    """HTML から表示テキストだけを取り出す。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def normalize(text: str) -> str:
    """行ごとに空白を潰し、空行を落とす。差分を読みやすくするため1行1要素にする。"""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t　]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


def fetch(url: str, user_agent: str) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        body = res.read()
        headers = {
            "status": str(res.status),
            "content_type": res.headers.get("Content-Type", ""),
            "last_modified": res.headers.get("Last-Modified", ""),
            "content_length": str(len(body)),
        }
    return body, headers


def fetch_sitemap_urls(
    sitemap_url: str, path_prefix: str, exclude_pattern: str | None, user_agent: str,
) -> list[str]:
    """sitemap.xml から path_prefix 配下のURLだけを取り出す。

    固定URLの巡回だけでは、町が『新しいお知らせページ』を出したこと自体を
    見逃してしまう。sitemapを見れば、そのセクションにURLが増えたかどうかが分かる。
    """
    body, _ = fetch(sitemap_url, user_agent)
    root = ET.fromstring(body)
    urls = []
    for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
        url = (loc.text or "").strip()
        if not url:
            continue
        path = urlparse(url).path
        if not path.startswith(path_prefix):
            continue
        if exclude_pattern and re.search(exclude_pattern, path):
            continue
        urls.append(url)
    return sorted(set(urls))


def fetch_news_items(feed_url: str, user_agent: str) -> list[dict]:
    """GoogleニュースのRSSから記事の見出し・媒体・日付・リンクを取り出す。

    町の公式ページだけを見ていると、新聞やテレビが先に報じた事実
    （アンケートの方式変更など）に気づけない。無料で読める媒体を
    横断的に拾うためにRSSを使う。
    """
    body, _ = fetch(feed_url, user_agent)
    root = ET.fromstring(body)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Googleニュースの見出しは「本文 - 媒体名」の形なので媒体名を落とす
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        items.append({
            "title": title,
            "source": source,
            "date": (it.findtext("pubDate") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
        })
    return items


def news_key(item: dict) -> str:
    """比較用のキーは見出しだけにする。

    Googleニュースは配信元のIPによって、同じ記事でも日付や媒体名の
    表記を変えて返す（例: "news.yahoo.co.jp" と "Yahoo!ニュース"）。
    手元とGitHub Actionsでは実行場所が違うため、日付や媒体名を
    キーに含めると、同じ記事が毎回「新着」に見えてしまう。
    見出しは場所によって変わらないので、これだけで照合する。
    """
    return re.sub(r'\s+', ' ', item['title']).strip()


def decode_html(body: bytes, content_type: str) -> str:
    encodings = []
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if m:
        encodings.append(m.group(1))
    m = re.search(rb'charset=["\']?([\w-]+)', body[:2048], re.I)
    if m:
        encodings.append(m.group(1).decode("ascii", "ignore"))
    encodings += ["utf-8", "cp932", "euc-jp"]

    for enc in encodings:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def pdf_to_text(body: bytes) -> str:
    """PDF の本文テキスト。pypdf が無い環境ではバイト列のハッシュだけで比較する。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception as exc:  # 壊れた PDF や暗号化 PDF
        print(f"    PDF を読めませんでした（バイト比較にフォールバック）: {exc}")
        return ""

    parts = []
    for i, page in enumerate(reader.pages, 1):
        try:
            parts.append(f"--- page {i} ---")
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append(f"--- page {i} (テキスト抽出失敗) ---")
    return "\n".join(parts)


def extract_article_meta(html: str) -> str:
    """ニュース記事は見出しと概要だけを見る。

    ページ全体のテキストを比べると、関連記事欄やランキング枠が入れ替わる
    たびに『変化あり』になってしまい、本当に記事が書き換わったのか・
    消えたのかが埋もれてしまう。
    """
    def meta(*patterns):
        for pat in patterns:
            m = re.search(pat, html, re.I | re.S)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        return ""

    title = meta(r"<title[^>]*>(.*?)</title>")
    og_title = meta(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)')
    desc = meta(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)',
    )

    lines = [f"title: {title}", f"og:title: {og_title}", f"description: {desc}"]
    return normalize('\n'.join(x for x in lines if x.split(": ", 1)[1]))


def extract(source: dict, body: bytes, headers: dict[str, str]) -> str:
    if source["type"] == "pdf":
        text = pdf_to_text(body)
        if not text.strip():
            # テキストを取り出せない場合はバイト列のハッシュを内容とみなす
            return f"[pypdf でテキスト抽出不可]\nsha256={hashlib.sha256(body).hexdigest()}\nbytes={len(body)}\n"
        return normalize(text)

    html = decode_html(body, headers["content_type"])

    if source["type"] == "news":
        return extract_article_meta(html)

    parser = TextExtractor()
    parser.feed(html)
    return normalize(parser.text())


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def diff_preview(old: str, new: str, label: str, max_lines: int = 40) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"{label}（前回）", tofile=f"{label}（今回）",
            lineterm="", n=1,
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... （ほか {len(diff) - max_lines} 行の差分は snapshots/ を参照）"]
    return "\n".join(diff)


def stamp_last_checked(today: str) -> bool:
    """index.html の「出典の最終確認日」を書き換える。本文の他の部分には触れない。"""
    if not INDEX_FILE.exists():
        return False
    html = INDEX_FILE.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'(<time id="lastChecked" datetime=")[^"]*(">)[^<]*(</time>)',
        rf"\g<1>{today}\g<2>{today}\g<3>",
        html,
    )
    if n and new_html != html:
        INDEX_FILE.write_text(new_html, encoding="utf-8", newline="")
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="府中町の公式資料の更新を確認する")
    ap.add_argument("--accept", action="store_true",
                    help="内容を確認済みとしてスナップショットとロックを更新する")
    ap.add_argument("--stamp", action="store_true",
                    help="index.html の出典の最終確認日を今日に更新する")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()

    if args.stamp and not args.accept:
        changed = stamp_last_checked(today)
        print(f"最終確認日を {today} に更新しました。" if changed else "最終確認日の更新箇所が見つかりませんでした。")
        return 0

    config = load_json(SOURCES_FILE, None)
    if config is None:
        print(f"{SOURCES_FILE} がありません。", file=sys.stderr)
        return 2

    user_agent = config.get("user_agent", "fuchu-city-info-checker/1.0")
    lock = load_json(LOCK_FILE, {})
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    changed: list[dict] = []
    failed: list[dict] = []
    new_lock: dict = {}

    for source in config["sources"]:
        sid, url = source["id"], source["url"]
        print(f"[{sid}] {url}")

        try:
            body, headers = fetch(url, user_agent)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            print(f"    取得失敗: {exc}")
            failed.append({"source": source, "error": str(exc)})
            # 取得できなかったものは前回の記録をそのまま残す
            if sid in lock:
                new_lock[sid] = lock[sid]
            continue

        text = extract(source, body, headers)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        snapshot_path = SNAPSHOT_DIR / f"{sid}.txt"
        previous = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else None

        new_lock[sid] = {
            "url": url,
            "label": source["label"],
            "sha256": digest,
            "bytes": len(body),
            "last_modified": headers["last_modified"],
            "checked_at": today,
        }

        if previous is None:
            print("    初回取得（比較対象なし）")
            snapshot_path.write_text(text, encoding="utf-8", newline="")
            continue

        if previous == text:
            print("    変化なし")
            # 確認日だけ更新し、ロックの他の値は据え置く
            continue

        print("    ★ 内容が変わっています")
        changed.append({
            "source": source,
            "diff": diff_preview(previous, text, source["label"]),
            "last_modified": headers["last_modified"],
        })
        if args.accept:
            snapshot_path.write_text(text, encoding="utf-8", newline="")

    # ---- 報道監視（新聞・テレビの新しい記事が出ていないか）----
    new_articles: list[dict] = []

    for watch in config.get("news_watches", []):
        wid = watch["id"]
        print(f"[{wid}] {watch['label']}")

        try:
            items = fetch_news_items(watch["feed_url"], user_agent)
            # 検索語だけでは観光記事などが混ざるので、見出しで絞る
            must = watch.get("title_must_match")
            if must:
                items = [i for i in items if re.search(must, i["title"])]
            # 同じ記事が別リンクで重複することがあるため、見出し＋媒体で一意にする
            seen, uniq = set(), []
            for i in items:
                k = (i["title"], i["source"])
                if k not in seen:
                    seen.add(k)
                    uniq.append(i)
            items = uniq
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ET.ParseError) as exc:
            print(f"    取得失敗: {exc}")
            failed.append({"source": {"label": watch["label"], "url": watch["feed_url"]}, "error": str(exc)})
            if wid in lock:
                new_lock[wid] = lock[wid]
            continue

        keys = sorted({news_key(i) for i in items})
        snapshot_path = SNAPSHOT_DIR / f"{wid}.txt"
        previous = (
            set(snapshot_path.read_text(encoding="utf-8").splitlines())
            if snapshot_path.exists() else None
        )

        new_lock[wid] = {
            "feed_url": watch["feed_url"],
            "label": watch["label"],
            "sha256": hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest(),
            "article_count": len(keys),
            "checked_at": today,
        }

        if previous is None:
            print(f"    初回取得（{len(keys)}件、比較対象なし）")
            snapshot_path.write_text("\n".join(keys) + "\n", encoding="utf-8", newline="")
            continue

        added = [i for i in items if news_key(i) not in previous]
        if not added:
            print("    新着なし")
            continue

        print(f"    ★ 新しい報道 {len(added)} 件")
        new_articles.append({"watch": watch, "items": added})
        if args.accept:
            snapshot_path.write_text("\n".join(keys) + "\n", encoding="utf-8", newline="")

    # ---- sitemap監視（新しいページが増えていないか）----
    new_pages: list[dict] = []

    for watch in config.get("sitemap_watches", []):
        wid = watch["id"]
        print(f"[{wid}] {watch['sitemap_url']} (prefix={watch['path_prefix']})")

        try:
            urls = fetch_sitemap_urls(
                watch["sitemap_url"], watch["path_prefix"],
                watch.get("exclude_pattern"), user_agent,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ET.ParseError) as exc:
            print(f"    取得失敗: {exc}")
            failed.append({"source": {"label": watch["label"], "url": watch["sitemap_url"]}, "error": str(exc)})
            if wid in lock:
                new_lock[wid] = lock[wid]
            continue

        snapshot_path = SNAPSHOT_DIR / f"{wid}.txt"
        previous_urls = (
            snapshot_path.read_text(encoding="utf-8").splitlines()
            if snapshot_path.exists() else None
        )

        digest = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()
        new_lock[wid] = {
            "sitemap_url": watch["sitemap_url"],
            "label": watch["label"],
            "sha256": digest,
            "url_count": len(urls),
            "checked_at": today,
        }

        if previous_urls is None:
            print(f"    初回取得（{len(urls)}件、比較対象なし）")
            snapshot_path.write_text("\n".join(urls) + "\n", encoding="utf-8", newline="")
            continue

        added = sorted(set(urls) - set(previous_urls))
        removed = sorted(set(previous_urls) - set(urls))

        if not added and not removed:
            print("    変化なし")
            continue

        print(f"    ★ ページ構成が変わっています（追加 {len(added)} / 削除 {len(removed)}）")
        new_pages.append({"watch": watch, "added": added, "removed": removed})
        if args.accept:
            snapshot_path.write_text("\n".join(urls) + "\n", encoding="utf-8", newline="")

    # ---- レポート出力 ----
    report = [f"# 公式資料の更新チェック（{today}）", ""]

    if new_articles:
        report.append("## 📰 新しい報道が見つかりました")
        report.append("")
        report.append(
            "新聞・テレビ各社の記事をGoogleニュース経由で確認したところ、"
            "前回このページを更新したあとに出た記事がありました。"
            "町の公式発表より先に報じられている事実が含まれている場合があります。"
            "内容を読んで、このページに反映すべきか判断してください。"
        )
        report.append("")
        for item in new_articles:
            report.append(f"### {item['watch']['label']}")
            report.append("")
            for a in item["items"]:
                media = f"{a['source']}" if a["source"] else "媒体不明"
                report.append(f"- **{a['title']}**")
                report.append(f"  - {media}｜{a['date']}")
                report.append(f"  - {a['link']}")
            report.append("")

    if new_pages:
        report.append("## 🆕 新しいページが見つかった可能性")
        report.append("")
        report.append(
            "固定で追いかけている6件とは別に、府中町サイトのsitemapでページ構成の"
            "変化を検知しました。アンケートの実施時期など、新しいお知らせが"
            "出た可能性があります。中身を見て、このページに反映すべきか判断してください。"
        )
        report.append("")
        for item in new_pages:
            w = item["watch"]
            report.append(f"### {w['label']}")
            report.append("")
            if item["added"]:
                report.append("**追加されたページ:**")
                report.append("")
                for u in item["added"]:
                    report.append(f"- {u}")
                report.append("")
            if item["removed"]:
                report.append("**なくなった（または移動した）ページ:**")
                report.append("")
                for u in item["removed"]:
                    report.append(f"- {u}")
                report.append("")

    if changed:
        report.append(f"**{len(changed)} 件の公式資料に変更がありました。**")
        report.append("")
        report.append("以下の差分を確認して、ページ本文（index.html）を直す必要があるか判断してください。")
        report.append("")
        for item in changed:
            src = item["source"]
            report.append(f"## {src['label']}")
            report.append("")
            report.append(f"- URL: {src['url']}")
            report.append(f"- このページでの使用箇所: {', '.join(src.get('used_in', ['—']))}")
            if item["last_modified"]:
                report.append(f"- サーバー上の更新日時: {item['last_modified']}")
            report.append("")
            report.append("```diff")
            report.append(item["diff"])
            report.append("```")
            report.append("")
    elif not new_pages and not new_articles:
        report.append("公式資料に変更はありませんでした。")
        report.append("")

    if failed:
        report.append("## 取得できなかった資料")
        report.append("")
        for item in failed:
            report.append(f"- {item['source']['label']}: {item['error']}")
            report.append(f"  - {item['source']['url']}")
        report.append("")
        report.append("URL が変更された、または削除された可能性があります。")
        report.append("")

    REPORT_FILE.write_text("\n".join(report), encoding="utf-8", newline="")

    if args.accept:
        LOCK_FILE.write_text(
            json.dumps(new_lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="",
        )
        if args.stamp:
            stamp_last_checked(today)
        print("\nスナップショットとロックを更新しました。")
        return 0

    if changed or failed or new_pages or new_articles:
        print(
            f"\n変更 {len(changed)} 件 / 新着報道 {len(new_articles)} 件 / "
            f"新規ページ {len(new_pages)} 件 / 取得失敗 {len(failed)} 件。"
            f"詳細は {REPORT_FILE.name} を参照。"
        )
        return 1

    print("\nすべての公式資料に変更はありませんでした。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
