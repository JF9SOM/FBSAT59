# Telemetry タブ詳細設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> Telemetry タブ（`src/ui/telemetry_tab.py`）・`src/comms/telemetry/` を変更・調査する前に
> 必ず読むこと。衛星選択コンボの構築方式・自動トランスポンダー選択のスコアリング・
> SATNOGS `status` の意味論について、2026-09-05 に実機報告を受けて調査・修正した内容を
> まとめてある。常時読み込む必要はない。

---

## 全体構成

Telemetry タブは AX.25 テレメトリーフレームを2つの経路で受信・デコードする:

- **Direwolf (AX.25) モード**（`_MODE_AFSK`）— 1200 baud Bell 202 AFSK（`AfskDemodulator`
  または Direwolf） / 4800・9600 baud G3RUH スクランブルド FSK・GMSK（Direwolf）
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
