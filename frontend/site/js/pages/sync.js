import { api } from '../api.js';
import { ENUMS, can, changeTone, syncTone, freshnessTone } from '../config.js';
import { badge, drawer, errorBox, errorMessage, esc, field, fmtTime, table, toast } from '../ui.js';

const DTYPES = Object.entries(ENUMS.dtype).map(([value, label]) => ({ value, label }));

function changesTable(changes) {
  if (!changes || !changes.length) return '<p class="muted">字段无变化（快照一致）。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>迁移规则</th><th>旧列</th><th>新列</th><th>旧类型</th><th>新类型</th></tr></thead>
    <tbody>${changes
      .map(
        (c) => `<tr style="cursor:default">
          <td>${badge(changeTone(c.change_kind), ENUMS.changeKind[c.change_kind] || c.change_kind)}</td>
          <td class="mono">${esc(c.old_name || (c.change_kind === 'added' ? '—' : c.column))}</td>
          <td class="mono">${esc(c.column)}</td>
          <td>${esc(c.old_dtype || '—')}</td>
          <td>${esc(c.dtype || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function snapshotDiff(before, after, changes) {
  const beforeMap = new Map((before || []).map((c) => [c.column, c]));
  const afterMap = new Map((after || []).map((c) => [c.column, c]));
  const renamedFrom = new Map((changes || []).filter((c) => c.change_kind === 'renamed').map((c) => [c.column, c.old_name]));
  const rows = [];
  for (const col of before || []) {
    const next = afterMap.get(col.column);
    let tone = 'ok';
    let mark = '保留';
    if (!next) {
      tone = 'danger';
      mark = renamedFrom.has(col.column) ? '' : '删除';
    } else if (next.dtype && col.dtype && next.dtype !== col.dtype) {
      tone = 'warn';
      mark = '类型变更';
    }
    rows.push({ side: 'before', column: col.column, dtype: col.dtype, tone, mark });
  }
  for (const col of after || []) {
    const prev = beforeMap.get(col.column);
    let tone = 'ok';
    let mark = '保留';
    if (!prev) {
      tone = renamedFrom.has(col.column) ? 'warn' : 'ok';
      mark = renamedFrom.get(col.column) ? `改名自 ${renamedFrom.get(col.column)}` : '新增';
    }
    if (!prev) rows.push({ side: 'after', column: col.column, dtype: col.dtype, tone, mark });
  }
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>字段</th><th>类型</th><th>变化</th></tr></thead>
    <tbody>${rows
      .map(
        (r) => `<tr style="cursor:default">
          <td class="mono">${esc(r.column)}</td>
          <td>${esc(r.dtype || '—')}</td>
          <td>${r.mark ? badge(r.tone, r.mark) : '<span class="muted">—</span>'}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function scopeTable(relations, suggestions) {
  const relRows = (relations || []).length
    ? relations
        .map(
          (r) => `<tr style="cursor:default"><td class="mono">${esc(r.left_table || '')}.${esc(r.left_column || '')}</td>
            <td class="mono">→ ${esc(r.right_table || '')}.${esc(r.right_column || '')}</td></tr>`,
        )
        .join('')
    : '<tr><td colspan="2" class="muted">无受影响关系</td></tr>';
  const sugRows = (suggestions || []).length
    ? suggestions
        .map(
          (s) => `<tr style="cursor:default"><td class="mono">${esc(s.target_name || '')}</td>
            <td>${badge(freshnessTone(s.freshness), s.freshness_label || s.freshness || '')}</td>
            <td class="muted">${esc(s.stale_reason || '')}</td></tr>`,
        )
        .join('')
    : '<tr><td colspan="3" class="muted">无受影响建议（新增列不影响既有结论）</td></tr>';
  return `<h3>受影响关系（${(relations || []).length}）</h3>
    <div class="table-wrap"><table class="data" style="min-width:0">
      <thead><tr><th>左表.列</th><th>右表.列</th></tr></thead><tbody>${relRows}</tbody></table></div>
    <h3>受影响建议（${(suggestions || []).length}）</h3>
    <div class="table-wrap"><table class="data" style="min-width:0">
      <thead><tr><th>目标表</th><th>依据状态</th><th>失效原因</th></tr></thead><tbody>${sugRows}</tbody></table></div>`;
}

function openDetail(view, ctx, syncId) {
  api
    .get(`/api/structure-syncs/${syncId}`)
    .then((detail) => {
      const canRebuild = can(ctx.user.role, 'sync:write') && detail.status === 'success';
      const { root, close } = drawer({
        title: `结构同步 #${detail.id} · ${detail.table_cn_name || ''}`,
        subtitle: `${detail.table_name || ''} · ${detail.status_label} · ${fmtTime(detail.created_at)}`,
        body: `<dl class="kv">
            <dt>状态</dt><dd>${badge(syncTone(detail.status), detail.status_label)}</dd>
            <dt>结构版本</dt><dd class="mono">v${(detail.before_snapshot || []).length} 列 → v${(detail.after_snapshot || []).length} 列</dd>
            <dt>操作人</dt><dd class="mono">${esc(detail.operator_email)}</dd>
            <dt>确认人</dt><dd class="mono">${esc(detail.confirmed_by || '—')} · ${fmtTime(detail.confirmed_at)}</dd>
            ${detail.error_message ? `<dt>失败原因</dt><dd class="badge danger">${esc(detail.error_message)}</dd>` : ''}
          </dl>
          <h3>变化前后字段</h3>
          ${snapshotDiff(detail.before_snapshot, detail.after_snapshot, detail.changes)}
          <h3>字段迁移规则（${(detail.changes || []).length}）</h3>
          ${changesTable(detail.changes)}
          ${scopeTable(detail.affected_relations, detail.affected_suggestions)}`,
        footer: canRebuild
          ? '<button class="btn accent" id="rebuild-btn">确认并按新结构重新建模</button><span class="mono muted" style="margin-left:8px">受影响建议需重新采纳，无关建议保持可用</span>'
          : '<span class="mono muted">仅管理员可确认重建；失败或已重建的同步不可再次确认。</span>',
      });
      root.querySelector('#rebuild-btn')?.addEventListener('click', async (event) => {
        const btn = event.currentTarget;
        btn.disabled = true;
        btn.textContent = '重建中…';
        try {
          const result = await api.post(`/api/structure-syncs/${detail.id}/rebuild`, {});
          toast(`已按新结构重建（运行 #${result.rebuilt_run_id}），受影响建议请重新采纳`);
          close();
          await syncPage.render(view, ctx);
        } catch (err) {
          toast(errorMessage(err), 'err');
          btn.disabled = false;
          btn.textContent = '确认并按新结构重新建模';
        }
      });
    })
    .catch((err) => toast(errorMessage(err), 'err'));
}

function columnEditor(columns) {
  const rows = columns
    .map(
      (c, i) => `<div class="sync-col-row" data-index="${i}">
        <input name="col_name" value="${esc(c.column)}" placeholder="列名" />
        <select name="col_dtype">${DTYPES.map((d) => `<option value="${d.value}" ${d.value === c.dtype ? 'selected' : ''}>${esc(d.label)}</option>`).join('')}</select>
        <button type="button" class="btn sm ghost" data-remove>删</button>
      </div>`,
    )
    .join('');
  return `<div id="col-editor">${rows}</div><button type="button" class="btn sm ghost" id="add-col">+ 增加字段</button>`;
}

function openCreate(view, ctx, tables) {
  const state = { tableId: tables[0]?.id || null, columns: [], rowsText: '', renames: [] };
  const loadTable = (tableId) => {
    api.get(`/api/source-tables/${tableId}`).then((detail) => {
      state.tableId = tableId;
      state.columns = (detail.structure_snapshot || detail.profiles.map((p) => ({ column: p.column, dtype: p.dtype }))).map((c) => ({ ...c }));
      document.getElementById('col-editor-wrap').innerHTML = columnEditor(state.columns);
      // 抽样行留空即沿用现有抽样行，避免预览截断导致误覆盖
      const rowsBox = document.getElementById('sample-rows');
      if (rowsBox) rowsBox.value = '';
      bindEditor();
    });
  };

  const { root, close } = drawer({
    title: '发起结构同步',
    subtitle: '按上游最新贴源结构提交；删除 / 改名 / 类型变更会立即失效相关建议的导出资格',
    body: `<form id="sync-form" style="display:grid;gap:12px">
      ${field({
        label: '贴源表',
        name: 'table_id',
        value: state.tableId,
        options: tables.map((t) => ({ value: t.id, label: `${t.cn_name}（${t.name}）v${t.structure_version}` })),
      })}
      <div>
        <label class="field"><b>新结构字段（列名 + 类型）</b></label>
        <div id="col-editor-wrap"></div>
      </div>
      <div class="field">
        <label for="rename_old"><b>改名规则（可选，格式：旧列名）</b></label>
        <input id="rename_old" placeholder="如 region_id，提交时与下方选中的新列配对" />
      </div>
      <div class="field">
        <label for="rename_new"><b>改名后的新列（可选）</b></label>
        <select id="rename_new"><option value="">— 不声明改名 —</option></select>
      </div>
      ${field({ label: '抽样行数 row_count（0 则按抽样行计数）', name: 'row_count', type: 'number', value: '0' })}
      <div class="field">
        <label for="sample-rows"><b>抽样行 JSON（留空沿用现有抽样行）</b></label>
        <textarea id="sample-rows" rows="6" placeholder="留空则保留当前抽样行并重新剖析；填写时必须是对象数组" style="width:100%;font-family:var(--font-mono);font-size:0.75rem"></textarea>
      </div>
      <div class="form-error" id="sync-error"></div>
      <button class="btn accent" type="submit">提交结构同步</button>
    </form>`,
  });

  const bindEditor = () => {
    const editor = root.querySelector('#col-editor-wrap');
    editor.querySelectorAll('[data-remove]').forEach((btn) =>
      btn.addEventListener('click', () => {
        btn.closest('.sync-col-row').remove();
        syncRenameOptions();
      }),
    );
    syncRenameOptions();
  };
  const syncRenameOptions = () => {
    const names = [...root.querySelectorAll('#col-editor-wrap [name=col_name]')].map((i) => i.value).filter(Boolean);
    const sel = root.querySelector('#rename_new');
    if (sel) sel.innerHTML = '<option value="">— 不声明改名 —</option>' + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  };
  root.querySelector('#add-col').addEventListener('click', () => {
    const wrap = root.querySelector('#col-editor-wrap');
    wrap.insertAdjacentHTML('beforeend', `<div class="sync-col-row">
      <input name="col_name" value="" placeholder="新列名" />
      <select name="col_dtype">${DTYPES.map((d) => `<option value="${d.value}">${esc(d.label)}</option>`).join('')}</select>
      <button type="button" class="btn sm ghost" data-remove>删</button></div>`);
    bindEditor();
    wrap.querySelector('.sync-col-row:last-child input')?.focus();
  });
  root.querySelector('[name=table_id]').addEventListener('change', (e) => loadTable(Number(e.target.value)));
  if (tables.length) loadTable(tables[0].id);

  root.querySelector('#sync-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const columns = [...root.querySelectorAll('#col-editor-wrap .sync-col-row')].map((row) => ({
      column: row.querySelector('[name=col_name]').value.trim(),
      dtype: row.querySelector('[name=col_dtype]').value,
    }));
    if (!columns.length || columns.some((c) => !c.column)) {
      root.querySelector('#sync-error').textContent = '字段列表不能为空，且每列都要有列名';
      return;
    }
    const renameOld = root.querySelector('#rename_old').value.trim();
    const renameNew = root.querySelector('#rename_new').value;
    const changes = renameOld && renameNew ? [{ change_kind: 'renamed', old_name: renameOld, column: renameNew }] : [];
    const rowsText = root.querySelector('#sample-rows').value.trim();
    let sampleRows = [];
    if (rowsText) {
      try {
        sampleRows = JSON.parse(rowsText);
        if (!Array.isArray(sampleRows)) throw new Error('not array');
      } catch {
        root.querySelector('#sync-error').textContent = '抽样行 JSON 不合法，需要是对象数组';
        return;
      }
    }
    const tableId = Number(form.table_id.value);
    try {
      const result = await api.post(`/api/source-tables/${tableId}/structure-sync`, {
        columns,
        sample_rows: sampleRows,
        row_count: Number(form.row_count.value) || 0,
        changes,
      });
      const stale = (result.affected_suggestion_ids || []).length;
      toast(stale ? `同步完成，${stale} 条建议依据已过期，导出已锁定` : '同步完成，无既有建议受影响');
      close();
      await syncPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#sync-error').textContent = errorMessage(err);
    }
  });
}

export const syncPage = {
  async render(view, ctx) {
    let syncs;
    let tables;
    try {
      [syncs, tables] = await Promise.all([
        api.get('/api/structure-syncs', { limit: 50 }),
        api.get('/api/source-tables', { limit: 50 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => syncPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>结构同步</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${syncs.total} 次同步 · 每次留痕可追溯</span>
            ${can(ctx.user.role, 'sync:write') ? '<button class="btn sm accent" id="new-sync">发起结构同步</button>' : ''}
          </div>
        </div>
        ${table({
          rows: syncs.items,
          empty: { title: '还没有结构同步记录', hint: '上游贴源表字段变更后，由管理员在这里发起同步。' },
          columns: [
            {
              key: 'id',
              label: '同步',
              render: (row) => `<b class="mono">#${row.id}</b><div class="mono muted">${fmtTime(row.created_at)}</div>`,
            },
            {
              key: 'table_name',
              label: '贴源表',
              render: (row) => `<b>${esc(row.table_cn_name || '')}</b><div class="mono muted">${esc(row.table_name || '')}</div>`,
            },
            {
              key: 'changes',
              label: '字段变化',
              render: (row) => Object.entries(row.change_summary || {})
                .filter(([, n]) => n)
                .map(([k, n]) => badge(changeTone(k), `${ENUMS.changeKind[k] || k} ${n}`))
                .join(' ') || '<span class="muted">无</span>',
            },
            { key: 'affected_relation_ids', label: '失效关系', className: 'num', render: (row) => (row.affected_relation_ids || []).length },
            { key: 'affected_suggestion_ids', label: '失效建议', className: 'num', render: (row) => (row.affected_suggestion_ids || []).length },
            { key: 'status', label: '状态', render: (row) => badge(syncTone(row.status), row.status_label) },
            { key: 'operator_email', label: '操作人', className: 'mono', render: (row) => esc(row.operator_email) },
          ],
        })}
      </section>
      <p class="mono muted">删除 / 改名 / 类型变更立即失效引用字段的关系与建议（导出资格锁定）；仅新增列不影响既有建议。确认重建后受影响建议需重新采纳，无关建议保持可用。</p>`;

    view.querySelectorAll('table.data tbody tr').forEach((rowEl) => {
      rowEl.addEventListener('click', () => openDetail(view, ctx, Number(rowEl.dataset.id)));
    });
    view.querySelector('#new-sync')?.addEventListener('click', () => openCreate(view, ctx, tables.items));
  },
};

export default syncPage;
