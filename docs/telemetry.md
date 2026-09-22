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
- **gr-satellitesモードでは常にサブタブ非表示**: gr-satellitesは自前で人間可読テキストに
  変換して`_append_row()`に渡す設計（[telemetry_tab.py:793](../src/ui/telemetry_tab.py:793)
  付近の`"-> Packet from"`パース）のため、この機能の対象外。`_on_mode_changed()`で
  `self._log_tabs.tabBar().setVisible(not is_gr)`によりタブバー自体を隠し、gr-satellites
  モードでは今まで通り単一テーブルに見えるようにしている

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

## 入力ソース行の「ⓘ」ボタン — デコードに必要な最低SNRの目安（2026-09-19 追加）

Telemetryタブの「Input Source」枠の1行目（Mode / 衛星 / Baud / 📋 Log の並び）の右端に
`ⓘ` ボタン（`_btn_snr_info`）を置いた。クリックで、速度別（1200 / 4800 / 9600 bps）に
Direwolfとgr-satellitesがデコードできる最低SNR・信号帯域・Direwolfの周波数許容差と、
gr-satellitesの長い前置信号の必要性などの注意を表示する非モーダルウィンドウ
（`_SnrGuideDialog`）が開く（一度開いたウィンドウは再利用）。表示内容は `_SNR_GUIDE_ROWS` と
`_snr_guide_html()`、数値の根拠・測定条件・生データは
[communications.md](communications.md) の「デコードに必要な最低SNRの目安」を参照。
数値を更新するときは両方を同時に直すこと。日本語訳は `locale/ja/LC_MESSAGES/fbsat59.po`
（[i18n.md](i18n.md) の手順）。テスト: `tests/test_telemetry_snr_guide.py`。

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
