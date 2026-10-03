(() => {
  const root = document.querySelector('#max-status');
  const detailRoot = document.querySelector('[data-max-deliveries]');
  if (!root && !detailRoot) return;
  const labels = {queued: 'В очереди', preparing: 'Подготовка', sending: 'Отправляется', verifying: 'Сверка', delivered: 'Доставлено', failed: 'Ошибка', held: 'Нужен ручной разбор', unknown: 'Результат неизвестен', stale: 'Источник изменён', cancelled: 'Отменено', prepared: 'Зафиксирована', running: 'Выполняется', complete: 'Подтверждена'};
  const access = {unchecked: 'Не проверен', ok: 'Публикация и чтение доступны', denied: 'Бот не подключён или нет прав', error: 'Ошибка проверки'};
  const el = (tag, value, className) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  };
  const link = (text, url) => {
    const a = el('a', text);
    a.href = url; a.rel = 'noopener';
    if (/^https:\/\/max\.ru\//.test(url)) a.target = '_blank';
    return a;
  };
  const api = async (url, body) => {
    const response = await fetch(url, body === undefined ? {cache: 'no-store'} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить действие');
    return data;
  };
  let current;
  let loading = false;
  function deliveryStatus(delivery) {
    const status = el('div');
    status.append(el('span', labels[delivery.status] || delivery.status, 'badge'));
    if (delivery.source_changed_at) status.append(el('p', 'После отправки изменился источник', 'notice'));
    if (delivery.error) status.append(el('p', delivery.error, 'muted'));
    if (delivery.status === 'failed') {
      const retry = el('button', 'Повторить'); retry.type = 'button';
      retry.onclick = () => action(retry, () => api(`/api/publication/deliveries/${delivery.id}/retry`, {}));
      status.append(retry);
    }
    if (delivery.status === 'unknown') {
      status.append(el('p', 'Сначала проверь канал. Автоматического повтора нет.', 'notice'));
      const proof = delivery.parts.find(p => !p.mid && p.absence_scan_count >= 2 && p.absence_scan_sha256
        && Date.now() - Date.parse(p.absence_checked_at) < 90000);
      if (proof) {
        const absent = el('button', 'Проверил канал: публикации нет'); absent.type = 'button';
        absent.onclick = () => {
          if (window.confirm('Отправитель дважды не нашёл сообщение. Ты тоже проверил канал и подтверждаешь, что публикации нет? После подтверждения будет разрешён повтор.')) {
            action(absent, () => api(`/api/publication/deliveries/${delivery.id}/confirm-absence`, {
              confirmed_absent: true, scan_sha256: proof.absence_scan_sha256}));
          }
        };
        status.append(absent);
      }
    }
    return status;
  }
  function deliveryLinks(delivery) {
    const urls = el('div');
    for (const part of delivery.parts) {
      if (part.status === 'cancelled') continue;
      const line = el('p');
      const pendingText = delivery.status === 'unknown' ? 'ID публикации пока не подтверждён' : 'Ещё не отправлено';
      line.append(part.url ? link(`Часть ${part.number} ↗`, part.url) : el('span', part.mid || pendingText));
      if (part.verified_at) line.append(el('small', ` · сверено ${new Date(part.verified_at).toLocaleString()}`));
      urls.append(line);
    }
    return urls;
  }
  async function reload() {
    if (loading) return;
    loading = true;
    try {
      if (!root) {
        const history = await api(`/api/publication/history?entry_id=${encodeURIComponent(detailRoot.dataset.maxDeliveries)}`);
        const nodes = history.deliveries.map(delivery => {
          const card = el('section', undefined, 'model-overview');
          card.append(el('h3', delivery.channel), deliveryStatus(delivery), deliveryLinks(delivery));
          return card;
        });
        detailRoot.replaceChildren(...(nodes.length ? nodes : [el('p', 'Отправок этого поста пока нет.', 'muted')]));
        return;
      }
      const [state, history, batches] = await Promise.all([api('/api/publication'), api('/api/publication/history'), api('/api/publication/batches')]);
      current = state;
      root.textContent = state.sender_enabled ? `Отправитель включён. Новые посты: ${state.automatic_enabled ? 'отправляются после подготовки' : 'автоматическая отправка выключена'}.` : 'Отправитель выключен. Новые отправки приостановлены, история сохранена.';
      if (state.runtime?.error) root.textContent += ` Ошибка цикла: ${state.runtime.error}.`;
      if (state.sender_enabled && (!state.runtime?.heartbeat || Date.now() - Date.parse(state.runtime.heartbeat) > 60000)) {
        root.textContent += ' Нет свежей отметки работы отправителя.';
      }
      const counts = state.counts || {};
      const queued = ['queued', 'preparing', 'sending', 'verifying'].reduce((sum, key) => sum + (counts[key] || 0), 0);
      document.querySelector('#max-counts').replaceChildren(...[
        ['В работе', queued], ['Доставлено', counts.delivered || 0],
        ['Ошибки', counts.failed || 0], ['Неизвестный результат', counts.unknown || 0],
        ['Нужен ручной разбор', counts.held || 0],
      ].map(([name, value]) => {
        const metric = el('div', undefined, 'metric');
        metric.append(el('span', name), el('strong', value));
        return metric;
      }));
      const channelRoot = document.querySelector('#max-channels'); channelRoot.replaceChildren();
      for (const channel of state.channels) {
        const card = el('section', undefined, 'model-overview max-channel-card');
        const title = el('h3'); title.append(channel.url ? link(channel.title, channel.url) : el('span', channel.title));
        card.append(title, el('p', access[channel.access] || channel.access, channel.access === 'ok' ? 'badge' : 'notice'));
        for (const route of state.routes.filter(r => r.channel_id === channel.id)) {
          const gate = route.quality_gate || {};
          card.append(el('p', `${route.filter_name || `Фильтр #${route.filter_id}`} → ${route.mark_name || `Лейбл #${route.mark_id}`} · ${route.enabled ? 'включён' : 'выключен'}`));
          if (gate.owner_acceptance) {
            card.append(el('p', 'Экспериментальный маршрут · разрешён владельцем без пройденного допуска модели', 'notice'));
            card.append(el('p', gate.owner_acceptance.reason, 'muted'));
            const accept = el('button', 'Подтвердить текущие условия фильтра'); accept.type = 'button';
            accept.onclick = () => {
              if (window.confirm('Разрешить текущую версию фильтра без нового ML-допуска? Результаты проверки качества останутся прежними.')) {
                action(accept, () => api(`/api/publication/routes/${route.id}/accept-current-filter`, {
                  reason: 'Владелец вручную подтвердил текущие условия экспериментального фильтра; новый допуск модели не заявляется.'}));
              }
            };
            card.append(accept);
          }
          card.append(el('p', gate.test_matched ? `${gate.model_key} · прежний test: ${gate.test_correct}/${gate.test_matched} совпадений · ${gate.model_version}` : `${gate.model_key || 'Модель'} · test этого маршрута не подтверждён · ${gate.model_version || 'артефакт не привязан'}`, 'muted'));
          if (gate.validation_matched) card.append(el('p', `Validation: ${gate.validation_correct}/${gate.validation_matched} · ${gate.reason || ''}`, 'muted'));
        }
        if (channel.error) card.append(el('p', channel.error, 'notice error'));
        const check = el('button', channel.check_requested ? 'Проверка в очереди' : 'Проверить права и историю');
        check.type = 'button'; check.disabled = channel.check_requested;
        check.onclick = () => action(check, () => api(`/api/publication/channels/${channel.id}/check`, {}));
        card.append(check); channelRoot.append(card);
      }
      if (!state.channels.length) channelRoot.append(el('p', 'Каналы ещё не настроены.', 'muted'));
      const batchRoot = document.querySelector('#max-batches'); batchRoot.replaceChildren();
      for (const batch of batches.batches) batchRoot.append(el('p', `#${batch.id} · ${batch.title} · ${batch.count} постов · ${labels[batch.status] || batch.status}`));
      if (!batches.batches.length) batchRoot.append(el('p', 'Проверенный состав первой партии ещё не зафиксирован.', 'muted'));
      const automatic = document.querySelector('#max-automatic');
      automatic.textContent = state.automatic_enabled ? 'Остановить автоматическую отправку' : 'Включить отправку новых постов';
      automatic.disabled = !state.sender_enabled || (!state.automatic_enabled && !batches.batches.some(b => b.status === 'complete'));
      const table = el('table'), head = el('thead'), row = el('tr');
      for (const t of ['Пост', 'Канал', 'Состояние', 'Публикации']) row.append(el('th', t));
      head.append(row); table.append(head);
      const tbody = el('tbody');
      for (const delivery of history.deliveries) {
        const tr = el('tr'), post = el('td'), status = el('td'), urls = el('td');
        post.append(link(`#${delivery.entry_id}`, `/pipeline/${delivery.entry_id}`));
        status.append(deliveryStatus(delivery)); urls.append(deliveryLinks(delivery));
        tr.append(post, el('td', delivery.channel), status, urls); tbody.append(tr);
      }
      table.append(tbody); document.querySelector('#max-history').replaceChildren(table);
    } catch (error) { (root || detailRoot).textContent = error.message; }
    finally { loading = false; }
  }
  async function action(button, fn) {
    button.disabled = true;
    try { await fn(); await reload(); } catch (error) { (root || detailRoot).textContent = error.message; button.disabled = false; }
  }
  if (root) {
    document.querySelector('#max-refresh').onclick = reload;
    document.querySelector('#max-automatic').onclick = function () { action(this, () => api('/api/publication/automatic', {enabled: !current.automatic_enabled})); };
  }
  reload();
  setInterval(() => { if (!document.hidden) reload(); }, 10000);
})();
