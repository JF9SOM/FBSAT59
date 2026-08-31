# UI コンポーネント詳細（Dashboard / Pass Chart / Autotrack / Web UI 他）

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

### カスタムFavoriteグループ設計（src/data/database.py）

```sql
CREATE TABLE custom_groups (
    id          INTEGER PRIMARY KEY,  -- 1-based group number
    name        TEXT NOT NULL,        -- display name (e.g. "Favorite 1")
    sort_order  INTEGER NOT NULL DEFAULT 0
);
-- satellites テーブルに favorite_group INTEGER DEFAULT 0 カラムを追加
-- 0=未所属, 1..N=custom_groups.id
```

- デフォルトで Favorite 1/2/3 を作成（既存 is_favorite=1 は Favorite 1 に移行）
- 右クリック → 「★ Favorite Groups」サブメニューでグループ割当・解除
- Settings > Custom Groups タブでグループ名インライン編集・追加・削除

### Dashboard タブ（src/ui/dashboard_view.py）

左2/3にズームマップ、右1/3にレーダー、下部に36pxのステータスバーを配置した統合ビュー。

#### レイアウト構造

```
┌─────────────────────────────┬──────────────┐
│  WorldMapView（ズーム）      │  RadarView   │
│  （2/3 幅）                 │  （1/3 幅）  │
├─────────────────────────────┴──────────────┤
│  ステータスバー（36px固定）                  │
│  衛星名 / EL / AZ / Range / 可視 / DL / UL │
└────────────────────────────────────────────┘
```

#### 主要な設計判断

- `QSplitter` で左右を分割。`setStretchFactor(0,2) / setStretchFactor(1,1)` + `setSizes([660, 330])` で初期2:1比率を強制
- Dashboard 表示時は Satellite Detail パネルを非表示: `currentChanged` ではなく起動時に `setVisible(False)` で初期化（`currentChanged` は初期タブでは発火しないため）
- ズームマップはグリッド線を非表示（`set_show_grid(False)`）— 衛星移動に伴う線のカクカク感を回避
- `isVisible()` チェックで非表示時の再描画をスキップ（CPU負荷削減）
- `track_data: SatTrackData | None` パラメータで Radar タブと同一のパス軌跡を表示
- レーダーの AOS/LOS 時刻表示は `set_use_utc()` で UTC/Local 切り替えに連動

#### WorldMapView への追加 API（src/ui/world_map.py）

| メソッド | 説明 |
|---|---|
| `set_show_grid(show: bool)` | グリッド線・赤道線の表示/非表示を切り替え |
| `set_zoom_region(lat, lon, span_deg)` | 指定座標を中心にズーム表示（デフォルト ±50°） |
| `clear_zoom()` | グローバルビューに戻す |

#### フットプリント描画の設計（`_draw_footprint` — src/ui/world_map.py）

**スキャンライン QPainterPath 方式**（ポリゴン方式から変更済み）:
- N=180 ラチチュードバンドを走査し、各バンドを `QRectF` として `QPainterPath` に追加
- `QPainterPath.setFillRule(Qt.FillRule.WindingFill)` で確実に全領域を塗りつぶし（OddEven 規則のワインディングキャンセル問題を回避）
- 緯度ごとに球面余弦定理で経度半幅 `dlon` を計算
- `cos(rho) = sin(lat0)*sin(lat) + cos(lat0)*cos(lat)*cos(dlon)` を解く
- `is_full_width[i]` フラグ: `dlon ≥ 180°` の行は極域を包む全経度帯 → `xl=0, xr=w` を直接設定
- Antimeridian（日付変更線）越え: `xl > xr` の行は左端・右端の2つの `QRectF` に分割
- fill: `rgba(100,200,255,140)`、outline: シアン `#00DCFF` 1.5px

**アウトラインスキップ規則（重要）**:
- `is_full_width[i] or is_full_width[i+1]` でスキップ（どちらか一方でも全幅行なら除外）
- `xl=0` / `xr=w` という人工座標が通常行の実座標と結ばれて横線になるのを防ぐ
- `and` 条件（両端とも全幅行のみスキップ）は横線を発生させるため使用禁止
- 水平幅 `abs(x2 - x1) < w/3` のセグメントのみ描画（日付変更線越えの大ジャンプを除外）
- スキップにより極境界の弧は閉じない（開いて見える）が、1.5px の細線で目立たなくする妥協策を採用（beta.32）
- 極境界を完全に閉じる根本修正は未解決。遷移点の通常行座標で水平閉じ線を引く方式を試みたが、遷移行の xl≈0/xr≈w により閉じ線自体も横線になる副作用があり断念

**ズームモードの座標整合（重要）**:
- `latlon_to_xy` は地図画像描画と同じクランプ済みlatレンジを使用する
- 地図描画: `lat_max = min(90, clat+span)` でクランプ → 実際のスパンが `2*span` より小さくなる
- オーバーレイ（衛星ドット・フットプリント）も同じ計算を使わないと、極地域で南方向にずれて見える
- `rendered_lat_span = min(90,clat+span) - max(-90,clat-span)` で y を正規化

**衛星ドットクリック**:
- `mousePressEvent` で衛星ドット中心12px以内のクリックを検出し `sat_clicked(int)` を emit
- `main_window.py` で `_world_map.sat_clicked.connect(self._select_satellite_by_norad)` に接続済み

#### デフォルト世界地図（NASA Topographic 1024px）

- `settings_dialog.get_world_map_path()`: 明示的な選択がない場合、`nasa-topo_1024.jpg` が存在すればそのパスを返す
- `main_window._apply_world_map()`: 初回起動時（ファイル未存在）はバックグラウンドスレッドで GPredict リポジトリから自動ダウンロード。完了後 `QMetaObject.invokeMethod` で再適用

### Group Pass Chart（src/ui/pass_chart.py — GroupPassChartView）

- Group タブで検索実行後に自動表示（それまでタブ非表示）
- 衛星ごとに12色パレットから自動割り当て（>12衛星は循環）
- 凡例は非表示。マウスホバーでツールチップ（衛星名＋最大仰角）
- Range選択: 4h / 8h / 12h / 24h（Target Pass Chartと同じ）
- UTC/Local 切り替えに連動

### Autotrack 設計（src/core/autotrack.py）

```sql
CREATE TABLE autotrack_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE autotrack_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id       INTEGER NOT NULL REFERENCES autotrack_lists(id) ON DELETE CASCADE,
    norad_cat_id  INTEGER NOT NULL,
    xpdr_uuid     TEXT NOT NULL,   -- 使用するトランスポンダーのUUID
    sort_order    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT ''
);
```

#### 切り替えロジック（AutotrackManager.check()）
1. 現在衛星が Min El 以上 → 継続追尾
2. 現在衛星が Min El 以下:
   a. 別の衛星がすでに可視 → 即座に切り替え（リスト順タイブレーク）
   b. 可視衛星なし → AOS が最も近い衛星に切り替え（リスト順タイブレーク）
3. パス途中は切り替えしない（LOS を待つ）

#### 使用前提条件
1. **Autotrack/Record メニュー** → Autotrack Lists 枠でリスト作成・衛星登録
2. Upcoming Passes > Group タブでパス検索実施
3. Autotrack/Record ダイアログで Autotrack Control 枠のリストを選択 → Enable Autotrack をオン
4. （任意）Autotrack Timer で開始・停止時刻を設定
5. （任意）Recording 枠で Audio / IQ 録音を有効化

### スマホブラウザ Web UI（src/web/static/）

#### タブ構成
| タブ | 内容 |
|---|---|
| **Tracking** | 衛星リスト・EL/AZ・Range・レーダー |
| **Antenna** | AZ/EL 大表示・周波数・トランスポンダー選択・RIG接続 |
| **Pass Prediction** | パス予測一覧 |
| **Group Pass** | グループ検索・パス一覧 |

#### Antenna タブ（手動アンテナ追尾用途に特化）

想定ユースケース: スマホで AZ/EL を見ながら八木アンテナを手動で向け、PCでドップラー補正する運用

| 機能 | 詳細 |
|---|---|
| **AZ/EL 大表示** | 42px の大きな数字でリアルタイム表示 |
| **パス進行バー** | AOS〜LOS の進行状況（緑バー）+ LOS カウントダウン |
| **周波数（読み取り専用）** | Doppler 補正済み DL/UL 周波数とシフト量を表示 |
| **トランスポンダーカードリスト** | 衛星選択時に自動取得・カード形式で表示・タップで選択 |
| **Connect/Disconnect RIG** | スマホからリグ接続をトリガー（設定はPC側で事前設定が必要） |
| **RIG ON/OFF ボタン** | 接続済みリグの Doppler 補正 ON/OFF |
| **ROT ON/OFF ボタン** | ローテーター接続時のみ表示 |

#### コンパス連動（レーダー North-Up / Compass 切り替え）

- レーダー画面右上の「N↑ North Up」ボタンで切り替え
- **Android**: HTTP でも動作（即時切り替え）
- **iOS 16+**: HTTP では `DeviceOrientationEvent` が無効（Apple のセキュリティ制限）。HTTPS が必要
- **iOS 13–15**: 許可ダイアログ後に使用可能

#### RIG 遠隔制御アーキテクチャ（src/web/rig_state.py）

```python
# RigWebState — Qt UI スレッド（書き込み）と FastAPI スレッド（読み込み）の共有状態
rig_connected: bool      # Rig 1 接続状態
rig_engaged: bool        # Doppler 補正動作中
dl_hz / ul_hz: float    # 補正済み周波数
rig_connect_requested    # POST /api/rig/connect でセット → Qt が処理して接続
rig_disconnect_requested # POST /api/rig/disconnect でセット → Qt が処理して切断
```

**WebSocket ペイロード拡張**（`/ws/tracking` レスポンスに追加）:
```json
{
  "rig": { "connected": true, "engaged": true, "dl_hz": 435611234, "mode": "SSB" },
  "rot": { "connected": false }
}
```

**REST エンドポイント**:
- `POST /api/rig/connect` `{norad, xpdr_uuid}` — 衛星・トランスポンダー選択＋接続
- `POST /api/rig/disconnect` — 切断
- `POST /api/rig/toggle` — Doppler ON/OFF トグル
- `POST /api/rot/toggle` — ローテーター ON/OFF トグル

---
