# 工装项目管理系统：Codex 接手说明

## 1. 项目定位

这是一个工装项目全流程管理系统，前端为原生 HTML/CSS/JavaScript。项目最初使用 Python `server.py` + SQLite，本轮已经开始迁移到 Netlify + Supabase。

新任务必须把本项目视为“在现有系统上继续迁移和补齐功能”，不要重写整个项目。

## 2. 唯一有效的源代码目录

必须在以下目录工作：

```text
C:\Users\GuangTou\Documents\Codex\2026-07-12\c-users-guangtou-documents-codex-2026-2\outputs\bom-5-bom-project-bom-5-optimized
```

不要误用以下旧目录作为线上源代码：

```text
C:\Users\GuangTou\Documents\Codex\2026-07-11\bom-5-bom-project-bom-5
```

旧目录下的 `.netlify-preview-20260714` 只是一次干净静态部署目录，不是源代码。

## 3. 当前线上架构

```text
浏览器
  -> Netlify 静态前端
  -> Supabase Edge Function: project-api
  -> Supabase PostgreSQL
```

### Netlify

- 生产网站：https://guang-workshop-manager.netlify.app
- Site ID：`ca9a8f03-95d4-4cee-8c6e-7530b3744d7b`
- 最近验证成功的 Deploy ID：`6a563de6e4a0efd26b787a17`
- 当前是手动 CLI 部署；GitHub 推送不会自动发布 Netlify。
- 部署时只能上传 `.html`、`.js`、`.css` 等静态资源。
- 不要上传 `.git`、`data`、`uploads`、SQLite、Python 缓存、交接文档和 `deploy-*.zip`。

### Supabase

- Project ref：`zrylyjtbjhqffqggphad`
- Project URL：https://zrylyjtbjhqffqggphad.supabase.co
- Edge Function：`project-api`
- API 前缀：

```text
https://zrylyjtbjhqffqggphad.supabase.co/functions/v1/project-api
```

- 已应用迁移：
  - `initial_project_schema`
  - `add_foreign_key_indexes`
  - `public_preview_core_tables`
  - `grant_public_preview_core_privileges`
- 数据库共 26 张 public 表，全部启用了 RLS。
- 不要把 service-role key、访问令牌或其他密钥写进仓库。

### GitHub

- 仓库：https://github.com/xutuanguang11-crypto/Guang.git
- 当前开发分支：`agent/initial-project`
- Supabase 前端连接提交：`c43e4e7`
- 草稿 PR：https://github.com/xutuanguang11-crypto/Guang/pull/2

## 4. 当前已经完成

- SQLite 表结构已迁移为 Supabase PostgreSQL，共 26 张表。
- 已补齐 18 个外键索引。
- 已迁移本地非空数据，共 187 条：
  - `activity_logs`：145
  - `app_settings`：18
  - `labor_rate_catalog`：10
  - `leads`：12
  - `lead_notes`：2
- 已修正这些表的自增序列。
- `app.js` 已将请求地址切换到 Supabase `project-api`。
- Netlify 生产站点已经成功发布并在线验证。
- 工作台能正常加载。
- 人工工价库能读取 10 条记录。
- 已在线验证材料新增、修改、删除完整流程，临时测试材料已删除。
- 浏览器复测时没有控制台错误。

注意：迁入的 12 条线索状态均为“已删除”，所以前端显示当前线索为 0 是正常现象。

## 5. 当前公开测试权限

用户暂时选择“无需登录直接测试”。

匿名权限当前只开放以下范围：

- `projects`：读取
- `project_alerts`：读取
- `work_orders`：读取
- `labor_reports`：读取
- `leads`：增删改查
- `material_catalog`：增删改查
- `labor_rate_catalog`：增删改查

合同、财务、文件、系统设置和操作日志没有全面开放。

这是临时测试配置。正式使用前必须增加 Supabase Auth，并关闭匿名写入权限。不要在未说明风险的情况下扩大匿名权限。

## 6. 当前 Edge Function 已支持的核心接口

```text
GET    /api/health
GET    /api/projects
GET    /api/alerts
GET    /api/work-orders
GET    /api/labor-reports

GET    /api/leads
POST   /api/leads
PATCH  /api/leads/{id}/advance

GET    /api/material-catalog
POST   /api/material-catalog
PATCH  /api/material-catalog/{id}/update
DELETE /api/material-catalog/{id}

GET    /api/labor-rates
POST   /api/labor-rates
DELETE /api/labor-rates/{id}
```

健康检查期望返回：

```json
{"ok":true,"version":"2026.07.14-supabase-preview"}
```

## 7. 尚未完成的主要工作

数据库表虽然已经建立，但以下线上业务接口还没有完整迁移：

1. 项目详情、项目创建/删除/完工。
2. 赢单后自动创建项目的完整事务。
3. 合同、合同变更、验收日期。
4. 付款节点、收款、应收提醒。
5. BOM 材料、BOM 费用及项目内编辑/删除。
6. 发票、机械费用、费用付款和利润统计。
7. 分包合同及分包付款节点。
8. 工单新建、推进、验收和照片。
9. 人工日报新增、修改、删除、审核和驳回。
10. 系统设置保存。
11. 项目文件、合同、发票、工单图片迁移到 Supabase Storage。
12. Supabase Auth、用户角色和正式 RLS 策略。
13. GitHub -> Netlify 自动部署。

## 8. 推荐实施顺序

### 阶段 A：补齐无文件业务接口

按模块逐步迁移，不要一次性重写：

1. 线索完整生命周期与赢单立项。
2. 项目详情和里程碑。
3. BOM、人工日报、工单。
4. 合同、付款、收款。
5. 发票、费用和利润统计。

每完成一个模块，都要同时验证 GET、POST、PATCH、DELETE 以及对应页面。

### 阶段 B：文件迁移

- 创建 Supabase Storage buckets。
- 迁移项目文件、合同、发票和工单图片。
- 数据库只存 bucket/path/文件元信息，不再把大文件 Base64 写入表。

### 阶段 C：正式安全权限

- 使用 Supabase Auth。
- 至少建立老板、项目经理、财务三类角色。
- 按角色配置 RLS。
- 移除匿名写入策略和 anon 表级写权限。

### 阶段 D：自动部署与运维

- 连接 GitHub 和 Netlify。
- 设置生产分支和 Deploy Preview。
- 建立数据库备份与恢复演练。
- 增加线上健康检查和错误日志检查。

## 9. 工作规则

- 开始前先运行 `git status --short`，不要覆盖用户已有修改。
- 当前可能存在未跟踪的 `deploy-*.zip`，不要提交，也不要擅自删除。
- 修改要小而明确；不要顺手重构无关文件。
- 保持现有原生 JavaScript 风格，除非用户明确要求换框架。
- 所有数据库 DDL 必须通过 Supabase migration。
- 数据查询和调试可以使用 Supabase SQL 工具。
- 不要在前端放 service-role key。
- 报告“按钮没反应”时，必须在真实线上页面复现，并检查：
  1. Netlify 是否加载了最新 JS。
  2. Edge Function 版本和日志。
  3. API 响应状态和正文。
  4. Supabase 表权限、GRANT 和 RLS 策略。
- RLS policy 和 PostgreSQL GRANT 是两层权限；只创建 policy 而没有 GRANT 会得到 `42501 permission denied`。
- 每次发布后必须打开生产 URL 做浏览器复测，不能只看部署成功提示。

## 10. 每个模块的验收标准

一个模块只有满足以下条件才算完成：

1. 页面能读取真实 Supabase 数据。
2. 新增成功。
3. 修改成功。
4. 删除或状态流转成功。
5. 刷新浏览器后数据仍然存在。
6. 错误输入能显示可理解的提示。
7. 浏览器控制台无新增错误。
8. Edge Function 日志没有 500。
9. 不扩大无关表权限。
10. Git 只包含本模块必要改动。

## 11. 新任务建议开场指令

在新 Codex 任务中打开本项目后，可以直接发送：

```text
请先完整阅读项目根目录 AGENTS.md，并检查 git status、线上 Netlify 页面、
Supabase project-api 和当前数据库权限。不要重写项目，也不要扩大匿名权限。
从 AGENTS.md 的“尚未完成的主要工作”开始，先补齐线索完整生命周期与赢单立项，
完成后在真实线上页面验证，再提交结果。
```

