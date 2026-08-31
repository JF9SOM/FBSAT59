# リグ機種別 実装ノート

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

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

#### 【GitHub Issue #26】IC-9700 satmode DirectモードでFT4送信がPTTオフに戻らない（v0.3.42〜、調査中）

**症状**: IC-9700・Directモード（satmode、クロスバンド）でFT4タブを使用中、送信サイクルが
終わってもPTTがオフに戻らない（送信しっぱなしになる）ことがあるとの報告（ei4gnb）。

**根本原因の特定**: 診断ログ（v0.3.42、`_log_cat_call_diag()`）を追加して実機ログを解析した
結果、以下の連鎖が判明した:

1. **Hamlib Pythonバインディングは`-threads`なしでビルドされている**
   （`.github/workflows/ci.yml`のHamlib向け`swig -python -Wall`。SoapySDR側の
   `swig -c++ -python -threads`と対照的）。SWIGの`-threads`は、ラップしたC関数呼び出しの間
   `Py_BEGIN_ALLOW_THREADS`/`Py_END_ALLOW_THREADS`でGILを解放する仕組みだが、これが無いため
   `self._rig.set_freq()`のようなブロッキングCAT呼び出しは、その通信往復（実測約200〜266ms、
   IC-9700のSub VFO書き込みは特に高コスト）の間ずっとPythonのGILを握り続ける
2. **satmodeクロスバンドのUL（Sub VFO）書き込み**は、`_tracking_through_tx()`
   （FT4/Q65のようにTX中もドップラー追尾を続けるモード）がTrueの間、通常の20Hz delta閾値
   ではなく**1Hz delta閾値**に締め付けられていた（IC-9100の表示ちらつき対策で通常時は緩めて
   あるものを、送信中の追尾精度優先でここだけ厳しくしていた）。この1Hz閾値が
   DopplerWorkerの毎サイクル（実測で1秒に約3回）ほぼ確実に交差し、CAT呼び出しがほぼ
   絶え間なく発生していた（送信時間の約79〜82%を占有）
3. これが`_TxWorker`のPortAudio音声コールバックスレッド（同じくGILが必要）を慢性的に
   飢餓状態にし、`sounddevice`が"output underflow"を報告、本来約5.04秒のFT4送信が
   10〜32秒以上に間延びし、`done.wait()`（音声完了シグナル待ち→PTTオフ）がなかなか
   戻らない、という一連の症状につながっていた

**これまでの修正（すべてユーザー承認の上で個別に実装・タグビルド済み）**:

| バージョン | 内容 |
|---|---|
| v0.3.42 | 診断ログ追加のみ（`_log_cat_call_diag()`、各CAT呼び出しの所要時間を記録） |
| v0.3.43 | `_TX_BLOCK_SIZE`を240（20ms）→6000（500ms）に拡大。CAT起因のストールを音声バッファで吸収し、underflow自体は解消 |
| v0.3.44 | `_TX_WATCHDOG_S = 7.0`（FT4周期7.5秒に対し）を新設。`done.wait(timeout=7.0)`が自然完了しなくても必ず`set_ptt(False)`を強制送信する安全弁 |
| v0.3.45 | satmodeクロスバンドのUL書き込みを、TX中は「1Hz delta閾値」から**`_TX_UL_MIN_INTERVAL_S = 1.0`秒の時間フロア**に変更（値そのものではなく経過時間で書き込み可否を判定）。CAT占有率を約80%→約25〜50%まで削減 |

**v0.3.45実機テスト後の追加報告と、今回（本セッション）実装した2件の追加修正**:

v0.3.45を試したユーザー（DM、RS-44・仰角17度の低仰角パス）から
「送信までに約1.37秒の遅延があり、送信自体は約6.77秒続く」との報告があった。
ログ（`fbsat59_ft4_decode_*.txt`・`fbsat59_log_*.txt`）を解析したところ:
- `status_flags=['none']`（underflow報告は完全に消えている）・PTTは毎回7秒以内に
  自然に戻っており、**「PTTが戻らない」という当初の致命的症状は解消**していた
- ただし**DL（Main VFO）側は一切スロットリングされておらず、旧来の常時1Hz delta閾値の
  まま**だったため、このパス（仰角17度）のようにDLの自然なドップラー変化率がたまたま
  1Hz/秒前後になる条件では、**DLもUL同様ほぼ1秒おきに書き込まれ**、結果として
  CAT占有率が約47〜50%（UL単体で見込んでいた約25%の倍）に留まっていた
- 500msという大きな音声ブロックサイズ自体も、CAT占有率がここまで下がった今となっては
  過剰で、コールバックそのものに余分なレイテンシ（要求500msに対し実測600〜750ms間隔）を
  持ち込んでおり、これが約1.5秒の余分な送信時間の主因と推定された

**本セッションで実装した2件の追加修正**（次回タグで反映予定）:

1. **satmodeクロスバンドDL書き込みにも、UL同様の時間フロアを追加**
   （`_TX_DL_MIN_INTERVAL_S = 3.0`秒、[controller.py](src/rig/controller.py)）。
   自局の送信中の音声はFt4Tab自身が最初からデコード対象外にしている
   （本ファイル「自局送信中の周期はデコードしない」セクション参照）ため、
   **DL補正はTX中は次のRX期間まで機能上の意味を持たない**——ULより緩い3秒間隔で
   十分という判断（ユーザー確認済み）。`_flush_pending_frequencies()`
   （PTT ON直前の最新値プリフラッシュ）も、`_last_dl_update_time`を`_last_ul_update_time`
   と同様に0リセットしてから呼ぶよう修正し、このフラッシュ自体が新しいDLフロアに
   阻害されないようにした
2. **`_TX_BLOCK_SIZE`を6000（500ms）→3000（250ms）に縮小**（[ft4_tab.py](src/ui/ft4_tab.py)）。
   DL/UL両方のスロットリングでCAT占有率がさらに下がった前提で、実測ベースの
   単発CATコール所要時間（約200〜266ms）は依然カバーしつつ、500ms化で生じていた
   コールバックあたりの余分なレイテンシを半減させる狙い。実音声デバイスでの検証は
   できていないため、次回の実機テストで再検討が必要な暫定値（underflowが再発すれば
   引き上げ、それでも間延びが残るならさらに絞るか、フロア自体をより厳しくする）

**さらに根本的な対策として、Hamlib Pythonバインディングの`-threads`ビルドも今回着手**
（`.github/workflows/ci.yml`のHamlib向け3箇所のSWIG呼び出し全てに`-threads`を追加）。
これが効けば、CAT呼び出しの頻度・所要時間に関わらず、CATの実際の通信待ち区間で
GILが解放されるため、音声コールバックスレッドがCATに飢餓状態にされること自体が
原理的になくなる（頻度ベースの間引きは「対症療法」、こちらは「原因そのものの除去」に
近い）。Hamlib本体（Cライブラリ）には一切手を入れず、既存のCIパイプラインの
SWIGコマンドに1フラグを足して同じ手順で再ビルドするだけで済む。ビルドし直すこと自体は
CIで完結するが、**実機（IC-9700）での検証はまだ行っていない**——SWIGの`-threads`は
一般に安全とされるパターン（純Cライブラリの同期API呼び出し、コールバックでPythonへ
戻らない）だが、実際に動作が変わらないか（特にDL/ULスロットリングとの組み合わせ）は
次回リリースでの実機確認が必要。

**訂正（2026-08-27、下記「`-threads`導入で露見したCAT呼び出しの排他制御漏れ」参照）**:
上記の「このプロジェクトは既にCAT操作全体を`_rig_cmd_lock`で直列化している」という
記述は誤りだった。実際には`set_ptt()`・`set_frequency()`・`set_mode()`等、複数の
公開APIメソッドが`_rig_cmd_lock`を一切取得せずに`self._rig`へ直接CATコマンドを
送っており、今まで安全だったのは`-threads`が無くGILがCAT呼び出しの間ずっと
偶然握られ続けていたことの副産物に過ぎなかった。詳細は次項参照。

**検証状況（2026-08-27時点）**: v0.3.42〜v0.3.45は実機（IC-9700、複数パス）で
underflow解消・PTTタイムアウト内復帰を確認済み。DL 3秒フロア・ブロックサイズ縮小・
Hamlib `-threads`ビルドの3件は本セッションでの実装のみで、**いずれも実機未検証**。
次回タグビルド後、ユーザー（DM・ei4gnb）の次回パスでの確認待ち。

**教訓**:
- 「頻度を落とす」（1Hz delta→時間フロア）という対症療法は、UL単体では効果があっても、
  **同じ問題を抱える別の書き込み経路（今回はDL）が無傷のまま残っていると、その経路が
  偶然にも同程度の頻度で発火し、削減効果を半分帳消しにする**ことがある。ある資源
  （今回はGIL）を競合する複数の書き込みパスがある場合、1つだけ直して満足せず、
  同種の経路をすべて洗い出すこと（本ファイルの「同じ役割の機能が複数ある場合は
  横展開して確認すること」という既出の教訓と同型）
- 「バッファを増やして問題を隠す」対策は、根本原因（CAT頻度過多）を先に軽減しない限り、
  それ自体が新しい—ただしずっと軽い—副作用（コールバックあたりのレイテンシ増加）を
  持ち込むことがある。対症療法を積み重ねる際は、上流の負荷が下がった時点で
  下流の対症療法（バッファサイズ等）を必ず再チューニングする必要がある
- 本質的な原因（GILを長時間握るブロッキングC呼び出し）に対しては、個別の呼び出し
  頻度を下げるより、根本的にGILを解放する仕組み（`-threads`）の方が原理的に優れるが、
  ビルド・検証のコストが高いため、まず実装が容易な対症療法で症状を軽減しつつ、
  並行してより根本的な修正に着手する、という2段構えのアプローチを取った

#### `-threads`導入で露見したCAT呼び出しの排他制御漏れと、`_rig_cmd_lock`のRLock化（2026-08-27）

**発端**: `-threads`ビルド自体の説明をした際、ユーザーから「CATがブロッキングしている間
他のPythonスレッドが動けるようになると、逆に弊害が出ることはないか」という質問があった。
実際にコードを精査したところ、`HamlibDirectController`の公開APIメソッドのうち
`set_frequency()`・`set_mode()`・`get_mode()`・`set_ctcss_tone()`（非satmode分岐）・
`set_dcs_code()`・`set_vfo()`・`set_ptt()`が、いずれも`self._rig`へ直接CATコマンドを
送るにもかかわらず**`_rig_cmd_lock`を一切取得していない**ことが判明した（ロックを
取っていたのは`get_frequency()`と、DL/UL書き込み本体である`_set_vfo_frequencies_locked()`
経由の`set_vfo_frequencies()`のみ）。

**なぜ今まで実害が出ていなかったか**: `-threads`が無い間は、DopplerWorkerスレッドが
`_rig_cmd_lock`を握って`set_freq()`のCAT往復（約200〜266ms）を待っている間、その
ブロッキング呼び出し自体がGILを丸ごと握り続けるため、**他のスレッドはPythonの
バイトコードを一切実行できず**、結果として上記の「ロックなし」メソッド群も事実上
直列化されていた——設計ではなく、GILを握りっぱなしにする副作用による偶然の安全だった。
`-threads`でこの偶然が無くなると、例えばFT4/Q65の`_TxWorker`スレッドが送信開始・終了の
瞬間に呼ぶ`set_ptt()`が、DopplerWorkerスレッドの`set_freq()`と**文字通り同時に**同じ
シリアルポート・同じリグハンドルへCATコマンドを送りかねない。Hamlibは「1コマンド送信→
応答を待つ→次のコマンド」という厳密な順序を前提に設計されているため、これが起きると
CATバイト列の混線・応答の取り違え・最悪の場合C層の内部バッファ破損によるクラッシュが
理論上起こり得る。

**対策**: 上記7メソッドすべての`self._rig.*`呼び出しを`with self._rig_cmd_lock:`で
包んだ。

**`_rig_cmd_lock`を`threading.Lock`から`threading.RLock`へ変更した理由**: 単純に
`Lock`のまま各メソッドをロックで包むと、内部の呼び出し連鎖がデッドロックする。具体的には
`_set_vfo_frequencies_locked()`（`_rig_cmd_lock`を保持したまま実行）の同バンド
フォールバック分岐が`_satmode_exit()`を呼び、`_satmode_exit()`が`self.set_mode()`
（今回ロック対象に追加したばかりのメソッド）を呼ぶ——**同一スレッドが既に保持している
ロックを、そのスレッド自身が再度取得しようとする**構図になる。`threading.Lock`は
非再入（reentrantではない）ため、これは即座にデッドロックする。`threading.RLock`
（再入可能ロック）に変更することで、「同一スレッドからの再取得は素通りさせつつ、
別スレッドからは確実にブロックする」という、まさに欲しかった性質が両立できた。

**ロック拡張は元の問題（Issue #26本体）を再発させないか**: 再発しない。理由は
「待たされる対象が違う」から。今回の対症療法は「DopplerWorkerスレッドが**それとは
無関係なPortAudio音声コールバックスレッド**からGILごとCPU時間を奪ってしまう」という、
音声スレッド側が一切関与しないロックにも触れないリソース飢餓だった。一方ロック拡張で
発生し得るのは「`set_ptt()`を呼んだFT4送信スレッド自身が、たまたまDopplerWorkerが
CAT通信中だった場合に、そのCAT往復1回分（最大約200〜266ms）だけ`_rig_cmd_lock`の
解放を待つ」という、**CAT呼び出しを行っている当事者スレッドどうしの短い順番待ち**に
過ぎない。音声コールバックスレッド自体は`_rig_cmd_lock`に一切触れないため、
`-threads`が効いている限りDopplerWorkerがロックを握っていようが自由に動き続けられる。
「TX全体が継続的に間延びする」という規模の問題と、「PTT ON/OFFの瞬間だけ稀に最大250ms
程度ずれる」という規模の問題は、性質も影響も別物であり、後者は7秒ウォッチドッグは
おろか実運用上ほぼ体感できないレベルと判断した。

**スコープ外にしたもの（意図的、今後の課題）**: `connect()`/`disconnect()`と
DopplerWorkerループの間の競合は、今回のロック拡張の対象に含めていない。これは
「CAT呼び出し中にGILが解放されて別スレッドの呼び出しと重なる」という今回の問題とは
別種の、より以前から存在する潜在的な競合（`disconnect()`が`self._rig.close()`を
呼んだ直後に`self._rig = None`にする間に、別スレッドが`is_connected`チェックを
通過した直後の`self._rig`参照でタイミングが噛み合わない可能性）で、セッション
ライフサイクル全体の設計見直しが必要になるため、今回は範囲外とした。

**検証**: `tests/test_rig.py`に`TestRigCmdLockReentrancy`（2件）を新設。
①`_rig_cmd_lock`が同一スレッドからの再取得ではブロックせず、別スレッドからは
実際にブロックすることを`threading.Thread`を使って直接検証、②`_satmode_exit()`が
`self.set_mode()`を呼ぶ実際の呼び出し連鎖（同バンドフォールバックのトリガー）を
バックグラウンドスレッド＋タイムアウト付きjoinで実行し、デッドロックすれば
このテスト自体が（無限ハングではなく）タイムアウトで失敗するようにした。
`ruff format`/`ruff check`/`mypy --strict`/`pytest tests/test_rig.py`
（170 passed、新規2件含む）すべてクリア。**実機での確認はまだ行っていない**
（コミット・pushのみ。ユーザー判断により、次回のタグビルド・リリースはまだ行わない）。

**教訓**: 「`-threads`にすると何か困ることはないか」というユーザーからの一言の
問いかけがなければ、この排他制御漏れは気づかれないまま次のリリースに乗っていた
可能性が高い。GILが偶然もたらしていた「暗黙の直列化」に頼ったコードは、それ自体が
一種の隠れた設計債務であり、GILを明示的に解放する変更（`-threads`・将来のasyncio化等）
を入れる際は、「今まで動いていたのは意図した排他制御のおかげか、それともGILの
副作用のおかげか」を必ず切り分けて確認する必要がある。

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
