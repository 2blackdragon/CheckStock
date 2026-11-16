'use strict';
const $ = (id) => document.getElementById(id),
  esc = (v) =>
    String(v ?? '').replace(
      /[&<>"']/g,
      (c) =>
        ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        })[c],
    );
const state = {
  view: 'stocks',
  page: 1,
  catalog: { products: [], warehouses: [] },
  controller: null,
  rows: [],
  formKey: null,
  confirm: null,
};
const titles = {
  stocks: [
    'Остатки товаров',
    'Где находится товар и сколько доступно для отправки.',
  ],
  operations: ['Операции', 'Резервы, отправки и приёмка товара.'],
  warehouses: ['Склады', 'Действующие и архивные фулфилменты.'],
  history: [
    'История операций',
    'Каждое изменение остатков — с товаром, количеством и маршрутом.',
  ],
};
const statuses = {
  reserved: 'В резерве',
  shipped: 'В пути к клиенту',
  in_transit: 'В пути',
  received: 'Принято',
  completed: 'Проведено',
  cancelled: 'Отменено',
};
const types = {
    receipt: 'Приход',
    transfer: 'Между ФФ',
    fbs: 'FBS',
    fbo: 'FBO',
  },
  events = {
    receipt: 'Приход',
    transfer: 'Отправка между ФФ',
    fbs: 'Резерв FBS',
    fbo: 'Отправка FBO',
    receive: 'Приёмка',
    ship: 'Отправка FBS',
    cancel: 'Отмена резерва',
  };
const fmt = (v) => new Intl.NumberFormat('ru-RU').format(v),
  date = (v) => new Date(v).toLocaleString('ru-RU');

async function api(path, options = {}) {
  const r = await fetch('/api/' + path, {
    ...options,
    headers: { 'Content-Type': 'application/json' },
  });
  const d = await r.json();
  if (!r.ok)
    throw Error(
      typeof d.detail === 'string' ? d.detail : 'Проверьте заполнение полей.',
    );
  return d;
}

async function post(path, data) {
  return api(path, { method: 'POST', body: JSON.stringify(data) });
}

function options(rows, current) {
  return rows
    .map(
      (w) =>
        `<option value="${esc(w.id)}" ${w.id === current ? 'selected' : ''}>${esc(w.name ?? w.sku)}${w.archived_at ? ' · архив' : ''}</option>`,
    )
    .join('');
}

async function catalog() {
  state.catalog = await api('catalog');
  const w = $('warehouse').value;
  $('warehouse').innerHTML =
    '<option value="">Все ФФ, включая архивные</option>' +
    options(state.catalog.warehouses, w);
}

function table(head, body) {
  const headings = head.map((heading) => `<th>${heading}</th>`).join('');

  return `
    <table>
      <thead><tr>${headings}</tr></thead>
      <tbody>${body.join('')}</tbody>
    </table>
  `;
}

function route(row) {
  const source = row.source_name || 'Поступление';
  const destination =
    row.destination_name ||
    (row.type === 'fbs' ? 'Клиент' : row.type === 'fbo' ? 'Маркетплейс' : 'ФФ');

  return `${esc(source)} → ${esc(destination)}`;
}

function badge(row) {
  return `
    <span class="badge ${esc(row.status)}">
      ${esc(statuses[row.status] || row.status)}
    </span>
  `;
}

function operationActions(row) {
  if (row.status === 'reserved') {
    return `
      <button data-action="ship" data-id="${row.id}">Отправить</button>
      <button data-action="cancel" data-id="${row.id}">Отменить</button>
    `;
  }

  if (row.status === 'in_transit') {
    return `
      <button data-action="receive" data-id="${row.id}">Принять</button>
    `;
  }

  return '';
}

function opRows(rows, actions = true) {
  return rows.map(
    (row) => `
    <tr>
      <td>${esc(types[row.type])}</td>
      <td>${esc(row.sku)}<small>${esc(row.name)}</small></td>
      <td>${fmt(row.quantity)}</td>
      <td>${route(row)}</td>
      <td>${badge(row)}</td>
      ${
        actions
          ? `<td><div class="row-actions">${operationActions(row)}</div></td>`
          : ''
      }
    </tr>
  `,
  );
}

function controls() {
  const v = state.view;
  $('title').textContent = titles[v][0];
  $('subtitle').textContent = titles[v][1];
  $('mode').hidden = v !== 'stocks';
  $('status').hidden = v !== 'operations';
  $('date-label').hidden = v !== 'history';
  $('warehouse').hidden = v === 'warehouses';
  $('search').closest('label').hidden = v === 'warehouses';
  $('new').textContent =
    v === 'warehouses' ? '＋ Добавить ФФ' : '＋ Новая операция';
  $('note').textContent =
    v === 'stocks'
      ? 'Резерв входит в остаток на ФФ. В пути — отдельно. Нажмите на товар для детализации.'
      : v === 'warehouses'
        ? 'Архивация сохраняет товар и историю. Остатки можно переместить на действующий ФФ.'
        : v === 'history'
          ? 'По умолчанию — последние 30 дней. Очистите дату, чтобы увидеть всю историю.'
          : 'По умолчанию показаны операции, требующие отправки или приёмки.';
  document
    .querySelectorAll('nav button')
    .forEach((b) => b.classList.toggle('active', b.dataset.view === v));
}

async function load() {
  state.controller?.abort();
  const c = new AbortController();
  state.controller = c;
  controls();
  $('message').textContent = '';
  $('refresh').disabled = true;
  $('content').setAttribute('aria-busy', 'true');
  try {
    let rows,
      has_next = false;
    const p = new URLSearchParams({
      page: state.page,
      limit: 25,
      q: $('search').value,
    });
    if ($('warehouse').value) p.set('warehouse_id', $('warehouse').value);
    if (state.view === 'warehouses') {
      await catalog();
      rows = state.catalog.warehouses;
      $('content').innerHTML = table(
        ['ФФ', 'Состояние', 'Действия'],
        rows.map(
          (w) =>
            `
              <tr>
                <td>${esc(w.name)}</td>
                <td>
                  <span class="badge">
                    ${w.archived_at ? 'Архивный' : 'Действующий'}
                  </span>
                </td>
                <td>
                  <div class="row-actions">
                  <button data-wh="rename" data-id="${w.id}">
                    Переименовать
                  </button>
                  <button
                    data-wh="${w.archived_at ? 'restore' : 'archive'}"
                    data-id="${w.id}"
                  >
                    ${w.archived_at ? 'Восстановить' : 'Архивировать'}
                  </button>
                  <button data-filter="${w.id}">Остатки</button>
                  </div>
                </td>
              </tr>
            `,
        ),
      );
    } else {
      if (state.view === 'stocks') p.set('mode', $('mode').value);
      if (state.view === 'operations') p.set('status', $('status').value);
      if (state.view === 'history' && $('since').value)
        p.set('since', new Date($('since').value + 'T00:00:00').toISOString());
      const data = await api(state.view + '?' + p, { signal: c.signal });
      rows = data.items;
      has_next = data.has_next;
      if (c.signal.aborted) return;
      if (state.view === 'stocks') {
        const wh = !!$('warehouse').value;
        $('content').innerHTML = table(
          [
            'Товар',
            'На ФФ',
            'Резерв FBS',
            'Доступно',
            wh ? 'Входящие ФФ' : 'В пути между ФФ',
            ...(wh ? ['Исходящие ФФ'] : []),
            'В пути FBO',
            'В пути FBS',
          ],
          rows.map((row) => {
            const columns = [
              'physical',
              'reserved',
              'available',
              'incoming',
              ...(wh ? ['outgoing'] : []),
              'fbo',
              'fbs',
            ];
            const cells = columns.map((key, index) => {
              const value =
                key === 'available'
                  ? `<strong>${fmt(row[key])}</strong>`
                  : fmt(row[key]);

              return `
                <td class="num ${index === 3 ? 'transit' : ''}">
                  ${value}
                </td>
              `;
            });

            return `
              <tr>
                <td>
                  <button class="product" data-product="${row.id}">
                    ${esc(row.name)}<small>${esc(row.sku)}</small>
                  </button>
                </td>
                ${cells.join('')}
              </tr>
            `;
          }),
        );
      }
      if (state.view === 'operations')
        $('content').innerHTML = table(
          ['Тип', 'Товар', 'Шт.', 'Маршрут', 'Статус', 'Действие'],
          opRows(rows),
        );
      if (state.view === 'history')
        $('content').innerHTML = table(
          ['Время', 'Событие', 'Товар', 'Шт.', 'Маршрут', 'Результат'],
          rows.map(
            (r) =>
              `
                <tr>
                  <td>${date(r.created_at)}</td>
                  <td>${esc(events[r.event_type])}</td>
                  <td>${esc(r.sku)}<small>${esc(r.name)}</small></td>
                  <td>${fmt(r.quantity)}</td>
                  <td>${route(r)}</td>
                  <td>${esc(statuses[r.to_status])}</td>
                </tr>
              `,
          ),
        );
    }
    state.rows = rows;
    if (!rows.length)
      $('content').innerHTML =
        '<div class="empty">Нет данных по выбранным условиям</div>';
    $('pagination').hidden = state.view === 'warehouses';
    $('page-label').textContent =
      `Страница ${state.page} · ${rows.length} записей`;
    $('prev').disabled = state.page === 1;
    $('next').disabled = !has_next;
  } catch (e) {
    if (e.name !== 'AbortError')
      $('message').textContent = 'Не удалось загрузить данные: ' + e.message;
  } finally {
    if (state.controller === c) {
      $('refresh').disabled = false;
      $('content').removeAttribute('aria-busy');
    }
  }
}

async function showDetail(id) {
  const p = state.catalog.products.find((x) => x.id === id);
  $('detail-title').textContent = p ? `${p.name} · ${p.sku}` : 'Товар';
  $('detail-content').textContent = 'Загрузка…';
  $('detail-dialog').showModal();
  try {
    const d = await api('products/' + id);
    $('detail-content').innerHTML =
      '<h3>Остатки по ФФ</h3>' +
      table(
        ['Склад', 'Физически', 'Резерв', 'Доступно'],
        d.balances.map(
          (b) =>
            `
              <tr>
                <td>
                  ${esc(b.name)}
                  ${b.archived_at ? '<span class="badge">Архив</span>' : ''}
                </td>
                <td>${fmt(b.on_hand)}</td>
                <td>${fmt(b.reserved)}</td>
                <td><strong>${fmt(b.on_hand - b.reserved)}</strong></td>
              </tr>
            `,
        ),
      ) +
      '<h3>Последние операции · до 25 записей</h3>' +
      table(
        ['Тип', 'Товар', 'Шт.', 'Маршрут', 'Статус'],
        opRows(d.operations, false),
      ) +
      '<p class="note">Полная история доступна в разделе «История»: поиск по артикулу.</p>';
  } catch (e) {
    $('detail-content').textContent = e.message;
  }
}

function formOptions() {
  const t = $('op-type').value,
    old = $('op-source').value;
  const active = state.catalog.warehouses.filter((w) => !w.archived_at);
  $('op-source').innerHTML = options(
    t === 'transfer' ? state.catalog.warehouses : active,
    old,
  );
  $('op-dest').innerHTML = options(active, $('op-dest').value);
  $('source-label').hidden = t === 'receipt';
  $('dest-label').hidden = !['receipt', 'transfer'].includes(t);
  $('submit-op').textContent = {
    receipt: 'Оприходовать',
    transfer: 'Отправить между ФФ',
    fbs: 'Зарезервировать',
    fbo: 'Отправить FBO',
  }[t];
  availability();
}
let availabilitySequence = 0;

async function availability() {
  const seq = ++availabilitySequence;
  $('available').textContent = '';
  try {
    const p = $('op-product').value;
    if (!p) return;
    const d = await api('products/' + p);
    if (seq !== availabilitySequence) return;
    const b = d.balances.find((x) => x.id === $('op-source').value);
    if ($('op-type').value !== 'receipt')
      $('available').textContent =
        `Доступно на выбранном ФФ: ${b ? b.on_hand - b.reserved : 0} шт. Количество будет повторно проверено при подтверждении.`;
  } catch (e) {
    $('available').textContent = e.message;
  }
}

async function openOperation() {
  try {
    await catalog();
    state.formKey = crypto.randomUUID();
    $('form-error').textContent = '';
    $('op-product').innerHTML = state.catalog.products
      .map(
        (p) =>
          `<option value="${p.id}">${esc(p.sku)} · ${esc(p.name)}</option>`,
      )
      .join('');
    formOptions();
    $('form-dialog').showModal();
  } catch (e) {
    $('message').textContent = e.message;
  }
}

function confirmAction(title, text, path, data = {}, withName = false) {
  state.confirm = { path, data, key: crypto.randomUUID(), withName };
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = text;
  $('name-label').hidden = !withName;
  $('warehouse-name').required = withName;
  $('warehouse-name').value = data.name || '';
  $('confirm-error').textContent = '';
  $('confirm-dialog').showModal();
}
$('operation-form').onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target),
    t = f.get('type');
  const data = {
    key: state.formKey,
    type: t,
    product_id: f.get('product_id'),
    quantity: Number(f.get('quantity')),
  };
  if (t !== 'receipt') data.source_warehouse_id = f.get('source_warehouse_id');
  if (['receipt', 'transfer'].includes(t))
    data.destination_warehouse_id = f.get('destination_warehouse_id');
  const btn = $('submit-op');
  btn.disabled = true;
  try {
    await post('operations', data);
    $('form-dialog').close();
    await load();
  } catch (err) {
    $('form-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
$('confirm-form').onsubmit = async (e) => {
  e.preventDefault();
  const c = state.confirm,
    btn = e.submitter;
  btn.disabled = true;
  try {
    await post(c.path, {
      ...c.data,
      key: c.key,
      ...(c.withName ? { name: $('warehouse-name').value } : {}),
    });
    $('confirm-dialog').close();
    await catalog();
    await load();
  } catch (err) {
    $('confirm-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
document.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.close) $(b.dataset.close).close();
  if (b.dataset.view) {
    state.view = b.dataset.view;
    state.page = 1;
    load();
  }
  if (b.dataset.product) showDetail(b.dataset.product);
  if (b.dataset.filter) {
    $('warehouse').value = b.dataset.filter;
    state.view = 'stocks';
    state.page = 1;
    load();
  }
  if (b.dataset.action) {
    const r = state.rows.find((x) => x.id === b.dataset.id);
    confirmAction(
      {
        ship: 'Отправить FBS?',
        cancel: 'Отменить резерв?',
        receive: 'Подтвердить приёмку?',
      }[b.dataset.action],
      `${r.sku} · ${r.quantity} шт. Операция изменит остатки и будет записана в историю.`,
      `operations/${b.dataset.id}/${b.dataset.action}`,
    );
  }
  if (b.dataset.wh) {
    const w = state.catalog.warehouses.find((x) => x.id === b.dataset.id),
      a = b.dataset.wh;
    confirmAction(
      {
        rename: 'Переименовать ФФ',
        archive: 'Архивировать ФФ?',
        restore: 'Восстановить ФФ?',
      }[a],
      a === 'archive'
        ? 'Товар и история сохранятся. Новые приходы и резервы будут запрещены. Можно вывести остатки на действующий ФФ и завершить начатые операции.'
        : w.name,
      `warehouses/${w.id}/${a}`,
      a === 'rename' ? { name: w.name } : {},
      a === 'rename',
    );
  }
});
$('new').onclick = () =>
  state.view === 'warehouses'
    ? confirmAction(
        'Добавить ФФ',
        'Название нового склада',
        'warehouses',
        {},
        true,
      )
    : openOperation();
$('refresh').onclick = () => load();
$('prev').onclick = () => {
  state.page--;
  load();
};
$('next').onclick = () => {
  state.page++;
  load();
};
let timer;
$('search').oninput = () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    state.page = 1;
    load();
  }, 300);
};
for (const id of ['warehouse', 'mode', 'status', 'since'])
  $(id).onchange = () => {
    state.page = 1;
    load();
  };
$('op-type').onchange = formOptions;
for (const id of ['op-product', 'op-source']) $(id).onchange = availability;
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !document.querySelector('dialog[open]')) load();
});
$('since').value = new Date(Date.now() - 30 * 86400000)
  .toISOString()
  .slice(0, 10);
(async () => {
  try {
    await catalog();
    await load();
  } catch (e) {
    $('message').textContent = 'Нет соединения с приложением: ' + e.message;
  }
})();
