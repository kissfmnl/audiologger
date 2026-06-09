(function () {
    const modal = document.getElementById("player-modal");
    const panel = document.getElementById("player-modal-panel");
    const closeBtn = document.getElementById("player-modal-close");
    const titleEl = document.getElementById("player-modal-title");
    const subtitleEl = document.getElementById("player-modal-subtitle");
    const canvas = document.getElementById("player-modal-canvas");
    const overviewCanvas = document.getElementById("player-modal-overview");
    const loadingEl = document.getElementById("player-modal-loading");
    const controlsEl = document.getElementById("player-modal-controls");
    const playBtn = document.getElementById("player-modal-play");
    const playIcon = document.getElementById("player-modal-play-icon");
    const pauseIcon = document.getElementById("player-modal-pause-icon");
    const currentTimeEl = document.getElementById("player-modal-current");
    const totalTimeEl = document.getElementById("player-modal-total");
    const trimSection = document.getElementById("player-modal-trim-section");
    const selStartBtn = document.getElementById("player-modal-sel-start-btn");
    const selEndBtn = document.getElementById("player-modal-sel-end-btn");
    const downloadBtn = document.getElementById("player-modal-download-btn");
    const selStartEl = document.getElementById("player-modal-sel-start");
    const selEndEl = document.getElementById("player-modal-sel-end");
    const trimStatusEl = document.getElementById("player-modal-trim-status");
    const zoomSlider = document.getElementById("player-modal-zoom");
    const zoomLabel = document.getElementById("player-modal-zoom-label");
    const panWrap = document.getElementById("player-modal-pan-wrap");
    const panSlider = document.getElementById("player-modal-pan");

    if (!modal || !canvas) {
        return;
    }

    const ctx = canvas.getContext("2d");
    const overviewCtx = overviewCanvas ? overviewCanvas.getContext("2d") : null;
    const peaksCache = new Map();
    const peaksInflight = new Map();
    const TARGET_VISIBLE_SECONDS = 120;
    const TIME_AXIS_HEIGHT_CSS = 18;
    const ZOOM_SLIDER_STEPS = 400;
    const WHEEL_ZOOM_SENSITIVITY = 0.007;
    const PEAKS_UPSAMPLE_MIN = 8192;
    const KEYBOARD_ZOOM_FACTOR = 0.54;
    const REGION_DETAIL_THRESHOLD_SEC = 480;
    const REGION_MAX_BARS = 8192;
    const DRAG_SELECT_THRESHOLD_PX = 5;
    const PAN_STEPS = 1000;
    const WAVEFORM_WAIT_SEC = 3;
    const SELECTION_DEFAULT_SEC = 10;
    const WAVE_COLOR_IDLE = "#C4B5FD";
    const WAVE_COLOR_PLAYED = "#7C3AED";
    const WAVE_COLOR_CURSOR = "#6D28D9";
    const countdownEl = document.getElementById("player-modal-countdown");
    const countdownRing = document.getElementById("player-modal-countdown-ring");
    const COUNTDOWN_CIRC = 163.4;

    const bootstrapEl = document.getElementById("peaks-bootstrap");
    if (bootstrapEl) {
        try {
            const bootstrap = JSON.parse(bootstrapEl.textContent || "{}");
            Object.entries(bootstrap).forEach(([url, data]) => {
                peaksCache.set(url, { ...(peaksCache.get(url) || {}), ...data });
            });
        } catch {
            // ignore invalid bootstrap JSON
        }
    }
    seedPeaksCacheFromButtons();

    let audio = null;
    let activeButton = null;
    let peaks = [];
    let detailPeaks = [];
    let detailPeaksMeta = null;
    let detailFetchTimer = null;
    let detailFetchGeneration = 0;
    const detailPeaksCache = new Map();
    let duration = 0;
    let rafId = null;
    let peaksUrl = "";
    let viewSpan = 1;
    let viewStart = 0;
    let lastFocusInView = 0.5;
    let pointerOverCanvas = false;
    let syncingZoomSlider = false;
    let dragSelect = null;
    let selectionRegion = null;
    let currentRecordingId = null;
    let canTrim = false;
    let clipMeta = { stationSlug: "", date: "", hour: "00" };

    function parseInlinePeaks(raw) {
        if (!raw) {
            return null;
        }
        const peaks = raw.split(",").map((value) => Number(value.trim())).filter((value) => Number.isFinite(value));
        return peaks.length ? peaks : null;
    }

    function seedPeaksCacheFromButtons() {
        document.querySelectorAll(".listen-btn").forEach((button) => {
            const url = button.dataset.peaksUrl;
            const inlinePeaks = parseInlinePeaks(button.dataset.peaks);
            if (!url || !inlinePeaks) {
                return;
            }
            peaksCache.set(url, {
                peaks: inlinePeaks,
                duration: Number(button.dataset.duration) || 3600,
                precise: true,
                ready: true,
                audio_url: button.dataset.audioUrl,
                title: button.dataset.title,
            });
        });
    }

    function formatTime(seconds) {
        const total = Math.max(0, Math.floor(seconds || 0));
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const secs = total % 60;
        if (hours > 0) {
            return `${hours}:${minutes.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
        }
        return `${minutes}:${secs.toString().padStart(2, "0")}`;
    }

    function sanitizeFilenamePart(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9._-]+/g, "-")
            .replace(/^-+|-+$/g, "") || "opname";
    }

    function buildClipFilename() {
        const station = sanitizeFilenamePart(clipMeta.stationSlug);
        const date = clipMeta.date || "datum";
        const hour = String(clipMeta.hour ?? 0).padStart(2, "0");
        return `${station}-${date}-clip-${hour}.mp3`;
    }

    function getTimeAxisHeight() {
        return Math.round(TIME_AXIS_HEIGHT_CSS * (window.devicePixelRatio || 1));
    }

    function getWaveHeight(canvasHeight) {
        return Math.max(1, canvasHeight - getTimeAxisHeight());
    }

    function timeAxisStep(visibleSeconds) {
        if (visibleSeconds <= 150) {
            return 10;
        }
        if (visibleSeconds <= 5 * 60) {
            return 30;
        }
        if (visibleSeconds <= 15 * 60) {
            return 60;
        }
        if (visibleSeconds <= 30 * 60) {
            return 120;
        }
        if (visibleSeconds <= 60 * 60) {
            return 300;
        }
        return 600;
    }

    function getMinViewSpan() {
        if (!duration) {
            return 1;
        }
        return Math.min(1, TARGET_VISIBLE_SECONDS / duration);
    }

    function refreshZoomLimits() {
        const minSpan = getMinViewSpan();
        if (viewSpan < minSpan) {
            setViewSpan(minSpan, lastFocusInView);
        } else {
            updateZoomUi();
        }
    }

    function getViewSpan() {
        return viewSpan;
    }

    function clampViewStart(start) {
        const span = getViewSpan();
        return Math.min(Math.max(0, start), Math.max(0, 1 - span));
    }

    function formatVisibleWindow(seconds) {
        const total = Math.max(1, Math.round(seconds || 0));
        if (total >= 3600) {
            return "1 uur";
        }
        if (total >= 60) {
            const mins = Math.floor(total / 60);
            const secs = total % 60;
            return secs ? `${mins}:${secs.toString().padStart(2, "0")}` : `${mins} min`;
        }
        return `${total} sec`;
    }

    function spanToSliderValue(span) {
        const minSpan = getMinViewSpan();
        if (span >= 1) {
            return ZOOM_SLIDER_STEPS;
        }
        const ratio = Math.log(span / minSpan) / Math.log(1 / minSpan);
        return Math.round(Math.min(ZOOM_SLIDER_STEPS, Math.max(0, ratio * ZOOM_SLIDER_STEPS)));
    }

    function sliderValueToSpan(value) {
        const minSpan = getMinViewSpan();
        if (value >= ZOOM_SLIDER_STEPS) {
            return 1;
        }
        const ratio = value / ZOOM_SLIDER_STEPS;
        return minSpan * Math.pow(1 / minSpan, ratio);
    }

    function updateZoomUi() {
        if (zoomLabel) {
            zoomLabel.textContent = formatVisibleWindow(duration * viewSpan);
        }
        if (zoomSlider) {
            syncingZoomSlider = true;
            zoomSlider.value = String(spanToSliderValue(viewSpan));
            syncingZoomSlider = false;
        }
        updatePanSlider();
    }

    function updatePanSlider() {
        if (!panWrap || !panSlider) {
            return;
        }
        if (viewSpan >= 1 - 1e-9) {
            panWrap.classList.add("hidden");
            return;
        }
        panWrap.classList.remove("hidden");
        const maxStart = Math.max(0, 1 - getViewSpan());
        if (maxStart <= 0) {
            panSlider.value = "0";
            return;
        }
        panSlider.value = String(Math.round((viewStart / maxStart) * PAN_STEPS));
    }

    function setViewStart(start) {
        viewStart = clampViewStart(start);
        updatePanSlider();
        scheduleDetailPeaksFetch();
        drawWaveform();
    }

    function setViewSpan(newSpan, focusInView = lastFocusInView) {
        const minSpan = getMinViewSpan();
        const oldSpan = viewSpan;
        const focusPoint = viewStart + focusInView * oldSpan;
        viewSpan = Math.min(1, Math.max(minSpan, newSpan));
        if (viewSpan >= 1 - 1e-9) {
            viewSpan = 1;
            viewStart = 0;
        } else {
            viewStart = clampViewStart(focusPoint - focusInView * viewSpan);
        }
        lastFocusInView = focusInView;
        updateZoomUi();
        scheduleDetailPeaksFetch();
        drawWaveform();
    }

    function resetZoom() {
        viewSpan = 1;
        viewStart = 0;
        clearDetailPeaks();
        updateZoomUi();
        drawWaveform();
    }

    function zoomIn(focusInView) {
        setViewSpan(viewSpan * KEYBOARD_ZOOM_FACTOR, focusInView ?? lastFocusInView);
    }

    function zoomOut(focusInView) {
        setViewSpan(viewSpan / KEYBOARD_ZOOM_FACTOR, focusInView ?? lastFocusInView);
    }

    function normalizeWheelDelta(event) {
        if (event.deltaMode === 1) {
            return event.deltaY * 16;
        }
        if (event.deltaMode === 2) {
            return event.deltaY * window.innerHeight;
        }
        return event.deltaY;
    }

    function resizeCanvas() {
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.floor(rect.width * dpr));
        canvas.height = Math.max(1, Math.floor(rect.height * dpr));
        if (overviewCanvas && overviewCtx) {
            const overviewRect = overviewCanvas.getBoundingClientRect();
            overviewCanvas.width = Math.max(1, Math.floor(overviewRect.width * dpr));
            overviewCanvas.height = Math.max(1, Math.floor(overviewRect.height * dpr));
        }
        drawWaveform();
        scheduleDetailPeaksFetch();
    }

    function timeToRatio(time) {
        return duration > 0 ? time / duration : 0;
    }

    function ratioToCanvasX(ratio) {
        const span = getViewSpan();
        return ((ratio - viewStart) / span) * canvas.width;
    }

    function shouldUseDetailPeaks() {
        return duration > 0 && duration * viewSpan <= REGION_DETAIL_THRESHOLD_SEC && viewSpan < 1 - 1e-9;
    }

    function detailPeaksMatchView() {
        if (!detailPeaksMeta || !detailPeaks.length) {
            return false;
        }
        return Math.abs(detailPeaksMeta.start - viewStart) < 1e-5
            && Math.abs(detailPeaksMeta.span - viewSpan) < 1e-5;
    }

    function clearDetailPeaks() {
        detailPeaks = [];
        detailPeaksMeta = null;
        if (detailFetchTimer) {
            clearTimeout(detailFetchTimer);
            detailFetchTimer = null;
        }
    }

    function regionPeaksRequestUrl(start, span, bars) {
        const separator = peaksUrl.includes("?") ? "&" : "?";
        return `${peaksUrl}${separator}region_start=${start.toFixed(6)}&region_span=${span.toFixed(6)}&bars=${bars}`;
    }

    function scheduleDetailPeaksFetch() {
        if (detailFetchTimer) {
            clearTimeout(detailFetchTimer);
        }
        if (!shouldUseDetailPeaks() || !peaksUrl) {
            clearDetailPeaks();
            return;
        }
        detailFetchTimer = setTimeout(() => {
            detailFetchTimer = null;
            fetchDetailPeaks();
        }, 70);
    }

    async function fetchDetailPeaks() {
        if (!shouldUseDetailPeaks() || !peaksUrl) {
            clearDetailPeaks();
            return;
        }

        const requestStart = viewStart;
        const requestSpan = viewSpan;
        const bars = Math.min(REGION_MAX_BARS, Math.max(1024, Math.ceil(canvas.width * 3)));
        const cacheKey = `${peaksUrl}:${requestStart.toFixed(6)}:${requestSpan.toFixed(6)}:${bars}`;
        const generation = ++detailFetchGeneration;

        if (detailPeaksCache.has(cacheKey)) {
            detailPeaks = detailPeaksCache.get(cacheKey);
            detailPeaksMeta = { start: requestStart, span: requestSpan };
            drawWaveform();
            return;
        }

        try {
            const response = await fetch(regionPeaksRequestUrl(requestStart, requestSpan, bars));
            if (!response.ok) {
                throw new Error("Detail wavevorm laden mislukt");
            }
            const data = await response.json();
            if (generation !== detailFetchGeneration) {
                return;
            }
            if (Math.abs(viewStart - requestStart) > 1e-5 || Math.abs(viewSpan - requestSpan) > 1e-5) {
                return;
            }
            const values = peakValues(data);
            if (!values.length) {
                return;
            }
            detailPeaks = values;
            detailPeaksMeta = { start: requestStart, span: requestSpan };
            detailPeaksCache.set(cacheKey, values);
            drawWaveform();
        } catch {
            // keep overview peaks until detail loads
        }
    }

    function detailPeaksToCanvas(source, width) {
        const samples = new Array(width);
        const srcLen = source.length;
        if (!srcLen) {
            return samples;
        }
        for (let x = 0; x < width; x += 1) {
            const from = Math.floor((x / width) * srcLen);
            const to = Math.max(from + 1, Math.ceil(((x + 1) / width) * srcLen));
            let max = 0;
            for (let i = from; i < to; i += 1) {
                max = Math.max(max, source[i] || 0);
            }
            samples[x] = max;
        }
        return samples;
    }

    function upsamplePeaks(source) {
        if (!source.length) {
            return [];
        }
        const targetLen = Math.max(PEAKS_UPSAMPLE_MIN, source.length * 16);
        if (source.length >= targetLen) {
            return source.slice();
        }
        const out = new Array(targetLen);
        for (let i = 0; i < targetLen; i += 1) {
            const pos = (i / (targetLen - 1)) * (source.length - 1);
            const i0 = Math.floor(pos);
            const i1 = Math.min(source.length - 1, i0 + 1);
            const t = pos - i0;
            out[i] = source[i0] * (1 - t) + source[i1] * t;
        }
        return out;
    }

    function peakAtRatio(ratio) {
        if (!peaks.length) {
            return 0;
        }
        const clamped = Math.min(1, Math.max(0, ratio));
        const pos = clamped * (peaks.length - 1);
        const i0 = Math.floor(pos);
        const i1 = Math.min(peaks.length - 1, i0 + 1);
        const t = pos - i0;
        return peaks[i0] * (1 - t) + peaks[i1] * t;
    }

    function getVisiblePeaks(width) {
        if (!peaks.length) {
            return [];
        }
        const span = getViewSpan();
        const samples = new Array(width);
        const sliceWidth = span / width;

        for (let x = 0; x < width; x += 1) {
            const center = viewStart + ((x + 0.5) / width) * span;
            let max = 0;
            const steps = Math.max(4, Math.ceil(sliceWidth * peaks.length * 5));
            for (let s = 0; s < steps; s += 1) {
                const ratio = center + (s / (steps - 1) - 0.5) * sliceWidth;
                max = Math.max(max, peakAtRatio(ratio));
            }
            samples[x] = max;
        }
        return samples;
    }

    function drawFilledWave(samples, width, height, mid, color, clipEndX) {
        if (!samples.length) {
            return;
        }

        ctx.save();
        if (clipEndX !== null) {
            ctx.beginPath();
            ctx.rect(0, 0, clipEndX, height);
            ctx.clip();
        }

        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(0, mid);
        for (let x = 0; x < width; x += 1) {
            const y = mid - samples[x] * mid * 0.92;
            if (x === 0) {
                ctx.lineTo(0, y);
            } else {
                const prevY = mid - samples[x - 1] * mid * 0.92;
                ctx.quadraticCurveTo(x - 0.35, prevY, x, y);
            }
        }
        for (let x = width - 1; x >= 0; x -= 1) {
            const y = mid + samples[x] * mid * 0.92;
            if (x === width - 1) {
                ctx.lineTo(x, y);
            } else {
                const prevY = mid + samples[x + 1] * mid * 0.92;
                ctx.quadraticCurveTo(x + 0.35, prevY, x, y);
            }
        }
        ctx.closePath();
        ctx.fill();
        ctx.restore();
    }

    function drawMinMaxWave(samples, width, height, mid, color, clipEndX) {
        if (!samples.length) {
            return;
        }

        ctx.save();
        if (clipEndX !== null) {
            ctx.beginPath();
            ctx.rect(0, 0, clipEndX, height);
            ctx.clip();
        }

        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = 0; x < width; x += 1) {
            const amp = samples[x] * mid * 0.98;
            const px = x + 0.5;
            ctx.moveTo(px, mid - amp);
            ctx.lineTo(px, mid + amp);
        }
        ctx.stroke();
        ctx.restore();
    }

    function drawWaveform() {
        const width = canvas.width;
        const height = canvas.height;
        const axisH = getTimeAxisHeight();
        const waveHeight = getWaveHeight(height);
        const mid = waveHeight / 2;
        ctx.clearRect(0, 0, width, height);

        if (!peaks.length) {
            ctx.fillStyle = "#E5E7EB";
            ctx.fillRect(0, mid - 1, width, 2);
            drawTimeAxis(width, height, axisH);
            drawOverview();
            return;
        }

        const useDetail = shouldUseDetailPeaks() && detailPeaksMatchView();
        const samples = useDetail
            ? detailPeaksToCanvas(detailPeaks, width)
            : getVisiblePeaks(width);
        const progress = timeToRatio(audio ? audio.currentTime : 0);
        const playheadX = ratioToCanvasX(progress);

        if (useDetail) {
            drawMinMaxWave(samples, width, waveHeight, mid, WAVE_COLOR_IDLE, null);
            if (playheadX > 0) {
                drawMinMaxWave(samples, width, waveHeight, mid, WAVE_COLOR_PLAYED, playheadX);
            }
        } else {
            drawFilledWave(samples, width, waveHeight, mid, WAVE_COLOR_IDLE, null);
            if (playheadX > 0) {
                drawFilledWave(samples, width, waveHeight, mid, WAVE_COLOR_PLAYED, playheadX);
            }
        }

        if (selectionRegion && duration > 0) {
            const startX = ratioToCanvasX(timeToRatio(selectionRegion.start));
            const endX = ratioToCanvasX(timeToRatio(selectionRegion.end));
            const left = Math.min(startX, endX);
            const selWidth = Math.abs(endX - startX);
            ctx.fillStyle = "rgba(124, 58, 237, 0.18)";
            ctx.fillRect(left, 0, selWidth, waveHeight);
            ctx.strokeStyle = WAVE_COLOR_PLAYED;
            ctx.lineWidth = 2;
            ctx.strokeRect(left + 0.5, 0.5, Math.max(0, selWidth - 1), waveHeight - 1);
        }

        if (playheadX >= 0 && playheadX <= width) {
            ctx.fillStyle = WAVE_COLOR_CURSOR;
            ctx.fillRect(Math.max(0, playheadX - 1), 0, 2, waveHeight);
        }

        drawTimeAxis(width, height, axisH);
        drawOverview();
    }

    function drawTimeAxis(width, height, axisH) {
        if (!duration) {
            return;
        }
        const span = getViewSpan();
        const visibleSeconds = duration * span;
        const step = timeAxisStep(visibleSeconds);
        const startSec = viewStart * duration;
        const endSec = startSec + visibleSeconds;
        const firstTick = Math.ceil(startSec / step) * step;
        const labelY = height - Math.round(axisH * 0.2);
        const tickTop = height - Math.round(axisH * 0.55);
        const tickBottom = height - Math.round(axisH * 0.25);

        ctx.save();
        ctx.fillStyle = "#E5E7EB";
        ctx.fillRect(0, height - axisH, width, 1);
        ctx.fillStyle = "#64748b";
        ctx.font = `${Math.max(8, Math.floor(width / 140))}px Inter, system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";

        for (let sec = firstTick; sec <= endSec; sec += step) {
            const ratio = sec / duration;
            const x = ratioToCanvasX(ratio);
            if (x < 0 || x > width) {
                continue;
            }
            ctx.fillRect(x, tickTop, 1, tickBottom - tickTop);
            const mins = Math.floor(sec / 60);
            const secs = Math.floor(sec % 60);
            ctx.fillText(`${mins}:${secs.toString().padStart(2, "0")}`, x, labelY);
        }
        ctx.restore();
    }

    function drawOverview() {
        if (!overviewCanvas || !overviewCtx || !peaks.length) {
            return;
        }

        const width = overviewCanvas.width;
        const height = overviewCanvas.height;
        const mid = height / 2;
        overviewCtx.clearRect(0, 0, width, height);

        const samples = getVisiblePeaksForRange(width, 0, 1);
        drawFilledWaveOnContext(overviewCtx, samples, width, height, mid, WAVE_COLOR_IDLE, null);

        const span = getViewSpan();
        const left = viewStart * width;
        const viewportWidth = Math.max(2, span * width);
        overviewCtx.fillStyle = "rgba(124, 58, 237, 0.12)";
        overviewCtx.fillRect(left, 0, viewportWidth, height);
        overviewCtx.strokeStyle = WAVE_COLOR_PLAYED;
        overviewCtx.lineWidth = 2;
        overviewCtx.strokeRect(left + 0.5, 0.5, Math.max(0, viewportWidth - 1), height - 1);

        if (audio && duration > 0) {
            const playX = timeToRatio(audio.currentTime) * width;
            overviewCtx.fillStyle = WAVE_COLOR_CURSOR;
            overviewCtx.fillRect(Math.max(0, playX - 1), 0, 2, height);
        }
    }

    function getVisiblePeaksForRange(width, rangeStart, rangeEnd) {
        if (!peaks.length) {
            return [];
        }
        const span = rangeEnd - rangeStart;
        const samples = new Array(width);
        const sliceWidth = span / width;
        for (let x = 0; x < width; x += 1) {
            const center = rangeStart + ((x + 0.5) / width) * span;
            let max = 0;
            const steps = Math.max(4, Math.ceil(sliceWidth * peaks.length * 5));
            for (let s = 0; s < steps; s += 1) {
                const ratio = center + (s / (steps - 1) - 0.5) * sliceWidth;
                max = Math.max(max, peakAtRatio(ratio));
            }
            samples[x] = max;
        }
        return samples;
    }

    function drawFilledWaveOnContext(targetCtx, samples, width, height, mid, color, clipEndX) {
        if (!samples.length) {
            return;
        }
        targetCtx.save();
        if (clipEndX !== null) {
            targetCtx.beginPath();
            targetCtx.rect(0, 0, clipEndX, height);
            targetCtx.clip();
        }
        targetCtx.fillStyle = color;
        targetCtx.beginPath();
        targetCtx.moveTo(0, mid);
        for (let x = 0; x < width; x += 1) {
            const y = mid - samples[x] * mid * 0.92;
            if (x === 0) {
                targetCtx.lineTo(0, y);
            } else {
                const prevY = mid - samples[x - 1] * mid * 0.92;
                targetCtx.quadraticCurveTo(x - 0.5, prevY, x, y);
            }
        }
        for (let x = width - 1; x >= 0; x -= 1) {
            const y = mid + samples[x] * mid * 0.92;
            if (x === width - 1) {
                targetCtx.lineTo(x, y);
            } else {
                const prevY = mid + samples[x + 1] * mid * 0.92;
                targetCtx.quadraticCurveTo(x + 0.5, prevY, x, y);
            }
        }
        targetCtx.closePath();
        targetCtx.fill();
        targetCtx.restore();
    }

    function stopAnimation() {
        if (rafId) {
            cancelAnimationFrame(rafId);
            rafId = null;
        }
    }

    function startAnimation() {
        stopAnimation();
        const tick = () => {
            if (audio) {
                currentTimeEl.textContent = formatTime(audio.currentTime);
            }
            drawWaveform();
            rafId = requestAnimationFrame(tick);
        };
        tick();
    }

    function setPlayingState(playing) {
        playIcon.classList.toggle("hidden", playing);
        pauseIcon.classList.toggle("hidden", !playing);
    }

    function revealControls() {
        loadingEl.classList.add("hidden");
        controlsEl.classList.remove("opacity-40", "pointer-events-none");
        if (activeButton) {
            activeButton.disabled = false;
        }
    }

    function formatTimePrecise(seconds) {
        if (seconds === null || seconds === undefined) {
            return "—";
        }
        const mins = Math.floor(seconds / 60);
        const secs = (seconds % 60).toFixed(1);
        return `${mins}:${secs.padStart(4, "0")}`;
    }

    function resetSelection() {
        selectionRegion = null;
        currentRecordingId = null;
        canTrim = false;
        if (selStartEl) {
            selStartEl.textContent = "—";
        }
        if (selEndEl) {
            selEndEl.textContent = "—";
        }
        if (downloadBtn) {
            downloadBtn.disabled = true;
        }
        if (trimStatusEl) {
            trimStatusEl.classList.add("hidden");
            trimStatusEl.textContent = "";
        }
        if (trimSection) {
            trimSection.classList.add("hidden");
        }
    }

    function updateSelectionUi() {
        if (!selectionRegion) {
            if (selStartEl) {
                selStartEl.textContent = "—";
            }
            if (selEndEl) {
                selEndEl.textContent = "—";
            }
            if (downloadBtn) {
                downloadBtn.disabled = true;
            }
            return;
        }
        if (selStartEl) {
            selStartEl.textContent = formatTimePrecise(selectionRegion.start);
        }
        if (selEndEl) {
            selEndEl.textContent = formatTimePrecise(selectionRegion.end);
        }
        if (downloadBtn) {
            downloadBtn.disabled = !canTrim || selectionRegion.end <= selectionRegion.start;
        }
    }

    function setSelectionStart() {
        if (!audio) {
            return;
        }
        const t = audio.currentTime;
        const max = duration || t + SELECTION_DEFAULT_SEC;
        if (selectionRegion) {
            if (t < selectionRegion.end) {
                selectionRegion.start = t;
            } else {
                selectionRegion = { start: t, end: Math.min(max, t + SELECTION_DEFAULT_SEC) };
            }
        } else {
            selectionRegion = { start: t, end: Math.min(max, t + SELECTION_DEFAULT_SEC) };
        }
        updateSelectionUi();
        drawWaveform();
    }

    function setSelectionEnd() {
        if (!audio) {
            return;
        }
        const t = audio.currentTime;
        if (selectionRegion) {
            if (selectionRegion.start < t) {
                selectionRegion.end = t;
            } else {
                selectionRegion = {
                    start: Math.max(0, t - SELECTION_DEFAULT_SEC),
                    end: t,
                };
            }
        } else {
            selectionRegion = {
                start: Math.max(0, t - SELECTION_DEFAULT_SEC),
                end: t,
            };
        }
        updateSelectionUi();
        drawWaveform();
    }

    async function downloadSelection() {
        if (!canTrim || !currentRecordingId || !selectionRegion) {
            return;
        }
        const startSec = Math.round(10 * selectionRegion.start) / 10;
        const endSec = Math.round(10 * selectionRegion.end) / 10;
        if (endSec <= startSec) {
            return;
        }

        downloadBtn.disabled = true;
        trimStatusEl.classList.remove("hidden");
        trimStatusEl.textContent = "Bezig met knippen…";
        trimStatusEl.className = "text-xs mt-2 text-muted";

        try {
            const response = await fetch("/api/trim", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    recording_id: currentRecordingId,
                    start_sec: startSec,
                    end_sec: endSec,
                }),
            });
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || "Knippen mislukt");
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = url;
            anchor.download = buildClipFilename();
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            URL.revokeObjectURL(url);
            trimStatusEl.textContent = "Fragment gedownload!";
            trimStatusEl.className = "text-xs mt-2 text-purple-600 font-medium";
        } catch (error) {
            trimStatusEl.textContent = error.message || "Knippen mislukt";
            trimStatusEl.className = "text-xs mt-2 text-red-500";
        } finally {
            updateSelectionUi();
        }
    }

    function destroyPlayer() {
        stopAnimation();
        if (audio) {
            audio.pause();
            audio.removeAttribute("src");
            audio.load();
            audio = null;
        }
        peaks = [];
        duration = 0;
        peaksUrl = "";
        resetSelection();
        resetZoom();
    }

    function closeModal() {
        destroyPlayer();
        modal.classList.add("hidden");
        document.body.classList.remove("overflow-hidden");
        if (activeButton) {
            activeButton.disabled = false;
            activeButton = null;
        }
    }

    function peaksRequestUrl(url, waitSeconds = 0) {
        if (!waitSeconds) {
            return url;
        }
        const separator = url.includes("?") ? "&" : "?";
        return `${url}${separator}wait=${waitSeconds}`;
    }

    function runCountdown(seconds = WAVEFORM_WAIT_SEC) {
        return new Promise((resolve) => {
            loadingEl.classList.remove("hidden");
            const totalMs = seconds * 1000;
            const started = performance.now();

            const tick = () => {
                const elapsed = performance.now() - started;
                const remaining = Math.max(0, totalMs - elapsed);
                const remainingSec = remaining / 1000;
                const progress = Math.min(1, elapsed / totalMs);

                if (countdownEl) {
                    countdownEl.textContent = remainingSec.toFixed(1);
                }
                if (countdownRing) {
                    countdownRing.style.strokeDashoffset = String(COUNTDOWN_CIRC * progress);
                }

                if (remaining <= 0) {
                    resolve();
                    return;
                }
                requestAnimationFrame(tick);
            };

            tick();
        });
    }

    function waitForPrecisePeaks(url, timeoutMs = 12000) {
        const started = Date.now();
        return new Promise((resolve) => {
            const attempt = async () => {
                try {
                    const data = await fetchPeaks(url, true);
                    if (data?.precise && peakValues(data).length) {
                        resolve(data);
                        return;
                    }
                } catch {
                    // keep trying
                }
                if (Date.now() - started >= timeoutMs) {
                    resolve(null);
                    return;
                }
                setTimeout(attempt, 800);
            };
            attempt();
        });
    }

    async function fetchPeaks(url, force = false, waitSeconds = 0) {
        const requestUrl = force ? url : peaksRequestUrl(url, waitSeconds);
        if (!force && waitSeconds === 0 && peaksCache.has(url)) {
            const cached = peaksCache.get(url);
            if (cached?.precise && peakValues(cached).length) {
                return cached;
            }
        }
        if (!force && peaksInflight.has(requestUrl)) {
            return peaksInflight.get(requestUrl);
        }

        const request = fetch(requestUrl)
            .then((response) => {
                if (!response.ok) {
                    throw new Error("Wavevorm laden mislukt");
                }
                return response.json();
            })
            .then((data) => {
                if (data?.precise && peakValues(data).length) {
                    peaksCache.set(url, { ...(peaksCache.get(url) || {}), ...data });
                }
                return peaksCache.get(url) || data;
            })
            .finally(() => {
                peaksInflight.delete(requestUrl);
            });

        peaksInflight.set(requestUrl, request);
        return request;
    }

    function prefetchPeaks(url) {
        if (!url || peaksCache.has(url) || peaksInflight.has(url)) {
            return;
        }
        fetchPeaks(url).catch(() => {});
    }

    function peakValues(data) {
        return data?.peaks || [];
    }

    function applyPeaksData(data) {
        peaks = upsamplePeaks(peakValues(data));
        duration = data.duration || duration || 3600;
        totalTimeEl.textContent = formatTime(duration);
        refreshZoomLimits();
        resizeCanvas();
        revealControls();
    }

    function pointerToFocusRatio(event) {
        const rect = canvas.getBoundingClientRect();
        return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    }

    function pointerToGlobalRatio(event) {
        const focus = pointerToFocusRatio(event);
        const span = getViewSpan();
        return Math.min(1, Math.max(0, viewStart + focus * span));
    }

    function seekFromPointer(event) {
        if (!audio || !duration) {
            return;
        }
        const focus = pointerToFocusRatio(event);
        const span = getViewSpan();
        const ratio = viewStart + focus * span;
        audio.currentTime = Math.min(duration, Math.max(0, ratio * duration));
        drawWaveform();
    }

    function loadPlayerData(data, fallbackTitle) {
        titleEl.textContent = data.title || fallbackTitle;
        if (data.is_live) {
            subtitleEl.textContent = "Live opname";
        } else {
            subtitleEl.textContent = "Volledig uur";
        }

        currentRecordingId = data.recording_id || null;
        canTrim = Boolean(currentRecordingId && !data.is_live);
        if (trimSection) {
            trimSection.classList.toggle("hidden", !canTrim);
        }

        applyPeaksData(data);
    }

    async function openPlayer(button) {
        const url = button.dataset.peaksUrl;
        const audioUrl = button.dataset.audioUrl;
        const fallbackTitle = button.dataset.title || "Opname";
        const buttonDuration = Number(button.dataset.duration) || 3600;
        if (!url || !audioUrl) {
            return;
        }

        clipMeta = {
            stationSlug: button.dataset.stationSlug || "",
            date: button.dataset.recordingDate || "",
            hour: button.dataset.recordingHour || "00",
        };

        activeButton = button;
        button.disabled = true;
        peaksUrl = url;

        destroyPlayer();
        modal.classList.remove("hidden");
        document.body.classList.add("overflow-hidden");
        controlsEl.classList.add("opacity-40", "pointer-events-none");
        titleEl.textContent = fallbackTitle;
        subtitleEl.textContent = "Wavevorm voorbereiden…";
        currentTimeEl.textContent = "0:00";
        totalTimeEl.textContent = formatTime(buttonDuration);
        resetSelection();
        setPlayingState(false);
        peaks = [];
        resizeCanvas();

        currentRecordingId = button.dataset.recordingId
            ? Number(button.dataset.recordingId)
            : null;
        canTrim = Boolean(currentRecordingId);

        const inlinePeaks = parseInlinePeaks(button.dataset.peaks);
        const cachedMeta = peaksCache.get(url) || {};
        if (inlinePeaks?.length) {
            cachedMeta.peaks = inlinePeaks;
            cachedMeta.precise = true;
            cachedMeta.duration = buttonDuration;
            peaksCache.set(url, cachedMeta);
        }

        const hasCachedPeaks = Boolean(cachedMeta.precise && peakValues(cachedMeta).length);
        const peaksPromise = hasCachedPeaks
            ? Promise.resolve(cachedMeta)
            : fetchPeaks(url, false, WAVEFORM_WAIT_SEC);

        try {
            const [, peakData] = await Promise.all([
                hasCachedPeaks ? Promise.resolve() : runCountdown(WAVEFORM_WAIT_SEC),
                peaksPromise,
            ]);
            let data = peakData;
            if (!data?.precise || !peakValues(data).length) {
                data = await waitForPrecisePeaks(url);
            }
            if (!peakValues(data).length) {
                throw new Error("Wavevorm kon niet worden geladen");
            }

            loadingEl.classList.add("hidden");
            loadPlayerData({ ...cachedMeta, ...data, duration: data.duration || buttonDuration }, fallbackTitle);

            audio = new Audio(audioUrl);
            audio.preload = "metadata";
            audio.addEventListener("play", () => setPlayingState(true));
            audio.addEventListener("pause", () => setPlayingState(false));
            audio.addEventListener("ended", () => setPlayingState(false));
            audio.addEventListener("loadedmetadata", () => {
                if (audio.duration && Number.isFinite(audio.duration)) {
                    duration = audio.duration;
                    totalTimeEl.textContent = formatTime(duration);
                    refreshZoomLimits();
                    resizeCanvas();
                }
            });

            controlsEl.classList.remove("opacity-40", "pointer-events-none");
            if (trimSection && canTrim) {
                trimSection.classList.remove("hidden");
            }
            resizeCanvas();
            startAnimation();
        } catch (error) {
            loadingEl.classList.remove("hidden");
            if (countdownEl) {
                countdownEl.textContent = "!";
            }
            subtitleEl.textContent = error.message || "Wavevorm laden mislukt";
            controlsEl.classList.add("opacity-40", "pointer-events-none");
        } finally {
            if (activeButton) {
                activeButton.disabled = false;
            }
        }
    }

    document.querySelectorAll(".listen-btn").forEach((button) => {
        button.addEventListener("click", () => openPlayer(button));
        button.addEventListener("mouseenter", () => {
            prefetchPeaks(button.dataset.peaksUrl);
        }, { once: true });
    });

    playBtn.addEventListener("click", () => {
        if (!audio) {
            return;
        }
        if (audio.paused) {
            audio.play().catch(() => {
                subtitleEl.textContent = "Afspelen mislukt";
            });
        } else {
            audio.pause();
        }
    });

    if (zoomSlider) {
        zoomSlider.addEventListener("input", () => {
            if (syncingZoomSlider) {
                return;
            }
            const focus = pointerOverCanvas ? lastFocusInView : 0.5;
            setViewSpan(sliderValueToSpan(Number(zoomSlider.value)), focus);
        });
    }

    if (panSlider) {
        panSlider.addEventListener("input", () => {
            const maxStart = Math.max(0, 1 - getViewSpan());
            const ratio = Number(panSlider.value) / PAN_STEPS;
            setViewStart(maxStart * ratio);
        });
    }

    canvas.addEventListener("mouseenter", () => {
        pointerOverCanvas = true;
    });

    canvas.addEventListener("mousemove", (event) => {
        lastFocusInView = pointerToFocusRatio(event);
        if (dragSelect && canTrim && duration) {
            if (Math.abs(event.clientX - dragSelect.startX) >= DRAG_SELECT_THRESHOLD_PX) {
                dragSelect.moved = true;
                const endRatio = pointerToGlobalRatio(event);
                const startRatio = Math.min(dragSelect.startRatio, endRatio);
                const endRatioFinal = Math.max(dragSelect.startRatio, endRatio);
                selectionRegion = {
                    start: startRatio * duration,
                    end: Math.max(startRatio * duration + 0.1, endRatioFinal * duration),
                };
                updateSelectionUi();
                drawWaveform();
            }
        }
    });

    canvas.addEventListener("wheel", (event) => {
        event.preventDefault();
        const focus = pointerToFocusRatio(event);
        lastFocusInView = focus;
        const delta = normalizeWheelDelta(event);
        const factor = Math.exp(delta * WHEEL_ZOOM_SENSITIVITY);
        setViewSpan(getViewSpan() * factor, focus);
    }, { passive: false });

    canvas.addEventListener("mousedown", (event) => {
        if (!canTrim || !duration || event.button !== 0) {
            return;
        }
        dragSelect = {
            startRatio: pointerToGlobalRatio(event),
            startX: event.clientX,
            moved: false,
        };
    });

    canvas.addEventListener("mouseup", (event) => {
        if (event.button !== 0) {
            return;
        }
        if (dragSelect) {
            const wasDrag = dragSelect.moved;
            dragSelect = null;
            if (!wasDrag) {
                seekFromPointer(event);
            }
            return;
        }
        if (!canTrim) {
            seekFromPointer(event);
        }
    });

    canvas.addEventListener("mouseleave", () => {
        pointerOverCanvas = false;
        dragSelect = null;
    });

    if (overviewCanvas) {
        overviewCanvas.addEventListener("click", (event) => {
            if (!duration) {
                return;
            }
            const rect = overviewCanvas.getBoundingClientRect();
            const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
            const span = getViewSpan();
            setViewStart(ratio - span / 2);
            drawWaveform();
        });
    }

    canvas.addEventListener("dblclick", () => {
        if (!audio) {
            return;
        }
        audio.play().catch(() => {
            subtitleEl.textContent = "Afspelen mislukt";
        });
    });

    if (selStartBtn) {
        selStartBtn.addEventListener("click", setSelectionStart);
    }
    if (selEndBtn) {
        selEndBtn.addEventListener("click", setSelectionEnd);
    }
    if (downloadBtn) {
        downloadBtn.addEventListener("click", downloadSelection);
    }

    closeBtn.addEventListener("click", closeModal);

    modal.addEventListener("click", (event) => {
        if (panel && panel.contains(event.target)) {
            return;
        }
        closeModal();
    });

    window.addEventListener("resize", () => {
        if (!modal.classList.contains("hidden")) {
            resizeCanvas();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (modal.classList.contains("hidden")) {
            return;
        }
        if (event.key === "Escape") {
            closeModal();
        } else if (event.key === "+" || event.key === "=") {
            zoomIn(lastFocusInView);
        } else if (event.key === "-") {
            zoomOut(lastFocusInView);
        } else if (event.key === "0") {
            resetZoom();
        } else if (canTrim && (event.key === "[" || event.code === "BracketLeft")) {
            setSelectionStart();
        } else if (canTrim && (event.key === "]" || event.code === "BracketRight")) {
            setSelectionEnd();
        } else if (
            (event.code === "Space" || event.key === " ")
            && event.target.tagName !== "INPUT"
            && event.target.tagName !== "TEXTAREA"
        ) {
            event.preventDefault();
            if (!audio) {
                return;
            }
            if (audio.paused) {
                audio.play().catch(() => {});
            } else {
                audio.pause();
            }
        }
    });
})();
