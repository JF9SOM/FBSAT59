# FBSAT59 ユーザーガイド — 概要

> このガイドはエンドユーザー向けで、アプリ内「AIヘルプ」の知識ベースです。
> 書かれていないことは推測で答えず、その旨を正直に伝えたうえで、
> Issue での報告を案内してください: https://github.com/JF9SOM/fbsat59/issues
>
> 画面の表記は「日本語表示（English）」の順で併記しています。表示言語を英語にしている場合は括弧内を見てください。

## FBSAT59 とは

アマチュア無線家向けの衛星追尾・通信ソフトです（GPredict の現代的な後継）。
Windows（8.1 以降、10/11 推奨）・macOS・Linux で動きます。主な機能:

- 衛星追尾: 世界地図・スカイレーダー・パスチャート・今後のパス・ダッシュボード
- TLE とトランスポンダー情報（SatNOGS）のバックグラウンド自動更新
- Hamlib 内蔵の無線機制御（rigctld の別起動は不要）: 周波数・モード・CTCSS のドップラー補正、ローテーター制御
- SDR 対応（RTL-SDR・HackRF など）: スペクトラム・復調・IQ 録音
- 「通信（Communications）」メニューのデジタルモード: APRS、Message Box/Digipeater、FT4、Q65、テレメトリー、CW デコーダー、SSTV/SSDV、METEOR/HRPT
- 「自動追尾/記録（Autotrack/Record）」: AOS から LOS までの自動追尾・リグ/ローテーター接続・録音
- スマホ/タブレットからの閲覧: ステータスバーに表示される URL（ポート 8080）を同一 LAN のブラウザで開く

## メニュー早見表

| メニュー | 主な内容 |
|---|---|
| ファイル（File） | QTH設定（自局位置）、全般設定、終了 |
| 衛星（Satellite） | トランスミッターの追加/編集/削除、衛星を隠す、TLEを手動追加、TLEを更新、トランスミッターDBを取得 |
| 無線機（Radio） | 無線機設定、ローテーター設定 |
| 通信（Communications） | APRS、Message Box/Digipeater、FT4、Q65、Telemetry、CW Decoder、SSTV/SSDV、METEOR/HRPT |
| 自動追尾/記録（Autotrack/Record） | 自動追尾/記録ダイアログを開く |
| ツール（Tools） | 登録した Web サイト（全般設定の Tools タブで編集） |
| 表示（View） | 言語（切替後に再起動が必要）、タイムゾーン（UTC/ローカル）、外観 |
| ヘルプ（Help） | 自動取得ルール、更新を確認、SDR ドライバー・Hamlib 更新・ft8lib・Direwolf・SatDump・gr-satellites・CW モデルの各インストール、バージョン情報、GitHub |

## 基本の流れ

1. ファイル > QTH設定 で自局位置を入れる
2. 初回の TLE/トランスポンダー取得（自動）を待つ
3. 衛星リストから衛星を選ぶ（地図・レーダー・パスが更新される）
4. 無線機 > 無線機設定 で無線機（または SDR）とポートを設定し、無線機コントロールで「Rig 1 に接続」を押す。トランスポンダーを選ぶとドップラー補正が始まる
5. 必要なら 無線機 > ローテーター設定 → 「ローテーターに接続」
6. 無人運用は「自動追尾/記録」を使う

## 保存場所

- ログ `fbsat59.log`（不具合報告に添付してください）:
  - macOS: `~/Library/Logs/fbsat59/fbsat59.log`
  - Windows: `%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log`
  - Linux: `~/.local/state/fbsat59/log/fbsat59.log`（古い環境では `~/.cache/fbsat59/log/`）
- データベース（設定・衛星・TLE）`fbsat59.db`: ユーザーごとのデータフォルダ
  （macOS `~/Library/Application Support/fbsat59`、Windows `%LOCALAPPDATA%\fbsat59\fbsat59`、Linux `~/.local/share/fbsat59`）

## 自動更新（通常は手動操作不要）

| データ | 間隔 |
|---|---|
| 宇宙ステーション（ISS など） | 1 時間 |
| アマチュア衛星 | 2 時間 |
| CubeSat | 4 時間 |
| 気象衛星 | 6 時間 |
| 地球観測/科学衛星 | 12 時間 |
| AMSAT 運用状況 | 24 時間 |
| SatNOGS トランスミッターDB | 起動時に 7 日以上古ければ更新 |

ヘルプ > 自動取得ルール でも確認できます。新規打ち上げ衛星など「今すぐ最新が欲しい」ときだけ
衛星 > TLEを更新、衛星 > トランスミッターDBを取得 を使います。

## 関連ガイド

- [getting-started.md](getting-started.md) — インストール・初回起動・無線機/SDR の設定
- [troubleshooting.md](troubleshooting.md) — 症状別の対処と不具合の報告方法
