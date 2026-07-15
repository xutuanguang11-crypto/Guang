/* Role-aware navigation, operation visibility, dashboard, and owner-only user management. */
const rbacEscape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const roleNames={owner:'老板',project_manager:'项目经理',finance:'财务',business:'业务'};
const mutatingAction=/新建|新增|创建|修改|编辑|删除|保存|提交|确认|审核|驳回|通过|销账|上传|录入|付款|收款|推进|完成|赢单|未成交|恢复|重置|停用|启用|派单/;

function applyRbacToDom(){
  const user=window.getCurrentUser?.();
  if(!user)return;
  document.querySelectorAll('[data-view]').forEach(item=>item.hidden=!window.canAccessView(item.dataset.view));
  const profile=document.querySelector('.profile');
  if(profile){
    const initial=profile.querySelector(':scope > span'),name=profile.querySelector('strong'),role=profile.querySelector('small');
    if(initial)initial.textContent=(user.display_name||user.email||'用').slice(0,1);
    if(name)name.textContent=user.display_name||user.email;
    if(role)role.textContent=user.role_label||roleNames[user.role]||user.role;
  }
  const title=document.querySelector('#page-title');
  if(title&&state?.view==='dashboard')title.textContent=`你好，${user.display_name||user.email}`;
  const view=state?.view||'dashboard',writable=window.canWriteView(view);
  const topAction=document.querySelector('#new-action');
  if(topAction)topAction.hidden=!writable;
  if(!writable){
    document.querySelectorAll('#view button, #modal-form button').forEach(button=>{
      if(button.matches('[data-close],.close')||/查看|详情|返回|导出|下载|关闭/.test(button.textContent||''))return;
      if(mutatingAction.test(button.textContent||''))button.hidden=true;
    });
  }
}

const renderBeforeRbac=render;
render=function(view){
  if(window.getCurrentUser?.()&&!window.canAccessView(view))view='dashboard';
  const result=renderBeforeRbac(view);
  Promise.resolve(result).finally(applyRbacToDom);
  return result;
};

const openModalBeforeRbac=openModal;
openModal=function(...args){
  const result=openModalBeforeRbac(...args);
  queueMicrotask(applyRbacToDom);
  return result;
};

const dashboardBeforeRbac=views.dashboard;
views.dashboard=async function(){
  const user=window.getCurrentUser?.();
  if(!user||user.role==='owner')return dashboardBeforeRbac();
  try{
    if(user.role==='business'){
      const [leads,projects]=await Promise.all([api('/api/leads'),api('/api/projects')]);
      layout(`<div class="grid stat-grid"><div class="stat"><span class="label">我的进行中线索</span><strong>${leads.items.filter(x=>x.status==='进行中').length}</strong></div><div class="stat"><span class="label">我的赢单项目</span><strong>${projects.items.length}</strong></div></div><div class="section-head"><h2>我的线索</h2><button class="link" data-go="leads">进入跟进 →</button></div><div class="card table-wrap"><table><thead><tr><th>线索</th><th>客户</th><th>状态</th></tr></thead><tbody>${leads.items.slice(0,8).map(x=>`<tr><td>${rbacEscape(x.name)}</td><td>${rbacEscape(x.customer)}</td><td>${rbacEscape(x.status)}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">暂无线索</td></tr>'}</tbody></table></div>`);
    }else if(user.role==='project_manager'){
      const [projects,orders,reports]=await Promise.all([api('/api/projects'),api('/api/work-orders'),api('/api/labor-reports')]);
      layout(`<div class="grid stat-grid"><div class="stat"><span class="label">已分配项目</span><strong>${projects.items.length}</strong></div><div class="stat"><span class="label">现场工单</span><strong>${orders.items.length}</strong></div><div class="stat"><span class="label">待审核日报</span><strong>${reports.items.filter(x=>x.status==='待审核').length}</strong></div></div><div class="section-head"><h2>我的项目</h2><button class="link" data-go="projects">进入项目 →</button></div><div class="card table-wrap"><table><tbody>${projects.items.map(x=>`<tr><td>${rbacEscape(x.code)} · ${rbacEscape(x.name)}</td><td>${rbacEscape(x.status)}</td><td>${Number(x.progress)||0}%</td></tr>`).join('')||'<tr><td class="empty">尚未分配项目</td></tr>'}</tbody></table></div>`);
    }else{
      const [projects,contracts]=await Promise.all([api('/api/projects'),api('/api/contracts')]);
      layout(`<div class="grid stat-grid"><div class="stat"><span class="label">项目总数</span><strong>${projects.items.length}</strong></div><div class="stat"><span class="label">合同数</span><strong>${contracts.items.length}</strong></div></div><div class="section-head"><h2>财务工作台</h2><button class="link" data-go="contracts">合同与回款 →</button></div><div class="card table-wrap"><table><tbody>${contracts.items.slice(0,8).map(x=>`<tr><td>${rbacEscape(x.code)}</td><td>${rbacEscape(x.project_name)}</td><td>${money(x.dynamic_amount||x.amount)}</td></tr>`).join('')||'<tr><td class="empty">暂无合同</td></tr>'}</tbody></table></div>`);
    }
    document.querySelectorAll('[data-go]').forEach(button=>button.onclick=()=>render(button.dataset.go));
  }catch(error){layout(`<div class="card empty">工作台加载失败：${rbacEscape(error.message)}</div>`)}
  applyRbacToDom();
};

const settingsBeforeUsers=views.settings;
views.settings=async function(){
  await settingsBeforeUsers();
  if(window.getCurrentUser?.()?.role!=='owner')return;
  const [users,projects]=await Promise.all([api('/api/users'),api('/api/projects')]);
  const section=document.createElement('div');
  section.innerHTML=`<div class="section-head"><h2>账号与角色</h2><button class="primary" id="create-user">＋ 创建账号</button></div><div class="notice">不开放公众注册。账号由老板创建、停用、重置临时密码，并为项目经理分配项目。</div><div class="card table-wrap"><table><thead><tr><th>姓名 / 邮箱</th><th>角色</th><th>项目经理归属</th><th>状态</th><th>操作</th></tr></thead><tbody>${users.items.map(item=>`<tr data-user-row="${item.id}"><td><strong>${rbacEscape(item.display_name)}</strong><span class="sub">${rbacEscape(item.email)}</span></td><td><select data-user-role><option value="owner" ${item.role==='owner'?'selected':''}>老板</option><option value="project_manager" ${item.role==='project_manager'?'selected':''}>项目经理</option><option value="finance" ${item.role==='finance'?'selected':''}>财务</option><option value="business" ${item.role==='business'?'selected':''}>业务</option></select></td><td><select data-user-projects multiple size="${Math.min(4,Math.max(1,projects.items.length))}">${projects.items.map(p=>`<option value="${p.id}" ${(item.project_ids||[]).includes(Number(p.id))?'selected':''}>${rbacEscape(p.code)} · ${rbacEscape(p.name)}</option>`).join('')}</select></td><td><label><input type="checkbox" data-user-active ${item.active?'checked':''}> 启用</label></td><td><button class="action" data-save-user>保存</button> <button class="action" data-reset-user>重置密码</button></td></tr>`).join('')}</tbody></table></div>`;
  document.querySelector('#view').appendChild(section);
  document.querySelector('#create-user').onclick=()=>openCreateUser(projects.items);
  document.querySelectorAll('[data-user-row]').forEach(row=>{
    row.querySelector('[data-save-user]').onclick=async()=>{
      const projectIds=[...row.querySelector('[data-user-projects]').selectedOptions].map(option=>Number(option.value));
      try{await api(`/api/users/${row.dataset.userRow}`,{method:'PATCH',body:JSON.stringify({role:row.querySelector('[data-user-role]').value,active:row.querySelector('[data-user-active]').checked,project_ids:projectIds})});toast('账号权限已保存');views.settings()}catch(error){toast(error.message)}
    };
    row.querySelector('[data-reset-user]').onclick=()=>openResetPassword(row.dataset.userRow);
  });
};

function projectAssignmentFields(projects){return `<div class="field full"><label>分配项目（仅项目经理）</label><select name="project_ids" multiple size="${Math.min(5,Math.max(2,projects.length))}">${projects.map(p=>`<option value="${p.id}">${rbacEscape(p.code)} · ${rbacEscape(p.name)}</option>`).join('')}</select></div>`}
function openCreateUser(projects){openModal('创建内部账号',`<div class="form-grid"><div class="field"><label>姓名 *</label><input name="display_name" required></div><div class="field"><label>邮箱 *</label><input name="email" type="email" required></div><div class="field"><label>临时密码 *</label><input name="password" type="password" minlength="8" required></div><div class="field"><label>角色 *</label><select name="role"><option value="project_manager">项目经理</option><option value="finance">财务</option><option value="business">业务</option><option value="owner">老板</option></select></div>${projectAssignmentFields(projects)}</div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">创建账号</button></div>`,async form=>{const data=Object.fromEntries(new FormData(form));data.project_ids=[...form.elements.project_ids.selectedOptions].map(option=>Number(option.value));try{await api('/api/users',{method:'POST',body:JSON.stringify(data)});closeModal();toast('账号已创建');views.settings()}catch(error){toast(error.message)}})}
function openResetPassword(userId){openModal('重置临时密码',`<div class="field"><label>新临时密码 *</label><input name="password" type="password" minlength="8" required></div><div class="form-actions"><button type="button" class="secondary" data-close>取消</button><button class="primary">确认重置</button></div>`,async form=>{try{await api(`/api/users/${userId}/reset-password`,{method:'POST',body:JSON.stringify({password:form.elements.password.value})});closeModal();toast('临时密码已重置')}catch(error){toast(error.message)}})}
