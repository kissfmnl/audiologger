(function () {
    const STORAGE_KEY = "audiologger_dashboard_view";
    const tableView = document.getElementById("stations-table-view");
    const cardsView = document.getElementById("stations-cards-view");
    const toggleButtons = document.querySelectorAll("[data-dashboard-view]");

    if (!tableView || !cardsView || !toggleButtons.length) {
        return;
    }

    function setView(view) {
        const isTable = view === "table";
        tableView.classList.toggle("hidden", !isTable);
        cardsView.classList.toggle("hidden", isTable);

        toggleButtons.forEach((button) => {
            const active = button.dataset.dashboardView === view;
            button.classList.toggle("bg-purple-600", active);
            button.classList.toggle("text-white", active);
            button.classList.toggle("shadow-sm", active);
            button.classList.toggle("bg-surface", !active);
            button.classList.toggle("text-body", !active);
            button.classList.toggle("border", !active);
            button.classList.toggle("border-gray-200", !active);
            button.classList.toggle("hover:bg-gray-100", !active);
        });

        localStorage.setItem(STORAGE_KEY, view);
    }

    const saved = localStorage.getItem(STORAGE_KEY);
    setView(saved === "cards" ? "cards" : "table");

    toggleButtons.forEach((button) => {
        button.addEventListener("click", () => setView(button.dataset.dashboardView));
    });
})();
