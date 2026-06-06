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
    const WAVEFORM_WAIT_SEC = 3;
    const countdownEl = document.getElementById("player-modal-countdown");
    const countdownRing = document.getElementById("player-modal-countdown-ring");
    const COUNTDOWN_CIRC = 326.7;

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
    let syncingZoomSlider = false;

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

        drawFilledWave(samples, width, height, mid, "#86EFAC", null);
        if (playheadX > 0) {
            drawFilledWave(samples, width, height, mid, "#1E3A8A", playheadX);
        }

        if (playheadX >= 0 && playheadX <= width) {
            ctx.fillStyle = "#312E81";
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
                    if (data?.precise && data.peaks?.length) {
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
            if (cached?.precise && cached.peaks?.length) {
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
                if (data?.precise && data.peaks?.length) {
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

    function applyPeaksData(data) {
        peaks = data.peaks || [];
        duration = data.duration || duration || 3600;
        totalTimeEl.textContent = formatTime(duration);
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

        if (data.recording_id && !data.is_live) {
            trimLink.href = `/player/${data.recording_id}`;
            trimLink.classList.remove("hidden");
        } else {
            trimLink.classList.add("hidden");
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
        trimLink.classList.add("hidden");
        setPlayingState(false);
        peaks = [];
        resizeCanvas();

        const inlinePeaks = parseInlinePeaks(button.dataset.peaks);
        const cachedMeta = peaksCache.get(url) || {};
        if (inlinePeaks?.length) {
            cachedMeta.peaks = inlinePeaks;
            cachedMeta.precise = true;
            cachedMeta.duration = buttonDuration;
            peaksCache.set(url, cachedMeta);
        }

        const hasCachedPeaks = Boolean(cachedMeta.precise && cachedMeta.peaks?.length);
        const peaksPromise = hasCachedPeaks
            ? Promise.resolve(cachedMeta)
            : fetchPeaks(url, false, WAVEFORM_WAIT_SEC);

        try {
            const [, peakData] = await Promise.all([runCountdown(WAVEFORM_WAIT_SEC), peaksPromise]);
            let data = peakData;
            if (!data?.precise || !data.peaks?.length) {
                data = await waitForPrecisePeaks(url);
            }
            if (!data?.peaks?.length) {
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
                }
            });

            controlsEl.classList.remove("opacity-40", "pointer-events-none");
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
