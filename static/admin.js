document.addEventListener('DOMContentLoaded', () => {
    initStationCollapsible();
    initEditMode();
    initCountrySelects();
    initEventToggles();
    initLogoModal();
    initDeleteButtons();
    scrollToFocus();
    hideDeleteToast();
});

function initStationCollapsible() {
    document.querySelectorAll('.station-collapse-toggle').forEach((btn) => {
        btn.addEventListener('click', () => {
            const card = btn.closest('.station-card');
            if (!card || card.classList.contains('is-editing')) return;
            card.classList.toggle('is-collapsed');
        });
    });

    document.getElementById('expand-all-stations')?.addEventListener('click', () => {
        document.querySelectorAll('.station-card').forEach((card) => {
            if (!card.classList.contains('is-editing')) {
                card.classList.remove('is-collapsed');
            }
        });
    });

    document.getElementById('collapse-all-stations')?.addEventListener('click', () => {
        document.querySelectorAll('.station-card').forEach((card) => {
            if (!card.classList.contains('is-editing')) {
                card.classList.add('is-collapsed');
            }
        });
    });
}

function initEditMode() {
    let openCard = null;

    const closeEdit = (card) => {
        if (!card) return;
        card.querySelector('.station-view')?.classList.remove('hidden');
        card.querySelector('.station-edit')?.classList.add('hidden');
        card.classList.remove('is-editing');
        card.classList.add('is-collapsed');
        if (openCard === card) openCard = null;
    };

    const openEdit = (card) => {
        if (openCard && openCard !== card) closeEdit(openCard);
        card.classList.remove('is-collapsed');
        card.querySelector('.station-view')?.classList.add('hidden');
        card.querySelector('.station-edit')?.classList.remove('hidden');
        card.classList.add('is-editing');
        openCard = card;
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };

    document.querySelectorAll('.station-card').forEach((card) => {
        card.querySelector('.edit-station-btn')?.addEventListener('click', () => openEdit(card));
        card.querySelector('.cancel-edit-btn')?.addEventListener('click', () => closeEdit(card));
    });

    window.openStationEdit = openEdit;
    window.closeStationEdit = closeEdit;
}

function initCountrySelects() {
    document.querySelectorAll('.station-row').forEach((row) => {
        const select = row.querySelector('.country-select');
        const timezoneSelect = row.querySelector('.timezone-select');
        const flagEl = row.querySelector('.country-flag');
        if (!select) return;

        const updateFromCountry = () => {
            const option = select.selectedOptions[0];
            if (flagEl && option) {
                flagEl.textContent = option.dataset.flag || '📻';
            }
            if (timezoneSelect && option?.dataset.defaultTimezone && !select.dataset.userPickedTimezone) {
                timezoneSelect.value = option.dataset.defaultTimezone;
            }
        };

        select.addEventListener('change', updateFromCountry);
        timezoneSelect?.addEventListener('change', () => {
            select.dataset.userPickedTimezone = '1';
        });

        if (flagEl) {
            const option = select.selectedOptions[0];
            if (option) flagEl.textContent = option.dataset.flag || '📻';
        }
    });
}

function initEventToggles() {
    document.querySelectorAll('.station-row').forEach((row) => {
        const toggle = row.querySelector('.event-toggle');
        const dates = row.querySelector('.event-dates');
        if (!toggle || !dates) return;

        const sync = () => {
            const on = toggle.checked;
            dates.classList.toggle('hidden', !on);
            row.querySelectorAll('.event-hint').forEach((el) => el.classList.toggle('hidden', !on));
        };

        toggle.addEventListener('change', sync);
        sync();
    });
}

function hideDeleteToast() {
    const toast = document.getElementById('delete-toast');
    if (!toast) return;
    setTimeout(() => toast.remove(), 3000);
}

function initDeleteButtons() {
    document.querySelectorAll('.delete-station-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const stationId = btn.dataset.stationId;
            const card = document.getElementById(`station-${stationId}`);
            btn.disabled = true;
            const originalText = btn.textContent;
            btn.textContent = '…';

            try {
                const response = await fetch(`/admin/stations/${stationId}/delete`, {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'fetch' },
                });
                if (!response.ok) {
                    throw new Error('Verwijderen mislukt');
                }
                if (card) {
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.98)';
                    card.style.transition = 'opacity 0.2s, transform 0.2s';
                    setTimeout(() => card.remove(), 200);
                }
            } catch (err) {
                btn.disabled = false;
                btn.textContent = originalText;
                alert(err.message);
            }
        });
    });
}

function scrollToFocus() {
    const focusId = window.ADMIN_FOCUS_ID;
    if (!focusId) return;
    const card = document.getElementById(`station-${focusId}`);
    if (card) {
        card.classList.remove('is-collapsed');
        requestAnimationFrame(() => card.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    }
}

let activeFormId = null;
let cropper = null;

function initLogoModal() {
    const modal = document.getElementById('logo-modal');
    const fileInput = document.getElementById('logo-file-input');
    const cropArea = document.getElementById('logo-crop-area');
    const cropImage = document.getElementById('logo-crop-image');
    const zoomSlider = document.getElementById('logo-zoom-slider');
    const applyBtn = document.getElementById('logo-modal-apply');
    const cancelBtn = document.getElementById('logo-modal-cancel');

    document.querySelectorAll('.logo-open-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            activeFormId = btn.dataset.formId;
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            fileInput.value = '';
            cropArea.classList.add('hidden');
            applyBtn.disabled = true;
            if (cropper) {
                cropper.destroy();
                cropper = null;
            }
        });
    });

    cancelBtn.addEventListener('click', closeLogoModal);
    modal.addEventListener('click', (event) => {
        if (event.target === modal) closeLogoModal();
    });

    fileInput.addEventListener('change', () => {
        const file = fileInput.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = () => {
            cropImage.src = reader.result;
            cropArea.classList.remove('hidden');
            applyBtn.disabled = false;
            if (cropper) cropper.destroy();
            cropper = new Cropper(cropImage, {
                aspectRatio: 1,
                viewMode: 1,
                dragMode: 'move',
                autoCropArea: 1,
                responsive: true,
                background: false,
            });
            zoomSlider.value = '1';
        };
        reader.readAsDataURL(file);
    });

    zoomSlider.addEventListener('input', () => {
        if (cropper) cropper.zoomTo(parseFloat(zoomSlider.value));
    });

    applyBtn.addEventListener('click', async () => {
        if (!cropper || !activeFormId) return;
        const canvas = cropper.getCroppedCanvas({
            width: 1080,
            height: 1080,
            imageSmoothingEnabled: true,
            imageSmoothingQuality: 'high',
        });
        const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
        const formSuffix = activeFormId.replace('form-', '');
        const input = document.getElementById(`logo-data-${formSuffix}`);
        if (input) input.value = dataUrl;

        const btn = document.querySelector(`.logo-open-btn[data-form-id="${activeFormId}"]`);
        if (btn) {
            btn.innerHTML = `<img src="${dataUrl}" class="w-16 h-16 object-cover rounded-xl" alt="Logo">`;
        }

        if (formSuffix !== 'new') {
            applyBtn.disabled = true;
            applyBtn.textContent = 'Opslaan…';
            try {
                const response = await fetch(`/admin/stations/${formSuffix}/logo`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'fetch',
                    },
                    body: JSON.stringify({ logo_data: dataUrl }),
                });
                if (!response.ok) {
                    const err = await response.json();
                    throw new Error(err.detail || 'Logo opslaan mislukt');
                }
            } catch (err) {
                alert(err.message);
            } finally {
                applyBtn.disabled = false;
                applyBtn.textContent = 'Toepassen';
            }
        }

        closeLogoModal();
    });
}

function closeLogoModal() {
    const modal = document.getElementById('logo-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    activeFormId = null;
}
