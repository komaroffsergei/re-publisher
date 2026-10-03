/* Только чтение результата и явный повтор OCR. Текст не вставляется как HTML. */
(() => {
  const panel = document.querySelector("[data-ocr-panel]");
  if (!panel) return;
  const output = panel.querySelector("[data-ocr-output]");
  const button = panel.querySelector("[data-ocr-retry]");
  const states = {
    not_started: "OCR ещё не запускался", queued: "OCR в очереди", running: "Распознаём надписи",
    complete: "OCR готов", no_text: "Надписей не найдено", needs_review: "Нужен ручной разбор",
    failed: "Ошибка OCR", stale: "Медиа изменилось — нужен новый OCR", waiting_media: "Ждём медиа",
  };
  let loading = false;
  async function refresh(retry = false) {
    if (loading || document.hidden || (!panel.open && !retry)) return;
    loading = true; button.disabled = true;
    try {
      const response = await fetch(`/api/pipeline/${panel.dataset.ocrPanel}/ocr`, retry ?
        {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}", credentials: "same-origin"} :
        {cache: "no-store", credentials: "same-origin"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "OCR недоступен");
      const current = data.runs.find(run => run.current);
      const signature = JSON.stringify(data);
      if (output.dataset.signature !== signature) {
        output.dataset.signature = signature; output.replaceChildren();
        const status = document.createElement("p");
        status.textContent = `${states[data.status] || data.status}${current?.elapsed_ms == null ? "" : ` · OCR: ${current.elapsed_ms} мс`}`;
        output.append(status);
        if (data.error) {const error = document.createElement("p"); error.textContent = data.error; error.className = "notice error"; output.append(error);}
        for (const [number, item] of (current?.results || []).entries()) {
          const title = document.createElement("h4"); title.textContent = `Медиа ${number + 1} · ${item.engine_version}`;
          const text = document.createElement("pre"); text.textContent = item.text || "Надписей не найдено";
          const small = document.createElement("small"); small.textContent = (item.blocks || []).map(b => `${b.text} (${Math.round(b.score * 100)}%)`).join(" · ");
          output.append(title, text, small);
        }
        const previous = data.runs.filter(run => !run.current);
        if (previous.length) {
          const history = document.createElement("details");
          const title = document.createElement("summary"); title.textContent = `Предыдущие запуски OCR · ${previous.length}`;
          history.append(title);
          for (const run of previous) {
            const row = document.createElement("p");
            row.textContent = `#${run.id} · ${states[run.status] || run.status} · ${run.elapsed_ms ?? "—"} мс · ${run.engine_version} · ${run.finished_at ? new Date(run.finished_at).toLocaleString("ru-RU") : "не завершён"}`;
            history.append(row);
          }
          output.append(history);
        }
      }
      button.disabled = ["queued", "running"].includes(data.status);
    } catch (error) {output.textContent = error.message; button.disabled = false;}
    finally {loading = false;}
  }
  panel.addEventListener("toggle", () => refresh());
  button.addEventListener("click", () => refresh(true));
  setInterval(() => refresh(), 5000);
})();
