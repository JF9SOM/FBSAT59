# Moon/EME 追尾設計

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## Moon/EME 追尾設計（2026-06-21 確定）

### 概要

月（Moon）を擬似衛星として扱い、EME（地球-月-地球）反射通信のための追尾・ドップラー補正を実現する。

### センチネル値

```python
MOON_ID: int = -1  # src/core/celestial_engine.py
```

NORAD IDの代わりに `-1` を使用。UI全体でMOON_IDを衛星と同列に扱う。

### JPL DE421 エフェメリス

- ファイル: `de421.bsp`（約17 MB、1900〜2053年有効）
- 初回起動時に自動ダウンロード・キャッシュ
- 保存先: `platformdirs.user_data_dir / "ephemeris" / de421.bsp`
- `CelestialEngine.load()` で遅延ロード（バックグラウンドスレッド推奨）

### CelestialEngine（src/core/celestial_engine.py）

| メソッド | 説明 |
|---|---|
| `load() -> bool` | DE421をロード（初回のみダウンロード） |
| `observe_moon(lat, lon, elev_m, at)` | 指定時刻の月の Observation を返す |
| `moon_subpoint(at)` | 月直下点（lat, lon）を返す |
| `moon_track(lat, lon, elev_m, hours, step_minutes)` | 指定時間のAZ/ELトラック（レーダー弧用） |
| `moon_events(lat, lon, elev_m, start, end)` | Moonrise/transit/Moonset を PassInfo として返す |

### "Celestial Bodies" フィルターグループ

- 衛星フィルタードロップダウンに `"Celestial Bodies"` グループを追加
- 選択時: 衛星リストは `[(MOON_ID, "Moon")]` のみ表示。TLEリスト・世界地図の衛星表示はクリア
- 月のサブルーナルポイント（直下点）を世界地図にドットで表示

### レーダー表示

- `RadarView` が MOON_ID の `SatTrackData` を受け取ると24時間の月軌道弧を描画
- フットプリントは表示しない（月にフットプリントは不要）
- Dashboard でも同様（`update_observation()` 内で `MOON_ID` 時は `draw_footprint()` をスキップ）

### Pass Chart（Moonrise/Moonset/Transit）

- `moon_events()` が Skyfield almanac で Moonrise/Moonset を検出
- 1窓（Moonrise→Moonset）を1つの `PassInfo` として表現（aos=Moonrise, tca=Transit, los=Moonset）
- Target/Group タブ・Pass Chart・Group Pass Chart 全て MOON_ID ブランチで対応
- 検索範囲を前後1日拡張して「すでに月出中」のケースも捕捉

### EME ドップラー補正（往復×2）

衛星は地球→衛星の片道ドップラーを適用するが、EMEは地球→月→地球の往復のため係数を2倍にする。

```python
rr = obs.range_rate_km_s * (2.0 if self._selected_norad == MOON_ID else 1.0)
```

適用箇所:
- `_update_rig_web_state()` — WebSocket状態
- `_update_selected_satellite()` — 衛星選択時の定期更新（MOON_IDでは呼ばれない）
- `_update_moon()` — 月選択時の定期更新（1 Hz）

### EME 周波数（src/data/eme_frequencies.json）

`transmitter_manager.get_transmitters(MOON_ID)` が呼ばれると `eme_frequencies.json` から読み込む。

| バンド | CW 周波数 | Digital 周波数 | モード |
|---|---|---|---|
| 50 MHz | 50.190 MHz | 50.200 MHz | CW / SSB(Q65/JT65) |
| 144 MHz | 144.000 MHz | 144.100 MHz | CW / SSB(Q65/JT65B) |
| 432 MHz | 432.000 MHz | 432.065 MHz | CW / SSB(Q65/JT65C) |
| 1296 MHz | 1296.000 MHz | 1296.065 MHz | CW / SSB(Q65/JT65D) |
| 2320 MHz | 2320.000 MHz | 2320.065 MHz | CW / SSB |
| 5760 MHz | 5760.000 MHz | 5760.065 MHz | CW / SSB |
| 10368 MHz | 10368.000 MHz | 10368.065 MHz | CW / SSB |

- `source='eme'`（SATNOGS同期で上書きされない）
- DL == UL（シンプレックス。リグはsame-bandロジックで制御）

### リグ制御（EME時）

- EMEはDL=ULのsame-band運用 → 衛星same-bandロジック（VFOA=RX, VFOB=TX / 通常split）がそのまま適用される
- `_update_moon()` 内で `_rig_busy_lock` + バックグラウンドスレッドで `set_vfo_frequencies(dl, ul)` を呼び出す（衛星の `_update_selected_satellite()` と同一パターン）
- Rig 1 / Rig 2 両対応

---
