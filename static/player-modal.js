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
    const MIN_ZOOM = 1;
    const TARGET_VISIBLE_SECONDS = 20 * 60;
    let maxZoom = MIN_ZOOM;
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
    let duration = 0;
    let rafId = null;
    let peaksUrl = "";
    let zoom = MIN_ZOOM;
    let viewStart = 0;
    let lastFocusInView = 0.5;
    let pointerOverCanvas = false;
    let syncingZoomSlider = false;
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

    function refreshMaxZoom() {
        maxZoom = Math.max(MIN_ZOOM, Math.ceil((duration || 3600) / TARGET_VISIBLE_SECONDS));
        if (zoomSlider) {
            zoomSlider.max = String(maxZoom);
        }
        if (zoom > maxZoom) {
            setZoomLevel(maxZoom, lastFocusInView);
        } else {
            updateZoomUi();
        }
    }

    function getViewSpan() {
        return 1 / zoom;
    }

    function clampViewStart(start) {
        const span = getViewSpan();
        return Math.min(Math.max(0, start), Math.max(0, 1 - span));
    }

    function updateZoomUi() {
        if (zoomLabel) {
            zoomLabel.textContent = `${zoom}×`;
        }
        if (zoomSlider) {
            syncingZoomSlider = true;
            zoomSlider.value = String(zoom);
            syncingZoomSlider = false;
        }
        updatePanSlider();
    }

    function updatePanSlider() {
        if (!panWrap || !panSlider) {
            return;
        }
        if (zoom <= MIN_ZOOM) {
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
        drawWaveform();
    }

    function setZoomLevel(newZoom, focusInView = lastFocusInView) {
        const oldSpan = getViewSpan();
        const focusPoint = viewStart + focusInView * oldSpan;
        zoom = Math.min(maxZoom, Math.max(MIN_ZOOM, Math.round(newZoom)));
        if (zoom === MIN_ZOOM) {
            viewStart = 0;
        } else {
            const newSpan = getViewSpan();
            viewStart = clampViewStart(focusPoint - focusInView * newSpan);
        }
        lastFocusInView = focusInView;
        updateZoomUi();
        drawWaveform();
    }

    function resetZoom() {
        setZoomLevel(MIN_ZOOM, 0.5);
    }

    function zoomAt(factor, focusInView) {
        setZoomLevel(zoom * factor, focusInView);
    }

    function zoomIn(focusInView) {
        zoomAt(2, focusInView ?? lastFocusInView);
    }

    function zoomOut(focusInView) {
        zoomAt(0.5, focusInView ?? lastFocusInView);
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
    }

    function timeToRatio(time) {
        return duration > 0 ? time / duration : 0;
    }

    function ratioToCanvasX(ratio) {
        const span = getViewSpan();
        return ((ratio - viewStart) / span) * canvas.width;
    }

    function getVisiblePeaks(width) {
        if (!peaks.length) {
            return [];
        }
        const span = getViewSpan();
        const startIdx = viewStart * peaks.length;
        const endIdx = (viewStart + span) * peaks.length;
        const samples = new Array(width).fill(0);

        for (let x = 0; x < width; x += 1) {
            const sliceStart = startIdx + (x / width) * (endIdx - startIdx);
            const sliceEnd = startIdx + ((x + 1) / width) * (endIdx - startIdx);
            const iStart = Math.floor(sliceStart);
            const iEnd = Math.max(iStart + 1, Math.ceil(sliceEnd));
            let max = 0;
            for (let i = iStart; i < iEnd && i < peaks.length; i += 1) {
                if (i >= 0) {
                    max = Math.max(max, peaks[i]);
                }
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
            const amplitude = samples[x] * mid * 0.92;
            ctx.lineTo(x, mid - amplitude);
        }
        for (let x = width - 1; x >= 0; x -= 1) {
            const amplitude = samples[x] * mid * 0.92;
            ctx.lineTo(x, mid + amplitude);
        }
        ctx.closePath();
        ctx.fill();
        ctx.restore();
    }

    function drawWaveform() {
        const width = canvas.width;
        const height = canvas.height;
        const mid = height / 2;
        ctx.clearRect(0, 0, width, height);

        if (!peaks.length) {
            ctx.fillStyle = "#E5E7EB";
            ctx.fillRect(0, mid - 1, width, 2);
            return;
        }

        const samples = getVisiblePeaks(width);
        const progress = timeToRatio(audio ? audio.currentTime : 0);
        const playheadX = ratioToCanvasX(progress);

        drawFilledWave(samples, width, height, mid, WAVE_COLOR_IDLE, null);
        if (playheadX > 0) {
            drawFilledWave(samples, width, height, mid, WAVE_COLOR_PLAYED, playheadX);
        }

        if (selectionRegion && duration > 0) {
            const startX = ratioToCanvasX(timeToRatio(selectionRegion.start));
            const endX = ratioToCanvasX(timeToRatio(selectionRegion.end));
            const left = Math.min(startX, endX);
            const selWidth = Math.abs(endX - startX);
            ctx.fillStyle = "rgba(124, 58, 237, 0.18)";
            ctx.fillRect(left, 0, selWidth, height);
            ctx.strokeStyle = WAVE_COLOR_PLAYED;
            ctx.lineWidth = 2;
            ctx.strokeRect(left + 0.5, 0.5, Math.max(0, selWidth - 1), height - 1);
        }

        if (playheadX >= 0 && playheadX <= width) {
            ctx.fillStyle = WAVE_COLOR_CURSOR;
            ctx.fillRect(Math.max(0, playheadX - 1), 0, 2, height);
        }

        drawTimeAxis(width, height);
        drawOverview();
    }

    function drawTimeAxis(width, height) {
        if (zoom <= MIN_ZOOM || !duration) {
            return;
        }
        const span = getViewSpan();
        const visibleSeconds = duration * span;
        const step = visibleSeconds <= 15 * 60 ? 60 : 120;
        const startSec = viewStart * duration;
        const endSec = startSec + visibleSeconds;
        const firstTick = Math.ceil(startSec / step) * step;

        ctx.save();
        ctx.fillStyle = "#64748b";
        ctx.font = `${Math.max(10, Math.floor(width / 90))}px Inter, system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";

        for (let sec = firstTick; sec <= endSec; sec += step) {
            const ratio = sec / duration;
            const x = ratioToCanvasX(ratio);
            if (x < 0 || x > width) {
                continue;
            }
            ctx.fillRect(x, height - 14, 1, 8);
            const mins = Math.floor(sec / 60);
            const secs = Math.floor(sec % 60);
            ctx.fillText(`${mins}:${secs.toString().padStart(2, "0")}`, x, height - 16);
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
        const startIdx = rangeStart * peaks.length;
        const endIdx = rangeEnd * peaks.length;
        const samples = new Array(width).fill(0);
        for (let x = 0; x < width; x += 1) {
            const sliceStart = startIdx + (x / width) * (endIdx - startIdx);
            const sliceEnd = startIdx + ((x + 1) / width) * (endIdx - startIdx);
            const iStart = Math.floor(sliceStart);
            const iEnd = Math.max(iStart + 1, Math.ceil(sliceEnd));
            let max = 0;
            for (let i = iStart; i < iEnd && i < peaks.length; i += 1) {
                if (i >= 0) {
                    max = Math.max(max, peaks[i]);
                }
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
            const amplitude = samples[x] * mid * 0.92;
            targetCtx.lineTo(x, mid - amplitude);
        }
        for (let x = width - 1; x >= 0; x -= 1) {
            const amplitude = samples[x] * mid * 0.92;
            targetCtx.lineTo(x, mid + amplitude);
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
        peaks = peakValues(data);
        duration = data.duration || duration || 3600;
        totalTimeEl.textContent = formatTime(duration);
        refreshMaxZoom();
        resizeCanvas();
        revealControls();
    }

    function pointerToFocusRatio(event) {
        const rect = canvas.getBoundingClientRect();
        return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
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
                    refreshMaxZoom();
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
            setZoomLevel(Number(zoomSlider.value), focus);
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

    canvas.addEventListener("mouseleave", () => {
        pointerOverCanvas = false;
    });

    canvas.addEventListener("mousemove", (event) => {
        lastFocusInView = pointerToFocusRatio(event);
    });

    canvas.addEventListener("wheel", (event) => {
        event.preventDefault();
        const focus = pointerToFocusRatio(event);
        lastFocusInView = focus;
        if (event.deltaY < 0) {
            zoomIn(focus);
        } else {
            zoomOut(focus);
        }
    }, { passive: false });

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

    canvas.addEventListener("click", seekFromPointer);

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
