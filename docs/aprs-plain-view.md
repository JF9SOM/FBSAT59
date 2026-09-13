# APRS 受信ログ — 平文 / 生パケット表示・Google Map 連携

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。
> APRS タブ全体の設計は [communications.md](communications.md) の
> 「メニュー: Communications > APRS」セクションが本体。ここはその
> 「Show トグル」「右クリック地図」まわりの詳細だけを扱う。

---

## 1. 概要（2026-09-08 実装）

APRS 受信ログは MIC-E・サードパーティ中継が大半で、生のデコード文字列を見ても
人間には読めない、という指摘（実運用フィードバック）を受けた対応。次の 4 コミットで実装:

| コミット | 内容 |
|---|---|
| `5077d26` | 受信行の右クリック「Open in Google Maps」＋世界地図タブの局ピン撤去 |
| `a4a50bc` | 「Show: 平文 / 生パケット」トグル＋`comms/aprs/humanize.py` 新規 |
| `8e2a07e` | 平文モードに最寄り都市「◯◯付近」を付加（同梱 GeoNames、完全オフライン） |
| `cf0054b` | トグルのラベル（`Show:` / `Plain` / `Raw packet` / ツールチップ）の日本語訳漏れ修正 |

### ユーザーと確定した仕様（実装前の合意）

1. 既定プロバイダ = Google Maps（`?q=<lat>,<lon>&z=15`。ピン＋ズーム）
2. 地図表示 UI = **受信リストの右クリックメニュー**（位置を持つ行のみ活性）
3. 生モードのサードパーティは**外側のまま**（オンエア通り）／平文モードは**内側を展開**
4. 「◯◯付近」の地名解決は**オフライン同梱 DB**（GeoNames cities15000, CC BY 4.0）
5. トグルは局設定行（My Call / SSID / Via / Baud の行）の Baud の右、既存の余白に配置
6. 世界地図タブのシアン▲局ピンは**残さず削除**
7. 自局 QTH は地図 URL に載せない／オフライン時のロード失敗は許容
8. MIC-E デコード値は過信せず、回帰テストセットを先に用意
9. Telemetry タブへの横展開・v2「両方」表示は**後日**

---

## 2. 実装内容

### 2.1 右クリック「Open in Google Maps」（`5077d26`）

- [src/ui/aprs_tab.py](../src/ui/aprs_tab.py)
  - 受信行 `QListWidgetItem` に位置を `setData(_ROLE_COORDS, (lat, lon))` で保持
  - `_log_list` に `CustomContextMenu` を設定 → `_on_log_context_menu()` が
    位置付き行でのみ「Open in Google Maps」を出す
  - `_item_coords(item)` / `_open_item_on_map(item)` に分離（`QMenu.exec` を
    テストで回避できるように。後述 3.1）
  - `open_map_url(str)` シグナル → `MainWindow._open_url_app_mode()`（既存の
    Chrome `--app=` 別ウィンドウ起動。SatNOGS/AMSAT リンクで実績あり）
  - 起動時に DB から読み戻す履歴行にも `latitude_deg`/`longitude_deg` を渡す
    （ただし DB 永続化は双方向 QSO 確定行のみ＝大半の受信行は座標を持たず、
    メニューに項目が出ない。想定内）
- [src/ui/world_map.py](../src/ui/world_map.py) — `set_aprs_stations()` /
  `clear_aprs_stations()` / `_draw_aprs_stations()` / `_aprs_stations` と
  paint 内の呼び出しを削除
- [src/ui/main_window.py](../src/ui/main_window.py) — `_on_open_aprs()` の
  `aprs_stations_updated`/`aprs_stations_cleared` 配線を `open_map_url` 1 本に置換

`_google_maps_url()` は `?q=<lat>,<lon>&z=15` 形式（非公式だがデスクトップ／
モバイル両対応でピン＋ズームが効く。公式の `search/?api=1&query=` はズーム無視で
精密な APRS フィックスには広すぎる）。

### 2.2 Show トグル（平文 / 生パケット）（`a4a50bc`）

- [src/comms/aprs/humanize.py](../src/comms/aprs/humanize.py)（新規）
  - `humanize_frame(Ax25Frame) -> str | None` / `humanize_tnc2(str) -> str | None`
  - パース中核は `aprslib`（**必須依存に追加**。純 Python・データファイルなし）
  - 対応: MIC-E・非圧縮/圧縮位置・メッセージ/ack/rej・掲示（bulletin）・
    オブジェクト/アイテム・ステータス・気象・サードパーティ展開・`T#` テレメトリ
  - 付加情報: シンボル→ラベル・8 方位・MIC-E ステータス（en route 等）・
    高度・コメント・「ISS 経由」注記・最寄り都市（2.3）
  - **解釈できない型は `None` を返す** → 呼び出し側が生の情報フィールドに
    フォールバック（空行にしない）
- [src/comms/aprs/parser.py](../src/comms/aprs/parser.py)
  - `AprsPacket.plain: str` フィールドを追加。`parse_aprs()` が `humanize_frame()`
    を**遅延 import** で呼んで populate（理由は 3.2）
- [src/ui/aprs_tab.py](../src/ui/aprs_tab.py)
  - 局設定行の Baud の右に `QComboBox`「Show: [Plain | Raw packet]」
  - 各行に平文・生の両テキストを `_ROLE_PLAIN` / `_ROLE_RAW`（`UserRole+1/+2`）で保持
  - `_on_display_mode_changed()` が `app_settings.aprs_display_mode` に永続化し、
    既存行を `setText()` で即再描画
  - `append_packet()` に `plain` 引数を追加。生モードの表示は `raw_frame`
    （＝`packet.raw_info`、オンエアの情報フィールド）、平文モードは `packet.plain`。
    DB / ADIF / TX エコーは従来通り `comment`（半デコードの短い要約）を使う

### 2.3 最寄り都市「◯◯付近」（`8e2a07e`）

- [src/comms/aprs/citylookup.py](../src/comms/aprs/citylookup.py)（新規）
  - 同梱 [src/data/cities15000.tsv.gz](../src/data/cities15000.tsv.gz)
    （GeoNames cities15000＝人口 15,000 以上、`name/lat/lon/country/population`
    のみ、人口降順、約 630 KB、**CC BY 4.0**。素性は
    [src/data/cities15000.README.txt](../src/data/cities15000.README.txt)）
  - `nearest_city(lat, lon, max_km=50.0)` — numpy ベクトル化 haversine で全 34k 行
    の最近傍を 1 回で計算。50 km 超は `None`
  - データファイル欠落時も `None`（＝最寄りなしと同じ扱い）で静かに劣化
- [src/comms/aprs/humanize.py](../src/comms/aprs/humanize.py) の `_city_note()` が
  位置行の末尾に `_(" · near {city}")`（日本語「◯◯付近」）を付加
- [scripts/fbsat59.spec](../scripts/fbsat59.spec) の `datas` に追加（frozen ビルドで
  `_MEIPASS/data/` へ収集。`community_transmitters.json` と同じ扱い）

### 2.4 i18n

- 平文の文言・シンボルラベル等は英語原文＋`_()`。日本語訳は
  [locale/ja/LC_MESSAGES/fbsat59.po](../locale/ja/LC_MESSAGES/fbsat59.po) に追加
  （humanize 分 約 65 件＋near-city 1 件＋トグルラベル 4 件、fuzzy 0）
- シンボル/MIC-E ステータス/方位の**テーブルはモジュールレベル定数**なので
  値を直接 `_()` で包むと起動時言語で固定される（[i18n.md](i18n.md) ピットフォール #2）。
  → 値は素の英語、`_N()`（gettext no-op マーカー）で抽出対象にし、
  lookup 時に `_()` で翻訳する方式。`xgettext` コマンドに `--keyword=_N` を追加済み

---

## 3. 設計判断・ハマりどころ

### 3.1 `QMenu.exec` を含むテストがハングする

`_on_log_context_menu()` が `menu.exec()` を呼ぶ経路をそのままテストすると、
オフスクリーンでもモーダルループに入って固まる。`monkeypatch.setattr(QMenu, "exec", …)`
は PySide6 の C++ バインドメソッドには効かず不発。
→ **「この item を地図で開く」ロジックを `_open_item_on_map()` に分離**し、テストは
そちらを直接叩く。メニュー生成＋`exec` の 1 行は設計上そのまま信頼する
（Qt 内部を過剰にテストしない方針）。

### 3.2 i18n：モジュールレベル `_()` は起動時言語で固定される

[i18n.md](i18n.md) のピットフォール #2 の通り。`humanize` を
`parser.parse_aprs()` 内で**遅延 import** することで、モジュール load 時点＝
起動シーケンスの `set_language()` 実行後になるようにしている。加えてラベル
テーブルは `_N()` マーカー＋lookup 時 `_()` で、万一早期 import されても
言語切替に追従する（アプリは言語切替後に再起動を促す設計なので実害はないが、
テストの安定のためにも lookup 時翻訳にしてある）。

### 3.3 i18n：トグルラベルの訳漏れ（`cf0054b` で修正）

今サイクルの `xgettext` 抽出を humanize.py 作成直後に回し、その**後で**
aprs_tab.py にトグル UI を足したため、`Show:` / `Plain` / `Raw packet` /
ツールチップが抽出に間に合わずカタログに載らなかった。コードは最初から
`_()` ラップ済みだったので訳文追加のみで解消。
**教訓: `_()` を足す UI 変更を全部終えてから `.po` を回すこと。**

### 3.4 MIC-E バイトの保全

`parse_aprs()` の `info` は `payload.decode("utf-8", "replace")` で、MIC-E の
高位バイトが化ける可能性がある。`humanize_frame()` は `frame` を直接受け取り
`payload.decode("latin-1")`（バイト↔コードポイント 1:1）で TNC2 を再構成して
`aprslib` に渡すため、全バイトが無損失で届く。

### 3.5 回帰テストセットの独立性

[tests/test_aprs_humanize.py](../tests/test_aprs_humanize.py) の `CASES` は
「素性の分かった実パケット → 期待平文」を固定。座標値は `aprslib`（枯れた
デコーダ）、文面はこのモジュールの設計。期待値は APRS 仕様 / aprs.fi と突き合わせ済み。
- 言語は英語に固定（`.po` カタログに依存させない）
- 「◯◯付近」は autouse フィクスチャで `_city_note` をスタブし、期待値を
  **コア文だけ**に保つ（同梱データファイルの内容変更で回帰テストが壊れないように）。
  都市機能の検証は [tests/test_aprs_citylookup.py](../tests/test_aprs_citylookup.py) が担当
- `None`（＝生フォールバック）も正しい結果としてテストする

---

## 4. ファイル対応表

| 役割 | ファイル |
|---|---|
| 平文レンダリング（aprslib ラッパー＋文章化） | [src/comms/aprs/humanize.py](../src/comms/aprs/humanize.py) |
| 最寄り都市（オフライン GeoNames） | [src/comms/aprs/citylookup.py](../src/comms/aprs/citylookup.py) ／ [src/data/cities15000.tsv.gz](../src/data/cities15000.tsv.gz) |
| `AprsPacket.plain` 生成 | [src/comms/aprs/parser.py](../src/comms/aprs/parser.py) |
| トグル UI・行の 2 テキスト保持・右クリック地図 | [src/ui/aprs_tab.py](../src/ui/aprs_tab.py) |
| `open_map_url` → アプリモードブラウザ | [src/ui/main_window.py](../src/ui/main_window.py)（`_open_url_app_mode`） |
| 世界地図ピン撤去 | [src/ui/world_map.py](../src/ui/world_map.py) |
| 回帰テスト（平文） | [tests/test_aprs_humanize.py](../tests/test_aprs_humanize.py) |
| 都市ルックアップ・humanize 連携テスト | [tests/test_aprs_citylookup.py](../tests/test_aprs_citylookup.py) |
| トグル UI・地図メニューのテスト | [tests/test_aprs_tab.py](../tests/test_aprs_tab.py) |
| バンドル定義 | [scripts/fbsat59.spec](../scripts/fbsat59.spec) |
| 日本語訳 | [locale/ja/LC_MESSAGES/fbsat59.po](../locale/ja/LC_MESSAGES/fbsat59.po) |

---

## 5. 今後の作業計画

### 5.1 近い将来（ユーザーと「後日」で合意済み）

1. **v2「両方（Both）」表示モード** — 平文の下に生パケットをグレーの小さい字で
   併記する 3 つ目のモード。学習・デバッグ用途。`_display_combo` に項目を足し、
   `_ROLE_PLAIN`/`_ROLE_RAW` の両方を 1 行にレンダリングする（`QListWidgetItem`
   の複数行テキスト or カスタム delegate）。
2. **Telemetry タブへの横展開** — 同じ「Show: 平文 / 生」トグルを
   [src/ui/telemetry_tab.py](../src/ui/telemetry_tab.py) にも。ただしあちらの
   「平文」は既に `decode_telemetry()` の出力なので、生 = `raw_hex`、
   平文 = デコード済みフィールド、という別マッピングになる。スコープ別。

### 5.2 humanize カバレッジの拡張（実運用で気になったら）

3. **気象パケットの風** — `aprslib` は `_ddd/sss` を weather dict ではなく
   位置の `course`/`speed` に入れる癖がある。`/_` シンボルのとき course/speed を
   風向風速として拾って `_weather_note()` に足す。
4. **シンボルテーブルの拡充** — 現状は頻出 25 種程度。未対応は「局」に
   フォールバックしている。実受信で出てきたものを順次追加。
5. **PHG / RNG / DF、圧縮位置の course/speed 精査、Mic-E Emergency の強調表示、
   `$GP…` 生 NMEA、ウェザー拡張（気圧・積雪）** など、必要が出たら。
6. **サードパーティの入れ子が 2 段以上**のケース（稀）— 現状は 1 段だけ展開。

### 5.3 地図・地名まわり

7. **右クリックメニューに「aprs.fi で開く」を第 2 項目として追加** —
   コミット 1 の提案時に「既定 = Google Maps」で確定したが aprs.fi 併設は
   保留になっていた。APRS ネイティブで、その局のトラック・周辺局まで文脈付きで
   見られる。`https://aprs.fi/#!call=a/<CALL>` 形式。
8. **都市 DB の更新手段** — 現状は手動（README に手順）。年 1 回程度で十分だが、
   `scripts/` に再生成スクリプトを置くと楽。
9. **都市名の現地語化** — GeoNames `name` 列は日本の都市だとローマ字（"Yokohama"）。
   `alternatenames` を引けば「横浜」だがファイルが数 MB 増える。日本の行だけ
   別途 alternatenames から日本語名を引いて差し込む、等のトレードオフ検討。

### 5.4 i18n・保守

10. **`.po` 全体マージで出る fuzzy の定期一掃** — [i18n.md](i18n.md) の手順通り。
    今回の作業では humanize 分の fuzzy 12 件をその場で修正済み（0 件）。
11. **未翻訳 54 件の棚卸し** — 大半は [i18n.md](i18n.md) の「意図的に英語のまま」
    （Dashboard/Pass Chart のタブ名、meteor/ft4-waterfall/q65/sdr-waterfall の
    タブ内部等）だが、実際に「英語のまま残す」で正しいか一度確認する。

### 5.5 サードパーティ中継の送信元コールサイン表示（2026-09-13 実装済み・追記）

**症状（実機フィードバック）**: 平文モードで表示される行の先頭コールサインが、
実際に送信した局（例 `JE9VAX-14`）ではなく、そのパケットを中継した I-Gate
（例 `JH9YVX-10`）になっていた。原因は、サードパーティ（`}`）パケットの
AX.25 フレーム自体の送信元 (`frame.src`) は常に中継局であり、実際の送信局は
情報フィールド内に入れ子で埋め込まれているため。

**修正**:
- [src/comms/aprs/humanize.py](../src/comms/aprs/humanize.py) — `_origin_callsign()`
  を追加（サードパーティを再帰的に辿り、最も内側の `subpacket['from']` を返す。
  非サードパーティなら `None`）。`humanize_tnc2_with_origin()` /
  `humanize_frame_with_origin()` が `(平文, 送信元コールサイン)` のタプルを返す
  新 API として追加され、既存の `humanize_tnc2()` / `humanize_frame()` は
  そのラッパー（テキストのみ返す）として後方互換を維持
- [src/comms/aprs/parser.py](../src/comms/aprs/parser.py) — `AprsPacket.plain_callsign:
  str | None` を追加。`parse_aprs()` が `humanize_frame_with_origin()` から populate
- [src/ui/aprs_tab.py](../src/ui/aprs_tab.py) — 受信行は平文用・生用で
  **別々のプレフィックス**を持つよう変更（`_append_log_item()`）:
  - **生**: 従来通り `{on-air の frame.src},{via}:`（＝オンエア表示は変更なし）
  - **平文**: `plain_callsign` があればそれに差し替え（`via` は表示しない——
    外側の `via`（`NOGATE` 等）は中継局への指示であり、送信元局の実際の経路を
    表さないため、送信元コールサインに付けると誤解を招く）
- aprslib 自体がサードパーティの二重入れ子（`}...}...`）をパースできない
  （内部で `NameError`）ため、`humanize_tnc2_with_origin()` はその例外も
  キャッチして `(None, None)` を返す（＝生表示にフォールバック、クラッシュしない）
- テスト: [tests/test_aprs_humanize.py](../tests/test_aprs_humanize.py)
  （`_origin_callsign` の単体・二重入れ子のフォールバック・`AprsPacket.plain_callsign`
  の populate）、[tests/test_aprs_tab.py](../tests/test_aprs_tab.py)
  （`test_plain_mode_shows_origin_callsign_for_relayed_packet` — 平文では
  送信元、生では中継局のまま、をアサート）

### 5.6 生モードの表示

12. **サードパーティの `[}]` プレフィックス** — 生モードのその表記は
    `parser.parse_aprs()` の旧フォールバック（`f"[{data_type}] {info[1:]}"`）由来で、
    humanize とは別系統。生モードは `raw_info`（＝情報フィールドそのまま）を
    表示しているので実際には `[}]` は付かず `}SRC>DEST,...:` がそのまま出る。
    表示として過不足ないか実機で確認。
