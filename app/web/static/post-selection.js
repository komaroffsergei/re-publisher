(() => {
  "use strict";
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const labels = {
    matched: "Совпало",
    rejected: "Не прошло",
    unknown: "Нет оценки",
  };
  function render(container, state, active = false) {
    const body = container.querySelector("[data-selection-body]");
    if (!body) return;
    const signature = JSON.stringify([state, active]);
    if (container.dataset.selectionSignature === signature) return;
    container.dataset.selectionSignature = signature;
    body.replaceChildren();
    const counter = container.querySelector("[data-mark-count]");
    if (counter) counter.textContent = (state.marks || []).length;
    const compact = container.dataset.compact === "true";
    const heading = container.querySelector("h3");
    if (heading) heading.hidden = !(state.marks || []).length;
    if (compact) {
      for (const mark of state.marks || []) {
        const chip = el("span", mark.name, "post-mark-chip");
        chip.style.borderColor = mark.color;
        body.append(chip);
      }
      return;
    }
    for (const mark of state.marks || []) {
      const block = el("div", undefined, "post-mark");
      const chip = el("span", mark.name, "post-mark-chip");
      chip.style.borderColor = mark.color;
      const remove = el("button", "Снять");
      remove.type = "button";
      remove.dataset.removeMark = mark.id;
      remove.disabled = active;
      remove.setAttribute("aria-label", `Снять признак ${mark.name}`);
      block.append(chip, remove);
      const sources = el("details");
      sources.append(el("summary", `Основания · ${mark.sources.length}`));
      for (const source of mark.sources) {
        sources.append(
          el(
            "p",
            `${source.name} · v${source.version} · ${source.model_key} · запуск #${source.run_id ?? "—"} · ${new Date(source.assigned_at).toLocaleString()}${source.stale ? " · прежний текст" : ""}\n${source.reason}`,
            "selection-reason",
          ),
        );
        for (const child of source.assigned?.subcategories || [])
          sources.append(
            el(
              "p",
              `${child.name}: ${child.score == null ? "нет оценки" : `${(child.score * 100).toFixed(2)}%`}`,
              "selection-reason",
            ),
          );
      }
      block.append(sources);
      body.append(block);
    }
    const checks = el("details");
    checks.append(
      el("summary", `Проверки фильтров · ${(state.checks || []).length}`),
    );
    if (state.checks?.length) body.append(checks);
    if (!state.checks?.length)
      checks.append(el("p", "Нет включённых фильтров.", "muted"));
    for (const check of state.checks || []) {
      const block = el(
        "div",
        undefined,
        `filter-check filter-check-${check.outcome}`,
      );
      block.append(
        el("strong", `${check.name}: ${labels[check.outcome]}`),
        el("p", check.reason, "selection-reason"),
      );
      checks.append(block);
    }
    const feedback = el("p", undefined, "selection-error");
    feedback.dataset.selectionFeedback = "";
    feedback.setAttribute("role", "status");
    body.append(feedback);
    const history = el("details");
    history.append(el("summary", "История лейблов"));
    history.addEventListener("toggle", async () => {
      if (!history.open || history.dataset.loaded) return;
      try {
        const response = await fetch(
          `/api/pipeline/${container.dataset.entryId}/marks/history`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("Не удалось загрузить историю");
        const data = await response.json();
        history.dataset.loaded = "true";
        if (!data.events.length)
          history.append(el("p", "История пустая", "muted"));
        for (const event of data.events)
          history.append(
            el(
              "p",
              `${event.action === "removed" ? "Снят" : "Назначен"}: ${event.mark_name} · ${new Date(event.at).toLocaleString()}${event.filter_name ? ` · ${event.filter_name} v${event.version} · ${event.model_key} · #${event.run_id}` : ""}\n${event.reason}`,
              "selection-reason",
            ),
          );
      } catch (error) {
        feedback.textContent = error.message;
      }
    });
    body.append(history);
  }
  window.renderPostSelection = (host, state, active) => {
    const node = host.matches("[data-post-selection]")
      ? host
      : host.querySelector("[data-post-selection]");
    if (node) render(node, state || { marks: [], checks: [] }, active);
  };
  document
    .querySelectorAll("[data-post-selection]")
    .forEach((node) =>
      render(
        node,
        JSON.parse(node.querySelector("[data-selection-initial]").textContent),
      ),
    );
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-remove-mark]");
    if (!button) return;
    const container = button.closest("[data-post-selection]");
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/pipeline/${container.dataset.entryId}/marks/${button.dataset.removeMark}/remove`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
          credentials: "same-origin",
        },
      );
      if (!response.ok)
        throw new Error(
          (await response.json()).detail || "Не удалось снять признак",
        );
      const stateResponse = await fetch(
        `/api/pipeline/${container.dataset.entryId}/marks`,
        { cache: "no-store" },
      );
      if (!stateResponse.ok) throw new Error("Не удалось обновить признаки");
      render(container, await stateResponse.json());
      document.dispatchEvent(new CustomEvent("selection-changed"));
    } catch (error) {
      container.querySelector("[data-selection-feedback]").textContent =
        error.message;
      button.disabled = false;
    }
  });
})();
