import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const headers = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "apikey, authorization, content-type",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Max-Age": "86400",
  "Content-Type": "application/json; charset=utf-8",
};
const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers });
type AppRole = "owner" | "project_manager" | "finance" | "business";
type UserContext = { id: string; email: string; displayName: string; role: AppRole; projectIds: number[] };
const roleLabels: Record<AppRole, string> = { owner: "老板", project_manager: "项目经理", finance: "财务", business: "业务" };
const loadUserContext = async (db: ReturnType<typeof createClient>, token: string): Promise<UserContext | null> => {
  const { data, error } = await db.auth.getUser(token);
  if (error || !data.user) return null;
  const [profileResult, roleResult, projectsResult] = await Promise.all([
    db.from("profiles").select("display_name,email,active").eq("user_id", data.user.id).maybeSingle(),
    db.from("user_roles").select("role").eq("user_id", data.user.id).maybeSingle(),
    db.from("project_members").select("project_id").eq("user_id", data.user.id),
  ]);
  const profile = profileResult.data;
  const role = roleResult.data?.role as AppRole | undefined;
  if (profileResult.error || roleResult.error || projectsResult.error || !profile?.active || !role || !roleLabels[role]) return null;
  return { id: data.user.id, email: profile.email || data.user.email || "", displayName: profile.display_name, role, projectIds: (projectsResult.data ?? []).map((item: any) => Number(item.project_id)) };
};
const routeModule = (path: string) => {
  if (/^\/api\/users(?:\/|$)/.test(path)) return "users";
  if (/^\/api\/(leads|lead-notes)(?:\/|$)/.test(path)) return "leads";
  if (/^\/api\/(settings|activity-logs)(?:\/|$)/.test(path)) return "settings";
  if (/^\/api\/(material-catalog|labor-rates)(?:\/|$)/.test(path)) return "catalogs";
  if (/^\/api\/work-orders(?:\/|$)/.test(path)) return "workorders";
  if (/^\/api\/(labor-reports|labor-report-batches)(?:\/|$)/.test(path)) return "labor";
  if (/^\/api\/(bom-items|bom-cost-items|equipment-costs|invoices|project-labor-rates|labor-price-options)(?:\/|$)/.test(path)) return "bom";
  if (/^\/api\/(contracts|contract-changes|payment-nodes|receipts|subcontracts|sub-payment-nodes|invoice-ledger|invoice-monthly-summary|ledger|profit|expense-payments|project-finance-summary)(?:\/|$)/.test(path)) return "finance";
  if (/^\/api\/(projects|alerts|milestones|project-files|download-file)(?:\/|$)/.test(path)) return "projects";
  return "unknown";
};
const canRequest = (user: UserContext, path: string, method: string) => {
  if (user.role === "owner" || path === "/api/me") return true;
  const module = routeModule(path);
  if (user.role === "business") return module === "leads" || (/^\/api\/projects(?:\/\d+\/detail)?$/.test(path) && method === "GET");
  if (user.role === "project_manager") {
    if (module === "projects") {
      if (/^\/api\/projects(?:\/|$)/.test(path)) return method === "GET" || method === "PATCH";
      return true;
    }
    if (["workorders","labor","bom"].includes(module)) return true;
    return ["finance","catalogs"].includes(module) && method === "GET";
  }
  if (user.role === "finance") {
    if (module === "finance") return true;
    if (["projects","labor","catalogs"].includes(module)) return method === "GET";
    if (module === "bom") return method === "GET" || /^\/api\/(bom-cost-items|equipment-costs|invoices)(?:\/|$)/.test(path);
  }
  return false;
};
const now = () => new Date().toISOString().slice(0, 19);
const number = (value: unknown, fallback = 0) => {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
};
const text = (value: unknown, fallback = "") => String(value ?? fallback).trim();
const selectFields = (body: Record<string, unknown>, names: string[]) =>
  Object.fromEntries(names.filter((name) => body[name] !== undefined).map((name) => [name, body[name] === "" ? null : body[name]]));

async function tableItems(db: ReturnType<typeof createClient>, table: string, url: URL) {
  let query = db.from(table).select("*").order("id", { ascending: false });
  for (const key of ["project_id", "status"]) {
    const value = url.searchParams.get(key);
    if (value) query = query.eq(key, value);
  }
  const { data, error } = await query;
  if (error) throw error;
  return data ?? [];
}

async function contractDynamicAmount(db: ReturnType<typeof createClient>, contract: Record<string, any> | null) {
  if (!contract) return 0;
  const { data, error } = await db.from("contract_changes").select("change_type,amount,status").eq("contract_id", contract.id);
  if (error) throw error;
  return (data ?? []).filter((item) => !item.status || item.status === "已生效").reduce(
    (total, item) => total + (item.change_type === "减项" ? -number(item.amount) : number(item.amount)),
    number(contract.amount),
  );
}
const requiredQuery = (url: URL, name: string) => url.searchParams.get(name);
const withProject = (row: any) => ({
  ...row,
  project_name: row.projects?.name ?? null,
  project_code: row.projects?.code ?? null,
  project_manager: row.projects?.manager ?? null,
  sales_owner: row.projects?.sales_owner ?? null,
  projects: undefined,
});

async function handleUserAdmin(path: string, method: string, body: Record<string, any>, currentUser: UserContext) {
  if (!path.startsWith("/api/users")) return null;
  if (!serviceRoleKey) return reply({ error: "用户管理服务未配置" }, 503);
  const admin = createClient(supabaseUrl, serviceRoleKey, { auth: { persistSession: false, autoRefreshToken: false } });
  if (path === "/api/users" && method === "GET") {
    const [{ data: authData, error: authError }, profilesResult, rolesResult, membersResult] = await Promise.all([
      admin.auth.admin.listUsers({ page: 1, perPage: 1000 }), admin.from("profiles").select("user_id,email,display_name,active,created_at"),
      admin.from("user_roles").select("user_id,role"), admin.from("project_members").select("user_id,project_id"),
    ]);
    if (authError || profilesResult.error || rolesResult.error || membersResult.error) throw authError || profilesResult.error || rolesResult.error || membersResult.error;
    const profiles = new Map<string, any>((profilesResult.data ?? []).map((item: any) => [item.user_id, item]));
    const roles = new Map<string, AppRole>((rolesResult.data ?? []).map((item: any) => [item.user_id, item.role]));
    const projectIds = new Map<string, number[]>();
    for (const item of membersResult.data ?? []) { const values = projectIds.get(item.user_id) ?? []; values.push(Number(item.project_id)); projectIds.set(item.user_id, values); }
    return reply({ items: (authData.users ?? []).map((authUser: any) => ({
      id: authUser.id, email: authUser.email, display_name: profiles.get(authUser.id)?.display_name ?? authUser.email?.split("@")[0] ?? "未命名",
      active: profiles.get(authUser.id)?.active === true, role: roles.get(authUser.id) ?? null,
      role_label: roles.get(authUser.id) ? roleLabels[roles.get(authUser.id)!] : "未分配", project_ids: projectIds.get(authUser.id) ?? [],
      created_at: authUser.created_at, last_sign_in_at: authUser.last_sign_in_at,
    })) });
  }
  if (path === "/api/users" && method === "POST") {
    const email = text(body.email).toLowerCase(), password = text(body.password), displayName = text(body.display_name), role = text(body.role) as AppRole;
    if (!email || !displayName || password.length < 8 || !roleLabels[role]) return reply({ error: "请填写邮箱、姓名、至少 8 位临时密码和有效角色" }, 400);
    const { data: created, error: createError } = await admin.auth.admin.createUser({ email, password, email_confirm: true, user_metadata: { display_name: displayName } });
    if (createError || !created.user) throw createError || new Error("账号创建失败");
    const userId = created.user.id;
    const profileResult = await admin.from("profiles").upsert({ user_id: userId, email, display_name: displayName, active: true });
    const roleResult = await admin.from("user_roles").upsert({ user_id: userId, role });
    if (profileResult.error || roleResult.error) { await admin.auth.admin.deleteUser(userId); throw profileResult.error || roleResult.error; }
    const projectIds = role === "project_manager" && Array.isArray(body.project_ids) ? body.project_ids.map(Number).filter(Number.isFinite) : [];
    if (projectIds.length) { const result = await admin.from("project_members").insert(projectIds.map((projectId: number) => ({ project_id: projectId, user_id: userId }))); if (result.error) throw result.error; }
    return reply({ id: userId }, 201);
  }
  let match = path.match(/^\/api\/users\/([0-9a-f-]+)$/i);
  if (match && method === "PATCH") {
    const userId = match[1];
    if (userId === currentUser.id && (body.active === false || (body.role && body.role !== "owner"))) return reply({ error: "不能停用或降级当前老板账号" }, 400);
    const updates: Record<string, unknown> = { updated_at: new Date().toISOString() };
    if (body.display_name !== undefined) updates.display_name = text(body.display_name);
    if (body.active !== undefined) updates.active = Boolean(body.active);
    const role = body.role === undefined ? null : text(body.role) as AppRole;
    if (role && !roleLabels[role]) return reply({ error: "角色无效" }, 400);
    const profileResult = await admin.from("profiles").update(updates).eq("user_id", userId); if (profileResult.error) throw profileResult.error;
    if (role) { const roleResult = await admin.from("user_roles").upsert({ user_id: userId, role, updated_at: new Date().toISOString() }); if (roleResult.error) throw roleResult.error; }
    if (body.active !== undefined) { const { error } = await admin.auth.admin.updateUserById(userId, { ban_duration: body.active ? "none" : "876000h" }); if (error) throw error; }
    if (Array.isArray(body.project_ids) || (role && role !== "project_manager")) {
      const clearResult = await admin.from("project_members").delete().eq("user_id", userId); if (clearResult.error) throw clearResult.error;
      const projectIds = role === "project_manager" || (!role && Array.isArray(body.project_ids)) ? (body.project_ids ?? []).map(Number).filter(Number.isFinite) : [];
      if (projectIds.length) { const insertResult = await admin.from("project_members").insert(projectIds.map((projectId: number) => ({ project_id: projectId, user_id: userId }))); if (insertResult.error) throw insertResult.error; }
    }
    return reply({ ok: true });
  }
  match = path.match(/^\/api\/users\/([0-9a-f-]+)\/reset-password$/i);
  if (match && method === "POST") {
    const password = text(body.password); if (password.length < 8) return reply({ error: "临时密码至少 8 位" }, 400);
    const { error } = await admin.auth.admin.updateUserById(match[1], { password }); if (error) throw error;
    return reply({ ok: true });
  }
  return reply({ error: "接口不存在" }, 404);
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
  try {
    const url = new URL(request.url);
    const apiIndex = url.pathname.indexOf("/api/");
    const path = apiIndex >= 0 ? url.pathname.slice(apiIndex) : url.pathname;
    const method = request.method;
    const body = method === "GET" || method === "DELETE" ? {} : await request.json().catch(() => ({}));

    if (path === "/api/health" && method === "GET")
      return reply({ ok: true, version: "2026.07.15-rbac-v14" });

    if (path === "/api/auth-config" && method === "GET")
      return reply({ url: supabaseUrl, anonKey });

    const authorization = request.headers.get("Authorization") ?? "";
    if (!authorization.startsWith("Bearer "))
      return reply({ error: "请先登录" }, 401);

    const token = authorization.slice(7);
    const db = createClient(supabaseUrl, anonKey, {
      global: { headers: { Authorization: authorization } },
      auth: { persistSession: false, autoRefreshToken: false },
    });
    const user = await loadUserContext(db, token);
    if (!user) return reply({ error: "登录已失效、账号已停用或尚未分配角色" }, 401);
    if (!canRequest(user, path, method)) return reply({ error: "无权执行此操作" }, 403);
    if (path === "/api/me" && method === "GET")
      return reply({ id: user.id, email: user.email, display_name: user.displayName, role: user.role, role_label: roleLabels[user.role], project_ids: user.projectIds });
    const userAdminResponse = await handleUserAdmin(path, method, body, user);
    if (userAdminResponse) return userAdminResponse;

    if (path === "/api/leads" && method === "GET") {
      const { data, error } = await db.from("leads").select("*").neq("status", "已删除").order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: data ?? [] });
    }
    if (path === "/api/leads" && method === "POST") {
      const row = {
        name: text(body.name), customer: text(body.customer),
        area: body.area === "" ? null : number(body.area), budget: body.budget === "" ? null : number(body.budget),
        owner: text(body.owner, user.displayName), owner_user_id: user.id, source: text(body.source),
        stage: 0, stage_since: now(), status: "进行中", created_at: now(),
      };
      if (!row.name || !row.customer) return reply({ error: "请填写线索名称和客户" }, 400);
      const { data, error } = await db.from("leads").insert(row).select().single();
      if (error) throw error;
      return reply(data, 201);
    }
    let match = path.match(/^\/api\/leads\/(\d+)\/advance$/);
    if (match && method === "PATCH") {
      const id = Number(match[1]);
      const { data: old, error: readError } = await db.from("leads").select("stage").eq("id", id).single();
      if (readError) throw readError;
      const { data, error } = await db.from("leads").update({
        stage: Math.min(4, number(old.stage) + 1), stage_since: now(),
      }).eq("id", id).select().single();
      if (error) throw error;
      return reply(data);
    }

    match = path.match(/^\/api\/leads\/(\d+)\/detail$/);
    if (match && method === "GET") {
      const id = Number(match[1]);
      const { data: lead, error: leadError } = await db.from("leads").select("*").eq("id", id).single();
      if (leadError) {
        if (leadError.code === "PGRST116") return reply({ error: "线索不存在" }, 404);
        throw leadError;
      }
      const { data: notes, error: notesError } = await db.from("lead_notes").select("*").eq("lead_id", id).order("id", { ascending: false });
      if (notesError && notesError.code !== "42501") throw notesError;
      return reply({ lead, notes: notes ?? [], notes_available: !notesError });
    }

    if (path === "/api/projects" && method === "GET") return reply({ items: await tableItems(db, "projects", url) });
    if (path === "/api/alerts" && method === "GET") {
      const { data, error } = await db.from("project_alerts").select("*, projects(name)").neq("status", "已处理").order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, project_name: item.projects?.name ?? "—", projects: undefined })) });
    }
    if (path === "/api/work-orders" && method === "GET") {
      const { data, error } = await db.from("work_orders").select("*, projects(name)").order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, project_name: item.projects?.name ?? "—", projects: undefined })) });
    }
    if (path === "/api/labor-reports" && method === "GET") {
      const { data, error } = await db.from("labor_reports").select("*, projects(name)").order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({
        ...item, project_name: item.projects?.name ?? "—",
        cost: item.pricing_mode === "按天"
          ? number(item.workers) * number(item.work_days) * number(item.daily_rate)
          : item.pricing_mode === "按小时"
            ? number(item.workers) * number(item.hours) * number(item.hourly_rate)
            : number(item.direct_amount),
        projects: undefined,
      })) });
    }

    if (path === "/api/material-catalog" && method === "GET")
      return reply({ items: await tableItems(db, "material_catalog", url) });
    if (path === "/api/material-catalog" && method === "POST") {
      const row: any = selectFields(body, ["material_code","material_name","specification","unit","category","primary_category","subcategory","supplier","note"]);
      Object.assign(row, {
        base_price: number(body.base_price), loss_rate: number(body.loss_rate),
        last_purchase_price: number(body.last_purchase_price), created_at: now(),
      });
      if (!text(row.material_code) || !text(row.material_name) || !text(row.unit))
        return reply({ error: "材料编码、名称和单位不能为空" }, 400);
      const { data, error } = await db.from("material_catalog").insert(row).select().single();
      if (error) throw error;
      return reply(data, 201);
    }
    match = path.match(/^\/api\/material-catalog\/(\d+)\/update$/);
    if (match && method === "PATCH") {
      const row: any = selectFields(body, ["material_code","material_name","specification","unit","category","primary_category","subcategory","supplier","note"]);
      for (const key of ["base_price","loss_rate","last_purchase_price"])
        if (body[key] !== undefined) row[key] = number(body[key]);
      const { data, error } = await db.from("material_catalog").update(row).eq("id", Number(match[1])).select().single();
      if (error) throw error;
      return reply(data);
    }
    match = path.match(/^\/api\/material-catalog\/(\d+)$/);
    if (match && method === "DELETE") {
      const { error } = await db.from("material_catalog").delete().eq("id", Number(match[1]));
      if (error) throw error;
      return reply({ ok: true });
    }

    if (path === "/api/labor-rates" && method === "GET")
      return reply({ items: await tableItems(db, "labor_rate_catalog", url) });
    if (path === "/api/labor-rates" && method === "POST") {
      const tradeName = text(body.trade_name || body.trade);
      const skill = text(body.skill_level, "大工");
      const mode = text(body.pricing_mode, "按天");
      if (!tradeName) return reply({ error: "请填写工种" }, 400);
      const trade = mode === "按天" ? `${tradeName}（${skill}）` : `${tradeName}（${skill}-${mode}）`;
      const row = {
        trade, trade_name: tradeName, skill_level: skill, pricing_mode: mode,
        hourly_rate: number(body.hourly_rate), daily_rate: number(body.daily_rate),
        piece_unit: text(body.piece_unit), piece_rate: number(body.piece_rate),
        note: text(body.note), created_at: now(),
      };
      const { data: found } = await db.from("labor_rate_catalog").select("id").eq("trade", trade).maybeSingle();
      const result = found
        ? await db.from("labor_rate_catalog").update(row).eq("id", found.id).select().single()
        : await db.from("labor_rate_catalog").insert(row).select().single();
      if (result.error) throw result.error;
      return reply(result.data, found ? 200 : 201);
    }
    match = path.match(/^\/api\/labor-rates\/(\d+)$/);
    if (match && method === "DELETE") {
      const { error } = await db.from("labor_rate_catalog").delete().eq("id", Number(match[1]));
      if (error) throw error;
      return reply({ ok: true });
    }


    if (path === "/api/settings" && method === "GET") {
      const { data, error } = await db.from("app_settings").select("setting_key,setting_value");
      if (error) throw error;
      return reply(Object.fromEntries((data ?? []).map((item) => [item.setting_key, item.setting_value])));
    }

    if (path === "/api/contracts" && method === "GET") {
      const { data, error } = await db.from("contracts").select("*, projects(name,code)").order("id", { ascending: false });
      if (error) throw error;
      const items = await Promise.all((data ?? []).map(async (row: any) => ({
        ...withProject(row),
        dynamic_amount: await contractDynamicAmount(db, row),
      })));
      return reply({ items });
    }

    match = path.match(/^\/api\/projects\/(\d+)\/detail$/);
    if (match && method === "GET") {
      const projectId = Number(match[1]);
      const [projectResult, contractResult, milestonesResult, ordersResult] = await Promise.all([
        db.from("projects").select("*").eq("id", projectId).maybeSingle(),
        db.from("contracts").select("*").eq("project_id", projectId).maybeSingle(),
        db.from("milestones").select("*").eq("project_id", projectId).order("planned_date"),
        db.from("work_orders").select("id", { count: "exact", head: true }).eq("project_id", projectId).not("status", "in", '("已销账","已完成")'),
      ]);
      if (projectResult.error) throw projectResult.error;
      if (!projectResult.data) return reply({ error: "项目不存在" }, 404);
      if (contractResult.error) throw contractResult.error;
      if (milestonesResult.error) throw milestonesResult.error;
      const contract = contractResult.data;
      const dynamicAmount = await contractDynamicAmount(db, contract);
      let nodes: any[] = [];
      let changes: any[] = [];
      if (contract) {
        const [nodeResult, changeResult] = await Promise.all([
          db.from("payment_nodes").select("*, receipts(amount)").eq("contract_id", contract.id).order("planned_date"),
          db.from("contract_changes").select("*").eq("contract_id", contract.id).order("id", { ascending: false }),
        ]);
        if (nodeResult.error) throw nodeResult.error;
        if (changeResult.error) throw changeResult.error;
        nodes = (nodeResult.data ?? []).map((node: any) => {
          const received = (node.receipts ?? []).reduce((sum: number, receipt: any) => sum + number(receipt.amount), 0);
          return { ...node, receipts: undefined, received_amount: received, effective_amount: node.ratio == null ? number(node.amount) : dynamicAmount * number(node.ratio) / 100 };
        });
        changes = changeResult.data ?? [];
      }
      return reply({
        project: projectResult.data,
        contract: contract ? { ...contract, dynamic_amount: dynamicAmount } : null,
        milestones: milestonesResult.data ?? [],
        payment_nodes: nodes,
        contract_changes: changes,
        open_work_orders: ordersResult.count ?? 0,
      });
    }

    if (path === "/api/bom-items" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("bom_items").select("*").eq("project_id", projectId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => {
        const purchase = number(item.design_quantity) * (1 + number(item.loss_rate) / 100);
        return { ...item, purchase_quantity: Math.round(purchase * 100) / 100, subtotal: Math.round(purchase * number(item.unit_price) * 100) / 100 };
      }) });
    }

    if (path === "/api/bom-cost-items" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("bom_cost_items").select("*").eq("project_id", projectId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, calculated_amount: item.cost_type === "人工费" && item.pricing_mode === "按天" ? number(item.workers) * number(item.work_days) * number(item.daily_rate) : number(item.amount) })) });
    }

    if (path === "/api/invoices" && method === "GET") {
      let query = db.from("cost_invoices").select("*, bom_items(material_name)").order("id", { ascending: false });
      const projectId = requiredQuery(url, "project_id");
      if (projectId) query = query.eq("project_id", projectId);
      const { data, error } = await query;
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, material_name: item.bom_items?.material_name ?? null, bom_items: undefined })) });
    }

    if (path === "/api/equipment-costs" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("equipment_costs").select("*").eq("project_id", projectId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: data ?? [] });
    }

    if (path === "/api/project-files" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("project_files").select("*").eq("project_id", projectId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: data ?? [] });
    }

    if (path === "/api/project-labor-rates" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("project_labor_rates").select("*").eq("project_id", projectId).order("id");
      if (error) throw error;
      return reply({ items: data ?? [] });
    }

    if (path === "/api/labor-price-options" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      const [globalResult, projectResult] = await Promise.all([
        db.from("labor_rate_catalog").select("*").order("trade"),
        projectId ? db.from("project_labor_rates").select("*").eq("project_id", projectId) : Promise.resolve({ data: [], error: null }),
      ]);
      if (globalResult.error) throw globalResult.error;
      if (projectResult.error) throw projectResult.error;
      const projectRates = new Map((projectResult.data ?? []).map((item: any) => [item.trade + "|" + item.skill_level, item]));
      const items = (globalResult.data ?? []).map((item: any) => {
        const trade = item.trade_name || item.trade;
        const skill = item.skill_level || "大工";
        const override: any = projectRates.get(trade + "|" + skill);
        return { ...item, trade_name: trade, skill_level: skill, daily_rate: override?.daily_rate ?? item.daily_rate, source: override ? "项目专属价" : "全局标准价" };
      });
      return reply({ items });
    }

    if (path === "/api/bom-labor-summary" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const [budgetResult, actualResult] = await Promise.all([
        db.from("bom_cost_items").select("*").eq("project_id", projectId).eq("cost_type", "人工费"),
        db.from("labor_reports").select("*").eq("project_id", projectId).eq("status", "已审核"),
      ]);
      if (budgetResult.error) throw budgetResult.error;
      if (actualResult.error) throw actualResult.error;
      const budget = (budgetResult.data ?? []).reduce((sum: number, item: any) => sum + (item.pricing_mode === "按天" ? number(item.workers) * number(item.work_days) * number(item.daily_rate) : number(item.amount)), 0);
      const actual = (actualResult.data ?? []).reduce((sum: number, item: any) => sum + (item.pricing_mode === "直接金额" ? number(item.direct_amount) : number(item.workers) * number(item.work_days) * number(item.daily_rate)), 0);
      return reply({ budget, actual, variance: actual - budget, details: actualResult.data ?? [] });
    }

    if (path === "/api/expense-payments" && method === "GET") {
      const sourceType = requiredQuery(url, "source_type");
      const sourceId = requiredQuery(url, "source_id");
      if (!sourceType || !sourceId) return reply({ error: "请选择费用明细" }, 400);
      const { data, error } = await db.from("expense_payments").select("*").eq("source_type", sourceType).eq("source_id", sourceId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: data ?? [] });
    }

    if (path === "/api/invoice-ledger" && method === "GET") {
      let query = db.from("invoice_ledger").select("*").order("invoice_date", { ascending: false });
      for (const key of ["project_id", "direction", "source_type", "source_id"]) {
        const value = requiredQuery(url, key);
        if (value) query = query.eq(key, value);
      }
      const month = requiredQuery(url, "month");
      if (month) query = query.gte("invoice_date", month + "-01").lte("invoice_date", month + "-31");
      const { data, error } = await query;
      if (error) throw error;
      const projectIds = [...new Set((data ?? []).map((item: any) => item.project_id).filter(Boolean))];
      const nodeIds = [...new Set((data ?? []).map((item: any) => item.payment_node_id).filter(Boolean))];
      const [projectResult, nodeResult] = await Promise.all([
        projectIds.length ? db.from("projects").select("id,name").in("id", projectIds) : Promise.resolve({ data: [], error: null }),
        nodeIds.length ? db.from("payment_nodes").select("id,name,planned_date,amount").in("id", nodeIds) : Promise.resolve({ data: [], error: null }),
      ]);
      if (projectResult.error) throw projectResult.error;
      if (nodeResult.error) throw nodeResult.error;
      const projects = new Map((projectResult.data ?? []).map((item: any) => [item.id, item]));
      const nodes = new Map((nodeResult.data ?? []).map((item: any) => [item.id, item]));
      return reply({ items: (data ?? []).map((item: any) => {
        const node: any = nodes.get(item.payment_node_id);
        return {
          ...item,
          project_name: (projects.get(item.project_id) as any)?.name ?? null,
          payment_node_name: node?.name ?? null,
          payment_due_date: node?.planned_date ?? null,
          payment_node_amount: node?.amount ?? null,
        };
      }) });
    }

    if (path === "/api/invoice-monthly-summary" && method === "GET") {
      const { data, error } = await db.from("invoice_ledger").select("direction,invoice_date,total_amount,tax_amount");
      if (error) throw error;
      const grouped = new Map<string, any>();
      for (const item of data ?? []) {
        const month = String(item.invoice_date ?? "").slice(0, 7);
        if (!month) continue;
        const row = grouped.get(month) ?? { month, inbound_amount: 0, outbound_amount: 0, inbound_tax: 0, outbound_tax: 0 };
        const inbound = item.direction === "进项";
        row[inbound ? "inbound_amount" : "outbound_amount"] += number(item.total_amount);
        row[inbound ? "inbound_tax" : "outbound_tax"] += number(item.tax_amount);
        grouped.set(month, row);
      }
      return reply({ items: [...grouped.values()].sort((a, b) => b.month.localeCompare(a.month)) });
    }

    if (path === "/api/subcontracts" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data, error } = await db.from("subcontracts").select("*, sub_payment_nodes(amount)").eq("project_id", projectId).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, planned_payment: (item.sub_payment_nodes ?? []).reduce((sum: number, node: any) => sum + number(node.amount), 0), sub_payment_nodes: undefined })) });
    }

    if (path === "/api/sub-payment-nodes" && method === "GET") {
      const id = requiredQuery(url, "subcontract_id");
      if (!id) return reply({ error: "请选择分包合同" }, 400);
      const { data, error } = await db.from("sub_payment_nodes").select("*").eq("subcontract_id", id).order("planned_date");
      if (error) throw error;
      return reply({ items: data ?? [] });
    }

    if (path === "/api/payment-nodes" && method === "GET") {
      let query = db.from("payment_nodes").select("*, contracts(code,project_id,amount), receipts(amount)").order("planned_date");
      const projectId = requiredQuery(url, "project_id");
      const { data, error } = await query;
      if (error) throw error;
      let items: any[] = [];
      for (const node of data ?? []) {
        const contract: any = node.contracts;
        if (projectId && String(contract?.project_id) !== String(projectId)) continue;
        const dynamic = await contractDynamicAmount(db, contract);
        items.push({ ...node, contract_code: contract?.code ?? null, received_amount: (node.receipts ?? []).reduce((sum: number, row: any) => sum + number(row.amount), 0), effective_amount: node.ratio == null ? number(node.amount) : dynamic * number(node.ratio) / 100, contracts: undefined, receipts: undefined });
      }
      return reply({ items });
    }

    if (path === "/api/contract-changes" && method === "GET") {
      const projectId = requiredQuery(url, "project_id");
      if (!projectId) return reply({ error: "请选择项目" }, 400);
      const { data: contracts, error: contractError } = await db.from("contracts").select("id,code").eq("project_id", projectId);
      if (contractError) throw contractError;
      const ids = (contracts ?? []).map((item: any) => item.id);
      if (!ids.length) return reply({ items: [] });
      const codes = new Map((contracts ?? []).map((item: any) => [item.id, item.code]));
      const { data, error } = await db.from("contract_changes").select("*").in("contract_id", ids).order("id", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({ ...item, contract_code: codes.get(item.contract_id) })) });
    }

    if (path === "/api/ledger" && method === "GET") {
      const { data, error } = await db.from("receipts").select("*, payment_nodes(name,contracts(projects(code,name)))").order("received_date", { ascending: false });
      if (error) throw error;
      return reply({ items: (data ?? []).map((item: any) => ({
        receipt_id: item.id,
        month: String(item.received_date ?? "").slice(0, 7),
        project_code: item.payment_nodes?.contracts?.projects?.code ?? null,
        project_name: item.payment_nodes?.contracts?.projects?.name ?? null,
        node_name: item.payment_nodes?.name ?? null,
        received_date: item.received_date,
        amount: item.amount,
        voucher_name: item.voucher_name,
      })) });
    }

    if (path === "/api/project-finance-summary" && method === "GET") {
      const projectId = Number(requiredQuery(url, "project_id") || 0);
      const [contractResult, paymentsResult, laborResult, invoicesResult, ledgerResult] = await Promise.all([
        db.from("contracts").select("*").eq("project_id", projectId).maybeSingle(),
        db.from("expense_payments").select("amount").eq("project_id", projectId),
        db.from("labor_reports").select("*").eq("project_id", projectId).eq("status", "已审核"),
        db.from("invoice_ledger").select("direction,total_amount").eq("project_id", projectId),
        db.from("payment_nodes").select("*, contracts!inner(project_id), receipts(amount)").eq("contracts.project_id", projectId),
      ]);
      for (const result of [contractResult, paymentsResult, laborResult, invoicesResult, ledgerResult]) if (result.error) throw result.error;
      const dynamic = await contractDynamicAmount(db, contractResult.data);
      const paid = (paymentsResult.data ?? []).reduce((sum: number, item: any) => sum + number(item.amount), 0);
      const labor = (laborResult.data ?? []).reduce((sum: number, item: any) => sum + (item.pricing_mode === "直接金额" ? number(item.direct_amount) : number(item.workers) * number(item.work_days) * number(item.daily_rate)), 0);
      const inbound = (invoicesResult.data ?? []).filter((item: any) => item.direction === "进项").reduce((sum: number, item: any) => sum + number(item.total_amount), 0);
      const outbound = (invoicesResult.data ?? []).filter((item: any) => item.direction === "销项").reduce((sum: number, item: any) => sum + number(item.total_amount), 0);
      const received = (ledgerResult.data ?? []).flatMap((item: any) => item.receipts ?? []).reduce((sum: number, item: any) => sum + number(item.amount), 0);
      const due = (ledgerResult.data ?? []).reduce((sum: number, item: any) => sum + (item.ratio == null ? number(item.amount) : dynamic * number(item.ratio) / 100), 0);
      return reply({ dynamic_income: dynamic, received_amount: received, paid_amount: paid, labor_actual: labor, management_cost: inbound + labor, management_profit: dynamic - inbound - labor, inbound_invoice: inbound, due_outbound_invoice: due, outbound_invoice: outbound, unissued_invoice: Math.max(due - outbound, 0), actual_profit: dynamic - inbound });
    }

    if (path === "/api/profit" && method === "GET") {
      const { data: projects, error } = await db.from("projects").select("id,code,name").order("id");
      if (error) throw error;
      const items = [];
      for (const project of projects ?? []) {
        const [contractResult, costsResult, receiptsResult] = await Promise.all([
          db.from("contracts").select("*").eq("project_id", project.id).maybeSingle(),
          db.from("invoice_ledger").select("direction,total_amount").eq("project_id", project.id),
          db.from("payment_nodes").select("receipts(amount), contracts!inner(project_id)").eq("contracts.project_id", project.id),
        ]);
        for (const result of [contractResult, costsResult, receiptsResult]) if (result.error) throw result.error;
        const income = await contractDynamicAmount(db, contractResult.data);
        const actual = (costsResult.data ?? []).filter((item: any) => item.direction === "进项").reduce((sum: number, item: any) => sum + number(item.total_amount), 0);
        const received = (receiptsResult.data ?? []).flatMap((item: any) => item.receipts ?? []).reduce((sum: number, item: any) => sum + number(item.amount), 0);
        const gross = income - actual;
        items.push({ ...project, income, material_budget: 0, labor: 0, actual_invoices: actual, bom_cost_actual: 0, equipment: 0, subcontract: 0, actual_cost: actual, received, gross_profit: gross, gross_margin: income ? gross / income * 100 : null });
      }
      return reply({ items });
    }

    if (path === "/api/dashboard" && method === "GET") {
      const [leadsResult, projectsResult, ordersResult] = await Promise.all([
        db.from("leads").select("id", { count: "exact", head: true }).eq("status", "进行中"),
        db.from("projects").select("id", { count: "exact", head: true }).neq("status", "已完成"),
        db.from("work_orders").select("id", { count: "exact", head: true }).not("status", "in", '("已销账","已完成")'),
      ]);
      return reply({ active_leads: leadsResult.count ?? 0, active_projects: projectsResult.count ?? 0, open_work_orders: ordersResult.count ?? 0 });
    }

    if (/^\/api\/(download-contract|download-invoice|download-file|work-order-photo)\//.test(path) && method === "GET")
      return reply({ error: "附件尚未迁移到对象存储" }, 404);


    const insertDefinitions: Record<string, { table: string; required: string[]; fields: string[]; defaults?: Record<string, unknown> }> = {
      "/api/lead-notes": { table: "lead_notes", required: ["lead_id","content"], fields: ["lead_id","content"], defaults: { created_at: now() } },
      "/api/expense-payments": { table: "expense_payments", required: ["project_id","source_type","source_id","payment_date","amount"], fields: ["project_id","source_type","source_id","payment_date","amount","voucher_name","note"], defaults: { created_at: now() } },
      "/api/bom-cost-items": { table: "bom_cost_items", required: ["project_id","cost_type","name"], fields: ["project_id","contract_change_id","cost_type","name","pricing_mode","workers","work_days","daily_rate","amount","cost_date","supplier","invoice_name","note"], defaults: { created_at: now() } },
      "/api/project-alerts": { table: "project_alerts", required: ["project_id","message"], fields: ["project_id","alert_type","message","status"], defaults: { alert_type: "回款提醒", status: "待处理", created_at: now() } },
      "/api/project-labor-rates": { table: "project_labor_rates", required: ["project_id","trade","skill_level","daily_rate"], fields: ["project_id","trade","skill_level","daily_rate"], defaults: { updated_at: now() } },
      "/api/labor-reports": { table: "labor_reports", required: ["project_id","report_date","trade"], fields: ["project_id","report_date","trade","workers","hours","hourly_rate","contract_change_id","pricing_mode","work_days","daily_rate","direct_amount","skill_level","team_name","leader","reporter"], defaults: { status: "待审核", created_at: now(), pricing_mode: "按天" } },
      "/api/equipment-costs": { table: "equipment_costs", required: ["project_id","name","amount","cost_date"], fields: ["project_id","name","amount","cost_date","invoice_name","contract_change_id"], defaults: { created_at: now() } },
      "/api/sub-payment-nodes": { table: "sub_payment_nodes", required: ["subcontract_id","name","amount"], fields: ["subcontract_id","name","amount","planned_date"], defaults: { status: "未付款" } },
      "/api/milestones": { table: "milestones", required: ["project_id","name","planned_date"], fields: ["project_id","name","planned_date"], defaults: { progress: 0, status: "未开始" } },
      "/api/bom-items": { table: "bom_items", required: ["project_id","material_code","material_name","unit","design_quantity","unit_price"], fields: ["project_id","material_code","material_name","specification","unit","design_quantity","loss_rate","unit_price","contract_change_id"], defaults: { created_at: now(), loss_rate: 0 } },
      "/api/invoices": { table: "cost_invoices", required: ["project_id","invoice_type","invoice_number","amount"], fields: ["project_id","bom_item_id","invoice_type","invoice_number","amount","file_name","contract_change_id"], defaults: { created_at: now() } },
      "/api/project-files": { table: "project_files", required: ["project_id","file_type","file_name"], fields: ["project_id","file_type","file_name","storage_name"], defaults: { created_at: now() } },
    };

    if (method === "POST" && insertDefinitions[path]) {
      const definition = insertDefinitions[path];
      const missing = definition.required.filter((name) => body[name] === undefined || body[name] === null || String(body[name]).trim() === "");
      if (missing.length) return reply({ error: "缺少必填字段：" + missing.join("、") }, 400);
      const row = { ...(definition.defaults ?? {}), ...selectFields(body, definition.fields) } as Record<string, unknown>;
      if (path === "/api/bom-cost-items" && row.cost_type === "人工费" && row.pricing_mode === "按天")
        row.amount = number(row.workers) * number(row.work_days) * number(row.daily_rate);
      if (path === "/api/project-files") {
        row.storage_name = row.file_name;
        delete row.file_data;
      }
      if (path === "/api/project-labor-rates") {
        const { data, error } = await db.from(definition.table).upsert(row, { onConflict: "project_id,trade,skill_level" }).select().single();
        if (error) throw error;
        return reply({ ok: true, item: data }, 201);
      }
      const { data, error } = await db.from(definition.table).insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id, item: data, attachment_warning: path === "/api/project-files" ? "文件内容尚未接入对象存储" : undefined }, 201);
    }

    if (path === "/api/settings" && method === "POST") {
      const rows = Object.entries(body).map(([setting_key, setting_value]) => ({ setting_key, setting_value: String(setting_value ?? ""), updated_at: now() }));
      const { error } = await db.from("app_settings").upsert(rows, { onConflict: "setting_key" });
      if (error) throw error;
      return reply({ ok: true });
    }

    if (path === "/api/invoice-ledger" && method === "POST") {
      const required = ["direction","invoice_type","invoice_number","invoice_date","counterparty","total_amount"];
      const missing = required.filter((name) => body[name] === undefined || String(body[name]).trim() === "");
      if (missing.length) return reply({ error: "缺少必填字段：" + missing.join("、") }, 400);
      const totalAmount = number(body.total_amount);
      const taxRate = number(body.tax_rate);
      const row = {
        ...selectFields(body, ["direction","project_id","payment_node_id","source_type","source_id","invoice_type","invoice_number","invoice_date","counterparty","tax_rate","tax_amount","total_amount","file_name","note"]),
        tax_rate: taxRate,
        total_amount: totalAmount,
        tax_amount: body.tax_amount === undefined || body.tax_amount === "" ? (taxRate ? totalAmount * taxRate / (100 + taxRate) : 0) : number(body.tax_amount),
        certification_status: text(body.certification_status, "未认证"),
        deduction_status: text(body.deduction_status, "未申报"),
        booked_status: text(body.booked_status, "未入账"),
        created_at: now(),
      };
      const { data, error } = await db.from("invoice_ledger").insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id, item: data }, 201);
    }

    if (path === "/api/contracts" && method === "POST") {
      for (const name of ["project_id","party_a","amount"]) if (!body[name]) return reply({ error: "项目、甲方和合同金额为必填项" }, 400);
      const code = "HT-" + new Date().toISOString().slice(2,7).replace("-","") + "-" + String(Date.now()).slice(-4);
      const row = { code, ...selectFields(body, ["project_id","party_a","amount","signed_date","completion_date","status","file_name","warranty_rate","warranty_months"]), status: text(body.status, "已签署"), warranty_rate: number(body.warranty_rate, 5), warranty_months: number(body.warranty_months, 12), created_at: now() };
      const { data, error } = await db.from("contracts").insert(row).select().single();
      if (error) throw error;
      await db.from("projects").update({ contract_amount: number(body.amount), status: "待排施工计划" }).eq("id", body.project_id);
      return reply({ id: data.id, code }, 201);
    }

    if (path === "/api/payment-nodes" && method === "POST") {
      for (const name of ["contract_id","name","ratio"]) if (!body[name]) return reply({ error: "合同、节点名称和付款比例为必填项" }, 400);
      const ratio = number(body.ratio);
      const { data: existing, error: readError } = await db.from("payment_nodes").select("ratio").eq("contract_id", body.contract_id);
      if (readError) throw readError;
      const used = (existing ?? []).reduce((sum: number, item: any) => sum + number(item.ratio), 0);
      if (ratio <= 0 || used + ratio > 100.0001) return reply({ error: "付款节点比例合计必须大于 0 且不超过 100%" }, 400);
      const { data: contract, error: contractError } = await db.from("contracts").select("*").eq("id", body.contract_id).single();
      if (contractError) throw contractError;
      const dynamic = await contractDynamicAmount(db, contract);
      const row = { contract_id: body.contract_id, name: body.name, ratio, amount: dynamic * ratio / 100, planned_date: body.planned_date || null, trigger_milestone_id: body.trigger_milestone_id || null, status: "未触发" };
      const { data, error } = await db.from("payment_nodes").insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id }, 201);
    }

    if (path === "/api/receipts" && method === "POST") {
      for (const name of ["payment_node_id","received_date","amount"]) if (!body[name]) return reply({ error: "付款节点、收款日期和金额为必填项" }, 400);
      const row = { ...selectFields(body, ["payment_node_id","received_date","amount","voucher_name"]), created_at: now() };
      const { data, error } = await db.from("receipts").insert(row).select().single();
      if (error) throw error;
      const { data: receipts } = await db.from("receipts").select("amount").eq("payment_node_id", body.payment_node_id);
      const { data: node } = await db.from("payment_nodes").select("*").eq("id", body.payment_node_id).single();
      const received = (receipts ?? []).reduce((sum: number, item: any) => sum + number(item.amount), 0);
      if (node && received >= number(node.amount)) await db.from("payment_nodes").update({ status: "已收款" }).eq("id", node.id);
      return reply({ id: data.id }, 201);
    }

    if (path === "/api/subcontracts" && method === "POST") {
      for (const name of ["project_id","vendor","scope","amount"]) if (!body[name]) return reply({ error: "项目、分包方、分包内容和金额为必填项" }, 400);
      const code = "FB-" + new Date().toISOString().slice(2,7).replace("-","") + "-" + String(Date.now()).slice(-4);
      const row = { code, ...selectFields(body, ["project_id","vendor","scope","amount","signed_date","completion_date","warranty_rate","warranty_months","file_name"]), status: "已签署", warranty_rate: number(body.warranty_rate, 5), warranty_months: number(body.warranty_months, 12), created_at: now() };
      const { data, error } = await db.from("subcontracts").insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id, code }, 201);
    }

    if (path === "/api/contract-changes" && method === "POST") {
      for (const name of ["contract_id","title","change_type","amount"]) if (!body[name]) return reply({ error: "主合同、变更名称、类型和金额为必填项" }, 400);
      const code = "BG-" + new Date().toISOString().slice(2,7).replace("-","") + "-" + String(Date.now()).slice(-4);
      const row = { code, ...selectFields(body, ["contract_id","title","change_type","amount","signed_date","file_name"]), status: "已生效", created_at: now() };
      const { data, error } = await db.from("contract_changes").insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id, code }, 201);
    }

    if (path === "/api/work-orders" && method === "POST") {
      for (const name of ["project_id","description","work_type","assignee"]) if (!body[name]) return reply({ error: "项目、问题描述、类型和责任人为必填项" }, 400);
      const code = "WO-" + new Date().toISOString().slice(2,7).replace("-","") + "-" + String(Date.now()).slice(-4);
      const row = { code, ...selectFields(body, ["project_id","title","description","work_type","assignee","reporter","due_at","contract_change_id","photo_name"]), title: text(body.title, text(body.description).slice(0,30)), reporter: text(body.reporter, "管理员"), status: "待派单", created_at: now(), updated_at: now() };
      const { data, error } = await db.from("work_orders").insert(row).select().single();
      if (error) throw error;
      return reply({ id: data.id, code }, 201);
    }

    if (path === "/api/labor-report-batches" && method === "POST") {
      const lines = Array.isArray(body.lines) ? body.lines : [];
      if (!body.project_id || !body.report_date || !lines.length) return reply({ error: "请选择项目、日期并至少添加一个工种" }, 400);
      const batchCode = "LR-" + new Date().toISOString().replace(/\D/g, "").slice(2,16);
      const rows = lines.map((line: any) => ({
        project_id: body.project_id, report_date: body.report_date, trade: line.trade,
        workers: number(line.workers), hours: 0, hourly_rate: 0, status: "待审核",
        created_at: now(), pricing_mode: "按天", work_days: number(line.work_days),
        daily_rate: number(line.daily_rate), direct_amount: 0, batch_code: batchCode,
        skill_level: text(line.skill_level, "大工"), team_name: text(body.team_name),
        leader: text(body.leader), reporter: "管理员",
      }));
      const { data, error } = await db.from("labor_reports").insert(rows).select("id");
      if (error) throw error;
      return reply({ batch_code: batchCode, ids: (data ?? []).map((item: any) => item.id), duplicates: [] }, 201);
    }

    let actionMatch = path.match(/^\/api\/([^/]+)\/(\d+)\/([^/]+)$/);
    if (actionMatch && method === "PATCH") {
      const [, entity, rawId, action] = actionMatch;
      const id = Number(rawId);

      if (entity === "leads") {
        const { data: lead, error } = await db.from("leads").select("*").eq("id", id).single();
        if (error) throw error;
        if (action === "back") {
          if (number(lead.stage) <= 0) return reply({ error: "已经是第一个阶段" }, 400);
          await db.from("leads").update({ stage: number(lead.stage) - 1, stage_since: now() }).eq("id", id);
        } else if (action === "lose") {
          if (!body.lost_reason) return reply({ error: "请选择未成交原因" }, 400);
          await db.from("leads").update({ status: "未成交", lost_reason: body.lost_reason, lost_note: body.lost_note || "" }).eq("id", id);
        } else if (action === "restore") {
          await db.from("leads").update({ status: lead.deleted_from_status || "进行中", deleted_at: null, deleted_from_status: null, stage_since: now() }).eq("id", id);
        } else if (action === "win") {
          const code = "XM-" + new Date().toISOString().slice(2,7).replace("-","") + "-" + String(Date.now()).slice(-4);
          const { error: projectError } = await db.from("projects").insert({ code, name: lead.name, sales_owner: lead.owner, sales_owner_user_id: lead.owner_user_id || user.id, manager: text(body.manager, "待分配"), contract_amount: number(lead.budget), progress: 0, status: "待建合同", bom_version: "V0.1", created_at: now(), source_lead_id: id });
          if (projectError) throw projectError;
          await db.from("leads").update({ status: "已赢单" }).eq("id", id);
          return reply({ ok: true, code });
        } else return reply({ error: "接口不存在" }, 404);
        return reply({ ok: true });
      }

      if (entity === "milestones" && action === "progress") {
        const progress = Math.max(0, Math.min(100, number(body.progress)));
        const status = progress === 100 ? "已完成" : progress > 0 ? "进行中" : "未开始";
        const { data, error } = await db.from("milestones").update({ progress, status }).eq("id", id).select().single();
        if (error) throw error;
        const { data: all } = await db.from("milestones").select("progress").eq("project_id", data.project_id);
        const average = (all ?? []).length ? Math.round((all ?? []).reduce((sum: number, item: any) => sum + number(item.progress), 0) / (all ?? []).length) : 0;
        await db.from("projects").update({ progress: average, status: "施工中" }).eq("id", data.project_id);
        return reply({ ok: true });
      }

      const simpleActions: Record<string, { table: string; values: Record<string, unknown> }> = {
        "contracts/acceptance": { table: "contracts", values: { acceptance_date: body.acceptance_date } },
        "sub-payment-nodes/paid": { table: "sub_payment_nodes", values: { status: "已付款" } },
        "work-orders/reject": { table: "work_orders", values: { status: "处理中", reject_reason: body.reject_reason, updated_at: now() } },
        "work-orders/close": { table: "work_orders", values: { status: "已完成", closure_photo: body.closure_photo || "", completed_at: now(), updated_at: now() } },
        "labor-reports/approve": { table: "labor_reports", values: { status: "已审核", reviewer: "管理员", reviewed_at: now(), reject_reason: null, rejected_at: null } },
        "labor-reports/reject": { table: "labor_reports", values: { status: "已驳回", reviewer: "管理员", reject_reason: body.reject_reason, rejected_at: now() } },
        "receipts/voucher": { table: "receipts", values: { voucher_name: body.file_name || body.voucher_name } },
        "invoice-ledger/project": { table: "invoice_ledger", values: { project_id: body.project_id || null } },
      };
      const simple = simpleActions[entity + "/" + action];
      if (simple) {
        const { error } = await db.from(simple.table).update(simple.values).eq("id", id);
        if (error) throw error;
        return reply({ ok: true });
      }

      if (entity === "invoice-ledger" && action === "status") {
        const values = selectFields(body, ["certification_status","deduction_status","booked_status"]) as Record<string, unknown>;
        if (body.certification_status === "已认证") values.certified_date = new Date().toISOString().slice(0,10);
        if (body.booked_status === "已入账") values.booked_date = new Date().toISOString().slice(0,10);
        const { error } = await db.from("invoice_ledger").update(values).eq("id", id);
        if (error) throw error;
        return reply({ ok: true });
      }

      const updateDefinitions: Record<string, { table: string; fields: string[] }> = {
        "labor-reports/update": { table: "labor_reports", fields: ["report_date","trade","skill_level","team_name","leader","workers","pricing_mode","work_days","daily_rate","direct_amount"] },
        "contracts/update": { table: "contracts", fields: ["party_a","amount","signed_date","completion_date","status"] },
        "bom-items/update": { table: "bom_items", fields: ["material_code","material_name","specification","unit","design_quantity","loss_rate","unit_price"] },
        "material-catalog/update": { table: "material_catalog", fields: ["material_code","material_name","specification","unit","base_price","category","primary_category","subcategory","loss_rate","supplier","last_purchase_price","note"] },
        "bom-cost-items/update": { table: "bom_cost_items", fields: ["cost_type","name","pricing_mode","workers","work_days","daily_rate","amount","cost_date","supplier","note"] },
      };
      const definition = updateDefinitions[entity + "/" + action];
      if (definition) {
        const values = selectFields(body, definition.fields);
        if (entity === "labor-reports") Object.assign(values, { status: "待审核", reviewer: null, reviewed_at: null, reject_reason: null, rejected_at: null });
        const { error } = await db.from(definition.table).update(values).eq("id", id);
        if (error) throw error;
        return reply({ ok: true });
      }

      if (entity === "projects" && action === "complete") {
        const { error } = await db.from("projects").update({ progress: 100, status: "已完工" }).eq("id", id);
        if (error) throw error;
        return reply({ ok: true });
      }

      if (entity === "work-orders" && action === "advance") {
        const { data, error } = await db.from("work_orders").select("status").eq("id", id).single();
        if (error) throw error;
        const next = data.status === "待派单" || data.status === "待派发" ? "处理中" : data.status === "处理中" ? "待验收" : null;
        if (!next) return reply({ error: "当前状态不能推进" }, 400);
        const values: Record<string, unknown> = { status: next, updated_at: now() };
        if (body.resolution_note) values.resolution_note = body.resolution_note;
        await db.from("work_orders").update(values).eq("id", id);
        return reply({ ok: true });
      }
    }

    const deleteMatch = path.match(/^\/api\/([^/]+)\/(\d+)$/);
    if (deleteMatch && method === "DELETE") {
      const tableMap: Record<string, string> = {
        "projects": "projects", "contracts": "contracts", "milestones": "milestones",
        "material-catalog": "material_catalog", "labor-rates": "labor_rate_catalog",
        "labor-reports": "labor_reports", "bom-items": "bom_items",
        "bom-cost-items": "bom_cost_items", "project-files": "project_files",
        "lead-notes": "lead_notes", "expense-payments": "expense_payments",
        "invoice-ledger": "invoice_ledger",
      };
      const entity = deleteMatch[1];
      const id = Number(deleteMatch[2]);
      if (entity === "leads") {
        const { data, error } = await db.from("leads").select("status").eq("id", id).single();
        if (error) throw error;
        const { error: updateError } = await db.from("leads").update({ deleted_from_status: data.status, status: "已删除", deleted_at: now() }).eq("id", id);
        if (updateError) throw updateError;
        return reply({ ok: true });
      }
      const table = tableMap[entity];
      if (!table) return reply({ error: "接口不存在" }, 404);
      const { error } = await db.from(table).delete().eq("id", id);
      if (error) throw error;
      return reply({ ok: true });
    }

    const permanentLead = path.match(/^\/api\/leads\/(\d+)\/permanent$/);
    if (permanentLead && method === "DELETE") {
      const id = Number(permanentLead[1]);
      await db.from("lead_notes").delete().eq("lead_id", id);
      const { error } = await db.from("leads").delete().eq("id", id);
      if (error) throw error;
      return reply({ ok: true, deleted_projects: 0 });
    }

    if (path === "/api/open-file" && method === "POST")
      return reply({ error: "网页端不能调用本机软件打开文件，请使用下载接口" }, 400);


    let detailMatch = path.match(/^\/api\/work-orders\/(\d+)\/detail$/);
    if (detailMatch && method === "GET") {
      const id = Number(detailMatch[1]);
      const { data: order, error } = await db.from("work_orders").select("*").eq("id", id).maybeSingle();
      if (error) throw error;
      if (!order) return reply({ error: "工单不存在" }, 404);
      const [projectResult, photosResult, logsResult] = await Promise.all([
        db.from("projects").select("name,manager,sales_owner").eq("id", order.project_id).maybeSingle(),
        db.from("work_order_photos").select("*").eq("work_order_id", id).order("id"),
        db.from("activity_logs").select("*").eq("entity", "work_order").eq("entity_id", id).order("id", { ascending: false }),
      ]);
      if (projectResult.error) throw projectResult.error;
      if (photosResult.error) throw photosResult.error;
      if (logsResult.error) throw logsResult.error;
      return reply({
        work_order: { ...order, project_name: projectResult.data?.name ?? null, project_manager: projectResult.data?.manager ?? null, sales_owner: projectResult.data?.sales_owner ?? null },
        photos: photosResult.data ?? [],
        logs: logsResult.data ?? [],
      });
    }

    detailMatch = path.match(/^\/api\/labor-reports\/(\d+)$/);
    if (detailMatch && method === "GET") {
      const id = Number(detailMatch[1]);
      const { data: report, error } = await db.from("labor_reports").select("*").eq("id", id).maybeSingle();
      if (error) throw error;
      if (!report) return reply({ error: "日报不存在" }, 404);
      const { data: project, error: projectError } = await db.from("projects").select("name").eq("id", report.project_id).maybeSingle();
      if (projectError) throw projectError;
      const cost = report.pricing_mode === "直接金额" ? number(report.direct_amount) : number(report.workers) * number(report.work_days) * number(report.daily_rate);
      return reply({ ...report, project_name: project?.name ?? null, cost });
    }

    return reply({ error: "该功能尚未迁移到测试接口", path, method }, 404);
  } catch (error) {
    console.error(error);
    const message = error instanceof Error ? error.message : JSON.stringify(error);
    const code = typeof error === "object" && error && "code" in error ? String((error as any).code) : "";
    if (code === "42501" || /row-level security|permission denied/i.test(message)) return reply({ error: "无权访问该数据" }, 403);
    return reply({ error: message || "服务器错误" }, 500);
  }
});
