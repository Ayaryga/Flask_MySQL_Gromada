document.addEventListener("DOMContentLoaded", () => {
    const animated = document.querySelectorAll(".feature-card, .news-card, .event-card, .partner-row div, .support-grid, .donate-grid");

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add("visible");
            }
        });
    }, { threshold: 0.15 });

    animated.forEach(el => {
        el.classList.add("reveal");
        observer.observe(el);
    });

    document.querySelectorAll("[data-count]").forEach(counter => {
        const target = Number(counter.dataset.count);
        let current = 0;
        const step = Math.max(1, Math.ceil(target / 40));
        const timer = setInterval(() => {
            current += step;
            if (current >= target) {
                current = target;
                clearInterval(timer);
            }
            counter.textContent = current;
        }, 35);
    });
});
