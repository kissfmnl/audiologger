document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.station-form').forEach(initStationForm);
});

function initStationForm(form) {
    const fileInput = form.querySelector('.logo-file-input');
    const dataInput = form.querySelector('.logo-data-input');
    const cropArea = form.querySelector('.logo-crop-area');
    const cropImage = form.querySelector('.logo-crop-image');
    const zoomSlider = form.querySelector('.logo-zoom-slider');
    const applyBtn = form.querySelector('.logo-apply-btn');
    const previewWrap = form.querySelector('.logo-preview-wrap');
    const previewImg = form.querySelector('.logo-preview');
    const scheduleRadios = form.querySelectorAll('input[name="schedule_mode"]');
    const hourCheckboxes = form.querySelectorAll('.schedule-hour-checkbox');

    let cropper = null;

    function updateHourCheckboxes() {
        const custom = form.querySelector('input[name="schedule_mode"][value="custom"]')?.checked;
        hourCheckboxes.forEach((cb) => {
            cb.disabled = !custom;
            cb.closest('label').style.opacity = custom ? '1' : '0.4';
        });
    }

    scheduleRadios.forEach((radio) => radio.addEventListener('change', updateHourCheckboxes));
    updateHourCheckboxes();

    if (!fileInput) return;

    fileInput.addEventListener('change', (event) => {
        const file = event.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = () => {
            cropImage.src = reader.result;
            cropArea.classList.remove('hidden');
            previewWrap.classList.add('hidden');
            dataInput.value = '';

            if (cropper) {
                cropper.destroy();
            }

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

    zoomSlider?.addEventListener('input', () => {
        if (cropper) {
            cropper.zoomTo(parseFloat(zoomSlider.value));
        }
    });

    applyBtn?.addEventListener('click', () => {
        if (!cropper) return;

        const canvas = cropper.getCroppedCanvas({
            width: 1080,
            height: 1080,
            imageSmoothingEnabled: true,
            imageSmoothingQuality: 'high',
        });

        if (!canvas) return;

        previewImg.src = canvas.toDataURL('image/jpeg', 0.92);
        dataInput.value = previewImg.src;
        previewWrap.classList.remove('hidden');
    });

    form.addEventListener('submit', (event) => {
        const custom = form.querySelector('input[name="schedule_mode"][value="custom"]')?.checked;
        if (custom) {
            const checked = Array.from(hourCheckboxes).some((cb) => cb.checked);
            if (!checked) {
                event.preventDefault();
                alert('Selecteer minimaal één uur, of kies "Elk heel uur".');
            }
        }
    });
}
