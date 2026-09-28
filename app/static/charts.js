// Dashboard charts (Chart.js). Colors come from CSS tokens so light/dark mode stay in sync.
(function () {
  const el = document.getElementById("chart-data");
  if (!el || !window.Chart) return;
  const data = JSON.parse(el.textContent);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const theme = () => ({
    s1: css("--series-1"), s2: css("--series-2"), s3: css("--series-3"),
    grid: css("--grid"), muted: css("--muted"), surface: css("--surface"), ink: css("--ink"),
  });

  function baseOptions(t) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink, borderColor: t.grid, borderWidth: 1 },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: t.muted, maxRotation: 0, autoSkip: true } },
        y: { stacked: true, beginAtZero: true, grid: { color: t.grid }, border: { display: false },
             ticks: { color: t.muted, precision: 0 } },
      },
    };
  }

  const bar = (label, values, color, t) => ({
    label, data: values, backgroundColor: color, borderColor: t.surface, borderWidth: { top: 2 },
    borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 36,
  });

  const charts = [];
  function draw() {
    charts.forEach((c) => c.destroy());
    charts.length = 0;
    const t = theme();
    const dayLabels = data.by_day.map((d) => {
      const [y, m, dd] = d.date.split("-").map(Number);
      return new Date(y, m - 1, dd).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    });
    const byDay = document.getElementById("chart-by-day");
    if (byDay) {
      charts.push(new Chart(byDay, {
        type: "bar",
        data: { labels: dayLabels, datasets: [
          bar("Answered", data.by_day.map((d) => d.answered), t.s1, t),
          bar("Missed", data.by_day.map((d) => d.missed), t.s2, t),
          bar("Outbound", data.by_day.map((d) => d.outbound), t.s3, t),
        ] },
        options: baseOptions(t),
      }));
    }
    const byHour = document.getElementById("chart-by-hour");
    if (byHour) {
      const hourLabel = (h) => (h % 12 || 12) + (h < 12 ? "a" : "p");
      charts.push(new Chart(byHour, {
        type: "bar",
        data: { labels: data.by_hour.map((h) => hourLabel(h.hour)), datasets: [
          bar("Answered", data.by_hour.map((h) => h.answered), t.s1, t),
          bar("Missed", data.by_hour.map((h) => h.missed), t.s2, t),
        ] },
        options: baseOptions(t),
      }));
    }
  }
  draw();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();
