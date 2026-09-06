# Windows 実機への SSH アクセスと開発環境

Mac（主開発機）から Windows 実機へ SSH で入り、**任意のコマンドを実行できる**汎用の
開発アクセス。ログ確認・pytest・Python レベルのデバッグ・パッケージ状態の確認・
ソースの最新化・スクリプトの試走など、Linux 機（`ssh GPD-MicroPC`）と同じ感覚で使える。
従来の「タグを打つ → CI ビルド → ダウンロード → インストール」を毎回回さずに、
Windows 固有の挙動を短サイクルで確認できる。**2026-09-03 構築。**

> ユーザーが「Windows に SSH で入って」「Windows 実機で〜を確認して」と言ったら、
> 用件がログ取得とは限らない。まずこのファイル全体（特に次の 2 セクション）を読み、
> `ssh windev` で目的のコマンドを組み立てて実行する。

---

## いちばん使うコマンド

Mac のターミナルから：

```bash
# ログ本体を取得（Rig/Hamlib/CI-V 系のログはこれ）
ssh windev "type %LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log" | tail -200

# 低レベル Hamlib CI-V トレース
ssh windev "type %LOCALAPPDATA%\fbsat59\fbsat59\Logs\hamlib_trace.log" | tail -200

# 最新ソースに更新（Windows 側はローカル変更ゼロなので常に ff で通る）
ssh windev "cd %USERPROFILE%\FBSAT59 & git pull --ff-only"

# test_rig.py（フルスイートやGUIテストは回さない。CLAUDE.md の方針どおり）
ssh windev "cd %USERPROFILE%\FBSAT59 & .venv\Scripts\python.exe -m pytest tests\test_rig.py -q"

# ネイティブ依存の再取得（通常は不要。--force で最新化）
ssh windev "cd %USERPROFILE%\FBSAT59 & .venv\Scripts\python.exe scripts\bootstrap_natives.py --force"
```

- `ssh` の宛先エイリアスは **`windev`**（Mac の `~/.ssh/config` に定義済み）。
  `HostName FUJITSU.local`（mDNS。**IP が変わっても追従**）、`User pc`、鍵認証（`~/.ssh/id_ed25519`）。
- パスワードは不要。パスプロンプトで固まらない。
- 日本語のエラーメッセージは cmd の CP932 で**文字化けする**が、`git` / `python` /
  `pytest` の出力は ASCII なので問題ない。

---

## 任意のコマンドを実行する（汎用）

デフォルトシェルは **cmd.exe**。基本形：

```bash
ssh windev "<cmdの1コマンド>"
ssh windev "cd %USERPROFILE%\FBSAT59 & <cmd1> & <cmd2>"      # & で連結（&& でも可）
```

シェル差の対応表（Linux 機の癖で書かないこと）：

| やりたいこと | Linux/mac | Windows(cmd) |
|---|---|---|
| ファイル表示 | `cat f` | `type f` |
| 一覧 | `ls` | `dir` / `dir /b` / `dir /s /b` |
| 環境変数展開 | `$VAR` | `%VAR%`（例 `%USERPROFILE%` `%LOCALAPPDATA%` `%PROGRAMFILES%`） |
| 検索 | `grep` | `findstr /I /C:"pat" file` |
| 複数行の一部 | `head`/`tail` | Mac 側でパイプして `| tail -200`（cmd に head/tail は無い） |

**クォートの入れ子**（`python -c` などを渡すとき）— cmd はバックスラッシュでの
クォートエスケープをしない。外側 `ssh windev "..."` の中の cmd レベルのクォートは
`\"` でエスケープし、Python コードの中は**シングルクォート**を使う：

```bash
ssh windev "cd %USERPROFILE%\FBSAT59 & .venv\Scripts\python.exe -c \"import sys; print(sys.version)\""
```

**PowerShell を使いたいとき**は bash 側をシングルクォートで囲む：

```bash
ssh windev 'powershell -NoProfile -Command "Get-Service sshd | Format-List Status,StartType"'
```

**長時間動くもの・GUI**：`python -m src.main`（GUI）は SSH 越しには画面が出ない。
インポート確認だけなら `set QT_QPA_PLATFORM=offscreen &` を前置。
mac には `timeout` コマンドが無いので、止め時間を制御したいときは Bash ツールの
`timeout` 引数か、バックグラウンド実行＋後で kill を使う。

---

## 運用ルール（重要）

- **コード変更は必ず Mac 側リポジトリで行い、commit → push する。**
  Windows のファイルは直接編集しない。Windows 側は常に「上流のクローン」で
  ローカル変更ゼロを維持する（`git pull --ff-only` が常に素直に通る状態）。
- Windows 実機でしか再現しない不具合の調査でも、修正パッチは Mac で当ててから
  `ssh windev "... git pull ..."` で受け取って検証する。

---

## Windows 側の構成

| 項目 | 値 |
|---|---|
| ホスト名 | `FUJITSU`（`FUJITSU.local` で mDNS 解決） |
| 主ユーザー | `pc`（管理者） |
| SSH サーバー | OpenSSH Server（winget パッケージ `Microsoft.OpenSSH.Preview`。`sshd` サービス Automatic） |
| Python | 3.11.x（winget `Python.Python.3.11`。Mac の 3.11 に合わせている） |
| Git | Git for Windows（winget `Git.Git`。`core.autocrlf=input`） |
| リポジトリ | `C:\Users\pc\FBSAT59`（`git clone https://github.com/JF9SOM/FBSAT59.git`） |
| 仮想環境 | `C:\Users\pc\FBSAT59\.venv`（`pip install -e .[dev,sdr,notifications,ax100digi]` 済み） |
| ログ | `%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log` / `hamlib_trace.log` |
| インストール版 | `C:\Program Files\FBSAT59\`（通常配布の .exe。SDR 用に流用、後述） |

### ネイティブ依存（`scripts/bootstrap_natives.py` が導入）

`platformdirs.user_data_dir("fbsat59")` = `%LOCALAPPDATA%\fbsat59\fbsat59\` 配下へ、
アプリが元々参照している場所と同じ場所に展開する。

| 部品 | 取得元（GitHub リリース／pip） | 展開先 |
|---|---|---|
| Hamlib | `hamlib-bundle` タグ（`JF9SOM/fbsat59`） | `…\hamlib\` |
| ft8lib（FT8/FT4 デコード） | `ft8lib-bundle` | `…\ft8lib\` |
| q65lib（Q65） | `q65lib-bundle` | `…\q65lib\` |
| ft4wsjt（WSJT-X FT4 エンジン） | `ft4wsjt-bundle` | `…\ft4wsjt\` |
| direwolf（APRS） | `direwolf-bundle`（**`/releases/latest` ではない**。プレリリースタグ） | `…\direwolf\` |
| CW デコーダモデル | `raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx` ＋ `pip install onnxruntime` | `…\cwmodel\` |
| lameenc（MP3 録音） | `pip install lameenc` | venv |

冪等（導入済みならスキップ）・オフラインでも起動を止めない・ランチャーを失敗させない設計。

**音声・DSP 系の pip 依存（`sounddevice` / `scipy` / `pyusb` / `soundfile` / `plyer` /
`reed-solomon-ccsds`）は `bootstrap_natives.py` では入れない。** これらは
`pyproject.toml` の `[sdr]` / `[notifications]` / `[ax100digi]` エクストラに属し、
venv セットアップ（および `win_launch.bat` の再インストール）で
`pip install -e .[dev,sdr,notifications,ax100digi]` として入れる。`[dev]` だけで
venv を作ると SDR Control の音声再生が `ModuleNotFoundError: sounddevice` で無音になり、
FT4 タブのレベルメータも「sounddevice not installed」表示のまま振れず、
テレメトリー/APRS の SDR 復調も `scipy` 欠落で動かない（2026-09-06 に実機で発生）。

---

## sshd の自己復旧（ガーディアン）— 2026-09-06 追加

2026-09-06、`Get-Service sshd` が「サービスが見つかりません」になり Mac から入れなく
なった（機能更新で `Microsoft.OpenSSH.Preview` が外れたと思われる。上のトラブル
シューティング表に手動復旧手順あり）。再発しても自動で戻るよう、Windows 側に常駐の
復旧タスクを入れてある。**リポジトリ外**（`git pull` の邪魔をしないため）に置く。

| 部品 | 場所 |
|---|---|
| ガーディアンスクリプト | 原本 `scripts/sshd_guardian.ps1`（コミット済み）→ 実機では `C:\ProgramData\fbsat59-sshd-guardian.ps1` へコピーして使う（リポジトリ外に置くので `git pull` に影響しない） |
| 実行ログ | `C:\ProgramData\fbsat59-sshd-guardian.log`（512KB で末尾400行に自動トリム） |
| スケジュールドタスク | `FBSAT59 sshd guardian` — 起動時＋1時間ごと、`SYSTEM` / RunLevel Highest |
| sshd 障害時アクション | `sc.exe failure sshd reset= 86400 actions= restart/5000/restart/10000/restart/60000`（スクリプトが毎回再適用） |

スクリプトが毎回やること（すべて冪等）:
1. `%WINDIR%\System32\OpenSSH\sshd.exe` が無ければ `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`
2. ホスト鍵が無ければ `ssh-keygen -A`、`sshd_config` が無ければ `sshd_config_default` からコピー
3. `administrators_authorized_keys` に Mac の公開鍵が無ければ書き戻し＋`icacls`（SYSTEM+Administrators のみ）
4. `sshd` サービスが無ければ `New-Service` で再登録＋`sc config obj= LocalSystem`＋障害時アクション
5. ファイアウォール規則 `sshd`（TCP22 / Profile Any）が無ければ再作成
6. スタートアップ種別を Automatic に、停止していれば `Start-Service`

新しい Windows 機で再構築するときは「新しい Windows 機で再構築する場合」の手順9を参照。
最終手段（OpenSSH 本体ごと復旧不能）は Chrome Remote Desktop で GUI から手動対応。

---

## ワンクリック起動（デスクトップ）

- **`scripts/win_launch.bat`**（コミット済み）: `git pull --ff-only` →
  `pyproject.toml` に差分があった時だけ `pip install -e .[dev,sdr,notifications,ax100digi]` →
  `bootstrap_natives.py` → `python -m src.main`。
  末尾に `pause` を置いていないので、**アプリ終了と同時にコンソールも閉じる**。
  実行中はログがコンソールに流れる。
- **`scripts/bootstrap_natives.py`**（コミット済み）: 上表のネイティブ依存の
  ヘッドレスダウンローダ。`--force` / `--only a,b` / `--skip a,b` / `--quiet`。
- **デスクトップのショートカット** `C:\Users\pc\Desktop\FBSAT59.lnk` → 上記 `.bat`。
  作業フォルダ = リポジトリルート、アイコン = `assets\icon.ico`。

---

## SDR（ローカル USB）をソース実行で使う仕組み

Windows では **デバイス列挙にも `import SoapySDR` が必須**（`SdrDevice.enumerate()` は
`SOAPY_AVAILABLE` が False だと空を返す。ctypes 直接実装は open/stream 部分のみ）。
SoapySDR は Windows では pip パッケージが無いため、ソースチェックアウトからは
そのままでは SDR サブシステム全体が無効になる。

対策として `src/main.py` に **frozen 用ブロックと対になる非 frozen 用ブロック**を追加してある：

- 条件: `win32` かつ **非 frozen** かつ `import SoapySDR` 不可 かつ
  `%PROGRAMFILES%\FBSAT59\_internal\SoapySDR.py` が存在
- 動作: その `_internal`（インストール版に同梱の SoapySDR 一式。CPython 3.11 x64 で
  ABI 一致）を `sys.path` に **append**（venv のパッケージは上書きしない）、
  `os.add_dll_directory`、`SOAPY_SDR_PLUGIN_PATH` 未設定なら
  `_internal\soapy_modules` をセット
- **配布 .exe には一切影響しない**（`getattr(sys, "frozen", False)` が True のため即スキップ）。
  CI（Linux）でも Windows 非 frozen 限定なので素通り。

前提: RTL-SDR / HackRF は Zadig で **WinUSB ドライバー**適用済みであること。
TCP Remote SDR（SoapyRemote）は当面対象外。

---

## 制約

- **GUI（PySide6 の画面）は SSH 越しには出せない。** 見た目の確認は Chrome Remote
  Desktop か実機で。SSH でできるのは CLI 作業（ログ・pytest・Python レベルの調査）。
- Windows は ICMP を既定で遮断するので `ping FUJITSU.local` は失敗する（正常。SSH は通る）。
- SSH のデフォルトシェルは cmd.exe。PowerShell を使いたいときは
  `ssh windev "powershell -NoProfile -Command \"...\""`。

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `ssh windev` がタイムアウト | Windows で `Get-Service sshd`。停止なら `Start-Service sshd`。ファイアウォール規則 `OpenSSH SSH Server Preview (sshd)` の `Profile` が `Any` か確認（`Set-NetFirewallRule -DisplayName "OpenSSH SSH Server Preview (sshd)" -Profile Any`） |
| `Get-Service sshd` が「サービスが見つかりません」（2026-09-06 実際に発生。Windows Update で Preview パッケージが外れたと思われる） | OpenSSH Server 自体が消えている。管理者 PowerShell で `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` → それでも `sshd` サービスが登録されない場合（`install-sshd.ps1` が同梱されない版がある）は手動で: `& 'C:\Windows\System32\OpenSSH\ssh-keygen.exe' -A`（ホスト鍵＋`C:\ProgramData\ssh` 生成）→ `Copy-Item C:\Windows\System32\OpenSSH\sshd_config_default C:\ProgramData\ssh\sshd_config` → `New-Service -Name sshd -BinaryPathName '"C:\Windows\System32\OpenSSH\sshd.exe"' -DisplayName "OpenSSH SSH Server" -StartupType Automatic` → `sc.exe config sshd obj= LocalSystem` → `Start-Service sshd`。続けて FW 規則 `New-NetFirewallRule -Name sshd -DisplayName "OpenSSH SSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any` と、鍵を `C:\ProgramData\ssh\administrators_authorized_keys` へ再登録（`icacls <file> /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"`） |
| `FUJITSU.local` が引けない | 同一 LAN か確認。mDNS が通らない環境ならルーターの DHCP 予約＋ホスト名解決、または Tailscale |
| `git pull` が ff で通らない | Windows 側にローカル変更が入っている。運用ルール違反。`git -C %USERPROFILE%\FBSAT59 status` を確認し、余計な変更は捨てる |
| SDR が認識されない | インストール版 `C:\Program Files\FBSAT59\` があるか（`_internal\SoapySDR.py`）。無ければ最新 .exe をインストール。RTL-SDR は Zadig で WinUSB 化 |
| `bootstrap_natives.py` が特定部品で FAIL | ネットワーク一時障害ならランチャーは続行する。`--only <name> --force` で個別再試行。GitHub API レート制限に当たったら `GITHUB_TOKEN` 環境変数を設定 |

---

## 新しい Windows 機で再構築する場合

1. 管理者 PowerShell で OpenSSH Server:
   `winget install --id Microsoft.OpenSSH.Preview -e --accept-source-agreements --accept-package-agreements`
   → `Start-Service sshd` → `Set-Service -Name sshd -StartupType Automatic`
   → `Set-NetFirewallRule -DisplayName "OpenSSH SSH Server Preview (sshd)" -Profile Any`
2. 鍵認証: Mac の `~/.ssh/id_ed25519.pub` を Windows の
   `C:\ProgramData\ssh\administrators_authorized_keys`（管理者ユーザーの場合）へ。
   ACL を SYSTEM + Administrators のみに（`icacls <file> /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"`）。
3. Mac の `~/.ssh/config` に `Host windev` エントリ（`HostName <host>.local` / `User <user>` / `IdentityFile ~/.ssh/id_ed25519`）。
4. Windows: `winget install Python.Python.3.11` / `winget install Git.Git`。
5. `git clone https://github.com/JF9SOM/FBSAT59.git`（`%USERPROFILE%` 直下）→
   `py -3.11 -m venv .venv` →
   `.venv\Scripts\python.exe -m pip install -e .[dev,sdr,notifications,ax100digi]`。
6. `.venv\Scripts\python.exe scripts\bootstrap_natives.py` でネイティブ依存を取得。
7. `WScript.Shell` でデスクトップに `FBSAT59.lnk`（ターゲット = `scripts\win_launch.bat`、
   作業フォルダ = リポジトリルート、アイコン = `assets\icon.ico`）を作成。
8. SDR を使うならインストール版 .exe も入れておく（`_internal` の SoapySDR 一式を流用するため）。
9. sshd 自己復旧を仕込む（上の「sshd の自己復旧（ガーディアン）」）:
   `scripts/sshd_guardian.ps1` を `C:\ProgramData\fbsat59-sshd-guardian.ps1` へコピー →
   管理者 PowerShell で一度実行（障害時アクションもこれで入る）→
   `Register-ScheduledTask -TaskName 'FBSAT59 sshd guardian'`
   （`-Trigger` は `-AtStartup` と1時間ごとの `-Once/-RepetitionInterval`、
   `-Principal` は `-UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest`）。
