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
    let catalog = [], catalogs = {},
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
        ["condition", "length"].includes(node.op) ? "rule-condition" : "rule-group",
      );
      if (["condition", "length"].includes(node.op)) {
        const kind = el("select"); kind.setAttribute("aria-label", "Тип условия");
        kind.add(new Option("Оценка модели", "condition")); kind.add(new Option("Длина поста", "length")); kind.value = node.op;
        kind.onchange = () => {
          for (const key of Object.keys(node)) delete node[key];
          Object.assign(node, kind.value === "length" ? {op:"length", compare:"lte", threshold:500} : condition());
          dirty(); draw();
        };
        wrapper.append(label("Условие", kind));
      }
      if (node.op === "length") {
        const compare = el("select"); compare.setAttribute("aria-label", "Сравнение длины");
        for (const [value,name] of Object.entries({gte:"≥",gt:">",lte:"≤",lt:"<",eq:"="})) compare.add(new Option(name,value));
        compare.value=node.compare; compare.onchange=()=>{node.compare=compare.value;dirty();};
        const size=el("input"); size.type="number"; size.min=0; size.step=1; size.required=true; size.value=node.threshold;
        size.setAttribute("aria-label","Длина в символах"); size.oninput=()=>{node.threshold=Number(size.value);dirty();};
        wrapper.append(label("Сравнение",compare),label("Символов",size));
      } else if (node.op === "condition") {
        const source=el("select"); source.setAttribute("aria-label","Источник оценки");
        source.add(new Option("Текст поста","text")); if(form.elements.requires_ocr.checked) source.add(new Option("OCR","ocr"));
        source.value=node.input_source || "text";
        source.onchange=()=>{node.input_source=source.value;dirty();};
        wrapper.append(label("Источник",source));
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
      form.elements.profile.value = item?.profile || "taxonomy";
      form.elements.requires_ocr.checked = item?.requires_ocr ?? false;
      catalog = catalogs[form.elements.profile.value]?.labels || [];
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
        profile: form.elements.profile.value,
        requires_ocr: form.elements.requires_ocr.checked,
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
      catalogs = data.catalogs || {taxonomy: data.catalog};
      catalog = catalogs[form.elements.profile.value]?.labels || [];
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
            `${item.requires_ocr ? "Требует OCR · " : ""}Признак: ${item.mark_name} · текущих совпадений: ${item.matches}`,
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
    form.elements.profile.addEventListener("change", () => {
      catalog = catalogs[form.elements.profile.value]?.labels || [];
      expression = {op: "and", children: [condition()]};
      dirty(); draw();
    });
    form.elements.requires_ocr.addEventListener("change", () => {
      const hasOcr = node => node.input_source === "ocr" || (node.children || []).some(hasOcr);
      if (!form.elements.requires_ocr.checked && hasOcr(expression)) {
        form.elements.requires_ocr.checked = true;
        feedback.textContent="Сначала измени или убери условия по OCR."; return;
      }
      dirty(); draw();
    });
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
        const requestDraft = structuredClone(draft());
        const data = await api("/api/pipeline/filters/preview", requestDraft);
        if (JSON.stringify(requestDraft) !== JSON.stringify(draft())) {
          feedback.textContent = "Условия изменились. Обнови предпросмотр.";
          return;
        }
        previewDigest = data.preview_digest;
        apply.disabled = false;
        feedback.textContent =
          "Предпросмотр готов. Настройки ещё не применены.";
        feedback.className = "";
        preview.append(el("h3", `Совпало: ${data.matched}`));
        const model =
          form.elements.model_key.value === "tfidf" ? "TF-IDF" : "MiniLM";
        const mark = marks.find(
          (item) => String(item.id) === assignedMark.value,
        );
        preview.append(el("p", `${model} · лейбл «${mark.name}»`, "muted"));
        if (data.matched > data.examples.length)
          preview.append(
            el(
              "p",
              `Показаны первые ${data.examples.length} совпадений из ${data.matched}.`,
              "muted",
            ),
          );
        if (!data.matched)
          preview.append(
            el("p", "По этим условиям совпадений нет.", "preview-empty"),
          );
        if (data.backfill_needed)
          preview.append(
            el(
              "p",
              `Ещё ${data.backfill_needed} постов требуют актуальных оценок. После применения фильтра они будут досчитаны.`,
              "muted",
            ),
          );
        const operators = { gte: "≥", gt: ">", lte: "≤", lt: "<", eq:"=", ne:"≠" };
        const inverted = { gte: "lt", gt: "lte", lte: "gt", lt: "gte", eq:"ne" };
        for (const example of data.examples) {
          const card = el("article", undefined, "preview-match");
          const heading = el("header", undefined, "preview-match-header");
          const link = el("a", `Пост #${example.entry_id} ↗`);
          link.href = `/pipeline/${example.entry_id}`;
          link.target = "_blank";
          link.rel = "noopener";
          heading.append(link, el("span", "Совпало", "preview-match-status"));
          card.append(
            heading,
            el(
              "p",
              `${example.text}${example.text_truncated ? "…" : ""}`,
              "preview-match-text",
            ),
          );
          const conditions = el("div", undefined, "preview-match-conditions");
          conditions.setAttribute("aria-label", "Условия, по которым совпало");
          for (const condition of example.matching_conditions) {
            if (condition.op === "length") {
              const sign=operators[condition.negated ? inverted[condition.compare] : condition.compare];
              conditions.append(el("div",`Длина поста ${condition.value} ${sign} ${condition.threshold} символов`,"preview-match-condition"));
              continue;
            }
            const item = catalog.find((item) => item.id === condition.label_id);
            const parent = catalog.find((parent) => parent.id === item?.parent);
            const name = parent ? `${parent.name} / ${item.name}` : item.name;
            const chip = el("div", undefined, "preview-match-condition");
            chip.append(el("strong", `${condition.input_source === "ocr" ? "OCR" : "Текст поста"} · ${name}`));
            const compare = condition.negated
              ? inverted[condition.compare]
              : condition.compare;
            chip.append(
              el(
                "span",
                `${(condition.score * 100).toFixed(2)}% ${operators[compare]} ${condition.threshold}%`,
              ),
            );
            if (condition.negated)
              chip.append(
                el(
                  "small",
                  `Через НЕ: исходное условие ${operators[condition.compare]} ${condition.threshold}% не выполнено.`,
                ),
              );
            conditions.append(chip);
          }
          card.append(conditions);
          preview.append(card);
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
          : form.elements.enabled.checked ? "Сохранено. Для OCR старый буфер автоматически не запускается." : "Сохранено. Фильтр отключён.";
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
