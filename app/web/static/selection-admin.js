(() => {
  "use strict";
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action) => {
    const node = el("button", text);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  async function api(url, data, method = "POST") {
    const response = await fetch(
      url,
      data === undefined
        ? { cache: "no-store", credentials: "same-origin" }
        : {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
            credentials: "same-origin",
          },
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Ошибка запроса");
    return result;
  }
  function report(node, error) {
    node.textContent = error.message || String(error);
    node.className = "selection-error";
  }

  async function marksPage() {
    const form = document.getElementById("mark-form");
    if (!form) return;
    const feedback = document.getElementById("marks-feedback");
    let selected = null;
    const reset = () => {
      selected = null;
      form.reset();
      document.getElementById("mark-form-heading").textContent =
        "Новый признак";
      feedback.textContent = "";
    };
    document.getElementById("mark-new").onclick = reset;
    async function load() {
      const { marks } = await api("/api/pipeline/marks");
      const list = document.getElementById("marks-list");
      list.replaceChildren();
      if (!marks.length)
        list.append(
          el(
            "article",
            "Словарь пока пустой. Создай свой лейбл в форме слева.",
          ),
        );
      for (const mark of marks) {
        const row = el("article");
        const heading = el("h2", mark.name);
        heading.style.borderLeft = `5px solid ${mark.color}`;
        heading.style.paddingLeft = "10px";
        row.append(
          heading,
          el("p", mark.description || "Без описания", "muted"),
          el(
            "p",
            `ID ${mark.id} · ${mark.count} постов${mark.archived ? " · архивный" : ""}`,
          ),
        );
        row.append(
          button("Редактировать", () => {
            selected = mark.id;
            for (const field of ["name", "description", "color"])
              form.elements[field].value = mark[field];
            form.elements.archived.checked = mark.archived;
            document.getElementById("mark-form-heading").textContent =
              `Признак #${mark.id}`;
            form.elements.name.focus();
          }),
        );
        list.append(row);
      }
    }
    form.onsubmit = async (event) => {
      event.preventDefault();
      const submit = form.querySelector("[type=submit]");
      submit.disabled = true;
      try {
        await api(
          `/api/pipeline/marks${selected ? `/${selected}` : ""}`,
          {
            name: form.elements.name.value,
            description: form.elements.description.value,
            color: form.elements.color.value,
            archived: form.elements.archived.checked,
          },
          selected ? "PUT" : "POST",
        );
        reset();
        feedback.textContent = "Сохранено";
        feedback.className = "";
        await load();
      } catch (error) {
        report(feedback, error);
      } finally {
        submit.disabled = false;
      }
    };
    await load();
  }

  async function filtersPage() {
    const form = document.getElementById("filter-form");
    if (!form) return;
    const feedback = document.getElementById("filters-feedback");
    const preview = document.getElementById("filter-preview-result");
    const apply = document.getElementById("filter-apply");
    let catalog = [],
      filters = [],
      marks = [],
      edited = null,
      previewDigest = null;
    let listSignature = "",
      loading = false;
    let expression = { op: "and", children: [] };
    const dirty = () => {
      previewDigest = null;
      apply.disabled = true;
      preview.replaceChildren();
      document.querySelectorAll("[data-rule-score]").forEach((node) => {
        node.textContent = "Оценка модели появится после предпросмотра.";
      });
    };
    const condition = () => ({
      op: "condition",
      label_id: catalog[0]?.id || "",
      compare: "gte",
      threshold: 60,
    });
    const label = (name, control) => {
      const node = el("label", name);
      node.append(control);
      return node;
    };
    function drawTree(node, parent, index, depth = 0) {
      const wrapper = el(
        "div",
        undefined,
        node.op === "condition" ? "rule-condition" : "rule-group",
      );
      if (node.op === "condition") {
        const category = el("select");
        category.setAttribute("aria-label", "Категория");
        for (const [kind, title] of [
          ["category", "Категории"],
          ["feature", "Обязательные признаки модели"],
        ]) {
          const group = el("optgroup");
          group.label = title;
          for (const item of catalog.filter(
            (item) => !item.parent && (item.kind || "category") === kind,
          ))
            group.append(new Option(item.name, item.id));
          category.append(group);
        }
        const selected = catalog.find((item) => item.id === node.label_id);
        category.value =
          selected?.parent || selected?.id || catalog[0]?.id || "";
        const subcategory = el("select");
        subcategory.setAttribute("aria-label", "Подкатегория");
        function drawSubcategories() {
          subcategory.replaceChildren(new Option("* — все подкатегории", "*"));
          for (const item of catalog.filter(
            (item) => item.parent === category.value,
          ))
            subcategory.add(new Option(item.name, item.id));
          subcategory.value =
            node.label_id === category.value ? "*" : node.label_id;
          subcategory.disabled = subcategory.options.length === 1;
        }
        drawSubcategories();
        category.onchange = () => {
          node.label_id = category.value;
          drawSubcategories();
          dirty();
        };
        subcategory.onchange = () => {
          node.label_id =
            subcategory.value === "*" ? category.value : subcategory.value;
          dirty();
        };
        subcategory.title =
          "* не ограничивает подкатегорию: сравнивается оценка общей категории. При выборе подкатегории сравнивается её собственная оценка.";
        const compare = el("select");
        compare.setAttribute("aria-label", "Сравнение");
        for (const [value, name] of Object.entries({
          gte: "≥",
          gt: ">",
          lte: "≤",
          lt: "<",
        }))
          compare.add(new Option(name, value));
        compare.value = node.compare;
        compare.onchange = () => {
          node.compare = compare.value;
          dirty();
        };
        const threshold = el("input");
        threshold.type = "number";
        threshold.min = 0;
        threshold.max = 100;
        threshold.step = "0.1";
        threshold.value = node.threshold;
        threshold.required = true;
        threshold.setAttribute("aria-label", "Порог в процентах");
        threshold.oninput = () => {
          node.threshold = Number(threshold.value);
          dirty();
        };
        wrapper.append(
          label("Категория", category),
          label("Подкатегория", subcategory),
          label("Сравнение", compare),
          label("Порог, %", threshold),
        );
        const score = el(
          "p",
          "Оценка модели появится после предпросмотра.",
          "muted",
        );
        score.dataset.ruleScore = "";
        score.dataset.labelId = node.label_id;
        wrapper.append(score);
        const changedCategory = category.onchange,
          changedSubcategory = subcategory.onchange;
        category.onchange = () => {
          changedCategory();
          score.dataset.labelId = node.label_id;
        };
        subcategory.onchange = () => {
          changedSubcategory();
          score.dataset.labelId = node.label_id;
        };
      } else {
        const header = el("div", undefined, "rule-group-header");
        const mode = el("select");
        mode.setAttribute("aria-label", "Логика группы");
        for (const [value, name] of Object.entries({
          and: "И — все условия",
          or: "ИЛИ — любое условие",
          not: "НЕ — отрицание",
        }))
          mode.add(new Option(name, value));
        mode.value = node.op;
        mode.onchange = () => {
          const old = node.op;
          node.op = mode.value;
          if (node.op === "not" && node.children.length !== 1)
            node.children = [
              {
                op: old,
                children: node.children.length ? node.children : [condition()],
              },
            ];
          dirty();
          draw();
        };
        header.append(mode);
        wrapper.append(header);
        node.children.forEach((child, i) =>
          wrapper.append(drawTree(child, node, i, depth + 1)),
        );
        if (node.op !== "not" || !node.children.length) {
          const actions = el("div", undefined, "selection-actions");
          actions.append(
            button("+ Условие", () => {
              node.children.push(condition());
              dirty();
              draw();
            }),
          );
          if (depth < 5)
            actions.append(
              button("+ Группа", () => {
                node.children.push({ op: "or", children: [condition()] });
                dirty();
                draw();
              }),
            );
          wrapper.append(actions);
        }
      }
      if (parent)
        wrapper.append(
          button("Убрать", () => {
            parent.children.splice(index, 1);
            dirty();
            draw();
          }),
        );
      return wrapper;
    }
    function draw() {
      document
        .getElementById("rule-builder")
        .replaceChildren(drawTree(expression));
    }
    const assignedMark = form.elements.mark_id;
    assignedMark.onchange = dirty;
    function drawMarks(selectedId = assignedMark.value) {
      assignedMark.replaceChildren(new Option("Выбери лейбл из словаря", ""));
      for (const mark of marks)
        if (!mark.archived || String(mark.id) === String(selectedId))
          assignedMark.add(
            new Option(
              `${mark.name}${mark.archived ? " · архивный" : ""}`,
              String(mark.id),
            ),
          );
      assignedMark.value = String(selectedId || "");
    }
    function reset(item = null, copy = false) {
      edited = item && !copy ? item : null;
      form.reset();
      form.elements.enabled.checked = item?.enabled ?? true;
      form.elements.name.value = item
        ? `${item.name}${copy ? " — копия" : ""}`
        : "";
      form.elements.model_key.value = item?.model_key || "tfidf";
      drawMarks(item?.mark_id);
      expression = item
        ? structuredClone(item.expression)
        : { op: "and", children: [condition()] };
      document.getElementById("filter-form-heading").textContent = edited
        ? `Фильтр #${edited.id} · версия ${edited.number}`
        : "Новый фильтр";
      feedback.textContent = "";
      feedback.className = "";
      dirty();
      draw();
      document
        .querySelectorAll("[data-filter-id]")
        .forEach((row) =>
          row.classList.toggle(
            "is-selected",
            String(edited?.id) === row.dataset.filterId,
          ),
        );
      if (!marks.some((mark) => !mark.archived))
        feedback.textContent =
          "Словарь пустой. Сначала создай лейбл на странице «Признаки».";
    }
    function draft() {
      return {
        name: form.elements.name.value,
        enabled: form.elements.enabled.checked,
        model_key: form.elements.model_key.value,
        mark_id: Number(assignedMark.value),
        expression,
        filter_id: edited?.id || null,
        base_version_id: edited?.base_version_id || null,
      };
    }
    async function load() {
      const [data, dictionary] = await Promise.all([
        api("/api/pipeline/filters"),
        api("/api/pipeline/marks"),
      ]);
      filters = data.filters;
      catalog = data.catalog.labels;
      if (JSON.stringify(marks) !== JSON.stringify(dictionary.marks)) {
        marks = dictionary.marks;
        drawMarks();
      }
      const signature = JSON.stringify(filters);
      if (signature === listSignature) return;
      listSignature = signature;
      const list = document.getElementById("filters-list");
      list.replaceChildren();
      if (!filters.length)
        list.append(
          el(
            "article",
            "Фильтров пока нет. Выбери лейбл из словаря и задай условия по оценкам модели.",
          ),
        );
      for (const item of filters) {
        const row = el("article");
        if (item.archived) row.className = "selection-archived";
        row.dataset.filterId = item.id;
        row.append(
          el("h2", item.name),
          el(
            "p",
            `${item.archived ? "Архивный" : item.enabled ? "Включён" : "Отключён"} · ${item.model_key === "tfidf" ? "TF-IDF" : "MiniLM"} · версия ${item.number}`,
          ),
          el(
            "p",
            `Признак: ${item.mark_name} · текущих совпадений: ${item.matches}`,
          ),
        );
        if (item.application) {
          const job = item.application;
          const statuses = {
            queued: "В очереди",
            running: "Применяется",
            complete: "Применено",
            cancelled: "Остановлено новой настройкой",
            failed: "Ошибка пересчёта",
          };
          row.append(
            el(
              "p",
              `${statuses[job.status] || job.status} · проверено ${job.processed} · совпало ${job.matched} · без оценки ${job.unknown} · досчитать ${job.backfilled}${job.error ? ` · ${job.error}. Примени настройки повторно.` : ""}`,
              "muted",
            ),
          );
        }
        const actions = el("div", undefined, "selection-actions");
        if (!item.archived) {
          actions.append(
            button("Редактировать", () => reset(item)),
            button("Копировать", () => reset(item, true)),
            button("Архивировать", async () => {
              try {
                await api(`/api/pipeline/filters/${item.id}/archive`, {});
                if (edited?.id === item.id) reset();
                await load();
              } catch (error) {
                report(feedback, error);
              }
            }),
          );
        } else actions.append(button("Копировать", () => reset(item, true)));
        row.append(actions);
        list.append(row);
      }
    }
    for (const name of ["name", "enabled", "model_key"])
      form.elements[name].addEventListener("input", dirty);
    document.getElementById("filter-new").onclick = () => reset();
    document.getElementById("filter-cancel").onclick = () => reset();
    form.onsubmit = async (event) => {
      event.preventDefault();
      const submit = document.getElementById("filter-preview");
      submit.disabled = true;
      dirty();
      try {
        const data = await api("/api/pipeline/filters/preview", draft());
        previewDigest = data.preview_digest;
        apply.disabled = false;
        feedback.textContent =
          "Предпросмотр готов. Настройки ещё не применены.";
        feedback.className = "";
        preview.append(el("h3", "Предпросмотр"));
        const metrics = el("div", undefined, "preview-metrics");
        for (const [key, title] of [
          ["total", "Всего"],
          ["matched", "Совпало"],
          ["rejected", "Не прошло"],
          ["unknown", "Нет оценки"],
          ["new_marks", "Новые лейблы"],
          ["backfill_needed", "Досчитать"],
        ]) {
          const metric = el("div");
          metric.append(el("strong", String(data[key])), el("span", title));
          metrics.append(metric);
        }
        preview.append(metrics);
        const percent = (score) =>
          score == null ? "нет оценки" : `${(score * 100).toFixed(2)}%`;
        if (data.examples.length) {
          const postSelect = el("select");
          postSelect.setAttribute("aria-label", "Пост для показа оценок");
          for (const example of data.examples)
            postSelect.add(
              new Option(`Пост #${example.entry_id}`, String(example.entry_id)),
            );
          function showScores() {
            const example = data.examples.find(
                (item) => String(item.entry_id) === postSelect.value,
              ),
              scores = new Map();
            function read(trace) {
              if (trace.op === "condition")
                scores.set(trace.label_id, trace.score);
              else trace.children?.forEach(read);
            }
            read(example.trace);
            document.querySelectorAll("[data-rule-score]").forEach((node) => {
              node.textContent = `Пост #${example.entry_id} · ${form.elements.model_key.value === "tfidf" ? "TF-IDF" : "MiniLM"}: ${percent(scores.get(node.dataset.labelId))}`;
            });
          }
          postSelect.onchange = showScores;
          preview.append(label("Пост для показа оценок", postSelect));
          showScores();
        }
        function scoreDetails(trace) {
          if (trace.reason) return trace.reason;
          if (trace.op === "condition") {
            const item = catalog.find((item) => item.id === trace.label_id);
            const parent = catalog.find((parent) => parent.id === item?.parent);
            const name = parent
              ? `${parent.name} / ${item.name}`
              : item?.name || trace.label_id;
            const score =
              trace.score == null
                ? "нет оценки"
                : `${(trace.score * 100).toFixed(2)}%`;
            return `${name}: оценка модели ${score} · порог ${{ gte: "≥", gt: ">", lte: "≤", lt: "<" }[trace.compare]} ${trace.threshold}%`;
          }
          const children = trace.children.map(scoreDetails);
          return trace.op === "not"
            ? `НЕ (${children[0]})`
            : `(${children.join(trace.op === "and" ? " И " : " ИЛИ ")})`;
        }
        for (const example of data.examples) {
          const line = el("p");
          const link = el("a", `#${example.entry_id}`);
          link.href = `/pipeline/${example.entry_id}`;
          link.target = "_blank";
          line.append(
            link,
            ` · ${{ matched: "Совпало", rejected: "Не прошло", unknown: "Нет оценки" }[example.outcome]}`,
          );
          preview.append(
            line,
            el("p", scoreDetails(example.trace), "selection-reason"),
          );
          preview.append(
            el(
              "p",
              `Назначаемый лейбл: ${marks.find((mark) => String(mark.id) === assignedMark.value)?.name || "—"}`,
              "selection-reason",
            ),
          );
        }
      } catch (error) {
        report(feedback, error);
      } finally {
        submit.disabled = false;
      }
    };
    apply.onclick = async () => {
      if (!previewDigest) return;
      apply.disabled = true;
      try {
        const data = await api("/api/pipeline/filters/apply", {
          ...draft(),
          preview_digest: previewDigest,
        });
        await load();
        reset(filters.find((item) => item.id === data.id));
        feedback.textContent = data.application_id
          ? "Сохранено. Пересчёт карточек поставлен в очередь."
          : "Сохранено. Фильтр отключён.";
      } catch (error) {
        report(feedback, error);
        apply.disabled = false;
      }
    };
    await load();
    reset();
    document.getElementById("filter-fields").disabled = false;
    document.getElementById("filter-new").disabled = false;
    window.setInterval(async () => {
      if (document.hidden || loading) return;
      loading = true;
      try {
        await load();
      } catch (error) {
        report(feedback, error);
      } finally {
        loading = false;
      }
    }, 15000);
  }
  marksPage().catch((error) =>
    report(document.getElementById("marks-feedback"), error),
  );
  filtersPage().catch((error) =>
    report(document.getElementById("filters-feedback"), error),
  );
})();
