# はじめに

## 1. インストール

https://github.com/JF9SOM/fbsat59/releases/latest からダウンロードします。

- **Windows**: `FBSAT59-Setup.exe` を実行。Windows 8.1 以降が必要です（無印 Windows 8 は非対応で、DLL 不足エラーで起動できません）。
- **macOS**: `FBSAT59.dmg` を開き、アプリを Applications へドラッグ。
- **Linux**: AppImage をダウンロードし `chmod +x FBSAT59-*.AppImage` して実行。USB 無線機を使うには `dialout` グループに自分を追加し、ログインし直してください。

以降のバージョンは ヘルプ > 更新を確認（Check for Updates）で入れられます。

## 2. 初回起動

1. スプラッシュ画面が出ます。初回はデータ取得のため時間がかかります。
2. **ファイル > QTH設定（Set QTH...）** で自局位置を入力します。緯度・経度・標高、または **グリッドロケーター（Grid Locator）** タブ（メイデンヘッド）が使えます。QTH が違うとパス時刻がずれます。
3. TLE と SatNOGS のトランスポンダー情報は自動で取得されます。衛星リストが空なら、インターネットに接続したまま数分待ち、それでも空なら 衛星 > TLEを更新 を試してください。
4. 表示 > タイムゾーン で UTC/ローカルを切替。表示 > 言語 で言語を変更（再起動が必要）。

## 3. 無線機を接続する（Hamlib）

1. **無線機 > 無線機設定...（Rig Settings）** には Rig 1・Rig 2 のタブのほか、SDR Settings・Sound Card・PTT のタブがあります。Rig 1 タブで接続方式（**Direct (Hamlib built-in)**／既存の rigctld を使う **NET (rigctld compatible)**／**SDR**）を選び、機種・シリアルポート・ボーレート（Icom は CI-V アドレスも）を設定します。
2. OK を押します。ダイアログを閉じても既存の接続は維持されます。
3. 無線機コントロールタブの **Rig 1 に接続** を押します。成功すると緑の「接続: 機種名」（SDR は水色の「SDR: 接続中」）、失敗すると赤の「未接続」と表示されます。
4. 衛星、続いてトランスポンダーを選ぶと、周波数・モード・トーンが設定され、ドップラー補正が追従します。「Cycle」プルダウンでリグの更新間隔を変えられます。
5. Rig 2 も同様です（例: 受信用に SDR を Rig 2 に割り当てる）。

ローテーター: **無線機 > ローテーター設定...**、続いて無線機コントロールの **ローテーターに接続**。赤の「未接続」はローテーターが応答していません（電源・ケーブル・ポートを確認）。

## 4. SDR を使う

1. SDR（RTL-SDR・HackRF など）を接続します。
2. **無線機 > 無線機設定... > SDR Settings** で **列挙（Enumerate）** を押し、デバイス・サンプルレート・ゲインを設定して Rig 1 または Rig 2 に割り当てます。
3. 無線機コントロールから接続すると **SDR Control** タブが有効になります（スペクトラム・復調・IQ 録音）。

OS ごとの注意:

- **Windows**: RTL-SDR と HackRF は **Zadig による WinUSB ドライバーの導入（一度だけ）** が必要です（https://zadig.akeo.ie/）。デバイスを接続 → Zadig の Options > List All Devices → デバイスを選択（RTL-SDR は Bulk-In, Interface 0 / HackRF は HackRF One）→ ドライバーを **WinUSB** にして Install Driver → FBSAT59 を再起動。**libusbK は選ばないでください。** ドライバー未導入でもデバイス一覧には表示され、開く段階で初めて失敗します。ヘルプ > SDRデバイスのインストール も参照。Airspy・Airspy HF+・ADALM-Pluto は Windows では未対応です。
- **macOS**: RTL-SDR・HackRF・Airspy・Remote SDR は同梱済みで、インストール不要です。
- **Linux**: SoapySDR モジュールを入れます。例: `sudo apt install python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf`
- SDRplay と ADALM-Pluto は同梱されていません（別途ソフトが必要。README 参照）。

## 5. デジタルモード

**通信（Communications）** メニューから開きます。それぞれ閉じられるタブです。

- 追加コンポーネントが必要なものは **ヘルプ** メニューから導入します: ft8lib（FT4）、FT4 Enhanced Decoder、Q65 Library、Direwolf（サウンドカードでの APRS/テレメトリー）、SatDump（METEOR/HRPT）、gr-satellites（テレメトリー、330 以上の衛星）、CW Model（CW デコーダー）
- 無線機からの音声は、無線機設定の **Sound Card** タブで選んだ入出力を使います。

## 6. 自動追尾/記録（Autotrack/Record）

メニューのダイアログで、衛星とトランスポンダーのリストを作り、自動追尾（AOS でリグとローテーターを接続、LOS で切断）、IQ 録音、METEOR/HRPT 受信、開始/停止タイマーを設定します。AOS/LOS の計算は選んだ衛星で決まるため、**受信したい衛星と同じ衛星**を選んでください。ローテーターが無い場合は「ローテーターを使用する」のチェックを外します。

## 7. スマホ/タブレットから見る

ステータスバーに URL（と QR コードボタン）が表示されます。同じ LAN のブラウザで開いてください。ポート 8080 をファイアウォールで塞がないこと。
