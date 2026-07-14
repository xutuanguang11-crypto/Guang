/* 现场工单全局总览、创建、筛选、详情和闭环。 */
const WORK_TYPES=['质量','安全','进度','材料','设计','其他'];
const WORK_STATUSES=['待派单','处理中','待验收','已完成'];
const workEsc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const workDate=value=>value?String(value).replace('T',' ').slice(0,16):'—';
const workPhotos=async files=>Promise.all(Array.from(files).map(filePayload));
const workBadge=status=>status==='已完成'?'green':status==='待验收'?'orange':status==='处理中'?'blue':'gray';

views.workorders=async function(){
  await loadProjects();
  const orders=await optionalItems('/api/work-orders');
  const now=Date.now(),done=x=>['已完成','已销账'].includes(x.status),overdue=x=>!done(x)&&x.due_at&&new Date(x.due_at).getTime()<now;
  const completed=orders.filter(done),timely=completed.filter(x=>!x.due_at||new Date(x.completed_at||x.updated_at)<=new Date(x.due_at));
  layout(`<div class="work-summary grid stat-grid optimized-stats">
    <div class="stat"><span class="label">工单总量</span><strong>${orders.length}</strong><span class="delta">权限范围内全部工单</span></div>
    <div class="stat"><span class="label">待我处理</span><strong>${orders.filter(x=>!done(x)).length}</strong><span class="delta">待派单、处理中及待验收</span></div>
    <div class="stat"><span class="label">超时工单</span><strong class="${orders.some(overdue)?'red':''}">${orders.filter(overdue).length}</strong><span class="delta">按整改截止时间统计</span></div>
    <div class="stat"><span class="label">整改及时率</span><strong>${completed.length?Math.round(timely.length/completed.length*100):0}%</strong><span class="delta">已完成工单统计</span></div>
  </div>
  <div class="toolbar work-toolbar"><div class="filters work-filters">
    <input id="work-q" placeholder="搜索工单编号、项目或问题" />
    <select id="work-status"><option value="">全部状态</option>${WORK_STATUSES.map(x=>`<option>${x}</option>`).join('')}</select>
    <select id="work-type"><option value="">全部类型</option>${WORK_TYPES.map(x=>`<option>${x}</option>`).join('')}</select>
    <select id="work-project"><option value="">全部项目</option>${appState.projects.map(p=>`<option value="${p.id}">${workEsc(p.name)}</option>`).join('')}</select>
    <input id="work-assignee" placeholder="责任人" />
    <input id="work-from" type="date" title="开始日期" /><input id="work-to" type="date" title="结束日期" />
  </div><div class="work-actions"><button class="secondary" id="work-overdue">超时工单</button><button class="secondary" id="go-projects">进入项目管理</button><button class="primary" id="add-work">＋ 新建现场工单</button></div></div>
  <div class="card table-wrap"><table class="work-table"><thead><tr><th>工单编号</th><th>所属项目</th><th>问题概要</th><th>上报人 / 时间</th><th>责任人</th><th>现场图片</th><th>整改截止</th><th>状态</th><th>操作</th></tr></thead><tbody id="work-body"></tbody></table></div>`);
  const draw=()=>{
    const q=$('#work-q').value.trim().toLowerCase(),status=$('#work-status').value,type=$('#work-type').value,pid=$('#work-project').value,assignee=$('#work-assignee').value.trim(),from=$('#work-from').value,to=$('#work-to').value,onlyOverdue=$('#work-overdue').classList.contains('active');
    const matchesStatus=x=>!status||(status==='待派单'?['待派单','待派发'].includes(x.status):status==='已完成'?done(x):x.status===status);
    const rows=orders.filter(x=>(!q||[x.code,x.project_name,x.title,x.description].some(v=>String(v||'').toLowerCase().includes(q)))&&matchesStatus(x)&&(!type||x.work_type===type)&&(!pid||String(x.project_id)===pid)&&(!assignee||String(x.assignee||'').includes(assignee))&&(!from||x.created_at.slice(0,10)>=from)&&(!to||x.created_at.slice(0,10)<=to)&&(!onlyOverdue||overdue(x)));
    $('#work-body').innerHTML=rows.map(x=>`<tr class="${overdue(x)?'work-overdue-row':''}"><td><strong>${workEsc(x.code)}</strong></td><td>${workEsc(x.project_name)}</td><td><strong>${workEsc(x.title||x.description)}</strong><span class="sub"><span class="badge gray">${workEsc(x.work_type)}</span> ${workEsc(x.description)}</span></td><td>${workEsc(x.reporter||'管理员')}<span class="sub">${workDate(x.created_at)}</span></td><td>${workEsc(x.assignee||'—')}</td><td>${x.photo_count?`<button class="work-thumb" data-detail="${x.id}" title="查看现场图片">📷 ${x.photo_count}</button>`:'—'}</td><td class="${overdue(x)?'red':''}">${workDate(x.due_at)}${overdue(x)?'<span class="sub red">已超时</span>':''}</td><td><span class="badge ${workBadge(done(x)?'已完成':x.status)}">${workEsc(done(x)?'已完成':x.status)}</span></td><td><button class="action" data-detail="${x.id}">查看</button></td></tr>`).join('')||'<tr><td colspan="9" class="empty">暂无符合条件的现场工单</td></tr>';
    document.querySelectorAll('[data-detail]').forEach(button=>button.onclick=()=>openWorkDetail(button.dataset.detail));
  };
  ['work-q','work-assignee','work-from','work-to'].forEach(id=>$(`#${id}`).oninput=draw);['work-status','work-type','work-project'].forEach(id=>$(`#${id}`).onchange=draw);
  $('#work-overdue').onclick=()=>{$('#work-overdue').classList.toggle('active');draw()};$('#go-projects').onclick=()=>render('projects');$('#add-work').onclick=openGlobalWorkModal;draw();
};

async function openGlobalWorkModal(){
  await loadProjects();if(!appState.projects.length)return toast('请先由赢单线索创建项目');
  openModal('新建现场工单',`<div class="form-grid"><div class="field full"><label>所属项目 *</label>${selectProjects()}</div><div class="field full"><label>问题标题 *</label><input name="title" maxlength="80" required placeholder="例如：西侧卫生间防水渗漏" /></div><div class="field full"><label>问题描述 *</label><textarea name="description" rows="4" required></textarea></div><div class="field"><label>问题类型 *</label><select name="work_type" required>${WORK_TYPES.map(x=>`<option>${x}</option>`).join('')}</select></div><div class="field"><label>处理责任人 *</label><input name="assignee" required placeholder="手工输入完整姓名" /></div><div class="field"><label>整改截止时间</label><input name="due_at" type="datetime-local" /></div><div class="field"><label>上报人</label><input value="管理员" disabled /></div><div class="field full"><label>现场图片 *（至少 1 张）</label><input name="photos" type="file" accept="image/*" multiple required /></div></div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">创建工单</button></div>`,async form=>{try{const files=form.elements.photos.files;if(!files.length)return toast('请至少上传 1 张现场图片');const data=Object.fromEntries(new FormData(form));delete data.photos;data.photos=await workPhotos(files);await api('/api/work-orders',{method:'POST',body:JSON.stringify(data)});closeModal();views.workorders();toast('现场工单已创建')}catch(error){toast(error.message)}});
}

async function openWorkDetail(id){
  try{const data=await api(`/api/work-orders/${id}/detail`),x=data.work_order,photos=data.photos||[],logs=data.logs||[],done=['已完成','已销账'].includes(x.status);
    const photoHtml=photos.map(p=>`<a href="/api/work-order-photo/${p.id}" target="_blank" class="work-photo"><img src="/api/work-order-photo/${p.id}" alt="${workEsc(p.stage)}" /><span>${workEsc(p.stage)}</span></a>`).join('')||'<span class="sub">暂无图片</span>';
    const actions=done?'':(['待派单','待派发'].includes(x.status)?`<button class="primary" data-work-start="${x.id}">确认派单</button>`:x.status==='处理中'?`<button class="primary" data-work-submit="${x.id}">提交验收</button>`:x.status==='待验收'?`<button class="secondary" data-work-reject="${x.id}">验收驳回</button><button class="primary" data-work-pass="${x.id}">验收通过</button>`:'');
    openModal(`工单详情 · ${x.code}`,`<div class="work-detail"><div class="work-detail-grid"><div><span>所属项目</span><strong>${workEsc(x.project_name)}</strong></div><div><span>状态</span><strong><i class="badge ${workBadge(done?'已完成':x.status)}">${workEsc(done?'已完成':x.status)}</i></strong></div><div><span>问题类型</span><strong>${workEsc(x.work_type)}</strong></div><div><span>责任人</span><strong>${workEsc(x.assignee)}</strong></div><div><span>上报人 / 时间</span><strong>${workEsc(x.reporter||'管理员')} · ${workDate(x.created_at)}</strong></div><div><span>整改截止</span><strong>${workDate(x.due_at)}</strong></div></div><h3>${workEsc(x.title||x.description)}</h3><p>${workEsc(x.description)}</p>${x.resolution_note?`<div class="work-note"><strong>处理说明</strong><p>${workEsc(x.resolution_note)}</p></div>`:''}${x.reject_reason?`<div class="work-note reject"><strong>最近驳回原因</strong><p>${workEsc(x.reject_reason)}</p></div>`:''}<h3>现场与整改图片</h3><div class="work-photos">${photoHtml}</div><h3>操作记录</h3><div class="work-log">${logs.map(l=>`<div><i></i><strong>${workEsc(l.action)}</strong><span>${workEsc(l.detail||'')} · ${workDate(l.created_at)}</span></div>`).join('')||'<span class="sub">暂无操作记录</span>'}</div></div><div class="form-actions"><button type="button" class="secondary" data-close>关闭</button>${actions}</div>`,()=>{});
    const start=$('[data-work-start]'),submit=$('[data-work-submit]'),reject=$('[data-work-reject]'),pass=$('[data-work-pass]');
    if(start)start.onclick=async()=>{await api(`/api/work-orders/${id}/advance`,{method:'PATCH',body:'{}'});closeModal();views.workorders();toast('工单已派单')};
    if(submit)submit.onclick=()=>openWorkProcessModal(id);
    if(reject)reject.onclick=()=>openWorkRejectModal(id);
    if(pass)pass.onclick=()=>openWorkPassModal(id);
  }catch(error){toast(error.message)}
}

function openWorkProcessModal(id){openModal('提交整改验收',`<div class="form-grid"><div class="field full"><label>处理说明 *</label><textarea name="resolution_note" rows="4" required></textarea></div><div class="field full"><label>整改后图片 *（至少 1 张）</label><input name="photos" type="file" accept="image/*" multiple required /></div></div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">提交验收</button></div>`,async form=>{try{if(!form.elements.photos.files.length)return toast('请上传整改后图片');await api(`/api/work-orders/${id}/advance`,{method:'PATCH',body:JSON.stringify({resolution_note:form.elements.resolution_note.value,photos:await workPhotos(form.elements.photos.files)})});closeModal();views.workorders();toast('已提交验收')}catch(e){toast(e.message)}})}
function openWorkRejectModal(id){openModal('验收驳回',`<div class="field"><label>驳回原因 *</label><textarea name="reject_reason" rows="4" required></textarea></div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">确认驳回</button></div>`,async form=>{try{await api(`/api/work-orders/${id}/reject`,{method:'PATCH',body:JSON.stringify({reject_reason:form.elements.reject_reason.value})});closeModal();views.workorders();toast('工单已退回责任人处理')}catch(e){toast(e.message)}})}
function openWorkPassModal(id){openModal('验收通过',`<div class="form-grid"><div class="field full"><label>验收图片 *（至少 1 张）</label><input name="photos" type="file" accept="image/*" multiple required /></div><div class="field full"><label>验收备注</label><textarea name="note" rows="3"></textarea></div></div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">验收通过</button></div>`,async form=>{try{if(!form.elements.photos.files.length)return toast('请上传验收图片');await api(`/api/work-orders/${id}/close`,{method:'PATCH',body:JSON.stringify({note:form.elements.note.value,photos:await workPhotos(form.elements.photos.files)})});closeModal();views.workorders();toast('工单已完成')}catch(e){toast(e.message)}})}

$('#new-action').onclick=()=>state.view==='workorders'?openGlobalWorkModal():openLeadModal();
