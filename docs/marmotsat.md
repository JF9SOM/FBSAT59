# MARMOTSat 対応の現状と方針

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> `src/comms/ax100digi/**`・`src/ui/ax100_digi_tab.py`、および将来の MARMOTSat DVB-S2
> 受信機能に触れる前に読むこと。常時読み込む必要はない。
>
> MARMOTSat は「AX100 VHF デジピーター（実装済みだが CSP ヘッダー未確定）」と
> 「HF DVB-S2 画像ビーコン（未実装・保留中）」の2機能にまたがり、いずれも**外部の
> 一次情報待ち**で止まっている。関連する散在情報（`docs/communications.md` の AX100
> セクション、`docs/roadmap.md` 11g、`docs/tle.md` の仮ID→実ID移行）への入口も兼ねる。

---

## 1. 衛星同定

MARMOTSat はカナダ・University of Victoria Centre for Aerospace Research（CfAR /
Propagation Lab）の 3U CubeSat。2026-07-07 に Falcon 9 Transporter で Vandenberg から
打ち上げ。2026-07 末に in-orbit commissioning 完了、VHF CW ビーコン運用中
（ビーコン文 `CQ DE VA7UVS CO-128 QRO 2W ANT DIPOLE WWW.MARMOTSAT.CA`）。
オープンソースの GNU Radio 互換 SDR「MCR（Modular CubeSat Radio、Hermes Lite 2 ベース）」
を搭載。

### NORAD ID は2つある（2026-09-06 時点の DB 実態）

| NORAD | `satellites.is_hidden` | 位置づけ |
|---|---|---|
| **69912** | 0（表示） | 実 NORAD ID。SATNOGS の全トランスミッターはこちら |
| **98272** | 2（システム非表示） | 打ち上げ時の仮 ID（90000 番台）。仮ID→実ID移行パイプラインで hidden 化済み（[docs/tle.md](tle.md) 参照） |

`is_ax100_digi_transmitter()`（`src/comms/mode_detection.py`）は
`_MARMOTSAT_NORAD_IDS = frozenset({69912, 98272})` で**両IDを許容**する（2026-09-06 変更、
経緯は [docs/communications.md](communications.md) の「仮ID 98272 → 実ID 69912 への追随」参照）。

### SATNOGS トランスミッター（NORAD 69912）

| description | mode | 帯 | 備考 |
|---|---|---|---|
| Mode V/V Digipeater | `FSK AX.100 Mode 5`（baud 1200） | 145.875 MHz | 本命。AX100 Digi タブの対象 |
| Mode U - Transmitter | FSK | UHF | |
| VHF CW TLM | CW | 145.875 MHz | CW テレメトリー |
| HF CW Telemetry Beacon | CW | 29.410 MHz | |
| HF Ionospheric LFM Sounder | FM | 29.410 MHz | トランスイオノスフェリック測深 |
| HF DVB-S2 | DVB-S2（baud 33000） | 29.410 MHz | §3 の保留機能 |

### コミュニティトランスミッター override

`src/data/community_transmitters.json` の `community-marmotsat-digi`（uuid で追随）：
`norad_cat_id=69912`・`type=Transceiver`・`mode="USB-D"`・`invert=false`・
`uplink_low=downlink_low=145875000`。SATNOGS の `FSK AX.100 Mode 5` エントリを
「SSB パスバンド内に固定オフセットで GMSK を乗せる」実運用（GreenCube と同じ方式）に
合わせて上書きする。`pick_preferred_transponder_index()` が `source='community'` を
常に優先するため、Radio Control のトランスポンダー自動選択ではこちらが選ばれる。

---

## 2. AX100 VHF デジピーター（145.875 MHz）

### 2.1 FBSAT59 側の実装（2026-07・実装済み）

- `src/comms/ax100digi/` + `src/ui/ax100_digi_tab.py`。詳細設計は
  [docs/communications.md](communications.md)「AX100 Digi 機能設計」。
- プロトコルスタック：GreenCube（IO-117）と同一の AX100 "ASM+Golay" GMSK
  （ASM `C9D08A7B` MSB → Golay(24,12) 長さ → G3RUH スクランブラ → CSP パケット →
  CRC32 → Reed-Solomon(223,255)）。Rig+サウンドカード（SSB モード）・SDR 両対応。
- `is_ax100_digi_transmitter()`：NORAD ∈ {69912, 98272} かつ description に
  "MODE V" または "DIGIPEATER" を含む。GreenCube 自体（53106）は運用終了のため
  NORAD で意図的に除外。
- トランスポンダー選択による自動タブオープンは**未配線**（メニューから開く）。
  `is_ax100_digi_transmitter()` は Comms Quick Panel のフィルタにだけ使われ、
  APRS/FT4/SSTV のような `*_transponder_selected` シグナルは emit しない。

### 2.2 CSP ヘッダーが未確定という制約（最重要）

`src/comms/ax100digi/tx.py` の

```python
DEFAULT_CSP_HEADER = CspHeader(priority=1, source=1, destination=5, dest_port=10, source_port=20)
```

は**実機・実地上局に対して未検証のプレースホルダー**。GreenCube Digipeater Manual v1.1
（S5Lab）はアプリケーション層のメッセージ本文の書式は精密に文書化しているが、
Terminal ソフトが組み立てる **CSP アドレッシング（source/destination ノード、ポート、
priority）は非公開**。このため `_CspSettingsDialog`（`src/ui/ax100_digi_tab.py`）で
ユーザーが値を編集できるようにしてある。**この「編集可能のまま／デフォルトは触らない」
方針は 2026-09 の再調査でも妥当と再確認された。**

### 2.3 調査履歴（時系列）

**2026-07（AX100 Digi 実装時〜2026-07-25）**
- AX100 "ASM+Golay" を使う他衛星は Sapienza S5Lab の GreenCube と LEDSAT のみ。
  **両方とも死亡**（GreenCube は ~2024-09 に電源系／放射線損傷で沈黙、LEDSAT は
  それより前に失敗）。→ 実際に中継パケットを受信して CSP ヘッダーを読む
  「RX リファレンス」経路が閉ざされている。
- GreenCube Terminal（OZ9AAR）はクローズドソースの Windows .exe（moonbounce.dk 配布）。
- S5Lab のテレメトリー資料：CSP **destination port 8**（LEDSAT も port 8、画像は
  port 11）。フリート内の慣習の手がかりだが、**デジピーターのサービスポートは未記載**。
- gr-satellites に IO-117 / GreenCube / MARMOTSat の SatYAML は無い
  （Daniel Estévez は GreenCube を追加していない）。

**2026-09-06（本ドキュメント作成時の再調査）**
- **MARMOTSat のデジピーターはまだ有効化されていない**。
  [propagationlab.ca/satellite](https://www.propagationlab.ca/satellite/)：
  "digipeater is **not currently enabled** … Once the digipeater is enabled,
  more detailed instructions will be posted here." → 捕捉対象がまだ存在しない。
- **UVic 公式が「GreenCube と同一運用」と明言**："designed to operate the same way
  as the well-known IO-117 GreenCube" / "equipment requirements for Amateurs and
  the usage are the same as for GreenCube"。→ GreenCube のアドレッシングが狙うべき値。
- **GitLab が移転**：`gitlab.orcasat.ca` → **`gitlab.uvic-cfar.com`**
  （旧ドメインは別サイト reysun.com の証明書を返す）。`open-source-projects`
  グループの公開リポジトリは3つのみ：
  - `mcr`（Modular CubeSat Radio、**ハードウェアのみ**）
  - `dvb-s2-decoder`（GNU Radio、最終更新 2025-04-24）
  - `hf-turnstile-antenna`
  → **フライトソフト／OBC／デジピーター／CSP のコードは非公開**。オープンソースから
    CSP ヘッダーは判明しない。
- S5Lab に `GreenCube_Digipeater.zip`（マニュアル＋クローズド GUI＋ツール類）がある。
- ユーザーが MARMOTSat / UVic チーム（カナダ）へメールで問い合わせ済み。
  2026-09-06 時点で返信なし。

### 2.4 デジピーターが有効化されたときの確定手順

1. [propagationlab.ca/satellite](https://www.propagationlab.ca/satellite/) と
   [marmotsat.ca/updates](https://www.marmotsat.ca/updates) で有効化アナウンスと
   使用手順の公開を確認する。
2. **FBSAT59 の AX100 Digi RX パイプラインで中継パケットを実際に受信し、
   デコード結果の CSP ヘッダー（source/destination ノード、ポート、priority）を読む。**
   2026-07 と違い、今回は MARMOTSat 自身がリファレンスになる（GreenCube/LEDSAT が
   両方死んでいて参照先が無かった状況が解消される）。
3. 補助手段：S5Lab の `GreenCube_Digipeater.zip` の GreenCube Terminal を KISS
   ループバックに向け、送信時に吐き出す CSP ヘッダーを観測する（MARMOTSat が
   「同じ」と言っている de-facto 標準）。
4. 確定したら `tx.py` の `DEFAULT_CSP_HEADER` を更新する。ただし
   `_CspSettingsDialog` の編集可能性は残す（機体差・将来の仕様変更に備える）。

---

## 3. HF DVB-S2 画像ビーコン（29.410 MHz）— 実装保留中

**2026-07-24、ユーザーの明示的判断で保留。** 一次情報が入手できるまで再開しない。
（`docs/roadmap.md` 11g と同一。詳細はそちらではなく本節を正とする。）

### 公表済みスペック（UVic Propagation Lab）

QPSK・roll-off 0.35・**33 または 66 kbaud**・FEC 1/2・ACM 未使用（CCM 固定 MODCOD）・
QO-100 DATV 運用慣行準拠。

### 未確定点・障害

- **パイロットシンボル ON/OFF が不明**。最有力の受信実装 `gr-dvbs2rx`（GNU Radio OOT）は
  パイロット ON 時のみ安定動作し、パイロットレス対応は上流でも未完成。実 IQ か
  flowgraph が無いと確認できず、この点を賭けにして実装を進めるのは危険。
- **受信実績ゼロ**：SatNOGS DB に MARMOTSat の観測・デコード実績なし、登録デコーダーなし。
  Libre Space Community も挑戦意欲の表明のみ。
- GNU Radio flowgraph：`gitlab.uvic-cfar.com/open-source-projects/dvb-s2-decoder`
  （description "GNU Radio algorithms to decode DVB-S2 transmissions from MARMOTSat"、
  最終更新 2025-04-24）。**2026-07-24 は `gitlab.orcasat.ca` が接続不可だったが、
  移転先ドメインでは到達可能になっている**（本ドキュメント作成時に確認）。
  → 再開する場合はまずこのリポジトリの中身（パイロット設定・MODCOD・レート）を確認する。

### 再開時の想定実装方針（未承認）

GNU Radio をアプリに組み込まず、既存の SatDump / gr-satellites 連携と同じ
「外部ツールをサブプロセス起動して IQ を渡す」パターンで、`gr-dvbs2rx` の
`dvbs2-rx` CLI または `leandvb` をラップする。

---

## 4. 監視先・再開トリガー

| URL | 何を見るか |
|---|---|
| [propagationlab.ca/satellite](https://www.propagationlab.ca/satellite/) | デジピーター有効化アナウンス＋使用手順。DVB-S2 スペックの更新 |
| [marmotsat.ca/updates](https://www.marmotsat.ca/updates) | ミッション更新（2026-08-14「Commissioning & VHF CW Beacon」が最新） |
| [gitlab.uvic-cfar.com/open-source-projects](https://gitlab.uvic-cfar.com/open-source-projects) | `dvb-s2-decoder` の中身。デジピーター／フライトソフトのリポジトリが新規公開されないか |
| [SatNOGS DB - MARMOTSat](https://db.satnogs.org/satellite/HPYK-4830-8894-4849-3491/) | 登録デコーダー・デコード実績（デジピーター／DVB-S2 とも現在ゼロ） |

**AX100 デジピーター再開の条件**：デジピーターが有効化される（→ §2.4 の手順で CSP
ヘッダーを実測）。または UVic / S5Lab から CSP アドレッシングの直接回答が得られる。

**DVB-S2 再開の条件**：(1) `dvb-s2-decoder` リポジトリでパイロット設定・MODCOD が
判明する、(2) 実 IQ キャプチャが入手できる、(3) SatNOGS / Libre Space Community に
デコード成功報告が出る、のいずれか。
