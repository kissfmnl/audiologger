(function () {
    const modal = document.getElementById("player-modal");
    const backdrop = document.getElementById("player-modal-backdrop");
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

    if (!modal || !canvas) {
        return;
    }

    const ctx = canvas.getContext("2d");
    const peaksCache = new Map();
    const peaksInflight = new Map();

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

    function resizeCanvas() {
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.floor(rect.width * dpr));
        canvas.height = Math.max(1, Math.floor(rect.height * dpr));
        drawWaveform();
    }

    function drawWaveform() {
        const width = canvas.width;
        const height = canvas.height;
        ctx.clearRect(0, 0, width, height);

        if (!peaks.length) {
            ctx.fillStyle = "#E5E7EB";
            const mid = height / 2;
            ctx.fillRect(0, mid - 1, width, 2);
            return;
        }

        const current = audio ? audio.currentTime : 0;
        const progress = duration > 0 ? current / duration : 0;
        const barWidth = width / peaks.length;
        const mid = height / 2;

        for (let index = 0; index < peaks.length; index += 1) {
            const value = peaks[index];
            const barHeight = Math.max(2, value * height * 0.88);
            const x = index * barWidth;
            const played = index / peaks.length < progress;
            ctx.fillStyle = played ? "#7C3AED" : "#D1D5DB";
            ctx.fillRect(x, mid - barHeight / 2, Math.max(1, barWidth - 0.25), barHeight);
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

    function seekFromPointer(event) {
        if (!audio || !duration) {
            return;
        }
        const rect = canvas.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        audio.currentTime = ratio * duration;
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

    canvas.addEventListener("click", seekFromPointer);
    canvas.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) {
            return;
        }
        seekFromPointer(event);

        const onMove = (moveEvent) => seekFromPointer(moveEvent);
        const onUp = () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
    });

    closeBtn.addEventListener("click", closeModal);
    backdrop.addEventListener("click", closeModal);
    window.addEventListener("resize", () => {
        if (!modal.classList.contains("hidden")) {
            resizeCanvas();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.classList.contains("hidden")) {
            closeModal();
        }
    });
})();
