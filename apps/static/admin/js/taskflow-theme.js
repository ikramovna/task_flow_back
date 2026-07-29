(function () {
    "use strict";

    const storageKey = "taskflow-theme";
    const body = document.body;

    function storedTheme() {
        const candidates = [
            localStorage.getItem(storageKey),
            localStorage.getItem("theme"),
            localStorage.getItem("taskflow_theme"),
        ];
        return candidates.find((value) => value === "light" || value === "dark");
    }

    function preferredTheme() {
        return storedTheme() ||
            (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    }

    function applyTheme(theme) {
        const dark = theme === "dark";
        body.classList.toggle("taskflow-dark", dark);
        body.classList.remove("dark-mode", "theme-dark");
        body.dataset.taskflowTheme = theme;

        const button = document.getElementById("taskflow-theme-toggle");
        if (button) {
            button.innerHTML = dark
                ? '<i class="fas fa-sun" aria-hidden="true"></i>'
                : '<i class="fas fa-moon" aria-hidden="true"></i>';
            button.title = dark ? "Light mode" : "Dark mode";
            button.setAttribute("aria-label", button.title);
        }
    }

    applyTheme(preferredTheme());

    document.addEventListener("DOMContentLoaded", function () {
        const menu = document.querySelector("#jazzy-navbar .navbar-nav.ml-auto");
        if (!menu || document.getElementById("taskflow-theme-toggle")) {
            return;
        }

        const item = document.createElement("li");
        item.className = "nav-item";

        const button = document.createElement("button");
        button.type = "button";
        button.id = "taskflow-theme-toggle";
        button.addEventListener("click", function () {
            const next = body.classList.contains("taskflow-dark") ? "light" : "dark";
            localStorage.setItem(storageKey, next);
            applyTheme(next);
        });

        item.appendChild(button);
        menu.prepend(item);
        applyTheme(preferredTheme());
    });
})();
