# src/data/ 作業時の必読ドキュメント

このディレクトリ（SQLite DB・マイグレーション・TLE 取得・SATNOGS/CelesTrak 同期・
トランスミッター管理・テレメトリーフォーマット定義）を変更・調査する前に、リポジトリ
ルートの以下を必ず Read すること。同期スケジュール・鮮度ゲート・IP ブロック回避の
設計判断と、過去の DB マイグレーション事故が記録されている。

- [../../docs/tle.md](../../docs/tle.md) — TLE 取り込みルール全体設計、ソース優先度・上書きルール、`fetch_active_tles()` の 2 フェーズ設計とサーキットブレーカー、仮 NORAD ID（90000 番台）→ 実 ID 移行パイプライン、SATNOGS status 全件取得、起動時鮮度チェックの網羅監査、CelesTrak/SATNOGS ブロック切り分け、DB マイグレーション注意事項
- ルート [../../CLAUDE.md](../../CLAUDE.md) の「データベーススキーマ（SQLite）」— テーブル定義の一次情報

`transmitters` テーブルの `mode` 列とリグ CAT モードの対応は [../../docs/rig-specific-notes.md](../../docs/rig-specific-notes.md) の「モード文字列 → リグ CAT モード変換テーブル」。

ルートの [../../CLAUDE.md](../../CLAUDE.md)（コア規約）が常に優先。このファイルは索引のみ。
