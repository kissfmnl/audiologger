document.addEventListener('DOMContentLoaded', () => {
    initSidebarNav();
    initCountrySelects();
    initEventToggles();
    initLogoModal();
    initDeleteButtons();
    scrollToFocus();
    hideDeleteToast();
});

function initSidebarNav() {
    document.querySelectorAll('[data-scroll-target]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            const target = document.getElementById(btn.dataset.scrollTarget);
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
            document.querySelectorAll('[data-scroll-target]').forEach((el) => {
                el.classList.remove('bg-purple-100', 'text-purple-700');
                el.classList.add('text-body');
            });
            btn.classList.add('bg-purple-100', 'text-purple-700');
            btn.classList.remove('text-body');
        });
    });
}

function initCountrySelects() {
    document.querySelectorAll('.country-select').forEach((select) => {
        const formId = select.getAttribute('form') || select.closest('tr')?.querySelector('[form]')?.getAttribute('form');
        const flagEl = document.querySelector(`.country-flag[data-for="${formId}"]`)
            || select.closest('tr')?.querySelector('.country-flag');

        const updateFlag = () => {
            const option = select.selectedOptions[0];
            if (flagEl && option) {
                flagEl.textContent = option.dataset.flag || '📻';
            }
        };
        select.addEventListener('change', updateFlag);
        updateFlag();
    });
}

function initEventToggles() {
    document.querySelectorAll('.station-row').forEach((row) => {
        const toggle = row.querySelector('.event-toggle');
        const dates = row.querySelector('.event-dates');
        const placeholder = row.querySelector('.event-placeholder');
        if (!toggle || !dates) return;

        const sync = () => {
            const on = toggle.checked;
            dates.classList.toggle('hidden', !on);
            if (placeholder) placeholder.classList.toggle('hidden', on);
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
            const row = document.getElementById(`station-${stationId}`);
            btn.disabled = true;
            btn.textContent = '…';

            try {
                const response = await fetch(`/admin/stations/${stationId}/delete`, {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'fetch' },
                });
                if (!response.ok) {
                    throw new Error('Verwijderen mislukt');
                }
                if (row) {
                    row.style.opacity = '0';
                    row.style.transition = 'opacity 0.2s';
                    setTimeout(() => row.remove(), 200);
                }
            } catch (err) {
                btn.disabled = false;
                btn.textContent = '×';
                alert(err.message);
            }
        });
    });
}

function scrollToFocus() {
    const focusId = window.ADMIN_FOCUS_ID;
    if (!focusId) return;
    const row = document.getElementById(`station-${focusId}`);
    if (row) {
        requestAnimationFrame(() => row.scrollIntoView({ behavior: 'smooth', block: 'center' }));
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

    applyBtn.addEventListener('click', () => {
        if (!cropper || !activeFormId) return;
        const canvas = cropper.getCroppedCanvas({
            width: 1080,
            height: 1080,
            imageSmoothingEnabled: true,
            imageSmoothingQuality: 'high',
        });
        const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
        const hidden = document.querySelector(`#logo-data-${activeFormId.replace('form-', '')}`)
            || document.getElementById(`logo-data-${activeFormId.replace('form-', '')}`);

        const formSuffix = activeFormId.replace('form-', '');
        const input = document.getElementById(`logo-data-${formSuffix}`);
        if (input) input.value = dataUrl;

        const btn = document.querySelector(`.logo-open-btn[data-form-id="${activeFormId}"]`);
        if (btn) {
            btn.innerHTML = `<img src="${dataUrl}" class="w-10 h-10 rounded-lg object-cover border border-purple-300" alt="Logo">`;
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
