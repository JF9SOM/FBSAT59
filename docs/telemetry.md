# Telemetry タブ詳細設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> Telemetry タブ（`src/ui/telemetry_tab.py`）・`src/comms/telemetry/` を変更・調査する前に
> 必ず読むこと。衛星選択コンボの構築方式・自動トランスポンダー選択のスコアリング・
> SATNOGS `status` の意味論について、2026-09-05 に実機報告を受けて調査・修正した内容を
> まとめてある。常時読み込む必要はない。

---

## 全体構成

Telemetry タブは AX.25 テレメトリーフレームを2つの経路で受信・デコードする:

- **Direwolf (AX.25) モード**（`_MODE_AFSK`）— 1200 baud Bell 202 AFSK / 4800・9600 baud
  G3RUH スクランブルド FSK・GMSK。Rig + Sound Card・SDR いずれの経路でも、実際のデコードは
  常にDirewolf本家が行う（2026-09-13以降。SDR経路はafsk_audio_demod.py/g3ruh_demod.pyが
  デコード用の音声を生成してDirewolfのstdinへ渡す。詳細は[docs/communications.md](communications.md)）
- **gr-satellites モード**（`_MODE_GR`）— gr_satellites サブプロセット経由。SDR 専用
  （Rig + Sound Card 非対応。詳細は「i18n: gr-satellites アイドル文言」節参照）

いずれのモードも、モード切替コンボの隣に衛星選択コンボ（`_combo_afsk_sat` /
`_combo_gr_sat`）を持ち、選択すると `satellite_selected(norad, mode_str)` シグナルが
`main_window.py` の `_on_telemetry_satellite_requested()`（[main_window.py:2781](../src/ui/main_window.py:2781)）
に届き、衛星リスト選択・Radio Control のトランスポンダー自動選択が連動する。

デコードされたフレームは `telemetry_log` テーブルに永続化され、SatNOGS DB へのアップロード
（`get_satnogs_uploader().submit()`）も行われる（Phase 2: gr-satellites の
`--kiss_server` 生フレーム経路にも対応済み）。

---

## 衛星選択コンボの構築方式（2026-09-05 に大幅変更）

### 変更の経緯

従来（〜2026-09-05）、`_populate_afsk_combo()` は2つのソースを無条件マージしていた:

1. `src/data/telemetry_formats/*.json`（手書きのフィールドレベルデコード定義、10件）を
   **DBに何も無くても無条件で**コンボに載せる
2. `mode_detection.get_norads_for_tab(conn, "telemetry")` が拾う、DB (`transmitters`)
   由来の AX.25 対応衛星

このうち①が原因で、**DBに `satellites`/`transmitters` 行が一切無い衛星**（例:
GOLF-TEE / AO-109、NORAD 47783）がコンボに表示されてしまい、選択しても
`_select_satellite_by_norad()` が衛星リストで見つけられず（サイレント no-op）、
`_refresh_radio_control()` もトランスミッターを0件返して `_on_telemetry_satellite_requested()`
が即 return する、という「選んでも何も起きない」ゴーストエントリ問題が発生していた。

実際に検証したところ、①のうちフィールド定義がある6衛星（40908 LilacSat-2 / 42017
Nayif-1 / 42829 Uguisu / 43786 ITASAT-1 / 47311 Maya-2 / 47783 GOLF-TEE）のうち、
②の条件（DBに生存中のAX.25対応トランスミッターがある）を満たすのは **LilacSat-2 (40908)
1件のみ**だった。この数字を踏まえ、①を完全に廃止し②のみに統一する方針にした。

### 現在の設計

- **Direwolf側** `_populate_afsk_combo()`（[telemetry_tab.py:435](../src/ui/telemetry_tab.py:435)）
  — `mode_detection.get_norads_for_tab(conn, "telemetry")` の結果のみを使う。
  `get_norads_for_tab()`（[mode_detection.py:182](../src/comms/mode_detection.py:182)）は
  `transmitters JOIN satellites WHERE t.alive=1 AND s.is_hidden=0` を
  `is_ax25_telemetry_transmitter()`（[mode_detection.py:77](../src/comms/mode_detection.py:77)）
  でフィルタする。このマッチャーは **`mode=="AFSK"` は無条件true**、**baud 4800/9600
  はdescriptionに"AX.25"/"AX25"を含む場合のみtrue**（既知の制約: Uguisu(BIRDS-1,
  42829)の生存中GMSK4800トランスミッター「TLM GMSK」はdescriptionに"AX.25"表記が
  無いため対象外になっている）。
- **gr-satellites側** `_populate_gr_combo()`（[telemetry_tab.py:532](../src/ui/telemetry_tab.py:532)）
  — gr-satellitesのsatyamlカタログ（`self._gr_sat_list`、後述、現状409衛星）を、
  `_hidden_norads()`（is_hidden除外）と `_norads_with_live_transmitter()`
  （[telemetry_tab.py:503](../src/ui/telemetry_tab.py:503)、DBに`alive=1`のtransmitterが
  1件でもあるか）の両方でフィルタする。同じ「ゴーストエントリ」問題（TEVEL2-1〜9等
  16衛星がDB行自体無し、STRAND-1等5衛星がDB行はあるが生存中トランスミッター0件）が
  gr-satellites側にも実在したため、Direwolf側と同日に同型の修正を行った。

### `telemetry_formats/*.json` の現在の役割（コンボ population からは完全に切り離し済み）

ファイル自体・以下の用途は削除していない:

- `decode_telemetry()`（[decoder.py:165](../src/comms/telemetry/decoder.py:165)）—
  受信フレームのフィールドレベルデコード。`load_format(norad)` が該当JSONを見つければ
  named fields、無ければ raw hex にフォールバック
- `_callsign_to_norad()`（[telemetry_tab.py:889](../src/ui/telemetry_tab.py:889)）—
  受信フレームのコールサインからNORADを逆引き（`list_formats()`を全走査）。DB非依存
  なので、コンボに出てこない衛星（例: GOLF-TEE）からのフレームでも正しく識別できる
- `_on_telemetry_satellite_requested()`（後述）— 宣言済み`modulation`をスコアリングの
  最優先ヒントとして使用

**JSON自体の信頼性に注意**: `40908.json`等、フィールド定義がある6件には
`"note": "Field definitions are based on community documentation and have not been
verified against actual received packets. Offsets and scales may require adjustment."`
という注記が付いている。2026-06-12〜13にAIセッションが「コミュニティ文書を参考に」
一括作成した未検証の推測値であり、JO-97(43803)・GhanaSat-1(42830)・DHABISAT(44829)・
ISS(25544)は既にfields空に修正済み（バイナリ形式未確認/CWのみ等の理由）。

---

## Direwolf(AFSK)モードのトランスポンダー自動選択スコアリング

`_on_telemetry_satellite_requested()`（[main_window.py:2781](../src/ui/main_window.py:2781)）の
`mode == "afsk"` 分岐。数字が小さいほど優先（`best_score`初期値999、`downlink_low`を
持たない候補=アップリンク専用は無条件でスコア計算前に `continue` で除外——2026-09-05、
CHUBUSAT-3の「Message Exchange Service Uplink」(41339, mode=AFSK, downlink_low=None)
が実際に受信可能な「9k6 GMSK TLM」を差し置いて選ばれていた事例で発覚・修正）:

| score | 条件 |
|---|---|
| 0 | `telemetry_formats/{norad}.json` の `modulation` 先頭アルファベット部分（例:"AFSK1200"→"AFSK"）と `mode` が一致 |
| 1 | descriptionに "AX.25" または "APRS" を含む（かつ非アナログモード） |
| 2 | `mode=="AFSK"` |
| 3 | descriptionに "TLM" または "TELEMETRY" を含む（かつ非アナログモード） |
| 4 | その他の非アナログモード（GMSK/FSK/BPSK等、名前は付くが上記に当たらないもの） |
| 5 | モード不明（空文字列） |
| 6 | アナログ/CWモード（`_TELEMETRY_ANALOG_MODES`、最終手段） |

`_TELEMETRY_ANALOG_MODES`（[main_window.py:128](../src/ui/main_window.py:128)）=
`{FM, FMN, AM, CW, CW-R, SSB, USB, LSB, SSTV, DVB-S, DVB-S2, ATV, APT, LRPT, HRPT}`。
score 1/3 の判定でこの集合を除外しているのは、descriptionに"TLM"等の文字列があっても
実際はアナログ信号（Direwolfでは絶対に復調不可能）という実例が複数あったため
（ISSの166MHz「Soyuz-TM and Progress M-1 TLM」はmode=FMのアナログ信号なのに
"TLM"の文字列一致だけでscore 0になっていた——修正前は最優先で選ばれていた）。

gr-satellitesモード（`mode != "afsk"`）は別ロジック: description に TLM/Telemetry を
含むものを最優先、無ければ `gr_satellites_backend.get_satellite_info(norad)` の
YAML定義に載っている周波数リストと `downlink_low` が最も近いものを選ぶ。

---

## SATNOGS `alive` / `satnogs_status` の意味論

`transmitters.alive`（`status=='active'`のブール値）と `transmitters.satnogs_status`
（生の SATNOGS `status` 文字列: active/inactive/invalid、manual/community 由来は NULL）
の設計判断は [docs/tle.md](tle.md) の「SATNOGSトランスミッター status の全件取得」節に
詳しいが、要点:

- **`status` は自動計測ではなく、コミュニティのレビュアーが手動でキュレーションする値**
  （`reviewed`/`approved`/`reviewer` フィールドを伴う）
- 大半の画面（`get_transmitters()`のデフォルト・Edit Transmitter・Autotrack・Comms
  Quick Panel・**Telemetryタブ**）は `alive=1` のみを表示する
- **Radio Controlタブのトランスポンダーコンボだけが例外**で、`satnogs_status` を使って
  inactive/invalidも表示する（色分け付き）。Telemetryタブは現状この例外に含めていない

### 実例: Ten-Koh 2 の AFSK1k2 が「消えた」理由（2026-09-05 に直接確認・裏取り済み）

Ten-Koh 2（NORAD 68261）は SATNOGS DB API 上で AFSK1k2/GMSK4k8/GMSK9k6/FM Digitalker
の4トランスミッターが同一タイムスタンプ（2026-03-13）で `inactive` になっている。
citation を実際にたどると（`https://community.libre.space/t/ten-koh-2-deploy-from-htv-x1/13861/36`）、
**運用元（奥山研究室、Nihon University）自身がX/旧Twitterで「Currently, only the CW
signal on 435.860 MHz is active.」と公式発表し、それを受けてSATNOGSコミュニティの
レビュアーが該当エントリを手動で `inactive` に更新した**、という経緯が確認できた。
つまりこれは**運用元確認済みの正確な情報**であり、`alive=0` によってTelemetryタブの
コンボから外れているのは意図通りの正しい挙動。

なお [docs/tle.md](tle.md) には「Ten-Koh 2はSATNOGSのレビュー漏れで実際は動いている
のにinactiveのままになっていた実例」という趣旨の記述があるが、今回の直接確認では
裏付けが取れなかった（現状のデータでは逆に「正確なinactive」だった）。当時どの時点
のデータを見てそう判断したかは不明。tle.md側の記述の要修正の可能性があるが、
未対応（2026-09-05時点）。

---

## gr-satellitesの衛星カタログソース

`list_gr_satellites_with_names()`（[gr_satellites_backend.py:117](../src/comms/telemetry/gr_satellites_backend.py:117)）
が `_satyaml_dir()` 配下の `*.yml` を全件読み込む。参照先は2通り
（[gr_satellites_install.py:114](../src/comms/telemetry/gr_satellites_install.py:114) `bundled_satyaml_dir()`）:

1. **バンドル版**（優先） — Help経由でインストールする、CIがconda-packで固めた
   `gnuradio-satellites` 環境内の `satellites/satyaml/*.yml`
2. **システム版** — apt等で別途入れたgr_satellitesのsatyamlディレクトリ

これは **FBSAT59自身のDBともSATNOGSとも完全に独立した、gr-satellitesという上流
プロジェクト本体が独自にメンテナンスしているカタログ**（現状409衛星）。周波数・
プロトコル定義もすべてgr-satellites側のYAMLに従う。ISSのようにgr-satellites側が
そもそも定義を持たない衛星は、DB側にどれだけ有効なデータがあってもコンボには出て
こない（ゴーストエントリ問題とは無関係な、単純な「カタログに無い」ケース）。

### 仮 NORAD ID のまま載っている衛星の名前照合（2026-09-20）

gr-satellites のカタログは、打ち上げ直後に SATNOGS の**仮 NORAD ID（90000 番台）で
登録した衛星をその ID のまま放置する**ことがある。FBSAT59 側は正式 ID へ移行済みなので
両者が一致せず、「DB に生存中トランスミッターがある」フィルタで落ちてコンボに出なかった
（例: Foresail-1p は yml が 98467、DB は 66778。実 IQ 録音の解析で発覚）。

`_populate_gr_combo()` は、カタログ NORAD が DB の生存中トランスミッター集合に無く、かつ
**カタログ NORAD が 90000 以上**の場合に限り、衛星名の正規化一致（小文字化・英数字以外除去）で
DB の生存中・非表示でない衛星を探す（`gr_satellites_backend.map_provisional_to_tracked()`）。

- 実 ID（< 90000）のエントリは名前が同じでも結びつけない（IRIS: yml 57315 と DB 39197 は
  別の衛星。誤結合防止）。DB 側候補が 0 件または複数件なら結びつけない
- コンボの `userData` は **FBSAT59 の正式 ID**。衛星リスト連動・Radio Control・
  SatNOGS アップロードの帰属・`set_satellite()` の自動選択は全てこの ID で動く
- gr_satellites 起動時だけカタログ側の ID（仮 ID）を渡す。対応は
  `TelemetryTab._gr_catalog_ids`（正式 ID → カタログ ID）、`GrSatellitesBackend.start()` の
  `catalog_norad` 引数。`started_norad`（フレームの帰属）は正式 ID のまま
- 非表示判定は**結びつけた後の ID**に対して行う。移行済み衛星は DB に古い仮 ID 行が
  `is_hidden=2` で残るため、先にカタログ ID で判定すると正常な衛星まで落ちる
- 2026-09-20 時点の実 DB では 21 衛星が該当（Foresail-1p、TEVEL2-1〜9、AEPEX、HUNITY、
  INHA-RoSAT、JINJUSAT-1B、HCT-SAT2、JACK-001/003、K-HERO、PHI-1、SNUGLITE-III DURI、
  SPIRONE）。下の「ゴーストエントリ」表の TEVEL2 は、DB 行が無かったのではなく、この
  仮 ID 不一致だった可能性が高い

**既知の未対応**: `main_window.py` の gr-satellites 用トランスポンダー自動選択
（`get_satellite_info(norad)`、description に TLM/Telemetry が無い場合の周波数近接
フォールバック）はカタログ ID で引くため、上記の衛星ではこのフォールバックだけ効かない
（TLM を含む description があれば影響なし）。

**Foresail-1p の注意**: フレーミングは GomSpace AX100（ASM）で AX.25 G3RUH ではないため、
Direwolf 経路では S/N が十分でも復調できない。gr-satellites 経路のみ有効。

### カタログに無い衛星用の同梱 satyaml（2026-10-19、Lobachevsky）

gr-satellites 本体のカタログに無い衛星は、`src/data/gr_satellites_extra/*.yml`（PyInstaller では
`data/gr_satellites_extra/`、`scripts/fbsat59.spec` の `datas`）に自前の satyaml を置く。
`gr_satellites_backend._catalog_entries()` がカタログ本体とこのディレクトリを合わせて走査し、
衛星コンボ・`get_satellite_info()` に反映する。**同じ NORAD がカタログ本体にあれば本体を優先**
（上流が追加した後は上流の定義に自動で切り替わる）。起動時は、同梱分に限り NORAD ではなく
yml のパスを `gr_satellites` に渡す（`extra_satyaml_path()`。gr_satellites は衛星引数にパスを受け付ける）。
gr-satellites 自体が未インストールなら従来どおり何も出ない。

- **Lobachevsky（RS83S、NORAD 67284、Geoscan 16U）**: 9k6 FSK、フレーミングは gr-satellites の
  `GEOSCAN`（CC1125、同期語 `0x930B51DE`、PN9、CC11xx CRC16）だが、**`frame size` は 74**
  （GEOSCAN-EDELVEIS の 66 では 1 フレームも出ない）。2026-10-19 の実 IQ 録音（250 kHz、約 11 分）の
  404〜405 秒のバーストで同期語が 727 ビット間隔で並び、CRC 合格を確認。gr_satellites 実行で
  400〜410 秒の切り出しから 17 PDU を復号。ペイロード先頭は `12 98 46 35 bd 46 xx`（末尾の `xx` が連番）。
  **72 バイトの中身（センサー値）は未解読**で、受信・表示のみ。
- 同梱 yml が実在の他の Geoscan 衛星（Geoscan-1〜6 等）にも 74 バイトで当てはまるかは未確認。
- 0 kHz 付近のバースト（約 283 秒・404〜405 秒）が対象。−70 kHz 付近の別信号は未調査。
- テスト: `tests/test_gr_satellites_backend.py` の `TestExtraSatyaml`（同梱 yml の定義、パス渡し、
  カタログ優先）。実フレームでのデコード自体は手元の gr_satellites での手動確認のみ。

### gr-satellites モードの「Fast」デコード（2026-10-19）

HADES FSK と同じ「速度: 通常／高速」プルダウンと「📂 Decode IQ Recording…」が gr-satellites モードでも出る
（選択は同じ `telemetry_decode_speed`）。「高速」はファイルを選ぶと `GrFileDecoder`
（`src/comms/telemetry/gr_satellites_file.py`）が `gr_satellites <衛星> --wavfile <ファイル> --iq` を
サブプロセスで走らせ、SDR パイプラインを通さず録音全体を一度に処理する。実録音（671 秒・1.3 GB、
Lobachevsky）が **約 26 秒**（実時間再生の「通常」は録音の長さそのもの）。■ Stop・モード切替で中止（プロセスを kill）。

- **フレームの時刻は推定値**: gr_satellites は復号したフレームに録音上の位置を出さない。ファイルは一定の速さで
  読まれるので、**フレームの文字列が出力に現れた時刻に比例**すると見なす
  （`estimate_frame_seconds()`）。起動・終了のオーバーヘッドは、先に録音冒頭 1 秒だけで gr_satellites を走らせて
  測り（`_measure_overhead()`）、起動と終了で半々とする。実録音で確認: 実際のバースト 235／283／404〜405 秒に
  対し推定 235.8／283.8／406 前後（誤差は数秒。起動時間を丸ごと差し引くと 10〜20 秒早く出た）。
  フレームは**ファイル名に開始時刻があれば `reliable=True` で SatNOGS へも自動投稿**する
  （フッターの「SatNOGS Upload」が ON のとき。時刻は数秒ずれうるが、ユーザー判断で投稿する。2026-10-19）。
  ファイル名に開始時刻が無ければ表示・保存のみで投稿しない（ステータスに明記）。
  投稿するフレーム本体は `gr_satellites --kiss_out <一時ファイル>` で書かせ、終了後に読み出して
  文字列の塊と**同じ順序・同数のときだけ**対応付ける（`GrFileDecoder._read_kiss()`。`--kiss_server` に
  対応しない古いビルドでは本体なし＝投稿なし）。実録音で 25 フレームすべてが 72 バイトの本体と対応。
  出力のタイミングが処理速度に依存するため、フレームは全部終わってから推定時刻つきでまとめて表に出る
  （実行中は件数だけ表示）。
- **重複**: 推定時刻は実行ごとに数秒ずれるので、同じ録音の再処理で `telemetry_log` が二重にならないよう、
  高速デコード中の gr 行の重複判定窓は 60 秒（`_GR_FAST_DUPLICATE_WINDOW_S`。通常は 5 秒。SatNOGS への二重投稿判定も同じ窓）。
- **録音の開始時刻**はファイル名から（無ければ現在時刻）。WAV は `comms/telemetry/iq_wav.py` で検証し、
  gr_satellites にはファイルをそのまま渡す（`fact` チャンクのある録音も正しく読めることを実録音で確認）。
- **PDU ダンプ形式のフレームを表に出すように修正（同日）**: gr-satellites は、形式を知らない衛星（例:
  Lobachevsky の `Telemetry: unknown`）のフレームを `-> Packet from …` ではなく
  `***** VERBOSE PDU DEBUG PRINT ******` から `************************************` までの 16 進ダンプで出す。
  従来のライブ側は `-> Packet from` を含む塊だけを行にしていたため、**この形式のフレームは表に一行も出なかった**。
  `starts_frame_block()` / `ends_frame_block()` / `FrameBlockCollector`（`gr_satellites_backend.py`）で
  どちらの形式も 1 フレームの塊として扱い、データ列には `parse_gr_pdu_dump()` で取り出したバイト列を HEX 表示する。
- テスト: `tests/test_gr_satellites_file.py`（疑似 gr_satellites で通し、時刻推定、WAV 書き出し）、
  `tests/test_gr_satellites_backend.py` の `TestFrameBlocks`、`tests/test_telemetry_tab.py` の gr 高速デコード系。
  **GUI（実アプリのファイル選択から表示まで）での確認は未実施**。

### Lobachevsky の「全項目デコード結果」（`frame_types` スキーマ、2026-10-19）

SatNOGS の `geoscan16u.ksy`（Geoscan 16U プラットフォーム）と、実録音の 25 フレームの並びが一致したので、
[`src/data/telemetry_formats/67284.json`](../src/data/telemetry_formats/67284.json) に全項目の定義を作った
（ksy を写さず、実フレームで確認した並びから自前で書いた）。フレームは 72 バイトで、5 種類:

| id | 判別（`match`） | 内容 |
|---|---|---|
| `GNSS-42` | offset 3 に `35bd42` | 衛星時刻(ms)、位置・速度(float, m・m/s)、姿勢クォータニオン、ADCS モード |
| `GNSS-43` | offset 3 に `35bd43` | 上記＋角速度（実フレーム未確認） |
| `GNSS-46` | offset 3 に `35bd46` | 記録番号＋時刻つきの位置・速度 2 サンプル（蓄積データの再送。約 23 分間隔） |
| `EPS` | offset 3 に `29bd` | バッテリー・電流・セル電圧・クーロンカウンタ・各種マスク・温度 |
| `BEACON` | offset 0 に `848a` | AX.25 形式の BEACON（EPS×2・ADCS・通信系、約 80 項目） |

先頭 3 バイトは `パケット ID(u16 LE) ＋ サイズ(u8)`、続く 2 バイトが system ID（`0xBD35` GNSS/ADCS、`0xBD29` EPS）。

**デコーダーの拡張**（`comms/telemetry/decoder.py`）: 新スキーマ `frame_types`（順に `match` を試し、最初に合った
定義の `fields` で読む。offset はペイロード先頭から）、型 `int32_le` / `uint64_le` / `unix_time_ms_le`。
gr-satellites モードの行は、`parse_gr_packet()` が空なら `parse_gr_pdu_dump()` の全フレームを渡す
（`_decode_gr_text()`）。行を選ぶと保存済みの全文から再デコードする（既存の仕組みのまま）。

**実フレームでの裏付け**: 衛星時刻 01:40:18.247 UTC が受信時刻（高速デコードの推定 01:40:19）と一致、位置の大きさ
約 6874 km、速度 約 7.6〜7.7 km/s、クォータニオンの大きさ 1.000、EPS のセル電圧 約 4.16 V×3（BEACON の
バッテリー電圧 12.478 V と整合）、システムマスク `0x883a` が EPS と BEACON で一致。
**単位が文書に無く推定のもの**: `GNSS-46` の位置は 0.1 m、速度は mm/s（大きさが軌道として合う）。EPS・BEACON の電流
(mA)・電圧(mV)も推定。ラベルに「(inferred unit)」と付けてある。温度 127 は「無効」と見られる（全チャンネルが 127 の
フレームがある）。**未対応**: 通信系ヘッダー（AX.25 の呼出符号。ksy は ror(1) で読むが本定義は SSID バイトまで）。
- **system `0xBD25`（定義なし）**: 2026-10-19 の録音に 1 フレームあり、ksy にも無い。構造は EPS（`0xBD29`）と同じに見え
  （温度が 28/26/26/24/n・a/34/36/36/35/7/8/24/27 ℃と妥当）、**もう一つの EPS ユニットの可能性**があるが、
  確証が無いので定義していない（生バイトのまま）。次の録音で出れば同じ並びで追加できる。
- テスト: `tests/test_telemetry_decoder.py`（実フレーム 4 種の値）、`tests/test_telemetry_tab.py`
  （PDU ダンプの行が「全項目デコード結果」を埋める）。**GUI（実アプリでの見え方）は未確認**。

---

## ゴーストエントリ問題（症状のパターンと発見済みの実例、2026-09-05）

**症状**: コンボで衛星を選択しても、衛星リストの選択もRadio Controlのトランスポンダー
リストも変わらない。

**根本メカニズム**: `_select_satellite_by_norad()`（[main_window.py:2508](../src/ui/main_window.py:2508)）
は該当衛星が衛星リストウィジェット（`satellites`テーブル由来）に無ければサイレントに
no-op。続く `_refresh_radio_control(norad)` 後の `if not transmitters: return`
（[main_window.py:2799](../src/ui/main_window.py:2799)付近）も、トランスミッターが0件なら
即終了。両方とも例外を投げずに黙って何もしないため、ユーザーからは「バグって
何も起きない」としか見えない。

発見・修正済みの実例:

| 衛星 | モード | 原因 |
|---|---|---|
| GOLF-TEE/AO-109 (47783) | Direwolf | telemetry_formats一括マージ（DB行なし）→①廃止で解決 |
| CHUBUSAT-3 (41339) | Direwolf | "Message Exchange Service Uplink"が`downlink_low=None`なのにmode=AFSKでスコア勝ち →downlink_low必須チェックで解決 |
| TEVEL2-1〜9 等16衛星 | gr-satellites | DB `satellites`行自体が無い →`_norads_with_live_transmitter()`で解決 |
| STRAND-1 等5衛星 | gr-satellites | `satellites`行はあるが生存中トランスミッター0件 →同上 |

---

## 定期的なDB追従の仕組み（新規スケジュールジョブは追加していない）

Telemetryタブは非常駐タブ（Communicationsメニューから開き、×で閉じる。開き直すたびに
`TelemetryTab.__init__()` が再実行される）。コンボは開いた時点のDB状態を都度クエリして
作り直すため、既存の**7日ごとの `satnogs_transmitter_refresh` ジョブ**（CLAUDE.md
「自動フェッチスケジュール」参照）がバックグラウンドでDBの `alive`/`satnogs_status` を
更新すれば、次回タブを開いたタイミングで自動的にコンボへ反映される。

**開きっぱなしのタブはライブ反映されない**（再ポーリングやシグナル接続は無い）。
同期完了後に反映させるには、タブを一度閉じて開き直す必要がある。

---

## i18n: gr-satellitesモードのアイドル文言（2026-09-05）

`_refresh_status()`（[telemetry_tab.py:954](../src/ui/telemetry_tab.py:954)）のアイドル時
（受信していない）メッセージは、モードによって文言が異なる:

- Direwolfモード: `"—  (connect Rig or SDR, then click ▶ Start)"` — Rig + Sound Card・
  SDRいずれの経路にも対応するため両方言及
- gr-satellitesモード: `"—  (connect SDR, then click ▶ Start)"` — **SDR専用**
  （Rig + Sound Card経路が無い）ため"Rig or"を含めない

日本語訳は `locale/ja/LC_MESSAGES/fbsat59.po`。更新手順は [docs/i18n.md](i18n.md) 参照。

---

## 「受信フレーム」テーブルの交互行コントラスト問題（2026-09-13 修正）

`setAlternatingRowColors(True)`（[telemetry_tab.py:286](../src/ui/telemetry_tab.py:286)付近）
自体は元から使われていたが、rawフレーム（未デコード）のデータ列にだけ
`data_item.setForeground(Qt.GlobalColor.gray)` という固定の中間グレーを前景色として
直接指定していた。この固定色が、OS/テーマ由来の交互行の明るい方の背景色や選択時の
ハイライト背景と組み合わさるとほぼ同系色になり、コントラストが失われて文字が読めなくなる
（黒背景の行では読めるが、明るい交互行・選択行では読めない）という実機報告があった。

**修正**: rawフレームは現在ほぼ全ての受信フレームで発生する通常のケースになっており、
グレー表示で特別視する意味が薄れていたため、この`setForeground()`呼び出し自体を削除した。
以後は時刻・コールサイン・衛星名と同じ、テーマ標準の文字色（3状態＝通常行・交互行・選択行
いずれでも自動的に十分なコントラストが確保される）で表示される。`Qt`（`PySide6.QtCore`）の
importもこの変更で不要になったため削除済み。

### 同じ問題が「棄却された CW 候補」行で再発（2026-09-24 修正）

CW TLM モードの「棄却された候補」行（`_append_row(dim=True)`）が、`setForeground(#888888)` という
**固定のグレー**で描かれていて、選択行のハイライト（グレー）や交互行の背景と同系色になり読めなく
なった（OrigamiSat-2 の実受信画面で発覚）。上記 2026-09-13 と同根の問題。

**修正**: 固定色をやめ、行に `_DIM_ROLE` を持たせて `_DimRowDelegate`（`QStyledItemDelegate`）が描く。
通常・交互行では**テーマの文字色と背景色を 6:4 で混ぜた色**（暗色・明色どちらのテーマでもコントラスト比
3:1 以上）、**選択行はテーマ標準の選択色**のまま。どの状態でも**斜体**で「棄却」を示す。
教訓: 前景色を固定値で指定しない（テーマ・選択・交互行の全てで読める保証がない）。テスト:
`tests/test_telemetry_tab.py`（暗色・明色テーマでのコントラスト、選択時、通常行が影響を受けないこと）。

### 交互行の背景色を全廃（2026-09-24、ユーザー指示）

`setAlternatingRowColors(True)` は、交互行の片方が必ず明るいグレーになり、白い文字が読みにくいという
指摘（OrigamiSat-2 の受信フレーム表）で、**アプリ内の全ての表・リストから削除した**（Telemetry の
受信フレーム表と全項目デコード表、APRS、FT4、Q65、衛星検索ダイアログ）。以後は**全行が同じ暗い背景
に白い文字**。選択行だけテーマ標準のハイライトになる。**新しい表・リストで交互行を有効にしないこと**:
`tests/test_no_alternating_row_colors.py` が `src/` を走査して `setAlternatingRowColors(True)` /
`alternate-background-color` を見つけると失敗する。上の 2026-09-13 の節の `setAlternatingRowColors`
への言及は、当時の状態の記録。

### 受信フレーム表の「データ」列は HEX（2026-09-24、ユーザー指示）

Direwolf（AX.25）モードの「データ」列は、それまで `TelemetryFrame.summary()`（JSON 定義の先頭 4 項目の
文字列。OrigamiSat-2 では「パケット長: 34.00 生成タイミング…」のような意味不明な表示）だったのを、
**フレーム全体を空白区切りの大文字 HEX**（`94 A6 62 B2 …`、SatNOGS Network の「Data」タブと同じ形式）
に変えた（`format_frame_hex()`）。他局のフレームと比較でき、バイト列をそのままコピーできる（画像
データの復元など）。長いフレームはセル内で折り返し、行が伸びて全体が読める（ツールチップにも全文）。
項目ごとの値は従来どおり「全項目デコード結果」タブ。`summary()` 自体は他で使うため残してある。
gr-satellites モードと CW TLM モードの「データ」列は変えていない（前者はデコード済み項目、後者は元々 HEX）。

**AX.25 として解釈できないフレームも表に出す**: 従来 `_on_ax25_frame` は `decode_ax25()` が None のフレーム
（ARICA-2 のように HDLC/CRC は AX.25 と同じで中身が AX.25 のアドレス部でないもの）を**黙って捨てて
いた**。今は `_on_non_ax25_frame()` が、選択中の衛星に紐付けて HEX で表示し、`telemetry_log` にも記録する
（「選択分を送信」が使える）。**SatNOGS へも AX.25 フレームと同じく自動アップロードする**（2026-10-14 変更。当初は形式不明のため送らなかったが、CRC 検証済みで SatNOGS は生バイトを受けるだけのため、ユーザー判断で自動送信に変更。ARICA-2 の 16:00 パスが一切アップロードされなかったのが契機）。SSDV タブは
エンジンの `raw_frame_received` から全フレームを受け取るので、これらも届く。

---

## テレメトリーIDごとに異なるフォーマットを持つ衛星への対応（2026-09-13 追加）

### 背景

OrigamiSat-2（NORAD 68795）のテレメトリーを実際にSDRで受信し、「受信フレーム」テーブルの
`[raw] <hex先頭40文字>` 表示だけでは中身が読めないという相談から、東京科学大学が公開して
いる公式仕様書「OrigamiSat-2 FMダウンリンク通信データフォーマット」（文書番号
ORI-2-0027-OPR, ver.2026-04-21。[受信報告ページ](http://www.origami.titech.ac.jp/archives/1958)
で公開）を入手し全項目デコードを試みたところ、**1機の衛星が複数の全く異なるテレメトリ構造
（ID01=MOBC HK, ID65=RasPi/カメラ制御 HK, ID100=ADCS基板 HK簡易, ID130=ADCS基板 HK詳細）
を切り替えて送信する**ことが判明した。従来の `telemetry_formats/{norad}.json` は
NORAD単位でフラットな `fields` 配列を1つだけ持つ設計（LilacSat-2等6衛星が使用中）で、
この種の複数テレメトリ構造には対応できないため、スキーマを拡張した。

### `telemetry_ids` スキーマ（後方互換）

`load_format(norad)` が返すJSONに、従来の `fields`（フラット・単一構造）に加えて、
**テレメトリIDをキーにした `telemetry_ids` マッピング**を新たに置けるようにした:

```json
{
  "norad": 68795,
  "name": "OrigamiSat-2",
  "callsign": "JS1YRU",
  "telemetry_ids": {
    "65":  { "label": "ID65 (RasPi/カメラ制御 HK)", "fields": [...] },
    "100": { "label": "ID100 (ADCS基板 HK)",        "fields": [...] },
    "130": { "label": "ID130 (ADCS基板 HK詳細)",     "fields": [...] }
  }
}
```

`decode_telemetry()`（[decoder.py:206](../src/comms/telemetry/decoder.py:206)）は、
フォーマットに `telemetry_ids` があれば **payload[2]（この通信プロトコルの共通ヘッダーに
おける「テレメトリID」バイト）** を読み、対応するIDの `fields` でデコードする。
`telemetry_ids` が無い（＝従来のフラット`fields`のみ、またはフォーマット自体が無い）場合は
今まで通りの単一 `fields` デコード経路にフォールバックする——**既存6衛星（LilacSat-2等）の
JSONは無変更で動作継続**。`get_telemetry_id_defs(norad)`（[decoder.py:191](../src/comms/telemetry/decoder.py:191)）
は `telemetry_ids` の有無だけを判定するヘルパーで、UI側が「このタブを選択可能にしてよいか」
を実際のフレーム受信前に判断するのに使う（後述）。

新たに `float64_be`（`>d`、8バイトdouble）を `_STRUCT_MAP` に追加した。ID130の
ADCS内部時刻（ユリウス日）・衛星位置速度（ECEF、m/m/s）がdouble型のため必要になった。

### `TelemetryField.is_integer`（2026-09-13 追加、表示フォーマット用）

`_decode_field()`は各フィールドについて `is_integer = (ftype がfloat32_be/float64_be以外) and (scale == 1.0)`
を計算して返すようになった。整数型（uint8/int8/uint16/uint32等）かつスケール未適用のフィールド
（パケット長・各種ステータスバイト・カウンタ等）は本来ちょうど整数値になるため、UI側
（[telemetry_tab.py:1046](../src/ui/telemetry_tab.py:1046)）がこれを見て「204」のように整数表示
し、`204.0000`のような不要な小数点以下ゼロを出さないようにする。スケールが適用されている
フィールド（例: SAP電流 = 11.764 × DATA）やfloat32/float64型のフィールドは対象外で、従来通り
値の大きさに応じた小数フォーマット（1000以上はカンマ区切り小数2桁、1以上は小数4桁、それ未満は
小数6桁）を使う。

### 「全項目デコード結果」タブ（入れ子タブ構造）

「受信フレーム」表示領域を`QTabWidget`（`self._log_tabs`、[telemetry_tab.py:286](../src/ui/telemetry_tab.py:286)）
で2枚のタブに分割した:

```
テレメトリータブ
└─ [受信フレーム] [全項目デコード結果]        ← 外側タブ（self._log_tabs）
                    │
                    └─ [ID65] [ID100] [ID130]  ← 内側タブ（self._decode_id_tabs）
                         └─ 「項目」/「値」の2列テーブル
```

- **「受信フレーム」タブ**: 従来の4列テーブル（`self._table`）をそのまま`self._raw_page`
  に載せただけで、挙動・見た目は無変更
- **「全項目デコード結果」タブ**: `_rebuild_decode_tabs(norad)`（[telemetry_tab.py:983](../src/ui/telemetry_tab.py:983)）
  が、`set_satellite()`（main_windowから衛星リスト選択時に呼ばれる公開API）経由で衛星が
  切り替わるたびに、`get_telemetry_id_defs(norad)` の結果から内側タブを作り直す。
  **フレームを一度も受信していない時点でも**、JSON定義から項目名（ラベル）だけを先に
  並べたテーブルを構築し、値欄は「—」で初期化する。`telemetry_ids`を持たない衛星
  （LilacSat-2等、旧フラット`fields`形式のみの衛星や、フォーマット自体が無い衛星）では
  外側タブの「全項目デコード結果」自体を`setTabEnabled(False)`でグレーアウトする
- **値の更新**: `_update_decode_tab(tf)`（[telemetry_tab.py:1026](../src/ui/telemetry_tab.py:1026)）
  が`_on_ax25_frame()`から毎フレーム呼ばれ、`tf.telemetry_id`に対応する内側タブの該当行
  だけを最新値に上書きする（テーブルを作り直さない、ライブ更新）。`tf.norad`が
  `_rebuild_decode_tabs()`時点の衛星と一致しない場合（例: Favoriteグループ表示中に
  別衛星のフレームが紛れ込んだ場合）は無視する
- **gr-satellitesモードでもサブタブは出す（2026-10-12 変更）**: 以前は gr モードでサブタブ非表示・
  表の下に全文ペイン（`_gr_detail`）だったが、UmKA-1 の gr-satellites 受信成功を機に AX.25 と同じ
  2 サブタブにした。「受信フレーム」の表は `時刻 / コールサイン(送信元, 例 `RS40S-1`) / 衛星 / データ`
  で、データ列は AX.25 モードと同じく info フィールドの HEX（`parse_gr_packet()` が gr-satellites の
  `callsign = u'..'` / `ssid = N` / `info = b'..'` の出力を解析）。従来はコールサイン列に
  `-> Packet from` の後ろ（送信機名 `9k6 FSK USP downlink`）が入り、データ列は 100 行超を `|` で
  つないだ文字列で、行が数千 px になり他列が画面外へ出ていた（空欄に見えた原因）。
  「全項目デコード結果」は、衛星に `telemetry_formats/{norad}.json` があればその内部タブ
  （下記 `records` スキーマ等）、**最後の内部タブ「gr-satellites 出力」は常に**（選択行の gr-satellites
  全文。定義の無い衛星ではこれだけ）。`_rebuild_decode_tabs()` はモード切替でも呼び直す。

### UmKA-1（NORAD 57172）と `records` スキーマ（2026-10-12）

UmKA-1（RS-40S）は Sputnix Unified Protocol（9k6 FSK USP、gr-satellites の送信機
`9k6 FSK USP downlink`）。SatNOGS DB のデコーダーは `sputnixusp`（[sputnixusp.ksy](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders/-/raw/master/ksy/sputnixusp.ksy)）。
AX.25 の info フィールドは `u16 type, u16, u16, u16 len`（リトルエンディアン）＋本体 `len` バイトの
**レコードの繰り返し**で、1 フレームに複数レコードが入る。この形のため新スキーマ `records`
（`decoder._decode_records()`）を追加した: `{"types": {"4246": {"id", "label", "fields"}}}`、
`fields` のオフセットは**レコード本体先頭基準**。1 フレームで複数の内部タブが同時に更新される
（`TelemetryFrame.record_ids`、フィールド名はレコードをまたいで一意にしておく）。フィールド型に
`unix_time_le`、整数型のビット抽出 `mask` を追加。

- **定義済みは実受信で確認した 2 種のみ**: `0x4246` UHF ビーコン（25 B）、`0xDE21` PS 定期テレメトリ
  （46 B）。ksy にはさらに `0x4216`（general）・`0xED21`（5ch）・`0xDF25`（6U）があるが未実装
  （実フレーム未確認。必要なら同じ要領で `57172.json` へ追加）。
- **実データでの裏付け**: 2026-10-12 のパス（IQ 録音 11:57:38Z 開始）の 6 フレームで、UHF 時刻
  `uhf_time` が 12:01:38〜12:04:08Z（バースト 220〜370 秒と一致）、`uhf_rxbitrate`=9600、
  UHF 電圧と電池電圧が約 7.97〜7.98 V で一致、PS テレメトリ番号と UHF 稼働時間がともに約 1 秒刻み。
- **未確認・要注意**: `ps_t1..4`（電池温度）は ksy では ℃ だが生値が 506〜575 で単位が不明なため
  生値表示。`uhf_rss_rx` は常に 255（-255 dBm と表示。受信なしの値と**推測**）。`ps_time` は
  2018 年の UNIX 時刻になり PS の時計が未同期とみられる（生値の秒数のまま表示）。各 `flags` は
  ビット配置の確認が取れていないため生値。
- **IQ 録音の解析**: 録音 682 秒に 30 秒周期のバーストが 220〜400 秒の 7 発（0 Hz 付近）。gr-satellites
  は 6 フレーム復号。最後（400 秒、約 7.7 dB）が未復号なのかは未確認。

### rawプレビューの切り詰め表示（2026-09-13 追加）

`summary()`（[decoder.py:96](../src/comms/telemetry/decoder.py:96)）の`[raw]`分岐
（フォーマット未対応、またはそのIDが`telemetry_ids`に定義されていない場合）は元々
`raw_hex[:40]`（16進数40文字＝20バイト）だけを返しており、**207バイトのフレームでも
20バイトの短いフレームでも同じ見た目**になり、「切り詰められている」ことが画面から
分からないという問題があった。実際に受信したフレーム全体は`raw_hex`プロパティ自体には
保持されており、`telemetry_log`テーブルにも全バイトが保存されている（表示だけが省略）。

切り詰めが実際に発生した場合（`raw_hex`の長さが40文字を超える場合）のみ、末尾に
`(40/207 hex chars — rest omitted)`（日本語: `（40/207文字を表示、以下省略）`）を追加する
ようにした。「207」はハードコードではなく、その受信フレーム自身の`len(raw_hex)`から
毎回動的に計算される（衛星・テレメトリIDによってフレーム長は異なるため）。

### OrigamiSat-2（NORAD 68795）フォーマットファイル

[`src/data/telemetry_formats/68795.json`](../src/data/telemetry_formats/68795.json)
を新規作成。ORI-2-0027-OPR仕様書に基づき **ID65・ID100・ID130を実装**。ID130は
実受信フレーム（2026-09-13、コールサインJS1YRU、SDR受信）で以下の物理的妥当性を確認済み:

- 姿勢クォータニオン(x,y,z,w)の大きさが≈1.000（単位クォータニオンとして正しい）
- 衛星位置ベクトルの大きさが≈6900km（地球半径6371km＋打上げ資料記載の軌道高度540kmと一致）
- 衛星速度ベクトルの大きさが≈7.6km/s（LEOの周回速度として妥当）

ID65・ID100は仕様書通りに実装したが、実フレームでの数値検証はまだ行っていない。

**ID01（MOBC HK）は意図的に未実装**: 仕様書のTable8（ID01データ部の内訳）に、
byte24が「RXマイコン再起動回数」（3.1.1.7項）と「各機器電源状態」（3.1.1.9項、byte24〜26）
の2箇所で重複して割り当てられており、かつbyte22がどの項目にも割り当てられていない、
という内部矛盾が仕様書自体にある（PDF原本を直接確認済み、こちらの読み取りミスではない）。
実受信フレームでの裏付けが取れる、または版元に確認が取れるまでは推測でオフセットを
決めず未実装のままにしている。

**LilacSat-2（NORAD 40908）は今回のスキーマ拡張の対象外**: 既存の`40908.json`は
フィールド定義が「コミュニティ文書を参考にした未検証の推測値」（ファイル内の`note`参照）
であり、実際に受信して数値を検証してから`telemetry_ids`形式への移行を検討する方針
（2026-09-13、ユーザー判断）。`get_telemetry_id_defs()`は`telemetry_ids`キーの有無だけを
見るため、この衛星は自動的に「全項目デコード結果」タブ非対応のまま（旧`fields`ベースの
一行サマリー表示は従来通り動作する）。

### 衛星内部時刻の日付表示と ID01 ヘッダー定義（2026-10-02）

`onboard_unix_time`（ヘッダー 4〜7 バイト目、u4be の UNIX 秒）は 16 進／整数のままでは読めないため、
フィールド型 `unix_time`（`decoder._decode_field`）を新設し `YYYY-MM-DD HH:MM:SS UTC` の文字列で
表示する（`is_string=True`、`raw_value` は元の秒数）。**これは衛星自身の時計であり受信時刻ではない**
（フレーム内に受信時刻の項目は無い。受信時刻は `telemetry_log.received_at`）。2026-10-02 のパスで
受信した ID01 フレーム 90 本のうち 89 本は 2026-07-28〜29 の時刻を持ち、今日の時刻（受信と数秒差）
を持つのは `gen_timing=0xFF` の 1 本だけだった。古い時刻のフレームは記録済みデータの再送と
**推測**されるが、ヘッダー以外の項目では裏付けていない。

ID01 は本体部の仕様矛盾（上記）が未解決のため、**ヘッダー部のみ**（時刻・送信数・パケット長・
生成タイミング・ID）を `telemetry_ids["1"]` に定義した。本体部は引き続き未実装。

**受信フレーム行の選択で全項目デコード結果を切り替える（2026-10-02、AX.25 のみ）**: 「全項目デコード結果」
は各 ID の**最新 1 本**しか保持せず、一覧の行を選んでも反応しなかったため、同じ ID の後続フレームが
来ると過去のフレーム（例: 今日の時刻を持つ 1 本）の内容が読めなかった。一覧の行選択
（`_on_table_selection_changed`）で、その行の AX.25 ペイロード（`_Ax25Row.payload`、無ければ
`raw` から `decode_ax25`）を再デコードして該当 ID のタブへ表示する。**CW TLM の行も対応済み**
（行が復号済みの `TelemetryFrame` を `_DECODED_ROLE` に持ち、保存済み行の再表示でも同じ）。灰色の
不採用候補は対象外。
**gr-satellites の行**は「Decoded Fields」タブ自体が無い（gr モードはタブバーを隠す）ため、
表の下の読み取り専用ペイン（`_gr_detail`、gr モードのみ表示）に、選んだ行の gr-satellites 出力の全文
（`Container:` 行を除きインデント保持）を表示する。全文は `telemetry_log.parsed_json` の `full` に保存し、
`full` を持たない旧い保存行は 1 行テキストを項目ごとに改行して表示する。実データでの見え方は未確認
（手元の DB に gr 行が無く、テストは合成テキスト）。

### テスト

[`tests/test_telemetry_decoder.py`](../tests/test_telemetry_decoder.py)に新規追加
（ネットワーク不要）。ID130実フレームのデコード結果の物理的妥当性チェック
（クォータニオン正規化・軌道半径）、`telemetry_ids`と旧`fields`形式の判別、
rawサマリーの切り詰め表示の有無、を検証する。

---

## ASCII/CSV方式の`csv_messages`スキーマ（2026-09-13 追加、Marina対応）

### 背景

Marina（NORAD 69920, コールサインOM9MAR, スロバキア, 9600bps AX.25 GFSK G3RUH,
VHF 145.925MHz / UHF 436.680MHz）のSatNOGSデコーダー（`satnogs-decoders`の
[`marina.ksy`](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders/-/blob/master/ksy/marina.ksy)、
"Based on the LASARSat satellite decoder"）を調査したところ、この衛星のテレメトリーは
OrigamiSat-2のようなバイナリ構造体ではなく、**AX.25のペイロードがそのままカンマ区切りの
ASCIIテキスト**（例: `"OBC,1234,987654,1700000000,...,WATCHDOG"`）であることが判明した。
先頭のタグ文字列（`MGS,` `OBC,` `PSU,` `SOL,` `CLS,` `LOD,` `U,` `V,`）でメッセージ種別を
判別し、以降はカンマ区切りの何番目かで各項目が決まる、という単純な設計。

これは`telemetry_ids`スキーマ（ヘッダーバイトの数値でID判別、バイトオフセットで
フィールド指定）の前提と根本的に異なるため、専用のスキーマを新設した。

（比較として調査した ARICA-2 は、CW側は`satnogs-decoders`の`arica2.ksy`が公開されている
もののビット単位パッキング（`type: b3`等）でバイト単位デコード不可、GMSK/AX.25側は
青山学院大学 坂本研究室が"the format of this telemetry data will not be disclosed"と
明言し意図的に非公開——という事情で対応を見送った。詳細はメモリ
`project_arica2_telemetry_undisclosed.md`参照）

### `csv_messages`スキーマ

`get_telemetry_id_defs()`（[decoder.py:191](../src/comms/telemetry/decoder.py:191)）は
`telemetry_ids`と`csv_messages`の両方をチェックし、どちらか存在する方を返す。UI
（`_rebuild_decode_tabs()`）側は返り値の構造（`{キー: {"label":..., "fields":[...]}}`）が
同じなので変更不要——ただしキーが数値文字列（"130"）とテキスト（"OBC"）の両方あり得るため、
ソート順を`(0, int(k)) if k.isdigit() else (1, k)`という複合キーに変更し、数値キーは数値順・
テキストキーはアルファベット順、が混在しても安全なようにした。

```json
{
  "csv_messages": {
    "OBC": {
      "label": "OBC（搭載コンピュータ）",
      "fields": [
        {"name": "obc_uptime", "index": 1, "type": "int", "label": "OBC稼働時間（今回起動から）", "unit": "s"},
        {"name": "obc_reset_cause", "index": 9, "type": "str", "label": "OBCリセット要因"}
      ]
    }
  }
}
```

`decode_telemetry()`（[decoder.py:206](../src/comms/telemetry/decoder.py:206)）は、
フォーマットに`csv_messages`があれば、payloadをASCIIテキストとしてデコードし、
`f"{key},"`で始まるかどうかで`csv_messages`の各キーと照合、一致したらカンマで
`split()`して各フィールドの`index`（0=先頭のタグ自身）で該当トークンを取り出す
（`_decode_csv_field()`, [decoder.py:139](../src/comms/telemetry/decoder.py:139)付近）。
`telemetry_id`（`TelemetryFrame`のフィールド）は数値だけでなく文字列も受け付けるよう
`int | str | None`に一般化した。

CSVフィールドの`type`は4種類:

| type | 内容 |
|---|---|
| `int` | 整数としてパース。`nan_sentinel`を指定すると、トークンが文字列`"nan"`の場合にその値を使う（Marinaの`SOL,`メッセージが欠測値を`"nan"`という文字列で送ってくるため） |
| `hex_int` | 16進文字列としてパース（例: PSUのチャンネル状態ビットマスク） |
| `bitflag` | `base`（既定16）でパースした値から`bit`ビット目だけを取り出す（0 or 1）。別フィールドの計算結果を参照するのではなく、同じトークンを毎回独立に再パースする設計にして、フィールド間の依存関係を持たせないようにした |
| `str` | 文字列そのまま（コールサイン等）。`TelemetryField.is_string=True`で表す |

`scale`・`add`（既存の`scale`に加えて、加算定数`add`を新設）で線形変換にも対応
（例: MarinaのRSSIは`raw/2 - 134` = dBm。marina.ksyに明記された式をそのまま反映）。

### `TelemetryField.is_string`（2026-09-13 追加）

文字列型フィールド（バイナリ`ascii`型・CSVの`str`型の両方）はこれまで
`unit`フィールドに文字列を詰める、というやや強引な流用（[decoder.py:149](../src/comms/telemetry/decoder.py:149)
付近の元々のコメント "not ideal but functional" 参照）で表現していたが、UI側が
数値と誤認して`0.000000 OM9MAR`のような表示をしないよう、`is_string: bool`を
明示的に追加した。`summary()`・`_update_decode_tab()`双方がこのフラグを見て、
文字列フィールドは`unit`の中身をそのまま表示するよう分岐する。

### フィールド値の確度について（Marina特有の注意）

`marina.ksy`自体に明示的な計算式があるもの（RSSIのdBm変換式、PSUチャンネル状態の
ビットフラグ分解、SOLセンサの`"nan"`欠測値センチネル）は確度が高いが、**温度・電流・
磁束密度・加速度・角速度等の大半のフィールドは`marina.ksy`自体がスケール変換式を
持たず、整数値をそのまま使うだけ**——つまり正しい物理単位への換算式は分からない。
[`69920.json`](../src/data/telemetry_formats/69920.json)ではこれらのラベルに
「(生値)」と付記し、単位は付与していない（LilacSat-2の`note`と同種の確度の
開示。実受信データで検証できるまでは推測でスケールを補わない方針）。

### テスト

`test_decode_marina_*`系のテストを[`tests/test_telemetry_decoder.py`](../tests/test_telemetry_decoder.py)
に追加（合成フレーム、実受信データではない）。OBC/PSU（16進ビットフラグ）/SOL（nan
センチネル）/U（RSSI線形変換）の各メッセージのデコード結果と、未知のタグに対する
rawフォールバックを検証する。

---

## バックエンド自身のコンソールログを見る「📋 Log」ボタン（2026-09-17 追加）

### 背景

OrigamiSat-2（68795）のAX.25 TLM受信試験で、ウォーターフォールには綺麗な信号が見えて
いたにもかかわらず「受信フレーム」に一度も何も出ない、という報告があった。調査した
ところ、SDR受信専用経路（`AudioBridge`、[docs/communications.md](communications.md)の
Direwolfセクション参照）では**Direwolf自身のstdout（起動バナー・警告・フレームごとの
`DECODED`行＋自前のaudio level評価）が誰にも読まれず捨てられていた**ことが判明した
（TX不可のSDR受信専用ブリッジではstdoutを読む理由が無い、という従来の設計上の正しい
判断が、結果的にDirewolf自身の診断情報も一緒に握りつぶしていた）。gr-satellitesバック
エンドも同様に、stderr（トレースバック・「そんな衛星は無い」等のエラー）を
`subprocess.DEVNULL`で完全に捨てていた。

### 実装

- `src/comms/aprs/direwolf_log.py` / `src/comms/telemetry/gr_satellites_log.py`
  （新規）— `sdr.diag_log`/`comms.ft4.decode_log`と同型の専用ロガー。それぞれ
  `direwolf.log` / `gr_satellites.log`（`fbsat59.log`とは別ファイル、同じログ
  ディレクトリ）に出力する
- `AudioBridge.run()`のSDR受信専用ブランチ（`direwolf.py`）: 何もしていなかった
  `self._stop_event.wait()`を、Direwolfのstdoutを1行ずつ読んで`direwolf.log`へ記録する
  ループに置き換えた。TXが発生しない経路なのでstdoutに生音声が混ざる心配はない
- `GrSatellitesBackend.start()`（`gr_satellites_backend.py`）: `stderr=subprocess.DEVNULL`
  → `subprocess.PIPE`にし、専用スレッド`_read_stderr()`で`[stderr]`タグ付きで
  `gr_satellites.log`へ記録。既存の`_read_stdout()`（UI表示用のフレームブロック
  パース）はそのまま維持しつつ、生の各行も同じログに記録するようにした
- Telemetryタブ: Baudコンボの右隣に「📋 Log」ボタンを追加（`_ProcessLogDialog`、
  非モーダルウィンドウ、🔄 Refreshで再読み込み）。クリック時に`_current_mode()`を見て
  Direwolf(AX.25)モードなら`direwolf.log`、gr-satellitesモードなら`gr_satellites.log`を
  開く。モードごとに別ウィンドウインスタンスをキャッシュするので、モード切替中でも
  取り違えない

Rig + Sound Card経由（TX可能なDirewolfセッション）のstdoutは実音声PCMと混在するため
対象外——SDR受信専用経路のみ。

### 🔄 Refreshは「消去」ではなく「再読み込み」——新規セッション開始時の自動クリアを追加（2026-09-17）

上記の実機検証（KNACKSAT-2、9600bps G3RUH、仰角22.8°ピークだが1フレームもデコード
できず）の直後、「🔄 Refreshを押してもログが消えない」という報告があった。原因は
実装通りの挙動——`_ProcessLogDialog.reload()`はファイルをそのまま読み直すだけで
消去はしない設計だった——だが、`direwolf.log`/`gr_satellites.log`の`FileHandler`は
プロセス生存中（さらにアプリ再起動をまたいでも）ずっと追記モード（`mode="a"`）で
開きっぱなしのため、**何セッション分も過去の内容が溜まり続け、「今回の試行で何が
起きたか」が読み取りにくい**という実害があった。ユーザーに確認したところ、
「新規受信セッション開始時に自動で空にする」方式を希望。

- `direwolf_log.py` / `gr_satellites_log.py`に`reset_direwolf_log()` /
  `reset_gr_satellites_log()`を追加。`FileHandler`を閉じて開き直すのではなく、
  既に開いている`handler.stream`を`seek(0)`＋`truncate(0)`でその場で空にする
  （読み手がファイル消失の瞬間を観測することがない）
- `DirewolfManager.start()`: `sdr_pipeline`が渡された（＝SDR受信セッション）場合のみ、
  プロセス起動前に`reset_direwolf_log()`を呼ぶ
- `GrSatellitesBackend.start()`: 常にSDR専用なので、`if self.is_running: self.stop()`の
  直後に無条件で`reset_gr_satellites_log()`を呼ぶ

これにより「📋 Log」を開いた時点のログは常にその回の受信試行のみを表す。既存の
🔄 Refreshボタンの挙動（消去ではなく再読み込み）自体は変更していない。

## 入力ソース行の「ⓘ」ボタン（2026-09-19 追加 → 2026-09-24 削除）

Input Source 枠の右端にあった `ⓘ` ボタンと「必要 SNR の目安」ウィンドウ（`_SnrGuideDialog`）は
**削除した**（ユーザー判断: 表の値は衛星系ではほぼ実現不可能で、目安として不適切）。
根拠となる合成信号での測定データは [communications.md](communications.md) の「デコードに必要な最低SNRの
目安」に残してある（ただし 2026-09-24 に 4800/9600bps へコヒーレント MSK 検波を追加したため、
その表の Direwolf の値は 4800/9600 の SDR 受信では最良値でなくなった）。`_SNR_GUIDE_ROWS`・
`_snr_guide_html()`・`tests/test_telemetry_snr_guide.py` と、対応する日本語訳の `.po` 項目も削除済み。

---

## 「CW TLM」モード — モールス符号の16進テレメトリの受信（2026-09-21 追加、ARICA-2 対応。2026-09-22 OrigamiSat-2 対応追加）

### 背景

ARICA-2（NORAD 68796）は AX.25/GMSK 側のテレメトリ形式を意図的に非公開にしている
（メモリ `project_arica2_telemetry_undisclosed.md`）が、**CW ビーコンのハウスキーピング
（HK）は公開されている**（SatNOGS `satnogs-decoders` の `arica2.ksy`＋坂本研究室の
[CW HK データページ](http://sakamotoagu.mydns.jp/ARICA-2/data/amateur/cw_hp_calender_srv.html)）。
2026-09-20 に録音した IQ（250 kHz、ARICA-2 のパス）を CW デコーダタブで再生して
`2FFE8594EB880124`（HK1）と `00D7C2A8D6B8EA`（HK3、先頭桁は誤読を補正）を読み、
坂本研の表と全項目照合できた（[docs/communications.md](communications.md)「CW Decoder — SDR入力を
専用CW復調器化」節）ことから、これを Telemetry タブの第3のモードとして取り込んだ。

### 全体の流れ

```
CW Decoder タブ（CwTab）— 確定した文字＋時刻を CwBlockExtractor へ
   └ frame_block_ready(text, start_utc, end_utc)   ← 3秒を超える無音で区切ったブロック
        └ TelemetryTab._on_cw_block()
             ├ comms.telemetry.cw_frames.decode_cw_frame()  ← ビット単位デコード＋妥当性検査
             ├ 「受信フレーム」表に行を追加（時刻 = ブロック先頭＝送信開始の UTC）
             ├ 「全項目デコード結果」（HK1/HK2/HK3 サブタブ）を更新
             └ telemetry_log に保存（received_at = 同じ UTC）
```

- **モード**: `_MODE_CW = "CW TLM"`（`Direwolf (AX.25)` / `gr-satellites` に続く3つ目）。選ぶと専用の
  衛星コンボ（`_combo_cw_sat`）と 🔍 が現れ、Baud・📋 Log は隠れる。
- **開始/停止**: ▶ Start で `cw_tlm_start_requested` を emit → `MainWindow._on_telemetry_cw_tlm_start()`
  が CW デコーダタブを開き（既に開いていればそれを使う）`TelemetryTab.attach_cw_tab()` で結び、
  `CwTab.start_decoding()` で開始する。**Telemetry タブは前面のまま**（CW タブは隣に開くだけ）。
  ■ Stop は `stop_decoding()`。CW タブが停止時に「作りかけのブロック」を最後に流す
  （`_stop_cw_tlm()` は停止要求の**後で** `_cw_tlm_norad` を消す。順序が逆だと最後のフレームを取りこぼす）。
- 衛星/送信機の選択は `satellite_selected(norad, "cw_tlm")` →
  `MainWindow._select_telemetry_satellite()`（旧 `_on_telemetry_satellite_requested()` の本体を改名）。
  `cw_tlm` は `mode_detection.is_cw_telemetry_transmitter` に合う送信機を Radio Control で選ぶ
  （無ければ任意の CW 送信機）。CW 送信機の選択は Radio Control が CW タブを自動オープンする
  （`cw_transponder_selected`）ので、`_on_telemetry_satellite_requested()`（ラッパー）が
  **Telemetry タブを前面に戻す**。

### 衛星・送信機の DB 検索（`comms/mode_detection.py`）

`is_cw_telemetry_transmitter()` = CW/CW-R モードで、**説明に `TLM`/`TELEMETRY` を含む**か、
**`cw_frames` フォーマットを持つ衛星**（＝デコードできる衛星）。`get_norads_matching(conn, matcher)`
（`get_norads_for_tab()` から切り出した共通ヘルパー）で `alive=1` かつ非 hidden の衛星を探す。
ARICA-2 の SATNOGS 送信機は単に `Mode U - CW`（TLM の語なし）なので、**フォーマット定義の有無が
無いと見つからない**。DB には他に `CW TLM`/`TLM CW`/`CW Telemetry` の衛星が約30あり（CAS-2T・
DUCHIFAT-1・PROITERES 等）、それらもコンボに並ぶ。**デコードできる衛星（`cw_frames` あり）が先頭**。
形式が無い衛星を選んで ▶ Start すると「まだ定義されていません」と出て開始しない
（2026-09-22 まで文言に「ARICA-2 のみ」と衛星名を固定で入れていたが、OrigamiSat-2 対応追加で
古い情報になったため汎用の文言に変更）。
`COMMS_TAB_CONFIG` には**キーを足していない**（実在のタブ用で、Quick Panel と自動オープンが使うため）。

### `cw_frames` スキーマ（`telemetry_formats/{norad}.json`、`comms/telemetry/cw_frames.py`）

`telemetry_ids`（バイト単位）とは別の、ビット詰めの16進フレーム用スキーマ:

```json
"cw_frames": { "HK1": {"label": "...", "hex_digits": 16, "fields": [ ... ]}, "HK2": ..., "HK3": ... },
"tables":    { "angvel_edges": [0.0, 0.173, ...] }
```

`fields` は MSB から順に消費される。種別: `flag`（1bit、`labels` で文言）・`uint`（`scale`/`add`/`unit`、
`sign_from` で前のフラグの符号）・`angvel`（5bit 符号＋大きさ）・`hms`（GPS 時刻を3項目から合成、bit なし）。
`hidden` は解析するが表示しない。`expect`（常にこの値）と `range`（[最小,最大]）が妥当性検査。
フレーム定義には任意で `id_prefix`（コールサイン等、データ部の直前に無音無しで続けて送られる文字列。
後述「`id_prefix`」節参照）も持てる。
`get_telemetry_id_defs()` が `cw_frames` も `{key: {label, fields}}` の形で返す（hidden を除く）ので、
「全項目デコード結果」の HK1/HK2/HK3 サブタブは既存コードのまま出る。**`telemetry_ids`（バイト単位の
AX.25 用）と `cw_frames` は同じ衛星が両方持てる**（2026-09-22、OrigamiSat-2 で判明。当初は
「どちらか一方」という前提で `telemetry_ids` があれば `cw_frames` を見ずに即 return していたため、
OrigamiSat-2 に `cw_frames` を追加しても「全項目デコード結果」タブに `TLM` サブタブが出ず、CW TLM
モードで受信してもテーブル行は増えるのにサブタブの値が更新されない、という不具合になった。
`get_telemetry_id_defs()` を「`telemetry_ids`/`csv_messages` を土台に、存在すれば `cw_frames` の
エントリをマージして返す」方式に修正済み。キーの衝突は起きない設計（`telemetry_ids` は数値文字列
キー、`cw_frames` は `"TLM"`/`"HK1"` のようなテキストキー）。

**`68796.json` に `modulation` キーを入れてはいけない**: `_select_telemetry_satellite()` の
Direwolf 分岐が `modulation` の先頭文字（`CW`）を「優先するモード」として使い、Direwolf コンボで
ARICA-2 を選ぶと GMSK ではなく CW 送信機が選ばれてしまう。

### ARICA-2 のフレーム（`arica2.ksy` ＝レイアウト・換算式、坂本研の表＝文言）

| フレーム | 16進桁数 | bit 数 | 内容 |
|---|---|---|---|
| HK1 (`cw1`) | 16 | 64 | コマンドID・セットアップ/アンテナ・各コアの電源・エラー・姿勢制御・角速度X/Y/Z 等 |
| HK2 (`cw2`) | 12 | 48 | サムネ/JPEG/GPS 更新・GPS 時刻・緯度/経度・高度 |
| HK3 (`cw3`) | 14 | 56 | 電池基板温度・ヒーター・電力の流れ・各機器電源・UHF 温度・アップリンク数・受信電圧・電池電圧 |

換算式（ksy）: 電池基板温度 = raw×0.7952 − 238.3712、UHF 温度 = (1.0331 − raw×5.04/1023)/0.0056、
受信電圧 = raw×5.04/1023、電池電圧 = raw×0.008978 + 6.1。ksy の `*_form`（呼出符号・beacon_type
付き）は SatNOGS 投稿フォーム用で電波上のものではない。

**実データでの確認（2026-09-20 の IQ 録音）**: HK1 `2FFE8594EB880124` は坂本研の表の該当行と
**全ての離散項目が一致**（SBD error=Abnormal、省電力遷移2、再起動 4/7、姿勢制御回数4、姿勢制御「不可」）。
HK3 `00D7C2A8D6B8EA` は温度 −237.576、UHF 温度 36.682、受信電圧 1.059、電池電圧 8.201、
アップリンク13、最終コマンドID 1 が表と一致。

**極性・表の決め方（実データと表を突き合わせた結果）**:
- **電池ヒーター**: 生ビット 1 ＝「off」。坂本研の32日分（126行）は全行 `off` で、受信フレームのビットは 1。
  ビット 0（on）のフレームは見たことがないので、0=on は未検証（JSON の `labels` は `{"1":"off","0":"on"}`）。
- **電力の流れ**: 0=charge（受信フレームと一致）、1=discharge（補集合）。
- **角速度**: 5bit **符号＋大きさ**（最上位=符号、下位4bit=大きさ）。X/Y/Z は同じ範囲表を共有し、
  境界は `0, 0.173, 0.377, 0.615, 0.895, 1.223, 1.609, 2.061, 2.591, 3.213, 3.943`（32日分の
  範囲表示18種から収集）。受信フレームの X=23（1 0111）→ −2.591〜−2.061、Y=2 → 0.377〜0.615、
  Z=0 → 0〜0.173 が全て表と一致。**大きさ10以上は下限しか分からない**ので「範囲未確認」と表示する。
  範囲表示の単位は公開ページに無く、表示しない。
- **spr2 のサブコア**: 坂本研の表は `sub1/2/4/5`、ksy は `sub1/2/3/5`。坂本研の名前を採用。
- **HK2** は実フレーム未受信。3つのフラグ（Thumbnail/JPEG/GPS update）は極性が未確認なので生ビット
  （0/1）で表示し、GPS 時刻・緯度経度・高度は ksy どおり。ksy の `valid`（時0–23・分/秒0–59・緯度≦90・経度≦180）
  を妥当性検査に使う。

### OrigamiSat-2 のフレーム（2026-09-22 追加、公式 CW 仕様書ベース）

ARICA-2と違い、OrigamiSat-2（NORAD 68795）は**CW用の公式データフォーマット文書が公開されている**
（[ORI-2-0027e-OPR "OrigamiSat-2 CW Downlink Communication Data Format" ver.1.1](http://www.origami.titech.ac.jp/wp/wp-content/uploads/2026/04/ORI-2-0027e-OPR_OrigamiSat-2_CW_Downlink_Communication_Data_Format_ver1.1.pdf)、
`68795.json` の `document_cw` 参照）。既存の `telemetry_ids`（ID65/100/130）は別文書
（ORI-2-0027-OPR「FMダウンリンク通信データフォーマット」）が定義する**AX.25側**のバイトオフセット
形式で、CWの28バイト固定フォーマットとは無関係の別構造（このため同じ衛星が `telemetry_ids` と
`cw_frames` の両方を持つ、初めてのケースになった。前述の `get_telemetry_id_defs()` マージ対応の
きっかけ）。

| フレーム | 16進桁数 | bit数 | 内容 |
|---|---|---|---|
| TLM | 56 | 224（28バイト） | 衛星モード（UVC状態/レベル・運用モード）・バッテリー電圧/電流/温度・発電状況（SAP×5面）・スイッチ情報・角速度X/Y/Z・OBC/ADCS/Raspiの最終コマンドID・ADCSモード・バス通信部/CBand送信機温度・OBC起動回数・予約コマンド数・衛星内部UNIX時刻・UVC閾値×4・バス通信ヒューズカット回数 |

仕様書通りバイト単位（ビット未満のサブフィールドは衛星モード・発電状況・スイッチ情報の3バイトのみ）。
換算式（仕様書2.2〜2.14節）: 電圧＝raw/16、電流＝(raw−32767)/10.9225、温度＝raw−128、
角速度＝raw/10−12.7、UVC閾値＝raw/10。ADCSモードは0x00=START UP/0x01=INITIAL/0x02=BDOT/
0x04=3AXIS/0x06=RMMEST/0x07=EARTHPOINT（Table 9）。OBCコマンド実行結果は仕様書に
「解釈方法は非公開」と明記されており生の数値のみ表示。SatNOGS投稿レイアウト（ARICA-2の
`arica2.ksy` `*_form` に相当するもの）は未確認のため `satnogs` キーは付けていない
（`build_satnogs_frame()` は常に `None` を返す）。

**実データでの確認（2026-09-22、コールサインJS1YRU、独立した2回のCWコピー）**: `817F7F8E841C05
827E773D160000008585230 66AB225BF4B42483E00` と `817F7E5284 1C057082 6D3D16000000858523066AB2
26084B42483E00`（空白はCWコピーの区切りで、実際の28バイトからは除去）を手動デコードし、
**UVC閾値4バイトが仕様書Figure 3の例示電圧値（7.5V/6.6V/7.2V/6.2V）と完全一致**、
**衛星内部UNIX時刻が受信当日の日付に変換され、2回の受信間で73秒進んでいた**
（衛星モードは両方とも Normal Mode＝3秒間隔ビーコンと矛盾しない）ことから、オフセット割り当ての
正しさを確認済み。電流・角速度バイトは2回で大きく食い違った（値そのものが変動する項目のため
無矛盾）ため、この2フィールドは今回のコピーでは数値の妥当性を検証できていない。

### `id_prefix`（2026-09-22、実機再現で発覚した不具合の修正）

上記の手動デコードでオフセット割り当てを確認した直後、実際にアプリ（Telemetry タブ CW TLM →
OrigamiSat-2、同じ IQ 録音を再生）で試したところ、**CW デコーダタブには同じ内容が表示されるのに
Telemetry タブの受信フレーム表には何も追加されない**という不具合が発生した。

原因は `frame_block_ready` の**ブロック分割そのもの**にあった。`CwBlockExtractor`（前述）は
文字間の無音が3秒を超えた地点でブロックを切るが、ORI-2-0027e-OPR Table 1 の「CW is transmitted
in the order of call sign, satellite name, and data section」は、コールサイン・衛星名・データを
**単語間の通常のCW間隔（3秒よりはるかに短い）で続けて送る**。このため実際に届くブロックは
`"JS1YRUORIGAMI2"` + 56桁の16進数字が**1つに繋がったテキスト**（例:
`JS1YRUORIGAMI2817F7F8E841C05827E773D16000000858523066AB225BF4B42483E00`、70文字）になり、
純粋な16進文字列を前提にしていた `match_frame_key()`/`decode_cw_frame()`/`is_near_miss()` の
どれにも一致せず、`_on_cw_block()` の「それ以外（ID テキスト・ノイズ）は無視」経路で**表にも
ログにも一切残らず黙って捨てられていた**（この「完全に無視」経路にはログが無く、原因調査のために
`CwTab._feed_block_extractor()`・`TelemetryTab._on_cw_block()` へ診断用 `logger.info()` を追加して
初めて実際のブロックテキストが可視化できた）。ARICA-2ではこの問題が出ていなかった＝ARICA-2の
CWビーコンはコールサイン等を挟まず素の16進のみを送っていると見られる（少なくとも今回検証した
実データはそうだった）。

修正として `cw_frames` のフレーム定義に任意の `id_prefix` を追加できるようにした
（`comms/telemetry/cw_frames.py`）。設定されていれば、ブロックの先頭からその**完全一致の**文字列を
取り除いてから桁数・16進判定を行う（`_hex_part()`/`_match()`）。OrigamiSat-2の`TLM`フレームには
`"id_prefix": "JS1YRUORIGAMI2"` を設定済み。`match_frame_key`・`decode_cw_frame`・
`is_near_miss`・`build_satnogs_frame` の4関数すべてがこの共通ヘルパーを通る。`id_prefix` を
持たないARICA-2の3フレームは従来通り素のブロック全体を見るので非対応化はない。

**プレフィックスの誤読は追わない（既存の厳格方針と同じ）**: `id_prefix` は完全一致でしか剥がさない。
コールサイン側が1文字でも化けたブロック（実際に `J1YRUORIFNO8273/9` のような例がログに出た）は
プレフィックスが一致せず、そのままノイズとして無視される（`is_near_miss` も対象外）。一方、
プレフィックスは正しく読めたがデータ部の16進が1桁化けたブロック（実際に `?` を含む70文字の
ブロックがログに出た）は、桁数は合うが16進として無効なので `is_near_miss` が真になり、グレーの
`[?]` 行として表示される（データとしては使われない）。

**2026-10-13 追記: ID の位置・誤読への対応（`split_frame_blocks()`、`_find_prefix()`）**: 同日の
OrigamiSat-2 の再生で、`CwBlockExtractor` が出したブロック8件がすべて棄却された。フレームは連続して
送られ、前のフレームの末尾（`…4B42483E00`）と次の `JS1YRU…` の間に3秒超の無音が無いため、
(a) ブロックの頭に前フレームの末尾が付く（`04B0WO483E003Y`＋`JS1YRU…`＋56桁。データ自体は正しい）、
(b) ID が `RJI1YRUORIGSMI2` のように読み違う、が起きていた。対応として `id_prefix` を**ブロック先頭
一致から「ブロック内のどこでも・編集距離3以内」の検索**に変えた（`_hex_part()` 経由なので
`decode_cw_frame`・`match_frame_key`・`is_near_miss`・`build_satnogs_frame` 全てに効く）。1ブロックに
複数フレームがあれば `split_frame_blocks()` が ID 位置で切り、`TelemetryTab._on_cw_block()` が1フレーム
ずつ処理する（時刻はブロック内の位置で按分）。桁数・16進・`expect`/`range` の検査は従来どおり厳格で、
末尾が切れたブロックや `?` を含むブロックは近似候補（灰色 `[?]`）のまま。IDは位置合わせにだけ使う。

**2026-10-13 追記（時刻検査・既知桁・SatNOGS 送信）**: `cw_frames` のフレーム定義に任意キー
`clock`（フレーム内の衛星時刻 `satellite_time` を受信時刻と照合。許容180秒。外れたら無効。上位6桁の
読めない桁は時計から補完）・`known_digits`（設定値の末尾10桁 `4B42483E00` の `?` 補完・途中で切れた
フレームの補完。読めている桁は上書きしない）・`satnogs`（送信形式）を追加（`repair_block()`、
`decode_cw_frame(..., when)`）。OrigamiSat-2 は SatNOGS にデコーダーが無く、各局は受信した生バイトを
そのまま送っているため `"satnogs": {"raw": true, "require_repeat": false}`＝**28バイトの生フレームを
そのまま送信**。毎フレーム内容が変わる（衛星時刻・センサ値）ので「同じフレームの2回受信」の確認は
成立せず、自動送信も繰り返し確認を待たない（`repeat_required()`）。ログには ID 抜きの16進だけが
残るので `build_satnogs_frame()` は16進のみの文字列も受け付ける。

### 誤読への対策（厳格モード）

CW には CRC が無く、AI の CW デコーダは弱い信号で桁を間違える（実際に HK3 の先頭桁が `1`/`0` で揺れた）。
`_on_cw_block()` の扱い:

| ブロック | 扱い |
|---|---|
| 16進桁数がちょうど12/14/16で、`expect`/`range` を満たす | **受信フレーム**（表に追加・全項目更新・DB 保存・件数に数える） |
| 桁数は合うが検査に落ちる（`not_used`≠0 等） | 灰色の行 `[?] HK1 <16進> (理由)`。**データとして使わない**・件数に数えない・DB に保存しない |
| 桁数が±1桁、または非16進文字が少し混ざる | 灰色の行 `[?] <文字列> (…不使用)` |
| それ以外（ID テキスト `DE JS1YSD ARICA2`・ノイズ） | 無視 |

将来の改善案（未実装）: ビーコンは約41.5秒周期で繰り返すので、桁ごとの多数決で確度を付ける。

### 時刻（`CwTab._signal_time_now()`）

- **ライブ**: 現在の UTC。
- **IQ 再生**: `SdrFileDevice.start_time_utc + position_s`（先頭サンプルの UTC ＋ 再生位置）。シークや
  一時停止にも追従する（位置を都度読むので）。ブロック内の文字ごとの時刻は
  `スナップショット時刻 − 窓長 + 文字のオフセット`。**デコードは約1秒かかり、その間にバッファが進む**ので、
  スナップショット時刻と破棄済みサンプル数は `_trigger_decode()` の時点で保存し、結果を受けた時に使う。
- ブロックの区切りは**音声時間**（単調増加）で判定するので、再生位置の飛びで2つのブロックが
  くっついたり割れたりしない。各文字は1回だけ渡す（窓が重なる分は `_block_up_to_abs` で除外）。
- 表示・記録・SatNOGS 送信の時刻は**ブロック先頭（送信を始めた時刻）**（2026-09-21 に末尾から変更）。SatNOGS DB で
  運用者の局（JI1IZR）が ARICA-2 の HK を約41.5秒周期の**送信開始時刻**で登録しているのに合わせた（CW の
  フレームは約17秒続くので、末尾だと約17秒ずれる）。他局は混在（開始/終了付近）で、SiDS 自体に定めは無い。
  実測: 07:01:47〜07:02:05 の HK1 は、先頭の文字の時刻（約 07:01:48）で記録される（初版は末尾の `07:02:04`）。

### IQ 録音の開始時刻（SDR コントロールタブ）

WAV には時刻情報が無い。`IQRecorder` は `{norad}_{name}_{YYYYMMDDTHHMMSSZ}.iq.wav` と名付けるので
`SdrFileDevice` が**ファイル名から UTC の開始時刻を読む**（`parse_start_time_from_filename()`）。
読めないファイル用に、SDR コントロールの再生行の **Offset 入力の右に「Start (UTC):」入力**
（`_playback_start_edit`、`QDateTimeEdit`、UTC）を置いた。読めた時はその時刻を表示し、**読めなかった時は
今日の 00:00:00 を表示**して、その値をデバイスへも渡す（表示 = 実際に使われる値）。修正すると
`SdrFileDevice.set_start_time_utc()` に反映される。再生中の録音がある時だけ有効。

### 「Fast」デコード（IQ 録音を一括で、2026-10-20 追加）

CW Decoder タブの **「📂 Decode IQ Recording…」**と、Telemetry タブ CW TLM モードの
**「速度: 通常（実時間）／高速」**＋「📂 Decode IQ Recording…」（`telemetry_decode_speed` を他モードと共用）。
通常は従来どおり SDR Control で録音を実時間再生する。「高速」はファイルを選ぶだけ。

- `comms/cw/file_decoder.py` の `CwFileDecoder`（QThread）が `iter_iq_blocks()` で読み、ライブと同じ
  CW モード `Demodulator`（48 kHz 音声）に通し、**音声 5 秒ごとに「その時点までの最後の 20 秒」**を
  デコード関数へ渡す（ライブの `_DECODE_INTERVAL_MS` 5 秒・`MAX_AUDIO_SECONDS` 20 秒と同じ窓の切り方）。
  最後の端数（1 秒以上）も 1 窓として処理する。窓は `window_ready(result, 開始s, 終了s)` で 1 つずつ通知。
- `CwTab.start_fast_decode(path)` が受けた窓を**ライブと同じ** `_reconcile_decode()`（確定/保留テキスト）→
  `_feed_block_extractor()`（多数決・ブロック抽出）へ流すので、読み方は実時間再生と一致する。
  `frame_block_ready` も同様に出るため Telemetry タブ側（`_on_cw_block`）は変更なし。窓の時刻は
  ファイル名の開始時刻（`parse_start_time_from_filename`）＋音声位置。ファイル名に時刻が無い録音は
  表示・保存はするが `signal_time_reliable()` が False（SatNOGS へは送らない）。
- 完了/中止時に `_flush_votes()` と `CwBlockExtractor.flush()` で作りかけの文字・ブロックを吐き出す。
  ■ Stop（Telemetry の Stop も `stop_decoding()` 経由）は途中までの結果を残して中止する。
- Telemetry ↔ CW タブ: `cw_tlm_fast_requested(path)` → `MainWindow._on_telemetry_cw_tlm_fast()` →
  `CwTab.start_fast_decode()`。終了は `CwTab.fast_decode_finished(要約)`、進捗は `fast_decode_progress`。
- CW タブ単体にも「速度」プルダウン（`telemetry_decode_speed` を Telemetry タブと共用）。通常は SDR Control で
  録音を開いて実時間再生し、オーバービューのクリックで `CwTab` が開始（`_open_recording_for_replay()` /
  `_on_iq_seek_done()`）。高速は上記の一括デコード。初期値は通常。
- **搬送波の自動オフセット（2026-10-20）**: CW モード `Demodulator` は IQ の**実部**だけを 300〜3000 Hz のバンドパスに
  通すため、録音の中心（0 Hz）に乗った CW は直流として消え、何も読めなかった（ドップラー補正済みの自動録音は
  信号がほぼ 0 Hz に来る。LASARsat `DE OK0LSR … THIS IS LASARSAT` で確認）。`find_cw_carrier_hz()` が録音を 1 回走査
  （0.25 秒フレームごとにスペクトルを自身の中央値と比較し、10 dB 超を「オン」と数える）し、**断続する最強の狭帯域線**
  （全フレームの 80% 超でオンの定常スパーと、オン 4 フレーム未満は除外）を選ぶ。`CwFileDecoder.run()` はその線が
  `CW_TARGET_PITCH_HZ`（700 Hz）に来るよう各ブロックを位相連続の複素ミキサーで移動してから復調する。線が無ければ
  そのまま。ドップラー追従はしない（補正済み録音前提。ゆっくりした残差だけを想定）。通常（実時間）再生は対象外。
- テスト: `tests/test_cw_file_decoder.py`（デモジュレーターをスタブ化＝CI に scipy 無し）、
  `tests/test_telemetry_cw_tlm.py` の `TestFastDecode`。
- 速度: ARICA-2 の実録音（`68796_ARICA-2_20261014T065853Z.iq.wav`、706 秒・1.4 GB）の DeepCW 高速デコードが
  **22.9 秒（約 31 倍速、142 窓）**（Mac、2026-10-20）。復調は数秒で、大半はモデル推論。

### テスト

`tests/test_cw_frames.py`（実フレームの全項目・HK2 の合成・妥当性検査・角速度表）、
`tests/test_cw_block_extractor.py`、`tests/test_cw_tab.py`（`TestFrameBlocks`/`TestControlApi`/`TestSignalTime`）、
`tests/test_telemetry_cw_tlm.py`（コンボ・モード切替・開始停止・受信ブロック・MainWindow 配線）、
`tests/test_mode_detection.py`、`tests/test_recording_start_time.py`、`tests/test_file_device.py`、
`tests/test_sdr_control_widget.py`。いずれも CW モデル・scipy 無しで動く（`SdrFileDevice` が絡むものだけ `importorskip`）。
実物の CW デコーダ＋再生パイプライン＋Telemetry タブを通した通しの動作も、ARICA-2 の録音で確認済み。

---

## SatNOGS DB へのアップロードと時刻（2026-09-21 追加）

### 「SatNOGS Upload: ON/OFF」の対象と、再生時の時刻

フッターの ON/OFF スイッチは**ライブ専用ではない**。Direwolf（AX.25）・gr-satellites・CW TLM の
どの経路でも、IQ 再生中でも効く。以前は AX.25 / gr-satellites の経路が**アップロード時刻に
`datetime.now()`（今の時刻）を使っていた**ため、古い録音を再生しながら ON にすると、
**再生日ではなく現在の時刻**で公開されてしまった。今は次のとおり（`TelemetryTab._frame_time()`）:

- 時刻は `comms.signal_clock.signal_time()`（ライブ＝現在の UTC、IQ 再生＝録音の開始時刻＋再生位置）。
  「受信フレーム」表・`telemetry_log`・SatNOGS への `timestamp` に同じ時刻が入る。
- 再生中の録音の開始時刻が**確定していない**（ファイル名に無く、SDR コントロールの「Start (UTC)」が
  仮の 00:00 のまま）ときは**アップロードしない**（`_submit_raw_frame()`。1回の実行につき1度だけ
  ステータスに理由を出す）。確定 = ファイル名から読めた、またはユーザーが入力した
  （`SdrFileDevice.start_time_confirmed`、`SdrControlWidget` は仮値を `confirmed=False` で渡す）。

### 日付

「受信フレーム」表の時刻列は `YYYY-MM-DD HH:MM:SS`（UTC）。再生する録音は何日の受信でもありうるので
日付が要る。CSV エクスポートは表をそのまま書き出すので日付が入る。`telemetry_log.received_at` は
以前から日付付きの ISO 形式。

### CW フレームの SatNOGS DB アップロード（`comms/telemetry/cw_upload.py`）

送るバイト列: **`arica-2`（7バイト）＋ 種別（HK1=1, HK2=2, HK3=3）＋ フレームのバイト**
（HK1=16、HK2=14、HK3=15 バイト）。`arica2.ksy` の `cw1_form`/`cw2_form`/`cw3_form`
（DL7NDR の CW アップロード用フォーム向け）と同じ並び。定義は `68796.json` の各フレームの
`satnogs`（`callsign`・`beacon_type`）、組み立ては `cw_frames.build_satnogs_frame()`。既存の
`SatnogsUploader`（SiDS、`timestamp` はミリ秒付き UTC）でそのまま送る。**SatNOGS が実際に
受理するかは、実際に送った結果で確認していない**（既存データの読み取り API は認証が要る）ので、
最初は1件だけ手で試すこと（形式は、運用者の局が既に投稿しているフレームと同じことを確認済み）。

CW にはCRCが無く、検査を通っても桁を間違えていることがある（HK3 の先頭桁 `1`/`0`）。公開DBを
汚さないため、次の規則で送る:

| 規則 | 内容 |
|---|---|
| **2回以上受信** | 同じフレームが、**別の送信として**（`MIN_REPEAT_GAP_S`=20秒以上離れて）30分以内に受信されていること。同じ録音を2回デコードしたものは数えない |
| **時刻が確実** | 録音開始時刻が仮値のフレーム（`telemetry_log.time_reliable=0`）は**手動でも送らない** |
| **一度だけ** | 送信済み（`satnogs_uploaded_at`）は送らない。同じフレームが30秒以内（CW フレームは約17秒続き、旧版は終了時刻で送っていた分との差も吸収する。同じ内容は41.5秒以上あけて再送されるので別の受信は落とさない）なら「同じ受信の再生」として送らない |

- **自動**: スイッチ ON のとき、CW フレームを記録するたびに `auto_send()` が規則を確認し、2回目が
  届いた時点で、待っていた1回目も一緒に送る。1回目だけのときは何もしない（静かに待つ）。
- **選択分を送信**（CW TLM モードのフッター）: 表で選んだ行を、1回しか受信していなくても、
  スイッチが OFF でも送る（`force=True`）。API キー・コールサイン・位置は必要。時刻と一度だけの規則は守る。
- **未送信を送信…**: このログ済みの未送信フレームのうち、規則を満たすものを確認ダイアログの後に送る
  （後から送るための操作。IQ を再生し直した分の重複は「一度だけ」で弾く）。
- **送信済みの印は、SatNOGS が受理（HTTP 2xx）したときだけ付ける。** `SatnogsUploader.submit(on_result=...)` が
  POST の結果（受理か・HTTP ステータス・本文、ネットワークエラーはステータス0）をワーカースレッドから返し、
  `TelemetryTab._upload_result` シグナルで GUI スレッドへ渡して `mark_uploaded()` する。拒否・失敗は
  ステータスに理由を出し（401/403 は「APIキーを確認」）、フレームは未送信のまま残るので再送できる。
  応答待ちのフレームは `_upload_pending` で二重にキューへ入れない。AX.25 / gr-satellites の送信も結果を受け、
  **失敗だけ**を1回の実行につき1度表示する。
  （初版は「キューに入れた」時点で印を付けたため、キーが不正で HTTP 401 で拒否されても
  「送信済み」と表示・記録された。`reset_unconfirmed_marks()` が既存 DB の旧マークを1度だけ消す。）
- `telemetry_log` に `satnogs_uploaded_at` と `time_reliable` 列を追加（`ensure_columns()`、既存 DB へは
  `ALTER TABLE`）。`SatnogsUploader.submit()` / `build_submission()` に `force`、`upload_blocker()`
  （送れない理由: `disabled`/`no_api_key`/`no_callsign`/`no_location`）を追加。

### 検証なしで全部送る（2026-10-22 変更）

SatNOGS Upload が ON のとき、フレームの検証結果にかかわらず**受信したものは全部送る**。
SatNOGS の仕組み（satnogs-client は `data*` ファイルを無検証でそのまま Network へ送り、
satnogs-db の `TelemetryViewSet.create` も `frame` を検証せず保存する）に合わせ、間違った
データでも多数集まるほど誤りを見つけやすく、受信の証拠にもなるというユーザー判断による。
上の CW の規則表のうち「**2回以上受信**」は**廃止**した（自動送信・「未送信を送信…」とも）。
「時刻が確実」「一度だけ」は残っている。追加で次も送る:

- HADES の修復済み（未検証）フレーム（表示は淡色のまま）。
- CW の妥当性検査に通らない `[?]` フレーム（ログにも保存し、通常の CW フレームと同じに送る）。
- CW の長さ違い（近似）ブロック: `cw_frames.build_lenient_satnogs_frame()` が、長さが最も近い
  フレーム定義の接頭辞・種別バイトに、受信した桁をそのまま（何も足さずに）付ける。
  奇数桁・16進以外の文字を含むものはバイト列にできないので送れない。この経路はログに保存しないので、同じ録音を再生し直すと
  重複して送られうる。

### SatNOGS DB が衛星を登録している NORAD ID（2026-09-21）

SatNOGS DB の受信側（`satnogs-db` の `TelemetryViewSet.create`）は、投稿の `noradID` を
`Satellite.objects.get(satellite_entry__norad_cat_id=noradID)` で探し、**見つからなければ
「New Satellite」という衛星エントリを新規作成する**（`norad_follow_id` では探さない）。
ARICA-2 は SatNOGS DB で **NORAD 98329**（`norad_follow_id`=68796）として登録されていて、68796 では
見つからない。そのまま 68796 で送ると、ARICA-2 に付かないうえ、SatNOGS DB に余計な衛星を作ってしまう。

- 本アプリでは、実 ID へ移行済みの衛星の `satellites.satnogs_source_id` に SatNOGS 側の ID が入っている
  （68796 → 98329）。`satnogs_norad_candidates()` が「`satnogs_source_id`、実 ID」の順の候補を返す。
- `SatnogsUploader` はワーカースレッドで、送る前に候補を**読み取りで照会**して
  （`GET /api/satellites/?norad_cat_id=…`、要 API キー、衛星ごとに1回・実行中は記憶）、SatNOGS DB が
  実際に載せている最初の ID で `noradID` を決める。**どれも載っていなければ送らず**、
  `SatNOGS DB does not list NORAD …` と結果に返す（HTTP 404 扱い）。照会自体ができないときも送らない。
  移行が進んで 98329 が無くなり 68796 が載った場合は、自動で 68796 に切り替わる。
- これは AX.25 / gr-satellites の送信にも同じく効く（仮 ID から実 ID へ移行済みの衛星は同じ危険があった）。
- ドライラン（POST を記録するだけの偽物で実 SatNOGS DB を照会）で、ARICA-2 の HK1 は
  `noradID=98329`・`frame=61726963612d32012ffe8594eb880124`（`arica-2`＋`01`＋16バイト）になることを確認済み。
  SatNOGS DB には運用者の局が同じ形式（`arica-2`＋1/2/3＋フレーム、16/14/15バイト）のフレームを
  既に投稿していて、DB 側でデコードされている。

### テスト

`tests/test_cw_upload.py`（規則）、`tests/test_signal_clock.py`、`tests/test_telemetry_cw_tlm.py`
（日付・CSV・再生時の時刻・自動/手動送信）、`tests/test_satnogs_uploader.py`（`force`・`upload_blocker`）、
`tests/test_cw_frames.py`（`build_satnogs_frame`）。

---

## AX.25 / gr-satellites の時計（第二段階、2026-09-21）

### 時計の共通化

Direwolf (AX.25)・gr-satellites・CW TLM の3モードすべてが、同じ `comms.signal_clock.signal_time()`
（ライブ＝現在の UTC、IQ 再生＝録音の開始時刻＋再生位置。仮の開始時刻は「信頼できない」扱い）を通す。
`TelemetryTab._frame_time()` が窓口で、AX.25 の `_on_ax25_frame()`、gr-satellites の
`_on_gr_telemetry()`（表の行）と `_on_gr_raw_frame()`（SatNOGS 送信）が使う。表・`telemetry_log`・
SatNOGS の `timestamp` に同じ時刻が入り、信頼できない時刻のフレームは送らない
（詳細は「SatNOGS DB へのアップロードと時刻」節）。CW は先頭文字の時刻、AX.25/gr は
フレームを受け取った時点の時刻（ミリ秒〜秒未満の短いフレームなので先頭/末尾の差は無視できる）。

### 実測（合成 IQ の再生。`scripts/g3ruh_sensitivity.py` の 9k6 G3RUH フレームを 0.19 秒の
バーストとして 4/10/16/22 秒に置き、開始時刻 12:00:00 の録音として再生、SNR 25 dB）

| 経路 | 表示・記録・送信される時刻（フレーム終端 +） |
|---|---|
| Direwolf (AX.25) | +0.07〜0.10 秒。4件とも 12:00:04/:10/:16/:22 |
| gr-satellites（実物の gr_satellites） | +0.23〜0.29 秒。表の行と SatNOGS 送信の両方が正しい時刻 |

どちらも受信後の処理遅延が 0.3 秒以下で、ライブ（`now()`）と同じ意味の時刻になる。

### gr-satellites モードの不具合（第二段階の検証で判明・修正）

gr-satellites モードは、SDR からの入力では**これまで一度もデコードされない状態**だった
（同梱の gr_satellites で実測。実機での完全な E2E が未検証だった）。原因は次の5つで、
いずれも合成 IQ を実物の gr_satellites に通して1つずつ確かめた:

1. **`--udp_raw` が無かった**: gr_satellites は `--udp` だけだと**16bit整数**を期待する
   （`udp_source(sizeof_short)`→`short_to_float`）。パイプラインは complex64 を送るので、ノイズとして
   読まれ、何もデコードされなかった。`--udp_raw`（float32/complex64）が必要。`--help` に
   `--udp_raw` があるビルドだけに付ける（`_supports_udp_raw()`、`--kiss_server` と同じ確認方法）。
2. **UDP データグラムが大きすぎた（macOS）**: 32768 バイトで送っていたが、macOS のループバックは
   `net.inet.udp.maxdgram`（既定 9216）を超えると EMSGSIZE で失敗し、しかもエラーを握りつぶしていた
   → 1サンプルも届かない。`_UDP_CHUNK_BYTES = 8192`（complex64 の整数個）に変更。
3. **KISS の受け口が1秒で切れた**: `_KissFrameReader` は `create_connection(timeout=1.0)` で接続し、
   この1秒が接続後の `recv()` にも残っていた。フレームはまばらなので、無音が1秒続くと `recv` が
   タイムアウトしてループを抜け、接続を閉じていた（以後のフレームは二度と読まれず、macOS では
   gr_satellites 側も `shutdown: Socket is not connected` で落ちた）。`settimeout(0.5)` にし、
   タイムアウトは「まだ何も無い」として待ち続ける。
4. **標準出力がバッファされた**: gr_satellites は Python 製で、標準出力がパイプだとブロック
   バッファされる（約8 KiB 溜まるまで出ない）。まばらなフレームは表に出なかった。
   `PYTHONUNBUFFERED=1` を環境に設定。
5. **表の行の区切り**: 標準出力を「空行」で区切っていたが、同梱版の gr_satellites はフレーム間に
   空行を入れない → プロセス終了まで1件も出なかった。次のフレームの見出し（`-> Packet from`）でも
   区切り、出力が0.3秒止まったらそのブロックを出す。また `-> Packet from` を含まないブロック
   （gr_satellites 自身の警告・進捗）は表に出さない（`gr_satellites.log` には残る）。

テスト: `tests/test_gr_satellites_backend.py`（各修正の回帰テスト。KISS の無音、UDP の loopback 転送、
`--udp_raw`、出力のバッファ、ブロックの区切り）、`tests/test_telemetry_clock.py`（AX.25/gr の
再生時の時刻・記録・送信・仮の時刻・ライブ）。**IQ 録音の再生 → 実物の Direwolf / gr_satellites →
Telemetry タブ**の通しの動作は合成 IQ で確認した。実信号（衛星）での確認は未実施。

---

## AX.25 フレームの SatNOGS 手動アップロード（2026-10-01）

**背景**: KNACKSAT-2（NORAD 67683、コールサイン `HS0K`）の 9600 bps AX.25 を受信できたが、
自動アップロードも手動アップロードもできなかった。原因は 2 つ:
1. `_callsign_to_norad()` が衛星名の部分一致と `telemetry_formats` しか見ておらず、`HS0K` を
   NORAD に対応付けられなかった → `norad=None` → `SatnogsUploader.submit()` が何もせず戻る。
   SatNOGS DB は衛星の `names` フィールドにコールサインを持つ（KNACKSAT-2 → `"HS0K"`、
   実 API で確認済み）。本アプリは `satellites.alt_names`（JSON 配列）に同じ値を保存しているので、
   これを**曖昧でない（1 衛星だけ一致する）場合に限り**フォールバックとして引く。
2. 「Send selected」ボタンは CW TLM モード専用だった。

**実装**: AX.25（Direwolf）モードでも「Send selected」を表示（gr-satellites のみ非表示）。
行ごとに `_Ax25Row`（**フル AX.25 フレーム**・時刻・NORAD・送信済み/送信中）を表の 1 列目の
`_AX25_ROLE` に保持する。`telemetry_log.raw_hex` は **ペイロードのみ**を保存するため、DB からは
SatNOGS が要求するフル フレームを再構成できない — **送信できるのは、そのセッションで表に残っている行だけ**
（アプリ再起動後は不可）。NORAD が不明な行は画面で選択中の衛星に紐付け、送信前に衛星名＋件数を
確認ダイアログで確認する（SatNOGS DB は公開のため）。自動アップロード済みの行は送信済みとして
記録され、二重送信しない。

### KNACKSAT-2（67683、HS0K）の全項目デコード（2026-10-01）

`src/data/telemetry_formats/67683.json`。レイアウトは SatNOGS decoders の `knacksat2.ksy`
（MIT、EmOne / Anol P.）から変換した。ビーコン（ペイロード先頭 `00 00`・149 バイト）のみ対象で、
ファイル名応答など他の種別（先頭 `40 5A` 等）はデコードしない。そのため flat `fields` リストに
任意キー `match: {"offset", "hex"}` を追加した（`decoder._payload_matches()`。無指定なら従来どおり
全ペイロードをデコード）。float は **リトルエンディアン**なので型 `float32_le` を追加。
単位は ksy に無く、フィールド名と実フレーム（バッテリー約8.2 V・約12.5 ℃・MPPT 電流×電圧＝電力）から
推定した。`*_res_*` は各ブロック先頭の CSP ヘッダー 5 バイトで、意味は未確認。

**訂正（同日）**: 当初の 67683.json は flat `fields` のみで、「Decoded Fields」タブ
（`_rebuild_decode_tabs()`）は flat 形式の衛星では無効になるため値が表示されなかった。
`match` 付き flat リストは `get_telemetry_id_defs()` で単一サブタブ `BEACON_ID`（`"beacon"`、
ラベルは `beacon_label`）として扱うよう修正。`match` の無い従来の flat 形式の挙動は変えていない。

### 保存済みフレームの再表示（Load saved、2026-10-01）

受信フレームは従来から `telemetry_log` に自動保存されていたが、表への再読み込み手段が無く、
再起動後は IQ 録音を再生し直す必要があった。Telemetry タブのフッターに **Load saved** を追加
（AX.25 モード。選択衛星の保存済みフレームを表と Decoded Fields へ戻す。表示済みの行は
`_LOGGED_ROLE` の log id で重複を避ける）。

- `telemetry_log` に列 `frame_hex`（**フル AX.25 フレーム**、SatNOGS 送信に必要）と
  `kind`（`ax25` / `hdlc` / `cw`）を追加（`_ensure_db_table()` が ALTER）。それ以前の行は
  `frame_hex` が NULL で、`kind` は `_saved_kind()` が推定する（callsign 空→hdlc、空白を含む→cw）。
- **フル フレームの無い旧行**はペイロードだけを表示し、SatNOGS へは送れない（"without a SatNOGS
  format" として数える）。デコード結果の表示には支障ない。
- `norad_cat_id` が NULL の行（コールサイン未対応付けの頃に保存）は、コールサインが今は選択衛星に
  対応付く場合に含める。
- 手動送信が受理されたら `satnogs_uploaded_at` を記録する（再読み込み後に二重送信しない）。
- CW TLM の行は対象外（CW は従来どおり録音からのデコード結果を保持）。

**CW TLM の再表示（2026-10-01）**: CW フレームは従来から `telemetry_log` に自動保存されている
（受信開始時刻・16 進文字列・デコード値 `parsed_json`）。**Load saved** は CW モードでは CW 行を
戻す。元のモールス文字列は残っていないため再デコードせず、保存値から `fields_from_saved()`
（`cw_frames.py`）で Decoded Fields の値を復元する（旗・角速度・時刻は保存された表示文字列を
そのまま使う）。フレーム種別（HK1/HK2/…）は、表示項目名が保存値と一致する定義から決める。
送信済みかは DB（`satnogs_uploaded_at`）から復元するので「Send selected」も
そのまま使える。`kind` 列が無い旧い行は `_saved_kind()` が保存項目名で CW / AX.25 を判別する。
「[?]」で灰色表示される不確かな候補は保存していないので戻らない。

### CW 受信の改善（OrigamiSat-2、2026-10-01）

強い OrigamiSat-2 のパス（録音 `0_unknown_20261001T065952Z`、搬送波は雑音中央値比で約25 dB）を
アプリと同じ経路でオフライン再生して再現した。1つ目のフレームは56桁すべて正しく読めたが、2つ目は
余分な「S」・「B」→「K」・「3E」の脱落があり、CW には誤り検出が無いため採用できなかった
（AX.25 との取り違えが原因ではない。信号が弱かったわけでもない）。

1. **確定までの余裕**（`cw_tab._PENDING_MARGIN_S`）は 5 s → 8 s を試したが**撤回し 5 s に戻した**
   （7 通りの窓位置で1つ目のフレームを正しく読めた回数は 5 s で 6/7、8 s で 5/7。余分な文字が
   出るかは窓と信号の位置関係で偶然に決まり、余裕を延ばしても改善しなかった）。根本原因は、
   文字を最初の1回の読み取りで確定する方式（窓位置依存）。重なる窓の多数決が本命の対策。
2. **古典的（タイミング）デコーダー**（`comms/cw/classic.py`）を追加し、CW タブの「Method」で
   AI モデルと選べる（`app_settings.cw_decoder_method`、既定はAI）。numpy のみ。
   - 搬送波は一つのキー ON の最中にも ±25〜35 Hz ふらつく（スペクトログラムで確認）ため、
     固定の狭帯域フィルターでは途中で信号を失う。短いフレームごとにトーン±75 Hz の最大成分を
     包絡線にしている。
   - ドット/ダッシュ・文字間隔は固定の 1:3 でなく、その信号の実測分布（2-means）から決める。
   - 結果: 強い区間（このパスの後半）は AI より正確（`0000808152006ABE06064B42483E00`）。
     弱い冒頭は雑音で誤り（`E E`、`?`）が残り、アプリのつなぎ合わせ（1回確定方式）を通した
     全体では AI モデルのほうがまだ良かった。強く安定した信号向けの選択肢という位置づけ。
   - 将来案: 重なる窓を投票でまとめる、または AI と古典の結果を比べて妥当なフレームを採る。
3. **「長さの合わない候補」**（`cw_frames.is_near_miss`）の許容を ±1 桁から ±2 桁に拡大。
   今日の2つ目のフレーム（56桁のところ54桁）は、従来は一覧にさえ出なかった。

**灰色の「[?]」行の表示（2026-10-02）**: 長さの合わない候補は、受信ブロック全体（先頭の
`JS1YRUORIGAMI2` 等の識別文字列を含む）をそのまま表示していたため、正常に読めた行（16進のみ）と
見た目が違っていた。`cw_frames.near_miss_hex()` で識別文字列を除いた16進だけを表示する。

**多数決（2026-10-02、`comms/cw/voting.py` の `WindowVoter`）**: デコーダーは直近20秒を5秒ごとに読み、
各文字は3〜4回読まれるが、従来は最初の1回で確定していたため、窓の位置しだいで誤読が採用された
（余裕を5→8 sにしても改善せず撤回、上記）。CW タブの **Majority vote**（既定ON、
`app_settings.cw_vote`）で、フレーム用の文字列（`frame_block_ready`）を全読み取りの多数決で決める。
- 各窓の左右 3 s の端は誤読が多い（実録音で確認）ので投票しない。セッション先頭の窓だけは左端が実際の
  音声の始まりなので投票する。
- 同じ文字の読み取りは窓をまたいで 0.04 s 未満しかずれない（実測）ので、0.12 s 以内を同一文字とする。
- 文字の「有無」は、その時刻を覆う窓の過半数がそこに何か読んだか、「何か」は最頻値（同数なら
  窓の端から遠い読み取り）で決める。余分な文字・誤読・脱落のどれも直る。
- 以降の窓が覆わなくなった範囲から確定する（確定は約15 s 遅れる）。停止時は残りを全て確定する。
- CW タブ上に表示するテキスト（transcript）は従来の方式のまま。
- 結果（OrigamiSat-2 録音、窓位置を7通りにずらす）: 1つ目のフレームは 7/7 正解（従来 6/7）。
  2つ目の `S`・`B→K`・`3E` 脱落は全ての窓で同じ誤読のため直らない（モデル自体の系統的な誤り）。

**表は衛星ごと（2026-10-02）**: 受信フレームの表は1つの衛星に属し、別の衛星を選ぶと
（`set_satellite()`）自動で空になる。ARICA-2 を読み込んだ後に OrigamiSat-2 を読み込むと両方が並ぶ、
という紛らわしさをなくすため。フレームはすべてログに残っているので「Load saved」で戻せる。
最初の選択時（それまで何も選んでいない）と、同じ衛星の再選択・選択解除では消さない。
Autotrack が衛星を切り替えたときも同じく空になる。

**モード切替でも表を空にする（2026-10-02）**: 表は 1 種類のフレーム（AX.25 / gr-satellites / CW）だけを
載せる。`_on_mode_changed()` でモードが変わると表を空にする（AX.25 の ARICA-2 を読み込んだ後に
CW TLM へ切り替えても AX.25 の行が残る、という紛らわしさをなくすため）。ログは残る。

**「Send unsent…」ボタンを廃止（2026-10-02）**: AX.25 モードには無く、同じフレームの二重送信は
`send_frames()` の重複チェックと DB の送信済み記録で防がれているため。複数読み取りで確認された
未送信フレームを一括送信する手段は無くなった（個別には「Send selected」で送れる）。
`cw_upload.eligible_unsent()` は UI から呼ばれなくなったが、テスト付きで残している。

**gr-satellites モードでも「Send selected」（2026-10-02）**: gr-satellites は1つのパケットにつき、
標準出力の解析結果（表の行になる文字列）と KISS ポートの生フレーム（SatNOGS に送るもの）を
別経路で出し、GUI には別々に届く。表の行と生フレームを**到着順（FIFO）かつ 5 秒以内**で対応づける
（`_on_gr_telemetry()` / `_on_gr_raw_frame()` / `_pair_gr_frame()`、待機中のものは
`_gr_rows` / `_gr_frames`）。先に来た側は待ち、5 秒待っても相手が来なければ捨てる。
対応した行は AX.25 と同じ `_Ax25Row`（フル フレームを持つ）を表の 1 列目に持つので、
`_send_selected_ax25()` がそのまま使える。自動アップロードの結果も同じ行に記録されるため、
自動送信済みを手動で二重に送らない。フレームが対応しなかった行は "without a SatNOGS format"
として数えられ送られない。**対応が順番と時刻だけの推定**なので、標準出力にあってフレームが
出ない（またはその逆の）パケットがあると、その後の対応が 1 つずれうる。送信前の確認ダイアログで
件数を確かめること。

**gr-satellites の保存と再表示（2026-10-02）**: gr-satellites の行も `telemetry_log` に自動保存し、
gr-satellites モードの **Load saved** で戻せる。`kind='gr'`、`callsign`、`parsed_json` に
`{"text": 表の文字列, "sat": 衛星名}`、`frame_hex`/`raw_hex` に対応づけた生フレーム
（`_persist_gr_row()`）。フレームが後から対応づいたときは同じ行を UPDATE する
（`_save_gr_frame()`）。送信済みは `satnogs_uploaded_at` に記録し、再表示後も二重送信しない。
フレームが対応しなかった行も文字列だけは戻るが送信はできない。AX.25 / CW の Load saved には
gr の行は混ざらない（`kind` で分ける）。

### コールサイン未対応の AX.25 フレームも選択衛星で SatNOGS へ送る（2026-10-14）

T.Microsat-1 の IQ 録音を AX.25 モードで再生すると 92 フレームをデコードできたのに、SatNOGS へ
1 件も送られなかった。原因は `_on_ax25_frame()` が NORAD をコールサインからしか決めておらず、
アプリが知らないコールサインだと `norad=None` → `SatnogsUploader.submit()` が何もせず戻っていたこと。
コールサインから決まらないときは `_active_norad()`（AX.25 モードで選択中の衛星）にフォールバックし、
デコード（その衛星のフォーマット）・ログ・アップロードの全てをその衛星の NORAD で行う。
非 AX.25 フレーム（`_on_non_ax25_frame`）と gr-satellites（`started_norad`）は元から選択衛星基準。
これで Telemetry タブの全モード（AX.25 / 非 AX.25 / gr-satellites / CW）が自動アップロードの対象になった。

### 同じ録音の再デコードで重複しない（2026-10-15）

同じ IQ 録音を再デコードするたびに、同じフレームが `telemetry_log` に追加され、さらに SatNOGS へも
**二重に送られていた**（HTTP 201 が重複分だけ返っていた）。`_find_logged_duplicate()` で、
**IQ 録音の再生中に限り**、同じ衛星・同じ内容（AX.25 / 非 AX.25 は `frame_hex`、gr-satellites は
`parsed_json`）・同じ種別で受信時刻が `_DUPLICATE_WINDOW_S`（5 秒）以内の行を既存とみなす。
再生の受信時刻のずれは平均 0.1〜0.7 秒・最大 2.25 秒（実測）。

- `_persist_frame()` / `_persist_gr_row()` は既存行があれば INSERT せずその id を返す。
- `_submit_raw_frame()` は、その受信が既に SatNOGS へ送信済みなら送らない（行に `sent` を付ける）。
- **ライブ受信では判定しない**（同じ内容が続けて来ても本物の別受信）。5 秒より離れた同内容も別受信。
- CW は別の重複判定（`cw_upload`）があるので対象外。
- 既に溜まっている重複行は整理していない（削除は別途ユーザーの了承が必要）。

### OPTISAT（68418）を AX.25 モードの衛星一覧に追加（2026-10-15）

OPTISAT の SATNOGS 送信機は「UHF 4k8 Telemetry」（GMSK・4800 baud）で説明に「AX.25」が無く、
`is_ax25_telemetry_transmitter()` の判定に掛からず AX.25 モードの衛星一覧に出なかった。IQ 録音を
T.MicroSat-1 と同じ AX.25 モードで再生するとデコードできたため、`mode_detection._CONFIRMED_AX25_BAUDS`
（NORAD ID → baud の例外リスト）に `68418: 4800` を追加した。「実際にデコードして確認できた衛星」だけを
入れる方針（baud だけでは AX.25 とは限らないため）。Baud は DB の `baud` 値から自動で 4800 に設定される。

### UNNE-1B（98205）の 200 baud 2FSK テレメトリ — デコーダー（2026-10-18、段階 1 のみ実装）

UNNE-1B（HADES-E2）の「Mode U - 200bps FSK TLM」（436.888 MHz）は AX.25 ではなく、HADES-SA / HADES-R と
同じ AMSAT-EA 「Genesis」系の素の 2FSK（トレーニング `AA..` ＋同期 `BF35` ＋ type/address ＋データ ＋
CRC-16）で、1.6〜2.5 秒のバーストが 30 秒ごとに出る。2026-10-18 01:20Z の IQ 録音から 6 フレームを
CRC 一致で復号して確認した（type 3・4・10・1・5・3。バイト 1〜4 の秒カウンターがちょうど 30 秒ずつ増える）。

- **`src/comms/telemetry/hades_fsk.py`**（numpy のみ・scipy 不要）: `HadesFskStream.feed(iq)` が `HadesFrame` を返す。
  デスクランブラーは gr-satellites の `hades_descrambler` 相当（各バイトの下位 1 ビットは未スクランブルで
  シフトレジスタも進めない、という AMSAT-EA 参照コードのクセごと）。CRC はスクランブルされたバイト列に対する
  CRC-CCITT-FALSE。長さは種別ごとの固定値（`HADES_R_LENGTHS`、CRC 込み。type 1・3・4・5・10 は実機で確認）。
- **キャリア探索**: 新規打ち上げ衛星の TLE は大きくずれる（UNNE-1B は 2026-10-18 時点で衛星が TLE 予測より約 80 秒進んでいる
  ＝ドップラー補正後の中心から −3〜−11 kHz のずれ。同じ打ち上げの HADES-L（98206）の TLE のほうが実測によく合う）。SatNOGS 局はこの帯域外で取れなかった
  （CSV 上 10/17〜18 の正規フレームはほぼ 0 件）。ここでは 10 秒窓の帯域電力探索（3.5 kHz 幅、中心から
  ±20 kHz）でバーストごとに搬送波を見つけてから復調する。TLE が古くても取れるのが目的。
- **復調の改善**: 弱いバーストでは判別器出力の 1 点サンプルだと孤立ビット誤りが出る。シンボル中央 60 % の
  平均で判定し、タイミングも少しずらした複数の候補ビット列を試して CRC が通ったものを採る
  （実録音で type 4・5 が修復なしで通った）。**1 ビット修復したフレームは `repaired=True` で、検証済みとして
  扱わない（SatNOGS へ投稿しない）**。種別バイトの置換は行わない。
- **投稿するデータの形**: SatNOGS network に載っているのと同じ「デスクランブル済み・CRC 除去済み」の
  `payload`（K8DP 局の type 3 フレームと構造一致を確認）。
- **Telemetry タブの新モード「HADES FSK」（段階 2、2026-10-18 実装）**: モードコンボの 4 番目（`_MODE_HADES`）。
  衛星コンボは `mode_detection.is_hades_fsk_transmitter()`（`HADES_FSK_NORADS` ＝ 実際にデコードできた衛星だけ。今は
  UNNE-1B 98205 のみ、かつ SatNOGS の送信機が FSK・200 baud）を DB から探す（他モードと同じ「DB に行がある衛星だけ」規則）。
  ▶ Start は SDR パイプラインを `subscribe()` する `HadesFskSdrDemod`（`hades_fsk_sdr.py`。`FskSsdvSdrDemod` と同じ
  QThread 構造）を起動する。フレームは表に `[種別] uptime N s  ±X.X kHz  16 進` の形で出し、`telemetry_log` に
  `kind='hades'`（`frame_hex`＝デスクランブル済みペイロード・CRC なし）で保存する（「Load saved」で再表示、IQ 録音の
  再生では同じ受信を重複登録しない）。受信時刻は「処理完了時刻 − バーストからの遅れ」（窓が閉じるまで最大約 10 秒遅れる
  ため）。1 ビット修復フレームは薄い表示で「修復済み・未検証」と明記し、件数に数えない。
  AX.25 用の Baud / ログボタンはこのモードでは隠す。SatNOGS 投稿のボタン（Upload / API / Send selected）は
  他モードと同じに出す（下記「SatNOGS 投稿」）。
  `main_window._select_telemetry_satellite()` の `mode == "hades"` 分岐で、Radio Control の送信機も 200 baud FSK の
  ものに合わせる。`hades_fsk.py` のデシメーションは多段（各段 10 以下）のポリフェーズ FIR で、2.4 Msps のような高い
  レートでも軽い。実録音を `SdrFileDevice` ＋ `SDRPipeline` ＋ タブで再生して、2 フレームが正しい時刻で表に出て DB に
  保存されることを確認した（2026-10-18）。
- **SatNOGS 投稿（段階 3、2026-10-18 実装）**: CRC 一致のフレームだけを `_submit_raw_frame()`（他モードと同じ経路・同じ
  フッターの Upload スイッチ／API キー／コールサイン／座標の条件）で SatNOGS DB へ送る。送る `frame` は
  **デスクランブル済み・CRC 除去済みのペイロード**（SatNOGS Network の「Data」と同じ形）。1 ビット修復フレームは
  表示のみで投稿せず、「Send selected」の対象にもならない（行に `_Ax25Row` を付けない）。「Send selected」は
  AX.25 と同じく確認ダイアログの後 `force=True` で送る。保存済みフレームを「Load saved」で戻しても、
  生バイトと送信済みの状態が残る（`frame_hex`・`satnogs_uploaded_at`）。IQ 録音の再生で同じ受信を
  二重に送らない（既存の `_find_logged_duplicate()`）。**ユーザーが Upload を ON にして API キーを入れない限り何も送られない**
  （既定 OFF）。
- **「Fast」デコード（録音ファイル全体を一度に、2026-10-18 実装）**: HADES FSK モードの「📂 Decode IQ Recording…」の右に
  「速度: 通常（実時間）／高速」のプルダウン（HADES FSK のときだけ表示、選択は `app_settings` の
  `telemetry_decode_speed` に保存）。「通常」は従来どおり SDR Control で録音を開き、実時間で再生しながらデコード
  （スペクトログラムをクリックして開始）。「高速」はファイルを選ぶと `HadesFileDecoder`（`hades_fsk_sdr.py`）が
  SDR パイプラインを通さず、ファイルを直接 `HadesFskStream` に流す。実録音（452 秒・約 900 MB）が **約 5 秒**。
  受信時刻はファイル名の開始時刻 ＋ バーストの位置。ファイル名に時刻がない録音は表示・保存はするが SatNOGS へは送らない
  （ステータスに明記）。同じ録音を再処理しても `telemetry_log` に二重登録せず、二重送信もしない（既存の
  `_find_logged_duplicate()` を高速デコード中も有効にした）。■ Stop で中止、実行中は ▶ Start と Decode ボタンを無効にする。
  **WAV の読み方**（`comms/telemetry/iq_wav.py`）: チャンクを順にたどる（`fact` があるとデータは 56 バイト目から。
  44 固定にすると I/Q が入れ替わる）。float32 ステレオと 16 ビット PCM ステレオに対応。通常再生が実時間なのは、
  他のデコーダー（Direwolf など）が実時間を前提にするため。**gr-satellites モードの高速化は 2026-10-19 に実装**
  （下の「gr-satellites モードの Fast デコード」節）。
- **「全項目デコード結果」タブ（2026-10-18 実装）**: `src/comms/telemetry/hades_fields.py`。AMSAT-EA の仕様書
  「MARIA-G, UNNE-1, HADES-R, HADES-ICM — Description of transmissions」V1.04（2025-01-06、
  https://www.amsat-ea.org/app/download/13945587/AMSAT+EA+-+Transmissions+description+for+MARIA-G_UNNE-1_HADES-R_HADES-ICM.pdf）
  の表をもとに、種別 1・2・3・4・5・8・9・10・12・14 の全項目を表にする（HADES FSK モードのときだけ。種別ごとに 1 サブタブ。
  ライブ受信・高速デコード・Load saved・行の選択のすべてで埋まる。選んだ行は、その行自身の値を表示する。1 ビット修復フレームは
  ライブでは表を更新しない）。`TelemetryFrame` / `TelemetryField` を使い、既存の `_update_decode_tab()` をそのまま使う。
  - **12 ビット項目の詰め方は仕様書の表どおりではない**: 16 ビット語を下位バイト先に送り、その語の並びを MSB 先頭の
    ビット列として読む（`hades_fields._packed()` ＝ 領域のバイトを 2 バイトずつ入れ替えてから MSB 先頭で切り出す）。
    電源（種別 1）の 14 バイトは vbus1・vbat1・vcpu・vbus2・vbus3・vbat2（各 12 ビット）＋ 4 ビット空き＋ibat・icpu・ipl（各 12 ビット）。
    統計（種別 4）の先頭は 5 バイト（vbus1 12・vbat1 12・vcpu 12・空き 4）。仕様書の「vbus2 は 16 ビット」は実際は 12 ビット。
  - **換算**（SatNOGS Network の 667 フレームで検証。全電圧が互いに 4.05〜4.14 V で一致、日照時は充電電流が負、温度は妥当、
    稼働秒カウンターは 30 秒刻み）: vbus1・vbat1 ＝ ADC×1.4 mV、vbus2・vbus3・vbat2 ＝ ×4 mV、vcpu ＝ 1210×4096/ADC mV
    （基準電圧との比。2.83 V と出るが仕様未確認）、ibat は符号付き 12 ビット（負＝充電）、パネル電力 Spa〜Spd・Spi は
    バイト値×2 mW（SatNOGS デコーダー `hadesr.ksy` に合わせた。仕様書は「mW」だけ）、温度 ＝ バイト/2 − 40 °C（255 ＝ 未使用・
    エラー）、統計の電圧バイトは ×64 mV、最大 ipl は ×4、信号レベルは ×0.5 dB、時系列（種別 14）の vbat1 はバイト×16×1.4 mV。
    ビット詰め・換算の確認には SatNOGS の `hadesr.ksy`（AGPL）を**参照した**が、コードは移植せず、実データで自分で検証した。
  - **種別 15 は音声（2026-10-18 解析）**: UNNE-1B では仕様書の「SMART-IR 実験」ではなく、**CODEC2 700C の音声メッセージ**
    （HADES-SA の種別 11 と同じ形: 連番 1 バイト＋35 バイト。35 バイトは `comms.voice.hades_voice.MASK` と XOR されている。
    37 パケット・約 14.8 秒）。SatNOGS が集めた 325 フレームを連番ごとに多数決で組み立てて復号したところ、**スペイン語の「ドン・キホーテ」
    第 1 章の朗読**（Whisper で認識）だった。長さは UNNE-1（アドレス 12）で **CRC 込み 39 バイト**（HADES-R は 41）なので、
    `hades_fsk.packet_lengths(address)` でアドレスごとの長さ表にした（SatNOGS の 37 バイト・ペイロードから逆算。実受信での
    CRC 確認は未）。表示名は「Voice」、「全項目デコード結果」の「15 Voice」に連番・メッセージ内の開始時刻・マスクを外した
    CODEC2 データを出す。**音声は Digital Voice タブで再生する**（2026-10-18 実装）: 検証済みの音声パケット
    （修復フレーム・他衛星のアドレスは除く）を Telemetry タブが Digital Voice タブへ渡す。受信または高速デコード 1 回につき最初の
    パケットで、Digital Voice タブを**今のタブの後ろに自動で開き**（`MainWindow._on_telemetry_voice_tab_requested()`）、
    以降の各パケットを `DigitalVoiceTab.add_external_packet(番号, マスクを外した 35 バイト, ストリーム時刻[秒])` で渡す。
    再生・Whisper 文字起こし・WAV 保存はそのタブの既存ボタン。組み立ては `VoiceMessageAssembler.add(..., trusted=True)`:
    UNNE-1B の音声は CRC で検証済みで 1.5 秒間隔（HADES-SA は CRC なし・0.4 秒間隔）なので、**パケット番号を受信時刻から補正しない**
    （`VoiceMessage.trusted`）。衛星は 37 パケットを繰り返し送るので、同じ番号は最初の 1 つを残し、取りこぼしは次の周で埋まる
    （SatNOGS の実フレームで、5 つに 1 つ欠けた 1 周目＋足りない分の 2 周目から 37/37・14.8 秒を確認）。信頼できる番号のパケットと
    そうでないものは同じメッセージにしない。このときの受信が実機で音声になるかは未確認（今回の録音に音声のパスがなかった）。
  - **種別 6（太陽センサー）**: 16 ビット語は**上位バイト先頭**（他の種別と逆）。観測間隔 6 個（秒。実データで 4・8・64・128 と
    出て仕様書の 1〜128 と一致）、センサー 8 個×サンプル 6 個の光量、ピーク 8 個、エラーコード 8 個（実データではセンサー 2 と 7 が
    常に非 0 で値は 0）。仕様書のセンサー名（SPA・SPB・SPC・SPD・90A・90D の 6 つ）と 8 個の対応が不明なので「センサー 0〜7」・
    生の値で出す。
  - **デコードしないもの**: 種別 11・13（種別 7 は 2026-10-22 に追加。下の「種別 7」参照）。種別 8（展開）と 9（拡張電源）は単位が仕様書にないので
    生の値、種別 12（軌道要素）は符号化の記述がなく UNNE-1B は今のところ 0 しか送っていないので生の 16 進。
    統計（種別 4）の最小 ipl は衛星が 1 mA のまま更新しない（最大より大きい）実データの癖があり、そのまま表示する。
  - 初期の確認で、仕様書が「Sclock」とする 32 ビットが実は稼働秒そのもの（Status では Uptime と同じ値）だと分かった。
- **種別 7（ICM ゲームメッセージ）を追加（2026-10-22）**: SatNOGS の `hadesr.ksy` / `hadesicm.ksy` の `icmgamemsg_frame`
  （時計 u4・メッセージ番号 u1・UTF-8 の文字列を末尾まで。CRC 込み 101 バイト＝ペイロード 99 バイト）。当初は UNNE-1B が送らないので
  外していたが、HADES-L など ICM 系は送る。HADES-SA の表（`TYPES_SA`）には入れない。SatNOGS の定義のうち、アプリに無かったのは
  この種別だけ（0xA `nebrijagame_frame`・0xB `fraunhoferexp_frame` は ksy が空、0xF `experiment_frame` は本アプリでは音声/BBS として
  定義済み、`hadesd.ksy` は種別 1〜5 のみ）。**HADES-L の 2026-10-22 の録音の 4 フレームはすべて種別 7 だが、ビット訂正（`repaired`）で
  CRC を通したもので、時計が飛び文字列も読めず、誤訂正の疑いが強い**。実フレーム未確認。
- **HADES-SA（68446）を HADES FSK モードに追加（2026-10-18）**: UNNE-1B と同じ「HADES FSK」モードの衛星コンボに HADES-SA
  が出る（`HADES_FSK_NORADS`＝{98205, 68446}。HADES-SA の送信機は SatNOGS の「FSK 800 bps 1600 Hz separation」＝モード
  `GENESIS FSK`・800 baud。Digital Voice タブが使うものと同じ。Radio Control の送信機も自動でそれに合う）。UNNE-1B は 200 baud の
  バースト、HADES-SA は **800 baud・トーン間隔 1600 Hz の連続信号**で、フレーム形式も違うため、復調器は別:
  - **復調**: `comms/telemetry/hades_sa_fsk.py` の `HadesSaFskStream`（`HadesFskStream` と同じ `feed()`/`flush()`/`reset()`
    で `HadesFrame` を返す）。SSDV モードと同じ 2FSK の前段（`comms/sstv/fsk_ssdv.py`: 12.5 kHz への間引き・トーン対の探索・
    シンボルごとのトーンのエネルギー・パケットごとのタイミング再調整）を使う。6 秒窓・3 秒送り。短いバーストは窓の中で
    トーンが弱く見えるので、トーン探索の閾値は SSDV（5 倍）より低い 3 倍。`HadesFskSdrDemod` / `HadesFileDecoder` は
    `norad` を受け取り `make_stream()` で選ぶ（68446 なら HADES-SA、それ以外は 200 baud）。タブは `_start_hades()` /
    `_start_fast_decode()` で衛星の NORAD ID を渡す。
  - **フレーム**: 同期語 `BF35` の次に **size バイト**があり、その後が type/address（UNNE-1B の形式では size が無く、同期語の直後が
    type/address）。size は type ごとの固定値（`HADES_SA_LENGTHS`、CRC 込み。type 3 は 41、15 は 73 で HADES-R と違う）。
    スクランブラー・CRC は HADES-R と同じ（`parse_packet(raw[1:], 3)`）。type 10（SSDV）・11（CODEC2 音声）・13（PN9）は
    テレメトリーではないので対象外（それぞれ SSTV/SSDV・Digital Voice タブの担当）。
  - **全項目デコード結果**（`hades_fields.py`）: `TYPES_SA`（address 3 で選ぶ）。種別 1・2・4・5・8・9・12・14 は HADES-R 系と
    同じ表で、SatNOGS Network の HADES-SA のフレーム（2026-10-11〜18 の 1 週間: 全 266 フレーム）で、電圧が互いに 3.9 V 台で一致、
    充電電流は負、温度が妥当、時系列の 6 種の変数が妥当、を確認した。**種別 3（状態、39 バイト）と 15（BBS）は HADES-SA 専用**:
    状態は仕様書の順（sclock・uptime・nrun・npayload・nwire・ntransponder・4 つのニブル・systems_status・…・
    payload_frames・payload_params・current_image_id）。実データでは `current_image_id`（48、その後 64〜67）が SSDV の
    画像 ID と、`payload_frames`（image 48 のとき 34）がその画像のパケット数（34）と一致した。**BBS（種別 15）は仕様書の表では
    1 件ずつ（コールサイン・メッセージ・フレーム数）だが、実際のパケットはブロック**: コールサイン 5×6 文字、メッセージ 5×7 文字、
    CODEC2 フレーム数 5×1 バイト（見えたものはすべて空の BBS ＝「------」「No data」・0。埋まった BBS のレイアウトは未確認）。
    「全項目デコード結果」のタブには SSDV（10）も UNNE-1B の音声（15 Voice）も出ない（`type_tabs(3)`）。
  - **実受信**: 2026-10-12 の録音（671 秒）の Fast デコードで、526.1 秒の BBS データ 1 パケットを CRC 一致で復号（12 秒。
    手元の別解析と同じ）。**HADES-SA の電圧・温度などのテレメトリーが入った録音はまだ無い**ので、種別 1〜5・12・14 の
    復調は合成 I/Q と SatNOGS のフレームでの確認まで。
  - テスト: `tests/test_hades_sa.py`（実フレームの項目・BBS・長さ表・合成 I/Q）、`tests/test_telemetry_tab_hades_sa.py`。
- **HADES-L（98206）を HADES FSK モードに追加（2026-10-20）**: 衛星コンボに HADES-L が出る（`HADES_FSK_NORADS`＝{98205, 68446,
  98206}、送信機は SatNOGS の「Mode U - 800bps FSK TLM」＝モード `FSK`・800 baud、436.665 MHz。`is_hades_fsk_transmitter()`）。
  **実録音（2026-10-20 12:55 UTC、IQ 634 秒）に 3 つのバースト**があり、すべて復号できた:
  800 baud・トーン間隔 1617 Hz の 2FSK、長さ 0.5〜1.5 秒の短いバースト（20 秒ごとのスロット。見つかったのは約 60 秒間隔の 2 つ＋
  その 20 秒後）。HADES-SA と同じ形式（トレーニング `AA..`・同期語 `BF35`・size バイト・type/address・スクランブル・CRC）だが、
  **アドレスは 5**。種別 4（電源統計、33 バイト）と 8（展開、29 バイト）は受信したまま CRC が一致（検証済み）、種別 9（拡張電源、
  121 バイト）は 1 ビット訂正して CRC 一致（`repaired`）。種別 4 は稼働 1,521,111 秒（約 17 日 14 時間）、バッテリー電圧 3.99〜4.47 V
  などで物理的に妥当。信号は中心から −4.5 k → −10.8 k → −12.4 kHz にずれていた（TLE 誤差。−30〜−100 Hz/s で動く）。
  - **既存の `HadesSaFskStream` では 0 フレームだった**（アドレス 3 で絞るのに加え、連続信号用の 6 秒窓＋FM 検波では 0.5 秒の弱い
    バーストのトーン検出・同期探索が通らない）。そこで**別の復調器** `comms/telemetry/hades_l_fsk.py` の `HadesLFskStream`
    （`HadesFskStream` を継承。200 baud 用のバースト検出＝中心から ±20 kHz の最強 3.5 kHz 帯を使い回す。検出の窓 0.6 秒・
    しきい値 2 dB に下げた。`HadesFskStream` に `_burst_window_s`/`_min_snr_db` の属性を追加）:
    ① バースト区間のスペクトルから**トーン対**（約 1.6 kHz 離れた 2 本）を探す ② シンボルごとのトーンのエネルギーを積分
    （整合フィルター。FM 検波より高感度）③ シンボルタイミング（20 段階）・トーンのずれ（±50 Hz）・ドリフト（±100 Hz/s）・
    極性を総当たり、同期語（≤1 ビット誤り）→ CRC で判定。まず「ずれ・ドリフトなし」で試し、同期語がまるで見えなければ雑音として
    打ち切る ④ CRC が通るものが無ければ、**同期語が完全一致でトレーニングが交互になっている候補**の信頼度の低い 16 ビットのうち
    最大 3 ビットを反転して CRC が通るものを `repaired` で出す（アップロードしない）。
  - **size バイトは使わない**: CRC の対象外で 1 ビット誤りで値が変わる（実録音の 1 つで 35 が 3 に化けていた）ので、長さは type の
    ニブルから表で決める（HADES-R と HADES-SA の両方の表を試し、CRC が決める。HADES-L の type 3・15 の長さは未確認）。
  - **全項目デコード結果**: アドレス 5 は HADES-R 系の表（`TYPES`）。種別 4・5・8・9 などは HADES-R 系と同じとみなして表示するが、
    **種別 9 の値は HADES-SA 同様に単位不明で、実データでは意味のある値か判断できていない**。種別 3 は HADES-R（29 バイト）か
    HADES-SA（41 バイト）の形か不明。CSV 取込（`_import_hades_frame`）も HADES-L はどちらの長さでも受ける。
  - **取りこぼし**: 同じ録音で 142 秒付近にも弱いバースト（3.5 dB、サイズ byte 27・種別 5 らしい）があるが、3 ビットまでの訂正では
    CRC が通らなかった。`make_stream(rate, 98206)` が `HadesLFskStream` を返し、`HadesFskSdrDemod`（実時間）と `HadesFileDecoder`
    （Fast）の両方に効く。処理時間は 634 秒の録音で約 25 秒。
  - テスト: `tests/test_hades_l.py`（実フレームの項目・合成 I/Q のバースト・size バイトの誤り・他アドレス・雑音・ビット訂正の範囲）。
- **CSV 取込（2026-10-18）**: Telemetry タブの最下部の行の右の並びは「保存済みを読込／**CSV取込…**／クリア」
  （「CSVエクスポート」は使わないので外し、「ログをクリア」は「クリア」に改名。表の CSV 書き出し `_on_export_csv()` も削除）。
  SatNOGS DB の衛星ページの「フレームのダウンロード」の CSV（1 行 `日時(UTC)|フレームの16進||観測局`。新しい順。
  `comms/telemetry/csv_import.py`）から、**選択中の衛星**のフレームを表とログ（`telemetry_log`）に入れる。AX.25 モードと
  HADES FSK モードで使える（gr-satellites・CW TLM では「このモードでは使えない」と出す）。
  - **HADES FSK**: SatNOGS には「デスクランブル済み・CRC なし」のペイロードが入っているので、アドレスが衛星のもの
    （HADES-SA は 3、UNNE-1B は 12）で、長さが「パケット長 − CRC の 2」のものだけを取る（`_import_hades_frame()`）。
    音声（UNNE-1B の種別 15）と、HADES-SA の SSDV・音声・PN9 は取らない。実データ（HADES-SA の 1 週間・4,776 フレーム）で
    246 行（種別 1・2・3・4・5・8・9・12・14・15）、約 5 秒。搬送波のずれは CSV に無いので行に出さない。
  - **AX.25**: 各フレームを `_on_ax25_frame()` に通し、受信時刻をファイルの時刻にする（`_frame_time()` が `_import_time` を返す）。
  - **共通**: 古い順に入れる。複数の局が同じ送信を報告した重複（同じ時刻・同じバイト列）は 1 つにし、すでに表にある受信
    （同じ CSV の再取込）は足さない（ログの `frame_hex` と時刻で照合）。**SatNOGS へは送り返さない**（`_submit_raw_frame()` が
    取込中は何もせず、行を「送信済み」にする。ログの `satnogs_uploaded_at` も入れるので、「保存済みを読込」で戻しても
    「選択分を送信」の対象にならない）。
  - テスト: `tests/test_telemetry_tab_hades_sa.py`。
- **解析の落とし穴（2026-10-18 に発覚）**: レコーダーの `.iq.wav` には `fact` チャンクがありデータは 56 バイト目から
  （44 バイト固定で読むと I/Q が入れ替わり周波数の符号が鏡像になる）。最初の解析はこれで「+3〜+12 kHz・約 68 秒遅れ」と
  誤っていた。正しくは **−3〜−11 kHz・衛星が TLE より約 80 秒進んでいる**。
- テスト: `tests/test_hades_fsk.py`（実フレームのゴールデン・合成 IQ でのキャリアずれ・チャンク分割・ノイズ）。

### 受信フレーム表の列幅（2026-10-19）

「時刻／コールサイン／衛星」の 3 列は内容に合わせて広がり、データ列が極端に狭くなっていた（行が非常に縦長に
なった）。3 列を `Interactive`（ヘッダーの境界をドラッグで変更可）にし、初期幅は「`2026-10-19`」「`★ Lobachevsky `」
の文字幅＋余白にした。時刻はスペースで 2 行、衛星名・コールサインも空白で折り返り、データ列（Stretch）が
残りの幅を使う。全モード共通（AX.25・gr-satellites・CW・HADES）。

### LASARsat（NORAD 62391）の AX.25 テレメトリー（2026-10-20）

SatNOGS の `lasarsat.ksy` に従い、`src/data/telemetry_formats/62391.json` を Marina と同じ `csv_messages` 形式で追加した。
先頭タグ `OBC`/`PSU`/`SOL`/`MGS`/`DOS`/`NAV`/`U`/`V` の全項目（PSU のチャンネルビット・SOL の `nan`=-32768 を含む）。
ksy の `to_i` と同じく符号なしで読むので、NAV の位置の負値は 4290989321 のような大きな数になる（ksy 通り）。
- 最後の項目が NUL 終端のため `decode_telemetry()` の csv 経路で末尾 `\x00` を除く（これが無いと最後の項目が欠ける）。
- 上のどれでもないフリーテキスト（"Celebrating 10 000 orbits!" や他局のデジピートメッセージ）は、形式ファイルの
  `text_message` 定義で `MSG` サブタブに入る。
- 実録音（2026-10-19 16:12 UTC、9600 baud）の Fast デコードで 3 フレーム（MSG・DOS・NAV）を項目に分解できた。
  項目の単位は ksy に無いので「(生値)」と表示。CW ビーコン（`de ok0lsr = u…`）は別途 CW 側で対応する。
- テスト: `tests/test_telemetry_decoder.py`（`test_lasarsat_*`）。

### CroCube（NORAD 62394）の AX.25 テレメトリー（2026-10-22）

SatNOGS の `crocube.ksy` に従い `src/data/telemetry_formats/62394.json` を LASARsat と同じ `csv_messages` 形式で追加した
（MGS 16 項目・OBC 6・PSU 11＋チャンネルビット・SOL 12・ATR 9・U/V 各 13、フリーテキストは `MSG`、CW ビーコン
`de 9a0cc = u…r…t…p… ar` は `cw_frames`）。単位は ksy に無いので「(生値)」表示。
**実フレームでは未確認**: 手元の CroCube 録音 2 本（2026-10-21）で gr-satellites が出した各 1 フレームは、AX.25 の
コールサイン欄が壊れており（`9A0CC`/`CQ` ではない）、ksy のどの型にも当てはまらない。テストは ksy の並びに沿った
作り物のフレームで行っている。実機の正しいフレームが取れたら定義を突き合わせること。

### モードの記憶（2026-10-22）

Telemetry タブのモード（AX.25 / gr-satellites / CW TLM / HADES FSK）を `app_settings` の `telemetry_mode` に保存し、次の起動で
戻す。従来はモードごとの「最後の衛星」だけを覚えていて、モード自体は毎回 AX.25 に戻っていた。タブの構築中（復元・gr-satellites が
無いときの強制切り替え）は保存しない。gr-satellites が使えないときは保存値があっても復元しない。

### gr-satellites の雑音フレームを除外する（2026-10-22）

CRC-16 は雑音でも約 1/65536 の確率で偶然一致し、雑音にはフラグ様のパターンが数万個あるので、データの無い
長い録音でも gr-satellites が 1 フレームほど「デコード」することがある（CroCube の無変調キャリアだけの録音 2 本で
実際に各 1 フレーム。アドレスは文字化けした 1 件・SSID の終端ビットだけ立つ）。`telemetry_tab.gr_packet_is_bogus()` が
出力に `callsign =` があるのにアドレスが 2 件未満、または A-Z/0-9 以外を含むものを雑音と判定し、**行を出さず・
`telemetry_log` に残さず・SatNOGS にも送らず**、ステータス行に「⚠ …無視しました（N件）」と出す。
アドレスの無い出力（AX.25 として解析されない枠組み）は判定しない。
- 副作用: SatNOGS への自動アップロードは、フレームの KISS 出力が先に来た場合に**その文章が来るまで保留**する
  （文章が来ないままなら 5 秒後に送る）。文章が先なら従来どおり即送信。
- Direwolf・コヒーレント MSK 経路は対象外（`decode_ax25()` が元々不正なアドレスを弾く）。

### CANVAS（NORAD 68635）の CCSDS テレメトリー（2026-10-22）

SatNOGS の `canvas.ksy` に従い `src/data/telemetry_formats/68635.json` を `frame_types` 形式で追加した（AX.25 の info 先頭が
CCSDS スペースパケット。APID `0x20` のビーコン 124 項目＋CCSDS ヘッダー）。**ksy が定義するのは APID 0x20 のビーコンだけ**。
単位は ksy に無いので生値。基板温度は ksy が `(生値 >> 4)` の符号付き 12 ビット。
- 実際に受けたのは、PARUS-6U1 の録音（2026-10-21、437.250 MHz・9600 baud GMSK、中心から +55 kHz）に写っていた
  `CANVAS-0 ← LASP-0` のフレーム（APID **5**、1 秒周期、連番とタイムスタンプが 1 秒ごとに 1 ずつ増える）。ヘッダーまでしか
  読めず（先頭約 30 バイトだけ信頼でき、その先はビット誤りで CRC が通らない）、APID 5 は ksy に無いので `CCSDS`
  タイプ（ヘッダーのみ）として表示する。ビーコン（APID 0x20）の定義は実フレーム未確認。
- 弱い信号のビット誤り訂正（弱いシンボルを反転して CRC を探す）は、同じバーストから**内容の違う CRC 通過フレームが
  複数出る**ため偽の一致が避けられず、採用しなかった。

#### LASARsat の CW ビーコン（2026-10-20）

`de ok0lsr = u<累積稼働時間>r<リセット回数>t<MCU温度>p<PA温度> ar`（`lasarsat.ksy` の `cw_message`）。16進ではなく
**10進テキスト**なので、`cw_frames` に `text_regex`（`id_prefix` の後ろ全体に fullmatch。正規化後は大文字・空白なし）と
フィールドごとの `group` を使う形を追加した（`cw_frames._decode_text_frame()`）。桁修復・SatNOGS アップロードは
なし（`satnogs` キーを書いていない）。ksy は読めない桁を `.` にして続行するが、こちらは正規表現に合わない読みを
`near_regex` で「棄却候補（グレー行・?）」にするだけでデータには使わない。実機の CW 受信では未確認（IQ 録音には
CW ビーコンが無かった）。テスト: `tests/test_cw_frames.py`（`test_lasarsat_cw_*`）。

LASARsat の `MSG` サブタブ（デジピートを含むフリーテキスト）には、AX.25 ヘッダー（宛先・送信元・経由局（`*` は中継済み）・PID）を
先に表示する（形式ファイルの `text_message.header_fields`。`decode_telemetry(..., ax25=Ax25Frame)` を受信時・行の再デコード・
保存済み読込の 3 経路から渡す）。ksy は経由局の ssid/hbit を別項目にするが、ここでは `OK0LSR*` の 1 文字列にまとめた。

### Foresail-1p（NORAD 66778）の Skylink/PUS テレメトリー（2026-10-20）

9k6 GFSK・AX100 の ASM フレーム（CCSDS スクランブラ）を gr-satellites（`FORESAIL-1P`・仮 NORAD 98467）が出す
PDU ダンプ（`VERBOSE PDU DEBUG PRINT`）を、SatNOGS の `foresail1p.ksy` どおりに全項目へ分解する
（`src/data/telemetry_formats/66778.json`、`frame_types` 形式・9 種）。ダンプ全体（認証タグ 4 B まで）が
`decode_telemetry(…, norad=66778)` に渡る（`_decode_gr_text()` → `parse_gr_pdu_dump()`）。

- フレーム構造: Skylink ヘッダー（識別子 `OH2F1S`・フラグ・連番・拡張 5 B）→ CCSDS PUS ヘッダー（9 B）→ 本体。
  本体の先頭の時刻は**ビッグエンディアン**の UNIX 秒、以降は**リトルエンディアン**（ksy の `meta: endian`）。
  `frame_types` は PUS のサービス種別（オフセット 23 の 2 バイト）で選ぶ: OBC `0302`・EPS `0303`・UHF `0304`・
  ADCS `0305`・イベント `0401`〜`0404`。VC 3 の中継フレーム（AX.25）はフラグ byte の下位 2 bit で選ぶ（`REPEATER`）。
- **オフセット固定の前提**: Skylink 拡張長は録音した全フレームで 5 B。長さが違うフレームは一致せず生 HEX になる。
- **エンジンへの追加**（`decoder.py`）: `match` に `mask`（指定ビットだけ比較）、フィールドに `to_end`/`trim_end`
  （末尾の認証タグ等を除いて最後まで）、型 `hex`・`base64`・`ax25_call`（各バイトを 1 ビット右シフトして ASCII）・
  `quat5`（5 B の「最小 3 成分」圧縮クォータニオン。`component` = q1/q2/q3/index/sign/largest。q は 12 bit を ±1/√2 に
  写像し、largest は他 3 成分から単位長になるよう計算）・`int32_be`。
- **実フレームでの検証（2026-10-18 01:32 UTC の録音・2 フレーム）**: ADCS（本体 83 B がぴったり・MJD 61331.0625
  = 2026-10-18 01:30 UTC・ADCS オフで位置/速度 0・クォータニオンは単位）と UHF（9600 baud・窓 250 ms・
  ブート回数など妥当）。**UHF は ksy にない 2 バイトが `total_ham_rx_frames` と `hardware_side_a_bootcount` の間にある**
  （実フレームの長さ 60 B ＝ ksy 58 B ＋ 2。`unknown_u16_after_ham_rx` として表示。値 21096）。
- **疑わしい値**: UHF の RSSI（-55/8/0 dBm）、ADCS の磁気センサー（約 12000、ksy の単位 µT なら地磁気の桁違い）。単位は ksy
  どおり表示するが信用しないこと。EPS の電圧・電流のうち ksy に単位が無いものは単位なし（生値）。
- OBC・EPS・イベント・REPEATER は実フレーム無し（ksy のみ・JSON の `note` に明記）。
- テスト: `tests/test_telemetry_decoder.py`（`test_foresail1p_*`・`test_payload_match_mask_*`）。
- **背景**: GPD で Foresail-1p が読めなかった原因は、アプリが使う gr-satellites が apt 版（NumPy 不整合で起動せず、
  `FORESAIL-1P` 定義も無い）だったこと。Help > gr-satellites Installation… で同梱版（`~/.local/share/fbsat59/
  gr-satellites-env/`）を入れると読める。

### AX.25 モードの 1200 baud「Fast」デコード（2026-10-21）

AX.25（Direwolf）モードの「Speed:」プルダウンと「Decode IQ Recording…」は、4800 / 9600 baud（コヒーレント MSK 検波）に加えて
**1200 baud でも出る**（`_FAST_AX25_BAUDS`＝1200/4800/9600）。UI は 4800/9600・gr-satellites モードと同じ（Fast でファイルを
選ぶと全体を一括デコード、Normal は SDR Control で実時間再生）。実装は `comms/aprs/afsk_file_decoder.py` の
`Afsk1200FileDecoder`（`CoherentMskFileDecoder` と同じシグナル `frame_found(bytes, 秒)` / `progress` / `decoded` / `failed`）:

- **前段**: ライブの SDR 経路と同じ `AfskAudioDiscriminator(satellite=True)`（衛星用 1200 baud 前段: IF ±5 kHz・弁別器 50 kHz・
  包絡線の谷で出力を絞る・±2.5 kHz クランプ・ディエンファシスなし。実録音 OrigamiSat-2 で従来 0〜1 → 20 フレーム）で I/Q を
  48 kHz 音声にする。PCM への変換もライブと同じ（`float × 32767` の 16 bit）。
- **復号は Direwolf**（実績のある復号器を使う方針）: このジョブ専用の Direwolf を起動し（`ADEVICE stdin stdout`・MODEM 1200）、
  音声を標準入力へ**実時間のペースを付けずに**流す（stdin の raw PCM は渡した速さで処理される）。KISS ポートはジョブ専用
  （ライブのセッションと衝突しない）。**Direwolf の KISSPORT は 49151 以下しか受け付けず**（超えると「Invalid TCP port」で
  既定の 8001 に戻る）、OS が割り当てる一時ポートは通常それを超えるので、20000〜40000 から空きを選ぶ。
- **出力デバイスは `null`**（`ADEVICE stdin null`）: GPD の Direwolf は apt の 1.7 で、ライブ経路が使う `ADEVICE stdin stdout` を
  受け付けず「Could not open audio device stdout」で止まり、KISS ポートも開かない（UI は「Direwolf did not open its KISS port」）。
  `null` は 1.7 でも macOS の 1.8.1 でも動く（送信はしないので出力は不要）。実機 GPD で合成 FM-AFSK 4/4・実録音（702 秒）を 44 秒で処理。
  **ライブの `DirewolfManager._write_config()` も同じ設定だったので同日 `stdin null` に直した**（GPD の Direwolf 1.7 で
  実際の設定を走らせ、1200/4800/9600 のどれも「Ready to accept KISS」まで起動することを確認。以前は「Could not open audio device
  stdout」で止まっていた。送信音声は Direwolf を経由しない（ARICA-2・APRS タブは自前の音声）ので影響なし）。
- **フレームの時刻**: Direwolf が KISS で報告した時点までに流した音声の長さ。パイプのバッファ分（1 秒未満）遅れるので **約 1 秒の精度**
  （コヒーレント検波のような厳密な時刻ではない。ファイル名の開始時刻があれば受信時刻の目安として使える）。音声の後ろに 2 秒の無音を
  足して録音の最後のフレームも出す。Direwolf が見つからなければ「Direwolf not found. Use Help > Direwolf… to install.」。
- **速度**: 合成 FM-AFSK（250 kS/s・9 秒）で約 1.4 秒。合成 4 フレームは 25 dB・15 dB とも 4/4（実 Direwolf、macOS）。
- **限界（実測）**: 2026-10-21 の OrigamiSat-2 の録音（14:24 JST 開始）は、この前段＋Direwolf でも 0 フレーム。キャリアとサイドバンドの
  電力から求めた 1 シンボルあたりの Es/N0 が平均 2〜3 dB・最良でも 5〜6 dB（非同期 FSK で誤りなしに取れるのは約 10 dB 以上）で、
  方式の問題ではなく信号が弱すぎる。もう一つの録音（16:09 終了）は変調のない搬送波だけでデータが乗っていない。
- **SatNOGS クライアントには同種の高速デコードは無い**（調査 2026-10-21）: `satnogs_afsk1200_ax25.py` は SDR 入力の実時間
  フローグラフ（FM 復調 → 1700 Hz 中心の 2 回目の弁別 → M&M クロック再生）、`grsat.py` も観測中に IQ を UDP で渡す実時間処理。
- テスト: `tests/test_afsk_file_decoder.py`（Direwolf の代役でのジョブの配管・取消・Direwolf 不在・読めないファイル、Direwolf があれば
  合成 FM-AFSK の実デコード）、`tests/test_telemetry_tab.py`（1200 でも Speed が出る・1200 は Direwolf、それ以上はコヒーレント）。

### KOSTKA（69935）・GRBBeta（60237）の定義追加と `csv_messages` の拡張（2026-10-22）

- `telemetry_formats/69935.json`（KOSTKA / OK0KST）と `60237.json`（GRBBeta / HA2GRB）を追加。どちらも SatNOGS の
  `kostka.ksy` / `grbbeta.ksy`（Kaitai Struct）を参考に書いた（ksy を直接読み込むわけではない）。
- KOSTKA のビーコンは識別子を持たず、**欄の数で見分ける**（9=OBC / 10=SOL / 15=EPS。14 欄で先頭が `U` のものは MRG）。
  そのため `csv_messages` のエントリに `match_count`（全欄が数値であることも条件）を追加した
  （`decoder._csv_message_matches`）。識別子のあるエントリは従来どおり先頭文字列で一致する。
  数値でない 9 欄のテキストはフリーテキスト（MSG）に落ちる。
- 小数の欄用に CSV の型 `float` を追加（KOSTKA の OBC 温度・SOL の温度/電圧）。
- **MRG の `mrg_last_digipeater` は「最後にデジピートした局」**で、他局が実際にデジピートされたかの手掛かりになる。
  2026-10-01〜22 の JJ1WTK ログでは KOSTKA は `digipeater_cnt=6`・最後の局 `JA1HCB` のまま変化なし。
- 単位: SOL の温度（°C）・電圧（V）は ksy の名前と値からの推定。MRG/EPS の温度・電流は単位不明の生値。
- GRBBeta: UHF の `U,` と VHF の `V,` ビーコン（LASARsat と同じ 14 欄）とフリーテキストのみ。ksy にある CSP の
  バイナリ PSU テレメトリーと CW ビーコンは未定義。
- 実ビーコン（JJ1WTK のログ）でのデコードを `tests/test_telemetry_decoder.py` に追加。

### Telemetry タブの「ASCII変換」サブタブ（貼り付け専用・2026-10-22）

- 「Received Frames」「Decoded Fields」に続く 3 つ目のサブタブ（`ui/telemetry_paste.py` の `AsciiPasteWidget`）。
  他局のログ（JJ1WTK の衛星ログなど）の文字列テレメトリーを貼り付けると、1 行ずつ衛星の定義で解読して全項目を
  木表示する。**貼り付けた内容は保存しない**（受信ログ・SatNOGS 投稿には一切入らない）。
- 受け付ける行: `[0 2026-10-20 23:07:35 UTC] OK0KST>CQ:U,9008906,...<0x00>`（`SRC>DEST[,PATH]:` ヘッダー付き。
  先頭の `[...]` の時刻は表示に使う。`<0xNN>` はそのバイトに戻す）、またはヘッダーなしのペイロードのみの行。
- 衛星は、ヘッダーの送信元コールサインを `telemetry_formats/*.json` の `callsign` と照合して自動判別する。
  ヘッダーのない行はタブで選択中の衛星、または上部のコンボで選んだ衛星の定義で読む（コンボが優先）。
- 対象は ASCII（`csv_messages`・`text_message`）の定義を持つ衛星。バイナリのテレメトリーの HEX 貼り付けは未対応。
- 実ログ（JJ1WTK の 2026-10 の 40 行：LASARsat・KOSTKA・MARINA が混在）が全行解読できることを確認済み。

### 未定義だった部分の追加: KOSTKA/MARINA/GRBBeta の CW ビーコンと GRBBeta の電源バイナリ（2026-10-22）

SatNOGS の ksy と JSON 定義を項目名で突き合わせ、ksy にあって定義に無かった**実データ部分**を追加した
（残りの差は AX.25 ヘッダー項目＝`text_message` の `header_fields` で表示済み、`*_beacon_raw` などの再構成値、名前だけの違い）。
**すべて ksy から書いたもので、実フレームでの確認はまだ**（後から確認する前提で先に入れた）。

- **CW ビーコン（`cw_frames.BEACON`）**: KOSTKA（`de ok0kst = u…r…t…p… ar`）・MARINA（`de om9mar = …`）・GRBBeta
  （`de ha2grb = …`、`ar` は任意）。LASARsat/CroCube と同形式。**GRBBeta の累積稼働時間は ksy が「分」なので ×60 で秒に換算**。
  これで 3 衛星とも Telemetry タブの「CW TLM」モードの対象になる（`is_cw_telemetry_transmitter` は `cw_frames` を持つ衛星を拾う）。
- **GRBBeta の PSU バイナリ（`frame_types.PSU`、75 バイト、50 項目）**: CSP ヘッダー（優先度・送信元/宛先・ポート・
  フラグ。送信元 2・送信元ポート 9 を `match`+`mask` で判別）＋ `psu_hk_t`（リトルエンディアン）。電圧/電流/電力は ksy が
  u2 で倍率を書いていないため**生値**、温度はケルビン（生値）。**先頭が CSP ヘッダーである前提**のオフセット
  （AX.25 ヘッダー付きで渡される場合は ksy の `message_type4` と同様に別途判定が要る。実フレーム次第）。
- **デコーダー**: これまで `frame_types` があると他のスキーマ（`csv_messages` 等）は使われなかった。`frame_types` の
  どれにも合わないフレームは他のスキーマへ回すように変更（`decode_telemetry`）。GRBBeta は文字列の U/V ビーコンと
  バイナリの PSU の両方を持つ。
- **確認の手順（実フレーム受信後）**: GRBBeta の PSU は受信フレームの HEX を見て、先頭 4 バイトが CSP ヘッダー
  （`b0 & 0x3E == 0x04`、`b2 & 0x3F == 0x09`）か、長さが 75 バイトか、時刻（RTC）が受信時刻と合うかを確かめる。
  CW は実際の CW デコード出力で正規表現に合うかを確かめる。

