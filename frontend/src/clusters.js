import {runStates, slots, tagStates} from './domain.js';
import {safe, num, localTime, notice, tag, title, field, table, select, button, paragraph} from './ui.js';

function bicScores(result) {
  const scores = Object.values(result.bic);
  const high = Math.max(...scores);
  const low = Math.min(...scores);
  return `<ul class="bic">${Object.entries(result.bic).map(([k, value]) => `
    <li class="${Number(k) === result.chosen_k ? 'selected' : ''}"><span>${k} 组</span>
    <i style="--size:${25+75*(high-value)/(high-low || 1)}%"></i><b>${num(value,2)}</b></li>`).join('')}</ul>`;
}

const roleLabel = role => role === 'cool' ? '凉湿' : '暖干';

function standardPicker(blocks, standards, role) {
  const current = standards?.current?.[role];
  const label = `${roleLabel(role)}标准地`;
  return `<label>${label}<select name="${role}_block_id" aria-label="${label}" required>
    ${blocks.map(b => `<option value="${b.id}" ${String(b.id) === String(current?.block_id) ? 'selected' : ''}>${safe(`${b.code} ${b.name}`)}</option>`).join('')}
  </select></label>`;
}

function standardsSection(blocks, standards, supervisor) {
  const current = standards?.current || {};
  const history = (standards?.history || []).slice(0, 12);
  const anchors = `<div class="standards-current">${['cool','warm'].map(role => {
    const entry = current[role];
    return `<article class="anchor-card"><span class="ref">${roleLabel(role)}标准地</span>
      <h3>${entry ? safe(`${entry.code} ${entry.name}`) : '尚未选择'}</h3>
      <p class="muted">${entry ? `${safe(entry.selected_by)} · ${localTime(entry.selected_at)}` : '请主管在识别页面选择一块标准地。'}</p></article>`;
  }).join('')}</div>`;
  const picker = supervisor ? `<form data-action="standards">
      <div class="fields">${standardPicker(blocks, standards, 'cool')}${standardPicker(blocks, standards, 'warm')}</div>
      ${button('保存标准地')}</form>
      <p class="muted">两块必须是不同地块。更换后，待签群名撤下并按原拟合重判；排灌照旧。</p>` : '';
  const rows = history.map(entry => `<tr class="${entry.retired_at ? '' : 'warn-row'}">
    <td>${roleLabel(entry.role)}</td><td>${safe(entry.code)}</td>
    <td>${safe(entry.selected_by)} · ${localTime(entry.selected_at)}</td>
    <td>${entry.retired_at ? `${safe(entry.retired_by)} 撤下 ${localTime(entry.retired_at)}` : '现行'}</td></tr>`);
  return `<section class="bottom standards"><div class="section-label"><h2>田间标准地</h2><span>换季按标准地定名</span></div>
    ${anchors}${picker}
    ${rows.length ? table(['角色','地块','指定','状态'], rows) : ''}</section>`;
}

function anchorTable(naming) {
  const rows = ['cool','warm'].map(role => {
    const anchor = naming.anchors?.[role];
    return `<tr><td>${roleLabel(role)}</td><td>${anchor?.code ? safe(anchor.code) : '—'}</td>
      <td>${anchor && anchor.component !== null && anchor.component !== undefined ? `第 ${anchor.component + 1} 群` : '未在窗口内'}</td></tr>`;
  });
  return table(['标准地','地块','最可能归入'], rows);
}

function namingTrace(naming) {
  if (naming?.mode !== 'anchored') return '';
  const history = (naming.history || []).filter(entry => entry.anchors).slice().reverse();
  return `${anchorTable(naming)}${history.length ? `<h3>本批标准地留痕</h3>${table(
    ['时间','事件','操作人','凉湿 / 暖干','候选指纹'], history.map(entry => `<tr>
      <td>${localTime(entry.at)}</td><td>${safe(entry.event)}</td><td>${safe(entry.actor)}</td>
      <td>${safe(entry.anchors?.cool?.code || '—')} / ${safe(entry.anchors?.warm?.code || '—')}</td>
      <td class="mono">${entry.token ? safe(entry.token.slice(0,10)) : '—'}</td></tr>`))}` : ''}`;
}

function affectedTable(run) {
  if (!run.affected?.length) return notice('本次没有已排轮次受影响。');
  return `${table(['轮次','地块','排定时间','旧标签','候选标签'], run.affected.map(r =>
    `<tr><td>${safe(r.code)}</td><td>${safe(r.block)}</td><td>${localTime(r.starts_at)}</td><td>${tag(r.basis_profile)}</td><td>${tag(r.proposed_profile)}</td></tr>`))}
    <p>签发后需按新标签改排。</p>`;
}

function namingPanel(run, supervisor) {
  const naming = run.naming;
  if (naming?.mode !== 'anchored') return '';
  const canRejudge = supervisor && ['pending','collision','invalid'].includes(naming.state);
  const rejudge = canRejudge ? `<form data-action="naming-retry" data-id="${run.id}">${button('按原拟合重判', 'secondary')}</form>` : '';
  const mapping = Object.entries(naming.mapping || {}).map(([component, profile]) =>
    `<li>第 ${Number(component) + 1} 群 · ${tag(profile)}</li>`).join('');
  if (naming.state === 'pending') {
    const sign = supervisor ? `<form data-action="decision" data-id="${run.id}">
        <input type="hidden" name="token" value="${safe(naming.token)}">
        ${paragraph('reason','签发理由','例：两块标准地已核对，按新群名签发。')}
        <div class="actions"><button name="approve" value="true" class="primary">签发群名</button>
        <button name="approve" value="false" class="secondary">退回候选标签</button></div></form>`
      : notice('群名待灌溉主管签发。签发前排灌照旧。');
    return `<div class="review naming"><h3>标准地定名 · 待签发</h3>
      <p>按两块标准地各自最可能归入的群定名，其余群为温和。签发后生效。</p>
      <ul class="naming-map">${mapping}</ul>
      ${namingTrace(naming)}${affectedTable(run)}${sign}${rejudge}</div>`;
  }
  const reason = naming.reason || '请更换标准地后再定名。';
  return `<div class="review naming"><h3>标准地定名 · 等待换地</h3>
    ${notice(reason, 'caution')}
    ${namingTrace(naming)}
    ${notice('旧标签继续生效，排灌照旧。更换标准地后按原拟合重判。')}${rejudge}</div>`;
}

function signedTrace(run) {
  const naming = run.naming;
  if (naming?.mode !== 'anchored' || naming.state !== 'signed') return '';
  return `<details class="form-fold"><summary>本批标准地定名留痕</summary>${namingTrace(naming)}</details>`;
}

function provisionalReview(run, supervisor) {
  const sign = supervisor ? `<form data-action="decision" data-id="${run.id}">${paragraph('reason','确认或退回理由','例：核对北坡备用探头，调整下一轮滴灌。')}
      <div class="actions"><button name="approve" value="true" class="primary">确认并标记待改排</button><button name="approve" value="false" class="secondary">退回候选标签</button></div></form>`
    : notice('请通知灌溉主管确认。');
  return `<div class="review"><h3>这次标签变更影响已排轮次</h3><p>确认前继续沿用旧标签，受影响的轮次暂缓执行。确认后需按新标签改排。</p>
    ${affectedTable(run)}${sign}</div>`;
}

export function clusterPage(runs, run, labels, blocks, standards, canWrite, supervisor) {
  const result = run?.result;
  const naming = run?.naming;
  const review = run?.status === 'review';
  const anchored = naming?.mode === 'anchored';
  const healthRows = run?.coverage.map(c => `<tr class="${c.deficient?'warn-row':''}"><td>${safe(c.code)}</td><td>${safe(c.sensor)}</td><td>${c.received} / ${c.expected}</td>
    <td>${c.missing.map(m => `${safe(m.day.slice(5))} ${slots[m.slot]}`).join('、') || '齐全'}</td></tr>`);
  const centroidRows = result ? (anchored && naming.components?.length
    ? naming.components
    : Object.entries(result.centroids).map(([profile, centroid]) => ({profile, centroid})))
    .map(item => `<tr><td>${tag(item.profile)}</td><td>${num(item.centroid.day_temp)} / ${num(item.centroid.night_temp)}</td><td>${num(item.centroid.day_rh,0)} / ${num(item.centroid.night_rh,0)}</td></tr>`) : [];
  return `${title('微气候群落', '三个连续日期，每地块应有六笔昼夜采样。超过 25% 缺测时停止发布。换季时按两块田间标准地定名。')}
    <div class="spread ${review || run?.status === 'naming' ? 'review-spread' : ''}">
      <section>
        <div class="section-label"><h2>识别批次</h2>${run ? `<span class="tag ${review || run.status==='blocked' || run.status==='naming' ? 'warning' : 'neutral'}">${runStates[run.status]}</span>` : ''}</div>
        ${runs.length ? `<div class="selector">${select('run','查看采样窗口',runs.map(r => [r.id,`${r.window_end} · ${runStates[r.status]} · #${r.id}`]), run?.id)}</div>` : notice('从有完整采样的窗口发起第一次识别。')}
        ${run?.error ? notice(run.error, 'caution') : ''}
        ${run?.status === 'queued' ? notice('采样快照已冻结，正在等待后台识别。') : ''}
        ${run?.status === 'naming' ? notice('标准地群名尚未定名，旧标签继续生效、排灌照旧。请主管更换标准地。', 'caution') : ''}
        ${review ? namingPanel(run, supervisor) : ''}
        ${review && !anchored ? provisionalReview(run, supervisor) : ''}
        ${signedTrace(run)}
        ${healthRows?.length ? `<h3>探头覆盖</h3>${table(['地块','探头','有效读数','缺测班次'],healthRows)}` : ''}
        ${run?.status === 'failed' && canWrite ? `<form data-action="retry" data-id="${run.id}">${button('按原快照重试')}</form>` : ''}
        ${run ? `<p class="muted hash" title="${safe(run.digest)}">快照 ${safe(run.digest.slice(0,16))} · 计算 ${run.attempts} 次</p>` : ''}
      </section>
      <section class="ledger">
        ${canWrite ? `<h2>发起识别</h2><form data-action="run" class="inline-form">${field('window_end','窗口截止日','type="date" value="2026-11-20"')}${button('冻结采样并识别')}</form>` : ''}
        ${result ? `<div class="section-label"><h2>BIC 组数比较</h2><span class="mono">选中 ${result.chosen_k} 组</span></div>
          ${bicScores(result)}<p class="muted">BIC 越小越合适。群名由主管指定的两块标准地归群决定，其余群为温和。</p>
          ${table(['群落','日 / 夜温 °C','日 / 夜湿 %'], centroidRows)}
          <button class="text-button evidence" type="button" data-evidence="${run.id}">保存拟合证据 JSON</button>` : ''}
        ${standardsSection(blocks, standards, supervisor)}
      </section>
    </div>
    <section class="bottom"><h2>地块标签账</h2>${labels.length ? table(['地块','标签','版本','窗口截止','状态','后验概率'], labels.map(l =>
      `<tr><td>${safe(l.code)}</td><td>${tag(l.profile)}</td><td>v${l.revision}</td><td>${safe(l.window_end)}</td><td>${tagStates[l.state]}</td><td>${num(l.confidence*100,1)}%</td></tr>`)) : notice('模型开始发布标签后，这里会保留生效和历史版本。')}</section>`;
}
