/**
 * pass_chart.js — Chart.js によるパス予測グラフィカル表示
 *
 * 依存: Chart.js >= 4.x, chartjs-adapter-date-fns (時刻軸用)
 *
 * 使い方:
 *   // API から自動取得して描画
 *   fetchAndRenderPasses('myCanvas', 25544, 'ISS (ZARYA)');
 *
 *   // 取得済みデータを直接描画
 *   const passes = await fetch('/api/satellites/25544/passes').then(r => r.json());
 *   renderPassChart('myCanvas', passes, 'ISS (ZARYA)');
 */

'use strict';

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/** 品質ランクごとの色定義 */
const PASS_QUALITY_COLORS = {
    excellent: { border: 'rgba(46, 204, 113, 0.9)',  fill: 'rgba(46, 204, 113, 0.15)' },
    good:      { border: 'rgba(52, 152, 219, 0.9)',  fill: 'rgba(52, 152, 219, 0.15)' },
    fair:      { border: 'rgba(241, 196, 15, 0.9)',  fill: 'rgba(241, 196, 15, 0.15)' },
    low:       { border: 'rgba(149, 165, 166, 0.9)', fill: 'rgba(149, 165, 166, 0.15)' },
};

/** 仰角曲線の生成サンプル数（AOS→TCA・TCA→LOS それぞれ） */
const ELEVATION_SAMPLE_POINTS = 20;

// ---------------------------------------------------------------------------
// ユーティリティ
// ---------------------------------------------------------------------------

/**
 * 最大仰角からパスの品質ランクを返す。
 * @param {number} maxElevationDeg - 最大仰角（度）
 * @returns {'excellent'|'good'|'fair'|'low'}
 */
function getPassQuality(maxElevationDeg) {
    if (maxElevationDeg >= 60) return 'excellent';
    if (maxElevationDeg >= 30) return 'good';
    if (maxElevationDeg >= 10) return 'fair';
    return 'low';
}

/**
 * AOS・TCA・LOS からサイン近似の仰角点列を生成する。
 * @param {Object} pass - パスオブジェクト（aos, tca/max_elevation_time, los, max_elevation_deg）
 * @param {number} [nPoints=ELEVATION_SAMPLE_POINTS] - サンプル数
 * @returns {Array<{x: Date, y: number}>}
 */
function generateElevationPoints(pass, nPoints = ELEVATION_SAMPLE_POINTS) {
    const aosMs  = new Date(pass.aos).getTime();
    const tcaMs  = new Date(pass.max_elevation_time || pass.tca).getTime();
    const losMs  = new Date(pass.los).getTime();
    const maxEl  = pass.max_elevation_deg;
    const points = [];

    // AOS → TCA（sin 上昇カーブ）
    for (let i = 0; i < nPoints; i++) {
        const t  = i / nPoints;
        const el = maxEl * Math.sin(Math.PI * t / 2);
        points.push({ x: new Date(aosMs + t * (tcaMs - aosMs)), y: el });
    }

    // TCA（頂点）
    points.push({ x: new Date(tcaMs), y: maxEl });

    // TCA → LOS（cos 下降カーブ）
    for (let i = 1; i <= nPoints; i++) {
        const t  = i / nPoints;
        const el = maxEl * Math.cos(Math.PI * t / 2);
        points.push({ x: new Date(tcaMs + t * (losMs - tcaMs)), y: el });
    }

    return points;
}

/**
 * パス詳細テキストを生成する（HTML 文字列）。
 * @param {Object} pass - パスオブジェクト
 * @returns {string} HTML 文字列
 */
function buildPassDetailHTML(pass) {
    const durationSec = Math.round(pass.duration_seconds ?? pass.duration_s ?? 0);
    const mins = Math.floor(durationSec / 60);
    const secs = durationSec % 60;
    const quality = getPassQuality(pass.max_elevation_deg);
    const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';

    return `
        <table class="pass-detail-table">
            <tr><th>AOS</th><td>${fmtTime(pass.aos)}</td></tr>
            <tr><th>Time of max elevation</th><td>${fmtTime(pass.max_elevation_time || pass.tca)}</td></tr>
            <tr><th>LOS</th><td>${fmtTime(pass.los)}</td></tr>
            <tr><th>Max elevation</th><td>${pass.max_elevation_deg.toFixed(1)}° <span class="quality-badge quality-${quality}">${quality}</span></td></tr>
            <tr><th>Duration</th><td>${mins} min ${secs} s</td></tr>
            <tr><th>AOS azimuth</th><td>${pass.aos_azimuth_deg.toFixed(1)}°</td></tr>
            <tr><th>LOS azimuth</th><td>${pass.los_azimuth_deg.toFixed(1)}°</td></tr>
        </table>
    `.trim();
}

// ---------------------------------------------------------------------------
// チャート描画
// ---------------------------------------------------------------------------

/**
 * パス一覧からチャートを描画する。
 *
 * @param {string}   canvasId  - <canvas> 要素の id
 * @param {Object[]} passes    - API /api/satellites/{norad}/passes のレスポンス配列
 * @param {string}   [satName=''] - 衛星名（タイトル・凡例に使用）
 * @returns {Chart|null} Chart.js インスタンス（パスが空の場合 null）
 */
function renderPassChart(canvasId, passes, satName = '') {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
        console.error(`renderPassChart: canvas#${canvasId} not found`);
        return null;
    }

    // 既存チャートを破棄
    const existing = Chart.getChart(canvas);
    if (existing) existing.destroy();

    if (!passes || passes.length === 0) {
        canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
        return null;
    }

    const datasets = [];

    // パス曲線
    passes.forEach((pass, idx) => {
        const quality = getPassQuality(pass.max_elevation_deg);
        const colors  = PASS_QUALITY_COLORS[quality];
        const label   = satName
            ? `${satName} #${idx + 1} max ${pass.max_elevation_deg.toFixed(1)}° (${quality})`
            : `Pass #${idx + 1} max ${pass.max_elevation_deg.toFixed(1)}° (${quality})`;

        datasets.push({
            label,
            data:            generateElevationPoints(pass),
            borderColor:     colors.border,
            backgroundColor: colors.fill,
            borderWidth:     2,
            fill:            true,
            tension:         0.4,
            pointRadius:     0,
            pointHitRadius:  8,
            passIndex:       idx,   // クリックイベントで使用
        });
    });

    // 現在時刻ライン
    const now = new Date();
    datasets.push({
        label:           'Current time',
        data:            [{ x: now, y: 0 }, { x: now, y: 90 }],
        borderColor:     'rgba(231, 76, 60, 0.9)',
        backgroundColor: 'transparent',
        borderWidth:     2,
        borderDash:      [6, 4],
        fill:            false,
        tension:         0,
        pointRadius:     0,
        passIndex:       null,
    });

    const chart = new Chart(canvas, {
        type: 'line',
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            interaction: { mode: 'nearest', intersect: false },
            plugins: {
                title: {
                    display: true,
                    text:    satName ? `${satName} pass prediction` : 'Pass prediction',
                    font:    { size: 16 },
                },
                legend: { position: 'bottom' },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `Elevation: ${ctx.parsed.y.toFixed(1)}°`,
                        title: (items) => {
                            const d = items[0]?.parsed?.x;
                            return d ? new Date(d).toLocaleTimeString() : '';
                        },
                    },
                },
            },
            scales: {
                x: {
                    type:  'time',
                    time:  {
                        unit:           'minute',
                        displayFormats: { minute: 'HH:mm' },
                        tooltipFormat:  'HH:mm:ss',
                    },
                    title: { display: true, text: 'Time (UTC)' },
                },
                y: {
                    min:   0,
                    max:   90,
                    title: { display: true, text: 'Elevation (deg)' },
                    ticks: { callback: (v) => `${v}°` },
                },
            },
            onClick: (event, elements) => {
                if (!elements.length) return;
                const el        = elements[0];
                const dataset   = chart.data.datasets[el.datasetIndex];
                const passIndex = dataset.passIndex;
                if (passIndex != null) {
                    showPassDetail(passes[passIndex]);
                    canvas.dispatchEvent(new CustomEvent('passclick', {
                        detail: passes[passIndex], bubbles: true,
                    }));
                }
            },
        },
    });

    return chart;
}

// ---------------------------------------------------------------------------
// パス詳細表示
// ---------------------------------------------------------------------------

/**
 * パス詳細を #pass-detail 要素に表示する。
 * 要素が存在しない場合は console.info に出力するだけ。
 *
 * @param {Object} pass - パスオブジェクト
 */
function showPassDetail(pass) {
    const detail = document.getElementById('pass-detail');
    if (!detail) {
        console.info('showPassDetail: pass =', pass);
        return;
    }
    detail.innerHTML = buildPassDetailHTML(pass);
    detail.style.display = 'block';
}

// ---------------------------------------------------------------------------
// API 連携
// ---------------------------------------------------------------------------

/**
 * API からパスを取得してチャートを描画する。
 *
 * @param {string} canvasId  - <canvas> 要素の id
 * @param {number} noradId   - NORAD カタログ番号
 * @param {string} satName   - 衛星名（タイトル表示用）
 * @param {Object} [options] - オプション
 * @param {number} [options.hours=24]   - 予測時間幅（時間）
 * @param {number} [options.minEl=5]    - 最低仰角（度）
 * @param {string} [options.apiBase=''] - API ベース URL
 * @returns {Promise<Chart|null>}
 */
async function fetchAndRenderPasses(canvasId, noradId, satName, options = {}) {
    const hours   = options.hours   ?? 24;
    const minEl   = options.minEl   ?? 5;
    const apiBase = options.apiBase ?? '';
    const url     = `${apiBase}/api/satellites/${noradId}/passes?hours=${hours}&min_el=${minEl}`;

    try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        const passes = await resp.json();
        return renderPassChart(canvasId, passes, satName);
    } catch (err) {
        console.error('fetchAndRenderPasses failed:', err);
        return null;
    }
}

// ---------------------------------------------------------------------------
// モジュールエクスポート（ESM / CommonJS 両対応）
// ---------------------------------------------------------------------------

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getPassQuality,
        generateElevationPoints,
        buildPassDetailHTML,
        renderPassChart,
        showPassDetail,
        fetchAndRenderPasses,
        PASS_QUALITY_COLORS,
    };
}
