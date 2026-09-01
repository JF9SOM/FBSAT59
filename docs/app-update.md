# アプリ更新通知（起動時チェック + 重要アップデート告知）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## 全体像（2026-09-01 実装）

FBSAT59 には以前から**手動**の更新機能がある（`Help > Check for Updates…` =
`src/ui/app_update_dialog.py`。GitHub Releases の `releases/latest` を見て、
プラットフォーム別にダウンロード + インストールまで行う）。

2026-09-01、SATNOGS の DB 仕様変更（`docs/tle.md`「SATNOGS衛星 `status` 語彙の変更」）の
ように**旧バージョンが実害を出す**ケースに備えて、次の3点を追加した:

1. **起動時の自動チェック**（`src/core/update_check.py` + `MainWindow._start_update_check()`）
2. リリースを**「重要（critical）」とフラグ付けする仕組み**（`update_manifest.json`）
3. 重要な場合の**強めのポップアップ**（設定で無効化できない）

---

## `update_manifest.json`（リポジトリ直下）

`https://raw.githubusercontent.com/JF9SOM/FBSAT59/main/update_manifest.json` から取得
（CDN 配信・GitHub API のレート制限に無関係）。

```json
{
  "latest_version": "0.3.49",
  "minimum_supported_version": "0.3.49",
  "critical": true,
  "message_ja": "重要なアップデートがあります。…",
  "message_en": "A critical update is available. …"
}
```

| フィールド | 意味 |
|---|---|
| `latest_version` | 最新リリースのバージョン（`v` は付けない） |
| `minimum_supported_version` | これ未満は「重要」対象。省略時は `latest_version` と同値 |
| `critical` | `true` かつ `current < minimum_supported_version` のときだけ強制ポップアップ |
| `message_ja` / `message_en` | 表示文言。**表示専用**。コマンドとして解釈しない |

**ダウンロード URL はマニフェストに持たせない。** 実際の取得は
`app_update_dialog` 側の `releases/latest` アセット解決を使う。マニフェストが
改ざん・タイプミスされても不正なバイナリへ誘導できないようにするため。

### リリース時の運用（重要）

- **毎リリース**: `latest_version` を新バージョンへ更新する
- **旧バージョンが実害を出すリリースのときだけ**: `minimum_supported_version` を
  その新バージョンへ引き上げ、`critical` を `true` にする
- 通常リリースでは `critical` は `false` に戻す（さもないと全員に強制ポップアップが出続ける）
- `update_manifest.json` は `main` ブランチのファイルなので、**タグを打つ前に**
  main へマージ/プッシュしておくこと（タグの中身ではなく `main` の HEAD が参照される）

---

## 判定ロジック（`core.update_check.evaluate()` — 純関数）

| 条件 | レベル | UI 挙動 |
|---|---|---|
| `critical` かつ `current < minimum_supported_version` | `CRITICAL` | **設定に関わらず**モーダル `QMessageBox`（Warning）。［今すぐ更新する］→ `AppUpdateDialog` を開く／［後で］→ 閉じる（次回起動で再表示） |
| `current < latest_version`（上記以外）かつ skip 済みでない | `NEW_VERSION` | `update_check_on_startup` が ON のときだけ情報 `QMessageBox`。［更新する］／［このバージョンをスキップ］（`update_notify_skipped_version` に保存）／［後で］ |
| それ以外（`current >= latest`、パース不能で非 critical、skip 済み） | `UP_TO_DATE` | 何もしない |

- バージョン比較は `packaging.version.Version`。`0.3.50.dev3 < 0.3.50`（dev はタグより前に
  ソートされる）なので、**dev ビルドはそもそも起動チェックの対象外**にしている
  （`is_release_version()`: `.devN` と `0.0.0`〔`workflow_dispatch` テストビルドの
  プレースホルダ〕を弾く。`MainWindow._start_update_check()` の入口でチェック）
- `current` がパース不能でも `critical` だけは発火する（floor 未満とみなす）

---

## 設定（Settings > Notifications タブ）

- 「Check for updates on startup」チェックボックス（`app_settings` キー
  `update_check_on_startup`、既定 ON）
- **重要アップデートの告知はこの設定に関わらず常に行われる。** チェックを外しても
  マニフェストの取得自体は毎起動走る（数百バイト）。設定が抑制するのは
  `NEW_VERSION`（通常の新版通知）だけ
- 単一の真実として `SettingsDialog.get_update_check_on_startup(conn)` を使う
  （`MainWindow._update_check_on_startup_enabled()` はこれに委譲）

## `app_settings` キー

| キー | 値 | 用途 |
|---|---|---|
| `update_check_on_startup` | `"1"` / `"0"` | 通常の起動時チェックの ON/OFF（未設定=ON） |
| `update_notify_skipped_version` | バージョン文字列 | 「このバージョンをスキップ」した版。より新しい版が出れば再通知 |

---

## 失敗時の挙動

ネットワーク不通・タイムアウト・JSON パース失敗・マニフェストのフィールド欠落 —
いずれも `logger.debug` に落として**無言で終了**。ダイアログもエラーも出さない。
起動処理がこのチェックに依存することは一切ない（`QTimer.singleShot(5000, …)` で
遅延起動 + `QThread` で別スレッド取得）。

---

## テスト

`tests/test_update_check.py` — `evaluate()` のマトリクス（最新/新版/critical/
skip/パース不能/dev ビルド/言語フォールバック）、`parse_manifest()`、
`is_release_version()`。Qt もネットワークも不要。
