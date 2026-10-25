/**
 * radar.js — Canvas API による極座標レーダー（スカイビュー）
 *
 * 使い方:
 *   const radar = new RadarView('myCanvas');
 *   radar.setTracks([
 *     {
 *       name: 'ISS', norad: 25544,
 *       azimuth: 45.0, elevation: 34.2, isVisible: true,
 *       track: [{az: 0, el: 0}, {az: 45, el: 34}, {az: 90, el: 20}],
 *       aosTime: '2026-05-10T12:00:00Z',
 *       losTime: '2026-05-10T12:10:00Z',
 *     },
 *   ]);
 *
 * コンパス連動（スマホ）:
 *   radar.setNorthUp(false);  // コンパス回転を有効化
 *   radar.setNorthUp(true);   // 北固定に戻す（デフォルト）
 *
 * 衛星タップ:
 *   radar.onSatClick(track => console.log(track.name));
 */

'use strict';

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/** 衛星の色リスト（複数衛星の色分けに使用） */
const SAT_COLORS = [
    '#e74c3c', '#3498db', '#2ecc71', '#f39c12',
    '#9b59b6', '#1abc9c', '#e67e22', '#34495e',
];

/** 同心円を引く仰角リスト */
const ELEVATION_RINGS = [0, 30, 60];

/** 方位ラベルと対応する方位角 */
const CARDINALS = [
    { label: 'N', az: 0 },
    { label: 'E', az: 90 },
    { label: 'S', az: 180 },
    { label: 'W', az: 270 },
];

// ---------------------------------------------------------------------------
// ユーティリティ
// ---------------------------------------------------------------------------

/**
 * 方位角・仰角をレーダー上の (x, y) に変換する。
 *
 * @param {number} az          - 方位角（度、北=0、東=90）
 * @param {number} el          - 仰角（度、0=地平線、90=天頂）
 * @param {number} cx          - 中心 X（ピクセル）
 * @param {number} cy          - 中心 Y（ピクセル）
 * @param {number} r           - 地平線円の半径（ピクセル）
 * @param {number} [rotDeg=0]  - レーダー全体の回転角（コンパス連動用）
 * @returns {{ x: number, y: number }}
 */
function azElToXY(az, el, cx, cy, r, rotDeg = 0) {
    const el2  = Math.max(0, Math.min(90, el));
    const d    = (90 - el2) / 90 * r;
    const azRad = (az - rotDeg) * Math.PI / 180;
    return {
        x: cx + d * Math.sin(azRad),
        y: cy - d * Math.cos(azRad),
    };
}

// ---------------------------------------------------------------------------
// RadarView クラス
// ---------------------------------------------------------------------------

class RadarView {
    /**
     * @param {string} canvasId - <canvas> 要素の id
     */
    constructor(canvasId) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) throw new Error(`canvas#${canvasId} not found`);
        /** @type {HTMLCanvasElement} */
        this._canvas = canvas;
        /** @type {CanvasRenderingContext2D} */
        this._ctx    = canvas.getContext('2d');

        /** @type {Array<Object>} */
        this._tracks  = [];
        this._rotDeg  = 0;       // コンパス由来の回転角
        this._northUp = true;    // true=北固定、false=コンパス連動
        this._compassAvailable = false;
        /** @type {Function|null} */
        this._satClickCb = null;

        this._setupCompass();
        this._setupClickHandler();
        this._startRenderLoop();
    }

    // ------------------------------------------------------------------ #
    // 公開 API
    // ------------------------------------------------------------------ #

    /**
     * 表示する衛星データ配列を設定する。
     * 各要素のフォーマット:
     *   { name, norad, azimuth, elevation, isVisible, track, aosTime, losTime }
     *   track: [{az, el}, ...]
     * @param {Array<Object>} tracks
     */
    setTracks(tracks) {
        this._tracks = tracks || [];
    }

    /**
     * コンパス連動 on/off を切り替える。
     * @param {boolean} northUp - true=北固定（デフォルト）
     */
    setNorthUp(northUp) {
        this._northUp = northUp;
        if (northUp) this._rotDeg = 0;
    }

    /**
     * 衛星クリック/タップ時のコールバックを登録する。
     * @param {Function} callback - track オブジェクトを受け取る関数
     */
    onSatClick(callback) {
        this._satClickCb = callback;
    }

    /** 手動で 1 フレーム描画する。 */
    render() {
        this._draw();
    }

    // ------------------------------------------------------------------ #
    // コンパス
    // ------------------------------------------------------------------ #

    _setupCompass() {
        if (typeof DeviceOrientationEvent === 'undefined') return;

        const handler = (/** @type {DeviceOrientationEvent} */ e) => {
            if (this._northUp) return;
            // iOS: webkitCompassHeading (0=North, clockwise)
            // Android: alpha is degrees from north (counter-clockwise), invert it
            const heading = e.webkitCompassHeading != null
                ? e.webkitCompassHeading
                : (360 - (e.alpha ?? 0)) % 360;
            this._rotDeg = heading;
            this._compassAvailable = true;
        };

        // iOS 13+ requires explicit permission (requested via the Compass button in index.html).
        // For non-iOS browsers just register the listener unconditionally.
        if (typeof DeviceOrientationEvent.requestPermission !== 'function') {
            window.addEventListener('deviceorientation', handler, true);
        } else {
            // Store handler so it can be registered after permission is granted
            this._compassHandler = handler;
        }
    }

    /**
     * Register the deviceorientation listener after iOS permission has been granted.
     * Called by index.html after DeviceOrientationEvent.requestPermission() succeeds.
     */
    activateCompassListener() {
        if (this._compassHandler) {
            window.addEventListener('deviceorientation', this._compassHandler, true);
            this._compassAvailable = true;
        }
    }

    // ------------------------------------------------------------------ #
    // クリック・タッチ
    // ------------------------------------------------------------------ #

    _setupClickHandler() {
        this._canvas.addEventListener('click', (e) => {
            if (!this._satClickCb) return;
            const rect = this._canvas.getBoundingClientRect();
            const px   = e.clientX - rect.left;
            const py   = e.clientY - rect.top;
            const { cx, cy, r } = this._geometry();
            const rot = this._northUp ? 0 : this._rotDeg;

            // 上に描いたものを優先するため逆順に検索
            for (const track of [...this._tracks].reverse()) {
                const pos  = azElToXY(track.azimuth, track.elevation, cx, cy, r, rot);
                const dist = Math.hypot(px - pos.x, py - pos.y);
                if (dist <= 12) {
                    this._satClickCb(track);
                    return;
                }
            }
        });
    }

    // ------------------------------------------------------------------ #
    // レンダーループ
    // ------------------------------------------------------------------ #

    _startRenderLoop() {
        const loop = () => {
            this._draw();
            requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
    }

    // ------------------------------------------------------------------ #
    // 描画
    // ------------------------------------------------------------------ #

    _geometry() {
        const w = this._canvas.width;
        const h = this._canvas.height;
        const margin = 36;
        const r  = (Math.min(w, h - margin) - 24) / 2;
        const cx = w / 2;
        const cy = (h - margin) / 2 + 12;
        return { cx, cy, r: Math.max(r, 1) };
    }

    _draw() {
        const ctx = this._ctx;
        const { cx, cy, r } = this._geometry();
        const rot = this._northUp ? 0 : this._rotDeg;

        ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

        this._drawBackground(ctx, cx, cy, r);
        this._drawRings(ctx, cx, cy, r);
        this._drawCrosshairs(ctx, cx, cy, r, rot);
        this._drawCardinals(ctx, cx, cy, r, rot);

        this._tracks.forEach((track, idx) => {
            const color = SAT_COLORS[idx % SAT_COLORS.length];
            this._drawTrack(ctx, track, color, cx, cy, r, rot);
            this._drawSatellite(ctx, track, color, cx, cy, r, rot);
        });

        this._drawCompassIndicator(ctx);
        this._drawStatus(ctx, cx, cy, r);
    }

    _drawBackground(ctx, cx, cy, r) {
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.fillStyle = '#1a1a2e';
        ctx.fill();
        ctx.strokeStyle = '#4a4a6a';
        ctx.lineWidth = 2;
        ctx.stroke();
    }

    _drawRings(ctx, cx, cy, r) {
        for (const el of ELEVATION_RINGS) {
            const cr = (90 - el) / 90 * r;
            ctx.beginPath();
            ctx.arc(cx, cy, cr, 0, Math.PI * 2);
            ctx.strokeStyle = '#2c3e50';
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.stroke();
            ctx.setLineDash([]);
            // 仰角ラベル（右側）
            ctx.fillStyle = '#7f8c8d';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(`${el}°`, cx + cr + 2, cy + 4);
        }
    }

    _drawCrosshairs(ctx, cx, cy, r, rot) {
        const n = azElToXY(0,   0, cx, cy, r, rot);
        const s = azElToXY(180, 0, cx, cy, r, rot);
        const e = azElToXY(90,  0, cx, cy, r, rot);
        const w = azElToXY(270, 0, cx, cy, r, rot);
        ctx.strokeStyle = '#2c3e50';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(n.x, n.y); ctx.lineTo(s.x, s.y);
        ctx.moveTo(e.x, e.y); ctx.lineTo(w.x, w.y);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    _drawCardinals(ctx, cx, cy, r, rot) {
        ctx.font = 'bold 13px sans-serif';
        ctx.textBaseline = 'middle';
        for (const { label, az } of CARDINALS) {
            const pos = azElToXY(az, 0, cx, cy, r + 14, rot);
            ctx.fillStyle = label === 'N' ? '#e74c3c' : '#bdc3c7';
            ctx.textAlign = 'center';
            ctx.fillText(label, pos.x, pos.y);
        }
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
    }

    _drawTrack(ctx, track, color, cx, cy, r, rot) {
        const pts = track.track || [];
        if (pts.length < 2) return;

        const fmt = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

        // Cyan track line
        ctx.strokeStyle = '#00bcd4';
        ctx.lineWidth = 2;
        ctx.beginPath();
        pts.forEach((pt, i) => {
            const { x, y } = azElToXY(pt.az, pt.el, cx, cy, r, rot);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();

        // Green AOS dot
        const aosPos = azElToXY(pts[0].az, pts[0].el, cx, cy, r, rot);
        ctx.beginPath();
        ctx.arc(aosPos.x, aosPos.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#4caf50';
        ctx.fill();
        if (track.aosTime) {
            ctx.fillStyle = '#00bcd4';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(`AOS ${fmt(track.aosTime)}`, aosPos.x + 6, aosPos.y - 2);
        }

        // Red LOS dot
        const last = pts[pts.length - 1];
        const losPos = azElToXY(last.az, last.el, cx, cy, r, rot);
        ctx.beginPath();
        ctx.arc(losPos.x, losPos.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#f44336';
        ctx.fill();
        if (track.losTime) {
            ctx.fillStyle = '#00bcd4';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(`LOS ${fmt(track.losTime)}`, losPos.x + 6, losPos.y + 12);
        }
    }

    _drawSatellite(ctx, track, color, cx, cy, r, rot) {
        const { x, y } = azElToXY(track.azimuth, track.elevation, cx, cy, r, rot);
        const dotR = 6;

        // Current position: red hollow circle
        ctx.beginPath();
        ctx.arc(x, y, dotR, 0, Math.PI * 2);
        ctx.strokeStyle = '#f44336';
        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.fillStyle = color;
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(track.name, x + dotR + 3, y + 4);
    }

    _drawCompassIndicator(ctx) {
        if (!this._compassAvailable || this._northUp) return;
        ctx.fillStyle = '#7f8c8d';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(`↑ ${Math.round(this._rotDeg)}°`, 6, 16);
    }

    _drawStatus(ctx, cx, cy, r) {
        const visible = this._tracks.filter(t => t.isVisible);
        if (!visible.length) return;
        const text = visible
            .map(t => `${t.name}: EL ${t.elevation.toFixed(1)}°  AZ ${t.azimuth.toFixed(1)}°`)
            .join('  |  ');
        ctx.fillStyle = '#ecf0f1';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(text, this._canvas.width / 2, cy + r + 22);
        ctx.textAlign = 'left';
    }
}

// ---------------------------------------------------------------------------
// モジュールエクスポート（ESM / CommonJS 両対応）
// ---------------------------------------------------------------------------

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { RadarView, azElToXY, SAT_COLORS };
}
