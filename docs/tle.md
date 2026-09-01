# TLE 取り込み・衛星/トランスミッター同期 詳細

> このファイルは [CLAUDE.md](../CLAUDE.md) から分離した詳細ドキュメントです。
> 関連する機能の実装・不具合調査を行う前に必ず読んでください。常時読み込む必要はありません。

---

## TLE 取り込みルール全体設計（2026-05-29 確定）

### TLE ソース一覧と優先度

| 関数 | ソース | 対象 NORAD 範囲 | 更新頻度 | source 値 | tle_group 値 |
|---|---|---|---|---|---|
| `fetch_and_update('celestrak-stations')` | CelesTrak STATIONS | ISS・CSS 等 | 1時間ごと | `celestrak` | `stations` |
| `fetch_and_update('celestrak-amateur')` | CelesTrak AMATEUR | アマチュア衛星 | 2時間ごと | `celestrak` | `amateur` |
| `fetch_and_update('celestrak-cubesat')` | CelesTrak CUBESAT | CubeSat | 4時間ごと | `celestrak` | `cubesat` |
| `fetch_and_update('celestrak-weather')` | CelesTrak WEATHER | 気象衛星 | 6時間ごと | `celestrak` | `weather` |
| `fetch_and_update('celestrak-earth-obs')` | CelesTrak RESOURCE | 地球観測 | 12時間ごと | `celestrak` | `earth-obs` |
| `fetch_and_update('celestrak-science')` | CelesTrak SCIENCE | 科学衛星 | 12時間ごと | `celestrak` | `science` |
| `fetch_active_tles()` | CelesTrak(複数グループ)+SATNOGS TLE API | 10000-89999・未収録 | 24時間ごと(起動時stale確認) | `celestrak` or `satnogs` | `amateur`(INSERT時) / 既存保持(UPDATE時) |
| `fetch_provisional_tles()` | SATNOGS TLE API | NORAD ≥ 90000 | 12時間ごと | `satnogs` | `amateur` |
| `fetch_legacy_tles()` | CelesTrak 個別照会 | NORAD < 10000 | 起動時1回のみ | `celestrak` | `legacy` |
| `add_manual_tle()` | ユーザー手動入力 | 任意 | 手動 | `manual` | `amateur` |

### 上書きルール（優先度）

```
manual（最高優先）> celestrak > satnogs > なし
```

- `source='manual'` の TLE は **いかなる自動同期でも上書きしない**
- 既存 TLE が `celestrak` の場合、`satnogs` ソースの取得結果で上書きしない
  （`fetch_provisional_tles()` は `INSERT OR REPLACE` だが `source='manual'` チェックで防御）
- `fetch_active_tles()` の UPDATE では `tle_group` を保持（分類を劣化させない）
- **起動時の未フェッチ/期限切れソース自動検出**: `TLEManager.is_source_stale(source_name)` が `sync_log` 未記録、または各ソース自身の `update_interval_hours` より古いソースを `True` で返す（2026-08-11 修正、旧実装は未記録のみ検出）→ MainWindow が起動時に対象グループを即時フェッチ
- **フェッチ順序制御**: `MainWindow._sort_sources_by_priority()` が `TLE_SOURCES["priority"]` 昇順でソート。`amateur`（汎用）を先にフェッチし、`cubesat`/`weather` 等がその後に上書きするよう保証

### tle_group と UI フィルタの対応

| tle_group 値 | UI フィルタ | 用途 |
|---|---|---|
| `amateur` | Amateur | アマチュア衛星全般（SATNOGS 登録衛星のデフォルト） |
| `cubesat` | CubeSat | CelesTrak CUBESAT グループ由来 |
| `weather` | Weather | 気象衛星 |
| `earth-obs` | Earth Observation | 地球観測衛星 |
| `science` | Science | 科学衛星 |
| `stations` | Space Stations | ISS・CSS 等 |
| `legacy` | Amateur | NORAD < 10000 の古い衛星（COALESCE で Amateur 扱い） |
| `NULL` | Amateur | TLE なし衛星（`COALESCE(tle_group, 'amateur')` でデフォルト適用） |

### TLE なし衛星の自動非表示ルール

`fetch_provisional_tles()` および `fetch_active_tles()` の Phase 2 で適用：

```
TLE が取得できなかった場合:
  status = 'unknown' or 'dead'  → 即時 is_hidden=2
  status = 'alive'
    tle_no_result_since が NULL  → 今日の日付を記録（猶予開始）
    30日以内                     → 紫イタリックで表示継続
    30日超過                     → is_hidden=2（自動非表示）

TLE が取得できた場合:
    tle_no_result_since を NULL にリセット（紫解除）
```

### fetch_active_tles() の2フェーズ設計（2026-08-11 改訂）

**Phase 1 — CelesTrak `GROUP=active` 一括取得（単一リクエスト）**

2026-08-09時点では「`satnogs`・`last-30-days`・`argos`・`orbcomm`・`spire`の5グループを個別に
取得する」設計だったが、2026-08-11に`GROUP=active`（全15,000機超を1リクエストで返す）へ切り替えた。
アクセス可能なグループを順に取得し、DB にある衛星のみ保存する（新規衛星レコードは作成しない）点は変わらない。

**方針転換の経緯（2026-08-09 見送り → 2026-08-11 採用）**: 2026-08-09時点の検討では
「`GROUP=active`は約16,000機（約2.7MB）を毎日丸ごとダウンロードすることになり、必要な分（1,482機）の
約11倍のデータを取得する計算になる」という理由で見送っていた。しかしその後もこのアプリのIPが
CelesTrak/SATNOGSに繰り返しブロックされる問題が実際に続いたため、「必要な分だけ取得する」という
データ量優先の原則そのものを再検討した。CelesTrakが公式に問題視しているのは
**HTTPエラー数（2時間で50件超えでファイアウォール）であって、正当にダウンロードしたバイト数ではない**。
5グループの個別取得はリクエスト数自体は5件と少なかったが、その先のPhase 2（衛星ごとの個別CATNR/SATNOGS
問い合わせ、1回のフルスキャンで800機超に達することもあった）が実質的なエラー源になっていた——
`GROUP=active`は精選済みグループに載っていないだけの多数の衛星も一度に解決してしまうため、
Phase 2に落ちる衛星の数を数百→ごく少数に減らせる。「1回で成功すればエラー0件」という単一リクエストの
強みは、必要な分だけを細切れに取りに行く方式より実際のブロック回避には効く、という判断で採用した
（ユーザー判断、2026-08-11）。

**`GROUP=active`固有の注意点（2h キャッシュと403の誤判定回避）**: CelesTrak側は`GROUP=active`の
レスポンスを約2時間ごとにしか更新しない。この間隔内に同一クライアントから2回目のリクエストを送ると、
本文に理由が明記された403が返る（`GP data has not updated since your last successful download of
GROUP=active at ...`）。これは不正アクセスとして弾かれたのではなく「前回と内容が変わっていない」
という通知に近い。`fetch_active_tles()`自体は最短でも24時間に1回（または3時間バックオフ後の
リトライ）しか自動実行されないため通常はこの2時間制約に触れないが、**「Satellite > Update TLE」
ボタンとSettings > OKは`is_active_tle_stale()`の鮮度ゲートを意図的にバイパスして即時実行する**
（本ファイル「起動時の TLE 同期フロー」参照）ため、これを2時間以内に連打すると発生しうる。
`_is_active_cache_not_yet_updated(response_text)`（`tle_manager.py`）が403応答本文にこの特定の
文言が含まれるかを判定し、含まれていれば**サーキットブレーカーに一切記録せず**（`celestrak_blocked`も
立てない）静かにスキップする。この判定がないと、正当な「まだ更新されていません」応答を実際の
ブロックと誤認し、3時間バックオフのリトライ予約と「ブロックされました」というユーザー向け表示を
誤発生させてしまう。

**Phase 2 — SATNOGS一括ダンプ1回（Phase 1で解決できなかった衛星向け、`_fetch_satnogs_bulk_tles()`）**

**2026-08-11、同日中に2段階の改訂を経て現在の形になった。** 最初はPhase 1の`GROUP=active`化
（上記）に合わせて、Phase 2も「CelesTrak個別CATNR（Phase 2a）→SATNOGS個別（Phase 2b）」の
2段構成のまま、並列数削減・ペーシング・User-Agent付与だけで対策する設計だった。しかしユーザーから
「そもそもSATNOGS側で一括取得できるなら、Phase 2a（CelesTrak個別）自体が要らないのでは。Phase 2は
元々CelesTrakに正式掲載されていない仮ID衛星を拾うためのものだったはず」という指摘があり、
実際に`GET https://db.satnogs.org/api/tle/?format=json`（`norad_cat_id`パラメータなし）を
ライブテストしたところ、**ページネーションなしで全件（1,670件、約512KB）が1回のリクエストで
返る**ことが判明した。しかもこのダンプには実ID帯（<90000）・仮ID帯（≥90000）の両方が混在しており、
Phase 2aの存在理由そのものだった3つの実例（NOAA 18/19＝CelesTrakの`GROUP=WEATHER`から外れた
衛星、ORIGAMISAT-2、ARICA-2の`satnogs_source_id`ルーティング）が**すべてこのダンプ単体で
解決可能**（`tle_source: "Space-Track.org"`）と確認できた。この検証結果を受け、Phase 2a
（CelesTrak個別CATNR）を完全に廃止し、Phase 2bと`fetch_provisional_tles()`（旧: 個別に
SATNOGSへ問い合わせていた仮ID専用メソッド）を、この一括ダンプ1回を共有する設計に統合した
（ユーザー判断、2026-08-11）。

`TLEManager._fetch_satnogs_bulk_tles()`が単一の情報源:
- `GET https://db.satnogs.org/api/tle/?format=json`（フィルタなし）で全件取得し、
  `{norad_cat_id: レコード}`の辞書を返す
- **個別問い合わせへのフォールバックは意図的に持たない**。失敗したらこの回のPhase 2は
  諦める（`errors`に計上・`satnogs_blocked`はブレーカーの状態を反映）だけで、
  「一括が失敗したら1件ずつ試す」という設計には**しない**——それをやると今回排除した
  「数百リクエストの個別ループ」問題がそのまま復活してしまうため
- **`TLEManager`インスタンス内で10分間キャッシュ**（`_SATNOGS_BULK_CACHE_TTL`）。
  `fetch_active_tles()`のPhase 2と`fetch_provisional_tles()`は起動シーケンス内で
  数秒〜十数秒しか離れずに呼ばれるため、キャッシュがないと同じ約512KBのダンプを
  毎回2回ダウンロードすることになる
- **SATNOGS側はこの無フィルタ問い合わせを公式のバルクモードとして文書化していない**
  （`/api/satellites/`・`/api/transmitters/`のようなページネーション付きバルク取得とは違い、
  この挙動はドキュメントではなくテストで確認したもの）。成功直後に即座に再リクエストすると
  HTTP 500が返ることを確認済み（数秒空けての再試行では安定して200が返った）。原因・再発条件は
  未確定のため、単発の一時的エラーとして扱っている（ブロックとは区別。ブロックの判定は
  429のみ）
- `satnogs_source_id`が設定されていればそちらのキーで、無ければ実NORAD IDで辞書を引く
  （詳細は後述の「仮ID→実ID移行」参照）

`fetch_active_tles()`のPhase 2は、Phase 1後もTLEが無い、または既存TLEの`source`が`'satnogs'`
（＝以前このPhase 2で取得したもの）の`10000-89999`衛星が対象。**`source='satnogs'`も対象に
含めるのが重要**——そうしないと、Phase 1でカバーされない衛星（ORIGAMISAT-2など）は最初に
TLEを取得できた時点で`tle_data`行を持ってしまい、以降は「TLE行が既にある」という理由だけで
永久にこのPhase 2から除外され、二度と更新されなくなる（下記「発覚した重大バグ」参照）。

保存時の`source`は`'satnogs'`固定（実際にはCelesTrakが最初に発見した衛星がPhase 1側で既に
`'celestrak'`として保存されているため、Phase 2に来る時点でSATNOGS由来のデータであることが
前提）。これは`tle_data.source`のCHECK制約に`'celestrak-catnr'`のような専用値が無いためだけで
なく、この列がこのメソッド自身のWHERE句で「Phase 1未カバーなので毎回リトライすべき対象」を
判定する目印としても使われているため、意図的な設計。

**サーキットブレーカー**: `_ErrorCountBreaker`はプロセス内累積カウンタではなく
**CelesTrakの実ポリシー通り2時間のローリングウィンドウ**で、かつ**`TLEManager`インスタンスの
生存期間全体で共有**（`fetch_and_update()`のグループ取得・Phase 1・Phase 2（SATNOGS一括）・
`fetch_legacy_tles()`・`fetch_meteor_tles()`がCelesTrak用ブレーカーを、Phase 2と
`fetch_provisional_tles()`がSATNOGS用ブレーカーを共有する。CelesTrak用とSATNOGS用は
完全に独立しており、一方がブロックされてももう一方は影響を受けない——`GROUP=active`が
403でブロックされていても、Phase 2のSATNOGS一括取得は正常に試みられる）。以前は各呼び出しが
毎回新規ブレーカーを生成しており、起動直後にこれらが連続実行されると、それぞれ独立した
20エラーの猶予を使い切るまで誰も気づけない構造だった。「接続自体ができない」
（`ConnectTimeout`/`ConnectError`）は、CelesTrak/SATNOGS双方について明示的な403/429と
同様に`blocked=True`として即座にブレーカーを倒す（このアプリの実際の被ブロック時の症状は
「HTTP応答が返る」よりも「接続要求がサイレントに破棄されタイムアウトする」ことの方が多いと
判明済みのため。本ファイル「SATNOGS・CelesTrakに接続できない時は...」参照）。`ReadTimeout`/
`RemoteProtocolError`（接続はできたが遅い/切れた）は個別失敗として扱い、即座にはブロック
扱いしない。

**識別可能なUser-Agent**（`FBSAT59/<version> (+https://github.com/JF9SOM/FBSAT59)`、
`src/data/http_client.py`）をCelesTrak/SATNOGS双方への全リクエストに付与。CelesTrak/SATNOGS
双方の利用ポリシーが、大量アクセスする側に連絡先入りの識別子を推奨している。

**起動時、SATNOGSトランスミッタ同期と衛星名/TLE同期チェーンを別スレッドで並行実行するのをやめ、
1スレッドで順番に実行**するよう変更済み（`main_window.py`の`_start_scheduler()`）。どちらも
db.satnogs.orgにアクセスするため、同時に2本のコネクションから畳みかけるより1本の定常的な
流れにした方が不正アクセスと区別しやすい。

**発覚した重大バグ（2026-08-09 発見・修正、GitHub上のやり取りではなくユーザー実機でのTLE不整合報告から）**:
Phase 2の対象条件が元々「TLE行が一つも無い衛星」のみだったため、Phase 1のどのグループにも載らない
衛星（ORIGAMISAT-2 / NORAD 68795 等）は、最初にTLEを取得できたその瞬間から「TLE行が既にある」と
みなされ、**以降どれだけ古くなっても二度と自動更新の対象に選ばれなくなる**という「一度きりのラチェット」
状態に陥っていた。ORIGAMISAT-2は実際に44日間（2026-06-27〜2026-08-09）更新されないまま放置され、
実際のAOS予測が約20分ずれるという実害が発生した（詳細な原因調査・パス予測誤差との相関は
本ファイル該当セクション参照）。同種の一発ラチェットに陥っていた衛星は、過去の3回のPhase 2単発実行
だけで約700機に達していた。

修正: Phase 2の対象条件を「TLE行が無い」OR「既存TLEの`source`が`'satnogs'`」に拡張。あわせて、
Phase 2の`INSERT OR REPLACE`が毎回`tle_group`を`'amateur'`に強制リセットしていた副次バグ（分類の劣化）
も、既存の`tle_group`を保持するよう修正済み。

**ダウンロード進捗のパーセント表示（2026-08-11、`_get_with_progress()`）**: Phase 1・Phase 2とも
1回のリクエストで完結する一括ダウンロードになったため、「今どのくらい進んでいるか」を`Content-Length`
ヘッダーと実受信バイト数から計算してステータスバーに表示できないか検討し、実装した。SATNOGSの
一括ダンプは実測で`Content-Length: 523906`が返ることを確認済み（本ファイル前述の一括エンドポイント
検証時）。

- `client.get(url, params=...)`（レスポンス全体を受け取ってから返る）を`client.stream("GET", url,
  params=...)`によるストリーミング受信に置き換えた`_get_with_progress()`を新設。Phase 1
  （`GROUP=active`）とPhase 2（SATNOGS一括ダンプ`_fetch_satnogs_bulk_tles()`）の両方が共通で使う
- `response.aiter_bytes()`でチャンクを受信するたびに`Content-Length`との比率からパーセントを計算し、
  1%刻みで変化があったときだけ`progress_callback(f"{label}: downloading... {pct}%")`を呼ぶ
  （毎チャンクそのまま呼ぶとステータスバーの更新が多すぎるため）
- **表示形式はパーセントのみ**（例: `SATNOGS: downloading... 45%`）。KB数の併記は情報過多と判断し
  見送った（ユーザー判断、2026-08-11）
- ストリーミングで受信し切ったレスポンスは、`async with client.stream(...) as response:`の
  ブロックを抜けた後も`.text`/`.json()`/`.raise_for_status()`がそのまま使える（httpxはストリームを
  最後まで読み切った時点で内容をキャッシュする仕様のため）。これにより呼び出し元の既存の
  例外処理コード（`_is_active_cache_not_yet_updated(exc.response.text)`等）は無変更で動く
- `Content-Length`ヘッダーが無い場合（chunked転送等）は、パーセント無しの`"{label}:
  downloading..."`を1回だけ表示するフォールバックにした
- **`fetch_provisional_tles()`には配線していない**。この関数の`progress_callback`は
  `(done: int, total: int)`という「衛星件数」を表す別のシグネチャ（`_fetch_satnogs_bulk_tles()`が
  使う文字列1個のシグネチャとは非互換）で、`main_window.py`側の`_prov_progress(done, total)`に
  直接バイト数を渡すと衛星件数と誤解される表示になってしまうため。実運用では`fetch_active_tles()`
  が先に呼ばれてキャッシュを温めるため、`fetch_provisional_tles()`が実際にバルクダウンロードを
  行う機会自体まれ（本ファイル前述のキャッシュ機構参照）という判断もあった

### 起動時の TLE 同期フロー（2026-08-10 順序変更・2026-08-11 鮮度ゲート監査で更新）

`MainWindow._start_scheduler()` が起動直後に以下を行う（`_no_background_sync` テストフィクスチャで
まるごと無効化される、CI上で毎回ネットワークを叩かないための唯一の入口）:

```
アプリ起動 → _start_scheduler()
  │
  ├─ APScheduler 開始（下記7ジョブを "interval" trigger で登録。misfire_grace_time付き。
  │   interval ジョブは登録直後には発火せず、登録時点から丸1区間経過して初めて実行される点に注意
  │   ——このため、起動時に「区間経過チェック」を別途行わないと、7区間ぶんの継続起動をしない限り
  │   一度も自己修復されない。2026-08-11 の監査でこの欠落を3件発見・修正した（後述）
  │
  ├─ [即時] AMSAT運用状況: AmsatFetcher.is_stale(24h) が True ならバックグラウンドで再取得
  │
  ├─ [即時] SATNOGSトランスポンダーDB: `source='satnogs'`行が0件、または
  │   TransmitterManager.is_satnogs_transmitters_stale(168h) が True ならバックグラウンドで再取得
  │   （後者は2026-08-11追加。以前は0件チェックのみで、初回同期後は168hジョブの発火待ちのみだった）
  │
  ├─ [バックグラウンド] _refresh_satellite_names_sync()  ← 以下を直列に実行
  │     1. sync_satellite_names()    ← SATNOGS 衛星名・ステータス更新・移行パイプライン
  │          ゲート: TransmitterManager.is_satellite_names_stale(24h)（2026-08-13追加。
  │          以前は無条件で毎回実行しており、約2700件のページネーション一括取得
  │          （`/api/satellites/?format=json`、TLE一括ダンプ`/api/tle/?format=json`とは
  │          別エンドポイント）を再起動のたびにフルで再実行していた）
  │     2. fetch_active_tles()       ← NORAD 10000-89999 衛星の TLE 補完
  │          ゲート: is_active_tle_stale(24h) OR is_active_tle_retry_due()
  │     3. fetch_provisional_tles()  ← NORAD ≥ 90000 衛星の TLE 取得
  │          ゲート: is_provisional_tle_stale(12h)（2026-08-11追加。以前は無条件で毎回実行）
  │     4. fetch_legacy_tles()       ← NORAD < 10000 衛星のクリーンアップ（対象0件ならno-op、毎回無条件）
  │     5. fetch_meteor_tles()       ← METEOR/HRPT 衛星の TLE 補完（衛星ごとneeds_update(24h)、毎回無条件）
  │     6. load_community_transmitters() ← ローカルJSON読み込み（ネットワーク不要、毎回無条件）
  │     7. CelesTrak 6グループ一括フェッチ（stations/amateur/cubesat/weather/earth-obs/science）
  │          ゲート: 各グループごとに is_source_stale(グループ自身のupdate_interval_hours) OR
  │                  is_group_empty()（2026-08-11、is_source_stale側を修正。以前は
  │                  「一度もフェッチしていないか」のみ判定し、経過時間を見ていなかった）
  │
  ├─ [バックグラウンド] DE421 天体暦ロード（Moon/EME追尾用、初回のみ約17MBダウンロード）
  │
  └─ [バックグラウンド] NTPクロック同期チェック
```

**ゲートの意味（起動のたびに毎回実行 vs 鮮度チェック後にのみ実行）**: 上記のうち
「毎回無条件」と書いたステップ（4・5・6）は、対象データ自体が自己制限的
（空なら即return・衛星ごとの内部staleness判定を持つ・ネットワーク不要のローカル処理）なため、
無条件に呼んでも実害がない設計。一方「ゲート: ...」と明記したステップ（1・2・3・7、および
AMSAT・SATNOGSトランスポンダーDB）は、**もし鮮度チェックを誤ると「毎回ネットワークを叩きすぎる」
（ブロックの原因になる）か「二度と更新されない」のどちらかに転ぶ**ため、専用の`is_*_stale()`系
メソッドで経過時間を明示的に判定している。

**`sync_satellite_names()`（ステップ1）が唯一ゲートを持たず、再起動のたびにフル再実行されていた
不具合（2026-08-13 発見・修正）**: 上記の項目6（CelesTrak不通時のSATNOGS巻き込まれ対策）を
実装した直後、ユーザーが実機で「一度起動・動作確認・終了・再起動」を繰り返したところ、
毎回SATNOGSからの取得（ログに`GET https://db.satnogs.org/api/satellites/?format=json`と
`SATNOGS satellite names sync completed: {'updated': 2766, 'skipped': 1}`）が走っているように
見える、という報告があった。実際にログを確認したところ、TLE本体の一括取得
（`fetch_active_tles()`のPhase 2・`fetch_provisional_tles()`）は`Active TLE cache is fresh
— skipping fetch.`/`Provisional TLE cache is fresh — skipping fetch.`と正しく鮮度キャッシュで
スキップされていたが、**`sync_satellite_names()`だけは元々ゲート自体が存在せず**、SATNOGSに
到達可能な限り毎回無条件でフルページネーション同期していたことが判明した（起動ごとに約10〜45秒、
ネットワーク状況依存）。

`sync_satellite_names()`は名前・ステータス（alive/dead/unknown）・エイリアス名を更新するだけで
TLEにもトランスミッターDBにも触れないが、TLE取得だけでは代替できない役割を持つ（CelesTrakの
仮名"OBJECT C"等を正式名称で上書き・死亡衛星の自動非表示判定・仮ID→実ID移行の検知起点）ため、
省略はできない。ユーザーとの相談の結果、他の起動ステップと同じ24時間鮮度ゲートを追加することで
合意した。

**修正**: `TransmitterManager.is_satellite_names_stale(max_age_hours=24.0)`を新設
（`is_satnogs_transmitters_stale()`と全く同型、`sync_log`の`sync_type='satnogs_names'`最新
エントリを参照）。`_refresh_satellite_names_sync()`のステップ1をこのゲートで囲んだ
（`_refresh_satellite_names_periodic()`——6時間ごとのAPSchedulerジョブ——は間隔自体がゲートの
役割を果たすため対象外のまま）。

**あわせて発覚した欠落: 手動同期メニューが存在しなかった**: 24時間ゲートを追加するにあたり、
既存の`Satellite → Sync SATNOGS`（`_on_sync_satnogs()`、2026-08-13に`Fetch Transmitter Database`
へ改名。後述参照）が実は**トランスミッターDB
（`/api/transmitters/`）専用**で、`sync_satellite_names()`（`/api/satellites/`、別エンドポイント・
別テーブル）を手動で即時実行する手段がそもそも存在しないことが分かった。既存ボタンに混ぜる案も
検討したが、①データの性質が別物 ②`sync_satellite_names()`は約2700件のページネーションで
10〜45秒かかるのに対し既存のトランスミッター同期は数秒で終わる——混ぜると「周波数だけ更新したい」
操作が毎回巻き込まれて遅くなる ③失敗時にどちらが失敗したか切り分けにくくなる、という理由で
**`Satellite → Sync Satellite Names`（`衛星名の同期`）を新規メニュー項目として追加**した
（`_on_sync_satellite_names()` → `_refresh_satellite_names_manual_sync()`、`_on_sync_satnogs()`
と同型・同じ`_satnogs_status`シグナルを共用）。明示的なボタン押下なので`is_satellite_names_stale()`
は意図的にバイパスする（Update TLEボタンが`is_active_tle_stale()`等をバイパスするのと同じ設計）。

**`Sync SATNOGS`を`Fetch Transmitter Database`へ改名（2026-08-13）**: 上記の`Sync Satellite
Names`メニュー追加により、「SATNOGS」という語自体がトランスミッターDB・衛星名・TLE（`fetch_active_tles()`
のPhase 2・`fetch_provisional_tles()`）という複数の別々の同期処理を指すようになり、
「`Sync SATNOGS`」という名前だけでは**どのSATNOGS同期を指しているのか判別できない**という
分かりにくさをユーザーが指摘した。英語ラベルも含めて改名するか確認した上で、
`_("Sync SATNOGS")` → `_("Fetch Transmitter Database")`に変更（`_on_sync_satnogs()`自体・
内部コメント・Auto Fetch Rulesダイアログの本文中の参照も含めて統一）。日本語訳は
「SATNOGSと同期」→「トランスミッターDBを取得」。ハンドラ名`_on_sync_satnogs()`・
`_refresh_satnogs_sync()`自体はSATNOGS APIのエンドポイント名（`/api/transmitters/`由来）との
対応が分かりやすいため変更していない——ユーザーに見える文言（メニューラベル・ヘルプ本文）だけを
改名の対象とした。

**教訓（i18nと機能追加が絡む改名の手順）**: Auto Fetch Rulesダイアログの日本語訳には、
`<b>Satellite → Sync SATNOGS</b>`のようにメニューパスを**英語のまま埋め込んだ**箇所が複数
あった（実際のメニュー表示は日本語の「衛星」「トランスミッターDBを取得」等）。これは
msgmergeが英語の`msgid`をそのまま流用して訳文を生成する性質上、翻訳者（Claude）が
明示的に「メニューパス部分も日本語のメニュー名に置き換える」意識を持たない限り機械的に
見過ごされやすい。この種のヘルプ本文にUIのメニューパスを埋め込む場合、**英語版の
`msgid`はそのメニューの実際の英語ラベルと一致させ、日本語版の`msgstr`も対応する日本語
メニューラベル（`_("...")`の実際の訳語）に置き換える**ことを徹底すること。

**「Cannot connect to CelesTrak」表示直後に、消えない「CelesTrak blocked — retry in 3h」が
出る不具合（2026-08-13 発見・修正）**: 上記の`celestrak_reachable`/`satnogs_reachable`対応
（同日先行の修正）実装後、実際にCelesTrakがブロックされている実機環境で手動「Update TLE」を
押したユーザーから、①冒頭の`❌ Cannot connect to CelesTrak`（約3秒）→②SATNOGS側の正常な
取得メッセージ群→③`⚠ CelesTrak blocked — retry in 3h`が表示されて**消えなくなる**、という
報告があった。「①で既にCelesTrakへ再接続しないと分かっているはずなのに、なぜ③が出るのか」
という指摘は正確だった。

**原因**: `fetch_active_tles(celestrak_reachable=False)`は実際にはCelesTrakへ再接続しない
（③のメッセージが「今まさに新しく失敗した」ことを意味するなら、それは誤り）が、内部的には
「本当に接続を試みて失敗した」場合と**全く同じように**`_celestrak_breaker.record_error(blocked=True)`
を呼ぶ設計にしていた（リトライスケジューリングの`stats["celestrak_blocked"]`を正しく機能
させるための意図的な設計。前回の修正コミット参照）。この結果`_schedule_active_tle_retry_if_blocked()`
が「新たにブロックされた」と誤認し、①と実質同じ情報を**別の文言で二重に**表示していた。
さらにこの関数がメッセージを表示すると、呼び出し元の`_fetch_all_tle_sources()`が持つ
「最後に表示をクリアする」ガード（`if not blocked: emit("")`）もスキップされるため、③が
そのまま画面に残り続けていた。

**修正**: `_schedule_active_tle_retry_if_blocked()`に`already_reported: set[str] | None`引数を
新設。呼び出し元（`_fetch_all_tle_sources()`・`_refresh_satellite_names_sync()`）が、その回の
実行冒頭で既にユーザーへ「Cannot connect to X」と伝えたホスト集合（`down`）をそのまま渡す。
`already_reported`に含まれるホストは表示対象から除外する（**リトライのスケジュール自体は
除外せず引き続き行う**——CelesTrakが実際にまだ落ちているという状態自体は変わらないため）。
関数は「実際に新規メッセージを表示したか」を`bool`で返すようになり、呼び出し元はこれを
（従来の生の`blocked`フラグの代わりに）最後の「表示クリア」判定に使う。これにより、
既に報告済みのホストしかブロックされていない場合は③が一切表示されず、ステータスバーは
通常通りクリアされる（両方新規にブロックされた場合や、片方だけ新規にブロックされた場合は、
新規分のみを名指しして引き続き表示する）。

**教訓**: 「実際には行っていない処理を、内部の統計・状態管理のためだけに『行ったことにする』」
設計（今回は`record_error(blocked=True)`）は、その統計を消費する**下流の全ての箇所**
（今回はUIメッセージ表示）が、この「実際には起きていない」という文脈を認識できるとは限らない。
統計値だけを見て「新しい出来事が起きた」と解釈するコードには、常にこの種の誤検知のリスクが
残る。

**Auto Fetch Rulesダイアログに「SATNOGSからのTLEデータ」セクションを追加（2026-08-13）**:
上記の一連のTLE同期フロー質問に答える過程で、`Active TLE fallback`・`Provisional TLEs`
の2行は自動取得スケジュール表に時間だけ載っており、これらが（CelesTrakの分類済み一覧
とは別に）SATNOGS自身のTLEデータベースを使うフォールバック処理であることを説明する
専用セクションが無いことが判明した（「衛星名/ステータス」「トランスミッターDB」には
専用セクションがあるのに、これだけ無かった）。両セクションと同じ構成（見出し＋説明段落）
で「TLE Data from SATNOGS」セクションを追加し、`Satellite → Update TLE`で両方を即時更新
できる旨も記載した。

**自動取得スケジュール表を「CelesTrak TLE」/「CelesTrak TLE以外の取得」の2表に分割
（2026-08-13）**: 単一の表に、CelesTrakの分類済みグループ一括取得（Space Stations・
Amateur Satellites・CubeSats・Weather Satellites・Earth Observation/Science・
METEOR/HRPT）と、それ以外の取得元（Active TLE fallback・Provisional TLEs・
Satellite Names/Status・AMSAT・Transmitter Database）が混在しており、どの行がどこから
取得しているのか分かりにくいという指摘を受けて分割した。`Active TLE fallback`は
実際にはCelesTrakの`GROUP=active`一括取得が主でSATNOGSへのフォールバックは補完的
（同一行内でユーザーとの相談の上、直前に追加した「TLE Data from SATNOGS」セクションの
分類（Provisional TLEsと同じ「SATNOGSへフォールバックする処理」）と一貫性を取るため
下側の表に分類することで合意した）。

**進捗メッセージ全般を「何をどこから取得しているか」が分かる文言に見直し（2026-08-13）**:
取得中にステータスバー下部へ表示される一連のメッセージを再点検した結果、複数箇所で
「どこから何を取得しているか」が読み取れない文言になっていたことが判明した。

- `sync_satellite_names()`の進捗（起動時）: `"Syncing satellites from SATNOGS..."` →
  `"Syncing satellite names from SATNOGS..."`（手動同期メニューの文言と統一。「satellites」
  だけだとTLE取得と紛らわしい）
- `fetch_active_tles()`のPhase 1開始通知: `"CelesTrak active..."` →
  `"CelesTrak: fetching active TLEs..."`（何のために接続しているか不明だった）
- `_get_with_progress()`のダウンロード進捗（Phase 1・Phase 2共通）:
  `"{label}: downloading... {pct}%"` → `"{label}: downloading TLE data... {pct}%"`
- `fetch_active_tles()`のPhase 2開始通知: `"SATNOGS: {n} satellite(s)..."` →
  `"SATNOGS: fetching TLE data for {n} satellite(s)..."`（名詞の羅列だけで動詞が無く、
  進行中なのか完了なのかも分からなかった）
- CelesTrak 6グループ一括フェッチの進捗（起動時・Update TLEボタン両方）:
  内部ソース名（例: `"celestrak-amateur"`）をそのまま表示していたのを、Settings画面の
  TLE Sourcesタブが既に持っていた表示名（例: `"Amateur Satellites (CelesTrak)"`）に
  統一。この表示名辞書はこれまで`settings_dialog.py`内のプライベート定数
  `_SOURCE_DISPLAY_NAMES`として重複しかねない形で存在していたため、
  `data.tle_manager.TLE_SOURCE_DISPLAY_NAMES`として公開・一本化し、両画面で共有する
  ようにした
- **Update TLEボタン（`_fetch_all_tle_sources()`）のCelesTrak 6グループフェッチ中、
  そもそも進捗メッセージが一切出ていなかった**ことも判明（起動時側の同種ループには
  既にあったが、Update TLE側には元から実装されていなかった）。同じ形式の進捗表示を
  追加した

**教訓**: 「進捗メッセージがある」ことと「そのメッセージが実際に分かりやすい」ことは別。
今回見つかった問題の多くは、メッセージ自体は存在するが、動詞が欠けている・データの種類
（TLE）が省略されている・内部識別子をそのまま表示している、という**質**の問題だった。
新しい進捗メッセージを追加する際は「これだけを見て、何がどこから取得されているか
第三者が分かるか」を基準にすること。

**2番目のステップだった `fetch_active_tles()` を最優先に変更**（2026-08-10）:
以前は「Phase 2のSATNOGSフォールバックが20〜30分かかりうるので他のステップを待たせない」という
理由で最後に実行していたが、この理由はPhase 2にサーキットブレーカー・並列化・CelesTrakフォールバックを
入れた今となっては古い（前述の各節参照）。一方で、この処理こそが通常のNORAD ID（例: ORIGAMISAT-2、
NORAD 68795）のTLEを実際に最新化する、最も価値の高いステップである。ステップ間に進捗表示が一切なかった
ため、ステップ1完了後にステータスバーの表示が更新されなくなると、ユーザーからは「フリーズした」ように
見え、実際には正常に動作中の後続ステップの途中でアプリを閉じてしまう、という報告が複数回の再起動を
経ても`fetch_active_tles()`に一度も到達できないという実害につながった（2026-08-10）。`fetch_active_tles()`
に`progress_callback`引数を新設し、フェーズ（CelesTrakグループ名・Phase 2a/2bの対象数）ごとに
ステータスバーへ進捗を表示するようにした上で、最優先の位置に移動した。

**「Satellite > Update TLE」ボタンがそもそも`fetch_active_tles()`を一度も呼んでいなかった不具合
（2026-08-10 発見・修正）**: 上記の順序変更・進捗表示を実装した v0.3.8 をWindows実機で検証した
ユーザーから、「1時間放置しても、手動でUpdate TLEボタンを押しても、ORIGAMISAT-2のTLEだけは
更新されない。ボタンを押した後にTLE更新時刻自体は進んでいるので、他の衛星は更新されているはず」
という報告があった。ログ（`fbsat59.log`）を解析したところ、`_fetch_all_tle_sources()`
（Update TLEボタン・Settings > OKの両方が共有する実装）は`SettingsDialog.get_enabled_sources()`
が返す**CelesTrakの決め打ちグループ6種**（stations/amateur/cubesat/weather/earth-obs/science）
を`fetch_and_update()`でループするだけで、**`fetch_active_tles()`（Phase 2の個別問い合わせで
ORIGAMISAT-2を解決する処理）を一切呼んでいなかった**ことが判明した。ORIGAMISAT-2はこの6グループの
どれにも属さない（2026-08-09に実際に問い合わせて確認済み）ため、**このボタンを何度押しても、
ネットワークが完全に正常であっても、原理的に一生解決できない**設計だった。ユーザーが「更新時刻は
進んだのに対象衛星だけ変わらない」と正確に見抜いた通りの状況で、実際にログにもその瞬間の6グループ
（`celestrak-stations/science/amateur/weather/cubesat/earth-obs`）の成功結果のみが記録されており、
`fetch_active_tles()`関連のログ行は一切存在しなかった。

**修正**: `_fetch_all_tle_sources()`の末尾に`fetch_active_tles(progress_callback=...)`の呼び出しを
追加。`is_active_tle_stale()`の24時間ゲートは意図的にバイパスする（バックグラウンドの定期実行とは
異なり、ユーザーが明示的に「今すぐ更新して」とボタンを押した以上、鮮度キャッシュより即時実行を
優先すべきと判断）。この関数はSettings > OKとUpdate TLEの両方から共有されているため、両方の
経路で同時に直る。

**教訓**: 「更新ボタンを押しても直らない」という報告を受けた際、TLE取得ロジック自体（Phase 2の
サーキットブレーカーやCelesTrak個別問い合わせ）を疑う前に、**そもそもそのボタンが正しい関数を
呼んでいるか**を確認すべきだった。今回はPhase 2側の実装は（この時点で）既に正しく動作していたが、
呼び出し経路の方が最初から欠落しており、Phase 2をどれだけ直しても症状は変わらなかったはずである。
また、ユーザーが提示した「TLE更新時刻は進んだのに対象衛星だけ変わらない」という一見矛盾した観察は、
実際には「複数の独立した更新経路のうち一部だけが動いている」ことを示す精度の高い手がかりであり、
額面通りに深掘りする価値があった。

### TLE/衛星名/トランスミッターDB同期 — 進捗メッセージ全体フロー（2026-08-13）

同期処理には4つの独立した起点（①起動時・②Update TLEボタン・③Sync Satellite Namesボタン・
④Fetch Transmitter Databaseボタン）があり、それぞれ表示されるメッセージの種類・順序・
表示方式（常時ラベル vs 自動消去する一時メッセージ）が異なる。「進捗メッセージ全般を
『何をどこから取得しているか』が分かる文言に見直し（2026-08-13）」（前述）の作業に伴い、
全体像を整理した。

#### ① 起動時（`_refresh_satellite_names_sync()`）

```
[両ホスト到達可否チェック]
  │
  ├─ 両方到達可能 → 下記へそのまま進む
  ├─ 片方だけ不通 → "❌ Cannot connect to {CelesTrak|SATNOGS}"（約3秒表示）→ 続行
  └─ 両方不通    → "❌ Cannot connect to CelesTrak/SATNOGS"（約10秒表示）
                    → community_transmitters読み込み（ログのみ）→ ""（クリア）→ 終了
       ↓（片方到達可能 or 両方到達可能の場合のみ続く）
[Step1: 衛星名同期]（SATNOGS到達可能 かつ is_satellite_names_stale(24h) の場合のみ）
  "🛰 Syncing satellite names from SATNOGS..."
  "🛰 Syncing satellite names... (n)"  ← n件処理ごとに更新
       ↓
[Step2: Active TLE補完]（is_active_tle_stale(24h) or is_active_tle_retry_due() の場合のみ）
  "🛰 CelesTrak: fetching active TLEs..."
  "🛰 CelesTrak: downloading TLE data... N%"
  "🛰 SATNOGS: fetching TLE data for n satellite(s)..."   ← Phase1で解決しきれなかった分だけ
  "🛰 SATNOGS: downloading TLE data... N%"
  （ブロック発生時のみ）"⚠ {CelesTrak|SATNOGS} blocked — retry in 3h"
       ↓
[Step3: Provisional TLE取得]（SATNOGS到達可能 かつ is_provisional_tle_stale(12h) の場合のみ）
  "🛰 Fetching provisional TLEs... (done/total)"
       ↓
[Step4: Legacy衛星クリーンアップ]（CelesTrak到達可能の場合のみ・表示メッセージなし、ログのみ）
       ↓
[Step5: METEOR/HRPT TLE確認]（CelesTrak到達可能の場合のみ・表示メッセージなし、ログのみ）
       ↓
[Step6: コミュニティ周波数読み込み]（表示メッセージなし、ログのみ）
       ↓
[Step7: CelesTrak 6グループ一括取得]（CelesTrak到達可能 かつ 鮮度切れグループがある場合のみ）
  "🛰 Fetching group TLEs: Amateur Satellites (CelesTrak) (1/6)..."
  ...（対象グループ数だけ繰り返し）
  ""（クリア）
       ↓
""（最終クリア。衛星リスト再表示と同時）
```

#### ② Satellite → Update TLE（`_fetch_all_tle_sources()`）

鮮度ゲートを全て無視して即時実行する点が①と異なる。

```
"🛰 Updating TLEs…"
       ↓
[両ホスト到達可否チェック]（①と同じ分岐・同じメッセージ）
       ↓（片方到達可能 or 両方到達可能の場合のみ続く）
[CelesTrak 6グループ一括取得]（CelesTrak到達可能なら常に、鮮度に関わらず実行）
  "🛰 Fetching group TLEs: Space Stations (CelesTrak) (1/6)..." ...
       ↓
[Active TLE補完]（常に実行）
  "🛰 CelesTrak: fetching active TLEs..." など、①のStep2と同じメッセージ群
       ↓
[Provisional TLE取得]（SATNOGS到達可能なら常に実行）
  "🛰 Fetching provisional TLEs... (done/total)"
       ↓
（ブロック発生時のみ、かつ新規にブロックされた分のみ）"⚠ {provider} blocked — retry in 3h"
       ↓
""（最終クリア。ブロックが新規表示された場合はクリアされず、そのメッセージのまま残る）
```

#### ③ Satellite → Sync Satellite Names（手動・単独）

```
"Syncing satellite names from SATNOGS..."（ステータスバー、5秒表示の一時メッセージ）
       ↓
（SATNOGS不通の場合）"❌ Cannot connect to SATNOGS" → 終了
（到達可能な場合）"Satellite names sync: {upd} updated, {skp} skipped"（8秒表示）
```

#### ④ Satellite → Fetch Transmitter Database（手動・単独）

```
"Syncing transmitter frequencies from SATNOGS..."（ステータスバー、5秒表示の一時メッセージ）
       ↓
（SATNOGS不通の場合）"❌ Cannot connect to SATNOGS" → 終了
（到達可能な場合）"SATNOGS sync: {ins} inserted, {upd} updated, {skp} skipped"（8秒表示）
```

#### 表示方式の違い（①②と③④）

- **①②**: `_sync_progress`シグナル経由。ステータスバー下部の常時ラベル（`_sync_label`）に
  表示され、明示的に`emit("")`するまで残り続ける
- **③④**: `_satnogs_status`シグナル経由。`QStatusBar.showMessage()`による一時メッセージ
  （5〜8秒で自動消去）として表示される

同じ「SATNOGSと通信する」処理でも、①②（TLE同期チェーンの一部）と③④（単独の手動ボタン）
とで表示の仕組みそのものが異なる点に注意。新しい同期処理を追加する際は、常時ラベルが適切か
（複数ステップにまたがる進行状況を示す場合）、一時メッセージが適切か（単発の完了通知）を
判断すること。

### 起動時鮮度チェックの網羅的監査と修正（2026-08-11）

#### 発端

4G Wifi経由でのTLEブロック検証（前述の各節参照）が一段落した後、ユーザーから
「Provisional TLEのfetchはCLAUDE.md上12時間ごとのはずだが、同期→終了→即座に再起動、を
繰り返しても毎回フェッチされる。なぜか」という指摘があった。調査したところ、
`fetch_provisional_tles()`の起動時呼び出しには**鮮度チェックが一切存在しない**ことが判明。
「他にも同じようなものがないか確認してから実装して」という指示を受け、`_start_scheduler()`が
登録する7つのAPSchedulerジョブ全てについて、対応する起動時ゲートが正しく実装されているかを
1つずつ監査した。結果、**3件の独立したバグ**が見つかった——2件は「毎回無条件でフェッチする」
（過剰）、1件は「一度目以降ほぼ永久にフェッチしない」（欠落）という、正反対の方向の不具合だった。

#### 根本原因（3件に共通）

APSchedulerの`"interval"`トリガーは**ジョブ登録の瞬間には発火せず、登録時点から丸1区間
経過して初めて実行される**。デスクトップアプリは毎回終了・再起動されるものであり、
「7日間（あるいは1〜12時間）連続で起動しっぱなしにする」という前提はほとんどのユーザーの
実利用パターンと一致しない。このため、**起動時に「前回の完了からどれだけ経過したか」を
独立して判定するゲートを別途持たない限り、定期ジョブは実質的に一度も発火しないまま
終わる**。`fetch_active_tles()`（`is_active_tle_stale()`）だけがこの原則を最初から
正しく実装しており、他は次のいずれかの誤りを持っていた。

#### 発見した3件のバグと修正

| # | 対象 | 症状 | 原因 | 修正 |
|---|---|---|---|---|
| 1 | `fetch_provisional_tles()`（NORAD≥90000） | 起動のたびに**必ず**全件フェッチ（過剰） | 起動時呼び出しに鮮度チェックが一切無かった | `TLEManager.is_provisional_tle_stale(12h)`を新設しゲート |
| 2 | `sync_from_satnogs()`（トランスミッタDB） | 初回同期後は**ほぼ永久に**再取得されない（欠落） | 起動時ゲートが`source='satnogs'`行0件（真の初回起動）のみを判定し、経過時間を見ていなかった | `TransmitterManager.is_satnogs_transmitters_stale(168h)`を新設し、0件チェックに`OR`で追加 |
| 3 | CelesTrak 6グループ一括フェッチ（stations/amateur/cubesat/weather/earth-obs/science） | バグ2と同型（欠落） | `is_source_stale()`が「一度もフェッチしていないか」のみを判定し、経過時間を見ていなかった（ドキュメント自身に「APSchedulerの定期ジョブが後は面倒を見る」という誤った前提が明記されていた） | `is_source_stale()`自体を修正し、各ソース自身の`TLE_SOURCES[...]["update_interval_hours"]`（1〜12h）との比較を追加 |

バグ1はユーザー自身の指摘、バグ2・3はその指摘をきっかけにした横展開監査で発見した
（`is_provisional_tle_stale()`実装後、同じ設計原則を他の6ジョブに機械的に当てはめて確認）。

いずれも`TLEManager.is_active_tle_stale()`と全く同じ形（`sync_log`の最新`finished_at`を
読み、`datetime.now(UTC) - last > timedelta(hours=max_age_hours)`で判定）に統一してある。

#### 「Update TLE」ボタン側にも同型のバグが1件見つかった

上記の監査中、`_fetch_all_tle_sources()`（Satellite > Update TLE / Settings > OK 共有）が
`fetch_provisional_tles()`を**一度も呼んでいない**ことが判明した。これは前述
「`fetch_active_tles()`を一度も呼んでいなかった不具合（2026-08-10）」と全く同じクラスの
バグで、Provisional衛星についてだけ同じ穴が残っていた。`fetch_active_tles()`と同じ扱い
（自身の鮮度ゲートをバイパスし無条件で実行）で追加した。

#### 起動時とUpdate TLEボタンの取得順序（最終形、2026-08-11時点）

**起動時**（`_refresh_satellite_names_sync()`、上記フローチャート参照）:

```
1. sync_satellite_names()        無条件
2. fetch_active_tles()           is_active_tle_stale(24h) OR is_active_tle_retry_due()
3. fetch_provisional_tles()      is_provisional_tle_stale(12h)                    ← 2026-08-11
4. fetch_legacy_tles()           無条件（自己制限的、実質no-op後は毎回一瞬で終わる）
5. fetch_meteor_tles()           無条件（衛星ごとneeds_update(24h)で自己制限）
6. load_community_transmitters() 無条件（ローカルJSON、ネットワーク不要）
7. CelesTrak 6グループ一括       is_source_stale(グループ自身の interval) OR is_group_empty()  ← 2026-08-11
```

AMSAT運用状況は`_start_scheduler()`内で上記とは別スレッド・独立に実行される
（`is_stale(24h)`）。SATNOGSトランスミッタDB同期（`satnogs_count==0 OR
is_satnogs_transmitters_stale(168h)`）は、以前は別スレッドで上記と**並行**実行していたが、
2026-08-11にdb.satnogs.orgへの同時多発アクセスを避けるため`_refresh_satellite_names_sync()`と
**同一スレッドで直列**（SATNOGSトランスミッタDB同期 → 上記1〜7の順）に変更した
（本ファイル「fetch_active_tles() の2フェーズ設計」内「リクエストの『行儀』自体の改善」参照）。

**Satellite > Update TLE / Settings > OK**（`_fetch_all_tle_sources()`）:

```
1. CelesTrak 6グループ一括       無条件（鮮度ゲートをバイパス。「今すぐ更新」という明示要求のため）
2. fetch_active_tles()           無条件（同上、2026-08-10 追加）
3. fetch_provisional_tles()      無条件（同上、2026-08-11 追加）
```

Update TLEボタンは**上記3つのみ**を呼ぶ設計であり、`fetch_legacy_tles()`・`fetch_meteor_tles()`・
`sync_from_satnogs()`・`sync_satellite_names()`・`load_community_transmitters()`は呼ばない
（起動時のみ実行される）。前者3つは「ボタンを押した以上、鮮度キャッシュより即時実行を優先すべき」
という設計判断（2026-08-10 に`fetch_active_tles()`で確定した方針をそのまま踏襲）で、
起動時の`is_*_stale()`ゲートを意図的にバイパスしている点に注意。

#### 監査で「問題なし」と確認したもの

- `fetch_legacy_tles()`: SQL WHERE句自体が「TLE未解決の衛星」のみを対象にする自己制限的設計。
  対象が0件になれば以降は毎回一瞬でno-op終了するため、鮮度ゲート不要
- `fetch_meteor_tles()`: `needs_update(norad, max_age_hours=24.0)`による衛星ごとの内部判定を
  既に持つ（対象は`METEOR_NORAD_IDS`の固定9機のみ）
- `AmsatFetcher.is_stale()`: 実装当初から`app_settings`のタイムスタンプで正しく経過時間判定
- `_refresh_satellite_names_periodic()`（衛星名の6時間ごと再同期）: `sync_satellite_names()`は
  ページネーションされた一括APIコールで、衛星ごとの個別問い合わせではないため、無条件に
  毎回実行しても実害が小さい設計として意図的にゲートなし

### DB マイグレーション注意事項（2026-05-29 バグ対応済み）

`tle_data` テーブルの CHECK 制約変更時はテーブル再作成が必要（SQLite 制約）。
過去に `SELECT *` による列順序不一致でデータロスが発生した。

**現在の正しい実装**（`database.py _apply_migrations()`）：
- 列名を明示した `INSERT OR IGNORE INTO tle_data (col1, col2, ...) SELECT col1, col2, ...`
- `_tle_data_backup` テーブルが残存していれば（前回のマイグレーション中断の証拠）自動復旧
- `SELECT *` は絶対に使用しないこと

---

## 仮NORAD ID（90000番台）衛星のTLE・トランスポンダー管理

### 背景

SATNOGS は正式 NORAD ID が未確定の衛星に 90000 番台の仮 ID を割り振る。
これらは CelesTrak グループフェッチでは TLE が取得できず、位置が表示されない。

### TLE 取得方法（src/data/tle_manager.py）

SATNOGS TLE API エンドポイントを使用：
```
GET https://db.satnogs.org/api/tle/?norad_cat_id={fake_id}&format=json
```

このエンドポイントは仮 ID に対して以下の3種類のいずれかを返す：
| tle_source | line1 の NORAD | 意味 |
|---|---|---|
| Space-Track.org | 実 NORAD ID | SATNOGS が内部で実 ID を把握 |
| CelesTrak (supplemental) | 実 NORAD ID | CelesTrak 補完カタログで解決 |
| SatNOGS Team | 仮 ID | 独自生成TLE（精度低め・更新頻度低） |

`fetch_provisional_tles()` は `is_hidden=0 AND norad_cat_id >= 90000` の全衛星を対象に
このAPIを呼び出し、TLE を `source='satnogs'`, `tle_group='amateur'` として保存する。

- 起動時に `_refresh_satellite_names_sync()` 完了後に自動実行
- APScheduler で 12 時間ごとに定期更新
- `source='manual'` の TLE は絶対に上書きしない

### 仮ID→実ID 移行パイプライン（src/data/transmitter_manager.py）

`_run_migration_pipeline(fake_id, real_id)` — **冪等。何度呼んでも安全。**

実行される手順（各ステップはスキップ条件あり）：
1. 実 ID の satellites レコードを作成（なければ）
2. 実 ID の衛星名が `OBJECT *` / `#NNNNN` / `Satellite #NNNNN` 等のプレースホルダー（
   `_is_placeholder_name()`、src/data/transmitter_manager.py）なら SATNOGS 名で上書き。
   以前は `OBJECT *` と `#` 始まりしか認識せず、`sync_from_satnogs()` がトランスミッタの
   `description` をそのまま仮の衛星名として登録していたケース（例: "Mode U - CW"）が
   永遠に上書きされないバグがあった（2026-07-04 修正、詳細は「Comms Quick Panel 設計」
   セクションの「関連する副次修正」参照）
3. TLE を仮 ID → 実 ID へコピー（実 ID 側に manual TLE があればスキップ）
4. トランスミッタを仮 ID → 実 ID へ移行（実 ID 側に既存ならスキップ）
5. `is_favorite` を実 ID にコピー
6. 実 ID 衛星に `satnogs_source_id = fake_id` を記録
7. 仮 ID を `is_hidden = 2`（システム非表示）に設定

#### トリガー
| トリガー | 発火場所 |
|---|---|
| (A) SATNOGS 衛星 API で `norad_follow_id` が設定された | `sync_satellite_names()` |
| (B) SATNOGS TLE API が返す line1 の NORAD が仮 ID と異なる | `fetch_provisional_tles()` |

### `satnogs_source_id` によるシームレスなトランスポンダー同期

移行後も SATNOGS は仮 ID 側でトランスポンダーを管理し続けることがある。
`satellites.satnogs_source_id = fake_id` が設定された実 ID 衛星は、
`sync_from_satnogs()` 内で以下のルーティングが適用される：

```
SATNOGS API に対して satellite__norad_cat_id=fake_id でクエリ
→ 返ってきたトランスポンダーを norad_cat_id=real_id として保存
```

#### 未実装項目（必要性は低いが、将来的な実装を検討すべき）

| 項目 | 内容 |
|---|---|
| **トリガー(C)：GUI手動設定** | 「この衛星の実 NORAD ID は〇〇」とユーザーが GUI から手動指定する機能。トリガー(A)(B) で自動カバーできるケースがほとんどのため現時点では不要。 |
| **フォールバック検知** | SATNOGS 側がトランスポンダーデータを実 ID に移行した場合に `satnogs_source_id` を自動で NULL にリセットする機能。現状では設定されていても実害はなく、SATNOGS が `norad_follow_id` をトランスポンダーに設定した時点で自然に解決される。 |

### 超古い衛星（NORAD < 10000）の自動クリーンアップ（src/data/tle_manager.py）

`fetch_legacy_tles()` — **起動時一回限りのクリーンアップ（以降は高速 no-op）**

対象：`norad_cat_id < 10000 AND is_hidden=0 AND TLEなし` の衛星（最大 21 機）

```
CelesTrak に個別照会（CATNR={norad}&FORMAT=TLE）
  ┌─ TLE 返却あり → まだ軌道上に存在する
  │   source='celestrak', tle_group='legacy' として保存・表示継続
  └─ TLE 返却なし → 軌道離脱済みと判断
      is_hidden=2（システム非表示）に設定
```

- 2回目以降の起動では対象行が 0 件 → 即リターン（API 呼び出しなし）
- `_refresh_satellite_names_sync()` の末尾でプロビジョナルTLEフェッチの後に実行

### ORIGAMISAT-2（NORAD 68795 / 仮 ID 98325）の状態（2026-08-09 実態に合わせて訂正）

```
satellites(norad_cat_id=68795):
  is_hidden = 0             ← 表示中
  satnogs_source_id = NULL  ← 仮IDへのルーティングは既に不要（移行済み・実IDで直接解決できる）
  alt_names = ["JS1YRU", "FO-126"]
  TLE: source=satnogs, tle_group=amateur  ← fetch_active_tles() Phase 2で継続的に自動更新される

satellites(norad_cat_id=98325):
  is_hidden = 2          ← システム非表示
  transmitters = 0件     ← 全て 68795 に移行済み
```

**旧記述（`satnogs_source_id=98325`・`TLE: source=manual`）は誤り・古い状態のスナップショットだった**
（少なくとも2026-08-09時点のDBとは一致しない。いつからずれていたかは不明）。特に`source=manual`は
致命的な誤りで、実際にmanualだった場合はいかなる自動同期でも上書きされないため、本セクション上部で
詳述した「TLEが44日間更新されなかった」問題は`source=manual`の記述が正しければ発生しようがなかった
はずである。ドキュメントと実DBの乖離に気づかないまま「この衛星は最終状態にあり変更不要」と
誤って結論づけていたことが、今回の一連の調査が長引いた一因だった。

**教訓**: DB上の実際の状態を記録したメモは、時間が経つと（今回のように自動同期の挙動変化や
手動操作で）静かに陳腐化する。「この衛星はもう解決済みのはず」という記述を信じて調査をスキップ
するのではなく、疑わしい挙動が報告されたら実DBを直接クエリして前提を検証すること。

---

### SATNOGSトランスミッター status の全件取得（2026-07-11 実装）

#### 背景

`TransmitterManager.sync_from_satnogs()` は従来 `status=active` をAPIクエリに付けており、
SATNOGSが `inactive`/`invalid` と分類したトランスミッターはDBに一切保存されなかった。
実際に調査したところ（Ten-Koh 2 / NORAD 68261の事例）、SATNOGSの `status` は自動集計ではなく
コミュニティのレビュアーが手動でキュレーションする値（`reviewed`/`approved`/`reviewer` フィールド
を伴う）で、実運用と食い違うことがある（レビュー漏れにより、実際には動いていないトランスミッター
が `active` のまま、逆に動いているはずのものが `inactive` のままになっているケースを実例で確認
済み）。一方で**衛星レベルのdead/unknown判定（`satellites.status`・`is_hidden`）は不要な衛星を
一覧から隠すために引き続き重要**であり、この2つは意味が異なるため区別して扱う。

このため、**トランスミッター単位のSATNOGS `status` はDBへの取り込み可否には使わず**、
active/inactive/invalidすべてを取得してDBに保存し、表示側で状態に応じて出し分ける設計に変更した。

#### 実装

- `sync_from_satnogs()`: APIクエリから `status` パラメータを削除（invalidも含め全件取得）
- `transmitters.satnogs_status`列（新設）: SATNOGSの生の `status` 文字列をそのまま保存
  （manual/community由来の行は`NULL`）。既存の `alive`（0/1、`status=='active'`と同義）は
  従来通り維持
- **デフォルトの表示は変更なし**: `get_transmitters()`（デフォルト`include_dead=False`）・
  Edit Transmitterダイアログ・Autotrackリスト検索・Comms Quick Panelは引き続き`alive=1`
  のみを対象にしており、この変更による見た目の影響はない
- **Radio Controlタブのトランスポンダーコンボのみ例外**: `MainWindow._refresh_radio_control()`
  の生SQLから`AND alive = 1`条件を外し、代わりに`ORDER BY`の先頭に
  `CASE WHEN alive=1 THEN 0 WHEN satnogs_status='invalid' THEN 2 ELSE 1 END`を追加して
  active優先ソートを維持（自動選択されるデフォルト項目が非activeにならないようにするため）。
  プルダウンを開いたときのみ、非active項目に背景色を付けて注意喚起する
  （`RadioControlWidget._xpdr_status_bg()` / `_XPDR_INACTIVE_BG`=`#b8860b`ダークゴールデンロッド
  ／`_XPDR_INVALID_BG`=`#8b0000`ダークレッド。閉じた状態のコンボ表示にはQtの仕様上、
  背景色は適用されない）。この2色は衛星リストの既存色（`#f1c40f`=AMSAT partial、
  `#e74c3c`=AMSAT non-operational）や`Qt.GlobalColor.yellow`（FT4タブの自局宛メッセージ
  ハイライト）と意図的に別トーンにしてある
- **スマホWeb UI（Antennaタブ）**: 元々`GET /api/satellites/{norad}/transmitters`自体には
  フィルタが無かったが、JS側（`index.html`）で`xpdrs.filter(x => x.alive)`により
  非activeを隠していた。このフィルタを削除し、色分けはせず全件そのまま表示（ユーザー判断で
  スマホ側は無色のまま）。APIのSQLに`ORDER BY alive DESC, description`を追加し、
  自動選択される最初のカードが極力activeなものになるようにした
- `_mobile_rig_connect()`・Autotrackの衛星切替（main_window.py）は`get_transmitters(...,
  include_dead=True)`に変更。これらは既にUUIDが判明した状態でトランスポンダーを検索するため、
  スマホ側が選択した非activeなuuidも正しく解決できる必要がある

#### Qt Rich Textの落とし穴（色凡例ダイアログ実装時に発覚）

Help > Satellite/Transmitter Colors ダイアログ（`MainWindow._on_satellite_color()`）に
上記2色の凡例行を追加した際、QLabelのリッチテキスト（QTextDocument）は**空の`<span>`に対する
`width`/`height`/`display:inline-block`を無視し、`background`（ショートハンド）も解釈しない**
ことが判明した。既存の衛星リスト用スウォッチも実は同じ理由で描画されていなかったが、
ラベル文字自体に`color`が付いていたため気づかれていなかった（新設したトランスミッター行は
文字を黒のままにしてスウォッチだけに頼っていたため、色が全く出ないというかたちで発覚した）。

**正しい実装**: `background-color`（ショートハンドではなく）を使い、`<span>`に`&nbsp;`等の
実コンテンツを持たせる。

```python
# NG: Qtでは何も描画されない
f'<span style="display:inline-block; width:14px; height:14px; background:{color};"></span>'

# OK: 実コンテンツ + background-color なら塗りつぶし四角として描画される
f'<span style="background-color:{color}; border:1px solid #555;">&nbsp;&nbsp;&nbsp;&nbsp;</span>'
```

`_on_satellite_color()`内の`_swatch()`ヘルパーをこの方式に修正済み（衛星リスト側のスウォッチも
副次的に正しく描画されるようになった）。QLabel/QTextDocumentで色見本・バッジ的なUIを作る際は
毎回この制約に注意すること。

### SATNOGS衛星 `status` 語彙の変更（`alive` → `in orbit`）と大量 `unknown` 化バグ（2026-09-01 修正）

#### 症状

衛星リストで大量の衛星（METEOR / NOAA / Metop を含む軌道上の衛星ほぼ全て、実測 ~1700 件）が
突然グレー（`status='unknown'`）表示になった。AMSAT 運用状況を持つアマチュア衛星は
`amsat_status` 由来の緑/黄/赤が優先されるため目立たないが、AMSAT 非掲載の気象衛星などは
色の根拠が `satellites.status` しか無いため一斉にグレー落ちする。

#### 原因

SATNOGS が `/api/satellites/` の `status` フィールドの語彙を刷新した。運用判断を持たなくなり、
`alive` は `in orbit` に改名、`dead` は廃止（軌道上にある限り `in orbit`。減衰予測日は別途
`decayed` 日付フィールド、受信実績は `reception_status` が担う）。現在の値は
`in orbit` / `re-entered` / `future` の3種類のみ。

`_SATNOGS_STATUS_MAP`（`src/data/transmitter_manager.py`、2026-05-21 実装）は
`alive`/`dead`/`re-entered`/`future` しか知らず、`sync_satellite_names()` の
`_SATNOGS_STATUS_MAP.get(raw, "unknown")` フォールバックにより **未知の `in orbit` が
すべて `unknown` に変換され**、8/30 前後の名前同期で軌道上の全衛星の `satellites.status` が
`unknown` に上書きされた。グレー表示に加え、[TLE なし衛星の自動非表示ルール](#tle-なし衛星の自動非表示ルール)
の「`status='unknown'` → 即時 `is_hidden=2`」経路にも乗ってしまう危険があった。

#### 修正（`fix(data): map SATNOGS 'in orbit' status + repair corrupted DB`）

1. `_SATNOGS_STATUS_MAP` に `"in orbit": "alive"` を追加。`in orbit` は厳密には運用中を
   意味しないが、旧 `alive` も同程度に緩く、実運用判断は `amsat_status` が担うため従来挙動に戻す。
2. `sync_satellite_names()` のフォールバック堅牢化: **未知の raw status を受け取ったとき、
   既存行が `alive`/`dead` なら上書きせず維持**（`logger.warning` を出す）。将来 SATNOGS が
   再び語彙を変えても大量破損しない。
3. `database.py _apply_migrations()` に**一度だけ実行される DB 修復** `_repair_satnogs_in_orbit_status()`
   を追加（`app_settings` の `db_repair_satnogs_in_orbit_v1` マーカーで冪等化）。
   - 対象: `status='unknown'` かつ `norad_cat_id < 90000` かつ `tle_data` に行あり かつ
     名前がプレースホルダでない（`future` 衛星・仮ID・正体不明物体を除外するヒューリスティック）
   - 処理: `status='alive'` に戻し、その行が `is_hidden=2`（自動非表示）なら `is_hidden=0` に戻す
     （`is_hidden=1` のユーザー手動非表示は触らない ← `sync_satellite_names()` の既存 un-hide ロジックと同じ）
   - 併せて `sync_log` の `satnogs_names` 行を削除し、次回起動で `is_satellite_names_stale()` を
     True にして権威的な名前再同期を1回強制（ヒューリスティックの取りこぼし・誤判定を SATNOGS の
     値で最終補正）。ネットワーク不要で起動直後に効き、オフラインでも修復される。

### SATNOGS・CelesTrakに接続できない時はまず「自分のIPがファイアウォールでブロックされていないか」を疑うこと（2026-08-10 確定）

過去に「SATNOGSに繋がらない」「TLE更新を押しても何も起きない」という症状を複数回、
家庭内ネットワーク側の問題（Wi-Fiの不調・ISP側の障害等）と誤って判断したことがあったが、
実際にはいずれも**過度なアクセスによりCelesTrak/SATNOGS側のファイアウォールに自分の
グローバルIPをブロックされていただけ**だった可能性が高い（2026-08-10、`fetch_active_tles()`
のサーキットブレーカー実装時の調査で確定）。

CelesTrakは公式の利用ポリシー（`https://celestrak.org/usage-policy.php`）で、
**2時間以内に403/404等のHTTPエラーが50回を超えるとIP単位でファイアウォールブロックされる**
と明記している。旧`fetch_active_tles()`のPhase 2（衛星ごとの個別CATNR問い合わせ）は
この上限を考慮せず無制限にループしていたため、対象衛星が多い環境では容易にこの閾値を
超えてブロックを引き起こしていた（詳細は「fetch_active_tles() の2フェーズ設計」セクション
の`_ErrorCountBreaker`参照）。SATNOGS側も同様の挙動（過去に報告された404連発）が
確認されている。

**症状の見分け方**: ブロックされている場合、HTTPエラー（403等）が即座に返るとは限らず、
**接続要求自体がサイレントに破棄されタイムアウトする**ことがある（ファイアウォールでの
drop）。「DNSは解決できる」「他サイト（Google等）へは接続できる」のに
「CelesTrak/SATNOGSだけタイムアウトする」という切り分けができれば、家庭内ネットワークの
問題ではなくIPブロックの可能性が高いと判断してよい。

```bash
getent hosts celestrak.org          # DNS解決確認
curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 "https://www.google.com"  # 対照確認
```

ブロックは永続的ではなく一定時間後に自動解除されると考えられるが、正確な解除時間は
公式ドキュメントに明記されていない。ブロック中かどうかを確認する最も確実な方法は、
**別のネットワーク（4G回線等、別のグローバルIPを持つ経路）から同じ問い合わせを試す**こと。
同じWi-Fi/回線を使う全端末（PC・スマホ問わず）は通常同一のグローバルIPを共有するため、
一度ブロックされるとそのネットワーク上のどの端末からアクセスしても同じ症状が再現する。

アプリ側の対策（サーキットブレーカー・`active_tle_retry_after`永続化リトライ）は
「今後ブロックを引き起こさないようにする」「ブロックされた場合に自動で後で再開する」
ためのものであり、**既にブロックされている状態を解除する手段ではない**（ブロック解除は
CelesTrak/SATNOGS側の裁量）点に注意。

---
