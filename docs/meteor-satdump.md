# SatDump / METEOR・HRPT 受信 詳細

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## SatDump 検出・起動・Linux librtlsdr不整合の一連の修正（2026-08-02、v0.2.44〜v0.2.5x）

METEOR/HRPTタブがSatDumpを一度も正しく起動できていなかった（macOS・Linuxとも）ことが、
ユーザーが実際にmacOS版でSatDumpをインストールして試したことをきっかけに一連の調査で
判明した。4つの独立した不具合が積み重なっており、1つ直しては次の不具合が現れる形で
段階的に発覚した。METEOR/HRPT機能は2026-06-29の実装完了以降、実機での起動確認が
一度もされていなかったと考えられる（CLAUDE.md本体にも「実機で受信確認済み」の記述が
一切なかった）。

### 1. macOS `.app`バンドル未検出（`find_satdump()`）

**症状**: `.dmg`をダウンロードして`SatDump.app`を`/Applications`にドラッグする、
という最も標準的な方法でインストールしても、Help > SatDump Installationで
「SatDumpが見つかりません」と表示され続けた。

**原因**: [`find_satdump()`](src/comms/meteor/satdump.py)は「FBSAT59専用のユーザー
ディレクトリ」と「システムPATH」の2箇所しか見ておらず、`.app`バンドルという第3の
標準的インストール形態を一切考慮していなかった。Help画面自体は`.dmg`を選択肢として
案内していたにもかかわらず、検出コードにそれに対応する分岐が存在しないという、
案内文と実装の食い違いだった。

**修正**: `sys.platform == "darwin"`のとき、`/Applications/SatDump.app/Contents/MacOS/satdump`
（および`~/Applications`）も検索対象に追加。

### 2. METEOR/HRPTタブのログウィンドウが実行中の内容を保持しない

**症状**: SatDumpが起動〜約30秒後にエラー終了したが、「ログ」ボタンを押しても
ウィンドウが空だった。

**原因**: `_LogWindow`（[meteor_tab.py](src/ui/meteor_tab.py)）は「ログ」ボタンを
**最初に**押した時点で初めて生成される設計で、それより前に`SatDumpProcess.log_line`
シグナル経由で流れてきた行は一切バッファされず、ウィンドウが存在しない間は
`_on_log_line()`が黙って読み捨てていた。実行→エラー→その後にログボタンを押す、
という通常の操作順序では、SatDump自身が出した本当のエラーメッセージが永遠に
失われる設計だった。

**修正**: `MeteorTab`に`self._log_buffer: deque[str]`（上限2000件）を追加し、
`_on_log_line()`・`_on_finished_err()`は常時バッファへ追記。「ログ」ボタンを押して
ウィンドウを新規生成する際、バッファの内容を`_LogWindow.append()`で再生してから表示する。

この修正により初めて、次項3・4の本当の原因（生のSatDump出力）が見えるようになった。

### 3. バージョン表示のANSI文字化け

**症状**: 「バージョン: ▤[31m▤[1m(E) Usage : ...」という文字化けが表示された。

**原因**: [`_get_satdump_version()`](src/ui/satdump_dialog.py)は`satdump --version`
の出力をそのまま最初の非空行として表示していたが、`--version`フラグを認識しない
SatDumpビルドでは、ANSIカラーコード付きの使い方（Usage）メッセージが代わりに
出力される。ANSIエスケープの除去も、Usageメッセージかどうかの判定も行っていなかった。

**修正**: 正規表現でANSIエスケープを除去し、出力に`usage`という語が含まれる場合は
「バージョン不明」と表示するよう変更。

### 4. `satdump live`のCLI引数順序の誤り（`exited with code 1`の真因）

**症状**: 上記2・3の修正でようやく見えた実際のログに
`Error parsing arguments! [json.exception.type_error.302] type must be string, but is null`
というエラーがあり、`exited with code 1`で失敗していた。

**原因調査**: SatDump公式リポジトリのソース（GitHub CLI `gh api`で実際のタグ
`1.2.2`——ユーザーの実機バージョンと完全一致——のソースを直接取得して確認）から、
`live`サブコマンドの引数仕様が判明した:
```
satdump live <pipeline_id> <output_directory> [--flags...]
```
出力ディレクトリは**位置引数**（`argv[3]`、パイプラインIDの直後）であり、
`--output`という名前のフラグは**そもそも存在しない**。しかし
[`SatDumpProcess.run()`](src/comms/meteor/satdump.py)は
`["live", pipeline, "--source", source, ..., "--output", str(output_dir), ...]`
という順序でコマンドを組み立てていたため、`argv[3]`（本来は出力ディレクトリで
あるべき場所）に文字列`"--source"`がそのまま入ってしまい、以降の全フラグ/値の
対応がひとつずつズレる。結果として`source`パラメータが一切設定されず、SatDump内部で
`parameters["source"]`（nlohmann::jsonの`operator[]`は存在しないキーをnull値で
自動生成する）に対し`.get<std::string>()`を呼んで例外——`exited with code 1`——と
なっていた。加えて`--finish_after_loss_of_lock`というフラグもSatDumpソース全体を
検索した限り現行版には存在しないことを確認した（クラッシュの原因ではないが無効な
フラグ）。

**この不具合はOS非依存**（`SatDumpProcess.run()`は純粋なPythonコードで、macOS/
Windows/Linuxすべてで同一のコマンドライン文字列を組み立てる）。つまりMETEOR/HRPT
機能はどのプラットフォームでも実装完了以来一度も正しく起動できていなかったと
考えられる。

**修正**: 出力ディレクトリを`--output`フラグではなく正しい位置引数として渡すよう
コマンド構築順序を修正。存在しない`--finish_after_loss_of_lock`フラグは削除。
副作用として、ロック消失時の自動停止機能は失われた（Autotrack経由のLOS自動停止
とは別ロジックのため無関係。手動起動時はパス終了後に手動で「■ 停止」を押す必要が
ある。今回はスコープ外として現状維持）。

### 5. Linux固有: `librtlsdr`のSONAME不整合と、nightly AppImageバンドル化の試みと撤回

**症状**（項目4の修正後、Linux開発環境で再現）: 引数解析自体は成功し、プラグイン・
TLE読み込みまで進むようになったが、最終的に
```
Error loading .../librtlsdr_sdr_support.so! Error : librtlsdr.so.0: cannot open shared object file
...
Could not find a handler for source type: rtlsdr!
```
で失敗した。

**原因**: `ldconfig -p | grep rtlsdr`・`dpkg -l`で調査したところ、Ubuntu系の
`librtlsdr2`パッケージが提供するSONAMEは`librtlsdr.so.2`だが、SatDumpの`.deb`は
`librtlsdr.so.0`を要求してリンクされていた、というディストリビューション側の
パッケージ間バージョン不整合と判明した。FBSAT59自身のSDR機能（SoapySDR経由）は
現行の`librtlsdr.so.2`向けに正しくビルドされているため無関係に動作しており、
「FBSAT59のSDR設定画面では認識できるのに、SatDump経由だけ失敗する」という
一見矛盾した症状の理由でもあった。macOS（`.app`バンドルが`librtlsdr`を内部に
同梱）・Windows（公式Portable版も同様に自己完結型と推定）はこの問題の対象外で、
**Linuxの`.deb`配布形態に固有の問題**。

**最初に試みて撤回した対策 — SatDump公式nightly AppImageの自動ダウンロード機能**:
SatDump公式のGitHub Releasesに、依存ライブラリを内部に同梱した自己完結型の
`SatDump.AppImage`が`nightly`タグ（安定版とは別のローリング開発版リリース）に
存在することを発見し、Help > SatDump Installationに「Download & Install」
ボタンを追加してこれを自動取得・配置する機能を実装した（自前のCIビルドや
ファイルホスティングは一切不要という触れ込みだった）。しかし実機検証で
以下の想定外の問題が次々に発覚し、**この機能は撤回した**（コミット`e0bae82`・
`54b9a22`を`git revert`）:
1. AppImage内部の`.desktop`ファイルに`Exec=satdump-ui`と指定されており、
   AppImageをそのまま実行すると常に**GUI版**（`satdump-ui`）が起動する設計
   だった。GUI版はGLFW/OpenGLウィンドウの初期化を試み、ディスプレイ・GPU
   環境が制約された開発環境では`Could not init GLFW Window! Exiting`で
   即座に失敗した
2. AppImageを`--appimage-extract`で展開し、内部の真のCLIバイナリ
   （`usr/bin/satdump`、GUI版`satdump-ui`とは別ファイル）を直接実行する
   代替策も試したが、`LD_LIBRARY_PATH`（共有ライブラリ探索）・作業ディレクトリ
   （設定ファイル`satdump_cfg.json`をCWD相対で探索する挙動）など、AppImageの
   起動ラッパー（`AppRun`）が内部的に設定している複数の環境変数・前提条件を
   自前で再現する必要があり、さらに`XDG_DATA_DIRS`だけでは解決しない・
   プラグインディレクトリの探索が別途失敗する（`No valid plugin directory
   found!`）等、ドキュメント化されていない挙動に何重にも依存していることが
   判明した
3. `nightly`タグの実体は`SatDump v2.0.0-alpha`（安定版v1.2.2とは世代が異なる
   開発版）で、多くのデコーダーモジュールが読み込まれない状態だった
   （`ax25_decoder`・`dvbs2_demod`等、"Module X is not loaded. Skipping
   pipeline!"警告多数）

**教訓**: 「公式が配布している自己完結型ビルド」であっても、実際に動かして
検証する前に「バンドル済みだから安全」と判断してはいけない。特に元々GUIアプリ
として設計されたソフトウェアのCLI実行パスは、パッケージング（今回はAppImage化）
の過程で本来のCLI専用パスとは別の、GUI優先のエントリーポイントに再配線されて
いることがある。

**最終的に採用した対策 — 検出してコマンドを提示するだけに留める**: 自前でのビルド・
バンドル配布は行わず、[`_find_rtlsdr_symlink_fix()`](src/ui/satdump_dialog.py)が
既知のライブラリディレクトリ（`/usr/lib/x86_64-linux-gnu`等）を走査し、
`librtlsdr.so.0`が存在せず`librtlsdr.so.N`（N≠0）が存在する場合にのみ、
Help > SatDump… ダイアログに「Fix librtlsdr Version Mismatch」枠を表示する。
既存の`CommandRow`（Copy / Run in Terminal、[copyable_text.py](src/ui/copyable_text.py)）
パターンをそのまま流用し、
```bash
sudo ln -s <検出したlibrtlsdr.so.N> <librtlsdr.so.0の期待パス> && sudo ldconfig
```
を提示する。「Run in Terminal」は実際のターミナルウィンドウを開いてコマンドを
実行するため、`sudo`のパスワード入力にもユーザーが対話的に応答できる
（[terminal_launcher.py](src/core/terminal_launcher.py)、サイレントにpkexec等を
自前で叩く実装は採用していない）。

**検証済み（2026-08-02）**: 上記1〜4の修正とこのシンボリックリンク適用により、
macOS（`.app`バンドル、実機確認済み）・Linux（開発環境、apt版`.deb`+シンボリック
リンク、実機確認済み）の両方で、METEOR/HRPT受信が実際に「Lock」状態まで到達する
ことを確認した。Windows版は未検証（公式Portable版は自己完結型と推定されるため
実害は無いと考えられるが、実機確認は次回のユーザー報告待ち）。

**総括的な教訓**:
- 「見つかりました（found）」という検出結果は「起動できる」ことを一切保証しない。
  検出ロジックとプロセス起動ロジックは完全に別物であり、両方を実際に動かして
  初めて機能全体の動作確認になる
- ログが失われる設計上の穴（今回の項目2）があると、その先の本当の不具合
  （項目3・4）が何重にも隠れたまま気づかれない。診断用のログ・出力を
  確実に保持する仕組みは、機能そのものと同じくらい優先度高く直すべき
- サードパーティCLIツールの引数仕様は、README等の二次情報を推測で信じず、
  実際に使っているバージョンの公式ソースコード（可能なら`gh api`等で当該タグを
  直接取得）を確認すること。特に「よくあるCLIパターン」（`--output`のような
  フラグ）を無条件に仮定しない

### 6. 実際に良好にロックできても画像が一度も生成されない不具合と、Windows版`stop()`が強制終了になっていた不具合（2026-08-19 発見・修正）

上記1〜5の修正でMETEOR/HRPT受信自体は「Lock」状態（Deframer: SYNCED）まで到達できる
ようになっていたが、実際に仰角80度級のパスでSNR最大9.8dB・BER最良1.5%・Deframer
SYNCEDが約2分半継続するという明確に良好な受信ができた際にも、**画像が一枚も
生成されなかった**という報告があった。

**原因調査**: 出力ディレクトリを直接確認したところ`meteor_m2-x_lrpt.cadu`
（デコード済み生フレーム、約1.5MB）のみが存在し、`images/`フォルダもPNGも
一切無かった。SatDump本家の`resources/pipelines/Meteor-M.json`を確認すると、
`meteor_m2-x_lrpt`パイプラインは`soft`（psk_demod）→`cadu`
（ccsds_conv_concat_decoder）→`products`（meteor_msumr_lrpt、CADUフレームから
実際のMSU-MR画像を合成する段階）の3段構成だが、実際の受信ログには
`Module psk_demod`・`Module ccsds_conv_concat_decoder`しか現れず、
`Module meteor_msumr_lrpt`が一度も実行されていなかった。

SatDump CLIのソース（`src-cli/legacy/live.cpp`）を確認すると、この`products`段階の
後処理は、`--finish_processing true`という追加パラメータを明示的に渡さない限り、
たとえ`Signal Received. Stopping.`という正常終了ログが出ていても**デフォルトで
スキップされる**仕様だった（`parameters.contains("finish_processing") ? ... :
false`）。`SatDumpProcess.run()`（`src/comms/meteor/satdump.py`）のコマンド
組み立てにはこのフラグが一度も含まれていなかった。**つまりMETEORタブは
2026-06-29の実装当初から、受信自体に成功していても画像を生成したことが
一度もなかった**可能性が高い。

**修正1（`--finish_processing true`追加）**: コマンド組み立てに無条件で追加。
あわせて、この後処理は停止シグナルを受けてからプロセスが実際に終了するまでの
間に実行され、その間もSatDumpは標準出力にログを出し続けるため、`run()`内の
`for line in stdout: ... if self.isInterruptionRequested(): break`という
早期離脱を削除し、**プロセスが本当に終了する（stdoutパイプが閉じる）まで
読み切る**よう変更した。この早期離脱を残したままだと、後処理中の出力を
表示し損なうだけでなく、誰も読まないパイプが埋まってSatDump自身の
`write()`がブロックし、後処理そのものが止まってしまうリスクもあった。

**修正2（Windows版`stop()`が実質強制終了だった問題）**: 上記調査と並行して、
`SatDumpProcess.stop()`が呼んでいる`self._proc.terminate()`は、POSIX
（macOS/Linux）ではSIGTERM（SatDump自身の`signal(SIGINT, sig_handler_live)`
ハンドラが捕捉し正常終了処理に入れる）だが、**Windowsでは`TerminateProcess()`
という無条件の強制終了**であり、SatDumpが正常終了処理に入る機会が一切ない
ことが判明した。つまりWindows版でアプリの「■ 停止」ボタンを押した場合、
修正1を入れても画像は生成されないままだったはずである。

Windowsでプロセスに正常終了を促す標準的な方法は`CTRL_BREAK_EVENT`
（`Popen.send_signal(signal.CTRL_BREAK_EVENT)`、対象を`CREATE_NEW_PROCESS_GROUP`
で起動しておく必要がある）だが、**FBSAT59はウィンドウ表示のみでコンソールを
持たないPyInstallerビルド**であるため、素の`send_signal()`は
`GenerateConsoleCtrlEvent`が「呼び出し元プロセスが対象と同じコンソール
セッションにアタッチされている必要がある」という制約に阻まれて失敗する
（CPython本体の既知の制限、[python/cpython#112190](https://github.com/python/cpython/issues/112190)）。
自プロセスがコンソールを持たないため、この制約を満たせない。

**採用した回避策**: 停止する瞬間だけ`AttachConsole(satdump_pid)`で
satdump.exe自身の（`CREATE_NO_WINDOW`で作られた非表示の）コンソールへ一時的に
アタッチし、`GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)`を発行してから
`FreeConsole()`で切り離す、という手順をctypesで実装した
（`_send_graceful_stop_windows()`）。MSVC CRTの`signal(SIGINT, ...)`は
`CTRL_C_EVENT`・`CTRL_BREAK_EVENT`の両方をSIGINTとして扱うため、この
イベントでSatDump自身の`sig_handler_live`が正しく起動することを、
Microsoft公式ドキュメント・CPython issueの議論の双方から確認した上で実装した
（実機Windowsでの動作確認は次回のユーザー報告待ち）。失敗した場合は
従来通り`terminate()`（強制終了）にフォールバックするため、最悪の場合でも
今までより状況が悪化することはない。

**検証状況**: `--finish_processing`フラグの付与・stdout読み切りへの変更・
Windows分岐のフォールバック制御フローは、フェイクの`Popen`/`sys.platform`を
使ったユニットテストで確認済み（`ruff`/`mypy --strict`/既存`test_rig.py`も
グリーン）。実際のWin32 API呼び出し（`AttachConsole`等）はmacOS開発機からは
検証できないため、実機Windowsでの最終確認は次回のユーザー報告待ち。

**`--finish_processing`自体はmacOS実機で確認済み（2026-08-20）**: N2-4・
仰角60度パスで、Deframer SYNCEDが約4分継続した後「■ 停止」を押したところ、
ログに`Signal Received. Stopping.`の後`Starting meteor_m2-x_lrpt`
（productsステージの再処理）が続き、`MSU-MR-1.png`等の個別チャンネル画像・
複数の合成（false color等）画像・投影済み画像まで、すべて
`{output_dir}/MSU-MR/`配下に正しく生成されることを確認した。修正1
（画像化工程そのもの）は意図通り機能している。

**教訓**: 「Deframer: SYNCED」というロック表示は、あくまで復調・フレーム同期が
成功したことを示すだけで、**そのフレームから実際に画像を合成する後段の処理が
別途必要**であり、かつSatDumpはその後段処理を明示的なフラグなしでは実行しない、
という多段パイプラインの設計を見落としていた。「受信自体は成功しているように
見えるのに最終成果物（画像）が出ない」という報告を受けたら、パイプライン定義
そのものを確認し、ログに出ているモジュールと定義上あるはずのモジュールを
突き合わせること。またWindowsのプロセス終了は「graceful」の意味がPOSIXと
根本的に異なる（`terminate()`が両OSで同じ意味だと思い込まない）ことも、
Hamlib・ft8lib等これまでの経緯と同様に繰り返し踏んだ落とし穴だった。

### 7. 画像は生成されているのにアプリ上には何も表示されない不具合（`ImageWatcher`が停止ボタンと同時に止まっていた、2026-08-20 発見・修正）

**症状**: 上記6の修正で画像自体は`{output_dir}/MSU-MR/`配下に確かに生成される
ようになったことをmacOS実機ログで確認したが、実際にアプリの画面上には受信画像も
サムネイル履歴も一切表示されなかった（「ソフト上には、全く画像が出てきません」
というユーザー報告）。

**原因**: `MeteorTab._on_stop()`（「■ 停止」ボタン押下時）が、SatDumpプロセスへ
停止シグナルを送るのと**同時に**`self._watcher.stop()`（`ImageWatcher`の
2秒間隔ポーリングタイマー）も止めていた。しかし修正6により、「■ 停止」を
押してからSatDumpが実際にプロセスを終了するまでの間（実機ログでは約6秒）に
画像合成の後処理が走り、その間にこそPNGファイル群が書き込まれる。つまり
`ImageWatcher`は、監視対象のファイルが実際に書き込まれる**前**に監視自体を
止められてしまっており、ボタンを押した瞬間に「まだ何もない」ディレクトリを
最後にポーリングしたきり、二度と見に行かない状態だった。生成された画像自体は
ディスク上に正しく存在するため、`📁 Open Folder`ボタン経由なら確認できるが、
アプリ内表示（受信画像プレビュー・受信履歴サムネイル）には一切反映されない、
という形で発覚した。

**修正**: `_on_stop()`から`self._watcher.stop()`を削除し、代わりに
`ImageWatcher`のポーリングは**SatDumpプロセスが実際に終了したことを示す
`finished_ok`/`finished_err`シグナル**が届くまで生かし続けるよう変更した。
両シグナルハンドラの先頭で共通ヘルパー`_stop_watcher_after_final_poll()`を
呼び、`ImageWatcher.poll_now()`（新設、内部の`_poll()`を即座に1回だけ強制実行
する公開メソッド）で最後のポーリング間隔の隙間に書き込まれたファイルを
確実に拾ってから`stop()`する。これにより、後処理中に生成される画像は
（次のタイマーティックを待たず）ほぼリアルタイムでプレビュー・履歴に反映され、
プロセス終了時点で取りこぼしなく最終確認が行われる。

**教訓**: 修正6（`--finish_processing`追加）は「停止後も一定時間ファイルが
増え続ける」という新しい前提をパイプラインに持ち込んだが、その前提の変化が
`ImageWatcher`のライフサイクル（停止ボタン＝即監視終了、という従来は正しかった
設計）と噛み合っていないことを見落としていた。一つの修正が「処理完了のタイミング」
を変えた場合、それに依存する他のコンポーネント（今回は出力ディレクトリの
監視タイマー）のライフサイクルも連動して見直す必要がある。

### 8. Windows実機で`--finish_processing`が一度も走らなかった根本原因 — CTRL_BREAK_EVENTはSatDump側で一切ハンドリングされていなかった（GitHub Issue #27、2026-08-22 発見・修正）

**症状**: Issue #27の修正一式（本ファイル前述、v0.3.31）をリリースした後、開発者自身が
Windows 11実機（METEOR-M、最大仰角60°の良好なパス）で検証したところ、受信自体は
SNR最大13dB前後・Deframer SYNCEDが5分半以上継続という好条件だったにもかかわらず、
「■ 停止」を押すと`satdump exited with code 3221225786`というエラーが表示され、
今回も画像が一枚も生成されなかった（CADUファイルのみ）。SatDump自身のログ
（`satdump_log_*.txt`、`Help > SatDump…`の「ログ」ウィンドウから保存可能）を
確認したところ、ログは受信中のプログレス行のまま唐突に途切れており、
**「Signal Received. Stopping.」という、graceful shutdownに入った際に必ず出るはずの
ログ行が一切存在しなかった**。

**原因**: SatDump本家のソース（`src-cli/live.cpp`、タグ`1.2.2`。masterでは
`src-cli/legacy/live.cpp`に移動済みだが1.2.2時点ではこちらのパス）を直接確認したところ、
シグナルハンドラの登録は`signal(SIGINT, sig_handler_live)` /
`signal(SIGTERM, sig_handler_live)`の2つのみで、**CTRL_BREAK_EVENT（Windows固有の
`SIGBREAK`）に対応するハンドラは一切存在しなかった**。旧`_send_graceful_stop_windows()`
は「Windows CRTの`signal(SIGINT, ...)`はCTRL_C_EVENT・CTRL_BREAK_EVENTの両方に
配線される」という前提でCTRL_BREAK_EVENTを送信していたが、これは誤りだった——
Windows CRTが`SIGINT`にマッピングするのは**CTRL_C_EVENTのみ**で、CTRL_BREAK_EVENTは
別シグナル（SIGBREAK）に割り当てられる。SatDumpにSIGBREAKハンドラが無い以上、
CTRL_BREAK_EVENTを送ってもプロセスは自身のgraceful shutdownパスに一切入らず、
Windowsのデフォルト動作（即時終了）がそのまま発動する。この終了コードが
`STATUS_CONTROL_C_EXIT`（`0xC000013A` = `3221225786`）——実機で報告されたエラー
コードと完全に一致した。`AttachConsole`/`GenerateConsoleCtrlEvent`自体は成功していたが、
送信していたシグナルの種類そのものが誤りだった、という結論になる。

**修正**: CTRL_BREAK_EVENTからCTRL_C_EVENT（Win32定数`0`）への切り替え。ただし
CTRL_C_EVENTには重要な制約があり、Microsoft公式ドキュメント（`CreateProcess`の
`CREATE_NEW_PROCESS_GROUP`フラグの説明）に明記されている通り
**「このフラグを付けて起動したプロセスは、CTRL+C信号が完全に無効化される」**。
旧`SatDumpProcess.run()`のWindows起動コマンドは`CREATE_NO_WINDOW |
CREATE_NEW_PROCESS_GROUP`（後者はCTRL_BREAK_EVENTを特定プロセスグループに絞って
送るための仕組みだった）で起動していたため、これを`CREATE_NO_WINDOW`単体に変更
（`CREATE_NO_WINDOW`だけでもsatdump.exe自身の非表示コンソールは作られるため、
`AttachConsole()`でそのコンソールに参加する既存の仕組みはそのまま使える）。
`_send_graceful_stop_windows()`側は`_CTRL_BREAK_EVENT = 1` →
`_CTRL_C_EVENT = 0`に定数を変更するのみ（`SetConsoleCtrlHandler`による自プロセスへの
巻き込まれ防止の仕組みは、CTRL_C_EVENTの方がむしろ素直に効く——Microsoftのドキュメント
通り、CTRL_C_EVENTは`SetConsoleCtrlHandler`で完全に抑制可能だが、CTRL_BREAK_EVENTは
本来抑制不能なイベントで、既存コードはハンドラが`TRUE`を返すことで「処理済み」と
偽装する回避策に頼っていた）。

テスト: `tests/test_meteor_satdump.py`（新規） —
`TestWindowsCreationFlags::test_windows_popen_omits_new_process_group_flag`が
Windows分岐の`subprocess.Popen`呼び出しに`CREATE_NEW_PROCESS_GROUP`が含まれないこと
を検証（`sys.platform`と`subprocess.Popen`をモンキーパッチ）。
`TestSendGracefulStopWindows`が、`ctypes.WinDLL`/`ctypes.WINFUNCTYPE`
（非Windows環境には存在しない属性のため`monkeypatch.setattr(..., raising=False)`で
追加）をフェイクのkernel32オブジェクトに差し替え、`GenerateConsoleCtrlEvent`へ実際に
渡される値が`0`（CTRL_C_EVENT）であって`1`（CTRL_BREAK_EVENT）ではないことを直接
検証する（`ctypes.wintypes.BOOL`/`DWORD`は非Windows環境でも存在するためモック不要、
`ctypes.WinDLL`/`WINFUNCTYPE`のみがWindows専用）。macOS開発環境で実行・グリーン確認済み。

**検証状況**: 静的なソース確認（SatDump本家のシグナルハンドラ登録箇所）とWindows API
仕様（Microsoft公式ドキュメントの`CREATE_NEW_PROCESS_GROUP`の副作用）に基づく修正。
実機Windowsでの動作確認は次回のユーザー（開発者自身）の再テスト待ち。

**教訓**: 前回（項目6）の実装時に残していた「Windows CRTはSIGINTをCTRL_C_EVENT・
CTRL_BREAK_EVENTの両方にマッピングする」という前提は、Microsoft公式ドキュメントを
実際には確認せず、一般的な理解（誤解）に基づいて書かれたものだった。加えて、
「送信先のプロセス（SatDump）が実際にそのシグナルをハンドリングしているか」を
一次情報（SatDump自身のソースコード）で確認せずに実装していた。外部プロセスへの
シグナル配送を実装する際は、①OS側の配送メカニズムの制約（今回は
`CREATE_NEW_PROCESS_GROUP`がCTRL+Cを無効化する副作用）と、②受信側プロセスが実際に
そのシグナルをハンドリングするコードを持っているか、の両方を実装前に一次情報で
確認すべきだった。前者だけを検証して「配送は成功している」と考えても、後者が
欠けていれば無意味である。

### 9. Autotrack Recording チェックボックス（Audio/IQ/METEOR）が再起動のたびにリセットされていた不具合（GitHub Issue #27、2026-08-22 発見・修正）

**症状**: v0.3.32でCADU→画像生成の不具合（項目8）を修正した後、報告者（on7ndr）が
METEOR-M N2-4のより良好なパスで再テストしたところ、①衛星選択後にGroup Passタブで
明示的に検索を実施、②Autotrack/RecordダイアログでリストのEnableチェックを入れ
「Tracking: METEOR M2-4」というステータス表示まで正しく出た、にもかかわらず、
**AOS到達時にMETEORウィンドウが自動起動しなかった**。手動でMETEORタブを開いて
受信・停止したところ画像は正常に生成された（項目8の修正自体は機能している）。

**原因**: `AutotrackRecordDialog`の「Audio Record (MP3)」「IQ Record」
「METEOR / HRPT Reception」の3チェックボックスは、`MainWindow.__init__()`で
```python
self._autotrack_meteor_record: bool = False
```
のように**毎回無条件でFalse初期化**されるだけで、DBへの永続化が一切実装されて
いなかった。つまり**アプリを再起動するたびに、これら3つのチェックボックスは
必ずOFFに戻る**。報告者の説明文には、Autotrackリスト自体の選択・Enable操作への
言及はあるが「METEOR / HRPT Reception」チェックボックスへの言及が一切なく、
これは前回のセッションで一度でも有効にしていたとしても、その後アプリを再起動
していれば気づかないうちにOFFへ戻っていた可能性が高いと判断した。
`_autotrack_meteor_record`がFalseの場合、`_autotrack_on_aos()`は
`_meteor_autotrack_aos()`を一切呼ばない設計（[main_window.py](src/ui/main_window.py)）
のため、これは「Trackingは表示されるがMETEORウィンドウは開かない」という今回の
症状と完全に一致する。

**修正**: `AutotrackRecordDialog`に`app_settings`テーブルへの永続化を追加
（キー`autotrack_recording_settings`、値は`{"audio_record": bool, "iq_record": bool,
"meteor_record": bool}`のJSON——`log_broadcast_settings`と同じ「単一キーJSON blob」
パターン）。3チェックボックスの`toggled`シグナルハンドラ（`_on_audio_rec_toggled`等）
から`_save_recording_settings()`を呼び、`__init__()`（UI構築・`_reload_at_lists()`の
直後）で`_load_recording_settings()`を呼んで復元する。復元時は`blockSignals()`で
静かに反映するのみ（誤ってこの時点で`*_changed`シグナルを発火させても、
`MainWindow`側はまだこれらのシグナルに接続していないため、単に聞き逃されるだけで
意味がない）。

`MainWindow.__init__()`側では、`self._at_dialog = AutotrackRecordDialog(...)`構築
（＝ダイアログの`__init__()`内で復元が完了している）**の後、`*_changed`シグナルを
接続した直後**に、`is_audio_record_enabled()`等のgetter経由で明示的に
`self._autotrack_audio_record`等へ同期する処理を追加した。ダイアログの復元処理は
シグナル接続より前（`AutotrackRecordDialog`のコンストラクタ内）に起きるため、
「復元時にシグナルを発火させてMainWindow側に伝える」という設計では原理的に
間に合わない——この時系列の問題を、getterによる明示的な同期で解決した。

テスト: `tests/test_autotrack_record_dialog.py`（新規、6件）— 保存なしのデフォルトは
全OFF・トグルでapp_settingsに保存される・新しいダイアログインスタンスが保存値を
復元する・OFFへの変更も保存される・壊れたJSON値は例外を出さず無視される・復元時に
`*_changed`シグナルが発火しないことを検証。`tests/test_main_window.py`に
`TestAutotrackRecordingCheckboxSync`（2件）— `app_settings`に`meteor_record: true`を
仕込んだ状態で`MainWindow`を構築すると`_autotrack_meteor_record`が`True`になること、
保存値が無い場合は全て`False`のままであることを検証。

**検証状況**: 静的なコード修正・ユニットテストに基づく。実機での確認は報告者の
次回パス待ち（2026-08-22時点）。

**教訓**: 「ステータス表示（Tracking: ...）が正しく出ている」ことは、そこから
連鎖するはずの下流の自動化（今回はMETEORタブの自動オープン）が実際に実行されて
いることを一切保証しない——両者の間に、UIの見た目からは分からない条件分岐
（今回は`_autotrack_meteor_record`フラグ）が挟まっていることがある。またこのプロジェクトは
既に`app_settings`への永続化パターン（`log_broadcast_settings`等）を複数箇所で
確立しているにもかかわらず、Autotrack Recordingダイアログの3チェックボックスは
実装時にこのパターンを踏襲し忘れていた——「設定を変更する UI コントロールを
新設する際は、再起動をまたいで保持すべきかどうかを都度検討し、保持すべきなら
既存の`app_settings`パターンを最初から使う」ことを徹底する必要がある。

### 10. Autotrack「リスト:」コンボの選択が`AutotrackManager`に一切伝わっていなかった不具合（GitHub Issue #27、2026-08-23 発見・修正）

**症状**: 項目9（Recordingチェックボックス永続化）の修正後、開発者自身がAutotrack機能を
初めて使ってみたところ、リスト"Met"を作成しMETEOR M2-3をエントリーとして追加、
「METEOR / HRPT 受信」もチェック済みの状態で「自動追尾を有効化」をチェックしても、
状態欄に**「先にパス検索を実行してください」**（項目2で自動化したはずのメッセージ）が
表示された。Autotrack Controlの「リスト:」コンボには"Met"が正しく表示されており、
一見して設定に不備は無いように見えた。

**原因**: `AutotrackRecordDialog.populate_list_combo()`（リスト新規作成・削除・
名前変更のたびに`_reload_at_lists()`から呼ばれる）は、`clear()`/`addItem()`の過程で
Qtが発火する無関係な中間`currentIndexChanged`イベントを抑制するため、常に
`blockSignals(True)`で全体をガードしていた。しかしこれには重大な副作用があり、
**再構築後に実際にどのリストが選択された状態になっても、`autotrack_list_changed`
シグナルが一度も発火しない**。リストを1つしか作らなかった場合、コンボには
「唯一の選択肢」として自動的にそのリストが表示される（見た目上は選択済み）が、
`MainWindow`側は一度もそのリストIDを教えられておらず、`AutotrackManager`の
`_entries`は空のまま残る。`AutotrackManager.check()`は`if not self.is_ready or
not self._entries: return None`で即座にreturnするため、項目2の自動ウォームアップ
機能自体は正しく実装されていても、そもそも`entries()`が空なのでウォームアップの
対象が無く、常に「Run a pass search first」表示に落ちていた。

ユーザーがこの罠に気づけない理由: コンボに複数リストがあり、ユーザーが手動で
ドロップダウンを開いて選び直す操作（＝本物の`currentIndexChanged`）をした場合は
正しく動作する。Issue #27の最初の報告者（on7ndr）が2回目のテストでは動作したのは、
2つのリストを作りその間で切り替え操作を行っていたためと推測される。一方、
開発者自身のように**リストが1つだけ**で、コンボを操作し直す必要性を感じない
（既に選択されて見えるため）ケースでは、この抜け穴に確実に落ちる。

**修正**: `populate_list_combo()`を、再構築前後で「実際に選択されたリストID」を
比較し、**変化した場合のみ**`autotrack_list_changed`をemitするよう変更
（`current_list_id()`という公開getterも新設）。「変化した場合のみ」にした理由は、
無条件にemitすると、既にEnable Autotrackで有効な追跡が動いている最中に
（無関係な）別リストの名前変更等で`populate_list_combo()`が呼ばれるたびに
`MainWindow._on_autotrack_list_changed()`が発火し、`_autotrack_enabled = False`で
意図せず追跡を止めてしまう副作用があるため。

さらに、この修正だけでは**アプリ起動直後**のケースがカバーできないことが判明した。
`AutotrackRecordDialog.__init__()`内で`_reload_at_lists()`（初回のコンボ構築）が
実行されるのは、`MainWindow.__init__()`が`self._at_dialog.autotrack_list_changed
.connect(self._on_autotrack_list_changed)`を呼ぶ**より前**——つまり項目9の
Recordingチェックボックス復元と全く同じ「コンストラクタ内の初期化はシグナル接続前に
起きる」という時系列の罠に、今回も同じ形で引っかかる。`MainWindow.__init__()`側にも、
シグナル接続の直後に`self._on_autotrack_list_changed(self._at_dialog
.current_list_id())`を明示的に呼ぶ処理を追加し、起動時点で既に存在する唯一の
（または最後に選択されていた）リストを確実に`AutotrackManager`へ伝えるようにした。

テスト: `tests/test_autotrack_record_dialog.py`に`TestListComboSelectionSync`
（4件）— 初めてのリスト作成でそのIDがemitされる・選択が変わらないリネームでは
emitされない・リストが0件のとき`current_list_id()`が`None`・選択中のリストを
削除すると新しい選択（`None`）がemitされることを検証。`TestMainWindowSyncsInitialListSelection`
（2件）— `MainWindow`構築前にDBへリストとエントリーを直接INSERTしておき、構築後に
`w._autotrack.entries()`が正しく非空になっていること、リストが無い場合は空のままで
あることを検証（このクラスはQThread相当のMainWindowを直接構築するため、
`test_main_window.py`と同じ`_no_background_sync`オートユースフィクスチャを
このファイルにも追加する必要があった——追加を忘れた状態で一度実行したところ、
実際にバックグラウンドスケジューラのスレッドが起動し「AMSAT status fetch failed:
cannot schedule new futures after interpreter shutdown」等のエラーがテスト末尾に
出力されることを確認して発覚した）。

**教訓**: `blockSignals()`は「不要な中間シグナルを抑制する」という所期の目的は
正しく果たすが、**最終的な状態変化そのものまで一緒に握りつぶしてしまう**という
副作用を持つ。`blockSignals(True)`でガードされた再構築処理を書く際は、ガードを
解除した直後に「その結果、実効的な状態が変わったかどうか」を明示的に比較し、
変わっていればガードの外で改めて通知する、という2段構えを常に検討すること。
今回は項目9（Recordingチェックボックス）と全く同じ「コンストラクタ内の初期化が
シグナル接続より前に起きる」パターンの罠にも重ねて引っかかっており、
`AutotrackRecordDialog`のようにコンストラクタ内で外部に通知すべき初期状態を
持つダイアログを新設・変更する際は、毎回この時系列の罠を疑うこと。

### 11. Autotrackのentriesがリスト選択後の追加・削除・並び替えに追従しない不具合と、ステータス欄の2行折り返し表示崩れ（GitHub Issue #27、2026-08-23 発見・修正）

**症状**: 項目10の修正後、開発者自身がWindows実機で確認したところ「赤い文字が消え、
緑色で『Next: METEOR M2-4 in 96 min』のような表示が出るようになった」（症状1がある程度
改善したことを示す）一方、「アプリを起動したまま自動追尾画面で設定した直後は計算されて
いないように見え、ソフトを再起動して初めて計算される」という新しい症状を報告した。
加えてmacOS版では、同じステータステキストが2行に折り返され、2行目が下の枠に隠れて
読めない、という表示崩れも発見された。

**原因1（entries不追従）**: `AutotrackManager.set_list(list_id)`は、呼ばれた**その瞬間の
DBスナップショット**を`self._entries`にキャッシュするだけの設計だった。項目10で
「コンボの選択が変わった時に`set_list()`を呼ぶ」経路は直したが、**一度`set_list()`が
呼ばれた後に、そのリストへ衛星（エントリー）を追加・削除・並び替えしても、その変更は
`AutotrackManager`側のキャッシュに一切反映されない**という別の問題が残っていた。
`AutotrackRecordDialog`の「衛星を追加...」「削除」「▲▼」ボタン
（`_on_at_add_entry`等）はDBを更新して`_reload_at_entries()`（ダイアログ内のUIツリー
表示のみ）を呼ぶだけで、`MainWindow`側の`AutotrackManager`インスタンスには一切通知
していなかった。アプリ再起動時は`MainWindow.__init__()`が起動時に一度DBから最新状態を
読み込むため正しく動くが、起動したまま設定を変更しても`set_list()`が再度呼ばれない限り
古いスナップショットのまま、という症状と一致する。

**修正1**: `AutotrackManager.set_list()`のDB読み込みロジックを`_refresh_entries()`
として切り出し、`check()`・`next_satellite_info()`・`entries()`の**呼び出しのたびに**
呼ぶよう変更（1秒ごとに呼ばれる`check()`でも、Autotrackリストは通常1〜数機程度の
小規模なものなので、毎回DBを読み直すコストは無視できるという判断。本ファイル既出の
「World-map elevationキャッシュを廃止してlive per-tick observe()にした」判断と同じ
考え方）。`_refresh_entries()`は`self._state`（追跡状態、`current_norad`等）には一切
触れない設計とし、`set_list()`（ユーザーが明示的に別のリストへ切り替えた場合のみ
`self._state = AutotrackState()`で状態をリセットする）とは役割を分離した——これを
怠ると、1秒ごとに呼ばれる`check()`のたびに追跡状態がリセットされ、Rule 3
（パス途中は切り替えない）が機能しなくなってしまう。

**原因2（ステータス欄2行崩れ）**: `AutotrackRecordDialog`の状態ラベル
（`_at_status_label`）は`setWordWrap(True)`のみで、`QFormLayout`内の行の高さは
ラベルの`sizeHint()`から自動計算される。「Next: METEOR M2-3 in 463 min」程度の
長さの文字列でも、フォントメトリクスの違い（同じ文字列でもmacOSとWindowsでは
折り返される幅が異なる）により、macOSでは2行に折り返されることがあり、
`QFormLayout`が2行分の高さを確保していなかったため2行目が下の枠に隠れて見えなく
なっていた。

**修正2**: `_at_status_label.setMinimumHeight(self._at_status_label.fontMetrics()
.height() * 2 + 4)`で、常に2行分の高さを明示的に確保する。フォントサイズに依存しない
よう`fontMetrics().height()`から動的に計算する方式にした。

テスト: `tests/test_autotrack.py`に`TestEntriesRefreshFromDb`（4件）——
`set_list()`後に追加したエントリーが`check()`・`entries()`・`next_satellite_info()`の
いずれからも即座に見えること、`_refresh_entries()`自体は追跡中の状態
（`current_norad`等）を破壊しないことを検証。`tests/test_autotrack_record_dialog.py`に
`TestStatusLabelHeight`（1件）——ステータスラベルの`minimumHeight()`が
フォント1行分の高さの2倍以上であることを検証。

**検証状況**: 静的なコード修正・ユニットテストに基づく。実機での確認は開発者自身の
次回パス待ち（2026-08-23時点）。ステータス欄の表示崩れ修正はmacOS実機で目視確認予定。

**教訓**: `set_list()`のような「選択操作」メソッドが、内部で状態のリセットとデータの
読み込みという**2つの異なる責務**を同時に持っていると、「データだけを最新化したいが
状態はリセットしたくない」というニーズ（今回のケース）に応えられない。責務を分離して
おけば、`check()`のような高頻度で呼ばれる関数からも安全に「データの再読み込みだけ」を
呼び出せる。またUI崩れの調査では、同じテキスト・同じレイアウトでもOS・フォントの違いで
折り返し位置が変わりうることを踏まえ、`setWordWrap(True)`を使うラベルには常に
「実際に複数行になった場合の高さ」を明示的に確保しておくことが安全である。

### 12. Next Pass表示の矛盾、およびAutotrack Timer自動開始が「有効化を手動で外した」設定を無視する不具合（GitHub Issue #27、2026-08-23 発見・修正）

**症状1**: Autotrackリストに METEOR M2-3・M2-4 の2機を登録し、どちらもまだ地平線下という
状況で「自動追尾を有効化」した際、実際にはM2-4の方が早いパスであるにもかかわらず、
状態欄には常に「Next: METEOR M2-3 in N min」と表示された。

**原因1**: `AutotrackManager`内部では実際にはM2-4を正しく次の追跡対象として選び
`current_norad`にセットしていたが、UI表示ロジックとの間に矛盾があった。`check()`の
Rule 2b（「誰も見えていないので最速AOSの衛星を選ぶ」）は、その最速候補が既に`current`と
同じ場合（＝前回のtickで既に選択済みで変更なし）`None`を返す。一方、`_check_autotrack()`
は`check()`が`None`を返した場合にのみ`next_satellite_info()`を呼んで「Next: ...」を
表示するが、この`next_satellite_info()`は`current`を**常に**除外して次点を探す設計に
なっていた。1回目のtickでM2-4が正しく選ばれ`current_norad`になった後、2回目以降の
tickでは「currentと同じ最速候補」なので`check()`は`None`を返し、`next_satellite_info()`
がcurrent（M2-4）を除外してしまうため、次点のM2-3が誤って表示され続けていた。

**修正1**: `current`を除外すべきなのは「実際に可視状態（Rule 1でトラッキング継続中）」の
場合のみで、「まだ見えていないが次の候補として選ばれているだけ」の場合は除外すべきではない。
`elevations.get(current, -90.0) >= min_el`で`current`が実際に可視かどうかを判定し、
可視の場合のみ除外する`current_is_visible`フラグを導入した。

**症状2**: 「自動追尾を有効化」のチェックを外してダイアログをCloseしても、アプリを
再起動すると自動追尾が有効な状態で立ち上がってしまう。

**原因2**: `_check_autotrack()`のAutotrack Timer自動開始ロジックが、「現在時刻 ≥
開始時刻」を無条件に「開始時刻に到達した」の合図として扱っていた:
```python
elif self._autotrack.is_ready:
    now_utc = datetime.now(UTC)
    start_utc = self._at_dialog.get_timer_start_utc()
    if now_utc >= start_utc:
        self._autotrack_enabled = True  # 無条件でオンにしてしまう
```
一方、Autotrack Timerの「開始 (ローカル):」欄のデフォルト値は、ダイアログが新規構築
される（＝アプリ起動の）たびに必ず「今の時刻」にリセットされる
（`self._timer_start_dt.setDateTime(QDateTime.currentDateTimeUtc()...)`）。つまり
アプリを起動すると、1秒後の最初の`_check_autotrack()`呼び出しで「現在時刻 ≥ 開始時刻」が
必ず真になり（1秒経過すれば必ず過ぎているため）、`is_ready`（entriesがあり準備完了）
なら無条件で`self._autotrack_enabled = True`にされてしまっていた。加えて、Enable
Autotrackのチェック状態自体もこれまで一切永続化されておらず、仮にチェック状態だけを
保存・復元しても、復元直後の最初のtickでこのTimerロジックがすぐに`True`で上書きして
しまう構造だった。

**修正2（2段構え）**:
1. **Enable Autotrackのチェック状態を永続化**（`AutotrackRecordDialog`、新規キー
   `autotrack_enabled`、`"1"`/`"0"`の単純な文字列値。既存の`_RECORDING_SETTINGS_KEY`
   と同じ`app_settings`パターン）。`MainWindow.__init__()`では、既存の「リスト選択の
   復元」処理（`_on_autotrack_list_changed()`）が副作用として無条件に
   `_autotrack_enabled = False`にリセットしてしまうため、**その処理を呼ぶ前に**
   復元済みの状態を`restored_autotrack_enabled`として退避し、呼んだ**後**に再適用する
   という順序にした
2. **Timer自動開始ロジックを「遷移検出」方式に変更**: 「現在時刻 ≥ 開始時刻」が真である
   ことだけでなく、**その前に一度「現在時刻 ＜ 開始時刻」を観測している**
   （`self._autotrack_timer_armed = True`）ことも条件に加えた。これにより、起動直後に
   デフォルト値（今の時刻）のせいでたまたま「もう過ぎている」状態になっていても、
   `armed`が`False`のままなので発動しない。ユーザーが実際に未来の時刻を設定した場合は、
   次のtickで`now < start_utc`が観測されて`armed = True`になり、その後実際に時刻が
   経過した時点で正しく発動する。`_autotrack_timer_armed`は、Enable Autotrackを手動で
   OFFにした場合（`_on_autotrack_toggled(False)`）とリストを切り替えた場合
   （`_on_autotrack_list_changed`）にもリセットする——手動でオフにしたのに、Timerの
   開始時刻がまだ未来のまま残っていて後で勝手に再度オンに戻る、という同種の症状の
   再発を防ぐため。

テスト: `tests/test_autotrack.py`に`TestNextSatelliteInfoExcludesOnlyVisibleCurrent`
（2件）——`current`がまだ見えていない最速候補自身の場合はそれ自身が正しく"Next"として
返ること、`current`が実際に可視（Rule 1でトラッキング中）の場合は正しく除外されて
次点が返ることを検証（`PassInfo`を返すフェイクPredictorを新設）。
`tests/test_autotrack_record_dialog.py`に`TestAutotrackEnabledPersistence`（4件）・
`TestMainWindowRestoresAutotrackEnabled`（5件、うち1件は`_check_autotrack()`を
`get_timer_start_utc()`をモックして未来→過去に切り替える形で実際にarmed→発火の遷移を
検証、もう1件は手動OFF時に`armed`がリセットされることを検証）。

**検証状況**: 静的なコード修正・ユニットテストに基づく。実機での確認は開発者自身の
次回パス待ち（2026-08-23時点）。

**教訓**: 「currentを除外する」「現在時刻が閾値を超えたら発火する」といった一見単純な
条件分岐も、**その値がどうやってその状態になったか**（本当に可視なのか、単に前回の
選択が持ち越されているだけなのか／ユーザーが意図的に設定したのか、単にUIのデフォルト値が
たまたまそうなっているだけなのか）を区別しないと、意図と異なる場面で誤発動する。
特に「UIウィジェットのデフォルト値が実行のたびに現在時刻にリセットされる」設計は、
それを見る側のロジックが「一度でも異なる状態を経由したか」という遷移を意識しない限り、
静かに壊れる典型例だった。

### 13. リストへの衛星エントリー追加がパス予測ウォームアップを再トリガーしない不具合（GitHub Issue #27、2026-08-23 発見・修正）

**症状**: 項目12の修正（Enable Autotrack永続化・Timer遷移検出）を適用した後、
開発者自身が「自動追尾を有効化してから10分後のパス」でテストしたが、AOSになっても
METEORタブ内での接続・受信開始状態にならなかった（アプリを再起動すると直後に
正しく自動起動した）。

**原因**: `_start_autotrack_warmup()`（項目1、Group Passタブでの手動パス検索省略
自動化のために実装したウォームアップ機構）は、Autotrackリストが新規作成された
**直後（まだ衛星エントリーが0件の時点）**に一度だけ試行され、その時点では
`entries()`が空なので即座にno-opで終わる（`AutotrackManager.is_ready`は
`False`のまま）。しかし、`AutotrackRecordDialog._on_at_add_entry()`
（「衛星を追加...」ボタン）・`_on_at_remove_entry()`・`_on_at_move_up/down()`は
いずれも`_reload_at_entries()`（ダイアログ内のツリー表示更新のみ）を呼ぶだけで、
**ウォームアップを再度試みる仕組みが一切なかった**。つまり、「リストを作成→
（空の状態で一度ウォームアップが空振りする）→衛星を追加」という、ごく普通の
操作順序を踏むと、その後どれだけ経ってもリストにエントリーが後から追加されたことは
`_start_autotrack_warmup()`に伝わらず、`AutotrackManager.is_ready`は
**永遠に`False`のまま**になる。

`_check_autotrack()`のTimer自動開始ロジック（項目12で実装）は
`elif self._autotrack.is_ready:`という条件下でのみ動作するため、`is_ready`が
`False`のままだとTimer機構は一度も発火しない——「Enable Autotrackを直接手動で
チェックした場合」（`_on_autotrack_toggled(True)`が独立してウォームアップを
再トリガーするため正しく動く）ではこの問題が表面化せず、**Autotrack Timer機能
（時間が来たら自動的に有効化されることを期待する運用）でのみ**発生する、という
発見しにくい組み合わせだった。

**修正**: `AutotrackRecordDialog._reload_at_entries()`の末尾で、既存の
`lists_modified`シグナル（元々はリスト自体の追加・削除・リネーム時のみemitされ、
Radio Controlのリストコンボ更新に使われていた）を新たにemitするよう変更。
`_on_at_add_entry`/`_on_at_remove_entry`/`_on_at_move_up`/`_on_at_move_down`は
すべてこの`_reload_at_entries()`を経由するため、1箇所の変更で全ハンドラに効く。
`MainWindow._on_autotrack_lists_modified()`（`lists_modified`の受信側）に
`self._start_autotrack_warmup(silent_if_empty=True)`を追加。
`_start_autotrack_warmup()`自体は既に`is_ready`なら即座にno-opになる設計
（冪等）なので、この追加呼び出しは「まだ一度もウォームアップに成功していない
リストに、初めて衛星が追加された」ケースにのみ意味を持ち、既にreadyなリストへの
以後の追加・削除では単なる無害なno-opになる（＝一度成功した後は、後から追加した
衛星のパス予測を個別に再計算する仕組みはない。項目1のウォームアップは
「Autotrackを動かしてよい前提が整ったか」を示す一度きりのゲートであり、
`check()`が呼ぶ`_get_next_aos()`等はキャッシュに依存せずその都度計算するため、
これで実害はない）。

テスト: `tests/test_autotrack_record_dialog.py`に
`TestReloadAtEntriesSignalsListsModified`（2件）——`_reload_at_entries()`が
`lists_modified`をemitすること、選択リストがない場合は例外なくno-opで済むことを
検証。`tests/test_main_window.py`の`TestAutotrackWarmup`に2件追加——
空リスト作成直後にウォームアップが空振りした後、衛星を追加すると
`_reload_at_entries()`経由で`is_ready`が`True`になること、一度readyになった後の
エントリー追加・削除では`get_passes()`が再実行されない（no-opのまま）ことを検証。

**テスト作成中に発覚した別の設計上の注意点**: `AutotrackRecordDialog`は
「Autotrack Lists」枠の`_at_list_widget`（リスト自体を選択、エントリー編集対象を
決める）と、「Autotrack Control」枠の`_at_sel_combo`（`current_list_id()`、
Enable Autotrackで実際に有効化する対象、MainWindowに伝わる）という、**見た目は
似ているが独立した2つの選択状態**を持つ。新規リスト作成時は`_at_sel_combo`側は
自動選択されるが（項目10で修正済み）、`_at_list_widget`側は自動選択されない
（`QListWidget.addItem()`だけでは`currentRowChanged`が発火しない）。ユーザーが
実際に左側のリスト一覧をクリックしてから「衛星を追加...」を押す通常の操作では
問題にならない（`_at_selected_list_id`が`None`のままだと「先にリストを選択して
ください」という明示的な警告が出るため、気づかれずに別のリストへ追加される、
という誤操作は起きにくい）が、テストコードで`AutotrackRecordDialog`を直接構築して
`_reload_at_entries()`等を呼ぶ場合は、`_at_list_widget.setCurrentRow(0)`を
明示的に呼ばないと`_at_selected_list_id`が`None`のままになる点に注意
（今回のテスト実装時に一度踏んだ）。

**検証状況**: 静的なコード修正・ユニットテストに基づく。実機での確認は開発者自身の
次回パス待ち（2026-08-23時点）。

**教訓**: Issue #27を通じて繰り返し発覚しているパターンとして、「ある操作
（今回は衛星エントリーの追加）が、別の独立した機構（ウォームアップ・エントリー
再読み込み）を再トリガーする必要があるのに、両者をつなぐ配線が最初から存在
しない」という抜け漏れが多い（項目11の`_refresh_entries()`、項目13の
`lists_modified`再利用はいずれも同じ形のバグ）。新しい状態変更ハンドラ
（追加・削除・並び替えボタン等）を実装する際は、「この変更を、他のどのコンポーネント
（ウォームアップ・キャッシュ・UI表示）が知る必要があるか」を明示的に洗い出し、
既存の類似ハンドラ（リスト自体の追加・削除等）が持つ通知経路を横展開できているか
確認すること。

### 14. AOS直後・LOS直前の低仰角域でパスが重ならない衛星同士に誤って切り替わり、SatDumpのデバイスクレームエラーを引き起こす不具合（GitHub Issue #27、2026-08-23 発見・修正）

**症状**: v0.3.36で項目9〜13を修正した後、開発者自身がmacOS実機（METEOR M2-3・M2-4を
登録したAutotrackリスト）でテストしたところ、SatDump起動が
`Kernel driver is active, or device is claimed by second instance of librtlsdr.`
`usb_claim_interface error -3`（`exit code 1`）で失敗した。「アプリを再起動すれば
直る」という体感から、当初は項目9〜13のAutotrack自動化機能全体を撤回し、
「Enable Autotrack」チェック時に再起動を促すポップアップを出す方式へ戻すことが
検討されたが、実際の原因はAutotrackのウォームアップ／Timer機構とは無関係の、
**衛星切り替え判定ロジック自体（`AutotrackManager.check()`）のバグ**だった。

**調査**: RTL-SDR自体は当時取り外されており「USB接触不良」という仮説は否定された。
`ps aux`でFBSAT59プロセスの子に`<defunct>`（SatDumpのゾンビ）が1件見つかり、
少なくとも1回はSatDump起動が試行・失敗していたことを確認。ユーザーへの確認で
「2機登録していたが、パスが重なることは全くない」という証言を得た。物理的な
パスの重なりが無いにもかかわらず切り替えが起きるとすれば、**Autotrackの内部判定
ロジックが誤って早期に切り替えている**はずだと考え、`check()`と`_get_next_aos()`を
精査した。

**根本原因**: `_get_next_aos()`は`p.aos >= now`でフィルタするため、**現在進行中の
パス（既にAOSを過ぎたが、まだLOSしていない）を除外し、次の周回のAOSを返す**設計。
一方、Rule 1（`AutotrackManager.check()`）は`current`の仰角が`min_el`
（デフォルト5.0度）以上でなければ継続追跡と判定しない。つまり、**仰角0〜5度の
グレーゾーン**（AOS直後でまだmin_elに届いていない、あるいはLOS直前で沈みつつある）
では、Rule 1は「継続追跡ではない」と判定してRule 2bへフォールバックし、現在追跡中の
衛星自身の`_get_next_aos()`は「次の周回」（例えば90分後）を誤って返してしまう。
もしリスト内の別の衛星の次のAOSがそれより早ければ、**パスが物理的に重なっていなくても
Rule 2bが誤ってその衛星へ切り替えてしまう**。この切り替えにより
`_autotrack_on_los()`（現在のSatDump停止）→`_autotrack_on_aos()`（次のSatDump起動）が
短時間に連続実行され、前のSatDumpの`--finish_processing`後処理（画像合成、数秒〜
十数秒）が完了する前に次のSatDumpが同じRTL-SDRデバイスを掴もうとして
「already claimed」エラーになる。

なお`_check_autotrack()`（main_window.py）側には既に、`el < 0`（真の地平線下）を
基準にした明示的なLOS判定が別途存在しており（本ファイル「起動時鮮度チェックの
網羅的監査」節等とは無関係の既存コード）、**設計者の意図としては「LOS＝真の地平線下
（0度）」だった**にもかかわらず、`AutotrackManager.check()`のRule 1/2b側は`min_el`
（5.0度）だけを基準にしており、この2つの閾値の不一致がグレーゾーンを生んでいた。

**修正**: Rule 1に「`current`の仰角が`min_el`未満でも、真の地平線下（0度未満）で
ない限りパス継続中とみなし、他の衛星へ切り替えない」というガードを追加
（`src/core/autotrack.py`の`check()`）。これにより、`_check_autotrack()`の明示的
LOS判定（`el < 0`）と閾値が統一され、Rule 2b（`_get_next_aos()`の「次の周回」
返却問題）が現在追跡中の衛星に対して誤発動することもなくなる。

テスト: `tests/test_autotrack.py`に`TestMidPassBelowMinElDoesNotSwitch`（2件）——
`current`の仰角が`min_el`未満・0度以上（低仰角・パス継続中）の状態で、より早い
次点AOSを持つ別の衛星が存在しても切り替わらないこと、`current`の仰角が真に負
（地平線下）になった場合は既存通り正しく切り替わることを検証。

**検証状況**: 静的なコード修正・ユニットテストに基づく。実機（macOS）での再確認は
次回パス待ち（2026-08-23時点）。

**教訓**: 「一度でも修正してもまだ直らない」という報告を受けたときに、これまでの
一連の修正全体を疑い撤回する（対症療法・再起動の強制）方向に流れそうになったが、
ユーザーへの一言確認（「パスは本当に重なっていないか」）が、内部ロジックのバグに
たどり着く決め手になった。「物理的に重ならないはずの2つのイベントが、アプリ内では
連続して発生しているように見える」という食い違いは、額面通りの外部要因（USB・
デバイス競合）を疑う前に、まず**内部の判定ロジックが同じ2つの閾値
（`min_el`と真の地平線）を一貫して使っているか**を疑うべきだった。同一プロジェクト内に
「min_el基準」と「0度基準」という2つのLOS/AOS判定基準が別々の場所に存在すること
自体が、以前（項目12）の「Next Pass表示の矛盾」でも一度踏んだのと同型の落とし穴
（一貫性のない閾値がグレーゾーンで食い違う）だった。

### 15. Autotrackトグル時の「再起動を促すポップアップ・自動再起動」を全面撤回（GitHub Issue #27、2026-08-23 実装→同日撤回）

**経緯**: 項目14を修正しリリースした後も、開発者自身のWindows 11タブレット実機
（RTL-SDR、80度の好パス）で「自動追尾を有効化してAOSを待ったが自動接続されない」
という報告があり、これを受けて①「自動追尾を有効化」チェック時に必ず警告ポップアップを
表示②OK押下で実際にアプリを自動再起動する、という機能を実装・リリースした
（当時のコミット`2b02a07`・`dede556`・`f93d661`）。

しかし実機で試したところ、以下の悪化が報告された:
- 再起動後もAOSで自動接続されない
- 手動終了→再起動後、Autotrackが自動的にSDR接続を試みたが
  `exited with code 1`（SDRデバイスをオープンできない）で失敗
- 「自動追尾の有効化」をオフにして再起動後、**手動で**METEORタブを開いてSDR接続・
  受信を開始したところ、途中までは正常に受信していたが、ロックが外れて停止ボタンを
  押しても**SatDumpの受信プロセスが終了せず**、アプリを強制終了せざるを得なかった
- 「今まで正常だった手動受信までおかしくなった」

**根本原因（推定）**: `MeteorTab.closeEvent()`は元々、SatDumpの終了を最大3秒待ち、
それでも終わらなければ`force=True`（**SIGKILL**）で強制終了する設計になっている
（本ファイル「クラッシュリスク」対応セクション参照）。自動再起動はEnable Autotrackの
トグル操作の**瞬間に無条件で**発火するため、もしその瞬間にSatDumpが受信中（かつ
`--finish_processing`の後処理でまだ終了できない状態）だった場合、`closeEvent()`の
3秒待ちがタイムアウトしてSIGKILLに至る。SIGKILLされたSatDumpはRTL-SDRデバイスの
USBハンドルを正常に解放する機会を与えられず、これがWindows環境でデバイス自体を
不安定な状態（ソフトの再起動だけでは治らず、以降の手動受信まで巻き込んで壊れる）に
陥らせたと推測される。ユーザーからの「なぜOKボタンでの自動再起動はダメで、
ユーザーが手動で再起動するならOKなのか」という問いに対しては、**自動か手動かの
違いそのものは本質ではなく**、自動化によって「ユーザーが受信中かどうかを判断して
タイミングを選ぶ余地」が失われ、危険なタイミング（受信中）で再起動が起きる頻度が
上がっていた、というのが正確な説明になる。

**対応**: ユーザーの明確な判断により、警告ポップアップ・自動再起動の仕組み全体
（`AutotrackRecordDialog._warn_restart_required()`とその呼び出し、
`src/core/app_restart.py`・`tests/test_app_restart.py`、
`main.py`の`_acquire_single_instance_lock()`に追加したリトライロジック、
関連テスト一式）を`git revert`で完全に撤回した（対象コミット3件をまとめて1コミットで
打ち消し）。「警告だけ出して自動再起動はしない」という中間案も検討したが、
「警告を見た直後にユーザーが手動再起動すれば結局同じ危険なタイミング問題を
抱える」として、ユーザー判断によりこちらも不採用となった。

**教訓**:
- 手作業でのコード削除（Editツールで1行ずつ差分を戻す）を試みかけたが、ユーザーから
  「git履歴で元に戻せばいいのでは」と指摘を受けた。直近の独立したコミット群が
  対応する変更を丸ごと取り消したい場合は、`git revert`（該当コミットをまとめて
  `--no-commit`で適用してから1つのコミットにする）の方が、手作業の切り貼りより
  確実かつ安全——この判断を早い段階でできなかったのは反省点
- 「再起動すれば直る」という現象の原因を、必ずしも「再起動の実行方法（自動/手動）」
  に求めるべきではない。今回の本当の変数は「受信中かどうか」というタイミングで
  あり、自動化はそのタイミング制御をユーザーから奪っただけだった。ユーザーからの
  「なぜ自動はダメで手動はいいのか」という問いは、こちらの説明の甘さを的確に
  突いたものだった
- 対症療法（今回は「トグルのたびに強制再起動」）を追加する際は、それが**既存の
  安全機構（`closeEvent()`の3秒タイムアウト→SIGKILL）と衝突しないか**を事前に
  検討すべきだった。個々の機構は単体では合理的でも、組み合わせると新たな障害
  モードを生むことがある

### 16. Enable Autotrack有効化時点でAOSが誤って一度だけ発火し、本当のAOSでは二度と発火しない不具合（GitHub Issue #27、2026-08-24 発見・修正）

**症状**: 項目9〜15の一連の修正後もなお、「自動追尾を有効化」がチェックされ
「METEOR / HRPT Reception」チェックボックスもオンの状態でAOSを待っても、
METEORタブの自動起動・SDR接続が一度も発生しない、という報告があった。
Autotrack Controlのステータス欄には「Tracking: 衛星名」ではなく
**「Next: 衛星名 in N min」がAOS予定時刻を過ぎても表示され続けている**ことを
ユーザーに確認してもらい、これが決定的な手がかりになった。

**根本原因**: `AutotrackManager.check()`のRule 2b（「誰も見えていない場合、
最速AOSの衛星を選ぶ」、[autotrack.py](src/core/autotrack.py)）は、その衛星が
**まだ地平線のはるか下にあっても**、リストの中で唯一の（または最速の）候補であれば
即座に`_state.current_norad`として確定してしまう設計だった。一方
`MainWindow._check_autotrack()`は、この`current`の変化（`is_new_sat`）を**その
まま「AOSが発生した」と解釈**し、無条件で`_autotrack_on_aos()`（リグ/ローテーター
接続・音声/IQ録音開始・METEORタブ自動起動をまとめて行う関数）を呼び出していた。

実際にユニットテストで再現・確認済み: 90分後にAOSが来る衛星（現在の仰角-30°）に
対し、Autotrack有効化直後の最初の`check()`呼び出しで`current_norad`が即座に
その衛星へ確定することを確認した。

```
check() result on first tick (satellite still below horizon): (57166, 'test-xpdr-uuid')
current_norad after this tick: 57166
```

つまり実際に起きていたのは:
1. 「自動追尾を有効化」チェック直後（ウォームアップ完了後の最初の1秒tick）、衛星が
   まだ地平線の何十分も下にあっても、`_autotrack_tracking_norad`が即座にその衛星の
   NORADにセットされ、`_autotrack_on_aos()`が**一度だけ誤って発火**する。この時点
   では信号が無いため、SatDumpの起動は無意味に終わる
2. `_autotrack_tracking_norad`は既にその衛星のNORADで埋まってしまっているため、
   **本当にAOSが到来しても`is_new_sat`は`False`のまま**——`_autotrack_on_aos()`は
   二度と呼ばれない

これはMETEORタブに限らず、リグ接続・ローテーター接続・音声/IQ録音の自動開始も
すべて同じタイミングバグの影響を受ける（`_autotrack_on_aos()`が全部まとめて呼ぶため）。

**さらに見落としていた第2の欠陥**: 「currentが変わった（`is_new_sat`）」タイミング
にだけAOSトリガーのロジックを実装する初版の修正では、実は不十分だった。
`check()`のRule 1（「currentがmin_el以上、またはmin_el未満でも真の地平線above
（0度以上）ならまだ継続追跡中」）は、衛星の仰角が一度のtickで一気にmin_el
（デフォルト5.0度）を超えてしまうケース（低頻度ポーリングや、テストで-30度→+10度と
一足飛びに変化させたケース）で、**`is_new_sat`が真になるタイミングを経由せず、
最初から`result is None`（Rule 1で「継続追跡中」）として処理されてしまう**ことが、
実際にこの初版修正へのユニットテスト作成中に発覚した。つまりAOS発火のチェックは
「`current`が切り替わった瞬間」だけでなく、**`check()`が`None`を返す（＝Rule 1で
継続追跡と判定される）分岐でも、`_autotrack_tracking_norad`が設定されている限り
毎tick行う必要がある**。

**修正**:
- `MainWindow`に`self._autotrack_aos_fired: bool`を新設。「`_autotrack_tracking_norad`が
  次に追いかける予定になった」ことと「実際に地平線を超えた（真のAOS）」ことを分離する
- 共通ヘルパー`_maybe_fire_autotrack_aos(norad, sat_name, el) -> bool`を新設し、
  `_autotrack_aos_fired`がまだ`False`かつ`el >= 0.0`（真の地平線above）の場合にのみ
  `_autotrack_on_aos()`を呼んで`True`を返す。このヘルパーを`_check_autotrack()`の
  **`result`が`None`の分岐（Rule 1で継続追跡と判定されたケース）と、`result`が
  非`None`の分岐（Rule 2a/2bで切り替えが起きたケース）の両方**から呼ぶことで、
  上記の第2の欠陥（一足飛びの仰角変化）もカバーする
- `_autotrack_on_los()`の冒頭に`if not self._autotrack_aos_fired: return`のガードを
  追加。AOSが一度も発火していない対象（Rule 2bでまだ地平線下のまま予約されているだけの
  衛星）に対して、Autotrack無効化・リスト切替・Timer失効等で誤ってLOS処理
  （未接続のリグを切断しようとする等、実害はないが無駄な処理）が走るのを防ぐ。
  呼び出し元（3箇所）は変更不要——ガードをこのメソッド自身に集約したことで、
  呼び出し漏れのリスクも構造的に排除した
- `_autotrack_on_aos()` → `_meteor_autotrack_aos()`の呼び出しにtry/exceptを追加
  （既存の音声/IQ録音開始と同じパターンに統一。以前はここだけ無防備で、例外が
  起きると`_check_autotrack()`全体が静かに中断されうる状態だった）
- AOS発火の瞬間・METEOR/HRPT起動の各段階（`METEOR_NORAD_IDS`不一致でのスキップ・
  タブ検出失敗）に診断ログ（`logger.info`/`logger.warning`）を追加。今後同種の
  報告があった場合、`fbsat59.log`で「AOSは発火したがMETEORタブが見つからない」
  のか「AOSそのものが発火していない」のかを即座に切り分けられるようにした

**テスト**: `tests/test_main_window.py`に`TestAutotrackAosTiming`（4件）・
`TestAutotrackOnLosGuard`（2件）を新設。地平線下ではAOSアクションが延期されること・
実際に地平線を超えた瞬間に一度だけ発火すること・可視のまま継続する間は再発火しない
こと・Rule 2a（既に可視）は初回tickから即座に発火すること・AOS未発火時の
`_autotrack_on_los()`が実質no-opであることを検証。既存のAutotrack関連テスト23件・
`tests/test_autotrack.py`10件・`test_rig.py`165件すべて回帰なしを確認済み。

**教訓**: 「`current`（次の追跡対象）が確定した」ことと「実際にAOSが発生した」こと
は、意味的には全く別の事象なのに、既存コードは同じ状態変化（`is_new_sat`）だけを
シグナルとして両方を扱っていた。さらに、その状態変化自体も「値が変わった瞬間」だけ
を見ていたため、値が一度のtickで閾値を飛び越えて変化するケース（Rule 1が「継続追跡」
と判定してしまう）を取りこぼす、という二重の見落としがあった。「イベントAが起きたら
アクションBを実行する」という設計では、Aの検出条件（今回は`current`の変化）が
Bが本来必要とする条件（今回は実際の可視性）と完全に一致しているかを、境界ケース
（一気に閾値を飛び越える、値が最初から条件を満たしている等）まで含めて確認すること。
また、ユーザーへの「Tracking表示だったかNext表示だったか」という一言の確認が、
2つの全く異なる仮説（早期発火 vs 内部同期ミス）を一つに絞り込む決め手になった——
症状の見た目が同じでも、UIの正確な文言を確認するだけで原因調査の分岐点が変わる
好例だった。

### 17. AOSタイミング修正（項目16）で初めて表面化した、Rig 1/2自動接続とMETEOR受信のSDR取り合い（GitHub Issue #27、2026-08-24 実機確認・修正）

**症状**: 項目16の修正をリリースした直後、実際にMETEOR M2-3の実パスで検証した
ユーザーから、「AOS発火・METEORタブ自動起動までは正しく動作するようになったが、
SDR接続が`satdump exited with code 1`で失敗する」という報告があった。ローテーター
未接続による`Connection refused`エラーも同時に出ていたが、これはユーザー自身が
「想定内」と正確に認識していた。

提供されたログ（`fbsat59.log`・SatDump側ログ）を突き合わせたところ、時系列が
決定的な証拠になった:

```
08:52:10 Autotrack AOS fired: norad=57166 ...
08:52:10 SdrRigAdapter.connect: sample_rate=1000000 ...     ← Rig 1/2としてRTL-SDRを接続
08:52:12 SDR opened: Generic RTL2832U OEM ...                ← FBSAT59本体が先に掴む
08:52:13 SDRPipeline started
08:52:13 Rotator: connect failed — Connection refused        ← 無害（ローテーター未接続）
08:52:13 Autotrack METEOR/HRPT autotrack_start invoked       ← ここでSatDumpを起動
```
SatDump側:
```
Kernel driver is active, or device is claimed by second instance of librtlsdr.
usb_claim_interface error -3
Could not open RTL-SDR device!
```

**根本原因**: `_autotrack_on_aos()`（[main_window.py](src/ui/main_window.py)）は
「Rig 1/2自動接続 → 録音開始 → METEORタブ起動」という順序で処理する。ユーザーは
このRTL-SDRをRig 1（またはRig 2）にも割り当てていたため、Autotrackが最初に
Rig接続でこのデバイスを掴んでしまい、直後にMETEORタブが独自にSatDumpで同じ
RTL-SDRを開こうとして「既に別インスタンスに掴まれている」と衝突していた。
METEOR/HRPT受信は元々Rig 1/2の接続状態とは無関係にRig Settings > SDR Settingsを
直接読んでSatDumpが自分でデバイスを開く設計（本ファイル「トランスポンダーと
周波数の独立性」参照）のため、Rig側で先に掴む必要は本来ない。

この競合自体はAutotrackの`_autotrack_on_aos()`に元から潜在していたバグだが、
項目16の修正でAOSアクションが初めて正しいタイミングで実行されるようになった
ことで、初めて実機で発現した——項目16修正前は、Enable Autotrack時点の誤発火に
巻き込まれてMETEORタブが（信号のないタイミングで）誤って起動していたため、
この種のデバイス競合が意味のある形で表面化する機会自体がなかったと考えられる。

**修正**: `_autotrack_on_aos()`のRig 1/Rig 2自動接続それぞれに、「そのRigが
`is_sdr`（`SdrRigAdapter`）であり、かつ`_autotrack_meteor_record`
（METEOR / HRPT Receptionチェックボックス）が有効な場合は、そのRigの自動接続を
スキップする」という条件を追加。Rig 1・Rig 2は独立に判定するため、
「Rig 1=物理リグ（ドップラー追尾用）・Rig 2=このRTL-SDR」のような構成でも、
Rig 1は従来通り自動接続され、Rig 2（SDR）だけがMETEORタブに道を譲る。
「METEOR / HRPT Reception」がオフの場合は従来通りRig 1/2とも自動接続する
（挙動は変更なし）。スキップした場合は診断用に`logger.info()`を出す。

テスト: `tests/test_main_window.py`に`TestAutotrackOnAosSkipsSdrRig`（4件）を
新設。SDR×METEOR有効→スキップ・非SDR×METEOR有効→接続（既存挙動維持）・
SDR×METEOR無効→接続（既存挙動維持）・Rig 2側での対称性、を検証。既存の
Autotrack関連テスト27件・`test_rig.py`165件すべて回帰なしを確認済み。

**教訓**: SoapySDRデバイスは1プロセス専有という既知の制約（本ファイル冒頭
「SDR フェーズ2」節等で既出）は、SDR Controlタブと外部ツール（SatDump・
gr-satellites等）の間の競合としては認識されていたが、**同一アプリ内の
「Rig 1/2としてのSDR接続」と「METEORタブが独自に起動するSatDump」という
2つの独立した経路が同じ物理デバイスを取り合いうる**という組み合わせは、
項目16のタイミング修正で初めて実際に発生する条件が揃うまで、誰も気づいて
いなかった。ある不具合を直すと、それまで別の不具合（今回はデバイス競合）が
発現する条件が揃っていなかっただけで実は隠れていた、というケースがある——
1つの修正のリリース後も「直ったはず」で終わらせず、実機での追加検証を
継続する価値がある好例だった。

### 過去の受信フォルダをタブ内で見返す機能（`📂 Open Past Reception…`、2026-08-20 実装）

#### 背景

受信直後の画像プレビュー・サムネイル履歴は、そのタブインスタンスが生きている間
（＝そのパスを受信した直後）しか見られず、後日`~/Pictures/fbsat59_meteor/{日時}/`
配下の過去フォルダを見返すには`📁 Open Folder`で外部のファイラー/写真ビューアを
開くしかなかった。ユーザーから「特定の日付のフォルダを選んだら、受信直後と同じ
ようにタブ内に表示できないか」との要望があり実装した。

#### 実装

- `_btn_open_past`（`📂 Open Past Reception…`ボタン、`📁 Open Folder`の隣）:
  `QFileDialog.getExistingDirectory()`を`~/Pictures/fbsat59_meteor/`起点で開き、
  選んだフォルダ配下の全PNG（`rglob("*.png")`）を`_load_images_from_folder()`で
  読み込む
- **既存の履歴は入れ替え**（ユーザー確定）: 呼ぶたびに`_history_list.clear()`して
  から読み込む。PNGが1枚も見つからない場合は`QMessageBox.information()`で
  案内するのみで、既存の履歴はそのまま残す（誤って空フォルダを選んで
  それまでの表示を失うことがないように）
- 受信中（`▶ Start`〜`■ Stop`〜`finished_ok/err`の間）はボタンを無効化
  （`_on_start()`で`setEnabled(False)`・`_reset_controls()`で`setEnabled(True)`）。
  ライブ受信中の履歴と過去フォルダの内容が混ざらないようにするための単純な排他制御

#### メインプレビューに最初に表示する1枚の選択（`_image_priority()`、ユーザー確定）

1回の受信でSatDumpが生成する約12枚のPNG（各チャンネルの生グレースケール・
複数のFalse Color合成×補正あり/なし・十分なデータ範囲があれば地図オーバーレイ
付き/地図投影済みのバージョン）のうち、従来は**検出された順の最後の1枚**が
プレビューに残るという、特に意図のない挙動だった（本ファイル前掲「実際の
良好にロックできても画像が一度も生成されない不具合」調査で見つかった課題）。

`_image_priority(filename)`をファイル名の部分文字列でランク付けする単純な
ヒューリスティックとして新設し、**ライブ受信（`_on_new_image()`）・過去
フォルダ読み込み（`_load_images_from_folder()`）の両方で共用**する。

**初版は`projected`を最優先にしていたが、実機のスクリーンショットで誤りと
判明し即日修正した**（2026-08-20）。`_map`系ファイル（`msu_mr_rgb_MSA_map.png`
等）は実測`1568×1376`——生スワスと同じフレームで、受信データそのものの上に
海岸線オーバーレイを描いただけ——なのに対し、`projected`系ファイル
（`rgb_msu_mr_rgb_..._projected.png`）は実測`4096×2048`固定の**全球分の
キャンバス**で、実際にデータがあるのはそのうちの細い帯だけ。これを最優先に
していたため、実際にユーザーが試すと「プレビュー欄に、ほとんど真っ黒な
世界地図（緑の海岸線ワイヤーフレーム）が表示され、右上の隅にわずかに衛星
画像の帯が写っているだけ」という状態になっていた（地理的な正確さでは
`projected`の方が優れているが、"一目で見て分かる"プレビューとしては最悪の
選択だった）。

最終的な優先順位: `_map`かつ`corrected`両方を含む=4 > `_map`のみ=3 >
`corrected`のみ=2 > それ以外（生チャンネル等）=1 > `projected`=0
（**最下位**）。`_map`と`corrected`を両方満たすファイル（例:
`msu_mr_rgb_MSA_corrected_map.png`）に専用の最高順位を設けたのは、単純な
「`_map`>`corrected`」の2値判定だけだと、同じ`_map`ティア内で複数候補
（色補正あり/なし）が並んだ場合にファイル名のアルファベット順というだけで
決着してしまい、意図せず補正なしの方が選ばれることがあったため（`_corrected_map`
と`_map`はソート順で"c"<"m"のため、後者が"後着優先"のタイブレークで勝って
しまっていた）。

ライブ受信では`self._preview_priority`（`_on_start()`で`-1`にリセット）を
「現在プレビューに表示中の画像の優先度」として保持し、新しい画像が届くたびに
`priority >= self._preview_priority`のときだけプレビュー・選択状態を更新する
（`>=`なので同順位内では後着優先＝従来の「最後の1枚」という感触を保ちつつ、
一度でも上位の画像——特に地図投影済み画像——が来たら、それより下位の画像が
後から来てもプレビューを奪われない）。サムネイル履歴自体には優先度に関わらず
**すべての画像を追加**するので、ユーザーはいつでも履歴からクリックして他の
画像を見られる。

過去フォルダ読み込みでは、フォルダ内の全PNGを`sorted()`（ファイル名順、
決定的な順序にするため）で走査し、履歴に全件追加しつつ最高優先度の1枚を
`best_item`として記録、最後に`self._history_list.setCurrentItem(best_item)`で
選択する（`currentItemChanged`シグナル経由で既存の`_on_history_selection()`が
呼ばれ、プレビュー表示自体は新規コードなしで実現できる）。

### METEORタブのライブWaterfall表示（2026-08-20 実装・実機確認済み）

#### 背景

METEOR受信中はSatDumpがSDRを排他的に握るため、SDR Controlタブでは電波の有無を
目視確認できない（受信を止めないと確認できない）という不便さがあった。加えて
METEOR/HRPTは「少しずつ画像が出てくる」昔の気象FAX方式ではなく、
`--finish_processing`（本ファイル前掲）による後処理が終わるまで画像が一切出ない
仕組みのため、受信中（数分〜十数分間）ずっとプレビュー欄が真っ黒のままになり、
「本当に電波を受信できているか」が受信完了まで分からないという不満があった。

前掲11h（「次回の作業候補」）で調査済みだった`satdump live`の
`--fft_enable --fft_size N --fft_rate N --http_server 127.0.0.1:PORT`
（SatDump自身がローカルHTTPサーバー`GET /api`でFFTスペクトラム値`fft_values`を
配信する、SDRの排他制御に影響しない内蔵機能。v1.2.2のバイナリに文字列として
実在することを`strings`コマンドで実機確認した上で着手）を使い、受信画像プレビュー
欄を**Image / Waterfallの2タブ**に分割する形で実装した。

#### 設計（ユーザー確定）

- **タブ切り替えは自動2回のみ**: ▶ Start押下時にWaterfallタブへ、受信完了時に
  Imageタブへ、それぞれ自動切替する。受信中にユーザーが手動で別タブへ切り替えた
  場合はそれ以降強制的に戻さない（自動切替が発生するのはこの2箇所のみ）
- **アイドル時（Start前）はImageタブがデフォルト**: 前回受信した画像・履歴が
  すぐ見られる状態を維持する
- **周波数軸は較正しない**: SatDumpの生`fft_values`のビン並び順・スケールは
  非公開のため、誤ったHz軸を表示するよりは軸なしの方が安全と判断。目的は
  「電波が来ているかどうか」の定性的な確認のみで、周波数精度の診断ではない

#### 実装

- `src/comms/meteor/satdump.py`: `SatDumpProcess`に`fft_http_port`引数を追加。
  指定時のみ上記4フラグをコマンドラインに付与する。FFTサイズ/レートは
  `_FFT_SIZE=512`・`_FFT_RATE=2`固定（信号確認用途のため軽量値で十分、UI設定は
  設けていない）
- `src/comms/meteor/fft_waterfall.py`（新規）: `find_free_port()`（空きTCPポート
  確保）と`SatDumpFftPoller`。`core.doppler_worker.DopplerWorker`・
  `comms.ft4.rx_capture.Ft4RxCaptureWorker`と同じ設計方針——**素の
  `threading.Thread`＋コールバック（`QThread`/`Signal`を継承しない）**——を踏襲し、
  Qtのイベントループなしでテスト可能にした。`urllib.request`で`/api`をポーリング
  （既存の`*_dialog.py`系ダウンロードワーカーと同じHTTPクライアント流儀）し、
  接続失敗が`_MAX_CONSECUTIVE_FAILURES`（15回、約6秒）続いた場合のみ一度だけ
  `on_unavailable`を報告する（SatDumpのHTTPサーバー起動には数秒かかることがある
  ため、起動直後の失敗は黙ってリトライする）
- `src/ui/meteor_waterfall.py`（新規）: `MeteorWaterfallWidget`。直近約2分間
  （`_HISTORY_ROWS=240`行、ポーラーの約2.5Hzポーリングに対応）のローリング
  ウィンドウを保持し、新しいFFTスナップショットが届くたびに5〜99.5パーセンタイル
  で正規化して`ui.ft4_waterfall_dialog`と同じパレット（黒→青→緑→黄→赤）で
  描画する。パレット・色マッピング関数は共有インポートにせず独立コピー
  （`ft4_waterfall_dialog.py`自身の「無関係な機能を結合させない」という既存の
  方針を踏襲）
- `src/ui/meteor_tab.py`: 受信画像表示部を`QTabWidget`化（Image/Waterfallの2タブ）。
  `SatDumpFftPoller`のコールバックはバックグラウンドスレッドから呼ばれるため、
  `MeteorTab`自身が持つ非公開Signal（`_fft_frame_received`/`_fft_unavailable`）
  経由でGUIスレッドへブリッジする（`Ft4Tab`の`period_skipped`と同型のパターン）。
  ポーラーは`_on_stop()`では止めず、`_on_finished_ok()`/`_on_finished_err()`
  （SatDumpの`--finish_processing`後処理が実際に終わった時点）まで動かし続ける
  ——`ImageWatcher`が`_stop_watcher_after_final_poll()`で行っているのと同じ理由
  （SatDump自身のHTTPサーバーもプロセスが本当に終了するまで生きているため）

#### テスト

`SatDumpFftPoller`はQt非依存のため`tests/test_meteor_fft_waterfall.py`で実際の
`http.server.HTTPServer`スタブを使い検証（4件・Qt不要）。`MeteorWaterfallWidget`
は新規QWidgetのため`tests/test_meteor_waterfall_widget.py`で`qtbot`使用（9件、
本ファイル前掲の「QWidget/QDialogを構築するテストは必ず`qtbot.addWidget()`を
使うこと」の教訓に従う）。

#### 実機確認（2026-08-20、METEOR-M N2-3、最大仰角約50°のパス）

- ▶ Start押下でWaterfallタブへの自動切替・受信完了でImageタブへの自動切替（12枚の
  画像・サムネイル表示）とも、想定通り動作することを実機で確認
- **色分けの見え方**: パーセンタイル正規化は本ウィジェット固有の仕様ではなく、
  直近ウィンドウ全体を毎回再計算して塗り分けるため「絶対的な信号強度」ではなく
  「その時点の直近2分間の中での相対順位」を表す。実際のパスでは、EL 13.5°
  （Lock前）時点では帯全体が赤（＝その時点までの2分間では最強だった）で
  表示され、EL 51.9°（Lock後）まで仰角が上がりSNRの実ダイナミックレンジが
  広がると、同じ帯が相対的には中位（緑〜黄）に落ち着き、代わりに瞬間ピークの
  細い縦線だけが新たに最上位（赤）として浮かび上がる、という遷移が確認できた。
  この一見「弱い時は赤・強い時は緑」に見える挙動は、固定スケールではなく
  適応的パーセンタイル正規化を採用したことによる自然な副作用であり、実装上の
  不具合ではない（同じ設計を採用している既存の`ui.ft4_waterfall_dialog`と
  挙動は同一）
- 中央付近に時間軸方向でほぼ動かない細い赤の縦線が2本見えるケースを確認。
  幅の広い帯（実際のLRPT信号と推定）とは見た目で区別でき、位置が一定な点から
  RTL-SDR系でよくあるLO漏れ（"DCスパイク"/"birdie"）等、受信機自身が出す
  固定周波数の妨害成分である可能性が高いと考えられる。周波数較正をしていない
  ため、正確に中心1ビンにあるのか、それとも中心からずれた別の妨害波なのかは
  この画面だけでは断定できない
- **「電波が来ているか」を確認するという当初の目的は達成できていることを確認**
  （実信号の帯と妨害波は幅・位置の安定性で見た目上区別できるため）。ユーザー
  判断により、上記の細い赤線を除外する追加対応は行わず現状のまま据え置き
  （実害が小さいため）
- 受信完了後の画像自体がノイジーだった点は、最大仰角約50°という低めのパスに
  よる信号強度不足が主因と考えられ、本機能（Waterfall）とは無関係
