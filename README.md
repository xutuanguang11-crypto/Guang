# 工装项目全流程管理系统

本项目为可本地运行的精简版项目管理系统：前端使用原生 HTML/CSS/JavaScript，后端使用 Python 标准库 HTTP 服务，数据保存到 SQLite。

## 一键启动（推荐）

双击 `双击启动系统.vbs`，系统会在后台启动并自动打开浏览器。

## 命令行启动

在项目目录运行：

```powershell
& 'C:\Users\GuangTou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' server.py
```

浏览器访问 `http://127.0.0.1:8000`。首次启动会自动生成 `data/project_manager.db` 和演示数据。

## 已实现

- SQLite 持久化：线索、项目、现场工单、人力日报和操作审计日志。
- REST API：线索查询/创建/推进阶段，工单销账，人力日报审核，项目与看板查询。
- 关键规则：线索顺序推进、销账照片校验、日报审核后才进入人工成本口径、关键操作日志。

## 后续生产化建议

- 增加用户表、密码哈希、角色权限和会话认证。
- 将附件存至对象存储，增加病毒扫描与文件权限控制。
- 改用 PostgreSQL/MySQL，部署在受控服务器并设置定时备份、HTTPS 和监控告警。
- 接入第三方电子签、企业微信/钉钉消息、短信与邮件服务。
