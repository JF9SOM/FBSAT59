# Doppler 追尾チューニング（RX オフセット / 帯域中心基準 / AO-73 invert）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

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
