(function () {
    const modal = document.getElementById("player-modal");
    const backdrop = document.getElementById("player-modal-backdrop");
    const closeBtn = document.getElementById("player-modal-close");
    const titleEl = document.getElementById("player-modal-title");
    const subtitleEl = document.getElementById("player-modal-subtitle");
    const waveformEl = document.getElementById("player-modal-waveform");
    const loadingEl = document.getElementById("player-modal-loading");
    const controlsEl = document.getElementById("player-modal-controls");
    const playBtn = document.getElementById("player-modal-play");
    const playIcon = document.getElementById("player-modal-play-icon");
    const pauseIcon = document.getElementById("player-modal-pause-icon");
    const currentTimeEl = document.getElementById("player-modal-current");
    const totalTimeEl = document.getElementById("player-modal-total");
    const trimLink = document.getElementById("player-modal-trim");

    if (!modal || typeof WaveSurfer === "undefined") {
        return;
    }

    const peaksCache = new Map();
    const peaksInflight = new Map();
    let wavesurfer = null;
    let activeButton = null;
    let activeAudio = null;

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

    function destroyPlayer() {
        if (wavesurfer) {
            wavesurfer.destroy();
            wavesurfer = null;
        }
        if (activeAudio) {
            activeAudio.pause();
            activeAudio.removeAttribute("src");
            activeAudio.load();
            activeAudio = null;
        }
        waveformEl.innerHTML = "";
    }

    function setPlayingState(playing) {
        playIcon.classList.toggle("hidden", playing);
        pauseIcon.classList.toggle("hidden", !playing);
    }

    function revealControls(duration) {
        loadingEl.classList.add("hidden");
        controlsEl.classList.remove("opacity-40", "pointer-events-none");
        if (duration) {
            totalTimeEl.textContent = formatTime(duration);
        }
        if (activeButton) {
            activeButton.disabled = false;
        }
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

    async function fetchPeaks(url) {
        if (peaksCache.has(url)) {
            return peaksCache.get(url);
        }
        if (peaksInflight.has(url)) {
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
                if (!data.is_live) {
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

    function prefetchAllVisible() {
        document.querySelectorAll(".listen-btn").forEach((button) => {
            prefetchPeaks(button.dataset.peaksUrl);
        });
    }

    async function openPlayer(button) {
        const peaksUrl = button.dataset.peaksUrl;
        const fallbackTitle = button.dataset.title || "Opname";
        if (!peaksUrl) {
            return;
        }

        activeButton = button;
        button.disabled = true;

        destroyPlayer();
        modal.classList.remove("hidden");
        document.body.classList.add("overflow-hidden");
        loadingEl.classList.remove("hidden");
        controlsEl.classList.add("opacity-40", "pointer-events-none");
        titleEl.textContent = fallbackTitle;
        subtitleEl.textContent = "Wavevorm laden…";
        totalTimeEl.textContent = "0:00";
        currentTimeEl.textContent = "0:00";
        trimLink.classList.add("hidden");
        setPlayingState(false);

        try {
            const data = await fetchPeaks(peaksUrl);
            titleEl.textContent = data.title || fallbackTitle;
            subtitleEl.textContent = data.is_live ? "Live opname" : "Volledig uur";

            if (data.recording_id && !data.is_live) {
                trimLink.href = `/player/${data.recording_id}`;
                trimLink.classList.remove("hidden");
            }

            activeAudio = new Audio(data.audio_url);
            activeAudio.preload = "auto";
            activeAudio.crossOrigin = "anonymous";

            wavesurfer = WaveSurfer.create({
                container: waveformEl,
                media: activeAudio,
                waveColor: "#D1D5DB",
                progressColor: "#7C3AED",
                cursorColor: "#6D28D9",
                barWidth: 1,
                barGap: 0,
                barRadius: 0,
                height: 128,
                normalize: true,
                fillParent: true,
                interact: true,
            });

            let revealed = false;
            const showWaveform = () => {
                if (revealed) {
                    return;
                }
                revealed = true;
                revealControls(data.duration);
            };

            wavesurfer.on("redraw", showWaveform);
            wavesurfer.on("decode", showWaveform);

            wavesurfer.on("ready", () => {
                showWaveform();
                totalTimeEl.textContent = formatTime(wavesurfer.getDuration() || data.duration);
            });

            wavesurfer.on("timeupdate", (time) => {
                currentTimeEl.textContent = formatTime(time);
            });

            wavesurfer.on("play", () => setPlayingState(true));
            wavesurfer.on("pause", () => setPlayingState(false));
            wavesurfer.on("finish", () => setPlayingState(false));

            wavesurfer.on("error", () => {
                subtitleEl.textContent = "Afspelen mislukt";
                showWaveform();
            });

            const loadPromise = data.peaks && data.peaks.length > 0
                ? wavesurfer.load(data.audio_url, [data.peaks], data.duration)
                : wavesurfer.load(data.audio_url);

            loadPromise.catch(() => {
                subtitleEl.textContent = "Afspelen mislukt";
            });

            requestAnimationFrame(showWaveform);
        } catch (error) {
            subtitleEl.textContent = error.message || "Laden mislukt";
            revealControls();
        }
    }

    document.querySelectorAll(".listen-btn").forEach((button) => {
        button.addEventListener("click", () => openPlayer(button));
        button.addEventListener("mouseenter", () => prefetchPeaks(button.dataset.peaksUrl), { once: true });
    });

    prefetchAllVisible();

    playBtn.addEventListener("click", () => {
        if (wavesurfer) {
            wavesurfer.playPause();
        }
    });

    closeBtn.addEventListener("click", closeModal);
    backdrop.addEventListener("click", closeModal);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.classList.contains("hidden")) {
            closeModal();
        }
    });
})();
