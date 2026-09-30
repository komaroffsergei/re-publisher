/* Compare persisted runs. All labels and scores are rendered as text, never as HTML. */
(() => {
  const names = { minilm: "MiniLM", tfidf: "TF-IDF" };
  const complete = (run) =>
    run.model_key in names && run.status === "complete" && run.result;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const percent = (score) =>
    score == null ? "—" : `${Math.round(score * 100)}%`;
  const stamp = (value) =>
    value
      ? new Date(value).toLocaleString("ru-RU", {
          dateStyle: "short",
          timeStyle: "short",
        })
      : "без даты";
  const latest = (runs, key) =>
    runs.find((run) => run.model_key === key && complete(run));

  function scoreMap(run, section, catalog) {
    const map = new Map();
    if (!run) return map;
    if (catalog && run.result.scores) {
      for (const item of catalog.filter((item) =>
        section === "features"
          ? item.kind === "feature"
          : item.kind !== "feature",
      )) {
        map.set(item.id, {
          name: item.name,
          score: run.result.scores[item.id],
          depth: item.parent ? 1 : 0,
        });
      }
      return map;
    }
    if (section === "categories") {
      for (const category of run.result.top_3 || []) {
        map.set(category.id, {
          name: category.name,
          score: category.score,
          depth: 0,
        });
        for (const child of category.subcategories || []) {
          map.set(child.id, { name: child.name, score: child.score, depth: 1 });
        }
      }
    } else {
      for (const feature of run.result.features || []) {
        map.set(feature.id, {
          name: feature.name,
          score: feature.score,
          depth: 0,
        });
      }
    }
    return map;
  }

  function comparisonRow(id, left, right) {
    const info = left.get(id) || right.get(id);
    const row = element(
      "div",
      `compare-row ${info.depth ? "compare-sub" : ""}`,
    );
    row.append(element("span", "compare-label", info.name));
    const scores = element("div", "compare-scores");
    for (const [key, value] of [
      ["minilm", left.get(id)?.score],
      ["tfidf", right.get(id)?.score],
    ]) {
      const cell = element("div", `compare-score compare-${key}`);
      const bar = element("span", "compare-bar");
      bar.style.width = `${Math.max(0, Math.min(100, Math.round((value || 0) * 100)))}%`;
      cell.append(bar, element("strong", "", percent(value)));
      cell.title = `${names[key]}: ${percent(value)}`;
      scores.append(cell);
    }
    row.append(scores);
    return row;
  }

  function renderComparison(root, leftRun, rightRun) {
    const output = root.querySelector("[data-compare-output]");
    output.replaceChildren();
    if (!leftRun && !rightRun) {
      output.append(
        element(
          "p",
          "taxonomy-info",
          "Готовых прогонов пока нет. Запустите любую модель стрелкой выше.",
        ),
      );
      return;
    }
    const caption = element("div", "compare-head");
    caption.append(
      element("span", "compare-label", "Оценки модели · не вероятность"),
    );
    const legend = element("div", "compare-scores");
    legend.append(
      element("strong", "compare-minilm", "MiniLM"),
      element("strong", "compare-tfidf", "TF-IDF"),
    );
    caption.append(legend);
    output.append(caption);
    if ([leftRun, rightRun].some((run) => run && !run.is_current_text)) {
      output.append(
        element(
          "p",
          "taxonomy-message is-warning",
          "Есть результат по старой версии текста. Сравнение может быть некорректным.",
        ),
      );
    }
    for (const section of ["categories", "features"]) {
      const catalog = root.querySelector("[data-compare-all]").checked
        ? root.taxonomyCatalog
        : null;
      const left = scoreMap(leftRun, section, catalog);
      const right = scoreMap(rightRun, section, catalog);
      if (!left.size && !right.size) continue;
      output.append(
        element(
          "h4",
          "compare-section-title",
          section === "categories"
            ? "Категории и подкатегории"
            : "Обязательные признаки",
        ),
      );
      for (const id of new Set([...left.keys(), ...right.keys()]))
        output.append(comparisonRow(id, left, right));
    }
    output.append(
      element(
        "p",
        "compare-footer",
        `Сложность: ${leftRun?.result?.technical_complexity ?? "—"} / ${rightRun?.result?.technical_complexity ?? "—"} · Время: ${leftRun?.elapsed_ms ?? "—"} / ${rightRun?.elapsed_ms ?? "—"} мс`,
      ),
    );
  }

  async function load(root, append = false) {
    const entryId = root.dataset.taxonomyCompare;
    const url = `/api/pipeline/${encodeURIComponent(entryId)}/taxonomy-runs${append && root.dataset.nextBeforeId ? `?before_id=${root.dataset.nextBeforeId}` : ""}`;
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Историю прогонов не удалось загрузить");
    const payload = await response.json();
    root.taxonomyCatalog = payload.catalog.labels;
    const oldRuns = append ? root.taxonomyRuns || [] : [];
    root.taxonomyRuns = [...oldRuns, ...payload.runs];
    root.dataset.nextBeforeId = payload.next_before_id || "";
    const more = root.querySelector("[data-compare-more]");
    more.hidden = !payload.next_before_id;
    render(root);
  }

  function render(root) {
    const runs = root.taxonomyRuns || [];
    root.querySelector("[data-compare-count]").textContent =
      `${runs.length} сохранённых прогонов${root.dataset.nextBeforeId ? "+" : ""}`;
    const selected = {};
    for (const key of Object.keys(names)) {
      const select = root.querySelector(`[data-compare-select="${key}"]`);
      const previous = select.value;
      const last = latest(runs, key);
      select.replaceChildren();
      select.append(new Option(`Нет результата ${names[key]}`, ""));
      for (const run of runs.filter(
        (item) => item.model_key === key && complete(item),
      )) {
        const suffix =
          run.origin === "legacy_snapshot" ? " · старый снимок" : "";
        select.append(
          new Option(
            `#${run.id} · ${stamp(run.finished_at)} · ${run.elapsed_ms ?? "?"} мс${run.is_current_text ? "" : " · старый текст"}${suffix}`,
            String(run.id),
          ),
        );
      }
      select.value = runs.some(
        (run) => String(run.id) === previous && complete(run),
      )
        ? previous
        : String(last?.id || "");
      selected[key] = runs.find((run) => String(run.id) === select.value);
    }
    renderComparison(root, selected.minilm, selected.tfidf);
  }

  document.querySelectorAll("[data-taxonomy-compare]").forEach((root) => {
    const details = root.closest(".taxonomy-compare-disclosure");
    const open = () =>
      load(root).catch((error) => {
        root.querySelector("[data-compare-output]").textContent = error.message;
      });
    if (details)
      details.addEventListener("toggle", () => {
        if (details.open && !root.taxonomyRuns) open();
      });
    else open();
    root
      .querySelectorAll("[data-compare-select], [data-compare-all]")
      .forEach((select) =>
        select.addEventListener("change", () => {
          const runs = root.taxonomyRuns || [];
          const chosen = (key) =>
            runs.find(
              (run) =>
                String(run.id) ===
                root.querySelector(`[data-compare-select="${key}"]`).value,
            );
          renderComparison(root, chosen("minilm"), chosen("tfidf"));
        }),
      );
    root.querySelector("[data-compare-more]").addEventListener("click", () =>
      load(root, true).catch((error) => {
        root.querySelector("[data-compare-output]").textContent = error.message;
      }),
    );
    root.refreshTaxonomyRuns = () => {
      if (!details || details.open) open();
    };
  });
})();
