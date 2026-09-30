(() => {
  'use strict';
  const el = (tag, text, className) => {const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;};
  const button = (text, action) => {const node = el('button', text); node.type = 'button'; node.addEventListener('click', action); return node;};
  async function api(url, data, method='POST') {
    const response = await fetch(url, data === undefined ? {cache:'no-store', credentials:'same-origin'} :
      {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data), credentials:'same-origin'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Ошибка запроса');
    return result;
  }
  function report(node, error) {node.textContent = error.message || String(error); node.className = 'selection-error';}

  async function marksPage() {
    const form = document.getElementById('mark-form'); if (!form) return;
    const feedback = document.getElementById('marks-feedback'); let selected = null;
    const reset = () => {selected=null; form.reset(); document.getElementById('mark-form-heading').textContent='Новый признак'; feedback.textContent='';};
    document.getElementById('mark-new').onclick = reset;
    async function load() {
      const {marks} = await api('/api/pipeline/marks'); const list=document.getElementById('marks-list'); list.replaceChildren();
      if (!marks.length) list.append(el('article', 'Словарь пустой. Добавь первый признак и выбери его в фильтре.'));
      for (const mark of marks) {
        const row=el('article'); const heading=el('h2',mark.name); heading.style.borderLeft=`5px solid ${mark.color}`; heading.style.paddingLeft='10px';
        row.append(heading, el('p',mark.description || 'Без описания','muted'), el('p',`ID ${mark.id} · ${mark.count} постов${mark.archived ? ' · архивный' : ''}`));
        row.append(button('Редактировать', () => {selected=mark.id; for (const field of ['name','description','color']) form.elements[field].value=mark[field]; form.elements.archived.checked=mark.archived; document.getElementById('mark-form-heading').textContent=`Признак #${mark.id}`; form.elements.name.focus();}));
        list.append(row);
      }
    }
    form.onsubmit=async (event) => {event.preventDefault(); const submit=form.querySelector('[type=submit]'); submit.disabled=true; try {
      await api(`/api/pipeline/marks${selected ? `/${selected}` : ''}`, {name:form.elements.name.value, description:form.elements.description.value, color:form.elements.color.value, archived:form.elements.archived.checked}, selected ? 'PUT' : 'POST');
      reset(); feedback.textContent='Сохранено'; feedback.className=''; await load();
    } catch(error) {report(feedback,error);} finally {submit.disabled=false;}};
    await load();
  }

  async function filtersPage() {
    const form=document.getElementById('filter-form'); if (!form) return;
    const feedback=document.getElementById('filters-feedback'); const preview=document.getElementById('filter-preview-result');
    const apply=document.getElementById('filter-apply'); let catalog=[], filters=[], marks=[], edited=null, previewDigest=null;
    let expression={op:'and',children:[]};
    const dirty=() => {previewDigest=null; apply.disabled=true; preview.replaceChildren();};
    const condition=()=>({op:'condition',label_id:catalog[0]?.id || '',compare:'gte',threshold:60});
    const label=(name, control) => {const node=el('label',name); node.append(control); return node;};
    function drawTree(node, parent, index, depth=0) {
      const wrapper=el('div',undefined,node.op==='condition' ? 'rule-condition' : 'rule-group');
      if (node.op==='condition') {
        const select=el('select'); select.setAttribute('aria-label','Категория или подкатегория');
        for (const item of catalog) {const parentName=catalog.find(parent=>parent.id===item.parent)?.name; select.add(new Option(parentName ? `${parentName} / ${item.name}` : item.name,item.id));}
        select.value=node.label_id; select.onchange=()=>{node.label_id=select.value;dirty();};
        const compare=el('select'); compare.setAttribute('aria-label','Сравнение'); for (const [value,name] of Object.entries({gte:'≥',gt:'>',lte:'≤',lt:'<'})) compare.add(new Option(name,value)); compare.value=node.compare; compare.onchange=()=>{node.compare=compare.value;dirty();};
        const threshold=el('input'); threshold.type='number';threshold.min=0;threshold.max=100;threshold.step='0.1';threshold.value=node.threshold;threshold.required=true;threshold.setAttribute('aria-label','Порог в процентах');threshold.oninput=()=>{node.threshold=Number(threshold.value);dirty();};
        wrapper.append(label('Категория',select),label('Сравнение',compare),label('Оценка, %',threshold));
      } else {
        const header=el('div',undefined,'rule-group-header');const mode=el('select');mode.setAttribute('aria-label','Логика группы'); for (const [value,name] of Object.entries({and:'И — все условия',or:'ИЛИ — любое условие',not:'НЕ — отрицание'})) mode.add(new Option(name,value));mode.value=node.op;
        mode.onchange=()=>{const old=node.op;node.op=mode.value;if (node.op==='not' && node.children.length!==1) node.children=[{op:old,children:node.children.length ? node.children : [condition()]}]; dirty();draw();};
        header.append(mode);wrapper.append(header);
        node.children.forEach((child,i)=>wrapper.append(drawTree(child,node,i,depth+1)));
        if (node.op!=='not' || !node.children.length) {
          const actions=el('div',undefined,'selection-actions');actions.append(button('+ Условие',()=>{node.children.push(condition());dirty();draw();}));
          if (depth<5) actions.append(button('+ Группа',()=>{node.children.push({op:'or',children:[condition()]});dirty();draw();}));wrapper.append(actions);
        }
      }
      if (parent) wrapper.append(button('Убрать',()=>{parent.children.splice(index,1);dirty();draw();}));
      return wrapper;
    }
    function draw() {document.getElementById('rule-builder').replaceChildren(drawTree(expression));}
    function reset(item=null, copy=false) {
      edited=item && !copy ? item : null;form.reset();form.elements.enabled.checked=item?.enabled ?? true;
      form.elements.name.value=item ? `${item.name}${copy ? ' — копия' : ''}` : '';
      form.elements.model_key.value=item?.model_key || 'tfidf';form.elements.mark_id.value=item?.mark_id || marks.find(m=>!m.archived)?.id || '';
      expression=item ? structuredClone(item.expression) : {op:'and',children:[condition()]};
      document.getElementById('filter-form-heading').textContent=edited ? `Фильтр #${edited.id} · версия ${edited.number}` : 'Новый фильтр';
      feedback.textContent='';feedback.className='';dirty();draw();
    }
    function draft() {return {name:form.elements.name.value, enabled:form.elements.enabled.checked, model_key:form.elements.model_key.value,
      mark_id:Number(form.elements.mark_id.value), expression, filter_id:edited?.id || null, base_version_id:edited?.base_version_id || null};}
    async function load() {
      const data=await api('/api/pipeline/filters');filters=data.filters;catalog=data.catalog.labels;
      const list=document.getElementById('filters-list');list.replaceChildren();
      if (!filters.length) list.append(el('article','Фильтров пока нет. Сначала добавь признак в словарь.'));
      for (const item of filters) {
        const row=el('article');if(item.archived) row.className='selection-archived';row.append(el('h2',item.name), el('p',`${item.archived ? 'Архивный' : item.enabled ? 'Включён' : 'Отключён'} · ${item.model_key==='tfidf' ? 'TF-IDF' : 'MiniLM'} · версия ${item.number}`),el('p',`Признак: ${item.mark_name} · текущих совпадений: ${item.matches}`));
        if(item.application) {const job=item.application;const statuses={queued:'В очереди',running:'Применяется',complete:'Применено',cancelled:'Остановлено новой настройкой',failed:'Ошибка пересчёта'};row.append(el('p',`${statuses[job.status] || job.status} · проверено ${job.processed} · совпало ${job.matched} · без оценки ${job.unknown} · досчитать ${job.backfilled}${job.error ? ` · ${job.error}. Примени настройки повторно.` : ''}`,'muted'));}
        const actions=el('div',undefined,'selection-actions');if(!item.archived) {actions.append(button('Редактировать',()=>reset(item)),button('Копировать',()=>reset(item,true)),button('Архивировать',async()=>{try {await api(`/api/pipeline/filters/${item.id}/archive`,{});if(edited?.id===item.id)reset();await load();} catch(error){report(feedback,error);}}));} else actions.append(button('Копировать',()=>reset(item,true)));
        row.append(actions);list.append(row);
      }
    }
    for(const name of ['name','enabled','model_key','mark_id']) form.elements[name].addEventListener('input',dirty);
    document.getElementById('filter-new').onclick=()=>reset();document.getElementById('filter-cancel').onclick=()=>reset();
    form.onsubmit=async(event)=>{event.preventDefault();const submit=document.getElementById('filter-preview');submit.disabled=true;dirty();try {
      const data=await api('/api/pipeline/filters/preview',draft());previewDigest=data.preview_digest;apply.disabled=false;
      feedback.textContent='Предпросмотр готов. Настройки ещё не применены.';feedback.className='';
      preview.append(el('h3','На текущих оценках'),el('p',`Всего ${data.total} · совпало ${data.matched} · не прошло ${data.rejected} · без оценки ${data.unknown} · новых признаков ${data.new_marks} · досчитать ${data.backfill_needed}`));
      for (const example of data.examples) {const line=el('p');const link=el('a',`#${example.entry_id}`);link.href=`/pipeline/${example.entry_id}`;link.target='_blank';line.append(link,` · ${{matched:'Совпало',rejected:'Не прошло',unknown:'Нет оценки'}[example.outcome]}`);preview.append(line);}
    }catch(error){report(feedback,error);}finally{submit.disabled=false;}};
    apply.onclick=async()=>{if(!previewDigest)return;apply.disabled=true;try {const data=await api('/api/pipeline/filters/apply',{...draft(),preview_digest:previewDigest});await load();reset(filters.find(item=>item.id===data.id));feedback.textContent=data.application_id ? 'Сохранено. Пересчёт карточек поставлен в очередь.' : 'Сохранено. Фильтр отключён.';}catch(error){report(feedback,error);apply.disabled=false;}};
    marks=(await api('/api/pipeline/marks')).marks;
    form.elements.mark_id.add(new Option('Выбери признак',''));
    for(const mark of marks) form.elements.mark_id.add(new Option(`${mark.name}${mark.archived ? ' · архивный' : ''}`,mark.id));
    await load();reset();
    document.getElementById('filter-fields').disabled=false;
    document.getElementById('filter-new').disabled=false;
    if(!marks.some(mark=>!mark.archived)){feedback.textContent='Словарь пустой. Добавь признак на странице «Признаки».';document.getElementById('filter-preview').disabled=true;}
    window.setInterval(()=>load().catch(error=>report(feedback,error)),5000);
  }
  marksPage().catch(error=>report(document.getElementById('marks-feedback'),error));
  filtersPage().catch(error=>report(document.getElementById('filters-feedback'),error));
})();
