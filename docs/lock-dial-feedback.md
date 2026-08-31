# Lock（L ボタン）— dial feedback 設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

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
