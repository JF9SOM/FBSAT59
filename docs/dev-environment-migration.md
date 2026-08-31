# 開発環境移行 — Ubuntu → macOS（2026-08-15）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## 開発環境移行 — Ubuntu → macOS（2026-08-15）

開発機をUbuntu（GPD MicroPC2、ユーザー`sadatoshi`、`gpd-linux.local`でSSH到達可能）から
Mac（ユーザー`sadatoshikoike`）に移行した。以降、FBSAT59の開発はMac単体で完結する
（Ubuntu機は引き続き起動していればSSHで到達可能だが、日常的な開発には使わない）。

### 移行したもの

- **Claudeメモリ**: `~/.claude/projects/<プロジェクトパスのハッシュ化文字列>/memory/` は
  マシンごとのローカルディレクトリのため、別マシンへは自動的に引き継がれない。
  `rsync -avz sadatoshi@gpd-linux.local:~/.claude/projects/-home-sadatoshi-FBSAT59/memory/
  ~/.claude/projects/-Users-sadatoshikoike-FBSAT59/memory/` の要領で直接コピーする必要がある。
  同一Mac内で別チャットを開く分にはこの操作は不要（プロジェクトパスが同じであれば自動的に
  同じメモリを参照する）
- **未コミットスクリプト7本**（`scripts/test_ft991_*.py`・`soundcard_level_meter.py`等、
  実機診断用でgit未追跡だったもの）
- 姉妹プロジェクト2つも同時に移行済み（詳細は下記）: **FBSAT59PP**（ハムフェア2026プレゼン
  資料、`~/FBSAT59PP`）・**ctld-launcher**（`~/ctld-launcher`、`JF9SOM/ctld-launcher`）

### 移行時の判断基準: 「実体」と「ローカル生成物」を区別する

3プロジェクトいずれも、**Linux上でビルドされたネイティブバイナリ・依存関係一式は
そのままコピーせず、Mac上で該当ツールチェーンを使って作り直した**。理由はアーキテクチャ
（x86_64 → arm64）とOS（Linux → macOS）の両方が変わるため、コンパイル済みバイナリを
含むディレクトリは原理的に持ち越せない：

| プロジェクト | コピーしなかったもの | Mac上での再構築方法 |
|---|---|---|
| FBSAT59PP | `scripts/node_modules`（134MB、`sharp`等ネイティブアドオン含む） | `npm install`（`package-lock.json`はコピー済み） |
| ctld-launcher | `.venv`・`build`・`dist`・各種キャッシュ・`hamlib-bundle`（すべて`.gitignore`対象） | `git clone` + `pip install -e ".[dev]"`。`pytest`146件パス・`ruff`/`mypy`クリーンを確認済み |

判断に迷ったら「その場で再生成できる（依存関係マニフェストがある）か」を基準にする。
実際の成果物（pptx・スクリーンショット・PNGアイコン素材等、OS非依存のデータ）は
そのまま`rsync`でコピーしてよい。

### Hamlibの重複整理（2026-08-15）

移行直後、Mac上にHamlibが4箇所に分散していた: Homebrew・radioconda（`~/radioconda`、
conda-forge製のham radio向けディストリビューション）・`~/src/hamlib-4.7.2`（手動ソース
ビルド）・FBSAT59の`.venv`。調査の結果、実際に必要なのは以下の2箇所だけで、他は
削除しても実害がないと判明した:

- **FBSAT59用**: `.venv`内に自前ビルドしたPythonバインディング
  （`_Hamlib.so` + `libhamlib.4.dylib`）。外部依存はHomebrewの`libusb`のみ
- **ctld-launcher用**: `.app`バンドル内（`Contents/Resources/hamlib/`）に`@rpath`経由で
  完全自己完結にバンドル済み

削除の判断は毎回`otool -L`で実際のリンク先を確認してから行った。特に
**Homebrewの`hamlib`は`brew uses --installed hamlib`が`gpredict`を依存先として表示するが、
`otool -L`で`gpredict`バイナリを直接見ると実行時に`libhamlib.dylib`をリンクしていない
ことが判明**（brewの依存関係表はビルド時依存であり、実行時の必須リンクではなかった）。
このため`brew uninstall --ignore-dependencies hamlib`で安全に削除できた。radioconda側も
`conda remove --dry-run`で他パッケージへの影響がないことを確認してから削除。
`~/src/hamlib-4.7.2`のビルドツリー（189MB、成果物は`.venv`にコピー済みで不要）も削除。
合計約3.9GB解放。

**教訓**: `conda clean --packages`は「どの環境からも参照されていない未使用キャッシュ全体」を
消す設計で、hamlib関連だけにスコープを絞れない。依頼は「hamlibの削除」だったが、実行した
結果hamlib以外の未使用パッケージキャッシュ3.65GB分も巻き込んで削除してしまった
（実害はないが依頼範囲を超えていた）。今後、cache-clean系コマンド（`conda clean`・
`brew cleanup`・`npm cache clean`等）を使う際は、対象を絞れる狭いコマンドが存在するか
先に確認し、なければ実行前にスコープの広さを明示して確認を取ること。

### デスクトップ起動用 `.app` ランチャー

単体の`.command`シェルスクリプト（Desktop上でダブルクリック実行する用途）には、実運用で
以下の構造的な弱点があると判明した:
1. Dockに登録してもアイコン表示が不安定（真っ白になることがある）
2. `fileicon`で設定したFinderのカスタムアイコン（拡張属性由来）は、そのファイルを
   後から書き換える（エディタが新規ファイルとして書き直す実装だと特に）と消える
3. Dockからのクリック起動が信頼できない（単体スクリプトはDockが想定する起動経路と
   相性が悪い）

これらを回避するため、**正式な`.app`バンドルでラップする方式に統一**した
（FBSAT59・ctld-launcherとも同じ構造）:

```
XXX.app/Contents/
  Info.plist          # CFBundleIconFile=icon, CFBundleExecutable=XXX
  MacOS/XXX           # ダブルクリック/Dockクリックで実行される起動スクリプト
  Resources/icon.icns # プロジェクトのassets/配下から生成・コピー
```

`.app`バンドルなら、アイコンはバンドル内リソース（Info.plist参照）として保持されるため
消えず、Dockでの表示・起動も本来の仕組みで正しく動作する。

**起動スクリプト（`Contents/MacOS/XXX`）の中身は用途で使い分ける**:

- **コンソール出力を見たい場合**（FBSAT59。Qt GUIアプリのログ・エラーを確認したいため
  ターミナルを開いたまま実行したい）:
  ```bash
  DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
  open -na /Applications/Ghostty.app --args -e "$DIR/run.command"
  ```
  Ghostty（後述）は`wait-after-command=false`がデフォルトのため、`run.command`
  （venv activate → 実行）が終了すると同時にウィンドウが自動的に閉じる
- **コンソール不要・直接GUI起動したい場合**（ctld-launcher。トレイ常駐アプリのため
  ターミナルが開くとむしろ邪魔）:
  ```bash
  cd "$HOME/ctld-launcher"
  source .venv/bin/activate
  exec ctld-launcher
  ```

**Terminal.appでの試行錯誤（最終的にGhosttyへ切り替えたため実害はないが、教訓として
記録）**:
- AppleScriptの`do script`でTerminal.app自体を未起動状態から起動させると、Terminalの
  「起動時デフォルトウィンドウ」と競合し、意図しない空ウィンドウが余分に開くことがある。
  `open -a Terminal <script>`（Finderのダブルクリックと同じ起動経路）を使う方が安全
- `tell application "Terminal" to count windows`は、実際には既に閉じたウィンドウを
  ゾンビ参照として数え続けることがある。真の画面状態を確認したい場合は
  `System Events`のAccessibilityツリー（`tell application "System Events" to tell
  process "Terminal" to count windows`）の方が信頼できる
- ウィンドウを「シェル終了時に自動で閉じる」ようにするには、自前のAppleScript
  close-by-ttyトリックより、Terminal.appプロファイル自体の`shellExitAction`設定
  （`~/Library/Preferences/com.apple.Terminal.plist`の
  `Window Settings:<プロファイル名>:shellExitAction`、`1`=正常終了時に閉じる）の方が
  堅牢
- **Ghostty採用後はこれらの問題が一切発生しない**（`open -na Ghostty.app --args -e
  <command>`でコマンド完了時に自動クローズ、余分なウィンドウも発生しない）。以降、
  ターミナルを表示したいランチャーはTerminal.appではなくGhosttyを使う方針とする

### 姉妹プロジェクトの移行詳細

- **FBSAT59PP**（`~/FBSAT59PP`、ハムフェア2026プレゼン資料。gitリポジトリではない
  プレーンフォルダ）: pptx本体2点・スクリーンショット8点・生成スクリプト
  （`build_deck.js`等、pptxgenjs）・アイコン素材をコピー。`node_modules`のみMac上で
  `npm install`。`node scripts/build_deck.js`で19枚のスライドが正常に再生成されることを
  確認済み。**Linux固有の実機スクリーンショット撮影手順（Xvfb仮想ディスプレイ経由の
  ヘッドレス起動）はMacでは使えない**（Macには実ディスプレイがあるためXvfb自体不要だが、
  `scripts/screenshot_driver_template.py`はMac向けに書き直しが必要。既存スクリーンショットを
  使う分には問題ない）
- **ctld-launcher**（`~/ctld-launcher`、`JF9SOM/ctld-launcher`の正規リポジトリ）:
  `git clone`で取得。`hamlib-bundle/`ディレクトリは`core/hamlib_locator.py`のソースを
  確認した結果、**PyInstallerパッケージング時（`scripts/ctld-launcher.spec`）にのみ
  読まれ、通常の開発時実行（`ctld-launcher`コマンド）では`sys._MEIPASS`
  （フリーズ時のみ設定）かシステムPATHしか見ない**と判明。つまり開発作業そのものには
  `hamlib-bundle/`の中身は無関係（動作確認済みのmacOS版バイナリを一応配置したが、
  必須ではない）

---
