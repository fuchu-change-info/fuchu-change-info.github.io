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
from html.parser import HTMLParser
from pathlib import Path

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


def extract(source: dict, body: bytes, headers: dict[str, str]) -> str:
    if source["type"] == "pdf":
        text = pdf_to_text(body)
        if not text.strip():
            # テキストを取り出せない場合はバイト列のハッシュを内容とみなす
            return f"[pypdf でテキスト抽出不可]\nsha256={hashlib.sha256(body).hexdigest()}\nbytes={len(body)}\n"
        return normalize(text)

    parser = TextExtractor()
    parser.feed(decode_html(body, headers["content_type"]))
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

    # ---- レポート出力 ----
    report = [f"# 公式資料の更新チェック（{today}）", ""]

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
    else:
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

    if changed or failed:
        print(f"\n変更 {len(changed)} 件 / 取得失敗 {len(failed)} 件。詳細は {REPORT_FILE.name} を参照。")
        return 1

    print("\nすべての公式資料に変更はありませんでした。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
