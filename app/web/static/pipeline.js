/* One poller for the board/detail. Reads do not assign marks or start processing. */
(() => {
  "use strict";
  const root = document.querySelector("[data-pipeline]");
  if (!root) return;
  const detail = root.dataset.detail === "true";
  const stages = {
    received: "Не готовы",
    sorted: "Отсортирован",
    filtered: "Отфильтрован",
    marking: "Маркировка",
    ready: "На публикацию",
  };
  const names = { tfidf: "TF-IDF", minilm: "MiniLM", media: "Медиа" };
  const statuses = {
    queued: "В очереди",
    running: "В работе",
    complete: "Готово",
    media_only: "Только медиа",
    failed: "Ошибка",
    stale: "Текст изменён",
  };
  const records = () => Array.from(root.querySelectorAll(".pipeline-record"));
  function render(record, state) {
    const previous = record.dataset.stage;
    record.currentState = state;
    record.dataset.stage = state.stage;
    const busy = state.active || record.pendingAction;
    record.classList.toggle("is-busy", Boolean(busy));
    record.setAttribute("aria-busy", String(Boolean(busy)));
    record.querySelectorAll("[data-taxonomy]").forEach((button) => {
      button.disabled =
        busy || state.deleted || button.dataset.modelEnabled !== "true";
    });
    const panel = record.querySelector("[data-model-statuses]");
    const signature = JSON.stringify(state.taxonomies);
    if (panel.dataset.signature !== signature) {
      panel.dataset.signature = signature;
      panel.replaceChildren();
      for (const key of Object.keys(names)) {
        const job = state.taxonomies[key];
        if (!job) continue;
        const line = document.createElement("p");
        line.className = `model-state state-${job.status}`;
        const uncertainty =
          job.result?.review_status === "needs_review" ? " · Не уверен" : "";
        line.textContent = `${names[key]} · ${statuses[job.status] || job.status}${uncertainty}${job.elapsed_ms == null ? "" : ` · ${job.elapsed_ms} мс`}`;
        if (!detail && job.result?.top_3?.length) {
          const category = job.result.top_3[0];
          const score = document.createElement("small");
          score.textContent = `${category.name} · ${Math.round(category.score * 100)}%`;
          score.title = "Оценка модели, не вероятность правильного ответа";
          line.append(document.createElement("br"), score);
        }
        if (job.error) {
          const error = document.createElement("span");
          error.textContent = job.error;
          line.append(document.createElement("br"), error);
        }
        if (detail && job.model_version) {
          const version = document.createElement("small");
          version.textContent = job.model_version;
          line.append(document.createElement("br"), version);
        }
        panel.append(line);
      }
    }
    window.renderPostSelection?.(record, state.selection, Boolean(busy));
    const source = record.querySelector("[data-mark-source]");
    if (source) {
      source.disabled = busy || !state.can_mark_source;
      if (!detail) source.hidden = !state.can_mark_source;
    }
    const autoError = record.querySelector("[data-automation-error]");
    if (autoError) {autoError.hidden = !state.auto_error; autoError.textContent = state.auto_error || "";}
    const retry = record.querySelector("[data-automation-retry]");
    if (retry) {retry.hidden = !state.can_retry; retry.disabled = busy || !state.can_retry;}
    const ready = record.querySelector("[data-ready-at]");
    if (ready) ready.textContent = state.ready_at ? `Подготовлен · ${new Date(state.ready_at).toLocaleString("ru-RU")} · отправка выключена` : "";
    if (detail) {
      record.querySelector("[data-marked-text]").textContent = state.marked_text || "";
      const url = record.querySelector("[data-marked-url]");
      if (url.dataset.url !== (state.marked_source_url || "")) {
        url.dataset.url = state.marked_source_url || "";
        url.replaceChildren();
        if (state.marked_source_url) {
          const link = document.createElement("a"); link.href = state.marked_source_url;
          link.textContent = state.marked_source_url; link.target = "_blank"; link.rel = "noopener"; url.append(link);
        }
      }
      root.querySelector("[data-stage-label]").textContent =
        stages[state.stage] || state.stage;
      record.querySelector("#source-marking").hidden = ![
        "filtered",
        "marking",
        "ready",
      ].includes(state.stage);
    } else if (previous !== state.stage) {
      if (root.dataset.filtered === "true") {
        record.remove();
        document.querySelector("[data-poll-status]").textContent =
          "Стадия изменилась. Обнови выборку кнопкой «Показать».";
      } else {
        const destination = root.querySelector(
          `[data-column="${state.stage}"] .column-cards`,
        );
        if (destination) destination.prepend(record);
        else record.remove();
      }
      root.querySelectorAll("[data-column]").forEach((col) => {
        col.querySelector(".column-empty").hidden = Boolean(
          col.querySelector(".pipeline-record"),
        );
      });
    }
  }
  async function request(url, data) {
    const response = await fetch(
      url,
      data === undefined
        ? { cache: "no-store" }
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
            credentials: "same-origin",
          },
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Ошибка запроса");
    return result;
  }
  let loading = false;
  async function refresh() {
    if (loading || document.hidden) return;
    const current = records();
    if (detail && !current.length) return;
    if (current.some((record) => record.pendingAction)) return;
    loading = true;
    try {
      if (detail) {
        const data = await request(`/api/pipeline/board-state?entry_ids=${current.map(node => node.dataset.entryId).join(",")}`);
        for (const record of current) {
          const state = data.entries[record.dataset.entryId];
          if (state) render(record, state);
        }
      } else {
        const response = await fetch(`/api/pipeline/board-fragment${location.search}`, {cache:"no-store",credentials:"same-origin"});
        if (!response.ok) throw new Error("Ошибка обновления доски");
        const fragment = document.createElement("template"); fragment.innerHTML = await response.text();
        if (records().some(record => record.pendingAction)) return;
        // Позицию берём после запроса: пользователь мог скроллить во время загрузки.
        const x = window.scrollX, y = window.scrollY, boardX = root.scrollLeft;
        const columns = new Map(Array.from(root.querySelectorAll("[data-column]")).map(col => [col.dataset.column, col.scrollTop]));
        const expanded = new Set(Array.from(root.querySelectorAll("details[open]")).map(node => `${node.closest(".pipeline-record")?.dataset.entryId}:${node.className}`));
        root.replaceChildren(fragment.content);
        initialize();
        root.querySelectorAll("details").forEach(node => {if (expanded.has(`${node.closest(".pipeline-record")?.dataset.entryId}:${node.className}`)) node.open = true;});
        root.querySelectorAll("[data-column]").forEach(col => col.scrollTop = columns.get(col.dataset.column) || 0);
        root.scrollLeft = boardX; window.scrollTo(x,y);
      }
      const status = document.querySelector("[data-poll-status]");
      if (status) status.textContent = "Карточки и счётчики обновлены. Отправка выключена.";
    } catch (error) {
      const status =
        document.querySelector("[data-poll-status]") ||
        root.querySelector("[data-action-feedback]");
      if (status)
        status.textContent = `Не удалось обновить состояния: ${error.message}`;
    } finally {
      loading = false;
    }
  }
  function initialize() {
  for (const record of records()) {
    render(
      record,
      JSON.parse(record.querySelector("[data-record-initial]").textContent),
    );
  }
  }
  initialize();
  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-taxonomy], [data-mark-source], [data-automation-retry]");
    if (!button || button.disabled) return;
    const record = button.closest(".pipeline-record");
    if (record.pendingAction) return;
    record.pendingAction = true;
    const feedback = record.querySelector("[data-action-feedback]");
    feedback.textContent = "";
    const state = record.currentState;
    record
      .querySelectorAll("[data-taxonomy], [data-mark-source], [data-automation-retry]")
      .forEach((node) => {
        node.disabled = true;
      });
    record.classList.add("is-busy");
    record.setAttribute("aria-busy", "true");
    try {
      if (button.hasAttribute("data-automation-retry")) {
        await request(`/api/pipeline/${record.dataset.entryId}/automation/retry`, {});
      } else if (button.hasAttribute("data-mark-source")) {
        const data = await request(
          `/api/pipeline/${record.dataset.entryId}/mark-source`,
          {},
        );
        if (detail) {
          const link = document.createElement("a");
          link.href = data.source_url;
          link.textContent = data.source_url;
          link.target = "_blank";
          link.rel = "noopener";
          record.querySelector("[data-marked-url]").replaceChildren(link);
          record.querySelector("[data-marked-text]").textContent =
            data.marked_text;
          record.querySelector("[data-marked-text]").closest("details").open =
            true;
        }
      } else {
        const job = await request(
          `/api/pipeline/${record.dataset.entryId}/taxonomy/${button.dataset.taxonomy}`,
          {},
        );
        render(record, {
          ...state,
          active: ["queued", "running"].includes(job.status),
          taxonomies: { ...state.taxonomies, [button.dataset.taxonomy]: job },
        });
      }
    } catch (error) {
      feedback.textContent = error.message;
      render(record, state);
    } finally {
      record.pendingAction = false;
      render(record, record.currentState);
      await refresh();
    }
  });
  let runSignature = "";
  if (detail) {
    const record = records()[0];
    const observer = new MutationObserver(() => {
      const state = record.currentState;
      const signature = JSON.stringify(
        Object.values(state?.taxonomies || {}).map((job) => [
          job.status,
          job.run_id,
          job.finished_at,
        ]),
      );
      if (signature !== runSignature) {
        runSignature = signature;
        root.querySelector("[data-taxonomy-compare]")?.refreshTaxonomyRuns?.();
      }
    });
    observer.observe(record.querySelector("[data-model-statuses]"), {
      childList: true,
    });
  }
  document.addEventListener("selection-changed", refresh);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  setInterval(refresh, 5000);
})();
