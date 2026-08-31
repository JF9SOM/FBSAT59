# Hamlib 詳細（バージョン管理・配布・NET/Rotator 実装メモ）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

### Hamlib バージョン管理・配布方針（2026-06-09 確定）

#### 必須バージョン
- **Hamlib 4.7 以上が必須**（FTX-1F モデル 1051 および SkyWatcher ローテーターは 4.7 以降でのみ動作）
- **4.7.2 以上を強く推奨**（4.7.2 で rigctld のセキュリティ修正2件
  — CVE-2026-54634 / GHSA-gpcq-c37x-pr4、GHSA-f72v-7gmh-m9mj — が入った。
  `send_raw` のスタック境界外書き込み・`read_string_generic` のオーバーフロー・
  rigctld の認証バイパス。NET モードで rigctld をネットワークに公開する構成では直接該当する）
- 配布バンドル（AppImage / .exe / .dmg）には必ず 4.7.2 を同梱すること

#### バンドル版 Hamlib のビルド

| プラットフォーム | ビルド方法 | PyInstaller 収集元 |
|---|---|---|
| Linux | ソースから `/opt/hamlib/4.7` にビルド | `/opt/hamlib/4.7/lib/*.so` |
| Windows | 公式 `hamlib-w64-4.7.2.zip` を展開 | `hamlib-win64\bin\*.dll` + Python bindings |
| macOS | Homebrew `brew install hamlib` | `$(brew --prefix hamlib)/lib/` |

#### バージョンアップ手順（4.7.1 → 4.7.2 で実施した手順、2026-08-06）

1. upstream のリリースアセットとソース tarball の構造が変わっていないか実際にダウンロードして確認する
   （`bindings/hamlib.swg` の有無・`hamlib_wrap.c` が含まれないこと・`include/hamlib/config.h`
   が含まれないこと・w64 zip の `bin/`・`include/hamlib/` レイアウト）。ここが同じなら CI は
   バージョン文字列の置換だけで済む
2. `include/hamlib/riglist.h` を展開して、本プロジェクトが使うモデルID
   （FTX-1=1051, FT991=1035, IC9100=3068, IC9700=3081, IC705=3085, IC910=3044）が
   変わっていないことを確認する
3. `.github/workflows/ci.yml` の `HAMLIB_VER` 3箇所（Linux / Windows / macOS）・ステップ名・
   Windows の `config.h` スタブ内の版数を置換する
4. **タグを push する**。hamlib バンドルのアップロードは
   `if: startsWith(github.ref, 'refs/tags/v')` でガードされており、`workflow_dispatch` では
   スキップされる。タグを打たない限り `hamlib-bundle` リリースのアセットは更新されない

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
| Linux | `hamlib-linux-x86_64-py311-4.7.2.tar.gz` | `$ORIGIN` rpath付きポータブルビルド |
| Windows | `hamlib-windows-x86_64-py311-4.7.2.zip` | フラットレイアウト（DLL + .pyd + Hamlib.py） |
| macOS | `hamlib-macos-arm64-py311-4.7.2.tar.gz` | `@loader_path` rpath + dylibbundler で依存解決済み |

`py311` の部分は Python バージョンに応じて変化（`hamlib_info.py` の `_PYVER_TAG` で決定）。

**アップロード先は upstream ではなく本リポジトリの `hamlib-bundle` プレリリース**
（ft8lib-bundle / q65lib-bundle / ft4wsjt-bundle と同じ方式）。upstream の
`Hamlib/Hamlib` リリースにあるのはソース tarball と Windows インストーラーだけで、
上表のアセットは一切存在しない。

#### アップデーターが upstream を見ていて一度も機能していなかった不具合（2026-08-06 発見・修正）

4.7.2 へのバンドル更新作業中に発覚。`src/core/hamlib_info.py` の `HAMLIB_GITHUB_API` が
`https://api.github.com/repos/Hamlib/Hamlib/releases/latest`（**upstream**）を指しており、
`_CheckWorker._find_asset_url()` がその upstream リリースのアセット一覧から
`hamlib-linux-x86_64-py311-<ver>.tar.gz` を探していた。しかしこの命名のアセットは
本リポジトリの `hamlib-bundle` リリースにしか存在しないため、**マッチは構造上必ず失敗**し、
毎回「Pre-built package not found for this platform / Python version」に落ちていた。
つまり **Help > Hamlib Update… の「Download & Install」ボタンは一度も表示されたことがなかった**
可能性が高い（他の3つのバンドルインストーラーは最初から
`https://api.github.com/repos/JF9SOM/fbsat59/releases/tags/{tag}` を見ており正しかった。
Hamlib のアップデーターだけがこのパターンから外れていた）。

副次的に `_on_check_result()` の `version == current` も、upstream のタグ由来の `"4.7.2"` と
`get_hamlib_version()`（＝`Hamlib.hamlib_version`、実際の値は `"Hamlib 4.7.2"`）を
直接比較しており、こちらも構造上必ず不一致になっていた。

**修正**:
- `HAMLIB_GITHUB_API` を `JF9SOM/fbsat59` の `hamlib-bundle` タグへ変更
- **バージョンはリリースのタグではなくアセット名から取り出す**。`hamlib-bundle` は
  ローリングのプレリリースでタグ自体が版数を持たない。さらに
  `gh release upload --clobber` は同名ファイルしか置き換えないため、
  4.7.2 を上げても 4.7.1 のアセットがリリースに残り続ける。したがって
  「最初にマッチしたもの」ではなく **`version_key()` による数値比較で最大のものを選ぶ**
  必要がある（`select_newest_asset()`）
- `get_hamlib_version_number()` を新設し、表示用文字列から数値部分だけを取り出して比較する。
  ユーザーインストール版は再起動するまでロードされないため、判定には
  `get_user_hamlib_version()`（`version.txt`）を優先する
- **判定は「一致しないか」ではなく「厳密に新しいか」**（`is_update_available()`）。
  旧実装は `version == current` の不一致をもって更新ありとみなしていたため、
  リリース側が同梱版より**古い**場合にダウングレードを提案してしまう。この状態は
  例外的なものではなく、タグを打つ前の開発ビルド（同梱 4.7.2／リリース 4.7.1）で
  日常的に発生する。実際 4.7.2 のバンドル更新時、`workflow_dispatch` でビルドした
  .dmg を macOS 実機で開いたところ「Download & Install Hamlib 4.7.1」が出た
  （2026-08-06、実機確認で発覚）
- アセット選択ロジック自体は `hamlib_info.py` 側に置いた（ダイアログ内のメソッドのままだと
  テストに PySide6 と QThread 構築が必要になるため）。`tests/test_hamlib_info.py` が
  Qt 非依存でカバーする

**教訓**: 同種の機能（バンドルの自動ダウンロード）が4つあるのに、そのうち1つだけが
別の実装パターン（upstream API を見る）になっていた。しかも失敗時は例外ではなく
「見つかりませんでした」という**もっともらしいメッセージ**に落ちるため、
壊れていること自体が長期間気づかれなかった。同じ役割のコードが複数ある場合、
新規追加時だけでなく既存分についても、参照先・命名規則が揃っているかを確認すること。

**関連ソースファイル:**
- `src/core/hamlib_info.py` — バージョン検出・ユーザーディレクトリ・アセット命名
- `src/ui/hamlib_update_dialog.py` — ダウンロード・展開・インストール UI
- `src/main.py` — ユーザーインストール版の優先ロード・Windows DLL パス登録
- `.github/workflows/ci.yml` — 各プラットフォームのポータブルパッケージビルドと Release アップロード

#### Linux 開発環境固有: sys.path surgery

開発機（`/opt/hamlib/4.7` が存在する場合のみ）は `/usr/lib/python3/dist-packages` を `sys.path` から除去して 4.7.x を優先ロードする。

**重要**: このブロックは `os.path.exists(_HAMLIB_SITE)` でガードされており、`/opt/hamlib/4.7` が存在しない一般ユーザー環境では一切実行されない。

SoapySDR も同じ `dist-packages` に存在するため、パス除去前に `import SoapySDR` をプリロードして `sys.modules` に保持する（`main.py` の `contextlib.suppress` ブロック）。

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
- **アクティブVFO切り替え（`V` コマンド）は絶対に使用禁止**。TXランプ点灯どころか、
  実機でMain/Subの役割そのものが入れ替わる実害を確認済み（2026-07-15、詳細は
  「Lock（Lボタン）— dial feedback設計」セクション参照）。ソフト側の読み戻し値
  （`f`/`i`/`v`）が正常に見えても、裏で物理的な役割が壊れていることがある
- ~~f/i（get_freq）コマンドはF/I送信直後に10秒以上かかる → 使用禁止~~
  **誤診断だった（2026-07-15訂正）**。原因はリグ側の制約ではなく、クライアント側
  `_cmd_raw()`が問い合わせ系コマンド（小文字、`f`/`i`等）の成功時応答に`RPRT`行が
  一切含まれない（`RPRT`はエラー時のみ）ことを考慮せず、無条件に`RPRT`を待ち続けて
  いたバグだった。修正済み（`_cmd_raw()`のdocstring参照）。`f`/`i`は現在、Lock
  dial feedbackで実際に毎秒利用しており、正常に動作している

### 実装上の重要事項
- set_vfo_frequencies()はバックグラウンドスレッドで実行（UIブロック防止）
- _cmd()はソケットタイムアウト10秒
- connect()時に_last_dl_hz/_last_ul_hzをNoneにリセット
- S 1 Mainは接続時1回のみ（毎サイクル送らない）
- f/iダイアルフィードバック（Lock機能）は実装済み。詳細は「Lock（Lボタン）—
  dial feedback設計」セクション参照

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

## NETモードでFTX-1のDL/ULが両方Sub VFOに誤配送されるバグ — 原因はFBSAT59ではなく
姉妹プロジェクトctld-launcherの側だった（2026-08-17 解決）

### 症状

FTX-1をNETモード（rigctld経由、`ctcss_method=="ftx1"`）でFO-29に接続し、ドップラー補正を
確認したところ、**DL（145.xxx MHz帯）・UL（435.xxx MHz帯）とも実機ではSub VFOへ書き込まれて
いた**（Main側は更新されない）。ユーザーからの報告時点で、以下が既に切り分け済みだった:
- Directモード（Hamlib in-process、rigctld不使用）では一度も発生していない
- macOS移行前（Ubuntu環境）ではNETモードでもこの問題は起きていなかった

### 誤った仮説と、それを覆した実機検証（教訓として記録）

**最初に「Hamlibのバージョンが原因」と誤って結論しかけた**。開発環境をUbuntu→macへ移行した際に
Hamlibを4.7.2へ更新していたため、①dmgパッケージ版ctld-launcher（同梱4.7.2）でも再現 → ②同じMac
上でHamlib 4.7.1のrigctldを（ctld-launcherを完全停止し）手動起動して試すと再現しない、という
2点だけを根拠に「4.7.1→4.7.2のFTX-1F backend変化が原因」と判断し、実際に公式Hamlibソース
（`rigs/yaesu/ftx1/*.c`・共有の`newcat.c`・core の `src/rig.c`/`src/misc.c`・`tests/rigctld.c`/
`rigctl_parse.c`）を4.7.1/4.7.2間で全ファイルdiffするところまで進めた（結果は「Memory mode」
関連の追加とCVE対応のパスワード認証修正のみで、VFO解決ロジック自体は1バイトも変化なし）。

この段階でユーザーから「Hamlibコアの問題とは思えない、これまで問題は起きていなかった」との
強い指摘があり、**同じ手順（ctld-launcherを完全停止して手動でrigctld起動）でHamlib 4.7.2を
試したところ、こちらも問題が再現しないことが判明**——直前の「4.7.1なら直る」という結果は
Hamlibバージョンの違いではなく、**「ctld-launcherが関与しているかどうか」という別の変数**が
たまたま同時に変わっていたことによる誤った結論だった。

**教訓**: A/Bテストで2つの環境を比較する際、意図して変えた変数（Hamlibバージョン）以外に
**同時に変わってしまっている変数がないか**（今回は「ctld-launcher経由かどうか」）を常に疑う
こと。特に「動いていた時期と動いていない時期」を比較する調査では、ユーザーが明示的に
述べた事実（「これまで問題はなかった」）が自分の仮説と矛盾する場合、仮説の方を疑うべきで、
外部ソースの差分を延々と追うより先に、比較対象自体が本当にクリーンな一変数比較になっているか
を確認する方が近道だった。

### 真の原因

姉妹プロジェクト[ctld-launcher](../ctld-launcher)（本プロジェクトのNETモードが接続する
`rigctld`を、GUIから起動・監視するランチャーアプリ）には、稼働中のrigctld/rotctldを
5秒間隔で健全性チェックする機能（`_run_health_checks`、v0.1.17でWSJT-Xハング対策として追加）
があり、これがHamlib組み込みのNET rigctlクライアント（`rigctl -m 2 -r host:port f`）を
サブプロセスとして起動し、稼働中のrigctldへFBSAT59とは**別のTCPクライアント**として
接続していた。

このクライアントは、Hamlibの汎用`rig_open()`が接続直後に自動送信する「v」(get_vfo)問い合わせを、
ctld-launcher自身のコードには一切現れない形で（Hamlibライブラリ内部の暗黙の挙動として）
発生させる。rigctldサーバー側の`rig_get_vfo()`（Hamlib `src/rig.c`）はこの問い合わせに対し、
無線機に実際に`VS;`(アクティブVFO問い合わせ)を送って結果を返すだけでなく、**rigctldプロセス
全体で共有される`current_vfo`の値をその結果で上書きする**（キャッシュ実装、Hamlib 4.7.1/4.7.2
とも同一コード）。

一方、rigctldのプレーンな「F」コマンド（VFO省略、本プロジェクトのNETモードが常用する形式）は
`rig_set_freq(rig, RIG_VFO_CURR, freq)`として処理され、`RIG_VFO_CURR`は`vfo_fixup()`では
解決されず、呼び出し元がその場の`current_vfo`へ置き換える実装になっている（`src/rig.c`確認済み）。
つまり**「F」の行き先は、その瞬間のrigctldの`current_vfo`が何であるかに完全に依存する**。

FTX-1Fはクロスバンド/split構成で継続的にドップラー追尾する際、無線機の物理的な表示VFOが
運用中にSub側を指すことがある既知のファームウェア癖を持つ（本ファイル「Lock（Lボタン）」
セクションで詳述している`VS`/`FT`コマンドの相互作用と同根）。ctld-launcherのヘルスチェックが
このタイミングに重なると、rigctldが管理する`current_vfo`が恒久的にSubへ書き換わってしまい、
以降**本プロジェクトが送るVFO省略の「F」書き込みも全てSub側へ誤配送される**——「I」（UL、
`set_split_freq`）はFTX-1F backend自体の仕様で元々Sub固定なので正しく見えるが、「F」（DL、
本来Main）も巻き込まれてSubへ行くため、結果としてDL/UL両方がSub VFOに書き込まれる、という
今回の症状に一致する。

**本プロジェクト自身は「V」/「v」コマンドを意図的に一度も送らない設計**（本ファイル「Lock
（Lボタン）— dial feedback設計」セクション参照。過去の実機トラブルで学んだ制約）だが、
**同じrigctldを共有する他クライアント（今回はctld-launcherのヘルスチェック）がそれを
送ってしまえば、本プロジェクト側の注意深さだけでは防げない**、という構図だった。

### 修正

修正はFBSAT59側ではなく[ctld-launcher](../ctld-launcher)側で行った（詳細はctld-launcherの
CLAUDE.md参照、v0.1.19）。`rigctl -m 2`（Hamlib NET rigctlクライアント経由）を使うのをやめ、
生ソケットで直接プレーンな「f」(rig)/「p」(rotator)をrigctld/rotctldへ送るだけの
`probe_daemon()`に置き換えた。Hamlibソース（`src/rig.c`の`rig_get_freq()`）を確認した結果、
`current_vfo`は読むだけで一切書き換えないことが確認できており、本プロジェクトのLock機能が
日常的に送っている「f」/「i」と全く同じ安全なコマンドである。ヘルスチェック・稼働中プロファイル
への「Test Connection」ボタンの両方をこの方式に切り替えている。

### 教訓

- 「稼働中デーモンへの疎通確認」のような一見無害な監視機能でも、それが使うクライアント
  ライブラリが接続の副作用として何を送っているかまでは意識されにくい。Hamlibのように
  複数のTCPクライアントが単一の共有リグ状態（`current_vfo`）を扱うプロトコルでは、
  ある1つのクライアントの「読み取りのつもり」の操作が、無線機の物理状態と絡んで**他の
  全クライアントの動作**に影響することがある
- 本プロジェクトが「V」を避けているという既存の防御は、**本プロジェクト自身の直接操作**
  にしか及ばない。同じrigctldに繋がる可能性のある外部ツール（GPredict・WSJT-X・
  ctld-launcher等）が同種の問題を起こしうることを、NETモード関連の不具合調査では
  今後も疑うこと
- A/Bテストで結論を出す前に、比較対象が本当に「意図した1変数だけ」の違いになっているか
  を再確認すること。今回は「Hamlibバージョン」のつもりが実際には「ctld-launcherの
  関与有無」も同時に変わっていた

---
