# src/rig/ 作業時の必読ドキュメント

このディレクトリ（Hamlib 制御・rigctld・CAT/CI-V・`SdrRigAdapter`・ローテーター制御）を
変更・調査する前に、リポジトリルートの以下を必ず Read すること。過去に実機で確定した
機種別の制約・不具合・設計判断が大量に記録されており、読まずに変更すると既知の不具合を
再発させる。

- [../../docs/hamlib.md](../../docs/hamlib.md) — Hamlib バージョン管理/配布、in-app アップデーター、sys.path surgery、`HamlibNetController` 実装メモ、`HamlibRotatorController` catch-up、NET モード FTX-1 Sub VFO 誤配送バグ
- [../../docs/rig-specific-notes.md](../../docs/rig-specific-notes.md) — FTX-1F / FT-991 / IC-9100 / IC-9700 / IC-910H / IC-821H / IC-705 の機種別実装ノート、CAT モード変換テーブル、GitHub Issue #16 続報
- [../../docs/lock-dial-feedback.md](../../docs/lock-dial-feedback.md) — Lock（L ボタン）dial feedback（`controller.py` の周波数読み書き・VFO 選択に直結）
- [../../docs/doppler-tuning.md](../../docs/doppler-tuning.md) — per-transponder RX オフセット、帯域中心 Doppler 追尾基準

ルートの [../../CLAUDE.md](../../CLAUDE.md)（コア規約）が常に優先。このファイルは索引のみ。
