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
