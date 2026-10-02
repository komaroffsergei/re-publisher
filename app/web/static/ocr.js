/* Только чтение результата и явный повтор OCR. Текст не вставляется как HTML. */
(() => {
  const panel = document.querySelector("[data-ocr-panel]");
  if (!panel) return;
  const output = panel.querySelector("[data-ocr-output]");
  const button = panel.querySelector("[data-ocr-retry]");
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
        status.textContent = `${data.status}${current?.elapsed_ms == null ? "" : ` · OCR: ${current.elapsed_ms} мс`}`;
        output.append(status);
        if (data.error) {const error = document.createElement("p"); error.textContent = data.error; error.className = "notice error"; output.append(error);}
        for (const [number, item] of (current?.results || []).entries()) {
          const title = document.createElement("h4"); title.textContent = `Медиа ${number + 1} · ${item.engine_version}`;
          const text = document.createElement("pre"); text.textContent = item.text || "Надписей не найдено";
          const small = document.createElement("small"); small.textContent = (item.blocks || []).map(b => `${b.text} (${Math.round(b.score * 100)}%)`).join(" · ");
          output.append(title, text, small);
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
