# 府中コンパス

広島県安芸郡府中町の「市制移行」について、公開資料をもとに中立に整理した非公式の情報ページ。10代のための判断材料として作成。

- 公開URL: https://fuchu-change-info.github.io/
- このサイトは府中町の公式サイトではありません。正式な情報は必ず[府中町公式サイト](https://www.town.fuchu.hiroshima.jp/site/jichiseido/)で確認してください。

## 構成

| ファイル | 役割 |
| --- | --- |
| `index.html` | ページ本体。CSS・JS を含む1ファイル完結 |
| `sources.json` | 記述の根拠にしている公式ページ・PDF の一覧 |
| `snapshots/` | 各公式資料の本文テキスト。前回取得時点の記録 |
| `sources.lock.json` | 各資料のハッシュと最終確認日 |
| `scripts/check_sources.py` | 公式資料が更新されていないか確認する |

## 公式資料の自動チェック

毎日 7:00（JST）に GitHub Actions が動きます。公開前で情報の動きが速いため毎日にしています。落ち着いたら週1に戻して構いません（`.github/workflows/check-sources.yml` の cron）。

見ているのは3種類です。

| 種類 | 何を見るか | 設定 |
| --- | --- | --- |
| 公式資料 | 町のページとPDFの中身が変わっていないか | `sources` |
| 新着ページ | 町のsitemapで「自治制度」に新しいページが増えていないか | `sitemap_watches` |
| 報道 | 新聞・テレビ各社が新しく報じていないか（Googleニュース経由） | `news_watches` |

報道の監視を入れているのは、**町の公式ページより先に新聞やテレビが報じることがある**ためです。実際、アンケートの答え方が「世帯ごと」から「個人単位」に変わった件はテレビ新広島が先に報じていました。

**変化があっても、ページ本文（index.html）は自動では書き換わりません。**

```
1. 毎朝7時の自動チェック
2. 変化を検知 → Issue「府中町の動きを検知しました」が作られる
   （記録＝snapshots と lock だけは main に自動コミットされる）
3. Issue を読んで、ページの記述を直す必要があるか判断する
4. 必要なら index.html を修正して push する
```

中立性を掲げているページなので、公式資料の読み取りは必ず人が確認してから反映します。

### 手元で実行する

```bash
pip install pypdf
python scripts/check_sources.py            # 変化を確認するだけ（変化ありなら終了コード 1）
python scripts/check_sources.py --accept --stamp   # 確認済みとして記録し、最終確認日を更新
```

チェックを今すぐ走らせたいときは、GitHub の Actions タブから「公式資料の更新チェック」を手動実行できます。

### 出典を追加・変更する

`sources.json` に追記します。`used_in` にはページ内のどの章で使っているかを書いておくと、差分を見たときに影響範囲がすぐ分かります。

## 公開

`main` ブランチに push すると GitHub Actions が `index.html` を GitHub Pages に公開します。公開されるのは `index.html` だけで、スナップショットなどの作業用ファイルは含みません。
