# CLAUDE.md — FBSAT59 開発指示書

このファイルはClaude Codeが本プロジェクトを理解し、一貫した判断をするための指示書です。
コードを書く前に必ずこのファイルを参照してください。

---

## 最重要ルール：実装前に必ずユーザーの了承を得ること

**いかなるコード変更・実装も、ユーザーが明示的に承認してから行うこと。**

- 「どうすればいいか」「何を直せばいいか」がわかっても、勝手に実装しない
- 実装方針を提案し、ユーザーが「OK」「やってください」等の承認を与えてから実装する
- ユーザーが依頼した内容のみを実装する。関連して気になる箇所があっても、了承なく追加・修正しない
- バグを発見しても、依頼されていない修正は勝手に行わない

---

## プロジェクト概要

**名称**: FBSAT59  
**旧称**: GPredict-Improved（v0.2.0 でAPRS・テレメトリー・FT4・SSTV等の通信機能を大幅増強した機会に、現名称 FBSAT59 へ改称）  
**目的**: アマチュア衛星追尾ソフト GPredict の現代的後継ソフトウェア  
**開発言語**: Python 3.11+  
**対象OS**: Linux（主開発環境: Ubuntu）, Windows, macOS  
**ライセンス**: GPL-2.0（GPredict互換）

### FBSAT59が解決する課題
- 現行GPredictはデスクトップ専用 → **同一LAN内のスマホ・タブレットからもブラウザでアクセス可能**にする
- rigctld/rotctldを別途手動起動が必要 → **Hamlibを内蔵してGUIから無線機・ローテーターを直接設定**
- 衛星周波数・モードの設定が隠しテキストファイル編集 → **GUIで追加・編集・削除が可能**
- TLEが手動更新 → **自動更新・品質スコアリング**
- SATNOGSデータのみに依存 → **手動追加・上書き機能付き**

---

## アーキテクチャ

```
fbsat59/
├── src/
│   ├── core/           # 衛星追尾エンジン（Skyfield）・ビジネスロジック
│   ├── ui/             # PySide6 Qt6 デスクトップUI
│   ├── web/            # FastAPI + WebSocket（LAN内ブラウザアクセス）
│   ├── rig/            # Hamlib制御（直接接続 + NET Control互換）
│   ├── sdr/            # SoapySDR バックエンド（デバイス・パイプライン・復調・録音）
│   ├── comms/          # デジタル通信（APRS・テレメトリー等）
│   │   └── aprs/       # APRSEngine・Direwolf管理・Bell 202 AFSK復調・AX.25パーサー
│   ├── data/           # データ同期（SATNOGS・TLE）・SQLiteDB・手動編集・テレメトリーフォーマット定義
│   └── i18n/           # 多言語対応基盤（gettextラッパー）
├── locale/
│   ├── en/LC_MESSAGES/ # 英語翻訳（デフォルト）
│   └── ja/LC_MESSAGES/ # 日本語翻訳
├── tests/
├── docs/
├── scripts/            # udevルール・インストールヘルパー
└── .github/workflows/  # CI/CD（Windows・Mac・Linux自動ビルド）
```

### 起動時の動作
1. `QApplication` 生成直後にスプラッシュ画面（`QSplashScreen`）を表示。
   DB初期化・地図データ読み込み・TLE取得・位置情報取得・MainWindow構築が
   すべて `window.show()` 前に直列実行されるため、特にWindowsで数秒間
   画面が空白になる問題への対策（2026-08-08 追加）。各段階でメッセージを
   更新し、`MainWindow.show()` 直後に `splash.finish(window)` で閉じる。
   多重起動検知で弾かれた場合は案内ダイアログの前に閉じる。
   実装: `src/main.py` の `_show_splash()` / `_splash_message()`
2. Qt6メインウィンドウを起動
3. バックグラウンドスレッドでFastAPI/uvicornをポート8080で起動
4. DataSyncManagerがTLE・SATNOGSデータを自動取得（初回 or 期限切れ時）
5. ステータスバーにLAN内アクセスURL + QRコードボタンを表示

### データフロー
```
SATNOGS API ──┐
Space-Track   ├──→ DataSyncManager ──→ SQLite DB ──→ CoreEngine(Skyfield)
CelesTrak     ┘                                           │
                                                          ├──→ Qt6 UI
手動入力 ──────────────────────────────→ SQLite DB        ├──→ Hamlib RigController
                                                          └──→ FastAPI WebSocket
```

---

## 技術スタック

| 用途 | ライブラリ | バージョン |
|------|-----------|-----------|
| デスクトップUI | PySide6 | >=6.6 |
| 軌道計算 | skyfield | >=1.48 |
| WebサーバーAPI | fastapi | >=0.110 |
| ASGIサーバー | uvicorn | >=0.27 |
| HTTPクライアント | httpx | >=0.27 |
| データベース | sqlite3 | 標準ライブラリ |
| DBマイグレーション | alembic | >=1.13 |
| データモデル | pydantic | >=2.6 |
| Hamlib制御 | Hamlib (python binding) | システム提供 |
| QRコード生成 | qrcode | >=7.4 |
| mDNS | zeroconf | >=0.131 |
| テスト | pytest | >=8.0 |
| パッケージング | PyInstaller | >=6.4 |

---

## コーディング規約

### 全般
- **型ヒント必須**: すべての関数・メソッドに型ヒントを付ける
- **docstring必須**: すべての公開クラス・関数にdocstringを書く（日本語可）
- **フォーマッター**: `ruff format`（black互換）
- **リンター**: `ruff check`
- **型チェック**: `mypy --strict`
- **コメント言語**: すべてのコードコメント（`#` 行コメント・docstring）は**英語**で書くこと。日本語コメントは使用しない。

### 命名規則
- クラス: `PascalCase`
- 関数・変数: `snake_case`
- 定数: `UPPER_SNAKE_CASE`
- プライベート: `_leading_underscore`

### エラーハンドリング
- ネットワークエラーは必ずキャッチしてローカルキャッシュにフォールバック
- ユーザー向けエラーはQt6のステータスバーかダイアログで表示（コンソールに捨てない）
- Hamlibエラーは接続状態をUIに反映してリトライ可能にする

### 非同期処理
- FastAPIのエンドポイントは `async def`
- Qt6のUIスレッドをブロックしない（重い処理はQThread or asyncio）
- TLE/SATNOGS取得はすべて非同期（httpx AsyncClient）

---

## データベーススキーマ（SQLite）

### satellites テーブル
```sql
CREATE TABLE satellites (
    norad_cat_id    INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    alt_names       TEXT,           -- JSON配列
    status          TEXT,           -- 'alive', 'dead', 'unknown'
    updated_at      DATETIME
);
```

### transmitters テーブル（SATNOGS + 手動）
```sql
CREATE TABLE transmitters (
    uuid            TEXT PRIMARY KEY,   -- SATNOGSのUUID or 'manual-{uuid4}'
    norad_cat_id    INTEGER REFERENCES satellites(norad_cat_id),
    description     TEXT NOT NULL,
    type            TEXT,           -- 'Transmitter', 'Transponder', 'Beacon'
    uplink_low      INTEGER,        -- Hz
    uplink_high     INTEGER,        -- Hz (バンドの場合)
    downlink_low    INTEGER,        -- Hz
    downlink_high   INTEGER,        -- Hz
    mode            TEXT,           -- 'FM', 'SSB', 'CW', 'DIGITALVOICE', etc.
    invert          BOOLEAN DEFAULT FALSE,
    baud            INTEGER,
    ctcss_tone      REAL,           -- Hz (FM用トーン)
    ctcss_tone_type TEXT,           -- 'CTCSS', 'DCS'
    alive           BOOLEAN DEFAULT TRUE,
    source          TEXT DEFAULT 'satnogs',  -- 'satnogs' or 'manual'
    manual_override BOOLEAN DEFAULT FALSE,   -- 手動データがSATNOGSより優先
    notes           TEXT,           -- ユーザーメモ
    satnogs_status  TEXT,           -- 生のSATNOGS status: 'active'/'inactive'/'invalid'
                                     -- (manual/community は NULL。2026-07-11 追加)
    updated_at      DATETIME
);
```

**`alive` と `satnogs_status` の関係（2026-07-11 確定）**: `alive` は
`satnogs_status == 'active'` のブール値（SATNOGS APIの `alive` フィールドと同じ定義）。
`get_transmitters()` のデフォルト（`include_dead=False`）・Edit Transmitterダイアログ・
Autotrackリスト検索・Comms Quick Panelなど大半の画面は `alive=1` のみを表示し続ける。
Radio Controlタブのトランスポンダーコンボだけが例外で、`satnogs_status` を使って
inactive/invalidも表示する（詳細は後述「SATNOGSトランスミッター status の全件取得」参照）。

### tle_data テーブル
```sql
CREATE TABLE tle_data (
    norad_cat_id    INTEGER PRIMARY KEY REFERENCES satellites(norad_cat_id),
    name            TEXT,
    line1           TEXT NOT NULL,
    line2           TEXT NOT NULL,
    epoch           DATETIME,
    source          TEXT,   -- 'celestrak', 'space-track', 'amsat', 'manual'
    fetched_at      DATETIME,
    quality_score   TEXT    -- 'excellent'(<6h), 'good'(<24h), 'fair'(<72h), 'poor'
);
```

### tle_history テーブル
```sql
CREATE TABLE tle_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_cat_id    INTEGER,
    line1           TEXT,
    line2           TEXT,
    epoch           DATETIME,
    source          TEXT,
    fetched_at      DATETIME
);
```

---

## 主要コンポーネントの設計原則

### CoreEngine (src/core/)
- `SatelliteEngine`: Skyfieldラッパー。パス予測・仰角/方位角/ドップラー計算
- `PassPredictor`: 指定期間のパス一覧を返す
- `DopplerCalculator`: 反転トランスポンダ対応の周波数補正計算
- Qt UIとFastAPI WebSocket両方から使えるようスレッドセーフに設計

### RigController (src/rig/)
- 抽象基底クラス `RigController` を定義
- `HamlibDirectController`: python-hamlibで直接COMポート接続
- `HamlibNetController`: TCP経由でrigctld/rotctldに接続（従来互換）
- `RotatorController`: ローテーター制御（同様の抽象化）
- モード設定・CTCSS/DCSトーン設定・VFO切り替えをサポート

### DataSyncManager (src/data/)
- バックグラウンドで動作（QThread）
- SATNOGS APIから全トランスポンダを日次取得・DBに保存
- TLEを複数ソースから取得（CelesTrak優先、Space-Trackはオプション）
- `manual_override=True` のレコードはSATNOGS上書きから保護
- オフライン時はキャッシュで継続動作

### 自動フェッチスケジュール（APScheduler）

アプリはバックグラウンドでTLE・トランスポンダーを自動更新する。**手動更新は通常不要。**
ユーザーは **Help → Auto Fetch Rules** でこのスケジュールを確認できる。

| データ種別 | 更新間隔 | APSchedulerジョブ |
|---|---|---|
| Space Stations（ISS・CSS等） | 1時間ごと | `_refresh_tle_sync`（各ソースの`update_interval_hours`を参照） |
| Amateur Satellites | 2時間ごと | 同上 |
| CubeSats | 4時間ごと | 同上 |
| Weather Satellites | 6時間ごと | 同上 |
| Earth Observation / Science | 12時間ごと | 同上 |
| Provisional TLEs（NORAD ≥ 90000） | 12時間ごと | `provisional_tle_refresh` |
| Active TLE fallback（NORAD 10000–89999） | 24時間ごと | `active_tle_refresh` |
| AMSAT運用状況 | 24時間ごと | `amsat_refresh` |
| METEOR/HRPT衛星（CelesTrakの群リストから除外された運用終了気象衛星の個別照会） | 12時間ごと | `meteor_tle_refresh`（2026-07-07 追加） |
| SATNOGSトランスポンダーDB | 7日ごと | `satnogs_transmitter_refresh`（2026-07-05 追加） |

SATNOGSトランスポンダーは**初回起動時に自動取得**。以降は7日ごとにバックグラウンドで自動再同期される
（`satnogs_transmitter_refresh`）。より早く最新化したい場合（新規打ち上げ衛星のトランスミッタ登録直後など）は
引き続き `Satellite → Fetch Transmitter Database`（旧称: Sync SATNOGS。2026-08-13改名、後述）で
手動更新できる。

**追加の経緯（2026-07-05）**: 初回起動時のみの自動取得だと、後から新たにトランスミッタが登録された衛星
（例: ISSから放出直後のCubeSat「Coconut」(98292)）がいつまでも空のトランスポンダーリストのままになり、
ユーザーが気づきにくいという問題があった。手動同期に頼らず定期的に自己修復するよう7日間隔の自動ジョブを追加した。

**起動時ゲートの欠陥修正（2026-08-11）**: 上記の「7日ごとに自動再同期」は、アプリを7日間連続で
起動しっぱなしにした場合にのみ成立する話だった。旧実装の起動時チェックは
`transmitters` テーブルに `source='satnogs'` の行が**1件もない場合のみ**同期を走らせる設計
（真の初回起動の検出のみが目的）で、一度でも同期に成功した後は完全にAPSchedulerの168時間
間隔ジョブに依存していた。ところがAPSchedulerの`interval`ジョブは**登録直後には発火せず、
その時点から丸一区間（168時間）経過して初めて実行される**ため、7日間連続で起動しっぱなしに
することが稀な通常のデスクトップ利用（毎回終了→再起動）では、このジョブは実質的に一度も
発火せず、**初回同期以降トランスミッタDBが永久に更新されない**という状態になっていた
（詳細は「起動時鮮度チェックの網羅的監査と修正」セクション参照）。
`TransmitterManager.is_satnogs_transmitters_stale(max_age_hours=168.0)`
（`TLEManager.is_active_tle_stale()`と同型）を新設し、起動時ゲートを
「`source='satnogs'`の行が0件、**または**最終同期から168時間超過」に変更した。

### TransmitterManager (src/data/)
- SATNOGS取得データと手動追加データを統合管理
- 手動追加データはSATNOGSより優先（`manual_override`フラグ）
- GUI経由でCRUD操作が可能
- エクスポート/インポート（JSON）対応

### Web API (src/web/)
- `GET /api/satellites` — 衛星一覧
- `GET /api/satellites/{norad}/transmitters` — トランスポンダ一覧
- `GET /api/satellites/{norad}/passes` — パス予測（以下フィールドを含む）
  - `max_elevation_deg`: 最大仰角（度）
  - `max_elevation_time`: 最大仰角に達する時刻（ISO 8601 UTC）
  - `duration_seconds`: パス継続時間（秒）
  - `quality`: 品質ランク（excellent/good/fair/low）
- `WebSocket /ws/tracking` — リアルタイム仰角/方位角/ドップラー
- `GET /api/tle/status` — TLE品質情報
- `GET /api/location` — 現在の自局位置情報を返す
- `POST /api/location/browser` — ブラウザ Geolocation API から座標を受け取り保存する

#### パス品質ランク定義
| ランク | 最大仰角 | 表示色 |
|--------|----------|--------|
| excellent | 60度以上 | 緑 (#2ecc71) |
| good | 30度以上60度未満 | 青 (#3498db) |
| fair | 10度以上30度未満 | 黄 (#f1c40f) |
| low | 10度未満 | グレー (#95a5a6) |

### グラフィカルパス予測表示 (src/ui/)
- `PassChartView` (src/ui/pass_chart.py): PySide6 + QtCharts ウィジェット
  - 横軸: 時刻（AOS〜LOS）、縦軸: 仰角（0〜90度）
  - 各パスをサイン近似の山型曲線で描画
  - 品質ランクで色分け
  - 現在時刻を赤い縦線で表示
  - パスクリック時に詳細情報を `pass_clicked` Signal で通知
- `pass_chart.js` (src/web/static/pass_chart.js): Chart.js によるブラウザ向け同等実装
  - `renderPassChart(canvasId, passes, satName)` — キャンバスにチャート描画
  - `fetchAndRenderPasses(canvasId, noradId, satName, options)` — APIから自動取得して描画
  - `showPassDetail(pass)` — クリック時の詳細ポップアップ

### レーダーチャート（スカイビュー）(src/ui/, src/web/static/)

#### デスクトップ版 (src/ui/radar_view.py)
PySide6 の QPainter で以下を実装:
- 円形レーダー表示（同心円で仰角 0/30/60/90 度を表示）
- 上が北固定（North-up）
- 衛星の現在位置をドットで表示（衛星名ラベル付き）
- パスの軌跡を曲線で描画（AOS から LOS まで）
- AOS/LOS の時刻をパス線の端に表示
- 現在仰角を下部に数値表示（例: "EL: 34.2°  AZ: 247.5°"）
- 複数衛星を色分けして同時表示
- `SatTrackData` データクラス: name, norad_cat_id, azimuth_deg, elevation_deg, is_visible, track, aos_time, los_time
- `az_el_to_xy(az, el, cx, cy, r)` — 方位角・仰角をレーダー上の (x, y) に変換するユーティリティ
- `sat_clicked(str)` Signal — 衛星ドットクリック時に衛星名を emit

#### ブラウザ版 (src/web/static/radar.js)
Canvas API で同等のレーダー表示:
- `RadarView` クラス: `new RadarView('canvasId')` でインスタンス化
- `setTracks(tracks)` — 衛星データ配列を設定して描画
- スマホでは `DeviceOrientationEvent` で方位を取得してレーダーを自動回転（コンパス連動）
- 方位センサーがない場合は北固定にフォールバック
- タッチ/クリックで衛星をタップすると `onSatClick(track)` コールバックを呼ぶ
- `azElToXY(az, el, cx, cy, r, rotationDeg)` — 座標変換ユーティリティ（公開関数）

### 自局位置の自動取得 (src/core/location.py)

取得優先順位:
1. GPS デバイス（gpsd デーモン経由 / python-gps）
2. ブラウザ Geolocation API（POST /api/location/browser 経由）
3. IPジオロケーション（ip-api.com）
4. 手動入力（緯度・経度・標高 / QTH グリッドロケーター形式）

主要コンポーネント:
- `LocationSource` enum: `GPS` / `Browser` / `IP` / `Manual`
- `Location` dataclass: latitude_deg, longitude_deg, elevation_m, source, accuracy_m, city, country
- `grid_to_latlon(grid: str) -> tuple[float, float]` — Maidenhead グリッドロケーターを緯度経度に変換
- `LocationManager` クラス:
  - `detect()` — 優先順位に従って自動取得（async）
  - `from_gps()` — gpsd 経由で GPS 座標取得（async）
  - `from_ip()` — ip-api.com で IP ジオロケーション（async）
  - `from_manual(lat, lon, elev)` — 手動設定
  - `from_grid(grid, elev)` — グリッドロケーターから設定
  - `set_browser_location(lat, lon, accuracy_m)` — ブラウザ位置を設定
  - `save(loc)` — app_settings に保存
  - `load_saved()` — 保存済みを読み込む
  - `status_text` プロパティ — ステータスバー表示テキスト（例: "QTH: 35.6895°N 139.6917°E (GPS)"）

### i18n (src/i18n/)

#### 設計方針
- Python 標準 `gettext` ベース。外部ライブラリ不要
- 翻訳ドメイン: `fbsat59`
- 翻訳ファイル: `locale/<lang>/LC_MESSAGES/fbsat59.{po,mo}`
- 新言語の追加は `.po` ファイルを追加して `msgfmt` でコンパイルするだけ

#### 公開 API

```python
from i18n import _, ngettext, set_language, get_language, available_languages

set_language("ja")          # 言語を変更（スレッドセーフ）
get_language()              # 現在の言語コードを返す → "ja"
available_languages()       # 利用可能な言語一覧 → ["en", "ja"]
_("Ready")                  # 翻訳 → "準備完了"
ngettext("%(n)d satellite", "%(n)d satellites", n)  # 複数形対応
```

#### 重要な規則
- `set_language()` は **Qt UI の設定変更時のみ**呼ぶ。起動時はシステムロケールを参照する予定
- `from i18n import _` してから `set_language()` を呼んでも、`_()` は常に最新のカタログを参照する（関数オブジェクトはモジュールの `_translation` グローバルを参照するため）
- `.mo` ファイルはコンパイル済みバイナリ。`.po` ファイルを編集したら必ず `msgfmt` で再コンパイルしてコミットする
- `locale/` はプロジェクトルート直下に配置（`src/` の外）

#### 翻訳対象
- UI テキスト全般（メニュー・ボタン・ラベル・ステータスメッセージ）
- エラーメッセージ（ユーザー向けのもの）
- 翻訳不要: ログ出力・コード内定数・NORAD IDなどのデータ値

---

## 外部API仕様

### SATNOGS API
- Base URL: `https://db.satnogs.org/api/`
- 認証不要
- `GET /transmitters/?satellite__norad_cat_id={norad}`（`status`パラメータは付けない。
  2026-07-11以降、active/inactive/invalid全件を取得する設計に変更済み。詳細は
  「SATNOGSトランスミッター status の全件取得」セクション参照）
- レート制限: 緩やか（日次更新で十分）

### CelesTrak
- `https://celestrak.org/SOCRATES/query.php?GROUP=amateur&FORMAT=tle`
- 認証不要
- アマチュア衛星: `amateur.txt`
- ISSなど主要局: `stations.txt`

### Space-Track.org（オプション）
- 要アカウント（無料）
- 設定画面でユーザー名/パスワードを入力
- OMM形式対応

### SATNOGS・CelesTrakに接続できない時はまず「自分のIPがファイアウォールでブロックされていないか」を疑うこと（2026-08-10 確定）

過去に「SATNOGSに繋がらない」「TLE更新を押しても何も起きない」という症状を複数回、
家庭内ネットワーク側の問題（Wi-Fiの不調・ISP側の障害等）と誤って判断したことがあったが、
実際にはいずれも**過度なアクセスによりCelesTrak/SATNOGS側のファイアウォールに自分の
グローバルIPをブロックされていただけ**だった可能性が高い（2026-08-10、`fetch_active_tles()`
のサーキットブレーカー実装時の調査で確定）。

CelesTrakは公式の利用ポリシー（`https://celestrak.org/usage-policy.php`）で、
**2時間以内に403/404等のHTTPエラーが50回を超えるとIP単位でファイアウォールブロックされる**
と明記している。旧`fetch_active_tles()`のPhase 2（衛星ごとの個別CATNR問い合わせ）は
この上限を考慮せず無制限にループしていたため、対象衛星が多い環境では容易にこの閾値を
超えてブロックを引き起こしていた（詳細は「fetch_active_tles() の2フェーズ設計」セクション
の`_ErrorCountBreaker`参照）。SATNOGS側も同様の挙動（過去に報告された404連発）が
確認されている。

**症状の見分け方**: ブロックされている場合、HTTPエラー（403等）が即座に返るとは限らず、
**接続要求自体がサイレントに破棄されタイムアウトする**ことがある（ファイアウォールでの
drop）。「DNSは解決できる」「他サイト（Google等）へは接続できる」のに
「CelesTrak/SATNOGSだけタイムアウトする」という切り分けができれば、家庭内ネットワークの
問題ではなくIPブロックの可能性が高いと判断してよい。

```bash
getent hosts celestrak.org          # DNS解決確認
curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 "https://www.google.com"  # 対照確認
```

ブロックは永続的ではなく一定時間後に自動解除されると考えられるが、正確な解除時間は
公式ドキュメントに明記されていない。ブロック中かどうかを確認する最も確実な方法は、
**別のネットワーク（4G回線等、別のグローバルIPを持つ経路）から同じ問い合わせを試す**こと。
同じWi-Fi/回線を使う全端末（PC・スマホ問わず）は通常同一のグローバルIPを共有するため、
一度ブロックされるとそのネットワーク上のどの端末からアクセスしても同じ症状が再現する。

アプリ側の対策（サーキットブレーカー・`active_tle_retry_after`永続化リトライ）は
「今後ブロックを引き起こさないようにする」「ブロックされた場合に自動で後で再開する」
ためのものであり、**既にブロックされている状態を解除する手段ではない**（ブロック解除は
CelesTrak/SATNOGS側の裁量）点に注意。

---

## Hamlib関連

### 対応デバイス
- Hamlibがサポートする700機種以上の無線機
- 主要なアマチュア衛星対応機: IC-9700, IC-9100, IC-705, FT-991A, TS-2000, FT-817ND

### モードマッピング（SATNOGS → Hamlib）
```python
MODE_MAP = {
    "FM":           Hamlib.RIG_MODE_FM,
    "SSB":          Hamlib.RIG_MODE_USB,   # 衛星SSBは通常USB
    "CW":           Hamlib.RIG_MODE_CW,
    "CW-R":         Hamlib.RIG_MODE_CWR,
    "DIGITALVOICE": Hamlib.RIG_MODE_FM,    # D-STARなど
    "BPSK":         Hamlib.RIG_MODE_PKTUSB,
    "AFSK":         Hamlib.RIG_MODE_PKTFM,
}
```

### Linux USBデバイス権限
- インストール時に `/etc/udev/rules.d/99-fbsat59.rules` を配置
- `dialout` グループへの追加を案内

### Hamlib バージョン管理・配布方針（2026-06-09 確定）

#### 必須バージョン
- **Hamlib 4.7 以上が必須**（FTX-1F モデル 1051 および SkyWatcher ローテーターは 4.7 以降でのみ動作）
- **4.7.2 以上を強く推奨**（4.7.2 で rigctld のセキュリティ修正2件
  — CVE-2026-54634 / GHSA-gpcq-c37x-pr4、GHSA-f72v-7gmh-m9mj — が入った。
  `send_raw` のスタック境界外書き込み・`read_string_generic` のオーバーフロー・
  rigctld の認証バイパス。NET モードで rigctld をネットワークに公開する構成では直接該当する）
- 配布バンドル（AppImage / .exe / .dmg）には必ず 4.7.2 を同梱すること

#### バンドル版 Hamlib のビルド

| プラットフォーム | ビルド方法 | PyInstaller 収集元 |
|---|---|---|
| Linux | ソースから `/opt/hamlib/4.7` にビルド | `/opt/hamlib/4.7/lib/*.so` |
| Windows | 公式 `hamlib-w64-4.7.2.zip` を展開 | `hamlib-win64\bin\*.dll` + Python bindings |
| macOS | Homebrew `brew install hamlib` | `$(brew --prefix hamlib)/lib/` |

#### バージョンアップ手順（4.7.1 → 4.7.2 で実施した手順、2026-08-06）

1. upstream のリリースアセットとソース tarball の構造が変わっていないか実際にダウンロードして確認する
   （`bindings/hamlib.swg` の有無・`hamlib_wrap.c` が含まれないこと・`include/hamlib/config.h`
   が含まれないこと・w64 zip の `bin/`・`include/hamlib/` レイアウト）。ここが同じなら CI は
   バージョン文字列の置換だけで済む
2. `include/hamlib/riglist.h` を展開して、本プロジェクトが使うモデルID
   （FTX-1=1051, FT991=1035, IC9100=3068, IC9700=3081, IC705=3085, IC910=3044）が
   変わっていないことを確認する
3. `.github/workflows/ci.yml` の `HAMLIB_VER` 3箇所（Linux / Windows / macOS）・ステップ名・
   Windows の `config.h` スタブ内の版数を置換する
4. **タグを push する**。hamlib バンドルのアップロードは
   `if: startsWith(github.ref, 'refs/tags/v')` でガードされており、`workflow_dispatch` では
   スキップされる。タグを打たない限り `hamlib-bundle` リリースのアセットは更新されない

#### in-app Hamlib アップデーター（Help > Hamlib Update…）

ユーザーが GUI からバンドル版を上書きできる仕組み。AppImage・exe・dmg は読み取り専用なのでバンドルは変更できず、代わりにユーザーデータディレクトリへインストールする。

**インストール先:**
```
Linux:   ~/.local/share/fbsat59/hamlib/
macOS:   ~/Library/Application Support/fbsat59/hamlib/
Windows: %APPDATA%/fbsat59/hamlib/
```

**起動時のロード優先順位:**
1. ユーザーインストール版（`sys.path.insert(0, ...)` で先頭に追加）
2. バンドル版 / システム版

**Windowsの追加処理**: `os.add_dll_directory(user_hamlib_dir)` が必要（Python 3.8+）。`main.py` の起動ブロックで実施済み。

**GitHub Releases アセット命名規則:**（CI が自動アップロード）

| プラットフォーム | ファイル名 | 内容 |
|---|---|---|
| Linux | `hamlib-linux-x86_64-py311-4.7.2.tar.gz` | `$ORIGIN` rpath付きポータブルビルド |
| Windows | `hamlib-windows-x86_64-py311-4.7.2.zip` | フラットレイアウト（DLL + .pyd + Hamlib.py） |
| macOS | `hamlib-macos-arm64-py311-4.7.2.tar.gz` | `@loader_path` rpath + dylibbundler で依存解決済み |

`py311` の部分は Python バージョンに応じて変化（`hamlib_info.py` の `_PYVER_TAG` で決定）。

**アップロード先は upstream ではなく本リポジトリの `hamlib-bundle` プレリリース**
（ft8lib-bundle / q65lib-bundle / ft4wsjt-bundle と同じ方式）。upstream の
`Hamlib/Hamlib` リリースにあるのはソース tarball と Windows インストーラーだけで、
上表のアセットは一切存在しない。

#### アップデーターが upstream を見ていて一度も機能していなかった不具合（2026-08-06 発見・修正）

4.7.2 へのバンドル更新作業中に発覚。`src/core/hamlib_info.py` の `HAMLIB_GITHUB_API` が
`https://api.github.com/repos/Hamlib/Hamlib/releases/latest`（**upstream**）を指しており、
`_CheckWorker._find_asset_url()` がその upstream リリースのアセット一覧から
`hamlib-linux-x86_64-py311-<ver>.tar.gz` を探していた。しかしこの命名のアセットは
本リポジトリの `hamlib-bundle` リリースにしか存在しないため、**マッチは構造上必ず失敗**し、
毎回「Pre-built package not found for this platform / Python version」に落ちていた。
つまり **Help > Hamlib Update… の「Download & Install」ボタンは一度も表示されたことがなかった**
可能性が高い（他の3つのバンドルインストーラーは最初から
`https://api.github.com/repos/JF9SOM/fbsat59/releases/tags/{tag}` を見ており正しかった。
Hamlib のアップデーターだけがこのパターンから外れていた）。

副次的に `_on_check_result()` の `version == current` も、upstream のタグ由来の `"4.7.2"` と
`get_hamlib_version()`（＝`Hamlib.hamlib_version`、実際の値は `"Hamlib 4.7.2"`）を
直接比較しており、こちらも構造上必ず不一致になっていた。

**修正**:
- `HAMLIB_GITHUB_API` を `JF9SOM/fbsat59` の `hamlib-bundle` タグへ変更
- **バージョンはリリースのタグではなくアセット名から取り出す**。`hamlib-bundle` は
  ローリングのプレリリースでタグ自体が版数を持たない。さらに
  `gh release upload --clobber` は同名ファイルしか置き換えないため、
  4.7.2 を上げても 4.7.1 のアセットがリリースに残り続ける。したがって
  「最初にマッチしたもの」ではなく **`version_key()` による数値比較で最大のものを選ぶ**
  必要がある（`select_newest_asset()`）
- `get_hamlib_version_number()` を新設し、表示用文字列から数値部分だけを取り出して比較する。
  ユーザーインストール版は再起動するまでロードされないため、判定には
  `get_user_hamlib_version()`（`version.txt`）を優先する
- **判定は「一致しないか」ではなく「厳密に新しいか」**（`is_update_available()`）。
  旧実装は `version == current` の不一致をもって更新ありとみなしていたため、
  リリース側が同梱版より**古い**場合にダウングレードを提案してしまう。この状態は
  例外的なものではなく、タグを打つ前の開発ビルド（同梱 4.7.2／リリース 4.7.1）で
  日常的に発生する。実際 4.7.2 のバンドル更新時、`workflow_dispatch` でビルドした
  .dmg を macOS 実機で開いたところ「Download & Install Hamlib 4.7.1」が出た
  （2026-08-06、実機確認で発覚）
- アセット選択ロジック自体は `hamlib_info.py` 側に置いた（ダイアログ内のメソッドのままだと
  テストに PySide6 と QThread 構築が必要になるため）。`tests/test_hamlib_info.py` が
  Qt 非依存でカバーする

**教訓**: 同種の機能（バンドルの自動ダウンロード）が4つあるのに、そのうち1つだけが
別の実装パターン（upstream API を見る）になっていた。しかも失敗時は例外ではなく
「見つかりませんでした」という**もっともらしいメッセージ**に落ちるため、
壊れていること自体が長期間気づかれなかった。同じ役割のコードが複数ある場合、
新規追加時だけでなく既存分についても、参照先・命名規則が揃っているかを確認すること。

**関連ソースファイル:**
- `src/core/hamlib_info.py` — バージョン検出・ユーザーディレクトリ・アセット命名
- `src/ui/hamlib_update_dialog.py` — ダウンロード・展開・インストール UI
- `src/main.py` — ユーザーインストール版の優先ロード・Windows DLL パス登録
- `.github/workflows/ci.yml` — 各プラットフォームのポータブルパッケージビルドと Release アップロード

#### Linux 開発環境固有: sys.path surgery

開発機（`/opt/hamlib/4.7` が存在する場合のみ）は `/usr/lib/python3/dist-packages` を `sys.path` から除去して 4.7.x を優先ロードする。

**重要**: このブロックは `os.path.exists(_HAMLIB_SITE)` でガードされており、`/opt/hamlib/4.7` が存在しない一般ユーザー環境では一切実行されない。

SoapySDR も同じ `dist-packages` に存在するため、パス除去前に `import SoapySDR` をプリロードして `sys.modules` に保持する（`main.py` の `contextlib.suppress` ブロック）。

---

## コミットメッセージ規則

形式: `<type>(<scope>): <概要（英語・50文字以内）>`

**type一覧:**

| type | 用途 |
|------|------|
| `feat` | 新機能追加 |
| `fix` | バグ修正 |
| `refactor` | リファクタリング（動作変更なし） |
| `test` | テスト追加・修正のみ |
| `chore` | 設定・ビルド・CI等 |

**scope一覧:**

| scope | 対象 |
|-------|------|
| `rig` | リグ制御関連 |
| `data` | DB・TLE・SatNOGS関連 |
| `ui` | Qt UIコンポーネント |
| `core` | 軌道計算・ドップラー計算 |
| `web` | WebサーバーAPI |
| `ci` | GitHub Actions |

**例:**
```
feat(rig): add set_vfo_frequencies for stable FTX-1 VFO control
fix(rig): resolve chk_vfo timeout disconnecting socket
feat(data): add SatNOGS type mapping in sync_from_satnogs
test(rig): add coverage for VFO sequence and timeout handling
```

## コミット後のプッシュ規則

**コミット直後に必ずpushすること。** 理由：
- CIの早期確認
- 作業内容のバックアップ
- コンテキスト引き継ぎ時の最新状態保証

コミットのみでpushを忘れた場合は、次のアクション前に必ずpushする。

---

## 実装完了後の自動コミット規則（2026-07-05 追加）

**承認を得て実装した変更は、下記「コミット前チェックリスト」を通過し次第、都度コミットすること。
「コミットしますか？」とユーザーに改めて確認する必要はない。**

**背景**: APRSタブのRig非接続受信対応・QTHダイアログのグリッド⇔緯度経度同期という2つの
実装済み変更が、コミットされないまま1週間近く放置され、存在自体を忘れられていたことがあった
（2026-07-05）。実装のたびに確実にコミットすることで、この種の「コミット忘れ」を防ぐ。

**運用ルール**:
- 「最重要ルール：実装前に必ずユーザーの了承を得ること」は変わらない。**実装そのものへの承認**は
  引き続き必須。このルールが自動化するのは「承認済みの実装をコミットする」工程のみ
- コミットは、承認済みの変更がコミット前チェックリストを通過した直後に行う。会話の区切りや
  セッション終了、ユーザーからの明示的な「コミットして」の指示を待たない
- 1セッション内で複数の独立した変更を行った場合は、既存の「コミットメッセージ規則」に従い
  論理的にまとまった単位ごとに分けてコミットする（1つの巨大なコミットにまとめない）
- コミット後は既存の「コミット後のプッシュ規則」通り、直後に必ずpushする
- `.env` / `secrets.toml` / `space_track_credentials.json` 等の機密情報ファイルは
  引き続きコミット対象から除外する（Git Safety Protocol 通り）

---

## テスト方針

- `tests/` 以下にpytest
- ネットワーク不要なテストは積極的に書く（Hamlibはモック）
- TLE計算・ドップラー計算は既知の値でリグレッションテスト
- CI（GitHub Actions）でLinux/Windows/macOS全プラットフォームでテスト実行

### ローカル実行の注意（GPD MicroPC2）

**`pytest tests/` による全テスト一括実行はシステムをフリーズさせる可能性がある。**
`test_main_window.py` の実行も同様にフリーズする。

ローカルでは **`test_rig.py` のみ** を実行すること：
```bash
python -m pytest tests/test_rig.py -q 2>&1 | tail -5
```

`test_main_window.py` のテストは CI（GitHub Actions）で確認する。

### QWidget/QDialogを構築するテストは必ず `qtbot.addWidget()` を使うこと（2026-07-14 発見）

`tests/test_rig_dialog_sdr.py`（SoapyRemote機能のテスト、Issue #12対応）を新規作成した際、
`QApplication` を手動管理し `widget.close()` で後始末する従来パターン（`app`フィクスチャ +
`try/finally: obj.close()`）を使ったところ、**全アサーションが成功した直後、プロセス終了時に
セグフォルト**するというCI障害が発生した（`gh run view --log-failed` で
`QObject: shared QObject was deleted directly.` の警告 → `Segmentation fault (core dumped)`）。

調査の結果、これは新規に書いたロジック（remote_hosts等）自体のバグではなく、
**`_SdrSettingsPanel`/`_AddRemoteHostDialog`（`src/ui/rig_dialog.py`）を構築するテストが
このプロジェクトで初めてだったために露呈した、既存ウィジェットコードに潜在していたQtオブジェクト
ライフタイム問題**だった（多少の再現性のブレはあるが、`QApplication` 手動管理 + `close()`
という後始末方式そのものが原因で、ウィジェット固有のロジックとは無関係と判明）。

**解決策**: 後始末を手動`close()`ではなく、`pytest-qt`（`pyproject.toml`に
`pytest-qt>=4.3`として既存）が提供する **`qtbot` フィクスチャ + `qtbot.addWidget(widget)`**
に変更したところ、8回連続・フルスイート3回連続で完全に再現しなくなった。`qtbot.addWidget()`
は単なるスタイルの好みではなく、`deleteLater()`とイベント処理を正しい順序で行うことで
このクラスのクラッシュを実際に回避する。

**教訓**: このプロジェクトの既存テスト（`test_telemetry_tab.py`等）は手動`app`フィクスチャ+
`close()`パターンを使っており、これまでたまたま問題が起きていなかっただけの可能性がある。
**新しくQWidget/QDialogを構築するテストを書く際は、既存パターンをコピーせず必ず
`qtbot`（引数名`qtbot: QtBot`、`from pytestqt.qtbot import QtBot`）を使い、
生成したウィジェットは`qtbot.addWidget(widget)`に登録すること。** 手動`close()`方式の
既存テストを見つけても、動いているなら無理に書き換える必要はない（後述「バグを発見しても、
依頼されていない修正は勝手に行わない」原則通り）が、新規テストでは踏襲しないこと。

### コミット前チェックリスト

**必ずこの順番で実行すること。いずれかが失敗したらコミットしない。**

```bash
# 1. フォーマット（自動修正）
ruff format src/ tests/

# 2. リントチェック
ruff check src/ tests/

# 3. テスト（test_rig.pyのみ）
python -m pytest tests/test_rig.py -q 2>&1 | tail -5
```

### mypy とオプショナルインポートの注意点（2026-06-12 確定）

CI は `pip install -e ".[dev]"` のみ実行するため、`scipy` などのオプショナル依存は**インストールされない**。
`pyproject.toml` の `ignore_missing_imports = true` により、mypy はインストールされていないモジュールのインポートを `Any` として扱い、エラーを出さない。

**オプショナルインポートの正しいパターン（`type: ignore` コメント不要）:**

```python
try:
    from scipy import signal as sp_signal
    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None   # type: ignore コメント不要
    _SCIPY_AVAILABLE = False
```

**やってはいけないパターン:**

```python
# NG1: 前方宣言すると import 自体が no-redef エラーになる
sp_signal: Any
try:
    from scipy import signal as sp_signal  # error: no-redef
    ...

# NG2: type: ignore[assignment] / [no-redef] / [unused-ignore] を付けると
#      CIでは「Unused type: ignore comment」として弾かれる
except ImportError:
    sp_signal = None  # type: ignore[no-redef]  ← CI で unused-ignore エラー
```

**理由:** mypy は `try/except ImportError` の except ブランチを「import が失敗した場合の新規定義」と解釈するため、`no-redef` も `assignment` もエラーにならない。`ignore_missing_imports = true` 環境ではさらにすべてが `Any` 扱いとなり、あらゆる `type: ignore` コメントが「未使用」として弾かれる。

---

## ビルド・配布

- **Linux**: AppImage（全distro対応）+ `.deb`（Ubuntu/Debian）
- **Windows**: PyInstaller → NSIS インストーラー `.exe`
- **macOS**: PyInstaller → `.dmg`
- **GitHub Actions**: タグpushで3プラットフォーム自動ビルド → GitHub Releases

### タグを打たずに手動テストビルドする（2026-07-07 追加）

`.github/workflows/ci.yml` に `workflow_dispatch`（`platforms` 入力: `macos`/`windows`/`linux`/`all`、デフォルト `macos`）を追加済み。GitHub の Actions タブ →
左メニュー「CI / Build」→ 右上「Run workflow」から、バージョンタグを打たずに任意のブランチ/コミットで
指定プラットフォームのみビルドできる。

- タグpush時と同じ3つの`build-*`ジョブを流用し、`if:` 条件に
  `github.event_name == 'workflow_dispatch' && contains(fromJSON('[...]'), github.event.inputs.platforms)`
  を追加しただけ（ジョブ本体は変更なし）
- バージョン文字列は各ジョブ冒頭の「Determine version」ステップで決定: タグpushなら通常通り
  `github.ref_name`（`v`除去）、workflow_dispatch実行時は`0.0.0-dev`固定
- `Attach to GitHub Release` / `Upload Hamlib bundle to hamlib-bundle release` の2ステップは
  `if: startsWith(github.ref, 'refs/tags/v')` でガードし、workflow_dispatch実行時はスキップ
  （**GitHub Releaseは一切作成・更新されない**）。成果物は`actions/upload-artifact`でアップロードされた
  ものを、該当実行ページ下部の「Artifacts」からダウンロードする
- 実機検証（IC-705のmacOS/Linux CI-V応答ズレバグ調査、後述）で実際に活用し、タグを消費せず
  何度も再ビルド・再テストできることを確認済み

---

## CI/CD トラブルシューティング履歴（v0.1.0-beta.34 で解決済み）

v0.1.0-beta.34 の CI 作業で判明した重要な知見。同様のエラーに遭遇したときのために記録する。

### Hamlib 4.7.x ソースビルド共通

（4.7.2 でも同じ。バージョンアップのたびに tarball の構造を再確認すること）

**問題**: `hamlib_wrap.c: No such file or directory`  
**原因**: Hamlib 4.7.x ソースtarballには SWIG が生成する `hamlib_wrap.c` が含まれない（`.swg` ファイルのみ）  
**解決**: ビルド前に `swig -python -Iinclude -Ihamlib-4.7.2/include -o bindings/hamlib_wrap.c bindings/hamlib.i` を実行

**問題**: `hamlib/config.h: No such file or directory`  
**原因**: `config.h` は autotools が生成するファイル。tarball・zip には含まれない  
**解決**: 必要な define のみ含む最小スタブを手動作成してインクルードパスに配置

### macOS 固有

**問題**: `symbol(s) not found for architecture arm64`（Python シンボルリンクエラー）  
**解決**: `clang` コンパイル行に `-undefined dynamic_lookup` を追加（macOS では Python シンボルを明示的にリンクしない）

**問題**: dylibbundler が `/tmp` シンボリックリンクで自己削除エラー  
**原因**: macOS の `/tmp` は `/private/tmp` へのシンボリックリンク。コピー元とコピー先が同一パスに解決される  
**解決**: prefix を `/tmp/` ではなく `$HOME/hamlib-portable-mac/` に変更

**問題**: dylibbundler が `--dest-dir` と同じ場所を参照して無限ループ  
**解決**: `--dest-dir ${PORTABLE_LIB}/deps` + `--install-path @loader_path/../deps` に変更

### Windows 固有

**問題**: `ImportError: DLL load failed while importing _Hamlib`（ABI ミスマッチ）  
**原因**: MSVC でコンパイルした `.pyd` と MinGW でビルドした `libhamlib-4.dll` は ABI が合わない  
**解決**: Python binding のコンパイルも MinGW GCC に統一。`hamlib-w32-4.7.x.zip`（32bit）ではなく `hamlib-w64-4.7.x.zip`（64bit）を使用

**問題**: Python 3.8+ で PATH 経由の DLL 探索が効かない  
**解決**: `os.add_dll_directory()` を使用（`main.py` 起動ブロックに実装済み）

**問題**: PyInstaller が作成した `dist\fbsat59\` が空になる  
**原因**: Windows Defender のリアルタイムスキャンが新規作成された未署名 exe/DLL を検疫  
**解決**: PyInstaller 実行前に `Set-MpPreference -DisableRealtimeMonitoring $true` を追加  
**追加対策**: `choco install nsis` は必ず PyInstaller より前のステップで実行する（PyInstaller 後に実行すると dist が消える可能性）

**問題**: `File: "dist\fbsat59\" -> no files found.`（NSIS）  
**原因**: NSIS の `File` コマンドは相対パスを **スクリプトファイルの場所**（`scripts\`）基準で解決する。CWD 基準ではない。`File /r "dist\fbsat59\"` は `scripts\dist\fbsat59\` を探しに行く  
**解決**: `File /r "..\dist\fbsat59\"` に変更（`scripts\` の一つ上 = リポジトリルート）

**問題**: `Can't open output file` / `Output: scripts\dist\FBSAT59-Setup.exe`（NSIS）  
**原因**: `OutFile` も同様にスクリプトファイル基準で解決される  
**解決**: `OutFile "..\dist\FBSAT59-Setup.exe"` に変更

> **NSIS パス解決の原則**（重要）:
> `scripts\installer.nsi` 内のすべてのファイル系ディレクティブ（`File`・`OutFile`・`Icon` 等）は、**スクリプトファイルが置かれているディレクトリ**（`scripts\`）を基準に相対パスを解決する。リポジトリルートの `dist\` を参照するには必ず `"..\dist\..."` と書くこと。  
> なお `makensis` コマンドライン引数（`/DAPP_VERSION` 等）や PowerShell 側の変数は CWD 基準で問題ない。

**問題**: `Error: invalid VIProductVersion format, should be X.X.X.X`（NSIS）  
**原因**: `VIProductVersion` は Windows リソースの仕様で `X.X.X.X`（数値4フィールド）必須。`0.1.0-beta.34` は不正  
**解決**: CI で `-beta.34` を除去して `.0` をパディングした `VIVERSION=0.1.0.0` を別途計算し、`/DVIVERSION=$viVer` で渡す。表示用の `APP_VERSION` は semver のまま維持

```powershell
$numericVer = ($ver -replace '-.*$', '')
$parts = $numericVer.Split('.')
while ($parts.Count -lt 4) { $parts += '0' }
$viVer = ($parts[0..3] -join '.')
makensis /DAPP_VERSION=$ver /DVIVERSION=$viVer scripts\installer.nsi
```

### ft4wsjt（libft4wsjt）ビルド固有（2026-07-05 解決・3プラットフォームCI緑確認済み）

`build-ft4wsjt.yml`（WSJT-X の `lib/ft4_decode.f90` から `libft4wsjt` をビルド、詳細は Communications > FT4 セクション参照）の CI 実装時に判明した知見。

**問題**: macOS で `fatal error: 'boost/crc.hpp' file not found`（`brew install boost` 実行後も発生）  
**原因**: Apple Silicon版 Homebrew は `/opt/homebrew` にインストールされるが、clang/g++ はこのパスをデフォルトでは検索しない  
**解決**: `scripts/build_ft4wsjt.sh` で `fftw3.f03` と同様に `boost/crc.hpp` の実在パスを候補ディレクトリ（`/usr/include`・`/usr/local/include`・`/opt/homebrew/include`・`${CONDA_PREFIX}/include` 等）から探索し、`crc14.cpp` のコンパイルに明示的に `-I` で渡す

**問題**: macOS でコンパイルは通るがリンク時に `ld: library 'fftw3f' not found`  
**原因**: 上記と同根。ライブラリ探索パスも `/opt/homebrew/lib` がデフォルトでは通っていない  
**解決**: `fftw3.f03` が見つかったインクルードディレクトリから `${FFTW3_F03_DIR%/include}/lib` を導出し、リンクコマンドに `-L` で明示的に渡す（Homebrew・conda-forge 双方のディレクトリ構成に対応できる導出方法）

**問題**: Windows で FFTW3/Boost を conda-forge から取得する際、`conda create` の依存解決に20分以上かかり終わらない  
**原因**: Miniforge をサイレントインストールして通常の `conda` ソルバーで解決する方式は、Boost のような依存関係の大きいパッケージで著しく遅い  
**解決**: `mamba-org/setup-micromamba@v2` アクションに置き換え。同じ conda-forge パッケージでも数分で解決完了する。`init-shell: bash` を指定し、後続ステップは `shell: bash -el {0}`（ログインシェル）にすることで `$CONDA_PREFIX` が自動設定され、`build_ft4wsjt.sh` 側の探索ロジック（上記2件と共通）がそのまま機能する

**追加の見落とし**: Windows ビルドで FFTW3 の実行時 DLL（`fftw3f*.dll`）を出力に同梱し忘れていた（MinGW ランタイムDLLのみコピーしていた）。ビルドと ctypes ロードは別物であり、リンクが通っても実行時に依存 DLL が同梱されていなければ `ctypes.CDLL()` は失敗する。`$CONDA_PREFIX/Library/bin/*fftw3f*.dll` を出力ディレクトリにコピーして解決

---

## 開発環境セットアップ（Ubuntu）

```bash
# システム依存パッケージ
sudo apt install python3.11 python3.11-venv python3-pip \
    libhamlib-dev python3-hamlib \
    qt6-base-dev libqt6webkit6-dev \
    pkg-config cmake

# Python仮想環境
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# udevルール（USB無線機アクセス）
sudo cp scripts/99-fbsat59.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG dialout $USER
```

---

## 開発環境移行 — Ubuntu → macOS（2026-08-15）

開発機をUbuntu（GPD MicroPC2、ユーザー`sadatoshi`、`gpd-linux.local`でSSH到達可能）から
Mac（ユーザー`sadatoshikoike`）に移行した。以降、FBSAT59の開発はMac単体で完結する
（Ubuntu機は引き続き起動していればSSHで到達可能だが、日常的な開発には使わない）。

### 移行したもの

- **Claudeメモリ**: `~/.claude/projects/<プロジェクトパスのハッシュ化文字列>/memory/` は
  マシンごとのローカルディレクトリのため、別マシンへは自動的に引き継がれない。
  `rsync -avz sadatoshi@gpd-linux.local:~/.claude/projects/-home-sadatoshi-FBSAT59/memory/
  ~/.claude/projects/-Users-sadatoshikoike-FBSAT59/memory/` の要領で直接コピーする必要がある。
  同一Mac内で別チャットを開く分にはこの操作は不要（プロジェクトパスが同じであれば自動的に
  同じメモリを参照する）
- **未コミットスクリプト7本**（`scripts/test_ft991_*.py`・`soundcard_level_meter.py`等、
  実機診断用でgit未追跡だったもの）
- 姉妹プロジェクト2つも同時に移行済み（詳細は下記）: **FBSAT59PP**（ハムフェア2026プレゼン
  資料、`~/FBSAT59PP`）・**ctld-launcher**（`~/ctld-launcher`、`JF9SOM/ctld-launcher`）

### 移行時の判断基準: 「実体」と「ローカル生成物」を区別する

3プロジェクトいずれも、**Linux上でビルドされたネイティブバイナリ・依存関係一式は
そのままコピーせず、Mac上で該当ツールチェーンを使って作り直した**。理由はアーキテクチャ
（x86_64 → arm64）とOS（Linux → macOS）の両方が変わるため、コンパイル済みバイナリを
含むディレクトリは原理的に持ち越せない：

| プロジェクト | コピーしなかったもの | Mac上での再構築方法 |
|---|---|---|
| FBSAT59PP | `scripts/node_modules`（134MB、`sharp`等ネイティブアドオン含む） | `npm install`（`package-lock.json`はコピー済み） |
| ctld-launcher | `.venv`・`build`・`dist`・各種キャッシュ・`hamlib-bundle`（すべて`.gitignore`対象） | `git clone` + `pip install -e ".[dev]"`。`pytest`146件パス・`ruff`/`mypy`クリーンを確認済み |

判断に迷ったら「その場で再生成できる（依存関係マニフェストがある）か」を基準にする。
実際の成果物（pptx・スクリーンショット・PNGアイコン素材等、OS非依存のデータ）は
そのまま`rsync`でコピーしてよい。

### Hamlibの重複整理（2026-08-15）

移行直後、Mac上にHamlibが4箇所に分散していた: Homebrew・radioconda（`~/radioconda`、
conda-forge製のham radio向けディストリビューション）・`~/src/hamlib-4.7.2`（手動ソース
ビルド）・FBSAT59の`.venv`。調査の結果、実際に必要なのは以下の2箇所だけで、他は
削除しても実害がないと判明した:

- **FBSAT59用**: `.venv`内に自前ビルドしたPythonバインディング
  （`_Hamlib.so` + `libhamlib.4.dylib`）。外部依存はHomebrewの`libusb`のみ
- **ctld-launcher用**: `.app`バンドル内（`Contents/Resources/hamlib/`）に`@rpath`経由で
  完全自己完結にバンドル済み

削除の判断は毎回`otool -L`で実際のリンク先を確認してから行った。特に
**Homebrewの`hamlib`は`brew uses --installed hamlib`が`gpredict`を依存先として表示するが、
`otool -L`で`gpredict`バイナリを直接見ると実行時に`libhamlib.dylib`をリンクしていない
ことが判明**（brewの依存関係表はビルド時依存であり、実行時の必須リンクではなかった）。
このため`brew uninstall --ignore-dependencies hamlib`で安全に削除できた。radioconda側も
`conda remove --dry-run`で他パッケージへの影響がないことを確認してから削除。
`~/src/hamlib-4.7.2`のビルドツリー（189MB、成果物は`.venv`にコピー済みで不要）も削除。
合計約3.9GB解放。

**教訓**: `conda clean --packages`は「どの環境からも参照されていない未使用キャッシュ全体」を
消す設計で、hamlib関連だけにスコープを絞れない。依頼は「hamlibの削除」だったが、実行した
結果hamlib以外の未使用パッケージキャッシュ3.65GB分も巻き込んで削除してしまった
（実害はないが依頼範囲を超えていた）。今後、cache-clean系コマンド（`conda clean`・
`brew cleanup`・`npm cache clean`等）を使う際は、対象を絞れる狭いコマンドが存在するか
先に確認し、なければ実行前にスコープの広さを明示して確認を取ること。

### デスクトップ起動用 `.app` ランチャー

単体の`.command`シェルスクリプト（Desktop上でダブルクリック実行する用途）には、実運用で
以下の構造的な弱点があると判明した:
1. Dockに登録してもアイコン表示が不安定（真っ白になることがある）
2. `fileicon`で設定したFinderのカスタムアイコン（拡張属性由来）は、そのファイルを
   後から書き換える（エディタが新規ファイルとして書き直す実装だと特に）と消える
3. Dockからのクリック起動が信頼できない（単体スクリプトはDockが想定する起動経路と
   相性が悪い）

これらを回避するため、**正式な`.app`バンドルでラップする方式に統一**した
（FBSAT59・ctld-launcherとも同じ構造）:

```
XXX.app/Contents/
  Info.plist          # CFBundleIconFile=icon, CFBundleExecutable=XXX
  MacOS/XXX           # ダブルクリック/Dockクリックで実行される起動スクリプト
  Resources/icon.icns # プロジェクトのassets/配下から生成・コピー
```

`.app`バンドルなら、アイコンはバンドル内リソース（Info.plist参照）として保持されるため
消えず、Dockでの表示・起動も本来の仕組みで正しく動作する。

**起動スクリプト（`Contents/MacOS/XXX`）の中身は用途で使い分ける**:

- **コンソール出力を見たい場合**（FBSAT59。Qt GUIアプリのログ・エラーを確認したいため
  ターミナルを開いたまま実行したい）:
  ```bash
  DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
  open -na /Applications/Ghostty.app --args -e "$DIR/run.command"
  ```
  Ghostty（後述）は`wait-after-command=false`がデフォルトのため、`run.command`
  （venv activate → 実行）が終了すると同時にウィンドウが自動的に閉じる
- **コンソール不要・直接GUI起動したい場合**（ctld-launcher。トレイ常駐アプリのため
  ターミナルが開くとむしろ邪魔）:
  ```bash
  cd "$HOME/ctld-launcher"
  source .venv/bin/activate
  exec ctld-launcher
  ```

**Terminal.appでの試行錯誤（最終的にGhosttyへ切り替えたため実害はないが、教訓として
記録）**:
- AppleScriptの`do script`でTerminal.app自体を未起動状態から起動させると、Terminalの
  「起動時デフォルトウィンドウ」と競合し、意図しない空ウィンドウが余分に開くことがある。
  `open -a Terminal <script>`（Finderのダブルクリックと同じ起動経路）を使う方が安全
- `tell application "Terminal" to count windows`は、実際には既に閉じたウィンドウを
  ゾンビ参照として数え続けることがある。真の画面状態を確認したい場合は
  `System Events`のAccessibilityツリー（`tell application "System Events" to tell
  process "Terminal" to count windows`）の方が信頼できる
- ウィンドウを「シェル終了時に自動で閉じる」ようにするには、自前のAppleScript
  close-by-ttyトリックより、Terminal.appプロファイル自体の`shellExitAction`設定
  （`~/Library/Preferences/com.apple.Terminal.plist`の
  `Window Settings:<プロファイル名>:shellExitAction`、`1`=正常終了時に閉じる）の方が
  堅牢
- **Ghostty採用後はこれらの問題が一切発生しない**（`open -na Ghostty.app --args -e
  <command>`でコマンド完了時に自動クローズ、余分なウィンドウも発生しない）。以降、
  ターミナルを表示したいランチャーはTerminal.appではなくGhosttyを使う方針とする

### 姉妹プロジェクトの移行詳細

- **FBSAT59PP**（`~/FBSAT59PP`、ハムフェア2026プレゼン資料。gitリポジトリではない
  プレーンフォルダ）: pptx本体2点・スクリーンショット8点・生成スクリプト
  （`build_deck.js`等、pptxgenjs）・アイコン素材をコピー。`node_modules`のみMac上で
  `npm install`。`node scripts/build_deck.js`で19枚のスライドが正常に再生成されることを
  確認済み。**Linux固有の実機スクリーンショット撮影手順（Xvfb仮想ディスプレイ経由の
  ヘッドレス起動）はMacでは使えない**（Macには実ディスプレイがあるためXvfb自体不要だが、
  `scripts/screenshot_driver_template.py`はMac向けに書き直しが必要。既存スクリーンショットを
  使う分には問題ない）
- **ctld-launcher**（`~/ctld-launcher`、`JF9SOM/ctld-launcher`の正規リポジトリ）:
  `git clone`で取得。`hamlib-bundle/`ディレクトリは`core/hamlib_locator.py`のソースを
  確認した結果、**PyInstallerパッケージング時（`scripts/ctld-launcher.spec`）にのみ
  読まれ、通常の開発時実行（`ctld-launcher`コマンド）では`sys._MEIPASS`
  （フリーズ時のみ設定）かシステムPATHしか見ない**と判明。つまり開発作業そのものには
  `hamlib-bundle/`の中身は無関係（動作確認済みのmacOS版バイナリを一応配置したが、
  必須ではない）

---

## 重要な設計判断

1. **手動追加トランスポンダはSATNOGSより優先**: `manual_override=True` のレコードはSATNOGS同期時に上書きされない
2. **オフライン動作を保証**: すべてのデータはローカルSQLiteにキャッシュ。ネットワーク不要で起動・動作可能
3. **初心者ファースト**: デフォルト設定で「インストールして起動するだけ」で動作。高度な設定はオプション
4. **GPredict互換性**: NET Controlモードで従来のrigctld/rotctldとの互換性を維持
5. **マルチプラットフォーム**: OS固有コードを最小化。プラットフォーム分岐は `src/core/platform.py` に集約
6. **Rig Settingsダイアログを閉じても接続は維持**: `MainWindow._load_rig_settings()` は
   OKを押すたびに `_build_rig_controller()` / `_build_sdr_rig_adapter()` で
   Rig 1/2 コントローラーを**未接続の新規インスタンスとして作り直す**（設定を変更していなくても）。
   このため以前は「設定ダイアログでOKを押すと無言でRigが切断される」問題があった
   （FT4のPTTが黙って失敗する原因になっていた。2026-07-03 修正）。
   現在は再構築前に旧コントローラーの `is_connected` を記録しておき、接続中だった場合は
   `RadioControlWidget._on_connect_rig1()` / `_on_connect_rig2()` を呼んで自動再接続する。

---

## 実装済み機能一覧（2026年6月30日時点・v0.2.6。FT4関連の追加修正は2026-07-03・v0.2.8まで反映）

- 衛星追尾エンジン（Skyfield）
- **Moon/EME追尾**（JPL DE421エフェメリス・CelestialEngine）— 詳細は「Moon/EME 追尾設計」セクション参照
- Qt6デスクトップUI（**Dashboard**・世界地図・レーダー・Pass Chart・Group Pass Chart・Radio Control）
- FastAPI内蔵Webサーバー（ポート8080）
- **スマホブラウザUI**（グループフィルター・Favorites・Group Pass・レーダー・Antenna タブ）
- Hamlib内蔵リグ制御（Direct/NET Control）・Rig 1 / Rig 2 デュアルリグ対応
- SATNOGS周波数DB同期・手動追加
- **コミュニティ周波数DB**（`src/data/community_transmitters.json`）— FT4コーリング周波数など、SATNOGSにない慣習周波数を `source='community'` として管理。SATNOGS同期で上書きされない
- TLE自動更新（CelesTrak: Amateur/CubeSat/Weather/Earth-Obs/Science/Stations）
- **SATNOGS仮ID（90000番台）衛星のTLE自動取得・仮ID→実ID移行パイプライン**
- **超古い衛星（NORAD < 10000）の一括チェック：CelesTrak 未収録なら自動非表示**
- AMSAT運用状況スクレイピング・色分け表示
- **カスタムFavoriteグループ**（Favorite 1/2/3 デフォルト、Settings > Custom Groups で追加/削除/改名可能）
- **フットプリント表示**（スキャンライン方式・極地域対応・ズーム地図との座標整合済み）
- Upcoming Passes（Target/Groupタブ・カレンダー選択・CSV出力）
- **Group Pass Chart** — グループ検索結果を衛星別カラーで描画（ホバーでツールチップ表示）
- カレンダーポップアップ改善（英語ロケール固定・週番号列非表示・To欄はCurrent Timeボタンなし）
- **AOS/LOS デスクトップ通知**（Linux: notify-send / macOS: osascript / Windows: plyer+PowerShell）
  - Settings > Notifications タブ: AOS通知ON/OFF・何分前か・LOS通知ON/OFF
  - Target衛星・Group検索結果の両方に対応
- **Autotrack / Record メニュー**（メニューバー。Radio と View の間）
  - クリックで非モーダルダイアログ `AutotrackRecordDialog`（src/ui/autotrack_record_dialog.py）を開く
  - **Autotrack Lists 枠**（最上部）: リスト作成・衛星＋トランスポンダー登録・並び替え（Settings から移動）
    - **衛星検索ダイアログ**（`_SatSearchDialog`）— Add Satellite ボタン押下時に表示。上部に QLineEdit 検索欄を持ち、入力に一致する衛星をリアルタイムフィルタリング。ダブルクリックまたは OK で選択
    - **衛星選択の注意**: Autotrack で AOS/LOS を正確に計算するため、受信する衛星と同一の NORAD ID を選択すること（トランスポンダーは SDR 受信目的なら任意）
  - **Autotrack Control 枠**: リスト選択コンボ・Enable チェックボックス・ステータス表示・?ヘルプボタン
  - **Recording 枠**: Audio Record (MP3) / IQ Record チェックボックス（AOS で自動開始・LOS で自動停止）
    - **METEOR / HRPT Reception チェックボックス** — チェックを ON にすると、Autotrack AOS 検出時に対応する衛星の METEOR/HRPT タブを自動オープンし SatDump 受信を開始、LOS で自動停止する
  - **Autotrack Timer 枠**: 開始時刻（カレンダーポップアップ付き QDateTimeEdit + Now ボタン）・停止時間（3/6/12/24時間コンボ）
    - View > Time Zone 設定に連動: UTC モードなら「Start (UTC):」、Local モードなら「Start (Local):」表示
    - 指定時刻になると Autotrack を自動開始、停止時刻になると自動停止（リグ・ローテーター切断・録音停止）
  - Radio Control タブの Autotrack 枠は「ON/OFF」のコンパクトインジケーターのみに縮小
  - AOS 時に自動でリグ＋ローテーターを接続、LOS 時に自動切断
- **CPU負荷最適化**
  - 世界地図更新を5秒ごとに変更（毎秒→5秒）
  - `_visible_norads`（フィルター表示中の衛星のみ）で Skyfield 計算
  - `_sat_name_cache` で毎秒の DB SELECT を排除
  - `_last_elevations` で仰角データを Autotrack と共有
- Radio Control レイアウト縦幅圧縮（Name/NORAD・DL/Doppler・UL/Doppler・Mode/CTCSS・AZ/EL を各1行に）
- **CW トグルボタン**（src/ui/radio_control_widget.py）— Mode 行にボタンを追加。USB/SSB/LSB トランスポンダー選択時のみ表示（FM 等は非表示）。クリックで両 VFO を CW-U（"CW"）/ CW-L（"CW-R"）に切り替え、もう一度クリックで元のモードへ復帰。FTX-1F / FT-991 Direct モードは raw CAT パス（`apply_transponder_state()`）経由。NET モードおよびその他は `send_mode_only()` 経由。
- **非 SATMODE リグの周波数プリセット**（v0.2.0 以降）— トランスポンダー選択時にリグが未接続でも DL/UL 周波数をリグに書き込む。NET モード: `_send_freq_preset_independent()` で独立 TCP ソケット経由。Direct モード: `_send_freq_preset_direct()` で短時間 Hamlib open/set_freq/close。Connect 前からリグの表示が正しい周波数になる。
- **`_nonsatmode_gen` 世代カウンター** — トランスポンダーを素早く切り替えた際の二重スレッド競合を防止（旧スレッドが新しい世代を検出して即時終了）。
- **無線機音声のMP3録音**（2026-07-24、src/ui/radio_control_widget.py）— Rotator枠を半分幅にし、右横に新設した「Recording」枠に ● REC / ■ STOP / 📁 ボタンを配置。音声は SDR ではなく Rig Settings > Sound Card の入力デバイスから `AudioDeviceManager.acquire_input()` 経由で取得（CW Decoder等と同じ共有RX経路）。録音バックエンドは SDR Control タブと同じ `sdr.AudioRecorder`（lameenc）を再利用し、保存先も共用の `~/audio_recordings`。REC押下のたびに `soundcard_settings` をDBから読み直すためキャッシュしない。Rotator枠自体も `QFormLayout`（単一行だと上下の余白が偏る）から `QHBoxLayout` 直付けに変更し、Recording枠と高さを揃えた際にAZ/EL表示が上寄りにならないよう修正。
- **スマホ Web UI 大幅強化**（Antenna タブ・コンパス切り替え・RIG 遠隔制御）
- **Dashboard タブ**（src/ui/dashboard_view.py）— ズームマップ＋レーダー＋ステータスバーの統合ビュー
  - Dashboard 表示中は Satellite Detail パネルを自動非表示
  - ズームマップはグリッド線・赤道線を非表示（WorldMapView.set_show_grid(False)）
  - NASA Topographic 1024px をデフォルト世界地図として採用（初回起動時に自動ダウンロード）
  - 速度予測ズームセンター（1Hz差分速度 × 3秒先読み + lerp 0.25）でスムーズ追尾
  - 速度スパイクガード: 0.15°/s 超の推定速度は衛星位置にスナップして暴走防止
- **World Map 衛星ドットクリック選択**（`sat_clicked(int)` シグナル → `_select_satellite_by_norad` 接続）
- **フットプリント描画 QPainterPath スキャンライン方式**（polar cap・antimeridian・極境界弧の全ケース修正済み）
- **MainWindow `_shutdown_flag`（threading.Event）**: `closeEvent()` 冒頭でセット。バックグラウンドスレッド（`_refresh_satellite_names_sync`）が各 `asyncio.run()` 呼び出しの間でフラグを確認し、インタプリタシャットダウン後の `futures` スケジュールを防ぐ
- **`is_source_stale(source_name)` (TLEManager)**: `sync_log` を照会し、一度もフェッチされていないソース、または各ソース自身の `TLE_SOURCES[...]["update_interval_hours"]` より古いソースを検出（2026-08-11、経過時間チェックを追加。詳細は「起動時鮮度チェックの網羅的監査と修正」セクション参照）。起動時に未フェッチ/期限切れの cubesat/weather/science/earth-obs 等グループを即時フェッチするトリガーとして使用
- **`_sort_sources_by_priority()` (MainWindow)**: TLE_SOURCES の `priority` フィールドでソース名を昇順ソート。amateur より先に cubesat/weather 等を上書きしないよう順序を制御
- **GitHub Actions: `make_latest: true`**（`prerelease: true` を廃止）。3プラットフォーム全ビルドジョブで設定済み。最新リリース: `v0.1.0`
- **Open in SatNOGS（クロスプラットフォーム）**: 右クリックメニューから衛星の SatNOGS ページをアプリモードで開く。`_open_url_app_mode` に統一済み（Linux: `shutil.which` / macOS: `.app` 絶対パス / Windows: `Program Files` 絶対パス）。Chromium系が見つからない場合は `QDesktopServices.openUrl` にフォールバック。ネットワーク接続エラーと「本当に見つからない」を区別して表示（2026-07-04、`_satnogs_network_error` シグナル追加）
- **Comms Quick Panel**（右側 Satellite Detail パネル下部）— Communicationsタブ表示中にミニレーダー・衛星クイック選択・周波数ミラー・Rig/Rotator接続ボタンを表示。詳細は「Comms Quick Panel 設計」セクション参照
- **メニューバー構成**（v0.2.0 以降）
  - File / Satellite / Radio / **Communications** / **Autotrack/Record** / View / Help
  - **Communications**: サブメニュー APRS / Telemetry / SSTV・SSDV / FT4 / Q65 / **CW Decoder**（クリックで非常駐タブを開く。× で閉じる）
  - **Autotrack/Record**: サブメニューなし。クリックで AutotrackRecordDialog を開く
  - **View メニュー**: Language（English / 日本語、チェック可能な `QActionGroup`。切替後は再起動が必要。詳細は「多言語化ロードマップ」参照）・Time Zone（UTC / Local Time）
  - Radar・Pass Chart エントリは削除済み（タブ直接選択で十分。Dashboard追加によるインデックスずれ問題を根本解決）
- **フッター RIG ラベル**（`_update_rig_label`）: Hamlib リグだけでなく SDR（SdrRigAdapter）接続時も「RIG: 1」「RIG: 2」「RIG: 1+2」に更新。`RadioControlWidget` に `rig_disconnected` / `rig2_disconnected` シグナルを追加し、切断時も「RIG: Off」に戻るよう修正済み
- **Q65 Phase 1（RX）**（`src/comms/q65/codec.py` + `src/ui/q65_tab.py`）— libq65 ctypes デコーダー。WSJT-X ソースから CI でビルド（build-q65lib.yml / ソースパス: `lib/qra/q65/`）。Help > Q65 Library Installation でバンドル版を自動インストール
- **Q65 Phase 2（TX/QSO）**（`src/comms/q65/encoder.py` + `src/comms/q65/qso.py`）— 純 Python TX エンコーダー。GF(64) 線形符号・CRC-12・65-FSK 音声合成（WSJT-X `q65_encoding_modules.f90` をポート）。QSO ステートマシン（IDLE→CALLING→EXCHANGE→CONFIRM→LOGGED）。SQLite `q65_log` 永続化・ADIF エクスポート
- **CW Decoder タブ**（`src/comms/cw/codec.py` + `src/ui/cw_tab.py`）— [deepcw-engine](https://github.com/e04/deepcw-engine) ONNX モデルを使った AIベース CW（モールス符号）デコーダー（v0.2.6）
  - CRNN + CTC アーキテクチャ。`model.onnx`（約 15 MB）を GitHub から自動ダウンロード。onnxruntime は pip で自動インストール
  - 前処理パラメーター（`model.onnx.json` 準拠）: `SAMPLE_RATE=3200` Hz・`FFT_LENGTH=256`・`HOP_LENGTH=48`・`log1p` 正規化・reflect padding・65 bins（400–1200 Hz）・`blank_index=41`
  - 入力: SDR（`audio_ready` Signal）またはサウンドカード（sounddevice）。リグ不要（受信専用）
  - 20 秒のローリングバッファ。5 秒以上蓄積後に 5 秒ごとデコード実行
  - CW/CW-R トランスポンダー選択時に自動オープン（`cw_transponder_selected` Signal）
  - **Help → CW Model Installation…**（`src/ui/cw_model_dialog.py`）でワンクリックインストール
- **METEOR / HRPT 受信タブ**（`src/ui/meteor_tab.py` + `src/comms/meteor/satdump.py`）— SatDump サブプロセス経由の気象衛星画像受信
  - **対応衛星・パイプライン**（`METEOR_PIPELINES`）:
    | 衛星 | モード | 周波数 | サンプルレート | NORAD |
    |---|---|---|---|---|
    | METEOR-M N2-3 | LRPT | 137.9 MHz / 137.1 MHz（各72k・80k） | 1.2 Msps | 57166 |
    | METEOR-M N2-4 | LRPT | 137.1 MHz / 137.9 MHz（各72k・80k） | 1.2 Msps | 59051 |
    | METEOR-M N2-3 | HRPT | 1700.0 MHz | 3 Msps | 57166 |
    | METEOR-M N2-4 | HRPT | 1700.0 MHz | 3 Msps | 59051 |
    | NOAA 18 | HRPT | 1707.0 MHz | 3 Msps | 28654 |
    | NOAA 19 | HRPT | 1698.0 MHz | 3 Msps | 33591 |
    | Metop-B | HRPT | 1701.3 MHz | 3 Msps | 38771 |
    | Metop-C | HRPT | 1701.3 MHz | 3 Msps | 43689 |
  - **METEOR-M N2-3/N2-4 の LRPT 周波数（137.1/137.9 MHz）・シンボルレート（72k/80k）— 全4通りを
    パイプラインとして用意（2026-08-15 80kパイプライン追加 v0.3.21 → 2026-08-17 周波数選択肢も
    追加）**: N2-3・N2-4はLRPTの周波数（137.1 MHz ⇔ 137.9 MHz）・シンボルレート（72k
    デフォルト・80k）のどちらも運用側が予告なく切り替えることがある（usradioguy.comの
    運用ログで過去に何度も切り替えが確認されている。特にN2-4は打ち上げ後のテスト期間中に
    両周波数を頻繁に往復した実績がある）。周波数がずれている場合はSatDumpが信号自体を
    見つけられない（Viterbi UNSYNCED）。周波数は合っているがシンボルレートがずれている場合は
    **Viterbiは`SYNCED`になりSNRも数dBと妥当な値を示すのに、Deframerだけが永久に`NOSYNC`
    のまま**という紛らわしい症状になる（畳み込み符号の復号はシンボルレート誤差にある程度
    寛容だが、フレーム同期語の検出はタイミングのズレが蓄積して合わなくなるため）。仰角60度・
    SNR良好なN2-4パスで実際にこの症状（10分間Viterbi SYNCED・Deframer NOSYNC）が発生し
    確認済み。METEOR/HRPTタブのLockインジケーターは`Deframer: synced`を見て緑にする実装
    （本ファイル該当セクション参照）のため、これは検出ロジックの誤りではなくSatDump側の
    復調状態を正しく反映した結果だった。
    このため`METEOR_PIPELINES`にはN2-3・N2-4それぞれ「137.9MHz(72k)」「137.9MHz(80k)」
    「137.1MHz(72k)」「137.1MHz(80k)」の4エントリを用意している（各衛星の従来デフォルト
    周波数を選択肢の先頭に置いているだけで、優先度に意味はない）。どの組み合わせが実際に
    アクティブかは受信してみるまで分からないため、**パス開始前にどの組み合わせから
    始めても問題ない**（常に特定の組み合わせから試す必要はない）。**受信中の切り替えは
    不可**——`satdump live`はパイプラインID・周波数をコマンドライン引数として1回起動時に
    固定するため、切り替えるには「■ 停止」→ コンボで別の組み合わせを選択 →
    「▶ 開始」で再起動する必要がある（`_on_start()`実行中はコンボ自体が`setEnabled(False)`
    になる）。自動フォールバック機能（一定時間Deframerが同期しなければ自動で別の組み合わせへ
    再起動する等）は2026-08-17時点では未実装（検討したが、まずは手動切替のみで様子見という
    判断）。
  - **UI 構成**（コンパクト2行レイアウト）:
    - Row 1: `Pipeline:` コンボ + `[SDR Connect]` + `[▶ Start]` + `[■ Stop]` + `[📋 Log]`
    - Row 2: ロックインジケーター + プログレスバー + ステータスラベル
    - 下部: 受信画像（左）＋ 受信履歴サムネイル（右）の水平スプリッター
  - **[SDR Connect]**: Rig Settings > SDR Settings で設定済みの SDR に自動接続（`get_db_path()` でDBパスを正確に参照）
  - **[📋 Log]**: SatDump の stdout/stderr を表示する浮動ログウィンドウ（`_LogWindow`）を開く。× で閉じてもログ内容は保持
  - **Autotrack 連携**:
    - `autotrack_start(norad)` — NORAD ID に一致する最初のパイプラインを自動選択して SatDump を起動
    - `autotrack_stop()` — 実行中の SatDump を停止
    - `main_window._meteor_autotrack_aos(norad)` / `_meteor_autotrack_los()` — Autotrack AOS/LOS に紐付け
    - `_autotrack_meteor_record: bool` フラグ — Autotrack/Record ダイアログの「METEOR / HRPT Reception」チェックで制御
  - **トランスポンダーと周波数の独立性**: SatDump は `METEOR_PIPELINES` の固定周波数を使用するため、Autotrack に登録するトランスポンダーは受信に影響しない。ただし**衛星（NORAD ID）は受信対象と完全に一致させること**（AOS/LOS 計算が変わるため）
  - **`METEOR_NORAD_IDS`**: `frozenset({35865, 40069, 44387, 57166, 59051, 28654, 33591, 38771, 43689})`
  - Radio Control でトランスポンダー description に「LRPT」「HRPT」「METEOR」が含まれると自動オープン
  - **パイプライン選択 → 左側衛星リスト連動**: `MeteorTab.satellite_selection_requested(norad, downlink_hz)`
    シグナル（Pipeline コンボ変更時に発火）→ `main_window._on_meteor_satellite_requested()` が
    `_select_satellite_by_norad(norad)` で左リストを自動選択し、Radio Control のトランスポンダーも
    最寄りの LRPT/HRPT を自動選択する（`cc3d29b` で実装済み・双方向）。ただし対象衛星が
    `satellites` テーブルに行を持たない場合はリストに現れず選択できない（次項参照）
  - **NOAA 18/19 が衛星リストに出ない問題の原因と修正（2026-07-07）**: NOAA 18 (28654) と
    NOAA 19 (33591) はアマチュア無線トランスポンダーを持たない（SATNOGS同期経路では
    `satellites` 行が作られない）上、運用終了に伴い CelesTrak の `GROUP=WEATHER`
    キュレーションリストからも外れていた（個別 `CATNR=` 照会には今も応答するが、群一括取得
    には含まれない）ため、通常の自動TLE取得（`fetch_and_update`）が対象を一度も見つけられず
    `satellites` 行が永遠に作られない、というのが根本原因だった（Metop-B/C・METEOR-M2-3/4は
    現在も `GROUP=WEATHER` に含まれているため無関係に正常動作していた）。
    `TLEManager.fetch_meteor_tles()`（`src/data/tle_manager.py`）が `METEOR_NORAD_IDS` 全件を
    対象に、TLE未取得または非manual TLEが24時間超過している衛星のみ CelesTrak 個別 CATNR
    照会を行い、`satellites` 行を `INSERT OR IGNORE` で保証しつつ `tle_group='weather'` で
    TLE を保存する（`fetch_legacy_tles()` と同型の個別照会パターンだが、対象は既存の
    `satellites` 行ではなく固定の `METEOR_NORAD_IDS` から出発する点が異なる）。
    起動時に `_refresh_satellite_names_sync()` 内で一度実行され、以降は
    `meteor_tle_refresh` ジョブ（12時間ごと）が自動追従する。
- **AX100 Digi タブ**（`src/comms/ax100digi/` + `src/ui/ax100_digi_tab.py`、2026-07 実装）—
  MARMOTSat VHF デジピータ（145.875 MHz、GreenCube/IO-117 と同一の AX100 "ASM+Golay" GMSK
  プロトコル）の受信・送信。Rig+サウンドカード（SSBモード）・SDR 両対応。詳細設計は
  「AX100 Digi 機能設計」セクション参照
- CI緑（mypy strict + pytest）

### SDR 機能（v0.1.0 時点で実装済み）

- **SoapySDR バックエンド**（`src/sdr/`）: device・pipeline・demodulator・recorder
  - SdrDevice: SoapySDR デバイス列挙（audio/null/remote ドライバ除外）・オープン・ストリーミング
  - **Windows SDR — SoapySDR 根本的非互換（v0.1.72 確定）**: Windows では SoapySDR が WinUSB ドライバーと根本的に非互換。`SoapySDR::Device::make()` の ABI チェック層が enumerate 後にデバイスを拒否する（`hackrf_init()+hackrf_exit()` / `hackrf_open()` が WinUSB ハンドルキャッシュを破壊）。このため **Windows では RTL-SDR・HackRF のみ対応**。Airspy・Airspy HF+・ADALM-Pluto は Windows 非対応（SoapySDR が使える見込みがない）。
  - **Windows RTL-SDR ctypes直接実装**（`RtlSdrDirectDevice` in `src/sdr/device.py`）: `sys.platform=="win32"` かつ `driver=="rtlsdr"` の場合のみ `librtlsdr.dll` を ctypes で直接呼ぶバイパス実装。uint8 I/Q → complex64 変換: `(sample - 127.5) / 127.5`。**動作確認済み v0.1.71（2026-06-25）**
  - **Windows HackRF ctypes直接実装**（`HackRfDirectDevice` in `src/sdr/device.py`）: `sys.platform=="win32"` かつ `driver=="hackrf"` の場合のみ `hackrf.dll` を ctypes で直接呼ぶバイパス実装。`hackrf_start_rx()` + `ctypes.CFUNCTYPE` コールバックによる非同期ストリーミング。signed int8 I/Q → complex64 変換: `arr / 128.0`。LNA（0-40dB, step 8）+ VGA（0-62dB, step 2）ゲイン分割。**`_cb_func` を `self` に保持して GC クラッシュを防止すること**。**v0.1.72 実装済み・実機確認待ち**
  - **両デバイスとも Zadig で WinUSB ドライバーを当てる必要がある**（初回一回限り）。libusbK は絶対に選ばない（クラッシュ実績）。
  - SDRPipeline（QThread）: I/Q 取得 → FFT（10fps スペクトラム）→ 復調 → 音声出力 → IQ 録音
  - Demodulator: NFM / USB / LSB / CW 各モード。DC ブロック IIR（30Hz HPF）で HackRF DC スパイク除去
  - CW 復調: エンベロープ検出なし・直接復調方式（ブーン音問題を根本解決）
- **SdrRigAdapter**（`src/rig/controller.py`）: RigController を継承し SDR を Rig として扱う
  - `is_sdr = True` プロパティで UI 側が SDR スロットを識別
  - connect() で sample_rate / ppm / gain / bias_tee を一括適用
- **Rig Settings > SDR Settings タブ**（第3タブ）
  - デバイス列挙・選択、サンプルレート、PPM補正、RFゲイン（Auto/Manual）
  - **Bias-T ON/OFF チェックボックス**（ドライバ別キー自動選択: HackRF=`bias_tx`/`"true"`, RTL-SDR=`biastee`/`"1"`）
  - Rig 1 / Rig 2 割り当てラジオボタン（割り当てたスロットの Hamlib タブを自動グレーアウト）
  - Hamlib バージョン表示は Rig 1/2 タブのみ（SDR タブには非表示）
  - **「Serial:」行は Windows では「実際にシリアルがある場合のみ」表示**（2026-08-05）—
    Windows の RTL-SDR/HackRF は `SoapySDR::Device::make()` をバイパスして ctypes で DLL を
    直接叩くうえ、WinUSB 対応でパッチした `findRTLSDR` は実機に問い合わせず `device_index=0`
    のエントリを返すだけなので（`_win_filter_rtlsdr_by_count()` の docstring 参照）、この欄は
    空になる。「Serial: —」が固定表示されるのを故障と誤解したユーザーからの問い合わせが
    実際にあった。
    一方、**SoapyRemote 経由のリモートSDRだけは例外**で、サーバー側から転送された実際の
    シリアルが入る（この経路は ctypes バイパスを通らない）。そのため「Windowsなら一律非表示」
    ではなく、`_update_serial_row(serial)` が `QFormLayout.setRowVisible()`（Qt 6.4+、
    本プロジェクトは PySide6>=6.6 要件のため使用可）で**値が実際に入っているかどうか**を
    基準に出し分ける。ドライバー名では判定していない。Windows 以外は従来通り常に表示（空なら
    「—」）。テストは `tests/test_rig_dialog_sdr.py`（`sys.platform` を monkeypatch し、
    Linux／Windows×シリアル有無／デバイス切替時の再非表示の4パターンを検証。ウィジェットの
    `isVisible()` は表示していないパネルでは常に `False` を返すため、レイアウト側の
    `isRowVisible()` で判定していることに注意）
  - **PPM Correction の「Measure…」ボタン**（自動クロックドリフト測定、`src/sdr/ppm_measure.py`、
    2026-08-14〜15実装）: 基準信号不要で`rtl_test -p`と同じ原理（設定サンプルレートと
    実際に受信したサンプル数の比較）でppmを自動測定する。実測を通じて2回の設計変更を経た:
    - **1回目（バッファ到着の最初/最後の2点だけで比率を計算）**: 同一ハードウェアの
      連続実行で+1700ppm・+3615ppmと大きくブレた。原因はSoapySDR/RTL-SDRがハードウェア
      クロックで駆動される固定サイズチャンク（RTL-SDRは131072サンプル、約131ms周期）で
      データを届けるため、測定窓の最初と最後のチャンクだけがソフトウェア側のポーリング
      タイミングに対して最大1チャンク分（30秒測定で最大約4400ppm相当）のランダムな
      位相誤差を持ってしまうため。バックログを空読みしてから測定開始する対策も試したが
      改善しなかった（位相のランダム性自体は排出処理では解消できないため）
    - **2回目（最小二乗フィット、採用）**: 成功した全チャンク到着を`(経過時間, 累積
      サンプル数)`点として記録し（30秒で約230点）、直線を最小二乗フィットしてその傾きを
      実効レートとする。個々の点の位相ジッターはフィットの切片をずらすだけで傾きには
      ほぼ影響しないため、多数の点で平均化されバイアスが実質的に解消される（実測で
      -6.8〜+1.2ppm程度まで収束）
    - **Windowsでは「Measure…」ボタン自体を非表示**（`sys.platform != "win32"`）:
      `RtlSdrDirectDevice`（Windows専用のRTL-SDR ctypes直接実装、本ファイル該当セクション
      参照）は`rtlsdr_read_sync()`という同期ブロッキング呼び出しを使っており、SoapySDR
      Linux/macOS版のような非同期コールバックキューを持たない。実測で1回あたり約25msの
      「無駄時間」が挟まることが確認され、常に約-23000ppmという（水晶発振器としてあり
      得ない、2.3%もの誤差）非現実的な値が出た。根本修正には`HackRfDirectDevice`同様
      `rtlsdr_read_async()`への書き換えが必要だが未着手のため、信頼できない数値を見せる
      くらいならボタン自体を出さない方針とした
    - 測定時間は当初30秒→精度向上のため60秒に延長（ウォームアップ5秒込みで計約65秒）
  - **RF Gain の Auto/Manual 選択が再起動のたびにAutoへ戻るバグと修正（2026-08-15）**:
    `_SdrSettingsPanel.load()`が`self._gain_auto_rb.setChecked(gain_auto)`のみを
    呼んでおり、`gain_auto=False`（Manual選択）の場合これは事実上のno-opだった——Qtの
    ラジオボタンは同一親の下では暗黙的に排他グループを形成し、**そのグループ内で唯一
    チェック済みのボタンに対して`setChecked(False)`を呼んでも黙って無視される**
    （チェックを外したいなら別のボタン側へ`setChecked(True)`を呼ぶ必要がある、という
    Qtのよく知られた仕様）。DB保存自体（`gain_db`含む）は正しく行われていたため実害が
    分かりにくく、画面は常にAuto表示（dB欄も連動して無効化）のまま復元されていた。
    `if gain_auto: self._gain_auto_rb.setChecked(True) else:
    self._gain_manual_rb.setChecked(True)`に修正。**この「排他グループの片方だけに
    setChecked(False)を呼んでも効かない」パターンは、他のラジオボタン群のload()実装
    （Rig1/2パネルの各種モード選択等）にも同種の潜在バグがないか、今後同じ症状の報告が
    あれば疑うこと**
- **SDR Control タブ**（常時表示・SDR未接続時はパネルをグレーアウト）
  - スペクトラムアナライザ（QtCharts、10fps）— 中心周波数は赤い縦線マーカーで表示
    （`center_freq_changed` Signal）。**チャート上部にあった「RX:」周波数ラベルは
    2026-08-05 に削除済み**: Passband Tune 枠の「Freq:」欄が同じ `_on_center_freq()`
    から更新される完全に同一の値を表示しており、狭いタブで縦幅を二重に消費していたため
  - **Passband Tune パネル**: ◀◀/◀/▶/▶▶ ボタン + ステップ選択（100Hz〜10kHz）+ オフセット表示 + Reset
    - SDR が Rig 1/Rig 2 どちらでも動作
    - Lock ON 時: 相手リグの TX を自動追従（反転トランスポンダーは符号反転）
    - トランスポンダー切り替え時にオフセット自動リセット
  - デモジュレーター（モード選択・ボリューム・AGC・Start/Stop Audio）
    - **MP3音声録音**（`● REC Audio` / `■ STOP` / `📁`）— `lameenc` によるピュアPythonエンコード、外部ツール不要
  - IQ レコーダー（帯域幅選択・REC/STOP・経過時間表示）
    - **📁ファイルマネージャーボタン**（IQ・Audio 両方）— SDR未接続時も常時クリック可能。巨大IQファイルの削除に使用
  - トランスポンダー選択に連動したモード自動切替（Connect 前でも反映）
- **Help > Clear TLE Sync History…**（2026-08-10 追加）
  - `sync_log` からTLE関連の全エントリ（`celestrak-active`・`satnogs-provisional`・
    `legacy-tle-check`・`meteor-tle-check`・`celestrak-{stations,amateur,cubesat,weather,
    earth-obs,science}`）を確認ダイアログ付きで削除
  - 開発環境ではDBを直接SQLで触って`celestrak-active`の記録を消せるが、リリースビルドには
    その手段がないため追加。次回の起動・定期ジョブ・Update TLEが「一度も取得したことがない」
    扱いで全ソースを再取得する
  - TLEデータ自体は消さない。あくまで「いつ最後に取得したか」の記録のみ削除
- **Help > Hamlib Update…**（in-app Hamlib アップデーター）
  - GitHub Releases から最新 hamlib バンドルをダウンロード・展開・ユーザーディレクトリへインストール
  - Linux / Windows / macOS 対応
- **Help > Check for Updates…**（アプリ自動更新）
  - GitHub Releases API で最新バージョンを確認
  - Windows: インストーラー（.exe）をダウンロードしてサイレントインストール
  - Linux: AppImage を置き換え
  - macOS: dmg をマウントして .app をコピー
- **Windows NSIS インストーラー形式**（ZIP 配布から変更）
  - `scripts/installer.nsi`: スタートメニュー・デスクトップショートカット・Add/Remove Programs 登録
  - サイレントインストール（`/S` フラグ）対応
- **実動作確認済みリグ・デバイス**（2026-06-13）
  - FTX-1F（Hamlib 4.7.1 モデル1051、NET Control）: ドップラー補正・VFO制御・CTCSS 動作確認済み
  - FTX-1F（Hamlib 4.7.1 モデル1051、Direct モード）: モード・CTCSS（raw CAT `MD1/MD0/CN1/CT1` via `os.open()`）動作確認済み（2026-06-18）。スプリット（`FT1;`/`FT0;`）動作確認済み（2026-06-29）
  - FT-991AM（Hamlib 4.7.1 モデル1036、NET Control）: ドップラー補正・VFO制御・CTCSS 動作確認済み
  - FT-991/FT-991A/FT-991AM（Direct モード、Hamlib model 1035）: スプリット・周波数・モード・CTCSS すべて動作確認済み（2026-06-28）
    - スプリット ON/OFF: `FT3;` / `FT2;`（`ST` コマンドは FT-991A 非対応で `?;` を返す）
    - トランスポンダー選択時: pyserial で `FA`/`FB`/`FT3;` をプリセット（Connect 前からリグ表示に反映）
    - アプリ終了時: `FT2;` でシンプレックスに復帰
    - モード: `SV/MD0` via pyserial（VFO-B は SV スワップ必須）
    - CTCSS: `CN0/CT0` via pyserial（SV スワップ不要・TX-VFO にグローバル適用）
  - IC-9100（Hamlib 4.7.1 モデル3068、Direct モード）: クロスバンド・同バンド両方の周波数・モード・CTCSS 動作確認済み（v0.1.27・2026-06-25）— SAT モード ON/OFF・ドップラー補正・VFO 逆転バグ修正済み。**Windowsでは接続方法に注意**（USB-B端子＋ICOM純正ドライバー必須。詳細は「Windows Direct モード — USB接続方法の注意」セクション参照）
  - IC-9100（Hamlib 4.7.1 モデル3068、NET Control）: クロスバンド・同バンド両方の周波数・モード・CTCSS 動作確認済み（v0.1.27・2026-06-25）
  - IC-9700（Hamlib 4.7.1 モデル3081、Direct/NET モード）: Linux・Windows 両方で IC-9100 と同様に動作確認済み（v0.1.27・2026-06-25）— `_SATMODE_USE_VFO_SUB` 分岐（VFO_SUB for UL）使用
  - IC-705（Hamlib 4.7.1 モデル3085、Direct/NET モード両方、Linux・macOS）: 周波数・モード・CTCSS（トーン周波数＋エンコードON/OFF）・スプリット全て動作確認済み（2026-07-06〜07）— 汎用（非satmode）Icom CI-Vリグとして初の実地検証。Connect後にドップラー補正でMain表示が固定される不具合（生CI-V応答未読み取りによる通信デシンクが原因）を2026-07-07にLinux/macOS両方で確認・修正済み。詳細は「IC-705 (Hamlib model 3085) — 汎用（非satmode）Icom CI-Vリグの参照実装」セクション参照。Windows 未確認
  - HackRF One（SoapyHackRF）: NFM/USB/CW 復調・スペクトラム・Bias-T 動作確認済み（Linux）
  - HackRF One（ctypes直接実装 `HackRfDirectDevice`）: **Windows 実装済み v0.1.72（2026-06-25）** — 実機確認待ち。Zadig で WinUSB ドライバー適用要
  - RTL-SDR（SoapyRTLSDR）: 基本動作確認済み（Linux）
  - RTL-SDR（ctypes直接実装 `RtlSdrDirectDevice`）: **Windows 動作確認済み（v0.1.71・2026-06-25）** — Zadig で WinUSB ドライバー適用要
  - Airspy R2・Mini（SoapyAirspy）: Linux/macOS brew/apt 対応（実機未確認）**Windows 非対応**
  - Airspy HF+（SoapyAirspyHF）: Linux/macOS brew/apt 対応（実機未確認）**Windows 非対応**
  - ADALM-Pluto（SoapyPlutoSDR + libiio）: Linux/macOS のみ対応 **Windows 非対応**
  - Rig 1（FTX-1F）+ Rig 2（RTL-SDR）デュアル構成: Passband Tune + Lock 連動動作確認済み

### Communications 機能（feature/communications ブランチ・v0.2.0 実装済み）

**ディレクトリ構成:**
```
src/
├── comms/
│   ├── aprs/
│   │   ├── engine.py       # APRSEngine — Direwolf/SDR 両パス統合・PTT制御
│   │   ├── parser.py       # AX.25 フレームデコード・APRS パース
│   │   ├── afsk_demod.py   # Bell 202 AFSK 1200 baud デモジュレーター（SDR 受信パス）
│   │   └── direwolf.py     # Direwolf サブプロセス管理・KISS TCP クライアント
│   ├── telemetry/
│   │   └── decoder.py      # テレメトリーフレームデコーダー（JSON 定義ベース）
│   ├── sstv/
│   │   ├── decoder.py      # SstvDecoder — pySSTV ラッパー（Robot36/PD120 等）
│   │   └── ssdv.py         # SsdvDecoder — ssdv CLI サブプロセス管理
│   ├── ft4/
│   │   ├── codec.py        # Ft4Codec — ft8_lib ctypes ラッパー（エンコード・デコード）
│   │   ├── scheduler.py    # Ft4Scheduler — 6秒周期タイミング管理（UTC アライン）
│   │   └── qso.py          # Ft4QsoManager — QSO ステートマシン・ft4_log DB 操作
│   ├── cw/
│   │   ├── model_info.py   # モデルパス管理・onnxruntime 検出・ダウンロード URL
│   │   └── codec.py        # CwDecoder — deepcw-engine ONNX 推論・前処理（CRNN + CTC）
│   └── q65/
│       ├── codec.py        # Q65Codec — libq65 ctypes RX デコーダー（Phase 1）
│       ├── encoder.py      # 純 Python TX エンコーダー — GF(64)・CRC-12・65-FSK 音声合成（Phase 2）
│       ├── scheduler.py    # Q65Scheduler — 15/30/60 秒周期タイミング管理
│       └── qso.py          # Q65QsoManager — QSO ステートマシン・q65_log DB 操作・ADIF エクスポート
├── data/
│   └── telemetry_formats/  # 衛星ごとのバイナリテレメトリーフォーマット定義（JSON）
```

**メニュー: Communications > APRS**（`src/ui/aprs_tab.py`）
- 受信ログ（タイムスタンプ / コールサイン / Via / 内容）
- 入力ソース自動切替: SDR → Bell 202 AFSK 受信専用 / Rig+サウンドカード → Direwolf TX/RX
- **メッセージ送信**: To / Message フォーム + Send ボタン（Rig+Direwolf 接続時のみ有効）
- **自局位置送信**（"Send My Position" グループ）:
  - `Auto-beacon every N min` チェックボックス（1〜60分間隔、ON時即時送信）
  - シンボル選択（Fixed Station `/-` / Mobile `/>` / Balloon `/O` / Antenna `/Y` / Satellite `/S`）
  - Comment テキスト（最大43文字）
  - Send Now ボタン
  - QTH座標を `LocationManager.load_saved()` から自動取得・表示
- **APRS位置パケット → Dashboardマップピン表示**（シアン▲マーカー + コールサインラベル）
  - `aprs_stations_updated(dict)` シグナル → `WorldMapView.set_aprs_stations()`
  - タブクローズ時 `aprs_stations_cleared()` → `WorldMapView.clear_aprs_stations()`
- ADIF エクスポート（.adi ファイル）
- SQLite `aprs_log` テーブルへ自動永続化

**メニュー: Communications > Telemetry**（`src/ui/telemetry_tab.py`）
- AX.25 フレーム受信 → JSON フォーマット定義でフィールドデコード
- 定義なし衛星は生 hex + 衛星名表示
- CSV エクスポート
- SQLite `telemetry_log` テーブルへ自動永続化
- **受信モード切り替え**:
  - **Bell 202 AFSK**: `src/data/telemetry_formats/` にフォーマット定義がある衛星のみコンボで選択可（12機）。Start 押下時 SDR 未接続なら自動接続を試みる
  - **gr-satellites**: インストール済みの場合のみ選択可。gr-satellites がサポートする全衛星（330機以上）をコンボで選択。Start 押下時 SDR に IQ を UDP 転送して gr_satellites サブプロセスを起動
- **衛星コンボ → メインリスト自動連動**: コンボで衛星を選択するとフィルタを「All Satellites」に切り替えたうえでメインの衛星リストでも自動選択
- **トランスポンダー自動選択**: 衛星選択時に description に「TLM」「Telemetry」を含むトランスポンダーを優先選択（なければ AFSK: type=Beacon→mode=AFSK→mode=CW、gr-satellites: YAML 周波数に最近傍）
- **gr-satellites バックエンド**（`src/comms/telemetry/gr_satellites_backend.py`）:
  - `detect_gr_satellites()` — gr_satellites CLI の検出
  - `list_gr_satellites_with_names()` — YAML から (norad, name) リストを返す
  - `get_satellite_info(norad)` — YAML から name・transmitters・frequencies を返す
  - `GrSatellitesBackend(QObject)` — サブプロセス管理・IQ UDP 転送・stdout パース
  - `_UdpIqForwarder` — SDR パイプラインから UDP でサンプルを送信

**メニュー: Communications > SSTV / SSDV**（`src/ui/sstv_tab.py`）
- SSTV 受信: pySSTV（Robot36/PD120/Martin/Scottie）、SDR audio_ready または sounddevice 入力
- SSDV 受信: AX.25 `raw_frame_received` Signal をタップ → ssdv CLI でデコード
- プログレッシブ画像表示・受信履歴サムネイル・PNG 手動/自動保存
- SQLite `sstv_log` テーブルへ自動永続化
- Radio Control でトランスポンダー説明に「SSTV」「SSDV」「IMAGING」が含まれると自動オープン

**メニュー: Communications > FT4**（`src/ui/ft4_tab.py`）
- **リグ（トランシーバー）必須**。SDR 単体では TX 不可
- 対応入力構成:
  - 標準: リグ + サウンドカード（Rig Settings > Sound Card で設定）
  - 上級: リグ 1（TX）+ SDR（RX）— SDR 接続時に RX Input で切り替え
- デコードメッセージ一覧（UTC / dB / DT / Hz / Message）
- TX クイックボタン: CQ / RST / R+RST / RR73 / 73
- QSO ステートマシン自動進行（IDLE→CALLING→EXCHANGE→CONFIRM→LOGGED）
- デコードメッセージのダブルクリックで応答シーケンス開始
- SQLite `ft4_log` テーブルへ永続化・ADIF エクスポート
- Radio Control でトランスポンダー説明に「FT4」「FT8」が含まれると自動オープン
- ft8_lib 未インストール時は赤バナー表示・TX Enable 無効化。インストール先: `~/.local/share/fbsat59/ft8lib/`
  - **Help → ft8lib Installation…** でバンドル版を自動ダウンロード・インストール（`src/ui/ft8lib_dialog.py`）
  - CI で `.github/workflows/build-ft8lib.yml` が毎週 kgoba/ft8_lib 最新タグを監視してビルド（Linux/Windows/macOS）

**FT4 拡張デコーダー — libft4wsjt（2026-07-04 実装）**

kgoba/ft8_lib（上記）は作者自身が「マイコン向け軽量参照実装」と明言する通り単一パス・BP（belief propagation）のみのデコーダーで、WSJT-X本家が持つ「3パス信号減算＋BP/OSD（Ordered Statistics Decoding）ハイブリッド復号」を持たない。RS-44の実パスで検証した結果、混雑した帯域ではWSJT-X本家に対し復号局数が大きく劣ることが判明したため、WSJT-X本体の実際のFT4デコードエンジン（`lib/ft4_decode.f90` 以下）を共有ライブラリとして移植した。

- `src/comms/ft4/wsjt_decoder.py`: `Ft4WsjtDecoder` — libft4wsjt ctypes ラッパー（RXのみ。TXは引き続きft8_lib）
- `Ft4Codec.decode_audio()`（`src/comms/ft4/codec.py`）: libft4wsjt 利用可能なら自動的にそちらを使用し、無ければ ft8_lib 単一パス復号にフォールバック。`decode_backend` プロパティで `"wsjtx"` / `"ft8lib"` / `"none"` を判定可能
- `scripts/build_ft4wsjt.sh` + `scripts/wsjtx_bridge/`: WSJT-X ソース（`lib/ft4_decode.f90` とその依存閉包）から `libft4wsjt.{so,dylib,dll}` をビルド。GUI/Qt/ネットワーク層は一切引き込まない
  - 依存閉包は静的解析＋実リンクで確定済み（`lib/ft4/*` の中核 + `lib/ft8/decode174_91.f90`・`osd174_91.f90` 等の LDPC/OSD 共有部 + `lib/77bit/packjt77.f90` 等の共通ユーティリティ）
  - `ft4wsjt_bridge.f90`: `ft4_decode` モジュールのFortranコールバック手続きポインタをC関数ポインタへ橋渡しするbind(C)ブリッジ（自前保守）
  - `normalizebmet.f90`: 本来 FT8 専用の `ft8b.f90` 末尾にのみ定義されているが FT4 側からも呼ばれる小さなサブルーチンを単体ファイルとして抽出・保守（アップストリーム変更時にドリフトする可能性あり、要注意）
  - 新規ネイティブ依存: `libfftw3`（本家と同じFFTW3ベースFFT）・Boost（`crc14.cpp` が `boost::augmented_crc` を使用。header-onlyで`libboost-dev`等で足りる）
- ft8_lib のみ利用可能（libft4wsjt 未インストール）の場合は青バナーで案内表示（TX/RXともブロックしない、情報提供のみ）
- **Help → FT4 Enhanced Decoder Installation…** でバンドル版を自動ダウンロード・インストール（`src/ui/ft4wsjt_dialog.py`）。インストール先: `~/.local/share/fbsat59/ft4wsjt/`
- CI で `.github/workflows/build-ft4wsjt.yml` が毎週 WSJT-X 最新リリースを監視してビルド（Linux/Windows/macOS）
  - **2026-07-05 に3プラットフォーム全て `workflow_dispatch` で実際に走らせグリーン確認済み**（Linux 55秒 / macOS 40秒 / Windows 2分47秒）。CI実装中に発覚した3件の不具合（Apple Silicon Homebrewのインクルード/ライブラリパス・Windows condaソルバーの遅さ・FFTW3ランタイムDLL同梱漏れ）とその対処は本ファイル「CI/CD トラブルシューティング履歴」内「ft4wsjt（libft4wsjt）ビルド固有」を参照
  - `ft4wsjt-bundle` プレリリースタグは既に3プラットフォーム分のアセットが公開済みで、`Help` メニューからのダウンロードが機能する
  - **v0.2.9（2026-07-04 リリース、3プラットフォーム全ビルド成功）でアプリ本体に本機能を含めて公開済み**。`Help > FT4 Enhanced Decoder Installation…` は v0.2.9 以降の AppImage/.exe/.dmg から利用可能
- テスト: `tests/test_ft4_wsjt_decoder.py`（libft4wsjt 未インストール時は skip）。輻輳帯域回帰テストで ft8_lib フォールバックと比較し、弱信号の復号漏れがないことを確認

**実運用でデコード数が伸びなかった問題の原因調査（2026-07-05 判明・修正済み）**

libft4wsjt導入直後、輻輳帯域シナリオのユニットテストではft8_libフォールバックより明確に優れていた
にもかかわらず、実際の受信ではWSJT-X本体が多数デコードできるパスでもFBSAT59はほとんど
デコードできない、という報告があった。調査の結果、原因はlibft4wsjt自体ではなく
`src/comms/audio_device_manager.py`（後述「共有サウンドカードアクセス設計」参照）の
`_resample()`にあった不具合と判明した。

共有入力ストリームは常に48kHzで開かれ、FT4は12kHzを要求する（48000 % 12000 == 0）。この
「割り切れる」ケースはアンチエイリアシングフィルタを一切通さない単純間引き（`chunk[::4]`）を
使っていたため、6kHz以上の帯域のノイズがFT4の復号帯域（200–3000Hz）へ直接折り返し、
実測で約6dB（ノイズパワー換算で約4倍）もノイズフロアを悪化させていた。弱信号デジタルモードに
とって6dBのSNR劣化は致命的で、「WSJT-X本体は同じ音声で多数デコードできるのにFBSAT59は
少数しかデコードできない」という症状と一致する。

このバグは2026-07-04のlibft4wsjt導入より前、2026-07-01の共有サウンドカード実装
（`_resample()`新設）時点から存在しており、ft8_lib単体運用時代から実質的にFT4の弱信号復号を
妨げていた。**libft4wsjt自体の復号性能向上は正しく機能している**（`test_ft4_wsjt_decoder.py`の
ユニットテストは合成音声を直接12kHzでデコーダーに渡すため`_resample()`を経由せず、このバグの
影響を受けていなかった。そのためテストは一貫してパスし続けていた）。

**修正**: `_resample()`の整数比専用高速パス（フィルタなし間引き）を廃止し、非整数比と同じ
`scipy.signal.resample_poly`（アンチエイリアシングFIR内蔵）経路に統一（コミット `c0f92a1`）。
CW Decoder（48000→3200）など、同じ整数比ダウンサンプルを使う他のCommunicationsタブにも
同様に恩恵がある。

**教訓**: 復号エンジン（libft4wsjt）が単体テストで実証済みに優秀でも、その手前の音声パイプライン
（サンプルレート変換）でノイズを注入していれば効果は実運用で見えない。「デコーダーを差し替えても
性能が上がらない」系の報告を受けたら、デコーダー自体だけでなく、音声取り込み・リサンプリングの
経路全体（特に合成テスト音声では素通りしてしまう共有パイプライン）を疑うこと。

**FT4 音声到達確認ツール（2026-07-06 実装）**

上記のアンチエイリアシング修正後もRS-44の実パスで0局デコードという報告があり、調査の過程で
「そもそもFT4タブに正しい音声が届いているか」を確認する手段がRig Settings > Sound Cardの
RXレベルメーターしかなく、しかもそのメーターはデバイスコンボの変更イベント（Refreshボタン等）
でしか再起動しないため開いた直後は無反応に見える、という診断性の低さが判明した。これを受けて
FT4タブ自体に2つの診断機能を追加した。

- **RX Levelメーター**（Configuration行、`src/ui/ft4_tab.py`）: `_audio_callback()` /
  `_on_sdr_audio_chunk()`（既存のRX音声取り込みコールバック）にピークdBFS計算を追加し、
  `level_updated` Signal 経由でQProgressBar+ラベルに反映。タブを開いている間・音声購読が
  アクティブな間は常時ライブ更新される（Rig Settings側のメーターのように再起動操作は不要）。
  色分けは同ファイルの`_start_meter()`と同じ基準（緑<-12dBFS・黄<-3dBFS・赤≥-3dBFS＝クリップ警告）
- **Show Waterfallポップアップ**（`src/ui/ft4_waterfall_dialog.py`・`Ft4WaterfallDialog`）:
  WSJT-X風のスクロール式スペクトログラムを表示する非モーダルダイアログ。`_on_rx_period_ended()`で
  RX期間終了ごとに直前6秒分の音声から`compute_display_spectrogram()`（単純STFT、200〜3000Hz帯）
  を計算し、直近`_HISTORY_PERIODS`（5周期＝30秒）分の履歴に追加して描画。デコードできた局があれば
  その周期の帯域内にのみ白の縦線マーカーを重ねる（履歴全体を貫く線ではない）
  - **軸の向き**: 横軸=周波数（Hz、左が低い）・縦軸=時間（上が最新＝新しい周期が上端に入り、
    古い周期は下へ押し出されてスクロールする。WSJT-Xのウォーターフォールと同じ「水が上から
    下へ流れ落ちる」向き）。目盛りは6秒（1周期）ごと（6s/12s/18s/24s/30s）
  - **2026-07-06、2度の修正を経て現在の設計に到達**:
    1. 初版は目盛り・軸ラベルが一切なく、軸の向きもWSJT-Xと逆（横軸=時間・縦軸=周波数）で
       ユーザーが読み取れないという指摘 → 軸ラベル追加＋WSJT-X慣例（横軸=周波数・縦軸=時間）
       に反転
    2. さらに、6秒ごとに画面全体が別の絵にまるごと差し替わるのは不自然という指摘 →
       `deque(maxlen=_HISTORY_PERIODS)`で複数周期分の生スペクトログラムを保持し、新しい周期を
       毎回連結して描画するスクロール方式に変更。連結時、各周期内のフレーム順序を
       `np.flip(axis=0)`で反転させてから（新しい方＝周期の末尾フレームを先頭に）周期の新しい順に
       つなげる必要がある点に注意 — これをしないと周期境界ごとに時間が0に巻き戻る不連続な
       見た目になる（最初の指摘の「不自然」の直接の原因）
  - **意図的に`codec.compute_waterfall()`とは別実装**: decode用のwaterfall計算は
    `ftx_find_candidates()`向けの time/frequency oversampling・固定小数点レイアウトに
    厳密に依存しており（前述「ウォーターフォール計算」セクション参照）、表示用ヘルパーを
    そこに結合すると誤って触った際にデコード自体を壊すリスクがあるため、完全に独立した
    シンプルな STFT を新設した
  - デコード可否に関わらず更新される（`decode_available`が false でも波形は見える）。
    「音は来ているが実際にFT4のトーンパターンが見えるか」を判断する二次診断として機能する
  - 6秒に1回の描画更新であり、WSJT-Xのような連続リアルタイムウォーターフォールではない
    （履歴を保持したスクロール表示にはなったが、更新頻度自体はRX期間終了時のみ）
  - ダイアログが非表示の間は`isVisible()`チェックで計算自体をスキップ（CPU負荷回避）。
    `hideEvent()`で履歴をクリアするため、別のパスで再度開いたときに無関係な古い音声と
    継ぎ目なく繋がって見えることはない

**RX Levelメーターが実機で振れなかった根本原因と修正（`AudioDeviceManager`のデバイス検証、2026-07-06）**

上記の診断ツール実装後、実機（FTX-1接続）でFT4タブのRX Levelメーターが振れず、Rig Settings
側のメーターは振れる、という報告があった。調査の結果、`soundcard_settings`に保存された
`input_device_index`（PortAudioの数値インデックス）が、保存時点から実機の状態が変わったこと
で**別のデバイスを指すようになっていた**ことが直接の原因と判明した（例: 保存値が指すインデックス
が現在は`max_input_channels=0`の"surround51"を指しており、録音不可能）。

PortAudioのデバイスインデックスはOS再起動やUSBオーディオ機器の抜き差しのたびに並び順が変わり
うる（本ファイル既出の「Linux/PipeWireピニング」設計セクション参照）。Rig Settings側のメーター
は開くたびに`sd.query_devices()`で**その場で再列挙**してデフォルトを選び直すため影響を受けない
が、FT4・CW・SSTV等の各タブは起動時にDBから読んだ**古い数値インデックスをそのまま**
`sounddevice.InputStream(device=N, ...)`に渡していたため、無効化したインデックスに当たると
PortAudioの生の例外（`except Exception as exc: status_label.setText(f"Audio open error: {exc}")`
等）が出るのみで、原因（デバイス番号のズレ）が全く分かりにくいメッセージになっていた。

**修正**: `src/comms/audio_device_manager.py`に`_validate_input_device(device)`を新設し、
`_SharedInputStream._open()`（全Communicationsタブが共有する唯一の入口）内、実際に
`sd.InputStream()`を呼ぶ前に検証する:
- `device=None`（システムデフォルト）は常に許可（検証スキップ）
- `sounddevice.query_devices`が無い環境（テストのフェイクモジュール等）も検証スキップし、
  実際の`InputStream()`呼び出しにエラー判定を委ねる
- インデックスが現在のデバイス一覧の範囲外、または`max_input_channels<=0`の場合は
  `RuntimeError`で「Rig Settings > Sound Cardを開いて入力デバイスを再選択してください」と
  明示するメッセージを送出

`_SharedInputStream.add_subscriber()`側も、`_open()`が例外を送出した場合は追加しかけた
subscriber登録を`pop()`で巻き戻してから re-raise するよう修正（開けなかったストリームに
購読者だけが残る不整合を防止）。この検証は1箇所（`AudioDeviceManager`）に集約されているため、
FT4・CW Decoder・SSTV/SSDV・APRS(Direwolf)・Rig Settingsメーターすべてに同時に効く。

テスト: `tests/test_audio_device_manager.py`の`TestInputDeviceValidation`
（`query_devices()`を返すフェイク`sounddevice`モジュールを使い、範囲外インデックス・
`max_input_channels=0`のデバイス・正常なデバイス・`device=None`・`query_devices`欠如の
5パターンを検証）。

**教訓**: 数値デバイスインデックスをDBに保存して後から使い回す設計は、USB機器の抜き差しや
OS再起動をまたぐと静かに破綻する。「片方のUIでは動くのにもう片方では動かない」という報告を
受けたら、両者が同じ設定値をどう解決しているか（その場で再列挙 vs 保存値をそのまま信用）の
違いをまず疑うこと。

**音声レベル修正後もRS-44高仰角パスでほぼデコードできなかった根本原因と修正
（デコード処理のメインスレッドブロッキング、2026-07-09）**

上記のノイズフロア修正・入力デバイス検証修正に加え、実機（FT-991A）側のPipeWire入力ゲイン
（40%→100%）とリグの`073 DATA OUT LEVEL`（50→80）も是正しRXレベルメーターが適正に振れる
状態になった後も、最大仰角80°のRS-44パス（WSJT-X側では10局以上デコード）でFBSAT59は
1局しかデコードできない、という報告があった。入力音量・デコードエンジン（libft4wsjt）双方が
既に検証済みで問題ないことが分かっていたため、残る説明変数はタイミング関連の不具合だった。

**原因**: `Ft4Tab._on_rx_period_ended()`が`Ft4Codec.decode_audio()`（libft4wsjtの3パス
subtract+BP/OSDデコード）を**Qtメインスレッド上で同期的に**呼び出していた。実測で混雑した
帯域（28局重複）のデコードに約0.4秒かかることを確認済み（本機より非力なGPD MicroPC2の
i3-N300ではさらに長くなる可能性が高い）。この呼び出しは`Ft4Scheduler`のQTimer（100ms周期）
が発火するスロットの中で実行されるため、デコードが長引くとメインスレッド上のQtイベントループ
全体がブロックされ、**スケジューラー自身のタイマーも一緒に停止する**。ブロック中に実際のUTC
時刻は进み続けるため、ブロックが解けて`_tick()`が再開したときには複数の6秒周期境界をまたいで
しまっていることがあるが、`_tick()`の境界検出ロジックは一度に1回の遷移しか処理しない
（`_prev_slot_num`との比較で最新のスロット番号への飛び越えを検出するのみ）。結果として
`_rx_buffer.clear()`（新周期のRX開始）が実際のUTC境界より大幅に遅れて実行され、以降の周期の
音声バッファが本来の6.048秒窓からズレた・短縮された内容になり、その後の周期のデコードも
連鎖的に劣化する。WSJT-Xは別プロセスとして動作しデコードもメインスレッドをブロックしないため
この問題を持たず、同じ音声から多数デコードできていた。

**修正**: `src/ui/ft4_tab.py`に`_RxDecodeWorker`（`_TxWorker`と同じ、素の`threading.Thread`で
動かすQObjectワーカー）を新設し、`decode_audio()`をバックグラウンドスレッドで実行するよう変更。
完了は`done: Signal(object, object)`（messages, audio）でメインスレッドに戻し、テーブル表示
（`_display_decoded()`）とウォーターフォール描画（Qt GUI操作のためメインスレッド必須）はそこで
行う。`Ft4Tab._decode_busy`フラグで、前の周期のデコードが終わっていない間は新しいデコードを
開始せず（該当周期は諦めてウォーターフォールだけ更新）、libft4wsjtブリッジのFortranモジュール
レベル`save`変数（`ft4wsjt_bridge.f90`の`g_c_callback`/`g_user_data`/`g_decoder`、非再入）へ
同時に2つの呼び出しが入ることも防いでいる。

**教訓**: 「6秒ごとに正確に境界を検出する」設計は、その境界検出自体を担うタイマーと同じ
イベントループ上で重い処理（特にネイティブライブラリのブロッキング呼び出し）を実行すると、
処理時間がそのままタイミング誤差としてシステム全体に伝播する。WSJT-Xのような外部の別プロセス
との比較で「入力音声・デコードエンジンは同じはずなのに自分のアプリだけ極端にデコード数が
少ない」という症状が出たら、デコード呼び出し自体がUIスレッド上でどれだけの時間専有している
か、そしてそれが自分自身の時刻管理（スケジューラー）を巻き込んでいないかを疑うこと。

**FT4専用デコードタイミングログ（2026-07-10 実装）**

上記のスレッド化修正後、実測した0.4秒という所要時間は開発機での単体テスト値であり、実運用
（衛星追尾・World Map/Radar/Dashboard描画・Autotrackポーリング等が同じCPUを奪い合っている
状態）でも同程度かは別問題、という指摘があった。共有アプリケーションログ（`fbsat59.log`）は
Qt/リグ/追尾関連のメッセージで埋もれて追いにくいため、FT4専用の別ログファイルを新設した。

- `src/comms/ft4/decode_log.py`: `get_ft4_decode_logger()` — `fbsat59.log`と同じ
  ディレクトリ（`platformdirs.user_log_dir("FBSAT59", "FBSAT59")`）に`ft4_decode.log`として
  出力。`logger.propagate = False`で共有ログ・stderrには一切流れない
- `_RxDecodeWorker.run()`（`src/ui/ft4_tab.py`）が`decode_audio()`の前後で`time.monotonic()`を
  取り、1周期ごとに`decode audio_len=6.05s duration=0.42s messages=3`の形式で記録
- `_decode_busy`により周期がスキップされた場合も
  `decode SKIPPED (previous decode still running) audio_len=6.05s`として記録され、
  実運用でどれだけの頻度で諦めが発生しているかが分かる
- 実際の`duration`が6秒に対してどの程度の余裕があるか、`SKIPPED`の発生頻度がどの程度かを見て、
  スレッド化だけで十分か、それともサブプロセス化（WSJT-Xの`jt9`ヘルパーと同等の別プロセス方式。
  重複デコードが安全に同時実行できる利点があるが、フリーズドバンドル内での自己再起動・IPC・
  プロセス管理が追加で必要になる）まで必要かを次回のパスで判断する

**このログが暴いた、より根本的な別バグ — 毎周期の半分しかデコードを試みていなかった
（`Ft4Scheduler`のRXスロット限定トリガー、2026-07-10 発見・修正）**

デコードのスレッド化・レベル調整・ノイズフロア修正をすべて終えた後も、最大仰角45°のRS-44パス
（強く聞こえていたにも関わらず）で2局しかデコードできない、という報告があった。上記の専用
ログ（`ft4_decode.log`）を確認したところ、`duration`は0.03〜0.4秒程度で常に余裕があり
`SKIPPED`もほぼ皆無だったが、**ログの行間隔が常に6秒ではなく12秒だった**（例:
`14:38:00`→`14:38:12`→`14:38:24`）。これは「デコードが間に合わず諦めている」のではなく、
**そもそも毎周期の半分がデコード自体を試みていない**ことを意味していた。

**原因**: `Ft4Scheduler`（`src/comms/ft4/scheduler.py`）はFT4の6秒スロットを偶数/奇数で
交互に割り当て、`tx_even`設定に基づき各スロットを「TXスロット」「RXスロット」に分類していた。
旧`rx_period_ended`シグナルは**RXスロットの終わりでしか発火せず**、`Ft4Tab._on_period_changed()`
も**RXスロットの開始時にしか`_rx_buffer.clear()`していなかった**。つまりTXスロットの間に
録音された6秒分の音声は、次のRXスロット開始時のバッファクリアで**一度もデコードされずに
毎回黙って捨てられていた**。

これはFT4プロトコルの理解として誤りだった。`tx_even`が決めるのは「**自局が送信して良い**
スロットパリティ」だけであり、他局がどちらのパリティで送受信しているかとは無関係（QSOを
最初に呼びかけた側が偶数・応答した側が奇数、等でパリティが決まるため、CQを出す局・応答する局は
両方のパリティに散らばる）。送信していない（モニターのみ、あるいはTX Enable OFF）局は、
**両方のパリティを毎回デコードする**必要がある。WSJT-Xは送信していない間は常に6秒ごとに
デコードしており、この「半分だけ聞く」制限は持たない。

**修正**:
- `Ft4Scheduler.rx_period_ended` → `period_ended`にリネームし、TX/RXスロットの区別なく
  **毎回のスロット境界で無条件に発火**するよう変更
- `Ft4Tab._on_period_changed()`も`_rx_buffer.clear()` + `_start_audio_capture()`を
  TX/RXスロットの区別なく**毎回実行**するよう変更（自局の送信トリガー判定はそのまま
  `is_tx and self._tx_enabled`の条件で残す）
- `Ft4Tab._on_rx_period_ended()` → `_on_period_ended()`にリネーム（挙動の変化に合わせて）

この修正により、モニター時（TX Enable OFF、既定状態）のデコード試行回数は理論上**2倍**になる。

**教訓**: TX/RXタイミングスケジューラーで「自局の送受信スロット」の概念を、そのまま
「復号対象にすべき音声かどうか」の判定に流用すると、送信していない側のスロットの活動を
まるごと取りこぼす。プロトコル上の「送信して良いタイミング」と「復号すべきタイミング」は
別の概念として扱うこと——特にFT4/FT8のような、QSO開始側と応答側でスロットパリティが
入れ替わるプロトコルでは、モニター動作は常に全スロットを対象にする必要がある。

**スケジューラー発火遅延ログ（2026-07-10 追加）**

毎スロットデコードに修正した後も、強信号のパスで期待したほど局数が伸びない場合に備え、
「デコード自体は速いが、そもそも周期の境界検出自体がメインスレッドの混雑で遅れているのでは
ないか（衛星追尾・ドップラー計算・Dashboard/World Map/Radar描画等との競合）」という仮説を
検証するため、`Ft4Scheduler._tick()`（`src/comms/ft4/scheduler.py`）にも`ft4_decode.log`への
記録を追加した。

- スロット境界を検出した瞬間（`self._prev_slot_num != slot_num`）に、同じ`ft4_decode.log`へ
  `scheduler boundary_lag=0.030s slots_missed=0 slot=333334`の形式で記録
  - `boundary_lag`: 検出時点で本来の境界時刻から何秒経過していたか（`now % 6.0`）。
    QTimerの分解能（100ms）を大きく超えていれば、メインスレッドが何かに占有されている証拠
  - `slots_missed`: 直前の`_tick()`呼び出しから今回までの間に、6秒境界を何回分まるごと
    飛び越えたか（0が正常。1以上なら、その間の1周期以上の音声が録音バッファの管理から
    完全に外れていたことを意味する — メインスレッドがまるまる6秒以上ブロックされていた
    場合に発生する、今回のデコードスレッド化前に実際に起きていたのと同種の障害）
- `decode audio_len=...`ログと同じファイル・同じタイムスタンプ精度で交互に記録されるため、
  「この周期はデコードに時間がかかったのか、それとも境界検出自体が遅れていたのか」を
  1つのログファイル内で突き合わせて判断できる

**実測: 12秒周期(旧)でも`audio_len`が5.1〜7.0秒の間で約60秒周期の「うなり」を示した
（`main_window`側1秒ハートビートの計測ログ追加、2026-07-10）**

上記2つの修正後の実ログを解析したところ、`decode audio_len=...`の値が本来の約6.05秒付近に
揃わず、5.1秒台（短い）・6.9秒台（長い）を挟みながら約60秒周期で規則的に変動していることが
判明した（例: 5.26→6.05→6.93→6.04→6.05→5.14→…の5行・60秒サイクルの繰り返し）。録音バッファの
開始・終了が本来の6秒境界からズレている直接証拠であり、信号強度やデコードエンジンの性能とは
無関係にデコード失敗を引き起こしうる。

有力な容疑者として`MainWindow._on_tick()`（`src/ui/main_window.py`、1秒周期のQTimer、
FT4スケジューラーと同じメインスレッドで動作）を特定した。衛星位置・ドップラー計算
（`_update_selected_satellite()`）に加え、5回に1回（5秒ごと）`_update_world_map()`、さらに
`_check_notifications()`・`_check_autotrack()`・`_update_rig_web_state()`・
`_detail_panel.refresh_freq_mirror()`をまとめて実行している。QTimerは「前回のコールバックが
終わってから次のintervalを数える」方式のため、1回あたりわずかな超過時間が毎回蓄積し、
6秒（FT4周期）・60秒（10周期）スケールで観測された「うなり」と時間スケール的に整合する
仮説として、`_on_tick()`自体にも同じ`ft4_decode.log`への計測ログを追加した:

- `main_window tick duration=0.085s map_updated=True`の形式で**毎回**（1秒ごと）記録
  （閾値を設けず全件記録: 単発の突出ではなく複数回にわたる小さな超過の蓄積が原因という
  仮説を検証するため、平均値も見られるようにする必要があった）
- `map_updated`で、5秒に1回の重い地図更新を含む回とそれ以外を区別できる
- `src/ui/main_window.py`は本来FT4専用ではないが、この調査に限定した一時的な計測のため、
  あえて`comms.ft4.decode_log`を直接importしている（**調査終了後は削除予定**）
- `scheduler boundary_lag=...`ログと突き合わせることで、「FT4の境界検出が遅れた瞬間に
  `_on_tick()`が実際に長く／頻繁に動いていたか」を直接相関確認できる

**RX音声取り込み・周期区切りをQtメインスレッドから完全に独立させる
（`Ft4RxCaptureWorker`、2026-07-10 実装）**

上記の一連の調査（デコードのスレッド化・毎周期デコード化・`boundary_lag`ログ）を経てもなお、
`MainWindow._on_tick()`（1秒周期・衛星追尾/ドップラー計算/5秒ごとの地図更新等）がFT4の
`Ft4Scheduler`と同じQtメインスレッド（単一のイベントループ）を共有している限り、
「`_on_tick()`が実行中の間はFT4側のQTimerも処理を待たされる」という構造的な問題は残る
という指摘があった。調査の結果、根本的な解決には「RXバッファの区切り判定・クリア・読み取り」
という一連の処理そのものをQtのイベントループから完全に切り離す必要があると判断した。

**設計**: `src/comms/ft4/rx_capture.py`の`Ft4RxCaptureWorker`が、RX音声バッファの所有権を
`Ft4Tab`から引き取り、`threading.Thread`上で`time.sleep()`ベースの精密な待機ループを
独自に回す（QTimerの100msポーリングではなく、次のUTC 6秒境界まで直接スリープするため
理論上ミリ秒未満の精度が出る）。

- `push_audio(chunk)`: 音声コールバックスレッド（サウンドカード or SDR）から呼ぶ。
  `threading.Lock`で保護された内部リストにチャンクを追加するだけ
- `start()`/`stop()`: 専用スレッドを起動/停止。起動直後に次のUTC 6秒境界まで一度スリープ
  してから本格的な蓄積を始めるため、最初の周期が中途半端なオフセットから始まらない
- 6秒ごとに目覚めると、その場でバッファをスナップショット・クリアし、`on_period`コールバックを
  **このスレッド自身から**呼び出す。Qtのウィジェットには一切触れない設計
- 呼び出し元が処理落ちして`time.sleep()`が0以下になった場合（プロセスのサスペンド等）は、
  過去分をまとめて発火させず現在時刻から再同期する（`slots_missed`の考え方をここでも踏襲）
- 同じ`ft4_decode.log`に`capture boundary_lag=0.000s`の形式で毎周期記録し、
  `Ft4Scheduler`側の`boundary_lag`と直接比較できるようにした

**`Ft4Tab`側の変更**:
- `_rx_buffer`（`Ft4Tab`が直接所有していたリスト）を廃止。`_audio_callback`/
  `_on_sdr_audio_chunk`は`self._rx_capture.push_audio(chunk)`を呼ぶだけになった
- `_on_capture_period(audio)`（新設） — `Ft4RxCaptureWorker`のスレッドから呼ばれる
  （**Qtメインスレッドではない**）。デコード可否・`_decode_busy`の判定はスレッドセーフな
  プレーンなPython操作のみなのでそのまま実行できるが、ウォーターフォール更新
  （QPainter/QPixmap操作）は必ずメインスレッドで行う必要があるため、新設した
  `period_skipped: Signal(object)`経由でメインスレッドの`_on_period_skipped()`に
  委譲する（Qtのシグナル/スロットは送信元のスレッドに関わらず、受信側QObjectのスレッドへ
  自動的にキュー配送されるため、これだけで安全）
- `_on_period_changed(is_tx)`（`Ft4Scheduler`の`period_changed`シグナル、引き続きQTimer駆動）
  は**自局の送信判定のみ**に役割を縮小。RXバッファのクリア・音声取り込み開始は完全に削除
- `Ft4Scheduler.rx_period_ended`改め`period_ended`シグナルも、接続先がなくなったため削除
  （`Ft4Scheduler`自体は引き続きTX判定・カウントダウン表示用にQTimerで動作し続ける。
  `boundary_lag`ログ自体は比較用診断として残した）
- `Ft4RxCaptureWorker`の起動/停止は`Ft4Scheduler`と1対1で連動させた
  （`_start_scheduler()`内で`self._rx_capture.start()`も呼ぶ。`closeEvent`/
  `_on_rig_disconnected`双方で`stop()`も対で呼ぶ）

**残る主要スレッド構成**: 音声コールバックスレッド（PortAudio/SDR、既存）→
`Ft4RxCaptureWorker`スレッド（新設、周期区切り判定）→ `_RxDecodeWorker`スレッド
（既存、実際のデコード）→ Qtメインスレッド（テーブル・ウォーターフォール等の表示更新のみ）。
Qtメインスレッドが関与するのは最後の表示更新だけであり、音声取り込み・周期区切り・
デコードという時刻精度が重要な経路には一切関与しない。

テスト: `tests/test_ft4_rx_capture.py`（実際に`threading.Thread`を動かし、`_PERIOD_S`を
短縮した上で複数周期の発火・空周期でコールバックが呼ばれないこと・`stop()`後に発火が
止まること・`start()`の冪等性・複数スレッドからの同時`push_audio()`が安全であることを検証。
Qtイベントループ・実オーディオデバイス不要）。

**世界地図更新（`_update_world_map()`）の負荷軽減 — 二重計算の解消＋非表示中の停止
（2026-07-10 実装）**

FT4のRXタイミング独立化後の実測ログで、`main_window tick duration`が地図更新を含む回
（5回に1回）だけ毎回1.0〜1.4秒かかっていることが判明した（前述「実測: main_windowティック
計測ログ」セクション参照）。原因は衛星フィルターが「All Satellites」相当（`is_hidden=0`で
1671機）になっているとき、`_update_world_map()`が毎回この全機について軌道計算をしていたこと。
特にFT4タブのQuick Comm Controlで衛星を選ぶと、`_on_comms_satellite_requested()`が自動的に
フィルターを「All Satellites」に切り替える仕様（前述「Comms Quick Panel 設計」参照）のため、
通信タブ使用中もこの重い計算が動き続けていた。

**根本原因1: 衛星ごとに位置計算を2回行っていた**

`_update_world_map()`は衛星1機ごとに`subpoints()`（内部で`subpoint()`→緯度経度用）と
`self._engine.observe()`（方位角・仰角用、オートトラックキャッシュ目的）を別々に呼んでおり、
それぞれが独立に`sat.at(t)`（SGP4伝播）を実行していた。同一時刻の同一衛星の位置を2回計算する
無駄が生じていた。

**修正1**: `core/engine.py`に`SatelliteEngine.observe_multi_with_subpoints()`を新設。
衛星ごとに`sat.at(t)`を1回だけ呼び、その`geocentric`から`wgs84.subpoint_of()`（緯度経度）と
`geocentric - ground_station.at(t)`（地心位置の差分によるトポセントリック位置、`.altaz()`で
方位角・仰角を導出）の両方を導出する。地上局自身の位置（`ground_station.at(t)`）もループの
外で1回だけ計算するよう変更（衛星に依存しないため）。数値は既存の`subpoint()`+`observe()`の
組み合わせと完全一致することを固定時刻での比較で確認済み（`tests/test_engine.py`の
`test_observe_multi_with_subpoints_matches_separate_calls`）。実測で1671機のループが
約0.84秒→約0.21秒（約4倍高速化）。

**根本原因2: 地図が実際に表示されていなくても毎回計算していた**

世界地図の内容を実際に描画しているのは「Dashboard」タブ（ズーム地図埋め込み）と
「World Map」タブの2つだけだが、`_update_world_map()`は`_on_tick()`から5秒ごとに
無条件で呼ばれており、Radar・Pass Chart・Radio Control・SDR Control・FT4含む通信タブが
アクティブな間も同じ重い計算が走り続けていた。

**修正2**: `_update_world_map()`を「軽い部分」（観測地点座標の`_pass_list`等への伝播、
毎回実行）と「重い部分」（衛星ごとの位置計算＋地図描画、`_is_map_tab_active()`が
`True`のときだけ実行）に分離。`_is_map_tab_active()`は`self._tab_widget.currentWidget()`
が`self._dashboard_view`または`self._world_map`かどうかを判定するだけの単純なヘルパー。
`_update_world_map()`は重い部分を実行したかどうかを`bool`で返すようにし、`_on_tick()`の
`map_updated`診断ログにもこの実際の実行有無を反映する。

`_on_tab_changed()`にも、Dashboard/World Mapへ切り替えた瞬間に`_update_world_map()`を
即座に1回呼ぶ処理を追加。次の5秒tickを待たずに最新状態を表示するため（切り替え直後に
古いスナップショットが一瞬表示されるのを防ぐ）。

**効果**: 地図が実際に見られているときの計算コストは約4分の1（二重計算解消）、
見られていないとき（FT4タブ使用中など）はほぼゼロになる。ドップラー補正
（`_update_selected_satellite()`）は同じ`_on_tick()`内で地図更新の直後に呼ばれているため、
これによりドップラー更新の間隔がより安定することも期待できる（引き続き検証中）。

テスト: `tests/test_engine.py`に`observe_multi_with_subpoints()`のテストを2件追加
（未知NORADが結果に含まれないこと・既存の`subpoint()`+`observe()`と完全一致すること）。

**「All Satellites」フィルター時は地図更新間隔を60秒に延長（2026-07-10 追加）**

`_is_map_tab_active()`によるスキップは「地図タブが見えているか」だけを見ており、
Dashboard/World Map表示中に「All Satellites」フィルター（1671機程度）を選んでいる場合は
引き続き5秒ごとに重い計算が走っていた。1000機超の点は地図上でどれがどれか判別できず、
5秒間隔の意味がそもそも薄いという指摘を受け、`MainWindow._on_tick()`内で
`self._filter_combo.currentText() == "All Satellites"`のときだけ更新間隔を
`_MAP_UPDATE_INTERVAL_ALL_SATELLITES`（60）ティック＝60秒に切り替えるようにした
（通常フィルター時は`_MAP_UPDATE_INTERVAL`＝5のまま）。`_map_tick_counter`は共通のまま
比較先のしきい値だけ動的に切り替えているため、フィルターを途中で変更してもカウンター自体の
リセットは不要（フィルターを狭める方向に変えた直後は、たまたま古いカウンター値が新しい
小さいしきい値を超えていれば次のtickで即座に更新される）。

**ドップラー補正・リグ送信をQtメインスレッドから完全に独立させる
（`DopplerWorker`、2026-07-10 実装）**

地図関連の負荷軽減後も、「ドップラー補正の更新間隔自体をメインスレッドの混雑から独立させたい」
という要望があった。コードを精査した結果、実は**リグへの実際のCAT送信（`set_vfo_frequencies()`）
は既にバックグラウンドスレッド化済み**だった（`_rig_busy_lock`/`_rig2_busy_lock`で「前回の送信が
終わっていなければスキップ」という、FT4のデコードと同じパターンが既に実装されていた）。残っていた
本当の問題は、**「新しいドップラー補正サイクルを開始するトリガー」自体が`_on_tick()`（1秒ごとの
QTimer）でしか発火せず、`_on_tick()`自体が他の処理（世界地図更新など）で不規則になる影響を
引き続き受ける**、という点だった。

**設計**: `src/core/doppler_worker.py`の`DopplerWorker`が、FT4の`Ft4RxCaptureWorker`と同じ
`threading.Thread`＋`time.sleep()`ベースの精密な待機ループで、`MainWindow._doppler_cycle()`を
一定間隔（デフォルト1秒、後述の「Cycle」設定で変更可）ごとに呼び出す。`Ft4RxCaptureWorker`と
異なり音声バッファのような状態は持たず、単に「一定間隔でコールバックを呼ぶだけ」の汎用的な
トリガーとして実装（`set_interval()`で実行中でも間隔変更可能）。

**`MainWindow._doppler_cycle()`**（新設、`DopplerWorker`のスレッドから呼ばれる。Qtメインスレッド
ではない）:
- 衛星観測（`self._engine.observe()`、`SatelliteEngine`はスレッドセーフ設計）・ドップラー計算・
  Tuneオーバーライドの消費・リグ1/2への送信トリガーを、旧`_update_selected_satellite()`から
  ほぼそのまま移動
- **Moon/EME（MOON_ID）は対象外**: `_update_moon()`が引き続き独自に同等のロジックを
  `_on_tick()`から実行する（変更なし）。Moonパスは衛星パスほど時刻精度がシビアでないため、
  2つの経路を統合するリスクを取らない判断（2026-07-10）
- Qtウィジェットには一切触れない。表示に必要な計算結果（`DopplerDisplayUpdate`データクラス:
  dl_nom/dl_corr/dl_shift/ul_nom/ul_corr/ul_shift/mode/ctcss_display）は`_doppler_computed`
  シグナル経由でメインスレッドの`_on_doppler_computed()`に渡す
- リグへの実送信（`_rig_send()`/`_rig2_send()`の`threading.Thread`起動）はそのまま維持。
  `DopplerWorker`自身のループはCAT応答待ちで一切ブロックされない

**Tuneオーバーライドの二重消費防止**: `_tune_dl_override`/`_tune_ul_override`は「一度使ったら
Noneに戻す」一回限りの消費ロジックを持つ。`_doppler_cycle()`が衛星パスの**唯一の**消費者になり
（`_update_moon()`は独立した別の読み取り箇所を持つ）、MOON_IDを除外しているため両者が同時に
動くことはなく、二重消費のレースは発生しない。

**`_update_selected_satellite()`側の変更**: ドップラー計算・リグ送信ブロックを丸ごと削除し、
代わりに`self._latest_doppler`（ワーカーが最後に計算した結果、`_on_doppler_computed()`で
更新）を読むだけにした。Dashboard更新は、トランスポンダー未選択時は位置のみ、選択時は
`self._latest_doppler`があればその周波数情報を使う（まだ1周期も経っていない場合は位置のみに
フォールバック）。`_on_transmitter_changed()`で`self._latest_doppler = None`にリセットし、
前のトランスポンダーの周波数が新しい選択に対して一瞬だけ古いまま表示される事故を防止。

**既存「Cycle」設定の転用**: `rig_cycle_ms`（Rig SettingsのCycleスピンボックス、10〜10000ms）は
従来`self._timer`（表示更新用1秒タイマー）の間隔を変更していたが、`_doppler_worker.set_interval()`
を呼ぶよう変更。`self._timer`自体は今後1秒固定（表示更新はもうタイミングクリティカルではないため
ユーザー調整不要と判断）。

**教訓（過去の議論から）**: CAT通信のラウンドトリップ時間（FTX-1Fで1コマンドあたり約150ms）が
物理的な下限になるため、間隔をむやみに縮める（例: 0.1秒）意味は薄い。今回はあくまで「間隔の
規則性を確保する」ことが目的で、間隔の数値自体はデフォルト1秒のまま変更していない。

テスト: `tests/test_doppler_worker.py`（6件、実際に`threading.Thread`を動かし、精密な間隔での
繰り返し発火・`stop()`後の停止・`start()`の冪等性・`set_interval()`の即時反映・コールバック内
例外がループを止めないこと・負の間隔値のクランプを検証。Qtイベントループ・実リグ不要）。

**一時ログ`TEMP_DOPPLER_RATE_LOG`・`main_window tick duration`は削除済み（2026-08-03）**

RS-44のTCA付近ドップラー変化率とFT4デコード失敗の相関を調べる目的で追加していた
`_doppler_cycle()`内の`TEMP_DOPPLER_RATE_LOG`ログ、および`_on_tick()`内の
`main_window tick duration`ログは、どちらも調査自体は完了していたにもかかわらず
コードから削除し忘れたまま残っていた。GitHub Issue #16のFT4デバッグで、この2つが
`ft4_decode.log`ファイルへ衛星選択の有無に関わらず起動直後から書き込み続けるため、
「FT4デコーダーがアプリ起動時から常駐している」という誤った印象を報告者に与える一因に
なっていたことが判明し、`src/ui/main_window.py`から該当行（および使われなくなった
`get_ft4_decode_logger`インポート）を削除した。

**教訓**: 一時診断ログは「削除すること」という自己指示をコード内・CLAUDE.md内に書き残す
だけでは不十分で、実際に消し忘れたまま長期間残り続けることがある。特に共有ログファイル
（`ft4_decode.log`）に複数の無関係な診断が同居していると、片方の存在が別の調査対象の
挙動を誤解させる副作用を生みうる。

**起動時NTPチェック — OS時計を直接補正せず、内部オフセットでFT4/Q65タイミングだけを
補償する設計（`src/core/clock_offset.py`、2026-07-23 実装）**

`MainWindow._check_ntp_sync_background()`（起動時にバックグラウンドスレッドで1回実行、
`core/ntp_check.py`が自前のSNTPラウンドトリップでOS時計とのズレを測定）は元々ズレを
検出すると警告ダイアログを出すだけだった。Windowsでこのダイアログが頻繁に出るがOS側の
時刻同期はコントロールパネルを開く必要があり面倒、という指摘を受け、**OS時計そのものを
自動修正する方式ではなく、測定したオフセットをアプリ内部でFT4/Q65のタイミング計算にだけ
適用する方式**を採用した。

**OS時計自体を直接補正しなかった理由**: Windowsで実際の時刻を変更するには管理者権限
（`SeSystemtimePrivilege`）が必要で、起動のたびにUAC昇格ダイアログが出るか、昇格なしで
実行されている場合は黙って失敗する。一方、この警告が存在する本来の目的
（`_NTP_DRIFT_WARN_THRESHOLD_S`のコメント参照）はFT4/Q65の6〜60秒周期境界検出の精度
確保であり、OS時計そのものを直す必要はない——**アプリ内部で使う「今の時刻」だけを
補正すれば同じ目的を達成でき、管理者権限もUAC昇格も不要**という判断（ユーザー確認済み）。

**設計**: `core/clock_offset.py`が`set_clock_offset(offset_s)`/`get_clock_offset()`/
`corrected_time()`（`time.time()+offset`）/`corrected_utcnow()`（`datetime.now(UTC)`相当）
を提供するプロセス全体のグローバル状態（`threading.Lock`保護）。起動時のNTPチェックで
`result.reachable`なら**ズレの大小に関わらず常に**`set_clock_offset(offset)`を呼ぶ。
NTPサーバーに一切到達できなかった場合（オフセット自体が測定不能）のみ、従来通りの
モーダル警告ダイアログ（`_ntp_check_failed`）を表示する。ズレを測定・補正できた場合は
モーダルを出さず、`_ntp_offset_applied`（新設シグナル）経由でステータスバーに8秒間だけ
表示する非モーダルな通知に変更した（補正済みなのでFT4/Q65のデコードには実害がなく、
ユーザーの操作を止める必要がないため）。

**タイミングクリティカルな箇所を`corrected_time()`/`corrected_utcnow()`に置き換え済み**:
- `comms/ft4/scheduler.py`（`Ft4Scheduler.current_slot_info()`・`_tick()`）
- `comms/ft4/rx_capture.py`（`Ft4RxCaptureWorker._run()`—境界計算に使う`time.time()`は
  スリープ時間の算出にも使われるため、一部だけ補正すると基準がずれて逆にタイミングが
  狂う。同一関数内の`time.time()`は例外なく全て`corrected_time()`に統一した）
- `comms/q65/scheduler.py`（`Q65Scheduler`の`utc_now()`/`period_phase()`/`period_index()`/
  `rx_start_time()`）
- `ui/q65_tab.py`（`_check_period_boundary()`の`now`。`self._scheduler.period_phase()`が
  既に補正済み時刻を使うため、比較対象の`now`だけ生の`time.time()`のままだと基準がずれる）

**意図的に対象外にしたもの**: `comms/ft4/qso.py`・`comms/q65/qso.py`の`datetime.now(UTC)`
（QSOログのタイムスタンプ記録用）・`ui/ft4_tab.py`の表示用UTC時計ラベルは、周期境界検出の
精度には関係しないため今回は変更していない（将来ログの正確性を上げたい場合は追加検討）。

テスト: 既存の`tests/test_ft4_rx_capture.py`（実スレッドを動かすテスト）で回帰がないことを
確認済み。`core/clock_offset.py`自体に専用テストは無い（`time.time()+定数`という自明な
関数のため、既存テストの実行結果が変わらないことをもって十分と判断）。

### GitHub Issue #16 続報 — IC-9700実運用で発覚した一連の不具合（2026-08〜、v0.2.48〜v0.3.4）

IC-9700 Sub VFO DATAモード対応（v0.2.47、前述「IC-9700 Sub VFO の DATA モード対応」
セクション参照）で解決が確認された後も、報告者（IC-9700・Directモード、RS-44/JO-97で
FT4運用）から実運用で4件の問題報告が続いた: ①Connect Rig直後にSub側のDATAモードが
元に戻る、②FT4の送信タイミングが周期の途中から始まっているように見える（動画添付）、
③デコードメッセージが破損する（スクリーンショット添付）、④Lockボタンの使い方が
分かりにくい。④は既存のツールチップ更新で対応済み（本ファイル「Ctrl+L ホットキー」
近辺のLock関連セクション参照）。①〜③は以下の通り、それぞれ独立した原因を持っていた。

さらに③の修正後、報告者が「自局信号がウォーターフォール上を送信のたびに上へ移動する／
送信中に傾く」という2つの新たな現象を（別現象として正確に切り分けて）報告し、これが
**反転トランスポンダーのアップリンク・ドップラー補正の符号誤り**という、FT4に限らず
反転リニアトランスポンダー全機種に影響する長年の潜在バグの発見につながった（v0.2.58）。

ドップラーが安定した後は、今度はFT4のQSO手順そのもの（メッセージボタンが無反応・
QSOが自動進行しない）が表面化し、QSOシーケンサーの再構築に至った（v0.3.0）。
いずれも本セクション末尾の該当項を参照。

#### ① Connect Rig直後にSub(UL)のDATAモードが元に戻るバグ（v0.2.50・コミット`5aade19`）

**原因**: v0.2.47のSub側DATAモード修正（`_send_sub_mode_civ_pyserial()`によるpyserial
直接書き込み）は、トランスポンダー選択時の初回適用（Stage 1）にのみ組み込まれていた。
Connect後に発生する再送信（Stage 2、`_resend_mode_ctcss_via_rig()`。IC-9100/9700の
SATモードメモリが前の衛星のバンド割り当てを引きずる問題への対策として存在する既存の
仕組み。詳細は「衛星切り替え時の VFO 割り当て逆転バグ修正」セクション参照）は、
修正前の古い`self._rig.set_mode(ul_hamlib, 0, vfo_sub)`（Hamlib経由、`force_vfo_swap`に
よりSubのDATAフラグが確実に反映されない）を相変わらず呼んでいた。Stage 1で正しく
設定した直後に、Stage 2が古い経路で上書きしてしまう構図だった。

**修正**: `_resend_mode_ctcss_via_rig()`のSubモード設定を、Stage 1と同じ
`_send_sub_mode_civ_pyserial()`経由に統一。この関数は`self._rig`を一旦close→pyserial
書き込み→新しいHamlibセッションで再open、という手順を踏むため、Stage 2内でも
`self._rig`を新しいセッションオブジェクトへ正しく差し替える必要がある（IC-9700の
satmodeキャッシュ再確立措置も含め、Stage 1の再open処理と同型に統一）。あわせて、
CTCSS無効時にも無条件で`set_ctcss_tone()`を呼んでいた漏れ（`if enable:`ガード欠如）
も修正。

**教訓**: 同じ問題（Subの不確実な書き込み）への対策が呼び出し箇所ごとに別々に
実装されていると、片方だけ直して満足し、もう片方（今回のStage 2）が古い経路のまま
放置されるリスクがある。「この機種のこの操作は生CI-V/pyserial経由でなければ
ならない」という制約は単一のヘルパー関数に閉じ込め、呼び出し箇所全てがそのヘルパーを
経由するよう横展開されているか確認すること。

#### libft4wsjt Uninstallが失敗する — FreeLibraryが返らずハングする不具合（v0.2.53〜54）

**症状**: Help > FT4 Enhanced Decoder Installation… の Uninstall ボタンを押すと応答が
なくなる（診断ログ`[ft4wsjt install diag]`で`free_libft4wsjt()`呼び出し後、対応する
「returned」ログが一切出ない）。

**原因**: 過去にWindows向けの同種の不具合として、ft8libの同梱版DLLに対する
`FreeLibrary()`がPyInstallerの内部ブックキーピングと衝突しデッドロックする問題を
修正済みだった（本ファイル「Windows — ft8lib インストールが『100%でハング』」
セクション参照。対策は「同梱版（`_MEIPASS`配下）に対しては`FreeLibrary`自体を
スキップする」というものだった）。libft4wsjtにも同型の`free_libft4wsjt()`があったが、
**ユーザーインストール版（`_MEIPASS`配下ではない、Uninstallの対象そのもの）に対する
`FreeLibrary()`呼び出しでも同様にハングする**ことが今回判明した。libft4wsjtは
ft8libより複雑なランタイム依存（FFTW3・Boost・Fortranランタイム）を持ち込んで
おり、DLLアンロード時のローダーロック競合がより起きやすいと考えられる。

**修正**: `free_libft4wsjt()`から`FreeLibrary`の呼び出し自体を完全に廃止し、単に
Pythonの参照（`_lib = None`）を手放すだけにした。かわりにUninstall自体は「削除
（`shutil.rmtree`）を試み、失敗したら削除の代わりにタイムスタンプ付き別名へ
リネームする」方式（`rename_away_for_reinstall()`）に変更。ハンドルが実際には
解放されずファイルがロックされたままでも、リネームであれば（同一ボリューム内
なら）成功することが多く、Uninstallボタンの操作自体は完了できる。リネームされた
古いディレクトリ（`ft4wsjt.uninstalled-{timestamp}`）は次回起動時、モジュール
ロード前の`_cleanup_stale_backups()`で自動的に削除を再試行する（その時点では
ハンドルを持つプロセス自体が既に終了しているため、削除が成功する可能性が高い）。
再インストール時の上書きも同じ`rename_away_for_reinstall()`を経由するよう統一。

**教訓**: 「ハンドルを明示的に解放してから削除する」という一見正しい設計が、
特定のネイティブライブラリ（依存関係が複雑なもの）では解放処理自体がブロック
してしまうことがある。前例（ft8lib）で学んだ「同梱版はFreeLibraryをスキップする」
という対策だけでは不十分で、ユーザーインストール版であっても同種のリスクがあると
判明した場合は、そもそも「解放してから削除」という設計自体を疑い、「削除できなければ
リネームで妥協し、後始末は後回しにする」という、より緩い代替パスを用意する方が
実用的。

#### 【最重要】FT4の周期を6.0秒と誤認していた — 実際は7.5秒（発見・修正、v0.2.54）

**発端**: 報告者が動画で「FT4の送信が周期の途中から始まっているように見える」ことを
示し、「FT4はFT8（15秒周期）の半分の7.5秒だと理解しているが、6秒という前提はおかしい
のでは。送信6秒＋ガードタイム1.5秒＝7.5秒ではないか」と、具体的な数値仮説まで示して
指摘した。

**当初の（誤った）反論**: 私は当初、`libft4wsjt`ブリッジの定数
`FT4WSJT_NMAX = 21*3456`（12000Hzで6.048秒）を根拠に「6.0秒で合っている」と2度に
わたり主張した。しかしこれは**デコーダーの解析バッファ長**（DTマージンを含む解析窓の
サイズ）であって、**周期の長さそのものではない**という区別を見落としていた。

**実際の検証**: 報告者の再度の指摘を受け、WSJT-X本家のソース
（`wsjtx/wsjtx`リポジトリの`Release_Notes.txt`・`lib/ft4/ft4_params.f90`）を直接
取得して確認した結果、以下が判明した:
- 実際の送信メッセージ長は`NSPS=576`サンプル/シンボル × `NN2=105`シンボル ÷ 12000Hz
  ＝ **5.04秒**（これは送信時間であって周期そのものではない）
- FT4の周期は2019年のWSJT-X 2.1.0-rc3で一時的に6.0秒だったが、**その直後の
  2.1.0-rc5/正式版で7.5秒へと後方互換性なく変更**され、以来ずっと7.5秒が現行仕様
  （`Release_Notes.txt`に明記）
- つまり本プロジェクトはFT4実装当初から、2019年に既に廃止された旧仕様の周期値を
  使い続けていたことになる

**影響範囲と修正**: `src/comms/ft4/codec.py`の`FT4_PERIOD`定数を単一の真実源として
`6.0`→`7.5`に変更し、これをハードコードで複製していた以下の全箇所を`FT4_PERIOD`の
import参照に置き換えた:
- `src/comms/ft4/scheduler.py`（`Ft4Scheduler.current_slot_info()`・`_tick()`内の
  4箇所の`6.0`リテラル）
- `src/comms/ft4/rx_capture.py`（`_PERIOD_S = 6.0`）
- `src/ui/ft4_tab.py`（カウントダウン表示`"6.0 s / 6"`等）
- `src/ui/ft4_waterfall_dialog.py`（`_TIME_TICK_STEP_S = 6.0`、履歴保持期間の計算）

ft8_lib（簡易フォールバックデコーダー）側は、TXエンコード・RXデコード（ウォーター
フォール計算）双方とも音声長から動的にスケールする実装になっており周期を独自に
ハードコードしていなかったため、**変更不要**と確認した（ユーザーからの確認依頼を
受けて調査し、変更なしという結論に至った）。

**教訓（最重要）**: 自分の実装内の定数（`FT4WSJT_NMAX`）を「裏付け」として使い、
その定数が実際に何を表しているか（解析バッファ長 vs 周期長）を再検証せずに
2度にわたり誤った結論を繰り返し主張してしまった。ユーザーが「インターネット上の
情報は全て7.5秒だ」という外部情報と、具体的な内訳仮説（6秒+1.5秒）を示して食い下がって
くれなければ、この誤りは見過ごされたままリリースされ続けていた可能性が高い。
実装者自身が持つ「自分のコードは正しいはず」という前提は、ユーザーからの具体的な
反証（外部情報・具体的な数値仮説）に対しては保留し、一次情報（今回は本家プロジェクト
自身のソース・リリースノート）に立ち返って検証すること。この周期の誤りは、報告者が
動画で示した「送信が周期の途中から始まる」という症状（FBSAT59内部では6.0秒グリッドで
自己無矛盾に動いていたが、実際の電波・他局・WSJT-X本体は7.5秒グリッドで動いていたため、
両者の間で本当の位相ズレが生じていた）を直接説明する。

#### デコードメッセージの破損 — AP復号（a priori decode）による自局CQの誤復号（v0.2.54）

**症状**: デコードメッセージ一覧に「CQ EI4GNB R-13」のような、CQメッセージのグリッド欄と
交信メッセージのレポート欄が混ざったような破損したメッセージが表示される、という報告
（スクリーンショット添付）。

**原因**: libft4wsjt（WSJT-X本家デコードエンジン）のコールバックは`nap`（a priori
decode種別、0=通常のブラインド復号、非0=既知のコンテキストをヒントに使った復号）を
返すが、この値をこれまで一切見ずに破棄していた。AP復号は本来、既に確立したQSOの
相手コールサインなど「既知の情報」をヒントとして弱い信号を救済する仕組みだが、
自局の送信がリニアトランスポンダーを経由して自局の受信機にそのまま返ってくる
（衛星経由の自局送信の自然な折り返し受信。意図的なループバックテストではなく通常の
運用で起こる現象）際、AP復号が自局コールサインをヒントとして使い、本来デコードできない
はずの弱い信号を「CQ」メッセージの体裁に無理やり当てはめて復号してしまい、レポート欄に
別のビット列が混入した破損メッセージが生成されていた。

**修正の経緯（2段階）**:
1. **第1版（コミット`6d5204a`）**: `nap != 0` かつメッセージが`"CQ "`で始まる場合は
   一律で破棄。動作は改善したが、ユーザーから「自局のCQのみを対象にしたのか、他局の
   CQまで巻き込んでいないか」と確認があった
2. **第2版（コミット`4772d72`、最終版）**: 確認の結果、第1版は**誰のコールサインで
   あっても**AP復号かつCQ体裁のメッセージを一律破棄する実装になっており、他局が
   既にQSO中で（その他局のコールサインがAPのヒント候補に入っている状態で）改めて
   CQを送った場合の正当な復号まで巻き込んで捨ててしまう可能性があった。
   `Ft4WsjtDecoder.my_call`（自局コールサイン、既にAP復号のヒントとしてライブラリへ
   渡している値）を使い、**メッセージ本文に自局コールサインが実際に含まれる場合のみ**
   破棄するよう修正。QSO中の交換メッセージ（RR73・R+RST等）へのAP復号は元々対象外
   のまま維持。

フェイクのライブラリコールバックを使い、①自局CQ・AP復号・破損（除外対象）
②自局CQ・非AP復号（保持）③QSO交換メッセージ・AP復号（保持）④他局CQ・AP復号
（保持、第2版で追加確認した新規ケース）の4パターンで動作検証済み。

**教訓**: 最初の修正が「症状は解消するが、意図した範囲より広く効いてしまっている」
可能性を、ユーザーからの一言の確認（「自分が送信したCQのみデコードしないように
したのであって、他人が送信したCQまでデコードしないようにしたわけではないですよね？」）
がなければ見過ごすところだった。フィルター条件を実装する際は、「症状を再現する
最小条件」だけでなく「除外対象を意図した範囲に正確に絞り込めているか」を常に
問い直すこと。

#### 自局送信中の周期はデコードしない（v0.2.55）

上記のAP復号フィルターを入れてもなお破損メッセージが出るという報告があり、報告者から
**私の仮説（自局CQのトランスポンダー折り返し受信）に対する強い反証**が示された:
①PTTを打たない受動監視用のWSJT-Xインスタンス（同じMain VFO音声を常時デコード、
フルデュプレックスのリグ）がFBSAT59の送信中に何一つデコードしていない、
②破損が発生した2回のテストはsplit方向が逆（2m TX/70cm RX と 70cm TX/2m RX）で、
ハーモニクス起因なら両方で再現するのは考えにくい。

コードを確認したところ、`_audio_callback()`（サウンドカード）・`_on_sdr_audio_chunk()`
（SDR）とも`self._tx_in_progress`を一切参照せず**自局が実際に送信している最中の音声も
無条件に取り込み・デコードしていた**（`Ft4RxCaptureWorker`も`_on_capture_period()`も
TX状態を見ない設計だった。これは「毎周期の半分しかデコードしない」バグの修正時に
意図してそうした挙動で、それ自体は正しい）。

**修正方針（ユーザー判断）**: 混入経路（RF経由の折り返しか、サウンドカード/インター
フェース内部の回り込みか）の特定を諦め、**そもそも自局送信中の音声はデコード対象に
しない**ことにした。`_transmit_now()`が送信開始時にフラグを立て、`_on_capture_period()`
が1周期後にそれを消費して既存の`period_skipped`経路（ウォーターフォールのみ更新）へ
直行する。代わりに`_display_own_tx()`の行を、既存の「自局宛メッセージ」用の黄色とは
別の色（`_OWN_TX_ROW_COLOR`＝シアン系`#4fc3f7`）にして、「自分が送った内容」と
「デコード結果」を一目で区別できるようにした。

**教訓**: 原因の特定にこだわるより「その入力を信用しない」と割り切る方が、複数の
混入経路がありうるケースでは確実に効く。報告者が示した反証（受動監視インスタンスが
何もデコードしていない・split方向が逆でも再現する）は、こちらの仮説を否定するのに
十分な精度を持っていた——「自分の仮説で症状を説明できる」ことと「その仮説が正しい」
ことは別で、反証可能な観測を提示されたら仮説側を捨てること。

#### 【最重要】アップリンクのドップラー補正の符号が反転トランスポンダーで逆だった（v0.2.58）

**症状**: v0.2.55で破損メッセージが解消した後、報告者から「自局信号がウォーターフォール
上を**送信のたびに上へ移動していく**」「送信中の信号が**傾いている**」という2点の報告が
あった（動画・スクリーンショット・ログ添付）。報告者自身が両者を別現象として区別し、
「両方のVFOに正しくドップラーが送られていれば同じ位置に留まるはずでは？」と的確に
指摘していた。

**調査方法**: `set_freq(MAIN/SUB, ...)`はINFOレベルでログに残るため、報告者が添付した
`050825c-fbsat59.log`から実際に書き込まれた周波数を直接解析した。RS-44の公称値
（DL 435.612 / UL 145.993 MHz）と照合すると:

```
DL offset = -4834.0 Hz
UL offset = -1620.0 Hz
ratio |UL|/|DL| = 0.33513   (u_nom/d_nom = 0.33514)
```

大きさは正しく比例しているが、**両方とも同符号**だった。衛星が遠ざかっている
（DLが公称より低い）とき、ULは公称より**高く**しなければならない（衛星の受信機に
公称周波数で届かせるため）。

**根本原因**: `DopplerCalculator.correct_uplink()`（`src/core/engine.py`）に
`invert` 引数があり、`invert=True`（反転トランスポンダー）のときアップリンク補正を
ダウンリンクと**同じ方向**にしていた。docstringにも「反転トランスポンダーは通過帯域が
鏡像なので、DLとULの補正は同じ方向」と書かれていたが、**これは物理的に誤り**。

アップリンクのドップラー補正は「衛星の**受信機**に公称周波数で届かせる」ための先行補正
であり、トランスポンダーが反転するかどうかとは無関係。反転が影響するのは①下り周波数の
対応関係と②サイドバンド（USB↔LSB）だけで、②は`_MODE_INVERT`（`main_window.py`）で
完全に別処理されている。

**定量的検証**: 往復伝搬（自局TX→アップリンクドップラー→反転トランスポンダー→
ダウンリンクドップラー→自局RX）をシミュレートすると:

| range_rate | 修正前（同符号） | 修正後（常に逆符号） |
|---|---|---|
| −6.0 km/s | −7344 Hz | −1500 Hz |
| 0.0 km/s | −1500 Hz | −1500 Hz |
| +6.0 km/s | **+4344 Hz** | −1500 Hz |

正しい符号なら自局信号は range_rate によらず常に同じ位置（＝音声トーンのオフセット）に
留まる。誤差は `2 × uplink_freq × (range_rate/c)`。報告者のログから逆算した実測値では
**5分間で約1.9 kHz上方向（送信1回15秒間隔あたり約95 Hz）**で、報告と完全に一致した。
影響量はRS-44/MO-122（V/U）で±5.8 kHz、JO-97（U/V）で**±17.4 kHz**。

**なぜ今まで発覚しなかったか**: 反転リニアトランスポンダー全機種に影響していたが、
SSB/CW運用では操作者が常に耳で再同調するため「トランスポンダーはよく流れるもの」として
見過ごされてきたと考えられる。FT4のように機械が復調するモードで初めて明確に表面化した。

**修正**: `invert` 引数を**引数自体ごと削除**（デフォルト値をFalseにするのではなく）。
`correct_transponder()` からも同様に削除し、呼び出し元（`main_window.py` 2箇所）を更新。
誤った符号反転が将来再導入されないようにするため。なお `main_window.py` の `invert`
変数自体は、Lockのダイヤルフィードバックのオフセット符号（反転機ではDL手動同調に対し
ULを逆方向に動かす）で引き続き必要なので残っている。

**テスト**: 誤った挙動を「仕様」として固定化していた2件（`test_correct_uplink_invert`・
`test_correct_transponder_invert_uplink_sign`）を削除し、物理的な正しさを検証する
テストに置換:
- `test_inverting_transponder_own_signal_stays_put` — 反転トランスポンダー経由の往復
  伝搬をシミュレートし、range_rate を −6〜+6 km/s で振っても自局信号の位置が1 Hz以内
  （FT4トーン間隔20.83 Hzの20分の1未満）に留まることを確認
- `test_correct_uplink_arrives_at_satellite_on_nominal` — 補正後のアップリンクが任意の
  range_rate で衛星の受信機に公称周波数ちょうどで届くことを確認

#### 送信中のドップラー凍結をトーン系のみ解除（v0.2.58）

**症状（上記と同時に報告された「傾き」）**: 送信中は`set_vfo_frequencies()`が
`_ptt_active`で早期リターンし、DL/UL両VFOが凍結する（`controller.py`）。報告者のログでは
実際に**7秒間の書き込み停止**が15秒ごと（FT4の送信スロット）に確認できた。

この凍結は元々**APRS用（送信約0.8秒）**の設計で、FT4（約5秒）・Q65（最大60秒）には
長すぎる。報告者の指摘通りIC-9700は送信中の周波数変更を受け付け、他のソフトは送信中も
更新している。傾き量は `(d−u)/d × DLの変化率 × 送信時間` で、報告者のパス（TCA後・
低レート）では9〜86 Hzだったが、高仰角パスのTCA付近では数百Hzに達する。

**修正（4段構え）**:

1. **`set_ptt(enabled, *, freeze_doppler: bool = True)`** — 基底`RigController`に
   キーワード引数を追加し、`_doppler_frozen`（`_ptt_active`の部分集合）で凍結を判定する
   よう変更。デフォルトTrueなので**呼び出し元が明示しない限り従来の挙動のまま**
2. **パケット系は凍結維持・トーン系は解除** — `set_ptt()`の呼び出し元は4つ
   （APRS・AX100 Digi・FT4・Q65）。FMパケットは送信中に搬送波が飛ぶと受信側の検波器に
   段差が出てビット化けの原因になり得るうえ、1秒未満なのでドリフトも無視できるため、
   **APRS・AX100 Digiは凍結を維持**（デフォルトのまま変更なし）。FT4・Q65のみ
   `freeze_doppler=False` を渡す
3. **送信中はUL閾値を1 Hzに** — satmodeのクロスバンド分岐にはUL書き込みの間引き
   （非FMで20 Hz / 15秒。IC-9100は`targetable_vfo = 0`のためSub書き込みのたびに実際の
   VFO切り替えが発生し表示がちらつくための対策）がある。`_tracking_through_tx()`が真の
   間だけ閾値を1 Hz（DLと同じ）に切り替える。FT4のトーン間隔20.83 Hzに対し20 Hz刻みだと
   送信の途中で約1トーン分の段差になるため。IC-9100での余分なVFO切り替えは実際の送信中の
   数秒間のみに限定される
4. **キーイング直前のフラッシュ** — 上記の間引きにより、**任意の瞬間でSub VFOは正しい値
   から0〜20 Hzずれた状態にある**。送信開始時点でこのずれを引きずり、最初のドップラー
   サイクル（最大1秒後）で急に20 Hz飛ぶと、まさに排除しようとしている「送信中の不連続」に
   なる。`set_vfo_frequencies()`が渡された値を**書き込みの有無に関わらず**
   `_pending_dl_hz`/`_pending_ul_hz`に記録し、`_flush_pending_frequencies()`が
   `set_ptt(freeze_doppler=False)`のキーイング直前にそれを再送する。この時点で
   `_ptt_active=True`・`_doppler_frozen=False`が既に立っているため、3で導入した1 Hz閾値が
   適用され確実に書き込まれる。DL/UL両方を再送するのは`_set_vfo_frequencies_locked()`が
   同バンド判定に両方を必要とするため（DL側は変化がなければ内部で自然にスキップされる）

**残る誤差（原理的な下限）**: ドップラーサイクル自体が1秒周期なので「1サイクル分の遅れ」は
残るが、これは**DL側とまったく同じ条件**。報告者のパスの実測値ではDL変化率18.4 Hz/sに対し
UL 6.2 Hz/s なので残差は約6 Hz（FT4トーン間隔の1/3以下）で、毎サイクル滑らかに更新される
ため段差にならない。つまり「アップリンクをダウンリンクと同じ精度まで引き上げる」ことが
達成できた。

**NETモードは対象外**: `HamlibNetController`のUL間引きは`is_satmode_rig and _is_same_band`
のときだけで、**クロスバンド（RS-44/FT4のケース）は元々1 Hz閾値**（`send_ul = last_ul is
None or abs(vfob_hz - last_ul) >= 1.0`）。したがって3・4の対応は不要で、2の凍結解除のみが
適用される。

**教訓**:
- 実装内の定数（今回は`FT4WSJT_NMAX`、前述のFT4周期の件）や自前のdocstring（今回は
  `correct_uplink()`の「反転なら同方向」という説明）を「裏付け」として使う前に、それが
  実際に何を意味しているかを一次原理に立ち返って検証すること。今回のdocstringは断定的に
  書かれていたが単に間違っていた
- ユーザー（報告者）が現象を**複数の別現象として正確に切り分けて**報告してくれた場合、
  その切り分け自体が強力な診断情報になる。今回は「送信のたびに上へ移動する」と「送信中に
  傾く」が実際に独立した2つのバグに対応しており、片方だけ直して満足するのを防いだ
- ログにINFOレベルで実際の書き込み値が残っていたことが決定的だった。公称値との差分を
  取るだけで符号の誤りが即座に判明した——リグへ実際に送った値をログに残す設計は、
  この種の「計算は合っているように見えるのに実機の挙動がおかしい」不具合の調査で
  極めて有効

**実機確認済み（2026-08-06、報告者のRS-44パス）**: v0.2.58で「傾きなし・上方向への移動なし」
「VFO両方が送信中・受信中とも常時追従」を確認。他局のデコード・呼び出しも正常に取れており、
報告者からも "we are now stable and doppler is being applied correctly" との評価を得た。
なお自局信号を目視で追うには仰角30度以上が必要（低仰角では経路損失で折り返しが弱く、
ウォーターフォール上で見えない）。

#### FT4 QSOシーケンサーの再構築（v0.3.0、2026-08-06）

**症状**: 上記のドップラー修正で実運用が安定した後、報告者から「QSOが進んでもメッセージが
自動で切り替わらない」「RST等のメッセージボタンを押しても何も起きない（CQボタンだけが動く）」
という報告があった。手入力で対応せざるを得ず、パスの時間切れで2つのQSOが未完了に終わった。

**原因1: ボタンが黙って何もしない**。`_on_btn_rst()` 等は `_get_qso_manager()` ではなく
`self._qso` を直接参照し、`their = qso.session.their_call if qso else ""` → `if their:` と
いう構造だった。`self._qso` が `None`（CQを押していない・行をダブルクリックしていない）か
相手コールサインが未確定だと、**フィードバックを一切出さずに終了**する。CQボタンだけが
`_get_qso_manager()`（QSOオブジェクトを生成する）を通っていたため、「CQだけが動く」という
症状になっていた。加えてこれらのボタンは `_tx_edit` に直接書き込むだけで**QSOの状態を
更新しない**ため、次のデコード時に状態機械が「ボタンが押されていない前提」で判断していた。

**原因2: プロトコル手順の誤り**。`advance()` の `CALLING` 分岐が、相手のグリッド応答に対して
`R{report}`（R付きレポート）を返していた。標準FT4手順では、この段階では**R無しのレポート**を
送り、相手のR付きレポートを受けてから `RR73` へ進む。1手順先行していた。

**原因3: 相手から呼ばれた場合に自動進行しない**。`_display_decoded()` の自動進行は
`qso.state not in (IDLE, LOGGED)` が条件のため、CQを出していない（IDLE）状態で呼ばれても
状態機械が一切反応しなかった。

**原因4: QSO完了後も送信し続ける**。`advance()` は `LOGGED` 到達時に `_pending_tx = ""` に
するが、`_transmit_now()` は `_tx_edit` を優先して読むため、**最後のメッセージ（RR73等）が
送信欄に残り続け、Haltを手動で押すまで毎TXスロットで送信され続ける**状態だった。

**設計（ユーザーとの複数回のやり取りで確定）**:

状態名を「**直前に送ったもの**」に統一し、各状態が何を待っているかを自明にした。
`GRID_SENT`・`RREPORT_SENT` の2状態を追加:

| 状態 | 直前に送ったもの | 待っているもの | 受信したら送るもの |
|---|---|---|---|
| `CALLING` | CQ | `<自局> <相手> <グリッド or レポート>` | グリッド→R無しレポート／レポート→R付きレポート |
| `GRID_SENT` | 自局グリッド | `<自局> <相手> <レポート>` | R付きレポート |
| `EXCHANGE` | R無しレポート | `<自局> <相手> R<レポート>` | RR73 |
| `RREPORT_SENT` | R付きレポート | `RR73 / RRR / 73` | 73 |
| `CONFIRM` | RR73 | `73` | （完了） |

- `respond_to()` を `respond_with_grid()`（MyGridボタン・ダブルクリック既定）と
  `respond_with_report()`（RSTボタン）に分割。衛星のパスは短いため、グリッドを省略して
  いきなりレポートを送る運用が多いという実情に対応
- **MyGridボタン**を CQ と RST の間に新設（6ボタン1行、幅815pxで収まることをオフスクリーンで
  確認済み）
- **ダブルクリックを一般化**: CQ行に限らずどの行からも相手コールサインを取得する。
  FT4のメッセージは `<宛先> <送信元> <内容>` なので送信元を採り、自局が送信元の場合は宛先側を
  採用する。73を出して終わろうとしている局は短いパスでは次に呼びたい相手そのもの、という
  ユーザーの指摘による
- **自動進行プルダウン**（最下部ボタン行の右端、既定「しない」）: `advance()` に
  `allow_auto_start` を追加し、IDLE状態で自局宛メッセージを受けたらQSOを自動開始する。
  複数局から同時に呼ばれた場合は**先着順**（状態機械が相手を確定した時点で以降の候補に
  マッチしなくなるため自然に実現）。受信のみの運用で勝手に応答を始めないよう既定はOFF
- **レポートは実測S/N**（`format_report()` で `+03`/`-12` 形式）。固定値 `-05` を全廃。
  `Ft4QsoSession.their_snr_db` に保持し、ダブルクリック時に表の `dB` 列から取り込む
- **`LOGGED` 到達時に送信を自動停止**（`_on_qso_complete()`）: TX Enable を解除し送信欄を
  クリアする。これにより「TX Enableを一度押せばQSO終了まで自動送信し、終わったら止まる」
  という、ユーザーが明示的に要望した動作になる
- `set_state()` を新設し、ボタンで手順を飛ばした場合もQSOの状態が追従するようにした

テスト: `tests/test_ft4_qso.py`（17件）— 状態機械の全5経路（CQ→グリッド／CQ→レポート／
MyGrid開始／RST開始／IDLE自動開始）・自動開始の可否・先着順・ボタンによる状態上書き・
レポート書式を検証。Qt非依存。加えてタブレベルの動作8項目をオフスクリーンで確認済み。

**検証状況**: 実機での確認は報告者の次回パス待ち（2026-08-06 時点）。

**教訓**: 「ボタンを押しても何も起きない」という報告は、ボタンのハンドラが例外を投げている
のではなく**ガード条件で静かに早期リターンしている**可能性をまず疑うこと。今回は
`if their:` という一見無害な条件が、フィードバックゼロの死んだボタンを4つ生んでいた。
UIのハンドラで早期リターンする際は、必ず理由をステータス欄等に出すこと。

#### CQ応答のTXスロットが相手局と同じパリティになるバグと修正（v0.3.4、2026-08-08）

**症状**: v0.3.1をRS-44実機（IC-9700）で試した報告者から、「TX Slotの偶数/奇数切り替えが
予測不能」「CQメッセージに応答すると、応答局と同じ周期で送信されてしまうことがある
（FULL AUTO・AUTO設定使用時）」という報告があった。「Auto」設定で実際にどちらのスロットへ
解決されたかを確認する手段も無く、原因の切り分けが難しい状態だった。

**原因**: `_auto_advance_qso()`・`_on_message_double_clicked()`（いずれも`src/ui/ft4_tab.py`）
が、CQをデコードした直後に`Ft4Scheduler.current_slot_info()`で現在のスロットパリティを取得し、
`tx_even=not is_even`で反転させて応答スロットを決めていた。しかし`current_slot_info()`は
`int(now / FT4_PERIOD)`から計算する壁時計基準のスロットで、デコード自体が相手の送信周期が
終わった**次の**周期の冒頭で完了する（`ft4_decode.log`実測でデコード所要時間は0.05〜0.2秒、
7.5秒周期に対して十分短い）。つまりこの時点で取得した「現在のスロット」は既に
「相手の送信周期の次」＝本来応答すべき正しいパリティになっており、そこからさらに`not`で
反転させると相手と**同じ**パリティに戻ってしまっていた。

FULL AUTOの自動応答（`_auto_advance_qso()`）はデコード完了直後に必ずこのタイミングで発火する
ため、常に相手と同じスロットで応答する再現性のあるバグだった。手動でCQ行をダブルクリックする
場合（`_on_message_double_clicked()`）は、クリックする瞬間の周期パリティが「デコード直後」
（正しい応答パリティと一致）か「1周期以上経過後」（相手と同じパリティに戻る）かでどちらの
結果になるかが変わるため、報告にあった「unpredictable」という見え方も一致する。

**修正**: `_display_decoded()`でデコード完了直後（＝相手への応答として正しいパリティ）の
スロットパリティを1回だけ計算し、各行の`QTableWidgetItem`に`Qt.ItemDataRole.UserRole`として
記録するよう変更。`_auto_advance_qso()`はこの値をそのまま使う（反転しない）よう修正し、
`_on_message_double_clicked()`もクリック時点で`current_slot_info()`を再取得するのをやめ、
行に記録された値をそのまま使うよう修正した。これによりダブルクリックの結果がクリックの
タイミングに左右されなくなる。

**あわせて実装した機能追加（同issue内の別要望）**:
- `ft4_decode.log`に、デコードされた各メッセージ（送信局のスロット・SNR・DT・周波数・本文）と、
  こちらが送信した各メッセージ（スロット・周波数・本文）をタイムスタンプ付きで記録
  （`_RxDecodeWorker.run()`・`_transmit_now()`）
- Decoded Messagesテーブルの各行を、送信局が使ったスロットのパリティで文字色分け（青=偶数／
  橙=奇数、`_EVEN_SLOT_TEXT_COLOR`/`_ODD_SLOT_TEXT_COLOR`）。ヘッダーに凡例とツールチップを追加
- カウントダウンの隣に「TX: EVEN」/「TX: ODD」インジケーター（`_update_tx_slot_indicator()`）
  を新設し、TX Slot: Autoが実際にどちらへ解決されたかを`_start_scheduler()`が呼ばれるたびに
  表示するようにした

**検証状況**: 静的なコード解析（デコード所要時間の実測ログとの突き合わせ）に基づく修正であり、
実機での確認は報告者の次回パス待ち（2026-08-08時点）。

**教訓**: 「相手の周期の"反対側"で応答する」という要件を実装する際、"現在時刻から求めた
スロットパリティ"を反転すればよいと考えがちだが、それが成立するのはコードが**相手の送信中に
リアルタイムで**実行されている場合だけ。デコード＋UI表示という処理を挟んだ後（＝既に次の周期に
入った後）に同じ"現在時刻ベース"の値を使うと、暗黙のうちに基準がずれる。「いつの時点のスロット
情報を使うべきか」は処理の実行タイミングとは独立して固定されるべき値であり、必要になった
その場で`current_slot_info()`を呼び直すのではなく、正しいタイミング（今回はデコード完了直後）で
一度だけ確定させてから後続処理に持ち回ることで、呼び出しタイミング依存のバグを構造的に排除できる。

**メニュー: Communications > Q65**（`src/ui/q65_tab.py`）
- **Phase 1（RX）**: libq65 ctypes デコーダー
  - libq65 未インストール時はバナー表示・デコード無効化。インストール先: `~/.local/share/fbsat59/q65lib/`
  - **Help → Q65 Library Installation…** でバンドル版を自動ダウンロード・インストール
  - CI で build-q65lib.yml が毎週 WSJT-X 最新リリースを監視してビルド

**libq65 の実装 — WSJT-X 本家デコードエンジンへの生CATブリッジ（2026-07-07 完了）**

旧`build-q65lib.yml`は`lib/qra/q65/*.f90`を機械的に全ファイルコンパイルするだけの実装で、以下2点が
未解決のまま長期間放置されていた（CIは`genq65.f90`の`use packjt77`未解決エラーで恒常的に失敗）。

1. **依存クロージャ不足**: `genq65.f90`（TXエンコード用サブルーチン、RXには不要）が`lib/77bit/packjt77.f90`
   の`packjt77`モジュールに依存するが、そのファイルをコンパイル対象に含めていなかった
2. **より根本的な問題**: 仮にコンパイルが通っても、`codec.py`が要求する「生音声サンプル→デコード済み
   メッセージ」という高レベル関数（`q65_decode(samples, n, submode, ...)`）はWSJT-X生ソースの
   どこにも存在しない。低レベルC実装（`q65.c`）はLDPCシンボルデコードのみを行い、音声処理
   （FFT・同期検出・復調）は`q65.f90`等のFortranコードに散らばっており、両者を繋ぐブリッジが
   一度も書かれていなかった（＝このビルドは仮に成功していても機能するデコーダーを生成したことがなかった）

`ft4wsjt`（`scripts/wsjtx_bridge/ft4wsjt_bridge.f90` + `scripts/build_ft4wsjt.sh`）と全く同じ
アプローチで、WSJT-X本家のトップレベル決定モジュール`lib/q65_decode.f90`（`module q65_decode`、
`decode()`型束縛手続き＋コールバックインターフェース）を実際にコンパイル・リンクして依存クロージャを
確定させ、`scripts/wsjtx_bridge/q65wsjt_bridge.f90`という新規ブリッジファイルを作成した。

**依存クロージャの確定方法**: WSJT-Xソースを実際にクローンし、`lib/q65_decode.f90`から`gfortran`で
繰り返しコンパイル→リンクを試行し、`undefined reference`が出るたびに該当ファイルを追加する反復法で
確定（手動で全`use`文・`call`文を追うより確実）。最終的な依存ファイル一覧は`scripts/build_q65lib.sh`の
`MODULE_CHAIN`/`LEAF_FILES`/`C_FILES`配列を参照。

**重要な落とし穴（開発中に発覚）**:
- `q65.f90`（Fortranの`module q65`定義）と`q65.c`（C言語のLDPCコーデック）が同じベース名`q65`を持つ。
  フラットな1ディレクトリにステージングして両方を`gcc`/`gfortran`でコンパイルすると、後から
  コンパイルした方が`q65.o`を上書きしてしまい、実際には正しくコンパイルされていた`module q65`の
  シンボル（`__q65_MOD_nfa`等）がリンク時に「undefined reference」として大量に出るという分かりにくい
  症状になった。`build_q65lib.sh`ではC側ソースを別ディレクトリ（`C_SRC_DIR`）にステージングし、
  さらにオブジェクトファイル名にも明示的に`_c.o`サフィックスを付けて衝突を回避している
- ブリッジ関数の動作確認テストプログラムで、書き換えられる仮引数（`lclearave`等）に対して
  Fortranの**リテラル定数**（`.true.`）をそのまま渡すとセグフォルトする。リテラル定数は読み取り専用
  メモリに配置されることがあり、呼び出し先が`lclearave=.false.`のように書き込もうとした瞬間に
  クラッシュする。これはFortran自体の未定義動作であり、呼び出し側は必ずローカル変数を用意して渡す
  必要がある（テストハーネス自身のバグであり、WSJT-X側のコードのバグではなかった）
- WSJT-X本家の`ft4_decode`同様、`q65_decode`も型束縛手続き（`class(q65_decoder)`＋
  `procedure(q65_decode_callback), pointer`）のコールバック方式で、bind(C)の仮引数に直接
  取れないため、`c_f_procpointer`で保存したCファンクションポインタ経由でアダプトする
  （`ft4wsjt_bridge.f90`と全く同じ`bridge_callback`パターン）

**FBSAT59固有の簡略化**（コンテストモード関連ロジックは一切使わないため）:
- `single_decode=.true.`（複数候補探索ループをスキップ、1周期1回のデコードのみ）
- `ncontest=0`・`lapcqonly=.false.`・`nQSOprogress=0`（コンテストモード無効）
- `lnewdat0=.true.`・`max_drift0=0`（毎回新規データとして扱う）
- `q65_set_list2.f90`（コンテスト専用の複数局同時デコード用）は実行時には呼ばれないが、
  Fortranはリンク時に全呼び出し先を解決するため、リンクには含める必要がある

**`lclearave`・`emedelay`は公開パラメータとして残した**: Q65の弱信号EME用途では周期をまたいだ
シンボルスペクトル累積平均（WSJT-X内部の`s1a`配列、モジュールレベルの`save`属性なのでライブラリが
ロードされている間永続する）が実運用上重要な機能のため、`codec.py`の`Q65Codec`からは呼び出しごとに
`lclearave=1`（累積クリア）を渡し、旧実装と同じ「1回のcallは1周期のみを見る」ステートレス挙動を
維持している。周期をまたいだ累積平均を実際に活用する呼び出し側の配線（トランスポンダー変更時に
クリアする等）は未着手（将来の拡張候補）。

**検証方法**: `tests/test_q65_codec.py` — このリポジトリ自身のPython製Q65 TXエンコーダー
（`src/comms/q65/encoder.py`）でメッセージを合成音声にエンコードし、その音声を新しい
`libq65`でデコードして元のメッセージと一致するかを確認するラウンドトリップテスト。
実機・実際のEME交信なしで実装の正しさを検証できる（`libq65`未インストール環境では自動スキップ）。

**2026-07-07 に3プラットフォーム全て `workflow_dispatch`（`force_build=true`）で実際に走らせ
グリーン確認済み**（Linux 41秒 / macOS 38秒 / Windows 1分39秒）。`q65lib-bundle` プレリリース
タグに3プラットフォーム分のアセット（`q65lib-linux-x86_64.tar.gz` / `q65lib-macos-arm64.tar.gz` /
`q65lib-windows-x86_64.zip`）が公開済みで、`Help > Q65 Library Installation…` からのダウンロード
が機能する。旧実装（機能しないスタブ）を置き換えた本ブリッジで実際に3プラットフォームのビルドが
通ったことを確認したのはこれが初めて。

**`Q65Codec`の内部インターフェース変更**: 旧実装は固定長配列バッファ（`msg_buf`・`snr_arr`等）を
事前確保してCから書き込ませるポーリング方式だったが、`ft4wsjt`同様の「デコードごとにコールバックを
1回呼ぶ」方式に変更（`q65wsjt_decode(..., callback, user_data)`）。`Q65Codec.decode()`の
**外部インターフェース（`Q65Codec(submode=..., nfa=..., nfb=..., nfqso=...).decode(samples, period_seconds=...)`）
はそのまま維持**しており、唯一の呼び出し元`src/ui/q65_tab.py`への変更は不要だった。

**ctypesの`dlsym`失敗が`AttributeError`であることを見落としていたバグ — Q65タブが無反応で
開かなくなる不具合（2026-08-02 発見・修正）**

macOS実機で「Communications > Q65」をクリックしても無反応（エラーも出ない）という報告があった。
原因は`_load_libq65()`の`except OSError:`が、ctypesがPOSIXで`dlsym()`失敗時に送出する
`AttributeError`（"dlsym(addr, symbol): symbol not found"）を捕捉していなかったこと。この例外は
モジュールレベルの`_lib: ctypes.CDLL | None = _load_libq65()`呼び出しで発生するため
`comms.q65.codec`のimport自体が丸ごと失敗し、`Q65Tab`・`main_window.py`の`_on_open_q65()`まで
巻き込んで、本来の「libq65が使えない場合はデコード無効バナー付きでタブは開く」フォールバックが
機能せず**タブが一切開かなかった**（`_on_open_q65()`自体にも`from ui.q65_tab import Q65Tab`が
try/exceptの外にあるという別のミスがあり、これも合わせて修正）。

`except`を`except (OSError, AttributeError):`に修正。実際の発生条件は、ユーザーが以前
`Help > Q65 Library Installation…`でインストールした`libq65.dylib`が本節前述のWSJT-Xブリッジ
実装より前の古いバージョンのまま残っており、`_find_libq65()`のユーザーインストール優先順位で
正しいバンドル版より先に読み込まれ、`q65wsjt_decode`シンボルが存在しなかったため。再インストール
（同ダイアログの「ダウンロードしてインストール」）で解決した。

**同一クラスの不具合が`comms/ft4/wsjt_decoder.py`の`_load_libft4wsjt()`にも実在**（未報告だが
`except OSError:`のみで同じ穴）したため横展開して修正済み。

**教訓**: `ctypes.CDLL()`自体の失敗は`OSError`だが、ロード成功後の**シンボル参照
（`lib.some_function`）の失敗はPOSIXでは`AttributeError`**。`ft4/codec.py`の`_find_ft8lib()`は
既にこの2段構えで対処済みだったが、後から似た構造で書かれた`q65/codec.py`・
`ft4/wsjt_decoder.py`には横展開されていなかった。ctypesで共有ライブラリをロードするコードは
`CDLL()`の失敗とシンボル参照の失敗を両方catchできているか確認すること。

**Q65 QSOマネージャーのバグ修正 — FT4のIssue #16修正の横展開で発覚（2026-08-09、コミット
`893534c`）**

GitHub Issue #16でのFT4修正（v0.3.0のQSOシーケンサー再構築、本ファイル前述）と同じ種類の不具合が
`Q65QsoManager`（`src/comms/q65/qso.py`）・`Q65Tab`（`src/ui/q65_tab.py`）にも残っていないか
確認するようユーザーから指示があり、突き合わせ調査の結果、以下が見つかり修正した:

- **RST/R+RST/73ボタンが無反応**: `self._qso.dx_call`が空だと**何のフィードバックもなく**
  黙って終了していた。FT4がv0.3.0で直した「死んだボタン」と同じパターン。
  `_require_dx_call()`を新設し「先にデコード一覧で相手局をダブルクリックしてください」を表示
- **レポートが固定値"-05"/"R-05"**: `call_station()`・`on_decoded()`（グリッド受信時）・
  RST/R+RSTボタンの3箇所すべてでハードコードされていた。`on_decoded()`は`snr`引数を
  受け取っているのに一度も使っていなかった。FT4の`format_report()`と同じ関数をQ65側にも実装し
  実測SNRを使うよう修正
- **`on_decoded()`のCALLING分岐が相手の送ってきた値をそのままエコーバックしていた**:
  相手が（グリッドではなく）レポートを直接返してきた場合、`self.rst_rcvd`には正しく相手の
  レポートを記録していたが、**こちらからの返信メッセージにも同じ変数を使い回していた**ため、
  本来送るべき「自局が測定した相手の信号レポート」ではなく、相手から受け取った数値をそのまま
  送り返していた
- **調査中にテストを書いていて発覚した、より根本的な語順バグ**: `on_decoded()`の
  CALLING・EXCHANGE・CONFIRM全状態の正規表現が`<相手コール> <自局コール> ...`
  （送信元→宛先の順）を前提にしていたが、Q65もFT4/FT8と同じ`ftx_message_encode()`
  （`encoder.py`の`pack77()`で確認済み）でメッセージをパックしており、実際の形式は
  `<自局コール> <相手コール> <payload>`（**宛先→送信元**の順、FT4のコード内コメント
  "directed messages are '<TO> <FROM> <payload>'" と同じ規約）のはず。実際にテストを
  書いて確認したところ、**正しい語順（宛先→送信元）のメッセージではCALLING状態から
  一切先に進まなかった**——つまりCQへの正しく宛先指定された応答を検知する自動遷移が、
  発見されるまで構造的に機能していなかった可能性が高い。手動のCall Station機能
  （ダブルクリック）は独立した経路のため影響を受けていない
- QSO完了（LOGGED）時、TX Enableは既に正しく自動解除されていたが、TXメッセージ欄は
  クリアされていなかった（表示のみの軽微な不整合）ため、あわせてクリアするよう修正
- `state`を書き込み可能な生の属性からFT4の`Ft4QsoManager`と同じ読み取り専用プロパティに変更

**テスト新設**: `tests/test_q65_qso.py`（11件）。`Q65QsoManager`にはこれまでテストが
一切存在しなかった。`app_settings`テーブルが必要な`_log_qso()`（UDPログブロードキャスト
経由）のため、DBフィクスチャは`tests/test_log_broadcast.py`と同じ「`SCHEMA_SQL`で
フルスキーマの:memory:DBを作る」パターンを踏襲した。

**mypyの `Non-overlapping equality check` に関する既知の癖（テスト作成中に遭遇）**:
同一関数内で`assert obj.property_attr == EnumA`の直後に、`obj`の状態を変える別のメソッド
呼び出しを挟んでから`assert obj.property_attr == EnumB`（EnumAとは異なる値）と書くと、
mypy strictが「Non-overlapping equality check」を誤検出することがある（最小再現コードで
確認済み。`@property`経由でも発生し、対象メソッドの返り値型やassertでラップする/しないは
無関係だった）。`tests/test_ft4_qso.py`の同種の連続state比較がなぜこれを踏まないかは
特定できなかったが、確実な回避策は**比較対象を都度、別名のローカル変数に代入してから
assertする**こと（`state_after_x = obj.state; assert state_after_x == EnumB`）。
同種のエラーに遭遇したら、まずこの回避策を試すこと。

**未確認の同種リスク（実害報告なし、今回は見送り）— AP復号フィルターの欠如**:
`Q65Codec.decode()`（`src/comms/q65/codec.py`）は`my_call`をAP復号（a priori decode）の
ヒントとしてlibq65に渡している（コンストラクタの`my_call`/`hiscall`/`hisgrid`引数）が、
デコードコールバックが受け取る`idec`（AP復号タイプ、FT4の`nap`に相当。libft4wsjtの
コールバックと同じ役割）を`del idec, user_data  # unused`で完全に読み捨てている。

FT4では全く同じ仕組み（`my_call`をAP復号ヒントとして渡す）が「自局のCQがトランスポンダー
経由で自分自身に戻ってきて、AP復号によって破損したメッセージとして誤デコードされる」という
実害を引き起こし（本ファイル「デコードメッセージの破損」セクション参照）、`nap`を見て
自局宛CQを除外するフィルターを追加する修正に至った。Q65（本アプリでの主用途はEME）は
FT4ほど「自局送信がそのまま自分の受信機に返ってくる」構造的経路を持たないと考えられるが、
本アプリのQ65実装がGitHub Issue #16で衛星トランスポンダー経由の運用に使われる可能性も
排除できないため、同一クラスの実害が将来報告された場合は、FT4の修正
（`_on_decode_done()`のnapチェック、自局コールサインを含むかどうかでの絞り込み）と
同じパターンをQ65側にも適用することを最初に検討すること。

- **Phase 2（TX/QSO）**: 純 Python エンコーダー（libq65 なしで TX 可能）
  - `encoder.py`: GF(64) 線形符号（生成行列 15×50）・CRC-12・65-FSK 音声合成（numpy）
    - WSJT-X `lib/qra/q65/q65_encoding_modules.f90` のアルゴリズムを Python に移植
    - `pack77()` は ft8_lib（FT4 と共用）を利用してメッセージを 77 ビットにパック
    - `synthesize_audio()`: 85 シンボル × nsps サンプル、連続位相累積 FSK + テーパー窓
  - `qso.py`: QSO ステートマシン（IDLE→CALLING→EXCHANGE→CONFIRM→LOGGED）
    - SQLite `q65_log` テーブルへ永続化・ADIF エクスポート（`PROP_MODE=SAT`, `MODE=Q65`）
  - **サブモード**: A（×1）/ B（×2）/ C（×4）/ D（×8）/ E（×16）トーン間隔
  - **周期**: 15s / 30s / 60s（nsps: 1800 / 3600 / 7200 サンプル @ 12000 Hz）
  - TX クイックボタン: CQ / RST / R+RST / RR73 / 73 + Free text
  - TX Enable（偶数/奇数スロット選択）・Halt TX・Log QSO・Export ADIF

**テレメトリーフォーマット定義**（`src/data/telemetry_formats/`）
| NORAD | 衛星名 | コールサイン | フィールド |
|-------|--------|-------------|-----------|
| 25544 | ISS (ARISS) | RS0ISS | なし（APRS パケット識別用） |
| 40908 | LilacSat-2 | BJ1SK | EPS 7項目 ※未検証 |
| 42017 | Nayif-1 (EO-88) | A6-NAYIF | EPS 5項目 ※未検証 |
| 42829 | Uguisu (BIRDS-1) | JG6YBW | EPS 4項目 ※未検証 |
| 42830 | GhanaSat-1 (BIRDS-1) | GSAT-1 | なし（名前識別用） |
| 43786 | ITASAT-1 | PY2ITA | EPS 4項目 ※未検証 |
| 43803 | JY1Sat (JO-97) | JY1SAT | なし（フォーマット非公開） |
| 44829 | DHABISAT (MYSat-2) | A6-DBSAT | なし（名前識別用） |
| 47311 | Maya-2 (BIRDS-2) | DU3ABE | EPS 4項目 ※未検証 |
| 47783 | GOLF-TEE (AO-109) | WJ9H | EPS 5項目 ※未検証 |

※ Fox-1シリーズ（AO-85/91/92）は DUV 200 baud のため 1200 baud AFSK デモジュレーターでは受信不可。

**PTT CAT 制御**（`src/rig/controller.py`）
- `RigController.set_ptt(enabled: bool)`: 基底クラスで `_ptt_active` フラグを管理
- `HamlibNetController.set_ptt()`: rigctld `T 1` / `T 0` コマンド
- `HamlibDirectController.set_ptt()`: Hamlib binding `rig.set_ptt()`
- **Doppler 凍結**: TX 中（`_ptt_active=True`）は `set_vfo_frequencies()` が早期リターン → 送信中の周波数変更を防止（約0.8秒: lead 150ms + audio 550ms + tail 100ms）

**PTT 送信シーケンス**（APRSメッセージ・位置パケット共通）:
```
PTT ON (CAT) → 150ms 待機 → KISS フレーム送信 → 550ms 待機 → 100ms 待機 → PTT OFF
```
全シーケンスは daemon スレッドで実行（Qt UI スレッドをブロックしない）。

**Help > Direwolf Installation…**（`src/ui/direwolf_dialog.py`）
- 現在使用中の Direwolf パス・バージョン・ソース（User-installed / System PATH / Bundled）を表示
- プラットフォーム別インストール案内（Linux: `apt install` コマンドコピー / Windows: GitHub Releases リンク / macOS: `brew install`）
- 「Download & Install」ボタン: GitHub Releases からバンドル版を取得・ユーザーディレクトリへインストール

**Bell 202 AFSK デモジュレーター**（`src/comms/aprs/afsk_demod.py`）
- SDR パスで AX.25 フレームを 1200 baud AFSK で受信
- アルゴリズム: デシメーション → 瞬時位相差分 → ボックスフィルター → NRZI デコード → HDLC 同期 + CRC-16/CCITT
- scipy 利用可能な場合は FIR フィルター付きデシメーション、不可の場合はストライドで代替
- `frame_received(bytes)` Signal で `KissClient` と互換インターフェース

### カスタムFavoriteグループ設計（src/data/database.py）

```sql
CREATE TABLE custom_groups (
    id          INTEGER PRIMARY KEY,  -- 1-based group number
    name        TEXT NOT NULL,        -- display name (e.g. "Favorite 1")
    sort_order  INTEGER NOT NULL DEFAULT 0
);
-- satellites テーブルに favorite_group INTEGER DEFAULT 0 カラムを追加
-- 0=未所属, 1..N=custom_groups.id
```

- デフォルトで Favorite 1/2/3 を作成（既存 is_favorite=1 は Favorite 1 に移行）
- 右クリック → 「★ Favorite Groups」サブメニューでグループ割当・解除
- Settings > Custom Groups タブでグループ名インライン編集・追加・削除

### コミュニティ周波数（src/data/community_transmitters.json）

| 衛星 | Rx (DL) | Tx (UL) | Mode（DB `mode`列） | invert | 出典 |
|------|---------|---------|------|------|------|
| RS-44 (NORAD 44909) | 435.612 MHz | 145.993 MHz | USB-D（invert=true→UL側はLSB-D） | true | JH1NHK |
| JO-97 (NORAD 43803) | 145.857 MHz | 435.118 MHz | USB-D（invert=true→UL側はLSB-D） | **true**（2026-08-02修正） | JH1NHK |
| MO-122 (NORAD 60209) | 435.812 MHz | 145.938 MHz | USB-D | false | JH1NHK |

`description` は引き続き「FT4 Calling — community standard」のまま（FT4タブの自動オープン判定は
description文字列を見るため無関係）。`mode` 列だけ実際のリグCATモードを表す `USB-D`/`LSB-D` に
変更している。詳細は後述の「モード文字列 → リグCATモード変換テーブル」参照。

**JO-97 の invert 誤り修正（2026-08-02、GitHub Issue #16、v0.2.48）**: 当初 `invert: false`
として登録していたが、報告者からIC-9700実機でSub側の側波帯（USB/LSB）が期待と逆になる
という指摘があり、SATNOGS自身の公式データ（`db.satnogs.org`のJO-97トランスミッタAPI応答、
"U/V SSB Transponder" エントリで `downlink_mode=USB`・`uplink_mode=LSB`）を確認したところ、
反転トランスポンダーであることが確定した。`invert: true` に修正し、descriptionも
「(U/V transponder)」から「(U/V inverting transponder)」に変更。RS-44と同じ反転パターン
（DL=USB-D固定・UL側は`_MODE_INVERT`のUSB-D⇔LSB-D変換で自動的にLSB-Dになる）に統一された。

### SATNOGSトランスミッター status の全件取得（2026-07-11 実装）

#### 背景

`TransmitterManager.sync_from_satnogs()` は従来 `status=active` をAPIクエリに付けており、
SATNOGSが `inactive`/`invalid` と分類したトランスミッターはDBに一切保存されなかった。
実際に調査したところ（Ten-Koh 2 / NORAD 68261の事例）、SATNOGSの `status` は自動集計ではなく
コミュニティのレビュアーが手動でキュレーションする値（`reviewed`/`approved`/`reviewer` フィールド
を伴う）で、実運用と食い違うことがある（レビュー漏れにより、実際には動いていないトランスミッター
が `active` のまま、逆に動いているはずのものが `inactive` のままになっているケースを実例で確認
済み）。一方で**衛星レベルのdead/unknown判定（`satellites.status`・`is_hidden`）は不要な衛星を
一覧から隠すために引き続き重要**であり、この2つは意味が異なるため区別して扱う。

このため、**トランスミッター単位のSATNOGS `status` はDBへの取り込み可否には使わず**、
active/inactive/invalidすべてを取得してDBに保存し、表示側で状態に応じて出し分ける設計に変更した。

#### 実装

- `sync_from_satnogs()`: APIクエリから `status` パラメータを削除（invalidも含め全件取得）
- `transmitters.satnogs_status`列（新設）: SATNOGSの生の `status` 文字列をそのまま保存
  （manual/community由来の行は`NULL`）。既存の `alive`（0/1、`status=='active'`と同義）は
  従来通り維持
- **デフォルトの表示は変更なし**: `get_transmitters()`（デフォルト`include_dead=False`）・
  Edit Transmitterダイアログ・Autotrackリスト検索・Comms Quick Panelは引き続き`alive=1`
  のみを対象にしており、この変更による見た目の影響はない
- **Radio Controlタブのトランスポンダーコンボのみ例外**: `MainWindow._refresh_radio_control()`
  の生SQLから`AND alive = 1`条件を外し、代わりに`ORDER BY`の先頭に
  `CASE WHEN alive=1 THEN 0 WHEN satnogs_status='invalid' THEN 2 ELSE 1 END`を追加して
  active優先ソートを維持（自動選択されるデフォルト項目が非activeにならないようにするため）。
  プルダウンを開いたときのみ、非active項目に背景色を付けて注意喚起する
  （`RadioControlWidget._xpdr_status_bg()` / `_XPDR_INACTIVE_BG`=`#b8860b`ダークゴールデンロッド
  ／`_XPDR_INVALID_BG`=`#8b0000`ダークレッド。閉じた状態のコンボ表示にはQtの仕様上、
  背景色は適用されない）。この2色は衛星リストの既存色（`#f1c40f`=AMSAT partial、
  `#e74c3c`=AMSAT non-operational）や`Qt.GlobalColor.yellow`（FT4タブの自局宛メッセージ
  ハイライト）と意図的に別トーンにしてある
- **スマホWeb UI（Antennaタブ）**: 元々`GET /api/satellites/{norad}/transmitters`自体には
  フィルタが無かったが、JS側（`index.html`）で`xpdrs.filter(x => x.alive)`により
  非activeを隠していた。このフィルタを削除し、色分けはせず全件そのまま表示（ユーザー判断で
  スマホ側は無色のまま）。APIのSQLに`ORDER BY alive DESC, description`を追加し、
  自動選択される最初のカードが極力activeなものになるようにした
- `_mobile_rig_connect()`・Autotrackの衛星切替（main_window.py）は`get_transmitters(...,
  include_dead=True)`に変更。これらは既にUUIDが判明した状態でトランスポンダーを検索するため、
  スマホ側が選択した非activeなuuidも正しく解決できる必要がある

#### Qt Rich Textの落とし穴（色凡例ダイアログ実装時に発覚）

Help > Satellite/Transmitter Colors ダイアログ（`MainWindow._on_satellite_color()`）に
上記2色の凡例行を追加した際、QLabelのリッチテキスト（QTextDocument）は**空の`<span>`に対する
`width`/`height`/`display:inline-block`を無視し、`background`（ショートハンド）も解釈しない**
ことが判明した。既存の衛星リスト用スウォッチも実は同じ理由で描画されていなかったが、
ラベル文字自体に`color`が付いていたため気づかれていなかった（新設したトランスミッター行は
文字を黒のままにしてスウォッチだけに頼っていたため、色が全く出ないというかたちで発覚した）。

**正しい実装**: `background-color`（ショートハンドではなく）を使い、`<span>`に`&nbsp;`等の
実コンテンツを持たせる。

```python
# NG: Qtでは何も描画されない
f'<span style="display:inline-block; width:14px; height:14px; background:{color};"></span>'

# OK: 実コンテンツ + background-color なら塗りつぶし四角として描画される
f'<span style="background-color:{color}; border:1px solid #555;">&nbsp;&nbsp;&nbsp;&nbsp;</span>'
```

`_on_satellite_color()`内の`_swatch()`ヘルパーをこの方式に修正済み（衛星リスト側のスウォッチも
副次的に正しく描画されるようになった）。QLabel/QTextDocumentで色見本・バッジ的なUIを作る際は
毎回この制約に注意すること。

### Dashboard タブ（src/ui/dashboard_view.py）

左2/3にズームマップ、右1/3にレーダー、下部に36pxのステータスバーを配置した統合ビュー。

#### レイアウト構造

```
┌─────────────────────────────┬──────────────┐
│  WorldMapView（ズーム）      │  RadarView   │
│  （2/3 幅）                 │  （1/3 幅）  │
├─────────────────────────────┴──────────────┤
│  ステータスバー（36px固定）                  │
│  衛星名 / EL / AZ / Range / 可視 / DL / UL │
└────────────────────────────────────────────┘
```

#### 主要な設計判断

- `QSplitter` で左右を分割。`setStretchFactor(0,2) / setStretchFactor(1,1)` + `setSizes([660, 330])` で初期2:1比率を強制
- Dashboard 表示時は Satellite Detail パネルを非表示: `currentChanged` ではなく起動時に `setVisible(False)` で初期化（`currentChanged` は初期タブでは発火しないため）
- ズームマップはグリッド線を非表示（`set_show_grid(False)`）— 衛星移動に伴う線のカクカク感を回避
- `isVisible()` チェックで非表示時の再描画をスキップ（CPU負荷削減）
- `track_data: SatTrackData | None` パラメータで Radar タブと同一のパス軌跡を表示
- レーダーの AOS/LOS 時刻表示は `set_use_utc()` で UTC/Local 切り替えに連動

#### WorldMapView への追加 API（src/ui/world_map.py）

| メソッド | 説明 |
|---|---|
| `set_show_grid(show: bool)` | グリッド線・赤道線の表示/非表示を切り替え |
| `set_zoom_region(lat, lon, span_deg)` | 指定座標を中心にズーム表示（デフォルト ±50°） |
| `clear_zoom()` | グローバルビューに戻す |

#### フットプリント描画の設計（`_draw_footprint` — src/ui/world_map.py）

**スキャンライン QPainterPath 方式**（ポリゴン方式から変更済み）:
- N=180 ラチチュードバンドを走査し、各バンドを `QRectF` として `QPainterPath` に追加
- `QPainterPath.setFillRule(Qt.FillRule.WindingFill)` で確実に全領域を塗りつぶし（OddEven 規則のワインディングキャンセル問題を回避）
- 緯度ごとに球面余弦定理で経度半幅 `dlon` を計算
- `cos(rho) = sin(lat0)*sin(lat) + cos(lat0)*cos(lat)*cos(dlon)` を解く
- `is_full_width[i]` フラグ: `dlon ≥ 180°` の行は極域を包む全経度帯 → `xl=0, xr=w` を直接設定
- Antimeridian（日付変更線）越え: `xl > xr` の行は左端・右端の2つの `QRectF` に分割
- fill: `rgba(100,200,255,140)`、outline: シアン `#00DCFF` 1.5px

**アウトラインスキップ規則（重要）**:
- `is_full_width[i] or is_full_width[i+1]` でスキップ（どちらか一方でも全幅行なら除外）
- `xl=0` / `xr=w` という人工座標が通常行の実座標と結ばれて横線になるのを防ぐ
- `and` 条件（両端とも全幅行のみスキップ）は横線を発生させるため使用禁止
- 水平幅 `abs(x2 - x1) < w/3` のセグメントのみ描画（日付変更線越えの大ジャンプを除外）
- スキップにより極境界の弧は閉じない（開いて見える）が、1.5px の細線で目立たなくする妥協策を採用（beta.32）
- 極境界を完全に閉じる根本修正は未解決。遷移点の通常行座標で水平閉じ線を引く方式を試みたが、遷移行の xl≈0/xr≈w により閉じ線自体も横線になる副作用があり断念

**ズームモードの座標整合（重要）**:
- `latlon_to_xy` は地図画像描画と同じクランプ済みlatレンジを使用する
- 地図描画: `lat_max = min(90, clat+span)` でクランプ → 実際のスパンが `2*span` より小さくなる
- オーバーレイ（衛星ドット・フットプリント）も同じ計算を使わないと、極地域で南方向にずれて見える
- `rendered_lat_span = min(90,clat+span) - max(-90,clat-span)` で y を正規化

**衛星ドットクリック**:
- `mousePressEvent` で衛星ドット中心12px以内のクリックを検出し `sat_clicked(int)` を emit
- `main_window.py` で `_world_map.sat_clicked.connect(self._select_satellite_by_norad)` に接続済み

#### デフォルト世界地図（NASA Topographic 1024px）

- `settings_dialog.get_world_map_path()`: 明示的な選択がない場合、`nasa-topo_1024.jpg` が存在すればそのパスを返す
- `main_window._apply_world_map()`: 初回起動時（ファイル未存在）はバックグラウンドスレッドで GPredict リポジトリから自動ダウンロード。完了後 `QMetaObject.invokeMethod` で再適用

### Group Pass Chart（src/ui/pass_chart.py — GroupPassChartView）

- Group タブで検索実行後に自動表示（それまでタブ非表示）
- 衛星ごとに12色パレットから自動割り当て（>12衛星は循環）
- 凡例は非表示。マウスホバーでツールチップ（衛星名＋最大仰角）
- Range選択: 4h / 8h / 12h / 24h（Target Pass Chartと同じ）
- UTC/Local 切り替えに連動

### Comms Quick Panel 設計（src/comms/mode_detection.py, src/ui/main_window.py — 2026-07-04 実装）

#### 概要

Communicationsタブ（FT4・APRS・SSTV/SSDV・CW Decoder・Q65・METEOR・Telemetry）を開いている間、
右側の「Satellite Detail」パネル下部に「Quick Comms Control」枠を表示する。Rig接続や周波数確認、
衛星位置確認のために Radio Control / Dashboard タブへ行き来する手間を減らすための機能。

表示要素（すべて `SatDetailPanel` クラス内、main_window.py）:
- ミニレーダー（`RadarView(compact=True)` — 凡例・ステータス文字列を省略し円を最大化。NSWEラベルは6pt）
- Input Source コンボ（衛星クイック選択。タブごとに対象衛星を動的フィルタ）
- D:/U: 周波数（Radio Control のドップラー補正値、または METEOR は SatDump 固定周波数をミラー）
- Rig 1 / Rig 2 / Rotator 接続プロキシボタン（`RadioControlWidget` の実ボタンを `.click()` するだけで、
  接続ロジック自体は一切重複させない）

Dashboard 等の常駐タブに切り替えると自動的に非表示になる（後述のスプリッター復元とセット）。

#### mode_detection.py — タブ別設定の単一情報源

`src/comms/mode_detection.py` の `COMMS_TAB_CONFIG` に、各タブの Quick Panel 表示設定を集約する:

| タブ | Input Source | 周波数欄 | 理由 |
|---|---|---|---|
| FT4 / APRS / SSTV | ○（動的フィルタ） | Radio Control ミラー | 対象衛星が少数〜中程度で絞り込みが機能する |
| CW Decoder | × | Radio Control ミラー | CW対応衛星が多すぎて絞り込みの意味がない |
| Q65 | × | 非表示 | 固定EMEバンド運用でトランスポンダー選択と無関係 |
| METEOR | × | SatDump固定周波数 | 独自の Pipeline コンボが既にあり、周波数もRadio Controlとは無関係（固定） |
| Telemetry | × | Radio Control ミラー | 独自の専用衛星コンボ（AFSK/gr-satellites）が既にある |

`is_ft4_transmitter()` / `is_aprs_transmitter()` / `is_sstv_transmitter()` / `is_cw_transmitter()` は
`RadioControlWidget._check_comms_auto_open()`（トランスポンダー選択時の自動タブオープン判定）と
**共有**しており、判定基準がQuick Panelの対象衛星リストと自動オープン判定とでズレることを防ぐ。

`is_ft4_transmitter()` は `"FT4"` のみに一致し、`"FT8"` には一致しない（2026-07-04 修正）。
FT4（6秒周期）とFT8（15秒周期）はプロトコルが異なり、アプリのFT4タブは6秒周期専用のため、
"FT8" とだけ書かれたトランスポンダー（例: Ariane 6 上段の GENESIS-A ペイロード）を拾うと、
実際には絶対に復号できないタブが開いてしまう。

`get_norads_for_tab(conn, tab_key)` は `matcher` を使い、`transmitters` テーブルを
`satellites.is_hidden = 0` で JOIN した上で対象衛星の NORAD 一覧を返す。この JOIN がないと、
TLE自動クリーンアップや仮ID移行で `is_hidden=2` になった衛星の残存トランスミッタ行が
Input Source コンボに混入する（2026-07-04、CAS-11 で発覚・修正済み）。

#### 衛星選択フロー（`MainWindow._on_comms_satellite_requested`）

Telemetryタブの `_on_telemetry_satellite_requested()` と同型の汎用ハンドラ:
1. 衛星フィルターを「All Satellites」に切り替え
2. `_select_satellite_by_norad(norad)` で左リストを選択
3. `_refresh_radio_control(norad)` でトランスポンダー一覧を取得
4. `COMMS_TAB_CONFIG[tab_key].matcher` に最初に一致するトランスポンダーを自動選択

#### スプリッター自動縮小・復元（`MainWindow._on_tab_changed`）

`_resident_tab_widgets`（Dashboard/World Map/Radar/Pass Chart/Group Pass Chart/Radio Control/
SDR Control）に含まれないタブ（＝Communicationsタブ全般）がアクティブになったとき:
- 直前のスプリッターサイズを `_pass_panel_saved_sizes` に退避（すでに退避済みなら上書きしない。
  Comms タブ同士を連続で切り替えても元のサイズを覚え続ける）
- 下部 Upcoming Passes パネルを `PassPanel.minimumHeight()`（200px）まで縮小し、
  Decoded Messages 等の表示領域を最大化
- resident タブに戻ったら退避したサイズへ復元し、Quick Comms Panel を非表示化

`MainWindow._comms_tab_keys: dict[QWidget, str]` が各 Communications タブのウィジェットと
`COMMS_TAB_CONFIG` のキー（`"ft4"` 等）を対応付ける。各 `_on_open_*()` でタブ生成直後に登録する。

METEOR タブは `MeteorTab.current_rx_frequency_mhz()` を通じて選択中のパイプライン
（`METEOR_PIPELINES`）の固定周波数を返す。SatDump は Radio Control の選択と無関係に
この固定周波数で受信するため、D:欄はここから取得し、U:欄は常に「RX only」表示にする。

#### `SatDetailPanel.refresh_freq_mirror()` の可視性判定に関する注意

Quick Panel が表示中かどうかの判定には `self._active_comms_tab is not None` を使う。
`QWidget.isVisible()` は、そのウィジェットの属するトップレベルウィンドウが実際に `show()` されて
初めて `True` を返す仕様のため（オフスクリーンテスト等でウィンドウを show していないと常に
`False` になる）、内部状態フラグの方を判定に使うこと。

#### 関連する副次修正（Quick Panel 実装中に発覚・2026-07-04 修正）

- **衛星名プレースホルダーバグ**（`TransmitterManager.sync_from_satnogs()`）: トランスポンダー
  同期が先に衛星レコードを仮登録する際、トランスミッタの `description`（例: "Mode U - CW"）を
  そのまま衛星名として使っていたため、後から正しい名前が判明しても永遠に上書きされない衛星が
  発生していた（`satnogs_source_id` を持つ衛星10件で発覚）。プレースホルダー判定を
  `_is_placeholder_name()` に共通化し、フォールバック名を `#{norad}` 形式に統一。既存の壊れた
  データは `database.py` の `_apply_migrations()` に追加した自己修復クエリ（仮ID側の正しい
  名前をコピー・冪等）で次回起動時に自動修復される
- **Open in SatNOGS のネットワークエラー切り分け**（`MainWindow._fetch_satnogs_uuid_bg`）:
  従来は接続エラー・タイムアウトも「SatNOGS page not found」に丸められ、実際の原因（回線側の
  問題か本当に存在しないのか）が画面から判断できなかった。`httpx.HTTPError` を個別にキャッチし
  `_satnogs_network_error` シグナルで別メッセージを表示するよう分離

### Autotrack 設計（src/core/autotrack.py）

```sql
CREATE TABLE autotrack_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE autotrack_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id       INTEGER NOT NULL REFERENCES autotrack_lists(id) ON DELETE CASCADE,
    norad_cat_id  INTEGER NOT NULL,
    xpdr_uuid     TEXT NOT NULL,   -- 使用するトランスポンダーのUUID
    sort_order    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT ''
);
```

#### 切り替えロジック（AutotrackManager.check()）
1. 現在衛星が Min El 以上 → 継続追尾
2. 現在衛星が Min El 以下:
   a. 別の衛星がすでに可視 → 即座に切り替え（リスト順タイブレーク）
   b. 可視衛星なし → AOS が最も近い衛星に切り替え（リスト順タイブレーク）
3. パス途中は切り替えしない（LOS を待つ）

#### 使用前提条件
1. **Autotrack/Record メニュー** → Autotrack Lists 枠でリスト作成・衛星登録
2. Upcoming Passes > Group タブでパス検索実施
3. Autotrack/Record ダイアログで Autotrack Control 枠のリストを選択 → Enable Autotrack をオン
4. （任意）Autotrack Timer で開始・停止時刻を設定
5. （任意）Recording 枠で Audio / IQ 録音を有効化

### スマホブラウザ Web UI（src/web/static/）

#### タブ構成
| タブ | 内容 |
|---|---|
| **Tracking** | 衛星リスト・EL/AZ・Range・レーダー |
| **Antenna** | AZ/EL 大表示・周波数・トランスポンダー選択・RIG接続 |
| **Pass Prediction** | パス予測一覧 |
| **Group Pass** | グループ検索・パス一覧 |

#### Antenna タブ（手動アンテナ追尾用途に特化）

想定ユースケース: スマホで AZ/EL を見ながら八木アンテナを手動で向け、PCでドップラー補正する運用

| 機能 | 詳細 |
|---|---|
| **AZ/EL 大表示** | 42px の大きな数字でリアルタイム表示 |
| **パス進行バー** | AOS〜LOS の進行状況（緑バー）+ LOS カウントダウン |
| **周波数（読み取り専用）** | Doppler 補正済み DL/UL 周波数とシフト量を表示 |
| **トランスポンダーカードリスト** | 衛星選択時に自動取得・カード形式で表示・タップで選択 |
| **Connect/Disconnect RIG** | スマホからリグ接続をトリガー（設定はPC側で事前設定が必要） |
| **RIG ON/OFF ボタン** | 接続済みリグの Doppler 補正 ON/OFF |
| **ROT ON/OFF ボタン** | ローテーター接続時のみ表示 |

#### コンパス連動（レーダー North-Up / Compass 切り替え）

- レーダー画面右上の「N↑ North Up」ボタンで切り替え
- **Android**: HTTP でも動作（即時切り替え）
- **iOS 16+**: HTTP では `DeviceOrientationEvent` が無効（Apple のセキュリティ制限）。HTTPS が必要
- **iOS 13–15**: 許可ダイアログ後に使用可能

#### RIG 遠隔制御アーキテクチャ（src/web/rig_state.py）

```python
# RigWebState — Qt UI スレッド（書き込み）と FastAPI スレッド（読み込み）の共有状態
rig_connected: bool      # Rig 1 接続状態
rig_engaged: bool        # Doppler 補正動作中
dl_hz / ul_hz: float    # 補正済み周波数
rig_connect_requested    # POST /api/rig/connect でセット → Qt が処理して接続
rig_disconnect_requested # POST /api/rig/disconnect でセット → Qt が処理して切断
```

**WebSocket ペイロード拡張**（`/ws/tracking` レスポンスに追加）:
```json
{
  "rig": { "connected": true, "engaged": true, "dl_hz": 435611234, "mode": "SSB" },
  "rot": { "connected": false }
}
```

**REST エンドポイント**:
- `POST /api/rig/connect` `{norad, xpdr_uuid}` — 衛星・トランスポンダー選択＋接続
- `POST /api/rig/disconnect` — 切断
- `POST /api/rig/toggle` — Doppler ON/OFF トグル
- `POST /api/rot/toggle` — ローテーター ON/OFF トグル

---

## Moon/EME 追尾設計（2026-06-21 確定）

### 概要

月（Moon）を擬似衛星として扱い、EME（地球-月-地球）反射通信のための追尾・ドップラー補正を実現する。

### センチネル値

```python
MOON_ID: int = -1  # src/core/celestial_engine.py
```

NORAD IDの代わりに `-1` を使用。UI全体でMOON_IDを衛星と同列に扱う。

### JPL DE421 エフェメリス

- ファイル: `de421.bsp`（約17 MB、1900〜2053年有効）
- 初回起動時に自動ダウンロード・キャッシュ
- 保存先: `platformdirs.user_data_dir / "ephemeris" / de421.bsp`
- `CelestialEngine.load()` で遅延ロード（バックグラウンドスレッド推奨）

### CelestialEngine（src/core/celestial_engine.py）

| メソッド | 説明 |
|---|---|
| `load() -> bool` | DE421をロード（初回のみダウンロード） |
| `observe_moon(lat, lon, elev_m, at)` | 指定時刻の月の Observation を返す |
| `moon_subpoint(at)` | 月直下点（lat, lon）を返す |
| `moon_track(lat, lon, elev_m, hours, step_minutes)` | 指定時間のAZ/ELトラック（レーダー弧用） |
| `moon_events(lat, lon, elev_m, start, end)` | Moonrise/transit/Moonset を PassInfo として返す |

### "Celestial Bodies" フィルターグループ

- 衛星フィルタードロップダウンに `"Celestial Bodies"` グループを追加
- 選択時: 衛星リストは `[(MOON_ID, "Moon")]` のみ表示。TLEリスト・世界地図の衛星表示はクリア
- 月のサブルーナルポイント（直下点）を世界地図にドットで表示

### レーダー表示

- `RadarView` が MOON_ID の `SatTrackData` を受け取ると24時間の月軌道弧を描画
- フットプリントは表示しない（月にフットプリントは不要）
- Dashboard でも同様（`update_observation()` 内で `MOON_ID` 時は `draw_footprint()` をスキップ）

### Pass Chart（Moonrise/Moonset/Transit）

- `moon_events()` が Skyfield almanac で Moonrise/Moonset を検出
- 1窓（Moonrise→Moonset）を1つの `PassInfo` として表現（aos=Moonrise, tca=Transit, los=Moonset）
- Target/Group タブ・Pass Chart・Group Pass Chart 全て MOON_ID ブランチで対応
- 検索範囲を前後1日拡張して「すでに月出中」のケースも捕捉

### EME ドップラー補正（往復×2）

衛星は地球→衛星の片道ドップラーを適用するが、EMEは地球→月→地球の往復のため係数を2倍にする。

```python
rr = obs.range_rate_km_s * (2.0 if self._selected_norad == MOON_ID else 1.0)
```

適用箇所:
- `_update_rig_web_state()` — WebSocket状態
- `_update_selected_satellite()` — 衛星選択時の定期更新（MOON_IDでは呼ばれない）
- `_update_moon()` — 月選択時の定期更新（1 Hz）

### EME 周波数（src/data/eme_frequencies.json）

`transmitter_manager.get_transmitters(MOON_ID)` が呼ばれると `eme_frequencies.json` から読み込む。

| バンド | CW 周波数 | Digital 周波数 | モード |
|---|---|---|---|
| 50 MHz | 50.190 MHz | 50.200 MHz | CW / SSB(Q65/JT65) |
| 144 MHz | 144.000 MHz | 144.100 MHz | CW / SSB(Q65/JT65B) |
| 432 MHz | 432.000 MHz | 432.065 MHz | CW / SSB(Q65/JT65C) |
| 1296 MHz | 1296.000 MHz | 1296.065 MHz | CW / SSB(Q65/JT65D) |
| 2320 MHz | 2320.000 MHz | 2320.065 MHz | CW / SSB |
| 5760 MHz | 5760.000 MHz | 5760.065 MHz | CW / SSB |
| 10368 MHz | 10368.000 MHz | 10368.065 MHz | CW / SSB |

- `source='eme'`（SATNOGS同期で上書きされない）
- DL == UL（シンプレックス。リグはsame-bandロジックで制御）

### リグ制御（EME時）

- EMEはDL=ULのsame-band運用 → 衛星same-bandロジック（VFOA=RX, VFOB=TX / 通常split）がそのまま適用される
- `_update_moon()` 内で `_rig_busy_lock` + バックグラウンドスレッドで `set_vfo_frequencies(dl, ul)` を呼び出す（衛星の `_update_selected_satellite()` と同一パターン）
- Rig 1 / Rig 2 両対応

---

## 既知の制約（プラットフォーム由来・修正不可）

### GNOME（Wayland）ではポップアップウィンドウの初期表示位置を指定できない

`SdrWaterfallDialog`（SDR Controlタブの🌊 Waterfallボタン）は`showEvent()`から
`QTimer.singleShot(0, self._position_top_right)`で画面右上へ`move()`する実装になっているが
（画面中央だと親ウィンドウの真上に重なるため）、**GNOME Shell / Mutter（Wayland）環境では
効かず、常にGNOMEのデフォルト配置（センタリング）で開く**（2026-08-09、開発機のGNOME
Waylandセッションで確認）。

**原因**: Waylandはセキュリティ・UI一貫性の設計上、クライアント（アプリ）が自分のトップレベル
ウィンドウの画面上の絶対位置を指定することを許可していない。位置決定はコンポジタ
（GNOMEなら Mutter）の専権事項で、アプリからの`move()`要求は無視される。これはQt/GTKの
実装の問題ではなく、Wayland自体の設計・GNOMEのポリシーによるもの。KDE（KWin）・Xfce
（xfwm4）・Windows・macOSでは通常通り動作すると見込まれる（未確認）が、GNOME環境だけは
アプリ側のコードで回避する標準的な方法が存在しない。

**対応**: 修正不可と判断し、コードはそのまま維持（他環境では正しく機能するため）。
`xdotool`/`wmctrl`等の外部ツールをサブプロセスで叩く回避策も検討したが、GNOME拡張機能の
有無やバージョンに依存する不安定な手段のため見送った（ユーザー判断、2026-08-09）。

---

## 既知のバグ（未修正）

### Windows — RTL-SDR Blog V4 でConnectを押してもrtlsdr_open()が呼ばれない（GitHub Issue #10・調査中）

**症状**: Windows 11、RTL-SDR Blog V4。SDR Device Installation・SDR Settingsではデバイスが
正しく検出される（`[RTL-SDR diag]`・`[RTL-SDR enum] rtlsdr_get_device_count() = 1`が
ログに出る）。しかしRadio ControlでRig 1にSDRを割り当てConnectを押しても、SDR Controlは
常にDisconnectedのままでスペクトラムも表示されない。

**ログ調査で判明した事実（2026-07-14）**: 報告者から提出された`fbsat59.log`（複数セッション・
5000行超）を精査した結果、`SdrRigAdapter.connect()`（[controller.py:3364](src/rig/controller.py:3364)）
が実行されると必ず出るはずの`logger.info("SdrRigAdapter.connect: ...")`が**一度も出現しない**
ことを確認した。つまりConnectボタンを押しても`SdrRigAdapter.connect()`自体が呼ばれていない。

`_on_connect_rig1()`（[radio_control_widget.py](src/ui/radio_control_widget.py)）から
`rig.connect()`までのコードを静的に読んだ限りでは論理的な欠陥は見当たらず（`self._rig1`の
セット・ボタン有効化・`_warn_rigctld_if_direct()`のSDR除外判定はいずれも正しい）、
「クリックハンドラ自体が呼ばれていない」のか「`self._rig1`が実は`None`だった」のか
「バックグラウンドスレッド内で例外が発生し、コンソールなしのWindows GUIアプリでは
標準エラー出力が失われて痕跡が残らなかった」のか、既存のログだけでは区別できなかった。

**現状の対処（診断ログ追加のみ・根本修正はまだ）**: `_on_connect_rig1()`に一時的な診断ログ
（`[RigConnect diag]`タグ）を追加済み。クリック受理時の`self._rig1`の型・`is_sdr`・
`is_connected`状態、各早期returnパス、スレッド開始、`rig.connect()`呼び出し前後
（例外が起きた場合は`logger.exception()`で必ず記録されるようtry/exceptで包んだ）を記録する。
報告者から次のログを受け取り次第、どこで処理が止まっているかを特定し、根本修正を行う。

**関連ファイル**: `src/ui/radio_control_widget.py`の`_on_connect_rig1()`（診断ログ、次回対応時に削除予定）

**有力な原因候補が判明（2026-08-05・未確認）— Help画面がZadigを「不要」と表示していた**:
本プロジェクトの開発者自身が実機のWindowsにRTL-SDRを接続して確認したところ、
**Zadig（WinUSBドライバー）未適用でもFBSAT59のデバイス一覧には正常に表示される**ことが
確認された（列挙はUSBディスクリプタを読むだけで成立し、実際にストリーミング用に開く段階で
初めて失敗する）。さらに、この状態でHelp > SDR Device Installationを開くと
「✅ すべてのデバイスにドライバーがインストール済みです。対応は不要です」と表示され、
**Zadigの案内が一切出なかった**。原因は`_build_install_section()`
（`src/ui/sdr_install_dialog.py`）の早期リターンで、WindowsはSoapySDRが同梱済み
（`SOAPY_AVAILABLE=True`）かつRTL-SDRが`driver="rtlsdr"`付きで列挙されるため
`needed_modules`が空になり、Zadig手順が置かれているWindows分岐に**一度も到達していなかった**。

つまりIssue #10の報告者も、この「対応は不要です」表示を見てZadigを適用しないまま
Connectを試していた可能性がある。症状（検出はされるがConnectで何も起きない）とも整合する。
**修正済み**（2026-08-05）: Windowsは「対応不要」の早期リターンから除外し、RTL-SDR/HackRF
検出時はデバイス名を明示した⚠️バナー（「一覧への表示はUSBディスクリプタを読んだだけで、
WinUSB適用まではストリーミング用に開けない」旨を明記）＋ZadigのURL入り手順を常に表示する。
デバイスマネージャーに「?」が2つ出る件も注記に追加した。あわせて
`_add_windows_buttons()`のべき等化も実施（Rescanのたびにボタンが増殖するバグがあったが、
Windows分岐が到達不能だったため今まで露見していなかった）。

報告者が新しいビルドでZadig適用後に解決するかは未確認。解決しない場合は上記の診断ログ
（`[RigConnect diag]`）による調査を継続すること。

**教訓**: 「デバイスが検出されている」ことは「そのデバイスを開いて使える」ことを一切保証
しない。列挙（enumerate）と実際のオープン・ストリーミングは別のコードパスであり、必要な
ドライバーの有無も別問題。さらに悪いことに、検出の成否だけを根拠に「対応不要」と表示する
UIは、ユーザーに必要なセットアップを積極的に省略させてしまう。インストールガイダンスの
「不要」判定は、検出結果ではなく**実際に使える状態かどうか**を根拠にすること（判定できない
場合は「不要」と言い切らない）。同種の不具合はMETEOR/HRPTのSatDump検出でも起きている
（本ファイル「SatDump 検出・起動・Linux librtlsdr不整合の一連の修正」の総括参照）。

---

### Windows — ft8lib インストールが「100%でハング」→ 再起動後も再インストール失敗（GitHub Issue #13・調査中）

**症状**: v0.2.14 で報告された `PermissionError: ft8.dll`（Help > ft8lib Installation の
Download & Install が展開中にロックされたDLLを上書きできず失敗）に対し、`_free_library()`
（DLLハンドル解放）とエラーメッセージ改善（v0.2.15、コミット `2cbbf2a`/`18f70d4`/`3f719e8`）を
実施済みだったが、v0.2.17 でも再発報告があった（2026-07-19）。今回の報告では新たに
「インストールが100%まで進んで**ハング**する」→ アプリ再起動 → 「未インストール」表示に戻る
（AppDataフォルダにはファイルは展開済み）→ 再インストールを試すと同じ
`PermissionError: ft8.dll`（「FT4タブを閉じて再起動してください」という改善済みメッセージ付き）
が出る、という一連の流れが報告された。

**静的解析で見つかり修正済みの確実なバグ（2026-07-19、根本原因かは未確認）**:
- `src/comms/q65/encoder.py` の `pack77()` が呼び出すたびに `_find_ft8lib()` で
  `ctypes.CDLL()` を新規ロードしていたが、**一度も `FreeLibrary` で解放していなかった**。
  Windowsでは`LoadLibrary`のたびに参照カウントが増え、対応する`FreeLibrary`を呼ばない限り
  アンロードされないため、Q65機能を一度でも使うと`ft8.dll`に余分な未解放参照が残り、
  Help画面側の`_refresh_status()`が１回`_free_library()`を呼んでも完全にはアンロードされない
  状態になり得る。`free_ft8lib()`という共有関数に切り出し（`src/comms/ft4/codec.py`）、
  `pack77()`側もtry/finallyで確実に解放するよう修正
- `PermissionError`のメッセージ表示（`ft8lib_dialog.py`の`_InstallWorker.run()`）が
  `str(exc)`（内部で`repr()`によりバックスラッシュが`\\`にエスケープされる）を使っていたため、
  Windowsパスの区切り文字が実際には1本なのに画面上は2本に見える、という別報告（ユーザーからの
  「これは正しいのか？」という質問）にも繋がっていた。`exc.filename`（生の文字列）を使う表示に
  修正済み

**未解決点（「100%でハング」の直接原因）**: 上記2件はいずれも静的解析で見つかった確実な不具合だが、
「進捗100%からダイアログが応答しなくなる」症状そのものを再現・確認できていない。ダウンロード後の
展開処理（`zipfile.extractall`）自体、または展開成功直後に`finished_ok`シグナル経由で呼ばれる
`_refresh_status()`（新しく展開したDLLをctypesでロードしてバージョン表示 → 解放、という一連の処理）
のどこかで止まっている可能性を疑っている（例: Windows Defenderのリアルタイムスキャンが
書き込み直後のDLLを排他的にロックし`ctypes.CDLL()`呼び出しが長時間ブロックされる、等）が、
確証はない。

**現状の対処（診断ログ追加のみ）**: `src/ui/ft8lib_dialog.py`に`[ft8lib install diag]`タグの
診断ログを追加済み（ダウンロード開始/終了・展開開始/終了・`finished_ok`emit前後・
`_on_install_ok`受信・`_refresh_status()`内の`_find_ft8lib()`/`_get_ft8lib_version()`/
`free_ft8lib()`各呼び出し前後）。報告者から次のログ（`fbsat59.log`、ハング発生時を含むセッション）
を受け取り次第、どの行の後で進行が止まっているかを特定し、根本修正を行う。

**関連ファイル**: `src/ui/ft8lib_dialog.py`（診断ログ、次回対応時に削除予定）・
`src/comms/ft4/codec.py`（`free_ft8lib()`共有関数）・`src/comms/q65/encoder.py`（`pack77()`修正）

**フォローアップ（v0.2.25、2026-07-22）— 根本原因を特定・修正**: 上記診断ログをリリースする前に
報告者が独自に再現手順を絞り込んでくれた（ft8libフォルダを削除してからクリーンインストールを
再試行）。結果、`PermissionError`（ロック）は再発せず展開も成功したが、**展開が成功し
`Download & Install`が100%まで進んでもHelp画面・FT4タブの両方が「ft8lib未インストール」の
ままになる**ことが判明した。これは「ロック」問題ではなく別の不具合だった。

**根本原因**: `_find_ft8lib()`（[codec.py](src/comms/ft4/codec.py)）が
`ctypes.CDLL(str(user_dir / "ft8.dll"))`でユーザーインストールディレクトリの`ft8.dll`を
絶対パス指定でロードしていたが、**そのディレクトリをDLL検索パスに登録していなかった**。
Python 3.8以降、Windows上の`ctypes.CDLL()`は「ロードするDLL自身のあるディレクトリ」を
暗黙に依存関係の検索対象へ含めなくなっている（`os.add_dll_directory()`で明示登録しない限り）。
`ft8.dll`はMinGWビルドのため`libgcc_s_seh-1.dll`・`libwinpthread-1.dll`という同じフォルダ内の
依存DLLを必要とするが、そのフォルダが登録されていないため、`ft8.dll`自体はフルパス指定で
開けても依存DLLの解決に失敗し（`OSError`／WinError 126相当）、既存の
`except (OSError, AttributeError): continue`に静かに握りつぶされて`_find_ft8lib()`が`None`を
返し続けていた——「ハングする」という報告は実際には（少なくともこの再現手順では）ハングでは
なく、常に失敗していたことに気づかれなかっただけだった可能性が高い。

これは本ファイルの**Hamlibユーザーインストール**で2026-07-21に確認・修正済みの不具合
（`main.py`の`os.add_dll_directory(_hamlib_user_str)`）と全く同じ原因・同じクラスの不具合。
`src/comms/ft4/codec.py`の`_find_ft8lib()`にはこの登録処理が一度も実装されていなかった。

**修正**: `_find_ft8lib()`にHamlibと同じパターンで
`os.add_dll_directory(str(user_dir))`（`hasattr(os, "add_dll_directory")`でPython 3.8+を
ガード、`user_dir.exists()`でディレクトリ未作成時のエラーを回避）を追加。さらに、
同一クラスの不具合が既に存在することを確認した以下2つのユーザーインストール型ネイティブ
ライブラリにも同じ修正を横展開した（いずれもMinGW/gfortranビルドで同様の依存DLLを同一
ディレクトリに持つため）:
- `src/comms/q65/codec.py`の`_find_libq65()`（`q65.dll`）
- `src/comms/ft4/wsjt_decoder.py`の`_find_libft4wsjt()`（`ft4wsjt.dll`、FFTW3/Boost依存）

**教訓**: このプロジェクトでは「ユーザーインストールディレクトリに配置したネイティブDLLを
ctypesで直接ロードする」という設計パターンが4箇所（Hamlib・ft8lib・libq65・libft4wsjt）に
存在する。Hamlibで一度この不具合を修正した際、同じパターンを使う他の3箇所への横展開を
その場で行わなかったため、翌日Issue #13で全く同じ不具合が別のライブラリ（ft8lib）として
再発した。**この種のDLL検索パス修正は、直接影響を受けたライブラリだけでなく、同じ
「ユーザーディレクトリ配置＋ctypes直接ロード」パターンを使う全箇所に横展開して確認すること。**

**検証状況**: 静的解析・既存precedent（Hamlibでの確認済み修正）との一致に基づく修正であり、
Windows実機での確認は次回のユーザーからの報告待ち。

**フォローアップ（v0.2.31、2026-07-23）— `add_dll_directory`修正は不十分と判明・詳細診断ログとリトライ処理を追加**:
報告者から取得した`fbsat59.log`（`[ft8lib install diag]`タグ付き）を解析した結果、
v0.2.26で入れた上記の`os.add_dll_directory()`修正は問題を解決していないことが判明した。
ログには**2種類の別々の症状**が記録されていた:

1. **展開開始からわずか9msでの`PermissionError`**（フォルダを削除して作り直した直後の
   クリーンインストールでも発生）。タイミングの短さから、書き込み直後の実行ファイルを
   リアルタイムスキャンするアンチウイルスソフトが瞬間的にファイルを排他ロックしている
   可能性が高いと推測しているが未確証
2. **展開が完全に成功した直後でも`_find_ft8lib()`が`None`を返す**（`extraction done`
   ログの直後の`_refresh_status()`呼び出しで再現）。これは(1)とは別の問題で、
   `add_dll_directory`によるDLL検索パス修正が効いていないことを直接示す証拠だった

問題は、`_find_ft8lib()`が`ctypes.CDLL()`の失敗と、その後のシンボル存在チェック
（`ftx_message_encode`/`ft4_encode`）の失敗を区別せずに`continue`していたため、
上記(2)のどちらが真の原因かをログから特定できなかったこと。

**追加した診断機能**:
- `_find_ft8lib()`（`src/comms/ft4/codec.py`）: `[ft8lib load diag]`タグで、
  `user_dir`の存在確認・`add_dll_directory()`成否・候補ごとのファイル存在/サイズ・
  `ctypes.CDLL()`自体の失敗（`OSError`、`winerror`付き）とシンボル欠落
  （`AttributeError`）を明確に区別して記録するよう変更
- 展開処理（`src/ui/ft8lib_dialog.py`の`_InstallWorker.run()`）: `PermissionError`
  発生時に0.3秒間隔で最大3回まで自動リトライする処理を追加（アンチウイルスの瞬間ロック
  という仮説が正しければリトライで解消するはずで、間接的な検証になる。FT4タブが
  DLLを開いたままの本当の永続ロックであれば3回とも失敗し、これまで通りのエラー表示
  になる）。各試行の結果を`[ft8lib install diag]`タグで記録

**次のログで判明するはずのこと**: 展開時の`PermissionError`がリトライで解消するかどうか
（(1)の原因の手がかり）、`_find_ft8lib()`が失敗する場合に`CDLL()`自体が失敗しているのか
（OS/依存DLLの問題）、それともロードは成功するがシンボルが無いのか（配布しているft8.dll
自体の不具合）を切り分けられる。

**根本原因が判明・修正（v0.2.34のユーザーログで確定、2026-07-24）**: 上記の詳細診断ログを
搭載したビルドから実際に得られたログで、ついに真の原因が判明した。**ユーザーインストール版
だけでなく、アプリに最初から同梱されているbundled版の`ft8.dll`（`_MEIPASS/ft8.dll`）でも
全く同じ症状**——`loaded OK but missing expected symbol: AttributeError("function
'ftx_message_encode' not found")`——が記録されていた。ファイルは正しく存在し
（`exists=True size=91364`）、`ctypes.CDLL()`によるロード自体も成功しているのに、
中身の関数がまったくエクスポートされていない、という状態。これはユーザーインストールの
タイミングや経路（ロック・検索パス・アンチウイルス等）とは無関係に、**Windows向けにビルド
された`ft8.dll`というファイルそのものが最初から壊れていた**ことを意味する。これまで疑って
いた「PermissionErrorによるロック」「DLL検索パス」「アンチウイルスの瞬間スキャン」は
（(A)の瞬間的なPermissionErrorという別症状は依然として残るとしても）この「未インストール」
表示の主因ではなかった。

**真の原因**: `.github/workflows/build-ft8lib.yml`のWindowsビルド手順
（`& gcc -shared -o "$FLAT\ft8.dll" @objs`）が、MinGW GCCの既知の落とし穴を踏んでいた。
**MinGW/Windows向けにGCCでDLLをビルドする場合、`__declspec(dllexport)`で明示されたシンボル
以外はデフォルトでエクスポートされない**。ft8_lib（kgoba/ft8_lib）のソースはLinux中心の
コードでそのような注釈を一切持たないため、Linux版（ELF `.so`はデフォルトで全グローバル
シンボルをエクスポートする）は問題なく動作する一方、**Windows版のDLLだけは関数が一つも
外部から呼べない状態でビルドされ続けていた**。つまりIssue #13の報告以前から、Windows向けの
ft8lib（ユーザーインストール版・アプリ同梱版とも）は一度も正しく機能したことがなかった
可能性が高い。

`build-ft8lib.yml`調査の過程で、**同じCIビルドパターン**（MinGW GCC/gfortranで
`-shared`のみ・エクスポート指定なし）を使う`scripts/build_q65lib.sh`（libq65）・
`scripts/build_ft4wsjt.sh`（libft4wsjt）の Windows/汎用ビルド分岐（`case "$(uname -s)"`の
`*)`節）にも**全く同じ不具合を確認**（未報告だが、Windows環境でのQ65・FT4拡張デコーダーの
インストールも同様に機能していなかったと推定される）。3箇所とも
`-Wl,--export-all-symbols`（GNU ldの標準オプション。Windows以外では no-op として文書化
されているため、Linux/macOSビルドへの副作用はない）をリンクコマンドに追加して修正。

**教訓**: 「ファイルは存在する・読み込みも成功する・それでも機能が使えない」という症状は、
実行時のロード経路（検索パス・ロック・権限）だけでなく、**配布物自体のビルドが壊れている
可能性**を疑うべき典型例だった。今回、ユーザーインストール版と同時にアプリ同梱版でも
同じ症状が出ていたことが、実行環境固有の問題ではなく配布物自体の問題であることを示す
決定的な証拠になった——同じ症状が「ユーザー環境だけ」でなく「開発側が作った同梱物」にも
出ていないか確認することは、原因切り分けの強力な手がかりになる。

**対応状況**: コード修正済み。次のステップとして、修正後の3ライブラリを`workflow_dispatch`
（`force_build=true`）で再ビルドし、`ft8lib-bundle`/`q65lib-bundle`/`ft4wsjt-bundle`の
各プレリリースタグのアセットを更新した上で、アプリ本体も新しいタグでリリースし直し、
同梱版のft8.dll等を差し替える必要がある。Windows実機での最終確認は次回のユーザー報告待ち。

**再ビルドで発覚した2つの追加バグ・最終修正（2026-07-25）**: `-Wl,--export-all-symbols`を
3ライブラリに適用してforce_buildした際、さらに2つの不具合が発覚した。

1. **`--export-all-symbols`はPE/COFFターゲット専用のldオプション**で、Linuxの通常のELF用
   `ld`では`unrecognized option`として拒否される。`build_q65lib.sh`/`build_ft4wsjt.sh`の
   `case "$(uname -s)"`で、このオプションをWindows/MinGWとLinux共通の`*)`分岐に入れて
   しまっていたため、Linux/macOSビルドまで巻き込んで一時的に壊してしまった（Linux
   ビルドがCI上で失敗しただけで、壊れた成果物が公開されることはなかった）。
   `MINGW*|MSYS*|CYGWIN*)`という専用分岐を新設し、既存の`*)`分岐（Linux）は一切変更せず
   完全に元のコマンドのまま維持することで解決。加えて`build-ft8lib.yml`（PowerShell）側は
   `-Wl,--export-all-symbols`を無引用符で書くとPowerShellがカンマを配列区切りと誤解釈し
   `Missing argument in parameter list`になる問題があり、`"-Wl,--export-all-symbols"`と
   明示的に1つの文字列としてクォートして解決した。

2. **`ft8.dll`のエクスポート自体は直った（0件→55件）が、`ftx_message_encode`を含む
   `ft8/message.c`由来の関数群だけが依然として欠落**していることを、実際にリリース
   アセットをダウンロードし`objdump`でエクスポートテーブルを直接検証して発見した
   （ログの`_find_ft8lib()`失敗報告だけでは推測の域を出なかったため、憶測で終わらせず
   実バイナリを検証したことで確定できた）。原因は`build-ft8lib.yml`のコンパイルループが
   **個々のファイルのコンパイル失敗を一切検知しない**設計だったこと（`gcc -c`の終了コード
   を見ず、生成された`.o`ファイルの有無だけで判定していたため、コンパイルが失敗しても
   静かに次のファイルへ進み、リンクは残ったオブジェクトだけで「成功」していた）。
   終了コードを明示チェックするよう修正した直後の再ビルドで、実際のコンパイルエラーが
   初めて可視化された：
   ```
   ft8/message.c:953: error: implicit declaration of function 'stpcpy'
   ```
   `stpcpy()`はGNU/POSIX拡張関数で、MinGWのヘッダーでは（Linux/macOSと異なり）宣言が
   見えない。ソースには`-DHAVE_STPCPY`が渡されているが、アップストリームのソース自体には
   `#ifdef HAVE_STPCPY`によるガードが一切存在せず（`grep`で確認済み）、このフラグは
   upstream Makefileの残骸で実質何もしていない。新しいGCCはこの「暗黙の関数宣言」を
   警告ではなくエラーとして扱うため、`message.c`だけがコンパイルに失敗し、上記1の検知漏れ
   と組み合わさって「`ftx_message_encode`が存在しないDLLがビルド成功と報告される」という
   状態になっていた。`-Dstpcpy=__builtin_stpcpy`（GCCが全ターゲットで認識する組み込み
   関数に置き換える、ヘッダー宣言もランタイムシンボルも不要な標準的な回避策）で解決。
   ローカル（Linux）で`message.c`が問題なくコンパイルでき`ftx_message_encode`シンボルが
   生成されることを確認した上で修正を投入。

**最終検証（2026-07-25）**: 修正後に再ビルドしたWindows版`ft8.dll`を実際にダウンロードし、
`objdump`でエクスポートテーブルを確認したところ、`ftx_message_encode`を含む
`ftx_message_*`系関数がすべて正しく含まれていることを確認済み（ファイルサイズも
91364→104146バイトに増加、`message.c`のコードが実際に含まれるようになった証拠と一致）。
v0.2.37でアプリ本体に反映・リリース済み。Windows実機での最終動作確認は次回のユーザー
報告待ち。

**教訓（今回のバグ調査全体を通じて）**:
- CIビルドスクリプトが「ファイルの存在確認」だけでコンパイル成否を判定する設計は、
  個別ファイルの失敗を静かに握りつぶし、何ヶ月も気づかれない不具合を生む。bashスクリプト
  なら`set -euo pipefail`、PowerShellなら`$LASTEXITCODE`の明示チェックを、外部コンパイラ
  ・リンカを呼び出すすべてのCIステップで徹底すること
- 「ログ上の推測」だけで「直った」と判断せず、可能な限り**実際の成果物（バイナリ）を
  ダウンロードして直接検証する**（`objdump`でのエクスポートテーブル確認等）ことが、
  今回のように複数の独立したバグが積み重なっているケースで、どの層まで直ったかを
  正確に切り分ける決め手になった

**フォローアップ（v0.2.38、2026-07-27）— ビルド自体は直ったが、Help画面がフリーズする新規バグを発見・修正**:
v0.2.37で`ftx_message_encode`のエクスポートが実際に修復され、報告者もFT4が正常動作する
ことを確認したが、「Help > ft8lib Installationを選んでも何も起きない（ウィンドウが一切
現れない、タスクバーにも出ない、フォーカスも移らない）」という新しい報告があった。

`[ft8lib install diag]`ログを精査したところ、複数回の試行すべてで
`_refresh_status: calling free_ft8lib()`のログの直後に続くはずの
`_refresh_status: free_ft8lib() returned`が**一度も出現しない**ことが判明した。
`_refresh_status()`は`Ft8LibDialog.__init__()`内、`dlg.exec()`が呼ばれる**前**に実行される
ため、ここでフリーズすると`__init__()`自体が返らず、ダイアログが一度も表示されない
（「フリーズしたようには見えない」という報告と一致する可能性が高い——GUIスレッドが
ブロックされていても、他のバックグラウンドスレッド（TLE取得等）はGILを介して進行し続ける
ため、アプリ全体が「応答なし」に見えるとは限らない）。

**原因**: ログに記録されたオブジェクトの型が`PyInstallerCDLL`だった点が手がかりになった。
これはPyInstallerが自前でDLLロードを横取りし、同梱依存DLLの解決を内部的にキャッシュ・
管理する仕組み（`ctypes.CDLL()`呼び出しへのフック）が返すラッパー型である。この管理下に
あるハンドルに対して素の`FreeLibrary()`をこちらから直接呼ぶと、PyInstaller自身の内部
ブックキーピングと衝突し、Windowsの典型的な「DLLローダーロック」系のデッドロックを
引き起こしていたと推測される（実機での完全な再現検証はできていないが、症状・ログの
一致度から妥当性が高い仮説と判断）。

**修正**: `free_ft8lib()`（`src/comms/ft4/codec.py`）に、解決されたライブラリパスが
PyInstallerの`_MEIPASS`配下（＝同梱版）かどうかを判定するガードを追加。同梱版の場合は
`FreeLibrary`の呼び出し自体をスキップする。**同梱版をアンロックする必要はそもそもない**
——このインストーラー機能が上書き対象とするのはユーザーインストールディレクトリの
コピーのみで、アプリに焼き込まれた同梱版を実行時に書き換えることは想定されていないため、
この安全対策によって機能上の損失はない。

テスト: `Path.is_relative_to()`によるパス判定ロジック自体はローカル（Linux）で単体検証
済み（同梱パス配下→True、ユーザーインストールパス→False）。実際のPyInstallerフリーズ
環境でのフリーズ解消そのものはWindows実機でしか確認できないため、次回のユーザー報告待ち。

**教訓**: PyInstallerがフックする`ctypes.CDLL()`が返すオブジェクトは、通常の`ctypes.CDLL`
とは異なる管理下にある。同梱依存DLLに対して素のWin32 API（`FreeLibrary`等）を直接叩く
コードを書く際は、対象が「アプリが独自にロードした分離ファイル」なのか「PyInstaller自身が
既に管理しているバンドル内依存」なのかを区別し、後者には手を出さないこと。

---

### AppImage — テキストフィールドにキー入力ができない（Linux 非Ubuntu系）

**症状**: Linux AppImage 版で、CI-Vアドレス・ポート名・テキスト入力フィールドにキーボードで文字が入力できない。マウス操作は正常。

**再現環境**: openSUSE Leap 16.0（Python 3.13 仮想環境から `python src/main.py` で起動した場合は正常動作する）

**原因（推定）**: AppImage に同梱された Qt6 / libxkbcommon が、非Ubuntu系ディストリビューションの XKB 設定ファイルを見つけられない。`QT_XKB_CONFIG_ROOT` / `XKB_CONFIG_ROOT` を `/usr/share/X11/xkb` または `/usr/share/xkb` に設定するコードは `src/main.py` に実装済みだが、それでも解消しない環境がある。

**現状の対処**:
- `src/main.py` に `QT_XKB_CONFIG_ROOT` / `XKB_CONFIG_ROOT` の自動設定を実装済み（起動時に `/usr/share/X11/xkb`, `/usr/share/xkb` を順に検索して最初に存在するパスをセット）
- それでも再現する場合は AppImage 内部の Qt プラグイン or libxkbcommon の問題と考えられる

**調査方針（次回対応時）**:
1. 問題が再現する環境で AppImage をターミナルから起動し、ログ出力を入手
2. `qt.qpa.keymapper` や `xkb` 関連の警告・エラーを確認
3. AppImage 内の `libxkbcommon.so` と XKB データのパスを確認
4. 必要であれば AppImage ビルド時に XKB データを同梱するか、`linuxdeploy` プラグインで解決を試みる

**ログ取得コマンド（問題再現環境で実行）**:
```bash
QT_LOGGING_RULES="qt.qpa.*=true" ./FBSAT59.AppImage 2>&1 | head -100
```

---

### アップリンクなし衛星（テレメトリー専用）でドップラー補正がリグに反映されない

**症状**: OTP-2（NORAD 63235、400.5 MHz DL）・CORVUS-BC1/BC2（2 GHz帯 DL）など、アップリンク周波数がない衛星を選択すると、リグの表示周波数が変わらない。Connect後もドップラー補正がリグに反映されず、リグは以前のキャッシュ周波数（例: 6.99920 MHz）を表示し続ける。

**再現条件**: `uplink_low = NULL` の衛星 + FT-991A（NET モード）構成で確認。RS-44 ビーコン（435 MHz）は同じくアップリンクなしだが正常動作する。

**原因の手がかり**:
- `src/ui/main_window.py` `_apply_transponder_state_to_rig()` 内で `ul_hz = float(uplink_low or dl_hz)` と代入しているため、`uplink_low=None` の衛星では `ul_hz = dl_hz` となる
- `set_transponder_freqs(dl_hz, dl_hz)` が呼ばれると `_is_same_band = True` になり、`_send_split_init_independent()` が `S 1 VFOB`（非 satmode リグには不正）を送る
- さらに FT-991A の対応周波数帯（HF/2m/70cm）外の周波数（400 MHz・2 GHz 帯）に対して `F` コマンドを送っても、リグが無視する可能性がある
- RS-44 ビーコン（435 MHz）が正常なのは FT-991A の 70cm 帯（430–450 MHz）に収まるため

**未調査点**: 上記2つの要因（`S 1 VFOB` の誤送信・対応外周波数の無視）のどちらが支配的か、または両方が絡むか未確定。

**関連ファイル**:
- `src/ui/main_window.py` — `_apply_transponder_state_to_rig()` 約2441–2443行目
- `src/rig/controller.py` — `set_transponder_freqs()`・`_send_split_init_independent()`・`_send_freq_preset_independent()`

---

## 次回の作業候補（v0.1.0 以降）

### 継続中・優先度高
0. ~~**RTL-SDR WinUSB Connect 失敗修正**~~ **→ v0.1.71 で解決済み（2026-06-25）**
1. **ドップラー補正の実動作確認** — 各種リグ（TS-2000・FT-817ND 等）での実衛星通信テスト（FTX-1F・FT-991AM・IC-9100・IC-9700・RTL-SDR/HackRF は確認済み）
1b. **AX.25 9600bps G3RUH のフィルタチューニング（実衛星パス待ち、2026-07-10 追加）** — SDR単体経由（`comms/aprs/g3ruh_demod.py`の`G3ruhDiscriminator`）・Rig+サウンドカード経由（Direwolf `MODEM 9600`）双方とも実装済みだが、実際の9600bps G3RUHデジピータ衛星での受信検証を一度も行っていない。特に`G3ruhDiscriminator`のIF帯域幅（`_IF_HALF_BW_HZ`）・偏移定数（`_DEVIATION_HZ`）はNFM音声復調のパラメータを流用した初回実装値であり、実信号でのチューニングが必要になる可能性が高い。詳細は「AX.25 9600bps G3RUH 対応」セクション参照。対象衛星のパスが来た時点で着手する
1c. **Lock（Lボタン）ツールチップに反映遅延の説明を追加（急ぎではない、GitHub Issue #19、2026-08-10 予告）** — IC-9700（satmode・USB接続）ユーザーから、手動リチューンがLock機能に反映されるまで体感5秒程度かかるという報告があり、既知の設計上のラグ（`_doppler_cycle()`は約1秒周期でリグを1回だけポーリング→次周期で補正値へ折り込み→さらに次の`_on_tick()`で画面表示に反映、という3段階構成のため最悪ケースで約3秒・平均1.5〜2秒。詳細は「Lock（Lボタン）— dial feedback設計」セクションの「既知の制約」参照）に一致することを確認済み。Issueへの返信では「近日中にツールチップへ追記する」と予告済みのため、[radio_control_widget.py](src/ui/radio_control_widget.py)の`_lock_btn.setToolTip()`（現状「DL is yours to tune manually; UL follows (inverting transponder aware).」のみ）に、この遅延（数秒程度、CATのラウンドトリップに起因）に関する一文を追記すること。あわせて、satmode機（IC-9100/9700/910H/821H）ではLock中はUL側もソフトウェアが一切書き込まず、リグ自身のハードウェアSub自動追従に委ねる設計（DLのみ読み取り）であることも、ツールチップまたは近傍のUIテキストで触れるかどうか検討する（現状のツールチップは非satmode機の挙動のみを説明しており、satmode機ユーザーには誤解を招きうる）
2. **ローテーター設定ダイアログの改善** — 接続テストボタン・AZ/ELリミット設定
3. **デバッグ用ログファイル出力の削除または設定化** — `src/main.py` の `_setup_logging()` にある frozen バンドル向けファイルログ出力（`platformdirs.user_log_dir`）は dmg デバッグ目的で追加したもの。Settings に「デバッグログを保存する」チェック（デフォルトOFF）を追加するか削除する。該当箇所: `src/main.py` 63〜75行目
4. ~~**Autotrack/Record メニューの実装**~~ **→ v0.1.0 以降で完了**（AutotrackRecordDialog・Autotrack Timer・AOS/LOS 自動接続・録音自動制御）
1e. ~~**Tuneボタンがバンド中心ではなく`downlink_low`（下限）に固定されている疑い**~~ **→ 2026-08-13 実装完了** — `_refresh_radio_control()`の生SQLに`downlink_high`・`uplink_high`を追加。原因は列の見落としで確定していた（`_current_transmitter`にキー自体が存在せず`_on_tune_requested()`が常にバンド下限側へフォールバックしていた）

### モバイル・Web UI
5. **スマホ・タブレット画面の継続確認** — Android 実機でのコンパス連動確認、各種ブラウザでの表示確認

### SDR・デジタルモード
6. ~~**SDR機能の追加（フェーズ1: 初期実装）**~~ **→ v0.1.0 で完了**
7. ~~**APRS 受信・送信・位置ビーコン実装**~~ **→ feature/communications（v0.2.0）で完了**（APRSEngine・Direwolf統合・Bell 202 AFSK復調・PTT CAT制御・Doppler凍結・地図ピン表示）。**AX.25 9600bps G3RUH対応は2026-07-10で追加完了**（Rig+サウンドカード・SDR単体経由の両方、Auto/1200/9600プルダウン。詳細は「AX.25 9600bps G3RUH 対応」セクション参照。フィルタチューニングは1bで実衛星パス待ち）
8. ~~**Telemetry タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（AX.25受信・JSON定義デコード・12衛星フォーマット定義）
8b. ~~**Telemetry タブ gr-satellites 統合**~~ **→ 2026-06-30 で完了**（gr-satellites サブプロセス・UDP IQ 転送・330機以上対応・衛星コンボ・SDR 自動接続・トランスポンダー自動選択・メインリスト連動）
9. **テレメトリーフォーマット定義の追加・検証** — 実際に受信したパケットでオフセット・スケールの検証。未定義衛星のフォーマット調査
10. ~~**CI: Direwolf バンドルビルド**~~ **→ feature/communications で完了**（Linux/Windows/macOS 3ジョブ、タグ push 時に direwolf-{platform}-{arch}.{tar.gz|zip} を Releases にアップロード）
11. ~~**FT4 タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（Ft4Codec/ctypes + ft8_lib・Ft4Scheduler・Ft4QsoManager・Ft4Tab UI・ADIF エクスポート。ft8_lib CI バンドルビルドは v0.2.0 タグ時に Direwolf と同時実施）
11c. ~~**Q65 Phase 1（RX）実装**~~ **→ 2026-06-26 で完了**（Q65Codec/libq65 ctypes・build-q65lib.yml CI・Help > Q65 Library Installation ダイアログ）
11d. ~~**Q65 Phase 2（TX/QSO）実装**~~ **→ 2026-06-26 で完了**（純 Python encoder.py: GF(64)・CRC-12・65-FSK / Q65QsoManager: QSOステートマシン・q65_log DB・ADIF / q65_tab.py: TX UI・TX Enable・Halt TX・Log QSO・Export ADIF）
11e. ~~**METEOR / HRPT 受信タブ実装**~~ **→ 2026-06-29 で完了**（SatDump サブプロセス管理・8衛星対応・Autotrack AOS/LOS 連携・SDR Connect・浮動ログウィンドウ・衛星検索ダイアログ）
11f. ~~**CW Decoder タブ実装**~~ **→ 2026-06-30 で完了（v0.2.6）**（deepcw-engine ONNX / onnxruntime 自動 pip インストール / model.onnx 自動ダウンロード / CW-R トランスポンダー自動オープン）
11g. **MARMOTSat DVB-S2 受信タブ実装（保留中、2026-07-24 追加）** — AX100 Digi実装（前述「AX100 Digi 機能設計」参照）に続き、MARMOTSatのHF DVB-S2画像ビーコン（29.410 MHz）受信を追加検討したが、**一次情報が入手できず保留**とした:
    - 公表済みスペック（UVic Propagation Lab）: QPSK・roll-off 0.35・33 or 66 kbaud・FEC 1/2・ACM未使用（CCM固定MODCOD）・QO-100 DATV運用慣行準拠
    - MARMOTSat公式サイトが案内するGNU Radio flowgraphのリンク先 `gitlab.orcasat.ca`（UVic CfARの自前GitLab、`/open-source-projects/dvb-s2-decoder` および `/open-source-projects/mcr`）は**ドメイン全体が接続不可**（`connect ECONNREFUSED`、Claude側サンドボックスだけでなくユーザー自身のネットワークからも確認済み。2026-07-24時点）
    - SatNOGSデータベース上でもMARMOTSatの観測・デコード実績は過去30日間ゼロ、登録済みデコーダーも無し。Libre Space Communityフォーラムにも「受信に挑戦したい」という意欲表明のみで成功報告なし（打ち上げ2026-07-07から17日経過時点）
    - 実装上の最大の未確定点: **パイロットシンボルON/OFF**。最有力候補の受信実装 `gr-dvbs2rx`（GNU Radio OOTモジュール）はパイロットON時のみ安定動作し、パイロットレス対応は上流でも未完成なため、この点の確認なしに実装を進めると動くかどうか賭けになる
    - 再開の判断材料（次回セッションでまず確認すること）: (1) `gitlab.orcasat.ca` が復旧したか (2) SatNOGS/Libre Space Communityに新しい受信報告が出たか (3) ユーザーが実IQキャプチャを入手またはMARMOTSat/UVicチームに直接連絡が取れたか
    - 再開時の想定実装方針（未承認）: GNU Radioをアプリに組み込まず、既存のSatDump/gr-satellites連携と同じ「外部ツールをサブプロセス起動してIQを渡す」パターンで `gr-dvbs2rx` の `dvbs2-rx` CLI または `leandvb` をラップする
11b. **SDR フェーズ2（将来）— アマチュア衛星・デジタルモード** — HRPT/LRPT は 11e で完了、gr-satellites は 8b で完了、AI-CW は 11f で完了
12. **SDR フェーズ2（将来）— 業務用衛星受信** — Inmarsat-C (STD-C)・Cospas-Sarsat L帯・Iridium L帯 ACARS・Orbcomm・みちびき（QZSS）データ放送（詳細は「業務用衛星受信」セクション参照）
13. ~~**SDR Device Installation ダイアログ**~~ **→ v0.1.0 で実装済み**（src/ui/sdr_install_dialog.py — USB VID/PID スキャン・apt/brew/Zadig 誘導）
14. ~~**Help > gr-satellites… ダイアログ**~~ **→ feature/communications で完了**（src/ui/gr_satellites_dialog.py — 検出ステータス・バンドルのDownload & Install・conda-forge/PPA/ソースビルド案内。2026-07-31にpip案内の誤りとmacOS向けバンドル配布を追加、詳細は「gr-satellitesのバンドル配布」セクション参照）
15. ~~**SSTV / SSDV 受信タブ**~~ **→ feature/communications で完了**（SstvDecoder・SsdvDecoder・SstvTab・SDR audio_ready 接続・AX.25 raw_frame_received タップ）

### データ・連携
16. **観測ログ機能** — 実際に追尾・通信した衛星パスを記録・集計・エクスポートする機能
17. ~~**多言語対応（日本語）フェーズ2 — メニュー・Help画面・主要タブ/ダイアログ**~~ **→ 2026-07-08 で完了**（View > Language > Japanese 実装済み。詳細は「多言語化ロードマップ」セクション参照。Web UI・タブ内部の一部は引き続き未着手）

### ハードウェア連携
18. **追加リグの実機テスト** — TS-2000・FT-817ND 等でのドップラー制御動作確認（IC-9100・IC-9700 は v0.1.27 で確認済み）
19. **WSJT-X / JS8Call 連携** — デジタルモード運用ソフトとの周波数・モード連動（将来）

---

## 多言語化ロードマップ

### 開発方針
**フェーズ1（完了）**: 英語モードのみで全機能を完成させる。  
**フェーズ2（2026-07-08 に着手・メニュー/Help/主要タブ/主要ダイアログ完了）**: 日本語モードを追加する。

コード中のすべての UI 文字列は `_("...")` でラップ済みであること。
新しい文字列を追加する際も必ず `_("English string")` で書くこと（日本語をハードコードしない）。

### フェーズ2 翻訳範囲（2026-07-08 時点）

**翻訳済み**:
- メニューバー全体（File/Satellite/Radio/Communications/Autotrack-Record/View/Help）
- Help メニューの全ダイアログ（About・Satellite/Transmitter Color Legend・Auto Fetch Rules・
  SDR Device Installation・Check for Updates・Hamlib Update・ft8lib/Direwolf/Q65/FT4拡張/
  CW Model/SatDump/gr-satellites Installation）
- Communications 全タブ（FT4・Q65・APRS・Telemetry・SSTV/SSDV・CW Decoder・METEOR/HRPT）
- Radio Control タブ・SDR Control タブ・Dashboard ステータスバー
- Target/Group パス予測パネル・Pass Chart タブ（タブ名以外）
- 主要ダイアログ（General Settings 全5タブ・Set QTH・Rig Settings 全4タブ・Rotator Settings・
  Autotrack/Record・ADIF エクスポート・Add/Edit Transmitter・Add Manual TLE）
- 衛星リストの右クリックコンテキストメニュー・Satellite メニューの確認/ステータスメッセージ
- 各種エラー・ステータスメッセージ（サウンドデバイスエラー・QRコード・Webサーバー等）

**未着手（意図的）**:
- Dashboard・World Map・Radar タブの内部表示、タブ名自体（Dashboard・Pass Chart・
  Group Pass Chart 等）— 当面英語のまま
- FT4 Waterfall ポップアップ（ユーザー判断により翻訳不要と確定）
- Web UI（`src/web/static/`）— gettext 非対応、別途対応が必要

### 未翻訳・fuzzy の一掃（2026-08-05 完了）

`.po` に長く残っていた fuzzy 9件・未翻訳51件を整理し、**fuzzy 0件・891 translated・
未翻訳24件**（＝上記「未着手（意図的）」と「英語のまま残すもの」に該当する語のみ）にした。

fuzzy は msgmerge が似た文字列から推測した訳がそのまま放置されていたもので、実際に見ると
**内容がまるで違う誤訳が多数**含まれていた（gr-satellites バンドルインストーラーの説明文が
libft4wsjt の文章をそのまま流用していた／「Manual Installation (Alternative)」が
「インストールが完了しました。」／「Extracting…」が「起動中…」等）。Q65タブのオープン失敗
メッセージに至っては msgid の `{error}` に対し訳文が `{exc}` という別名のプレースホルダーを
持っており、**もし使われていれば KeyError になる状態**だった（fuzzy エントリは `msgfmt` が
`.mo` から除外するため実際には一度も使われず、露見していなかった）。

**教訓**: `msgmerge` 後の `#, fuzzy` は「だいたい合っている訳」ではなく「**機械が似ていると
判断しただけの、内容を確認していない訳**」。本ファイルの `.po` 更新手順に既に
「1件ずつ内容を見て正しい訳に直してから `#, fuzzy` 行を削除すること」と書いてあるが、
実際には長期間放置されて上記のような状態になっていた。`.po` をマージしたセッションでは、
その場で fuzzy を0にしてからコミットすること。

新規に翻訳したもの: SoapyRemote / Add Remote Host 関連UI（Rig Settings・SDR Device
Installation の注記全文）、`CommandRow` の「📋 コピー」「▶ ターミナルで実行」ボタン
（全 Help ダイアログで共用されるため影響範囲が広い）、gr-satellites のダウンロード進捗、
onnxruntime 欠落警告、パス予測パネルの現地時刻ヘッダー、main_window の各種ラベル。

### 用語・表記の方針（英語のまま残すもの）

技術用語・プロトコル名・ハム無線の手続き語は無理に訳さず英語のまま統一する。

| カテゴリ | 具体例 |
|---|---|
| リグ制御モード名 | Direct・NET・CTCSS・CAT・CI-V |
| SDR/DSP 用語 | SDR・IQ・AGC・BW・RF・PPM・Bias-T・LNA |
| 周波数・信号の略語 | DL・UL・RX・TX・AZ・EL・dBFS・N/A |
| ハム無線の手続き語 | CQ・RST・R+RST・RR73・73・QSO・ADIF・DT |
| リグ番号ラベル | Rig 1 / Rig 2（Radio Control・Rig Settings・FT4・Q65・パス予測パネルで統一） |
| 日付範囲フィールド | From: / To:（ログエクスポート・パス予測パネル・APRS宛先で共有のため統一） |

**「Range」訳語の使い分け**（同一英単語でも文脈により訳を変える例）:
- Satellite Detail パネル・Dashboard ステータスバー（衛星までの距離）→ **「レンジ」**
- Pass Chart タブ（時間範囲セレクター）→ 上記と msgid が競合するため **英語のまま**
  （`src/ui/pass_chart.py` では `_()` でラップせず生文字列 `"Range:"` を使用）

### i18n 実装上の落とし穴（重要）

翻訳作業中に発覚した、`_()` の使い方次第で **翻訳が静かに効かなくなる** 2つのパターン。
新しいコードを書く・既存コードを翻訳対応させる際は必ず確認すること。

**1. f-string の `{}` 内に `_()` を書くと xgettext が検出できない**

```python
# NG: xgettext がこの _() 呼び出しを検出できず、.pot に一切現れない
label.setText(f"{_('Range')}: {value}")

# OK: 先に変数へ代入してから f-string で使う
range_label = _("Range")
label.setText(f"{range_label}: {value}")
```
実行時は問題なく動作する（Python はこの2つを区別しない）ため、**アプリを動かして気づくことはできない**。
`xgettext` で再抽出した後、該当 msgid が `.pot` に含まれているか確認する以外に発見する方法がない。

**2. モジュールレベルの定数として `_()` を呼ぶと、起動時の言語が固定されてしまう**

```python
# NG: モジュール import 時（= アプリ起動時の最初の import で）一度だけ評価される。
#     main_window.py はこの手のモジュールを起動シーケンスの非常に早い段階
#     （_load_saved_language() より前）で import することが多く、常に英語になる。
_RANGE_OPTIONS = (
    (_("Next 4 hours"), 4.0),
    ...
)

# OK: 関数化して呼び出し時に評価する（対象ウィジェットの __init__ 内で呼ばれるため、
#     _load_saved_language() 実行後になる）
def _range_options() -> tuple[tuple[str, float], ...]:
    return (
        (_("Next 4 hours"), 4.0),
        ...
    )
```
`gr_satellites_dialog.py`・`pass_chart.py` で実際に発生したパターン。

**3. 同一英語文字列を複数箇所で共有している場合の競合**

gettext は msgid（英語原文）をキーに翻訳を引くため、同じ英語文字列を異なる文脈で
使っていると訳語が競合する（例: 「Range」＝距離 vs 時間範囲、「To:」＝宛先 vs 日付範囲）。
競合が発覚したら、**片方を英語のまま残す**（`_()` でラップしない生文字列に戻す）か、
**元の英語文字列自体を文脈固有のものに変える**（例: `"Time Range:"` のように）かの
どちらかで解消する。どちらが良いかはユーザーに確認すること（本ファイルの「最重要ルール」通り、
表示文言の見た目に関わる判断は実装者が独断しない）。

**4. 原文中の「パーセント記号」が printf 書式指定子と誤認され、日本語訳が弾かれる
（2026-08-05 発見・修正済み）**

xgettext は msgid をスキャンして「この文は書式文字列か」を自動判定し、該当すれば
`#, python-format` フラグを付ける。以後 `msgfmt --check` は「訳文にも同じプレースホルダーが
揃っているか」を検査する（`{error}` を消す・`{exc}` に書き換える等の事故を防ぐための安全装置。
実際、この検査対象になっていなかった fuzzy エントリで `{exc}` 誤りが1件見つかっている）。

問題は、**単なる「％」の意味で使った `%` も書式指定子と誤認されうる**こと。実測した挙動:

| 原文中の並び | xgettext の判定 | 理由 |
|---|---|---|
| `(% of full scale)` | ❌ python-format 付与 | `% o` = スペース修飾子付き8進数と解釈 |
| `need well under 100% here.` | ❌ python-format 付与 | `% h` と解釈 |
| `Simple percent: 50%.` | ✅ フラグなし | `%` の直後がピリオドで書式指定子として成立しない |

フラグが付くと、日本語訳側の `（フルスケールに対する%）` の `%）` が
「無効な変換指定子」として `msgfmt --check` にエラー扱いされる。**実行時の実害はない**
（該当文字列は `%` フォーマットを一切通さず、そのまま表示するだけ）が、`--check` を使う
ビルド工程を将来追加すると通らなくなる。素の `msgfmt`（本ファイルの手順）はこの検査を
行わないため、長期間気づかれずに残っていた。

**対処方針**: `.po` 側に `#, no-python-format` を手書きで足す方法もあるが、`.pot` を
再生成するたびに消える可能性がある。**原文側で `%` の直後に「スペース＋英字」が来ない
書き方に変える**のが恒久的（例: `(% of full scale)` → `(percentage of full scale)`、
`100% here.` → `100%.`）。上表の通り文末の `100%.` は誤認されないので、数値表現自体は
残せる。実際に Q65 タブ・FT4 タブの送信レベルツールチップ2件をこの方針で書き換え、
`.pot` から `python-format` フラグを全廃した（該当コードには理由をコメントで明記済み。
「`(% of full scale)` のほうが自然では」と将来戻されるのを防ぐため）。

### 言語切り替えの永続化

`View > Language > English / 日本語` はチェック可能な `QActionGroup`。選択すると
`app_settings` テーブル（キー: `ui_language`、値: `"en"` / `"ja"`）に保存され、
`MainWindow._load_saved_language()` が `_build_ui()` / `_build_menu()` より **前** に
呼ばれてこの値を読み込み `i18n.set_language()` を適用する。

**既存ウィジェットは動的に再翻訳されない**（gettext ベースの `_()` は呼び出された瞬間の
翻訳カタログを使うだけで、Qt のようなレイアウト全体の再翻訳機構は持たない）ため、
言語切替後は「再起動が必要です」ダイアログを表示するだけに留めている。

### 日本語 `.po` 更新の作業手順

#### 1. 翻訳対象文字列の抽出
```bash
# src/ 以下の _("...") を全て抽出して .pot ファイルを生成
xgettext --language=Python --keyword=_ --keyword=ngettext:1,2 \
    -o locale/fbsat59.pot \
    $(find src/ -name "*.py")
```

#### 2. 既存 ja.po へのマージ（`ja.po` は既に存在するため msginit は不要）
```bash
msgmerge --update --backup=off \
    locale/ja/LC_MESSAGES/fbsat59.po \
    locale/fbsat59.pot
```
`msgmerge` は既存訳を保持しつつ新規文字列を追加するが、**msgid の文言がわずかでも変わると
`#, fuzzy`（似た文字列からの誤った推測）が付くことがある**。マージ後は必ず
`grep -c '#, fuzzy' locale/ja/LC_MESSAGES/fbsat59.po` で件数を確認し、1件ずつ内容を見て
正しい訳に直してから `#, fuzzy` 行を削除すること（誤った推測をそのまま残すと実際に誤訳のまま
配布されてしまう）。

#### 3. .po ファイルの翻訳編集
`locale/ja/LC_MESSAGES/fbsat59.po` を開き、`msgstr ""` の部分に日本語訳を記入する。

```po
# 例
msgid "Ready"
msgstr "準備完了"

msgid "Range"
msgstr "レンジ"
```

#### 4. .mo ファイルのコンパイル
```bash
msgfmt locale/ja/LC_MESSAGES/fbsat59.po \
       -o locale/ja/LC_MESSAGES/fbsat59.mo
```
警告・エラーが出た場合はコミットしない（`msgfmt --statistics` で翻訳済み/未翻訳件数を確認する習慣が有用）。

#### 5. 動作確認
```python
import sys
sys.path.insert(0, "src")
from i18n import _, set_language
set_language("ja")
print(_("Range"))  # 追加・変更した文字列が意図通り訳されているか確認
```
GUIを実際に起動しての確認が難しい場合、上記のようなワンライナーで個々の msgid の翻訳結果を
検証すれば十分。レイアウト崩れ（日本語訳が長くなり枠が窮屈になる等）は
`QT_QPA_PLATFORM=offscreen` で該当ウィジェットをオフスクリーン生成し `sizeHint()` や
`width()` を確認することで、実機を使わずに検出できる。

#### 6. コミット前チェックリスト・コミット対象
本ファイル冒頭の「コミット前チェックリスト」（`ruff format` → `ruff check` →
`pytest tests/test_rig.py`）を通してから、`.po` と `.mo` の両方をコミットする
（`.mo` はバイナリだが配布に必要）。`locale/fbsat59.pot` も併せてコミットしておくと
次回のマージ作業がしやすい。

### 注意事項
- `_("...")` の中身は**常に英語**で書く（gettext の msgid が英語前提）
- Qt 標準ダイアログ（QMessageBox等）のボタン文字列は Qt 側の翻訳ファイル（`qtbase_ja.qm`）が担当するため別途対応不要
- Web UI（`src/web/static/`）の JavaScript 文字列は別管理（gettext 非対応）。フェーズ2では手動置換またはブラウザ向け i18n ライブラリの導入を検討する

---

## HamlibRotatorController — Catch-up タイムアウト設計

### 仕組み
接続直後の初回 `set_position()` 呼び出し時、ローテーターは現在位置から
目標 AZ/EL へ向かって動き始める（**catch-up フェーズ**）。
この間、毎サイクル `get_position()` でローテーターの実位置を確認し、
目標との差が **5 度以内**になった時点で通常追跡（毎サイクル P コマンド送信）に移行する。

### タイムアウト再送信
低速なローテーター（AZGTI 等）や衛星と同方向移動中など、
5 度以内に収束しないまま時間が経過するケースがある。
`_CATCH_UP_TIMEOUT = 60.0`（秒）を超えても catch-up が終わらない場合は、
現在の衛星 AZ/EL を改めて P コマンドで送信してタイマーをリセットする。
これにより、ローテーターが古い目標位置に向かって動き続ける問題を回避する。

### 定数（src/rig/controller.py — HamlibRotatorController）
| 定数 | 値 | 意味 |
|---|---|---|
| `_CATCH_UP_THRESHOLD` | 5.0 度 | この差以内になったら通常追跡へ移行 |
| `_CATCH_UP_TIMEOUT` | **60.0 秒** | この時間を超えたら P コマンドを再送信 |

---

## HamlibNetController 実装メモ（2026-05-20 確認済み）

### rigctld 標準プロトコルと VFO 割り当て（全機種共通）

**接続時（1回のみ）:**
  S 1 Main → RPRT 0  （split ON。Main=RX(DL) / Sub=TX(UL) を確立）

**毎サイクル（1秒間隔）:**
  F {dl_hz} → RPRT 0  （Main=RX / ダウンリンク周波数。前回から1Hz以上変化した場合のみ）
  I {ul_hz} → RPRT 0  （Sub=TX / アップリンク周波数。前回から1Hz以上変化した場合のみ）

**VFO 割り当ての原則（Hamlib 全機種共通）:**
- `S 1 Main` 送信後: **Main = RX（ダウンリンク）、Sub = TX（アップリンク）**
- `F {hz}`: Main VFO（RX/ダウンリンク）の周波数を設定
- `I {hz}`: Sub VFO（TX/アップリンク）の周波数を設定（split TX）
- 各バックエンドがこの割り当てを実現する仕組みはリグ固有だが（下記参照）、結果は全機種共通

**各リグでの実現メカニズム（Hamlib ソースで確認済み）:**
| リグ | S 1 Main の動作 |
|------|----------------|
| FTX-1F | バックエンドが S コマンド引数に関わらず Main=RX を強制 |
| IC-9700 | `S 1 Main`（tx_vfo=Main）が satmode を自動 ON → satmode 時は常に Main=RX, Sub=TX |
| FT-991A | 標準 split 動作: Main(VFOA)=RX, Sub(VFOB)=TX（実機確認済み） |
| その他 Hamlib 対応機 | 同様の split 動作で Main=RX, Sub=TX |

### FTX-1F 固有の制約（Hamlib バックエンドが吸収）
- S 1 Main 応答に約150ms かかる
- F/I コマンド応答は約150ms
- **アクティブVFO切り替え（`V` コマンド）は絶対に使用禁止**。TXランプ点灯どころか、
  実機でMain/Subの役割そのものが入れ替わる実害を確認済み（2026-07-15、詳細は
  「Lock（Lボタン）— dial feedback設計」セクション参照）。ソフト側の読み戻し値
  （`f`/`i`/`v`）が正常に見えても、裏で物理的な役割が壊れていることがある
- ~~f/i（get_freq）コマンドはF/I送信直後に10秒以上かかる → 使用禁止~~
  **誤診断だった（2026-07-15訂正）**。原因はリグ側の制約ではなく、クライアント側
  `_cmd_raw()`が問い合わせ系コマンド（小文字、`f`/`i`等）の成功時応答に`RPRT`行が
  一切含まれない（`RPRT`はエラー時のみ）ことを考慮せず、無条件に`RPRT`を待ち続けて
  いたバグだった。修正済み（`_cmd_raw()`のdocstring参照）。`f`/`i`は現在、Lock
  dial feedbackで実際に毎秒利用しており、正常に動作している

### 実装上の重要事項
- set_vfo_frequencies()はバックグラウンドスレッドで実行（UIブロック防止）
- _cmd()はソケットタイムアウト10秒
- connect()時に_last_dl_hz/_last_ul_hzをNoneにリセット
- S 1 Mainは接続時1回のみ（毎サイクル送らない）
- f/iダイアルフィードバック（Lock機能）は実装済み。詳細は「Lock（Lボタン）—
  dial feedback設計」セクション参照

### send_mode_only() VFO順序の根拠

全 Hamlib 対応機共通の VFO 割り当て: Sub=TX(UL), Main=RX(DL)

**クロスバンド（satmode リグ）の正しい順序:**
```
S 1 Main  （satmode確立）
V Sub  → M {ul_mode} 0  （Sub=TX=アップリンク）
V Main → M {dl_mode} 0  （Main=RX=ダウンリンク）
```

**同バンド（satmode リグ・V/V または U/U）の正しい順序:**
```
S 1 VFOB  （通常split確立: VFOA=RX, VFOB=TX）
V VFOB → M {ul_mode} 0  （VFOB=TX=アップリンク）
V VFOA → M {dl_mode} 0  （VFOA=RX=ダウンリンク）（または V Main 相当）
```

**非satmode リグ（FTX-1F, FT-991A等）:**
```
V Sub  → M {ul_mode} 0  （Sub=TX=アップリンク）
V Main → M {dl_mode} 0  （Main=RX=ダウンリンク）
```

`send_mode_only()` は `_is_same_band` フラグで上記3パターンを自動分岐する。
S 1 Main / S 1 VFOB は `apply_transponder_state()` 内の `_send_split_init_independent()` でモード設定より先に独立ソケットで送信し、satmodeを確立してからmode・CTCSSを設定する（Direct modeと同じ順序）。`connect()` の `_init_vfo()` でも再送されるが、リグはすでにSATモードに入っているため冪等。

### 動作確認環境
- リグ: Yaesu FTX-1F
- PC: GPD MicroPC2 (Ubuntu)
- Hamlib: 4.7.1-rc (2026-02-16) モデルID 1051
- 接続: USB → /dev/FTX1CAT → udev/systemd → rigctld:4532

### モード文字列 → リグCATモード変換テーブル（2026-07-05 確定）

トランスポンダーの `mode` 文字列（SATNOGS形式）は、以下5つの独立したテーブルすべてに
登録しないとリグに正しく反映されない（`src/rig/controller.py`）:

| テーブル | 用途 |
|---|---|
| `MODE_MAP` | 汎用 Hamlib 定数（起動時に静的構築） |
| `_build_live_hamlib_mode_map()` | Direct mode の satmode/generic Hamlib 経路（旧: 3箇所に重複していたのを統合） |
| `_FTX1_MODE_CODES` | FTX-1F raw CAT（`MD` コマンド1桁コード） |
| `_FT991_MODE_MAP` | FT-991/FT-991A raw CAT（Direct・NET両方の `ctcss_method=="ft991"` 経路で共用） |
| `_SATNOGS_TO_RIGCTLD_MODE` | NET mode 汎用 rigctld（`M {mode} 0` コマンド名） |

**未登録モードは必ずFMにフォールバックすること。** Direct mode 側は元々 `.get(mode, "4")` /
`.get(mode, RIG_MODE_FM)` で徹底されていたが、NET mode 側（`HamlibNetController.send_mode_only()`
の `ft991` 分岐・汎用rigctld分岐）はフォールバックせず「両VFOとも未登録なら何も送信しない」
実装になっており、未登録モード選択時にリグが**直前のモードのまま放置される**バグがあった
（2026-07-05 修正）。`SSTV`・`SSDV`・`DOKA`・`FSK` 等、SATNOGSには存在するが本アプリの
CATテーブルには元々登録されていないモード文字列は今後も出てくるため、このフォールバックは
どちらの経路でも必須。

**FT4通話周波数の実装（`community_transmitters.json`）**: 当初 `mode: "FT4"` を使っていたが
上記5テーブルのどこにも存在せず、リグがFMにフォールバック（Direct mode）または何も送信されない
（NET mode）状態だった。FT4はUSB相当のデータモードで復調する必要があるため、`"USB-D"`
（DATA-USB相当）／`"LSB-D"`（DATA-LSB相当）を新設し全5テーブルに追加。`_MODE_INVERT`
（main_window.py）にも `USB-D⇔LSB-D` を追加し、RS-44のような反転トランスポンダーで
既存のUSB⇔LSB反転と同じ仕組みでDL/ULのサイドバンドが自動的に入れ替わるようにした。

**教訓**: 「あるトランスポンダーのモード文字列を選択してもリグの実際の動作が変わらない/前の
モードのままになる」系の不具合を見たら、まずこの5テーブルすべてに当該モード文字列が登録されて
いるか、かつNET mode側にFMフォールバックが効いているかを確認すること。

---

## NETモードでFTX-1のDL/ULが両方Sub VFOに誤配送されるバグ — 原因はFBSAT59ではなく
姉妹プロジェクトctld-launcherの側だった（2026-08-17 解決）

### 症状

FTX-1をNETモード（rigctld経由、`ctcss_method=="ftx1"`）でFO-29に接続し、ドップラー補正を
確認したところ、**DL（145.xxx MHz帯）・UL（435.xxx MHz帯）とも実機ではSub VFOへ書き込まれて
いた**（Main側は更新されない）。ユーザーからの報告時点で、以下が既に切り分け済みだった:
- Directモード（Hamlib in-process、rigctld不使用）では一度も発生していない
- macOS移行前（Ubuntu環境）ではNETモードでもこの問題は起きていなかった

### 誤った仮説と、それを覆した実機検証（教訓として記録）

**最初に「Hamlibのバージョンが原因」と誤って結論しかけた**。開発環境をUbuntu→macへ移行した際に
Hamlibを4.7.2へ更新していたため、①dmgパッケージ版ctld-launcher（同梱4.7.2）でも再現 → ②同じMac
上でHamlib 4.7.1のrigctldを（ctld-launcherを完全停止し）手動起動して試すと再現しない、という
2点だけを根拠に「4.7.1→4.7.2のFTX-1F backend変化が原因」と判断し、実際に公式Hamlibソース
（`rigs/yaesu/ftx1/*.c`・共有の`newcat.c`・core の `src/rig.c`/`src/misc.c`・`tests/rigctld.c`/
`rigctl_parse.c`）を4.7.1/4.7.2間で全ファイルdiffするところまで進めた（結果は「Memory mode」
関連の追加とCVE対応のパスワード認証修正のみで、VFO解決ロジック自体は1バイトも変化なし）。

この段階でユーザーから「Hamlibコアの問題とは思えない、これまで問題は起きていなかった」との
強い指摘があり、**同じ手順（ctld-launcherを完全停止して手動でrigctld起動）でHamlib 4.7.2を
試したところ、こちらも問題が再現しないことが判明**——直前の「4.7.1なら直る」という結果は
Hamlibバージョンの違いではなく、**「ctld-launcherが関与しているかどうか」という別の変数**が
たまたま同時に変わっていたことによる誤った結論だった。

**教訓**: A/Bテストで2つの環境を比較する際、意図して変えた変数（Hamlibバージョン）以外に
**同時に変わってしまっている変数がないか**（今回は「ctld-launcher経由かどうか」）を常に疑う
こと。特に「動いていた時期と動いていない時期」を比較する調査では、ユーザーが明示的に
述べた事実（「これまで問題はなかった」）が自分の仮説と矛盾する場合、仮説の方を疑うべきで、
外部ソースの差分を延々と追うより先に、比較対象自体が本当にクリーンな一変数比較になっているか
を確認する方が近道だった。

### 真の原因

姉妹プロジェクト[ctld-launcher](../ctld-launcher)（本プロジェクトのNETモードが接続する
`rigctld`を、GUIから起動・監視するランチャーアプリ）には、稼働中のrigctld/rotctldを
5秒間隔で健全性チェックする機能（`_run_health_checks`、v0.1.17でWSJT-Xハング対策として追加）
があり、これがHamlib組み込みのNET rigctlクライアント（`rigctl -m 2 -r host:port f`）を
サブプロセスとして起動し、稼働中のrigctldへFBSAT59とは**別のTCPクライアント**として
接続していた。

このクライアントは、Hamlibの汎用`rig_open()`が接続直後に自動送信する「v」(get_vfo)問い合わせを、
ctld-launcher自身のコードには一切現れない形で（Hamlibライブラリ内部の暗黙の挙動として）
発生させる。rigctldサーバー側の`rig_get_vfo()`（Hamlib `src/rig.c`）はこの問い合わせに対し、
無線機に実際に`VS;`(アクティブVFO問い合わせ)を送って結果を返すだけでなく、**rigctldプロセス
全体で共有される`current_vfo`の値をその結果で上書きする**（キャッシュ実装、Hamlib 4.7.1/4.7.2
とも同一コード）。

一方、rigctldのプレーンな「F」コマンド（VFO省略、本プロジェクトのNETモードが常用する形式）は
`rig_set_freq(rig, RIG_VFO_CURR, freq)`として処理され、`RIG_VFO_CURR`は`vfo_fixup()`では
解決されず、呼び出し元がその場の`current_vfo`へ置き換える実装になっている（`src/rig.c`確認済み）。
つまり**「F」の行き先は、その瞬間のrigctldの`current_vfo`が何であるかに完全に依存する**。

FTX-1Fはクロスバンド/split構成で継続的にドップラー追尾する際、無線機の物理的な表示VFOが
運用中にSub側を指すことがある既知のファームウェア癖を持つ（本ファイル「Lock（Lボタン）」
セクションで詳述している`VS`/`FT`コマンドの相互作用と同根）。ctld-launcherのヘルスチェックが
このタイミングに重なると、rigctldが管理する`current_vfo`が恒久的にSubへ書き換わってしまい、
以降**本プロジェクトが送るVFO省略の「F」書き込みも全てSub側へ誤配送される**——「I」（UL、
`set_split_freq`）はFTX-1F backend自体の仕様で元々Sub固定なので正しく見えるが、「F」（DL、
本来Main）も巻き込まれてSubへ行くため、結果としてDL/UL両方がSub VFOに書き込まれる、という
今回の症状に一致する。

**本プロジェクト自身は「V」/「v」コマンドを意図的に一度も送らない設計**（本ファイル「Lock
（Lボタン）— dial feedback設計」セクション参照。過去の実機トラブルで学んだ制約）だが、
**同じrigctldを共有する他クライアント（今回はctld-launcherのヘルスチェック）がそれを
送ってしまえば、本プロジェクト側の注意深さだけでは防げない**、という構図だった。

### 修正

修正はFBSAT59側ではなく[ctld-launcher](../ctld-launcher)側で行った（詳細はctld-launcherの
CLAUDE.md参照、v0.1.19）。`rigctl -m 2`（Hamlib NET rigctlクライアント経由）を使うのをやめ、
生ソケットで直接プレーンな「f」(rig)/「p」(rotator)をrigctld/rotctldへ送るだけの
`probe_daemon()`に置き換えた。Hamlibソース（`src/rig.c`の`rig_get_freq()`）を確認した結果、
`current_vfo`は読むだけで一切書き換えないことが確認できており、本プロジェクトのLock機能が
日常的に送っている「f」/「i」と全く同じ安全なコマンドである。ヘルスチェック・稼働中プロファイル
への「Test Connection」ボタンの両方をこの方式に切り替えている。

### 教訓

- 「稼働中デーモンへの疎通確認」のような一見無害な監視機能でも、それが使うクライアント
  ライブラリが接続の副作用として何を送っているかまでは意識されにくい。Hamlibのように
  複数のTCPクライアントが単一の共有リグ状態（`current_vfo`）を扱うプロトコルでは、
  ある1つのクライアントの「読み取りのつもり」の操作が、無線機の物理状態と絡んで**他の
  全クライアントの動作**に影響することがある
- 本プロジェクトが「V」を避けているという既存の防御は、**本プロジェクト自身の直接操作**
  にしか及ばない。同じrigctldに繋がる可能性のある外部ツール（GPredict・WSJT-X・
  ctld-launcher等）が同種の問題を起こしうることを、NETモード関連の不具合調査では
  今後も疑うこと
- A/Bテストで結論を出す前に、比較対象が本当に「意図した1変数だけ」の違いになっているか
  を再確認すること。今回は「Hamlibバージョン」のつもりが実際には「ctld-launcherの
  関与有無」も同時に変わっていた

---

## Lock（Lボタン）— dial feedback設計（2026-07-16 確定）

### 要件（ユーザー確定仕様）

Lock（RadioControlWidgetのLボタン）は「衛星のドップラーシフト量をそのままULにミラーする」
機能では**ない**。正しい要件は次の通り：

1. Lock ON中、運用者がリグのDL側VFOを物理的に手で回す
2. ソフトはその変化を検知し、回した量（Hz、無変換の生値）だけULを反転して加減算する
   （反転トランスポンダーなら符号反転。GPredict本家のdial feedbackと同じ「生のHzデルタ」方式
   ——帯域幅比によるスケーリングではない。この点は2026-07-15に本家GPredictソースを確認して確定
   済み。V/Uトランスポンダーの3:1比うんぬんは誤解だった）
3. Lock OFFにした瞬間、その時点の周波数からドップラー補正を再開する（ゼロにリセットして
   元の中心周波数へスナップバックしない）

### 実装に至るまでの重要な失敗と教訓（すべて2026-07-15〜16、FTX-1F実機で確認）

過去に何度も「ログ上は動いているように見える」状態で実装を提出し、実機テストで
「全く動いていない」と繰り返し指摘された。この経緯そのものが教訓として重要：

1. **`V`（VFO切替）コマンドは絶対に使用禁止**。DLを明示的なVFO指定で読もうとして
   `V Main` 等を送ったところ、ソフト側の読み戻し値（`f`/`i`/`v`）は終始正常に見えて
   いたにもかかわらず、裏で実機のMain/Sub役割そのものが入れ替わっていた（TXランプが
   Sub→Mainへ移動）。1回きりの使用でも実害が出る。読み込みは常に`F`/`I`と対称な
   **VFO指定なしの`f`/`i`**のみを使うこと
2. **フレッシュな接続の最初のF/I/fサイクルだけ`f`が壊れる**（`I`書き込み直後の`f`が
   Sub側の値を返す、rigctldの「current VFO」キャッシュが原因）。2サイクル目以降は
   自己修復する。`i`（get_split_freq、`I`の読み込み版）は順序に関わらず常に信頼できる。
   このため、DLの読み値は同じポーリングで読んだUL値と近すぎないか毎回クロスチェックし
   （`_DIAL_FEEDBACK_CROSSCHECK_HZ`）、怪しければ棄却する防御を入れている。
   **注意（2026-07-20 ユーザーから質問・回答済み）**: この「UL読み取り」は**ULへ実際に
   書き込む値を決めるためのものではない**（書き込み値は`_doppler_cycle()`が計算する
   `ul_corr`＋オフセットであり、読み取ったUL値とは無関係）。目的は純粋に上記クロス
   チェックのみ——DLの読み値が異常（=実はUL値を誤って返している）でないかを検算する
   ための防御的な読み取りであり、UL自体の値が何かの計算に使われることはない
3. **読み込みと書き込みを別々のタイマー間隔で動かしてはいけない**。当初、Connect後の
   読み込み検知を独立した2秒間隔のポーラーで行っていたところ、Dopplerサイクル本体
   （約1秒間隔）の次の書き込みが、ポーラーが読む前に手動リチューンを上書きしてしまい、
   検出自体が成立しなかった。読み込みと書き込みは**同一サイクル・同一スレッド**で
   行う必要がある（GPredict本家も単一ループ）
4. **検出したサイクルで即座に書き込みへ畳み込む必要がある**。「次のサイクルで反映」と
   いう1サイクル遅延の設計では、検出したサイクルではまだ古い値を書き込んでしまうため、
   ダイヤルを回した瞬間に一旦もとの周波数へ強制的にスナップバックしてしまう
5. **最大の設計ミス**: 上記4までの修正を全て行っても「回している最中にも周波数が
   元に戻る」問題が解決しなかった。原因は、**Lock ON中もDL側を毎サイクル絶対周波数で
   書き込み続けていたこと自体**だった。書き込みは約150〜300ms前に読み取った値を
   もとにしており、これが運用者のリアルタイムなダイヤル操作と競合し、回している
   最中に古い値へ引き戻し続けていた（「いくら回しても、すぐに戻る、どれだけ回したかも
   分からない」という報告と一致）。**周期的な絶対周波数書き込みは、自由な手動VFO操作
   とは本質的に相容れない**。唯一の解決策は、Lock ON中はDL側の書き込みを完全に止める
   ことだった
6. 上記5の修正直後、今度は「ULの書き込みも同時に止めてしまう」設計にしたところ、
   ユーザーから「ULには物理的な競合相手（運用者の手）がいないのだから、書き込みを
   止める必要はない」との指摘があり、UL側は書き込みを継続する設計に変更した（実運用の
   衛星通信では、Lockが長時間ONのままだと実際の送信周波数がドップラー未補正のまま
   放置されるのは望ましくないため）

### 最終的な設計

**状態**: `MainWindow._dial_feedback_offset_hz`（float, Hz）—— 手動リチューンで検出した
オフセット。**Lock ON/OFFの切替ではリセットされない**（要件3）。リセットされるのは
トランスポンダー変更時とTボタン押下時のみ

**`_doppler_cycle()`**（DopplerWorker、約1秒間隔）:
```python
dl_corr = correct_downlink(dl_nom, rr)              # 常に計算（Lock状態に関わらず）
ul_corr = correct_uplink(ul_nom, rr, invert=invert)  # 同上。ULは常に自分自身の正しい
                                                       # 搬送波周波数比でスケールされる
                                                       # （DLの絶対シフト量を1:1でミラー
                                                       # するのではない。V/Uトランスポンダー
                                                       # で約3:1比になるのが物理的に正しい）
dl_corr_base = dl_corr  # _rig_send()の直接計算用ベースライン（オフセット抜き）
if self._dial_feedback_offset_hz != 0.0:              # Lock状態に関わらず無条件に適用
    dl_corr += offset
    ul_corr += (-offset if invert else offset)
```

**`_rig_send()`**（Doppler cycleと同一スレッド、Rig 1接続中）:
- `do_dial_feedback = Lock ON かつ HamlibNetController かつ ctcss_method in ("ftx1","ft991")`
- `do_dial_feedback`が真の場合：
  1. `rig.get_frequency()`/`rig.get_split_frequency()`で`f`/`i`を読む（`V`不使用）
  2. クロスチェック・サニティチェック通過後、`self._dial_feedback_offset_hz = live_dl - dl_corr_base`
     （**差分の累積ではなく、毎回「読み値 − 期待値」で直接算出**。DLを一切書かなくなった
     ことで「自分の書き込みが実際に反映されたか」という曖昧さ自体が消え、単純な直接計算で
     十分かつ安全になった）
  3. `rig.set_vfo_frequencies(None, ul)` —— **DLは`None`を渡して書き込みを完全にスキップ**
     （`vfoa_hz=None`だと`set_vfo_frequencies()`内部の送信条件`vfoa_hz is not None`が
     falseになりF送信自体が起きない）。**ULは`_doppler_cycle()`が計算した値（表示用と
     同じ、このサイクルの読みより1サイクル遅れた値）をそのまま書き込み続ける**
- `do_dial_feedback`が偽（Lock OFFまたは対象外リグ）の場合：従来通り`rig.set_vfo_frequencies(dl, ul)`
  で両方書き込む。この`dl`/`ul`は`_doppler_cycle()`で計算済みのオフセット込みの値なので、
  Lock OFFにした瞬間から自然に「そのオフセットを保持したままドップラー補正を再開」できる
  （要件3を満たすための特別な分岐は不要——単に既存の書き込み経路がオフセット込みの値を
  使うだけで自動的に実現される）

**`_lock_watch_cycle()`**（Connect前のみ、`_lock_watch_worker`による2秒間隔ポーリング）:
- Connect後は即座にreturn（読み込みは`_rig_send()`に一本化）
- `self._engine.observe()`で自前にDopplerベースラインを計算し、独立接続
  （`read_dl_ul_independent()`、`S 1 Main`→読み込みのみ、`F`/`I`は一切送らない）で読む
- **書き込みは一切行わない**（`write_ul_independent()`は呼び出し元がなくなったため
  `HamlibNetController`から削除済み）。Connect前はリグが実際に送受信しているわけでは
  ないため、UL継続書き込みの恩恵は薄いと判断し、シンプルに読み込み専用のままにしている

### スコープ

Rig 1 のみ。対象:
- `HamlibNetController`（NET mode）:
  - `ctcss_method in ("ftx1", "ft991")`: 接続前後とも対応（`_lock_watch_cycle()`含む）
  - satmode（「Icom SAT mode rig」チェックボックスON）: `ctcss_method`非依存の独立分岐、
    **接続後のみ**。DLのみ読み取り、ULは読み書きしない（2026-07-22修正、下記
    「satmode NETモードにも同一クラスの不具合が実在した」参照）
  - `ctcss_method == "hamlib"` かつ非satmode: **接続後のみ**、未検証のベストエフォート
    （IC-705 NET modeを含む汎用リグ全般。下記「NETモード汎用"hamlib"バケットへの展開」参照）
  - `ctcss_method == "custom_cat"`は対象外
- `HamlibDirectController`（Direct mode、**全機種対応・接続後のみ**。接続前の
  `_lock_watch_cycle()`は引き続き上記NET modeの`ftx1`/`ft991`専用）。内訳:
  - FTX-1F・FT-991/FT-991A・IC-705: Hamlibソースで個別に安全性を確認済み
  - satmode（IC-9100/9700等）: クロスバンド（リニアトランスポンダ）用途限定。
    DLのみ"Main"で読み取り、ULは読み書きしない（2026-07-22修正、下記「satmode
    Directモード実機確認で判明した重大な誤り」参照）
  - それ以外の非satmode機種（汎用Hamlibルート）: 未検証のベストエフォート

Rig 2は対象外（今後の課題、2026-07-20 ユーザーと確認済み）。`_doppler_cycle()`内の
`_rig2_send()`は今回のLock機能実装を通じて一度も変更しておらず、常に
`rig2.set_vfo_frequencies(dl2, ul2)`という無条件書き込みのまま。Rig 1と全く同じ
「DL手動リチューンが毎サイクル上書きされる」問題が未解決で残っている。実装する場合の注意点:
`self._trsp_lock`（Lockボタンの状態）は今回のdial feedback機能と、既存の別機能
（SDRのPassband TuneオフセットをLock時に相手リグのTXへミラーする機能）の**両方**で
共有されている変数のため、Rig 2固有のdial feedbackオフセット状態は`_dial_feedback_offset_hz`
とは別に新設する必要がある（Rig 1とRig 2は物理的に別のリグ＝別のDLダイヤルのため）。
対象外の組み合わせでは`self._dial_feedback_offset_hz`は常に0.0のままで、Lockは何もしない
（副作用なし）。

### Directモードへの展開（FTX-1F、2026-07-20 実装）

NET modeでの実装・実機確認が完了した後、Directモードや他リグへの展開を開始した。手始めに
FTX-1FのDirectモードから着手し、実機の前にHamlibソース自体（`/home/sadatoshi/Hamlib-4.7`に
ローカル配置済みの、`/opt/hamlib/4.7`ビルド元と同一バージョンのソースツリー）を読んで設計上の
リスクを洗い出した。

**確認1: VFO切り替えリスクは無い**。FTX-1F専用バックエンド（`rigs/yaesu/ftx1/ftx1.c`）は
`.targetable_vfo = RIG_TARGETABLE_ALL`を宣言しており、`ftx1_get_freq()`/`ftx1_set_freq()`
（`rigs/yaesu/ftx1/ftx1_freq.c`）は指定VFOに応じて`FA;`（VFOA/Main）または`FB;`（VFOB/Sub）を
**直接**CATで送るだけ。Hamlib本体の`rig_get_freq()`（`src/rig.c`）は`targetable_vfo &
RIG_TARGETABLE_FREQ`が真の場合、アクティブVFOの切り替え（`set_vfo()`）を一切行わずに
直接バックエンドの`get_freq`を呼ぶ分岐を通ることを確認済み。NET modeで実機破損の原因となった
「V」コマンド相当の危険はDirectモードには存在しない。

**調査中に見つかった別の罠（結果的に無関係と判明）**: `ftx1_vfo.c`の`ftx1_set_split_freq()`/
`ftx1_get_split_freq()`には、UL書き込みがHamlibコアの内部キャッシュ機構のバグでDL(Main)側の
キャッシュ枠まで巻き込んで壊してしまう（"VFOA and MAIN share freqMainA slot"）という既知の
不具合への対処コードがあり、「UL読み取り→DL読み取り」の順で呼ばれることを前提に復元される
（コメントに「GPredictはget_freqの前にget_split_freqを呼ぶ」と明記）。これを見て当初は
Directモードでも読み取り順序をNET modeと逆にする必要があると判断したが、`src/cache.c`の
`rig_set_cache_freq()`を実際に読んだところ、`RIG_VFO_B`書き込みは`cachep->freqMainB`という
**MAIN(`freqMainA`)とは完全に独立したキャッシュ枠**に書き込むことが判明した。上記の不具合は
`rig_set_split_freq()`（`set_split_freq`/`get_split_freq`API）経由の呼び出しに限定されており、
FTX-1F Directモードの既存UL書き込み実装（`_set_vfo_frequencies_locked()`、非satmode分岐）は
**元々plainな`set_freq(VFOB)`を使っており`set_split_freq`を一切呼んでいない**ため、この不具合
はそもそも該当しないと結論した。したがって読み取り順序は任意でよい（実装は`get_frequency
("VFOA")`→`get_frequency("VFOB")`の順のまま、NET modeと合わせている）。

**実装**（[main_window.py](src/ui/main_window.py)・[controller.py](src/rig/controller.py)）:
- `_is_dial_feedback_rig()`: `HamlibDirectController`かつ`model_id in _FTX1_MODEL_IDS`の場合も
  真を返すよう拡張
- `_rig_send()`（`_doppler_cycle()`内）: `isinstance(rig, HamlibNetController)`で分岐し、
  Directモードは`rig.get_frequency("VFOA")`（DL）/`rig.get_frequency("VFOB")`（UL）を使用。
  それ以外（オフセット計算・クロスチェック・サニティチェック・DLを書かずULだけ書く設計）は
  NET modeと完全に共通のロジックをそのまま流用
- `HamlibDirectController.get_frequency()`: 呼び出し元がなく未使用だった既存メソッドに、
  `set_vfo_frequencies()`と同じ`_rig_cmd_lock`を追加。Lock機能からの読み取りが、同スレッド内で
  直後に呼ばれる書き込み（`set_vfo_frequencies()`）や、他の同時実行し得るHamlib呼び出し
  （モード/CTCSS変更等）と競合しないようにするため

**未実装（今後の課題）**: 接続前（`_lock_watch_cycle()`）のDirectモード対応。NET modeの
`read_dl_ul_independent()`に相当する「接続前に短命なHamlibセッションを開いて覗き見る」実装が
必要になるが、FTX-1F Directモードはボーレート誤設定時のHamlibタイムアウトフリーズを避けるため
モード/CTCSS設定を意図的にHamlib経由で行わず生CAT（`os.open()`）を使っている（本ファイル
「FTX-1F 固有の制約」参照）。周波数読み取り専用の一時的なHamlibセッションであっても同種の
リスクを抱える可能性があるため、今回は見送り、Directモードは接続後のみの対応とした。

**この調査の副産物として発見・修正した別バグ**: FTX-1F Directモードの実機確認中、Connect直後に
TXがSub→Mainへ勝手に戻る不具合を発見した（IC-705対応時に追加された`set_vfo(VFOA)`表示復元が
共有コードパス経由でFTX-1Fにも巻き込まれていた）。詳細は「Rig-Specific Implementation Notes」
内「FTX-1F (Hamlib model 1051)」セクションの「Connect直後にTXがSubからMainへ勝手に戻るバグと
修正」参照。この一件は今回のLock機能自体のバグではなく、Directモードの既存コードに以前から
潜んでいた不具合だったが、Lock機能の実機確認作業がきっかけで発覚した。

#### FT-991 / FT-991A への展開（2026-07-20 実装）

FTX-1Fでの実装・実機確認完了後、同じDirectモードのLock機能をFT-991/FT-991A（Hamlib model
1035/1036）にも展開した。FTX-1Fのとき同様、実機の前にHamlibソース
（`/home/sadatoshi/Hamlib-4.7/rigs/yaesu/ft991.c`・`newcat.c`）を確認した。

**確認**: `ft991.c`は`.targetable_vfo = RIG_TARGETABLE_FREQ`を宣言（FTX-1Fの`RIG_TARGETABLE_ALL`
ほど広くはないが、周波数の直接読み取りに必要なビットは含まれる）。実装本体`newcat_get_freq()`
（`newcat.c`、FT-991含む多くのYaesu機で共用）は、指定VFOに応じて`FA;`/`FB;`を直接送るだけで、
内部で`set_vfo()`を一切呼ばない（`newcat_set_vfo_from_alias()`はVFO定数のローカルな解決のみで
CAT通信を発生させない）。FTX-1Fと同じ安全性が確認できた。

また、FT-991 DirectモードのUL書き込みは元々Hamlibを経由せず生CAT`FB;`を直接書き込む方式
（`_FT991_DIRECT_MODEL_IDS`専用分岐）で、`set_split_freq()`を一切呼んでいない。DL/ULの
キャッシュ枠分離（`freqMainA`/`freqMainB`）はHamlib**コア共通**の仕組みのため、FTX-1Fで確認した
「読み取り順序は任意でよい」という結論もそのまま当てはまる。

**実装**: `_is_dial_feedback_rig()`の対象を`_FTX1_MODEL_IDS`単体から
`_FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS`に拡張しただけ。`_rig_send()`のDirectモード分岐
（`get_frequency("VFOA")`/`get_frequency("VFOB")`）は既に機種非依存の実装だったため変更不要。
`_lock_watch_cycle()`の接続前ガードも同様に機種非依存（`isinstance(rig, HamlibNetController)`
判定のみ）のため変更不要だった。

#### IC-705・および汎用Hamlibルートへの展開（2026-07-20 実装）

FT-991対応の直後、ユーザーから「IC-705は専用の明示的分岐にして他機種の変更に巻き込まれない
ようにし、それ以外の非satmode機（汎用Hamlibルート）にも一応Lock機能を入れておこう」という
方針が示され、その通りに実装した。

**IC-705の確認**: `rigs/icom/ic7300.c`（IC-705のcaps定義。IC-7300と同系列のバックエンドを
流用）は`.targetable_vfo`に`RIG_TARGETABLE_FREQ`を含む。実装本体`icom_get_freq()`
（`rigs/icom/icom.c`）は、この宣言があり`force_vfo_swap`条件（Main/Sub**と**A/Bを両方
持つsatmode機のみ該当。IC-705はA/Bのみなので非該当）に当てはまらない場合、Icom公式CI-Vコマンド
`0x25`（`icom_get_freq_x25()`）で指定VFOを直接読み取り、VFO切り替えを一切行わない。FTX-1F/
FT-991と同じ安全性が確認できた（`0x25`はIcom公式のドキュメント化されたコマンドであり、
FTX-1Fの`VS`/`FT`のような非公式流用よりむしろ安全性の根拠は強い）。

IC-705 DirectモードのUL書き込みは、FTX-1Fと同じく素の`set_freq(VFOB)`（`set_split_freq()`を
使わない）ため、読み取り順序も任意でよい。

**IC-705を独立分岐にした理由**: 今回のFTX-1F TX巻き戻りバグ（本セクション前半「Connect直後に
TXがSubからMainへ勝手に戻るバグと修正」参照）は、IC-705向けに追加された`set_vfo(VFOA)`表示
復元が、共有コードパス経由でFTX-1Fにも意図せず適用されてしまったのが原因だった。この教訓を
踏まえ、`_is_dial_feedback_rig()`ではIC-705を`_FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS`とは
別の独立した`if`分岐として扱い、将来IC-705固有の変更が入っても他機種に影響しない構造にした。

**汎用フォールバック（未検証・ベストエフォート）**: 上記いずれにも該当しない非satmode
Direct-modeリグ（`not rig.is_satmode`）も、ユーザーの明示判断により一律でLock機能の対象とした。
ただし**これは個別の安全性確認を経ていない**。FTX-1F/FT-991/IC-705はそれぞれHamlibソースの
`targetable_vfo`宣言と実際の読み取り実装を確認した上で安全と判断したが、汎用バケットに該当する
未知の機種が同じ安全性を持つ保証はない——今回のFTX-1Fの件のように、一見安全に見える操作が
特定機種のファームウェアでは異なる副作用を持つ、という事態は実機でしか発覚しないことがある。
**このバケットに該当する機種を実際に使う場合は、FTX-1F/FT-991/IC-705のときと同様にHamlib
ソースと実機で個別に確認し、確認が取れた時点でこのバケットから外して専用の明示的分岐に
昇格させること。**

**実装**: `_is_dial_feedback_rig()`を以下の構造に変更（[main_window.py](src/ui/main_window.py)）:
```python
if isinstance(rig, HamlibNetController) and rig._ctcss_method in ("ftx1", "ft991"):
    return True
if not isinstance(rig, HamlibDirectController):
    return False
if rig._model_id in (_FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS | _IC705_MODEL_IDS):
    return True
return not rig.is_satmode
```
`_rig_send()`・`_lock_watch_cycle()`はどちらも既に機種非依存の実装だったため変更不要
（FT-991のときと同じ）。

#### NETモード汎用"hamlib"バケットへの展開（2026-07-20 実装）

Directモードへの展開が一段落した後、ユーザーから「NETモードのIC-705はどうなるのか」という
質問があり、調査の結果、Directモードとは事情が異なることが判明した。

**IC-705をNETモードで個別扱いできない理由**: Rig Settingsの`ctcss_method`プルダウン
（[rig_dialog.py:500-504](src/ui/rig_dialog.py#L500-L504)）は`"hamlib"`（Hamlib standard・
デフォルト）/`"ftx1"`/`"ft991"`/`"custom_cat"`の4値のみで、IC-705専用の値は存在しない。
satmodeかどうかは別のチェックボックス（`is_satmode_rig`）で管理されている。つまりIC-705を
NETモードで使う場合は`ctcss_method="hamlib"`＋satmodeチェックOFFになり、これは**他の
汎用リグと設定上まったく区別がつかない**。Directモードの`model_id`（機種を一意に識別できる）
とは根本的に粒度が異なり、「IC-705だけを独立分岐にする」ことが構造的にできない。

**NETモードの読み取り機構は「今アクティブなVFO」を聞くだけ**: `get_frequency()`/
`get_split_frequency()`はバレの`f`/`i`（`RIG_VFO_CURR`、VFO指定なし）を送る。「現在アクティブな
VFOの周波数」を聞くことは、どのリグであっても定義上VFO切り替えを一切必要としない（既に
アクティブなものを聞くだけのため）。この点はDirectモードの`targetable_vfo`確認と違い、
リグの機種に依存せず常に安全と言える。

ただし残る未検証点は、**rigctldの内部的な「現在アクティブなVFO」追跡が、`_init_vfo()`の
split初期化後に確実にDL側を指し続けるか**という部分で、これはFTX-1Fで実機検証して初めて
判明した話（「接続直後の最初の1サイクルだけ`f`が壊れて自己修復する」という癖、本ファイル
「FTX-1F 固有の制約」参照）であり、Hamlibソースを読むだけでは他機種について確認しきれない。
Directモードの汎用フォールバックより一段階、確認の難易度が高いことをユーザーと共有した上で、
それでも「一応入れておこう」という判断で実装した。

**実装**: `_is_dial_feedback_rig()`のNET mode判定を拡張:
```python
if isinstance(rig, HamlibNetController):
    if rig._ctcss_method in ("ftx1", "ft991"):
        return True
    return rig._ctcss_method == "hamlib" and not rig.is_satmode
```
`get_frequency()`/`get_split_frequency()`/`set_vfo_frequencies()`はいずれも既に
`ctcss_method`非依存の実装だったため、`_rig_send()`側の変更は不要。

**接続前ポーリングは対象外のまま**: `_lock_watch_cycle()`内の`read_dl_ul_independent()`は
引き続き`ctcss_method in ("ftx1", "ft991")`のみをサポートする内部ゲートを持つため、
`"hamlib"`バケットは接続後のみで機能する。`_lock_watch_cycle()`自体にも、この汎用バケットが
接続前に無意味な「read failed」ログを出し続けないよう、`ctcss_method not in ("ftx1",
"ft991")`での早期returnを追加した（元々`isinstance(rig, HamlibNetController)`のチェックを
通過してしまうため、追加のガードが必要だった）。

#### satmode機（IC-9100/9700等）NETモードへの展開（2026-07-20 実装・最も未検証）

Directモード・NETモード汎用バケットへの展開に続き、最後の未対応領域だったsatmode機に着手した。
ユーザーから「satmodeはVFOAを回すとVFOBも連動して動くのではないか。それならULの書き込みを
やめてもいいのでは」という指摘があったが、**これはHamlibソースだけでは確認できなかった**
（Hamlibは飽くまでCAT/CI-V制御プロトコルのライブラリであり、CATコマンドのやり取りなしに
リグ内部のRF/DSPだけで完結するような挙動——もし実在するなら——はソースに一切現れない。
`icom.c`を"SATMODE"で検索してもCI-Vによるsatmode ON/OFF制御しか見つからず、VFO連動
トラッキングに関する記述はなかった）。

この点は未検証のまま、ユーザーの判断で**保守的な設計**（これまでの全機種と同じ「DLは読むだけ・
ULは書き込み続ける」）で進めることにした。判断理由: 現状Lock機能はsatmode機に対して
一切動作しないため、この保守的な実装がたとえ不完全でも「今までと変わらない」だけで、
悪化はしないという判断（ユーザー本人の言葉）。

**実装**: `_is_dial_feedback_rig()`にsatmodeの独立分岐を追加（`ctcss_method`の値とは無関係、
`is_satmode`プロパティのみで判定）:
```python
if isinstance(rig, HamlibNetController):
    if rig._ctcss_method in ("ftx1", "ft991"):
        return True
    if rig.is_satmode:
        return True
    return rig._ctcss_method == "hamlib"
```
`get_frequency()`/`get_split_frequency()`/`set_vfo_frequencies()`はいずれも既に
satmode非依存の実装（RXサイクルの`vfoa_hz is not None`判定・UL間引きロジックとも、
DLを書くかどうかとは無関係に動作）だったため、`_rig_send()`側の変更は不要。接続前
ポーリングも同様の理由で対象外のまま（satmode NETリグは通常`ctcss_method="hamlib"`のため、
既存の`ctcss_method not in ("ftx1", "ft991")`ガードで自然に弾かれる）。

**これまでで最も未検証な理由**: satmodeのクロスバンド時は`S 1 Main`送信によりHamlibが
実際に**ハードウェアのSATMODEを起動**する（`set_func(RIG_FUNC_SATMODE, 1)`相当）。
これは他の全リグ（ソフト/仮想的なsplit管理のみ）と質的に異なるステートフルな
ハードウェア状態遷移であり、satmodeがアクティブな間、rigctldの「現在のVFO」追跡が
継続的にMain（DL）を指し続けるかは一度も確認していない。加えて上記の「VFOA/VFOB
連動トラッキング機能」の実在も未確認のまま。**GitHubで問題が報告された場合、
まずこのVFO連動トラッキング機能の実在確認を優先し、実在するなら「ULの書き込みも
停止する」設計への変更を検討すること。**

#### satmode機Directモードへの展開（2026-07-20 実装）

NET modeでのsatmode対応に続き、Directモード（IC-9100/9700等）にも展開した。

**スコープの単純化（ユーザー判断）**: 当初、同バンド機（satmode解除してVFOA/VFOB通常splitに
フォールバックするケース）への対応も含めてコントローラー内部に専用メソッドを新設する設計を
提案したが、ユーザーから「Lock機能は本質的にリニアトランスポンダ（クロスバンド）専用の
機能であり、同バンドFM機には不要」との指摘があり、同バンドケースは意図的に対象外とした。
これにより実装が大幅に単純化された。

**DL/ULの読み取りVFO指定**: `RIG_VFO_MAIN`/`RIG_VFO_SUB`/`RIG_VFO_TX`という専用のVFO文字列
（`"VFOA"`/`"VFOB"`ではなく`"Main"`/`"TX"`）を使う。理由は`rigs/icom/icom.c`の`icom_get_freq()`
を確認して判明した:
- IC-9100/9700は`vfo_list`に`Main/Sub`と`A/B`の両方を含む機種（`VFO_HAS_MAIN_SUB_A_B_ONLY`が
  真）で、この種の機種では`RIG_VFO_SUB`（またはSUB_A/SUB_B）の読み取りだけがVFO切り替え
  （`set_vfo_curr()`）を強制される（コメント「Icom 0x25 command can only manipulate VFO A/B
  *or* VFO Main/Sub frequencies」）。ただし**Main側はこの判定の対象外**（`RIG_VFO_SUB`系のみが
  対象）なので、DLは`get_freq(RIG_VFO_MAIN)`で問題なく安全に読める
- UL側は`RIG_VFO_TX`という専用の読み取り経路（`icom_get_tx_freq()`、CI-Vコマンド
  `S_RD_TX_FREQ`＝送信周波数専用の直接読み取り）が用意されており、これが使えればVFO切り替え
  無しで安全に読める。この専用コマンドが特定機種・ファームウェアで使えない場合は
  （`priv->x1cx03cmdfails`で検知）、Hamlib自身の内部的なswap-then-restore（`set_vfo_curr()`）へ
  自動的にフォールバックする——これは今回の一連の調査で見つかった他の不具合（FTX-1Fの
  `set_vfo(VFOA)`副作用等）と違い、**Hamlib公式の、対称的で十分にテストされた内部機構**であり、
  本プロジェクト独自の場当たり的な外部VFO管理とは根本的にリスクの質が異なる

`HamlibDirectController._vfo_str_to_const()`に`"TX": self._hamlib.RIG_VFO_TX`のマッピングを
1行追加しただけで、コントローラー側に新規メソッドは不要だった。

**実装**: `_is_dial_feedback_rig()`のDirectモード判定を「`HamlibDirectController`なら無条件で
真」に単純化（FTX-1F/FT-991/IC-705の明示列挙は変わらず残し、それ以外はsatmode・汎用問わず
すべて対象——satmodeを除外する理由がもはや無くなったため）。satmode/非satmodeの区別は
`_is_dial_feedback_rig()`（対象かどうかの判定）ではなく`_rig_send()`側に移し、
`rig.is_satmode`で読み取りVFO文字列を`"Main"/"TX"`（satmode）か`"VFOA"/"VFOB"`
（それ以外）かに振り分ける:
```python
if rig.is_satmode:
    live_dl = rig.get_frequency("Main")
    live_ul = rig.get_frequency("TX")
else:
    live_dl = rig.get_frequency("VFOA")
    live_ul = rig.get_frequency("VFOB")
```
書き込み側（`set_vfo_frequencies(None, ul)`）は元々satmode/非satmode問わず
`vfoa_hz is not None`判定でDLスキップに対応済みだったため変更不要。

これでLock機能は、Rig 1・NET/Directモード問わず、事実上すべてのリグ構成に対応した
（Rig 2のみ今後の課題として残る）。

#### satmode Directモード実機確認で判明した重大な誤り — UL（"TX"）読み取りは安全ではなかった（2026-07-22 修正）

上記実装から2日後、実際にIC-9100（知人から借用）で実機確認したところ、2つの障害が報告された：

1. **DLの周波数がソフトで読み込めなくなる**（`get_frequency("Main")`の返り値が特定の値のまま
   フリーズし、DLダイヤルを実際に回しても追従しない）
2. **Lockボタンをオフにすると「Python is not responding」ダイアログが表示され、ソフトが応答
   しなくなる**（ユーザーが強制終了を選択すると`Killed`——SIGKILL——としてプロセスが終了する。
   これはPythonの例外・クラッシュではなく、Qtメインスレッドが完全にブロックされるハング）

`hamlib_trace.log`（Hamlibの内部デバッグトレース、実機接続時に有効化される）を確認したところ、
決定的な証拠が見つかった。`get_frequency("TX")`を呼ぶと：

```
rig_get_freq called vfo=TX
vfo_fixup(2103): split=0, vfo==TX tx_vfo=TX
vfo_fixup: RIG_VFO_TX changed to Sub, split=0, satmode=1
```

**`RIG_VFO_TX`は、`icom_get_freq()`に届く前の、もっと手前の段階——汎用`rig_get_freq()`
（`src/rig.c`）が呼ぶ`vfo_fixup()`（`src/misc.c`）——で無条件に`RIG_VFO_SUB`へ変換されて
しまっていた**：

```c
else if (vfo == RIG_VFO_TX)
{
    ...
    else if (VFO_HAS_MAIN_SUB_A_B_ONLY && satmode) { vfo = RIG_VFO_SUB; }
    ...
}
```

つまり実装時に「`RIG_VFO_TX`は`icom_get_tx_freq()`という専用の安全な読み取り経路を持つ」と
判断した根拠（`icom_get_freq()`内の`if (vfo == RIG_VFO_TX) { icom_get_tx_freq(...); ... }`
分岐）は、**`vfo_fixup()`によって`vfo`がその時点で既に`RIG_VFO_SUB`に書き換えられているため、
一度も実行されていなかった**。実際には毎サイクル、IC-9100/9700のような`Main/Sub`と`A/B`を
両方持つ機種（`VFO_HAS_MAIN_SUB_A_B_ONLY`）で強制される`force_vfo_swap`経路（実際のVFO切り替え
`set_vfo_curr()`を伴う）に入っていた。トレース中には`set_vfo_curr: ... returning2(-1)
Invalid parameter`という失敗も多数記録されており、FBSAT59がsatmodeを`set_func(SATMODE,1)`
だけで有効化しHamlib標準の`set_split_vfo()`を一度も呼ばない（＝Hamlib内部の`split`キャッシュ
フラグが`0`のまま）ことと相まって、このVFO切り替えの整合性が取れず、Hamlib内部のVFO追跡状態が
繰り返すうちに壊れていったと考えられる。この状態破壊が、DLの読み取りフリーズと、Lockオフ後の
最初の書き込み再開時にシリアル通信がハングした（Qtメインスレッドが巻き込まれてブロックされ、
「Python is not responding」に至った）ことの、両方の説明として整合する。

**追加の設計判断（ユーザー指摘）**: 上記のバグ発覚と合わせて、ユーザーから「satmode機はそもそも
RX（Main）を回すとTX（Sub）もハードウェア側で自動的に連動して動く機種であり、ソフトウェアから
ULへ書き込む意味自体が薄いのではないか」という指摘があり、実機でこの連動動作自体は確認できて
いた（本ファイル前方のsatmode NETモード展開セクションで「未確認」としていた仮説が、今回の
実機テストで真だったと判明）。

**修正**: satmodeのDirectモードLock処理を大幅に単純化した。
- UL（"TX"）の読み取りを完全に廃止。DLのみ`get_frequency("Main")`で読む
  （Mainは`vfo_fixup()`の対象外——`RIG_VFO_TX`/`_SUB`/`_SUB_A`/`_SUB_B`のみが対象——なので
  引き続き安全）
- クロスチェック（DL値とUL値が近すぎないか）も、比較対象のUL自体が存在しなくなったため廃止
  （このクロスチェックは元々NET modeの「今アクティブなVFOが曖昧」という問題への対策であり、
  Directモードの明示的VFO指定読み取りにはその曖昧さ自体が無いため、無くても安全性は変わらない）
- **satmodeはLock ON中、DLもULも一切書き込まない**（他の全リグはUL書き込みを継続するのに対し、
  satmodeだけの例外）。ハードウェア側の自動連動に任せる
- Lockをオフにした瞬間、通常の書き込み経路（`rig.set_vfo_frequencies(dl, ul)`、両方書き込み）に
  自然に戻るため、「オフにした時点の周波数からドップラー補正を再開する」という要件3はそのまま
  満たされる（既存の無条件オフセット適用の仕組みをそのまま利用しているだけで、satmode専用の
  特別な復帰処理は不要）

**教訓**: Hamlibの高レベルAPI（`icom_get_freq()`の`if (vfo == RIG_VFO_TX)`分岐）を読んで
「安全な専用経路がある」と判断しても、その手前の汎用レイヤー（`rig_get_freq()`→`vfo_fixup()`）
で引数自体が書き換えられてしまい、期待した分岐に一度も到達していない、ということがありうる。
Hamlibのソースを読んで安全性を判断する際は、呼び出し対象の関数単体だけでなく、**そこに到達
するまでの上位ラッパー（`rig_get_freq()`/`rig_set_freq()`）が引数をどう変換するか**まで
追う必要がある。今回はこの見落としに、実機での2つの独立した症状（DLフリーズ・Lockオフ時の
ハング）が揃って初めて気づけた——ログ上の推測だけで「安全なはず」と判断せず、実機検証を
最後まで待つことの重要性を改めて示す事例。

#### satmode NETモードにも同一クラスの不具合が実在した — 実機確認と修正（2026-07-22）

上記のDirectモード修正の直後、ユーザーから「NETモードのsatmodeは大丈夫なのか」と問われ、
実装前にまずHamlibソースを読んで確認した。NETモードの`i`コマンド（`get_split_frequency()`）は
rigctld内部で最終的に`rig_get_split_freq(rig, RIG_VFO_TX, ...)`（`tests/rigctl_parse.c`の
`get_split_freq`ハンドラで確認）というAPIを呼んでおり、これは`get_frequency("TX")`と
同じ危険（`vfo_fixup()`によるVFO強制切り替え）を抱えている可能性が高いと判断した。

`rig_get_split_freq()`（`src/rig.c`）自体を読むと、`caps->targetable_vfo &
RIG_TARGETABLE_FREQ`が真の機種だけは直接`caps->get_split_freq()`を呼ぶ「速いパス」を通り、
それ以外は`vfo_fixup()`を経由する「Assisted mode」（`set_vfo(tx_vfo)`で一時的にSubへ切替
→読む→`set_vfo(save_vfo)`でMainへ復帰を試みる）に落ちる。IC-9700は`targetable_vfo`に
`RIG_TARGETABLE_FREQ`を含むため速いパス（安全）だが、**IC-9100は`targetable_vfo = 0`
のため「Assisted mode」に落ちる**——Directモードで実害を確認したのと同じ危険な経路。

この時点ではユーザーの了承を得た上でまず実機（IC-9100・NETモード、`rigctld`を手動起動して
テスト）で試してもらったところ、以下が確認された:

- **最初の接続では理想的に動作**（Lock ONでDL書き込み停止・ダイヤルを回した分だけ正しく
  読み取り・ULへの書き込みは継続）
- **2回目以降の接続で不安定化**。`fbsat59.log`を確認すると、DL読み取りのつもりの`f`が
  UL帯（2m帯）の値（`live_dl=145840000.0`・`live_dl=144490000.0`）を繰り返し返しており、
  `_DIAL_FEEDBACK_SANITY_HZ`のimplausible-jump判定で誤反映こそ防げていたが、根本的には
  「現在アクティブなVFO」自体がSubに固定されてしまっていた

**原因**: `rig_get_split_freq()`の「Assisted mode」の復帰ステップ
（`caps->set_vfo(rig, save_vfo)`、`save_vfo = RIG_VFO_MAIN`）の戻り値は
「try and revert even if we had an error above」という扱いで実質的に無視される。実機で
この復帰が黙って失敗すると、rigctldの「現在VFO」はSubに固定されたまま残り、以降`f`
（現在VFO取得、VFO引数なし）を送るたびにSubの周波数（UL帯）が返り続ける。1回目の接続では
たまたま復帰に成功していた（または`S 1 Main`直後の状態と偶然一致していた）だけで、
再接続を繰り返すうちに復帰が失敗する状態に陥った、と考えれば「最初だけ理想的・2回目以降
不安定」という実機報告と正確に一致する。

**修正**: Directモードと同じ設計に統一した。satmode NETモードは`i`（`get_split_frequency()`）
を一切呼ばず、DLのみ既存の`f`（`get_frequency()`、VFO引数なし）で読む。ULのクロスチェック
（`_DIAL_FEEDBACK_CROSSCHECK_HZ`との比較）も、比較対象のUL自体を読まなくなったため実施しない。
Lock ON中はDLもULも書き込まず、Directモードと同じくリグ自身の確認済みハードウェアMain→Sub
自動連動に任せる。`_rig_send()`内で`isinstance(rig, HamlibNetController) and rig.is_satmode`
を独立した早期分岐として、既存の非satmode NET分岐（ftx1/ft991/generic hamlib、`f`/`i`両方を
読みクロスチェックする経路）より手前に追加した。

**教訓**: Direct/NET一方のリグ種別で見つかった「高レベルAPIの手前で引数が書き換えられる」
という類のHamlib不具合は、同じ根本原因（`rig_get_freq()`/`rig_get_split_freq()`双方が
共有する`vfo_fixup()`と`targetable_vfo`判定）を持つ他の経路にも実在しないか、実装済みの
修正と対になる箇所（今回はDirectの"TX"読み取り修正に対するNETの"i"コマンド）を必ず
洗い出して確認すること。ユーザーからの「NETモードは大丈夫なのか」という一言がなければ、
この不具合はDirectモードの陰に隠れたまま次のGitHub報告まで発覚しなかった可能性が高い。

**実機再検証済み（2026-07-22）**: 修正後、同じIC-9100・同じ手動`rigctld`環境で接続・切断・
再接続を複数回繰り返す再現手順を再度試したところ、以前見られた「2回目以降で不安定化」
（DL読み取りがUL帯の値を返し続ける現象）は再発せず、安定して動作することを確認した。
これでLock機能はsatmodeについても、Direct・NET両モードとも実機で安定動作が確認された
状態になった（Rig 2は引き続き対象外）。

**別ユーザーのIC-9700実機でも確認済み（2026-07-22、GitHub Issue #14）**: v0.2.21投稿への
返信として、Issue #14の報告者（ei4gnb）がv0.2.25でIC-9700実機を使ってテストし、「LOCKで
手動チューニングでき、もう一度LOCKを押すと現在のVFO位置からドップラー補正が再開される」
（期待通りの動作）と報告があった。NET/Directいずれのモードで使用したかは報告に明記されて
いないが、IC-9100でNETモードは（本セクションの修正前でも）比較的正常に動いていたことを
踏まえると、NETモードだった可能性が高いと推測している（確定情報ではない）。いずれにせよ、
本プロジェクト保有のIC-9100借用実機に加え、別ユーザー・別個体のIC-9700でも独立して
Lock機能の正常動作が確認できたことになる。

#### satmode Directモード — Ctrl+Lで「Python is not responding」を再現・原因特定・修正（2026-07-22）

Ctrl+Lホットキー実装後、実機（IC-9100・Directモード）で試したところ、1回目のLock ONは
問題なく動作した（DL書き込み停止・手動リチューン量を正しく読み取り）が、**2回目に押した
（Lock OFF）ときに「Python is not responding」が再発**した。ユーザーからの指摘で
「今日は時々この現象が起きていたが、satmode機へのドップラー書き込み方法自体は変える
べきではない」という前提のもと、`fbsat59.log`・`hamlib_trace.log`を確認し、Hamlibソースを
さらに深く調査した結果、これまで見落としていた事実が判明した。

**新たな発見**: `icom_get_freq()`自体の内部ロジック（Mainは`force_vfo_swap`判定の対象外）
とは**別に**、その手前の汎用`rig_get_freq()`/`rig_set_freq()`（`src/rig.c`）自体が、
独立して`caps->targetable_vfo & RIG_TARGETABLE_FREQ`をチェックしている。IC-9100は
`targetable_vfo = 0`のため、この汎用ラッパーのレベルで「要求されたVFOが現在のVFOと
一致しない限り、内部で`caps->set_vfo()`を挟む」という分岐に入る——**これはMain読み取りも
例外ではない**（`vfo == rs->current_vfo`が条件のため）。

さらに`rig_set_freq()`（クロスバンドsatmodeのUL書き込み `set_freq(VFO_TX/SUB, ul_hz)`が
内部で通る経路）を読むと、この非targetable経路では`rig_set_vfo(rig, vfo)`でSubへ切り替えた
後、**Mainへの復帰処理が一切ない**（`rig_get_freq()`/`rig_get_split_freq()`にはある
「try and revert」の復帰ロジックが、`rig_set_freq()`には存在しない）。一方、既存の
コントローラー実装（`_set_vfo_frequencies_locked()`のクロスバンドsatmode分岐）も、
UL書き込み後にMainへ戻す処理を持たない（同バンドフォールバック分岐にはある
`self._rig.set_vfo(rx_vfo)`が、クロスバンド分岐には無い）。

これらを組み合わせると、実際に起きていたことが説明できる: UL書き込み直後、Hamlib内部の
「現在VFO」はSubに残ったまま。**その直後にLockをONにすると**、Lockの読み取り
（`get_frequency("Main")`）が「現在VFOがMainでない」ため、汎用`rig_get_freq()`の
非targetable経路（Subへ切替→読み→**元のSubへ復帰**）を毎サイクル発動させる。これは
今回のLock機能が、このsatmodeブランチに初めて持ち込んだ新しいVFOアクセスパターンであり、
UL書き込みの後始末の悪さ（Mainへの復帰なし）と組み合わさって、短時間に何度もVFO切替
コマンドが送られることになり、リグ側の拒否（Hamlib error -9、`hamlib_trace.log`で確認済み）
と、最終的な応答なしハング（トレースがCI-Vコマンド送信直後、応答ログなしで途切れる）を
引き起こしていた。

**ユーザーからの重要な指摘**: 「同バンド分岐にある`self._rig.set_vfo(rx_vfo)`をクロスバンド
にも追加すればいいのでは」という対策案に対し、ユーザーから「同バンドはHamlib標準のsplit
モードでありsatmodeとは別物。satmodeで明示的な`set_vfo()`を新設すれば、今回の調査全体が
示した『satmodeでの明示的VFO切替は危険』という結論に反するのでは」という的確な指摘があった。
その通りであり、この対策案は撤回した。

**採用した修正（通常の書き込みロジックには一切手を加えない、Lock読み取り側だけに限定した
対策）**: `HamlibDirectController`に`self._last_written_vfo: str | None`（"Main"/"Sub"/None）
を新設し、クロスバンドsatmode分岐のDL書き込み成功時に`"Main"`、UL書き込み成功時に`"Sub"`を
記録する（`connect()`/`disconnect()`で`None`にリセット）。公開メソッド
`last_written_vfo_is_main() -> bool`を追加し、`_rig_send()`のsatmode Direct分岐は
**このメソッドが`False`を返す場合（直前がUL書き込み、または不明）、`get_frequency("Main")`
自体を一切呼ばず、そのサイクルはスキップ**（読み取り失敗時と同様、オフセットは前回値を維持）
するよう変更した。DLはほぼ毎サイクル書き込まれるため、危険な瞬間は実質「UL書き込み直後の
1サイクルだけ」に限定され、次のサイクルには自然にMainへ戻って通常通り読み取れる。

この設計により:
- 通常の書き込みロジック（DL/UL双方の`_set_vfo_frequencies_locked()`）は一切変更なし
- 新設した`_last_written_vfo`フラグの更新も、書き込みが成功した後に追記するだけで、
  書き込みのタイミング・順序・リトライロジックには影響しない
- Lockの読み取り側だけが、危険な瞬間（UL書き込み直後）を検知して自ら1サイクル分だけ
  沈黙する、という限定的な変更にとどまる

テスト: `tests/test_main_window.py`の`TestLockDialFeedback`に
`test_rig_send_direct_satmode_skips_read_when_last_write_was_ul`を追加
（`rig._last_written_vfo = "Sub"`の状態で`get_frequency()`が一切呼ばれず、オフセットも
変化しないことを検証）。既存の3件の satmode Direct テスト（読み取り成功・失敗・
implausible jump）は`_make_satmode_direct_rig()`ヘルパーが`_last_written_vfo = "Main"`を
デフォルト設定するよう変更し、通常の読み取り経路を引き続き検証する。

**検証状況（2026-07-22 実機再検証済み）**: 修正後、実機（IC-9100・Directモード）でLock
ON/OFFを何度も連続で繰り返す再現手順を試したところ、「Python is not responding」は
再発せず、安定して動作することを確認した。

**この不具合がDirectモード特有である理由（NETモードでは再現しない根拠）**: NETモードの
UL書き込み（rigctldの`I`コマンド）は内部でHamlibの`rig_set_split_freq()`を呼んでおり、
これは末尾に「try and revert even if we had an error above」という**復帰処理**
（`caps->set_vfo(rig, curr_vfo)`で書き込み前の状態へ戻す）を持つ。一方、Directモードの
コントローラーコード（`_set_vfo_frequencies_locked()`のクロスバンドsatmode分岐）は、この
split対応APIを使わず、素の`set_freq(vfo_tx, ul_hz)`を直接呼んでいる。これは汎用
`rig_set_freq()`を経由するが、その非targetable経路には復帰処理が一切ない（本セクション前半
で確認済み）。つまり:
- NETモード: UL書き込み → `rig_set_split_freq()` → Subへ切替→書き込み→**Mainへ復帰**
  （Hamlib自身が行う）
- Directモード: UL書き込み → `rig_set_freq()` → Subへ切替→書き込み→**復帰なし**（Subに
  残ったまま）

NETモードは書き込みのたびにMainへ戻る設計のAPIを使っているため、Lockの読み取りが割り込んでも
「現在VFOがSubのまま」という危険な状態にほぼ陥らない。これが、今回の不具合がDirectモード
限定で発生し、NETモードでは（同じIC-9100・同じsatmodeであっても）再現しなかった根本理由。

### 既知の制約

- **`_DIAL_FEEDBACK_SANITY_HZ`（1サイクルで許容する最大周波数変化）は`200_000.0`Hz**
  （2026-07-22、`50_000.0`から引き上げ）。FO-29実機テストで、広いトランスポンダー
  （帯域幅約100kHz）の端から端まで意図的にダイヤルを大きく回した際、正しい読み取りが
  「1サイクルでの変化が大きすぎる＝ありえない値」として棄却されてしまう事象が発生した
  （ユーザーからの報告「読み込みに失敗しているように見える」の正体。実際には読み取り自体は
  成功しており、閾値が保守的すぎただけだった）。既知のアマチュア衛星トランスポンダーで
  最も広い部類（約130kHz）でも余裕を持ってカバーできる値に引き上げた。この定数は
  `_lock_watch_cycle()`・NET mode・非satmode Directモード・satmode Directモードの
  4箇所すべてで共有されている（リグ種別ごとの個別値は現状持たない、ユーザー判断）。
  完全に撤廃する案も検討したが、「読み取り自体は成功したがゲームは異常値」という
  ケース（通信エラーによる`-1`は別途`live_dl < 0`で弾かれるため対象外）に対する
  最後の防波堤として、閾値を引き上げるに留めた。
- ソフト上の表示（DL/UL周波数ラベル）は、リグの実際の値に対して**常に1サイクル遅れる**。
  `_rig_send()`が読んで`self._dial_feedback_offset_hz`を更新するのはこのサイクルだが、
  それが表示に反映されるのは**次の**`_doppler_cycle()`呼び出し。この間に衛星のドップラー
  シフトが自然に数Hz変化するため、表示とリグの実際の値がわずかにズレて見えることがある
  （バグではなく設計上のラグ、実機確認済み・許容範囲と判断）
- **実際にダイヤルを回してから画面表示に反映されるまでは体感で数秒かかる**（2026-07-20、
  実機確認）。上記の1サイクル遅延に加え、表示自体を書き換える`_on_tick()`が
  `DopplerWorker`の周期（デフォルト1秒、Rig SettingsのCycle設定）とは**別の固定1秒
  QTimer**（`MainWindow.__init__`の`self._timer`）であるため、以下の最大3段階が
  積み重なる: ①`_rig_send()`が実際にf/iを読みに行くまで最大1周期 → ②読み取った
  オフセットを`dl_corr`/`ul_corr`に折り込むのは次の1周期 → ③その結果を
  `self._latest_doppler`から画面ラベルへ書き出すのは次の`_on_tick()`（最大1秒後）。
  周期をデフォルト1秒のままとした場合、最悪で約3秒・平均1.5〜2秒程度のラグになる
  （FTX-1Fの1コマンドあたり約150msのCAT応答時間が実質的な下限のため、Cycle設定を
  下げても短縮には限度がある。③の表示タイマー自体はコード側で固定・非公開設定）
- Connect前（`_lock_watch_cycle()`）はUL側も書き込まない非対称な挙動になっている

### 実運用で発覚した別バグ — `live_dl`読み取りが不定時間フリーズする問題と、その原因（Hamlib `rig_set_uplink`）・修正（2026-07-20）

上記の設計で実装・実機確認が完了した後、実運用で「最初はDLの変化を正しく読み取れていたが、
何度かLock ON/OFFを繰り返すうちに読み取らなくなることがある」という新しい不具合が報告された。
ユーザーに確認したところ、読み取りが固まっている最中（8秒・30秒・10秒以上など、毎回バラバラの
長さ）も実際にDLのダイヤルを回し続けていたことが確認され、タイムアウトベースの現象ではないと
判断した。

**原因**: Hamlib本家`src/rig.c`の`rig_get_freq()`には、その名も`rig_set_uplink(rig, val)`
という**GPredict向けに実装された既存機能**がある（doc comment: "For GPredict to avoid reading
frequency on uplink VFO"）。`val=2`（Mainを無視）がセットされていると、`VFO_MAIN`に解決される
すべての`get_freq()`呼び出しは実機に一切問い合わせず、キャッシュの値をそのまま返し続ける。
これは時間経過で失効するキャッシュではなく、**明示的にリセットされるまで無期限に固定される**
（`rig.c`内、`rs->uplink == 2 && vfo == RIG_VFO_MAIN`の分岐、キャッシュ参照後即座に
`RETURNFUNC(RIG_OK)`で返し実機問い合わせを完全にスキップする）。8秒・30秒・10秒以上とバラバラの
長さで固まっていた症状と正確に一致する。

このフラグは以下の2経路でのみセットされる:
1. rigctld起動時の`-x`/`--uplink=N`オプション（本プロジェクトの`rigctld-ftx1.service`
   systemdユニットのExecStartには存在しないことを確認済み。この経路ではない）
2. rigctl/rigctldの拡張プロトコルコマンド`\uplink <val>`を、**同じrigctld TCPポートに
   接続した何らかのクライアントが送信した場合**

rigctldは全クライアントで単一の静的`RIG *my_rig`オブジェクトを共有する実装（`tests/rigctld.c`）
のため、過去に一度でも別のクライアント（GPredict自体を含む。まさにこのAPIの存在理由）が
同じrigctldポート（本プロジェクトの環境では`-T 0.0.0.0`でLAN全体に公開されている）に接続して
`\uplink`を送信していれば、それ以降ずっとFBSAT59側の読み取りも巻き込まれて固定されたままになる。
ユーザー確認により、過去にGPredict含む複数のソフトからこのrigctldへのアクセス実績があることが
判明し、原因として整合した。

**修正**（`HamlibNetController._init_vfo()`、[controller.py](src/rig/controller.py)）:
`ctcss_method in ("ftx1", "ft991")`の場合のみ、既存の`S 1 Main`/`S 1 VFOB`送信の直後に
`\uplink 0`を無条件送信し、誰が・なぜフラグを立てていたかに関わらず接続の都度リセットする。
`\uplink`はset系コマンドなので`RPRT 0`で応答し、既存の`_cmd()`/`_cmd_raw()`の仕組み
（`command[:1].islower()`によるquery/set判定。`\`は非アルファベットなのでset扱いになり
正しく動作する）にそのまま乗る。get_freq経由の読み取りは現状Lockのdial feedback機能
（`get_frequency()`/`get_split_frequency()`、呼び出し元は`main_window.py`の`_rig_send()`
のみ）でしか使っていないため、リセットもその対象条件（ftx1/ft991）だけに絞った。

**教訓**: 「ログ上は毎サイクル正しく計算しているのに、入力値（`live_dl`）だけが一定時間
固定される」という症状を見たら、自分のアプリのタイミング設計（同一サイクル内で読み書きしている
か等）を疑う前に、まず**下位レイヤー（今回はHamlib自体）が、この用途向けに元々何らかの
無効化・キャッシュ機構を持っていないか**を疑うこと。特に「GPredict向け」等、同種のアプリケーション
のために既存の主要ライブラリが用意している特別なオプトイン/アウト機構は、複数のクライアントが
同じデーモン（rigctld）を共有する構成では、自分が使っていなくても他のクライアントの操作で
静かに有効化されうる。

### Ctrl+L ホットキー（2026-07-22 実装、GitHub Issue #14）

Issue #14の報告者（ei4gnb）から、IC-9700実機でLock機能自体は期待通り動作したという確認と
合わせて、「実際の運用ではユーザーの手はVFOダイヤルにあり、マウスでLockボタンを押しに
行くのは実用上不便。ホットキー、あるいは将来的にはミニキーボード/MIDI/HIDデバイスへの
マッピング機能が欲しい」という要望があった。今回はミニキーボード/MIDI/HID対応という
大きな機能拡張ではなく、まず固定のキーボードショートカットのみを実装した。

**キー割り当て**: `Ctrl+L`（ユーザー確認済み）。単独の`L`キーは却下した——コールサイン
入力欄等のテキストフィールドにフォーカスがある間は文字として入力されてしまい、ホットキー
として機能しないため。`Ctrl+L`ならテキスト入力と衝突しない。

**有効範囲**: アプリ全体（ユーザー確認済み）。`QShortcut(QKeySequence("Ctrl+L"),
self)`をMainWindow自身に対して生成し、デフォルトの`Qt.WindowShortcut`コンテキストのまま
（Radio Controlタブが表示されていなくても、メインウィンドウ内のどこかにフォーカスが
あれば発火する）。

**実装**:
- `RadioControlWidget.toggle_lock()`（`src/ui/radio_control_widget.py`）: `_lock_btn`の
  チェック状態を反転させるだけの公開メソッド。`setChecked()`は実際にクリックした場合と
  同じく`toggled`（→`lock_changed`）シグナルを発火するため、既存の`_on_lock_changed()`
  以降の処理は一切変更不要
- `MainWindow.__init__()`（`self._radio_control = RadioControlWidget()`の直後）:
  `self._lock_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)`を生成し
  `.activated.connect(self._radio_control.toggle_lock)`
- Lockボタンのツールチップも`"Lock (Ctrl+L): ..."`に更新し、日本語訳
  （`Lock（Ctrl+L）: ...`）も追従（`.po`/`.mo`再コンパイル済み）

テスト: `tests/test_main_window.py`の`TestTuneLockButtons`に2件追加
（`test_toggle_lock_flips_checked_state_and_emits`: `RadioControlWidget`単体で
`toggle_lock()`が状態反転と`lock_changed`発火の両方を行うことを検証／
`test_ctrl_l_shortcut_toggles_lock`: `MainWindow`が`Ctrl+L`を実際に
`toggle_lock()`へ配線していることを、`_lock_shortcut.activated.emit()`経由で検証。
実キー入力のシミュレートは行わず、シグナル自体をemitして配線のみを確認する方式——
オフスクリーン環境でのキーイベント配送の不確実性を避けるため）。

**未実装（要望の残り）**: ミニキーボード/MIDI/HIDデバイスへの汎用マッピング機能は
今回のスコープ外。将来必要になった場合は別途検討する。

### SDR専用のLock機能（2026-07-22 実装、GitHub Issue #12 派生）

上記のCAT機（Rig 1）向けLock機能とは**完全に独立した**、SDRがRig 1/Rig 2いずれかに
割り当てられている場合専用のLock機能。SDR Controlタブの「Tune」ボタンの右隣に専用の
「L」ボタンを新設した（Radio Controlタブの既存「L」ボタン・`_trsp_lock`とは別の状態
`MainWindow._sdr_lock`で管理。CATのLockが必要とする「相手リグのTXへのミラー」等の
役割は今回のSDR Lockと無関係のため、あえて既存の`_trsp_lock`を流用せず独立させた）。

**背景**: Issue #12（Remote SDR対応）の解決後、報告者から「Passband TuneでSDRの周波数を
変えても、選択中のトランスポンダー（ISSのFM V/Uリピーター等）の周波数にすぐ戻ってしまい、
手動で別の周波数（440MHz帯のビーコン等）の信号強度を確認できない」という報告があった。
調査の結果、これは「FMかリニアトランスポンダーか」の違いではなく、**トランスポンダーが
選択されている限り、モードを問わずドップラー補正サイクルが毎秒SDRの中心周波数を上書き
し続ける**という、Passband Tune機能全体（矢印ボタン・Freq+Tuneボックス共通）の設計上の
制約だと判明した。

**設計**: CATリグのdial-feedback Lockと発想は同じだが、SDRには「物理ダイヤルを読み取る際の
曖昧さ」がそもそも存在しない（ソフトウェアが直接周波数を制御しているため、CATリグのように
「read-backしたタイミングが自分の書き込みと重なって誤検出する」レースが起こり得ない）ため、
CAT版よりも単純に実装できた。

- **SDR Lock ON**: `_doppler_cycle()`が、SDRが割り当てられているRigスロットへの周波数書き込み
  （`rig.set_vfo_frequencies()`）を完全に停止する。代わりに毎サイクル、SDRの**実際の現在周波数**
  （`rig.get_frequency()`。ハードウェアラウンドトリップではなく単純な属性読み取りなので、CATの
  ような読み取りタイミングの曖昧さがない）を読み、「生のドップラー補正値（`dl_corr`、
  `_sdr_tune_offset`加算前）との差分」を毎回`_sdr_tune_offset`として再計算する。これにより、
  Lock中にPassband Tuneの矢印ボタンやFreq+Tuneボックスでどこに動かしても、その動きが
  自動的に`_sdr_tune_offset`に反映され続ける
- **SDR Lock OFF**: 特別な遷移処理は一切不要。既存の通常書き込み式
  `dl_rig1 = dl_corr + _sdr_tune_offset`が、Lock中に更新され続けていた`_sdr_tune_offset`を
  そのまま使うため、Lockを解除した瞬間から、その周波数を起点にドップラー補正が自然に再開する
- オフセット値はUIにも反映する（`SdrControlWidget.set_tune_offset_display()`。矢印ボタン以外の
  経路——Lock中の自動再計算——でオフセットが変わったことをkHz表示ラベルに正しく反映するため、
  既存の`tune_offset_changed`シグナル経路とは逆方向の、MainWindow→ウィジェットの新設シグナル
  `_sdr_lock_offset_computed`（`_rig_send()`/`_rig2_send()`のバックグラウンドスレッドから
  emitされるため、Qtウィジェット操作を伴う実処理は必ずメインスレッド側のスロットで行う。
  `_doppler_computed`と同型のクロススレッドパターン）経由で伝える

**スコープ**: Rig 1・Rig 2どちらがSDRでも対応（`sdr_is_rig1`/`sdr_is_rig2`をそれぞれ独立に
判定するため、CAT側の「Rig 2は未対応」という制約は今回のSDR Lockには当てはまらない）。

#### Passband Tune「Freq:」欄・「T」ボタンの再設計（2026-07-22 実装）

上記SDR Lock実装後、ユーザー自身から「Passband Tune機能の設計自体がそもそもおかしい。
だからIssue #12でコメントされたのだ」という指摘があった。経緯を確認したところ、Freq:欄
（絶対周波数を手入力できる枠）は2026-07-11に**あとから追加**されたもので、当初トランス
ミッタの周波数しか表示・操作できなかったPassband Tune機能に「任意の周波数を手動入力
したい」という別のユースケース（衛星非選択時に地上局の基準信号を受信する等）を後付けした
結果、「矢印ボタンによるオフセット方式（Lock状態に関わらず`dl_rig1 = dl_corr +
_sdr_tune_offset`の式で常に維持される、Doppler-cycle経由の正しい経路）」と
「Freq:欄による絶対周波数の直接書き込み方式（`device.set_center_freq()`を直接呼ぶだけの
別経路で、次のDoppler-cycleサイクルの書き込みに即座に上書きされる）」という**2つの
非互換な仕組みが同居**していたことが混乱の真因だったと判明した。

**再設計方針（ユーザー確定、Radio ControlのT/Lボタンと機能を完全に一致させる）**:
- **Resetボタンは廃止**。「トランスポンダー中心へ戻す」役割は「T」ボタンに統合
- **「T」ボタン**: トランスポンダーのドップラー補正済み中心周波数へ戻す
  （`reset_tune_offset()`、内部的には従来のReset同等）。**トランスポンダー未選択時は
  押せない**（`SdrControlWidget.set_transponder_active()`で有効/無効を制御。戻る先の
  「中心」自体が存在しないため）
- **「Freq:」欄を1つに統一**: 別枠だった「+0.000 kHz」オフセット表示ラベルを廃止し、
  常にSDRの実際の周波数を表示する「Freq:」欄1つに一本化した（この欄は元々
  `center_freq_changed`経由でライブ同期済みだったため、追加の表示配線は不要だった）。
  矢印ボタン・手入力とも、**トランスポンダー選択中はオフセット方式**（`_apply_tune()`が
  従来通り`_sdr_tune_offset`を加算し`tune_offset_changed`をemit。手入力は新設の
  `manual_freq_requested(freq_hz)`シグナルでMainWindowへ絶対周波数を渡し、
  `MainWindow._on_sdr_manual_freq_requested()`が`self._latest_doppler.dl_corr`
  （直近のドップラー補正済み中心。トランスポンダー変更のたびに`None`へリセットされるため
  古い選択の値が紛れ込むことはない）との差分を`_sdr_tune_offset`として設定する——矢印と
  全く同じ経路に合流させることで、Lock状態に関わらず確実に維持されるようにした）、
  **トランスポンダー未選択中はSDRへ直接書き込み**（`_doppler_cycle()`自体が
  トランスポンダー未選択時は即座にreturnしオフセットを一切消費しないため、オフセット方式
  では何も起きない。矢印・手入力とも`device.set_center_freq()`を直接呼ぶ従来の即時方式を
  維持し、2026-07-11に追加された「任意周波数を手動で聴く」というユースケースをそのまま
  保持する）
- **「L」ボタン（SDR Lock）は変更なし**——「ドップラー補正の書き込みを止めるボタン」という
  ユーザー自身の説明どおり、既存のPhase実装（前項参照）が既にこの意味と一致していたため

**内部API変更**: `SdrControlWidget.set_tune_offset_display()`は`sync_tune_offset()`へ
改名（もはやkHzラベルを更新する役目がなく、`_tune_offset_hz`の内部同期のみを行うため）。
`MainWindow._on_sdr_lock_offset_computed()`もこの新名称を呼ぶよう追従。

---

## 永続的な per-transponder RX オフセット（GitHub Issue #18、2026-08-10 実装）

### 背景

Lock（dial feedback）はセッション内のみの手動補正で、衛星切り替え・Tune・アプリ再起動で
毎回リセットされる。ユーザーから「衛星のTCXO経年劣化等による恒久的な周波数ズレを、
パスのたびに手動で合わせ直さずに済むよう、トランスポンダーごとに保存できるオフセットが
欲しい」との要望（Issue #18）があった。TCXOドリフト起因のズレは衛星ごとにほぼ一定で
パスをまたいでも変わらないことが多いため、永続化が有効という判断（ユーザーとの事前検討で
確定）。温度依存のパス内リアルタイムドリフトまでは吸収できず、その分は引き続きLockで
運用者が追従する必要がある。

### 設計（ユーザー確定の3点）

1. **保存単位はトランスポンダー（`transmitters`行）ごと**（衛星＝NORAD IDごとではない）
2. **Tune（T）ボタン押下時も保持する**（リセットしない）— T一発でオフセット込みの正しい
   中心周波数へ戻れることが、Issue #18の目的（パス開始時の再チューニング省略）に直結するため
3. **DLのみ（v1スコープ）**。ULは引き続きLock等で運用者が都度合わせる

### UI（`src/ui/radio_control_widget.py`）

`sat_group`内の`name_norad_row`（NORAD値表示の右横の空きスペース）に`QSpinBox`
（`_offset_spin`、範囲±5000Hz・ステップ10Hz・サフィックス" Hz"）を配置。カスタムの▲▼
ボタンではなくQt標準の`QSpinBox`を使うことで、「手動で直接タイプ入力」「内蔵の上下矢印で
インクリメント」の両方を追加コードなしで満たす（Qtの既定レンダリングは値欄が左・矢印が
右端の縦積みで、まさに要望通りの見た目になる）。

- 選択中トランスポンダーが無い間は無効化・0表示（`_sync_offset_display(None)`）
- トランスポンダー切り替え（`_on_xpdr_changed`）・初期選択（`set_transmitters`）のたびに
  該当行の`rx_offset_hz`（無ければ0）を`blockSignals`で表示のみ同期（ユーザー編集と
  区別するため、プログラム的な表示更新では`rx_offset_changed`シグナルを発火しない）
- ユーザーが値を変更すると`rx_offset_changed: Signal(float)`を発火

### データ（`src/data/database.py` / `src/data/transmitter_manager.py`）

- `transmitters.rx_offset_hz REAL DEFAULT 0`（`manual_override`/`favorite_group`と同じ
  `ALTER TABLE`マイグレーションパターン。CHECK制約変更に伴う旧テーブル再作成パス
  （`needs_tx_migration`）にも列を追加済み、対象になった場合でも値が失われない）
- SATNOGS/communityの同期はすべて列を明示指定した`UPDATE`文のため、この列は再同期で
  上書きされない（追加の保護ロジック不要）
- `TransmitterManager.update_transmitter()`の`allowed`集合に`"rx_offset_hz"`を追加した
  だけで、既存の汎用アップデートAPIをそのまま再利用（専用セッター不要）

### 適用ロジック（`src/ui/main_window.py`）

- `_doppler_cycle()`: `dl_nom`を読んだ直後に`dl_nom += rx_offset_hz`として、以降の
  `DopplerCalculator.correct_downlink()`・Tune上書き消費・表示・リグ送信すべてに単一の
  注入点で反映させる。Lockの`_dial_feedback_offset_hz`（ドップラー計算**後**に加算し
  `dl_shift`をNoneにする設計）とは異なり、こちらは計算**前**の"公称値"を動かすため、
  `dl_shift`は「オフセット込みの正しい基準からの純粋なドップラーシフト」として意味を
  保ったまま表示され続ける（Noneにしない）
- `_on_tune_requested()`: `_tune_dl_override`の算出（バンド中心 or `downlink_low`）に
  `rx_offset_hz`を加算。`_dial_feedback_offset_hz`は従来通り0にリセットするが、
  `rx_offset_hz`はリセットしない
- `_on_rx_offset_changed(value)`: `self._current_transmitter["rx_offset_hz"]`を直接書き換え
  （次のDopplerWorkerサイクルが即座に新しい値を使えるようにするため、DB再読込を待たない）
  つつ、`TransmitterManager.update_transmitter(uuid, rx_offset_hz=value)`でDBへ永続化
- **Moon/EME（MOON_ID）は対象外**: `_doppler_cycle()`自体がMOON_IDを除外しており、
  `_update_moon()`はこのフィールドを一切参照しない。EMEトランスミッタは
  `eme_frequencies.json`由来でDB行を持たないため、Offsetスピンボックス自体は
  （ウィジェットがMOON_IDを意識しない設計のため）表示・編集はできてしまうが、
  `_on_rx_offset_changed()`の`UPDATE ... WHERE uuid=?`は該当行が無く無害な no-op に
  なるだけで、Moonのドップラー計算には一切影響しない（Lock機能が既にMoonに対して同様に
  機能的に無力なのと同じ、意図的な未対応）

テスト: `tests/test_database.py`（`rx_offset_hz`のデフォルト値・永続化）・
`tests/test_transmitter_manager.py`（`update_transmitter()`経由の永続化・community/SATNOGS
再同期後も値が生き残ること）・`tests/test_main_window.py`
（`TestTuneLockButtons`にOffsetスピンボックスのUI同期・シグナル発火テスト、
`TestLockDialFeedback`に`_doppler_cycle()`でのオフセット折り込み・Lock offsetとの合成・
`_on_tune_requested()`でのオフセット保持・`_on_rx_offset_changed()`の永続化テストを追加）。

### バグ修正 — 衛星を切り替えて戻るとOffsetが消える（v0.3.7実運用で発覚・修正済み、2026-08-12）

v0.3.7リリース後、Issue #18報告者（drmgcm69）から「RS-44でOffsetを設定した後、JO-97に
切り替えてからRS-44に戻ると、スピンボックスに値が表示されない（消えた）」との報告があった。

**原因**: 衛星選択時に実際に呼ばれるのは`TransmitterManager.get_transmitters()`
（`SELECT *`のため新規列も自動的に含まれる）ではなく、`MainWindow._refresh_radio_control()`
内の**別の生SQL**だった。こちらは列を明示指定したSELECT文で、`rx_offset_hz`追加時に
この存在を見落としており、列リストに含まれていなかった：

```sql
SELECT uuid, description, type,
       downlink_low, uplink_low, mode, ctcss_tone, invert,
       alive, satnogs_status, norad_cat_id, source   -- rx_offset_hz が抜けていた
FROM transmitters WHERE norad_cat_id = ? ORDER BY ...
```

`_on_rx_offset_changed()`によるDB永続化自体は正しく機能していた（DB上の値は消えていない）。
しかし衛星を切り替えて戻ると、この生SQLで`_current_transmitter`が**丸ごと新しい辞書に
再構築される**ため、`rx_offset_hz`キー自体が存在しない辞書になり、スピンボックスの表示が
0（未設定）に戻っていた——**表示上だけの不具合**で、保存した値自体は毎回のパスで
正しく`_doppler_cycle()`に適用され続けていた（実際の周波数補正は途切れていなかった）。

**修正**: 上記SELECT文の列リストに`rx_offset_hz`を追加。

**教訓**: `transmitters`テーブルへの新規列追加時、`TransmitterManager.get_transmitters()`
（`SELECT *`）だけでなく、**同じ役割を果たす別経路の生SQL**（列を明示指定するSELECT文）が
複数存在しないか確認すべきだった。本ファイル既出のHamlibアップデーターの教訓
（「同じ役割の機能が複数ある場合、新規実装時だけでなく既存分についても参照先が揃っているか
確認すること」）と同型の見落とし。`grep -n "FROM transmitters"`で関連箇所を横断的に洗い出す
習慣が必要。

テスト: `tests/test_main_window.py`の`TestLockDialFeedback`に
`test_refresh_radio_control_surfaces_saved_rx_offset`を追加（`_refresh_radio_control()`を
直接呼び、DBに保存済みの`rx_offset_hz`が`_current_transmitter`とOffsetスピンボックスの
両方に正しく反映されることを検証）。

---

## 帯域を持つトランスポンダーのDoppler追尾基準を帯域下限→帯域中心に修正（GitHub Issue #20、2026-08-13）

### 発端

「衛星周波数の編集フォームがLow/High形式で、centerを知っている場合どう入力すればいいか
分からない」という報告（Issue #20、報告者はFO-29 V/Uトランスポンダーを例に
"center up 145.950 / center dwn 435.850"と表現）。当初はAdd/Edit Transmitterダイアログの
UX（説明不足）の話だと考えたが、実際にSATNOGS APIでFO-29のデータを確認したところ
（`uplink_low=145.900/uplink_high=146.000`・`downlink_low=435.800/downlink_high=435.900`、
平均が報告者の言うcenterと完全一致）、報告者は手動入力を一切行っておらず、**リグへ継続的に
送られる周波数（Doppler追尾の基準）自体が帯域下限（`downlink_low`）になっており、
帯域中心ではなかった**ことが判明した。開発者自身もこの挙動を把握しておらず、
「知らなかった、直すべき」との判断で修正した。

### 修正内容

- `_band_center_or_low(low, high)`（新設、`src/ui/main_window.py`モジュールレベル）:
  `high`が記録されていれば`(low+high)/2`（帯域中心）、無ければ`low`のみ（単一周波数の
  FM/FT4衛星は元々`downlink_high=None`のためこちらにフォールバックし挙動は変わらない）
- `_doppler_cycle()`（DopplerWorker駆動、リグへの継続書き込み・表示の実体）の
  `dl_nom`/`ul_nom`を、`downlink_low`/`uplink_low`直読みから
  `_band_center_or_low()`経由に変更
- `_lock_watch_cycle()`（Connect前のLockポーラー）の`dl_nom`も同様に変更。この関数を
  一貫させないと、Connect前後でLock機能の基準周波数が食い違う新たな不整合を生むと
  判明したため、承認済みスコープを`_doppler_cycle()`単体からこちらにも広げた
  （詳細は下記「影響範囲の見積もり違い」参照）
- Tuneボタン（`_on_tune_requested()`）は元々`(low+high)/2`を計算していたため無変更。
  一発限りの上書きに頼らず、常時この基準で追尾するようになった

### 意図的に対象外にしたもの

- `_update_rig_web_state()`（スマホWeb UIのWebSocket状態）・`_update_moon()`
  （Moon/EME、DL==ULのシンプレックスのため`downlink_high`は実質常にNone）も
  `downlink_low`直読みのまま。スマホWeb UIの表示がデスクトップと乖離する可能性は
  残るが、実際にリグを駆動する経路ではないため今回は見送り

### 影響範囲の見積もり違い — Lock（Lボタン）機能のテストへの波及

`_doppler_cycle()`のみを対象とした狭い承認で実装を始めたところ、`dl_nom`/`ul_nom`が
Lock（Lボタン）のdial-feedbackオフセット計算の基準値としても共用されていることが判明し、
`TestLockDialFeedback`（`tests/test_main_window.py`）の帯域ありトランスポンダー
（`_TRANSMITTER`フィクスチャ、DL 145.800-145.950MHz・UL 435.000-435.150MHz）を使う
接続後テスト群、約20件が軒並み「基準＝帯域下限」を前提にハードコードされた期待値を
持っており、そのままでは全滅することが判明した。ユーザーに報告の上、`_lock_watch_cycle()`
も含めて一貫させ、影響を受けた全テストの期待値を新しい帯域中心基準
（DL center=145,875,000 / UL center=435,075,000）に合わせて更新する方針で承認を得た。

`_lock_watch_cycle()`のクロスチェック・implausible-jump系テストは、DL読み取り値が
UL読み取り値と一致するかだけを見る、または閾値（`_DIAL_FEEDBACK_SANITY_HZ`=200kHz）を
超えるかだけを見るため、基準そのものには依存せず無変更のものもあった
（`test_lock_watch_discards_reading_close_to_ul_crosscheck`等）。一方
「+80Hz手動リチューン」を模したモック値（旧: `downlink_low + 80`）は、新基準では
「帯域中心 + 80」に、implausible-jumpのモック値も新基準からの相対値に、それぞれ
書き換えが必要だった。単一周波数（FM/FT4、`downlink_high=None`）を使うテスト
（`_on_rx_offset_changed()`系等）は`_band_center_or_low()`が`low`にフォールバックする
ため無変更のまま。

**教訓**: 「継続Doppler追尾の基準を直す」という一見単純な変更でも、同じ変数
（`dl_nom`）が別機能（Lockのdial-feedback基準）にも共用されていることがある。
承認前にテストフィクスチャの実データ（`downlink_high`が設定されているか）まで
確認していれば、この影響範囲はもっと早い段階で見積もれた。

### 追加修正（同日）— Connect前の周波数プリセットも帯域中心に統一

上記の初回修正では意図的にスコープ外としていた「トランスポンダー選択時、Connect前でも
リグへ周波数を書き込むプリセット」経路（`_apply_transponder_state_to_rig()`）も、
ユーザーからの追加指示（「スマホとEMEは今回対応不要だが、Connect前プリセットは直す
必要がある」）を受けて同じ`_band_center_or_low()`で統一した。スマホWeb UI
（`_update_rig_web_state()`）・Moon/EME（`_update_moon()`）は指示通り引き続き対象外。

`_apply_transponder_state_to_rig()`内で`downlink_low`/`uplink_low`を直接読んでいた
4箇所（NET modeの`set_transponder_freqs()`呼び出し・satmode Directの`_transponder_dl_hz`/
`_transponder_ul_hz`アンカー・FTX-1F/FT-991 raw CATの`_send_freq_preset_direct()`・
generic Direct（IC-705等）の同関数呼び出し）を、関数冒頭で一度だけ計算した共通の
`dl_hz`/`ul_hz`（`_band_center_or_low()`経由）を参照する形に統一（`dl_mode`/`ul_mode`/
`ctcss_hz`が既にこのパターンだったため、それに揃えた形）。NET modeの
`_send_freq_preset_independent()`自体は`set_transponder_freqs()`が内部に保存した値を
そのまま読むだけの設計のため、呼び出し元の`dl_hz`/`ul_hz`を直せば自動的に正しくなる
（controller.py側は無変更）。

このパスに対する既存テストは元々ゼロ件だったため（`grep`で確認済み）、既存テスト破壊の
リスクは無かった。回帰防止のため`TestLockDialFeedback`に3件追加:
NET mode・Direct mode（FTX-1F raw CAT）それぞれで帯域中心が渡ることを確認するテストと、
単一周波数（`downlink_high=None`）のFM/FT4系は従来通りLowのみが使われることを確認する
テスト。いずれも`_do_nonsatmode()`/`_do_direct_cat()`（`threading.Thread`で起動される
バックグラウンドクロージャ）が`try/except`を持たない、または持っていてもモック漏れの
メソッド呼び出しで例外を投げうるため、`TestLockDialFeedback._SyncThread`で同期実行しつつ
経路上の全メソッドをモックして決定的に検証した。

---

## AO-73の反転トランスポンダーがUSB/USBになる不具合と「SatNOGS公式値へリセット」機能（GitHub Issue #20 続報、2026-08-14）

### 発端

v0.3.15リリース後、報告者（drmgcm69、IC-9700 satmode Direct）から「AO-73だけリニア衛星の中で
唯一、アップリンク・ダウンリンク両方がUSBになる」との報告があった。報告者に依頼して取得した
`fbsat59.log`（Help画面ではなく`scripts/collect_windows_rig_log.bat`で採取）を解析したところ、
決定的な証拠が見つかった:

```
apply_transponder_state (satmode Hamlib) dl=SSB ul=SSB ctcss=0.0
DIAG readback (pyserial) SUB/UL mode_byte=0x01 ... (ul_mode=SSB; 0x00=LSB 0x01=USB)
```

`mode`の値が**文字通り"SSB"という文字列**になっていた。周波数（DL 145.965/UL 435.150MHz）が
SatNOGS公式データ（DL 145.950-145.970・UL 435.130-435.150、`invert=true`、`mode=USB`）とも
コミュニティFT4エントリ（145.952/435.148）とも一致しないことから、報告者がAdd/Edit
Transmitterダイアログで独自にこのAO-73行を編集し、周波数を好みの値に変更した際、
Modeプルダウン（`transmitter_dialog.py`の`_MODES = ["FM", "SSB", "CW", "CW-R",
"DIGITALVOICE", "BPSK", "AFSK", "Other"]`）から一見自然に見える"SSB"を選んでしまったものと
判明した。

### 根本原因

`_build_live_hamlib_mode_map()`（[controller.py:107-126](src/rig/controller.py:107)）は
`"SSB": _H.RIG_MODE_USB`という変換を持っており、ダウンリンク側は"SSB"でも問題なくUSBとして
送信される。しかし`_MODE_INVERT`（[main_window.py:119](src/ui/main_window.py:119)）には
**"SSB"というキー自体が存在しなかった**ため、`ul_mode = _MODE_INVERT.get(mode, mode) if
invert else mode`が`invert=True`でも`"SSB"`のままフォールバックし、アップリンクが
一切反転されずDL/UL両方がUSBになっていた。

### 修正1: `_MODE_INVERT`に"SSB"→"LSB"を追加

`_MODE_INVERT`辞書に`"SSB": "LSB"`を追加（[main_window.py:119-133](src/ui/main_window.py:119)）。
"SSB"を選んだ既存行・将来の行のどちらでも、`invert=True`なら正しくLSBへ変換されるようになる。
Modeプルダウンから"SSB"自体を削除する案も検討したが、今回はこの最小修正のみを実装（プルダウンの
選択肢を変える判断は別途要検討として保留）。

回帰テスト: `tests/test_main_window.py`の`TestModeInvertDataModes`に
`test_ssb_inverts_to_lsb`を追加。

### 修正2: 「Reset to SatNOGS Official Value」ボタン

上記の根本原因調査で、「SatNOGS由来の行を手動編集して`manual_override=1`が立つと、以後の
SatNOGS同期で永久に上書きされない」という既存の保護機構（`sync_from_satnogs()`、
[transmitter_manager.py:598-608](src/data/transmitter_manager.py:598)、`uuid`一致かつ
`manual_override`または`source in ('manual','community')`ならスキップ）が、今回のような
「手動編集で壊れた行を元に戻す手段が無い」という副作用を生んでいることが判明した。ユーザーの
提案により、Add/Edit Transmitterダイアログに手動で元に戻せるボタンを追加した。

**設計判断の根拠**（実装前に調査確認済み）:
- `update_transmitter()`の`allowed`列集合（[transmitter_manager.py:329-345](src/data/transmitter_manager.py:329)）
  には`uuid`・`source`が含まれておらず、**編集では絶対に書き換わらない**。つまり
  `source='satnogs'`は手動編集で`manual_override=1`が立った後も、「本物のSatNOGS UUIDへの
  参照が生きている」ことを示す永続的で信頼できる目印として使える
- SatNOGS APIは`?satellite__norad_cat_id=`と同じ要領で`?uuid={uuid}&format=json`という
  単一UUID指定のフィルタも受け付ける（クエリパラメータ、`/api/transmitters/{uuid}/`という
  パス形式ではない）。実際に`curl`で動作確認済み（結果はリスト形式、`satellite__norad_cat_id`
  一括取得と同型）

**実装**:
- `TransmitterManager.fetch_satnogs_transmitter(uuid)`（新設、
  [transmitter_manager.py](src/data/transmitter_manager.py)、`sync_from_satnogs()`直後に配置）
  — 単一UUIDでSatNOGSへ問い合わせ、`sync_from_satnogs()`と全く同じフィールドマッピング
  （CTCSSのdescription正規表現抽出フォールバック含む）で1件分の辞書を返す。SatNOGS側に
  UUIDが見つからない場合は`None`（呼び出し元は「見つかりませんでした」と表示）、
  接続エラー等は握りつぶさず**そのまま re-raise**（呼び出し元が既存の「SatNOGSに接続できません」
  表示パターンを使えるように）
- `TransmitterDialog`（[transmitter_dialog.py](src/ui/transmitter_dialog.py)）:
  - `_prefill()`から、SatNOGSが実際にデータを持つフィールド（description・周波数4種・
    type・mode・invert・CTCSS）の反映処理を`_apply_satnogs_fields()`として切り出し、
    `_prefill()`（NORAD ID・Notes・Overwrite protectionチェックボックスの反映は別途担当）と
    リセットボタンの両方から共有
  - ボタン自体は`self._edit_mode and self._existing.get("source") == "satnogs"`のときのみ
    `_build_ui()`内で生成・表示（新規追加行やmanual/community行では非表示）
  - `_on_reset_to_satnogs()`: `asyncio.run(self._tm.fetch_satnogs_transmitter(uuid))`で
    取得し、成功したら`_apply_satnogs_fields(rec)`でフォームへ反映**するのみ**（DBには一切
    書き込まない。ユーザーがOKを押すまで確定しない、というユーザー確定済みの設計）。
    あわせてOverwrite protectionチェックボックスを**オフ**にする（今後また自動同期で
    保護されるよう、手動保護を解除する意図）。NORAD IDとNotes（どちらもSatNOGSのデータでは
    ない）はリセットの対象外で、既存の値のまま維持される
  - ネットワーク接続不可時は既存の`_do_satnogs_import()`と同じ
    `except Exception as exc: QMessageBox.critical(self, _("Error"), str(exc))`パターンを
    踏襲（ユーザー確認済み、新しいパターンは導入しない）

**意図的にやらなかったこと**: UUIDが見つかった場合の自動DB書き込み（Save前確定方式のため）、
SatNOGSに一切データが無い`source='manual'`/`'community'`行へのボタン表示、Modeプルダウンから
"SSB"を削除すること（3点ともユーザーとの相談で対象外と確定、または保留）。

テスト:
- `tests/test_transmitter_manager.py`（Qt非依存、ローカル実行可）に`TestFetchSatnogsTransmitter`
  （4件）— `sync_from_satnogs()`と同じフィールドマッピングになること・description正規表現
  フォールバック・UUID未検出時`None`・接続エラーの re-raise を検証
- `tests/test_main_window.py`の`TestTransmitterDialog`に8件追加（ボタンの表示/非表示条件
  4パターン・リセット時のフォーム反映とDB書き込み無し・見つからない場合の警告・接続エラー
  時のエラー表示）。`test_main_window.py`はCLAUDE.mdのルール通りローカル実行せず
  `--collect-only`（237件収集成功）で確認、最終確認はCI待ち

### 追加修正（同日）— `_MODES`に"USB"/"LSB"が一度も存在しなかった不具合をCIが検出

上記のリセット機能をpushしたところ、CIが`test_reset_populates_fields_without_writing_db`で
`assert w._mode_combo.currentText() == "USB"`失敗（実際は`'SSB'`のまま）で赤くなった。
`gh run watch --exit-status; echo "EXIT: $?"`という監視コマンドの書き方自体に不具合があり
（`echo`が常に0を返すため`watch`本体の非ゼロ終了コードを握りつぶしてしまう）、実際には失敗して
いたのに一度見逃しかけた——バックグラウンド監視コマンドを組み立てる際は、パイプやセミコロンで
後続コマンドを繋いで終了コードを上書きしていないか要注意。

原因を追ったところ、`transmitter_dialog.py`の`_MODES`（Modeプルダウンの選択肢）が
`["FM", "SSB", "CW", "CW-R", "DIGITALVOICE", "BPSK", "AFSK", "Other"]`で、**"USB"・"LSB"が
最初から一度も選択肢に存在していなかった**ことが判明。ユーザー自身も並行して同じ問題に開発版で
気づいており、以下の方針で確定した:

- `_MODES`から`"SSB"`を削除し、`"USB"`・`"LSB"`・`"USB-D"`・`"LSB-D"`を追加
  （`["FM", "USB", "LSB", "USB-D", "LSB-D", "CW", "CW-R", "DIGITALVOICE", "BPSK", "AFSK",
  "Other"]`）。反転（Invert）チェックボックスON時のアップリンク変換は既存の`_MODE_INVERT`
  （USB⇔LSB・USB-D⇔LSB-D）がそのまま適用されるため、この4つを選択肢に加えるだけで
  正しく機能する
- `_MODE_INVERT`の`"SSB": "LSB"`エントリ自体は削除せず維持（今回の`_MODES`変更前に
  `mode="SSB"`のまま保存された既存行の後方互換用。新規行はもう"SSB"を選べないため
  発生し得ないが、既存の壊れた行が実行時に落ちないようにするための安全策）
- Modeの表示ラベルを`"Mode:"`から`"Mode (Downlink):"`に変更。この欄が設定するのは
  **ダウンリンク側のモードのみ**（アップリンクはInvertチェックボックスの状態に応じて
  `_MODE_INVERT`が自動導出する）ことを、ユーザーが編集画面を見ただけで分かるようにする
  ためのラベル変更（今回の一連の調査で「Radio Controlパネルの"Mode:"表示もDL側のみ」
  という設計を発見済みだったが、そちらは今回のスコープ外のまま変更していない）
- 新規追加した「Reset to SatNOGS Official Value」ボタン・関連メッセージの日本語訳を追加
  （`locale/ja/LC_MESSAGES/fbsat59.po`/`.mo`）。`msgmerge`後、"Mode (Downlink):"が
  無関係な訳（"ダウンリンク:"のみ）に、"Reset to SatNOGS"が全く無関係な既存訳
  （"Open in SatNOGS"の訳"SatNOGSで開く"）にfuzzy推測されていたため、内容を確認し
  正しい訳に修正した上でfuzzyを解消（本ファイル既出の「`msgmerge`後の`#, fuzzy`は
  機械が似ていると判断しただけの、内容を確認していない訳」という教訓通りの実例）

`_MODES`はこのダイアログ内でのみ使われる定数（他モジュールからの参照なし）であることを
`grep`で確認済み。既存テストで`_MODES`に依存する箇所（`test_edit_mode_prefills_fields`の
`mode="FM"`等）への影響もないことを確認した。

### 追加修正（同日）— Offsetスピンボックスの可変範囲を±5000Hz→±10000Hzに拡大

v0.3.16で修正版をDMさんに試してもらったところ、AO-73のUSB/LSB問題・Resetボタンとも実機で
正常動作を確認できたとの報告があった。ただしAO-73自体のTCXOドリフトが他衛星より大きく
（実測で約9500Hz程度、IC-9700のRITだけでは足りずフルパスを通して追いかけるのに苦労した
とのこと）、Issue #18由来のOffsetスピンボックスの可変範囲（`radio_control_widget.py`の
`_RX_OFFSET_RANGE_HZ = 5000`）ではAO-73の実測ドリフト量そのものに対して余裕が無いことが
判明した。ユーザー判断で余裕を持たせて`10000`（±10kHz）に変更（定数1箇所のみ、DB側の
`rx_offset_hz REAL`列に範囲制約は無いため他に影響なし）。

テスト: `tests/test_main_window.py`の`TestTuneLockButtons`に
`test_offset_spin_range_is_10000hz`を追加。

---

## Rig-Specific Implementation Notes

### FTX-1F (Hamlib model 1051)

#### NET モード
- rigctld backend forces Sub=TX, Main=RX regardless of S command argument (FTX-1F specific quirk; other rigs achieve the same result through standard split or satmode mechanisms)
- `S 1 Main` is required for split (not `S 1 Sub`) — rigctld standard protocol, universal across all rigs
- `F {hz}` → Main (RX/DL),  `I {hz}` → Sub (TX/UL) via rigctld — universal VFO assignment
- Mode setting: `V Sub → M {ul_mode} 0 → V Main → M {dl_mode} 0` via independent socket
- `V` (active VFO switch) command causes TX LED to light → forbidden in Doppler cycle
- CTCSS: Hamlib `L CTCSS_TONE` → `RPRT -11` (not supported by backend)
  Custom CAT via rigctld `w` command: `CN10{tone:03d};CT11;` / `CT10;`
  `CN P1=1:SUB, P2=0:CTCSS, P3=tone index 000-049`

#### Direct モード（`_FTX1_MODEL_IDS = frozenset({1051})`）
- ボーレート誤設定時に Hamlib がシリアル応答待ちでタイムアウトし（最大数十秒）、Python GIL を保持したまま UI がフリーズする問題を回避するため、モード・CTCSS 設定を Hamlib 経由で行わない（ボーレートが正しければ Hamlib でも動作するが、raw CAT の方が `set_vfo(VFOB)` を呼ばない分シンプル）
- トランスポンダー選択時に `_apply_mode_and_ctcss_cat_ftx1(dl_mode, ul_mode, ctcss_hz)` をバックグラウンドスレッドで呼び出す
- FTX-1F の `MD` コマンドは P1 で VFO を直接指定できる（P1=1=SUB, P1=0=MAIN）。SV スワップ不要
  ```
  MD1{ul_code};     — SUB (TX/UL) モード設定
  MD0{dl_code};     — MAIN (RX/DL) モード設定
  CN10{tone:03d};   — CTCSSトーン番号（P1=1:SUB, P2=0:CTCSS）
  CT11;             — CTCSS ENC ON（SUB=TX 側）
  CT10;             — CTCSS OFF
  ```
- 書き込みは `os.open(O_WRONLY|O_NOCTTY|O_NONBLOCK)` で行う（ポートが Hamlib に占有されていなければ動作）
- **注意**: `os.open()` は termios を設定しない。Hamlib が事前にポートを開いてボーレートを設定している場合のみ正しく動作する。ユーザーがボーレートを正しく設定していることが前提（Rig Settings のボーレートテストボタンで確認可能）
- **スプリット（TX VFO 制御）**: Hamlib `set_split_vfo()` は FTX-1F で `None` を返し VFO-B を TX にできない。raw CAT `FT` コマンドを使う（2026-06-29 実機確認）:
  ```
  FT1;  — VFO-B (Sub) を TX に設定（Connect 時・freq preset 時）
  FT0;  — VFO-A (Main) を TX に戻す（アプリ終了時）
  ```
  FT-991A の `FT2;`/`FT3;` は FTX-1F では無視される（非対応）。
  `_init_split()` と `_send_freq_preset_direct()` で `_FTX1_MODEL_IDS` を独立 `elif` ブランチで処理。
  アプリ終了時は `_release_rig_split_on_exit()`（main_window.py）が `FT0;` を pyserial で送信。

#### Connect直後にTXがSubからMainへ勝手に戻るバグと修正（2026-07-20 発見・修正）

**症状**: Connectボタンを押した直後（Lockボタンは無関係）、実機のTXがMain側に移ってしまう
（`FT1;`でSubをTXに設定したはずなのに）。ユーザーからの「FTX-1は最初に実装したリグで、
あとからFT-991やICOM（IC-705）を追加した際に、何かにつられて壊れたのでは」という指摘を受けて
git履歴を遡って特定した。

**原因**: `_set_vfo_frequencies_locked()`の非satmode「generic」分岐（IC-705とFTX-1Fが**共有**
している経路）に、UL書き込み直後、以下の1行があった:
```python
self._rig.set_vfo(rx_vfo)   # rx_vfo = "VFOA"
```
これはコミット`6885275`「fix(rig): restore VFO-A display after generic-rig UL write」
（**2026-07-06、IC-705対応の過程で追加**）で入ったもので、コミットメッセージにも
「Icom CI-V backends (confirmed on IC-705) leave their internal CURR vfo pointer on
VFO-B...」と明記されている通り、**IC-705専用に確認・意図された**表示復元コマンドだった。

FTX-1F（2026-06-29に最初に実装・実機確認済み）はsatmodeでもFT-991でもないため、この
generic分岐を**IC-705と意図せず共有**しており、この行がFTX-1Fにも巻き込まれて毎回
（UL更新のたび）実行されていた。`ftx1_vfo.c`（Hamlib FTX-1Fバックエンド）のコメントに
よれば`set_vfo(VFOA)`は生CAT`VS0;`（アクティブVFO選択）を送る。本来`VS`（アクティブVFO
選択）と`FT`（TX VFO指定）は独立したコマンドのはずだが、FBSAT59は正式な`ST`（Split）
コマンドを意図的に一度も送っていない（`ftx1_vfo.c`のコメントにも「Note: ST (Split)
command NOT used」とある）ため、リグ側が正式なsplit状態を認識しておらず、`VS0;`が
副作用としてTX側の指定まで巻き戻してしまう（Sub→Main）——`_init_split()`が接続時に
送った`FT1;`を、直後の最初のDoppler書き込みサイクルで即座に上書きしてしまう、という
挙動を実機で確認した。

**修正**: `_model_id not in _FTX1_MODEL_IDS`の場合のみ`self._rig.set_vfo(rx_vfo)`を実行する
よう変更（[controller.py](src/rig/controller.py)、`_set_vfo_frequencies_locked()`の
generic分岐）。FTX-1Fは自前の`FT1;`/`FT0;`機構で既にTX VFOを管理しており、この
IC-705専用の表示復元は不要かつ有害だったため、完全にスキップする。

**教訓（ユーザーの指摘通り）**: 複数機種が同じ「generic」コードパスを共有する設計では、
後から別機種（IC-705）のために追加した修正が、コミットメッセージで対象機種を明記していても、
**共有分岐に置かれている限り既存の他機種（FTX-1F）にも黙って適用されてしまう**。
「以前は動いていたのに、後から別の機能を追加したら壊れた」という報告を受けたら、
対象コードが複数機種で共有されているブランチにないか、`git log -S`で該当行の追加コミットを
遡り、そのコミットが本当に今問題の機種向けだったかを確認すること。

**再発防止としては検討していない**: 現状はFTX-1Fを`if`で個別除外する対症療法。今後さらに
機種が増えた場合、「このgeneric分岐がどの機種にどんな副作用を持つか」を機種ごとに
明示的に検証してから合流させる方が安全だが、現時点ではリグの種類がまだ少ないため
個別除外で対応している。

### FT-991 / FT-991A (Hamlib models 1035 / 1036)

Hamlib 4.7.2 の公式モデルリスト: **1035 = FT-991**（FT-991A も同バックエンドを使用）。
rig_dialog.py のカスタムリストでは 1036 = FT-991A として登録。`_FT991_DIRECT_MODEL_IDS = frozenset({1035, 1036})` で両方を対象にする。

#### NET モード（`ctcss_method == "ft991"` で識別）
- `MD` コマンドは P1=0 固定（Main VFO のみ対象）。VFO-B（UL）のモード設定には SV スワップが必要
- Hamlib `set_mode(RIG_VFO_B)` → `-11 Feature not available`
- CTCSS: Hamlib `L CTCSS_TONE` → `RPRT -11` (not supported by backend)
  Custom CAT: `CN00{tone:03d};CT02;` / `CT00;`
  `CN P1=0:fixed, P2=0:CTCSS, P3=tone index 000-049`
  `CT P2=2`: CTCSS ENC only; `CT00;` to disable
- rigctld `w CN…` works but requires FM mode to be active on the rig
- `SV`/`MD` commands via rigctld `w` each take ~2 s (wait for RPRT with 2 s timeout)
- `send_mode_only()` の FT-991 パス（`ctcss_method == "ft991"`）:
  ```
  MD0{dl_code};                — VFO-A (DL) モード設定
  SV; MD0{ul_code}; SV;       — VFO-B (UL) モード設定（SV スワップ）
  ```
- `send_ctcss_cat()`: `CN00{tone:03d};CT02;` を SV スワップなしで送信
  → `CT02`（CTCSS ENC）は TX-VFO（スプリット時は VFO-B）にグローバルに適用されるため SV 不要（FT-991AM で動作確認済み）
- `send_mode_only()` はバックグラウンドスレッドで実行（UI フリーズ防止）

#### Direct モード（`_FT991_DIRECT_MODEL_IDS = frozenset({1035, 1036})`）
- トランスポンダー選択時に `_apply_mode_and_ctcss_cat_ft991(dl_mode, ul_mode, ctcss_hz)` をバックグラウンドスレッドで呼び出す
- FTX-1F と異なり MD P1 固定のため VFO-B モード設定は SV スワップが必要:
  ```
  SV;               — VFO-B を Main に切り替え
  MD0{ul_code};     — UL モード設定（現 Main = 元 VFO-B）
  SV;               — 元に戻す
  MD0{dl_code};     — DL モード設定（Main = VFO-A）
  CN00{tone:03d};   — CTCSS トーン番号（SV スワップ不要: TX-グローバル）
  CT02;             — CTCSS ENC ON
  CT00;             — CTCSS OFF
  ```
- 書き込みは **pyserial** を使用（`os.open()` と異なり termios / ボーレートを正しく設定）
- `_port_lock` を取得して `connect()` との競合を防ぐ
- `_FT991_MODE_MAP`（`HamlibNetController` と共用）を使用してモードコードを引く
- main_window.py では `_FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS` をまとめて同一ブランチで処理

### IC-9700 / IC-9100 / IC-910H / IC-821H (Icom satmode rigs — `_SATMODE_RIG_IDS`)
- These rigs implement Hamlib **satmode**: firmware always routes Main=RX(DL) and Sub=TX(UL)
- Direct mode split init: Hamlib `set_func(RIG_FUNC_SATMODE, 1)` — `open → set_func → close → open` sequence
- Direct mode freq (cross-band): `set_freq(RIG_VFO_MAIN, dl_hz)` + `set_freq(RIG_VFO_TX, ul_hz)`
- Direct mode mode + CTCSS: Hamlib `_apply_mode_and_ctcss_hamlib()` (before connect) or `_satmode_exit()` (same-band at connect time)
- `HamlibDirectController._satmode` flag is set automatically when model_id ∈ `_SATMODE_RIG_IDS`

#### Windows Direct モード — USB接続方法の注意（2026-07-22 確認）

IC-9100等はPCへのUSB接続方法が複数存在する（リグ背面のUSB-B端子 / リモートジャック経由の汎用USBシリアル変換ケーブル 等）が、**Windows環境でDirectモードを使うには、リグ本体のUSB端子からPCへ接続し、ICOM純正のUSBドライバーをインストールする必要がある**。

リモートジャック＋汎用USBシリアル変換ケーブル（FTDI FT232R等）の構成では、Windows上でHamlibの`rig.open()`が確実にタイムアウトする（Hamlib error -5, RIG_ETIMEOUT）不具合を実機（IC-9100）で確認した。同一のリグ・同一のPCで、USB-B端子＋ICOM純正ドライバーの構成に変更したところ、問題なく接続できることを確認済み（Linux環境では両方の接続方法とも問題なく動作していた）。

原因はHamlib自体（Windows用シリアル層 `lib/termios.c`、rxtxライブラリから約2001年に移植されたコード）ではなく、汎用USBシリアル変換チップとWindows側のドライバーの組み合わせに起因すると推定される（Hamlib側の生CI-V直叩きによる回避も試みたが、Hamlibの調整済みシーケンスをpyserialで再現する過程で別の不具合を招くだけで根本解決には至らず、最終的にケーブル変更で解決したためこの方針は撤回した）。NETモード（rigctld経由）でこの問題が発生するかは未確認。

#### Cross-band UL frequency write — VFO_TX approach (confirmed 2026-06-20)

**Hamlib `set_func(RIG_FUNC_SATMODE, 1)` works correctly** in Hamlib 4.7.1:
- IC-9100/IC-9700: sends CI-V `16 5A 01` (SAT mode ON)
- IC-910H: sends CI-V `1A 07 01` (different command, handled automatically by Hamlib model backend)
- IC-821H: same `16 5A` as IC-9100

**CI-V commands for reference**:
- `FE FE <civ_addr> E0 16 5A 01 FD` — SAT mode ON (IC-9100/9700/821H)
- `FE FE <civ_addr> E0 16 5A 00 FD` — SAT mode OFF
- `FE FE <civ_addr> E0 16 59 xx FD` — Dual Watch (completely unrelated; do NOT confuse with SAT mode)

**Python binding caveat**: `rig.set_func()` takes exactly **2 arguments** `(func, status)`. Calling with 3 arguments `rig.set_func(CURR, SATMODE, 1)` silently passes `func=CURR` (a VFO constant), which causes `rig_has_set_func` to return 0 → ENAVAIL → no CI-V is sent. Always use `rig.set_func(RIG_FUNC_SATMODE, 1)`.

**`_SATMODE_USE_VFO_SUB = frozenset({3081})`** (IC-9700 のみ):

IC-9100 は `RIG_VFO_TX` でUL周波数書き込みが正常動作するが、IC-9700 は `open()` 時の read-back で `cache->satmode=1` が正しくセットされず `VFO_TX` が拒否されるケースがある。IC-9700 は代わりに `RIG_VFO_SUB` を使う。また、2回目の `open()` 後に追加で `set_func(SATMODE,1)` を送り `cache->satmode=1` を強制する。

| モデル | UL周波数 VFO定数 | 2回目 open() 後の追加 set_func | 備考 |
|---|---|---|---|
| IC-9700 (3081) | `RIG_VFO_SUB` | あり（`_SATMODE_USE_VFO_SUB`） | read-back が不完全なため強制上書き |
| IC-9100 (3068) / IC-910H (3044) / IC-821H (3034) | `RIG_VFO_TX` | なし | 2回目 open() で cache->satmode=1 確立 |

**Implementation (src/rig/controller.py)**:
- `connect()`: for satmode rigs — `rig.open()` → `time.sleep(0.3)` → `rig.set_func(RIG_FUNC_SATMODE, 1)` → `rig.close()` → `rig.open()`. Second open reads satmode=1 from rig, sets `cache->satmode=1`, which allows `set_freq(VFO_TX)` for UL writes.
- `satmode_warmup()`: same open→set_func→close sequence, called from background thread at startup. Imports Hamlib directly (`import Hamlib as _H`) rather than using `self._hamlib`, because `self._hamlib` is `None` until the first `connect()` call.
- `_init_split()`: just sets `_satmode_active = True` — SAT mode was already entered by `connect()`
- DL: `set_freq(RIG_VFO_MAIN, dl_hz)` as before
- UL (periodic): IC-9700 → `set_freq(RIG_VFO_SUB, ul_hz)` / IC-9100 → `set_freq(RIG_VFO_TX, ul_hz)`

**Why VFO_TX works**: in SAT mode, Hamlib maps `RIG_VFO_TX` to the TX VFO (Sub/UL). With `cache.satmode=1` (set by the second `rig.open()`), `ic9700_set_vfo` routes the command correctly. Confirmed with test script `scripts/test_ic9100_hamlib_satmode2.py` (2026-06-20).

#### NET mode satmode detection and transponder selection flow (confirmed 2026-06-17)

**Satmode detection for NET mode**: `HamlibNetController` does NOT query the rig model via rigctld (`_` command). Instead, `is_satmode` property returns:
```python
return self._satmode or self._ctcss_method == "icom_civ"
```
- `_satmode` is always `False` in NET mode (no model ID lookup)
- `ctcss_method == "icom_civ"` is set by user in Rig Settings → this is the definitive indicator

**Why no model name query**: `_fetch_model_name()` (which sent `_` to rigctld) was **removed**. It caused a socket race with the Doppler F/I cycle and was unreliable. If you need to re-add model detection in NET mode, do NOT use `_` command — find another approach.

**Transponder selection flow (NET mode satmode rig)**:
1. User selects transponder → `_apply_transponder_state_to_rig()` in `main_window.py`
2. `set_transponder_freqs(dl_hz, ul_hz)` → sets `_is_same_band` flag + stores `_transponder_dl_hz`/`_transponder_ul_hz`
3. `set_current_modes(dl_mode, ul_mode)` → stores DL/UL modes for UL throttle threshold and Stage-2 resend
4. If rig is connected: `_disconnect_rig()` first (user must re-press Connect for new satellite)
5. Background thread: `apply_transponder_state(dl_mode, ul_mode, ctcss_hz)`
   - caches `_current_dl_mode`/`_current_ul_mode`; sets `_pending_ctcss_hz` and `_pending_mode_net = True`
   - acquires `_cmd_lock` (pauses Doppler F/I)
   - `_send_split_init_independent()` — **`S 1 Main`（または同バンド時は `S 1 VFOB`）を独立ソケットで先送り**してsatmodeを確立（Direct modeの `set_func(SATMODE,1)` に相当）
   - `_send_freq_preset_independent()` — **DL/UL周波数を先書き**して IC-9100/9700 SAT mode Main/Sub バンド割り当てをアンカー（後述 Stage 1）
   - `send_mode_only()` via independent socket (VFO branch by `_is_same_band`)
   - `_apply_ctcss_civ_direct()` via rigctld TCP commands (`V Sub / C / U TONE / V Main / U TONE 0`)
6. After connect() + first live Doppler `I` write: **Stage-2 resend** — `send_mode_only()` + `_apply_ctcss_civ_direct()` (see below)

**順序の根拠**: satmode確立→周波数アンカー→mode設定→CTCSS設定 の順序。`S 1 Main` を先に送ることでsatmodeを確立し、続いて周波数を書いてIC-9100のSATモードメモリのバンド割り当てを新しい衛星に固定してからモード・CTCSSを送る。`connect()` の `_init_vfo()` が再度 `S 1 Main` を送っても、リグはすでにSATモードに入っているためCTCSS状態をリセットしない。

**Auto-disconnect on satellite change**: when `rig.is_satmode == True` and `rig.is_connected == True`, `_apply_transponder_state_to_rig()` calls `_disconnect_rig()` before re-sending mode/CTCSS. User must manually re-press Connect for the new satellite.

> **動作確認状況（2026-06-20時点）**
> - **Direct モード（IC-9100実機）**: 周波数・モード・CTCSSトーン（クロスバンド・同バンド両方）すべて動作確認済み
>   - SATモード有効化: Hamlib `set_func(RIG_FUNC_SATMODE, 1)` の `open→set_func→close→open` 方式
>   - モード・CTCSS: `_apply_mode_and_ctcss_hamlib()` で Hamlib のみ使用（pyserial 廃止・クロスプラットフォーム対応）
>   - クロスバンドUL: `set_freq(VFO_TX)` 方式で正常書き込み確認済み（IC-9700 は `VFO_SUB`）
>   - SAT ランプが点灯した状態でドップラー補正が正常動作（RS-44・ISS クロスバンドで確認）
>   - 同バンドFM（ISS等）: `_satmode_exit()` 後に `set_mode()` でモードを再設定（`_satmode_exit()` 内の sleep を 0.4s に設定して IC-9100 の内部モード復元を待つ）
>   - 同バンドDL表示を `set_vfo(VFOA)` で確実に復元（`set_freq(VFOA)` では不可）
>   - 同バンドDL更新も 2000 Hz / 60 秒で間引き（`_last_dl_update_time` 管理）
>   - HF/VHF クロスバンド（AO-7: 29MHz DL / 145MHz UL）: SAT mode 正常動作確認済み
>   - `satmode_warmup()`: 起動時に直接 `import Hamlib` することで正常動作（`self._hamlib is None` 問題を修正）
> - **NET モード（IC-9100 + rigctld）**: 周波数・モード・CTCSSトーン（クロスバンド・同バンド両方）動作確認済み
>   - トランスポンダー選択時の順序: `_send_split_init_independent()`（S 1 Main）→ `_send_freq_preset_independent()`（DL/UL周波数先書き）→ `send_mode_only()` → `_apply_ctcss_civ_direct()`
>   - CTCSS: `_apply_ctcss_civ_direct()` が rigctld TCP コマンド（`V Sub / C / U TONE / V Main / U TONE 0`）を送信（pyserial 廃止・macOS でも動作）。
>     **訂正（2026-07-15）**: この時点（2026-06-20）では実際にはトーン周波数書き込みコマンドが
>     `L CTCSS_TONE`（誤り）のままで、`RPRT -11`で拒否され続けていた。TONEエンコーダーのON/OFF
>     （`U TONE`）だけが独立して成功するため一見動いているように見えていた。詳細・修正内容は
>     後述の「`L CTCSS_TONE` → `C` 修正」セクション参照
>   - HF/VHF クロスバンド（AO-7: 29MHz DL / 145MHz UL）: SAT mode 正常動作確認済み
>
> **衛星切り替え時の VFO 逆転バグ修正（2026-06-21, cf62d6d）**: 実機確認待ち
>   - 2-stage freq anchor を追加（詳細は上記セクション参照）
>   - IC-9700 の `_SATMODE_USE_VFO_SUB` 分岐も Stage 1/2 の周波数プリセットに反映済み

**`_freq_band()` のバンド分類（クロスバンド判定に使用）**:
| 周波数範囲 | 戻り値 | 例 |
|---|---|---|
| < 30 MHz | `"HF"` | AO-7 DL 29MHz |
| 30–300 MHz | `"VHF"` | 145MHz（2m） |
| 300–3000 MHz | `"UHF"` | 435MHz（70cm） |
| 3000 MHz 以上 | `"SHF"` | — |

> **注意**: 旧実装は 200MHz 未満をすべて `"VHF"` に分類していたため、HF(29MHz) と VHF(145MHz) が同バンドと誤判定され、satmode が解除されて Main/Sub が入れ替わるバグがあった（AO-7で発覚・修正済み）。

#### CTCSS / Mode setting — IC-9100 / IC-9700 / IC-910H / IC-821H (Direct mode and NET mode)

**Hamlib でモード・CTCSS 両方とも動作する**（2026-06-20 IC-9100 実機で確認）。
旧来の pyserial raw CI-V アプローチは廃止し、全て Hamlib コマンドに統一した。

**なぜ Hamlib で動くか**:
- `set_mode(mode, 0, RIG_VFO_MAIN/RIG_VFO_SUB)` → icom バックエンドが `07 D0/D1` + `06` CI-V を正しく生成
- `set_vfo(VFO_SUB)` + `set_ctcss_tone(VFO_SUB, deci_hz)` + `set_func(FUNC_TONE, 1)` → CI-V `07 D1` + `1B 00 <BCD>` + `16 42 01` を生成
- 各モデル固有の CI-V コマンドは Hamlib バックエンドが自動選択（IC-910H の `1A 07 01` 等）

**`_apply_mode_and_ctcss_hamlib(dl_mode, ul_mode, ctcss_hz)`** — Direct mode の中心実装:
1. `import Hamlib as _H` を直接実行（`self._hamlib` は connect() 前は None なので使えない）
2. `rig.open()` → `set_func(RIG_FUNC_SATMODE, 1)` → `rig.close()` — satmode ON を送信
3. `rig2.open()` — 2回目の open で `cache->satmode=1` が確立（これがないと `VFO_MAIN/SUB` が拒否される）
   - IC-9700 のみ (`_SATMODE_USE_VFO_SUB`): さらに `set_func(SATMODE,1)` を追加送信して `cache->satmode=1` を強制上書き（read-back が不完全なため）
4. **Stage 1 周波数プリセット**: `_transponder_dl_hz`/`_transponder_ul_hz` が設定済みであれば先に `set_freq(VFO_MAIN, dl)` + `set_freq(vfo_ul_preset, ul)` を送信して IC-9100/9700 SAT mode Main/Sub バンド割り当てをアンカー。UL VFO定数: IC-9700 → `VFO_SUB`、IC-9100以下 → `VFO_TX`
5. `set_mode(dl_hamlib, 0, VFO_MAIN)` + `set_mode(ul_hamlib, 0, VFO_SUB)` — DL/UL モード設定
6. `set_vfo(VFO_SUB)` → `set_ctcss_tone(VFO_SUB, deci_hz)` → `set_func(FUNC_TONE, 1/0)` — Sub CTCSS
7. `set_vfo(VFO_MAIN)` → `set_func(FUNC_TONE, 0)` — Main CTCSS クリア（ブリード防止）
8. `rig2.close()`
- 全体を `_port_lock` で保護（connect() との競合を防ぐ）

**Direct mode — `set_ctcss_tone(tone_hz)`**:
  - satmode + **not connected** → `_apply_mode_and_ctcss_hamlib()` を呼ぶ（Hamlib 直接、port free）
  - satmode + **connected** → deferred（Hamlib がポートを保持中。`_satmode_enter` が apply 済み）
  - non-satmode → standard Hamlib `set_ctcss_tone` / `set_func` path（FTX-1F, FT-991A 等）

**`_port_lock` — `_apply_mode_and_ctcss_hamlib` と `connect()` の競合防止**:

`HamlibDirectController` の `_port_lock = threading.Lock()` が以下を順序保証する:
- `_apply_mode_and_ctcss_hamlib()` 全体（open→set_func→close→open→[mode/ctcss]→close）
- `connect()` 内の `rig.open()`
- `send_mode_only()` 内の `rig.open()` / `rig.close()`

**NET mode — `_apply_ctcss_civ_direct(tone_hz)`**:
pyserial を廃止し、独立した rigctld TCP ソケットでコマンドを送信（macOS でも動作）:
```
V Sub                    # VFO Sub を選択
C <deci_hz>              # CTCSS 周波数設定（デシ Hz 整数。無効化時はスキップ）
U TONE 1/0              # CTCSS エンコーダー ON/OFF
V Main                   # VFO Main を復元
U TONE 0                 # Main の CTCSS クリア（ブリード防止）
```

**`L CTCSS_TONE` → `C` 修正（2026-07-15、IC-9100実機で確認・修正済み）**: 上記は元々
`L CTCSS_TONE <deci_hz>`（rigctldのLEVEL設定構文）を使っていたが、CTCSS_TONEはLEVELとして
登録されておらず`RPRT -11`（ENAVAIL）で拒否されるため、**トーン周波数自体は一度も書き込めて
いなかった**。直後の`U TONE 1`（エンコーダー有効化、正しいコマンドなので独立して成功する）
だけは実行されるため、TONEランプは点灯するが周波数はリグに残っていた以前の値のままになる、
という発覚しにくい不具合だった（IC-705の`set_ctcss_tone()`で判明した`L`→`C`の教訓と同一原因。
詳細は「IC-705 (Hamlib model 3085)」セクション参照）。IC-9100実機で`L CTCSS_TONE`が
`RPRT -11`で拒否され`C`は`RPRT 0`で成功することを確認した上で、`_apply_ctcss_civ_direct()`
を`C {deci_hz}`（`tone_hz <= 0`時はスキップ）に修正済み。

#### Direct モード CTCSS 連打で別のトーンが誤書き込みされるバグと修正（`_port_lock`解放タイミング、2026-07-23）

**症状**（IC-9100・Windows実機で確認）: SO-50で74.4Hzを書き込んだ直後に67Hzボタンを押すと、
67Hzではなく88.5Hz（別の正当なCTCSSトーン値）が誤って書き込まれることがあった。67Hzボタンを
何度か押し直すと最終的には正しく67Hzが書き込まれる、という間欠的な不具合。

**原因**: `_apply_mode_and_ctcss_hamlib()`（satmode機用）・`_apply_ctcss_hamlib_standalone()`
（IC-705等の汎用非satmode機用）の両方で、`rig.close()`（実際にHamlibセッション＝シリアル
ポートを閉じる処理）が`finally:`節にあり、**`with self._port_lock:`ブロックの外**で
実行されていた。`with`ブロックを抜けた時点で`_port_lock`は解放されるが、その直後の
`finally`での`close()`はまだ完了していない——つまり「鍵は返したが、部屋の鍵は物理的には
まだ閉め終わっていない」状態が一瞬発生する。この隙に次のボタン押下（別スレッド）が
`_port_lock`を取得して新しいHamlibセッションのopen()を試みると、同一COMポートに対して
2つのセッションが一瞬重なり、CI-Vバスの送受信が混線して、別の（しかしCI-Vとしては正当な）
トーン値が書き込まれてしまう。Windowsのシリアルポートはこの「閉じている最中の再オープン」
に弱いことが`_open_rig_with_retry()`のdocstringにも記載済みで、症状がWindowsで顕在化
しやすい一因と考えられる。

**修正**: 両関数とも、`try/except/finally`（`close()`を含む）全体を`with self._port_lock:`
の内側に入れ、ポートを完全に閉じ終わるまで鍵を保持するよう再構成（`_send_freq_preset_direct()`
の既存の正しい実装と同じ形に統一）。これにより、前のCTCSS書き込みが完全に終わるまで、
後から来たボタン押下は待機してから処理を開始するようになる（同時実行による混線ではなく、
順番待ちになる）。

**プラットフォーム適用範囲**: `src/rig/controller.py`のOS非分岐の共通コードのため、
Windows専用ではなくLinux/macOSを含む全プラットフォームのDirect モードに同じ修正が
適用されている。ただし今回のように間欠的に顕在化するかどうかはOS依存（Windowsの方が
シリアルポートのopen/close競合に敏感）で、Linux実機ではこれまで報告されていなかった。

#### 衛星切り替え時の VFO 割り当て逆転バグ修正（2-stage freq anchor, 2026-06-21）

**バグの原因**: IC-9100/9700 は SAT モードメモリに「前回の Main/Sub バンド割り当て」を保持する。`set_func(SATMODE,1)` / `S 1 Main` を送るとこのメモリが復元される。AO-73（145MHz DL/435MHz UL）→ ISS V/U（145MHz DL/435MHz UL）のように、同じバンド構成でも **衛星が変わると SAT モードメモリが前の衛星のバンド状態で復元**されるため、直後に送ったモードや CTCSS が誤った VFO に書き込まれる。

例: AO-73（UHF DL）→ ISS V/U（VHF DL）に切り替えると、IC-9100 が Main=UHF の状態のまま復元し、次の `set_mode(VFO_MAIN, FM)` が UHF Main（本来は VHF）に書かれ CTCSS トーンも同様に逆転する。

**修正方針 — 2-stage アプローチ**:

| ステージ | タイミング | 処理 | 目的 |
|---|---|---|---|
| **Stage 1** | トランスポンダー選択時（connect前） | DL/UL周波数を先に書く → モード/CTCSSを送る | IC-9100/9700 SAT mode バンド割り当てを新衛星に固定してからモード書き込み |
| **Stage 2** | connect後・最初の Doppler UL 書き込み時 | モード+CTCSSを再送 | ライブ接続上でバンド割り当て確定後の保険的再確認 |

**Stage 1 の実装**:
- Direct mode: `main_window._apply_transponder_state_to_rig()` でトランスポンダーの DL/UL Hz を `rig._transponder_dl_hz`/`rig._transponder_ul_hz` にセット → `_apply_mode_and_ctcss_hamlib()` 内で step 4 として周波数を先書き
- NET mode: `set_transponder_freqs()` で `_transponder_dl_hz`/`_transponder_ul_hz` を保存 → `apply_transponder_state()` 内で `_send_split_init_independent()` の直後に `_send_freq_preset_independent()` を呼ぶ

**Stage 2 の実装**:
- Direct mode: `apply_transponder_state()` で `_pending_mode_ctcss = True` をセット → `_set_vfo_frequencies_locked()` で最初の UL 書き込み後に `_resend_mode_ctcss_via_rig()` を呼んで `self._rig` 経由でモード+CTCSS を再送
- NET mode: `apply_transponder_state()` で `_pending_mode_net = True`/`_pending_ctcss_hz` をセット → `set_vfo_frequencies()` で最初の `I` 書き込み後に `send_mode_only()` + `_apply_ctcss_civ_direct()` を再送（`_cmd_lock` 保持中だが両関数は独立ソケットで `_cmd_lock` を取得しないため安全）

**`_SATMODE_USE_VFO_SUB` と周波数プリセットの関係**:
- Stage 1 でも Stage 2（Doppler UL 書き込み）でも、VFO定数の選択は同じ分岐（IC-9700 = `VFO_SUB`、その他 = `VFO_TX`）に従う。

**IC-9100 mode behaviour — key facts**:
- Entering SAT mode: IC-9100 does **not** unconditionally reset to FM. It generally preserves the mode from the previous session.
- Exiting SAT mode (`set_func(SATMODE, 0)`): IC-9100 **does** restore its "normal-mode memory" (typically USB). This is why `_satmode_exit()` calls `self.set_mode()` after `set_split_vfo()` — to force the transponder's correct DL/UL modes. Sleep after `set_func(SATMODE, 0)` is **0.4s** (increased from 0.1s) to wait for IC-9100's internal mode restoration before applying modes.

**Direct mode — Connect ボタンは常にバックグラウンドスレッドで実行**: `_on_connect_rig1()` は `rig.connect()` を `threading.Thread` で別スレッドに移す。UI スレッドは「Connecting...」表示のまま待機し、完了後に `_rig1_connect_done: Signal = Signal(bool)` 経由で `_finish_rig1_connect()` に通知されてボタン・ステータスを更新する。この変更以前は UI スレッドで同期的に `connect()` を呼んでいたため、IC-9100 の SATMODE 設定に数秒かかる際にウィンドウがフリーズし、キューに溜まったクリックイベントで二重接続が発生していた。

**Direct mode — When CTCSS button is pressed while connected (Doppler running)**: port is held by Hamlib. `_on_ctcss_send()` in `main_window.py` takes a special path for `HamlibDirectController` + `_satmode=True` + `is_connected=True`:
1. `_disconnect_rig()` on UI thread (releases port)
2. Background thread: `set_ctcss_tone(tone_hz)` → `_apply_mode_and_ctcss_hamlib()` (`_port_lock` acquired)
3. Background thread: `rig.connect()` (waits for `_port_lock` to be released before `rig.open()`)
4. `QMetaObject.invokeMethod(self, "_on_satmode_rig_reconnected", QueuedConnection)` to refresh UI on UI thread

**NET mode — CTCSS is sent as part of `apply_transponder_state()`**: no separate disconnect/reconnect needed. See NET mode transponder selection flow above.

**pyserial availability**: pyserial は FT-991A Direct モード（`_apply_mode_and_ctcss_cat_ft991`）と FTX-1F NET モード（`_send_direct_cat`）で引き続き使用。`main.py` の sys.path surgery 前に事前 import が必要:
```python
with contextlib.suppress(Exception):
    import serial as _serial_preload  # noqa: F401
if _HAMLIB_SYS in sys.path:
    sys.path.remove(_HAMLIB_SYS)
```

**FT-991A / FTX-1F are completely unaffected**: they use `_CAT_CTCSS_METHODS` (checked first in `_on_ctcss_send`) and never reach the satmode Hamlib path.

**`_SATMODE_RIG_IDS`** (src/rig/controller.py):
```python
_SATMODE_RIG_IDS: frozenset[int] = frozenset({
    3081,  # IC-9700
    3068,  # IC-9100
    3044,  # IC-910H
    3034,  # IC-821H
})
```

#### Same-band duplex (V/V, U/U) — Direct mode

IC-9100/9700 のサットモードは **Main と Sub を必ず異なるバンドに割り当てる** ハードウェア制約がある。
ISS APRS (145.825 MHz UL/DL 同一) や AO-91 (435 MHz UL/435 MHz DL 同一) などの同バンド衛星では satmode が使えない。

`_freq_band(hz)` で DL と UL のバンドを比較し、同一の場合は **`_is_same_band = True`** と判定して自動的に分岐する：

| 条件 | VFO割り当て | 周波数更新方式 |
|---|---|---|
| **クロスバンド** (V/U, U/V) | satmode (Main=RX, Sub=TX) | `set_freq(RIG_VFO_MAIN, dl)` + `set_freq(RIG_VFO_TX, ul)` |
| **同バンド** (V/V, U/U) | 通常split (VFO-A=RX, VFO-B=TX) | `set_freq(RIG_VFO_A, dl)` + `set_freq(RIG_VFO_B, ul)` + `set_vfo(VFOA)` |

**同バンド時の処理フロー** (`_set_vfo_frequencies_locked`):
1. `_is_same_band == True` を検出
2. `_satmode_active == True` ならば `_satmode_exit()` を呼んでサットモードを解除（SAT MODE OFF → split ON）
3. 以降は VFO-A/B の通常 split でドップラー補正

**`_satmode_exit()`**:
- `self._rig.set_func(RIG_FUNC_SATMODE, 0)` で SAT モードを OFF
- `time.sleep(0.4)` — IC-9100 の内部 normal-mode memory 復元（通常 USB）を待つ。0.4s 未満だと set_mode(FM) が USB で上書きされるレースが発生する
- `set_split_vfo(RIG_VFO_CURR, 1, RIG_VFO_B)` で通常 split (VFO-B=TX) を有効化
- `set_mode(dl_mode, VFOA)` + `set_mode(ul_mode, VFOB)` でトランスポンダーのモードを再設定
- `_satmode_active = False` にセット（finally ブロック内でセットするため、例外時も確実に解除される）

**UL更新頻度（同バンドFM）**: IC-9100 は VFO-B 切り替え時に表示がちらつく。FM/AFSK の場合はキャプチャーレンジ (±5 kHz) が ISS 最大ドップラー (±3.5 kHz at 145 MHz) を上回るため、UL 更新を間引く:
- 閾値: 2000 Hz 以上の変化、または前回更新から 60 秒経過（FM/AFSK）
- 非 FM は 20 Hz / 15 秒

**DL更新頻度（同バンドFM）**: DL も同じ閾値（2000 Hz / 60 秒）で間引く（`_last_dl_update_time` で管理）。UL と同様に FM キャプチャーレンジで十分なため。

**UL更新後のVFO-A表示リストア**: `set_freq(RIG_VFO_B, ul_hz)` 後、Hamlibのicomバックエンドは内部のCURRをVFO-Bのままにするため、IC-9100のディスプレイがUL周波数を表示し続ける。UL更新が完了するたびに `rig.set_vfo(rx_vfo)` を呼び、CI-V `07 00`（VFO-A選択）を送信してDL表示に戻す。`set_freq(VFOA, hz)` では効果がないことが実機確認済み（周波数書き込みのみでディスプレイ切り替えは行われない）。

**モード設定 (`send_mode_only`)**: `_satmode_active` フラグで VFO を選択
- `_satmode_active == True` → `RIG_VFO_MAIN` / `RIG_VFO_SUB`（旧 `SUB_A` は satmode で拒否されるため修正済み）
- `_satmode_active == False`（同バンド）→ `RIG_VFO_A` / `RIG_VFO_B`

**同バンド時の CTCSS**: `HamlibDirectController.set_ctcss_tone()` は `self._satmode == True` であれば `_satmode_active` の状態（cross-band / same-band）に関わらず常に `_apply_mode_and_ctcss_hamlib()` を呼ぶ。したがって同バンド衛星でも Hamlib 経由でトーンが正しく設定され、動作する（実機確認済み）。

#### Same-band duplex (V/V, U/U) — NET mode

NET mode の同バンド対応は Direct mode と同じロジックで `_is_same_band` フラグによる分岐。

`set_transponder_freqs(dl_hz, ul_hz)` で `_is_same_band` を設定（Connect 前のトランスポンダー選択時）:
```python
self._is_same_band = self._freq_band(dl_hz) == self._freq_band(ul_hz)
```

| 条件 | split init (`_init_vfo`) | send_mode_only VFO |
|---|---|---|
| **クロスバンド** (V/U, U/V) | `S 1 Main`（rigctld satmode） | Sub/Main |
| **同バンド** (V/V, U/U) | `S 1 VFOB`（通常 split） | VFOB/Main (VFOA相当) |

**UL 更新頻度（同バンド）**: IC-9100 は `I` コマンド（VFOB 更新）時にディスプレイが一瞬ちらつく。
Direct mode と同じ閾値を NET mode にも適用（`_last_ul_update_time` + `_current_dl_mode` で管理）:
- FM / AFSK / DIGITALVOICE: 2000 Hz 以上の変化、または前回更新から 60 秒経過
- SSB / CW など非 FM: 20 Hz / 15 秒

残る 2フラッシュ（約1分ごとのUL更新時）は IC-9100 ハードウェアの動作（VFOB 更新直後に一瞬 VFOB 表示 → VFOA 表示に戻る）であり、ソフトウェアバグではない。

#### IC-9700 Sub VFO の DATA モード（-D サフィックス）対応 — GitHub Issue #16（2026-07〜08、v0.2.47 で解決）

**症状**: RS-44/JO-97/MO-122 のFT4トランスポンダー（`USB-D`/`LSB-D`）選択時、Main（DL）は正しく`-D`表示になるが、**Sub（UL/TX側）だけは常にベースの側波帯（USB/LSB）のまま**で、CW/DATAトグルボタンでも同様だった。実機はIC-9700（Direct モード、Windows）。

**根本原因の特定**: HamlibソースコードのIcomバックエンド（`icom.c`の`icom_set_mode()`）を精査した結果、IC-9700のようなMain/Sub+A/B型リグでは、Sub側へのDATAモードフラグ設定時に`force_vfo_swap`が常にtrueとなり、高速な結合コマンド（`0x26`）が`-RIG_ENAVAIL`で強制的に無効化され、古い方式のコマンド（`C_CTL_MEM`/`S_MEM_DATA_MODE`＝`1A 06`）にフォールバックすることが判明。この古い方式がSubに対して確実に反映されない、というのが真の原因だった。

**解決までの経緯（複数の対策・複数のバグを経て収束）**:

1. **生CIVを`rig.send_raw()`で送る初回の対策 → 却下**: `1A 06`が効かないなら生CI-Vで直接書けばよいと考え、既に開いているHamlibセッション上で`rig.send_raw()`を呼んだところ、**実機でプロセスがクラッシュ**した（"stack smashing detected"、Python SWIGバインディングの既知のリスク）。原因調査の過程で、標準の`pyserial`（Hamlibのsend_raw()を経由しない別経路）であれば同種の失敗条件下でも通常の例外（`SerialException`）で済み、プロセスをクラッシュさせないことを確認。この対策は完全に撤回し、`send_raw()`ベースの再導入は安全性確認なしに行わないこと、という注記をコードに残した。

2. **pyserialへの切り替え（v0.2.45）で新たに発覚した2つのバグ**:
   - **バグA: CI-Vのechoを考慮しておらず、読み取りロジックが常に破綻していた**。CI-Vは半二重の単線バスのため、送信したコマンド自体がまず自分に返ってくる（echo）。`ser.read_until(b"\xfd")`は最初に見つかった`\xFD`（＝echo自身の終端）で止まってしまい、リグからの本当の応答を一度も読めていなかった。この結果、診断ログは常に`mode_byte=0x-1 data_flag=0x-1`という無意味な値を表示していた。実機のログを手作業でデコードしたところ（echo部分を除去して本当の応答だけを取り出す）、**"1A 06"コマンド自体は実際にSubのDATAフラグをONにできており、読み戻しでも正しく確認できていた**ことが判明——つまりコマンド自体は機能していた。
   - **修正**: `send()`ヘルパーで、読み取った応答が「自分が送信したフレームと完全一致」する場合はecho と判断し、もう一度`read_until()`を呼んで本当の応答を取得するようにした（`resp == f`での判定）。
   - **バグB（実害あり・リグレッション）: pyserial区間の後にHamlibセッションを再openする際、IC-9700専用のsatmodeキャッシュ修正処理が抜けていた**。`connect()`の最初のsatmode確立シーケンスでは「2回目のopen直後にIC-9700だけ`set_func(SATMODE,1)`をもう一度送ってHamlib内部の`cache->satmode`を確立する」という既存の仕組み（`_SATMODE_USE_VFO_SUB`分岐）があるが、pyserial区間の**後**に新設した3回目の再open処理では、この措置を入れ忘れていた。このため再openしたHamlibセッションがsatmode状態を正しく認識できず、後続の`set_vfo(MAIN)`が`Hamlib error -9`で拒否され、**トランスポンダー選択のたびに毎回`RigControlError`で失敗する**という、以前より悪化した回帰バグになっていた。
   - **修正**: pyserial区間後の再open処理にも、既存の"IC-9700 extra set_func(SATMODE,1)"と全く同じ措置を追加した。

3. **v0.2.46でバグA・Bを修正後もなお失敗し発覚した3つ目のバグ（CI-Vアドレスの機種取り違え）**: `_civ_addr_int()`は、Rig SettingsのCI-Vアドレス欄が空欄の場合`0x65`（このヘルパー自身のdocstringに"IC-9100用のデフォルト"と明記されている値）にフォールバックする。新設したpyserialコード（`_send_sub_mode_civ_pyserial()`）はこの共通ヘルパーをそのまま流用していたため、**報告者のIC-9700（実際のCI-Vアドレスは0xA2）に対して誤って0x65へ送信**しており、応答が一切返らず`CI-V no ACK after 3 attempts`で失敗していた。HamlibのMain側の処理が今まで通り成功していたのは、Hamlib自身が機種ごとの正しいデフォルトアドレスを内部で自動解決しているためで、CI-Vアドレス欄が空欄でも影響を受けなかった。IC-705向けに既にあった同種の対策（`_IC705_DEFAULT_CIV_ADDR = 0xA4`という機種専用の定数）と同じパターンで、`_IC9700_DEFAULT_CIV_ADDR = 0xA2`を新設し、`_model_id in _SATMODE_USE_VFO_SUB`の場合はこちらを優先するよう修正（v0.2.47）。0xA2はHamlib自身のソース（`rigs/icom/ic7300.c`の`IC9700_priv_caps`構造体、`0xA2, /* default address */`）でも確認済みの正しい値。

**最終確認（v0.2.47、実機ログで確認済み）**: LSB ⇔ LSB-D の切り替えで、Subの読み戻し値が`data_flag=0x00`⇔`data_flag=0x01`と正しく連動することを確認。報告者からも動作確認の報告あり。

**教訓**:
- 生CI-Vコマンドの安全性は「`send_raw()`経由か`pyserial`経由か」で全く異なる。前者はPython SWIGバインディングのクラッシュリスクを常に疑うこと。
- 半二重・単線バスのCI-Vでは、USBシリアル変換アダプタによっては**自分の送信内容がechoとして先に返ってくる**ことがある（Hamlib自身も`IC9700_priv_caps`に`serial_USB_echo_check`というフラグを持っており、この特性を把握している）。`read_until()`等の「最初の終端文字で止まる」読み取り方式は、echoを本当の応答と誤認しやすいので要注意。
- close/reopenを伴うHamlibセッション操作で、特定機種専用の初期化措置（今回のIC-9700 satmodeキャッシュ修正）がある場合、**その措置を必要とする箇所すべて**に同じ措置を横展開できているか確認すること。1箇所に追加して満足せず、同じ関数内で複数回close/reopenする設計に変えた場合は特に注意。
- CI-Vアドレスなどの機種固有デフォルト値は、既存の共有ヘルパー（今回の`_civ_addr_int()`）が実は特定の一機種（IC-9100）専用の値を返すものだった、というケースがある。新しいコードから安易に共有ヘルパーを流用する前に、そのヘルパーが本当に汎用なのか、docstringも含めて確認すること。

### IC-705 (Hamlib model 3085) — 汎用（非satmode）Icom CI-Vリグの参照実装（2026-07-06〜07 確認済み）

IC-9100/9700等と異なりMain/Sub概念を持たない、VFOA/VFOBのみの汎用Icom CI-Vリグ。実機での動作確認を通じて、**Hamlibの高レベルAPI（`set_split_vfo()`・`L CTCSS_TONE`・`set_func(FUNC_TONE)`）がこのモデルのバックエンドで複数箇所不安定/誤り**であることが判明し、生CAT/CI-Vへのバイパスで解決した。satmode機（`_SATMODE_RIG_IDS`）にもFTX-1F/FT-991A（`_FTX1_MODEL_IDS`/`_FT991_DIRECT_MODEL_IDS`）にも属さない、真に汎用的な非satmode NETモードリグとして初めて実地検証されたケース。

**Direct モード（`HamlibDirectController`、`_IC705_MODEL_IDS = frozenset({3085})`）**:
- `_init_split()`: `set_split_vfo(RIG_VFO_A, 1, tx_vfo)`（明示的にRX vfoを渡す）とすると、Hamlibバックエンドがtx_vfo割り当てを反転させる不具合を確認（`RIG_VFO_A`ではなく`RIG_VFO_CURR`を渡すことで解消。`_send_freq_preset_direct()`は元々`RIG_VFO_CURR`を使っており問題なかった）
- UL書き込み（`set_freq(VFOB, ul_hz)`）後、Hamlibのicomバックエンド内部の「現在選択中VFO」がVFOBのまま残り、メイン画面がULを表示し続ける → `set_vfo(rx_vfo)`でVFOA表示に復元（IC-9100の同一バンドフォールバックで既に確立していたのと同じパターン）
- split ON/OFF（`_init_split()`・`_send_freq_preset_direct()`・終了時の`_release_rig_split_on_exit()`）は結局すべて**生CI-Vフレーム**（`C_CTL_SPLT=0x0F`、`S_SPLT_ON=0x01`/`S_SPLT_OFF=0x00`）に置き換えた。`set_split_vfo()`自体が特定の引数の組み合わせで`icom_set_split_vfo: unsupported split <VFO定数の値>`という不可解なエラーを返すことがあり（Hamlib側のSWIGバインディングか内部ロジックの不具合と推定、根本原因は特定できず）、既存のFTX-1F/FT-991A同様「Hamlib呼び出しを信用せず生CATへ逃げる」方針に合流した
- `rig.send_raw()`のPython SWIGバインディングは**`stack smashing detected`でプロセスがクラッシュする既知の危険**があり、生CI-V送信は`send_raw()`ではなく独立したpyserial/`os.open()`書き込みで行うこと（`_apply_ctcss_civ_via_send_raw()`のような既存のsatmode用ヘルパーも将来的に要注意）
- CTCSS: `set_func(RIG_FUNC_TONE, ...)`はコマンド間に十分な遅延（0.15秒、既存の`_apply_ctcss_civ_via_send_raw()`と同じ値）を入れないと**トーン周波数は書き込めるがエンコードON自体が反映されない**。`_apply_ctcss_hamlib_standalone()`（未接続時の一時セッション）・`set_ctcss_tone()`（接続時）の両方に遅延を追加して解決
- CTCSS設定枠（Rig Settings）は元々NETモードのみを想定した設計だが、Directモード選択時は`_ctcss_group`がグレイアウトされ**編集不可**になるだけで、**保存済みの古い値はモード判定ロジックに読み込まれ適用され続ける**。汎用Directリグ（satmode/FTX-1F/FT-991A以外）は`main_window._is_generic_direct_rig()`で判定し、`ctcss_method`の値に関わらず常に標準Hamlib経路（`set_ctcss_tone()`）を使うようにして、NETモード時代の設定値が紛れ込む余地を構造的に排除した（`_do_nonsatmode()`・`_on_ctcss_send()`両方に適用）

**NET モード（`HamlibNetController`）— モデル自動判定は存在しない**:
NETモードはDirectモードと異なりHamlibモデル番号を直接取得できない（`_`コマンドによる問い合わせは過去のソケット競合で廃止済み）。そのため各分岐は**Rig SettingsダイアログでユーザーがCTCSS Tone Settings枠内で選択した設定値のみ**を根拠にしている:
```python
is_satmode_rig = self._satmode or self._is_satmode_rig   # "Icom SAT mode rig" チェックボックス
is_yaesu_cat   = self._ctcss_method in ("ftx1", "ft991")  # CTCSS Method プルダウン
```
どちらにも該当しない（デフォルトの"Hamlib standard"のまま）場合にのみ「汎用リグ」として扱われる、消去法の設計。IC-705を積極的に検出しているわけではないので、**この設定をユーザーが正しく選択している前提**であることに注意（FT-991Aを繋いだまま設定を"Hamlib standard"のままにすると汎用経路に落ちて誤動作する）。

- `_init_vfo()`・`send_mode_only()`・`_send_split_init_independent()`が無条件に`S 1 Main`/`Main`/`Sub`命名を使っていたのは、FTX-1F/FT-991Aのrigctldバックエンド固有の癖（S 1 Main以外だと不定動作）を「非satmode リグ全般の仕様」と誤って一般化したもの。IC-705にはMain/Sub概念がなく、`"Main"`という文字列がバックエンドに誤解釈されRX/TXが入れ替わる（実機確認: ダウンリンクがVFOBに、アップリンクがVFOAに入った）。`is_yaesu_cat`で分岐し、汎用リグは素直な`VFOA`/`VFOB`命名（`S 1 VFOB`等）を使うよう修正
- `HamlibNetController.set_ctcss_tone()`が`L CTCSS_TONE {value}`（rigctldの**LEVEL設定構文**）を使っていたが、CTCSS_TONEはLEVELではなく専用コマンド文字を持つため、rigctldは`RPRT -11`（ENAVAIL）で拒否する。正しくは`C {deci-Hz}`（専用コマンド）。**satmode NETモードのCTCSS実装（`_apply_ctcss_civ_direct()`）にも同一パターンの`L CTCSS_TONE`が存在していたが、2026-07-15にIC-9100実機で同一の`RPRT -11`拒否を確認の上`C`に修正済み。詳細は「ICOM SATMODE機（IC-9100/9700等）NETモードCTCSS — `L CTCSS_TONE` → `C` 修正」セクション参照**
- CTCSSトーンはVFOごとに独立して保持される（実機確認: VFOAとVFOBに別々の値を書き込み・読み戻し可能）。`send_mode_only()`はVFOA（ダウンリンク）を選択した状態で終わるため、VFO切り替えなしで`C`を送るとダウンリンク側に誤って書き込まれる → `V VFOB`で明示的に切り替えてから`C`、その後`V VFOA`で表示を復元
- `C {value}`は**トーン周波数のみ**を設定し、エンコードのON/OFF自体は別のfunc（`U TONE 1`/`U TONE 0`）で制御する。この分離に気づかず`C`だけ送っていたため、以前のDirectモードテストでたまたまエンコードがONのまま残っていた間は「動いているように見えていた」だけだった。`tone_hz <= 0`の場合は`C 0`がrigctldに`RPRT -9`で拒否されるため`C`自体をスキップし、`U TONE 0`のみ送る
- `set_ctcss_tone()`は接続前（`self._sock is None`）は独立ソケットを新規に開いて送信する（`_send_freq_preset_independent()`と同じパターン）。`_on_ctcss_send()`（Activateボタン等の手動送信ハンドラ）に残っていた`if not rig.is_connected: return`という古いガードが、この独立ソケット経路に到達する前にブロックしていたため削除済み
- `_send_split_init_independent()`・`_send_freq_preset_independent()`は、周波数自体はVFOA/VFOBに正しく書き込まれる（読み戻しで確認済み）が、**画面表示が更新されないことがある**（Directモードで既知の「CURR復元」パターンと同根）。汎用リグ判定時のみ末尾に`V VFOA`を追加して表示を強制リフレッシュする（FTX-1F/FT-991A/satmode機には一切影響しないよう分岐）

**教訓**: Hamlibの高レベルAPIが「本来動くはずのコマンド」でも、特定モデルのバックエンド実装次第で無反応・誤動作・クラッシュしうる。実機のない機種を新規サポートする際は、Direct/NET問わず生CAT/CI-Vへのフォールバックを疑い、可能な限り実機で個々のコマンドの読み戻しを確認すること。

#### Connect後にMain（DL）表示が完全に固定される不具合と、生CI-V「応答未読み取り」による通信デシンク（2026-07-07 修正済み）

**症状**: Direct モードでConnect後、しばらくすると（衛星・モードを問わず、RS-44/SO-50/ISS等すべてで再現）
Main（DL）表示がConnect時点の値のまま完全に固定され、以降ドップラー補正が一切反映されなくなる。
Sub（UL）側は正常に更新され続ける。macOSでもLinuxでも再現（Windows未検証）。

**調査の迷走**（教訓として記録）:
1. 当初「macOS固有のUSBシリアルドライバのタイミング問題」と誤診断し、DL/UL/VFO復元コマンド間の
   sleepを0.05秒→0.15秒に拡大したところ、**それまで正常だった衛星まで含めて全滅**した。
   sleepを増やすほど悪化するという事実が、後から振り返れば「コマンド間隔の問題ではない」ことを
   示す決定的な手がかりだった（無関係な要素をいじって「改善しない」ならまだしも「悪化する」動きが
   出たら、それは診断の前提そのものが誤っている合図として扱うべき）
2. ユーザーが`HAMLIB_DEBUG`相当の詳細トレースログを取得してくれたことで真因が判明。トレースには
   「`07 00`（VFO A選択）コマンドを送ったのに、返ってきたのは全く無関係な`25 00`（周波数照会）の
   応答だった」という明確な証拠が残っていた（`icom_check_ack: command timed out (0x25)` として
   Hamlib自身が矛盾を検出しログに残していた）

**根本原因**: `_init_split()`・`_send_freq_preset_direct()`・`_release_rig_split_on_exit()`が、
IC-705のSplit ON/OFF生CI-Vフレーム（`0F 01`/`0F 00`）を`os.open()`または独立した`pyserial.Serial`で
**送信専用（応答を一切読み取らず）**に送っていた。IC-705は必ず何らかの応答を返すが、その応答バイト列は
**同一デバイスノードに対する全てのfd/openセッションで共有されるカーネルのtty受信バッファ**に残り続ける
（書き込んだ側のfdが既にcloseしていても消えない）。この読み捨てられなかった応答バイト列を、直後や
少し後に開かれるHamlib自身のセッション（`self._rig`）が**自分の別コマンドへの応答だと誤解して**
読み取ってしまい、そこから**セッション終了までの全てのリクエスト/レスポンスが恒久的に1つずつズレる**。
一度発生すると自己修復せず、Connect後どれだけ待っても直らない（今回の「完全に固定」という症状と
一致）。

**修正**（`src/rig/controller.py`・`src/ui/main_window.py`）: 上記3箇所すべてで、生CI-Vフレーム送信後に
**必ず応答を読み取ってから**閉じるよう変更。
- `_init_split()`: `os.open()`のフラグを`O_WRONLY`→`O_RDWR`に変更し、`write()`後に`time.sleep(0.1)`
  → `os.read(_fd, 32)`（`contextlib.suppress(OSError)`で保護）してから`close()`
- `_send_freq_preset_direct()` / `_release_rig_split_on_exit()`: 既に`pyserial.Serial(..., timeout=1)`
  を使っていたので、`ser.write(...)`の直後に`ser.read(32)`を追加するだけで済んだ

**教訓**: Hamlibと同じシリアルポートに対して独立したfd/セッションで生CAT/CI-Vを送る際は、
**書き込みっぱなしにせず必ず応答を読み取って捨てること**。書き込み専用（`O_WRONLY`）で
`open→write→close`する既存パターン（FTX-1F・FT-991の生CAT送信等）は、対象コマンドが本当に
無応答（ACKを返さない）と分かっている場合を除き、同じ危険を抱えている可能性がある。
「Hamlibの呼び出しが例外を出さずに成功しているのに実機の挙動がおかしい」系の不具合を見たら、
まずHamlibのデバッグトレース（`rig_debug`相当。今回はユーザーが取得したログに`icom_check_ack`
のミスマッチとして残っていた）を確認し、リクエストと応答の対応関係がズレていないかを疑うこと。

### NET mode (rigctld) vs Direct mode (Hamlib built-in)
- FTX-1F: both NET and Direct work; NET preferred (more stable)
  - Direct: `_apply_mode_and_ctcss_cat_ftx1()` — `MD1{ul}/MD0{dl}` via `os.open()`, `CN1/CT1` for CTCSS
  - NET: uses independent socket for mode/CTCSS commands to avoid Doppler cycle conflict
- FT-991 / FT-991A (models 1035/1036): both NET and Direct work (Direct confirmed 2026-06-18)
  - Direct: `_apply_mode_and_ctcss_cat_ft991()` — `SV;MD0{ul};SV;MD0{dl}` via pyserial, `CN0/CT0` for CTCSS
    - pyserial 使用（FTX-1F の `os.open()` と異なりボーレート設定が確実）
    - CTCSS は SV スワップ不要（`CT02` は TX-VFO にグローバル適用）
  - NET: `ctcss_method == "ft991"` で識別。`send_mode_only()` が SV スワップを行う。`send_ctcss_cat()` は SV スワップなし
  - `_FT991_DIRECT_MODEL_IDS = frozenset({1035, 1036})`。main_window.py では `_FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS` を一括判定
- IC-9700 / IC-9100 / IC-910H / IC-821H (satmode rigs): both NET and Direct work (confirmed 2026-06-20, cross-band and same-band)
  - NET satmode detection: `ctcss_method == "icom_civ"` (user setting) — model name query removed
  - NET mode + CTCSS: rigctld TCP commands (`V Sub / C / U TONE / V Main`) — pyserial 廃止、macOS でも動作。tone write command fixed from `L CTCSS_TONE` to `C` 2026-07-15 (confirmed on real IC-9100; `L CTCSS_TONE` returns RPRT -11)
  - NET mode + same-band: `S 1 VFOB` instead of `S 1 Main`; UL throttled to reduce display flicker
  - **IC-910H / IC-821H**: Hamlib がモデル固有 CI-V を自動選択するため同一コードパスで動作するはず（実機未確認）
- Detection: use `ctcss_method` setting value (`"ft991"`, `"icom_civ"`, `"hamlib"`) — **never** use `w ID;` or rigctld `_` command (causes 10 s timeout and socket race)

---

## 仮NORAD ID（90000番台）衛星のTLE・トランスポンダー管理

### 背景

SATNOGS は正式 NORAD ID が未確定の衛星に 90000 番台の仮 ID を割り振る。
これらは CelesTrak グループフェッチでは TLE が取得できず、位置が表示されない。

### TLE 取得方法（src/data/tle_manager.py）

SATNOGS TLE API エンドポイントを使用：
```
GET https://db.satnogs.org/api/tle/?norad_cat_id={fake_id}&format=json
```

このエンドポイントは仮 ID に対して以下の3種類のいずれかを返す：
| tle_source | line1 の NORAD | 意味 |
|---|---|---|
| Space-Track.org | 実 NORAD ID | SATNOGS が内部で実 ID を把握 |
| CelesTrak (supplemental) | 実 NORAD ID | CelesTrak 補完カタログで解決 |
| SatNOGS Team | 仮 ID | 独自生成TLE（精度低め・更新頻度低） |

`fetch_provisional_tles()` は `is_hidden=0 AND norad_cat_id >= 90000` の全衛星を対象に
このAPIを呼び出し、TLE を `source='satnogs'`, `tle_group='amateur'` として保存する。

- 起動時に `_refresh_satellite_names_sync()` 完了後に自動実行
- APScheduler で 12 時間ごとに定期更新
- `source='manual'` の TLE は絶対に上書きしない

### 仮ID→実ID 移行パイプライン（src/data/transmitter_manager.py）

`_run_migration_pipeline(fake_id, real_id)` — **冪等。何度呼んでも安全。**

実行される手順（各ステップはスキップ条件あり）：
1. 実 ID の satellites レコードを作成（なければ）
2. 実 ID の衛星名が `OBJECT *` / `#NNNNN` / `Satellite #NNNNN` 等のプレースホルダー（
   `_is_placeholder_name()`、src/data/transmitter_manager.py）なら SATNOGS 名で上書き。
   以前は `OBJECT *` と `#` 始まりしか認識せず、`sync_from_satnogs()` がトランスミッタの
   `description` をそのまま仮の衛星名として登録していたケース（例: "Mode U - CW"）が
   永遠に上書きされないバグがあった（2026-07-04 修正、詳細は「Comms Quick Panel 設計」
   セクションの「関連する副次修正」参照）
3. TLE を仮 ID → 実 ID へコピー（実 ID 側に manual TLE があればスキップ）
4. トランスミッタを仮 ID → 実 ID へ移行（実 ID 側に既存ならスキップ）
5. `is_favorite` を実 ID にコピー
6. 実 ID 衛星に `satnogs_source_id = fake_id` を記録
7. 仮 ID を `is_hidden = 2`（システム非表示）に設定

#### トリガー
| トリガー | 発火場所 |
|---|---|
| (A) SATNOGS 衛星 API で `norad_follow_id` が設定された | `sync_satellite_names()` |
| (B) SATNOGS TLE API が返す line1 の NORAD が仮 ID と異なる | `fetch_provisional_tles()` |

### `satnogs_source_id` によるシームレスなトランスポンダー同期

移行後も SATNOGS は仮 ID 側でトランスポンダーを管理し続けることがある。
`satellites.satnogs_source_id = fake_id` が設定された実 ID 衛星は、
`sync_from_satnogs()` 内で以下のルーティングが適用される：

```
SATNOGS API に対して satellite__norad_cat_id=fake_id でクエリ
→ 返ってきたトランスポンダーを norad_cat_id=real_id として保存
```

#### 未実装項目（必要性は低いが、将来的な実装を検討すべき）

| 項目 | 内容 |
|---|---|
| **トリガー(C)：GUI手動設定** | 「この衛星の実 NORAD ID は〇〇」とユーザーが GUI から手動指定する機能。トリガー(A)(B) で自動カバーできるケースがほとんどのため現時点では不要。 |
| **フォールバック検知** | SATNOGS 側がトランスポンダーデータを実 ID に移行した場合に `satnogs_source_id` を自動で NULL にリセットする機能。現状では設定されていても実害はなく、SATNOGS が `norad_follow_id` をトランスポンダーに設定した時点で自然に解決される。 |

### 超古い衛星（NORAD < 10000）の自動クリーンアップ（src/data/tle_manager.py）

`fetch_legacy_tles()` — **起動時一回限りのクリーンアップ（以降は高速 no-op）**

対象：`norad_cat_id < 10000 AND is_hidden=0 AND TLEなし` の衛星（最大 21 機）

```
CelesTrak に個別照会（CATNR={norad}&FORMAT=TLE）
  ┌─ TLE 返却あり → まだ軌道上に存在する
  │   source='celestrak', tle_group='legacy' として保存・表示継続
  └─ TLE 返却なし → 軌道離脱済みと判断
      is_hidden=2（システム非表示）に設定
```

- 2回目以降の起動では対象行が 0 件 → 即リターン（API 呼び出しなし）
- `_refresh_satellite_names_sync()` の末尾でプロビジョナルTLEフェッチの後に実行

### ORIGAMISAT-2（NORAD 68795 / 仮 ID 98325）の状態（2026-08-09 実態に合わせて訂正）

```
satellites(norad_cat_id=68795):
  is_hidden = 0             ← 表示中
  satnogs_source_id = NULL  ← 仮IDへのルーティングは既に不要（移行済み・実IDで直接解決できる）
  alt_names = ["JS1YRU", "FO-126"]
  TLE: source=satnogs, tle_group=amateur  ← fetch_active_tles() Phase 2で継続的に自動更新される

satellites(norad_cat_id=98325):
  is_hidden = 2          ← システム非表示
  transmitters = 0件     ← 全て 68795 に移行済み
```

**旧記述（`satnogs_source_id=98325`・`TLE: source=manual`）は誤り・古い状態のスナップショットだった**
（少なくとも2026-08-09時点のDBとは一致しない。いつからずれていたかは不明）。特に`source=manual`は
致命的な誤りで、実際にmanualだった場合はいかなる自動同期でも上書きされないため、本セクション上部で
詳述した「TLEが44日間更新されなかった」問題は`source=manual`の記述が正しければ発生しようがなかった
はずである。ドキュメントと実DBの乖離に気づかないまま「この衛星は最終状態にあり変更不要」と
誤って結論づけていたことが、今回の一連の調査が長引いた一因だった。

**教訓**: DB上の実際の状態を記録したメモは、時間が経つと（今回のように自動同期の挙動変化や
手動操作で）静かに陳腐化する。「この衛星はもう解決済みのはず」という記述を信じて調査をスキップ
するのではなく、疑わしい挙動が報告されたら実DBを直接クエリして前提を検証すること。

---

## TLE 取り込みルール全体設計（2026-05-29 確定）

### TLE ソース一覧と優先度

| 関数 | ソース | 対象 NORAD 範囲 | 更新頻度 | source 値 | tle_group 値 |
|---|---|---|---|---|---|
| `fetch_and_update('celestrak-stations')` | CelesTrak STATIONS | ISS・CSS 等 | 1時間ごと | `celestrak` | `stations` |
| `fetch_and_update('celestrak-amateur')` | CelesTrak AMATEUR | アマチュア衛星 | 2時間ごと | `celestrak` | `amateur` |
| `fetch_and_update('celestrak-cubesat')` | CelesTrak CUBESAT | CubeSat | 4時間ごと | `celestrak` | `cubesat` |
| `fetch_and_update('celestrak-weather')` | CelesTrak WEATHER | 気象衛星 | 6時間ごと | `celestrak` | `weather` |
| `fetch_and_update('celestrak-earth-obs')` | CelesTrak RESOURCE | 地球観測 | 12時間ごと | `celestrak` | `earth-obs` |
| `fetch_and_update('celestrak-science')` | CelesTrak SCIENCE | 科学衛星 | 12時間ごと | `celestrak` | `science` |
| `fetch_active_tles()` | CelesTrak(複数グループ)+SATNOGS TLE API | 10000-89999・未収録 | 24時間ごと(起動時stale確認) | `celestrak` or `satnogs` | `amateur`(INSERT時) / 既存保持(UPDATE時) |
| `fetch_provisional_tles()` | SATNOGS TLE API | NORAD ≥ 90000 | 12時間ごと | `satnogs` | `amateur` |
| `fetch_legacy_tles()` | CelesTrak 個別照会 | NORAD < 10000 | 起動時1回のみ | `celestrak` | `legacy` |
| `add_manual_tle()` | ユーザー手動入力 | 任意 | 手動 | `manual` | `amateur` |

### 上書きルール（優先度）

```
manual（最高優先）> celestrak > satnogs > なし
```

- `source='manual'` の TLE は **いかなる自動同期でも上書きしない**
- 既存 TLE が `celestrak` の場合、`satnogs` ソースの取得結果で上書きしない
  （`fetch_provisional_tles()` は `INSERT OR REPLACE` だが `source='manual'` チェックで防御）
- `fetch_active_tles()` の UPDATE では `tle_group` を保持（分類を劣化させない）
- **起動時の未フェッチ/期限切れソース自動検出**: `TLEManager.is_source_stale(source_name)` が `sync_log` 未記録、または各ソース自身の `update_interval_hours` より古いソースを `True` で返す（2026-08-11 修正、旧実装は未記録のみ検出）→ MainWindow が起動時に対象グループを即時フェッチ
- **フェッチ順序制御**: `MainWindow._sort_sources_by_priority()` が `TLE_SOURCES["priority"]` 昇順でソート。`amateur`（汎用）を先にフェッチし、`cubesat`/`weather` 等がその後に上書きするよう保証

### tle_group と UI フィルタの対応

| tle_group 値 | UI フィルタ | 用途 |
|---|---|---|
| `amateur` | Amateur | アマチュア衛星全般（SATNOGS 登録衛星のデフォルト） |
| `cubesat` | CubeSat | CelesTrak CUBESAT グループ由来 |
| `weather` | Weather | 気象衛星 |
| `earth-obs` | Earth Observation | 地球観測衛星 |
| `science` | Science | 科学衛星 |
| `stations` | Space Stations | ISS・CSS 等 |
| `legacy` | Amateur | NORAD < 10000 の古い衛星（COALESCE で Amateur 扱い） |
| `NULL` | Amateur | TLE なし衛星（`COALESCE(tle_group, 'amateur')` でデフォルト適用） |

### TLE なし衛星の自動非表示ルール

`fetch_provisional_tles()` および `fetch_active_tles()` の Phase 2 で適用：

```
TLE が取得できなかった場合:
  status = 'unknown' or 'dead'  → 即時 is_hidden=2
  status = 'alive'
    tle_no_result_since が NULL  → 今日の日付を記録（猶予開始）
    30日以内                     → 紫イタリックで表示継続
    30日超過                     → is_hidden=2（自動非表示）

TLE が取得できた場合:
    tle_no_result_since を NULL にリセット（紫解除）
```

### fetch_active_tles() の2フェーズ設計（2026-08-11 改訂）

**Phase 1 — CelesTrak `GROUP=active` 一括取得（単一リクエスト）**

2026-08-09時点では「`satnogs`・`last-30-days`・`argos`・`orbcomm`・`spire`の5グループを個別に
取得する」設計だったが、2026-08-11に`GROUP=active`（全15,000機超を1リクエストで返す）へ切り替えた。
アクセス可能なグループを順に取得し、DB にある衛星のみ保存する（新規衛星レコードは作成しない）点は変わらない。

**方針転換の経緯（2026-08-09 見送り → 2026-08-11 採用）**: 2026-08-09時点の検討では
「`GROUP=active`は約16,000機（約2.7MB）を毎日丸ごとダウンロードすることになり、必要な分（1,482機）の
約11倍のデータを取得する計算になる」という理由で見送っていた。しかしその後もこのアプリのIPが
CelesTrak/SATNOGSに繰り返しブロックされる問題が実際に続いたため、「必要な分だけ取得する」という
データ量優先の原則そのものを再検討した。CelesTrakが公式に問題視しているのは
**HTTPエラー数（2時間で50件超えでファイアウォール）であって、正当にダウンロードしたバイト数ではない**。
5グループの個別取得はリクエスト数自体は5件と少なかったが、その先のPhase 2（衛星ごとの個別CATNR/SATNOGS
問い合わせ、1回のフルスキャンで800機超に達することもあった）が実質的なエラー源になっていた——
`GROUP=active`は精選済みグループに載っていないだけの多数の衛星も一度に解決してしまうため、
Phase 2に落ちる衛星の数を数百→ごく少数に減らせる。「1回で成功すればエラー0件」という単一リクエストの
強みは、必要な分だけを細切れに取りに行く方式より実際のブロック回避には効く、という判断で採用した
（ユーザー判断、2026-08-11）。

**`GROUP=active`固有の注意点（2h キャッシュと403の誤判定回避）**: CelesTrak側は`GROUP=active`の
レスポンスを約2時間ごとにしか更新しない。この間隔内に同一クライアントから2回目のリクエストを送ると、
本文に理由が明記された403が返る（`GP data has not updated since your last successful download of
GROUP=active at ...`）。これは不正アクセスとして弾かれたのではなく「前回と内容が変わっていない」
という通知に近い。`fetch_active_tles()`自体は最短でも24時間に1回（または3時間バックオフ後の
リトライ）しか自動実行されないため通常はこの2時間制約に触れないが、**「Satellite > Update TLE」
ボタンとSettings > OKは`is_active_tle_stale()`の鮮度ゲートを意図的にバイパスして即時実行する**
（本ファイル「起動時の TLE 同期フロー」参照）ため、これを2時間以内に連打すると発生しうる。
`_is_active_cache_not_yet_updated(response_text)`（`tle_manager.py`）が403応答本文にこの特定の
文言が含まれるかを判定し、含まれていれば**サーキットブレーカーに一切記録せず**（`celestrak_blocked`も
立てない）静かにスキップする。この判定がないと、正当な「まだ更新されていません」応答を実際の
ブロックと誤認し、3時間バックオフのリトライ予約と「ブロックされました」というユーザー向け表示を
誤発生させてしまう。

**Phase 2 — SATNOGS一括ダンプ1回（Phase 1で解決できなかった衛星向け、`_fetch_satnogs_bulk_tles()`）**

**2026-08-11、同日中に2段階の改訂を経て現在の形になった。** 最初はPhase 1の`GROUP=active`化
（上記）に合わせて、Phase 2も「CelesTrak個別CATNR（Phase 2a）→SATNOGS個別（Phase 2b）」の
2段構成のまま、並列数削減・ペーシング・User-Agent付与だけで対策する設計だった。しかしユーザーから
「そもそもSATNOGS側で一括取得できるなら、Phase 2a（CelesTrak個別）自体が要らないのでは。Phase 2は
元々CelesTrakに正式掲載されていない仮ID衛星を拾うためのものだったはず」という指摘があり、
実際に`GET https://db.satnogs.org/api/tle/?format=json`（`norad_cat_id`パラメータなし）を
ライブテストしたところ、**ページネーションなしで全件（1,670件、約512KB）が1回のリクエストで
返る**ことが判明した。しかもこのダンプには実ID帯（<90000）・仮ID帯（≥90000）の両方が混在しており、
Phase 2aの存在理由そのものだった3つの実例（NOAA 18/19＝CelesTrakの`GROUP=WEATHER`から外れた
衛星、ORIGAMISAT-2、ARICA-2の`satnogs_source_id`ルーティング）が**すべてこのダンプ単体で
解決可能**（`tle_source: "Space-Track.org"`）と確認できた。この検証結果を受け、Phase 2a
（CelesTrak個別CATNR）を完全に廃止し、Phase 2bと`fetch_provisional_tles()`（旧: 個別に
SATNOGSへ問い合わせていた仮ID専用メソッド）を、この一括ダンプ1回を共有する設計に統合した
（ユーザー判断、2026-08-11）。

`TLEManager._fetch_satnogs_bulk_tles()`が単一の情報源:
- `GET https://db.satnogs.org/api/tle/?format=json`（フィルタなし）で全件取得し、
  `{norad_cat_id: レコード}`の辞書を返す
- **個別問い合わせへのフォールバックは意図的に持たない**。失敗したらこの回のPhase 2は
  諦める（`errors`に計上・`satnogs_blocked`はブレーカーの状態を反映）だけで、
  「一括が失敗したら1件ずつ試す」という設計には**しない**——それをやると今回排除した
  「数百リクエストの個別ループ」問題がそのまま復活してしまうため
- **`TLEManager`インスタンス内で10分間キャッシュ**（`_SATNOGS_BULK_CACHE_TTL`）。
  `fetch_active_tles()`のPhase 2と`fetch_provisional_tles()`は起動シーケンス内で
  数秒〜十数秒しか離れずに呼ばれるため、キャッシュがないと同じ約512KBのダンプを
  毎回2回ダウンロードすることになる
- **SATNOGS側はこの無フィルタ問い合わせを公式のバルクモードとして文書化していない**
  （`/api/satellites/`・`/api/transmitters/`のようなページネーション付きバルク取得とは違い、
  この挙動はドキュメントではなくテストで確認したもの）。成功直後に即座に再リクエストすると
  HTTP 500が返ることを確認済み（数秒空けての再試行では安定して200が返った）。原因・再発条件は
  未確定のため、単発の一時的エラーとして扱っている（ブロックとは区別。ブロックの判定は
  429のみ）
- `satnogs_source_id`が設定されていればそちらのキーで、無ければ実NORAD IDで辞書を引く
  （詳細は後述の「仮ID→実ID移行」参照）

`fetch_active_tles()`のPhase 2は、Phase 1後もTLEが無い、または既存TLEの`source`が`'satnogs'`
（＝以前このPhase 2で取得したもの）の`10000-89999`衛星が対象。**`source='satnogs'`も対象に
含めるのが重要**——そうしないと、Phase 1でカバーされない衛星（ORIGAMISAT-2など）は最初に
TLEを取得できた時点で`tle_data`行を持ってしまい、以降は「TLE行が既にある」という理由だけで
永久にこのPhase 2から除外され、二度と更新されなくなる（下記「発覚した重大バグ」参照）。

保存時の`source`は`'satnogs'`固定（実際にはCelesTrakが最初に発見した衛星がPhase 1側で既に
`'celestrak'`として保存されているため、Phase 2に来る時点でSATNOGS由来のデータであることが
前提）。これは`tle_data.source`のCHECK制約に`'celestrak-catnr'`のような専用値が無いためだけで
なく、この列がこのメソッド自身のWHERE句で「Phase 1未カバーなので毎回リトライすべき対象」を
判定する目印としても使われているため、意図的な設計。

**サーキットブレーカー**: `_ErrorCountBreaker`はプロセス内累積カウンタではなく
**CelesTrakの実ポリシー通り2時間のローリングウィンドウ**で、かつ**`TLEManager`インスタンスの
生存期間全体で共有**（`fetch_and_update()`のグループ取得・Phase 1・Phase 2（SATNOGS一括）・
`fetch_legacy_tles()`・`fetch_meteor_tles()`がCelesTrak用ブレーカーを、Phase 2と
`fetch_provisional_tles()`がSATNOGS用ブレーカーを共有する。CelesTrak用とSATNOGS用は
完全に独立しており、一方がブロックされてももう一方は影響を受けない——`GROUP=active`が
403でブロックされていても、Phase 2のSATNOGS一括取得は正常に試みられる）。以前は各呼び出しが
毎回新規ブレーカーを生成しており、起動直後にこれらが連続実行されると、それぞれ独立した
20エラーの猶予を使い切るまで誰も気づけない構造だった。「接続自体ができない」
（`ConnectTimeout`/`ConnectError`）は、CelesTrak/SATNOGS双方について明示的な403/429と
同様に`blocked=True`として即座にブレーカーを倒す（このアプリの実際の被ブロック時の症状は
「HTTP応答が返る」よりも「接続要求がサイレントに破棄されタイムアウトする」ことの方が多いと
判明済みのため。本ファイル「SATNOGS・CelesTrakに接続できない時は...」参照）。`ReadTimeout`/
`RemoteProtocolError`（接続はできたが遅い/切れた）は個別失敗として扱い、即座にはブロック
扱いしない。

**識別可能なUser-Agent**（`FBSAT59/<version> (+https://github.com/JF9SOM/FBSAT59)`、
`src/data/http_client.py`）をCelesTrak/SATNOGS双方への全リクエストに付与。CelesTrak/SATNOGS
双方の利用ポリシーが、大量アクセスする側に連絡先入りの識別子を推奨している。

**起動時、SATNOGSトランスミッタ同期と衛星名/TLE同期チェーンを別スレッドで並行実行するのをやめ、
1スレッドで順番に実行**するよう変更済み（`main_window.py`の`_start_scheduler()`）。どちらも
db.satnogs.orgにアクセスするため、同時に2本のコネクションから畳みかけるより1本の定常的な
流れにした方が不正アクセスと区別しやすい。

**発覚した重大バグ（2026-08-09 発見・修正、GitHub上のやり取りではなくユーザー実機でのTLE不整合報告から）**:
Phase 2の対象条件が元々「TLE行が一つも無い衛星」のみだったため、Phase 1のどのグループにも載らない
衛星（ORIGAMISAT-2 / NORAD 68795 等）は、最初にTLEを取得できたその瞬間から「TLE行が既にある」と
みなされ、**以降どれだけ古くなっても二度と自動更新の対象に選ばれなくなる**という「一度きりのラチェット」
状態に陥っていた。ORIGAMISAT-2は実際に44日間（2026-06-27〜2026-08-09）更新されないまま放置され、
実際のAOS予測が約20分ずれるという実害が発生した（詳細な原因調査・パス予測誤差との相関は
本ファイル該当セクション参照）。同種の一発ラチェットに陥っていた衛星は、過去の3回のPhase 2単発実行
だけで約700機に達していた。

修正: Phase 2の対象条件を「TLE行が無い」OR「既存TLEの`source`が`'satnogs'`」に拡張。あわせて、
Phase 2の`INSERT OR REPLACE`が毎回`tle_group`を`'amateur'`に強制リセットしていた副次バグ（分類の劣化）
も、既存の`tle_group`を保持するよう修正済み。

**ダウンロード進捗のパーセント表示（2026-08-11、`_get_with_progress()`）**: Phase 1・Phase 2とも
1回のリクエストで完結する一括ダウンロードになったため、「今どのくらい進んでいるか」を`Content-Length`
ヘッダーと実受信バイト数から計算してステータスバーに表示できないか検討し、実装した。SATNOGSの
一括ダンプは実測で`Content-Length: 523906`が返ることを確認済み（本ファイル前述の一括エンドポイント
検証時）。

- `client.get(url, params=...)`（レスポンス全体を受け取ってから返る）を`client.stream("GET", url,
  params=...)`によるストリーミング受信に置き換えた`_get_with_progress()`を新設。Phase 1
  （`GROUP=active`）とPhase 2（SATNOGS一括ダンプ`_fetch_satnogs_bulk_tles()`）の両方が共通で使う
- `response.aiter_bytes()`でチャンクを受信するたびに`Content-Length`との比率からパーセントを計算し、
  1%刻みで変化があったときだけ`progress_callback(f"{label}: downloading... {pct}%")`を呼ぶ
  （毎チャンクそのまま呼ぶとステータスバーの更新が多すぎるため）
- **表示形式はパーセントのみ**（例: `SATNOGS: downloading... 45%`）。KB数の併記は情報過多と判断し
  見送った（ユーザー判断、2026-08-11）
- ストリーミングで受信し切ったレスポンスは、`async with client.stream(...) as response:`の
  ブロックを抜けた後も`.text`/`.json()`/`.raise_for_status()`がそのまま使える（httpxはストリームを
  最後まで読み切った時点で内容をキャッシュする仕様のため）。これにより呼び出し元の既存の
  例外処理コード（`_is_active_cache_not_yet_updated(exc.response.text)`等）は無変更で動く
- `Content-Length`ヘッダーが無い場合（chunked転送等）は、パーセント無しの`"{label}:
  downloading..."`を1回だけ表示するフォールバックにした
- **`fetch_provisional_tles()`には配線していない**。この関数の`progress_callback`は
  `(done: int, total: int)`という「衛星件数」を表す別のシグネチャ（`_fetch_satnogs_bulk_tles()`が
  使う文字列1個のシグネチャとは非互換）で、`main_window.py`側の`_prov_progress(done, total)`に
  直接バイト数を渡すと衛星件数と誤解される表示になってしまうため。実運用では`fetch_active_tles()`
  が先に呼ばれてキャッシュを温めるため、`fetch_provisional_tles()`が実際にバルクダウンロードを
  行う機会自体まれ（本ファイル前述のキャッシュ機構参照）という判断もあった

### 起動時の TLE 同期フロー（2026-08-10 順序変更・2026-08-11 鮮度ゲート監査で更新）

`MainWindow._start_scheduler()` が起動直後に以下を行う（`_no_background_sync` テストフィクスチャで
まるごと無効化される、CI上で毎回ネットワークを叩かないための唯一の入口）:

```
アプリ起動 → _start_scheduler()
  │
  ├─ APScheduler 開始（下記7ジョブを "interval" trigger で登録。misfire_grace_time付き。
  │   interval ジョブは登録直後には発火せず、登録時点から丸1区間経過して初めて実行される点に注意
  │   ——このため、起動時に「区間経過チェック」を別途行わないと、7区間ぶんの継続起動をしない限り
  │   一度も自己修復されない。2026-08-11 の監査でこの欠落を3件発見・修正した（後述）
  │
  ├─ [即時] AMSAT運用状況: AmsatFetcher.is_stale(24h) が True ならバックグラウンドで再取得
  │
  ├─ [即時] SATNOGSトランスポンダーDB: `source='satnogs'`行が0件、または
  │   TransmitterManager.is_satnogs_transmitters_stale(168h) が True ならバックグラウンドで再取得
  │   （後者は2026-08-11追加。以前は0件チェックのみで、初回同期後は168hジョブの発火待ちのみだった）
  │
  ├─ [バックグラウンド] _refresh_satellite_names_sync()  ← 以下を直列に実行
  │     1. sync_satellite_names()    ← SATNOGS 衛星名・ステータス更新・移行パイプライン
  │          ゲート: TransmitterManager.is_satellite_names_stale(24h)（2026-08-13追加。
  │          以前は無条件で毎回実行しており、約2700件のページネーション一括取得
  │          （`/api/satellites/?format=json`、TLE一括ダンプ`/api/tle/?format=json`とは
  │          別エンドポイント）を再起動のたびにフルで再実行していた）
  │     2. fetch_active_tles()       ← NORAD 10000-89999 衛星の TLE 補完
  │          ゲート: is_active_tle_stale(24h) OR is_active_tle_retry_due()
  │     3. fetch_provisional_tles()  ← NORAD ≥ 90000 衛星の TLE 取得
  │          ゲート: is_provisional_tle_stale(12h)（2026-08-11追加。以前は無条件で毎回実行）
  │     4. fetch_legacy_tles()       ← NORAD < 10000 衛星のクリーンアップ（対象0件ならno-op、毎回無条件）
  │     5. fetch_meteor_tles()       ← METEOR/HRPT 衛星の TLE 補完（衛星ごとneeds_update(24h)、毎回無条件）
  │     6. load_community_transmitters() ← ローカルJSON読み込み（ネットワーク不要、毎回無条件）
  │     7. CelesTrak 6グループ一括フェッチ（stations/amateur/cubesat/weather/earth-obs/science）
  │          ゲート: 各グループごとに is_source_stale(グループ自身のupdate_interval_hours) OR
  │                  is_group_empty()（2026-08-11、is_source_stale側を修正。以前は
  │                  「一度もフェッチしていないか」のみ判定し、経過時間を見ていなかった）
  │
  ├─ [バックグラウンド] DE421 天体暦ロード（Moon/EME追尾用、初回のみ約17MBダウンロード）
  │
  └─ [バックグラウンド] NTPクロック同期チェック
```

**ゲートの意味（起動のたびに毎回実行 vs 鮮度チェック後にのみ実行）**: 上記のうち
「毎回無条件」と書いたステップ（4・5・6）は、対象データ自体が自己制限的
（空なら即return・衛星ごとの内部staleness判定を持つ・ネットワーク不要のローカル処理）なため、
無条件に呼んでも実害がない設計。一方「ゲート: ...」と明記したステップ（1・2・3・7、および
AMSAT・SATNOGSトランスポンダーDB）は、**もし鮮度チェックを誤ると「毎回ネットワークを叩きすぎる」
（ブロックの原因になる）か「二度と更新されない」のどちらかに転ぶ**ため、専用の`is_*_stale()`系
メソッドで経過時間を明示的に判定している。

**`sync_satellite_names()`（ステップ1）が唯一ゲートを持たず、再起動のたびにフル再実行されていた
不具合（2026-08-13 発見・修正）**: 上記の項目6（CelesTrak不通時のSATNOGS巻き込まれ対策）を
実装した直後、ユーザーが実機で「一度起動・動作確認・終了・再起動」を繰り返したところ、
毎回SATNOGSからの取得（ログに`GET https://db.satnogs.org/api/satellites/?format=json`と
`SATNOGS satellite names sync completed: {'updated': 2766, 'skipped': 1}`）が走っているように
見える、という報告があった。実際にログを確認したところ、TLE本体の一括取得
（`fetch_active_tles()`のPhase 2・`fetch_provisional_tles()`）は`Active TLE cache is fresh
— skipping fetch.`/`Provisional TLE cache is fresh — skipping fetch.`と正しく鮮度キャッシュで
スキップされていたが、**`sync_satellite_names()`だけは元々ゲート自体が存在せず**、SATNOGSに
到達可能な限り毎回無条件でフルページネーション同期していたことが判明した（起動ごとに約10〜45秒、
ネットワーク状況依存）。

`sync_satellite_names()`は名前・ステータス（alive/dead/unknown）・エイリアス名を更新するだけで
TLEにもトランスミッターDBにも触れないが、TLE取得だけでは代替できない役割を持つ（CelesTrakの
仮名"OBJECT C"等を正式名称で上書き・死亡衛星の自動非表示判定・仮ID→実ID移行の検知起点）ため、
省略はできない。ユーザーとの相談の結果、他の起動ステップと同じ24時間鮮度ゲートを追加することで
合意した。

**修正**: `TransmitterManager.is_satellite_names_stale(max_age_hours=24.0)`を新設
（`is_satnogs_transmitters_stale()`と全く同型、`sync_log`の`sync_type='satnogs_names'`最新
エントリを参照）。`_refresh_satellite_names_sync()`のステップ1をこのゲートで囲んだ
（`_refresh_satellite_names_periodic()`——6時間ごとのAPSchedulerジョブ——は間隔自体がゲートの
役割を果たすため対象外のまま）。

**あわせて発覚した欠落: 手動同期メニューが存在しなかった**: 24時間ゲートを追加するにあたり、
既存の`Satellite → Sync SATNOGS`（`_on_sync_satnogs()`、2026-08-13に`Fetch Transmitter Database`
へ改名。後述参照）が実は**トランスミッターDB
（`/api/transmitters/`）専用**で、`sync_satellite_names()`（`/api/satellites/`、別エンドポイント・
別テーブル）を手動で即時実行する手段がそもそも存在しないことが分かった。既存ボタンに混ぜる案も
検討したが、①データの性質が別物 ②`sync_satellite_names()`は約2700件のページネーションで
10〜45秒かかるのに対し既存のトランスミッター同期は数秒で終わる——混ぜると「周波数だけ更新したい」
操作が毎回巻き込まれて遅くなる ③失敗時にどちらが失敗したか切り分けにくくなる、という理由で
**`Satellite → Sync Satellite Names`（`衛星名の同期`）を新規メニュー項目として追加**した
（`_on_sync_satellite_names()` → `_refresh_satellite_names_manual_sync()`、`_on_sync_satnogs()`
と同型・同じ`_satnogs_status`シグナルを共用）。明示的なボタン押下なので`is_satellite_names_stale()`
は意図的にバイパスする（Update TLEボタンが`is_active_tle_stale()`等をバイパスするのと同じ設計）。

**`Sync SATNOGS`を`Fetch Transmitter Database`へ改名（2026-08-13）**: 上記の`Sync Satellite
Names`メニュー追加により、「SATNOGS」という語自体がトランスミッターDB・衛星名・TLE（`fetch_active_tles()`
のPhase 2・`fetch_provisional_tles()`）という複数の別々の同期処理を指すようになり、
「`Sync SATNOGS`」という名前だけでは**どのSATNOGS同期を指しているのか判別できない**という
分かりにくさをユーザーが指摘した。英語ラベルも含めて改名するか確認した上で、
`_("Sync SATNOGS")` → `_("Fetch Transmitter Database")`に変更（`_on_sync_satnogs()`自体・
内部コメント・Auto Fetch Rulesダイアログの本文中の参照も含めて統一）。日本語訳は
「SATNOGSと同期」→「トランスミッターDBを取得」。ハンドラ名`_on_sync_satnogs()`・
`_refresh_satnogs_sync()`自体はSATNOGS APIのエンドポイント名（`/api/transmitters/`由来）との
対応が分かりやすいため変更していない——ユーザーに見える文言（メニューラベル・ヘルプ本文）だけを
改名の対象とした。

**教訓（i18nと機能追加が絡む改名の手順）**: Auto Fetch Rulesダイアログの日本語訳には、
`<b>Satellite → Sync SATNOGS</b>`のようにメニューパスを**英語のまま埋め込んだ**箇所が複数
あった（実際のメニュー表示は日本語の「衛星」「トランスミッターDBを取得」等）。これは
msgmergeが英語の`msgid`をそのまま流用して訳文を生成する性質上、翻訳者（Claude）が
明示的に「メニューパス部分も日本語のメニュー名に置き換える」意識を持たない限り機械的に
見過ごされやすい。この種のヘルプ本文にUIのメニューパスを埋め込む場合、**英語版の
`msgid`はそのメニューの実際の英語ラベルと一致させ、日本語版の`msgstr`も対応する日本語
メニューラベル（`_("...")`の実際の訳語）に置き換える**ことを徹底すること。

**「Cannot connect to CelesTrak」表示直後に、消えない「CelesTrak blocked — retry in 3h」が
出る不具合（2026-08-13 発見・修正）**: 上記の`celestrak_reachable`/`satnogs_reachable`対応
（同日先行の修正）実装後、実際にCelesTrakがブロックされている実機環境で手動「Update TLE」を
押したユーザーから、①冒頭の`❌ Cannot connect to CelesTrak`（約3秒）→②SATNOGS側の正常な
取得メッセージ群→③`⚠ CelesTrak blocked — retry in 3h`が表示されて**消えなくなる**、という
報告があった。「①で既にCelesTrakへ再接続しないと分かっているはずなのに、なぜ③が出るのか」
という指摘は正確だった。

**原因**: `fetch_active_tles(celestrak_reachable=False)`は実際にはCelesTrakへ再接続しない
（③のメッセージが「今まさに新しく失敗した」ことを意味するなら、それは誤り）が、内部的には
「本当に接続を試みて失敗した」場合と**全く同じように**`_celestrak_breaker.record_error(blocked=True)`
を呼ぶ設計にしていた（リトライスケジューリングの`stats["celestrak_blocked"]`を正しく機能
させるための意図的な設計。前回の修正コミット参照）。この結果`_schedule_active_tle_retry_if_blocked()`
が「新たにブロックされた」と誤認し、①と実質同じ情報を**別の文言で二重に**表示していた。
さらにこの関数がメッセージを表示すると、呼び出し元の`_fetch_all_tle_sources()`が持つ
「最後に表示をクリアする」ガード（`if not blocked: emit("")`）もスキップされるため、③が
そのまま画面に残り続けていた。

**修正**: `_schedule_active_tle_retry_if_blocked()`に`already_reported: set[str] | None`引数を
新設。呼び出し元（`_fetch_all_tle_sources()`・`_refresh_satellite_names_sync()`）が、その回の
実行冒頭で既にユーザーへ「Cannot connect to X」と伝えたホスト集合（`down`）をそのまま渡す。
`already_reported`に含まれるホストは表示対象から除外する（**リトライのスケジュール自体は
除外せず引き続き行う**——CelesTrakが実際にまだ落ちているという状態自体は変わらないため）。
関数は「実際に新規メッセージを表示したか」を`bool`で返すようになり、呼び出し元はこれを
（従来の生の`blocked`フラグの代わりに）最後の「表示クリア」判定に使う。これにより、
既に報告済みのホストしかブロックされていない場合は③が一切表示されず、ステータスバーは
通常通りクリアされる（両方新規にブロックされた場合や、片方だけ新規にブロックされた場合は、
新規分のみを名指しして引き続き表示する）。

**教訓**: 「実際には行っていない処理を、内部の統計・状態管理のためだけに『行ったことにする』」
設計（今回は`record_error(blocked=True)`）は、その統計を消費する**下流の全ての箇所**
（今回はUIメッセージ表示）が、この「実際には起きていない」という文脈を認識できるとは限らない。
統計値だけを見て「新しい出来事が起きた」と解釈するコードには、常にこの種の誤検知のリスクが
残る。

**Auto Fetch Rulesダイアログに「SATNOGSからのTLEデータ」セクションを追加（2026-08-13）**:
上記の一連のTLE同期フロー質問に答える過程で、`Active TLE fallback`・`Provisional TLEs`
の2行は自動取得スケジュール表に時間だけ載っており、これらが（CelesTrakの分類済み一覧
とは別に）SATNOGS自身のTLEデータベースを使うフォールバック処理であることを説明する
専用セクションが無いことが判明した（「衛星名/ステータス」「トランスミッターDB」には
専用セクションがあるのに、これだけ無かった）。両セクションと同じ構成（見出し＋説明段落）
で「TLE Data from SATNOGS」セクションを追加し、`Satellite → Update TLE`で両方を即時更新
できる旨も記載した。

**自動取得スケジュール表を「CelesTrak TLE」/「CelesTrak TLE以外の取得」の2表に分割
（2026-08-13）**: 単一の表に、CelesTrakの分類済みグループ一括取得（Space Stations・
Amateur Satellites・CubeSats・Weather Satellites・Earth Observation/Science・
METEOR/HRPT）と、それ以外の取得元（Active TLE fallback・Provisional TLEs・
Satellite Names/Status・AMSAT・Transmitter Database）が混在しており、どの行がどこから
取得しているのか分かりにくいという指摘を受けて分割した。`Active TLE fallback`は
実際にはCelesTrakの`GROUP=active`一括取得が主でSATNOGSへのフォールバックは補完的
（同一行内でユーザーとの相談の上、直前に追加した「TLE Data from SATNOGS」セクションの
分類（Provisional TLEsと同じ「SATNOGSへフォールバックする処理」）と一貫性を取るため
下側の表に分類することで合意した）。

**進捗メッセージ全般を「何をどこから取得しているか」が分かる文言に見直し（2026-08-13）**:
取得中にステータスバー下部へ表示される一連のメッセージを再点検した結果、複数箇所で
「どこから何を取得しているか」が読み取れない文言になっていたことが判明した。

- `sync_satellite_names()`の進捗（起動時）: `"Syncing satellites from SATNOGS..."` →
  `"Syncing satellite names from SATNOGS..."`（手動同期メニューの文言と統一。「satellites」
  だけだとTLE取得と紛らわしい）
- `fetch_active_tles()`のPhase 1開始通知: `"CelesTrak active..."` →
  `"CelesTrak: fetching active TLEs..."`（何のために接続しているか不明だった）
- `_get_with_progress()`のダウンロード進捗（Phase 1・Phase 2共通）:
  `"{label}: downloading... {pct}%"` → `"{label}: downloading TLE data... {pct}%"`
- `fetch_active_tles()`のPhase 2開始通知: `"SATNOGS: {n} satellite(s)..."` →
  `"SATNOGS: fetching TLE data for {n} satellite(s)..."`（名詞の羅列だけで動詞が無く、
  進行中なのか完了なのかも分からなかった）
- CelesTrak 6グループ一括フェッチの進捗（起動時・Update TLEボタン両方）:
  内部ソース名（例: `"celestrak-amateur"`）をそのまま表示していたのを、Settings画面の
  TLE Sourcesタブが既に持っていた表示名（例: `"Amateur Satellites (CelesTrak)"`）に
  統一。この表示名辞書はこれまで`settings_dialog.py`内のプライベート定数
  `_SOURCE_DISPLAY_NAMES`として重複しかねない形で存在していたため、
  `data.tle_manager.TLE_SOURCE_DISPLAY_NAMES`として公開・一本化し、両画面で共有する
  ようにした
- **Update TLEボタン（`_fetch_all_tle_sources()`）のCelesTrak 6グループフェッチ中、
  そもそも進捗メッセージが一切出ていなかった**ことも判明（起動時側の同種ループには
  既にあったが、Update TLE側には元から実装されていなかった）。同じ形式の進捗表示を
  追加した

**教訓**: 「進捗メッセージがある」ことと「そのメッセージが実際に分かりやすい」ことは別。
今回見つかった問題の多くは、メッセージ自体は存在するが、動詞が欠けている・データの種類
（TLE）が省略されている・内部識別子をそのまま表示している、という**質**の問題だった。
新しい進捗メッセージを追加する際は「これだけを見て、何がどこから取得されているか
第三者が分かるか」を基準にすること。

**2番目のステップだった `fetch_active_tles()` を最優先に変更**（2026-08-10）:
以前は「Phase 2のSATNOGSフォールバックが20〜30分かかりうるので他のステップを待たせない」という
理由で最後に実行していたが、この理由はPhase 2にサーキットブレーカー・並列化・CelesTrakフォールバックを
入れた今となっては古い（前述の各節参照）。一方で、この処理こそが通常のNORAD ID（例: ORIGAMISAT-2、
NORAD 68795）のTLEを実際に最新化する、最も価値の高いステップである。ステップ間に進捗表示が一切なかった
ため、ステップ1完了後にステータスバーの表示が更新されなくなると、ユーザーからは「フリーズした」ように
見え、実際には正常に動作中の後続ステップの途中でアプリを閉じてしまう、という報告が複数回の再起動を
経ても`fetch_active_tles()`に一度も到達できないという実害につながった（2026-08-10）。`fetch_active_tles()`
に`progress_callback`引数を新設し、フェーズ（CelesTrakグループ名・Phase 2a/2bの対象数）ごとに
ステータスバーへ進捗を表示するようにした上で、最優先の位置に移動した。

**「Satellite > Update TLE」ボタンがそもそも`fetch_active_tles()`を一度も呼んでいなかった不具合
（2026-08-10 発見・修正）**: 上記の順序変更・進捗表示を実装した v0.3.8 をWindows実機で検証した
ユーザーから、「1時間放置しても、手動でUpdate TLEボタンを押しても、ORIGAMISAT-2のTLEだけは
更新されない。ボタンを押した後にTLE更新時刻自体は進んでいるので、他の衛星は更新されているはず」
という報告があった。ログ（`fbsat59.log`）を解析したところ、`_fetch_all_tle_sources()`
（Update TLEボタン・Settings > OKの両方が共有する実装）は`SettingsDialog.get_enabled_sources()`
が返す**CelesTrakの決め打ちグループ6種**（stations/amateur/cubesat/weather/earth-obs/science）
を`fetch_and_update()`でループするだけで、**`fetch_active_tles()`（Phase 2の個別問い合わせで
ORIGAMISAT-2を解決する処理）を一切呼んでいなかった**ことが判明した。ORIGAMISAT-2はこの6グループの
どれにも属さない（2026-08-09に実際に問い合わせて確認済み）ため、**このボタンを何度押しても、
ネットワークが完全に正常であっても、原理的に一生解決できない**設計だった。ユーザーが「更新時刻は
進んだのに対象衛星だけ変わらない」と正確に見抜いた通りの状況で、実際にログにもその瞬間の6グループ
（`celestrak-stations/science/amateur/weather/cubesat/earth-obs`）の成功結果のみが記録されており、
`fetch_active_tles()`関連のログ行は一切存在しなかった。

**修正**: `_fetch_all_tle_sources()`の末尾に`fetch_active_tles(progress_callback=...)`の呼び出しを
追加。`is_active_tle_stale()`の24時間ゲートは意図的にバイパスする（バックグラウンドの定期実行とは
異なり、ユーザーが明示的に「今すぐ更新して」とボタンを押した以上、鮮度キャッシュより即時実行を
優先すべきと判断）。この関数はSettings > OKとUpdate TLEの両方から共有されているため、両方の
経路で同時に直る。

**教訓**: 「更新ボタンを押しても直らない」という報告を受けた際、TLE取得ロジック自体（Phase 2の
サーキットブレーカーやCelesTrak個別問い合わせ）を疑う前に、**そもそもそのボタンが正しい関数を
呼んでいるか**を確認すべきだった。今回はPhase 2側の実装は（この時点で）既に正しく動作していたが、
呼び出し経路の方が最初から欠落しており、Phase 2をどれだけ直しても症状は変わらなかったはずである。
また、ユーザーが提示した「TLE更新時刻は進んだのに対象衛星だけ変わらない」という一見矛盾した観察は、
実際には「複数の独立した更新経路のうち一部だけが動いている」ことを示す精度の高い手がかりであり、
額面通りに深掘りする価値があった。

### TLE/衛星名/トランスミッターDB同期 — 進捗メッセージ全体フロー（2026-08-13）

同期処理には4つの独立した起点（①起動時・②Update TLEボタン・③Sync Satellite Namesボタン・
④Fetch Transmitter Databaseボタン）があり、それぞれ表示されるメッセージの種類・順序・
表示方式（常時ラベル vs 自動消去する一時メッセージ）が異なる。「進捗メッセージ全般を
『何をどこから取得しているか』が分かる文言に見直し（2026-08-13）」（前述）の作業に伴い、
全体像を整理した。

#### ① 起動時（`_refresh_satellite_names_sync()`）

```
[両ホスト到達可否チェック]
  │
  ├─ 両方到達可能 → 下記へそのまま進む
  ├─ 片方だけ不通 → "❌ Cannot connect to {CelesTrak|SATNOGS}"（約3秒表示）→ 続行
  └─ 両方不通    → "❌ Cannot connect to CelesTrak/SATNOGS"（約10秒表示）
                    → community_transmitters読み込み（ログのみ）→ ""（クリア）→ 終了
       ↓（片方到達可能 or 両方到達可能の場合のみ続く）
[Step1: 衛星名同期]（SATNOGS到達可能 かつ is_satellite_names_stale(24h) の場合のみ）
  "🛰 Syncing satellite names from SATNOGS..."
  "🛰 Syncing satellite names... (n)"  ← n件処理ごとに更新
       ↓
[Step2: Active TLE補完]（is_active_tle_stale(24h) or is_active_tle_retry_due() の場合のみ）
  "🛰 CelesTrak: fetching active TLEs..."
  "🛰 CelesTrak: downloading TLE data... N%"
  "🛰 SATNOGS: fetching TLE data for n satellite(s)..."   ← Phase1で解決しきれなかった分だけ
  "🛰 SATNOGS: downloading TLE data... N%"
  （ブロック発生時のみ）"⚠ {CelesTrak|SATNOGS} blocked — retry in 3h"
       ↓
[Step3: Provisional TLE取得]（SATNOGS到達可能 かつ is_provisional_tle_stale(12h) の場合のみ）
  "🛰 Fetching provisional TLEs... (done/total)"
       ↓
[Step4: Legacy衛星クリーンアップ]（CelesTrak到達可能の場合のみ・表示メッセージなし、ログのみ）
       ↓
[Step5: METEOR/HRPT TLE確認]（CelesTrak到達可能の場合のみ・表示メッセージなし、ログのみ）
       ↓
[Step6: コミュニティ周波数読み込み]（表示メッセージなし、ログのみ）
       ↓
[Step7: CelesTrak 6グループ一括取得]（CelesTrak到達可能 かつ 鮮度切れグループがある場合のみ）
  "🛰 Fetching group TLEs: Amateur Satellites (CelesTrak) (1/6)..."
  ...（対象グループ数だけ繰り返し）
  ""（クリア）
       ↓
""（最終クリア。衛星リスト再表示と同時）
```

#### ② Satellite → Update TLE（`_fetch_all_tle_sources()`）

鮮度ゲートを全て無視して即時実行する点が①と異なる。

```
"🛰 Updating TLEs…"
       ↓
[両ホスト到達可否チェック]（①と同じ分岐・同じメッセージ）
       ↓（片方到達可能 or 両方到達可能の場合のみ続く）
[CelesTrak 6グループ一括取得]（CelesTrak到達可能なら常に、鮮度に関わらず実行）
  "🛰 Fetching group TLEs: Space Stations (CelesTrak) (1/6)..." ...
       ↓
[Active TLE補完]（常に実行）
  "🛰 CelesTrak: fetching active TLEs..." など、①のStep2と同じメッセージ群
       ↓
[Provisional TLE取得]（SATNOGS到達可能なら常に実行）
  "🛰 Fetching provisional TLEs... (done/total)"
       ↓
（ブロック発生時のみ、かつ新規にブロックされた分のみ）"⚠ {provider} blocked — retry in 3h"
       ↓
""（最終クリア。ブロックが新規表示された場合はクリアされず、そのメッセージのまま残る）
```

#### ③ Satellite → Sync Satellite Names（手動・単独）

```
"Syncing satellite names from SATNOGS..."（ステータスバー、5秒表示の一時メッセージ）
       ↓
（SATNOGS不通の場合）"❌ Cannot connect to SATNOGS" → 終了
（到達可能な場合）"Satellite names sync: {upd} updated, {skp} skipped"（8秒表示）
```

#### ④ Satellite → Fetch Transmitter Database（手動・単独）

```
"Syncing transmitter frequencies from SATNOGS..."（ステータスバー、5秒表示の一時メッセージ）
       ↓
（SATNOGS不通の場合）"❌ Cannot connect to SATNOGS" → 終了
（到達可能な場合）"SATNOGS sync: {ins} inserted, {upd} updated, {skp} skipped"（8秒表示）
```

#### 表示方式の違い（①②と③④）

- **①②**: `_sync_progress`シグナル経由。ステータスバー下部の常時ラベル（`_sync_label`）に
  表示され、明示的に`emit("")`するまで残り続ける
- **③④**: `_satnogs_status`シグナル経由。`QStatusBar.showMessage()`による一時メッセージ
  （5〜8秒で自動消去）として表示される

同じ「SATNOGSと通信する」処理でも、①②（TLE同期チェーンの一部）と③④（単独の手動ボタン）
とで表示の仕組みそのものが異なる点に注意。新しい同期処理を追加する際は、常時ラベルが適切か
（複数ステップにまたがる進行状況を示す場合）、一時メッセージが適切か（単発の完了通知）を
判断すること。

### 起動時鮮度チェックの網羅的監査と修正（2026-08-11）

#### 発端

4G Wifi経由でのTLEブロック検証（前述の各節参照）が一段落した後、ユーザーから
「Provisional TLEのfetchはCLAUDE.md上12時間ごとのはずだが、同期→終了→即座に再起動、を
繰り返しても毎回フェッチされる。なぜか」という指摘があった。調査したところ、
`fetch_provisional_tles()`の起動時呼び出しには**鮮度チェックが一切存在しない**ことが判明。
「他にも同じようなものがないか確認してから実装して」という指示を受け、`_start_scheduler()`が
登録する7つのAPSchedulerジョブ全てについて、対応する起動時ゲートが正しく実装されているかを
1つずつ監査した。結果、**3件の独立したバグ**が見つかった——2件は「毎回無条件でフェッチする」
（過剰）、1件は「一度目以降ほぼ永久にフェッチしない」（欠落）という、正反対の方向の不具合だった。

#### 根本原因（3件に共通）

APSchedulerの`"interval"`トリガーは**ジョブ登録の瞬間には発火せず、登録時点から丸1区間
経過して初めて実行される**。デスクトップアプリは毎回終了・再起動されるものであり、
「7日間（あるいは1〜12時間）連続で起動しっぱなしにする」という前提はほとんどのユーザーの
実利用パターンと一致しない。このため、**起動時に「前回の完了からどれだけ経過したか」を
独立して判定するゲートを別途持たない限り、定期ジョブは実質的に一度も発火しないまま
終わる**。`fetch_active_tles()`（`is_active_tle_stale()`）だけがこの原則を最初から
正しく実装しており、他は次のいずれかの誤りを持っていた。

#### 発見した3件のバグと修正

| # | 対象 | 症状 | 原因 | 修正 |
|---|---|---|---|---|
| 1 | `fetch_provisional_tles()`（NORAD≥90000） | 起動のたびに**必ず**全件フェッチ（過剰） | 起動時呼び出しに鮮度チェックが一切無かった | `TLEManager.is_provisional_tle_stale(12h)`を新設しゲート |
| 2 | `sync_from_satnogs()`（トランスミッタDB） | 初回同期後は**ほぼ永久に**再取得されない（欠落） | 起動時ゲートが`source='satnogs'`行0件（真の初回起動）のみを判定し、経過時間を見ていなかった | `TransmitterManager.is_satnogs_transmitters_stale(168h)`を新設し、0件チェックに`OR`で追加 |
| 3 | CelesTrak 6グループ一括フェッチ（stations/amateur/cubesat/weather/earth-obs/science） | バグ2と同型（欠落） | `is_source_stale()`が「一度もフェッチしていないか」のみを判定し、経過時間を見ていなかった（ドキュメント自身に「APSchedulerの定期ジョブが後は面倒を見る」という誤った前提が明記されていた） | `is_source_stale()`自体を修正し、各ソース自身の`TLE_SOURCES[...]["update_interval_hours"]`（1〜12h）との比較を追加 |

バグ1はユーザー自身の指摘、バグ2・3はその指摘をきっかけにした横展開監査で発見した
（`is_provisional_tle_stale()`実装後、同じ設計原則を他の6ジョブに機械的に当てはめて確認）。

いずれも`TLEManager.is_active_tle_stale()`と全く同じ形（`sync_log`の最新`finished_at`を
読み、`datetime.now(UTC) - last > timedelta(hours=max_age_hours)`で判定）に統一してある。

#### 「Update TLE」ボタン側にも同型のバグが1件見つかった

上記の監査中、`_fetch_all_tle_sources()`（Satellite > Update TLE / Settings > OK 共有）が
`fetch_provisional_tles()`を**一度も呼んでいない**ことが判明した。これは前述
「`fetch_active_tles()`を一度も呼んでいなかった不具合（2026-08-10）」と全く同じクラスの
バグで、Provisional衛星についてだけ同じ穴が残っていた。`fetch_active_tles()`と同じ扱い
（自身の鮮度ゲートをバイパスし無条件で実行）で追加した。

#### 起動時とUpdate TLEボタンの取得順序（最終形、2026-08-11時点）

**起動時**（`_refresh_satellite_names_sync()`、上記フローチャート参照）:

```
1. sync_satellite_names()        無条件
2. fetch_active_tles()           is_active_tle_stale(24h) OR is_active_tle_retry_due()
3. fetch_provisional_tles()      is_provisional_tle_stale(12h)                    ← 2026-08-11
4. fetch_legacy_tles()           無条件（自己制限的、実質no-op後は毎回一瞬で終わる）
5. fetch_meteor_tles()           無条件（衛星ごとneeds_update(24h)で自己制限）
6. load_community_transmitters() 無条件（ローカルJSON、ネットワーク不要）
7. CelesTrak 6グループ一括       is_source_stale(グループ自身の interval) OR is_group_empty()  ← 2026-08-11
```

AMSAT運用状況は`_start_scheduler()`内で上記とは別スレッド・独立に実行される
（`is_stale(24h)`）。SATNOGSトランスミッタDB同期（`satnogs_count==0 OR
is_satnogs_transmitters_stale(168h)`）は、以前は別スレッドで上記と**並行**実行していたが、
2026-08-11にdb.satnogs.orgへの同時多発アクセスを避けるため`_refresh_satellite_names_sync()`と
**同一スレッドで直列**（SATNOGSトランスミッタDB同期 → 上記1〜7の順）に変更した
（本ファイル「fetch_active_tles() の2フェーズ設計」内「リクエストの『行儀』自体の改善」参照）。

**Satellite > Update TLE / Settings > OK**（`_fetch_all_tle_sources()`）:

```
1. CelesTrak 6グループ一括       無条件（鮮度ゲートをバイパス。「今すぐ更新」という明示要求のため）
2. fetch_active_tles()           無条件（同上、2026-08-10 追加）
3. fetch_provisional_tles()      無条件（同上、2026-08-11 追加）
```

Update TLEボタンは**上記3つのみ**を呼ぶ設計であり、`fetch_legacy_tles()`・`fetch_meteor_tles()`・
`sync_from_satnogs()`・`sync_satellite_names()`・`load_community_transmitters()`は呼ばない
（起動時のみ実行される）。前者3つは「ボタンを押した以上、鮮度キャッシュより即時実行を優先すべき」
という設計判断（2026-08-10 に`fetch_active_tles()`で確定した方針をそのまま踏襲）で、
起動時の`is_*_stale()`ゲートを意図的にバイパスしている点に注意。

#### 監査で「問題なし」と確認したもの

- `fetch_legacy_tles()`: SQL WHERE句自体が「TLE未解決の衛星」のみを対象にする自己制限的設計。
  対象が0件になれば以降は毎回一瞬でno-op終了するため、鮮度ゲート不要
- `fetch_meteor_tles()`: `needs_update(norad, max_age_hours=24.0)`による衛星ごとの内部判定を
  既に持つ（対象は`METEOR_NORAD_IDS`の固定9機のみ）
- `AmsatFetcher.is_stale()`: 実装当初から`app_settings`のタイムスタンプで正しく経過時間判定
- `_refresh_satellite_names_periodic()`（衛星名の6時間ごと再同期）: `sync_satellite_names()`は
  ページネーションされた一括APIコールで、衛星ごとの個別問い合わせではないため、無条件に
  毎回実行しても実害が小さい設計として意図的にゲートなし

### DB マイグレーション注意事項（2026-05-29 バグ対応済み）

`tle_data` テーブルの CHECK 制約変更時はテーブル再作成が必要（SQLite 制約）。
過去に `SELECT *` による列順序不一致でデータロスが発生した。

**現在の正しい実装**（`database.py _apply_migrations()`）：
- 列名を明示した `INSERT OR IGNORE INTO tle_data (col1, col2, ...) SELECT col1, col2, ...`
- `_tle_data_backup` テーブルが残存していれば（前回のマイグレーション中断の証拠）自動復旧
- `SELECT *` は絶対に使用しないこと

---

## SDR 機能設計方針（2026-06-08 確定）

### バックエンド

**SoapySDR** を採用。RTL-SDR・HackRF・Airspy 等の多機種対応。
- Python binding は pip 非対応 → システムパッケージ経由またはバンドル版を使用
- `SoapySDR` が import できない場合は SDR 機能を自動非表示（graceful degradation）
- デバイス列挙: `SoapySDR.Device.enumerate()` / 未インストール時は `pyusb` で USB VID/PID スキャン

#### Windows バンドル構成と対応デバイス（v0.1.72 確定）

**Windows では SoapySDR は根本的に使用できない。** SoapySDR の enumerate が
`hackrf_init()+hackrf_exit()` や `libusb_init()+libusb_exit()` を複数回呼び出し、
WinUSB ハンドルキャッシュを破壊する。このため RTL-SDR・HackRF は
ctypes で DLL を直接呼ぶバイパス実装を使用する。

**Windows でサポートするデバイス（v0.1.72 時点）**:

| デバイス | 実装方式 | Zadig/WinUSB | DLL |
|---|---|---|---|
| RTL-SDR（RTL2832U 系） | `RtlSdrDirectDevice`（ctypes） | ✓ 一回限り | `librtlsdr.dll` |
| HackRF One | `HackRfDirectDevice`（ctypes） | ✓ 一回限り | `hackrf.dll` |
| Airspy / Airspy HF+ | **非対応** | — | SoapySDR 経由は不可 |
| ADALM-Pluto | **非対応** | — | SoapySDR 経由は不可 |

SoapySDR モジュール DLL（SoapyRTLSDR.dll・SoapyHackRF.dll 等）は Windows インストーラーに
同梱しているが、ctypes バイパスにより実際には使用されない。

バンドル DLL の配置: core DLL + Python binding は `_MEIPASS/`、モジュール DLL は `_MEIPASS/soapy_modules/`。
起動時に `SOAPY_SDR_PLUGIN_PATH=soapy_modules/` をセット（`src/main.py` の frozen ブロック）。

conda-forge パッケージ取得スクリプト: `scripts/extract_soapy_conda.py`（CI の Windows ビルドステップで実行）。
SoapyPlutoSDR は conda-forge に存在しないため CI で MSVC ソースビルドし `soapy-win64/modules/` に配置する（ただし Windows では実際に使用されない）。

#### Linux / macOS インストール方法

| OS | コマンド |
|---|---|
| Ubuntu | `sudo apt install python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf soapysdr-module-airspy` |
| macOS | `brew install soapysdr soapyrtlsdr soapyhackrf soapyairspy` |

#### Linux AppImage が apt/zypper インストール済みの SoapySDR を見つけられないバグ（2026-07-14 修正・GitHub Issue #11）

**症状**: Debian/openSUSE等でHelp > SDR Device Installationの案内通り
`python3-soapysdr`・`soapysdr-module-rtlsdr`等をaptでインストール済み（ターミナルから
`import SoapySDR`が正常動作することも確認済み）でも、AppImage実行時は常に
「SoapySDR not installed」と表示される。ログには
`SoapySDR import failed: ModuleNotFoundError: No module named 'SoapySDR'`。

**原因**: `src/main.py`には`/opt/hamlib/4.7`が存在する場合のみ発動する開発機専用の
sys.path操作ブロックはあったが、**一般ユーザーのAppImage実行環境向けに
システムの`dist-packages`/`site-packages`をsys.pathへ追加する仕組みが存在しなかった**。
PyInstallerでフリーズされたAppImageのデフォルトsys.pathはバンドル済みモジュール
（`_MEIPASS`）のみで構成され、ホストの`/usr/lib/python3/dist-packages`等は
自動的には含まれない。AppImageの起動スクリプト（`scripts/build-appimage.sh`が
生成するAppRun）も`LD_LIBRARY_PATH`のみ設定しており、Python側のパスには一切関与しない。
Linux/macOSでSoapySDR自体を意図的にCIバンドルしない設計（本セクション冒頭参照）は
「システムパッケージ経由で見つかる」ことを前提にしていたが、その前提を実際に成立させる
コードが欠けていた。

**修正**: `src/main.py`に、`sys.platform == "linux" and getattr(sys, "frozen", False)`
（AppImage実行時のみ・既存の`/opt/hamlib`ブロックとは完全に独立）で発動する新しいブロックを
追加。以下を`sys.path`の**末尾に**追加する（バンドル済みモジュールを優先させ、importの
失敗時のみシステム側にフォールバックさせるため`insert(0, ...)`ではなく`append(...)`）:
- `/usr/lib/python3/dist-packages`・`/usr/lib/python{3.X}/dist-packages`（Debian/Ubuntu系）
- `/usr/lib/python3/site-packages`・`/usr/lib/python{3.X}/site-packages`（openSUSE/Fedora系）

**既知の限界**: この方式はAppImageが束ねるPythonバージョン（CI固定で3.11）とユーザーの
システムPythonバージョンが一致している場合のみ有効（拡張モジュールはABI互換性が必要）。
Debian 12・Ubuntu 22.04/24.04等は標準で3.11系のため通常問題にならない想定。

#### macOS .app にも同一クラスのバグが存在していた（2026-07-31 発見・修正）

**症状**: Homebrewで`brew install soapysdr soapyrtlsdr`（Help > SDR Device Installation
経由）を実行しても、.appバンドル実行時にRTL-SDRが一切認識されない。

**原因**: 上記Linux AppImageのバグと全く同じ根本原因が、**macOS向けには一度も修正されて
いなかった**。`src/main.py`にはLinux向けの`sys.platform == "linux" and
getattr(sys, "frozen", False)`ブロックは存在したが、対応する`darwin`向けブロックが
欠けていたため、PyInstallerでフリーズされた.appは常にHomebrewの`site-packages`を
見えないままだった。

**修正**: Linux向けブロックと同じ設計・同じ場所（直後）に`sys.platform == "darwin" and
getattr(sys, "frozen", False)`ブロックを追加。`sys.path`の**末尾に**（`append`、`insert(0,
...)`ではない）以下を追加:
- `/opt/homebrew/lib/python3.*/site-packages`（Apple Silicon）
- `/usr/local/lib/python3.*/site-packages`（Intel）

Homebrew自身のPythonバージョンは時期によって変わり続けるため（3.12・3.13等）、Linux版が
`f"/usr/lib/python{_pyver}/dist-packages"`のように**自アプリのPythonバージョン**を使って
パスを組み立てていたのとは異なり、macOS版は`glob.glob("*.../python3.*/site-packages")`で
**実際に存在するディレクトリをそのまま探索**する方式にした（Homebrewのpythonバージョンは
自アプリの束ねるPythonバージョンとは無関係な別物のため、決め打ちできない）。

**既知の限界**: Linux版と同様、Homebrewのpython3バージョンとアプリが束ねるPythonバージョン
（CI固定で3.11）が異なると、コンパイル済み拡張モジュール（`.so`）のABIが一致せず
importに失敗する可能性がある。この場合は`brew install python@3.11`等でバージョンを
合わせるか、根本的にはconda-forge抽出方式（本ファイル「gr-satellitesのバンドル配布」
セクション参照）のような自前バンドルに切り替える必要がある。実機での動作確認は未実施
（ユーザーが次回タグビルド前に確認予定）。

#### macOS SoapySDR conda-forge 同梱 — 上記「既知の限界」が実機で的中・Homebrew依存を撤廃（2026-08-01 実装・実機のRTL-SDRで動作確認済み）

**発端**: 上記のsys.path修正をリリースしたユーザー実機（M2 MacBook Air、Homebrewで
`soapysdr`/`soapyrtlsdr`インストール済み）で実際にRTL-SDRが認識されるか確認したところ、
`_SoapySDR.so`自体は発見・dlopenされるようになった（sys.path修正は正しく機能）ものの、
その先で`Library not loaded: @rpath/libSoapySDR.0.8.dylib`という**別の**dlopenエラーが
発生した。`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`を試したところこのエラー自体は
解消し`SoapySDR`は「インストール済み」と認識されるようになったが、続いて
`SoapySDR.Device.enumerate()`が`TypeError: in method 'SoapySDRKwargs___getitem__', argument
1 of type 'std::map< std::string,std::string > *'`という、SWIGバインディングの型情報が
壊れているような実行時エラーで失敗した。

一方、Homebrew自身の`python3`（`"$(brew --prefix)"/bin/python3`）から同じ`import
SoapySDR; SoapySDR.Device.enumerate()`を実行すると、RTL-SDRを正しく検出できた
（`Found Rafael Micro R820T tuner`）。**同じファイルなのに、どのPythonプロセスから
読み込むかで結果が変わる**という事実が決め手になった。

**根本原因**: アプリがバンドルしているPythonは**CPython 3.11**
（`.github/workflows/ci.yml`のmacOSビルドジョブで`actions/setup-python@v5
python-version: "3.11"`固定）だが、ユーザーのHomebrewの`python3`は**3.14**。
`_SoapySDR.so`はSWIGが生成したコンパイル済みC拡張で、特定のCPython ABI（マイナー
バージョンごとに内部構造体レイアウトが異なりうる）に強く依存する。3.14向けにビルド
された拡張を3.11のインタプリタプロセスに読み込むと、importやdlopen自体は成功して
しまうことがある一方、実際にC++オブジェクト（`std::map`をラップした`SoapySDRKwargs`）
を操作する段になって型情報の不整合が表面化する——今回の`TypeError`はまさにこのクラスの
症状で、「既知の限界」パラグラフが予告していた通りの実機再現だった。

**方針転換の経緯**: 当初「ctypesで`librtlsdr.dylib`を直接叩く」案（Windows版の
`RtlSdrDirectDevice`と同型）を提案したところ、ユーザーから「それだと対応SDRが
Windows版同様RTL-SDR限定になってしまうのでは」と的確な指摘があった。実際その通りで、
Windows版がctypesバイパスに頼っているのはWinUSBハンドルキャッシュ破壊という
**SoapySDR自体が原理的に使えない**問題への対処であり、Airspy・AirspyHF・PlutoSDRは
代替手段がないため今もWindows非対応のまま。macOSの問題はPythonバージョン不一致という
**より狭い**原因のため、ctypesバイパスは過剰（＆デバイスを限定してしまう）と判断した。

**採用した方式**: Windows版が既に実践している「conda-forgeからSoapySDR本体・Python
バインディング・各デバイスモジュールを抽出し、アプリに同梱する」パターンをmacOS向けに
移植。ただし**この設計判断の根拠として「Windows版で実証済み」と最初に説明したのは不正確
だった**とユーザーから訂正を受けた——Windows版は同梱の仕組み自体はあるが、その同梱
SoapySDR経由のデバイスアクセスはWinUSB問題によりWindowsでは実際には機能しておらず
（RTL-SDR/HackRFはctypesバイパスでしか動いていない）、「バンドルすれば動く」ことを
証明した実例ではない。macOSにはWinUSBのようなハンドルキャッシュ破壊問題は存在しない
ため機能する可能性は高いと判断したが、**これは未検証の新しい試みである**点を明記した
上でユーザーの承認を得た。

conda-forgeのosx-arm64チャンネルを実際に調査した結果、`soapysdr-0.8.1-py311h2c37856_5.conda`
という**Python 3.11専用ビルド**（アプリの束ねるPythonと完全一致）が存在することを確認。
デバイスモジュール（`soapysdr-module-{rtlsdr,hackrf,airspy,airspyhf,remote}`）自体は
Pythonバインディングを持たない純C++プラグイン（`.so`、`dlopen`でロードされるだけ）の
ため、Pythonバージョンとは無関係にどれでも使える。

**実装（`scripts/extract_soapy_conda_macos.py`、新規）**: Windows版`extract_soapy_conda.py`
と同型だが、macOS conda-forgeパッケージ特有の点に対応: `lib/libSoapySDR.0.8.dylib`が
実ファイルではなく`libSoapySDR.0.8.1.dylib`への**tarシンボリックリンク**として
格納されている（`@rpath/libSoapySDR.0.8.dylib`という他バイナリからの参照名と、
ディスク上の実ファイル名が異なる）。シンボリックリンクはtar展開時にデータを
持たないため、CIアーティファクトとして再tar/アップロードする過程で消失しやすい
——このスクリプトは全シンボリックリンクをその場で実体化（リンク先の実バイトを
リンク自身の名前でも書き出す）し、展開後のディレクトリにシンボリックリンクが
一切残らないようにしている。`include/SoapySDR/*.h(pp)`ヘッダーも抽出する
（後述のPythonバインディング自前ビルドで使用）。

**dylibbundlerでのrpath修正は最終的に完全撤廃——3段階の失敗を経て判明（2026-08-01）**:

当初はHamlibのmacOSビルドで既に使われている`dylibbundler`（同ジョブで`brew install`済み）
を流用する設計だった。ところが`workflow_dispatch`で試したところ、以下の3段階すべてで
つまずいた。

1. **ハング**: `--overwrite-dir`だけでは足りず、`soapy-macos/root/`（`--dest-dir`）に
   あらかじめ`cp`で同名ファイルを配置してから実行する設計だったため、`dylibbundler`が
   「既存ファイルを上書きするか」の個別確認プロンプト（`--overwrite-files`という別フラグ
   でしか抑制できない）で停止し、GitHub Actionsの対話端末なし・EOFを返さない標準入力
   という環境で**30分以上応答なしのまま**固まった（ユーザーからの「時間がかかりすぎ」
   という指摘で発覚）。`--overwrite-files`追加・`</dev/null`によるstdinリダイレクト・
   `--search-path`と`--dest-dir`の分離・`--no-codesign`・1ファイル1ステップへの分割
   （`timeout-minutes`で強制打ち切り）と、考えられる対策を順に試したが、**最小構成
   （1ファイルのみ、ハードタイムアウト付き）でも17分以上ハングし、キルシグナルでも
   終了しなかった**。同じジョブ内でHamlib向けの`dylibbundler`呼び出しは常に高速に
   終わっており、原因はこのランナーやツール自体の一般的な不具合ではなく、conda-forge
   がビルドしたSoapySDRバイナリに特有の何かだったと推測されるが、ハング中は一切ログが
   出ないため、正確なメカニズムは最後まで特定できなかった
2. **代替実装（`otool -L` + `install_name_tool`を直接呼ぶ自前スクリプト）**:
   `dylibbundler`が実際に必要としているごく一部の機能（依存関係の列挙・不足分のコピー・
   参照の書き換え）だけを`scripts/fix_soapy_rpaths_macos.py`として自前実装し、ハングは
   完全に解消（Linux上でフェイクの`otool`/`install_name_tool`/`codesign`を使い、共有
   依存関係の重複コピー防止を含む再帰ロジックをローカル検証済み）。CI実行でも
   全ての「Fix rpaths」ステップは数秒で成功するようになった
3. **`libpython3.11.dylib`への固定リンク**: ハング解消後に判明した別の問題として、
   conda-forgeがビルドした`_SoapySDR.cpython-311-darwin.so`が**conda-forge自身の
   Python 3.11に対して固定リンク**されていた（`@rpath/libpython3.11.dylib`）。
   アプリが同梱するPyInstaller版Python 3.11とは別バイナリのため、参照を書き換える
   必要があったが、「別のlibpythonを追加で同梱する」のはプロセス内に2つの独立した
   CPythonランタイムが同時ロードされる危険な選択肢のため、「PyInstaller自身が同梱する
   libpythonを指すよう書き換える」方針にしたところ、**CI検証用にコピーしたファイルの
   コード署名が無効**というさらに別の問題に直面した（python.org公式Frameworkビルドの
   署名が、コピー後の配置場所で無効化される現象。詳細な原因は未解明のまま棚上げ）

3段階目でユーザーから「これ以上その場しのぎの対症療法を繰り返すのではなく、いったん
立ち止まって再検討せよ」との明確な指示があり、GQRX（[gqrx-sdr/gqrx](https://github.com/gqrx-sdr/gqrx)、
定評あるmacOS SDR OSS）のmacOSビルド方式を調査した。GQRXはC++/Qtアプリで
SoapySDRのC++ APIを直接使っており、Pythonバインディング自体を持たないため
この`libpython`問題自体が原理的に発生しない（`dylibbundler`も使っておらず、Qt公式の
`macdeployqt6`でリンク依存関係を解決し、SoapySDRのプラグインモジュール自体はrpath
修正なしでそのままコピーするという、より軽量な方式だった）。GQRXの知見は
「conda-forgeを素材として使う設計自体は妥当」という傍証にはなったが、Pythonバインディング
固有のこの問題への直接の答えにはならなかった。

**最終的に採用した根本解決策——SoapySDRのPythonバインディングを自前でSWIGビルド**:
SoapySDR本家のソース自体（`python/CMakeLists.txt`）を確認したところ、
`if(APPLE) list(APPEND PYTHON_LIBRARIES "-undefined dynamic_lookup") endif()`と、
まさにこの問題を回避する設計が**上流に最初から存在していた**ことが判明した
（`-undefined dynamic_lookup`は特定のlibpythonに固定リンクせず、実行時に「今動いている
インタプリタ自身」からPythonのC API シンボルを解決する手法——本プロジェクトが
Hamlibの`_Hamlib.so`で既に使っているのと全く同じパターン）。conda-forgeのビルド
レシピはこの分岐を取らず、独自にpython実行環境へ明示的にリンクしていたと見られる。

conda-forgeビルドの`libSoapySDR.dylib`（C++コア）自体はそのまま使い、Pythonとの
橋渡し層（`_SoapySDR.so`）だけを自前でビルドし直す方式にした:
- SoapySDR公式ソース（`soapy-sdr-0.8.1`タグ）から`python/SoapySDR.in.i`
  （SWIGインターフェース定義。単一ファイルで完結、`@SOAPY_SDR_ABI_VERSION@`という
  唯一のCMakeテンプレート変数を持つ。値は同梱ヘッダーの`Version.h`から動的取得）を取得
- `swig -c++ -python -threads -I<headers> -o SoapySDR_wrap.cxx SoapySDR.i`
- `clang++ -shared -fPIC -O2 -std=c++11 -undefined dynamic_lookup -I<headers>
  -I<python_include> SoapySDR_wrap.cxx -L<lib> -lSoapySDR -Wl,-rpath,@loader_path
  -o _SoapySDR<EXT_SUFFIX>`
- 生成された`_SoapySDR.so`はlibpythonへの依存を一切持たないため、CI検証ステップの
  署名問題も含め、上記3つの問題がすべて同時に解消した

この方式のSWIG生成段階（プレースホルダー置換・ヘッダー・生成コマンド）はLinux開発機
でも事前検証済み（実際のmacOSコンパイルはCI runnerでしか確認できない）。

**CI（`build-macos`ジョブ）への実装ステップ**（最終形）:
1. 「Bundle SoapySDR for macOS (conda-forge pre-built)」— パッケージ群をダウンロードし
   `extract_soapy_conda_macos.py`で`soapy-macos/{lib,python,modules,include}/`に展開
2. 「Build SoapySDR Python binding from source (SWIG)」— 上記の自前ビルドで
   `soapy-macos/python/_SoapySDR<EXT_SUFFIX>`・`SoapySDR.py`をconda-forge版から上書き
3. 「Prepare SoapySDR bundle root + strip quarantine attrs」— `soapy-macos/root/`へ
   python層をコピーし、`xattr -cr`でcurlダウンロード由来の`com.apple.quarantine`を除去
4. 「Fix rpaths — （ファイル名ごとに5ステップ）」— `fix_soapy_rpaths_macos.py`で
   `_SoapySDR.so`・各デバイスモジュール`.so`の`@rpath`参照を`@loader_path`ベースへ
   書き換え。1ファイル1ステップ構成は当初dylibbundlerハング調査用の切り分け目的
   だったが、そのまま残している（各ステップが数秒で終わるため実害なし、かつ
   将来同種の問題が再発した場合の診断性を保てるため）
5. 「Verify bundled SoapySDR imports and enumerates」— Homebrewに一切頼らず
   抽出・修正済みファイルだけで`import SoapySDR; SoapySDR.Device.enumerate()`を実行
- `scripts/fbsat59.spec`: darwin分岐に`soapy_binaries`ブロックを追加
  （`soapy-macos/root/*` → `"."`、`soapy-macos/modules/*.so` → `"soapy_modules"`）。
  Windows分岐の同名ブロックと対称的な構造
- `src/main.py`: darwin frozen時に`SOAPY_SDR_PLUGIN_PATH`を`_MEIPASS/soapy_modules`
  へ設定するブロックを新設（Windows分岐と同じパターン）。既存のHomebrew site-packages
  追記ブロック（2026-07-31実装）は同梱ビルドが欠けていた場合の最終フォールバックとして
  残した（`append`のため優先度は常に同梱版より低い）

**今回のスコープ外（意図的）**: PlutoSDR・BladeRFは`libiio`等の追加依存が重く、
検証用の実機も手元にないため見送った。Homebrew経由の案内（Help > SDR Device
Installation）は引き続きこの2機種向けに有効なまま残している。`soapysdr-module-remote`
（Remote SDR）も、dylibbundlerハングの原因切り分け中に一時的にこのバッチから除外した
まま（原因はdylibbundler自体だったと判明したため無実だった可能性が高い）。次回、
再度バンドル対象に加えて動作確認すること。

**検証状況（2026-08-01）**: `workflow_dispatch`でCI完全グリーン（SoapySDRのimport・
`Device.enumerate()`・PyInstallerビルド・DMG作成まで全て成功）を確認した上で、
**ユーザーの実機（M2 MacBook Air）でRTL-SDRが正常に認識されることを確認済み**。
Homebrewのインストール状況に一切依存せず動作する。

**教訓**: サードパーティのビルド済みバイナリに依存する際、そのビルドが「上流の
標準的な回避策」をなぜか取っていないケースがある（今回のconda-forgeのlibpython
固定リンクは、SoapySDR自身のCMakeがAppleでは`-undefined dynamic_lookup`を使う設計に
なっていたにもかかわらず、パッケージング側の事情でそれをバイパスしていた）。対症療法
（参照先の書き換え）を重ねる前に、まず**その部分だけでも自前でビルドし直せないか**を
検討する価値がある——今回は結果的にビルド済みバイナリの数分の一の分量（Pythonバインディング
のみ）を自前ビルドするだけで、複数の問題が同時に解消した。また、同種のツール
（`dylibbundler`等）が「別の文脈では問題なく動いている」からといって、今回の対象
バイナリに対しても安全とは限らない——原因不明のまま複数の対症療法を試すより、
早い段階で「この特定の依存関係自体を作り直せないか」という別のレイヤーの解決策を
検討すべきだったという反省点も残る。

---

#### PlutoSDR（ADALM-Pluto）Windows バンドル実装メモ（v0.1.5 で実装・CI 緑確認済み）

**背景**: SoapyPlutoSDR は conda-forge に存在しないためソースビルドが必要。
CI の "Build SoapyPlutoSDR for Windows" ステップで実装済み（`v0.1.5`、2回の修正で緑確認）。

##### 依存関係と実際の入手方法

| ライブラリ | 入手方法 | 備考 |
|---|---|---|
| libiio | conda-forge win-64（`libiio>=0.26`）| DLL + ヘッダー両方取得できる |
| libad9361 | 不要 | 260 kHz 以上のサンプルレートなら動作。アマチュア衛星用途には十分 |
| SoapyPlutoSDR | `pothosware/SoapyPlutoSDR` ソースビルド（MSVC + Ninja） | 出力は `PlutoSDRSupport.dll` |

##### libad9361 の役割（省略している理由）

`libad9361` は低サンプルレート時に AD9361 チップへ FIR フィルターを自動ロードするライブラリ。

| 状態 | 最低サンプルレート | 影響 |
|---|---|---|
| libad9361 **あり** | 約 65 kHz（25 MHz ÷ 384） | 低レートでも FIR で品質維持 |
| libad9361 **なし** | 約 260 kHz（25 MHz ÷ 96） | 260 kHz 以上なら通常動作 |

アマチュア衛星用途（FM/SSB/CW・IQ録音）は 260 kHz 以上で十分なため省略。

##### CI 実装（.github/workflows/ci.yml）

"Bundle SoapySDR for Windows" ステップの直後に配置。

**重要な落とし穴（v0.1.5 デバッグで判明）**:

1. **conda cmake が PATH に入り VS 検出に失敗する問題**
   - conda で cmake をインストールすると conda のパスが優先され、VS を見つけられない cmake が使われる
   - **対策**: conda には cmake を含めない。システム cmake をフルパス `C:\Program Files\CMake\bin\cmake.exe` で指定。VS ジェネレーター (`"Visual Studio 17 2022"`) を使わず、`vcvarsall.bat` で MSVC を PATH に追加してから Ninja ジェネレーターを使う

2. **DLL ファイル名が想定と異なる問題**
   - SoapyPlutoSDR のビルド出力は `SoapyPlutoSDR.dll` ではなく **`PlutoSDRSupport.dll`**（CMake target 名）
   - SoapySDR は `SOAPY_SDR_PLUGIN_PATH` ディレクトリの全 DLL をロードするためファイル名は問わない
   - **対策**: `Get-ChildItem` のフィルターを `PlutoSDRSupport.dll` に設定

**実際に動作するビルド手順（ci.yml のステップ）**:
```powershell
# 1. conda で libiio + soapysdr ヘッダー取得（cmake は含めない！）
conda create --prefix pluto-deps -c conda-forge "soapysdr=0.8.1" "libiio>=0.26"

# 2. vcvarsall で MSVC を PATH に設定（VS ジェネレーターを避けて Ninja を使うため）
$vcvarsall = (vswhere でパス取得)
cmd /c "`"$vcvarsall`" x64 && set" → 環境変数をプロセスに反映

# 3. システム cmake + Ninja でビルド
"C:\Program Files\CMake\bin\cmake.exe" -G Ninja -DCMAKE_PREFIX_PATH=pluto-deps\Library
cmake --build SoapyPlutoSDR-build

# 4. コピー（出力名に注意）
PlutoSDRSupport.dll → soapy-win64/modules/   # ← SoapyPlutoSDR.dll ではない
libiio.dll         → soapy-win64/bin/
```

##### ユーザー側の追加作業

- USB 接続時: WinUSB ドライバーを Zadig で適用（RTL-SDR と同様・一回限り）
- ネットワーク接続時（192.168.2.1）: 追加ドライバー不要

##### BladeRF について

libbladerf は conda-forge win-64 に存在するが SoapyBladeRF はなし。
PlutoSDR と同じアプローチ（MSVC ソースビルド）で追加可能。

**既存環境への対応**: `SoapySDR.Device.enumerate()` が成功すれば即 Ready。追加作業なし。
**排他制御**: SoapySDR デバイスは 1 プロセス占有。Ground-Station 等と同時使用不可。

#### Bias-T ドライバ別キー対応（実装済み・2026-06-09 確定）

`SoapySDR.Device.writeSetting()` は未知のキーを**例外なしに無視する**ため、
try-except で複数キーを試す方式は機能しない。ドライバ名で分岐が必須。

```python
driver = (self._info.driver or "").lower()
if "hackrf" in driver:
    key = "bias_tx"
    value = "true" if enabled else "false"   # HackRF: 文字列 "true"/"false"
elif "rtlsdr" in driver or "rtl" in driver:
    key = "biastee"
    value = "1" if enabled else "0"           # RTL-SDR: 文字列 "1"/"0"
else:
    key = "biastee"
    value = "true" if enabled else "false"    # その他: 汎用フォールバック
```

#### CW 復調方式（エンベロープ検出なし・2026-06-09 確定）

エンベロープ検出（`np.abs()` + LPF）はバンドパスフィルタで帯域制限したノイズにも
必ず正値を返すため、信号がなくてもブーン音が発生する（AGC が増幅）。
CW 復調は**エンベロープ検出を一切行わない**方式を採用：

```
I/Q → DC除去 → 2段デシメーション（~8kHz）→ 実部取り出し → SOS BPF(300-3000Hz) → 出力
```

- ナチュラルオフセット（搬送波が中心周波数±数百〜数千Hz）がそのまま音声になる
- BPF は `scipy.signal.butter(4, [300/nyq, 3000/nyq], btype='band', output='sos')` + `sosfilt()`
  （狭帯域 b,a 形式は数値的に不安定なため SOS 形式必須）
- サイドトーン注入不要

### ディレクトリ構成

```
src/sdr/
├── __init__.py
├── device.py          # SoapySDRDevice — デバイス列挙・接続・サンプル取得
├── pipeline.py        # SDRPipeline (QThread) — pub/sub I/Q配信ハブ
├── demodulator.py     # NFM / USB / LSB / CW 復調（numpy + scipy）
├── recorder.py        # IQRecorder — CF32 WAV 書き出し
├── sdr_state.py       # SdrWebState — Web UI 向け状態共有
└── plugins/
    ├── base.py            # SdrPlugin 抽象基底クラス
    ├── fm_demod.py        # NFM 復調（初期実装）
    ├── ssb_cw_demod.py    # SSB/CW 復調（初期実装）
    ├── iq_recorder.py     # IQ 録音（初期実装）
    ├── direwolf.py        # 将来: APRS / Direwolf 連携
    ├── wsjtx.py           # 将来: FT4 / WSJT-X 連携
    └── sstv.py            # 将来: SSTV 受信
```

**注意**: SatDump 連携は SDR プラグインではなく独立モジュールとして実装済み:
```
src/
├── comms/
│   └── meteor/
│       ├── __init__.py
│       └── satdump.py   # METEOR_PIPELINES・METEOR_NORAD_IDS・SatDumpProcess(QThread)
├── ui/
│   └── meteor_tab.py    # MeteorTab — UI・SatDump 制御・Autotrack 連携
```

### I/Q パイプライン設計（pipeline.py）

`SDRPipeline` は pub/sub バスとして設計する。各プラグインがサンプルを購読し、
パイプライン本体に触れずにプラグイン追加が可能。

```
SoapySDR（QThread内）
    │  I/Q samples (numpy CF32, 2.4MHz)
    ▼
SDRPipeline
    ├── FFT → スペクトラムデータ → Signal → SDR Control UI（10fps）
    ├── → FM/SSB/CW Demodulator → sounddevice 音声出力
    ├── → IQRecorder → CF32 WAV ファイル
    ├── → [将来] SatDump stdin pipe
    └── → [将来] Direwolf / SSTV AudioSource
```

### AudioSource 抽象層

狭帯域データモード（APRS・SSTV 等）の音声入力元を抽象化する。
デコーダープラグインは `AudioSource` インターフェースのみを見るため、
SDR ソフトウェア復調とリグサウンドカード入力を透過的に切り替えられる。

```python
class AudioSource:  # 抽象基底
    pass

class SdrAudioSource(AudioSource):
    # SoapySDR → ソフトウェア復調 → PCM バッファ

class SoundcardAudioSource(AudioSource):
    # sounddevice.InputStream → PCM バッファ
    # リグの AF 出力が入っているデバイスを sounddevice.query_devices() で列挙して指定
```

- SDR 未接続時は SdrAudioSource オプションをグレーアウト
- リグ未接続時は SoundcardAudioSource オプションをグレーアウト

### SdrPlugin 抽象基底クラス（plugins/base.py）

```python
class SdrPlugin:
    name: str                      # "APRS / Direwolf"
    supported_modes: list[str]     # ["FM", "AFSK"]
    requires_tx_audio: bool        # APRS TX は True
    requires_external: str | None  # "direwolf", "satdump" 等

    def start(self, center_freq_hz: float) -> None: ...
    def stop(self) -> None: ...
    def get_widget(self) -> QWidget: ...  # SDR Control タブ内に埋め込む UI
    def is_available(self) -> bool: ...  # 外部ツール検出
```

### SdrRigAdapter（src/rig/controller.py への追加）

既存の `RigController` 抽象基底クラスを継承し、SDR を Rig 1 / Rig 2 として扱えるようにする。

```
RigController（抽象）
    ├── HamlibDirectController   ← 既存
    ├── HamlibNetController      ← 既存
    └── SdrRigAdapter            ← 新設
          SoapySDRDevice を内包
          is_sdr = True プロパティ
          connect() → SDRPipeline 起動
          set_frequency() → SoapySDR 中心周波数を設定（ドップラー補正連動）
```

### UI 設計

#### Rig Settings ダイアログ — SDR Settings タブ（第3タブ）

- デバイス列挙ボタン（`[Enumerate]`）
- デバイス選択ドロップダウン（SoapySDR.Device.enumerate() の結果）
- Sample Rate / Bandwidth / PPM Offset / RF Gain 設定
- **Assign as: ○ Rig 1  ● Rig 2** ラジオボタン
- IQ 録音保存先ディレクトリ設定

#### Radio Control — SDR 接続時の表示

```
通常の Rig:  [RIG: Connected  ■ 435.612 MHz]
SDR の Rig:  [SDR: Connected  ■ 435.612 MHz]  ← シアン色で区別
```

#### SDR Control タブ（Radio Control タブの隣）

- **SDR 未接続時は `setEnabled(False)`**（グレーアウト）
- 接続後にアクティブ化
- 内部はプラグインホスト構造（将来プラグインがタブとして追加される）

**初期実装のパネル構成:**
```
┌─ Spectrum ──────────────────────────────────────┐
│  QtCharts QLineSeries で約 10fps のリアルタイム FFT │
│  横軸: 周波数, 縦軸: dBFS, 中心周波数マーカー（赤） │
└──────────────────────────────────────────────────┘
┌─ Demodulator ───────────────────────────────────┐
│  Mode: [NFM ▼] [USB] [LSB] [CW]                  │
│  Filter BW: スライダー  Volume: スライダー  AGC    │
│  [▶ Start Audio]  [■ Stop Audio]                 │
└──────────────────────────────────────────────────┘
┌─ IQ Recorder ───────────────────────────────────┐
│  BW: [250 kHz ▼]   ファイル名自動生成             │
│  [● REC]  [■ STOP]   経過時間 / ファイルサイズ    │
└──────────────────────────────────────────────────┘
```

### トランスポンダー選択 → デモジュレーターモード自動切替

Radio Control でトランスポンダーを選択すると SDR Control のモードを自動設定する。

| SATNOGS mode 値 | SDR Control で自動選択 |
|---|---|
| `FM` / `DIGITALVOICE` | NFM |
| `SSB` / `USB` | USB |
| `LSB` | LSB |
| `CW` / `CW-R` | CW |
| `BPSK` / `AFSK` | USB（IQ 録音推奨） |

### IQ 録音ファイル仕様

- フォーマット: WAV CF32（32bit float ステレオ I/Q）
- サンプリングレート: 選択した帯域幅（例: 250 kHz）
- ファイル名: `{NORAD}_{衛星名}_{AOS時刻UTC}.iq.wav`
- SDR#・GQRX・SDR++ 等で直接再生・復調可能

### 将来拡張プラグイン（フェーズ2以降）

#### アマチュア衛星・デジタルモード

| プラグイン | バックエンド | 受信入力 | 送信 |
|---|---|---|---|
| ~~HRPT/LRPT 画像~~ | ~~SatDump（サブプロセス stdin pipe）~~ | ~~SDR のみ~~ | ~~なし~~ | → **実装済み** `src/comms/meteor/` |
| 衛星テレメトリーデコード | gr-satellites（GNU Radio OOT モジュール、サブプロセス） | SDR のみ | なし |
| APRS | Direwolf（TCP KISS） | SDR or Rigサウンドカード | Rig サウンドカード + PTT |
| ~~FT4~~ | ~~WSJT-X（UDP 連携）~~ | ~~SDR or Rigサウンドカード~~ | ~~Rig サウンドカード + PTT~~ |
| → **FT4（実装済み）** | **ft8_lib ctypes（内蔵）** | Rig必須・SDR RX 補助可 | Rig サウンドカード + PTT |
| ~~CW デコード~~ | ~~AI-CW デコーダー（内蔵、ML推論）~~ | ~~SDR or Rigサウンドカード~~ | ~~なし~~ | → **実装済み** `src/comms/cw/`（deepcw-engine ONNX）|
| SSTV 受信 | pySSTV（内蔵） | SDR or Rigサウンドカード | なし |

外部ツール（SatDump・Direwolf・gr-satellites）はサブプロセス起動。内部実装しない。
FT4 は ft8_lib ctypes で内蔵実装済み（WSJT-X UDP 方式から変更）。

#### 業務用衛星受信（フェーズ2以降・計画中）

HackRF / RTL-SDR + 適切な LNA・フィルターで受信可能な業務用衛星信号のデコードを追加予定。
いずれもオープンソースのデコーダーが存在し、SDR プラグインとして組み込める。

| 衛星システム | 周波数帯 | 内容 | 主なOSSデコーダー候補 |
|---|---|---|---|
| **Inmarsat-C（STD-C）** | 1.5 GHz L帯 | 海事安全情報（MSI）・EGC（Enhanced Group Call）・LRIT | [aero](https://github.com/jontio/JAERO)・[inmarsat-c](https://github.com/Outernet-Project/aero) |
| **Cospas-Sarsat L帯下り** | 1544.5 MHz | 捜索救助ビーコン位置情報（PLB/EPIRB/ELT） | [LRPT decoder](https://github.com/opensatelliteproject)・gr-satellites |
| **Iridium L帯 ACARS** | 1616〜1626.5 MHz | 航空 ACARS メッセージ・衛星電話傍受（表示のみ） | [iridium-toolkit](https://github.com/dholm/iridium-toolkit) |
| **Orbcomm** | 137〜138 MHz VHF | IoT/M2M データメッセージ・AIS 補完 | [gr-orbcomm](https://github.com/dholm/gr-orbcomm)・[orbcomm-decoder](https://github.com/microp11/orbcomm) |
| **みちびき（QZSS）データ放送** | 1278.75 MHz L6帯 | 高精度測位補強（MADOCA-PPP）・災害危機管理通報 | [qzsl6tool](https://github.com/yoronneko/qzsl6tool) |

**実装方針:**
- 各デコーダーはサブプロセスとして起動し、stdout/パイプ経由でデコード結果を受け取る
- 専用 UI パネルを SDR Control タブ内のプラグインタブとして追加
- IQ 録音ファイルからのオフライン再解析にも対応予定
- ライセンスに注意: 各国の電波法規制を遵守すること（受信のみ・復号結果の二次利用不可の場合あり）

#### gr-satellites について
- GNU Radio の OOT（Out-Of-Tree）モジュール。100 機種以上のアマチュア衛星テレメトリーフォーマットに対応
- `gr_satellites` コマンド（CLI）を IQ ストリームに繋いでサブプロセス起動する方式が最も移植性が高い
- **`pip install gr-satellites` は存在しない**（2026-07-31 判明・下記「gr-satellitesのバンドル配布」参照）。正しいインストール方法は conda-forge（`conda install -c conda-forge gnuradio-satellites`）・Ubuntu PPA・ソースビルドのいずれか
- インストール状態確認・誘導は `Help > gr-satellites Installation…`（`src/ui/gr_satellites_dialog.py`）で実装済み（バンドルのダウンロード・手動インストール手順の両方を提示）

#### gr-satellitesのバンドル配布（2026-07-31 実装・macOSのみ・実機未検証）

**発端**: Help画面の「gr-satellites Installation」ダイアログが案内していた `pip install gr-satellites`
を実際にmacOSユーザーが試したところ `ERROR: Could not find a version that satisfies the requirement
gr-satellites (from versions: none)` で失敗。`curl -sI https://pypi.org/simple/gr-satellites/` が
PyPIサーバー自身から404を返すことを確認し、**gr-satellitesはPyPIに一切公開されていない**ことが
判明した（`pip config`・ネットワーク・radioconda環境はすべて正常だった）。公式ドキュメント
（[gr-satellites readthedocs](https://gr-satellites.readthedocs.io/en/latest/installation.html)・
[conda版](https://gr-satellites.readthedocs.io/en/latest/installation_conda.html)）を確認したところ、
正しいインストール方法は次の3通り: (1) conda-forge `conda install -c conda-forge gnuradio-satellites`
（公式が「Windowsでの推奨方法」と明記）、(2) Ubuntu PPA `ppa:daniestevez/gr-satellites`、
(3) cmakeによるソースビルド。このバグ自体は本ファイルの過去の記述（旧「gr-satellitesについて」節）
にも存在しており、実装当初から一度も正しく動作したことがなかったと考えられる。

**方針転換の経緯**: 当初は誤った案内文をconda-forge/PPA/ソースビルドの正しい内容に修正するだけの
予定だったが、ユーザーから「毎回ユーザーにビルドさせるのではなく、他のバンドル済みソフト
（ft8lib/libq65/libft4wsjt）と同様にこちらでCIビルド・バンドルできないか」との要望があった。

**ft8lib等との規模の違い**: ft8lib/libq65/libft4wsjtは依存の少ない小さなCライブラリ（FFTW・
Boost程度）で、単一の.so/.dylib/.dllをCIでビルドするだけで済む。一方gr-satellitesはGNU Radio
本体（Homebrewでフルインストールすると59個の依存関係・数百MB規模）に依存するPythonモジュールで、
実行には巨大なC++共有ライブラリ群＋専用Python環境が必要——単純に同じ手法は使えない。

**採用した方式（conda-forge抽出）**: 調査の結果、conda-forgeのGNU Radioは細かくパッケージ分割
されており、gr-satellitesが実際に必要とするのはQt/PyQt/GTK等のGUI要素を一切含まない最小構成
だと判明した:
- `gnuradio-core`（約4.45MB、win-64）: fftw・gsl・boost・portaudio・volk等に依存。Qt/PyQt不要
- `gnuradio-satellites`（約0.76MB）: `gnuradio-core`・`gnuradio-pmt`・`gnuradio-zeromq`・
  construct・matplotlib-base・numpy・requests等に依存。**`gnuradio-qtgui`への依存なし**
  （[conda-forge/gnuradio-satellites-feedstock](https://github.com/conda-forge/gnuradio-satellites-feedstock)
  のメタデータで確認済み）

`gr_satellites`はFBSAT59自身のPythonプロセスに読み込むのではなく**独立したサブプロセスとして
起動**する既存アーキテクチャ（`GrSatellitesBackend`）のため、バンドルする環境のPythonバージョンは
FBSAT59本体のPyInstaller用Pythonと一致させる必要がない。この前提のもと、CIで
`conda create --prefix ... -c conda-forge gnuradio-core gnuradio-satellites python=3.11`により
独立した環境を作成し、業界標準ツール`conda-pack`（環境を可搬なアーカイブ化し、展開後に
`conda-unpack`で絶対パスを再配置する）でパッケージ化してGitHub Releasesで配布する方式を採用した。

**実装済みコンポーネント**:
- `.github/workflows/build-gnuradio-satellites.yml`（新規） — macOS（`macos-14`、Apple Silicon）
  向けのみ実装。`workflow_dispatch`のみでトリガー（スケジュール実行は成功確認後に追加予定）。
  conda-forge環境作成 → `conda-pack`でアーカイブ化 → 別ディレクトリへ展開して`conda-unpack`＋
  `gr_satellites --help`＋`python -c "import gnuradio.satellites"`のスモークテスト →
  `gr-satellites-bundle`プレリリースタグへ`gr-satellites-macos-arm64.tar.gz`としてアップロード
  （既存のft8lib-bundle等と同じ命名パターン）
- `src/comms/telemetry/gr_satellites_install.py`（新規） — バンドル環境のパス解決を集約。
  `find_gr_satellites_executable() -> tuple[Path, bool] | None`（実行ファイルパス, バンドル済みか）・
  `bundled_satyaml_dir()`（衛星定義YAMLディレクトリ、`lib/python*/site-packages/satellites/satyaml/`
  をglobで探索）・`bundled_version()`（`conda-meta/gnuradio-satellites-*.json`のファイル名から
  バージョン文字列を抽出、サブプロセス起動不要）・`uninstall_bundle()`
- `src/comms/telemetry/gr_satellites_backend.py`（修正） — `detect_gr_satellites()`・
  `list_gr_satellites_*()`・`get_satellite_info()`が`gr_satellites_install`経由でバンドルを
  優先検出するよう変更。**PYTHONPATHのNumPy 1.x互換ハック（`_GR_PYTHONPATH`）は
  システムインストール（apt等）検出時のみ適用**——バンドル環境は自己完結しているため不要
  （`start()`内で`is_bundled`フラグにより分岐）
- `src/ui/gr_satellites_dialog.py`（大幅改修） — 「Install Bundled Environment (Recommended)」
  枠を新設し、ft8lib_dialog.pyと同じ`_InstallWorker`（QThread）パターンでダウンロード・展開・
  `conda-unpack`実行・Uninstallボタンを実装。手動インストール手順（Manual Installation）は
  正しい内容（conda-forge/PPA/ソースビルド）に修正した上でフォールバックとして残した

**サイズ見積もり（机上調査のみ、実測未確認）**: 約150〜400MB程度と推定（GitHub Releasesの
1ファイル2GiB制限は問題にならない）。

**実機（GitHub Actions macOS runner）での`workflow_dispatch`実行で発覚・解決したバグ
（2026-07-31）**:

1回目の実行はconda-forge環境作成（130パッケージ）・`conda-pack`（140MB、警告なし完走）・
`conda-unpack`（exit code 0）まで問題なく進んだが、`gr_satellites --help`が
`ModuleNotFoundError: No module named 'gnuradio'`で失敗した。`set +e`に変更し全診断を
1回のログにまとめて出す2回目の実行で、以下が判明した:

- 再配置後のPython（明示的に`bin/python`を指定して呼び出した場合）は`sys.path`が正しく
  `site-packages`を含み、`import numpy`（対照実験）・`import gnuradio`はどちらも成功
- 一方`import gnuradio.satellites`は失敗——ただしこれは**バンドルの不具合ではなく調査側の
  誤り**だった。conda-forgeの`gnuradio-satellites`パッケージを直接ダウンロードして中身を
  検証したところ、gr-satellitesは`gnuradio.satellites`ではなく**トップレベルの`satellites`
  パッケージ**として`site-packages/satellites/`にインストールされる（870ファイル確認、
  `gr_satellites`スクリプト自身も`import satellites.core`等と書かれている）。既存コード
  （`_SATYAML_DIR = Path(...) / "satellites" / "satyaml"`）は元々正しく実装されていた
- **実際のバグ**: `gr_satellites`エントリポイントスクリプトのshebangは`#!/usr/bin/env python`
  で、絶対パスを含まない。つまり`conda-unpack`（絶対パスの書き換えツール）には書き換える
  対象がそもそも無く、これは想定通りの「何もしない」だった。問題は、このスクリプトを
  `/tmp/gr-sat-smoketest/bin/gr_satellites`のように**直接実行**すると、shebangの
  `env python`解決がその時点の**呼び出し元シェルのPATH**に依存し、バンドル環境の
  Pythonとは無関係になってしまうこと（CIでは無関係なpythonが解決され、実際のユーザー
  環境でも同様——PATH上に何があるか次第で結果が変わる、再現性のないバグになる）

**修正**: `gr_satellites_install.py`に`resolve_gr_satellites_command()`を新設。バンドル版は
`[bundled_python_path, bundled_script_path]`という2要素のargvを返し、スクリプト自身の
shebangに一切頼らず明示的にバンドル済みpythonへ渡すようにした（`find_gr_satellites_executable()`
は表示専用として残し、実際の起動には使わない）。`gr_satellites_backend.py`の`start()`も
これに合わせて更新。CIワークフローのスモークテストも同じ「shebang経由（参考情報のみ、
失敗して当然）」と「明示的python経由（本番と同じ呼び出し方、これが実際のテスト対象）」の
両方を記録するよう修正し、`import gnuradio.satellites`の誤りも`import satellites`に訂正済み。

**教訓**: 「ログ上、明示的に呼び出したpythonでは動くのに、スクリプト単体を直接実行すると
動かない」という食い違いが出たら、まずスクリプトのshebang行そのもの（絶対パスかPATH依存か）
を疑うこと。`conda-unpack`は「絶対パスとして記録されているものを書き換える」ツールであり、
そもそも絶対パスが書かれていない箇所（`#!/usr/bin/env python`のような可搬性重視の慣習的
shebang）には無力——「ツールが仕事をしなかった」のではなく「そのツールの担当範囲外だった」
と気づくまでに複数回の実機検証が必要だった。

**3回目の`workflow_dispatch`実行で判明: `--help`のexit 1は仕様通り、パッケージング・修正は
成功（2026-07-31）**: 上記の修正（`resolve_gr_satellites_command()`）後の再実行で、
`import gnuradio`・`import satellites`は明示的python経由で共に成功し、
`gr_satellites --help`（explicit python invocation）もusageメッセージを正しく出力した。
ただしexit codeが1だったため、スモークテストは依然「失敗」と判定していた。実際の
`gr_satellites`スクリプト本体（conda-forgeパッケージから直接ダウンロードして中身を確認）を
読んだところ、これは仕様通りの挙動と判明:

```python
def main():
    parser = argument_parser()
    if len(sys.argv) >= 2 and sys.argv[1] in ['--version', '--list_satellites']:
        options = parser.parse_args()
        sys.exit(0)
    if len(sys.argv) <= 1 or sys.argv[1][0] == '-':
        parser.print_usage(file=sys.stderr)
        sys.exit(1)   # --help 単体はここに該当。意図的な exit 1
```

`gr_satellites`は「第1引数に衛星名/NORAD ID/YAMLパスを必須で取る」設計
（`gr_satellites <satellite> [options]`）で、`--version`・`--list_satellites`だけが
衛星名なしで動作する例外。`--help`単体はこのCLI自身の仕様上「不正な使い方」であり、
exit 1は正しい。**アプリ本体（`gr_satellites_backend.py`）は元々
`[python, script, str(norad), "--udp", ...]`という「NORAD IDを第1引数に渡す」正しい
呼び出し方をしていたため、一連の調査を通じて実は一度も影響を受けていなかった**——
問題があったのはCIのスモークテストが`--help`という誤った検証コマンドを使っていた点のみ。
スモークテストは`--version`（衛星名なしでも正常終了する）に差し替え済み。

**結論**: バンドル化アプローチ（conda-forge抽出 → `conda-pack` → `conda-unpack`）自体は
GNU Radioのような大規模パッケージでも機能することが実機で確認できた。残る未検証点は
「`--version`に差し替えた後のフルパス実行が緑になるか」（次回`workflow_dispatch`で確認予定）
のみ。

**macOS: `workflow_dispatch`でグリーン確認済み（2026-07-31）**。`gr-satellites-bundle`
プレリリースタグに`gr-satellites-macos-arm64.tar.gz`が公開済み。

**Linux・Windowsジョブを追加（2026-07-31）**: macOS成功を受け、同一ワークフロー内に
`build-linux`（`ubuntu-22.04`）・`build-windows`（`windows-latest`）ジョブを追加した。

- **Linux**: macOSジョブとほぼ同一構成（`conda-pack`はmacOS特有のcodesigning等の癖を持たない
  ため、緑になる可能性が高いと見ている）。診断ステップもmacOSと同じ内容
- **Windows**: Windows conda環境はmacOS/Linuxと**根本的にレイアウトが異なる**
  （`bin/<name>`のshebangスクリプトではなく`Scripts/<name>.exe`という自己完結型の
  ランチャーstub、`python.exe`は`bin/`ではなく環境ルート直下）ため、macOS/Linuxで機能した
  「`bundled_python bundled_script`」形式がそのまま通用するとは限らない。この点は
  **本プロジェクトで一度も検証したことがなく未知数**なため、Windowsのスモークテストは
  あえて「探索的な診断優先」（`set +e`でPython importチェックのみをSTATUSに反映し、
  `Scripts/gr_satellites.exe`ランチャーの起動確認は参考情報として記録するのみで
  合否判定に含めない）にしてある。実行結果を見てから
  `gr_satellites_install.py`の`resolve_gr_satellites_command()`のWindows分岐を
  調整する前提
  - CI・アプリ側（`gr_satellites_dialog.py`）双方で、当初`python.exe conda-unpack.exe`
    （`.exe`ランチャーの中身をPythonソースとして渡そうとする、明らかに不正な呼び出し）と
    誤って実装していたバグを実装中に発見・修正済み（`Scripts/conda-unpack.exe`は
    自己完結型ランチャーなので直接実行が正しい）
  - 配布形式はWindowsも含め全プラットフォームで`.tar.gz`に統一（`zipfile`同様
    `tarfile`もPython標準ライブラリでクロスプラットフォームに動作するため、
    外部ツール依存を増やす`.zip`を使う理由がない）

**1回目の`workflow_dispatch`実行結果（2026-07-31）**: Linux・macOSは両方とも
Uploadステップまで到達しグリーン（`gr-satellites-bundle`に
`gr-satellites-linux-x86_64.tar.gz`・`gr-satellites-macos-arm64.tar.gz`が公開済み）。
**Windowsのみ失敗**——原因は`tar (child): Cannot connect to D: resolve failed`。
`$RUNNER_TEMP`はネイティブWindowsパス（`D:\a\_temp`）で、これに素朴に`/file`を連結すると
`D:\a\_temp/file`という**バックスラッシュとスラッシュが混在したパス**になり、Git Bash付属の
`tar`（bsdtar）が先頭の`D:`を「ドライブレター」ではなく`ホスト名:ファイル`という
リモートtar構文のホスト名と誤解釈していた。`conda-pack`自体（Pythonツール、Windows APIは
`/`と`\`を区別なく受け付ける）や`ls`はこの混在パスでも問題なく動いていたため、
`tar -xzf`だけがこの罠にはまっていた。

**修正**: `cygpath -u "$RUNNER_TEMP"`でPOSIX形式のパス（例: `/d/a/_temp`）に一度変換し、
以降すべてのステップでその変換済みパスを使うよう統一（`Pack`ステップで変換して
`$GITHUB_ENV`経由で`Smoke-test`ステップに引き継ぐ設計）。

**2回目の`workflow_dispatch`実行で3プラットフォームとも完全グリーン（2026-07-31）**:
`cygpath`修正後の再実行で、macOS・Linux・Windowsすべて`Upload`ステップまで到達。ただし
Windowsのスモークテストは`Scripts/gr_satellites.exe`を「参考情報のみ」（合否判定に含めない）
にしていたため、この時点では**実際に`gr_satellites`を起動できることまでは確認できていなかった**。
ログを精査したところ`Scripts/`には`conda-unpack`関連ファイルしかなく、`gr_satellites`が
見当たらないことが判明した。

**根本原因**: conda-forgeのWindowsパッケージ規約では、`Scripts/`は純粋なPythonの
console-scriptエントリポイント専用で、GNU RadioのOOTモジュール（gnuradio-satellitesのような
C/C++寄りのパッケージ）の実行ファイルは**`Library/bin/`**に配置される。実際にwin-64版
`gnuradio-satellites` .condaパッケージを直接ダウンロードして中身を確認したところ、
`Library/bin/gr_satellites.py`（素のPythonスクリプト、macOS/Linuxの`bin/gr_satellites`と
同内容）と`Library/bin/gr_satellites.exe`（ランチャー、未使用）が存在することを確認した。

**修正**: `gr_satellites_install.py`の`_bundled_executable_path()`のWindows分岐を
`Scripts/gr_satellites.exe`から`Library/bin/gr_satellites.py`に変更。既存の
`resolve_gr_satellites_command()`（`[bundled_python, bundled_script]`形式で明示的に
呼び出す設計）はそのまま活用でき、macOS/Linuxで実証済みの安全な呼び出しパターンが
Windowsでもそのまま機能する。CIのWindowsスモークテストも、この正しいパスを使った
`gr_satellites --version`をmacOS/Linuxと同じ「合否判定に使う本番相当のテスト」に格上げ済み。

**3回目の`workflow_dispatch`実行で3プラットフォームとも完全グリーン、CI側の検証は完了
（2026-07-31）**: `Library/bin/gr_satellites.py`への修正後の再実行で、macOS・Linux・Windows
すべて`gr_satellites --version`が実際に`gr_satellites v5.9.0`のバージョン文字列を正しく出力し
「Smoke test passed.」で終了することを確認した。これでCI側（ビルド・`conda-pack`・
`conda-unpack`・実際のCLI起動）は3プラットフォームとも実証済み。

**`gr_satellites_dialog.py`の追随修正**: 上記CI調査と並行してダイアログ側（`_InstallWorker`の
ダウンロード拡張子・展開・`conda-unpack`呼び出し分岐）もすでに正しい実装になっていたため、
追加の実装は不要だった。唯一、Windows向け手動インストール手順（バンドルを使わない代替経路）
の案内文に残っていた「Windows対応は限定的、WSL2推奨」という古い注記だけを削除し、
バンドル版が推奨経路であることを明記するよう更新した。

**残る未検証点（2026-07-31時点）**: CI上のスモークテストでの動作確認までは完了したが、
「実際にユーザーのマシンでHelp画面の『Download & Install』を押し、GitHub Releasesから
実際にダウンロード・展開して、Telemetryタブで実際の衛星テレメトリーを受信できるか」という
アプリ経由のエンドツーエンド検証はまだ行っていない。

#### AI-CW デコーダーについて
- 候補: **morse-decoder**（PyTorch CNN ベース）・**DeepMorse**・**cwdecoder**（RNN）など
- 従来のゼロクロス検出方式より S/N 比の低い信号でも高精度にデコード可能
- 内蔵実装方針: sounddevice または SdrAudioSource から 8kHz PCM を取得 → Python 内で推論
- モデルファイル（数 MB 程度）はアプリバンドルに同梱するか、初回起動時に自動ダウンロード
- ライセンスに注意（MIT または Apache 2.0 のモデルを選定すること）

### SDR Device Installation ダイアログ（Help メニュー）

- USB VID/PID スキャン（`pyusb`）でデバイスを識別
- SoapySDR インストール状態を表示
- **Linux**: `pkexec apt-get install` でボタン操作による自動インストール
- **Windows**: PothosSDR はブラウザでダウンロードページを開く（`QDesktopServices.openUrl`）、Zadig は直リンク `.exe` をダウンロードして起動。いずれもウィザード操作はユーザーが手動で行う
- **macOS**: Homebrew があれば `brew install` を自動実行
- すでにインストール済み環境では即 `✅ Ready` 表示

### 依存パッケージ（optional）

```toml
[project.optional-dependencies]
sdr = [
    "pyusb>=1.2",         # USB VID/PID スキャン（SDR Device Installation 用）
    "scipy>=1.12",        # DSP フィルタ・ヒルベルト変換
    "sounddevice>=0.4",   # 音声出力（PortAudio ラッパー）
]
# SoapySDR はシステムパッケージ経由のため pip dependencies に含めない
```

### SDR のドップラー補正 — 再同調デッドバンド（2026-08-06 実装）と、デジタル補正への発展余地

#### 発端となった問い

「gr-satellites や SatDump は SDR の中心周波数をドップラー補正しなくても復調できると聞くが、
本当か。ならば FBSAT59 もそれらの受信中は補正しないほうがよいのではないか」というユーザーからの
指摘。調査の結果、**SatDump については正しく、しかも FBSAT59 は既にそう実装されていた**一方、
**gr-satellites については一般には成り立たない**と判明した。加えて、gr-satellites 経路には
「補正するか否か」とは別の実害（毎秒のハードウェア再同調）が潜んでいた。

#### 前提: 2つの経路はドップラーの扱いが根本的に違う

| 経路 | SDR の所有者 | 中心周波数 | ドップラー補正 |
|---|---|---|---|
| METEOR / HRPT（SatDump） | **SatDump 自身** | `METEOR_PIPELINES` のハードコード値を `--frequency` で固定 | **なし**（元から） |
| Telemetry（gr-satellites） | FBSAT59（`SdrRigAdapter` = Rig 1/2） | `_doppler_cycle()` が毎サイクル書き込み | あり |

`MeteorTab._on_start()` は起動前に `_disconnect_sdr()` で FBSAT59 側の SDR を切り離すため、
**Rig Settings > SDR Settings のサンプルレート設定も METEOR には一切効かない**（SatDump へ渡る
のは `METEOR_PIPELINES` の `samplerate`: LRPT 1.2 Msps / HRPT 3 Msps）。この点はユーザーが
誤解しやすいので注意。

#### なぜ SatDump は補正不要で、gr-satellites は必要なのか

ドップラー量をシンボルレートと比較すると明確に分かれる（LEO の最大レンジレート ≒ 7 km/s）。

| 対象 | 周波数 | 最大ドップラー | シンボルレート | 比 | 判定 |
|---|---|---|---|---|---|
| LRPT | 137 MHz | ±3.2 kHz | 72 kbaud | 4% | Costas ループが余裕で吸収 |
| NOAA HRPT | 1698 MHz | ±40 kHz | 665 kbaud | 6% | 同上 |
| BPSK 1200（AO-73 等） | 145 MHz | ±3.4 kHz | 1.2 kbaud | 280% | **補正必須** |
| FSK 9k6 G3RUH | 435 MHz | ±10 kHz | 9.6 kbaud | 104% | **補正必須** |

gr-satellites 側の実際の許容範囲は、ドキュメントではなく実装
（`python/components/demodulators/*.py`）から読み取れる:

- **BPSK**: 前段 `freq_xlating_fir_filter` のローパスが `cutoff = baudrate * 2.0` ←
  これがハードリミット。その後段の `fll_band_edge_cc` の引き込みは概ね ±(0.5〜1)×baudrate
- **FSK（IQ）**: Carson 則のローパス `|deviation| + baudrate/2` がハードリミット。通過さえ
  すれば後段の `quadrature_demod` → `dc_blocker_ff` が静的オフセットを吸収する
- **AFSK（IQ）**: Carson 則が `fm_deviation + af_carrier + |deviation|` = 既定 3000+1700+500
  = **±5.2 kHz**

さらに FBSAT59 は `--iq` で渡していて `--f_offset` を指定していないため、gr-satellites は
**信号が IQ の中心（DC）にある前提**で動く。したがって gr-satellites 経路でドップラー補正を
止めるのは不可。「gr-satellites は補正不要」という通説は、広帯域 IQ 録音の後処理運用か、
SatNOGS 時代の FM/AFSK の慣行が一般化されて伝わったものと思われる。

#### 実際の問題は「補正の掛け方」だった — 毎秒のハードウェア再同調

`SdrRigAdapter.set_vfo_frequencies()` にはしきい値が無く、`_doppler_cycle()` が毎サイクル
（既定1秒）無条件に `set_center_freq()` を呼んでいた。**10分のパスで約600回**の再同調になる。
ハードウェア再同調はチューナ PLL の再ロックを伴い位相が不連続になる（RTL-SDR では数サンプル
落ちることもある）。1200 baud の AX.25 フレームは 256 バイトで約1.7秒かかるので、
**事実上すべてのフレームが1回以上の再同調で分断されていた**計算になる。

#### 実装した対策（案B・2026-08-06）

`SdrRigAdapter` に再同調デッドバンドを追加（`_SDR_RETUNE_DEADBAND_HZ = 200.0`）。

- `set_frequency()` は、前回**実際に書き込んだ**値から 200 Hz 未満の要求を握りつぶす。
  基準を「前回の要求値」ではなく「前回の書き込み値」に置くのが要点で、要求値基準にすると
  小刻みな変化が延々と抑制され続けて周波数が青天井にずれる
- 書き込みが失敗した場合は基準を更新しない（失敗した値が基準になると以後ずっとずれる）
- `set_retune_deadband(hz)` で変更可能（`0` で従来どおり毎サイクル再同調に戻る）。現状 UI からは
  設定できず、コード上の定数のみ
- 効果は概算で再同調回数が約1/6（435 MHz で約100回/パス、145 MHz で約34回/パス）

**デッドバンドを迂回する経路（重要）**: 100 Hz 刻みの Passband Tune など、
**ドップラー以外の理由で目標周波数が動く操作は、デッドバンドに飲み込まれてはならない**。
`SdrRigAdapter.invalidate_retune_cache()` を呼ぶと次回の書き込みが無条件になる。
`MainWindow._invalidate_sdr_retune_cache()`（Rig 1/2 のうち SDR の方を探して呼ぶヘルパー）が
以下から呼ばれる:

| 呼び出し元 | 理由 |
|---|---|
| `_on_sdr_tune_offset()` | Passband Tune の矢印（最小 100 Hz、デッドバンド未満） |
| `_on_sdr_manual_freq_requested()` | Freq 欄への手入力 |
| `_on_sdr_lock_changed(False)` | Lock 中は一切書いていないので基準が陳腐化している |
| `connect()` / `disconnect()`（アダプター内部） | デバイスの状態が未知 |

トランスポンダー変更・衛星選択解除は、`SdrControlWidget.reset_tune_offset()` が
`tune_offset_changed(0.0)` を emit → `_on_sdr_tune_offset()` に届くため、**既存のシグナル
連鎖で自動的にカバーされている**（個別の呼び出し追加は不要）。

**既知のトレードオフ**: SDR の音声を SSB/CW で聴いている間は、最大 200 Hz の残差が音程の
ふらつきとして聞こえる。NFM では聞き取れず、デジタル復号系（gr-satellites・CW Decoder 等）
には無関係。SSB/CW 常用で気になる場合は `set_retune_deadband()` を小さくするか、
下記の案Aに進むこと。

**未検証**: 再同調グリッチが実際にどれだけフレーム損失を招いていたかは実測していない。
この対策自体が、その仮説の検証になる位置づけ（改善が見えなければ原因は別のところにある）。

テスト: `tests/test_rig.py::TestSdrRetuneDeadband`（フェイクデバイスで書き込み回数を検証。
初回書き込み・抑制・境界越え・基準が前回書き込み値であること・`invalidate` による強制書き込み・
書き込み失敗時に基準を汚さないこと・`0` で無効化・切断でリセットを網羅。実 SDR 不要）。

#### 案A（未実装）— ハードウェアを固定し、ドップラーをデジタルに適用する

デッドバンドは再同調の**回数を減らす**だけで、位相不連続を**ゼロにはできない**。しかも
435 MHz の高仰角パスでは TCA 付近のドップラー変化率が約 170 Hz/s に達するため、
**最も信号が強いまさにその時間帯だけ、デッドバンドがほとんど効かない**（1.2秒に1回は再同調
する）。根本解決するなら、チューナは公称周波数に固定したまま、IQ サンプル列に

```
出力[n] = 入力[n] × exp(-j·2π·Δf·n / fs)     Δf = ドップラー補正後の周波数 − 固定した中心周波数
```

を掛ける方式（デジタル局発）になる。位相アキュムレータをチャンク間で持ち越せば位相は完全に
連続で、`Δf` を更新した瞬間も波形が途切れない。SatNOGS や `gr-gpredict-doppler` と同じ考え方。

**既存の再利用可能な部品**: `src/comms/ax100digi/audio_bridge.py` の `FrequencyShifter` が
まさに「位相アキュムレータを持ち越すステートフルな複素ミキサ」。AX100 Digi 用に書いたものだが、
`Δf` を実行中に差し替えられるようにする改修だけで流用できる。

**実装位置の設計判断（未決）**:
- **A-1: `SDRPipeline` の中に入れる** — 下流すべて（SDR Control の音声復調・スペクトラム・
  gr-satellites への UDP 転送）が補正済みストリームを受け取る。SDR で音声も聴く場合に
  現在と同じ使い勝手が保てるので本命。ただしスペクトラム表示の「中心周波数マーカー」が
  ハードウェアの実周波数を指すのか補正後の論理周波数を指すのか、意味づけの整理が要る
- **A-2: `_UdpIqForwarder.push_samples()` だけに入れる** — 変更範囲は最小だが、
  gr-satellites 受信中はハードウェアが公称周波数で固定されるため、同時に SDR の音声を聴くと
  衛星が中心からずれて聞こえる

**CPU コスト（この開発機 i3-N300 で実測）**: 1コアに対し 2.4 Msps で 10.4% / 1.024 Msps で
3.3% / 250 ksps で 0.8%。Celeron N4000（Goldmont Plus・2コア・**AVX 非対応**）は概ね 3〜4倍
遅いと見込まれるため、2.4 Msps では1コアの 35〜40%（＝2コア全体の約20%）に達する。
250 ksps なら約3%で実用範囲。

#### 非力なマシン（Celeron N4000 級）向けの設定指針

実は案A/Bの選択より、**SDR サンプルレートのほうが桁違いに効く**。gr-satellites の初段ローパスは
遷移幅を `baudrate * 0.2` で設計するため、**タップ数がサンプルレートに正比例して増える**:

| SDR サンプルレート | FIR タップ数（BPSK 1200 の場合・概算） | 演算量 |
|---|---|---|
| 2.4 Msps（既定値） | 約 33,000 | 約 400 M MAC/s |
| 250 ksps | 約 3,400 | 約 41 M MAC/s |

UDP ループバックの転送量も complex64 で 19.2 MB/s → 2 MB/s と1/10になる。**UDP には
フロー制御がないため、gr_satellites が処理落ちするとエラーも出ずにデータグラムが捨てられ、
単に「デコードされない」という形で現れる**——非力なマシンで最もはまりやすい失敗モード。

**アマチュア衛星のテレメトリー（1200〜9600 baud）に 2.4 MHz の帯域は完全に過剰**なので、
Rig Settings > SDR Settings のサンプルレートを **250 kHz**（`_SAMPLE_RATES` の先頭、
[rig_dialog.py](src/ui/rig_dialog.py)）にすることをまず勧める。既定が 2.4 MHz なのは
汎用の SDR Control 用途に合わせたもので、gr-satellites 用途では不適切。

注意: RTL-SDR は 300 k〜900 kHz のレートをハードウェア的に受け付けない。`_SAMPLE_RATES` が
250 kHz の次に 1.0 MHz へ飛んでいるのはそのためで、中間値を追加してはいけない。

METEOR 側は前述のとおりレートがハードコードで、LRPT（72 kSym/s・占有帯域 約115 kHz）なら
理屈の上では 250 ksps の窓（±125 kHz）に収まるが、SatDump 側のリサンプラ最低入力レート等の
制約は未検証。HRPT（665 kSym/s・占有帯域 約1.1 MHz）は 250 ksps では原理的に不可能。

### Remote SDR（SoapyRemote）対応（2026-07-14 実装・GitHub Issue #12）

#### 背景

ユーザーから「アンテナ設備は別棟（裏庭の小屋等）にあり、そこに置いたSDRをネットワーク経由で
使いたい」という要望（[Issue #12](https://github.com/JF9SOM/FBSAT59/issues/12)）があった。
SoapySDR公式の`SoapyRemote`モジュールがまさにこの用途向けに存在し、クライアント側は普通の
SoapySDRモジュールとして振る舞うため、既存の`SdrDevice`アーキテクチャにそのまま乗せられる。

#### なぜWindowsのRTL-SDR/HackRF ctypesバイパスと無関係なのか

WindowsでRTL-SDR/HackRFだけ`SoapySDR::Device::make()`を経由せずctypes直接呼び出しに
バイパスしている理由は、SoapySDRの列挙・オープン処理が`libusb_init()`等を複数回呼ぶことで
**WinUSBの内部ハンドルキャッシュが破損する**という、ローカルUSBデバイスアクセス特有の問題
（詳細は「SDR 機能（v0.1.0 時点で実装済み）」セクション参照）。

SoapyRemoteのクライアント側はUSB/libusbを一切呼ばない純粋なTCP/IPネットワーククライアント
（実際にUSBを触るのは物理的にSDRが繋がっているリモート側のマシンだけ）であり、この問題の
対象外。`SdrDevice.open()`（[device.py](src/sdr/device.py)）は`driver=="rtlsdr"`/`"hackrf"`
の場合だけ明示的にctypesバイパス分岐に入り、それ以外（Airspy・AirspyHF・そして`remote`も
該当）は汎用の`SoapySDR.Device(args)`パスを通る。Airspy/AirspyHFはこの汎用パスでWindows実績
があるため、`remote`も同じ土俵に乗ると考えられる（実機・実際のリモートSoapySDRServerでの
動作確認は次回のパス待ち）。

#### デバイス文字列の仕様（SoapyRemote公式wiki確認済み）

```
driver=remote, remote=<host>[:port], remote:driver=<実機ドライバ名 例:rtlsdr>
```

同一LAN内であればUDPブロードキャストで自動検出され`SoapySDR.Device.enumerate()`結果に
`driver="remote"`として現れる。従来`_NON_SDR_DRIVERS`（[device.py](src/sdr/device.py)）が
これを除外していたため、実際には動く状態でも一覧に出てこなかった。除外リストから`"remote"`
を削除済み（`audio`/`null`/`mircsdr`は引き続き除外）。

#### 手動ホスト指定（LANブロードキャストが届かない場合）

VPN経由・ルーティングを跨ぐ構成等、自動検出が機能しない環境向けに、Rig Settings > SDR
Settingsに **「Add Remote Host…」** ボタンを追加（[rig_dialog.py](src/ui/rig_dialog.py)の
`_AddRemoteHostDialog`）。Host・Port（デフォルト55132）・任意のRemoteドライバー名ヒント・
任意の表示名を入力すると、上記デバイス文字列を組み立てて`SdrDeviceInfo(driver="remote", ...)`
としてデバイスコンボに追加する。

**永続化**: 新規テーブルは作らず、既存の`sdr_settings`（`app_settings`テーブルの単一JSON
キー）に`remote_hosts`フィールドを追加する形で保存する（`_SdrSettingsPanel.save()`/`load()`）。
これにより`RigSettingsDialog`側の読み書きプラミング（`self._conn`経由のapp_settings
読み書き）を一切変更せずに済んだ。

**実際の接続時の扱い**: `MainWindow._build_sdr_rig_adapter()`（main_window.py）は元々
`sdr_settings['device_args']`という生のSoapySDR argsディクショナリだけを見て
`SdrDeviceInfo`を再構築しており、コンボのインデックスや列挙結果には一切依存していない。
そのため「Add Remote Host…」で追加したエントリも、選択して保存すれば以降の起動時に
再列挙・再検出なしでそのまま接続対象になる（手動追加データが自動検出に優先する、という
本プロジェクト全体の設計方針——手動登録トランスミッタ・手動TLE等——と同型のパターン）。

**Remove**: 選択中のデバイスが「Add Remote Host…」で追加した保存済みエントリの場合のみ
「Remove」ボタンが有効になる（実ハードウェアやLANブロードキャストで自動検出された`remote`
エントリでは無効のまま。ローカルの保存リストから消す対象ではないため）。

#### パッケージ配布状況（全プラットフォーム調査済み）

| OS | パッケージ | 対応方法 |
|---|---|---|
| Linux | `soapysdr-module-remote`（apt） | ユーザーが自分でシステムインストール（rtlsdr/hackrf等の既存モジュールと同じ扱い。CIバンドル対象外——LinuxのAppImageはSoapySDR自体をCIでバンドルしていない） |
| macOS | `soapyremote`（Homebrew） | 同上（macOSもCIでSoapySDRをバンドルしていない） |
| Windows | `soapysdr-module-remote`（conda-forge, win-64, v0.5.2） | **CIでバンドル**（Windowsのみ`scripts/extract_soapy_conda.py`経由でconda-forgeパッケージを抽出する既存方式を踏襲） |

サーバー側（実際にSDRが繋がっているマシン）のセットアップはFBSAT59の管轄外（ユーザー自身が
`SoapySDRServer`を起動する）。Help > SDR Device Installationダイアログに「Note for Remote
SDR Users」として案内文を追加済み（[sdr_install_dialog.py](src/ui/sdr_install_dialog.py)）。

#### Windows CIバンドルの詳細（依存関係調査済み・低リスク確認済み）

`soapysdr-module-remote-0.5.2-h23704b7_2.tar.bz2`の`info/index.json`を実際にダウンロードして
`depends`フィールドを確認したところ:
```
soapysdr >=0.8.0,<0.9.0a0
vc >=14.1,<15.0a0
vs2015_runtime >=14.16.27012
```
**Boostへの動的リンク依存なし**（静的リンク済みと推定）。`vc`/`vs2015_runtime`は他の
conda-forgeパッケージ（既存のsoapysdr本体等）も要求する標準MSVC再頒布ランタイムで、
GitHub Actions Windowsランナーには元々インストール済み。ビルドタグ`h23704b7`は既存の
`soapysdr-module-hackrf-0.3.4-h23704b7_0`・`soapysdr-module-airspy-0.2.0-h23704b7_0`と同じで、
現在ピン留めしているSoapySDR本体（`soapysdr-0.8.1-py311haef1a59_6.conda`）と同一ABI世代
であることも確認済み。パッケージ内容は単一ファイル
`Library/lib/SoapySDR/modules0.8/remoteSupport.dll`のみで、`extract_soapy_conda.py`の
既存の汎用ロジック（`"modules" in name`のDLLを`soapy-win64/modules/`へ）がそのまま対応する
（rtlsdrSupport.dllのような特別スキップ処理は不要）。以上の理由から、過去にPlutoSDR追加時に
遭遇したようなCI不安定化のリスクは低いと判断したが、実際のビルド成否は`workflow_dispatch`
での確認が必要。

#### 実運用で発覚した「remoteSupport.dllは正常にロードされるがレジストリに登録されない」不具合と、ソースからの自前ビルドへの切り替え（2026-07-16 調査・対応）

v0.2.15リリース後、Issue #12の報告者（Windows・PothosSDR併用環境）から「Add Remote Hostで
リモートホストを追加し接続すると常に`SoapySDR::Device::make() no match`で失敗する」という
報告を受け、複数ラウンドの診断（ログ収集・ファイアウォール診断・`LoadLibrary`直接テスト用の
診断スクリプトを都度作成）で以下を順に否定した:

1. **PothosSDR併用によるモジュール重複**（`duplicate entry for remote`ログ） — PothosSDR
   完全アンインストール後も再現し否定
2. **Windowsファイアウォールによるブロック** — FBSAT59に対する明示的Inbound Allowルールが
   既に存在・Outboundは既定で許可・管理者権限実行でも再現し否定
3. **アンチウイルスによる隔離** — 直近のWindows Defender検出履歴なしで否定
4. **`soapy_modules`配下のDLLが一般的にロードできない問題** — 診断スクリプト第1版は
   `SetDllDirectory`を呼ばずに素の`LoadLibrary()`でテストしていたため、`main.py`の
   `os.add_dll_directory(_MEIPASS)`と同じ検索パスが再現できておらず、5モジュール全てが
   `ERROR_MOD_NOT_FOUND(126)`という偽陽性を出していた（スクリプト自身のバグ、実アプリの
   不具合ではない）。`SetDllDirectory`を追加した修正版で再テストすると
   **`remoteSupport.dll`を含む全モジュールが`LoadLibrary`単体では正常に成功**することを確認

上記4点を踏まえて`pothosware/SoapySDR`の`lib/Modules.in.cpp`・`lib/Factory.cpp`・
`lib/Registry.cpp`・`client/Registration.cpp`（SoapyRemote側）のソースを直接精査した結果、
以下が判明した:

- `Modules.in.cpp`の`loadModules()`は**エラーがない場合は一切ログを出力しない**仕様
  （`errorMsg`が空文字なら`SoapySDR::logf()`自体を呼ばない）。そのため実機ログで
  `remoteSupport.dll`の`loadModule(...)`行が一度も出ないことは「スキップされている」
  のではなく「エラーなくロード・登録に成功している」ことを示す可能性がある——という
  解釈にいったん傾いたが、`Factory.cpp`の`Device::make()`を読むと「no match」は
  `Registry::listMakeFunctions()`に指定した`driver`名（`remote`）のキーが
  **一つも見つからない場合にのみ**発生する処理であることが確定した。ABI不一致・重複の
  どちらのエラーであっても`Registry::Registry()`コンストラクタは`errorMsg`を設定し、
  それは必ず`loadModules()`側でログに出る（`getLoaderResult()`経由）。エラーログが
  一切ないのにレジストリにキーが存在しない、という組み合わせは、`LoadLibrary`自体は
  成功しているのに`client/Registration.cpp`末尾の
  `static SoapySDR::Registry registerRemote("remote", &findRemote, &makeRemote,
  SOAPY_SDR_ABI_VERSION);`という**ファイルスコープの静的初期化子がDLLロード時
  （DllMain相当）に正しく実行されていない**ことを強く示唆する

- 独立した参考データとして、同じくSoapySDRベースの別のOSS衛星追尾ソフト
  [SkyRoof](https://github.com/VE3NEA/SkyRoof)（GPLv3、C#からSoapySDRをP/Invoke経由で
  直接呼び出す実装）のソースを確認したところ、`Vendor/SoapySDR/SDR/remoteSupport.dll`に
  同梱されているバイナリのバージョン文字列は`0.6.0-c09b2f1`——pothosware/SoapyRemoteの
  公式タグ`0.5.2`（2020-07-20リリース）より後、`c09b2f1`コミット（2020-11-05、
  ref clock rate API追加）を含む非公式ビルドだった。FBSAT59がconda-forge経由で同梱していた
  のは公式`0.5.2`タグそのもの。SkyRoof側は同じ「remote」ドライバで正常に動作している
  （報告者もSkyRoofからは同じリモートSDRに問題なく接続できると証言）ことから、
  **conda-forgeの0.5.2ビルド固有の問題**（ビルド設定・リンカー最適化等でこの静的
  初期化子が欠落している可能性）を疑う根拠として扱った

以上を受け、`rtlsdrSupport.dll`（WinUSB対応で既にOsmocomソースから自前ビルド済み、
「Rig-Specific Implementation Notes」以前のセクション参照）と同じ方針で、
**`remoteSupport.dll`もconda-forgeバイナリを信用せず`pothosware/SoapyRemote`の
ソース（`master`、2026-07-16時点のHEAD。Changelog.txt上は0.5.2以降"0.6.0 (pending)"の
まま停滞しており、SkyRoofが使ったコミットと機能的に同一）から自前ビルドする**方針に
切り替えた（`.github/workflows/ci.yml`「Build SoapyRemote client from source (Windows)」
ステップ、`write_soapy_cmake_config.py`が生成する`SoapySDRConfig.cmake`をRTLSDRビルドと
共用）。

**`server`サブディレクトリの除外が必須**: `SoapyRemote`の`CMakeLists.txt`は
`add_subdirectory(client)`と`add_subdirectory(server)`を無条件で呼ぶが、
`server/CMakeLists.txt`は`target_link_libraries(SoapySDRServer PRIVATE SoapySDR
SoapySDRRemoteCommon)`という**名前空間なしの生の`SoapySDR`ターゲット**を参照する。
`write_soapy_cmake_config.py`が生成する簡易`SoapySDRConfig.cmake`は
`SoapySDR::SoapySDR`（名前空間付きIMPORTEDターゲット）しか定義しないため、
`add_subdirectory(server)`を残したままではconfigure段階で
「target "SoapySDR" not found」エラーになる。FBSAT59はSoapyRemoteの
**クライアント側（`remoteSupport.dll`）のみ**必要（サーバー機能は使わない）なので、
`scripts/patch_soapyremote_client_only.py`で`add_subdirectory(server)`の行を
コメントアウトしてから configure する（`add_subdirectory(system)`はLinux限定の
条件分岐が既についているため未対応のまま無害）。

**検証状況（2026-07-16時点）**: CIビルド自体（`workflow_dispatch`）の成否確認は未実施。
さらに重要な点として、**この自前ビルドが実際にIssue #12の症状を解消するかどうかも
まだ実機で未検証**——「conda-forgeビルド固有の問題」という仮説はSkyRoofとのバージョン差
という状況証拠に基づくもので、確定した原因究明ではない。次のリリースで報告者（および
開発者自身が用意する検証環境）に実際にテストしてもらい、症状が解消するかで仮説の
正否を判断する。解消しない場合はDllMain内の静的初期化子が実行されない別の原因
（Windows特有のローダーロック絡みの問題等）を追う必要がある。

---

## Communications 機能設計方針（2026-06-12 確定・v0.2.0 基本実装済み）

### 概要

メニューバーに **Communications** メニューを新設し（Radio と Autotrack/Record の間）、
APRS・FT4・SSTV 等のデジタル通信機能をサブメニューとして追加していく。
各機能は専用タブとして開き、× ボタンで個別にクローズできる非常駐タブとして実装する。

**メニュー構成:**
```
File / Satellite / Radio / Communications / Autotrack/Record / View / Help
                              └── APRS        （v0.2.0 実装済み）
                              └── Telemetry   （v0.2.0 実装済み）
                              └── SSTV / SSDV （feature/communications 実装済み）
                              └── FT4         （feature/communications 実装済み）
```

---

### SSTV / SSDV 機能設計（feature/communications 実装済み）

#### 概要

SSTV（アナログ画像伝送）と SSDV（デジタル画像伝送）をひとつの **SSTV タブ**で受信・表示する。
音声入力（リグサウンドカード / SDR）を共通インターフェースとし、デコーダーをモードで切り替える。

#### 受信経路と対応モード

| モード | 伝送方式 | 入力 | デコーダー | 代表衛星 |
|---|---|---|---|---|
| SSTV | FM 音声変調（アナログ） | sounddevice / SDR 音声出力 | pySSTV（純Python） | ISS（Robot36/PD120）・IO-86 等 |
| SSDV（音声経路） | FM 音声変調（デジタルパケット） | sounddevice / SDR 音声出力 | `ssdv` CLI ツール（サブプロセス） | 一部 CubeSat |
| SSDV（AX.25経路） | AX.25 パケット | Telemetry パイプライン共用 | `ssdv` CLI ツール | JY1Sat 等（将来） |

#### ディレクトリ構成

```
src/
├── comms/
│   ├── sstv/
│   │   ├── __init__.py
│   │   ├── decoder.py      # SstvDecoder — pySSTV ラッパー・音声チャンク受け付け
│   │   ├── file_decoder.py # load_audio_mono() — 録音ファイル(MP3/WAV)→44100Hzモノラルへ変換（後述）
│   │   └── ssdv.py         # SsdvDecoder — ssdv CLI サブプロセス管理・パケット再構成
```

#### 自動タブオープン（Radio Control 連動）

トランスポンダー選択時に description / mode を検査し、対応タブを自動オープンする。

```python
desc = xpdr.description.upper()
if "SSTV" in desc or "SSDV" in desc:
    → SSTV タブを自動オープン
if "APRS" in desc or xpdr.mode == "AFSK":
    → APRS タブを自動オープン
```

- Communications メニューからの手動オープンも引き続き可能（機能の存在をユーザーに示す）
- 既に開いている場合は重複して開かない（フォーカスを移動するだけ）

#### 録音ファイルからの画像デコード（2026-07-24 実装、`📂 Decode Recording…`）

Radio Control / SDR Control の音声REC機能（共に `~/audio_recordings` にMP3保存）で録れた
音声から、ライブ受信を待たずにSSTV画像を再生成できる。**SSTVモード専用**（SSDVはAX.25パケット
経由でデコードする設計のため、音声ファイル再生では対応不可）。

- **MP3→PCM変換**: `soundfile`（libsndfile 1.1+同梱、`pip install soundfile`のみでffmpeg等の
  外部ツール不要）を新規依存として追加（`pyproject.toml` の `packaging`/`sdr` extras）。
  `file_decoder.load_audio_mono(path, target_rate)` がファイルを読み込み・モノラル化・
  44100Hzへリサンプル（`scipy.signal.resample_poly`、AudioDeviceManagerの`_resample()`と
  同じアンチエイリアシング手法を独立実装——モジュール間の密結合を避けるため意図的に別実装）
- **実際の音声再生はしない**（無音・高速デコード）。ファイル全体を`SstvDecoder.push_samples()`に
  **一度だけ**渡す。ライブ経路のように数秒ごとのチャンクに分けて渡すと、`SstvDecoder._process()`
  が呼び出しのたびに新しい`image`配列・`line=0`から再スタートする実装のため、録音全体を
  横断する1つの同期列を追えず画像が壊れる。ファイル全体が最初から揃っているオフライン処理
  だからこそ、この一括投入が可能かつ最も正確
  （**既知の制約**: 1回のデコードで検出できるのは録音内の最初の1画像分のみ。1つの録音に
  複数のSSTV送信が含まれる場合、2枚目以降は無視される）
- バックグラウンド`QThread`（`_FileDecodeWorker`、src/ui/sstv_tab.py）で実行し、UIをブロックしない。
  デコーダー自体はUIスレッドで生成（信号がQtの自動キュー接続で安全にメインスレッドへ届くため）
- 結果はライブ受信と同じ受信履歴サムネイル一覧・`sstv_log` DB・自動保存PNGに統合。ただし
  ラベル・ファイル名は`self._sat_name`（現在選択中の衛星）ではなく**録音ファイル名（拡張子なし）**
  を使う——録音時に選択されていた衛星と現在の選択が異なる場合の誤帰属を防ぐため
  （`_record_completed_image(qimg, mode, sat_name_override=...)`に共通化）

#### タブ UI 設計

```
┌─ SSTV / SSDV ──────────────────────────────────────── × ┐
│ Mode: [SSTV ▼]  Input: SDR (HackRF One)  [● REC] [Stop] │
├───────────────────────────────────────┬──────────────────┤
│  受信画像（プログレッシブ表示）         │  受信履歴        │
│  1行ずつリアルタイムで描画             │  サムネイル一覧  │
│                                       │  （クリックで    │
│                                       │   拡大表示）     │
├───────────────────────────────────────┴──────────────────┤
│  [💾 Save PNG]  [🗑 Clear]  [📂 Decode Recording…]  受信: 14:23 UTC / ISS │
└──────────────────────────────────────────────────────────┘
```

- **プログレッシブ表示**: 受信中に画像が上から1行ずつ描画される（SSTV の特性）
- **受信履歴**: セッション中に受信した画像をサムネイルで保持
- **PNG 保存**: 手動ボタン。ファイル名は `SSTV_{衛星名}_{受信時刻UTC}.png`
- **自動保存オプション**: Settings で ON/OFF（デフォルト ON）
- Mode ドロップダウン: `SSTV` / `SSDV` — 切り替えで入力パイプラインとデコーダーを変更

#### pySSTV 対応モード

| SSTV モード | 使用衛星 |
|---|---|
| Robot36 | ISS（主要イベント） |
| PD120 | ISS（一部イベント）・その他 |
| Martin M1 / M2 | 地上局運用・一部衛星 |
| Scottie S1 / S2 | 地上局運用 |

#### ssdv ツールの検出・バンドル方針

Direwolf と同様の優先順位で検出:
1. ユーザーインストール版（`~/.local/share/fbsat59/ssdv/`）
2. システムインストール版（`which ssdv` / PATH）
3. バンドル版（アプリ同梱）

`Help > SSDV Installation…` ダイアログ:
- Linux: `apt install ssdv` コマンド案内
- macOS: `brew install ssdv` コマンド案内
- Windows: GitHub Releases からバイナリをダウンロード
- SSDV モード選択時のみ必要（SSTV のみなら不要）

#### データ永続化

```sql
CREATE TABLE sstv_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  DATETIME NOT NULL,
    norad_sat    INTEGER,
    mode         TEXT NOT NULL,   -- 'Robot36', 'PD120', 'SSDV' etc.
    file_path    TEXT,            -- 保存した PNG のパス
    callsign     TEXT            -- 送信局コールサイン（判明した場合）
);
```

---

### AprsEngine 一本化・Communications タブのライフサイクル修正（2026-07-10）

#### AprsEngine のプロセス全体シングルトン化（コミット `7ca2e68`）

**発覚した問題**: `AprsTab`（`AprsEngine`経由）と`TelemetryTab`のBell 202 AFSKモードが、それぞれ独立に`DirewolfManager`/`AfskDemodulator`を1セットずつ持っていた。両者はコード上一切共有されておらず、CLAUDE.md旧版の「復調器は一つだけ起動しフレームを両方に配信する（pub/subパターン）」という設計メモは実装と食い違っていた。

両タブが同時にRig+サウンドカードモードでDirewolfを起動すると、どちらも`KISSPORT 8001`固定のため2つ目のDirewolfプロセスがポートbindに失敗する可能性が高く、さらに`_AUDIO_OWNER = "APRS/Direwolf"`という文字列がモジュールレベルで両者共通だったため、`AudioDeviceManager`の排他ロックは「同一owner名なら再取得OK」の設計上、論理的に別プロセスなのに同じownerとして扱われてロック取得自体は素通りしていた。加えて`stop()`が参照カウントされておらず、片方のタブを閉じるともう片方がまだ使っていてもDirewolfごと道連れで停止していた。

**修正**: `AprsEngine`（`src/comms/aprs/engine.py`）を`get_aprs_engine(conn)`経由で取得するプロセス全体のシングルトンに変更（`AudioDeviceManager`と同型のパターン）。`start_rig()`/`start_sdr()`/`stop()`/`add_owner()`が`owner`タグ引数を取るref-counting方式になり、`self._owners: set[str]`が空になった時だけ実際にDirewolf/AfskDemodulatorを停止する。`TelemetryTab`は自前の`DirewolfManager`/`AfskDemodulator`を完全に削除し、共有エンジンの`raw_frame_received`/`error_occurred`シグナルを購読するだけに変更。`SstvTab`（SSDVモード）は`raw_frame_received`を購読するだけでowner登録していなかったため、後日別途`add_owner()`経由での参加が必要と判明（後述）。

#### タブ close() が呼ばれず、closeEvent 内の後始末が実行されない不具合（コミット `5fa268e`）

**発覚した問題**: `main_window.py`の`_on_tab_close_requested()`（×ボタン押下時）は`removeTab()` + `widget.deleteLater()`のみを呼んでおり、`MainWindow.closeEvent()`（アプリ終了時）も開いているComms タブに対して`close()`を呼ぶ処理が一切なかった。Qtの`closeEvent()`は`close()`が呼ばれた時にしか発火しない（`deleteLater()`は削除を予約するだけ）ため、**×ボタンでタブを閉じてもアプリを終了しても、各タブの`closeEvent()`（Direwolf/SatDump/gr-satellitesサブプロセスの停止、共有サウンドカードロックの解放）が一度も実行されていなかった**。Communicationsタブは`QTabWidget`の子ウィジェットでありトップレベルウィンドウではないため、Qtがアプリ終了時に自動で`closeEvent()`を発火させる対象にも含まれない。

**修正**: `_on_tab_close_requested()`は`widget.deleteLater()`の前に`widget.close()`を追加。`MainWindow.closeEvent()`は既存の後始末処理の前に、開いている全タブに対して`.close()`を呼ぶループを追加。この2箇所の修正だけで、全Communicationsタブ（APRS・Telemetry・FT4・Q65・SSTV/SSDV・CW Decoder・METEOR/HRPT）の既存`closeEvent()`実装が確実に動くようになった。

#### closeEvent 監査で発覚した派生バグの修正（コミット `e13ed40` / `cc0dcd7` / `9731d05`）

上記修正によって各タブの`closeEvent()`が実際に発火するようになったことで、`closeEvent()`の中身自体に存在した以下の問題も顕在化・修正した。

| 深刻度 | タブ | 問題 | 修正 |
|---|---|---|---|
| クラッシュリスク | METEOR/HRPT | `SatDumpProcess.stop()`が`requestInterruption()`+`terminate()`を送るだけでスレッドの実終了を待たず、`closeEvent()`直後に`close()`+`deleteLater()`が連続実行されるとQThreadが実行中のまま破棄され`QThread: Destroyed while thread is still running`でアプリ全体がアボートしうる | `closeEvent()`が`self._process.wait(3000)`で実終了を待ち、タイムアウトしたら`stop(force=True)`（SIGKILL）で強制終了してから`_reenable_sdr_tab()`/ログウィンドウ破棄に進むよう変更（`satdump.py`に`stop(force: bool)`引数追加） |
| 実害あり（回帰） | SSTV/SSDV | 上記のAprsEngine一本化で導入したref-countingに、SSDVモードが一切参加していなかった（`raw_frame_received`を購読するだけでowner登録なし）。APRSタブとSSTV/SSDVタブを両方開いた状態でAPRSタブだけを閉じると、`engine.stop("aprs")`が最後のownerとして扱われDirewolfが本当に止まり、SSTV/SSDVは開いたままエラー表示もなく受信が止まっていた | `AprsEngine.add_owner(owner)`を新設（開始はしないがpipelineを生かし続けたい受動的な参加者用）。`SstvTab`のSSDV開始/停止で`add_owner("sstv")`/`stop("sstv")`を呼ぶよう修正 |
| 安全上のリスク | FT4 / Q65 | `_TxWorker.run()`/`_transmit_audio()`は`sd.wait()`（送信音声の再生完了、Q65は最大60秒）が返ってから初めてPTT OFFする設計。closeEvent側の待機がFT4は皆無・Q65は`join(timeout=2.0)`（実質意味がない短さ）だったため、送信中にタブ/アプリを閉じるとdaemonスレッドが生き残り、CPythonのインタプリタシャットダウン時にfinally節（PTT OFF）を実行し切る前にスレッドごと打ち切られ、無線機が送信状態のまま制御を失うリスクがあった | 両方の`closeEvent()`で`sd.stop()`により送信を即座に打ち切り（`sd.wait()`がすぐ返る）、短いjoin後もスレッドが終わっていなければ`rig.set_ptt(False)`を直接呼ぶ安全策を追加 |
| 軽微（自己修復） | FT4 / Q65 / APRS | FT4・Q65は`closeEvent()`でSDRパイプラインの`audio_ready`シグナルを明示的にdisconnectしていなかった（CW Decoder/SSTVは切っている）。AprsTabもエンジンへの3本のシグナル接続を明示的に切っていなかった（AprsEngineがシングルトン化しタブより長生きするようになったため、Qtの自動切断＝受信側QObject破棄時に頼るのは望ましくない） | FT4・Q65に`_disconnect_sdr_audio()`を追加（`self._sdr_pipeline`を新規に保持するよう変更）。AprsTabの`closeEvent()`で3本のシグナルを明示的にdisconnect |

**教訓**: `closeEvent()`が実装されていても、実際に呼ばれる経路（`close()`経由か、`deleteLater()`だけで済ませていないか）を確認しないと「後始末コードは書いたが一度も実行されていない」状態になりうる。特にPySide6/Qtでは`deleteLater()`は`closeEvent()`を発火させない。

#### AudioBridge にも同じ問題が実在した — macOS実機crashで発覚・修正（2026-08-02）

APRSタブの×ボタンでアプリ全体がabort()でクラッシュする報告があった。クラッシュログの
`Triggered by Thread: 8 AudioBridge`が手がかり。`DirewolfManager.stop()`（`comms/aprs/direwolf.py`）
は`AudioBridge`（Direwolf stdin/stdout⇔音声デバイスのQThread）の`stop()`（`wait(3000)`）を、
**Direwolfプロセスの`terminate()`より先に**呼んでいた。`AudioBridge.run()`は`proc.stdout.read()`
でブロッキング読み込みしているため、プロセスがまだ生きていると読み込みがすぐ返らず、`wait(3000)`が
タイムアウトしても構わず`self._audio = None`で参照を落とす→スレッドが実際にはまだ動いている状態で
Python参照を失い、上記SatDumpProcessと同じ「QThread: Destroyed while thread is still running」で
abort()。`stop()`内で`self._proc.terminate()`を最初に呼ぶよう順序を入れ替え解決（実機確認済み）。

**教訓**: 同種の不具合は`closeEvent()`側だけでなく`stop()`実装内部の「待機」と「相手側の
ブロッキングI/Oを解除する処理」の順序にも潜みうる。相手（今回はDirewolfプロセス）を先に
終了させないと、待機自体がタイムアウトするまで無意味になる。

---

### APRS 機能設計（v0.2.0 目標）

#### ディレクトリ構成

```
src/
├── comms/
│   ├── __init__.py
│   ├── aprs/
│   │   ├── __init__.py
│   │   ├── engine.py          # APRSEngine — KISS TCP 接続・フレーム送受信
│   │   ├── parser.py          # AX.25 / APRS フレームパーサー（位置・メッセージ）
│   │   ├── afsk_demod.py      # Bell 202 AFSK 復調器（SDR 用純 Python 実装）
│   │   └── direwolf.py        # Direwolf サブプロセス管理
│   └── ft4/                   # 将来
```

#### 全体アーキテクチャ

```
[SDR Connect 時]
SDRPipeline の I/Q → afsk_demod.py（numpy/scipy）→ AX.25 パーサー → APRSEngine

[Rig Connect 時（サウンドカード設定済み）]
sounddevice IN → Direwolf stdin（ADEVICE stdin stdout）
Direwolf stdout → sounddevice OUT（TX 音声）
Direwolf KISS TCP :8001 → APRSEngine
```

#### 入力ソース自動切替ルール

| Rig Control の状態 | APRS 入力ソース | 送信可否 |
|---|---|---|
| SDR Connect のみ | SDR（Python 復調） | 不可（受信専用） |
| Rig Connect のみ（Sound Card 設定済み） | サウンドカード + Direwolf | 可（PTT あり） |
| 両方 Connect | Rig 優先（送信できる方） | 可 |
| どちらも未接続 | — | APRS タブを開かない |

入力ソースは APRS タブ内に「表示のみ」で示す（ユーザーが選択するものではない）。

#### Direwolf 検出・バンドル方針

検出の優先順位:
1. ユーザーインストール版（`Help > Direwolf...` でインストールしたもの）
2. システムインストール版（`which direwolf` / PATH）
3. バンドル版（アプリに同梱）

インストール先（ユーザーインストール版）:
```
Linux:   ~/.local/share/fbsat59/direwolf/
macOS:   ~/Library/Application Support/fbsat59/direwolf/
Windows: %APPDATA%/fbsat59/direwolf/
```

Direwolf は `ADEVICE stdin stdout` モードで起動するため、ALSA / PortAudio への依存なし。
バンドルビルドは CI で各プラットフォーム向けにソースビルドし GitHub Releases にアップロード。

`Help > Direwolf...` ダイアログ:
- 現在使用中の Direwolf パス・バージョンを表示
- 未インストール時はプラットフォーム別インストール支援
  - Linux: `apt install direwolf` コマンドをクリップボードにコピー or `pkexec` 自動実行
  - Windows: GitHub Releases から `.zip` をダウンロード
  - macOS: `brew install direwolf` をターミナルで実行
- 常時: バンドル版を最新版に更新するボタン

#### PTT 制御（Direwolf 使用時）

Direwolf の PTT は `NONE` に設定し、アプリ側が Hamlib CAT 経由で制御する。

```
送信直前: Doppler 補正済み UL 周波数を確定・CAT でリグにセット
PTT ON:  RigController.set_ptt(True)（CAT コマンド）
         Direwolf が音声送出（約 0.3〜0.5 秒）
PTT OFF: RigController.set_ptt(False)
         Doppler 補正ループを再開
```

送信中（約 0.5 秒）のドップラー変化は 5〜10 Hz 程度で無視できるため、
送信中は Doppler 補正ループを停止し、周波数変更を禁止する。

シリアル RTS/DTR による PTT は将来の後付けオプションとして保留。

#### SDR 純 Python 復調パイプライン（受信専用）

Bell 202 AFSK（1200 baud、マーク 1200 Hz / スペース 2200 Hz）を numpy + scipy で復調する。
CW 復調の既存パイプラインを流用できる。

```
SDRPipeline の I/Q（~48kHz にデシメーション）
    → バンドパスフィルタ（900〜2500 Hz、SOS 形式）
    → mark/space 電力比較（ゴートツェルフィルタ or Hilbert 変換）
    → ビットスライサー（1200 baud クロック同期）
    → HDLC フレーム同期・フラグ検出
    → AX.25 フレームデコード
    → APRS パーサー（位置・メッセージ・テレメトリー）
```

AX.25 テレメトリーを送る衛星（FUNcube 等）も同じパイプラインで受信可能。

#### APRS タブ UI 設計

**タブの開閉:**
- `Communications > APRS` クリックで開く（非常駐）
- タブ右上の × ボタンでクローズ
- クローズ時: Direwolf 停止・KISS TCP 切断・SDR 復調停止（Rig/SDR 接続は維持）
- Rig/SDR どちらも未接続の場合はクローズ状態を維持（タブを開かない）
- 常駐タブ（Dashboard 等）は × を非表示にする（`tabBar().setTabButton(index, position, None)`）

**レイアウト:**
```
┌─ APRS ──────────────────────────────────────────────────── × ┐
│ Callsign: [JF9SOM  ] SSID: [-9▼] Via: [ARISS          ]      │
│ Input: SDR (HackRF One)  ← 自動検出・表示のみ                  │
├──────────────────────────────────────────────────────────────┤
│ 受信ログ（タイムスタンプ / コールサイン / 内容）                  │
│  14:23:01  JA1XYZ > APRS,ARISS*: Hello from Tokyo            │
│  14:22:45  W1ABC  > APRS,ARISS*: [位置情報あり → 地図ピン]     │
├──────────────────────────────────────────────────────────────┤
│ To: [JA1XYZ      ]  Message: [                    ]  [Send]  │
│ （Send は Rig Connect 時のみ有効・SDR 受信専用時はグレーアウト） │
└──────────────────────────────────────────────────────────────┘
```

**設定の保存:** コールサイン・SSID・Via パスは `app_settings` に保存（再起動後も維持）。

#### Dashboard 地図への位置表示

位置情報を含む APRS パケットを受信した場合、Dashboard のズームマップに局ピンを表示する。

- ピンにコールサイン ラベルを付ける
- 衛星ドットとは異なる色・形状（例: ▲マーカー）で区別する
- タブクローズ時にピンをクリア

#### データ永続化

既存の SQLite DB に `aprs_log` テーブルを追加:

```sql
CREATE TABLE aprs_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  DATETIME NOT NULL,
    callsign     TEXT NOT NULL,
    via          TEXT,
    latitude_deg REAL,
    longitude_deg REAL,
    comment      TEXT,
    raw_frame    TEXT,
    norad_sat    INTEGER   -- パス中に受信した衛星の NORAD ID（任意）
);
```

#### Rig Settings — Sound Card タブ（第4タブ）

既存の Rig Settings ダイアログに Sound Card タブを追加する。
APRS だけでなく将来の FT4・SSTV 等でも共用する音声 I/O 設定。

| 設定項目 | 内容 |
|---|---|
| 入力デバイス | sounddevice で列挙したデバイス一覧から選択 |
| 出力デバイス | 同上 |
| サンプルレート | 48000 Hz 固定（Direwolf デフォルト） |
| テストボタン | ループバックテストで設定確認 |

Sound Card タブが未設定の場合、Rig Connect 時も Direwolf を起動しない。
（APRS タブの Input 欄に「Sound Card not configured」と表示）

#### ADIF ログ出力

送受信した全 QSO を ADIF（.adi）形式でエクスポートできる。

**保存タイミング**: 送受信のたびに SQLite `aprs_log` テーブルにリアルタイム保存。
.adi ファイルへの書き出しはエクスポートボタン押下時のみ。

**ADIF フィールド:**

| フィールド | 内容 |
|---|---|
| `CALL` | 相手コールサイン |
| `QSO_DATE` | 日付（YYYYMMDD UTC） |
| `TIME_ON` | 時刻（HHMMSS UTC） |
| `BAND` | 使用バンド（例: 2m） |
| `MODE` | APRS |
| `FREQ` | 使用周波数（MHz、Radio Control のトランスポンダーから取得） |
| `COMMENT` | メッセージ内容 |
| `SAT_NAME` | 衛星名（ISS 等） |
| `PROP_MODE` | `SAT`（衛星経由を示す ADIF 標準値） |
| `MY_GRIDSQUARE` | 自局グリッドロケーター |
| `GRIDSQUARE` | 相手局グリッドロケーター（位置情報があれば） |

**エクスポートボタン**: タブ下部に配置。保存済み QSO 件数を隣に表示。
ファイル名は `aprs_log_YYYYMMDD.adi` で保存ダイアログを表示する。

---

### AX.25 9600bps G3RUH 対応（2026-07-10 実装）

#### 背景

DirewolfのMODEM設定が`MODEM 1200`（Bell 202 AFSK）に固定されていたため、9600bps G3RUHを使う衛星デジピータの受信・送信は、ハードウェア（無線機のDATA端子を叩く外付けサウンドカード等）を揃えても不可能だった。Direwolf自体は9600bps G3RUHのソフトウェアデコード機能を元々内蔵しているため、必要だったのはFBSAT59側からDirewolfへの設定伝達と、baudレート選択UIの追加のみだった。

#### 経路1: Rig + サウンドカード経由（コミット `8aeb8ff`）

- `direwolf.py`: `DirewolfManager.start()`/`_write_config()`が`modem`引数（`"1200"`/`"9600"`）を取るようになり、`MODEM 1200`固定を廃止
- `engine.py`:
  - `resolve_ax25_modem(conn, radio_control)` — `app_settings`の`ax25_baud_mode`キー（`"auto"`/`"1200"`/`"9600"`、デフォルト`auto`）を読み、autoの場合は選択中トランスポンダーの`transmitters.baud`列（`RadioControlWidget.current_transmitter()`、今回新設のgetter）を見て9600か判定。9600以外・未選択なら1200にフォールバック
  - `restart_if_modem_changed(new_modem)` — DirewolfのMODEM設定は起動時に読み込む設定ファイルの値であり実行中に動的変更できないため、衛星切り替え等でbaudが変わった場合はDirewolf自体を裏で再起動する（owner参照カウントは変更しない）
- `aprs_tab.py`（「Station Settings」枠）・`telemetry_tab.py`（「Input Source」枠）双方に Auto/1200/9600 のプルダウンを追加。同じ`ax25_baud_mode`キーを共有するため、片方のタブで変更すればもう片方にも引き継がれる
- テスト: `tests/test_aprs_engine.py`（フェイクの`DirewolfManager`でowner維持・no-op判定等を検証、実Direwolfバイナリ不要）

#### 経路2: SDR単体経由（コミット `fea993f`）

SDR単体（Rig+サウンドカード無し）でも、Direwolfの内蔵G3RUHデコーダーをそのまま使えるようにする経路。**AX.25の復号自体をPythonで再実装するのではなく**、SDRのI/Qから「無線機のDATA端子相当の生の弁別器出力」をソフトウェアで合成し、それをDirewolfのstdinに流し込む方式を採用。

- 新規 `src/comms/aprs/g3ruh_demod.py`:
  - `G3ruhDiscriminator` — `sdr/demodulator.py`のNFM位相差判別器（`np.angle(x[n]*conj(x[n-1]))`）を流用しつつ、①デエンファシスを除去 ②IF帯域幅を広げた復調DSP。出力は48kHz float32 PCM（Direwolfが要求するレートと一致）
  - `G3ruhSdrDemod`（QThread）— `afsk_demod.py`の`AfskDemodulator`と同じく、SDRパイプラインの生I/Qに`pipeline.subscribe()`で直接subscribe（SDR Controlタブの復調モードコンボとは独立）。**これにより他の復調モード（CW Decoder等）と同じSDRを同時に使える**
- `direwolf.py`: `AudioBridge`が`sdr_pipeline`引数を取れるよう拡張。指定時は実サウンドカードの代わりに`G3ruhSdrDemod`をDirewolfのstdinへ接続する
- `engine.py`:
  - `start_sdr_direwolf(owner, pipeline)` — 常時9600bps・受信専用（SDRは送信不可のため）。MYCALLはダミー（`"N0CALL"`、送信しないため実害なし、Telemetryタブの既存Direwolf受信専用セッションと同じ慣習）
  - `sync_sdr_baud(pipeline, target_modem)` — 実行中のSDRセッションを、1200（軽量`AfskDemodulator`、`start_sdr()`）⇄9600（SDR駆動Direwolf、`start_sdr_direwolf()`）の間で切り替える。**この2つはDirewolfのMODEM設定変更ではなく全く別の復号メカニズムの切り替え**であるため、`_last_rig_params is not None`（Rig+サウンドカードDirewolfセッション、9600でも対象外）を除外した上で完全な停止→再起動を行う。owner参照カウントは維持
- `aprs_tab.py`/`telemetry_tab.py`: SDR接続時、`resolve_ax25_modem()`の結果が9600なら`start_sdr_direwolf()`、それ以外は既存の`start_sdr()`に自動分岐。トランスポンダー変更・baud設定変更のハンドラは`restart_if_modem_changed()`と`sync_sdr_baud()`を両方無条件に呼ぶ（各メソッドが自分が管理しているセッション種別だけに反応し、それ以外はno-opになるよう内部でガードしているため、呼び出し側で分岐する必要がない）
- テスト: `tests/test_g3ruh_demod.py` — 合成信号（一定周波数オフセットのI/Q）を`G3ruhDiscriminator.process()`に通し、理論値（`delta_f / DEVIATION_HZ`）に収束することを検証。単に「クラッシュしない」だけでなく判別器の数値的な正しさを確認済み

#### 未検証・今後必要な作業（実衛星パスでの確認待ち、2026-07-10 時点）

`G3ruhDiscriminator`のフィルタチューニング（IF帯域幅`_IF_HALF_BW_HZ`・偏移定数`_DEVIATION_HZ`、共に`g3ruh_demod.py`冒頭で定義）は、既存のNFM復調パラメータを流用した初回実装であり、**実際の9600bps G3RUH衛星信号での検証を一度も行っていない**。合成信号（一定周波数オフセット）によるDSPロジック自体の正しさは`tests/test_g3ruh_demod.py`で確認済みだが、以下は実機・実パスでの確認が必要:

- 実際の9600bps G3RUHデジピータ衛星のパス受信時に、SDR単体経由（`start_sdr_direwolf`）でDirewolfが実際にフレームをデコードできるか
- できない/デコード数が少ない場合、`_IF_HALF_BW_HZ`（現在`_DEVIATION_HZ + 8_000.0`）・`_DEVIATION_HZ`（現在5000Hz、NFM音声用の値を流用）の再チューニングが必要になる可能性が高い
- Rig + サウンドカード経由（外付けG3RUH-soundcard等の実ハードウェア）でのMODEM 9600自体の動作確認も、実衛星パスでは未実施

対象となりうる9600bps衛星のパスが来た際に、実際にDirewolfのログ（`Help > Direwolf...`から確認できるバージョン情報とは別に、必要であれば`direwolf.conf`の`AGWPORT`/ログ出力レベルを一時的に上げる等）を見ながら復号数を確認し、上記2パラメータを調整すること。

#### `ARATE 48000` 明示が必須と判明（ARICA-2 4800bps G3RUH 実信号検証、2026-07-12）

上記の未検証事項とは別に、4800bps G3RUH（ARICA-2）の実信号受信検証で、`direwolf.conf`に
`ARATE 48000`を明示しないとデコードできないことが判明した。`_write_config()`
（`src/comms/aprs/direwolf.py`）は元々`ADEVICE stdin stdout`のみで、実際に音声を供給する
レート（`AudioBridge`・`G3ruhSdrDemod`とも常に48kHz固定）をDirewolfに伝えていなかった。
`stdin`からの生PCMパイプにはWAVヘッダーが無いため、Direwolfはサンプルレートを自力で
推測できず、宣言なしでは誤ったレート前提でデコードしていたと考えられる。

**修正**: `_write_config()`に`ARATE 48000`を無条件で追加（1200/4800/9600すべての
MODEM設定に対して常に出力）。`tests/test_direwolf.py`に
`test_write_config_always_declares_arate_48000()`を追加して回帰を防止。

**教訓**: `MODEM`行（変調方式・ボーレート）が正しくても、`ADEVICE stdin stdout`で
生PCMパイプを使う構成では`ARATE`でサンプルレートを別途明示しないとデコードが成立しない。
9600bps側（上記「未検証」セクション）のデコード数が伸びない場合も、まずこの`ARATE`宣言が
実際に効いているかを疑うこと（今回の修正で解消済みのはずだが、実信号での9600bps確認は
引き続き未実施）。

---

### Telemetry タブ設計（v0.2.0 目標・APRS と同時実装）

#### 概要

AX.25 フレームを受信し、衛星ごとのフォーマット定義に従ってテレメトリー値を表示する。
APRS とはアプリ層が異なるが、物理層・データリンク層（Bell 202 AFSK + AX.25）は共通のため
APRS の復調パイプラインを流用する。

**メニュー位置**: `Communications > Telemetry`（APRS の次）

#### 対応範囲（v0.2.0）

| 対応 | 内容 |
|---|---|
| ✅ | 1200 baud Bell 202 AFSK 衛星（AX.25） |
| ✅ | APRS 形式ペイロード（位置・テレメトリー） |
| ✅ | JSON 定義ファイルによる独自バイナリ形式の解釈 |
| ✅ | 定義なし衛星の生 hex 表示 |
| ❌ | 9600 baud G3RUH FSK（後回し） |
| ✅ | gr-satellites 連携（2026-06-30 完了、macOS向けバンドル配布は2026-07-31追加。詳細は「gr-satellites について」セクション参照） |

#### タブ UI 設計

**開閉**: `Communications > Telemetry` クリックで開く。× で閉じる（非常駐・APRS と同じ）

**衛星・トランスポンダー選択**: Radio Control タブで選択中のものを自動参照（APRS と共通）

**レイアウト:**
```
┌─ Telemetry ────────────────────────────────────── × ┐
│ Satellite: JO-97 (43803)   Input: SDR (HackRF One)   │
├──────────────────────────────────────────────────────┤
│ 受信ログ                                              │
│  14:23:01  JO-97  battery_v: 3.82V  temp_c: 24.1°C  │
│  14:22:45  JO-97  [raw] A3 F2 00 1B 44 ...           │
├──────────────────────────────────────────────────────┤
│ [Export CSV...]                  Frames: 18 received │
└──────────────────────────────────────────────────────┘
```

**エクスポート**: CSV 形式（フィールドが衛星ごとに異なるため ADIF より CSV が適切）
ファイル名: `telemetry_{衛星名}_{YYYYMMDD}.csv`

#### フォーマット定義ファイル（JSON）

`src/data/telemetry_formats/{norad_cat_id}.json` に衛星ごとに配置。
アプリ同梱で主要 1200 baud アマチュア衛星を順次追加していく。

```json
{
  "norad": 43803,
  "name": "JO-97",
  "callsign": "JO-97",
  "modulation": "AFSK1200",
  "ax25_pid": "0xF0",
  "fields": [
    {"name": "battery_v",  "offset": 0, "length": 2,
     "type": "uint16_be", "scale": 0.001, "unit": "V"},
    {"name": "temp_c",     "offset": 2, "length": 2,
     "type": "int16_be",  "scale": 0.1,  "unit": "°C"},
    {"name": "tx_power_mw","offset": 4, "length": 2,
     "type": "uint16_be", "scale": 1.0,  "unit": "mW"}
  ]
}
```

**フィールド型一覧**: `uint8`, `int8`, `uint16_be`, `uint16_le`, `int16_be`, `int16_le`,
`uint32_be`, `float32_be`, `ascii`

定義ファイルがない衛星は AX.25 フレームのコールサイン・ペイロードを生 hex で表示する。

#### データ永続化

```sql
CREATE TABLE telemetry_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   DATETIME NOT NULL,
    norad_cat_id  INTEGER,
    callsign      TEXT NOT NULL,
    raw_hex       TEXT NOT NULL,
    parsed_json   TEXT,          -- JSON 定義でデコードした値（JSON 文字列）
    signal_db     REAL           -- 受信時の信号強度（取得できれば）
);
```

#### 入力ソース自動切替（APRS と同じルール）

| Rig Control の状態 | Telemetry 入力ソース |
|---|---|
| SDR Connect | SDR（Python 復調・受信専用） |
| Rig Connect（Sound Card 設定済み） | サウンドカード + Direwolf |
| どちらも未接続 | タブを開かない |

#### 復調パイプライン共有

APRS と Telemetry は同じ Bell 202 AFSK 復調器・AX.25 デコーダーを共用する。
両タブが同時に開かれている場合、復調器は一つだけ起動しフレームを両方に配信する
（pub/sub パターン）。

---

### FT4 機能設計（feature/communications 実装済み・2026-06-13）

#### 概要

衛星経由の FT4 QSO を WSJT-X を起動せずに行う。コーデックに **ft8_lib**（C ライブラリ、
kgoba 実装、GPL2 互換）を採用し、シンプルな QSO フローに特化した UI を提供する。

**ft8_lib CI バンドルビルドは v0.2.0 タグを打つ時に Direwolf と同時実施する**（現時点では
ローカルで手動ビルドした `libft8.so` を `~/.local/share/fbsat59/ft8lib/` に配置すれば動作）。

**重要**: FT4 は送受信が必須。**リグ（トランシーバー）が必須**。SDR 単体では使用不可。

#### バックエンド: ft8_lib

| 選択肢 | 採否 | 理由 |
|---|---|---|
| WSJT-X UDP プロトコル経由 | ❌ | ヘッドレスモードなし。X11 依存 |
| ft8_lib（C ライブラリ ctypes） | ✅ **採用** | WSJT-X 由来コーデック・自前 UI に完全統合可能 |
| Python 純実装（pyft8 等） | ❌ | 精度・速度に不安 |

**ft8_lib バンドル方針**（Hamlib・Direwolf と同じ方式）:
- 開発環境: ローカルビルドした `libft8.so` をユーザーディレクトリに配置して ctypes でロード
- 配布: CI でソースビルドし GitHub Releases にアップロード（`.github/workflows/build-ft8lib.yml` — **実装済み 2026-06-26**）
  - `ft8lib-linux-x86_64.tar.gz` / `ft8lib-windows-x86_64.zip` / `ft8lib-macos-{arch}.tar.gz`
  - `ft8lib-bundle` プレリリースタグに自動アップロード（毎月曜 11:00 UTC、または手動 `force_build=true`）
- `Help > ft8lib Installation…` ダイアログ: `src/ui/ft8lib_dialog.py` — **実装済み 2026-06-26**
  - 現在のインストール状況（パス・バージョン・ソース）を表示
  - 「Download & Install」ボタンで `ft8lib-bundle` リリースから自動取得・展開・インストール
  - 手動ビルド手順を QTextBrowser で表示（インストール済み時は非表示）
- ユーザーインストール先:
  ```
  Linux:   ~/.local/share/fbsat59/ft8lib/
  macOS:   ~/Library/Application Support/fbsat59/ft8lib/
  Windows: %APPDATA%/fbsat59/ft8lib/
  ```

#### ft8_lib ローカルビルド手順（Linux・開発環境用）

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/kgoba/ft8_lib.git
cd ft8_lib

# -fPIC を付けて全ファイルをコンパイル（shared library に必須）
make clean
make -j$(nproc) CFLAGS="-O3 -DHAVE_STPCPY -I. -fPIC"

# FFT オブジェクトファイルを含めて .so を生成
gcc -shared -fPIC -o libft8.so \
  .build/ft8/constants.o .build/ft8/crc.o .build/ft8/decode.o \
  .build/ft8/encode.o .build/ft8/ldpc.o .build/ft8/message.o .build/ft8/text.o \
  .build/common/audio.o .build/common/monitor.o .build/common/wave.o \
  .build/fft/kiss_fft.o .build/fft/kiss_fftr.o

mkdir -p ~/.local/share/fbsat59/ft8lib/
cp libft8.so ~/.local/share/fbsat59/ft8lib/
```

**重要な落とし穴（2026-06-13 確認）**:

| 問題 | 原因 | 対処 |
|---|---|---|
| `relocation R_X86_64_PC32 ... can not be used when making a shared object` | `-fPIC` なしでコンパイルされたオブジェクトファイルを `.so` に使おうとした | `make clean` してから `CFLAGS="-O3 -DHAVE_STPCPY -I. -fPIC"` で再ビルド |
| `undefined symbol: kiss_fftr` | FFT オブジェクトが `.so` に含まれていない | `gcc -shared` に `.build/fft/kiss_fft.o .build/fft/kiss_fftr.o` を追加 |
| このリポジトリに `CMakeLists.txt` は存在しない | Makefile ベースのビルドシステム | `cmake` ではなく `make` を使う |

#### ft8_lib API バージョンについて（重要）

`kgoba/ft8_lib` は **現在の main ブランチ** と **旧バージョン** で API が異なる。
`codec.py` は**現在の main ブランチの API** に対応している。

| 関数 | 旧 API（非対応） | 現 API（対応済み） |
|---|---|---|
| メッセージエンコード | `pack77(text, payload)` | `ftx_message_encode(msg, NULL, text)` |
| トーン生成（FT4） | `genft4(payload, tones)` | `ft4_encode(payload, tones)` |
| 候補検出 | `ft8_find_sync(wf, ...)` | `ftx_find_candidates(wf, ...)` |
| デコード | `ft8_decode(wf, cand, ...)` | `ftx_decode_candidate(wf, cand, ...)` |
| テキスト変換 | `unpack77(payload, text)` | `ftx_message_decode(msg, NULL, text, NULL)` |

CI ビルド時は `git clone` で最新 main を取得すれば問題ない。
旧 API のヘッダー（`pack77` 等）が見えるバージョンを使わないこと。

ウォーターフォールの `mag` フィールドは `uint8_t` 型で、
値 = `clamp((dB + 120.0) * 2.0, 0, 255)`（`WF_ELEM_T = uint8_t` の場合）。
`WATERFALL_USE_PHASE` マクロが未定義の場合は常に `uint8_t`（デフォルト）。

#### ウォーターフォール計算 — time/frequency オーバーサンプリング必須（2026-07-03 確定）

`compute_waterfall()`（`src/comms/ft4/codec.py`）は、本家 `kgoba/ft8_lib` の
`common/monitor.c` の `monitor_process()` を忠実に再現する必要がある。
当初の実装は 576 サンプル（1シンボル＝48ms）ごとに重ならない FFT を1回だけ計算し
`time_osr=1, freq_osr=1` として `ftx_find_candidates()` に渡していたが、これは
**信号強度に関係なくほぼ確実に同期検出に失敗する**（相手局の送信開始タイミングは
こちらの録音バッファ境界とは無関係にランダムな位相を持つため、48ms 単位の粗い窓では
シンボル境界がずれた分だけ2シンボル分の音が混ざり、Costas 同期相関が取れない）。

**正しい実装**（`_WF_TIME_OSR = 2`, `_WF_FREQ_OSR = 2` — 本家デモデコーダー
`demo/decode_ft8.c` の `kTime_osr`/`kFreq_osr` と同じ値）:

- `block_size = FT4_SAMPLES_PER_SYM`（576）、`subblock_size = block_size // time_osr`、
  `nfft = block_size * freq_osr`
- 解析窓は `nfft` サンプル長で、`subblock_size` ずつスライドさせながら計算する
  （`monitor_process()` の `last_frame` スライディングウィンドウと等価）
- 窓関数は `np.hanning()`（対称型）ではなく、本家と同じ周期的 Hann窓
  `window[i] = (2/nfft) * sin(π·i/nfft)^2` を使うこと。対称型窓を使うと
  time_osr/freq_osr を入れても正しく同期しない
- `mag` 配列は `(num_blocks, time_osr, freq_osr, num_bins)` の順で連続配置し、
  `ftx_waterfall_t.block_stride = time_osr * freq_osr * num_bins` を正しく設定する
  （`ft8/decode.c` の `get_cand_mag()` のインデックス計算式 `((time_offset*time_osr+time_sub)*freq_osr+freq_sub)*num_bins+freq_offset` と一致させること）

**検証方法**: 自己エンコード→デコードのラウンドトリップで、シンボル境界に整合しない
任意のオフセット（例: 0.123s, 0.4567s）に信号を配置してデコードできるかを確認する
（`tests/test_ft4_codec.py`）。`time_osr=1` の旧実装ではオフセット 0.0s（完全整合）を
含む全ケースで失敗することを確認済み。

**SNR（dB表示）**: ft8_lib には真のSNR計算 API が無い。本家デモデコーダーも
`cand->score * 0.5f`（`// TODO: compute better approximation of SNR` とコメントあり）
という粗い近似を使っているため、本実装もこれに倣い `candidates[i].score * 0.5` を使用する
（以前は `0.0` にハードコードされており、常に表示が +0 dB になるバグがあった。2026-07-03 修正）。

#### ディレクトリ構成

```
src/
├── comms/
│   ├── ft4/
│   │   ├── __init__.py
│   │   ├── codec.py        # Ft4Codec — ft8_lib ctypes ラッパー（エンコード・デコード）。RXはwsjt_decoder優先・ft8_libフォールバック
│   │   ├── wsjt_decoder.py # Ft4WsjtDecoder — libft4wsjt ctypes ラッパー（WSJT-X本家3パスRXデコード）
│   │   ├── scheduler.py    # Ft4Scheduler — 6秒周期タイミング管理
│   │   └── qso.py          # Ft4QsoState — QSO ステートマシン
├── ui/
│   ├── ft4_tab.py          # Ft4Tab — Communications > FT4 タブ
│   └── ft4wsjt_dialog.py   # Ft4WsjtDialog — Help > FT4 Enhanced Decoder Installation…
```

#### 対応構成（音声入力ソース）

| 構成 | TX | RX | 用途 |
|---|---|---|---|
| **リグ + サウンドカード**（標準・必須） | リグ AF IN → sounddevice OUT | sounddevice IN ← リグ AF OUT | 一般的な衛星 QSO |
| **リグ 1（TX）+ SDR（RX）** | Rig 1 AF IN → sounddevice OUT | SDR audio_ready → デコード | 高感度受信が必要な場合 |
| SDR のみ | ❌ 不可 | — | TX できないため無効 |

**タブを開く条件**（実装確定・2026-07-03）: `Communications > FT4` は Rig の接続状態に
関わらず常に開く（`_on_open_ft4()` にゲート条件は無い）。**受信はRigの接続状態と無関係に
動作する**ため（音声取り込みは Sound Card 設定のデバイスのみを見ており、Rig の CAT 接続は
参照しない）、タブを開いた瞬間に自動でRXスケジューラーが起動し、Sound Card さえ設定済みなら
Rig 未接続でも即座にデコードが始まる。送信（PTT）だけは Rig 1 の接続が必須で、
未接続のまま送信しようとすると `set_ptt()` の戻り値をチェックして
「PTT command failed — check Rig 1 connection」を表示する（後述）。

**Input バナーの意味に注意**: タブ上部の「Input: Rig connected」/「Input: Rig not connected」
は **Rig 1 の CAT 接続状態のみ**を示し、サウンドカードの音声取り込み状態とは無関係。
赤（Rig not connected）でも音声は正常に取れていることがあるので、実際にデコードできるか
どうかの判断材料にはならない。

**RX ソース切り替え（タブ内 UI）**:
```
RX Input:  ● Rig Soundcard  ○ SDR (HackRF One)  ← SDR 接続時のみ SDR を選択可
```

**RXの自動開始**（2026-07-03 確定）: `Ft4Tab.__init__()` の末尾で
`self._start_scheduler(tx_even=True)` を無条件に呼び、CQ ボタンや TX Enable を
一度も押さなくても受信スケジューラーが動き出すようにしている。以前は
`_start_scheduler()` が CQ ボタン・TX Enable トグル・デコード行ダブルクリックからしか
呼ばれず、タブを開いて音声を入力するだけでは何もデコードされない、という分かりにくい
挙動だった。送信は `_tx_enabled` フラグで別途ガードされているため、この自動開始で
勝手に送信されることはない。

**TX Slot（Even/Odd）手動選択**（2026-07-03 実装）: Transmit 欄に
「TX Slot: Auto / Even / Odd」コンボボックスがある。デフォルト（Auto）は従来通り
CQ ボタン押下・TX Enable オン時点の6秒スロットで送信するが、Even/Odd を明示指定すると
そのスロットで固定送信できる。デコード行への応答（相手の逆スロットで送るプロトコル上の
制約がある）はこの設定の対象外で常に自動。`_resolve_tx_even()` が判定ロジックを持つ。
設定は `ft4_settings` の `tx_slot_mode` キーで永続化。

#### タブ UI 設計

```
┌─ FT4 ──────────────────────────────────────────────────── × ┐
│ My Call: [JF9SOM]  Grid: [PM86]                              │
│ RX Input: ● Rig Soundcard  ○ SDR (SDR接続時のみ有効)         │
│ Period: ● TX  ○ RX   ⏱ 00:04 / 06   [▶ TX Enable] [■ Halt] │
├──────────────────────────────────────────────────────────────┤
│ Decoded Messages                                             │
│  UTC    dB    DT    Hz    Message                            │
│  14:23  -12  +0.2   512   CQ JA1XYZ PM95    ← クリックで応答 │
│  14:23  -18   0.0   489   JF9SOM JA1XYZ -03                 │
│  14:24   -8  +0.1   512   JF9SOM JA1XYZ R-05                │
├──────────────────────────────────────────────────────────────┤
│ TX: [JF9SOM JA1XYZ -05                ]  [Generate ▼]       │
│ [CQ]  [RST]  [R+RST]  [RR73]  [73]  [Free…]                │
├──────────────────────────────────────────────────────────────┤
│ Active QSO: JA1XYZ  Sent: -05  Rcvd: -03  [Log QSO] [Clear]│
│ Status: Waiting next TX period…                              │
└──────────────────────────────────────────────────────────────┘
```

**省略する機能**（WSJT-X との差分）:
- Band Activity 一覧（複数周波数同時デコード）→ 1 周波数のみ
- コンテストモード・レート表示
- JTAlert / HamLog 連携（ADIF エクスポートで代替）
- Waterfall スペクトラム（SDR Control タブのスペクトラムを参照）
- FT8（15 秒周期）→ 将来対応。衛星パスが短いため FT4 優先

#### QSO ステートマシン（Ft4QsoState）

```
IDLE
  │ CQ ボタン押下 or デコード結果クリック
  ▼
CALLING  → TX: "CQ JF9SOM PM86"
  │ 相手コールサインを含む応答を受信
  ▼
EXCHANGE → TX: "JA1XYZ JF9SOM -05"
  │ "R-XX" を受信（R+RST 確認）
  ▼
CONFIRM  → TX: "JA1XYZ JF9SOM RR73"
  │ "73" 受信 or [Log QSO] 手動ボタン
  ▼
LOGGED → IDLE
```

- **TX Enable ON 時**: 各ステートで次の TX メッセージを自動生成・送信（自動シーケンス）
- **Halt TX**: 即時停止。次の RX 周期を待つ
- **Free text**: 任意メッセージを 1 回だけ送信（ステートマシンをバイパス）
- **クリックで応答**: デコード行をクリックするとその局の callsign を自動設定し EXCHANGE へ遷移

#### 時間管理（Ft4Scheduler）

FT4 は **6 秒周期**（UTC の偶数秒が一方、奇数秒がもう一方の TX スロット）。

| 時刻 (秒内) | 動作 |
|---|---|
| 0.0〜0.5s | 前周期音声バッファをデコーダーへ渡す |
| 0.5〜5.5s | TX: sounddevice で FT4 音声を出力（PTT ON → 音声 → PTT OFF） |
| 5.5〜6.0s | 次周期の TX メッセージを準備 |

- タイミング基準: `time.time()` UTC 秒（NTP 同期前提、0.5s 以内の精度で十分）
- TX/RX スロット割り当て:
  - CQ 局は偶数スロット TX → 奇数スロット RX
  - 応答局は奇数スロット TX → 偶数スロット RX（デコード結果クリック時に自動決定）
- `Ft4Scheduler` は `QTimer`（1秒間隔）で駆動。精度が必要な TX 開始は `time.sleep()` で微調整

#### PTT・ドップラー制御

APRS と同じパターンを使用:
- TX 開始前: Doppler 凍結（`_ptt_active = True`）→ 送信中の周波数変更を防止
- PTT ON: `RigController.set_ptt(True)`
- 音声送出: sounddevice で FT4 エンコード済み音声を再生（約 5.2 秒）
- PTT OFF: `RigController.set_ptt(False)` → Doppler 補正ループ再開

FT4 の TX は約 5.2 秒間継続するため、Doppler 補正はその間停止。
衛星パス中央付近（最大仰角前後）での周波数変化は 5 秒で数 Hz 程度であり実用上無視できる。

**`_TxWorker` のエラー通知**（2026-07-03 確定）: `set_ptt(True)` の戻り値を必ずチェックし、
`False`（Rig 未接続・CAT失敗等）が返った場合は音声を再生せず
`error` シグナルで「PTT command failed — check Rig 1 connection」を通知して即座に終了する。
**`error` と `finished` シグナルは必ずどちらか一方だけを発行する**（両方発行すると、
`finished` ハンドラーの「TX done」表示が `error` ハンドラーの表示を直後に上書きしてしまい、
失敗が画面上まったく見えなくなるため）。この2つは以前 `finally` ブロックで
`finished` を無条件発行していたために両方発行されるバグがあった。

**TX Enable 成功時のステータス表示**: `_on_tx_enable_toggled(True)` が成功パスで
「TX enabled — waiting for next period」を表示する。以前はここでステータス欄を
一切更新しておらず、過去に表示された「TX halted」等の文言がそのまま残り続けて
TX Enable が実際に効いているのか画面から判断できないという問題があった。

#### CQ 応答時のコールサイン抽出（2026-07-03 確定）

デコード行のダブルクリック応答は、`_parse_cq_call_grid()`（`src/ui/ft4_tab.py`）で
末尾の単語が Maidenhead グリッド形式（`^[A-R]{2}[0-9]{2}$`）にマッチするかどうかを見て
コールサインとグリッドを抽出する。単純に「CQの直後の単語＝コールサイン」とすると、
"CQ WWA BI4SSB QM86" のような修飾子付き directed CQ（コンテスト/DX等のキーワードが
コールサインの前に入る）でキーワード側を誤って相手コールサインとして扱ってしまう。
グリッドにマッチする末尾の単語の**直前**の単語を常にコールサインとすることで、
修飾子の有無・グリッドの有無に関わらず正しく抽出できる。

#### 周波数設定

Radio Control で選択したトランスポンダーの周波数を使用（Doppler 補正済み）。
FT4 用トランスポンダーは `community_transmitters.json` にすでに登録済み:
| 衛星 | DL | UL | Mode |
|---|---|---|---|
| RS-44 (44909) | 435.612 MHz | 145.993 MHz | FT4 |
| JO-97 (43803) | 145.857 MHz | 435.118 MHz | FT4 |
| MO-122 (60209) | 435.812 MHz | 145.938 MHz | FT4 |

Radio Control でこれらのトランスポンダーを選択時、FT4 タブを自動オープン（SSTV と同じ連動）。

#### データ永続化

```sql
CREATE TABLE ft4_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    qso_date      TEXT NOT NULL,    -- YYYYMMDD UTC
    time_on       TEXT NOT NULL,    -- HHMMSS UTC (QSO 開始)
    time_off      TEXT,             -- HHMMSS UTC (QSO 終了)
    call          TEXT NOT NULL,    -- 相手コールサイン
    gridsquare    TEXT,             -- 相手グリッドロケーター
    rst_sent      TEXT,             -- 送信シグナルレポート（-05 等）
    rst_rcvd      TEXT,             -- 受信シグナルレポート
    freq_hz       INTEGER,          -- 使用周波数（DL Hz）
    norad_cat_id  INTEGER,          -- 使用衛星
    sat_name      TEXT              -- 衛星名
);
```

ADIF エクスポート対応（APRS と同形式）:
`PROP_MODE=SAT`, `SAT_NAME={衛星名}`, `MODE=FT4`

#### 既存コンポーネントとの統合

| 機能 | 使用する既存コンポーネント |
|---|---|
| 音声入力（RX） | `SstvTab._connect_audio_source()` と同パターン |
| 音声出力（TX） | sounddevice.play()（Direwolf 不要） |
| PTT 制御 | `RigController.set_ptt()` ← APRS と同一 |
| Doppler 凍結 | `_ptt_active` フラグ ← APRS と同一 |
| グリッドロケーター | `LocationManager.load_saved()` から自動取得 |
| Sound Card 設定 | Rig Settings > Sound Card タブ（APRS と共用） |
| SDR RX | `SDRPipeline.audio_ready` Signal（構成 B の場合） |

### 共有サウンドカードアクセス設計（src/comms/audio_device_manager.py・2026-07-01 実装済み）

#### 背景

CW Decoder・SSTV/SSDV・FT4・Q65・APRS(Direwolf) は同時に複数タブを開ける非常駐タブであり、
いずれも Rig Settings > Sound Card で設定した **同一の** `input_device_index` /
`output_device_index` を参照する。各タブが個別に `sounddevice.InputStream()` /
`sd.play()` を直接呼ぶと、ALSA `hw:` デバイスのように二重オープンを拒否するデバイスでは
2つ目のタブがエラーになる。`AudioDeviceManager`（プロセス全体で1つのシングルトン）が
この共有アクセスを仲介する。

#### 設計方針：RXは共有・TXは排他

| 方向 | 方式 | 理由 |
|---|---|---|
| RX（入力） | **pub/sub 共有**：実ストリームはデバイス1つにつき1本だけ開き、購読者全員へファンアウト配信 | CW Decoder と SSTV を同時に開いて同じ受信音声を両方で見る、という使い方が正当なユースケースのため |
| TX（出力） | **排他ロック**：1タブのみが所有権を取得できる | 2タブが同時に送信すると音声が混ざって送信されてしまい、共有する意味がないため |

RX 共有ストリームは常に `_HW_SAMPLE_RATE = 48000` Hz で実デバイスを開き、各購読者が要求した
サンプルレートへ都度リサンプリングして配信する（`_resample()`）：
- 整数比・非整数比を問わず常に `scipy.signal.resample_poly`（アンチエイリアシングFIR内蔵）を使用。
  利用不可時は `np.interp` による線形補間フォールバック（CLAUDE.md 記載のオプショナルインポート
  パターンに準拠し `type: ignore` 不要）

**2026-07-05までのバグ（修正済み）**: 整数比（48000→3200, 48000→12000等）はフィルタなしの
単純デシメーション（`chunk[::N]`）を使っていた。6kHz以上のノイズが復号帯域に直接折り返し、
FT4の復号帯域（200–3000Hz）で約6dBノイズフロアが悪化するバグで、FT4の実運用デコード数が
WSJT-X本体に対し大きく劣る原因になっていた。詳細と修正コミット（`c0f92a1`）は前述「FT4 拡張
デコーダー — libft4wsjt」セクション内「実運用でデコード数が伸びなかった問題の原因調査」を参照。

#### 起動直後の入力レベル低下・自己修復（settle-reopen, 2026-07-03 確定）

Linux/PipeWire環境で、一部のリグ用USBサウンドカード（FT-991Aで確認。FTX-1Fでは未確認）は、
共有ストリームを最初に開いた直後だけ音声レベルが異常に低く、Rig Settings > Sound Cardの
「Refresh Devices」を押す（副作用で入力デバイスコンボが再構築され `_on_input_device_changed()`
経由でメーター用ストリームが開き直る）と正常なレベルに戻る、という現象が確認された。
原因は特定できていない（USBオーディオコーデックのウォームアップ、PipeWireのルーティング
安定化待ちなどが候補）が、**「一度閉じて開き直す」だけで直る**ことは確認済み。

これを毎回手動で行わずに済むよう、`_SharedInputStream._open()` は最初に開いてから
`_REOPEN_SETTLE_DELAY_S`（1.5秒）後、バックグラウンドで一度だけ内部の `sd.InputStream` を
閉じて開き直す（PipeWireへのピン留めも再実行）。購読者（FT4・CW Decoder・SSTV・Rig Settings
のレベルメーター等、すべて同じ `AudioDeviceManager` 経由）からは pub/sub インターフェースしか
見えないため、この開き直しは完全に透過的——Rig Settings の Sound Card タブを一度も開かなくても、
FT4 タブを開いて自動RX開始するだけで恩恵を受けられる。

- 全購読者が開き直し前に離脱していた場合は何もしない（死んだストリームを復活させない）
- 開き直し後の `_open(schedule_settle_reopen=False)` は再度タイマーを仕込まない
  （無限に開閉を繰り返さないためのガード）

#### API（`AudioDeviceManager` / `get_audio_device_manager()`）

```python
mgr = get_audio_device_manager()

# RX — 共有（owner文字列は各タブ固有の識別名。再度呼ぶとコールバック/レートを更新するだけ）
mgr.acquire_input(owner: str, device: int | None, samplerate: int, callback) -> None
mgr.release_input(owner: str, device: int | None) -> None  # 最後の購読者が抜けたときのみ実ストリームを close

# TX — 排他ロック
mgr.acquire_output(owner: str, device: int | None) -> bool   # True=取得成功(または既に自分が保持) / False=他タブが使用中
mgr.release_output(owner: str, device: int | None) -> None
mgr.output_owner(device: int | None) -> str | None            # エラーメッセージ組み立て用
```

`device=None`（システムデフォルト出力を使う Q65 の TX など）も有効なキーとして扱う。

#### 各タブでの利用箇所

| タブ / モジュール | owner 文字列 | RX | TX |
|---|---|---|---|
| CW Decoder (`src/ui/cw_tab.py`) | `"CW Decoder"` | ✅ soundcard 選択時 | — （受信専用） |
| SSTV/SSDV (`src/ui/sstv_tab.py`) | `"SSTV/SSDV"` | ✅ soundcard 選択時 | — （受信専用） |
| FT4 (`src/ui/ft4_tab.py`) | `"FT4"` | ✅ RX期間ごとに購読 | ✅ `_TxWorker` が送信直前に取得・送信後に解放 |
| Q65 (`src/ui/q65_tab.py`) | `"Q65"` | — （RXはSDRのみ対応） | ✅ `_transmit_audio()` が送信直前に取得・送信後に解放 |
| APRS/Direwolf (`src/comms/aprs/direwolf.py`) | `"APRS/Direwolf"` | ✅ `AudioBridge` が実行中ずっと購読 | ✅ `DirewolfManager.start()` がブリッジ生存期間全体でロック保持（下記参照） |

**Direwolf が TX ロックをセッション全体で保持する理由**: `ADEVICE stdin stdout` モードの
Direwolf は実際に送信していない間も継続的に stdout へ PCM を書き込むため（内部タイミング
維持のため）、`AudioBridge` は個々の送信バーストごとではなく **開始 (`DirewolfManager.start()`)
から終了 (`stop()`) までロックを保持し続ける**。`out_device` が未設定（`None` 以外の実デバイス
未指定）の場合はロック取得自体を行わない。TXロックが他タブに握られている場合、
`DirewolfManager.start()` は `(False, "Sound card output is in use by ...")` を返し、
`AprsEngine.start_rig()` が既存の `error_occurred` シグナル経由でステータスバーに表示する
（新規UIは追加していない）。

#### 検証状況

- `tests/test_audio_device_manager.py`（35ケース）: フェイクの `sounddevice.InputStream`
  を使い、RXのファンアウト・購読者参照カウント・リサンプリング比率、TXの排他制御（同一owner
  の再取得可・別ownerは拒否・解放後は他ownerが取得可）、settle-reopen（一度だけ開き直る・
  全購読者離脱後は開き直らない・二重に開き直らない）をハードウェア不要で検証済み。
  settle-reopenのテストは `_REOPEN_SETTLE_DELAY_S` を `monkeypatch` で上書きしており、
  デフォルトでは自動フィクスチャで999秒に固定して既存テストへの影響を遮断している
  （実値1.5秒のままだとテスト全体の実行時間内にタイマーが発火し、無関係な後続テストの
  フェイクストリーム数を汚染してしまうため）
- **実機での複数タブ同時使用（例: CW Decoder + SSTV 同時オープン）は未確認**。GUI上に
  「共有中」であることを示す表示は無いため、この機能が正しく動作しているかは目視では
  確認できない。ユニットテストの正しさを信頼する運用とする（2026-07-01、ユーザー判断）

### CW/FT4/Q65 タブ — SDR再接続でaudio_readyが二度と届かなくなるバグと修正（2026-07-22、GitHub Issue #12 派生）

#### 背景

Issue #12でSDR切断バグ（本ファイル「SDR専用のLock機能」節の直前、`_apply_transponder_state_to_rig()`
のRig1強制切断バグ）を修正しv0.2.29をリリースした後、報告者からCW Decoderタブで
「Input: SDR選択時、Levelメーターが常に『— dB』のまま」「Soundcardに切り替えると-98dBは
表示されるがデコードされない」という新しい報告があった。

#### 原因

`CwTab._connect_sdr_audio()`（`src/ui/cw_tab.py`）は「Start」ボタンを押した瞬間に一度だけ
`sdr_ctrl._pipeline`を取得し`pipeline.audio_ready`を購読するだけで、以降その参照を見直す
仕組みが無かった。一方`MainWindow._on_rig_slot_connected()`はRig 1/2が（再）接続される
たびに**新しい`SDRPipeline`インスタンスを毎回生成**する（`SdrControlWidget`自身は
`set_pipeline()`経由で正しく追従する）。`SdrRigAdapter.disconnect()`は切断時に
`pipeline.stop()`で実際にスレッドを止めるため、**CW Decoderの「Start」を押した後に
SDRが一度でも再接続されると、それ以降`audio_ready`は永久に届かなくなる**——Levelメーター
が「— dB」のまま固まっていたのはこれが原因だった。同一パターン（`__init__`時に一度だけ
`pipeline.audio_ready`を購読し、以降見直さない）がFT4タブ・Q65タブにも存在していた。

面白いことに`CwTab`には`notify_sdr_connected()`/`notify_sdr_disconnected()`という
そのためと思われるメソッドが既に定義済みだったが、`grep`した限りMainWindow側から
一度も呼ばれておらず、完全な死んだコードだった（誰かが対策しようとして配線を忘れたと見られる）。

**SSTV・Telemetryタブは調査の結果、同じ問題を抱えていないと判明した**。両タブは
`RadioControlWidget`の`rig_connected`/`rig2_connected`/`rig_disconnected`/
`rig2_disconnected`シグナルに直接接続しており（`_on_rig_connected()`/
`_on_rig_disconnected()`）、SDR再接続のたびに`getattr(rig, "_pipeline", None)`で
**都度最新のpipelineを取得し直して**再購読している（SSTVの`_connect_audio_source()`は
`_find_sdr_pipeline()`を毎回呼ぶ設計）。Telemetryタブに至っては、SDRが切断された時点で
`_on_rig_disconnected()`が受信自体を完全に停止する（`_on_stop()`）ため、再接続後は
ユーザーが改めてStartを押す必要はあるが、「見た目は動いているのに実は無音」という
ミスリーディングな状態には陥らない。FT4・Q65にも`rig_connected`/`rig_disconnected`への
接続自体は存在するが、これはRig 1（CAT、PTT用）の接続状態表示専用で、SDRパイプラインとは
無関係だった。

#### 修正

- `CwTab.refresh_sdr_pipeline(pipeline)`（`notify_sdr_connected`/`notify_sdr_disconnected`を
  置き換え）: 既存購読を解除し、`pipeline is None`（SDR切断）なら`Listening...`のまま
  停止し「SDR disconnected」を表示、実行中かつSDR入力選択中なら新しいpipelineへ再購読する
- `Ft4Tab.refresh_sdr_pipeline(pipeline)` / `Q65Tab.refresh_sdr_pipeline(pipeline)`:
  どちらも`_disconnect_sdr_audio(); _connect_sdr_audio()`を呼ぶだけ（`_connect_sdr_audio()`が
  `sdr_ctrl._pipeline`から常に最新値を取得し直す設計のため、これだけで十分）。入力ソースが
  SDR以外でも安全に呼べる（`_on_sdr_audio_chunk`/`_on_audio_chunk`側で入力ソースを判定して
  いるため、無関係な購読が残っても実害はない）
- `MainWindow._notify_comms_tabs_sdr_pipeline(pipeline)`（新設）: `_on_rig_slot_connected()`
  （新pipeline生成直後）・`_on_rig_slot_disconnected()`（`set_pipeline(None)`直後）の両方から
  呼び出す。`self._comms_tab_keys`（開いているCommunicationsタブの辞書）を走査し、
  `refresh_sdr_pipeline`を持つタブ（CW/FT4/Q65のみ、duck typing）にだけ通知する。
  **SSTV/Telemetryは意図的に対象外**——両タブは既に自前の`rig_connected`/`rig_disconnected`
  経由で正しく再購読しているため、ここから追加で呼ぶと同じpipelineに二重購読
  （＝音声チャンクが二重処理される）してしまう

#### 教訓

「SDRを再接続するたびに新しい`SDRPipeline`インスタンスが生成される」という設計は
`SdrControlWidget`自身は正しく追従するが、**それ以外の場所で`_pipeline`を一度だけ
キャプチャして使い回す実装は全て同じ罠を踏む**。新しくSDR音声を消費するタブ/モジュールを
書く際は、単に接続時に一度取得するのではなく、`refresh_sdr_pipeline()`のような明示的な
再購読フックを最初から用意するか、SSTV/Telemetryのように`rig_connected`/`rig_disconnected`
シグナルから都度取得し直す設計のどちらかを踏襲すること。またこの手のバグは「動いている
ように見えるが実は無音」という形で発覚しにくいため、レベルメーター等の診断表示があっても
「表示が動いていない＝データが来ていない」に気づくまで時間がかかることがある。

#### フォローアップ — v0.2.30でも再現、真因は別にあった（`request_audio`/`release_audio`、2026-07-23）

上記の再接続修正（v0.2.30）を投入した後も、報告者から「CW Decoderは相変わらず動かない。ただし
スペクトラムの中心周波数マーカーは効いていた」との再報告があった。スクリーンショットでは
SDR Control側のスペクトラムに強いCW信号がマーカー位置ぴったりに見えているのに、CW Decoder
タブ側は`Level: — dB`のまま——再接続は絡んでいない単発の新規セッションでの再現だったため、
上記の再購読バグとは別の、より根本的な原因があると判明した。

**真因**: `SDRPipeline.run()`（`src/sdr/pipeline.py`）は`if self._audio_enabled:`の中で
復調（`self._demodulator.process(iq)`）・`audio_ready`シグナルの発行・スピーカー再生
（`_play_audio()`）の3つ全てをまとめて実行しており、`_audio_enabled`を`True`にする
`set_audio_enabled()`は**`SdrControlWidget`自身の「▶ Start Audio」ボタンからしか
呼ばれていなかった**。CW Decoder・FT4・Q65・SSTV（アナログSSTVパスのみ。SSDVは
`AprsEngine`経由の別系統`pipeline.subscribe()`＝生IQ購読であり、この`_audio_enabled`
ゲートと無関係なため対象外。Telemetryタブも同様にAFSK/gr-satellitesとも`subscribe()`系統
のため対象外）は、どれも`pipeline.audio_ready`を購読するだけで、**自分から復調を有効化する
呼び出しを一切行っていなかった**。つまり「CW Decoderの'Start'を押す」だけでは何も起きず、
ユーザーが**別のSDR Controlタブに切り替えて『Start Audio』も押す**という、UI上まったく
自明でない前提条件を満たさない限り、これらのタブは永遠に無音のままだった。

**修正**: `SDRPipeline`に参照カウント式の`request_audio(owner)`/`release_audio(owner)`
（`AudioDeviceManager`/`AprsEngine`と同じownerタグ方式）を新設。`run()`のゲート条件を
`if self._audio_enabled or self._demod_requesters:`に変更し、復調と`audio_ready`発行は
「SDR Control自身の再生ON、またはrequest中のタブが1つでもある」場合に実行するよう分離。
**スピーカー再生（`_play_audio()`）だけは`self._audio_enabled`のみに限定**——CW Decoder等が
`request_audio()`しただけでユーザーが望んでいないスピーカー出力まで勝手に始まることを防ぐ。
CW/FT4/Q65/SSTV（アナログ）のSDR購読開始・終了処理（`_connect_sdr_audio()`/
`_disconnect_sdr_audio()`または`_connect_audio_source()`/`_disconnect_audio_source()`）に
それぞれ`request_audio()`/`release_audio()`の対を追加した。

**教訓**: 「同じシグナル/フラグに複数の異なる目的（ユーザー向けスピーカー再生 vs.
デコーダーが必要とする復調済みデータ）を1本にまとめて載せる」設計は、片方の目的（この場合
SDR Controlの手動再生ボタン）を経由しない限りもう片方（デコーダー purposes）が機能しない、
という気づきにくい依存関係を生む。「別タブの無関係に見えるボタンを押さないと動かない」系の
不具合は、まず「このデータの発行が本当に自分の操作だけで有効化されているか、他の何かに
暗黙に相乗りしていないか」を疑うこと。今回も直前の再接続バグ修正（見た目は近い症状）を
先に見つけて満足しかけたが、実機での再検証（別セッション・再接続なしでも再現）がなければ
この2つ目の、より根本的な原因を見逃すところだった。

#### 2度目のフォローアップ — v0.2.31でも改善なし、真因は`_sdr_control`参照が最初から存在しなかったこと（2026-07-23）

`request_audio`/`release_audio`修正（v0.2.31）をリリースした後も、報告者から
「Level: --dB のまま、デコードも一切なし。変化なし」との報告があった。ここまで2回連続で
「見つけた原因を直しても症状が変わらない」という状況になったため、今度は`_connect_sdr_audio()`
の**入口**から丁寧に追い直した。

**真因**: `CwTab`/`Ft4Tab`/`Q65Tab`/`Ax100DigiTab`はいずれも
`getattr(self._radio_control, "_sdr_control", None)`でSDRパイプラインの入手元
（`SdrControlWidget`インスタンス）を探していた。しかし`RadioControlWidget`
（`src/ui/radio_control_widget.py`）は**`_sdr_control`という属性を一度も定義・設定して
いなかった**（`grep`で確認——クラス定義内に一切出現しない）。`MainWindow`は
`self._sdr_control = SdrControlWidget()`を**自分自身の**属性として保持するだけで、
`self._radio_control`（`RadioControlWidget`インスタンス）側にこれを渡す配線が
存在しなかった。そのため`getattr(...)`は常に`None`を返し、`_connect_sdr_audio()`は
`if sdr_ctrl is None: return`で毎回即座に抜けていた——**`pipeline.audio_ready.connect()`
にも今回追加した`request_audio()`にも一度も到達していなかった**。

これは今回の2回の修正（SDRPipeline参照の陳腐化対策・`_audio_enabled`分離）が
**両方とも無意味だったわけではなく**、どちらも実在する正しいバグ修正だったが、
その手前でそもそも`sdr_ctrl`自体を入手できていなかったため、一度も効果を発揮する
機会がなかった、という状況だった。CW Decoder（および実機未検証だがFT4・Q65・
AX100 Digiタブ）のSDR入力は、**実装されて以来一度も実際に動作したことがなかった**
可能性が高い（Soundcard入力側は別経路のため無関係。SSTV/Telemetry/SSDVは
`_find_sdr_pipeline()`で`self._radio_control._rig1`/`_rig2`を直接見に行く設計のため、
この`_sdr_control`属性欠落バグとは無関係で影響を受けていなかった）。

**修正**: `RadioControlWidget`に`self._sdr_control: Any = None`属性と
`set_sdr_control(sdr_control: Any) -> None`セッターを新設。`MainWindow`が
`self._sdr_control = SdrControlWidget()`を構築した直後に
`self._radio_control.set_sdr_control(self._sdr_control)`を呼んで配線する。
これにより`CwTab`/`Ft4Tab`/`Q65Tab`/`Ax100DigiTab`側の`getattr(...)`呼び出しは
一切変更せずに（元々あった参照経路がついに実体を持つようになるだけで）修正が完了する。

**なぜMeteorTabだけは無事だったか**: `MeteorTab`は`_on_open_meteor()`
（`main_window.py`）で`sdr_widget=self._sdr_control`という**明示的なコンストラクタ引数**
として直接渡されており、`getattr(radio_control, "_sdr_control", ...)`という間接参照に
依存していなかった。この設計の違いが、同じ「SDRコントロールタブの参照を他タブに渡す」
という目的に対して、なぜ一方は動き一方は完全に死んでいたかを分けた分岐点だった。

**教訓**: `getattr(obj, "attr_name", None)`によるダックタイピング的な参照取得は、
「そのobjが本当にattr_nameを持っているか」を型チェッカーが検証してくれない
（`Any`型を経由するため`mypy --strict`でも検出不能）。実際には一度も存在しなかった
属性への参照が、例外を投げず静かに`None`にフォールバックし続け、何年もの間気づかれずに
残っていた。新しくタブ間でウィジェット参照を受け渡す設計をする際は、`MeteorTab`のように
**コンストラクタの明示的な引数として渡す**か、今回のように**専用のセッターメソッドを
用意して呼び出し漏れが型として分かる形にする**方が、`getattr`によるダックタイピングより
事故を防ぎやすい。また、「原因を修正したのに症状が変わらない」が2回連続で起きたときは、
その原因が本当に実行パス上にあるのか（今回のように、もっと手前で早期returnしていて
一度もそこまで到達していない可能性）を疑い、修正箇所から遡ってエントリーポイントまで
実際に辿り直すこと。

#### SDRPipeline motorboating — 調査用の一時的診断ログ（`src/sdr/diag_log.py`・2026-07-25 追加）

上記一連の修正でCW Decoder等のSDR音声受信が実際に動くようになった後、報告者から新たに
「Telemetryタブ（SDR経由のAX.25受信）でStartを押すと、SDR Controlの音声再生がモーターボート
のようにブツブツ途切れる」「ISSやCWビーコンの音質も悪い」という報告があった。

コードを読んだ限りでは、TelemetryのSDR購読経路（`AfskDemodulator`/`G3ruhSdrDemod`、どちらも
`pipeline.subscribe()`経由）は`push_samples()`自体が`queue.put_nowait()`だけの軽い処理で、
重いDSP処理は自前の別`QThread`（`run()`）に逃がす設計に最初からなっており、
`SDRPipeline.run()`のループ自体を直接ブロックする作りにはなっていなかった。そのため
「購読者のコールバックがパイプラインスレッドをブロックしている」という当初の仮説は
コード上は否定的だったが、確定的な原因は特定できなかった（実機でのタイミング測定なしに
静的なコードリーディングだけでは判断できない領域）。

**方針**: 憶測で修正を入れる前に、まず`SDRPipeline`のループが実時間に追いついているかを
直接観測できる**一時的な診断ログ**を追加し、報告者に再現してもらってログを提出してもらう
方式にした（過去のFT4タイミング調査 `ft4_decode.log` と同じアプローチ）。

- `src/sdr/diag_log.py`: `get_sdr_diag_logger()` — `fbsat59.log`と同じディレクトリに
  `sdr_pipeline_diag.log`として出力。`logger.propagate = False`で共有ログには流れない
- `SDRPipeline.run()`: 1秒ごとに集計サマリーを1行出力
  （`iters`＝そのウィンドウでのループ回数・`partial`＝`read_samples()`が`_BLOCK_SIZE`未満の
  部分ブロックを返した回数・`avg_lag`/`max_lag`＝各イテレーションの所要時間から、処理した
  サンプル数に相当する実時間分を差し引いた値（正＝実時間に追いついていない）・
  `max_audio_write`＝`_play_audio()`の`write()`呼び出しにかかった最大時間・
  `audio_enabled`/`demod_requesters`＝そのときの状態）。毎ブロックではなく1秒集計にしたのは、
  ブロック長（`_BLOCK_SIZE=16384`）とサンプルレート次第では毎秒15〜140回のログになり
  膨大になるため
- `SDRPipeline._play_audio()`: `sounddevice.OutputStream`の`blocksize`は**最初の呼び出し時の
  PCM長に固定**されている（既存コード）。もし後続の呼び出しで異なる長さのPCMが渡されたら
  `blocksize_mismatch`として即座にログする——「部分読み取り時にPCM長が変わり、固定
  blocksizeのストリームに書き込むと不安定になるのでは」という仮説を直接検証するため
- `AfskDemodulator.push_samples()` / `G3ruhSdrDemod.push_samples()`: キュー満杯による
  サンプル取りこぼし（`contextlib.suppress(queue.Full)`で従来サイレントに握りつぶしていた）
  を、初回発生時と以降50回ごとにログするよう変更

**配布**: `scripts/collect_sdr_diag_log.bat`（Windows）— `collect_windows_sdr_log.bat`と
同じ「ダブルクリックでデスクトップにコピーするだけ」のパターン。GitHub上のraw URL経由で
リンクを渡し、報告者にダウンロード・実行してもらう。

**この診断ログは原因特定後に削除すること。** 過去の`TEMP_DOPPLER_RATE_LOG`と同様、
削除を忘れがちな一時的計測なので、次にこの節を読む際は調査が完了しているか確認し、
完了していればコード（`diag_log.py`本体・各所の呼び出し・このCLAUDE.md節）を削除すること。

### ログソフト連携 — UDP ADIF ブロードキャスト設計（src/comms/log_broadcast.py・2026-07-05 実装済み）

#### 概要

FT4・Q65・APRSでQSOがログされるたびに、ADIFレコード1件をUDPでブロードキャストするオプション機能。
wavelog-gate・JT-LinkerのようにUDPポートでプレーンなADIFテキストを待ち受ける軽量なログ中継ソフトとの
連携を想定している。**WSJT-Xが使うバイナリUDPプロトコル（JTAlert/GridTracker等が対象）ではない。**

#### 対象・対象外

| モジュール | 対象 | 除外されるもの |
|---|---|---|
| FT4 | 全QSO（LOGGED確定時） | — |
| Q65 | 全QSO（LOGGED確定・手動ログ両方） | — |
| APRS | `_is_confirmed_reply()` で双方向メッセージ交換が確定したものだけ | 位置ビーコン・ACKのみのやり取り・一方的な受信ログ |

Telemetry・SSTV/SSDV・CW Decoder・METEORは受信専用でQSOの概念がないため対象外。

#### APRSのADIF記録方針（2026-07-05 確定）

APRSには信号レポート交換という概念自体が無いため、確定したメッセージ交換は
`RST_SENT`/`RST_RCVD` とも固定値 `599`（フルクイエティング）としてログする。

またADIF 3.x仕様の `MODE` 列挙値に `APRS` は存在しない（正規の値は `PKT`）。
`MODE=APRS` のまま出力するとeQSL/LoTWがレコードを受け付けないため、APRSのQSOは
必ず `MODE=PKT` でログする（手動ADIFエクスポート・UDPブロードキャストの両方に適用）。
FT4/Q65は元々ADIF仕様の正規列挙値（`FT4`/`Q65`）なので対象外。

#### ADIFレコード生成の共通化（`src/ui/adif_utils.py`）

`build_adif_record(fields: dict[str, str]) -> str` が tag→value の辞書から
1レコード分のADIF文字列（`<EOR>` 込み）を生成する。空値のフィールドは自動的に省略される。
以下の両方から共有して使用することで、「エクスポートで見えるADIFフォーマット」と
「UDPで飛ぶADIFフォーマット」が将来ズレないようにしている:
- 手動一括エクスポート（`src/ui/log_export_dialog.py` の `LogExportDialog._collect_records()`）
- 今回のリアルタイムUDPブロードキャスト

#### フック位置

| モジュール | 関数 |
|---|---|
| FT4 | `Ft4QsoManager.log_qso()` → `_broadcast_adif()`（src/comms/ft4/qso.py） |
| Q65 | `Q65QsoManager._log_qso()` → `_broadcast_adif()`（src/comms/q65/qso.py） |
| APRS | `AprsTab.append_packet()` の `log=True` 分岐 → `_broadcast_adif()`（src/ui/aprs_tab.py） |

いずれもDBへのINSERT・commitが成功した直後にのみブロードキャストする（ログ失敗時は送信しない）。

#### `LogBroadcaster`（src/comms/log_broadcast.py）

- プロセス内シングルトン（`get_log_broadcaster()`）。`send_adif_record(adif_text: str)` は
  無効時は即return、有効時はUDPで送信（送信失敗は例外を握りつぶすのみ。fire-and-forgetの
  UDPが送信失敗でQSOログ処理自体を止めることは絶対に避ける）
- 各フック呼び出し時に毎回 `reload_settings(conn)` を呼んでから送信するため、Settings
  ダイアログ側から明示的にリロードを伝播させる仕組みは不要（次回QSO時に自動で最新設定を反映）
- 設定は `app_settings` テーブルに単一JSONキー `log_broadcast_settings` として永続化
  （`{"enabled": bool, "host": str, "port": int}`、デフォルト `127.0.0.1:2333`）。
  `core/notifier.py` の `notification_settings` と同じ「単一キーJSON blob」パターンを踏襲

#### Settings UI

`File → General Settings` に「Logging」タブを追加（`src/ui/settings_dialog.py`）。
Host はLAN内の別マシンも指定可能なフリーテキスト（デフォルト `127.0.0.1`）、Port は
1–65535のスピンボックス（デフォルト `2333`）。

#### テスト

`tests/test_log_broadcast.py` — 実UDPソケット（127.0.0.1・OS割当ポート）を使い、
有効/無効時の送受信・設定の永続化（不正なJSONからのフォールバック含む）・
シングルトン性を検証（ネットワーク・実ログソフト不要）。実機のwavelog-gate/JT-Linkerとの
疎通確認は未実施。

### AX100 Digi 機能設計（`src/comms/ax100digi/` + `src/ui/ax100_digi_tab.py`・2026-07 実装）

#### 概要

MARMOTSat の VHF デジピータ（145.875 MHz）は、GreenCube（IO-117・435.310 MHz）と**同一の
AX100 "ASM+Golay" GMSK プロトコルスタック**を使う（MARMOTSat自身のドキュメントに
"equipment requirements ... are the same as for Greencube" と明記）。GreenCube用の
既存デコーダー資産（gr-satellites）は存在するが本アプリには組み込まれていなかったため、
ゼロから自前実装した。**実際の衛星IQキャプチャによる検証はまだ行っておらず**、現状は
自己符号化→自己復号のラウンドトリップテストで各プロトコル層の結線を検証した段階
（Phase 0/1）。実運用は Rig+サウンドカード（SSBモード）・SDR 両対応。

#### プロトコルスタック（gr-satellites のソースからビット精度で移植）

```
[32bit ASM同期語] [24bit Golay(24,12)長さフィールド] [frame_len バイト]
                                                          └─ CCSDS descramble（任意）
                                                             └─ RS(255,223) shortened（任意）
                                                                └─ CSPパケット（payload）
```

| モジュール | 内容 | 移植元 |
|---|---|---|
| `golay.py` | Golay(24,12) 拡張符号 encode/decode（シンドローム法、最大3ビット誤り訂正） | gr-satellites `golay24.c` |
| `randomizer.py` | CCSDS 擬似ランダム化（バイト単位 XOR シーケンス、8bit多項式LFSR） | gr-satellites `randomizer.c` |
| `rs_ccsds.py` | RS(255,223) CCSDS dual-basis のラッパー | PyPI `reed-solomon-ccsds`（純Python+numpy、ネイティブビルド不要） |
| `csp.py` | CSP (Cubesat Space Protocol) v1 ヘッダー（32bit：priority/source/destination/dest_port/source_port/flags） | CSPプロトコル仕様 |
| `frame.py` | 上記を結線するフレームコーデック本体（`find_frames()`/`encode_frame()`） | gr-satellites `u482c_decode_impl.cc` |
| `message.py` | GreenCube Digipeater Manual のアプリ層メッセージ形式 `$Src > $Dst, $SatName, STORE=$Time $Message` | GreenCube Digipeater Manual (Sapienza/S5Lab, Issue 1.1) |
| `gmsk_demod.py` | GMSK復調（非コヒーレント位相差判別器＋固定位相総当たりでのビットスライス） | 自前設計（後述） |
| `audio_bridge.py` | 実音声⇔複素ベースバンド変換（Hilbert FIR＋位相連続周波数シフタ） | 自前設計（後述） |
| `tx.py` | メッセージ→CSP→フレーム→GMSK音声波形の送信エンコーダー | — |
| `engine.py` | `Ax100DigiReceiver`（ローリングバッファ＋`push_samples()`/`decode_pending()`） | — |

**`reed-solomon-ccsds` を意図的に `dev` extras に含めていない**（`pyproject.toml` の
`ax100digi`/`packaging` extras のみに追加）。理由: Hamlib/ft8lib/libq65/libft4wsjt が
背負ってきたネイティブライブラリのクロスプラットフォームビルドの負担を避けるため、
純Python実装のパッケージをあえて選定した経緯があるが、それでも CI の
`pip install -e ".[dev]"` には含めていない（配布バンドルには `packaging` extras 経由で
同梱される）。この結果、`tests/test_ax100digi_*.py` は全て
`pytestmark = pytest.mark.skipif(not rs_ccsds.is_available(), ...)` で**CI上は常にSKIP**
される（これは意図した設計であり不具合ではない。詳細は「CIエラー調査で判明したこと」
参照）。

#### GMSK復調方式 — 固定位相総当たり方式（PLLなし、2026-07 確定）

AX100フレームは短い（最大258バイト、1200baudで約1.7秒）ため、SDRのクロックドリフトが
無視できるという前提のもと、継続的なPLL/クロックリカバリループではなく、**8候補位相の
総当たり**（「フレームデコーダー自体がその位相の良し悪しの判定基準になる」方式）を採用。
非コヒーレント位相差判別器で復調した後、8つの候補開始位相それぞれでビットスライスを
試み、`frame.find_frames()` に通して実際にフレームが取れた位相を採用する。継続的な
クロック追従が必要になるほど長時間の信号ではないため、この単純な方式で十分という判断。

#### Rig+サウンドカード（SSBモード）音声ブリッジ

GreenCube自身の運用方式（SSBパスバンド内に固定オーディオオフセットでGMSK信号を乗せる、
`DEFAULT_SHIFT_HZ = 1600.0`）を踏襲。`audio_bridge.py` の `AnalyticSignalConverter`
（Hilbert FIR変換器）で実音声を複素解析信号に変換し、`FrequencyShifter`（位相連続
ミキサー）で1600Hzオフセットを除去/付加することで、SDRの複素I/Qパスと共通の
`GmskDiscriminator`/フレームデコーダーを両入力方式で共用できる。

#### タブUI（`src/ui/ax100_digi_tab.py`）— 段階的なUX改善の経緯

以下は全てユーザーからの明示的な要望を受けて実装した機能（実装順）:

| 機能 | 内容 |
|---|---|
| Input/Output選択 | Rig Soundcard / SDR のラジオボタン切り替え |
| PTTシーケンス | `_TxWorker`（`ft4_tab.py`と同じ素の`threading.Thread`パターン）。lead 0.20s / tail 0.50s は GreenCube `config.ini` の `KeyUpDelay`/`KeyDownDelay` デフォルト値と一致させた |
| 自局コールサイン | メッセージ送信枠での入力欄を廃止し `_get_my_call()` が `app_settings['callsign']`（File > Set QTH で設定）を毎回読み直す方式に変更（キャッシュしない＝設定後の再起動不要） |
| To/Satellite/STORE= の1行化 | 3つの入力欄を1行にまとめてコンパクト化 |
| メッセージ本文の履歴 | `_remember_content()` — 最大20件・重複排除・先頭移動、コンボから選択可。専用の消去ボタンあり |
| CSPアドレス設定 | `_CspSettingsDialog`（Priority/Source/Destination/Dest Port/Source Portを編集可能）。`DEFAULT_CSP_HEADER = CspHeader(priority=1, source=1, destination=5, dest_port=10, source_port=20)` は**実際のMARMOTSat/GreenCube地上局に対して未確認のプレースホルダー**（GreenCube Digipeater Manualはアプリ層メッセージ形式は精密に文書化しているが、CSPアドレッシング自体は非公開）——このため設定を露出してユーザーが調整できるようにしてある |
| 手動スケルチ | `_squelch_slider`（範囲0-60、デフォルト0=OFF）。`_peak_dbfs()`（実音声・複素IQ両対応、`np.abs()`ベース）で毎チャンクのピークdBFSを計算し `_passes_squelch()` で足切り。「Export CSV」ボタンの左に配置（ユーザー指定の位置） |
| UTC/Local時刻表示 | `set_use_utc()`（duck-typed、`MainWindow._notify_comms_tabs_use_utc()`から呼ばれる） |
| CSV/ADIFエクスポート | CSV: 全行ダンプ。ADIF: `dest = my_call` の行のみ（＝自局宛の確認済み交信のみ。APRSの`_is_confirmed_reply()`と同じ考え方） |
| DB永続化 | `ax100_digi_log` テーブル（`_ensure_table()`）— 受信フレームは毎回`_persist_frame()`でINSERT |

#### ノイズ/無音を誤ってデコードしてしまう不具合と修正（2026-07）

**症状**: 無線機を接続していない（無音入力の）状態でも、何かのメッセージらしきものを
デコードしてしまう、というユーザー報告。

**原因**: Golay(24,12)は2^24の符号空間のうち有効な符号語は4096個しかないが、各符号語の
半径3の誤り訂正球が空間の大部分（約58%）を覆っている。このため**ランダムな24ビットの
Golayフィールドでも約58%の確率で「訂正成功」してしまい**、たまたま復号された `rs_flag`
ビットが0（RS未使用）だった場合、続く `frame_len` 分のランダムバイト列が**一切の追加検証
なしに**「ペイロード」として受理されてしまう。RS(255,223)はランダムデータに対して
天文学的に低い確率でしか成功しないため、これをフィルタとして使うのが最も効果的。

**修正1（`frame.py`）**: `find_frames()`/`_try_decode_at()` に `require_rs: bool = True`
（デフォルトTrue）を追加。Golay復号結果の `rs_flag` が False の候補は無条件で棄却する。
GreenCube/MARMOTSatの実運用スタックは常にRSを使うため、実フレームを誤って棄却することは
ない。統計的検証（`np.random.default_rng(seed)`, 20シード・各200万ビットのノイズ）で、
`require_rs=False` では19/20シードで疑似フレームが発生するのに対し、`require_rs=True`
では全シードで0件を確認（`tests/test_ax100digi_frame.py`）。

**修正2（手動スケルチ）**: 上記のRS必須化だけでは「強すぎるフィルタにすると逆に本物の
弱信号もデコードできなくなるのでは」というユーザー懸念があったため、静的なフィルタ強化
だけに頼らず、ユーザー自身が信号強度に応じて調整できる**手動スケルチスライダー**を追加
（上表参照）。要求なしにスケルチ自体を実装するのではなく、まずRS必須化を提案・実装した後、
「スケルチを強力にしすぎると信号がデコードできなくなるのでは」というユーザーの的確な
懸念に応える形でスライダー方式に落ち着いた。

#### Quick Comms Panel が常に29MHzのトランスポンダーを選んでしまうバグと根本原因（2026-07）

**症状**: `is_ax100_digi_transmitter()`（NORAD 98272 かつ description に "MODE V" または
"DIGIPEATER" を含む）のマッチャーを実装し `COMMS_TAB_CONFIG["ax100digi"]` に登録しても、
Quick Comms Panel の Input Source コンボは常に145.875MHzのデジピータではなく29MHz帯の
HF系トランスミッタ（MARMOTSatは同一NORAD IDに HF CW ビーコン・HF DVB-S2・HF LFM
Sounderなど複数のトランスミッタを持つ）を選んでしまい、マッチャーの文字列条件を
何度調整しても解消しなかった。

**根本原因**: `MainWindow._refresh_radio_control()` のSQL SELECT文が**そもそも
`norad_cat_id` 列を一切SELECTしていなかった**。このため各トランスポンダーdictには
`norad_cat_id` キー自体が存在せず、`xpdr.get("norad_cat_id")` は常に `None` を返し、
マッチャーは**すべての候補を無条件で拒否**していた。呼び出し元の
`next(..., 0)` はマッチが一つも無い場合デフォルトのインデックス0（＝周波数最小のエントリ
＝29.410MHz）に静かにフォールバックしていたため、「マッチャーが機能していない」ことが
画面上は「間違ったトランスポンダーが選ばれる」としか見えず、原因特定に複数ラウンドの
デバッグを要した。修正はSELECT句に `norad_cat_id`（後に `source` も）を追加するだけ
だったが、実際の挙動再現には `_refresh_radio_control()` を通した完全なDB経由の
エンドツーエンドテストが必要だった。

#### SATNOGSデータ不備への対応 — コミュニティトランスポンダーエントリ（2026-07）

MARMOTSatは打ち上げ直後で、SATNOGS側のこのVHFトランスミッタのデータが不完全（
`mode=AFSK`・アップリンクなしのまま登録されている）。`src/data/community_transmitters.json`
に `community-marmotsat-digi` エントリ（NORAD 98272、up=down=145.875MHz、`mode="USB-D"`
[LSB-DではなくUSBをユーザーが選択]、`type="Transceiver"`）を追加し、正しい周波数・モードを
提供する。SATNOGSの不正確なエントリと本エントリの両方が `is_ax100_digi_transmitter()`
にマッチしてしまうため、`mode_detection.pick_preferred_transponder_index()` を新設し、
**`source='community'` のマッチを常に優先**するようにした（単純な最初のマッチだと、
DBクエリのソート順次第でどちらが選ばれるか非決定的になってしまうため）。

#### CIエラー調査で判明したこと（2026-07-24）

上記のコミュニティトランスポンダーエントリ追加（community衛星が3件→4件に増加）が原因で、
`tests/test_main_window.py` にハードコードされていた衛星数のアサーション3箇所
（`test_satellite_list_populates_from_db`・`test_empty_db_gives_empty_satellite_list`・
`test_all_norads_populated`、いずれも「2 from populated_db + 3 community satellites」等の
コメント付きで固定値を期待していた）が実数と1件ずれて全滅した。`gh run view <run-id>
--log-failed` でCIログの `FAILED` 行を辿って特定（`assert 7 == 6` 等の形で表示される）。
`community_transmitters.json` に新しい衛星を追加する際は、この種のハードコードされた
件数アサーションが他にないか確認すること（`grep -n "community satellite" tests/`）。

#### mypy とオプショナルインポートの落とし穴（`rs_ccsds.py`、通常パターンとの違い）

本ファイル既出の「mypy とオプショナルインポートの注意点」で示した
`try: from X import Y / except ImportError: Y = None` パターンは、CIの
`ignore_missing_imports=true` によりパッケージが**インストールされていない**環境でのみ
機能する。`reed-solomon-ccsds` はローカル開発環境に**実際にインストールした**ため、
このパターンのままだと mypy が実在する型を推論し `None` への再代入と衝突して
`Incompatible types in assignment` エラーになった。`rs_ccsds.py` では
`import reed_solomon_ccsds as _rs`（モジュール全体を1つの名前でインポート）とし、
`except ImportError: _rs = None` → 使用箇所で `if _rs is None: raise ...` という
narrowing を行う方式に変更して解決。**この関連コードを修正する際は、必ずローカル環境で
`pip uninstall reed-solomon-ccsds` / `pip install reed-solomon-ccsds` を切り替えながら
両方の状態でmypyを実行して確認すること**（CIは常に未インストール状態のため、ローカルで
インストール済み状態のみ確認すると見落とす）。

#### 未検証・今後の課題

- **実際の衛星IQキャプチャによる検証を一度も行っていない**（自己符号化→自己復号の
  ラウンドトリップテストのみ）。GMSK復調パラメータ（判別器の帯域幅等）・TXのCSP
  ヘッダーデフォルト値（`DEFAULT_CSP_HEADER`）は実運用で調整が必要になる可能性が高い
- 手動スケルチスライダーの適正なデフォルト値・実際の弱信号でのRS要求フィルタとの
  兼ね合いは実機・実パスでの検証待ち

## SatDump 検出・起動・Linux librtlsdr不整合の一連の修正（2026-08-02、v0.2.44〜v0.2.5x）

METEOR/HRPTタブがSatDumpを一度も正しく起動できていなかった（macOS・Linuxとも）ことが、
ユーザーが実際にmacOS版でSatDumpをインストールして試したことをきっかけに一連の調査で
判明した。4つの独立した不具合が積み重なっており、1つ直しては次の不具合が現れる形で
段階的に発覚した。METEOR/HRPT機能は2026-06-29の実装完了以降、実機での起動確認が
一度もされていなかったと考えられる（CLAUDE.md本体にも「実機で受信確認済み」の記述が
一切なかった）。

### 1. macOS `.app`バンドル未検出（`find_satdump()`）

**症状**: `.dmg`をダウンロードして`SatDump.app`を`/Applications`にドラッグする、
という最も標準的な方法でインストールしても、Help > SatDump Installationで
「SatDumpが見つかりません」と表示され続けた。

**原因**: [`find_satdump()`](src/comms/meteor/satdump.py)は「FBSAT59専用のユーザー
ディレクトリ」と「システムPATH」の2箇所しか見ておらず、`.app`バンドルという第3の
標準的インストール形態を一切考慮していなかった。Help画面自体は`.dmg`を選択肢として
案内していたにもかかわらず、検出コードにそれに対応する分岐が存在しないという、
案内文と実装の食い違いだった。

**修正**: `sys.platform == "darwin"`のとき、`/Applications/SatDump.app/Contents/MacOS/satdump`
（および`~/Applications`）も検索対象に追加。

### 2. METEOR/HRPTタブのログウィンドウが実行中の内容を保持しない

**症状**: SatDumpが起動〜約30秒後にエラー終了したが、「ログ」ボタンを押しても
ウィンドウが空だった。

**原因**: `_LogWindow`（[meteor_tab.py](src/ui/meteor_tab.py)）は「ログ」ボタンを
**最初に**押した時点で初めて生成される設計で、それより前に`SatDumpProcess.log_line`
シグナル経由で流れてきた行は一切バッファされず、ウィンドウが存在しない間は
`_on_log_line()`が黙って読み捨てていた。実行→エラー→その後にログボタンを押す、
という通常の操作順序では、SatDump自身が出した本当のエラーメッセージが永遠に
失われる設計だった。

**修正**: `MeteorTab`に`self._log_buffer: deque[str]`（上限2000件）を追加し、
`_on_log_line()`・`_on_finished_err()`は常時バッファへ追記。「ログ」ボタンを押して
ウィンドウを新規生成する際、バッファの内容を`_LogWindow.append()`で再生してから表示する。

この修正により初めて、次項3・4の本当の原因（生のSatDump出力）が見えるようになった。

### 3. バージョン表示のANSI文字化け

**症状**: 「バージョン: ▤[31m▤[1m(E) Usage : ...」という文字化けが表示された。

**原因**: [`_get_satdump_version()`](src/ui/satdump_dialog.py)は`satdump --version`
の出力をそのまま最初の非空行として表示していたが、`--version`フラグを認識しない
SatDumpビルドでは、ANSIカラーコード付きの使い方（Usage）メッセージが代わりに
出力される。ANSIエスケープの除去も、Usageメッセージかどうかの判定も行っていなかった。

**修正**: 正規表現でANSIエスケープを除去し、出力に`usage`という語が含まれる場合は
「バージョン不明」と表示するよう変更。

### 4. `satdump live`のCLI引数順序の誤り（`exited with code 1`の真因）

**症状**: 上記2・3の修正でようやく見えた実際のログに
`Error parsing arguments! [json.exception.type_error.302] type must be string, but is null`
というエラーがあり、`exited with code 1`で失敗していた。

**原因調査**: SatDump公式リポジトリのソース（GitHub CLI `gh api`で実際のタグ
`1.2.2`——ユーザーの実機バージョンと完全一致——のソースを直接取得して確認）から、
`live`サブコマンドの引数仕様が判明した:
```
satdump live <pipeline_id> <output_directory> [--flags...]
```
出力ディレクトリは**位置引数**（`argv[3]`、パイプラインIDの直後）であり、
`--output`という名前のフラグは**そもそも存在しない**。しかし
[`SatDumpProcess.run()`](src/comms/meteor/satdump.py)は
`["live", pipeline, "--source", source, ..., "--output", str(output_dir), ...]`
という順序でコマンドを組み立てていたため、`argv[3]`（本来は出力ディレクトリで
あるべき場所）に文字列`"--source"`がそのまま入ってしまい、以降の全フラグ/値の
対応がひとつずつズレる。結果として`source`パラメータが一切設定されず、SatDump内部で
`parameters["source"]`（nlohmann::jsonの`operator[]`は存在しないキーをnull値で
自動生成する）に対し`.get<std::string>()`を呼んで例外——`exited with code 1`——と
なっていた。加えて`--finish_after_loss_of_lock`というフラグもSatDumpソース全体を
検索した限り現行版には存在しないことを確認した（クラッシュの原因ではないが無効な
フラグ）。

**この不具合はOS非依存**（`SatDumpProcess.run()`は純粋なPythonコードで、macOS/
Windows/Linuxすべてで同一のコマンドライン文字列を組み立てる）。つまりMETEOR/HRPT
機能はどのプラットフォームでも実装完了以来一度も正しく起動できていなかったと
考えられる。

**修正**: 出力ディレクトリを`--output`フラグではなく正しい位置引数として渡すよう
コマンド構築順序を修正。存在しない`--finish_after_loss_of_lock`フラグは削除。
副作用として、ロック消失時の自動停止機能は失われた（Autotrack経由のLOS自動停止
とは別ロジックのため無関係。手動起動時はパス終了後に手動で「■ 停止」を押す必要が
ある。今回はスコープ外として現状維持）。

### 5. Linux固有: `librtlsdr`のSONAME不整合と、nightly AppImageバンドル化の試みと撤回

**症状**（項目4の修正後、Linux開発環境で再現）: 引数解析自体は成功し、プラグイン・
TLE読み込みまで進むようになったが、最終的に
```
Error loading .../librtlsdr_sdr_support.so! Error : librtlsdr.so.0: cannot open shared object file
...
Could not find a handler for source type: rtlsdr!
```
で失敗した。

**原因**: `ldconfig -p | grep rtlsdr`・`dpkg -l`で調査したところ、Ubuntu系の
`librtlsdr2`パッケージが提供するSONAMEは`librtlsdr.so.2`だが、SatDumpの`.deb`は
`librtlsdr.so.0`を要求してリンクされていた、というディストリビューション側の
パッケージ間バージョン不整合と判明した。FBSAT59自身のSDR機能（SoapySDR経由）は
現行の`librtlsdr.so.2`向けに正しくビルドされているため無関係に動作しており、
「FBSAT59のSDR設定画面では認識できるのに、SatDump経由だけ失敗する」という
一見矛盾した症状の理由でもあった。macOS（`.app`バンドルが`librtlsdr`を内部に
同梱）・Windows（公式Portable版も同様に自己完結型と推定）はこの問題の対象外で、
**Linuxの`.deb`配布形態に固有の問題**。

**最初に試みて撤回した対策 — SatDump公式nightly AppImageの自動ダウンロード機能**:
SatDump公式のGitHub Releasesに、依存ライブラリを内部に同梱した自己完結型の
`SatDump.AppImage`が`nightly`タグ（安定版とは別のローリング開発版リリース）に
存在することを発見し、Help > SatDump Installationに「Download & Install」
ボタンを追加してこれを自動取得・配置する機能を実装した（自前のCIビルドや
ファイルホスティングは一切不要という触れ込みだった）。しかし実機検証で
以下の想定外の問題が次々に発覚し、**この機能は撤回した**（コミット`e0bae82`・
`54b9a22`を`git revert`）:
1. AppImage内部の`.desktop`ファイルに`Exec=satdump-ui`と指定されており、
   AppImageをそのまま実行すると常に**GUI版**（`satdump-ui`）が起動する設計
   だった。GUI版はGLFW/OpenGLウィンドウの初期化を試み、ディスプレイ・GPU
   環境が制約された開発環境では`Could not init GLFW Window! Exiting`で
   即座に失敗した
2. AppImageを`--appimage-extract`で展開し、内部の真のCLIバイナリ
   （`usr/bin/satdump`、GUI版`satdump-ui`とは別ファイル）を直接実行する
   代替策も試したが、`LD_LIBRARY_PATH`（共有ライブラリ探索）・作業ディレクトリ
   （設定ファイル`satdump_cfg.json`をCWD相対で探索する挙動）など、AppImageの
   起動ラッパー（`AppRun`）が内部的に設定している複数の環境変数・前提条件を
   自前で再現する必要があり、さらに`XDG_DATA_DIRS`だけでは解決しない・
   プラグインディレクトリの探索が別途失敗する（`No valid plugin directory
   found!`）等、ドキュメント化されていない挙動に何重にも依存していることが
   判明した
3. `nightly`タグの実体は`SatDump v2.0.0-alpha`（安定版v1.2.2とは世代が異なる
   開発版）で、多くのデコーダーモジュールが読み込まれない状態だった
   （`ax25_decoder`・`dvbs2_demod`等、"Module X is not loaded. Skipping
   pipeline!"警告多数）

**教訓**: 「公式が配布している自己完結型ビルド」であっても、実際に動かして
検証する前に「バンドル済みだから安全」と判断してはいけない。特に元々GUIアプリ
として設計されたソフトウェアのCLI実行パスは、パッケージング（今回はAppImage化）
の過程で本来のCLI専用パスとは別の、GUI優先のエントリーポイントに再配線されて
いることがある。

**最終的に採用した対策 — 検出してコマンドを提示するだけに留める**: 自前でのビルド・
バンドル配布は行わず、[`_find_rtlsdr_symlink_fix()`](src/ui/satdump_dialog.py)が
既知のライブラリディレクトリ（`/usr/lib/x86_64-linux-gnu`等）を走査し、
`librtlsdr.so.0`が存在せず`librtlsdr.so.N`（N≠0）が存在する場合にのみ、
Help > SatDump… ダイアログに「Fix librtlsdr Version Mismatch」枠を表示する。
既存の`CommandRow`（Copy / Run in Terminal、[copyable_text.py](src/ui/copyable_text.py)）
パターンをそのまま流用し、
```bash
sudo ln -s <検出したlibrtlsdr.so.N> <librtlsdr.so.0の期待パス> && sudo ldconfig
```
を提示する。「Run in Terminal」は実際のターミナルウィンドウを開いてコマンドを
実行するため、`sudo`のパスワード入力にもユーザーが対話的に応答できる
（[terminal_launcher.py](src/core/terminal_launcher.py)、サイレントにpkexec等を
自前で叩く実装は採用していない）。

**検証済み（2026-08-02）**: 上記1〜4の修正とこのシンボリックリンク適用により、
macOS（`.app`バンドル、実機確認済み）・Linux（開発環境、apt版`.deb`+シンボリック
リンク、実機確認済み）の両方で、METEOR/HRPT受信が実際に「Lock」状態まで到達する
ことを確認した。Windows版は未検証（公式Portable版は自己完結型と推定されるため
実害は無いと考えられるが、実機確認は次回のユーザー報告待ち）。

**総括的な教訓**:
- 「見つかりました（found）」という検出結果は「起動できる」ことを一切保証しない。
  検出ロジックとプロセス起動ロジックは完全に別物であり、両方を実際に動かして
  初めて機能全体の動作確認になる
- ログが失われる設計上の穴（今回の項目2）があると、その先の本当の不具合
  （項目3・4）が何重にも隠れたまま気づかれない。診断用のログ・出力を
  確実に保持する仕組みは、機能そのものと同じくらい優先度高く直すべき
- サードパーティCLIツールの引数仕様は、README等の二次情報を推測で信じず、
  実際に使っているバージョンの公式ソースコード（可能なら`gh api`等で当該タグを
  直接取得）を確認すること。特に「よくあるCLIパターン」（`--output`のような
  フラグ）を無条件に仮定しない
