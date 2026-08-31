# FBSAT59 詳細ドキュメント

CLAUDE.md 本体から分離した詳細ドキュメント一式。CLAUDE.md がコア（規約・ルール・
アーキテクチャ）、この `docs/` が機種別実装ノート・設計詳細・不具合調査履歴を担う。
**関連する機能の変更・不具合調査を行う前に、該当ファイルを必ず読むこと。** 普段は読み込まなくてよい。

| ファイル | 内容 |
|---|---|
| [docs/hamlib.md](docs/hamlib.md) | Hamlib バージョン管理・配布方針、in-app アップデーター、sys.path surgery、Rotator catch-up、NET Controller 実装メモ、NET モード FTX-1 Sub VFO 誤配送バグ |
| [docs/rig-specific-notes.md](docs/rig-specific-notes.md) | リグ機種別実装ノート（FTX-1F / FT-991 / IC-9100 / IC-9700 / IC-910H / IC-821H / IC-705）、CAT モード変換、GitHub Issue #16 続報（IC-9700） |
| [docs/lock-dial-feedback.md](docs/lock-dial-feedback.md) | Lock（L ボタン）dial feedback 設計、Ctrl+L、SDR 専用 Lock、Passband Tune 再設計 |
| [docs/doppler-tuning.md](docs/doppler-tuning.md) | 永続 per-transponder RX オフセット、帯域中心 Doppler 追尾、AO-73 反転トランスポンダー修正・SatNOGS 公式値リセット |
| [docs/tle.md](docs/tle.md) | TLE 取り込みルール全体設計、仮 NORAD ID（90000 番台）衛星管理、SATNOGS status 全件取得、CelesTrak/SATNOGS ブロック切り分け |
| [docs/sdr.md](docs/sdr.md) | SDR 機能設計方針（SoapySDR / Windows ctypes バイパス / PlutoSDR / Remote SDR / Doppler 補正）、実装済み SDR 機能一覧 |
| [docs/communications.md](docs/communications.md) | APRS / Telemetry / SSTV・SSDV / FT4 / Q65 / CW / AX100 Digi / gr-satellites / SatNOGS アップロード、Comms Quick Panel、共有サウンドカード、ログ UDP ブロードキャスト、コミュニティ周波数 |
| [docs/meteor-satdump.md](docs/meteor-satdump.md) | SatDump 検出・起動の一連の修正、METEOR/HRPT タブ、ライブ Waterfall、過去受信フォルダ、Autotrack 連携の不具合群（Issue #27） |
| [docs/ui-components.md](docs/ui-components.md) | Dashboard タブ、Group Pass Chart、Autotrack 設計、カスタム Favorite グループ、スマホ Web UI |
| [docs/moon-eme.md](docs/moon-eme.md) | Moon/EME 追尾設計（DE421 / CelestialEngine / EME ドップラー往復補正 / EME 周波数） |
| [docs/i18n.md](docs/i18n.md) | 多言語化ロードマップ、翻訳範囲、i18n 実装上の落とし穴、日本語 .po 更新手順 |
| [docs/known-issues.md](docs/known-issues.md) | 既知の制約（プラットフォーム由来・修正不可）、既知のバグ（未修正） |
| [docs/ci-cd.md](docs/ci-cd.md) | CI/CD トラブルシューティング履歴（Hamlib / macOS / Windows / ft4wsjt ビルド固有） |
| [docs/dev-environment-migration.md](docs/dev-environment-migration.md) | 開発環境移行 Ubuntu → macOS（2026-08-15）の記録 |
| [docs/roadmap.md](docs/roadmap.md) | 次回の作業候補 |
