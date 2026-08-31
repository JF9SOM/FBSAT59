# CI/CD トラブルシューティング履歴

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## CI/CD トラブルシューティング履歴（v0.1.0-beta.34 で解決済み）

v0.1.0-beta.34 の CI 作業で判明した重要な知見。同様のエラーに遭遇したときのために記録する。

### Hamlib 4.7.x ソースビルド共通

（4.7.2 でも同じ。バージョンアップのたびに tarball の構造を再確認すること）

**問題**: `hamlib_wrap.c: No such file or directory`  
**原因**: Hamlib 4.7.x ソースtarballには SWIG が生成する `hamlib_wrap.c` が含まれない（`.swg` ファイルのみ）  
**解決**: ビルド前に `swig -python -Iinclude -Ihamlib-4.7.2/include -o bindings/hamlib_wrap.c bindings/hamlib.i` を実行

**問題**: `hamlib/config.h: No such file or directory`  
**原因**: `config.h` は autotools が生成するファイル。tarball・zip には含まれない  
**解決**: 必要な define のみ含む最小スタブを手動作成してインクルードパスに配置

### macOS 固有

**問題**: `symbol(s) not found for architecture arm64`（Python シンボルリンクエラー）  
**解決**: `clang` コンパイル行に `-undefined dynamic_lookup` を追加（macOS では Python シンボルを明示的にリンクしない）

**問題**: dylibbundler が `/tmp` シンボリックリンクで自己削除エラー  
**原因**: macOS の `/tmp` は `/private/tmp` へのシンボリックリンク。コピー元とコピー先が同一パスに解決される  
**解決**: prefix を `/tmp/` ではなく `$HOME/hamlib-portable-mac/` に変更

**問題**: dylibbundler が `--dest-dir` と同じ場所を参照して無限ループ  
**解決**: `--dest-dir ${PORTABLE_LIB}/deps` + `--install-path @loader_path/../deps` に変更

### Windows 固有

**問題**: `ImportError: DLL load failed while importing _Hamlib`（ABI ミスマッチ）  
**原因**: MSVC でコンパイルした `.pyd` と MinGW でビルドした `libhamlib-4.dll` は ABI が合わない  
**解決**: Python binding のコンパイルも MinGW GCC に統一。`hamlib-w32-4.7.x.zip`（32bit）ではなく `hamlib-w64-4.7.x.zip`（64bit）を使用

**問題**: Python 3.8+ で PATH 経由の DLL 探索が効かない  
**解決**: `os.add_dll_directory()` を使用（`main.py` 起動ブロックに実装済み）

**問題**: PyInstaller が作成した `dist\fbsat59\` が空になる  
**原因**: Windows Defender のリアルタイムスキャンが新規作成された未署名 exe/DLL を検疫  
**解決**: PyInstaller 実行前に `Set-MpPreference -DisableRealtimeMonitoring $true` を追加  
**追加対策**: `choco install nsis` は必ず PyInstaller より前のステップで実行する（PyInstaller 後に実行すると dist が消える可能性）

**問題**: `File: "dist\fbsat59\" -> no files found.`（NSIS）  
**原因**: NSIS の `File` コマンドは相対パスを **スクリプトファイルの場所**（`scripts\`）基準で解決する。CWD 基準ではない。`File /r "dist\fbsat59\"` は `scripts\dist\fbsat59\` を探しに行く  
**解決**: `File /r "..\dist\fbsat59\"` に変更（`scripts\` の一つ上 = リポジトリルート）

**問題**: `Can't open output file` / `Output: scripts\dist\FBSAT59-Setup.exe`（NSIS）  
**原因**: `OutFile` も同様にスクリプトファイル基準で解決される  
**解決**: `OutFile "..\dist\FBSAT59-Setup.exe"` に変更

> **NSIS パス解決の原則**（重要）:
> `scripts\installer.nsi` 内のすべてのファイル系ディレクティブ（`File`・`OutFile`・`Icon` 等）は、**スクリプトファイルが置かれているディレクトリ**（`scripts\`）を基準に相対パスを解決する。リポジトリルートの `dist\` を参照するには必ず `"..\dist\..."` と書くこと。  
> なお `makensis` コマンドライン引数（`/DAPP_VERSION` 等）や PowerShell 側の変数は CWD 基準で問題ない。

**問題**: `Error: invalid VIProductVersion format, should be X.X.X.X`（NSIS）  
**原因**: `VIProductVersion` は Windows リソースの仕様で `X.X.X.X`（数値4フィールド）必須。`0.1.0-beta.34` は不正  
**解決**: CI で `-beta.34` を除去して `.0` をパディングした `VIVERSION=0.1.0.0` を別途計算し、`/DVIVERSION=$viVer` で渡す。表示用の `APP_VERSION` は semver のまま維持

```powershell
$numericVer = ($ver -replace '-.*$', '')
$parts = $numericVer.Split('.')
while ($parts.Count -lt 4) { $parts += '0' }
$viVer = ($parts[0..3] -join '.')
makensis /DAPP_VERSION=$ver /DVIVERSION=$viVer scripts\installer.nsi
```

### ft4wsjt（libft4wsjt）ビルド固有（2026-07-05 解決・3プラットフォームCI緑確認済み）

`build-ft4wsjt.yml`（WSJT-X の `lib/ft4_decode.f90` から `libft4wsjt` をビルド、詳細は Communications > FT4 セクション参照）の CI 実装時に判明した知見。

**問題**: macOS で `fatal error: 'boost/crc.hpp' file not found`（`brew install boost` 実行後も発生）  
**原因**: Apple Silicon版 Homebrew は `/opt/homebrew` にインストールされるが、clang/g++ はこのパスをデフォルトでは検索しない  
**解決**: `scripts/build_ft4wsjt.sh` で `fftw3.f03` と同様に `boost/crc.hpp` の実在パスを候補ディレクトリ（`/usr/include`・`/usr/local/include`・`/opt/homebrew/include`・`${CONDA_PREFIX}/include` 等）から探索し、`crc14.cpp` のコンパイルに明示的に `-I` で渡す

**問題**: macOS でコンパイルは通るがリンク時に `ld: library 'fftw3f' not found`  
**原因**: 上記と同根。ライブラリ探索パスも `/opt/homebrew/lib` がデフォルトでは通っていない  
**解決**: `fftw3.f03` が見つかったインクルードディレクトリから `${FFTW3_F03_DIR%/include}/lib` を導出し、リンクコマンドに `-L` で明示的に渡す（Homebrew・conda-forge 双方のディレクトリ構成に対応できる導出方法）

**問題**: Windows で FFTW3/Boost を conda-forge から取得する際、`conda create` の依存解決に20分以上かかり終わらない  
**原因**: Miniforge をサイレントインストールして通常の `conda` ソルバーで解決する方式は、Boost のような依存関係の大きいパッケージで著しく遅い  
**解決**: `mamba-org/setup-micromamba@v2` アクションに置き換え。同じ conda-forge パッケージでも数分で解決完了する。`init-shell: bash` を指定し、後続ステップは `shell: bash -el {0}`（ログインシェル）にすることで `$CONDA_PREFIX` が自動設定され、`build_ft4wsjt.sh` 側の探索ロジック（上記2件と共通）がそのまま機能する

**追加の見落とし**: Windows ビルドで FFTW3 の実行時 DLL（`fftw3f*.dll`）を出力に同梱し忘れていた（MinGW ランタイムDLLのみコピーしていた）。ビルドと ctypes ロードは別物であり、リンクが通っても実行時に依存 DLL が同梱されていなければ `ctypes.CDLL()` は失敗する。`$CONDA_PREFIX/Library/bin/*fftw3f*.dll` を出力ディレクトリにコピーして解決

---
