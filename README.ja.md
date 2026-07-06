# FBSAT59

🌐 日本語 | [English](README.md)

**GPredict の現代的後継** — アマチュア衛星追尾ソフトウェア

[![CI](https://github.com/JF9SOM/fbsat59/actions/workflows/ci.yml/badge.svg)](https://github.com/JF9SOM/fbsat59/actions)
[![Release](https://img.shields.io/github/v/release/JF9SOM/fbsat59)](https://github.com/JF9SOM/fbsat59/releases/latest)
[![License: GPL v2](https://img.shields.io/badge/License-GPL%20v2-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)

FBSAT59 は、長年アマチュア無線家に愛用されてきた
[GPredict](https://github.com/csete/gpredict)（Alexandru Csete OZ9AEC 作）の設計を引き継ぎつつ、
現代的な Python スタックで一から作り直したソフトウェアです。

---

## 主な改善点

| 機能 | GPredict | FBSAT59 |
|------|----------|-------------------|
| プラットフォーム | デスクトップのみ | デスクトップ + **同一LAN内のスマホ・タブレットからブラウザでアクセス** |
| 無線機制御 | rigctld を別途起動 | **Hamlib 内蔵**（700機種以上）— GUIで無線機を選択するだけ |
| SDR対応 | なし | **HackRF / RTL-SDR（SoapySDR経由）** — スペクトラム・復調・IQ録音 |
| ドップラー補正 | 周波数のみ | **周波数 + モード + CTCSS/DCSトーン**を自動設定 |
| デュアルリグ | 対応 | **Rig 1 + Rig 2** — SDRドングルをリグとして割り当て可能 |
| 衛星周波数DB | SATNOGSのみ・テキスト編集 | SATNOGS自動同期 + **GUIから手動追加・編集** |
| TLE更新 | 自動更新あり | **複数ソースから自動更新・品質スコアリング** |
| パス予測表示 | リスト表示のみ | **グラフィカルパスチャート + スカイレーダー + 世界地図フットプリント** |
| ローテーター制御 | 別途 rotctld | **Hamlib内蔵 — GUIでローテーターを選択するだけ。rotctld不要** |
| 対応OS | Linux・Windows・macOS（GTK+）| **Linux・Windows・macOS・Raspberry Pi** |

---

## 主な機能

### デスクトップUI（Qt6）
- **ダッシュボード** — ズームマップ + レーダー + ライブステータスバーを一画面に統合
- **世界地図** — 衛星フットプリント・地上軌跡・ドットクリックで衛星選択
- **レーダー（スカイビュー）** — 北固定、AOS/LOS時刻表示、複数衛星を色分け表示
- **パスチャート** — 仰角カーブをグラフ表示（品質ランク色分け：excellent/good/fair/low）
- **グループパスチャート** — 複数衛星のパスをまとめて表示、ホバーでツールチップ
- **アップカミングパス** — 対象衛星またはグループで検索、カレンダー選択、CSV出力
- **ラジオコントロール** — ドップラー補正、モード/CTCSS自動設定、トランスポンダーリスト；**CWトグルボタン**（USB/LSBトランスポンダー選択時にワンクリックでCW-U/CW-Lに切り替え、もう一度押すと元のモードに復帰）；**周波数プリセット**（トランスポンダー選択時にDL/UL周波数をリグに書き込み、Connect前から正しい周波数がセット済みになる）
- **SDRコントロール** — リアルタイムスペクトラムアナライザー、NFM/USB/LSB/CW復調、IQ録音、トランスポンダーロック付きパスバンドチューニング
- **Autotrack/Record** — 衛星を順次自動追尾。タイマーで開始・停止時刻を設定可能。AOS時にリグ・ローテーターを自動接続、LOS時に自動切断。SDR Audio/IQ録音をAOS〜LOS間で自動制御。**METEOR / HRPT Reception** を有効にするとAOS時にSatDumpを自動起動、LOSで自動停止
  - 衛星追加ダイアログに文字検索欄を実装 — 衛星名またはNORAD IDで絞り込み可能
- **AOS/LOSデスクトップ通知**（Linux: notify-send / macOS: osascript / Windows: PowerShell）

### METEOR / HRPT 気象衛星画像受信
Radio Control でLRPT/HRPTトランスポンダーを選択すると自動オープン（またはメニューから手動で開くことも可能）。

[SatDump](https://github.com/SatDump/SatDump) をサブプロセスとして起動し、気象衛星の画像を受信します。

| 衛星 | モード | 周波数 | 必要なSDR |
|---|---|---|---|
| METEOR-M N2-3 | LRPT | 137.9 MHz | RTL-SDR / HackRF |
| METEOR-M N2-4 | LRPT | 137.1 MHz | RTL-SDR / HackRF |
| METEOR-M N2-3 | HRPT | 1700.0 MHz | HackRF + パラボラ + LNA |
| METEOR-M N2-4 | HRPT | 1700.0 MHz | HackRF + パラボラ + LNA |
| NOAA 18 | HRPT | 1707.0 MHz | HackRF + パラボラ + LNA |
| NOAA 19 | HRPT | 1698.0 MHz | HackRF + パラボラ + LNA |
| Metop-B | HRPT | 1701.3 MHz | HackRF + パラボラ + LNA |
| Metop-C | HRPT | 1701.3 MHz | HackRF + パラボラ + LNA |

- **[SDR Connect]** — Rig Settings の SDR 設定を読み込んで自動接続
- **[📋 Log]** — SatDump の stdout/stderr を表示する浮動ログウィンドウを開く
- **Autotrack 連携** — Autotrack/Record ダイアログの「METEOR / HRPT Reception」チェックを ON にすると、AOS 時に SatDump を自動起動、LOS で自動停止
  - Autotrack リストに登録する**衛星は受信対象と一致させること**（AOS/LOS 計算の基準になる）
  - トランスポンダーの選択は SatDump の受信には影響しない（SatDump は固定周波数を使用）

### Communications（デジタル通信）
メニューバーの **Communications**（Radio と Autotrack/Record の間）からアクセス。各機能は × で閉じられる非常駐タブとして開きます。

- **APRS** — Rig + サウンドカード + Direwolf（TCP KISS）または SDR 内蔵の Bell 202 AFSK 復調器でAX.25/APRSパケットを受信・デコード。APRSメッセージ・位置ビーコンの送信にも対応（PTTはCAT制御）。受信した位置パケットはDashboardマップにシアン▲ピンで表示。コールサイン・SSID・Viaパスを保存。ADIF出力対応。
- **Telemetry** — アマチュア衛星の AX.25 テレメトリーフレームを受信・デコード。2 つの受信モードを搭載:
  - **Bell 202 AFSK** — 内蔵 1200 baud 復調器（SDR）または Direwolf（リグ + サウンドカード）。衛星コンボにはバイナリフォーマット定義済みの 12 機を表示（ISS・JO-97・RS-44・MO-122 等）。定義のない衛星は生 Hex 表示。Start 押下時 SDR を自動接続。
  - **gr-satellites** — [gr-satellites](https://github.com/daniestevez/gr-satellites) がインストール済みの場合のみ選択可。SDR の生 IQ を UDP で `gr_satellites` サブプロセスに転送。330 機以上に対応。Start 押下時 SDR を自動接続。
  - どちらのモードでも衛星コンボで選択するとメインの衛星リストが自動連動し、Radio Control がテレメトリー/ビーコン用トランスポンダー周波数に自動切り替わる。CSV 出力対応。
- **SSTV / SSDV** — アマチュア衛星（例：ISS 145.800 MHz PD120・437.550 MHz Robot36）のSSTV画像（Robot36、PD120、Martin、Scottie）とSSDVパケットを受信。SDR音声またはリグのサウンドカード入力に対応。トランスポンダー説明に「SSTV」「SSDV」「IMAGING」が含まれると自動オープン。
- **FT4** — 内蔵 ft8_lib（ctypes）でFT4の送受信が可能（WSJT-X不要）。Rig + PTTで送信。RS-44・JO-97・MO-122 等のFT4運用衛星で自動オープン。ADIF出力対応。
- **Q65** — EME（地球-月-地球）弱信号デジタルモード。libq65（WSJT-X ソースからビルド）でデコード（**Help → Q65 Library Installation** でバンドル版を自動インストール）。送信（TX）は純 Python 実装のため libq65 なしでも動作。QSOステートマシン（IDLE→CALLING→EXCHANGE→CONFIRM→LOGGED）、PTTはCAT制御・送信中ドップラー凍結。サブモード A〜E、周期 15/30/60 秒。ADIF出力対応。
- **UDP ログブロードキャスト** — FT4・Q65・APRS（双方向メッセージ交換が確定したもの）のQSOがログされるたびに、ADIFレコード1件を設定可能なホスト:ポート（デフォルト `127.0.0.1:2333`）へUDP送信するオプション機能。wavelog-gate・JT-Linkerなど軽量なログ中継ソフトと互換（JTAlert/GridTracker等が使うWSJT-Xバイナリ形式ではなく、プレーンなADIFテキスト）。**File → General Settings → Logging** で有効化・送信先を設定（LAN内の別マシンも指定可能）。APRSはAPRS自体に信号レポート交換の概念がないため、`MODE=PKT`・レポートは固定599/599でログされる（eQSL/LoTWとの互換性のため）。
- **Help → Direwolf Installation…** — 全プラットフォームでDirewolfの検出・インストール・更新が可能
- **Help → gr-satellites…** — gr-satellitesのインストール状態確認・インストール案内（apt / brew / pip）

### モバイルブラウザUI
同じLAN内のスマホ・タブレットからアプリインストール不要でアクセス可能です。

- **Trackingタブ** — 衛星リスト、リアルタイムEL/AZ/距離 + レーダー
- **Antennaタブ** — 大きな数字でAZ/ELを表示、パス進行バー、トランスポンダーカード、リモートRIG接続/切断
- **Pass Predictionタブ** — 衛星ごとのアップカミングパス一覧
- **Group Passタブ** — グループ検索とパス表示
- **コンパス連動レーダー**（Android：デバイスの向きに連動して自動回転）

### 無線機・ローテーター制御
- Hamlib 4.7.1 内蔵 — rigctld 別途起動不要
- NET Controlモード（rigctld/rotctld互換）— 既存環境と併用可能
- デュアルリグ：Rig 1 + Rig 2 独立制御（例：IC-9700 + HackRF）
- 反転トランスポンダー対応、パスバンドチューニング
- キャッチアップ追尾（タイムアウト再送信付き）

### データ管理
- **SATNOGS** トランスポンダーDB 自動同期（日次）
- **コミュニティ周波数DB** — FT4コーリング周波数など、SANTOGSにない慣習周波数
- **TLE複数ソース**: CelesTrak Amateur/CubeSat/Weather/Earth-Obs/Science/Stations、SATNOGS TLE API、手動入力
- TLE品質スコアリング：excellent（6時間未満）/ good（24時間未満）/ fair（72時間未満）/ poor
- 仮NORAD ID（90000番台）を実IDへ自動移行
- 手動入力のTLE・トランスポンダーは自動同期で絶対に上書きされない
- カスタムFavoriteグループ（グループ名・数を自由に設定）

### 自動フェッチスケジュール

FBSAT59はTLEおよびトランスポンダーデータをバックグラウンドで自動的に取得・更新します。
**通常、手動更新は不要です。**
手動更新は、打ち上げたばかりの衛星のパス直前など、直ちに最新データが必要な場合のみ行ってください。

| データ種別 | 更新間隔 |
|---|---|
| Space Stations（ISS・CSS等） | **1時間**ごと |
| Amateur Satellites（アマチュア衛星） | **2時間**ごと |
| CubeSats | **4時間**ごと |
| Weather Satellites（気象衛星） | **6時間**ごと |
| Earth Observation / Science（地球観測・科学衛星） | **12時間**ごと |
| Provisional TLEs（NORAD ≥ 90000の仮ID衛星） | **12時間**ごと |
| Active TLE fallback（NORAD 10000–89999） | **24時間**ごと |
| AMSAT運用状況 | **24時間**ごと |

SATNOGSトランスポンダーデータは初回起動時に自動取得されます。
以降は必要に応じて **Satellite → Sync SATNOGS** で手動更新してください。
アプリ内では **Help → Auto Fetch Rules** でこのスケジュールを確認できます。

### アプリ内アップデーター
- **Help → Check for Updates** — 最新リリースを自動ダウンロードしてインストール
- **Help → Hamlib Update** — アプリを再インストールせずにHamlibのみアップグレード

---

## インストール

### Windows

[Releases](https://github.com/JF9SOM/fbsat59/releases/latest) ページから
`FBSAT59-Setup.exe` をダウンロードして実行してください。

### macOS

[Releases](https://github.com/JF9SOM/fbsat59/releases/latest) ページから
`FBSAT59.dmg` をダウンロードし、開いてアプリケーションフォルダにドラッグしてください。

### Linux（AppImage）

```bash
# ダウンロード後、実行権限を付与して起動
chmod +x FBSAT59-*.AppImage
./FBSAT59-*.AppImage
```

### Linux（ソースから — Ubuntu/Debian）

```bash
# 1. システム依存パッケージ
sudo apt install python3.11 python3-pip libhamlib-dev python3-hamlib \
                 python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf

# 2. リポジトリをクローン
git clone https://github.com/JF9SOM/fbsat59.git
cd fbsat59

# 3. Python仮想環境
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# 4. udevルール設定（USB無線機アクセス権）
sudo cp scripts/99-fbsat59.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG dialout $USER
# 一度ログアウト・ログインしてください

# 5. 起動
python -m src.main
```

---

## SDR クイックスタート

1. SDRデバイス（HackRF One、RTL-SDR等）を接続
2. **Settings → Rig Settings → SDR Settings** を開く
3. **Enumerate** をクリックしてデバイスを検出
4. デバイスを選択し、サンプルレート・ゲインを設定して Rig 1 または Rig 2 に割り当て
5. **Connect** をクリック — **SDR Controlタブ** がアクティブになる
6. 衛星とトランスポンダーを選択 — モードが自動設定される

### SDR — プラットフォーム対応状況

| プラットフォーム | SDR 対応 |
|---|---|
| **Windows** | ✅ RTL-SDR・HackRF One のみ（ctypes 直接接続 — Zadig で WinUSB ドライバー要） |
| **Linux** | ✅ SoapySDR 対応デバイス全般（システムパッケージでインストール） |
| **macOS** | ✅ SoapySDR 対応デバイス全般（Homebrew でインストール） |

**Windows** — Windows では SoapySDR が WinUSB ドライバーと根本的に非互換であり、
デバイスを正常に開けません。RTL-SDR と HackRF は SoapySDR をバイパスして、
デバイス DLL（`librtlsdr.dll` / `hackrf.dll`）を ctypes で直接呼び出します。
**RTL-SDR・HackRF ともに、初回のみ Zadig で WinUSB ドライバーを当てる必要があります。**
Airspy・Airspy HF+・ADALM-Pluto は **Windows では非対応** です。

> ⚠️ **Windows Zadig セットアップ（RTL-SDR・HackRF 共通）**
> 1. デバイスを USB に接続する。
> 2. [Zadig](https://zadig.akeo.ie/)（無料）をダウンロードして起動する。
> 3. Zadig で **Options → List All Devices** を選択し、デバイスを選ぶ
>    （RTL-SDR: *Bulk-In, Interface 0* / HackRF: *Hackrf One*）。
>    ドライバーを **WinUSB** に設定 → **Install Driver** をクリック。
>    **libusbK は絶対に選ばない** — デバイス検出が失敗します。
> 4. FBSAT59 を再起動する。
>
> 詳細は **Help → SDR Device Installation** を参照してください。

**Linux** — apt でインストール：
```bash
sudo apt install python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf \
                 soapysdr-module-airspy
```

**macOS** — Homebrew でインストール：
```bash
brew install soapysdr soapyrtlsdr soapyhackrf soapyairspy
```

> LimeSDR など他の SoapySDR 対応デバイスは、Linux/macOS で対応モジュールを
> インストールすれば動作する可能性がありますが、Windows 版インストーラーには同梱されていません。

> **SDRplay（RSP1、RSP2、RSPdx 等）** — 全プラットフォームで同梱されていません。
> SoapySDRPlay3 が SDRplay 社の独自プロプライエタリ API ライブラリに依存しており、再配布できないためです。
>
> SDRplay デバイスを使用するには（全プラットフォーム共通）：
> 1. **SDRplay API** を [sdrplay.com/downloads](https://www.sdrplay.com/downloads/) からインストール
>    （Windows/macOS はインストーラー、Linux は `.run` スクリプト）
> 2. **SoapySDRPlay3** をインストール：
>    - Linux: `sudo apt install soapysdr-module-sdrplay3`
>    - macOS: ソースビルドまたは `conda install -c conda-forge soapysdr-module-sdrplay3`
>    - Windows: [github.com/pothosware/SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3) からビルドまたは conda
> 3. 本ソフトウェアを再起動 — SoapySDR 経由でデバイスが自動検出されます。

> **ADALM-Pluto（PlutoSDR）** — 全プラットフォームで同梱されていません（Windows は CI ビルドが不安定、Linux/macOS はパッケージマネージャーで簡単にインストール可能）。
>
> **PlutoSDR ネットワーク接続の仕組み：** USB で接続すると、PlutoSDR は仮想 Ethernet アダプターを作成します。
> どのプラットフォームでも特別なドライバー（Zadig / WinUSB）は不要で、IP アドレス **192.168.2.1** で通信できます。
>
> ADALM-Pluto を使用するには（全プラットフォーム共通）：
> 1. PlutoSDR を USB で接続（USB ネットワークアダプターが自動インストールされます）
> 2. **libiio** をインストール：
>    - Linux: `sudo apt install libiio-dev`
>    - macOS: `brew install libiio`
>    - Windows: [github.com/analogdevicesinc/libiio/releases](https://github.com/analogdevicesinc/libiio/releases) からインストーラーを使用
> 3. **SoapyPlutoSDR** をインストール：
>    - Linux: `sudo apt install soapysdr-module-plutosdr`
>    - macOS: `brew install soapyplutosdr` または `conda install -c conda-forge soapysdr-module-plutosdr`
>    - Windows: `conda install -c conda-forge soapysdr-module-plutosdr` または [github.com/pothosware/SoapyPlutoSDR](https://github.com/pothosware/SoapyPlutoSDR) からビルド
> 4. 本ソフトウェアを再起動 — PlutoSDR が自動検出されます。

---

## アーキテクチャ

```
fbsat59/
├── src/
│   ├── core/     # 衛星追尾エンジン（Skyfield）— 仰角・ドップラー・パス予測
│   ├── ui/       # PySide6 Qt6 デスクトップUI
│   ├── web/      # FastAPI + WebSocket（LAN内ブラウザアクセス、ポート8080）
│   ├── rig/      # Hamlib 無線機・ローテーター制御 + SdrRigAdapter
│   ├── sdr/      # SoapySDR バックエンド — デバイス・パイプライン・復調・録音
│   ├── comms/    # デジタル通信 — APRS・Direwolf・Bell 202 AFSK復調・AX.25・FT4・Q65
│   ├── data/     # TLE/SATNOGS同期・SQLite DB・手動入力
│   └── i18n/     # 多言語対応（gettextベース）
├── locale/
│   ├── en/LC_MESSAGES/   # 英語文字列
│   └── ja/LC_MESSAGES/   # 日本語文字列
└── tests/
```

起動時の動作：
1. Qt6メインウィンドウを起動
2. FastAPI/uvicorn をバックグラウンドスレッドで起動（ポート8080）
3. `DataSyncManager` がTLE・SATNOGSデータを自動取得（初回または期限切れ時）
4. ステータスバーにLAN内アクセスURL + QRコードボタンを表示

---

## 開発環境セットアップ

```bash
pip install -e ".[dev]"

# フォーマット
ruff format src/ tests/

# リント
ruff check src/ tests/

# 型チェック
mypy --strict src/

# テスト（低スペック環境では test_rig.py のみ推奨）
python -m pytest tests/test_rig.py -q

# .po ファイルを編集した後は再コンパイル
msgfmt locale/ja/LC_MESSAGES/fbsat59.po \
      -o locale/ja/LC_MESSAGES/fbsat59.mo
```

Claude Code 向け開発指示書は [CLAUDE.md](CLAUDE.md) を参照してください。

---

## 動作確認済みハードウェア

| デバイス | 種別 | Windows | Linux/macOS | 備考 |
|---------|------|---------|-------------|------|
| Yaesu FTX-1F | トランシーバー | ✓ | ✓ | Hamlib 4.7.1 モデル1051、NET Control、ドップラー補正確認済 |
| Yaesu FT-991AM | トランシーバー | ✓ | ✓ | Hamlib 4.7.1 モデル1036、NET Control、ドップラー補正確認済 |
| Icom IC-9100 | トランシーバー | — | ✓ | Hamlib 4.7.1 モデル3068、NET/Direct、SATモード、ドップラー補正確認済（v0.1.27） |
| Icom IC-9700 | トランシーバー | ✓ | ✓ | Hamlib 4.7.1 モデル3081、NET/Direct、SATモード、ドップラー補正確認済（v0.1.27） |
| Icom IC-705 | トランシーバー | — | ✓ | Hamlib 4.7.1 モデル3085、NET/Direct、汎用（非satmode）リグ。周波数・モード・スプリット・CTCSS動作確認済み。複数のHamlibバックエンド不具合を生CAT/CI-Vで回避（2026-07） |
| RTL-SDR | SDR | ✓（WinUSB/Zadig 要）* | ✓ | Windows: ctypes 直接、Linux/macOS: SoapyRTLSDR |
| HackRF One | SDR | ✓（WinUSB/Zadig 要）* | ✓ | Windows: ctypes 直接、Linux/macOS: SoapyHackRF |
| Airspy R2 / Mini | SDR | ❌ 非対応 | ✓ | SoapyAirspy（Linux/macOS のみ） |
| Airspy HF+ | SDR | ❌ 非対応 | ✓ | SoapyAirspyHF（Linux/macOS のみ） |
| ADALM-Pluto | SDR | ❌ 非対応 | ✓ | SoapyPlutoSDR（Linux/macOS のみ） |
| FTX-1F + RTL-SDR | デュアルリグ | ✓ | ✓ | パスバンドチューニング + Lock連動確認済 |

\* Windows では RTL-SDR・HackRF ともに Zadig で WinUSB ドライバーを当てる必要があります（初回一回限り）。
Windows の SoapySDR は WinUSB との根本的な非互換性があるため、RTL-SDR と HackRF は ctypes でバイパスしています。

---

## 今後の予定（フェーズ2）

### デジタルモード — アマチュア衛星（SDR）
- ~~**HRPT / LRPT**~~ — **実装済み**（METEOR-M / NOAA 18-19 / Metop-B/C、SatDump経由、Autotrack連携）
- ~~**CW解析**~~ — **実装済み**（[deepcw-engine](https://github.com/e04/deepcw-engine) ONNX モデルによる CRNN + CTC AIデコーダー。**Help → CW Model Installation…** からワンクリックインストール。SDR またはサウンドカード入力対応。CW/CW-R トランスポンダー選択時に自動オープン）
- ~~**gr-satellites 深度統合**~~ — **実装済み**（Telemetry タブで gr-satellites サブプロセスへ UDP IQ 転送・330機以上対応・衛星コンボ・SDR 自動接続・トランスポンダー自動選択）

### 業務用衛星受信（SDR）— 計画中
HackRF / RTL-SDR + 適切な LNA・フィルターで受信可能な業務用衛星信号。
いずれもオープンソースのデコーダーが存在し、SDR プラグインとして組み込む計画です。

| 衛星システム | 周波数帯 | 内容 | 主なOSSデコーダー |
|---|---|---|---|
| **Inmarsat-C（STD-C）** | 1.5 GHz L帯 | 海事安全情報（MSI）・EGC・LRIT | [JAERO](https://github.com/jontio/JAERO) |
| **Cospas-Sarsat L帯** | 1544.5 MHz | 捜索救助ビーコン位置情報（PLB/EPIRB/ELT） | gr-satellites |
| **Iridium L帯 ACARS** | 1616〜1626.5 MHz | Iridium経由の航空ACARSメッセージ | [iridium-toolkit](https://github.com/dholm/iridium-toolkit) |
| **Orbcomm** | 137〜138 MHz VHF帯 | IoT/M2Mデータメッセージ・AIS補完 | [gr-orbcomm](https://github.com/dholm/gr-orbcomm) |
| **みちびき（QZSS）データ放送** | 1278.75 MHz L6帯 | 高精度測位補強（MADOCA-PPP）・災害危機管理通報 | [qzsl6tool](https://github.com/yoronneko/qzsl6tool) |

各デコーダーはサブプロセスとして起動し、デコード結果を SDR Control タブの専用プラグインパネルに表示します。IQ録音ファイルからのオフライン再解析にも対応予定です。

### UI / UX
- **日本語UI** — 翻訳ファイルはすでに準備済み。フェーズ2で正式対応予定
- **観測ログ** — 運用した衛星パスの記録・集計・エクスポート
- **SDRデバイスインストールダイアログ** — USB VID/PIDスキャン、RTL-SDR/HackRFのドライバーインストール誘導

### ハードウェア
- TS-2000・FT-817ND 等での実機ドップラー制御テスト（IC-9100・IC-9700 は v0.1.27 で確認済み）
- WSJT-X / JS8Call との周波数・モード同期

ご意見・ご要望は [GitHub Issues](https://github.com/JF9SOM/fbsat59/issues) またはプルリクエストでお寄せください。

---

## 新しい言語を追加する

1. `locale/en/LC_MESSAGES/fbsat59.po` を
   `locale/<言語コード>/LC_MESSAGES/fbsat59.po` にコピー
2. `msgstr` の行を翻訳
3. コンパイル: `msgfmt locale/<言語コード>/LC_MESSAGES/fbsat59.po -o locale/<言語コード>/LC_MESSAGES/fbsat59.mo`
4. 新しい言語は設定ダイアログに自動で表示されます

---

## ライセンス

GPL-2.0-or-later（GPredict互換）

---

## 謝辞

- [GPredict](https://github.com/csete/gpredict) — Alexandru Csete OZ9AEC
- [Skyfield](https://rhodesmill.org/skyfield/) — Brandon Rhodes
- [Hamlib](https://hamlib.github.io/) — Hamlib Development Team
- [SATNOGS](https://satnogs.org/) — Libre Space Foundation
- [SoapySDR](https://github.com/pothosware/SoapySDR) — Pothosware
- [Direwolf](https://github.com/wb2osz/direwolf) — WB2OSZ（John Langner）— AX.25 / APRS / KISS ソフトウェアTNC
- [ft8_lib](https://github.com/kgoba/ft8_lib) — Kārlis Goba YL3JG — FT4/FT8コーデック（C ライブラリ、GPL-2.0）
- [pySSTV](https://github.com/dholm/pySSTV) — Dominik Heidler DL2DH — SSTVエンコーダー/デコーダー
- [gr-satellites](https://github.com/daniestevez/gr-satellites) — Daniel Estévez EA4GPZ — アマチュア衛星テレメトリーデコーダー
- [SatDump](https://github.com/SatDump/SatDump) — SatDump コントリビューター — 気象衛星画像デコーダー（METEOR LRPT/HRPT・NOAA HRPT・Metop HRPT）。FBSAT59 は SatDump をサブプロセスとして起動します。同梱はしていません。
- [WSJT-X](https://wsjt.sourceforge.io/) — Joe Taylor K1JT および WSJT-X 開発チーム —
  Q65 プロトコル、libq65 ソースコード（`lib/qra/q65/`）、および `src/comms/q65/encoder.py` に実装した
  GF(64) 符号化アルゴリズムは WSJT-X（GPL-2.0）から派生しています。
  FBSAT59 は WSJT-X 本体を同梱せず、libq65 のみ WSJT-X ソースツリーから別途コンパイルしています。
- [deepcw-engine](https://github.com/e04/deepcw-engine) — e04 — CRNN + CTC アーキテクチャによるAIベースのCW（モールス符号）デコードモデル。FBSAT59 は事前学習済みの `model.onnx` を GitHub からダウンロードし、onnxruntime で推論を実行します。モデルは同梱していません。
