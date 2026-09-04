# 次回の作業候補

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## 次回の作業候補（v0.1.0 以降）

### 継続中・優先度高
0. ~~**RTL-SDR WinUSB Connect 失敗修正**~~ **→ v0.1.71 で解決済み（2026-06-25）**
1. **ドップラー補正の実動作確認** — 各種リグ（TS-2000・FT-817ND 等）での実衛星通信テスト（FTX-1F・FT-991AM・IC-9100・IC-9700・RTL-SDR/HackRF は確認済み）
1b. **AX.25 9600bps G3RUH のフィルタチューニング（実衛星パス待ち、2026-07-10 追加）** — SDR単体経由（`comms/aprs/g3ruh_demod.py`の`G3ruhDiscriminator`）・Rig+サウンドカード経由（Direwolf `MODEM 9600`）双方とも実装済みだが、実際の9600bps G3RUHデジピータ衛星での受信検証を一度も行っていない。特に`G3ruhDiscriminator`のIF帯域幅（`_IF_HALF_BW_HZ`）・偏移定数（`_DEVIATION_HZ`）はNFM音声復調のパラメータを流用した初回実装値であり、実信号でのチューニングが必要になる可能性が高い。詳細は「AX.25 9600bps G3RUH 対応」セクション参照。対象衛星のパスが来た時点で着手する
1c. **Lock（Lボタン）ツールチップに反映遅延の説明を追加（急ぎではない、GitHub Issue #19、2026-08-10 予告）** — IC-9700（satmode・USB接続）ユーザーから、手動リチューンがLock機能に反映されるまで体感5秒程度かかるという報告があり、既知の設計上のラグ（`_doppler_cycle()`は約1秒周期でリグを1回だけポーリング→次周期で補正値へ折り込み→さらに次の`_on_tick()`で画面表示に反映、という3段階構成のため最悪ケースで約3秒・平均1.5〜2秒。詳細は「Lock（Lボタン）— dial feedback設計」セクションの「既知の制約」参照）に一致することを確認済み。Issueへの返信では「近日中にツールチップへ追記する」と予告済みのため、[radio_control_widget.py](src/ui/radio_control_widget.py)の`_lock_btn.setToolTip()`（現状「DL is yours to tune manually; UL follows (inverting transponder aware).」のみ）に、この遅延（数秒程度、CATのラウンドトリップに起因）に関する一文を追記すること。あわせて、satmode機（IC-9100/9700/910H/821H）ではLock中はUL側もソフトウェアが一切書き込まず、リグ自身のハードウェアSub自動追従に委ねる設計（DLのみ読み取り）であることも、ツールチップまたは近傍のUIテキストで触れるかどうか検討する（現状のツールチップは非satmode機の挙動のみを説明しており、satmode機ユーザーには誤解を招きうる）
2. **ローテーター設定ダイアログの改善** — 接続テストボタン・AZ/ELリミット設定
3. **デバッグ用ログファイル出力の削除または設定化** — `src/main.py` の `_setup_logging()` にある frozen バンドル向けファイルログ出力（`platformdirs.user_log_dir`）は dmg デバッグ目的で追加したもの。Settings に「デバッグログを保存する」チェック（デフォルトOFF）を追加するか削除する。該当箇所: `src/main.py` 63〜75行目
4. ~~**Autotrack/Record メニューの実装**~~ **→ v0.1.0 以降で完了**（AutotrackRecordDialog・Autotrack Timer・AOS/LOS 自動接続・録音自動制御）
1e. ~~**Tuneボタンがバンド中心ではなく`downlink_low`（下限）に固定されている疑い**~~ **→ 2026-08-13 実装完了** — `_refresh_radio_control()`の生SQLに`downlink_high`・`uplink_high`を追加。原因は列の見落としで確定していた（`_current_transmitter`にキー自体が存在せず`_on_tune_requested()`が常にバンド下限側へフォールバックしていた）

### モバイル・Web UI
5. **スマホ・タブレット画面の継続確認** — Android 実機でのコンパス連動確認、各種ブラウザでの表示確認

### SDR・デジタルモード
6. ~~**SDR機能の追加（フェーズ1: 初期実装）**~~ **→ v0.1.0 で完了**
7. ~~**APRS 受信・送信・位置ビーコン実装**~~ **→ feature/communications（v0.2.0）で完了**（APRSEngine・Direwolf統合・Bell 202 AFSK復調・PTT CAT制御・Doppler凍結・地図ピン表示）。**AX.25 9600bps G3RUH対応は2026-07-10で追加完了**（Rig+サウンドカード・SDR単体経由の両方、Auto/1200/9600プルダウン。詳細は「AX.25 9600bps G3RUH 対応」セクション参照。フィルタチューニングは1bで実衛星パス待ち）
8. ~~**Telemetry タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（AX.25受信・JSON定義デコード・12衛星フォーマット定義）
8b. ~~**Telemetry タブ gr-satellites 統合**~~ **→ 2026-06-30 で完了**（gr-satellites サブプロセス・UDP IQ 転送・330機以上対応・衛星コンボ・SDR 自動接続・トランスポンダー自動選択・メインリスト連動）
9. **テレメトリーフォーマット定義の追加・検証** — 実際に受信したパケットでオフセット・スケールの検証。未定義衛星のフォーマット調査
9b. ~~**SatNOGS DB テレメトリー投稿 Phase 2（gr-satellites 経路）**~~ **→ 2026-09-04 で完了**（`--kiss_server` サイドチャネル + `_KissFrameReader` + `_on_gr_raw_frame` 配線・gr モードでの SatNOGS ボタン非表示 revert。詳細は「SatNOGS DB へのテレメトリー自動アップロード」セクション参照。**実信号での完全 E2E（録音 IQ + 実 gr_satellites）のみ手動検証待ち**）。あわせて任意項目として `telemetry_log.satnogs_submitted` 列（投稿ステータス記録）も同セクション末尾に記載（引き続き未着手）
10. ~~**CI: Direwolf バンドルビルド**~~ **→ feature/communications で完了**（Linux/Windows/macOS 3ジョブ、タグ push 時に direwolf-{platform}-{arch}.{tar.gz|zip} を Releases にアップロード）
11. ~~**FT4 タブ実装**~~ **→ feature/communications（v0.2.0）で完了**（Ft4Codec/ctypes + ft8_lib・Ft4Scheduler・Ft4QsoManager・Ft4Tab UI・ADIF エクスポート。ft8_lib CI バンドルビルドは v0.2.0 タグ時に Direwolf と同時実施）
11c. ~~**Q65 Phase 1（RX）実装**~~ **→ 2026-06-26 で完了**（Q65Codec/libq65 ctypes・build-q65lib.yml CI・Help > Q65 Library Installation ダイアログ）
11d. ~~**Q65 Phase 2（TX/QSO）実装**~~ **→ 2026-06-26 で完了**（純 Python encoder.py: GF(64)・CRC-12・65-FSK / Q65QsoManager: QSOステートマシン・q65_log DB・ADIF / q65_tab.py: TX UI・TX Enable・Halt TX・Log QSO・Export ADIF）
11e. ~~**METEOR / HRPT 受信タブ実装**~~ **→ 2026-06-29 で完了**（SatDump サブプロセス管理・8衛星対応・Autotrack AOS/LOS 連携・SDR Connect・浮動ログウィンドウ・衛星検索ダイアログ）
11h. ~~**METEORタブにウォーターフォール表示ボタンを追加**~~ **→ 2026-08-20 で完了・実機確認済み**（受信画像プレビュー欄をImage/Waterfallの2タブに分割。`satdump live`に`--fft_enable --fft_size N --fft_rate N --http_server 127.0.0.1:PORT`を追加しSatDump自身のHTTP API `fft_values`をポーリング。実機（METEOR-M N2-3、最大仰角約50°）でStart時のWaterfall自動表示・完了時のImage自動表示・電波受信の視覚的確認とも動作確認済み。詳細は「METEORタブのライブWaterfall表示」セクション参照）
11f. ~~**CW Decoder タブ実装**~~ **→ 2026-06-30 で完了（v0.2.6）**（deepcw-engine ONNX / onnxruntime 自動 pip インストール / model.onnx 自動ダウンロード / CW-R トランスポンダー自動オープン）
11g. **MARMOTSat DVB-S2 受信タブ実装（保留中、2026-07-24 追加）** — AX100 Digi実装（前述「AX100 Digi 機能設計」参照）に続き、MARMOTSatのHF DVB-S2画像ビーコン（29.410 MHz）受信を追加検討したが、**一次情報が入手できず保留**とした:
    - 公表済みスペック（UVic Propagation Lab）: QPSK・roll-off 0.35・33 or 66 kbaud・FEC 1/2・ACM未使用（CCM固定MODCOD）・QO-100 DATV運用慣行準拠
    - MARMOTSat公式サイトが案内するGNU Radio flowgraphのリンク先 `gitlab.orcasat.ca`（UVic CfARの自前GitLab、`/open-source-projects/dvb-s2-decoder` および `/open-source-projects/mcr`）は**ドメイン全体が接続不可**（`connect ECONNREFUSED`、Claude側サンドボックスだけでなくユーザー自身のネットワークからも確認済み。2026-07-24時点）
    - SatNOGSデータベース上でもMARMOTSatの観測・デコード実績は過去30日間ゼロ、登録済みデコーダーも無し。Libre Space Communityフォーラムにも「受信に挑戦したい」という意欲表明のみで成功報告なし（打ち上げ2026-07-07から17日経過時点）
    - 実装上の最大の未確定点: **パイロットシンボルON/OFF**。最有力候補の受信実装 `gr-dvbs2rx`（GNU Radio OOTモジュール）はパイロットON時のみ安定動作し、パイロットレス対応は上流でも未完成なため、この点の確認なしに実装を進めると動くかどうか賭けになる
    - 再開の判断材料（次回セッションでまず確認すること）: (1) `gitlab.orcasat.ca` が復旧したか (2) SatNOGS/Libre Space Communityに新しい受信報告が出たか (3) ユーザーが実IQキャプチャを入手またはMARMOTSat/UVicチームに直接連絡が取れたか
    - 再開時の想定実装方針（未承認）: GNU Radioをアプリに組み込まず、既存のSatDump/gr-satellites連携と同じ「外部ツールをサブプロセス起動してIQを渡す」パターンで `gr-dvbs2rx` の `dvbs2-rx` CLI または `leandvb` をラップする
11b. **SDR フェーズ2（将来）— アマチュア衛星・デジタルモード** — HRPT/LRPT は 11e で完了、gr-satellites は 8b で完了、AI-CW は 11f で完了
12. **SDR フェーズ2（将来）— 業務用衛星受信** — Inmarsat-C (STD-C)・Cospas-Sarsat L帯・Iridium L帯 ACARS・Orbcomm・みちびき（QZSS）データ放送（詳細は「業務用衛星受信」セクション参照）
13. ~~**SDR Device Installation ダイアログ**~~ **→ v0.1.0 で実装済み**（src/ui/sdr_install_dialog.py — USB VID/PID スキャン・apt/brew/Zadig 誘導）
14. ~~**Help > gr-satellites… ダイアログ**~~ **→ feature/communications で完了**（src/ui/gr_satellites_dialog.py — 検出ステータス・バンドルのDownload & Install・conda-forge/PPA/ソースビルド案内。2026-07-31にpip案内の誤りとmacOS向けバンドル配布を追加、詳細は「gr-satellitesのバンドル配布」セクション参照）
15. ~~**SSTV / SSDV 受信タブ**~~ **→ feature/communications で完了**（SstvDecoder・SsdvDecoder・SstvTab・SDR audio_ready 接続・AX.25 raw_frame_received タップ）

### データ・連携
16. **観測ログ機能** — 実際に追尾・通信した衛星パスを記録・集計・エクスポートする機能
17. ~~**多言語対応（日本語）フェーズ2 — メニュー・Help画面・主要タブ/ダイアログ**~~ **→ 2026-07-08 で完了**（View > Language > Japanese 実装済み。詳細は「多言語化ロードマップ」セクション参照。Web UI・タブ内部の一部は引き続き未着手）

### ハードウェア連携
18. **追加リグの実機テスト** — TS-2000・FT-817ND 等でのドップラー制御動作確認（IC-9100・IC-9700 は v0.1.27 で確認済み）
19. **WSJT-X / JS8Call 連携** — デジタルモード運用ソフトとの周波数・モード連動（将来）

---
