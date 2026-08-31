# SDR 機能 詳細設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

### SDR 機能（v0.1.0 時点で実装済み）

- **SoapySDR バックエンド**（`src/sdr/`）: device・pipeline・demodulator・recorder
  - SdrDevice: SoapySDR デバイス列挙（audio/null/remote ドライバ除外）・オープン・ストリーミング
  - **Windows SDR — SoapySDR 根本的非互換（v0.1.72 確定）**: Windows では SoapySDR が WinUSB ドライバーと根本的に非互換。`SoapySDR::Device::make()` の ABI チェック層が enumerate 後にデバイスを拒否する（`hackrf_init()+hackrf_exit()` / `hackrf_open()` が WinUSB ハンドルキャッシュを破壊）。このため **Windows では RTL-SDR・HackRF のみ対応**。Airspy・Airspy HF+・ADALM-Pluto は Windows 非対応（SoapySDR が使える見込みがない）。
  - **Windows RTL-SDR ctypes直接実装**（`RtlSdrDirectDevice` in `src/sdr/device.py`）: `sys.platform=="win32"` かつ `driver=="rtlsdr"` の場合のみ `librtlsdr.dll` を ctypes で直接呼ぶバイパス実装。uint8 I/Q → complex64 変換: `(sample - 127.5) / 127.5`。**動作確認済み v0.1.71（2026-06-25）**
  - **Windows HackRF ctypes直接実装**（`HackRfDirectDevice` in `src/sdr/device.py`）: `sys.platform=="win32"` かつ `driver=="hackrf"` の場合のみ `hackrf.dll` を ctypes で直接呼ぶバイパス実装。`hackrf_start_rx()` + `ctypes.CFUNCTYPE` コールバックによる非同期ストリーミング。signed int8 I/Q → complex64 変換: `arr / 128.0`。LNA（0-40dB, step 8）+ VGA（0-62dB, step 2）ゲイン分割。**`_cb_func` を `self` に保持して GC クラッシュを防止すること**。**v0.1.72 実装済み・実機確認待ち**
  - **両デバイスとも Zadig で WinUSB ドライバーを当てる必要がある**（初回一回限り）。libusbK は絶対に選ばない（クラッシュ実績）。
  - SDRPipeline（QThread）: I/Q 取得 → FFT（10fps スペクトラム）→ 復調 → 音声出力 → IQ 録音
  - Demodulator: NFM / USB / LSB / CW 各モード。DC ブロック IIR（30Hz HPF）で HackRF DC スパイク除去
  - CW 復調: エンベロープ検出なし・直接復調方式（ブーン音問題を根本解決）
- **SdrRigAdapter**（`src/rig/controller.py`）: RigController を継承し SDR を Rig として扱う
  - `is_sdr = True` プロパティで UI 側が SDR スロットを識別
  - connect() で sample_rate / ppm / gain / bias_tee を一括適用
- **Rig Settings > SDR Settings タブ**（第3タブ）
  - デバイス列挙・選択、サンプルレート、PPM補正、RFゲイン（Auto/Manual）
  - **Bias-T ON/OFF チェックボックス**（ドライバ別キー自動選択: HackRF=`bias_tx`/`"true"`, RTL-SDR=`biastee`/`"1"`）
  - Rig 1 / Rig 2 割り当てラジオボタン（割り当てたスロットの Hamlib タブを自動グレーアウト）
  - Hamlib バージョン表示は Rig 1/2 タブのみ（SDR タブには非表示）
  - **「Serial:」行は Windows では「実際にシリアルがある場合のみ」表示**（2026-08-05）—
    Windows の RTL-SDR/HackRF は `SoapySDR::Device::make()` をバイパスして ctypes で DLL を
    直接叩くうえ、WinUSB 対応でパッチした `findRTLSDR` は実機に問い合わせず `device_index=0`
    のエントリを返すだけなので（`_win_filter_rtlsdr_by_count()` の docstring 参照）、この欄は
    空になる。「Serial: —」が固定表示されるのを故障と誤解したユーザーからの問い合わせが
    実際にあった。
    一方、**SoapyRemote 経由のリモートSDRだけは例外**で、サーバー側から転送された実際の
    シリアルが入る（この経路は ctypes バイパスを通らない）。そのため「Windowsなら一律非表示」
    ではなく、`_update_serial_row(serial)` が `QFormLayout.setRowVisible()`（Qt 6.4+、
    本プロジェクトは PySide6>=6.6 要件のため使用可）で**値が実際に入っているかどうか**を
    基準に出し分ける。ドライバー名では判定していない。Windows 以外は従来通り常に表示（空なら
    「—」）。テストは `tests/test_rig_dialog_sdr.py`（`sys.platform` を monkeypatch し、
    Linux／Windows×シリアル有無／デバイス切替時の再非表示の4パターンを検証。ウィジェットの
    `isVisible()` は表示していないパネルでは常に `False` を返すため、レイアウト側の
    `isRowVisible()` で判定していることに注意）
  - **PPM Correction の「Measure…」ボタン**（自動クロックドリフト測定、`src/sdr/ppm_measure.py`、
    2026-08-14〜15実装）: 基準信号不要で`rtl_test -p`と同じ原理（設定サンプルレートと
    実際に受信したサンプル数の比較）でppmを自動測定する。実測を通じて2回の設計変更を経た:
    - **1回目（バッファ到着の最初/最後の2点だけで比率を計算）**: 同一ハードウェアの
      連続実行で+1700ppm・+3615ppmと大きくブレた。原因はSoapySDR/RTL-SDRがハードウェア
      クロックで駆動される固定サイズチャンク（RTL-SDRは131072サンプル、約131ms周期）で
      データを届けるため、測定窓の最初と最後のチャンクだけがソフトウェア側のポーリング
      タイミングに対して最大1チャンク分（30秒測定で最大約4400ppm相当）のランダムな
      位相誤差を持ってしまうため。バックログを空読みしてから測定開始する対策も試したが
      改善しなかった（位相のランダム性自体は排出処理では解消できないため）
    - **2回目（最小二乗フィット、採用）**: 成功した全チャンク到着を`(経過時間, 累積
      サンプル数)`点として記録し（30秒で約230点）、直線を最小二乗フィットしてその傾きを
      実効レートとする。個々の点の位相ジッターはフィットの切片をずらすだけで傾きには
      ほぼ影響しないため、多数の点で平均化されバイアスが実質的に解消される（実測で
      -6.8〜+1.2ppm程度まで収束）
    - **Windowsでは「Measure…」ボタン自体を非表示**（`sys.platform != "win32"`）:
      `RtlSdrDirectDevice`（Windows専用のRTL-SDR ctypes直接実装、本ファイル該当セクション
      参照）は`rtlsdr_read_sync()`という同期ブロッキング呼び出しを使っており、SoapySDR
      Linux/macOS版のような非同期コールバックキューを持たない。実測で1回あたり約25msの
      「無駄時間」が挟まることが確認され、常に約-23000ppmという（水晶発振器としてあり
      得ない、2.3%もの誤差）非現実的な値が出た。根本修正には`HackRfDirectDevice`同様
      `rtlsdr_read_async()`への書き換えが必要だが未着手のため、信頼できない数値を見せる
      くらいならボタン自体を出さない方針とした
    - 測定時間は当初30秒→精度向上のため60秒に延長（ウォームアップ5秒込みで計約65秒）
  - **RF Gain の Auto/Manual 選択が再起動のたびにAutoへ戻るバグと修正（2026-08-15）**:
    `_SdrSettingsPanel.load()`が`self._gain_auto_rb.setChecked(gain_auto)`のみを
    呼んでおり、`gain_auto=False`（Manual選択）の場合これは事実上のno-opだった——Qtの
    ラジオボタンは同一親の下では暗黙的に排他グループを形成し、**そのグループ内で唯一
    チェック済みのボタンに対して`setChecked(False)`を呼んでも黙って無視される**
    （チェックを外したいなら別のボタン側へ`setChecked(True)`を呼ぶ必要がある、という
    Qtのよく知られた仕様）。DB保存自体（`gain_db`含む）は正しく行われていたため実害が
    分かりにくく、画面は常にAuto表示（dB欄も連動して無効化）のまま復元されていた。
    `if gain_auto: self._gain_auto_rb.setChecked(True) else:
    self._gain_manual_rb.setChecked(True)`に修正。**この「排他グループの片方だけに
    setChecked(False)を呼んでも効かない」パターンは、他のラジオボタン群のload()実装
    （Rig1/2パネルの各種モード選択等）にも同種の潜在バグがないか、今後同じ症状の報告が
    あれば疑うこと**
- **SDR Control タブ**（常時表示・SDR未接続時はパネルをグレーアウト）
  - スペクトラムアナライザ（QtCharts、10fps）— 中心周波数は赤い縦線マーカーで表示
    （`center_freq_changed` Signal）。**チャート上部にあった「RX:」周波数ラベルは
    2026-08-05 に削除済み**: Passband Tune 枠の「Freq:」欄が同じ `_on_center_freq()`
    から更新される完全に同一の値を表示しており、狭いタブで縦幅を二重に消費していたため
  - **Passband Tune パネル**: ◀◀/◀/▶/▶▶ ボタン + ステップ選択（100Hz〜10kHz）+ オフセット表示 + Reset
    - SDR が Rig 1/Rig 2 どちらでも動作
    - Lock ON 時: 相手リグの TX を自動追従（反転トランスポンダーは符号反転）
    - トランスポンダー切り替え時にオフセット自動リセット
  - デモジュレーター（モード選択・ボリューム・AGC・Start/Stop Audio）
    - **MP3音声録音**（`● REC Audio` / `■ STOP` / `📁`）— `lameenc` によるピュアPythonエンコード、外部ツール不要
  - IQ レコーダー（帯域幅選択・REC/STOP・経過時間表示）
    - **📁ファイルマネージャーボタン**（IQ・Audio 両方）— SDR未接続時も常時クリック可能。巨大IQファイルの削除に使用
  - トランスポンダー選択に連動したモード自動切替（Connect 前でも反映）
- **Help > Clear TLE Sync History…**（2026-08-10 追加）
  - `sync_log` からTLE関連の全エントリ（`celestrak-active`・`satnogs-provisional`・
    `legacy-tle-check`・`meteor-tle-check`・`celestrak-{stations,amateur,cubesat,weather,
    earth-obs,science}`）を確認ダイアログ付きで削除
  - 開発環境ではDBを直接SQLで触って`celestrak-active`の記録を消せるが、リリースビルドには
    その手段がないため追加。次回の起動・定期ジョブ・Update TLEが「一度も取得したことがない」
    扱いで全ソースを再取得する
  - TLEデータ自体は消さない。あくまで「いつ最後に取得したか」の記録のみ削除
- **Help > Hamlib Update…**（in-app Hamlib アップデーター）
  - GitHub Releases から最新 hamlib バンドルをダウンロード・展開・ユーザーディレクトリへインストール
  - Linux / Windows / macOS 対応
- **Help > Check for Updates…**（アプリ自動更新）
  - GitHub Releases API で最新バージョンを確認
  - Windows: インストーラー（.exe）をダウンロードしてサイレントインストール
  - Linux: AppImage を置き換え
  - macOS: dmg をマウントして .app をコピー
- **Windows NSIS インストーラー形式**（ZIP 配布から変更）
  - `scripts/installer.nsi`: スタートメニュー・デスクトップショートカット・Add/Remove Programs 登録
  - サイレントインストール（`/S` フラグ）対応
- **実動作確認済みリグ・デバイス**（2026-06-13）
  - FTX-1F（Hamlib 4.7.1 モデル1051、NET Control）: ドップラー補正・VFO制御・CTCSS 動作確認済み
  - FTX-1F（Hamlib 4.7.1 モデル1051、Direct モード）: モード・CTCSS（raw CAT `MD1/MD0/CN1/CT1` via `os.open()`）動作確認済み（2026-06-18）。スプリット（`FT1;`/`FT0;`）動作確認済み（2026-06-29）
  - FT-991AM（Hamlib 4.7.1 モデル1036、NET Control）: ドップラー補正・VFO制御・CTCSS 動作確認済み
  - FT-991/FT-991A/FT-991AM（Direct モード、Hamlib model 1035）: スプリット・周波数・モード・CTCSS すべて動作確認済み（2026-06-28）
    - スプリット ON/OFF: `FT3;` / `FT2;`（`ST` コマンドは FT-991A 非対応で `?;` を返す）
    - トランスポンダー選択時: pyserial で `FA`/`FB`/`FT3;` をプリセット（Connect 前からリグ表示に反映）
    - アプリ終了時: `FT2;` でシンプレックスに復帰
    - モード: `SV/MD0` via pyserial（VFO-B は SV スワップ必須）
    - CTCSS: `CN0/CT0` via pyserial（SV スワップ不要・TX-VFO にグローバル適用）
  - IC-9100（Hamlib 4.7.1 モデル3068、Direct モード）: クロスバンド・同バンド両方の周波数・モード・CTCSS 動作確認済み（v0.1.27・2026-06-25）— SAT モード ON/OFF・ドップラー補正・VFO 逆転バグ修正済み。**Windowsでは接続方法に注意**（USB-B端子＋ICOM純正ドライバー必須。詳細は「Windows Direct モード — USB接続方法の注意」セクション参照）
  - IC-9100（Hamlib 4.7.1 モデル3068、NET Control）: クロスバンド・同バンド両方の周波数・モード・CTCSS 動作確認済み（v0.1.27・2026-06-25）
  - IC-9700（Hamlib 4.7.1 モデル3081、Direct/NET モード）: Linux・Windows 両方で IC-9100 と同様に動作確認済み（v0.1.27・2026-06-25）— `_SATMODE_USE_VFO_SUB` 分岐（VFO_SUB for UL）使用
  - IC-705（Hamlib 4.7.1 モデル3085、Direct/NET モード両方、Linux・macOS）: 周波数・モード・CTCSS（トーン周波数＋エンコードON/OFF）・スプリット全て動作確認済み（2026-07-06〜07）— 汎用（非satmode）Icom CI-Vリグとして初の実地検証。Connect後にドップラー補正でMain表示が固定される不具合（生CI-V応答未読み取りによる通信デシンクが原因）を2026-07-07にLinux/macOS両方で確認・修正済み。詳細は「IC-705 (Hamlib model 3085) — 汎用（非satmode）Icom CI-Vリグの参照実装」セクション参照。Windows 未確認
  - HackRF One（SoapyHackRF）: NFM/USB/CW 復調・スペクトラム・Bias-T 動作確認済み（Linux）
  - HackRF One（ctypes直接実装 `HackRfDirectDevice`）: **Windows 実装済み v0.1.72（2026-06-25）** — 実機確認待ち。Zadig で WinUSB ドライバー適用要
  - RTL-SDR（SoapyRTLSDR）: 基本動作確認済み（Linux）
  - RTL-SDR（ctypes直接実装 `RtlSdrDirectDevice`）: **Windows 動作確認済み（v0.1.71・2026-06-25）** — Zadig で WinUSB ドライバー適用要
  - Airspy R2・Mini（SoapyAirspy）: Linux/macOS brew/apt 対応（実機未確認）**Windows 非対応**
  - Airspy HF+（SoapyAirspyHF）: Linux/macOS brew/apt 対応（実機未確認）**Windows 非対応**
  - ADALM-Pluto（SoapyPlutoSDR + libiio）: Linux/macOS のみ対応 **Windows 非対応**
  - Rig 1（FTX-1F）+ Rig 2（RTL-SDR）デュアル構成: Passband Tune + Lock 連動動作確認済み

## SDR 機能設計方針（2026-06-08 確定）

### バックエンド

**SoapySDR** を採用。RTL-SDR・HackRF・Airspy 等の多機種対応。
- Python binding は pip 非対応 → システムパッケージ経由またはバンドル版を使用
- `SoapySDR` が import できない場合は SDR 機能を自動非表示（graceful degradation）
- デバイス列挙: `SoapySDR.Device.enumerate()` / 未インストール時は `pyusb` で USB VID/PID スキャン

#### Windows バンドル構成と対応デバイス（v0.1.72 確定）

**Windows では SoapySDR は根本的に使用できない。** SoapySDR の enumerate が
`hackrf_init()+hackrf_exit()` や `libusb_init()+libusb_exit()` を複数回呼び出し、
WinUSB ハンドルキャッシュを破壊する。このため RTL-SDR・HackRF は
ctypes で DLL を直接呼ぶバイパス実装を使用する。

**Windows でサポートするデバイス（v0.1.72 時点）**:

| デバイス | 実装方式 | Zadig/WinUSB | DLL |
|---|---|---|---|
| RTL-SDR（RTL2832U 系） | `RtlSdrDirectDevice`（ctypes） | ✓ 一回限り | `librtlsdr.dll` |
| HackRF One | `HackRfDirectDevice`（ctypes） | ✓ 一回限り | `hackrf.dll` |
| Airspy / Airspy HF+ | **非対応** | — | SoapySDR 経由は不可 |
| ADALM-Pluto | **非対応** | — | SoapySDR 経由は不可 |

SoapySDR モジュール DLL（SoapyRTLSDR.dll・SoapyHackRF.dll 等）は Windows インストーラーに
同梱しているが、ctypes バイパスにより実際には使用されない。

バンドル DLL の配置: core DLL + Python binding は `_MEIPASS/`、モジュール DLL は `_MEIPASS/soapy_modules/`。
起動時に `SOAPY_SDR_PLUGIN_PATH=soapy_modules/` をセット（`src/main.py` の frozen ブロック）。

conda-forge パッケージ取得スクリプト: `scripts/extract_soapy_conda.py`（CI の Windows ビルドステップで実行）。
SoapyPlutoSDR は conda-forge に存在しないため CI で MSVC ソースビルドし `soapy-win64/modules/` に配置する（ただし Windows では実際に使用されない）。

#### Linux / macOS インストール方法

| OS | コマンド |
|---|---|
| Ubuntu | `sudo apt install python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf soapysdr-module-airspy` |
| macOS | `brew install soapysdr soapyrtlsdr soapyhackrf soapyairspy` |

#### Linux AppImage が apt/zypper インストール済みの SoapySDR を見つけられないバグ（2026-07-14 修正・GitHub Issue #11）

**症状**: Debian/openSUSE等でHelp > SDR Device Installationの案内通り
`python3-soapysdr`・`soapysdr-module-rtlsdr`等をaptでインストール済み（ターミナルから
`import SoapySDR`が正常動作することも確認済み）でも、AppImage実行時は常に
「SoapySDR not installed」と表示される。ログには
`SoapySDR import failed: ModuleNotFoundError: No module named 'SoapySDR'`。

**原因**: `src/main.py`には`/opt/hamlib/4.7`が存在する場合のみ発動する開発機専用の
sys.path操作ブロックはあったが、**一般ユーザーのAppImage実行環境向けに
システムの`dist-packages`/`site-packages`をsys.pathへ追加する仕組みが存在しなかった**。
PyInstallerでフリーズされたAppImageのデフォルトsys.pathはバンドル済みモジュール
（`_MEIPASS`）のみで構成され、ホストの`/usr/lib/python3/dist-packages`等は
自動的には含まれない。AppImageの起動スクリプト（`scripts/build-appimage.sh`が
生成するAppRun）も`LD_LIBRARY_PATH`のみ設定しており、Python側のパスには一切関与しない。
Linux/macOSでSoapySDR自体を意図的にCIバンドルしない設計（本セクション冒頭参照）は
「システムパッケージ経由で見つかる」ことを前提にしていたが、その前提を実際に成立させる
コードが欠けていた。

**修正**: `src/main.py`に、`sys.platform == "linux" and getattr(sys, "frozen", False)`
（AppImage実行時のみ・既存の`/opt/hamlib`ブロックとは完全に独立）で発動する新しいブロックを
追加。以下を`sys.path`の**末尾に**追加する（バンドル済みモジュールを優先させ、importの
失敗時のみシステム側にフォールバックさせるため`insert(0, ...)`ではなく`append(...)`）:
- `/usr/lib/python3/dist-packages`・`/usr/lib/python{3.X}/dist-packages`（Debian/Ubuntu系）
- `/usr/lib/python3/site-packages`・`/usr/lib/python{3.X}/site-packages`（openSUSE/Fedora系）

**既知の限界**: この方式はAppImageが束ねるPythonバージョン（CI固定で3.11）とユーザーの
システムPythonバージョンが一致している場合のみ有効（拡張モジュールはABI互換性が必要）。
Debian 12・Ubuntu 22.04/24.04等は標準で3.11系のため通常問題にならない想定。

#### macOS .app にも同一クラスのバグが存在していた（2026-07-31 発見・修正）

**症状**: Homebrewで`brew install soapysdr soapyrtlsdr`（Help > SDR Device Installation
経由）を実行しても、.appバンドル実行時にRTL-SDRが一切認識されない。

**原因**: 上記Linux AppImageのバグと全く同じ根本原因が、**macOS向けには一度も修正されて
いなかった**。`src/main.py`にはLinux向けの`sys.platform == "linux" and
getattr(sys, "frozen", False)`ブロックは存在したが、対応する`darwin`向けブロックが
欠けていたため、PyInstallerでフリーズされた.appは常にHomebrewの`site-packages`を
見えないままだった。

**修正**: Linux向けブロックと同じ設計・同じ場所（直後）に`sys.platform == "darwin" and
getattr(sys, "frozen", False)`ブロックを追加。`sys.path`の**末尾に**（`append`、`insert(0,
...)`ではない）以下を追加:
- `/opt/homebrew/lib/python3.*/site-packages`（Apple Silicon）
- `/usr/local/lib/python3.*/site-packages`（Intel）

Homebrew自身のPythonバージョンは時期によって変わり続けるため（3.12・3.13等）、Linux版が
`f"/usr/lib/python{_pyver}/dist-packages"`のように**自アプリのPythonバージョン**を使って
パスを組み立てていたのとは異なり、macOS版は`glob.glob("*.../python3.*/site-packages")`で
**実際に存在するディレクトリをそのまま探索**する方式にした（Homebrewのpythonバージョンは
自アプリの束ねるPythonバージョンとは無関係な別物のため、決め打ちできない）。

**既知の限界**: Linux版と同様、Homebrewのpython3バージョンとアプリが束ねるPythonバージョン
（CI固定で3.11）が異なると、コンパイル済み拡張モジュール（`.so`）のABIが一致せず
importに失敗する可能性がある。この場合は`brew install python@3.11`等でバージョンを
合わせるか、根本的にはconda-forge抽出方式（本ファイル「gr-satellitesのバンドル配布」
セクション参照）のような自前バンドルに切り替える必要がある。実機での動作確認は未実施
（ユーザーが次回タグビルド前に確認予定）。

#### macOS SoapySDR conda-forge 同梱 — 上記「既知の限界」が実機で的中・Homebrew依存を撤廃（2026-08-01 実装・実機のRTL-SDRで動作確認済み）

**発端**: 上記のsys.path修正をリリースしたユーザー実機（M2 MacBook Air、Homebrewで
`soapysdr`/`soapyrtlsdr`インストール済み）で実際にRTL-SDRが認識されるか確認したところ、
`_SoapySDR.so`自体は発見・dlopenされるようになった（sys.path修正は正しく機能）ものの、
その先で`Library not loaded: @rpath/libSoapySDR.0.8.dylib`という**別の**dlopenエラーが
発生した。`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`を試したところこのエラー自体は
解消し`SoapySDR`は「インストール済み」と認識されるようになったが、続いて
`SoapySDR.Device.enumerate()`が`TypeError: in method 'SoapySDRKwargs___getitem__', argument
1 of type 'std::map< std::string,std::string > *'`という、SWIGバインディングの型情報が
壊れているような実行時エラーで失敗した。

一方、Homebrew自身の`python3`（`"$(brew --prefix)"/bin/python3`）から同じ`import
SoapySDR; SoapySDR.Device.enumerate()`を実行すると、RTL-SDRを正しく検出できた
（`Found Rafael Micro R820T tuner`）。**同じファイルなのに、どのPythonプロセスから
読み込むかで結果が変わる**という事実が決め手になった。

**根本原因**: アプリがバンドルしているPythonは**CPython 3.11**
（`.github/workflows/ci.yml`のmacOSビルドジョブで`actions/setup-python@v5
python-version: "3.11"`固定）だが、ユーザーのHomebrewの`python3`は**3.14**。
`_SoapySDR.so`はSWIGが生成したコンパイル済みC拡張で、特定のCPython ABI（マイナー
バージョンごとに内部構造体レイアウトが異なりうる）に強く依存する。3.14向けにビルド
された拡張を3.11のインタプリタプロセスに読み込むと、importやdlopen自体は成功して
しまうことがある一方、実際にC++オブジェクト（`std::map`をラップした`SoapySDRKwargs`）
を操作する段になって型情報の不整合が表面化する——今回の`TypeError`はまさにこのクラスの
症状で、「既知の限界」パラグラフが予告していた通りの実機再現だった。

**方針転換の経緯**: 当初「ctypesで`librtlsdr.dylib`を直接叩く」案（Windows版の
`RtlSdrDirectDevice`と同型）を提案したところ、ユーザーから「それだと対応SDRが
Windows版同様RTL-SDR限定になってしまうのでは」と的確な指摘があった。実際その通りで、
Windows版がctypesバイパスに頼っているのはWinUSBハンドルキャッシュ破壊という
**SoapySDR自体が原理的に使えない**問題への対処であり、Airspy・AirspyHF・PlutoSDRは
代替手段がないため今もWindows非対応のまま。macOSの問題はPythonバージョン不一致という
**より狭い**原因のため、ctypesバイパスは過剰（＆デバイスを限定してしまう）と判断した。

**採用した方式**: Windows版が既に実践している「conda-forgeからSoapySDR本体・Python
バインディング・各デバイスモジュールを抽出し、アプリに同梱する」パターンをmacOS向けに
移植。ただし**この設計判断の根拠として「Windows版で実証済み」と最初に説明したのは不正確
だった**とユーザーから訂正を受けた——Windows版は同梱の仕組み自体はあるが、その同梱
SoapySDR経由のデバイスアクセスはWinUSB問題によりWindowsでは実際には機能しておらず
（RTL-SDR/HackRFはctypesバイパスでしか動いていない）、「バンドルすれば動く」ことを
証明した実例ではない。macOSにはWinUSBのようなハンドルキャッシュ破壊問題は存在しない
ため機能する可能性は高いと判断したが、**これは未検証の新しい試みである**点を明記した
上でユーザーの承認を得た。

conda-forgeのosx-arm64チャンネルを実際に調査した結果、`soapysdr-0.8.1-py311h2c37856_5.conda`
という**Python 3.11専用ビルド**（アプリの束ねるPythonと完全一致）が存在することを確認。
デバイスモジュール（`soapysdr-module-{rtlsdr,hackrf,airspy,airspyhf,remote}`）自体は
Pythonバインディングを持たない純C++プラグイン（`.so`、`dlopen`でロードされるだけ）の
ため、Pythonバージョンとは無関係にどれでも使える。

**実装（`scripts/extract_soapy_conda_macos.py`、新規）**: Windows版`extract_soapy_conda.py`
と同型だが、macOS conda-forgeパッケージ特有の点に対応: `lib/libSoapySDR.0.8.dylib`が
実ファイルではなく`libSoapySDR.0.8.1.dylib`への**tarシンボリックリンク**として
格納されている（`@rpath/libSoapySDR.0.8.dylib`という他バイナリからの参照名と、
ディスク上の実ファイル名が異なる）。シンボリックリンクはtar展開時にデータを
持たないため、CIアーティファクトとして再tar/アップロードする過程で消失しやすい
——このスクリプトは全シンボリックリンクをその場で実体化（リンク先の実バイトを
リンク自身の名前でも書き出す）し、展開後のディレクトリにシンボリックリンクが
一切残らないようにしている。`include/SoapySDR/*.h(pp)`ヘッダーも抽出する
（後述のPythonバインディング自前ビルドで使用）。

**dylibbundlerでのrpath修正は最終的に完全撤廃——3段階の失敗を経て判明（2026-08-01）**:

当初はHamlibのmacOSビルドで既に使われている`dylibbundler`（同ジョブで`brew install`済み）
を流用する設計だった。ところが`workflow_dispatch`で試したところ、以下の3段階すべてで
つまずいた。

1. **ハング**: `--overwrite-dir`だけでは足りず、`soapy-macos/root/`（`--dest-dir`）に
   あらかじめ`cp`で同名ファイルを配置してから実行する設計だったため、`dylibbundler`が
   「既存ファイルを上書きするか」の個別確認プロンプト（`--overwrite-files`という別フラグ
   でしか抑制できない）で停止し、GitHub Actionsの対話端末なし・EOFを返さない標準入力
   という環境で**30分以上応答なしのまま**固まった（ユーザーからの「時間がかかりすぎ」
   という指摘で発覚）。`--overwrite-files`追加・`</dev/null`によるstdinリダイレクト・
   `--search-path`と`--dest-dir`の分離・`--no-codesign`・1ファイル1ステップへの分割
   （`timeout-minutes`で強制打ち切り）と、考えられる対策を順に試したが、**最小構成
   （1ファイルのみ、ハードタイムアウト付き）でも17分以上ハングし、キルシグナルでも
   終了しなかった**。同じジョブ内でHamlib向けの`dylibbundler`呼び出しは常に高速に
   終わっており、原因はこのランナーやツール自体の一般的な不具合ではなく、conda-forge
   がビルドしたSoapySDRバイナリに特有の何かだったと推測されるが、ハング中は一切ログが
   出ないため、正確なメカニズムは最後まで特定できなかった
2. **代替実装（`otool -L` + `install_name_tool`を直接呼ぶ自前スクリプト）**:
   `dylibbundler`が実際に必要としているごく一部の機能（依存関係の列挙・不足分のコピー・
   参照の書き換え）だけを`scripts/fix_soapy_rpaths_macos.py`として自前実装し、ハングは
   完全に解消（Linux上でフェイクの`otool`/`install_name_tool`/`codesign`を使い、共有
   依存関係の重複コピー防止を含む再帰ロジックをローカル検証済み）。CI実行でも
   全ての「Fix rpaths」ステップは数秒で成功するようになった
3. **`libpython3.11.dylib`への固定リンク**: ハング解消後に判明した別の問題として、
   conda-forgeがビルドした`_SoapySDR.cpython-311-darwin.so`が**conda-forge自身の
   Python 3.11に対して固定リンク**されていた（`@rpath/libpython3.11.dylib`）。
   アプリが同梱するPyInstaller版Python 3.11とは別バイナリのため、参照を書き換える
   必要があったが、「別のlibpythonを追加で同梱する」のはプロセス内に2つの独立した
   CPythonランタイムが同時ロードされる危険な選択肢のため、「PyInstaller自身が同梱する
   libpythonを指すよう書き換える」方針にしたところ、**CI検証用にコピーしたファイルの
   コード署名が無効**というさらに別の問題に直面した（python.org公式Frameworkビルドの
   署名が、コピー後の配置場所で無効化される現象。詳細な原因は未解明のまま棚上げ）

3段階目でユーザーから「これ以上その場しのぎの対症療法を繰り返すのではなく、いったん
立ち止まって再検討せよ」との明確な指示があり、GQRX（[gqrx-sdr/gqrx](https://github.com/gqrx-sdr/gqrx)、
定評あるmacOS SDR OSS）のmacOSビルド方式を調査した。GQRXはC++/Qtアプリで
SoapySDRのC++ APIを直接使っており、Pythonバインディング自体を持たないため
この`libpython`問題自体が原理的に発生しない（`dylibbundler`も使っておらず、Qt公式の
`macdeployqt6`でリンク依存関係を解決し、SoapySDRのプラグインモジュール自体はrpath
修正なしでそのままコピーするという、より軽量な方式だった）。GQRXの知見は
「conda-forgeを素材として使う設計自体は妥当」という傍証にはなったが、Pythonバインディング
固有のこの問題への直接の答えにはならなかった。

**最終的に採用した根本解決策——SoapySDRのPythonバインディングを自前でSWIGビルド**:
SoapySDR本家のソース自体（`python/CMakeLists.txt`）を確認したところ、
`if(APPLE) list(APPEND PYTHON_LIBRARIES "-undefined dynamic_lookup") endif()`と、
まさにこの問題を回避する設計が**上流に最初から存在していた**ことが判明した
（`-undefined dynamic_lookup`は特定のlibpythonに固定リンクせず、実行時に「今動いている
インタプリタ自身」からPythonのC API シンボルを解決する手法——本プロジェクトが
Hamlibの`_Hamlib.so`で既に使っているのと全く同じパターン）。conda-forgeのビルド
レシピはこの分岐を取らず、独自にpython実行環境へ明示的にリンクしていたと見られる。

conda-forgeビルドの`libSoapySDR.dylib`（C++コア）自体はそのまま使い、Pythonとの
橋渡し層（`_SoapySDR.so`）だけを自前でビルドし直す方式にした:
- SoapySDR公式ソース（`soapy-sdr-0.8.1`タグ）から`python/SoapySDR.in.i`
  （SWIGインターフェース定義。単一ファイルで完結、`@SOAPY_SDR_ABI_VERSION@`という
  唯一のCMakeテンプレート変数を持つ。値は同梱ヘッダーの`Version.h`から動的取得）を取得
- `swig -c++ -python -threads -I<headers> -o SoapySDR_wrap.cxx SoapySDR.i`
- `clang++ -shared -fPIC -O2 -std=c++11 -undefined dynamic_lookup -I<headers>
  -I<python_include> SoapySDR_wrap.cxx -L<lib> -lSoapySDR -Wl,-rpath,@loader_path
  -o _SoapySDR<EXT_SUFFIX>`
- 生成された`_SoapySDR.so`はlibpythonへの依存を一切持たないため、CI検証ステップの
  署名問題も含め、上記3つの問題がすべて同時に解消した

この方式のSWIG生成段階（プレースホルダー置換・ヘッダー・生成コマンド）はLinux開発機
でも事前検証済み（実際のmacOSコンパイルはCI runnerでしか確認できない）。

**CI（`build-macos`ジョブ）への実装ステップ**（最終形）:
1. 「Bundle SoapySDR for macOS (conda-forge pre-built)」— パッケージ群をダウンロードし
   `extract_soapy_conda_macos.py`で`soapy-macos/{lib,python,modules,include}/`に展開
2. 「Build SoapySDR Python binding from source (SWIG)」— 上記の自前ビルドで
   `soapy-macos/python/_SoapySDR<EXT_SUFFIX>`・`SoapySDR.py`をconda-forge版から上書き
3. 「Prepare SoapySDR bundle root + strip quarantine attrs」— `soapy-macos/root/`へ
   python層をコピーし、`xattr -cr`でcurlダウンロード由来の`com.apple.quarantine`を除去
4. 「Fix rpaths — （ファイル名ごとに5ステップ）」— `fix_soapy_rpaths_macos.py`で
   `_SoapySDR.so`・各デバイスモジュール`.so`の`@rpath`参照を`@loader_path`ベースへ
   書き換え。1ファイル1ステップ構成は当初dylibbundlerハング調査用の切り分け目的
   だったが、そのまま残している（各ステップが数秒で終わるため実害なし、かつ
   将来同種の問題が再発した場合の診断性を保てるため）
5. 「Verify bundled SoapySDR imports and enumerates」— Homebrewに一切頼らず
   抽出・修正済みファイルだけで`import SoapySDR; SoapySDR.Device.enumerate()`を実行
- `scripts/fbsat59.spec`: darwin分岐に`soapy_binaries`ブロックを追加
  （`soapy-macos/root/*` → `"."`、`soapy-macos/modules/*.so` → `"soapy_modules"`）。
  Windows分岐の同名ブロックと対称的な構造
- `src/main.py`: darwin frozen時に`SOAPY_SDR_PLUGIN_PATH`を`_MEIPASS/soapy_modules`
  へ設定するブロックを新設（Windows分岐と同じパターン）。既存のHomebrew site-packages
  追記ブロック（2026-07-31実装）は同梱ビルドが欠けていた場合の最終フォールバックとして
  残した（`append`のため優先度は常に同梱版より低い）

**今回のスコープ外（意図的）**: PlutoSDR・BladeRFは`libiio`等の追加依存が重く、
検証用の実機も手元にないため見送った。Homebrew経由の案内（Help > SDR Device
Installation）は引き続きこの2機種向けに有効なまま残している。`soapysdr-module-remote`
（Remote SDR）も、dylibbundlerハングの原因切り分け中に一時的にこのバッチから除外した
まま（原因はdylibbundler自体だったと判明したため無実だった可能性が高い）。次回、
再度バンドル対象に加えて動作確認すること。

**検証状況（2026-08-01）**: `workflow_dispatch`でCI完全グリーン（SoapySDRのimport・
`Device.enumerate()`・PyInstallerビルド・DMG作成まで全て成功）を確認した上で、
**ユーザーの実機（M2 MacBook Air）でRTL-SDRが正常に認識されることを確認済み**。
Homebrewのインストール状況に一切依存せず動作する。

**教訓**: サードパーティのビルド済みバイナリに依存する際、そのビルドが「上流の
標準的な回避策」をなぜか取っていないケースがある（今回のconda-forgeのlibpython
固定リンクは、SoapySDR自身のCMakeがAppleでは`-undefined dynamic_lookup`を使う設計に
なっていたにもかかわらず、パッケージング側の事情でそれをバイパスしていた）。対症療法
（参照先の書き換え）を重ねる前に、まず**その部分だけでも自前でビルドし直せないか**を
検討する価値がある——今回は結果的にビルド済みバイナリの数分の一の分量（Pythonバインディング
のみ）を自前ビルドするだけで、複数の問題が同時に解消した。また、同種のツール
（`dylibbundler`等）が「別の文脈では問題なく動いている」からといって、今回の対象
バイナリに対しても安全とは限らない——原因不明のまま複数の対症療法を試すより、
早い段階で「この特定の依存関係自体を作り直せないか」という別のレイヤーの解決策を
検討すべきだったという反省点も残る。

---

#### PlutoSDR（ADALM-Pluto）Windows バンドル実装メモ（v0.1.5 で実装・CI 緑確認済み）

**背景**: SoapyPlutoSDR は conda-forge に存在しないためソースビルドが必要。
CI の "Build SoapyPlutoSDR for Windows" ステップで実装済み（`v0.1.5`、2回の修正で緑確認）。

##### 依存関係と実際の入手方法

| ライブラリ | 入手方法 | 備考 |
|---|---|---|
| libiio | conda-forge win-64（`libiio>=0.26`）| DLL + ヘッダー両方取得できる |
| libad9361 | 不要 | 260 kHz 以上のサンプルレートなら動作。アマチュア衛星用途には十分 |
| SoapyPlutoSDR | `pothosware/SoapyPlutoSDR` ソースビルド（MSVC + Ninja） | 出力は `PlutoSDRSupport.dll` |

##### libad9361 の役割（省略している理由）

`libad9361` は低サンプルレート時に AD9361 チップへ FIR フィルターを自動ロードするライブラリ。

| 状態 | 最低サンプルレート | 影響 |
|---|---|---|
| libad9361 **あり** | 約 65 kHz（25 MHz ÷ 384） | 低レートでも FIR で品質維持 |
| libad9361 **なし** | 約 260 kHz（25 MHz ÷ 96） | 260 kHz 以上なら通常動作 |

アマチュア衛星用途（FM/SSB/CW・IQ録音）は 260 kHz 以上で十分なため省略。

##### CI 実装（.github/workflows/ci.yml）

"Bundle SoapySDR for Windows" ステップの直後に配置。

**重要な落とし穴（v0.1.5 デバッグで判明）**:

1. **conda cmake が PATH に入り VS 検出に失敗する問題**
   - conda で cmake をインストールすると conda のパスが優先され、VS を見つけられない cmake が使われる
   - **対策**: conda には cmake を含めない。システム cmake をフルパス `C:\Program Files\CMake\bin\cmake.exe` で指定。VS ジェネレーター (`"Visual Studio 17 2022"`) を使わず、`vcvarsall.bat` で MSVC を PATH に追加してから Ninja ジェネレーターを使う

2. **DLL ファイル名が想定と異なる問題**
   - SoapyPlutoSDR のビルド出力は `SoapyPlutoSDR.dll` ではなく **`PlutoSDRSupport.dll`**（CMake target 名）
   - SoapySDR は `SOAPY_SDR_PLUGIN_PATH` ディレクトリの全 DLL をロードするためファイル名は問わない
   - **対策**: `Get-ChildItem` のフィルターを `PlutoSDRSupport.dll` に設定

**実際に動作するビルド手順（ci.yml のステップ）**:
```powershell
# 1. conda で libiio + soapysdr ヘッダー取得（cmake は含めない！）
conda create --prefix pluto-deps -c conda-forge "soapysdr=0.8.1" "libiio>=0.26"

# 2. vcvarsall で MSVC を PATH に設定（VS ジェネレーターを避けて Ninja を使うため）
$vcvarsall = (vswhere でパス取得)
cmd /c "`"$vcvarsall`" x64 && set" → 環境変数をプロセスに反映

# 3. システム cmake + Ninja でビルド
"C:\Program Files\CMake\bin\cmake.exe" -G Ninja -DCMAKE_PREFIX_PATH=pluto-deps\Library
cmake --build SoapyPlutoSDR-build

# 4. コピー（出力名に注意）
PlutoSDRSupport.dll → soapy-win64/modules/   # ← SoapyPlutoSDR.dll ではない
libiio.dll         → soapy-win64/bin/
```

##### ユーザー側の追加作業

- USB 接続時: WinUSB ドライバーを Zadig で適用（RTL-SDR と同様・一回限り）
- ネットワーク接続時（192.168.2.1）: 追加ドライバー不要

##### BladeRF について

libbladerf は conda-forge win-64 に存在するが SoapyBladeRF はなし。
PlutoSDR と同じアプローチ（MSVC ソースビルド）で追加可能。

**既存環境への対応**: `SoapySDR.Device.enumerate()` が成功すれば即 Ready。追加作業なし。
**排他制御**: SoapySDR デバイスは 1 プロセス占有。Ground-Station 等と同時使用不可。

#### Bias-T ドライバ別キー対応（実装済み・2026-06-09 確定）

`SoapySDR.Device.writeSetting()` は未知のキーを**例外なしに無視する**ため、
try-except で複数キーを試す方式は機能しない。ドライバ名で分岐が必須。

```python
driver = (self._info.driver or "").lower()
if "hackrf" in driver:
    key = "bias_tx"
    value = "true" if enabled else "false"   # HackRF: 文字列 "true"/"false"
elif "rtlsdr" in driver or "rtl" in driver:
    key = "biastee"
    value = "1" if enabled else "0"           # RTL-SDR: 文字列 "1"/"0"
else:
    key = "biastee"
    value = "true" if enabled else "false"    # その他: 汎用フォールバック
```

#### CW 復調方式（エンベロープ検出なし・2026-06-09 確定）

エンベロープ検出（`np.abs()` + LPF）はバンドパスフィルタで帯域制限したノイズにも
必ず正値を返すため、信号がなくてもブーン音が発生する（AGC が増幅）。
CW 復調は**エンベロープ検出を一切行わない**方式を採用：

```
I/Q → DC除去 → 2段デシメーション（~8kHz）→ 実部取り出し → SOS BPF(300-3000Hz) → 出力
```

- ナチュラルオフセット（搬送波が中心周波数±数百〜数千Hz）がそのまま音声になる
- BPF は `scipy.signal.butter(4, [300/nyq, 3000/nyq], btype='band', output='sos')` + `sosfilt()`
  （狭帯域 b,a 形式は数値的に不安定なため SOS 形式必須）
- サイドトーン注入不要

### ディレクトリ構成

```
src/sdr/
├── __init__.py
├── device.py          # SoapySDRDevice — デバイス列挙・接続・サンプル取得
├── pipeline.py        # SDRPipeline (QThread) — pub/sub I/Q配信ハブ
├── demodulator.py     # NFM / USB / LSB / CW 復調（numpy + scipy）
├── recorder.py        # IQRecorder — CF32 WAV 書き出し
├── sdr_state.py       # SdrWebState — Web UI 向け状態共有
└── plugins/
    ├── base.py            # SdrPlugin 抽象基底クラス
    ├── fm_demod.py        # NFM 復調（初期実装）
    ├── ssb_cw_demod.py    # SSB/CW 復調（初期実装）
    ├── iq_recorder.py     # IQ 録音（初期実装）
    ├── direwolf.py        # 将来: APRS / Direwolf 連携
    ├── wsjtx.py           # 将来: FT4 / WSJT-X 連携
    └── sstv.py            # 将来: SSTV 受信
```

**注意**: SatDump 連携は SDR プラグインではなく独立モジュールとして実装済み:
```
src/
├── comms/
│   └── meteor/
│       ├── __init__.py
│       └── satdump.py   # METEOR_PIPELINES・METEOR_NORAD_IDS・SatDumpProcess(QThread)
├── ui/
│   └── meteor_tab.py    # MeteorTab — UI・SatDump 制御・Autotrack 連携
```

### I/Q パイプライン設計（pipeline.py）

`SDRPipeline` は pub/sub バスとして設計する。各プラグインがサンプルを購読し、
パイプライン本体に触れずにプラグイン追加が可能。

```
SoapySDR（QThread内）
    │  I/Q samples (numpy CF32, 2.4MHz)
    ▼
SDRPipeline
    ├── FFT → スペクトラムデータ → Signal → SDR Control UI（10fps）
    ├── → FM/SSB/CW Demodulator → sounddevice 音声出力
    ├── → IQRecorder → CF32 WAV ファイル
    ├── → [将来] SatDump stdin pipe
    └── → [将来] Direwolf / SSTV AudioSource
```

### AudioSource 抽象層

狭帯域データモード（APRS・SSTV 等）の音声入力元を抽象化する。
デコーダープラグインは `AudioSource` インターフェースのみを見るため、
SDR ソフトウェア復調とリグサウンドカード入力を透過的に切り替えられる。

```python
class AudioSource:  # 抽象基底
    pass

class SdrAudioSource(AudioSource):
    # SoapySDR → ソフトウェア復調 → PCM バッファ

class SoundcardAudioSource(AudioSource):
    # sounddevice.InputStream → PCM バッファ
    # リグの AF 出力が入っているデバイスを sounddevice.query_devices() で列挙して指定
```

- SDR 未接続時は SdrAudioSource オプションをグレーアウト
- リグ未接続時は SoundcardAudioSource オプションをグレーアウト

### SdrPlugin 抽象基底クラス（plugins/base.py）

```python
class SdrPlugin:
    name: str                      # "APRS / Direwolf"
    supported_modes: list[str]     # ["FM", "AFSK"]
    requires_tx_audio: bool        # APRS TX は True
    requires_external: str | None  # "direwolf", "satdump" 等

    def start(self, center_freq_hz: float) -> None: ...
    def stop(self) -> None: ...
    def get_widget(self) -> QWidget: ...  # SDR Control タブ内に埋め込む UI
    def is_available(self) -> bool: ...  # 外部ツール検出
```

### SdrRigAdapter（src/rig/controller.py への追加）

既存の `RigController` 抽象基底クラスを継承し、SDR を Rig 1 / Rig 2 として扱えるようにする。

```
RigController（抽象）
    ├── HamlibDirectController   ← 既存
    ├── HamlibNetController      ← 既存
    └── SdrRigAdapter            ← 新設
          SoapySDRDevice を内包
          is_sdr = True プロパティ
          connect() → SDRPipeline 起動
          set_frequency() → SoapySDR 中心周波数を設定（ドップラー補正連動）
```

### UI 設計

#### Rig Settings ダイアログ — SDR Settings タブ（第3タブ）

- デバイス列挙ボタン（`[Enumerate]`）
- デバイス選択ドロップダウン（SoapySDR.Device.enumerate() の結果）
- Sample Rate / Bandwidth / PPM Offset / RF Gain 設定
- **Assign as: ○ Rig 1  ● Rig 2** ラジオボタン
- IQ 録音保存先ディレクトリ設定

#### Radio Control — SDR 接続時の表示

```
通常の Rig:  [RIG: Connected  ■ 435.612 MHz]
SDR の Rig:  [SDR: Connected  ■ 435.612 MHz]  ← シアン色で区別
```

#### SDR Control タブ（Radio Control タブの隣）

- **SDR 未接続時は `setEnabled(False)`**（グレーアウト）
- 接続後にアクティブ化
- 内部はプラグインホスト構造（将来プラグインがタブとして追加される）

**初期実装のパネル構成:**
```
┌─ Spectrum ──────────────────────────────────────┐
│  QtCharts QLineSeries で約 10fps のリアルタイム FFT │
│  横軸: 周波数, 縦軸: dBFS, 中心周波数マーカー（赤） │
└──────────────────────────────────────────────────┘
┌─ Demodulator ───────────────────────────────────┐
│  Mode: [NFM ▼] [USB] [LSB] [CW]                  │
│  Filter BW: スライダー  Volume: スライダー  AGC    │
│  [▶ Start Audio]  [■ Stop Audio]                 │
└──────────────────────────────────────────────────┘
┌─ IQ Recorder ───────────────────────────────────┐
│  BW: [250 kHz ▼]   ファイル名自動生成             │
│  [● REC]  [■ STOP]   経過時間 / ファイルサイズ    │
└──────────────────────────────────────────────────┘
```

### トランスポンダー選択 → デモジュレーターモード自動切替

Radio Control でトランスポンダーを選択すると SDR Control のモードを自動設定する。

| SATNOGS mode 値 | SDR Control で自動選択 |
|---|---|
| `FM` / `DIGITALVOICE` | NFM |
| `SSB` / `USB` | USB |
| `LSB` | LSB |
| `CW` / `CW-R` | CW |
| `BPSK` / `AFSK` | USB（IQ 録音推奨） |

### IQ 録音ファイル仕様

- フォーマット: WAV CF32（32bit float ステレオ I/Q）
- サンプリングレート: 選択した帯域幅（例: 250 kHz）
- ファイル名: `{NORAD}_{衛星名}_{AOS時刻UTC}.iq.wav`
- SDR#・GQRX・SDR++ 等で直接再生・復調可能

### 将来拡張プラグイン（フェーズ2以降）

#### アマチュア衛星・デジタルモード

| プラグイン | バックエンド | 受信入力 | 送信 |
|---|---|---|---|
| ~~HRPT/LRPT 画像~~ | ~~SatDump（サブプロセス stdin pipe）~~ | ~~SDR のみ~~ | ~~なし~~ | → **実装済み** `src/comms/meteor/` |
| 衛星テレメトリーデコード | gr-satellites（GNU Radio OOT モジュール、サブプロセス） | SDR のみ | なし |
| APRS | Direwolf（TCP KISS） | SDR or Rigサウンドカード | Rig サウンドカード + PTT |
| ~~FT4~~ | ~~WSJT-X（UDP 連携）~~ | ~~SDR or Rigサウンドカード~~ | ~~Rig サウンドカード + PTT~~ |
| → **FT4（実装済み）** | **ft8_lib ctypes（内蔵）** | Rig必須・SDR RX 補助可 | Rig サウンドカード + PTT |
| ~~CW デコード~~ | ~~AI-CW デコーダー（内蔵、ML推論）~~ | ~~SDR or Rigサウンドカード~~ | ~~なし~~ | → **実装済み** `src/comms/cw/`（deepcw-engine ONNX）|
| SSTV 受信 | pySSTV（内蔵） | SDR or Rigサウンドカード | なし |

外部ツール（SatDump・Direwolf・gr-satellites）はサブプロセス起動。内部実装しない。
FT4 は ft8_lib ctypes で内蔵実装済み（WSJT-X UDP 方式から変更）。

#### 業務用衛星受信（フェーズ2以降・計画中）

HackRF / RTL-SDR + 適切な LNA・フィルターで受信可能な業務用衛星信号のデコードを追加予定。
いずれもオープンソースのデコーダーが存在し、SDR プラグインとして組み込める。

| 衛星システム | 周波数帯 | 内容 | 主なOSSデコーダー候補 |
|---|---|---|---|
| **Inmarsat-C（STD-C）** | 1.5 GHz L帯 | 海事安全情報（MSI）・EGC（Enhanced Group Call）・LRIT | [aero](https://github.com/jontio/JAERO)・[inmarsat-c](https://github.com/Outernet-Project/aero) |
| **Cospas-Sarsat L帯下り** | 1544.5 MHz | 捜索救助ビーコン位置情報（PLB/EPIRB/ELT） | [LRPT decoder](https://github.com/opensatelliteproject)・gr-satellites |
| **Iridium L帯 ACARS** | 1616〜1626.5 MHz | 航空 ACARS メッセージ・衛星電話傍受（表示のみ） | [iridium-toolkit](https://github.com/dholm/iridium-toolkit) |
| **Orbcomm** | 137〜138 MHz VHF | IoT/M2M データメッセージ・AIS 補完 | [gr-orbcomm](https://github.com/dholm/gr-orbcomm)・[orbcomm-decoder](https://github.com/microp11/orbcomm) |
| **みちびき（QZSS）データ放送** | 1278.75 MHz L6帯 | 高精度測位補強（MADOCA-PPP）・災害危機管理通報 | [qzsl6tool](https://github.com/yoronneko/qzsl6tool) |

**実装方針:**
- 各デコーダーはサブプロセスとして起動し、stdout/パイプ経由でデコード結果を受け取る
- 専用 UI パネルを SDR Control タブ内のプラグインタブとして追加
- IQ 録音ファイルからのオフライン再解析にも対応予定
- ライセンスに注意: 各国の電波法規制を遵守すること（受信のみ・復号結果の二次利用不可の場合あり）

#### gr-satellites について
- GNU Radio の OOT（Out-Of-Tree）モジュール。100 機種以上のアマチュア衛星テレメトリーフォーマットに対応
- `gr_satellites` コマンド（CLI）を IQ ストリームに繋いでサブプロセス起動する方式が最も移植性が高い
- **`pip install gr-satellites` は存在しない**（2026-07-31 判明・下記「gr-satellitesのバンドル配布」参照）。正しいインストール方法は conda-forge（`conda install -c conda-forge gnuradio-satellites`）・Ubuntu PPA・ソースビルドのいずれか
- インストール状態確認・誘導は `Help > gr-satellites Installation…`（`src/ui/gr_satellites_dialog.py`）で実装済み（バンドルのダウンロード・手動インストール手順の両方を提示）

#### gr-satellitesのバンドル配布（2026-07-31 実装・macOSのみ・実機未検証）

**発端**: Help画面の「gr-satellites Installation」ダイアログが案内していた `pip install gr-satellites`
を実際にmacOSユーザーが試したところ `ERROR: Could not find a version that satisfies the requirement
gr-satellites (from versions: none)` で失敗。`curl -sI https://pypi.org/simple/gr-satellites/` が
PyPIサーバー自身から404を返すことを確認し、**gr-satellitesはPyPIに一切公開されていない**ことが
判明した（`pip config`・ネットワーク・radioconda環境はすべて正常だった）。公式ドキュメント
（[gr-satellites readthedocs](https://gr-satellites.readthedocs.io/en/latest/installation.html)・
[conda版](https://gr-satellites.readthedocs.io/en/latest/installation_conda.html)）を確認したところ、
正しいインストール方法は次の3通り: (1) conda-forge `conda install -c conda-forge gnuradio-satellites`
（公式が「Windowsでの推奨方法」と明記）、(2) Ubuntu PPA `ppa:daniestevez/gr-satellites`、
(3) cmakeによるソースビルド。このバグ自体は本ファイルの過去の記述（旧「gr-satellitesについて」節）
にも存在しており、実装当初から一度も正しく動作したことがなかったと考えられる。

**方針転換の経緯**: 当初は誤った案内文をconda-forge/PPA/ソースビルドの正しい内容に修正するだけの
予定だったが、ユーザーから「毎回ユーザーにビルドさせるのではなく、他のバンドル済みソフト
（ft8lib/libq65/libft4wsjt）と同様にこちらでCIビルド・バンドルできないか」との要望があった。

**ft8lib等との規模の違い**: ft8lib/libq65/libft4wsjtは依存の少ない小さなCライブラリ（FFTW・
Boost程度）で、単一の.so/.dylib/.dllをCIでビルドするだけで済む。一方gr-satellitesはGNU Radio
本体（Homebrewでフルインストールすると59個の依存関係・数百MB規模）に依存するPythonモジュールで、
実行には巨大なC++共有ライブラリ群＋専用Python環境が必要——単純に同じ手法は使えない。

**採用した方式（conda-forge抽出）**: 調査の結果、conda-forgeのGNU Radioは細かくパッケージ分割
されており、gr-satellitesが実際に必要とするのはQt/PyQt/GTK等のGUI要素を一切含まない最小構成
だと判明した:
- `gnuradio-core`（約4.45MB、win-64）: fftw・gsl・boost・portaudio・volk等に依存。Qt/PyQt不要
- `gnuradio-satellites`（約0.76MB）: `gnuradio-core`・`gnuradio-pmt`・`gnuradio-zeromq`・
  construct・matplotlib-base・numpy・requests等に依存。**`gnuradio-qtgui`への依存なし**
  （[conda-forge/gnuradio-satellites-feedstock](https://github.com/conda-forge/gnuradio-satellites-feedstock)
  のメタデータで確認済み）

`gr_satellites`はFBSAT59自身のPythonプロセスに読み込むのではなく**独立したサブプロセスとして
起動**する既存アーキテクチャ（`GrSatellitesBackend`）のため、バンドルする環境のPythonバージョンは
FBSAT59本体のPyInstaller用Pythonと一致させる必要がない。この前提のもと、CIで
`conda create --prefix ... -c conda-forge gnuradio-core gnuradio-satellites python=3.11`により
独立した環境を作成し、業界標準ツール`conda-pack`（環境を可搬なアーカイブ化し、展開後に
`conda-unpack`で絶対パスを再配置する）でパッケージ化してGitHub Releasesで配布する方式を採用した。

**実装済みコンポーネント**:
- `.github/workflows/build-gnuradio-satellites.yml`（新規） — macOS（`macos-14`、Apple Silicon）
  向けのみ実装。`workflow_dispatch`のみでトリガー（スケジュール実行は成功確認後に追加予定）。
  conda-forge環境作成 → `conda-pack`でアーカイブ化 → 別ディレクトリへ展開して`conda-unpack`＋
  `gr_satellites --help`＋`python -c "import gnuradio.satellites"`のスモークテスト →
  `gr-satellites-bundle`プレリリースタグへ`gr-satellites-macos-arm64.tar.gz`としてアップロード
  （既存のft8lib-bundle等と同じ命名パターン）
- `src/comms/telemetry/gr_satellites_install.py`（新規） — バンドル環境のパス解決を集約。
  `find_gr_satellites_executable() -> tuple[Path, bool] | None`（実行ファイルパス, バンドル済みか）・
  `bundled_satyaml_dir()`（衛星定義YAMLディレクトリ、`lib/python*/site-packages/satellites/satyaml/`
  をglobで探索）・`bundled_version()`（`conda-meta/gnuradio-satellites-*.json`のファイル名から
  バージョン文字列を抽出、サブプロセス起動不要）・`uninstall_bundle()`
- `src/comms/telemetry/gr_satellites_backend.py`（修正） — `detect_gr_satellites()`・
  `list_gr_satellites_*()`・`get_satellite_info()`が`gr_satellites_install`経由でバンドルを
  優先検出するよう変更。**PYTHONPATHのNumPy 1.x互換ハック（`_GR_PYTHONPATH`）は
  システムインストール（apt等）検出時のみ適用**——バンドル環境は自己完結しているため不要
  （`start()`内で`is_bundled`フラグにより分岐）
- `src/ui/gr_satellites_dialog.py`（大幅改修） — 「Install Bundled Environment (Recommended)」
  枠を新設し、ft8lib_dialog.pyと同じ`_InstallWorker`（QThread）パターンでダウンロード・展開・
  `conda-unpack`実行・Uninstallボタンを実装。手動インストール手順（Manual Installation）は
  正しい内容（conda-forge/PPA/ソースビルド）に修正した上でフォールバックとして残した

**サイズ見積もり（机上調査のみ、実測未確認）**: 約150〜400MB程度と推定（GitHub Releasesの
1ファイル2GiB制限は問題にならない）。

**実機（GitHub Actions macOS runner）での`workflow_dispatch`実行で発覚・解決したバグ
（2026-07-31）**:

1回目の実行はconda-forge環境作成（130パッケージ）・`conda-pack`（140MB、警告なし完走）・
`conda-unpack`（exit code 0）まで問題なく進んだが、`gr_satellites --help`が
`ModuleNotFoundError: No module named 'gnuradio'`で失敗した。`set +e`に変更し全診断を
1回のログにまとめて出す2回目の実行で、以下が判明した:

- 再配置後のPython（明示的に`bin/python`を指定して呼び出した場合）は`sys.path`が正しく
  `site-packages`を含み、`import numpy`（対照実験）・`import gnuradio`はどちらも成功
- 一方`import gnuradio.satellites`は失敗——ただしこれは**バンドルの不具合ではなく調査側の
  誤り**だった。conda-forgeの`gnuradio-satellites`パッケージを直接ダウンロードして中身を
  検証したところ、gr-satellitesは`gnuradio.satellites`ではなく**トップレベルの`satellites`
  パッケージ**として`site-packages/satellites/`にインストールされる（870ファイル確認、
  `gr_satellites`スクリプト自身も`import satellites.core`等と書かれている）。既存コード
  （`_SATYAML_DIR = Path(...) / "satellites" / "satyaml"`）は元々正しく実装されていた
- **実際のバグ**: `gr_satellites`エントリポイントスクリプトのshebangは`#!/usr/bin/env python`
  で、絶対パスを含まない。つまり`conda-unpack`（絶対パスの書き換えツール）には書き換える
  対象がそもそも無く、これは想定通りの「何もしない」だった。問題は、このスクリプトを
  `/tmp/gr-sat-smoketest/bin/gr_satellites`のように**直接実行**すると、shebangの
  `env python`解決がその時点の**呼び出し元シェルのPATH**に依存し、バンドル環境の
  Pythonとは無関係になってしまうこと（CIでは無関係なpythonが解決され、実際のユーザー
  環境でも同様——PATH上に何があるか次第で結果が変わる、再現性のないバグになる）

**修正**: `gr_satellites_install.py`に`resolve_gr_satellites_command()`を新設。バンドル版は
`[bundled_python_path, bundled_script_path]`という2要素のargvを返し、スクリプト自身の
shebangに一切頼らず明示的にバンドル済みpythonへ渡すようにした（`find_gr_satellites_executable()`
は表示専用として残し、実際の起動には使わない）。`gr_satellites_backend.py`の`start()`も
これに合わせて更新。CIワークフローのスモークテストも同じ「shebang経由（参考情報のみ、
失敗して当然）」と「明示的python経由（本番と同じ呼び出し方、これが実際のテスト対象）」の
両方を記録するよう修正し、`import gnuradio.satellites`の誤りも`import satellites`に訂正済み。

**教訓**: 「ログ上、明示的に呼び出したpythonでは動くのに、スクリプト単体を直接実行すると
動かない」という食い違いが出たら、まずスクリプトのshebang行そのもの（絶対パスかPATH依存か）
を疑うこと。`conda-unpack`は「絶対パスとして記録されているものを書き換える」ツールであり、
そもそも絶対パスが書かれていない箇所（`#!/usr/bin/env python`のような可搬性重視の慣習的
shebang）には無力——「ツールが仕事をしなかった」のではなく「そのツールの担当範囲外だった」
と気づくまでに複数回の実機検証が必要だった。

**3回目の`workflow_dispatch`実行で判明: `--help`のexit 1は仕様通り、パッケージング・修正は
成功（2026-07-31）**: 上記の修正（`resolve_gr_satellites_command()`）後の再実行で、
`import gnuradio`・`import satellites`は明示的python経由で共に成功し、
`gr_satellites --help`（explicit python invocation）もusageメッセージを正しく出力した。
ただしexit codeが1だったため、スモークテストは依然「失敗」と判定していた。実際の
`gr_satellites`スクリプト本体（conda-forgeパッケージから直接ダウンロードして中身を確認）を
読んだところ、これは仕様通りの挙動と判明:

```python
def main():
    parser = argument_parser()
    if len(sys.argv) >= 2 and sys.argv[1] in ['--version', '--list_satellites']:
        options = parser.parse_args()
        sys.exit(0)
    if len(sys.argv) <= 1 or sys.argv[1][0] == '-':
        parser.print_usage(file=sys.stderr)
        sys.exit(1)   # --help 単体はここに該当。意図的な exit 1
```

`gr_satellites`は「第1引数に衛星名/NORAD ID/YAMLパスを必須で取る」設計
（`gr_satellites <satellite> [options]`）で、`--version`・`--list_satellites`だけが
衛星名なしで動作する例外。`--help`単体はこのCLI自身の仕様上「不正な使い方」であり、
exit 1は正しい。**アプリ本体（`gr_satellites_backend.py`）は元々
`[python, script, str(norad), "--udp", ...]`という「NORAD IDを第1引数に渡す」正しい
呼び出し方をしていたため、一連の調査を通じて実は一度も影響を受けていなかった**——
問題があったのはCIのスモークテストが`--help`という誤った検証コマンドを使っていた点のみ。
スモークテストは`--version`（衛星名なしでも正常終了する）に差し替え済み。

**結論**: バンドル化アプローチ（conda-forge抽出 → `conda-pack` → `conda-unpack`）自体は
GNU Radioのような大規模パッケージでも機能することが実機で確認できた。残る未検証点は
「`--version`に差し替えた後のフルパス実行が緑になるか」（次回`workflow_dispatch`で確認予定）
のみ。

**macOS: `workflow_dispatch`でグリーン確認済み（2026-07-31）**。`gr-satellites-bundle`
プレリリースタグに`gr-satellites-macos-arm64.tar.gz`が公開済み。

**Linux・Windowsジョブを追加（2026-07-31）**: macOS成功を受け、同一ワークフロー内に
`build-linux`（`ubuntu-22.04`）・`build-windows`（`windows-latest`）ジョブを追加した。

- **Linux**: macOSジョブとほぼ同一構成（`conda-pack`はmacOS特有のcodesigning等の癖を持たない
  ため、緑になる可能性が高いと見ている）。診断ステップもmacOSと同じ内容
- **Windows**: Windows conda環境はmacOS/Linuxと**根本的にレイアウトが異なる**
  （`bin/<name>`のshebangスクリプトではなく`Scripts/<name>.exe`という自己完結型の
  ランチャーstub、`python.exe`は`bin/`ではなく環境ルート直下）ため、macOS/Linuxで機能した
  「`bundled_python bundled_script`」形式がそのまま通用するとは限らない。この点は
  **本プロジェクトで一度も検証したことがなく未知数**なため、Windowsのスモークテストは
  あえて「探索的な診断優先」（`set +e`でPython importチェックのみをSTATUSに反映し、
  `Scripts/gr_satellites.exe`ランチャーの起動確認は参考情報として記録するのみで
  合否判定に含めない）にしてある。実行結果を見てから
  `gr_satellites_install.py`の`resolve_gr_satellites_command()`のWindows分岐を
  調整する前提
  - CI・アプリ側（`gr_satellites_dialog.py`）双方で、当初`python.exe conda-unpack.exe`
    （`.exe`ランチャーの中身をPythonソースとして渡そうとする、明らかに不正な呼び出し）と
    誤って実装していたバグを実装中に発見・修正済み（`Scripts/conda-unpack.exe`は
    自己完結型ランチャーなので直接実行が正しい）
  - 配布形式はWindowsも含め全プラットフォームで`.tar.gz`に統一（`zipfile`同様
    `tarfile`もPython標準ライブラリでクロスプラットフォームに動作するため、
    外部ツール依存を増やす`.zip`を使う理由がない）

**1回目の`workflow_dispatch`実行結果（2026-07-31）**: Linux・macOSは両方とも
Uploadステップまで到達しグリーン（`gr-satellites-bundle`に
`gr-satellites-linux-x86_64.tar.gz`・`gr-satellites-macos-arm64.tar.gz`が公開済み）。
**Windowsのみ失敗**——原因は`tar (child): Cannot connect to D: resolve failed`。
`$RUNNER_TEMP`はネイティブWindowsパス（`D:\a\_temp`）で、これに素朴に`/file`を連結すると
`D:\a\_temp/file`という**バックスラッシュとスラッシュが混在したパス**になり、Git Bash付属の
`tar`（bsdtar）が先頭の`D:`を「ドライブレター」ではなく`ホスト名:ファイル`という
リモートtar構文のホスト名と誤解釈していた。`conda-pack`自体（Pythonツール、Windows APIは
`/`と`\`を区別なく受け付ける）や`ls`はこの混在パスでも問題なく動いていたため、
`tar -xzf`だけがこの罠にはまっていた。

**修正**: `cygpath -u "$RUNNER_TEMP"`でPOSIX形式のパス（例: `/d/a/_temp`）に一度変換し、
以降すべてのステップでその変換済みパスを使うよう統一（`Pack`ステップで変換して
`$GITHUB_ENV`経由で`Smoke-test`ステップに引き継ぐ設計）。

**2回目の`workflow_dispatch`実行で3プラットフォームとも完全グリーン（2026-07-31）**:
`cygpath`修正後の再実行で、macOS・Linux・Windowsすべて`Upload`ステップまで到達。ただし
Windowsのスモークテストは`Scripts/gr_satellites.exe`を「参考情報のみ」（合否判定に含めない）
にしていたため、この時点では**実際に`gr_satellites`を起動できることまでは確認できていなかった**。
ログを精査したところ`Scripts/`には`conda-unpack`関連ファイルしかなく、`gr_satellites`が
見当たらないことが判明した。

**根本原因**: conda-forgeのWindowsパッケージ規約では、`Scripts/`は純粋なPythonの
console-scriptエントリポイント専用で、GNU RadioのOOTモジュール（gnuradio-satellitesのような
C/C++寄りのパッケージ）の実行ファイルは**`Library/bin/`**に配置される。実際にwin-64版
`gnuradio-satellites` .condaパッケージを直接ダウンロードして中身を確認したところ、
`Library/bin/gr_satellites.py`（素のPythonスクリプト、macOS/Linuxの`bin/gr_satellites`と
同内容）と`Library/bin/gr_satellites.exe`（ランチャー、未使用）が存在することを確認した。

**修正**: `gr_satellites_install.py`の`_bundled_executable_path()`のWindows分岐を
`Scripts/gr_satellites.exe`から`Library/bin/gr_satellites.py`に変更。既存の
`resolve_gr_satellites_command()`（`[bundled_python, bundled_script]`形式で明示的に
呼び出す設計）はそのまま活用でき、macOS/Linuxで実証済みの安全な呼び出しパターンが
Windowsでもそのまま機能する。CIのWindowsスモークテストも、この正しいパスを使った
`gr_satellites --version`をmacOS/Linuxと同じ「合否判定に使う本番相当のテスト」に格上げ済み。

**3回目の`workflow_dispatch`実行で3プラットフォームとも完全グリーン、CI側の検証は完了
（2026-07-31）**: `Library/bin/gr_satellites.py`への修正後の再実行で、macOS・Linux・Windows
すべて`gr_satellites --version`が実際に`gr_satellites v5.9.0`のバージョン文字列を正しく出力し
「Smoke test passed.」で終了することを確認した。これでCI側（ビルド・`conda-pack`・
`conda-unpack`・実際のCLI起動）は3プラットフォームとも実証済み。

**`gr_satellites_dialog.py`の追随修正**: 上記CI調査と並行してダイアログ側（`_InstallWorker`の
ダウンロード拡張子・展開・`conda-unpack`呼び出し分岐）もすでに正しい実装になっていたため、
追加の実装は不要だった。唯一、Windows向け手動インストール手順（バンドルを使わない代替経路）
の案内文に残っていた「Windows対応は限定的、WSL2推奨」という古い注記だけを削除し、
バンドル版が推奨経路であることを明記するよう更新した。

**残る未検証点（2026-07-31時点）**: CI上のスモークテストでの動作確認までは完了したが、
「実際にユーザーのマシンでHelp画面の『Download & Install』を押し、GitHub Releasesから
実際にダウンロード・展開して、Telemetryタブで実際の衛星テレメトリーを受信できるか」という
アプリ経由のエンドツーエンド検証はまだ行っていない。

#### AI-CW デコーダーについて
- 候補: **morse-decoder**（PyTorch CNN ベース）・**DeepMorse**・**cwdecoder**（RNN）など
- 従来のゼロクロス検出方式より S/N 比の低い信号でも高精度にデコード可能
- 内蔵実装方針: sounddevice または SdrAudioSource から 8kHz PCM を取得 → Python 内で推論
- モデルファイル（数 MB 程度）はアプリバンドルに同梱するか、初回起動時に自動ダウンロード
- ライセンスに注意（MIT または Apache 2.0 のモデルを選定すること）

### SDR Device Installation ダイアログ（Help メニュー）

- USB VID/PID スキャン（`pyusb`）でデバイスを識別
- SoapySDR インストール状態を表示
- **Linux**: `pkexec apt-get install` でボタン操作による自動インストール
- **Windows**: PothosSDR はブラウザでダウンロードページを開く（`QDesktopServices.openUrl`）、Zadig は直リンク `.exe` をダウンロードして起動。いずれもウィザード操作はユーザーが手動で行う
- **macOS**: Homebrew があれば `brew install` を自動実行
- すでにインストール済み環境では即 `✅ Ready` 表示

### 依存パッケージ（optional）

```toml
[project.optional-dependencies]
sdr = [
    "pyusb>=1.2",         # USB VID/PID スキャン（SDR Device Installation 用）
    "scipy>=1.12",        # DSP フィルタ・ヒルベルト変換
    "sounddevice>=0.4",   # 音声出力（PortAudio ラッパー）
]
# SoapySDR はシステムパッケージ経由のため pip dependencies に含めない
```

### SDR のドップラー補正 — 再同調デッドバンド（2026-08-06 実装）と、デジタル補正への発展余地

#### 発端となった問い

「gr-satellites や SatDump は SDR の中心周波数をドップラー補正しなくても復調できると聞くが、
本当か。ならば FBSAT59 もそれらの受信中は補正しないほうがよいのではないか」というユーザーからの
指摘。調査の結果、**SatDump については正しく、しかも FBSAT59 は既にそう実装されていた**一方、
**gr-satellites については一般には成り立たない**と判明した。加えて、gr-satellites 経路には
「補正するか否か」とは別の実害（毎秒のハードウェア再同調）が潜んでいた。

#### 前提: 2つの経路はドップラーの扱いが根本的に違う

| 経路 | SDR の所有者 | 中心周波数 | ドップラー補正 |
|---|---|---|---|
| METEOR / HRPT（SatDump） | **SatDump 自身** | `METEOR_PIPELINES` のハードコード値を `--frequency` で固定 | **なし**（元から） |
| Telemetry（gr-satellites） | FBSAT59（`SdrRigAdapter` = Rig 1/2） | `_doppler_cycle()` が毎サイクル書き込み | あり |

`MeteorTab._on_start()` は起動前に `_disconnect_sdr()` で FBSAT59 側の SDR を切り離すため、
**Rig Settings > SDR Settings のサンプルレート設定も METEOR には一切効かない**（SatDump へ渡る
のは `METEOR_PIPELINES` の `samplerate`: LRPT 1.2 Msps / HRPT 3 Msps）。この点はユーザーが
誤解しやすいので注意。

#### なぜ SatDump は補正不要で、gr-satellites は必要なのか

ドップラー量をシンボルレートと比較すると明確に分かれる（LEO の最大レンジレート ≒ 7 km/s）。

| 対象 | 周波数 | 最大ドップラー | シンボルレート | 比 | 判定 |
|---|---|---|---|---|---|
| LRPT | 137 MHz | ±3.2 kHz | 72 kbaud | 4% | Costas ループが余裕で吸収 |
| NOAA HRPT | 1698 MHz | ±40 kHz | 665 kbaud | 6% | 同上 |
| BPSK 1200（AO-73 等） | 145 MHz | ±3.4 kHz | 1.2 kbaud | 280% | **補正必須** |
| FSK 9k6 G3RUH | 435 MHz | ±10 kHz | 9.6 kbaud | 104% | **補正必須** |

gr-satellites 側の実際の許容範囲は、ドキュメントではなく実装
（`python/components/demodulators/*.py`）から読み取れる:

- **BPSK**: 前段 `freq_xlating_fir_filter` のローパスが `cutoff = baudrate * 2.0` ←
  これがハードリミット。その後段の `fll_band_edge_cc` の引き込みは概ね ±(0.5〜1)×baudrate
- **FSK（IQ）**: Carson 則のローパス `|deviation| + baudrate/2` がハードリミット。通過さえ
  すれば後段の `quadrature_demod` → `dc_blocker_ff` が静的オフセットを吸収する
- **AFSK（IQ）**: Carson 則が `fm_deviation + af_carrier + |deviation|` = 既定 3000+1700+500
  = **±5.2 kHz**

さらに FBSAT59 は `--iq` で渡していて `--f_offset` を指定していないため、gr-satellites は
**信号が IQ の中心（DC）にある前提**で動く。したがって gr-satellites 経路でドップラー補正を
止めるのは不可。「gr-satellites は補正不要」という通説は、広帯域 IQ 録音の後処理運用か、
SatNOGS 時代の FM/AFSK の慣行が一般化されて伝わったものと思われる。

#### 実際の問題は「補正の掛け方」だった — 毎秒のハードウェア再同調

`SdrRigAdapter.set_vfo_frequencies()` にはしきい値が無く、`_doppler_cycle()` が毎サイクル
（既定1秒）無条件に `set_center_freq()` を呼んでいた。**10分のパスで約600回**の再同調になる。
ハードウェア再同調はチューナ PLL の再ロックを伴い位相が不連続になる（RTL-SDR では数サンプル
落ちることもある）。1200 baud の AX.25 フレームは 256 バイトで約1.7秒かかるので、
**事実上すべてのフレームが1回以上の再同調で分断されていた**計算になる。

#### 実装した対策（案B・2026-08-06）

`SdrRigAdapter` に再同調デッドバンドを追加（`_SDR_RETUNE_DEADBAND_HZ = 200.0`）。

- `set_frequency()` は、前回**実際に書き込んだ**値から 200 Hz 未満の要求を握りつぶす。
  基準を「前回の要求値」ではなく「前回の書き込み値」に置くのが要点で、要求値基準にすると
  小刻みな変化が延々と抑制され続けて周波数が青天井にずれる
- 書き込みが失敗した場合は基準を更新しない（失敗した値が基準になると以後ずっとずれる）
- `set_retune_deadband(hz)` で変更可能（`0` で従来どおり毎サイクル再同調に戻る）。現状 UI からは
  設定できず、コード上の定数のみ
- 効果は概算で再同調回数が約1/6（435 MHz で約100回/パス、145 MHz で約34回/パス）

**デッドバンドを迂回する経路（重要）**: 100 Hz 刻みの Passband Tune など、
**ドップラー以外の理由で目標周波数が動く操作は、デッドバンドに飲み込まれてはならない**。
`SdrRigAdapter.invalidate_retune_cache()` を呼ぶと次回の書き込みが無条件になる。
`MainWindow._invalidate_sdr_retune_cache()`（Rig 1/2 のうち SDR の方を探して呼ぶヘルパー）が
以下から呼ばれる:

| 呼び出し元 | 理由 |
|---|---|
| `_on_sdr_tune_offset()` | Passband Tune の矢印（最小 100 Hz、デッドバンド未満） |
| `_on_sdr_manual_freq_requested()` | Freq 欄への手入力 |
| `_on_sdr_lock_changed(False)` | Lock 中は一切書いていないので基準が陳腐化している |
| `connect()` / `disconnect()`（アダプター内部） | デバイスの状態が未知 |

トランスポンダー変更・衛星選択解除は、`SdrControlWidget.reset_tune_offset()` が
`tune_offset_changed(0.0)` を emit → `_on_sdr_tune_offset()` に届くため、**既存のシグナル
連鎖で自動的にカバーされている**（個別の呼び出し追加は不要）。

**既知のトレードオフ**: SDR の音声を SSB/CW で聴いている間は、最大 200 Hz の残差が音程の
ふらつきとして聞こえる。NFM では聞き取れず、デジタル復号系（gr-satellites・CW Decoder 等）
には無関係。SSB/CW 常用で気になる場合は `set_retune_deadband()` を小さくするか、
下記の案Aに進むこと。

**未検証**: 再同調グリッチが実際にどれだけフレーム損失を招いていたかは実測していない。
この対策自体が、その仮説の検証になる位置づけ（改善が見えなければ原因は別のところにある）。

テスト: `tests/test_rig.py::TestSdrRetuneDeadband`（フェイクデバイスで書き込み回数を検証。
初回書き込み・抑制・境界越え・基準が前回書き込み値であること・`invalidate` による強制書き込み・
書き込み失敗時に基準を汚さないこと・`0` で無効化・切断でリセットを網羅。実 SDR 不要）。

#### 案A（未実装）— ハードウェアを固定し、ドップラーをデジタルに適用する

デッドバンドは再同調の**回数を減らす**だけで、位相不連続を**ゼロにはできない**。しかも
435 MHz の高仰角パスでは TCA 付近のドップラー変化率が約 170 Hz/s に達するため、
**最も信号が強いまさにその時間帯だけ、デッドバンドがほとんど効かない**（1.2秒に1回は再同調
する）。根本解決するなら、チューナは公称周波数に固定したまま、IQ サンプル列に

```
出力[n] = 入力[n] × exp(-j·2π·Δf·n / fs)     Δf = ドップラー補正後の周波数 − 固定した中心周波数
```

を掛ける方式（デジタル局発）になる。位相アキュムレータをチャンク間で持ち越せば位相は完全に
連続で、`Δf` を更新した瞬間も波形が途切れない。SatNOGS や `gr-gpredict-doppler` と同じ考え方。

**既存の再利用可能な部品**: `src/comms/ax100digi/audio_bridge.py` の `FrequencyShifter` が
まさに「位相アキュムレータを持ち越すステートフルな複素ミキサ」。AX100 Digi 用に書いたものだが、
`Δf` を実行中に差し替えられるようにする改修だけで流用できる。

**実装位置の設計判断（未決）**:
- **A-1: `SDRPipeline` の中に入れる** — 下流すべて（SDR Control の音声復調・スペクトラム・
  gr-satellites への UDP 転送）が補正済みストリームを受け取る。SDR で音声も聴く場合に
  現在と同じ使い勝手が保てるので本命。ただしスペクトラム表示の「中心周波数マーカー」が
  ハードウェアの実周波数を指すのか補正後の論理周波数を指すのか、意味づけの整理が要る
- **A-2: `_UdpIqForwarder.push_samples()` だけに入れる** — 変更範囲は最小だが、
  gr-satellites 受信中はハードウェアが公称周波数で固定されるため、同時に SDR の音声を聴くと
  衛星が中心からずれて聞こえる

**CPU コスト（この開発機 i3-N300 で実測）**: 1コアに対し 2.4 Msps で 10.4% / 1.024 Msps で
3.3% / 250 ksps で 0.8%。Celeron N4000（Goldmont Plus・2コア・**AVX 非対応**）は概ね 3〜4倍
遅いと見込まれるため、2.4 Msps では1コアの 35〜40%（＝2コア全体の約20%）に達する。
250 ksps なら約3%で実用範囲。

#### 非力なマシン（Celeron N4000 級）向けの設定指針

実は案A/Bの選択より、**SDR サンプルレートのほうが桁違いに効く**。gr-satellites の初段ローパスは
遷移幅を `baudrate * 0.2` で設計するため、**タップ数がサンプルレートに正比例して増える**:

| SDR サンプルレート | FIR タップ数（BPSK 1200 の場合・概算） | 演算量 |
|---|---|---|
| 2.4 Msps（既定値） | 約 33,000 | 約 400 M MAC/s |
| 250 ksps | 約 3,400 | 約 41 M MAC/s |

UDP ループバックの転送量も complex64 で 19.2 MB/s → 2 MB/s と1/10になる。**UDP には
フロー制御がないため、gr_satellites が処理落ちするとエラーも出ずにデータグラムが捨てられ、
単に「デコードされない」という形で現れる**——非力なマシンで最もはまりやすい失敗モード。

**アマチュア衛星のテレメトリー（1200〜9600 baud）に 2.4 MHz の帯域は完全に過剰**なので、
Rig Settings > SDR Settings のサンプルレートを **250 kHz**（`_SAMPLE_RATES` の先頭、
[rig_dialog.py](src/ui/rig_dialog.py)）にすることをまず勧める。既定が 2.4 MHz なのは
汎用の SDR Control 用途に合わせたもので、gr-satellites 用途では不適切。

注意: RTL-SDR は 300 k〜900 kHz のレートをハードウェア的に受け付けない。`_SAMPLE_RATES` が
250 kHz の次に 1.0 MHz へ飛んでいるのはそのためで、中間値を追加してはいけない。

METEOR 側は前述のとおりレートがハードコードで、LRPT（72 kSym/s・占有帯域 約115 kHz）なら
理屈の上では 250 ksps の窓（±125 kHz）に収まるが、SatDump 側のリサンプラ最低入力レート等の
制約は未検証。HRPT（665 kSym/s・占有帯域 約1.1 MHz）は 250 ksps では原理的に不可能。

### Remote SDR（SoapyRemote）対応（2026-07-14 実装・GitHub Issue #12）

#### 背景

ユーザーから「アンテナ設備は別棟（裏庭の小屋等）にあり、そこに置いたSDRをネットワーク経由で
使いたい」という要望（[Issue #12](https://github.com/JF9SOM/FBSAT59/issues/12)）があった。
SoapySDR公式の`SoapyRemote`モジュールがまさにこの用途向けに存在し、クライアント側は普通の
SoapySDRモジュールとして振る舞うため、既存の`SdrDevice`アーキテクチャにそのまま乗せられる。

#### なぜWindowsのRTL-SDR/HackRF ctypesバイパスと無関係なのか

WindowsでRTL-SDR/HackRFだけ`SoapySDR::Device::make()`を経由せずctypes直接呼び出しに
バイパスしている理由は、SoapySDRの列挙・オープン処理が`libusb_init()`等を複数回呼ぶことで
**WinUSBの内部ハンドルキャッシュが破損する**という、ローカルUSBデバイスアクセス特有の問題
（詳細は「SDR 機能（v0.1.0 時点で実装済み）」セクション参照）。

SoapyRemoteのクライアント側はUSB/libusbを一切呼ばない純粋なTCP/IPネットワーククライアント
（実際にUSBを触るのは物理的にSDRが繋がっているリモート側のマシンだけ）であり、この問題の
対象外。`SdrDevice.open()`（[device.py](src/sdr/device.py)）は`driver=="rtlsdr"`/`"hackrf"`
の場合だけ明示的にctypesバイパス分岐に入り、それ以外（Airspy・AirspyHF・そして`remote`も
該当）は汎用の`SoapySDR.Device(args)`パスを通る。Airspy/AirspyHFはこの汎用パスでWindows実績
があるため、`remote`も同じ土俵に乗ると考えられる（実機・実際のリモートSoapySDRServerでの
動作確認は次回のパス待ち）。

#### デバイス文字列の仕様（SoapyRemote公式wiki確認済み）

```
driver=remote, remote=<host>[:port], remote:driver=<実機ドライバ名 例:rtlsdr>
```

同一LAN内であればUDPブロードキャストで自動検出され`SoapySDR.Device.enumerate()`結果に
`driver="remote"`として現れる。従来`_NON_SDR_DRIVERS`（[device.py](src/sdr/device.py)）が
これを除外していたため、実際には動く状態でも一覧に出てこなかった。除外リストから`"remote"`
を削除済み（`audio`/`null`/`mircsdr`は引き続き除外）。

#### 手動ホスト指定（LANブロードキャストが届かない場合）

VPN経由・ルーティングを跨ぐ構成等、自動検出が機能しない環境向けに、Rig Settings > SDR
Settingsに **「Add Remote Host…」** ボタンを追加（[rig_dialog.py](src/ui/rig_dialog.py)の
`_AddRemoteHostDialog`）。Host・Port（デフォルト55132）・任意のRemoteドライバー名ヒント・
任意の表示名を入力すると、上記デバイス文字列を組み立てて`SdrDeviceInfo(driver="remote", ...)`
としてデバイスコンボに追加する。

**永続化**: 新規テーブルは作らず、既存の`sdr_settings`（`app_settings`テーブルの単一JSON
キー）に`remote_hosts`フィールドを追加する形で保存する（`_SdrSettingsPanel.save()`/`load()`）。
これにより`RigSettingsDialog`側の読み書きプラミング（`self._conn`経由のapp_settings
読み書き）を一切変更せずに済んだ。

**実際の接続時の扱い**: `MainWindow._build_sdr_rig_adapter()`（main_window.py）は元々
`sdr_settings['device_args']`という生のSoapySDR argsディクショナリだけを見て
`SdrDeviceInfo`を再構築しており、コンボのインデックスや列挙結果には一切依存していない。
そのため「Add Remote Host…」で追加したエントリも、選択して保存すれば以降の起動時に
再列挙・再検出なしでそのまま接続対象になる（手動追加データが自動検出に優先する、という
本プロジェクト全体の設計方針——手動登録トランスミッタ・手動TLE等——と同型のパターン）。

**Remove**: 選択中のデバイスが「Add Remote Host…」で追加した保存済みエントリの場合のみ
「Remove」ボタンが有効になる（実ハードウェアやLANブロードキャストで自動検出された`remote`
エントリでは無効のまま。ローカルの保存リストから消す対象ではないため）。

#### パッケージ配布状況（全プラットフォーム調査済み）

| OS | パッケージ | 対応方法 |
|---|---|---|
| Linux | `soapysdr-module-remote`（apt） | ユーザーが自分でシステムインストール（rtlsdr/hackrf等の既存モジュールと同じ扱い。CIバンドル対象外——LinuxのAppImageはSoapySDR自体をCIでバンドルしていない） |
| macOS | `soapyremote`（Homebrew） | 同上（macOSもCIでSoapySDRをバンドルしていない） |
| Windows | `soapysdr-module-remote`（conda-forge, win-64, v0.5.2） | **CIでバンドル**（Windowsのみ`scripts/extract_soapy_conda.py`経由でconda-forgeパッケージを抽出する既存方式を踏襲） |

サーバー側（実際にSDRが繋がっているマシン）のセットアップはFBSAT59の管轄外（ユーザー自身が
`SoapySDRServer`を起動する）。Help > SDR Device Installationダイアログに「Note for Remote
SDR Users」として案内文を追加済み（[sdr_install_dialog.py](src/ui/sdr_install_dialog.py)）。

#### Windows CIバンドルの詳細（依存関係調査済み・低リスク確認済み）

`soapysdr-module-remote-0.5.2-h23704b7_2.tar.bz2`の`info/index.json`を実際にダウンロードして
`depends`フィールドを確認したところ:
```
soapysdr >=0.8.0,<0.9.0a0
vc >=14.1,<15.0a0
vs2015_runtime >=14.16.27012
```
**Boostへの動的リンク依存なし**（静的リンク済みと推定）。`vc`/`vs2015_runtime`は他の
conda-forgeパッケージ（既存のsoapysdr本体等）も要求する標準MSVC再頒布ランタイムで、
GitHub Actions Windowsランナーには元々インストール済み。ビルドタグ`h23704b7`は既存の
`soapysdr-module-hackrf-0.3.4-h23704b7_0`・`soapysdr-module-airspy-0.2.0-h23704b7_0`と同じで、
現在ピン留めしているSoapySDR本体（`soapysdr-0.8.1-py311haef1a59_6.conda`）と同一ABI世代
であることも確認済み。パッケージ内容は単一ファイル
`Library/lib/SoapySDR/modules0.8/remoteSupport.dll`のみで、`extract_soapy_conda.py`の
既存の汎用ロジック（`"modules" in name`のDLLを`soapy-win64/modules/`へ）がそのまま対応する
（rtlsdrSupport.dllのような特別スキップ処理は不要）。以上の理由から、過去にPlutoSDR追加時に
遭遇したようなCI不安定化のリスクは低いと判断したが、実際のビルド成否は`workflow_dispatch`
での確認が必要。

#### 実運用で発覚した「remoteSupport.dllは正常にロードされるがレジストリに登録されない」不具合と、ソースからの自前ビルドへの切り替え（2026-07-16 調査・対応）

v0.2.15リリース後、Issue #12の報告者（Windows・PothosSDR併用環境）から「Add Remote Hostで
リモートホストを追加し接続すると常に`SoapySDR::Device::make() no match`で失敗する」という
報告を受け、複数ラウンドの診断（ログ収集・ファイアウォール診断・`LoadLibrary`直接テスト用の
診断スクリプトを都度作成）で以下を順に否定した:

1. **PothosSDR併用によるモジュール重複**（`duplicate entry for remote`ログ） — PothosSDR
   完全アンインストール後も再現し否定
2. **Windowsファイアウォールによるブロック** — FBSAT59に対する明示的Inbound Allowルールが
   既に存在・Outboundは既定で許可・管理者権限実行でも再現し否定
3. **アンチウイルスによる隔離** — 直近のWindows Defender検出履歴なしで否定
4. **`soapy_modules`配下のDLLが一般的にロードできない問題** — 診断スクリプト第1版は
   `SetDllDirectory`を呼ばずに素の`LoadLibrary()`でテストしていたため、`main.py`の
   `os.add_dll_directory(_MEIPASS)`と同じ検索パスが再現できておらず、5モジュール全てが
   `ERROR_MOD_NOT_FOUND(126)`という偽陽性を出していた（スクリプト自身のバグ、実アプリの
   不具合ではない）。`SetDllDirectory`を追加した修正版で再テストすると
   **`remoteSupport.dll`を含む全モジュールが`LoadLibrary`単体では正常に成功**することを確認

上記4点を踏まえて`pothosware/SoapySDR`の`lib/Modules.in.cpp`・`lib/Factory.cpp`・
`lib/Registry.cpp`・`client/Registration.cpp`（SoapyRemote側）のソースを直接精査した結果、
以下が判明した:

- `Modules.in.cpp`の`loadModules()`は**エラーがない場合は一切ログを出力しない**仕様
  （`errorMsg`が空文字なら`SoapySDR::logf()`自体を呼ばない）。そのため実機ログで
  `remoteSupport.dll`の`loadModule(...)`行が一度も出ないことは「スキップされている」
  のではなく「エラーなくロード・登録に成功している」ことを示す可能性がある——という
  解釈にいったん傾いたが、`Factory.cpp`の`Device::make()`を読むと「no match」は
  `Registry::listMakeFunctions()`に指定した`driver`名（`remote`）のキーが
  **一つも見つからない場合にのみ**発生する処理であることが確定した。ABI不一致・重複の
  どちらのエラーであっても`Registry::Registry()`コンストラクタは`errorMsg`を設定し、
  それは必ず`loadModules()`側でログに出る（`getLoaderResult()`経由）。エラーログが
  一切ないのにレジストリにキーが存在しない、という組み合わせは、`LoadLibrary`自体は
  成功しているのに`client/Registration.cpp`末尾の
  `static SoapySDR::Registry registerRemote("remote", &findRemote, &makeRemote,
  SOAPY_SDR_ABI_VERSION);`という**ファイルスコープの静的初期化子がDLLロード時
  （DllMain相当）に正しく実行されていない**ことを強く示唆する

- 独立した参考データとして、同じくSoapySDRベースの別のOSS衛星追尾ソフト
  [SkyRoof](https://github.com/VE3NEA/SkyRoof)（GPLv3、C#からSoapySDRをP/Invoke経由で
  直接呼び出す実装）のソースを確認したところ、`Vendor/SoapySDR/SDR/remoteSupport.dll`に
  同梱されているバイナリのバージョン文字列は`0.6.0-c09b2f1`——pothosware/SoapyRemoteの
  公式タグ`0.5.2`（2020-07-20リリース）より後、`c09b2f1`コミット（2020-11-05、
  ref clock rate API追加）を含む非公式ビルドだった。FBSAT59がconda-forge経由で同梱していた
  のは公式`0.5.2`タグそのもの。SkyRoof側は同じ「remote」ドライバで正常に動作している
  （報告者もSkyRoofからは同じリモートSDRに問題なく接続できると証言）ことから、
  **conda-forgeの0.5.2ビルド固有の問題**（ビルド設定・リンカー最適化等でこの静的
  初期化子が欠落している可能性）を疑う根拠として扱った

以上を受け、`rtlsdrSupport.dll`（WinUSB対応で既にOsmocomソースから自前ビルド済み、
「Rig-Specific Implementation Notes」以前のセクション参照）と同じ方針で、
**`remoteSupport.dll`もconda-forgeバイナリを信用せず`pothosware/SoapyRemote`の
ソース（`master`、2026-07-16時点のHEAD。Changelog.txt上は0.5.2以降"0.6.0 (pending)"の
まま停滞しており、SkyRoofが使ったコミットと機能的に同一）から自前ビルドする**方針に
切り替えた（`.github/workflows/ci.yml`「Build SoapyRemote client from source (Windows)」
ステップ、`write_soapy_cmake_config.py`が生成する`SoapySDRConfig.cmake`をRTLSDRビルドと
共用）。

**`server`サブディレクトリの除外が必須**: `SoapyRemote`の`CMakeLists.txt`は
`add_subdirectory(client)`と`add_subdirectory(server)`を無条件で呼ぶが、
`server/CMakeLists.txt`は`target_link_libraries(SoapySDRServer PRIVATE SoapySDR
SoapySDRRemoteCommon)`という**名前空間なしの生の`SoapySDR`ターゲット**を参照する。
`write_soapy_cmake_config.py`が生成する簡易`SoapySDRConfig.cmake`は
`SoapySDR::SoapySDR`（名前空間付きIMPORTEDターゲット）しか定義しないため、
`add_subdirectory(server)`を残したままではconfigure段階で
「target "SoapySDR" not found」エラーになる。FBSAT59はSoapyRemoteの
**クライアント側（`remoteSupport.dll`）のみ**必要（サーバー機能は使わない）なので、
`scripts/patch_soapyremote_client_only.py`で`add_subdirectory(server)`の行を
コメントアウトしてから configure する（`add_subdirectory(system)`はLinux限定の
条件分岐が既についているため未対応のまま無害）。

**検証状況（2026-07-16時点）**: CIビルド自体（`workflow_dispatch`）の成否確認は未実施。
さらに重要な点として、**この自前ビルドが実際にIssue #12の症状を解消するかどうかも
まだ実機で未検証**——「conda-forgeビルド固有の問題」という仮説はSkyRoofとのバージョン差
という状況証拠に基づくもので、確定した原因究明ではない。次のリリースで報告者（および
開発者自身が用意する検証環境）に実際にテストしてもらい、症状が解消するかで仮説の
正否を判断する。解消しない場合はDllMain内の静的初期化子が実行されない別の原因
（Windows特有のローダーロック絡みの問題等）を追う必要がある。

---
