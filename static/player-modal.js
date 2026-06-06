(function () {
    const modal = document.getElementById("player-modal");
    const backdrop = document.getElementById("player-modal-backdrop");
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
    const zoomInBtn = document.getElementById("player-modal-zoom-in");
    const zoomOutBtn = document.getElementById("player-modal-zoom-out");
    const zoomResetBtn = document.getElementById("player-modal-zoom-reset");
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

    const bootstrapEl = document.getElementById("peaks-bootstrap");
    if (bootstrapEl) {
        try {
            const bootstrap = JSON.parse(bootstrapEl.textContent || "{}");
            Object.entries(bootstrap).forEach(([url, data]) => {
                if (data.ready) {
                    peaksCache.set(url, data);
                }
            });
        } catch {
            // ignore invalid bootstrap JSON
        }
    }

    let audio = null;
    let activeButton = null;
    let peaks = [];
    let duration = 0;
    let pollTimer = null;
    let rafId = null;
    let peaksUrl = "";
    let zoom = MIN_ZOOM;
    let viewStart = 0;
    let lastFocusInView = 0.5;

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

    function updateZoomLabel() {
        if (zoomResetBtn) {
            zoomResetBtn.textContent = zoom === MIN_ZOOM ? "1×" : `${zoom}×`;
        }
    }

    function updatePanSlider() {
        if (!panWrap || !panSlider) {
            return;
        }
        const span = getViewSpan();
        if (zoom <= MIN_ZOOM) {
            panWrap.classList.add("hidden");
            return;
        }
        panWrap.classList.remove("hidden");
        const maxStart = Math.max(0, 1 - span);
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

    function resetZoom() {
        zoom = MIN_ZOOM;
        viewStart = 0;
        updateZoomLabel();
        updatePanSlider();
        drawWaveform();
    }

    function zoomAt(factor, focusInView) {
        const oldSpan = getViewSpan();
        const focusPoint = viewStart + focusInView * oldSpan;
        zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * factor));
        if (zoom === MIN_ZOOM) {
            viewStart = 0;
        } else {
            const newSpan = getViewSpan();
            viewStart = clampViewStart(focusPoint - focusInView * newSpan);
        }
        updateZoomLabel();
        updatePanSlider();
        drawWaveform();
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

    function clearPoll() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function destroyPlayer() {
        clearPoll();
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
                if (data.ready && !data.is_live) {
                    peaksCache.set(url, data);
                }
                return data;
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
        resetZoom();
        resizeCanvas();
        revealControls();
    }

    function startPeaksPoll(url) {
        clearPoll();
        pollTimer = setInterval(async () => {
            try {
                const data = await fetchPeaks(url, true);
                if (data.ready && data.peaks && data.peaks.length > 0) {
                    applyPeaksData(data);
                    subtitleEl.textContent = data.is_live ? "Live opname" : "Volledig uur";
                    clearPoll();
                }
            } catch {
                // keep polling
            }
        }, 400);
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

    async function openPlayer(button) {
        const url = button.dataset.peaksUrl;
        const audioUrl = button.dataset.audioUrl;
        const fallbackTitle = button.dataset.title || "Opname";
        if (!url || !audioUrl) {
            return;
        }

        activeButton = button;
        button.disabled = true;
        peaksUrl = url;

        destroyPlayer();
        modal.classList.remove("hidden");
        document.body.classList.add("overflow-hidden");
        loadingEl.classList.remove("hidden");
        controlsEl.classList.add("opacity-40", "pointer-events-none");
        titleEl.textContent = fallbackTitle;
        subtitleEl.textContent = "Laden…";
        currentTimeEl.textContent = "0:00";
        totalTimeEl.textContent = "0:00";
        trimLink.classList.add("hidden");
        setPlayingState(false);

        audio = new Audio(audioUrl);
        audio.preload = "auto";

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
        revealControls();
        startAnimation();

        try {
            const cached = peaksCache.get(url);
            if (cached) {
                titleEl.textContent = cached.title || fallbackTitle;
                subtitleEl.textContent = cached.is_live ? "Live opname" : "Volledig uur";
                if (cached.recording_id && !cached.is_live) {
                    trimLink.href = `/player/${cached.recording_id}`;
                    trimLink.classList.remove("hidden");
                }
                applyPeaksData(cached);
                if (cached.ready) {
                    if (activeButton) {
                        activeButton.disabled = false;
                    }
                    return;
                }
            }

            const data = await fetchPeaks(url, false);
            titleEl.textContent = data.title || fallbackTitle;
            subtitleEl.textContent = data.is_live ? "Live opname" : "Volledig uur";

            if (data.recording_id && !data.is_live) {
                trimLink.href = `/player/${data.recording_id}`;
                trimLink.classList.remove("hidden");
            }

            if (data.ready && data.peaks && data.peaks.length > 0) {
                applyPeaksData(data);
            } else {
                subtitleEl.textContent = "Wavevorm voorbereiden…";
                startPeaksPoll(url);
            }
        } catch (error) {
            subtitleEl.textContent = error.message || "Laden mislukt";
            revealControls();
        }
    }

    document.querySelectorAll(".listen-btn").forEach((button) => {
        button.addEventListener("click", () => openPlayer(button));
        button.addEventListener("mouseenter", () => {
            prefetchPeaks(button.dataset.peaksUrl);
        }, { once: true });
    });

    document.querySelectorAll(".listen-btn").forEach((button) => {
        prefetchPeaks(button.dataset.peaksUrl);
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

    if (zoomInBtn) {
        zoomInBtn.addEventListener("click", () => zoomIn(lastFocusInView));
    }
    if (zoomOutBtn) {
        zoomOutBtn.addEventListener("click", () => zoomOut(lastFocusInView));
    }
    if (zoomResetBtn) {
        zoomResetBtn.addEventListener("click", resetZoom);
    }

    if (panSlider) {
        panSlider.addEventListener("input", () => {
            const span = getViewSpan();
            const maxStart = Math.max(0, 1 - span);
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
