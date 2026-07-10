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
1. Qt6メインウィンドウを起動
2. バックグラウンドスレッドでFastAPI/uvicornをポート8080で起動
3. DataSyncManagerがTLE・SATNOGSデータを自動取得（初回 or 期限切れ時）
4. ステータスバーにLAN内アクセスURL + QRコードボタンを表示

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
    updated_at      DATETIME
);
```

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
引き続き `Satellite → Sync SATNOGS` で手動更新できる。

**追加の経緯（2026-07-05）**: 初回起動時のみの自動取得だと、後から新たにトランスミッタが登録された衛星
（例: ISSから放出直後のCubeSat「Coconut」(98292)）がいつまでも空のトランスポンダーリストのままになり、
ユーザーが気づきにくいという問題があった。手動同期に頼らず定期的に自己修復するよう7日間隔の自動ジョブを追加した。

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
- `GET /transmitters/?satellite__norad_cat_id={norad}&status=active`
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
- **Hamlib 4.7.1 以上が必須**（FTX-1F モデル 1051 および SkyWatcher ローテーターは 4.7 以降でのみ動作）
- 配布バンドル（AppImage / .exe / .dmg）には必ず 4.7.1 を同梱すること

#### バンドル版 Hamlib のビルド

| プラットフォーム | ビルド方法 | PyInstaller 収集元 |
|---|---|---|
| Linux | ソースから `/opt/hamlib/4.7` にビルド | `/opt/hamlib/4.7/lib/*.so` |
| Windows | 公式 `hamlib-w32-4.7.1.zip` を展開 | `hamlib-win64\bin\*.dll` + Python bindings |
| macOS | Homebrew `brew install hamlib` | `$(brew --prefix hamlib)/lib/` |

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
| Linux | `hamlib-linux-x86_64-py311-4.7.1.tar.gz` | `$ORIGIN` rpath付きポータブルビルド |
| Windows | `hamlib-windows-x86_64-py311-4.7.1.zip` | フラットレイアウト（DLL + .pyd + Hamlib.py） |
| macOS | `hamlib-macos-arm64-py311-4.7.1.tar.gz` | `@loader_path` rpath + dylibbundler で依存解決済み |

`py311` の部分は Python バージョンに応じて変化（`hamlib_info.py` の `_PYVER_TAG` で決定）。

**関連ソースファイル:**
- `src/core/hamlib_info.py` — バージョン検出・ユーザーディレクトリ・アセット命名
- `src/ui/hamlib_update_dialog.py` — ダウンロード・展開・インストール UI
- `src/main.py` — ユーザーインストール版の優先ロード・Windows DLL パス登録
- `.github/workflows/ci.yml` — 各プラットフォームのポータブルパッケージビルドと Release アップロード

#### Linux 開発環境固有: sys.path surgery

開発機（`/opt/hamlib/4.7` が存在する場合のみ）は `/usr/lib/python3/dist-packages` を `sys.path` から除去して 4.7.1 を優先ロードする。

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

### Hamlib 4.7.1 ソースビルド共通

**問題**: `hamlib_wrap.c: No such file or directory`  
**原因**: Hamlib 4.7.1 ソースtarballには SWIG が生成する `hamlib_wrap.c` が含まれない（`.swg` ファイルのみ）  
**解決**: ビルド前に `swig -python -Iinclude -Ihamlib-4.7.1/include -o bindings/hamlib_wrap.c bindings/hamlib.i` を実行

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
**解決**: Python binding のコンパイルも MinGW GCC に統一。`hamlib-w32-4.7.1.zip`（32bit）ではなく `hamlib-w64-4.7.1.zip`（64bit）を使用

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
- **`is_source_stale(source_name)` (TLEManager)**: `sync_log` を照会し、一度もフェッチされていないソース（`never-fetched`）を検出。初回起動時に cubesat/weather/science/earth-obs グループを即時フェッチするトリガーとして使用
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
    | METEOR-M N2-3 | LRPT | 137.9 MHz | 1.2 Msps | 57166 |
    | METEOR-M N2-4 | LRPT | 137.1 MHz | 1.2 Msps | 59051 |
    | METEOR-M N2-3 | HRPT | 1700.0 MHz | 3 Msps | 57166 |
    | METEOR-M N2-4 | HRPT | 1700.0 MHz | 3 Msps | 59051 |
    | NOAA 18 | HRPT | 1707.0 MHz | 3 Msps | 28654 |
    | NOAA 19 | HRPT | 1698.0 MHz | 3 Msps | 33591 |
    | Metop-B | HRPT | 1701.3 MHz | 3 Msps | 38771 |
    | Metop-C | HRPT | 1701.3 MHz | 3 Msps | 43689 |
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
- **SDR Control タブ**（常時表示・SDR未接続時はパネルをグレーアウト）
  - スペクトラムアナライザ（QtCharts、10fps）＋ **RX 周波数リアルタイム表示**（`center_freq_changed` Signal）
  - **Passband Tune パネル**: ◀◀/◀/▶/▶▶ ボタン + ステップ選択（100Hz〜10kHz）+ オフセット表示 + Reset
    - SDR が Rig 1/Rig 2 どちらでも動作
    - Lock ON 時: 相手リグの TX を自動追従（反転トランスポンダーは符号反転）
    - トランスポンダー切り替え時にオフセット自動リセット
  - デモジュレーター（モード選択・ボリューム・AGC・Start/Stop Audio）
    - **MP3音声録音**（`● REC Audio` / `■ STOP` / `📁`）— `lameenc` によるピュアPythonエンコード、外部ツール不要
  - IQ レコーダー（帯域幅選択・REC/STOP・経過時間表示）
    - **📁ファイルマネージャーボタン**（IQ・Audio 両方）— SDR未接続時も常時クリック可能。巨大IQファイルの削除に使用
  - トランスポンダー選択に連動したモード自動切替（Connect 前でも反映）
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
  - IC-9100（Hamlib 4.7.1 モデル3068、Direct モード）: クロスバンド・同バンド両方の周波数・モード・CTCSS 動作確認済み（v0.1.27・2026-06-25）— SAT モード ON/OFF・ドップラー補正・VFO 逆転バグ修正済み
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

| 衛星 | Rx (DL) | Tx (UL) | Mode（DB `mode`列） | 出典 |
|------|---------|---------|------|------|
| RS-44 (NORAD 44909) | 435.612 MHz | 145.993 MHz | USB-D（invert=true→UL側はLSB-D） | JH1NHK |
| JO-97 (NORAD 43803) | 145.857 MHz | 435.118 MHz | USB-D | JH1NHK |
| MO-122 (NORAD 60209) | 435.812 MHz | 145.938 MHz | USB-D | JH1NHK |

`description` は引き続き「FT4 Calling — community standard」のまま（FT4タブの自動オープン判定は
description文字列を見るため無関係）。`mode` 列だけ実際のリグCATモードを表す `USB-D`/`LSB-D` に
変更している。詳細は後述の「モード文字列 → リグCATモード変換テーブル」参照。

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

## 既知のバグ（未修正）

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

### ICOM SATMODE機（IC-9100/9700等）NETモードCTCSS — `L CTCSS_TONE` が壊れている疑い（未検証・保留中、2026-07-07）

**背景**: IC-705（汎用非satmodeリグ）のNETモードCTCSS調査で、`HamlibNetController.set_ctcss_tone()`が使っていた`L CTCSS_TONE {value}`コマンドは、rigctldのLEVEL設定構文であり、CTCSS_TONEはLEVELとして登録されていないため`RPRT -11`（ENAVAIL）で拒否されることが実機で確定した（`C {deci-Hz}`が正しい専用コマンド。詳細は「IC-705 (Hamlib model 3085)」セクション参照）。

**懸念**: satmode NETモード（IC-9100/IC-9700等）のCTCSS自動適用実装 `_apply_ctcss_civ_direct()`（`src/rig/controller.py`）も、**全く同じ`L CTCSS_TONE`を使っている**：

```python
_cmd_drain("V Sub")
_cmd_drain(f"L CTCSS_TONE {tone_deci}")   # ← IC-705と同じ理由でRPRT -11の可能性
_cmd_drain(f"U TONE {'1' if enable else '0'}")  # ← これは正しいコマンドなので独立して成功する
_cmd_drain("V Main")
_cmd_drain("U TONE 0")
```

Hamlibソース（`src/misc.c`の`rig_parse_level()`・`rig_ext_lookup()`、`rigs/icom/ic9100.c`・`ic7300.c`のIC-9700定義）を確認したが、"CTCSS_TONE"という文字列はどのIcomモデルのLEVEL/extlevel/extfunc/extparmテーブルにも登録されていない。IC-9100/9700でも理論上は同じ`RPRT -11`が返るはずである。

**なぜ「動いているように見える」可能性があるか**: `_cmd_drain()`は応答を検証せず握りつぶすため、`L CTCSS_TONE`が失敗しても後続の`U TONE 1`（エンコード有効化、これは正しいコマンドなので独立して成功する）は実行され、**TONEランプ自体は点灯する**。しかし周波数自体はリグに残っていた「以前の値」のままの可能性がある。ユーザーが実機で自動適用を「散々確認した」のは事実だが、テストした衛星の多くが67.0Hz系（ISS・AO-91等、AMSAT FM衛星で最も一般的な値）だった場合、たまたま以前の設定値と一致し続けて気づかなかった可能性を否定できない（本セッションでIC-705調査中に繰り返し遭遇した「残留状態のおかげでたまたま正しく見える」パターンと同型）。

**保留の判断（2026-07-07、ユーザー判断）**: 実機（IC-9100/9700）での検証環境が整うまで、この修正は保留とする。**実装は行っていない**。

**提案されている修正内容（未実装）**:
```python
_cmd_drain("V Sub")
if enable:
    _cmd_drain(f"C {tone_deci}")   # L CTCSS_TONE → C に変更
_cmd_drain(f"U TONE {'1' if enable else '0'}")
_cmd_drain("V Main")
_cmd_drain("U TONE 0")
```
`tone_hz <= 0`（無効化）時は`C 0`を送らない（IC-705で`RPRT -9`拒否を確認済みのため、`C`コマンド自体をスキップし`U TONE 0`のみ送る）。

**検証方法（次回対応時）**: 67.0Hz以外の値を要求する衛星（例: IO-86の88.5Hz、SO-50 Activation の74.4Hz）でトランスポンダーを自動選択し、実際にそのリグ設定（メニュー等でCTCSS周波数の現在値を確認できる機種であれば）が正しい値に切り替わるかを確認する。単に「Tランプが点くか」だけでは`U TONE`の成否しか分からず、周波数自体の正しさは検証できない。

**関連ファイル**:
- `src/rig/controller.py` — `HamlibNetController._apply_ctcss_civ_direct()`（約2557行目）
- 同ファイル内 `set_ctcss_tone()`（汎用リグ用、今回修正済み — 同じ`L`→`C`の教訓が反映されている）

---

## 次回の作業候補（v0.1.0 以降）

### 継続中・優先度高
0. ~~**RTL-SDR WinUSB Connect 失敗修正**~~ **→ v0.1.71 で解決済み（2026-06-25）**
1. **ドップラー補正の実動作確認** — 各種リグ（TS-2000・FT-817ND 等）での実衛星通信テスト（FTX-1F・FT-991AM・IC-9100・IC-9700・RTL-SDR/HackRF は確認済み）
2. **ローテーター設定ダイアログの改善** — 接続テストボタン・AZ/ELリミット設定
3. **デバッグ用ログファイル出力の削除または設定化** — `src/main.py` の `_setup_logging()` にある frozen バンドル向けファイルログ出力（`platformdirs.user_log_dir`）は dmg デバッグ目的で追加したもの。Settings に「デバッグログを保存する」チェック（デフォルトOFF）を追加するか削除する。該当箇所: `src/main.py` 63〜75行目
4. ~~**Autotrack/Record メニューの実装**~~ **→ v0.1.0 以降で完了**（AutotrackRecordDialog・Autotrack Timer・AOS/LOS 自動接続・録音自動制御）

### モバイル・Web UI
5. **スマホ・タブレット画面の継続確認** — Android 実機でのコンパス連動確認、各種ブラウザでの表示確認

### SDR・デジタルモード
6. ~~**SDR機能の追加（フェーズ1: 初期実装）**~~ **→ v0.1.0 で完了**
7. ~~**APRS 受信・送信・位置ビーコン実装**~~ **→ feature/communications（v0.2.0）で完了**（APRSEngine・Direwolf統合・Bell 202 AFSK復調・PTT CAT制御・Doppler凍結・地図ピン表示）
8. ~~**Telemetry タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（AX.25受信・JSON定義デコード・12衛星フォーマット定義）
8b. ~~**Telemetry タブ gr-satellites 統合**~~ **→ 2026-06-30 で完了**（gr-satellites サブプロセス・UDP IQ 転送・330機以上対応・衛星コンボ・SDR 自動接続・トランスポンダー自動選択・メインリスト連動）
9. **テレメトリーフォーマット定義の追加・検証** — 実際に受信したパケットでオフセット・スケールの検証。未定義衛星のフォーマット調査
10. ~~**CI: Direwolf バンドルビルド**~~ **→ feature/communications で完了**（Linux/Windows/macOS 3ジョブ、タグ push 時に direwolf-{platform}-{arch}.{tar.gz|zip} を Releases にアップロード）
11. ~~**FT4 タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（Ft4Codec/ctypes + ft8_lib・Ft4Scheduler・Ft4QsoManager・Ft4Tab UI・ADIF エクスポート。ft8_lib CI バンドルビルドは v0.2.0 タグ時に Direwolf と同時実施）
11c. ~~**Q65 Phase 1（RX）実装**~~ **→ 2026-06-26 で完了**（Q65Codec/libq65 ctypes・build-q65lib.yml CI・Help > Q65 Library Installation ダイアログ）
11d. ~~**Q65 Phase 2（TX/QSO）実装**~~ **→ 2026-06-26 で完了**（純 Python encoder.py: GF(64)・CRC-12・65-FSK / Q65QsoManager: QSOステートマシン・q65_log DB・ADIF / q65_tab.py: TX UI・TX Enable・Halt TX・Log QSO・Export ADIF）
11e. ~~**METEOR / HRPT 受信タブ実装**~~ **→ 2026-06-29 で完了**（SatDump サブプロセス管理・8衛星対応・Autotrack AOS/LOS 連携・SDR Connect・浮動ログウィンドウ・衛星検索ダイアログ）
11f. ~~**CW Decoder タブ実装**~~ **→ 2026-06-30 で完了（v0.2.6）**（deepcw-engine ONNX / onnxruntime 自動 pip インストール / model.onnx 自動ダウンロード / CW-R トランスポンダー自動オープン）
11b. **SDR フェーズ2（将来）— アマチュア衛星・デジタルモード** — HRPT/LRPT は 11e で完了、gr-satellites は 8b で完了、AI-CW は 11f で完了
12. **SDR フェーズ2（将来）— 業務用衛星受信** — Inmarsat-C (STD-C)・Cospas-Sarsat L帯・Iridium L帯 ACARS・Orbcomm・みちびき（QZSS）データ放送（詳細は「業務用衛星受信」セクション参照）
13. ~~**SDR Device Installation ダイアログ**~~ **→ v0.1.0 で実装済み**（src/ui/sdr_install_dialog.py — USB VID/PID スキャン・apt/brew/Zadig 誘導）
14. ~~**Help > gr-satellites… ダイアログ**~~ **→ feature/communications で完了**（src/ui/gr_satellites_dialog.py — 検出ステータス・apt/brew/pip インストール案内）
15. ~~**SSTV / SSDV 受信タブ**~~ **→ feature/communications で完了**（SstvDecoder・SsdvDecoder・SstvTab・SDR audio_ready 接続・AX.25 raw_frame_received タップ）

### 配布・ビルド
15. **Windows・macOS v0.1.0 ビルドの動作確認** — CI ビルド成功後、実機での SDR 含む全機能検証

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
- Help メニューの全ダイアログ（About・Satellite Color Legend・Auto Fetch Rules・SDR Device
  Installation・Check for Updates・Hamlib Update・ft8lib/Direwolf/Q65/FT4拡張/CW Model/SatDump/
  gr-satellites Installation）
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
- f/i（get_freq）コマンドはF/I送信直後に10秒以上かかる → 使用禁止
- アクティブVFO切り替え（V コマンド）はTX点灯を引き起こす → 使用禁止

### 実装上の重要事項
- set_vfo_frequencies()はバックグラウンドスレッドで実行（UIブロック防止）
- _cmd()はソケットタイムアウト10秒
- connect()時に_last_dl_hz/_last_ul_hzをNoneにリセット
- f/iダイアルフィードバックは実装しない（FTX-1非対応）
- S 1 Mainは接続時1回のみ（毎サイクル送らない）

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

### FT-991 / FT-991A (Hamlib models 1035 / 1036)

Hamlib 4.7.1 の公式モデルリスト: **1035 = FT-991**（FT-991A も同バックエンドを使用）。
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
   - `_apply_ctcss_civ_direct()` via rigctld TCP commands (`V Sub / L CTCSS_TONE / U TONE / V Main / U TONE 0`)
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
> - **NET モード（IC-9100 + rigctld）**: 周波数・モード・CTCSSトーン（クロスバンド・同バンド両方）すべて動作確認済み
>   - トランスポンダー選択時の順序: `_send_split_init_independent()`（S 1 Main）→ `_send_freq_preset_independent()`（DL/UL周波数先書き）→ `send_mode_only()` → `_apply_ctcss_civ_direct()`
>   - CTCSS: `_apply_ctcss_civ_direct()` が rigctld TCP コマンド（`V Sub / L CTCSS_TONE / U TONE / V Main / U TONE 0`）を送信（pyserial 廃止・macOS でも動作）
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
L CTCSS_TONE <deci_hz>  # CTCSS 周波数設定（デシ Hz 整数）
U TONE 1/0              # CTCSS エンコーダー ON/OFF
V Main                   # VFO Main を復元
U TONE 0                 # Main の CTCSS クリア（ブリード防止）
```

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
- `HamlibNetController.set_ctcss_tone()`が`L CTCSS_TONE {value}`（rigctldの**LEVEL設定構文**）を使っていたが、CTCSS_TONEはLEVELではなく専用コマンド文字を持つため、rigctldは`RPRT -11`（ENAVAIL）で拒否する。正しくは`C {deci-Hz}`（専用コマンド）。**satmode NETモードのCTCSS実装（`_apply_ctcss_civ_direct()`）にも同一パターンの`L CTCSS_TONE`が存在する。詳細・検証方針は「既知のバグ（未修正）」内「ICOM SATMODE機（IC-9100/9700等）NETモードCTCSS — `L CTCSS_TONE` が壊れている疑い」セクション参照（2026-07-07、実機検証待ちで保留中）**
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
  - NET mode + CTCSS: rigctld TCP commands (`V Sub / L CTCSS_TONE / U TONE / V Main`) — pyserial 廃止、macOS でも動作
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

### ORIGAMISAT-2（NORAD 68795 / 仮 ID 98325）の状態

```
satellites(norad_cat_id=68795):
  is_hidden = 0          ← 表示中
  satnogs_source_id = 98325  ← 仮 ID でトランスポンダーを取得
  alt_names = ["JS1YRU", "FO-126"]
  TLE: source=manual     ← CelesTrakから手動取得・絶対上書きしない

satellites(norad_cat_id=98325):
  is_hidden = 2          ← システム非表示
  transmitters = 0件     ← 全て 68795 に移行済み
```

この衛星は既に最終状態にあり、移行パイプラインは冪等ルールにより何も変更しない。

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
- **初回起動時の未フェッチソース自動検出**: `TLEManager.is_source_stale(source_name)` が `sync_log` 未記録のソースを `True` で返す → MainWindow が起動時に未フェッチグループを即時フェッチ
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

### fetch_active_tles() の2フェーズ設計

CelesTrak `GROUP=active`（全15,000機）は 403 Forbidden で取得不可のため、代替の2フェーズ構成を採用：

**Phase 1 — CelesTrak 複数グループ一括取得（高速）**
アクセス可能なグループを順に取得し、DB にある衛星のみ保存：
- `satnogs`（664機）・`last-30-days`（265機）・`argos`・`orbcomm`・`spire`
- 約 470機分のマッチ → INSERT（`tle_group='amateur'`）または UPDATE（`tle_group` 保持）
- 新規衛星レコードは作成しない

**Phase 2 — SATNOGS TLE API 並列フォールバック（最大 20 並列）**
Phase 1 後も TLE なしの `10000-89999` 衛星を個別照会：
- `GET https://db.satnogs.org/api/tle/?norad_cat_id={norad}&format=json`
- TLE あり → 保存（`source='satnogs'`, `tle_group='amateur'`）
- TLE なし → 上記の自動非表示ルールを適用

### 起動時の TLE 同期フロー

```
アプリ起動
  │
  ├─ APScheduler 開始（2h/4h/6h/12h/24h の定期ジョブを登録）
  │
  ├─ [バックグラウンド] _refresh_satellite_names_sync()
  │     1. sync_satellite_names()    ← SATNOGS 衛星名・ステータス更新・移行パイプライン
  │     2. fetch_provisional_tles()  ← NORAD ≥ 90000 衛星の TLE 取得
  │     3. fetch_legacy_tles()       ← NORAD < 10000 衛星のクリーンアップ（初回のみ実質動作）
  │
  └─ [バックグラウンド・stale時のみ] _refresh_active_tle_sync()
        fetch_active_tles()          ← NORAD 10000-89999 未収録衛星の TLE 補完（24h 経過時）
```

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
- インストール: Linux は `pip install gr-satellites`（GNU Radio 3.10 以上が前提）
- SDR Device Installation ダイアログに gr-satellites のインストール状態確認・誘導を追加予定

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
│  [💾 Save PNG]  [🗑 Clear]   受信: 14:23 UTC / ISS        │
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
| ❌ | gr-satellites 連携（後回し） |

#### gr-satellites 連携（将来）

gr-satellites は GNU Radio が必須依存のためバンドル・自動インストールは行わない。
将来的に以下を追加する:
- システムへのインストール済みを自動検出（Direwolf と同じ方式）
- `Help > gr-satellites...` でインストール案内（apt / Homebrew のコマンド表示のみ）
- GNU Radio / gr-satellites が検出された場合のみ 9600 baud 衛星等の拡張デコードを有効化

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
