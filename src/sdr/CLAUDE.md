# src/sdr/ 作業時の必読ドキュメント

このディレクトリ（SoapySDR バックエンド・デバイス列挙・IQ パイプライン・復調・録音）を
変更・調査する前に、リポジトリルートの以下を必ず Read すること。プラットフォーム別の
根本的制約（特に Windows）と実機で確定した挙動が記録されている。

- [../../docs/sdr.md](../../docs/sdr.md) — SoapySDR 採用方針、Windows ctypes 直接実装バイパス（RTL-SDR/HackRF）、PlutoSDR/BladeRF バンドル、Remote SDR（SoapyRemote）、`SdrRigAdapter`、Doppler 補正の再同調デッドバンド、`request_audio`/`release_audio`
- [../../docs/communications.md](../../docs/communications.md) — SDR を入力に使う各 Communications タブ（`audio_ready` 購読・`SDRPipeline` 再取得の罠）
- [../../docs/meteor-satdump.md](../../docs/meteor-satdump.md) — SatDump が SDR を排他使用する挙動、Autotrack 連携での取り合い

ルートの [../../CLAUDE.md](../../CLAUDE.md)（コア規約）が常に優先。このファイルは索引のみ。
