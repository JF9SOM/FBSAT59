# 既知の制約・既知のバグ（未修正）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## 既知の制約（プラットフォーム由来・修正不可）

### GNOME（Wayland）ではポップアップウィンドウの初期表示位置を指定できない

`SdrWaterfallDialog`（SDR Controlタブの🌊 Waterfallボタン）は`showEvent()`から
`QTimer.singleShot(0, self._position_top_right)`で画面右上へ`move()`する実装になっているが
（画面中央だと親ウィンドウの真上に重なるため）、**GNOME Shell / Mutter（Wayland）環境では
効かず、常にGNOMEのデフォルト配置（センタリング）で開く**（2026-08-09、開発機のGNOME
Waylandセッションで確認）。

**原因**: Waylandはセキュリティ・UI一貫性の設計上、クライアント（アプリ）が自分のトップレベル
ウィンドウの画面上の絶対位置を指定することを許可していない。位置決定はコンポジタ
（GNOMEなら Mutter）の専権事項で、アプリからの`move()`要求は無視される。これはQt/GTKの
実装の問題ではなく、Wayland自体の設計・GNOMEのポリシーによるもの。KDE（KWin）・Xfce
（xfwm4）・Windows・macOSでは通常通り動作すると見込まれる（未確認）が、GNOME環境だけは
アプリ側のコードで回避する標準的な方法が存在しない。

**対応**: 修正不可と判断し、コードはそのまま維持（他環境では正しく機能するため）。
`xdotool`/`wmctrl`等の外部ツールをサブプロセスで叩く回避策も検討したが、GNOME拡張機能の
有無やバージョンに依存する不安定な手段のため見送った（ユーザー判断、2026-08-09）。

---

## 既知のバグ（未修正）

### Windows 8（8.1未満）でDLL不足エラーにより起動できない（対応不可・仕様上の制約、2026-08-21 Facebook経由で報告）

**症状**: Windows 8（8.1適用前）ユーザーから、インストール後の起動時に
「DLLが見つからない」旨のエラーが出て起動できないとFacebook経由で報告があった。

**原因**: `pyproject.toml`の`requires-python = ">=3.11"`・CI（`.github/workflows/ci.yml`）の
ビルドとも **Python 3.11** を使用しているが、**CPythonは3.9以降、公式には
Windows 8.1以降のみをサポート**しており、無印のWindows 8はこの最低要件を満たさない。
Windows 8.1/10/11には組み込まれている **UCRT（Universal C Runtime、
`api-ms-win-crt-*.dll`群）や一部のApiSetスタブ**が無印Windows 8には存在しないため、
PyInstallerでバンドルされた`python311.dll`がこれらに依存する形で起動時にDLL不足
エラーとなる。Visual C++ 再頒布可能パッケージ（`VCRUNTIME140.dll`等）未インストールの
場合とは別の原因であることに注意（そちらは再頒布パッケージのインストールで解決するが、
今回はOS自体がPython 3.11ランタイムの要件を満たしていない）。

**対応方針（ユーザー判断、2026-08-21）**: **ソフト側での回避は基本的に困難**と判断し、
修正は行わない。README（英語版・日本語版）・本ファイル冒頭の対象OS欄に
「Windows 8.1以降が必要（Windows 10/11推奨）、無印Windows 8は非対応」と明記した。
報告者にはWindows 8.1以降（できればWindows 10/11）へのアップグレードを案内する返信をした。

**教訓**: `requires-python`やCIのPythonバージョンだけを見ていると気づきにくいが、
**Pythonのバージョンそのものが対応OSの下限を規定する**ことがある（今回は3.9以降で
Windows 7非対応→実質Windows 8.1が最低ライン）。同種の「DLLが見つからない」報告を
将来受けた際は、まずVC++再頒布パッケージ未導入かOSバージョン自体の非対応かを
エラーメッセージのDLL名（`VCRUNTIME140.dll`/`MSVCP140.dll`系か`api-ms-win-crt-*.dll`系か）
で切り分けること。

### Windows — RTL-SDR Blog V4 でConnectを押してもrtlsdr_open()が呼ばれない（GitHub Issue #10・調査中）

**症状**: Windows 11、RTL-SDR Blog V4。SDR Device Installation・SDR Settingsではデバイスが
正しく検出される（`[RTL-SDR diag]`・`[RTL-SDR enum] rtlsdr_get_device_count() = 1`が
ログに出る）。しかしRadio ControlでRig 1にSDRを割り当てConnectを押しても、SDR Controlは
常にDisconnectedのままでスペクトラムも表示されない。

**ログ調査で判明した事実（2026-07-14）**: 報告者から提出された`fbsat59.log`（複数セッション・
5000行超）を精査した結果、`SdrRigAdapter.connect()`（[controller.py:3364](src/rig/controller.py:3364)）
が実行されると必ず出るはずの`logger.info("SdrRigAdapter.connect: ...")`が**一度も出現しない**
ことを確認した。つまりConnectボタンを押しても`SdrRigAdapter.connect()`自体が呼ばれていない。

`_on_connect_rig1()`（[radio_control_widget.py](src/ui/radio_control_widget.py)）から
`rig.connect()`までのコードを静的に読んだ限りでは論理的な欠陥は見当たらず（`self._rig1`の
セット・ボタン有効化・`_warn_rigctld_if_direct()`のSDR除外判定はいずれも正しい）、
「クリックハンドラ自体が呼ばれていない」のか「`self._rig1`が実は`None`だった」のか
「バックグラウンドスレッド内で例外が発生し、コンソールなしのWindows GUIアプリでは
標準エラー出力が失われて痕跡が残らなかった」のか、既存のログだけでは区別できなかった。

**現状の対処（診断ログ追加のみ・根本修正はまだ）**: `_on_connect_rig1()`に一時的な診断ログ
（`[RigConnect diag]`タグ）を追加済み。クリック受理時の`self._rig1`の型・`is_sdr`・
`is_connected`状態、各早期returnパス、スレッド開始、`rig.connect()`呼び出し前後
（例外が起きた場合は`logger.exception()`で必ず記録されるようtry/exceptで包んだ）を記録する。
報告者から次のログを受け取り次第、どこで処理が止まっているかを特定し、根本修正を行う。

**関連ファイル**: `src/ui/radio_control_widget.py`の`_on_connect_rig1()`（診断ログ、次回対応時に削除予定）

**有力な原因候補が判明（2026-08-05・未確認）— Help画面がZadigを「不要」と表示していた**:
本プロジェクトの開発者自身が実機のWindowsにRTL-SDRを接続して確認したところ、
**Zadig（WinUSBドライバー）未適用でもFBSAT59のデバイス一覧には正常に表示される**ことが
確認された（列挙はUSBディスクリプタを読むだけで成立し、実際にストリーミング用に開く段階で
初めて失敗する）。さらに、この状態でHelp > SDR Device Installationを開くと
「✅ すべてのデバイスにドライバーがインストール済みです。対応は不要です」と表示され、
**Zadigの案内が一切出なかった**。原因は`_build_install_section()`
（`src/ui/sdr_install_dialog.py`）の早期リターンで、WindowsはSoapySDRが同梱済み
（`SOAPY_AVAILABLE=True`）かつRTL-SDRが`driver="rtlsdr"`付きで列挙されるため
`needed_modules`が空になり、Zadig手順が置かれているWindows分岐に**一度も到達していなかった**。

つまりIssue #10の報告者も、この「対応は不要です」表示を見てZadigを適用しないまま
Connectを試していた可能性がある。症状（検出はされるがConnectで何も起きない）とも整合する。
**修正済み**（2026-08-05）: Windowsは「対応不要」の早期リターンから除外し、RTL-SDR/HackRF
検出時はデバイス名を明示した⚠️バナー（「一覧への表示はUSBディスクリプタを読んだだけで、
WinUSB適用まではストリーミング用に開けない」旨を明記）＋ZadigのURL入り手順を常に表示する。
デバイスマネージャーに「?」が2つ出る件も注記に追加した。あわせて
`_add_windows_buttons()`のべき等化も実施（Rescanのたびにボタンが増殖するバグがあったが、
Windows分岐が到達不能だったため今まで露見していなかった）。

報告者が新しいビルドでZadig適用後に解決するかは未確認。解決しない場合は上記の診断ログ
（`[RigConnect diag]`）による調査を継続すること。

**教訓**: 「デバイスが検出されている」ことは「そのデバイスを開いて使える」ことを一切保証
しない。列挙（enumerate）と実際のオープン・ストリーミングは別のコードパスであり、必要な
ドライバーの有無も別問題。さらに悪いことに、検出の成否だけを根拠に「対応不要」と表示する
UIは、ユーザーに必要なセットアップを積極的に省略させてしまう。インストールガイダンスの
「不要」判定は、検出結果ではなく**実際に使える状態かどうか**を根拠にすること（判定できない
場合は「不要」と言い切らない）。同種の不具合はMETEOR/HRPTのSatDump検出でも起きている
（本ファイル「SatDump 検出・起動・Linux librtlsdr不整合の一連の修正」の総括参照）。

---

### Windows — ft8lib インストールが「100%でハング」→ 再起動後も再インストール失敗（GitHub Issue #13・調査中）

**症状**: v0.2.14 で報告された `PermissionError: ft8.dll`（Help > ft8lib Installation の
Download & Install が展開中にロックされたDLLを上書きできず失敗）に対し、`_free_library()`
（DLLハンドル解放）とエラーメッセージ改善（v0.2.15、コミット `2cbbf2a`/`18f70d4`/`3f719e8`）を
実施済みだったが、v0.2.17 でも再発報告があった（2026-07-19）。今回の報告では新たに
「インストールが100%まで進んで**ハング**する」→ アプリ再起動 → 「未インストール」表示に戻る
（AppDataフォルダにはファイルは展開済み）→ 再インストールを試すと同じ
`PermissionError: ft8.dll`（「FT4タブを閉じて再起動してください」という改善済みメッセージ付き）
が出る、という一連の流れが報告された。

**静的解析で見つかり修正済みの確実なバグ（2026-07-19、根本原因かは未確認）**:
- `src/comms/q65/encoder.py` の `pack77()` が呼び出すたびに `_find_ft8lib()` で
  `ctypes.CDLL()` を新規ロードしていたが、**一度も `FreeLibrary` で解放していなかった**。
  Windowsでは`LoadLibrary`のたびに参照カウントが増え、対応する`FreeLibrary`を呼ばない限り
  アンロードされないため、Q65機能を一度でも使うと`ft8.dll`に余分な未解放参照が残り、
  Help画面側の`_refresh_status()`が１回`_free_library()`を呼んでも完全にはアンロードされない
  状態になり得る。`free_ft8lib()`という共有関数に切り出し（`src/comms/ft4/codec.py`）、
  `pack77()`側もtry/finallyで確実に解放するよう修正
- `PermissionError`のメッセージ表示（`ft8lib_dialog.py`の`_InstallWorker.run()`）が
  `str(exc)`（内部で`repr()`によりバックスラッシュが`\\`にエスケープされる）を使っていたため、
  Windowsパスの区切り文字が実際には1本なのに画面上は2本に見える、という別報告（ユーザーからの
  「これは正しいのか？」という質問）にも繋がっていた。`exc.filename`（生の文字列）を使う表示に
  修正済み

**未解決点（「100%でハング」の直接原因）**: 上記2件はいずれも静的解析で見つかった確実な不具合だが、
「進捗100%からダイアログが応答しなくなる」症状そのものを再現・確認できていない。ダウンロード後の
展開処理（`zipfile.extractall`）自体、または展開成功直後に`finished_ok`シグナル経由で呼ばれる
`_refresh_status()`（新しく展開したDLLをctypesでロードしてバージョン表示 → 解放、という一連の処理）
のどこかで止まっている可能性を疑っている（例: Windows Defenderのリアルタイムスキャンが
書き込み直後のDLLを排他的にロックし`ctypes.CDLL()`呼び出しが長時間ブロックされる、等）が、
確証はない。

**現状の対処（診断ログ追加のみ）**: `src/ui/ft8lib_dialog.py`に`[ft8lib install diag]`タグの
診断ログを追加済み（ダウンロード開始/終了・展開開始/終了・`finished_ok`emit前後・
`_on_install_ok`受信・`_refresh_status()`内の`_find_ft8lib()`/`_get_ft8lib_version()`/
`free_ft8lib()`各呼び出し前後）。報告者から次のログ（`fbsat59.log`、ハング発生時を含むセッション）
を受け取り次第、どの行の後で進行が止まっているかを特定し、根本修正を行う。

**関連ファイル**: `src/ui/ft8lib_dialog.py`（診断ログ、次回対応時に削除予定）・
`src/comms/ft4/codec.py`（`free_ft8lib()`共有関数）・`src/comms/q65/encoder.py`（`pack77()`修正）

**フォローアップ（v0.2.25、2026-07-22）— 根本原因を特定・修正**: 上記診断ログをリリースする前に
報告者が独自に再現手順を絞り込んでくれた（ft8libフォルダを削除してからクリーンインストールを
再試行）。結果、`PermissionError`（ロック）は再発せず展開も成功したが、**展開が成功し
`Download & Install`が100%まで進んでもHelp画面・FT4タブの両方が「ft8lib未インストール」の
ままになる**ことが判明した。これは「ロック」問題ではなく別の不具合だった。

**根本原因**: `_find_ft8lib()`（[codec.py](src/comms/ft4/codec.py)）が
`ctypes.CDLL(str(user_dir / "ft8.dll"))`でユーザーインストールディレクトリの`ft8.dll`を
絶対パス指定でロードしていたが、**そのディレクトリをDLL検索パスに登録していなかった**。
Python 3.8以降、Windows上の`ctypes.CDLL()`は「ロードするDLL自身のあるディレクトリ」を
暗黙に依存関係の検索対象へ含めなくなっている（`os.add_dll_directory()`で明示登録しない限り）。
`ft8.dll`はMinGWビルドのため`libgcc_s_seh-1.dll`・`libwinpthread-1.dll`という同じフォルダ内の
依存DLLを必要とするが、そのフォルダが登録されていないため、`ft8.dll`自体はフルパス指定で
開けても依存DLLの解決に失敗し（`OSError`／WinError 126相当）、既存の
`except (OSError, AttributeError): continue`に静かに握りつぶされて`_find_ft8lib()`が`None`を
返し続けていた——「ハングする」という報告は実際には（少なくともこの再現手順では）ハングでは
なく、常に失敗していたことに気づかれなかっただけだった可能性が高い。

これは本ファイルの**Hamlibユーザーインストール**で2026-07-21に確認・修正済みの不具合
（`main.py`の`os.add_dll_directory(_hamlib_user_str)`）と全く同じ原因・同じクラスの不具合。
`src/comms/ft4/codec.py`の`_find_ft8lib()`にはこの登録処理が一度も実装されていなかった。

**修正**: `_find_ft8lib()`にHamlibと同じパターンで
`os.add_dll_directory(str(user_dir))`（`hasattr(os, "add_dll_directory")`でPython 3.8+を
ガード、`user_dir.exists()`でディレクトリ未作成時のエラーを回避）を追加。さらに、
同一クラスの不具合が既に存在することを確認した以下2つのユーザーインストール型ネイティブ
ライブラリにも同じ修正を横展開した（いずれもMinGW/gfortranビルドで同様の依存DLLを同一
ディレクトリに持つため）:
- `src/comms/q65/codec.py`の`_find_libq65()`（`q65.dll`）
- `src/comms/ft4/wsjt_decoder.py`の`_find_libft4wsjt()`（`ft4wsjt.dll`、FFTW3/Boost依存）

**教訓**: このプロジェクトでは「ユーザーインストールディレクトリに配置したネイティブDLLを
ctypesで直接ロードする」という設計パターンが4箇所（Hamlib・ft8lib・libq65・libft4wsjt）に
存在する。Hamlibで一度この不具合を修正した際、同じパターンを使う他の3箇所への横展開を
その場で行わなかったため、翌日Issue #13で全く同じ不具合が別のライブラリ（ft8lib）として
再発した。**この種のDLL検索パス修正は、直接影響を受けたライブラリだけでなく、同じ
「ユーザーディレクトリ配置＋ctypes直接ロード」パターンを使う全箇所に横展開して確認すること。**

**検証状況**: 静的解析・既存precedent（Hamlibでの確認済み修正）との一致に基づく修正であり、
Windows実機での確認は次回のユーザーからの報告待ち。

**フォローアップ（v0.2.31、2026-07-23）— `add_dll_directory`修正は不十分と判明・詳細診断ログとリトライ処理を追加**:
報告者から取得した`fbsat59.log`（`[ft8lib install diag]`タグ付き）を解析した結果、
v0.2.26で入れた上記の`os.add_dll_directory()`修正は問題を解決していないことが判明した。
ログには**2種類の別々の症状**が記録されていた:

1. **展開開始からわずか9msでの`PermissionError`**（フォルダを削除して作り直した直後の
   クリーンインストールでも発生）。タイミングの短さから、書き込み直後の実行ファイルを
   リアルタイムスキャンするアンチウイルスソフトが瞬間的にファイルを排他ロックしている
   可能性が高いと推測しているが未確証
2. **展開が完全に成功した直後でも`_find_ft8lib()`が`None`を返す**（`extraction done`
   ログの直後の`_refresh_status()`呼び出しで再現）。これは(1)とは別の問題で、
   `add_dll_directory`によるDLL検索パス修正が効いていないことを直接示す証拠だった

問題は、`_find_ft8lib()`が`ctypes.CDLL()`の失敗と、その後のシンボル存在チェック
（`ftx_message_encode`/`ft4_encode`）の失敗を区別せずに`continue`していたため、
上記(2)のどちらが真の原因かをログから特定できなかったこと。

**追加した診断機能**:
- `_find_ft8lib()`（`src/comms/ft4/codec.py`）: `[ft8lib load diag]`タグで、
  `user_dir`の存在確認・`add_dll_directory()`成否・候補ごとのファイル存在/サイズ・
  `ctypes.CDLL()`自体の失敗（`OSError`、`winerror`付き）とシンボル欠落
  （`AttributeError`）を明確に区別して記録するよう変更
- 展開処理（`src/ui/ft8lib_dialog.py`の`_InstallWorker.run()`）: `PermissionError`
  発生時に0.3秒間隔で最大3回まで自動リトライする処理を追加（アンチウイルスの瞬間ロック
  という仮説が正しければリトライで解消するはずで、間接的な検証になる。FT4タブが
  DLLを開いたままの本当の永続ロックであれば3回とも失敗し、これまで通りのエラー表示
  になる）。各試行の結果を`[ft8lib install diag]`タグで記録

**次のログで判明するはずのこと**: 展開時の`PermissionError`がリトライで解消するかどうか
（(1)の原因の手がかり）、`_find_ft8lib()`が失敗する場合に`CDLL()`自体が失敗しているのか
（OS/依存DLLの問題）、それともロードは成功するがシンボルが無いのか（配布しているft8.dll
自体の不具合）を切り分けられる。

**根本原因が判明・修正（v0.2.34のユーザーログで確定、2026-07-24）**: 上記の詳細診断ログを
搭載したビルドから実際に得られたログで、ついに真の原因が判明した。**ユーザーインストール版
だけでなく、アプリに最初から同梱されているbundled版の`ft8.dll`（`_MEIPASS/ft8.dll`）でも
全く同じ症状**——`loaded OK but missing expected symbol: AttributeError("function
'ftx_message_encode' not found")`——が記録されていた。ファイルは正しく存在し
（`exists=True size=91364`）、`ctypes.CDLL()`によるロード自体も成功しているのに、
中身の関数がまったくエクスポートされていない、という状態。これはユーザーインストールの
タイミングや経路（ロック・検索パス・アンチウイルス等）とは無関係に、**Windows向けにビルド
された`ft8.dll`というファイルそのものが最初から壊れていた**ことを意味する。これまで疑って
いた「PermissionErrorによるロック」「DLL検索パス」「アンチウイルスの瞬間スキャン」は
（(A)の瞬間的なPermissionErrorという別症状は依然として残るとしても）この「未インストール」
表示の主因ではなかった。

**真の原因**: `.github/workflows/build-ft8lib.yml`のWindowsビルド手順
（`& gcc -shared -o "$FLAT\ft8.dll" @objs`）が、MinGW GCCの既知の落とし穴を踏んでいた。
**MinGW/Windows向けにGCCでDLLをビルドする場合、`__declspec(dllexport)`で明示されたシンボル
以外はデフォルトでエクスポートされない**。ft8_lib（kgoba/ft8_lib）のソースはLinux中心の
コードでそのような注釈を一切持たないため、Linux版（ELF `.so`はデフォルトで全グローバル
シンボルをエクスポートする）は問題なく動作する一方、**Windows版のDLLだけは関数が一つも
外部から呼べない状態でビルドされ続けていた**。つまりIssue #13の報告以前から、Windows向けの
ft8lib（ユーザーインストール版・アプリ同梱版とも）は一度も正しく機能したことがなかった
可能性が高い。

`build-ft8lib.yml`調査の過程で、**同じCIビルドパターン**（MinGW GCC/gfortranで
`-shared`のみ・エクスポート指定なし）を使う`scripts/build_q65lib.sh`（libq65）・
`scripts/build_ft4wsjt.sh`（libft4wsjt）の Windows/汎用ビルド分岐（`case "$(uname -s)"`の
`*)`節）にも**全く同じ不具合を確認**（未報告だが、Windows環境でのQ65・FT4拡張デコーダーの
インストールも同様に機能していなかったと推定される）。3箇所とも
`-Wl,--export-all-symbols`（GNU ldの標準オプション。Windows以外では no-op として文書化
されているため、Linux/macOSビルドへの副作用はない）をリンクコマンドに追加して修正。

**教訓**: 「ファイルは存在する・読み込みも成功する・それでも機能が使えない」という症状は、
実行時のロード経路（検索パス・ロック・権限）だけでなく、**配布物自体のビルドが壊れている
可能性**を疑うべき典型例だった。今回、ユーザーインストール版と同時にアプリ同梱版でも
同じ症状が出ていたことが、実行環境固有の問題ではなく配布物自体の問題であることを示す
決定的な証拠になった——同じ症状が「ユーザー環境だけ」でなく「開発側が作った同梱物」にも
出ていないか確認することは、原因切り分けの強力な手がかりになる。

**対応状況**: コード修正済み。次のステップとして、修正後の3ライブラリを`workflow_dispatch`
（`force_build=true`）で再ビルドし、`ft8lib-bundle`/`q65lib-bundle`/`ft4wsjt-bundle`の
各プレリリースタグのアセットを更新した上で、アプリ本体も新しいタグでリリースし直し、
同梱版のft8.dll等を差し替える必要がある。Windows実機での最終確認は次回のユーザー報告待ち。

**再ビルドで発覚した2つの追加バグ・最終修正（2026-07-25）**: `-Wl,--export-all-symbols`を
3ライブラリに適用してforce_buildした際、さらに2つの不具合が発覚した。

1. **`--export-all-symbols`はPE/COFFターゲット専用のldオプション**で、Linuxの通常のELF用
   `ld`では`unrecognized option`として拒否される。`build_q65lib.sh`/`build_ft4wsjt.sh`の
   `case "$(uname -s)"`で、このオプションをWindows/MinGWとLinux共通の`*)`分岐に入れて
   しまっていたため、Linux/macOSビルドまで巻き込んで一時的に壊してしまった（Linux
   ビルドがCI上で失敗しただけで、壊れた成果物が公開されることはなかった）。
   `MINGW*|MSYS*|CYGWIN*)`という専用分岐を新設し、既存の`*)`分岐（Linux）は一切変更せず
   完全に元のコマンドのまま維持することで解決。加えて`build-ft8lib.yml`（PowerShell）側は
   `-Wl,--export-all-symbols`を無引用符で書くとPowerShellがカンマを配列区切りと誤解釈し
   `Missing argument in parameter list`になる問題があり、`"-Wl,--export-all-symbols"`と
   明示的に1つの文字列としてクォートして解決した。

2. **`ft8.dll`のエクスポート自体は直った（0件→55件）が、`ftx_message_encode`を含む
   `ft8/message.c`由来の関数群だけが依然として欠落**していることを、実際にリリース
   アセットをダウンロードし`objdump`でエクスポートテーブルを直接検証して発見した
   （ログの`_find_ft8lib()`失敗報告だけでは推測の域を出なかったため、憶測で終わらせず
   実バイナリを検証したことで確定できた）。原因は`build-ft8lib.yml`のコンパイルループが
   **個々のファイルのコンパイル失敗を一切検知しない**設計だったこと（`gcc -c`の終了コード
   を見ず、生成された`.o`ファイルの有無だけで判定していたため、コンパイルが失敗しても
   静かに次のファイルへ進み、リンクは残ったオブジェクトだけで「成功」していた）。
   終了コードを明示チェックするよう修正した直後の再ビルドで、実際のコンパイルエラーが
   初めて可視化された：
   ```
   ft8/message.c:953: error: implicit declaration of function 'stpcpy'
   ```
   `stpcpy()`はGNU/POSIX拡張関数で、MinGWのヘッダーでは（Linux/macOSと異なり）宣言が
   見えない。ソースには`-DHAVE_STPCPY`が渡されているが、アップストリームのソース自体には
   `#ifdef HAVE_STPCPY`によるガードが一切存在せず（`grep`で確認済み）、このフラグは
   upstream Makefileの残骸で実質何もしていない。新しいGCCはこの「暗黙の関数宣言」を
   警告ではなくエラーとして扱うため、`message.c`だけがコンパイルに失敗し、上記1の検知漏れ
   と組み合わさって「`ftx_message_encode`が存在しないDLLがビルド成功と報告される」という
   状態になっていた。`-Dstpcpy=__builtin_stpcpy`（GCCが全ターゲットで認識する組み込み
   関数に置き換える、ヘッダー宣言もランタイムシンボルも不要な標準的な回避策）で解決。
   ローカル（Linux）で`message.c`が問題なくコンパイルでき`ftx_message_encode`シンボルが
   生成されることを確認した上で修正を投入。

**最終検証（2026-07-25）**: 修正後に再ビルドしたWindows版`ft8.dll`を実際にダウンロードし、
`objdump`でエクスポートテーブルを確認したところ、`ftx_message_encode`を含む
`ftx_message_*`系関数がすべて正しく含まれていることを確認済み（ファイルサイズも
91364→104146バイトに増加、`message.c`のコードが実際に含まれるようになった証拠と一致）。
v0.2.37でアプリ本体に反映・リリース済み。Windows実機での最終動作確認は次回のユーザー
報告待ち。

**教訓（今回のバグ調査全体を通じて）**:
- CIビルドスクリプトが「ファイルの存在確認」だけでコンパイル成否を判定する設計は、
  個別ファイルの失敗を静かに握りつぶし、何ヶ月も気づかれない不具合を生む。bashスクリプト
  なら`set -euo pipefail`、PowerShellなら`$LASTEXITCODE`の明示チェックを、外部コンパイラ
  ・リンカを呼び出すすべてのCIステップで徹底すること
- 「ログ上の推測」だけで「直った」と判断せず、可能な限り**実際の成果物（バイナリ）を
  ダウンロードして直接検証する**（`objdump`でのエクスポートテーブル確認等）ことが、
  今回のように複数の独立したバグが積み重なっているケースで、どの層まで直ったかを
  正確に切り分ける決め手になった

**フォローアップ（v0.2.38、2026-07-27）— ビルド自体は直ったが、Help画面がフリーズする新規バグを発見・修正**:
v0.2.37で`ftx_message_encode`のエクスポートが実際に修復され、報告者もFT4が正常動作する
ことを確認したが、「Help > ft8lib Installationを選んでも何も起きない（ウィンドウが一切
現れない、タスクバーにも出ない、フォーカスも移らない）」という新しい報告があった。

`[ft8lib install diag]`ログを精査したところ、複数回の試行すべてで
`_refresh_status: calling free_ft8lib()`のログの直後に続くはずの
`_refresh_status: free_ft8lib() returned`が**一度も出現しない**ことが判明した。
`_refresh_status()`は`Ft8LibDialog.__init__()`内、`dlg.exec()`が呼ばれる**前**に実行される
ため、ここでフリーズすると`__init__()`自体が返らず、ダイアログが一度も表示されない
（「フリーズしたようには見えない」という報告と一致する可能性が高い——GUIスレッドが
ブロックされていても、他のバックグラウンドスレッド（TLE取得等）はGILを介して進行し続ける
ため、アプリ全体が「応答なし」に見えるとは限らない）。

**原因**: ログに記録されたオブジェクトの型が`PyInstallerCDLL`だった点が手がかりになった。
これはPyInstallerが自前でDLLロードを横取りし、同梱依存DLLの解決を内部的にキャッシュ・
管理する仕組み（`ctypes.CDLL()`呼び出しへのフック）が返すラッパー型である。この管理下に
あるハンドルに対して素の`FreeLibrary()`をこちらから直接呼ぶと、PyInstaller自身の内部
ブックキーピングと衝突し、Windowsの典型的な「DLLローダーロック」系のデッドロックを
引き起こしていたと推測される（実機での完全な再現検証はできていないが、症状・ログの
一致度から妥当性が高い仮説と判断）。

**修正**: `free_ft8lib()`（`src/comms/ft4/codec.py`）に、解決されたライブラリパスが
PyInstallerの`_MEIPASS`配下（＝同梱版）かどうかを判定するガードを追加。同梱版の場合は
`FreeLibrary`の呼び出し自体をスキップする。**同梱版をアンロックする必要はそもそもない**
——このインストーラー機能が上書き対象とするのはユーザーインストールディレクトリの
コピーのみで、アプリに焼き込まれた同梱版を実行時に書き換えることは想定されていないため、
この安全対策によって機能上の損失はない。

テスト: `Path.is_relative_to()`によるパス判定ロジック自体はローカル（Linux）で単体検証
済み（同梱パス配下→True、ユーザーインストールパス→False）。実際のPyInstallerフリーズ
環境でのフリーズ解消そのものはWindows実機でしか確認できないため、次回のユーザー報告待ち。

**教訓**: PyInstallerがフックする`ctypes.CDLL()`が返すオブジェクトは、通常の`ctypes.CDLL`
とは異なる管理下にある。同梱依存DLLに対して素のWin32 API（`FreeLibrary`等）を直接叩く
コードを書く際は、対象が「アプリが独自にロードした分離ファイル」なのか「PyInstaller自身が
既に管理しているバンドル内依存」なのかを区別し、後者には手を出さないこと。

---

### AppImage — テキストフィールドにキー入力ができない（Linux 非Ubuntu系）

**症状**: Linux AppImage 版で、CI-Vアドレス・ポート名・テキスト入力フィールドにキーボードで文字が入力できない。マウス操作は正常。

**再現環境**: openSUSE Leap 16.0（Python 3.13 仮想環境から `python src/main.py` で起動した場合は正常動作する）

**原因（推定）**: AppImage に同梱された Qt6 / libxkbcommon が、非Ubuntu系ディストリビューションの XKB 設定ファイルを見つけられない。`QT_XKB_CONFIG_ROOT` / `XKB_CONFIG_ROOT` を `/usr/share/X11/xkb` または `/usr/share/xkb` に設定するコードは `src/main.py` に実装済みだが、それでも解消しない環境がある。

**現状の対処**:
- `src/main.py` に `QT_XKB_CONFIG_ROOT` / `XKB_CONFIG_ROOT` の自動設定を実装済み（起動時に `/usr/share/X11/xkb`, `/usr/share/xkb` を順に検索して最初に存在するパスをセット）
- それでも再現する場合は AppImage 内部の Qt プラグイン or libxkbcommon の問題と考えられる

**調査方針（次回対応時）**:
1. 問題が再現する環境で AppImage をターミナルから起動し、ログ出力を入手
2. `qt.qpa.keymapper` や `xkb` 関連の警告・エラーを確認
3. AppImage 内の `libxkbcommon.so` と XKB データのパスを確認
4. 必要であれば AppImage ビルド時に XKB データを同梱するか、`linuxdeploy` プラグインで解決を試みる

**ログ取得コマンド（問題再現環境で実行）**:
```bash
QT_LOGGING_RULES="qt.qpa.*=true" ./FBSAT59.AppImage 2>&1 | head -100
```

---

### アップリンクなし衛星（テレメトリー専用）でドップラー補正がリグに反映されない

**症状**: OTP-2（NORAD 63235、400.5 MHz DL）・CORVUS-BC1/BC2（2 GHz帯 DL）など、アップリンク周波数がない衛星を選択すると、リグの表示周波数が変わらない。Connect後もドップラー補正がリグに反映されず、リグは以前のキャッシュ周波数（例: 6.99920 MHz）を表示し続ける。

**再現条件**: `uplink_low = NULL` の衛星 + FT-991A（NET モード）構成で確認。RS-44 ビーコン（435 MHz）は同じくアップリンクなしだが正常動作する。

**原因の手がかり**:
- `src/ui/main_window.py` `_apply_transponder_state_to_rig()` 内で `ul_hz = float(uplink_low or dl_hz)` と代入しているため、`uplink_low=None` の衛星では `ul_hz = dl_hz` となる
- `set_transponder_freqs(dl_hz, dl_hz)` が呼ばれると `_is_same_band = True` になり、`_send_split_init_independent()` が `S 1 VFOB`（非 satmode リグには不正）を送る
- さらに FT-991A の対応周波数帯（HF/2m/70cm）外の周波数（400 MHz・2 GHz 帯）に対して `F` コマンドを送っても、リグが無視する可能性がある
- RS-44 ビーコン（435 MHz）が正常なのは FT-991A の 70cm 帯（430–450 MHz）に収まるため

**未調査点**: 上記2つの要因（`S 1 VFOB` の誤送信・対応外周波数の無視）のどちらが支配的か、または両方が絡むか未確定。

**関連ファイル**:
- `src/ui/main_window.py` — `_apply_transponder_state_to_rig()` 約2441–2443行目
- `src/rig/controller.py` — `set_transponder_freqs()`・`_send_split_init_independent()`・`_send_freq_preset_independent()`

---
