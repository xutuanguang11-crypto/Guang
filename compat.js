/* 防止浏览器先加载新界面、后端仍为旧进程时发生字段缺失。 */
const apiBase=api;
api=async function(path,options={}){
  const data=await apiBase(path,options);
  if(path==='/api/projects' && Array.isArray(data.items)){
    data.items=data.items.filter(project=>project.source_lead_id!==undefined&&project.source_lead_id!==null);
  }
  if(/^\/api\/projects\/\d+\/detail$/.test(path)){
    data.milestones=Array.isArray(data.milestones)?data.milestones:[];
    data.payment_nodes=Array.isArray(data.payment_nodes)?data.payment_nodes:[];
    data.contract_changes=Array.isArray(data.contract_changes)?data.contract_changes:[];
  }
  return data;
};
render('dashboard');
