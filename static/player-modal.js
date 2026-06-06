(function () {
    const modal = document.getElementById("player-modal");
    const panel = document.getElementById("player-modal-panel");
    const closeBtn = document.getElementById("player-modal-close");
    const titleEl = document.getElementById("player-modal-title");
    const subtitleEl = document.getElementById("player-modal-subtitle");
    const canvas = document.getElementById("player-modal-canvas");
    const loadingEl = document.getElementById("player-modal-loading");
    const controlsEl = document.getElementById("player-modal-controls");
    const playBtn = document.getElementById("player-modal-play");
    const playIcon = document.getElementById("player-modal-play-icon");
    const pauseIcon = document.getElementById("player-modal-pause-icon");
    const currentTimeEl = document.getElementById("player-modal-current");
    const totalTimeEl = document.getElementById("player-modal-total");
    const trimLink = document.getElementById("player-modal-trim");
    const zoomSlider = document.getElementById("player-modal-zoom");
    const zoomLabel = document.getElementById("player-modal-zoom-label");
    const panWrap = document.getElementById("player-modal-pan-wrap");
    const panSlider = document.getElementById("player-modal-pan");

    if (!modal || !canvas) {
        return;
    }

    const ctx = canvas.getContext("2d");
    const peaksCache = new Map();
    const peaksInflight = new Map();
    const MIN_ZOOM = 1;
    const MAX_ZOOM = 48;
    const PAN_STEPS = 1000;
    const PLACEHOLDER_BARS = 512;

    const bootstrapEl = document.getElementById("peaks-bootstrap");
    if (bootstrapEl) {
        try {
            const bootstrap = JSON.parse(bootstrapEl.textContent || "{}");
            Object.entries(bootstrap).forEach(([url, data]) => {
                peaksCache.set(url, data);
            });
        } catch {
            // ignore invalid bootstrap JSON
        }
    }

    let audio = null;
    let activeButton = null;
    let peaks = [];
    let duration = 0;
    let precisePollTimer = null;
    let rafId = null;
    let peaksUrl = "";
    let zoom = MIN_ZOOM;
    let viewStart = 0;
    let lastFocusInView = 0.5;
    let syncingZoomSlider = false;

    function generatePlaceholderPeaks(bars = PLACEHOLDER_BARS) {
        const peaks = [];
        for (let index = 0; index < bars; index += 1) {
            peaks.push(Math.round((0.07 + 0.05 * Math.sin(index * 0.06)) * 10000) / 10000);
        }
        return peaks;
    }

    function buildInstantPeakData(duration, meta = {}) {
        const hasPeaks = Array.isArray(meta.peaks) && meta.peaks.length > 0;
        return {
            ...meta,
            peaks: hasPeaks ? meta.peaks : generatePlaceholderPeaks(),
            duration: meta.duration || duration || 3600,
            ready: true,
            precise: Boolean(meta.precise && hasPeaks),
        };
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
        zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(newZoom)));
        if (zoom === MIN_ZOOM) {
            viewStart = 0;
        } else {
            const newSpan = getViewSpan();
            viewStart = clampViewStart(focusPoint - focusInView * newSpan);
        }
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

        drawFilledWave(samples, width, height, mid, "#D1D5DB", null);
        if (playheadX > 0) {
            drawFilledWave(samples, width, height, mid, "#7C3AED", playheadX);
        }

        if (playheadX >= 0 && playheadX <= width) {
            ctx.fillStyle = "#6D28D9";
            ctx.fillRect(Math.max(0, playheadX - 1), 0, 2, height);
        }
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
                if (zoom > MIN_ZOOM) {
                    const progress = timeToRatio(audio.currentTime);
                    const span = getViewSpan();
                    if (progress < viewStart + span * 0.08 || progress > viewStart + span * 0.92) {
                        setViewStart(progress - span / 2);
                    }
                }
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

    function clearPrecisePoll() {
        if (precisePollTimer) {
            clearInterval(precisePollTimer);
            precisePollTimer = null;
        }
    }

    function destroyPlayer() {
        clearPrecisePoll();
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

    async function fetchPeaks(url, force = false) {
        if (!force && peaksCache.has(url)) {
            return peaksCache.get(url);
        }
        if (!force && peaksInflight.has(url)) {
            return peaksInflight.get(url);
        }

        const request = fetch(url)
            .then((response) => {
                if (!response.ok) {
                    throw new Error("Wavevorm laden mislukt");
                }
                return response.json();
            })
            .then((data) => {
                peaksCache.set(url, { ...(peaksCache.get(url) || {}), ...data });
                return peaksCache.get(url);
            })
            .finally(() => {
                peaksInflight.delete(url);
            });

        peaksInflight.set(url, request);
        return request;
    }

    function prefetchPeaks(url) {
        if (!url || peaksCache.has(url) || peaksInflight.has(url)) {
            return;
        }
        fetchPeaks(url).catch(() => {});
    }

    function applyPeaksData(data) {
        peaks = data.peaks || [];
        duration = data.duration || duration || 3600;
        totalTimeEl.textContent = formatTime(duration);
        resizeCanvas();
        revealControls();
    }

    function startPreciseUpgrade(url) {
        clearPrecisePoll();
        precisePollTimer = setInterval(async () => {
            try {
                const data = await fetchPeaks(url, true);
                if (data.precise && data.peaks && data.peaks.length > 0) {
                    applyPeaksData(data);
                    clearPrecisePoll();
                }
            } catch {
                // keep trying quietly
            }
        }, 2000);
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

    function loadPlayerData(data, fallbackTitle, { allowUpgrade = true } = {}) {
        titleEl.textContent = data.title || fallbackTitle;
        if (data.is_live) {
            subtitleEl.textContent = "Live opname";
        } else if (data.precise) {
            subtitleEl.textContent = "Volledig uur";
        } else {
            subtitleEl.textContent = "Voorbeeld — echte wavevorm volgt";
        }

        if (data.recording_id && !data.is_live) {
            trimLink.href = `/player/${data.recording_id}`;
            trimLink.classList.remove("hidden");
        } else {
            trimLink.classList.add("hidden");
        }

        applyPeaksData(data);
        if (allowUpgrade && !data.precise && peaksUrl) {
            startPreciseUpgrade(peaksUrl);
        }
    }

    async function openPlayer(button) {
        const url = button.dataset.peaksUrl;
        const audioUrl = button.dataset.audioUrl;
        const fallbackTitle = button.dataset.title || "Opname";
        const buttonDuration = Number(button.dataset.duration) || 3600;
        if (!url || !audioUrl) {
            return;
        }

        activeButton = button;
        button.disabled = true;
        peaksUrl = url;

        destroyPlayer();
        modal.classList.remove("hidden");
        document.body.classList.add("overflow-hidden");
        loadingEl.classList.add("hidden");
        controlsEl.classList.remove("opacity-40", "pointer-events-none");
        titleEl.textContent = fallbackTitle;
        currentTimeEl.textContent = "0:00";
        trimLink.classList.add("hidden");
        setPlayingState(false);

        const cachedMeta = peaksCache.get(url) || {};
        const instantData = buildInstantPeakData(buttonDuration, cachedMeta);
        loadPlayerData(instantData, fallbackTitle, { allowUpgrade: false });

        audio = new Audio(audioUrl);
        audio.preload = "metadata";

        audio.addEventListener("play", () => setPlayingState(true));
        audio.addEventListener("pause", () => setPlayingState(false));
        audio.addEventListener("ended", () => setPlayingState(false));
        audio.addEventListener("loadedmetadata", () => {
            if (audio.duration && Number.isFinite(audio.duration)) {
                duration = audio.duration;
                totalTimeEl.textContent = formatTime(duration);
            }
        });

        resizeCanvas();
        startAnimation();

        if (cachedMeta.precise && cachedMeta.peaks?.length) {
            if (activeButton) {
                activeButton.disabled = false;
            }
            return;
        }

        try {
            const data = await fetchPeaks(url);
            loadPlayerData({ ...cachedMeta, ...data }, fallbackTitle);
        } catch (error) {
            subtitleEl.textContent = error.message || "Wavevorm kon niet worden verfijnd";
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
            setZoomLevel(Number(zoomSlider.value), lastFocusInView);
        });
    }

    if (panSlider) {
        panSlider.addEventListener("input", () => {
            const maxStart = Math.max(0, 1 - getViewSpan());
            const ratio = Number(panSlider.value) / PAN_STEPS;
            setViewStart(maxStart * ratio);
        });
    }

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

    canvas.addEventListener("click", seekFromPointer);

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
        }
    });
})();
