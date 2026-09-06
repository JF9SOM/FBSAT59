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

## 作業前に読むべき詳細ドキュメント（docs/）

CLAUDE.md 本体はコア（規約・ルール・アーキテクチャ）のみ。以下の領域に触れる前に、
対応する `docs/*.md` を**必ず Read すること。** 該当ファイルには過去の不具合・実機で
確定した機種別の制約・設計判断が集約されており、読まずに変更すると既知の不具合を再発させる。
全一覧は末尾「詳細ドキュメント索引」を参照。`src/{rig,sdr,comms,data}/` には同趣旨の
スタブ `CLAUDE.md` を置いてあり、そのツリーで作業すると自動で読み込まれる。さらに
`.claude/settings.json` の `PreToolUse` フック（`.claude/hooks/inject-docs.sh`）が、
`src/` 配下のファイルを Edit/Write する直前に、対応する `docs/*.md` の全文をコンテキストへ
自動注入する（セッション内・領域ごとに1回のみ）。対応表:

| Edit/Write 対象 | 自動注入される docs |
|---|---|
| `src/comms/meteor/**` | meteor-satdump.md |
| `src/comms/telemetry/**` | telemetry.md |
| `src/comms/**`（meteor・telemetry 以外） | communications.md |
| `src/rig/**` | hamlib.md, rig-specific-notes.md |
| `src/sdr/**` | sdr.md |
| `src/data/**` | tle.md |
| `src/core/celestial_engine.py` | moon-eme.md |
| `src/core/autotrack.py` | ui-components.md, meteor-satdump.md |
| `src/core/update_check.py` | app-update.md |
| `src/core/**`（他） | doppler-tuning.md |
| `src/ui/main_window.py` | lock-dial-feedback.md, doppler-tuning.md, ui-components.md |
| `src/ui/telemetry_tab.py` | telemetry.md |
| `src/ui/**`（他） | ui-components.md |
| `src/i18n/**` | i18n.md |

| 触る領域 / タスク | 先に読むファイル |
|---|---|
| `src/rig/` 全般・Hamlib・CAT/CI-V・rigctld・`SdrRigAdapter` | [docs/hamlib.md](docs/hamlib.md), [docs/rig-specific-notes.md](docs/rig-specific-notes.md) |
| ドップラー補正・Lock（L ボタン）・Tune・per-transponder RX オフセット | [docs/lock-dial-feedback.md](docs/lock-dial-feedback.md), [docs/doppler-tuning.md](docs/doppler-tuning.md) |
| `src/data/` の TLE・SATNOGS 同期・仮 NORAD ID・トランスミッター DB・DB マイグレーション | [docs/tle.md](docs/tle.md) |
| `src/sdr/` 全般・SoapySDR・デバイス列挙・IQ パイプライン・Remote SDR | [docs/sdr.md](docs/sdr.md) |
| `src/comms/`（APRS/SSTV/FT4/Q65/CW/AX100）・Direwolf・共有サウンドカード | [docs/communications.md](docs/communications.md) |
| MARMOTSat（`src/comms/ax100digi/`・AX100 VHF デジピーターの CSP ヘッダー・将来の HF DVB-S2 受信・NORAD 69912/98272） | [docs/marmotsat.md](docs/marmotsat.md)（＋ [docs/communications.md](docs/communications.md) の AX100 セクション） |
| Telemetryタブ・`src/comms/telemetry/`（Direwolf/AX.25・gr-satellites・衛星選択コンボの構築方式・SATNOGS `status` の意味論） | [docs/telemetry.md](docs/telemetry.md) |
| METEOR/HRPT・SatDump・`src/comms/meteor/`・Autotrack 連携 | [docs/meteor-satdump.md](docs/meteor-satdump.md) |
| Dashboard/Pass Chart/レーダー/Autotrack/Favorite グループ/スマホ Web UI | [docs/ui-components.md](docs/ui-components.md) |
| Moon/EME 追尾 | [docs/moon-eme.md](docs/moon-eme.md) |
| アプリ更新通知・起動時チェック・`update_manifest.json`・重要アップデート告知 | [docs/app-update.md](docs/app-update.md) |
| i18n・翻訳・`.po`/`.mo` 更新 | [docs/i18n.md](docs/i18n.md) |
| CI/CD・ネイティブライブラリのビルド（Hamlib/ft8lib/libq65/libft4wsjt） | [docs/ci-cd.md](docs/ci-cd.md) |
| Windows 実機の動作確認・ログ収集・SSH 接続・`ssh windev`・`scripts/win_launch.bat`・`scripts/bootstrap_natives.py` | [docs/windows-dev-ssh.md](docs/windows-dev-ssh.md) |
| 「既知のバグ」「既知の制約（修正不可）」を確認したいとき | [docs/known-issues.md](docs/known-issues.md) |

---

## プロジェクト概要

**名称**: FBSAT59  
**旧称**: GPredict-Improved（v0.2.0 でAPRS・テレメトリー・FT4・SSTV等の通信機能を大幅増強した機会に、現名称 FBSAT59 へ改称）  
**目的**: アマチュア衛星追尾ソフト GPredict の現代的後継ソフトウェア  
**開発言語**: Python 3.11+  
**対象OS**: Linux（主開発環境: Ubuntu）, Windows, macOS  
**ライセンス**: GPL-2.0（GPredict互換）

**Windows最低バージョン: Windows 8.1以降（Windows 10/11推奨）。無印のWindows 8は非対応**
（詳細は「既知のバグ（未修正）」セクションの「Windows 8（8.1未満）でDLL不足エラーにより
起動できない」参照）。

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
| AMSAT Upcoming Satellites「In Testing」リスト | 24時間ごと | `amsat_upcoming_refresh`（2026-09-05 追加） |
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
- `GET /satellites/` の `status` フィールドは 2026-09 に語彙が変わり、現在は
  `in orbit` / `re-entered` / `future` の3種類のみ（旧 `alive`/`dead` は廃止。運用判断は
  持たず、減衰予測は別途 `decayed` 日付フィールド、受信実績は `reception_status`）。
  `_SATNOGS_STATUS_MAP` は `in orbit`→`alive` にマップする。旧マップは `in orbit` を
  知らず全軌道上衛星を `unknown` に落とすバグがあり、`database.py` に一度きりの DB 修復
  （`db_repair_satnogs_in_orbit_v1`）を入れた。詳細は [docs/tle.md](docs/tle.md) の
  「SATNOGS衛星 `status` 語彙の変更」セクション参照
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

> CelesTrak / SATNOGS に接続できない時の切り分け（過度なアクセスによる IP ブロックの疑い）は [docs/tle.md](docs/tle.md) の該当節を参照。

**CelesTrak/SATNOGSへの接続確認・調査中にファイアウォールブロックが疑われる症状
（DNS解決や他サイトへの接続は正常なのに、CelesTrakまたはSATNOGSへの接続要求だけが
タイムアウトする／サイレントに破棄される）に遭遇したら、原因調査を続ける前にまず
ユーザーへ「自宅ネットワークの回線を、別のグローバルIPを持つ経路（4G Wifiルーター等）
に手動で切り替えてもらえないか」と依頼すること。** ユーザー宅の環境は過去に実際に
CelesTrak/SATNOGS側からIPブロックを受けたことがあり（[docs/tle.md](docs/tle.md) の
該当節参照）、再発した場合の確実な切り分け・回避策は別回線からの接続に限られる
（アプリ側のサーキットブレーカーはブロックの再発防止・自動リトライ用であり、
既にブロックされた状態自体を解除する手段ではない）。切り替え後は
`curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 "https://celestrak.org/..."` 等で
接続を再確認してから調査を再開する。

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

> Hamlib のバージョン管理・配布方針、in-app アップデーター、sys.path surgery、Rotator / NET Controller 実装メモ、NET モード FTX-1 Sub VFO 誤配送バグは [docs/hamlib.md](docs/hamlib.md) を参照。

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

### リリース時の `update_manifest.json`（2026-09-01 追加）

アプリは `raw.githubusercontent.com/.../main/update_manifest.json` を起動時に見て、
旧バージョンのユーザーへ更新を促す。`latest_version` は**タグ push 時に CI の
`bump-manifest` ジョブが自動で `main` に反映**するので手動更新は不要。
`critical` / `minimum_supported_version` だけ手動運用で、旧バージョンが実害を出す
リリース（例: 2026-09 の SATNOGS status 語彙変更）のときだけ引き上げ・`true` にし、
通常リリースでは `critical: false` に戻す。詳細は [docs/app-update.md](docs/app-update.md)。

### リリース（タグ push）時に Claude が必ず確認すること（2026-09-02 追加）

ユーザーから「タグを打って」「リリースして」「`vX.Y.Z` を出して」等の依頼を受けたら、
**タグを push する前に**次の2点をユーザーに質問すること（勝手に判断しない）:

1. **「今回のリリースは *critical* ですか？ アップデートを強制するためのフラグ
   （`update_manifest.json` の `critical`）をオンにしますか？」**
   - ユーザーが「はい（critical）」と答えた場合のみ、タグを打つ前に `main` の
     `update_manifest.json` を `critical: true` にし、`minimum_supported_version` を
     その新バージョンへ引き上げ、`message_ja` / `message_en` を今回の内容に更新して
     push する（アプリが読むのは常に `main` の raw ファイル。タグの中身ではない）。
   - ユーザーが「いいえ（通常リリース）」なら `critical` は `false` のまま。**直前の
     critical リリースから `true` のまま残っていないかも確認**し、残っていれば
     `false` に戻す commit を先に push する。
2. コミット漏れ（`git status` にリリースに含めるべき未コミット変更がないか）と、
   `main` の CI が緑であること。

`latest_version` は CI が自動更新するので触らない。この質問を省略してタグを打たない。

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

> CI/CD トラブルシューティング履歴（v0.1.0-beta.34 で解決済み）は [docs/ci-cd.md](docs/ci-cd.md) へ移動しました。

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

> 開発環境移行 Ubuntu → macOS（2026-08-15）の記録は [docs/dev-environment-migration.md](docs/dev-environment-migration.md) へ移動しました。
>
> **Windows 実機への SSH アクセス**（`ssh windev` で Mac から接続し、**任意のコマンドを
> 実行できる**汎用の開発アクセス。ログ確認・pytest・デバッグ・パッケージ状態確認・
> ソース更新など。ワンクリック起動 `scripts/win_launch.bat` とネイティブ依存の自動取得
> `scripts/bootstrap_natives.py` を含む）は [docs/windows-dev-ssh.md](docs/windows-dev-ssh.md) を参照。
> ユーザーが「**Windows に SSH で入って**」「Windows 版の〜を確認して／試して」等と言ったら
> （用件はログ取得に限らない）、まず [docs/windows-dev-ssh.md](docs/windows-dev-ssh.md) を読み、
> 「任意のコマンドを実行する（汎用）」のクォート／シェル差の注意に従って `ssh windev` で実行する。
> なお Windows のファイルは直接編集しない（編集は Mac で → commit → push → Windows は `git pull` のみ）。

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
- **「In Testing (AMSAT)」フィルタ**（2026-09-05 追加）— [amsat.org/upcoming-satellites/](https://www.amsat.org/upcoming-satellites/) の「In Testing:」表を`AMSATUpcomingFetcher`（`src/data/amsat_upcoming.py`）がスクレイピングし、既存の「Operational (AMSAT)」と同じ名前・designatorマッチングでフィルタリストとして表示。フィルタ選択時のみ「↗ AMSAT Upcoming Page」リンクを表示（アプリモードで開く）。新規打ち上げ直後でTLE/NORAD IDが未確定な衛星は、DB(`satellites`テーブル)に既にある衛星との名前マッチのみで判定するため、TLE取得前はリストに現れない
- **「TLM/Beacon only (AMSAT)」フィルタ**（2026-09-05 追加）— AMSAT Status Page（`https://www.amsat.org/status/`）に埋め込まれた凡例（`#648fff`=Sat/Mode Active、`#ffb000`=TLM/Beacon only、`#dc267f`=Not Heard、`#fe6100`=Conflicting reports）のうち、従来は判定していなかった`#ffb000`（黄）を`AMSATStatusFetcher._parse_tables()`が新たに認識し、既存の（実装済みだが使われていなかった）`"partial"`ステータス値・黄色(`#f1c40f`)表示にマッピング。新規スクレイパー・新規スケジューラジョブは不要（既存`amsat_refresh`・24時間キャッシュを共用）。「↗ AMSAT Status Page」リンクは「Operational (AMSAT)」とこのフィルタの両方で表示される。1衛星が複数モードを持ち一部がActive・一部がTLM/Beacon onlyの場合はActive（Operational）を優先し二重掲載しない
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
    | METEOR-M N2-3 | LRPT | 137.9 MHz（72k） | 1.2 Msps | 57166 |
    | METEOR-M N2-4 | LRPT | 137.9 MHz（72k） | 1.2 Msps | 59051 |
    | METEOR-M N2-3 | HRPT | 1700.0 MHz | 3 Msps | 57166 |
    | METEOR-M N2-4 | HRPT | 1700.0 MHz | 3 Msps | 59051 |
    | NOAA 18 | HRPT | 1707.0 MHz | 3 Msps | 28654 |
    | NOAA 19 | HRPT | 1698.0 MHz | 3 Msps | 33591 |
    | Metop-B | HRPT | 1701.3 MHz | 3 Msps | 38771 |
    | Metop-C | HRPT | 1701.3 MHz | 3 Msps | 43689 |
  - **METEOR-M N2-3/N2-4 の LRPT パイプライン — 一度は4通り（137.1/137.9 MHz × 72k/80k）に
    拡張したが、実際には使われていないことが判明し137.9MHz(72k)のみに戻した
    （2026-08-15 80kパイプライン追加 v0.3.21 → 2026-08-17 周波数選択肢も追加 →
    2026-08-20 137.1MHz/80kを削除）**: N2-3・N2-4はLRPTの周波数（137.1 MHz ⇔
    137.9 MHz）・シンボルレート（72k デフォルト・80k）のどちらも運用側が予告なく
    切り替えることがある、という前提（usradioguy.comの運用ログで過去に切り替えが
    確認されていた）から、当初は両衛星とも4通り全ての組み合わせを選択肢として
    用意していた。しかし2026-08-20、実運用に詳しいユーザーがFacebook上のAPT/LRPT
    受信コミュニティに直接確認したところ、**現時点ではN2-3・N2-4とも137.1MHz・
    80kのどちらも使われておらず、両衛星とも常に137.9MHz(72k)固定**という回答を
    得た。これを受けて4エントリ構成を撤回し、各衛星につき137.9MHz(72k)の
    1エントリのみに戻した（`meteor_m2-x_lrpt_80k`パイプラインは以後
    `METEOR_PIPELINES`から参照されなくなった。SatDump側のパイプライン定義
    自体は削除していないため、将来また運用が変わった場合は同じ要領で
    再度エントリを追加できる）。
    周波数がずれている場合はSatDumpが信号自体を見つけられない（Viterbi
    UNSYNCED）。周波数は合っているがシンボルレートがずれている場合は
    **Viterbiは`SYNCED`になりSNRも数dBと妥当な値を示すのに、Deframerだけが永久に
    `NOSYNC`のまま**という紛らわしい症状になる（畳み込み符号の復号はシンボルレート
    誤差にある程度寛容だが、フレーム同期語の検出はタイミングのズレが蓄積して
    合わなくなるため）。仰角60度・SNR良好なN2-4パスで実際にこの症状（10分間
    Viterbi SYNCED・Deframer NOSYNC）が発生し確認済み——ただしこれも今回の
    ヒアリングを踏まえると、AGCオンによる偽ロック（本ファイル「METEOR受信専用の
    RF Gain設定」セクション参照）が主因だった可能性が高く、パイプライン自体の
    ミスマッチが原因だったかどうかは確定していない。METEOR/HRPTタブのLock
    インジケーターは`Deframer: synced`を見て緑にする実装（本ファイル該当
    セクション参照）のため、これは検出ロジックの誤りではなくSatDump側の
    復調状態を正しく反映した結果だった。
  - **METEOR受信専用のRF Gain設定（2026-08-18 追加）**: 従来、METEORタブが起動する
    SatDumpのRFゲイン（`--agc`/`--gain`）はRig Settings > SDR SettingsのRF Gain欄
    （`sdr_settings`、Rig 1/2としてのSDR・SDR Controlタブと共用）をそのまま流用していた。
    しかしこの共用設定はFM受信等の別用途に合わせて調整されることが多く、137MHz帯の
    LRPT受信に最適とは限らない（固定ゲインが高すぎるとVHF帯の近隣強信号でフロントエンドが
    飽和し、SNR表示だけ高く出て実際には復調できない、という実機報告あり）。METEORタブ
    自身（受信画像下の`📁 Open Folder`/`🗑 Clear`ボタンの並びに追加、行の増設なし）に
    独立した`Gain: [dB]`コントロール（QSpinBoxのみ）を新設し、`meteor_settings`
    （`sdr_settings`とは別の`app_settings`キー）に保存するようにした。初回
    （`meteor_settings`が空の間）はその時点の`sdr_settings`の値を初期値として
    引き継ぐが、一度でもユーザーがこのコントロールを操作すると以降は完全に独立し、
    Rig Settings側を後から変更しても影響を受けない。`source`（デバイス選択）・`ppm`は
    引き続き`sdr_settings`を共用（変更していない）。
    **AGC（Auto）は選択肢自体を廃止し常時手動固定（2026-08-20）**: 実機検証
    （N2-3・N2-4、macOS/Windows双方）で、RTL-SDRのハードウェアAGCをONにすると、
    実際の信号強度や衛星の可視性と無関係に、Viterbi側だけが約3.5dB・BER 0.11〜0.21
    という再現性のある固定値で「SYNCED」を報告し続ける偽ロックが生じることが確認された
    （アンテナを完全に外した状態でも同じ値が出続けた）。AGCオフ（固定ゲイン）にすると
    同じ状況で正しくNOSYNC/SNR 0dBを示し、実際の衛星可視時間に一致してSNRが
    0→最大値→0と滑らかに変化する健全な追尾パターンが再現された。このためAuto
    ラジオボタンは廃止し、`Gain:`欄は常に手動指定のQSpinBoxのみとした（既定値40dB。
    ただしこの既定値自体もこれまで一度も成功実績がなく、実際の最適値は今後の実運用で
    調整が必要）。`SatDumpProcess`への`agc`引数は常に`False`で呼び出される。
  - **UI 構成**（コンパクト2行レイアウト）:
    - Row 1: `Pipeline:` コンボ + `[SDR Connect]` + `[▶ Start]` + `[■ Stop]` + `[📋 Log]`
    - Row 2: ロックインジケーター + プログレスバー + ステータスラベル
    - 下部: 受信画像プレビュー（左。Image/Waterfallの2タブ構成——受信中は
      SatDumpのFFT HTTP APIをポーリングしたライブWaterfallへ自動切替、完了で
      Imageへ自動復帰。詳細は「METEORタブのライブWaterfall表示」参照。
      `📁 Open Folder`/`📂 Open Past Reception…`/`🗑 Clear`ボタン付き）
      ＋ 受信履歴サムネイル（右）の水平スプリッター（`📂 Open Past
      Reception…`の詳細は「過去の受信フォルダをタブ内で見返す機能」参照）
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

> 各機能の詳細設計・不具合調査履歴は下記「詳細ドキュメント索引」の該当ファイルを参照。

---

## 詳細ドキュメント索引

以下は CLAUDE.md 本体から分離した詳細ドキュメント（`docs/` 配下）。
**関連する機能の変更・不具合調査を行う前に、該当ファイルを必ず読むこと。** 普段は読み込まなくてよい。

本体・各ドキュメント内に残る「〜『XXX』セクション参照」という相互参照は、参照先が
`docs/` 配下のファイルへ移動している場合がある。見つからなければ `grep -rn 'XXX' docs/` で探すこと。

| ファイル | 内容 |
|---|---|
| [docs/hamlib.md](docs/hamlib.md) | Hamlib バージョン管理・配布方針、in-app アップデーター、sys.path surgery、Rotator catch-up、NET Controller 実装メモ、NET モード FTX-1 Sub VFO 誤配送バグ |
| [docs/rig-specific-notes.md](docs/rig-specific-notes.md) | リグ機種別実装ノート（FTX-1F / FT-991 / IC-9100 / IC-9700 / IC-910H / IC-821H / IC-705）、CAT モード変換、GitHub Issue #16 続報（IC-9700） |
| [docs/lock-dial-feedback.md](docs/lock-dial-feedback.md) | Lock（L ボタン）dial feedback 設計、Ctrl+L、SDR 専用 Lock、Passband Tune 再設計 |
| [docs/doppler-tuning.md](docs/doppler-tuning.md) | 永続 per-transponder RX オフセット、帯域中心 Doppler 追尾、AO-73 反転トランスポンダー修正・SatNOGS 公式値リセット |
| [docs/tle.md](docs/tle.md) | TLE 取り込みルール全体設計、仮 NORAD ID（90000 番台）衛星管理、SATNOGS status 全件取得、CelesTrak/SATNOGS ブロック切り分け |
| [docs/sdr.md](docs/sdr.md) | SDR 機能設計方針（SoapySDR / Windows ctypes バイパス / PlutoSDR / Remote SDR / Doppler 補正）、実装済み SDR 機能一覧 |
| [docs/communications.md](docs/communications.md) | APRS / SSTV・SSDV / FT4 / Q65 / CW / AX100 Digi / gr-satellites / SatNOGS アップロード、Comms Quick Panel、共有サウンドカード、ログ UDP ブロードキャスト、コミュニティ周波数 |
| [docs/telemetry.md](docs/telemetry.md) | Telemetry タブ（Direwolf/AX.25・gr-satellites）の衛星選択コンボ構築方式、AFSK自動トランスポンダー選択スコアリング、SATNOGS `alive`/`status` の意味論、gr-satellites 衛星カタログソース、ゴーストエントリ問題の実例と修正 |
| [docs/meteor-satdump.md](docs/meteor-satdump.md) | SatDump 検出・起動の一連の修正、METEOR/HRPT タブ、ライブ Waterfall、過去受信フォルダ、Autotrack 連携の不具合群（Issue #27） |
| [docs/marmotsat.md](docs/marmotsat.md) | MARMOTSat の現状と方針（NORAD 69912/98272、AX100 VHF デジピーターの CSP ヘッダー未確定問題と調査履歴、HF DVB-S2 保留の経緯、監視先・再開トリガー） |
| [docs/ui-components.md](docs/ui-components.md) | Dashboard タブ、Group Pass Chart、Autotrack 設計、カスタム Favorite グループ、スマホ Web UI |
| [docs/moon-eme.md](docs/moon-eme.md) | Moon/EME 追尾設計（DE421 / CelestialEngine / EME ドップラー往復補正 / EME 周波数） |
| [docs/app-update.md](docs/app-update.md) | 起動時アップデートチェック、`update_manifest.json`、重要アップデートの強制告知、リリース時の manifest 更新手順 |
| [docs/i18n.md](docs/i18n.md) | 多言語化ロードマップ、翻訳範囲、i18n 実装上の落とし穴、日本語 .po 更新手順 |
| [docs/known-issues.md](docs/known-issues.md) | 既知の制約（プラットフォーム由来・修正不可）、既知のバグ（未修正） |
| [docs/ci-cd.md](docs/ci-cd.md) | CI/CD トラブルシューティング履歴（Hamlib / macOS / Windows / ft4wsjt ビルド固有） |
| [docs/dev-environment-migration.md](docs/dev-environment-migration.md) | 開発環境移行 Ubuntu → macOS（2026-08-15）の記録 |
| [docs/windows-dev-ssh.md](docs/windows-dev-ssh.md) | Windows 実機への SSH アクセス（`ssh windev`）、ワンクリック起動ランチャー、ネイティブ依存の自動取得、ソース実行時の SDR 対応、再構築手順 |
| [docs/roadmap.md](docs/roadmap.md) | 次回の作業候補 |
