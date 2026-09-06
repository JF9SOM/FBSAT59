# Communications 機能 詳細設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

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

#### SatNOGS DB へのテレメトリー自動アップロード（Phase 1 実装済み、2026-08-31）

受信したテレメトリーフレームを SatNOGS DB（`db.satnogs.org`）へ自動投稿する機能。
SkyRoof（VE3NEA、GPL-3.0）の `SkyRoof/DSP/SatnogsUploader.cs` を参照実装として移植した。

**プロトコル（SiDS = Simple Downlink Sharing Convention）**:
- `POST https://db.satnogs.org/api/telemetry/`（本番固定。db-dev はプラットフォーム開発用のため使わない）
- `Content-Type: application/x-www-form-urlencoded`、1 フレーム 1 POST、タイムアウト 15 秒、**リトライなし**
- 認証: `Authorization: Token <APIキー>`。**SatNOGS DB は仕様変更で匿名投稿を廃止し API キー必須**
  （未認証だと HTTP 401）。キーは SatNOGS DB アカウントの Settings → API Key で取得する恒久キー
- フォームフィールド（SkyRoof と同一書式）:
  | フィールド | 値 |
  |---|---|
  | `noradID` | NORAD 番号 |
  | `source` | 自局コールサイン（trim + 大文字） |
  | `locator` | 固定文字列 `longLat` |
  | `longitude` | `abs(経度)` 小数4桁 + `E`/`W`（例 `139.6917E`） |
  | `latitude` | `abs(緯度)` 小数4桁 + `N`/`S`（例 `35.6895N`） |
  | `timestamp` | `%Y-%m-%dT%H:%M:%S.fffZ`（UTC・ミリ秒・末尾リテラル `Z`） |
  | `frame` | 生フレームの hex（**FCS 抜きの全 AX.25 フレーム**。KISS / AfskDemodulator が受信した `raw` バイト列そのまま） |
  | `version` | `FBSAT59 <version>` |

**実装（`src/comms/telemetry/satnogs_uploader.py`）**:
- `SatnogsUploader` — `get_satnogs_uploader()` でプロセス全体シングルトン（`comms.log_broadcast` と同じパターン）
- `queue.Queue` + 単一 `threading.Thread` ワーカー（`DopplerWorker` / `Ft4RxCaptureWorker` と同じ「素の Thread」流儀。QThread 非依存＝テストしやすい）
- **SiDS フィールドは GUI スレッド側（`submit()` → `build_submission()`）で全解決**し、ワーカーには
  `(api_key, form_fields)` の完成品だけを渡す。ワーカーは `httpx.post` のみで **SQLite ハンドルを
  スレッドまたぎで一切触らない**（SkyRoof の `Submit` がデコードスレッドで dict を組んでキューへ
  積むのと同じ設計）
- 失敗（401・接続エラー等）はレスポンスボディを `fbsat59.log` に記録するのみ。QSO ログを止めない
  fire-and-forget 原則（`LogBroadcaster` と同じ）
- 設定は `app_settings` の単一 JSON キー `satnogs_upload_settings`（`{"enabled": bool, "api_key": str}`）。
  `load_/save_satnogs_upload_settings(conn)` ヘルパー
- コールサイン・座標は `app_settings`（`callsign` / `observer_location`）から**毎 submit 読み直し**
  （キャッシュしない＝設定変更後の再起動不要）
- 送信ゲート（この全部を満たしたときだけ投稿）: `enabled` / API キー非空 / コールサイン非空 /
  座標取得済み / `norad` が None でない。**CRC ゲートは実装していない** — FBSAT59 が UI に出す
  フレームは AFSK（HDLC CRC-16/CCITT）・Direwolf・gr-satellites（FEC/CRC）いずれも検証済みのため
  （SkyRoof は自前デコーダーが CRC-unknown を出しうるので明示ゲートが要るが、FBSAT59 は
  「受信できた＝検証済み」）

**UI（`src/ui/telemetry_tab.py`、フッター1行）**:
- `[SatNOGS Upload: ON/OFF]`（`QPushButton` + `setCheckable`。ON=緑 `#27ae60` / OFF=グレー
  `#7f8c8d`、白文字。赤は「エラー」色なので単に無効なだけの状態には使わない、ユーザー判断）
- `[API]` — `_SatnogsApiKeyDialog`（案内文 + 平文 `QLineEdit` + OK/Cancel）
- `[SatNOGS ↗]` — `open_satnogs_requested(norad, name)` を emit → `MainWindow._open_in_satnogs`
  （既存の `satnogs_uuid` キャッシュ / バックグラウンド取得を再利用）。`_active_norad()` は
  アクティブなモードのコンボの選択衛星、無ければメインリストの選択にフォールバック
- `Clear Log` を `Export CSV…` の左へ移動（フッターの行数は増やさない、ユーザー要望）
- **gr-satellites モードのときは上記3ボタンを `setVisible(False)` で完全非表示**（`_on_mode_changed()`）。
  Phase 1 は AFSK 経路のみ対応のため
- ON にした時に API キー / コールサイン / 位置が未設定なら `_lbl_status` にヒント表示（送信自体は
  サイレントにスキップ）

**テスト**: `tests/test_satnogs_uploader.py`（Qt 非依存・実ネットワーク不要、fake post_fn）＋
`tests/test_telemetry_tab.py` にフッター UI テストを追加。

#### Phase 2（gr-satellites 経路対応 — 2026-09-04 実装済み）

gr-satellites 経路は `_on_gr_telemetry(text: str)` が **stdout のパース済みテキストしか受け取らず、
生フレームバイト列がパイプラインのどこにも存在しない**ため、Phase 1 では対象外にしていた。

**採用方針**: `gr_satellites` の起動 argv に `--kiss_server <port> --kiss_server_address 127.0.0.1`
を追加し、FBSAT59 側から KISS TCP クライアントで接続してデータフレームごとに生バイト列を取り出す。
**stdout のテレメトリーパース表示（テーブル）は変更していない**（`--kiss_server` は追加の frame
出力シンクであり、`--hexdump` と違って stdout パースを潰さない）。`--hexdump` パース案は
「パース済み表示を潰す」「テキストレイアウトが版差に弱い」ため却下。

**実装**（`src/comms/telemetry/gr_satellites_backend.py`）:
- `_supports_kiss_server(argv_prefix, env)` — `<argv> --help` の出力に `--kiss_server` 文字列が
  含まれるかで判定。結果は argv_prefix ごとにプロセス内キャッシュ（`_kiss_server_supported_cache`）
  し、`start()` のたびに約0.3秒かかる `--help` 起動を毎回走らせない。gr_satellites 3.x（`--kiss_server`
  はあるが `--kiss_server_address` が無い版）等の古いシステムインストールでは `False` になり、
  この場合は `--kiss_server` 系フラグを一切 argv に追加しない（フォールバック。ゲート＋
  `status_changed` に「SatNOGS upload unavailable (gr_satellites too old for --kiss_server)」を
  一言追加するのみで、gr-satellites 自体の動作は妨げない）
- `_KissFrameReader`（素の `threading.Thread`。既存の `_read_stdout` と同じ流儀）— 空きポート
  （`comms.meteor.fft_waterfall.find_free_port()` を再利用）へ 200ms 間隔・約3秒のリトライで接続し、
  `comms.aprs.direwolf._kiss_decode_frames()` をそのまま再利用してデフレーム（型バイト除去の
  薄いラッパーは不要と判明——同関数がすでにデータフレーム判定・アンエスケープ・コマンドバイト
  除去まで行う）。データフレームごとに新シグナル `raw_frame_received: Signal(bytes)` を emit
- `started_norad` プロパティ — サブプロセスは 1 衛星向け起動なので per-frame の NORAD 解決は不要
- `stop()` の順序: **KISS ソケット close → プロセス terminate/wait → stdout reader・KISS reader
  の両スレッド join**（`AudioBridge`/Direwolf と同じ「producer を先に止めないとブロッキング read
  が返らない」教訓）

**`src/ui/telemetry_tab.py`**:
- `_gr_backend.raw_frame_received` → `_on_gr_raw_frame(raw)` を接続。`started_norad` が None
  でなければ `get_satnogs_uploader().submit(self._conn, raw, norad, now)` を呼ぶだけ
  （`_on_ax25_frame()` の AFSK 経路と同型）
- **gr-satellites モードで SatNOGS 3ボタンを隠していた `_on_mode_changed()` の
  `setVisible(False)` を revert**——Phase 2 では gr モードでも投稿対象になるため常時表示

**gr_satellites 自身の `submit_tlm` には任せなかった理由**: `~/.gr_satellites/config.ini` の
`[Groundstation]` にトークン欄が無く API キー必須化後の匿名 SiDS は 401 の可能性が高い／設定が
別ファイルに分散しトグルと不整合／投稿結果が FBSAT59 のログに出ない／トグルと無関係に全衛星を
投稿してしまう、等。

**テスト**: `tests/test_gr_satellites_backend.py`（`_supports_kiss_server` のキャッシュ・フラグ検出・
`start()` への配線・`_KissFrameReader` を実ソケットで検証）、`tests/test_telemetry_tab.py`
（`_on_gr_raw_frame` の転送・gr モードでの表示維持）。

**バンドル版（v5.9.0）でのスモークテスト（2026-09-04、手動）**: `gr_satellites 43803 --udp
--udp_port <port> --iq --samp_rate 48000 --kiss_server <port2> --kiss_server_address 127.0.0.1`
を実行し、KISS TCP ポートへの接続に成功、stdout の起動ログ（`socket_pdu` 警告・
`udp_source: Listening...`）も従来通り出力されることを確認済み。**ただし実信号を復号して
SatNOGS へ実際に POST するところまでの完全な E2E（録音 IQ + 実 gr_satellites）は未実施** —
CI 非対象・手動検証が必要な項目として残っている（AX.25 衛星1機・非 AX.25 衛星1機の録音 IQ で
「KISS ペイロード = 全フレームか」を確認すること）。

**詳細な実装計画は下記「【別添】Phase 2 実装計画詳細（gr-satellites 経路）」を参照（実装済み後の記録として残す）。**

#### 未実装（任意・将来）— 1フレームごとの投稿ステータス記録

現状は投稿の成否を `fbsat59.log` に書くだけで、`telemetry_log` テーブルには記録しない
（Phase 1 では「選択肢A」を採用、ユーザー判断）。UI に「✓ 投稿済み」列を出したい場合は
`telemetry_log` に `satnogs_submitted INTEGER DEFAULT 0` を追加（`rx_offset_hz` と同じ
`ALTER TABLE` パターン）し、ワーカースレッドから成功時に `UPDATE` する必要があるが、
その場合バックグラウンドスレッドからの DB 書き込み（専用コネクション or シグナルで GUI
スレッドへ marshal）が新たに必要になる。`LogBroadcaster` が UDP のみで DB を触らないことで
避けている複雑さなので、要望が出てから追加する。

#### 【別添】Phase 2 実装計画詳細（gr-satellites 経路）

**ステータス**: 2026-09-04 実装済み（手順1〜6・8完了、手順7の完全E2Eのみ手動検証待ち。
上の「Phase 2（gr-satellites 経路対応 — 2026-09-04 実装済み）」参照）。以下は着手時に
書いた計画の原文（実装後もどのような代替案を却下したかの記録として残す）。

##### 1. 方針: `--kiss_server` サイドチャネル

`gr_satellites` の起動 argv に `--kiss_server <port>` を追加し、FBSAT59 側から KISS TCP
クライアントで接続。KISS データフレームごとに生バイト列を取り出して
`SatnogsUploader.submit()` に渡す。**stdout のパース済みテキスト表示（テーブル）は一切
変更しない**。

ローカルの gr_satellites v5.9.0（バンドル版と同一）で確認済みのフラグ:
- `--kiss_server [PORT]` — PORT はフラグの任意位置引数（`--kiss_server 8200`）。別途
  `--kiss_server_port` は無い
- `--kiss_server_address KISS_SERVER_ADDRESS` — バインドアドレス（デフォルト `127.0.0.1`）
- `--kiss_server` は**追加の frame 出力シンク**であり、`--hexdump` と違って stdout の
  テレメトリーパースを潰さない（両立する）

**却下した代替案**:
| 案 | 却下理由 |
|---|---|
| `--hexdump` パース | stdout のパース表示を潰す（テーブルの中身が失われる）。`pdu vector contents =` のテキストレイアウトが gr_satellites 3.x/4.x/5.x で変わり版差に弱い |
| `--kiss_out <file>` + `--kiss_append` | ファイル tail・ローテーション・「BE u64 ms タイムスタンプ前置き KISS 変種」の扱い・後始末が必要 |
| `--zmq_pub` | pyzmq 依存が自 venv 側に増える。KISS は新規依存ゼロ |

##### 2. 変更コンポーネント

**`src/comms/telemetry/gr_satellites_backend.py`**:
- 新シグナル `raw_frame_received = Signal(bytes)`
- `start()`:
  - 空き TCP ポートを動的確保（`comms/meteor/fft_waterfall.py::find_free_port()` と同じ要領）し
    `cmd` に `--kiss_server <port> --kiss_server_address 127.0.0.1` を追加
  - Popen 後、`_KissFrameReader`（**素の `threading.Thread`**。この backend は既に `_read_stdout`
    を素スレッドで回しているので合わせる）を起動。`127.0.0.1:<port>` へ接続リトライ
    （200ms 間隔・約3秒。gr_satellites 側のサーバー起動待ち）→ ストリームを読み、
    **`comms.aprs.direwolf._kiss_decode_frames()` を再利用**して KISS デフレーム →
    データフレーム（type nibble == 0）ごとに `raw_frame_received.emit(bytes)`
  - **接続前にデコードされた最初の数秒分のフレームは失われる**（gr_satellites の KISS サーバーは
    未接続クライアント向けにバッファしない）。Phase 1 と同じく「Start 後のみ」なので許容
- `stop()`: **KISS ソケット close → プロセス terminate → 両スレッド（stdout / kiss reader）を
  join** の順。`AudioBridge`/Direwolf で学んだ「先に producer を止めないとブロッキング read が
  返らない」教訓（本ファイル該当セクション参照）
- `_started_norad: int | None` を保持（フレームの NORAD 帰属に使う。サブプロセスは 1 衛星向け
  起動なので per-frame 解決は不要）

**`src/ui/telemetry_tab.py`**:
- gr 起動パスで `self._gr_backend.raw_frame_received` → `_on_gr_raw_frame` を接続
- `_on_gr_raw_frame(raw: bytes)`:
  ```python
  norad = self._gr_backend.started_norad
  if norad is not None:
      get_satnogs_uploader().submit(self._conn, raw, norad,
                                   datetime.datetime.now(datetime.UTC))
  ```
- **commit `f95bd3b` の「gr-satellites モードで SatNOGS 3ボタンを `setVisible(False)`」を revert**
  （`_on_mode_changed` の可視性ループ + テスト `test_satnogs_controls_hidden_in_gr_satellites_mode`）。
  Phase 2 では gr モードでも SatNOGS 投稿が有効になるため
- `_active_norad()` は既に gr モードで `_combo_gr_sat` を見る実装済み。SatNOGS ↗ リンクも
  そのまま gr 衛星で機能する

**（任意）`telemetry_log` への gr フレーム永続化**: 現状 gr 経路は `telemetry_log` に一切
INSERT していない（`_on_gr_telemetry` は `_append_row` のみ）。生バイトが手に入るので
`_on_gr_raw_frame` で `raw_hex` = フレーム全体として INSERT を追加してもよいが、
**スコープを絞るなら Phase 2 コア（投稿）に含めず別途**。

##### 3. フレームの中身・タイムスタンプ

- **`frame` の中身**: KISS ペイロード = gr_satellites のデフレーマ出力そのまま（AX.25 衛星なら
  AX.25 ヘッダ込み、非 AX.25 なら deframe 後のパケット）。これは gr_satellites 自身の
  `submit_tlm` が SiDS で送るのと同じ内容 → SatNOGS の期待と一致
- **`timestamp`**: `--kiss_server`（プレーン KISS）は RX タイムスタンプを埋めない。
  `received_at` = FBSAT59 の reader がフレームを受け取った壁時計。IQ バッファ + デコードの
  パイプライン遅延（通常 1 秒未満）が乗るが、SkyRoof も submit 時刻（`DateTime.UtcNow`）を
  使っており SatNOGS の許容も緩いので問題なし。より正確にしたければ
  `core.clock_offset.corrected_utcnow()`（FT4/Q65 が使う NTP 補正時計）に **AFSK 経路含め
  両方**そろえる、というのも選択肢（別判断）

##### 4. gr_satellites 版・フラグ可用性

- `--kiss_server` は gr-satellites 3.x から存在。`--kiss_server_address` は 4.x+。バンドル版
  （5.9.0）は全対応
- システム（apt）インストールが古い 3.x の可能性 → `--kiss_server_address` を省略
  （デフォルト `127.0.0.1`）してフォールバック
- backend 初期化時に `gr_satellites --help` の usage 文字列をキャッシュし `--kiss_server` の
  有無を判定（`detect_gr_satellites()` と同じゲート思想）。無ければ Phase 2 配線をスキップし、
  gr モードで「この gr_satellites 版は KISS サーバー非対応のため SatNOGS 投稿不可」を一言表示

##### 5. テスト

| 対象 | 方法 |
|---|---|
| KISS デフレーマ | `comms.aprs.direwolf._kiss_decode_frames()` は既存。gr 由来の型/ポートバイト除去の薄いラッパーを足すならそこを単体テスト（空・エスケープ `DB DC`/`DB DD`・read またぎの部分フレーム・連続フレーム・非データ型の無視） |
| `_KissFrameReader` | `socket` で 127.0.0.1:0 に実サーバーを立て、テスト側から KISS バイト列を書き込み、`raw_frame_received` が正しいバイト列で発火することを検証（`tests/test_log_broadcast.py` の実ソケット方式と同型。Qt 非依存に寄せる） |
| `telemetry_tab._on_gr_raw_frame` | `get_satnogs_uploader` を monkeypatch（Phase 1 テストと同じ）し、起動 NORAD で `submit()` が呼ばれることを検証 |
| エンドツーエンド（実 gr_satellites + 実 IQ） | **CI 非対象**（インストール + サンプル必要）。AX.25 衛星1機 + 非 AX.25 衛星1機の録音 IQ を fixture 化し、手動検証手順として文書化。「KISS ペイロード = 全フレーム（info のみでない）」をここで確認 |

##### 6. スコープ境界

**やる**: gr-satellites 経路の生フレーム → KISS server 経由 SatNOGS 投稿 / gr モード非表示の
revert / デフレーマ + reader のテスト

**やらない（Phase 2 では）**:
- CW テレメトリー → バイト列化（衛星ごとの符号→バイト マッピングが必要、機種依存で重い。
  SkyRoof も CW は投稿しない）
- SSTV / SSDV / FM 音声の投稿（SkyRoof も対象外）
- `telemetry_log` スキーマ変更 / per-frame 投稿ステータス列（引き続き先送り）
- `--kiss_out` ファイルリプレイ

##### 7. リスクと緩和

| リスク | 緩和 |
|---|---|
| KISS サーバーのポート衝突 | 空きポート動的確保。`--kiss_server_address 127.0.0.1` でローカル限定 |
| reader スレッドと stdout スレッドの 2 本化でシャットダウン競合 | `stop()` で「socket close → proc terminate → join ×2」の順を厳守。テストで stop の冪等性・ブロッキング read 解放を確認 |
| 接続前フレームの取りこぼし | 仕様（Phase 1 と同じ「Start 後のみ」）として許容。リトライ接続で起動レースは吸収 |
| バンドル conda-pack 版で KISS サーバーが動くか | 5.9.0 は対応。念のため E2E 手動検証で 1 回確認 |
| 非 AX.25 衛星で KISS ペイロードが info のみだった場合 | E2E 手動検証で「全フレームか」を確認。gr_satellites の submitter と同内容なので基本問題なし |

##### 8. 作業順序

1. `gr_satellites --help` で `--kiss_server [PORT]` / `--kiss_server_address` をバンドル版・
   システム版それぞれで最終確認
2. `_kiss_decode_frames()` の再利用可否確認（型バイト除去の薄いラッパー要否）
3. `GrSatellitesBackend`: `raw_frame_received` シグナル + `_KissFrameReader` + `start()`/`stop()`
   改修 + `_started_norad`
4. デフレーマ / reader の単体テスト
5. `telemetry_tab`: `_on_gr_raw_frame` 配線 + gr モード非表示 revert + テスト
6. `--kiss_server` 非対応版のフォールバック（ゲート + 一言表示）
7. E2E 手動検証（録音 IQ、AX.25 + 非 AX.25 各 1 機）→ 手順を本ファイルに追記
8. 本節を「実装済み」に更新（採用方針・stdout パース維持・非表示 revert を明記）
9. コミット前チェックリスト（`ruff` / `mypy` / `test_rig.py`）→ コミット

##### 9. 規模感

コア（手順 3〜6）は Phase 1 と同程度（新規 reader スレッド + デフレーマラッパー + 配線 +
テスト）。E2E 手動検証（手順 7）に実機の gr_satellites と録音 IQ の準備時間が別途かかる、
というのが Phase 1 との主な違い。

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

**`is_aprs_transmitter()` の判定基準（2026-09-06 変更、C2 ルール）**:
`"APRS" in desc` **または** `("DIGIPEATER" in desc かつ mode == "AFSK")`。
以前は `"APRS" in desc or mode == "AFSK"` で、**mode=AFSK 単独で一致**していた。
これだと KOSEN-2R「Mode U - AFSK1k2 - AX.25」のような **AFSK1k2 の AX.25
テレメトリービーコン**（本来 Telemetry タブの担当）まで APRS 扱いになり、
(a) そのトランスポンダーを選ぶと Rig 1 接続時に APRS タブが自動オープンする
（`_check_comms_auto_open` が `_pending_comms_tab="aprs"` をラッチ →
`_finish_rig1_connect` が `aprs_transponder_selected` を emit）、
(b) APRS の Quick Panel 衛星フィルタに AFSK テレメトリー衛星が数十件並ぶ、
という2つの実害があった（実機報告 2026-09-06）。`mode=="AFSK"` 単独を落とし、
"DIGIPEATER" 表記かつ AFSK のものだけ追加で拾うことで、"APRS" 表記の無い実在の
AFSK1k2 パケットデジピーター（CSS Tianhe / KOYO / SCION-X / UiTMSAT-2 / PARUS-T1、
いずれも mode=AFSK）は維持しつつ、Direwolf で復調できない GMSK / FSK-AX.100
デジピーター（MARMOTSat・BEESAT 等）と AX.25 テレメトリー機は除外する。
Telemetry タブのマッチャー `is_ax25_telemetry_transmitter()` は従来どおり
mode=AFSK 単独で全 AFSK1k2 機を拾う（こちらはそれが正しい）。

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

**`Download & Install` ボタンの参照先と展開（2026-09-06 修正）**: `_InstallWorker`
（`src/ui/direwolf_dialog.py`）は当初アプリ本体リリース（`releases/latest`）の
アセットから `direwolf-<os>-<arch>.tar.gz` を探していたが、事前ビルド済みバイナリは
専用タグ **`direwolf-bundle`** リリースにしか無い（本体リリースの添付は
`.dmg`/`.exe`/`.AppImage` のみ。`build-direwolf.yml` 参照）ため、macOS/Linux では
必ず "No bundled Direwolf package found" で失敗していた。参照先を
`releases/tags/direwolf-bundle` に変更。加えて macOS/Linux の tarball は全体が
単一の `direwolf-flat/` ディレクトリで包まれている（`tar -C /tmp direwolf-flat`）
のに対し `tar.extractall(dest_dir)` はそれを剥がさないため、バイナリが
`<user_dir>/direwolf/direwolf-flat/direwolf` に置かれ `find_direwolf()`
（`<user_dir>/direwolf/direwolf` を見る）が検出できなかった。staging ディレクトリへ
展開してから「単一トップレベルディレクトリならその中身を、そうでなければ全体を」
`dest_dir` へフラット移動する方式に変更（Windows zip はトップレベルディレクトリ無し
なので後者の分岐でそのまま通る）。

**macOS: GUI 起動時の PATH に Homebrew/MacPorts が入らない問題（2026-09-06 修正）**:
Finder や `.app` ラッパー（`run.command` のような非ログインシェル経由を含む）から
起動されたアプリは、`/opt/homebrew/bin` 等を欠いた最小 PATH を継承する。この状態では
`brew install direwolf` 済みでも `find_direwolf()` の `shutil.which("direwolf")` が
None を返し「未インストール」と表示される（rigctld・satdump など他の外部コマンドも
同様）。`src/main.py` の起動処理冒頭で、macOS では実在する
`/opt/homebrew/{bin,sbin}` `/usr/local/{bin,sbin}` `/opt/local/{bin,sbin}` を
`os.environ["PATH"]` の先頭に追加する（frozen/ソース両方で実行。対話シェルの
PATH 優先順位に合わせて prepend）。

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

**Linux 限定にガード（2026-09-06 確定）**: この settle-reopen は当初プラットフォーム
分岐なしで全 OS で実行していたが、macOS + 汎用「USB Audio CODEC」（安価なリグ I/F
チップ）環境で、**close → 即 reopen した側のストリームが完全な無音（全サンプル 0.0）を
返し続ける**という逆効果が実機で確認された（CW デコーダーのレベルが永続的に
`-200.0 dB`＝ビット単位の無音。一方 Rig Settings のレベルメーターは
`_SharedInputStream` を通さず独自に `sd.InputStream` を開くため reopen されず
`-33 dBFS` と正常に振れており、この非対称性が切り分けの決め手になった）。
元々この現象自体が Linux/PipeWire 固有（PipeWire のルーティング安定化待ちが有力な
仮説）で、macOS/CoreAudio には対応する必要がないため、`_settle_reopen_supported()`
（`sys.platform == "linux"` を返す）を新設し、`_open()` のタイマー起動を
`if schedule_settle_reopen and _settle_reopen_supported():` にガードした。
テスト（`TestSettleReopen`）は reopen 挙動を検証する3ケースで
`_settle_reopen_supported` を `True` に monkeypatch し全 CI プラットフォームで
同じ結果になるようにし、非対応プラットフォームで reopen が仕込まれないことを
確認する `test_no_reopen_when_platform_unsupported` を追加した。

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
