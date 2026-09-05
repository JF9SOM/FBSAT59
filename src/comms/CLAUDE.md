# src/comms/ 作業時の必読ドキュメント

このディレクトリ（APRS・Telemetry・SSTV/SSDV・FT4・Q65・CW・AX100 Digi・METEOR）を
変更・調査する前に、リポジトリルートの以下を必ず Read すること。デジタルモードの
タイミング設計・共有リソース管理・実運用で判明した多数の不具合が記録されている。

- [../../docs/communications.md](../../docs/communications.md) — 各タブの設計、Direwolf、Bell 202/G3RUH 復調、共有サウンドカード（`AudioDeviceManager`）、FT4 のタイミング/スレッド設計の一連の修正、SatNOGS DB アップロード、ログ UDP ブロードキャスト、Comms Quick Panel、コミュニティ周波数
- [../../docs/telemetry.md](../../docs/telemetry.md) — `src/comms/telemetry/`（Telemetryタブの衛星選択コンボ構築方式、AFSK自動トランスポンダー選択スコアリング、SATNOGS `alive`/`status` の意味論、gr-satellites 衛星カタログソース、ゴーストエントリ問題）
- [../../docs/meteor-satdump.md](../../docs/meteor-satdump.md) — `src/comms/meteor/`（SatDump サブプロセス管理、ライブ Waterfall、Autotrack 連携の不具合群 Issue #27）
- [../../docs/lock-dial-feedback.md](../../docs/lock-dial-feedback.md) — FT4/Q65 送信中のドップラー凍結解除など、リグ制御と絡む部分
- [../../docs/tle.md](../../docs/tle.md) — Telemetry のフレームを SatNOGS へ投稿する経路（Phase 2 計画含む）

ルートの [../../CLAUDE.md](../../CLAUDE.md)（コア規約）が常に優先。このファイルは索引のみ。
