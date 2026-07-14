"""工装项目管理系统本地服务：静态前端、SQLite 数据库与 REST API。"""

from __future__ import annotations

import json
import mimetypes
import base64
import os
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, date
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATABASE = DATA_DIR / "project_manager.db"
UPLOAD_DIR = DATA_DIR / "uploads"
STAGES = ["线索接入", "现场测绘", "方案出图", "算量报价", "商务谈判"]


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def log(conn: sqlite3.Connection, entity: str, entity_id: int, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO activity_logs (entity, entity_id, action, detail, operator, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (entity, entity_id, action, detail, "系统管理员", datetime.now().isoformat(timespec="seconds")),
    )


def ensure_seed_data(conn: sqlite3.Connection) -> None:
    """只补充安全的系统默认参数；业务数据必须由真实流程创建。"""
    now = datetime.now().isoformat(timespec="seconds")
    defaults={"project_prefix":"XM","approval_threshold":"50000","warranty_rate":"5","warranty_months":"12","default_loss_rate":"3","tax_rate":"9","reminder_days":"30","attachment_limit_mb":"50","backup_time":"02:00","tax_deadline_day":"15","tax_reminder_days":"7","tax_urgent_days":"3","large_invoice_threshold":"100000","receivable_pre_days":"3","receivable_yellow_days":"30","receivable_orange_days":"60","receivable_red_days":"90","prior_input_tax_credit":"0"}
    conn.executemany("INSERT OR IGNORE INTO app_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?)",[(key,value,now) for key,value in defaults.items()])
    conn.commit()


def save_upload(file_name: str, encoded_data: str) -> str:
    if not file_name or not encoded_data:
        return file_name or ""
    safe_name = "".join(char for char in Path(file_name).name if char.isalnum() or char in ".-_ ")
    target = UPLOAD_DIR / f"{datetime.now():%Y%m%d%H%M%S}_{safe_name}"
    target.write_bytes(base64.b64decode(encoded_data.split(",")[-1]))
    return target.name


def delete_upload(storage_name: str | None) -> None:
    if not storage_name:
        return
    target = (UPLOAD_DIR / Path(storage_name).name).resolve()
    if target.parent == UPLOAD_DIR.resolve() and target.exists():
        target.unlink()


def add_months(value: str | None, months: int) -> str | None:
    if not value:
        return None
    base = date.fromisoformat(value[:10])
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day).isoformat()


def create_monthly_progress_alerts(conn: sqlite3.Connection) -> None:
    month = datetime.now().strftime("%Y-%m")
    rows = conn.execute("SELECT id, name FROM projects WHERE source_lead_id IS NOT NULL AND status='施工中'").fetchall()
    for project in rows:
        marker = f"{month} 月度施工进度确认"
        exists = conn.execute("SELECT 1 FROM project_alerts WHERE project_id=? AND message=?", (project["id"], marker)).fetchone()
        if not exists:
            conn.execute("INSERT INTO project_alerts(project_id, alert_type, message, created_at) VALUES (?, '月度进度提醒', ?, ?)", (project["id"], marker, datetime.now().isoformat(timespec="seconds")))
    warranty_nodes = conn.execute("SELECT n.id, n.name, p.id AS project_id, p.name AS project_name FROM payment_nodes n JOIN contracts c ON c.id=n.contract_id JOIN projects p ON p.id=c.project_id WHERE n.name='质保金' AND n.status!='已收款' AND n.planned_date<=date('now','+30 day')").fetchall()
    for node in warranty_nodes:
        marker = f"{node['project_name']}的质保金将在30天内到期，请安排催收"
        exists = conn.execute("SELECT 1 FROM project_alerts WHERE project_id=? AND message=?", (node["project_id"], marker)).fetchone()
        if not exists:
            conn.execute("UPDATE payment_nodes SET status='待收款' WHERE id=?", (node["id"],))
            conn.execute("INSERT INTO project_alerts(project_id, alert_type, message, created_at) VALUES (?, '质保金催收', ?, ?)", (node["project_id"], marker, datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def contract_dynamic_amount(conn: sqlite3.Connection, contract_id: int) -> float:
    base = conn.execute("SELECT amount FROM contracts WHERE id=?", (contract_id,)).fetchone()
    if not base:
        return 0.0
    changes = conn.execute("SELECT COALESCE(SUM(CASE WHEN change_type='减项' THEN -amount ELSE amount END),0) FROM contract_changes WHERE contract_id=? AND status='已生效'", (contract_id,)).fetchone()[0]
    return float(base["amount"] + changes)


def delete_project_records(conn: sqlite3.Connection, project_id: int) -> None:
    project=conn.execute("SELECT source_lead_id FROM projects WHERE id=?",(project_id,)).fetchone()
    source_lead_id=project["source_lead_id"] if project else None
    contract_ids=[row[0] for row in conn.execute("SELECT id FROM contracts WHERE project_id=?",(project_id,))]
    for contract_id in contract_ids:
        node_ids=[row[0] for row in conn.execute("SELECT id FROM payment_nodes WHERE contract_id=?",(contract_id,))]
        for node_id in node_ids:
            for row in conn.execute("SELECT voucher_name FROM receipts WHERE payment_node_id=?",(node_id,)):
                delete_upload(row["voucher_name"])
            conn.execute("DELETE FROM receipts WHERE payment_node_id=?",(node_id,))
        for row in conn.execute("SELECT file_name FROM contract_changes WHERE contract_id=?",(contract_id,)):
            delete_upload(row["file_name"])
        conn.execute("DELETE FROM payment_nodes WHERE contract_id=?",(contract_id,))
        conn.execute("DELETE FROM contract_changes WHERE contract_id=?",(contract_id,))
    for row in conn.execute("SELECT file_name FROM contracts WHERE project_id=?",(project_id,)):
        delete_upload(row["file_name"])
    conn.execute("DELETE FROM contracts WHERE project_id=?",(project_id,))
    subcontract_ids=[row[0] for row in conn.execute("SELECT id FROM subcontracts WHERE project_id=?",(project_id,))]
    for subcontract_id in subcontract_ids:
        conn.execute("DELETE FROM sub_payment_nodes WHERE subcontract_id=?",(subcontract_id,))
    for row in conn.execute("SELECT file_name FROM subcontracts WHERE project_id=?",(project_id,)):
        delete_upload(row["file_name"])
    conn.execute("DELETE FROM subcontracts WHERE project_id=?",(project_id,))
    for row in conn.execute("SELECT storage_name FROM project_files WHERE project_id=?",(project_id,)):
        delete_upload(row["storage_name"])
    for table,column in (("cost_invoices","file_name"),("equipment_costs","invoice_name"),("bom_cost_items","invoice_name"),("expense_payments","voucher_name"),("invoice_ledger","file_name")):
        for row in conn.execute(f"SELECT {column} FROM {table} WHERE project_id=?",(project_id,)):
            delete_upload(row[column])
    for row in conn.execute("SELECT wp.file_name FROM work_order_photos wp JOIN work_orders w ON w.id=wp.work_order_id WHERE w.project_id=?",(project_id,)):
        delete_upload(row["file_name"])
    conn.execute("DELETE FROM work_order_photos WHERE work_order_id IN (SELECT id FROM work_orders WHERE project_id=?)",(project_id,))
    for table in ("cost_invoices","equipment_costs","bom_cost_items","bom_items","work_orders","labor_reports","milestones","project_alerts","project_files"):
        conn.execute(f"DELETE FROM {table} WHERE project_id=?",(project_id,))
    conn.execute("DELETE FROM expense_payments WHERE project_id=?",(project_id,))
    conn.execute("DELETE FROM invoice_ledger WHERE project_id=?",(project_id,))
    conn.execute("DELETE FROM projects WHERE id=?",(project_id,))
    if source_lead_id:
        conn.execute("UPDATE leads SET status='进行中', deleted_at=NULL, deleted_from_status=NULL, stage_since=? WHERE id=?",(datetime.now().isoformat(timespec="seconds"),source_lead_id))


def init_database() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    conn = connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS leads (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, customer TEXT NOT NULL,
          area REAL, budget REAL, owner TEXT NOT NULL, source TEXT, stage INTEGER NOT NULL DEFAULT 0,
          stage_since TEXT NOT NULL, status TEXT NOT NULL DEFAULT '进行中', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          sales_owner TEXT, manager TEXT, contract_amount REAL NOT NULL DEFAULT 0,
          progress INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, bom_version TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, project_id INTEGER, description TEXT NOT NULL,
          work_type TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL, due_at TEXT, closure_photo TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS work_order_photos (
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL,
          stage TEXT NOT NULL, file_name TEXT NOT NULL, original_name TEXT,
          uploaded_by TEXT NOT NULL DEFAULT '管理员', created_at TEXT NOT NULL,
          FOREIGN KEY(work_order_id) REFERENCES work_orders(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS labor_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, report_date TEXT NOT NULL, trade TEXT NOT NULL,
          workers INTEGER NOT NULL, hours REAL NOT NULL DEFAULT 8, hourly_rate REAL NOT NULL,
          status TEXT NOT NULL DEFAULT '待审核', created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS activity_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
          action TEXT NOT NULL, detail TEXT, operator TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contracts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, project_id INTEGER NOT NULL UNIQUE,
          party_a TEXT NOT NULL, amount REAL NOT NULL, signed_date TEXT, completion_date TEXT,
          status TEXT NOT NULL DEFAULT '草稿', file_name TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS milestones (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name TEXT NOT NULL,
          planned_date TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '未开始',
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS bom_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, material_code TEXT NOT NULL,
          material_name TEXT NOT NULL, specification TEXT, unit TEXT NOT NULL, design_quantity REAL NOT NULL,
          loss_rate REAL NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0, invoice_amount REAL NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS cost_invoices (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, bom_item_id INTEGER,
          invoice_type TEXT NOT NULL, invoice_number TEXT NOT NULL, amount REAL NOT NULL, file_name TEXT,
          created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id),
          FOREIGN KEY(bom_item_id) REFERENCES bom_items(id)
        );
        CREATE TABLE IF NOT EXISTS payment_nodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL, name TEXT NOT NULL,
          amount REAL NOT NULL, planned_date TEXT, trigger_milestone_id INTEGER, status TEXT NOT NULL DEFAULT '未触发',
          FOREIGN KEY(contract_id) REFERENCES contracts(id), FOREIGN KEY(trigger_milestone_id) REFERENCES milestones(id)
        );
        CREATE TABLE IF NOT EXISTS receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, payment_node_id INTEGER NOT NULL, received_date TEXT NOT NULL,
          amount REAL NOT NULL, voucher_name TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(payment_node_id) REFERENCES payment_nodes(id)
        );
        CREATE TABLE IF NOT EXISTS project_alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, alert_type TEXT NOT NULL,
          message TEXT NOT NULL, status TEXT NOT NULL DEFAULT '待处理', created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS equipment_costs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name TEXT NOT NULL,
          amount REAL NOT NULL, cost_date TEXT NOT NULL, invoice_name TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS subcontracts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, code TEXT NOT NULL UNIQUE,
          vendor TEXT NOT NULL, scope TEXT NOT NULL, amount REAL NOT NULL, signed_date TEXT,
          completion_date TEXT, warranty_rate REAL NOT NULL DEFAULT 5, warranty_months INTEGER NOT NULL DEFAULT 12,
          status TEXT NOT NULL DEFAULT '已签署', file_name TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS sub_payment_nodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, subcontract_id INTEGER NOT NULL, name TEXT NOT NULL,
          amount REAL NOT NULL, planned_date TEXT, status TEXT NOT NULL DEFAULT '未付款',
          FOREIGN KEY(subcontract_id) REFERENCES subcontracts(id)
        );
        CREATE TABLE IF NOT EXISTS contract_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL, code TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, change_type TEXT NOT NULL, amount REAL NOT NULL, signed_date TEXT,
          status TEXT NOT NULL DEFAULT '已生效', file_name TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(contract_id) REFERENCES contracts(id)
        );
        CREATE TABLE IF NOT EXISTS material_catalog (
          id INTEGER PRIMARY KEY AUTOINCREMENT, material_code TEXT NOT NULL UNIQUE, material_name TEXT NOT NULL,
          specification TEXT, unit TEXT NOT NULL, base_price REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS labor_rate_catalog (
          id INTEGER PRIMARY KEY AUTOINCREMENT, trade TEXT NOT NULL UNIQUE, hourly_rate REAL NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_labor_rates (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, trade TEXT NOT NULL,
          skill_level TEXT NOT NULL DEFAULT '大工', daily_rate REAL NOT NULL,
          updated_at TEXT NOT NULL, UNIQUE(project_id,trade,skill_level),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_files (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, file_type TEXT NOT NULL,
          file_name TEXT NOT NULL, storage_name TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS bom_cost_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, contract_change_id INTEGER,
          cost_type TEXT NOT NULL, name TEXT NOT NULL, pricing_mode TEXT, workers REAL NOT NULL DEFAULT 0,
          work_days REAL NOT NULL DEFAULT 0, daily_rate REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
          cost_date TEXT, supplier TEXT, invoice_name TEXT, note TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS app_settings (
          setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lead_notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, content TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS expense_payments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, source_type TEXT NOT NULL,
          source_id INTEGER NOT NULL, payment_date TEXT NOT NULL, amount REAL NOT NULL,
          voucher_name TEXT, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, project_id INTEGER,
          payment_node_id INTEGER, source_type TEXT, source_id INTEGER, invoice_type TEXT NOT NULL,
          invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL, counterparty TEXT NOT NULL,
          tax_rate REAL NOT NULL DEFAULT 0, tax_amount REAL NOT NULL DEFAULT 0,
          total_amount REAL NOT NULL, file_name TEXT, note TEXT, created_at TEXT NOT NULL
        );
        """
    )
    for statement in (
        "ALTER TABLE leads ADD COLUMN lost_reason TEXT",
        "ALTER TABLE leads ADD COLUMN lost_note TEXT",
        "ALTER TABLE projects ADD COLUMN source_lead_id INTEGER",
        "ALTER TABLE contracts ADD COLUMN warranty_rate REAL NOT NULL DEFAULT 5",
        "ALTER TABLE contracts ADD COLUMN warranty_months INTEGER NOT NULL DEFAULT 12",
        "ALTER TABLE contracts ADD COLUMN acceptance_date TEXT",
        "ALTER TABLE payment_nodes ADD COLUMN ratio REAL",
        "ALTER TABLE bom_items ADD COLUMN contract_change_id INTEGER",
        "ALTER TABLE work_orders ADD COLUMN contract_change_id INTEGER",
        "ALTER TABLE labor_reports ADD COLUMN contract_change_id INTEGER",
        "ALTER TABLE cost_invoices ADD COLUMN contract_change_id INTEGER",
        "ALTER TABLE equipment_costs ADD COLUMN contract_change_id INTEGER",
        "ALTER TABLE work_orders ADD COLUMN photo_name TEXT",
        "ALTER TABLE work_orders ADD COLUMN title TEXT",
        "ALTER TABLE work_orders ADD COLUMN reporter TEXT NOT NULL DEFAULT '管理员'",
        "ALTER TABLE work_orders ADD COLUMN resolution_note TEXT",
        "ALTER TABLE work_orders ADD COLUMN reject_reason TEXT",
        "ALTER TABLE work_orders ADD COLUMN completed_at TEXT",
        "ALTER TABLE leads ADD COLUMN deleted_at TEXT",
        "ALTER TABLE leads ADD COLUMN deleted_from_status TEXT",
        "ALTER TABLE material_catalog ADD COLUMN category TEXT",
        "ALTER TABLE material_catalog ADD COLUMN primary_category TEXT",
        "ALTER TABLE material_catalog ADD COLUMN subcategory TEXT",
        "ALTER TABLE material_catalog ADD COLUMN note TEXT",
        "ALTER TABLE material_catalog ADD COLUMN loss_rate REAL NOT NULL DEFAULT 0",
        "ALTER TABLE material_catalog ADD COLUMN supplier TEXT",
        "ALTER TABLE material_catalog ADD COLUMN last_purchase_price REAL NOT NULL DEFAULT 0",
        "ALTER TABLE labor_rate_catalog ADD COLUMN pricing_mode TEXT NOT NULL DEFAULT '按天'",
        "ALTER TABLE labor_rate_catalog ADD COLUMN daily_rate REAL NOT NULL DEFAULT 0",
        "ALTER TABLE labor_rate_catalog ADD COLUMN trade_name TEXT",
        "ALTER TABLE labor_rate_catalog ADD COLUMN skill_level TEXT",
        "ALTER TABLE labor_rate_catalog ADD COLUMN note TEXT",
        "ALTER TABLE labor_rate_catalog ADD COLUMN piece_unit TEXT",
        "ALTER TABLE labor_rate_catalog ADD COLUMN piece_rate REAL NOT NULL DEFAULT 0",
        "ALTER TABLE invoice_ledger ADD COLUMN certification_status TEXT NOT NULL DEFAULT '未认证'",
        "ALTER TABLE invoice_ledger ADD COLUMN certified_date TEXT",
        "ALTER TABLE invoice_ledger ADD COLUMN deduction_status TEXT NOT NULL DEFAULT '未申报'",
        "ALTER TABLE invoice_ledger ADD COLUMN booked_status TEXT NOT NULL DEFAULT '未入账'",
        "ALTER TABLE invoice_ledger ADD COLUMN booked_date TEXT",
        "ALTER TABLE labor_reports ADD COLUMN pricing_mode TEXT NOT NULL DEFAULT '按天'",
        "ALTER TABLE labor_reports ADD COLUMN work_days REAL NOT NULL DEFAULT 1",
        "ALTER TABLE labor_reports ADD COLUMN daily_rate REAL NOT NULL DEFAULT 0",
        "ALTER TABLE labor_reports ADD COLUMN direct_amount REAL NOT NULL DEFAULT 0",
        "ALTER TABLE labor_reports ADD COLUMN batch_code TEXT",
        "ALTER TABLE labor_reports ADD COLUMN skill_level TEXT NOT NULL DEFAULT '大工'",
        "ALTER TABLE labor_reports ADD COLUMN team_name TEXT",
        "ALTER TABLE labor_reports ADD COLUMN leader TEXT",
        "ALTER TABLE labor_reports ADD COLUMN reporter TEXT NOT NULL DEFAULT '管理员'",
        "ALTER TABLE labor_reports ADD COLUMN reviewer TEXT",
        "ALTER TABLE labor_reports ADD COLUMN reviewed_at TEXT",
        "ALTER TABLE labor_reports ADD COLUMN reject_reason TEXT",
        "ALTER TABLE labor_reports ADD COLUMN rejected_at TEXT",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    UPLOAD_DIR.mkdir(exist_ok=True)
    ensure_seed_data(conn)
    conn.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        # 本地系统频繁迭代，禁止浏览器继续使用旧的 HTML/JS/CSS 缓存。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def json_response(self, data: object, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size == 0:
            return {}
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("请求数据必须为 JSON") from error

    def api_error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.json_response({"error": message}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            if parsed.path == "/":
                self.path = "/index.html"
            return super().do_GET()
        conn = connection()
        try:
            if parsed.path == "/api/health":
                return self.json_response({"status": "ok", "database": DATABASE.name, "version": "2026.07.12-redwood"})
            if parsed.path == "/api/leads":
                query = parse_qs(parsed.query)
                values: list[object] = []
                where = []
                if query.get("q"):
                    where.append("(name LIKE ? OR customer LIKE ?)")
                    values.extend([f"%{query['q'][0]}%", f"%{query['q'][0]}%"])
                if query.get("stage"):
                    where.append("stage = ?")
                    values.append(STAGES.index(query["stage"][0]))
                sql = "SELECT * FROM leads" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
                rows = [dict(row) for row in conn.execute(sql, values)]
                return self.json_response({"items": rows, "stages": STAGES})
            if parsed.path.startswith("/api/leads/") and parsed.path.endswith("/detail"):
                lead_id=int(parsed.path.split("/")[3]);lead=conn.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone()
                if not lead:return self.api_error("线索不存在",HTTPStatus.NOT_FOUND)
                notes=conn.execute("SELECT * FROM lead_notes WHERE lead_id=? ORDER BY id DESC",(lead_id,))
                return self.json_response({"lead":dict(lead),"notes":[dict(row) for row in notes]})
            if parsed.path == "/api/projects":
                return self.json_response({"items": [dict(row) for row in conn.execute("SELECT * FROM projects WHERE source_lead_id IS NOT NULL ORDER BY id DESC")]})
            if parsed.path == "/api/work-orders":
                query=parse_qs(parsed.query);project_id=query.get("project_id",[None])[0]
                sql="SELECT w.*, p.name AS project_name, p.manager AS project_manager, p.sales_owner, (SELECT COUNT(*) FROM work_order_photos wp WHERE wp.work_order_id=w.id) AS photo_count FROM work_orders w JOIN projects p ON p.id=w.project_id WHERE p.source_lead_id IS NOT NULL"
                args=[]
                if project_id: sql+=" AND w.project_id=?";args.append(project_id)
                if query.get("status"): sql+=" AND w.status=?";args.append(query["status"][0])
                if query.get("work_type"): sql+=" AND w.work_type=?";args.append(query["work_type"][0])
                if query.get("assignee"): sql+=" AND w.assignee LIKE ?";args.append(f"%{query['assignee'][0]}%")
                if query.get("from"): sql+=" AND substr(w.created_at,1,10)>=?";args.append(query["from"][0])
                if query.get("to"): sql+=" AND substr(w.created_at,1,10)<=?";args.append(query["to"][0])
                if query.get("overdue"): sql+=" AND w.due_at<? AND w.status!='已完成'";args.append(datetime.now().isoformat(timespec="minutes"))
                if query.get("q"):
                    word=f"%{query['q'][0]}%";sql+=" AND (w.code LIKE ? OR p.name LIKE ? OR w.title LIKE ? OR w.description LIKE ?)";args.extend([word,word,word,word])
                sql+=" ORDER BY w.id DESC"
                rows = conn.execute(sql,args)
                return self.json_response({"items": [dict(row) for row in rows]})
            if parsed.path.startswith("/api/work-orders/") and parsed.path.endswith("/detail"):
                work_id=int(parsed.path.split("/")[3])
                row=conn.execute("SELECT w.*,p.name AS project_name,p.manager AS project_manager,p.sales_owner FROM work_orders w JOIN projects p ON p.id=w.project_id WHERE w.id=?",(work_id,)).fetchone()
                if not row:return self.api_error("工单不存在",HTTPStatus.NOT_FOUND)
                photos=[dict(x) for x in conn.execute("SELECT * FROM work_order_photos WHERE work_order_id=? ORDER BY id",(work_id,))]
                logs=[dict(x) for x in conn.execute("SELECT * FROM activity_logs WHERE entity='work_order' AND entity_id=? ORDER BY id DESC",(work_id,))]
                return self.json_response({"work_order":dict(row),"photos":photos,"logs":logs})
            if parsed.path.startswith("/api/work-order-photo/"):
                photo_id=int(parsed.path.rsplit("/",1)[-1]);row=conn.execute("SELECT file_name,original_name FROM work_order_photos WHERE id=?",(photo_id,)).fetchone()
                if not row:return self.api_error("图片不存在",HTTPStatus.NOT_FOUND)
                target=UPLOAD_DIR/row["file_name"]
                if not target.exists():return self.api_error("图片文件不存在",HTTPStatus.NOT_FOUND)
                raw=target.read_bytes();self.send_response(HTTPStatus.OK);self.send_header("Content-Type",mimetypes.guess_type(row["original_name"] or row["file_name"])[0] or "image/jpeg");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path == "/api/labor-reports":
                query=parse_qs(parsed.query);where=["p.source_lead_id IS NOT NULL"];args=[]
                for key,column in (("project_id","l.project_id"),("status","l.status"),("report_date","l.report_date")):
                    if query.get(key):where.append(f"{column}=?");args.append(query[key][0])
                rows = conn.execute("SELECT l.*, p.name AS project_name, CASE WHEN l.pricing_mode='直接金额' THEN l.direct_amount ELSE l.workers*l.work_days*l.daily_rate END AS cost FROM labor_reports l JOIN projects p ON p.id=l.project_id WHERE "+" AND ".join(where)+" ORDER BY l.report_date DESC,l.id DESC",args)
                return self.json_response({"items": [dict(row) for row in rows]})
            if parsed.path.startswith("/api/labor-reports/"):
                report_id = int(parsed.path.rsplit("/", 1)[-1])
                row = conn.execute("SELECT l.*, p.name AS project_name, CASE WHEN l.pricing_mode='直接金额' THEN l.direct_amount ELSE l.workers*l.work_days*l.daily_rate END AS cost FROM labor_reports l JOIN projects p ON p.id=l.project_id WHERE l.id=? AND p.source_lead_id IS NOT NULL", (report_id,)).fetchone()
                if not row:
                    return self.api_error("日报不存在", HTTPStatus.NOT_FOUND)
                return self.json_response(dict(row))
            if parsed.path == "/api/contracts":
                items = []
                for row in conn.execute("SELECT c.*, p.name AS project_name, p.code AS project_code FROM contracts c JOIN projects p ON p.id=c.project_id WHERE p.source_lead_id IS NOT NULL ORDER BY c.id DESC"):
                    item = dict(row)
                    item["dynamic_amount"] = contract_dynamic_amount(conn, row["id"])
                    items.append(item)
                return self.json_response({"items": items})
            if parsed.path.startswith("/api/download-contract/"):
                contract_id=int(parsed.path.rsplit("/",1)[-1]);row=conn.execute("SELECT file_name FROM contracts WHERE id=?",(contract_id,)).fetchone()
                if not row or not row["file_name"]:return self.api_error("合同附件不存在",HTTPStatus.NOT_FOUND)
                target=UPLOAD_DIR/row["file_name"]
                if not target.exists():return self.api_error("合同附件文件不存在",HTTPStatus.NOT_FOUND)
                raw=target.read_bytes();self.send_response(HTTPStatus.OK);self.send_header("Content-Type",mimetypes.guess_type(row["file_name"])[0] or "application/pdf");self.send_header("Content-Disposition",f"inline; filename*=UTF-8''{row['file_name']}");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path.startswith("/api/download-invoice/"):
                invoice_id=int(parsed.path.rsplit("/",1)[-1]);row=conn.execute("SELECT file_name FROM invoice_ledger WHERE id=?",(invoice_id,)).fetchone()
                if not row or not row["file_name"]:return self.api_error("发票附件不存在",HTTPStatus.NOT_FOUND)
                target=UPLOAD_DIR/row["file_name"]
                if not target.exists():return self.api_error("发票附件文件不存在",HTTPStatus.NOT_FOUND)
                raw=target.read_bytes();self.send_response(HTTPStatus.OK);self.send_header("Content-Type",mimetypes.guess_type(row["file_name"])[0] or "application/pdf");self.send_header("Content-Disposition",f"inline; filename*=UTF-8''{row['file_name']}");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path == "/api/bom-items":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows = conn.execute("SELECT *, ROUND(design_quantity*(1+loss_rate/100),2) AS purchase_quantity, ROUND(design_quantity*(1+loss_rate/100)*unit_price,2) AS subtotal FROM bom_items WHERE project_id=? ORDER BY id DESC", (project_id,))
                return self.json_response({"items": [dict(row) for row in rows]})
            if parsed.path == "/api/invoices":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                rows = conn.execute("SELECT i.*, b.material_name FROM cost_invoices i LEFT JOIN bom_items b ON b.id=i.bom_item_id" + (" WHERE i.project_id=?" if project_id else "") + " ORDER BY i.id DESC", ([project_id] if project_id else []))
                return self.json_response({"items": [dict(row) for row in rows]})
            if parsed.path == "/api/equipment-costs":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows = conn.execute("SELECT * FROM equipment_costs WHERE project_id=? ORDER BY cost_date DESC, id DESC", (project_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/material-catalog":
                return self.json_response({"items":[dict(row) for row in conn.execute("SELECT * FROM material_catalog ORDER BY material_code")]})
            if parsed.path == "/api/labor-rates":
                return self.json_response({"items":[dict(row) for row in conn.execute("SELECT * FROM labor_rate_catalog ORDER BY trade")]})
            if parsed.path == "/api/labor-price-options":
                project_id=parse_qs(parsed.query).get("project_id",[None])[0]
                items=[];seen=set()
                for row in conn.execute("SELECT * FROM labor_rate_catalog WHERE pricing_mode='按天' ORDER BY trade"):
                    item=dict(row);item["source"]="全局标准价"
                    key=(item.get("trade_name") or item["trade"],item.get("skill_level") or "大工");seen.add(key)
                    if project_id:
                        project_rate=conn.execute("SELECT daily_rate FROM project_labor_rates WHERE project_id=? AND trade=? AND skill_level=?",(project_id,item.get("trade_name") or item["trade"],item.get("skill_level") or "大工")).fetchone()
                        if project_rate:item["daily_rate"]=project_rate["daily_rate"];item["source"]="项目专属价"
                    items.append(item)
                if project_id:
                    for row in conn.execute("SELECT * FROM project_labor_rates WHERE project_id=? ORDER BY trade,skill_level",(project_id,)):
                        key=(row["trade"],row["skill_level"])
                        if key not in seen:items.append({"trade":f"{row['trade']}（{row['skill_level']}）","trade_name":row["trade"],"skill_level":row["skill_level"],"pricing_mode":"按天","daily_rate":row["daily_rate"],"source":"项目专属价"})
                return self.json_response({"items":items})
            if parsed.path == "/api/project-labor-rates":
                project_id=parse_qs(parsed.query).get("project_id",[None])[0]
                if not project_id:return self.api_error("请选择项目")
                return self.json_response({"items":[dict(row) for row in conn.execute("SELECT * FROM project_labor_rates WHERE project_id=? ORDER BY trade,skill_level",(project_id,))]})
            if parsed.path == "/api/bom-labor-summary":
                project_id=parse_qs(parsed.query).get("project_id",[None])[0]
                if not project_id:return self.api_error("请选择项目")
                budget=conn.execute("SELECT COALESCE(SUM(CASE WHEN pricing_mode='按天' THEN workers*work_days*daily_rate ELSE amount END),0) FROM bom_cost_items WHERE project_id=? AND cost_type='人工费'",(project_id,)).fetchone()[0]
                actual=conn.execute("SELECT COALESCE(SUM(CASE WHEN pricing_mode='直接金额' THEN direct_amount ELSE workers*work_days*daily_rate END),0) FROM labor_reports WHERE project_id=? AND status='已审核'",(project_id,)).fetchone()[0]
                details=[dict(row) for row in conn.execute("SELECT id,report_date,trade,skill_level,team_name,leader,workers,work_days,daily_rate,workers*work_days*daily_rate AS amount,reporter,reviewer,reviewed_at FROM labor_reports WHERE project_id=? AND status='已审核' ORDER BY report_date DESC,id DESC",(project_id,))]
                return self.json_response({"budget":budget,"actual":actual,"variance":actual-budget,"details":details})
            if parsed.path == "/api/project-files":
                project_id=parse_qs(parsed.query).get("project_id",[None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows=conn.execute("SELECT * FROM project_files WHERE project_id=? ORDER BY id DESC",(project_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/bom-cost-items":
                project_id=parse_qs(parsed.query).get("project_id",[None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows=conn.execute("SELECT *, CASE WHEN cost_type='人工费' AND pricing_mode='按天' THEN workers*work_days*daily_rate ELSE amount END AS calculated_amount FROM bom_cost_items WHERE project_id=? ORDER BY id DESC",(project_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/settings":
                return self.json_response({row["setting_key"]:row["setting_value"] for row in conn.execute("SELECT * FROM app_settings")})
            if parsed.path == "/api/expense-payments":
                query=parse_qs(parsed.query);source_type=query.get("source_type",[None])[0];source_id=query.get("source_id",[None])[0]
                if not source_type or not source_id:return self.api_error("请选择费用明细")
                rows=conn.execute("SELECT * FROM expense_payments WHERE source_type=? AND source_id=? ORDER BY payment_date DESC,id DESC",(source_type,source_id))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/invoice-ledger":
                query=parse_qs(parsed.query);where=[];args=[]
                for key,column in (("project_id","project_id"),("direction","direction"),("source_type","source_type"),("source_id","source_id")):
                    if query.get(key):where.append(f"{column}=?");args.append(query[key][0])
                if query.get("month"):where.append("substr(invoice_date,1,7)=?");args.append(query["month"][0])
                sql="SELECT i.*,p.name AS project_name,n.name AS payment_node_name,n.planned_date AS payment_due_date,n.amount AS payment_node_amount,COALESCE((SELECT SUM(r.amount) FROM receipts r WHERE r.payment_node_id=n.id),0) AS received_amount FROM invoice_ledger i LEFT JOIN projects p ON p.id=i.project_id LEFT JOIN payment_nodes n ON n.id=i.payment_node_id"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY i.invoice_date DESC,i.id DESC"
                return self.json_response({"items":[dict(row) for row in conn.execute(sql,args)]})
            if parsed.path == "/api/invoice-monthly-summary":
                rows=conn.execute("SELECT substr(invoice_date,1,7) AS month, SUM(CASE WHEN direction='进项' THEN total_amount ELSE 0 END) AS inbound_amount, SUM(CASE WHEN direction='销项' THEN total_amount ELSE 0 END) AS outbound_amount, SUM(CASE WHEN direction='进项' THEN tax_amount ELSE 0 END) AS inbound_tax, SUM(CASE WHEN direction='销项' THEN tax_amount ELSE 0 END) AS outbound_tax FROM invoice_ledger GROUP BY substr(invoice_date,1,7) ORDER BY month DESC")
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/project-finance-summary":
                project_id=int(parse_qs(parsed.query).get("project_id",[0])[0]);contract=conn.execute("SELECT id FROM contracts WHERE project_id=?",(project_id,)).fetchone();dynamic=contract_dynamic_amount(conn,contract["id"]) if contract else 0
                paid=conn.execute("SELECT COALESCE(SUM(amount),0) FROM expense_payments WHERE project_id=?",(project_id,)).fetchone()[0]
                labor_actual=conn.execute("SELECT COALESCE(SUM(CASE WHEN pricing_mode='直接金额' THEN direct_amount ELSE workers*work_days*daily_rate END),0) FROM labor_reports WHERE project_id=? AND status='已审核'",(project_id,)).fetchone()[0]
                received=conn.execute("SELECT COALESCE(SUM(r.amount),0) FROM receipts r JOIN payment_nodes n ON n.id=r.payment_node_id JOIN contracts c ON c.id=n.contract_id WHERE c.project_id=?",(project_id,)).fetchone()[0]
                inbound=conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM invoice_ledger WHERE project_id=? AND direction='进项'",(project_id,)).fetchone()[0]
                outbound=conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM invoice_ledger WHERE project_id=? AND direction='销项'",(project_id,)).fetchone()[0]
                due=0
                if contract:
                    for node in conn.execute("SELECT amount,ratio,status,planned_date FROM payment_nodes WHERE contract_id=?",(contract["id"],)):
                        if node["status"] in ("待收款","已收款") or (node["planned_date"] and node["planned_date"]<=date.today().isoformat()):due+=round(dynamic*node["ratio"]/100,2) if node["ratio"] is not None else node["amount"]
                return self.json_response({"dynamic_income":dynamic,"received_amount":received,"paid_amount":paid,"labor_actual":labor_actual,"management_cost":inbound+labor_actual,"management_profit":dynamic-inbound-labor_actual,"inbound_invoice":inbound,"due_outbound_invoice":due,"outbound_invoice":outbound,"unissued_invoice":max(due-outbound,0),"actual_profit":dynamic-inbound})
            if parsed.path.startswith("/api/download-file/"):
                file_id=int(parsed.path.rsplit("/",1)[-1]);row=conn.execute("SELECT file_name, storage_name FROM project_files WHERE id=?",(file_id,)).fetchone()
                if not row:return self.api_error("文件不存在",HTTPStatus.NOT_FOUND)
                target=UPLOAD_DIR/row["storage_name"]
                if not target.exists():return self.api_error("存储文件不存在",HTTPStatus.NOT_FOUND)
                raw=target.read_bytes();self.send_response(HTTPStatus.OK);self.send_header("Content-Type",mimetypes.guess_type(row["file_name"])[0] or "application/octet-stream");self.send_header("Content-Disposition",f"attachment; filename*=UTF-8''{row['file_name']}");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path == "/api/subcontracts":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows = conn.execute("SELECT s.*, COALESCE((SELECT SUM(amount) FROM sub_payment_nodes n WHERE n.subcontract_id=s.id),0) AS planned_payment FROM subcontracts s WHERE s.project_id=? ORDER BY s.id DESC", (project_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/sub-payment-nodes":
                subcontract_id = parse_qs(parsed.query).get("subcontract_id", [None])[0]
                if not subcontract_id:
                    return self.api_error("请选择分包合同")
                rows = conn.execute("SELECT * FROM sub_payment_nodes WHERE subcontract_id=? ORDER BY planned_date, id", (subcontract_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/payment-nodes":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                sql = "SELECT n.*, c.code AS contract_code, p.name AS project_name, COALESCE((SELECT SUM(amount) FROM receipts r WHERE r.payment_node_id=n.id),0) AS received_amount FROM payment_nodes n JOIN contracts c ON c.id=n.contract_id JOIN projects p ON p.id=c.project_id"
                if project_id:
                    sql += " WHERE p.id=?"
                sql += " ORDER BY n.planned_date, n.id"
                items=[]
                for row in conn.execute(sql, [project_id] if project_id else []):
                    item=dict(row); item["effective_amount"]=round(contract_dynamic_amount(conn, row["contract_id"])*row["ratio"]/100,2) if row["ratio"] is not None else row["amount"]; items.append(item)
                return self.json_response({"items":items})
            if parsed.path == "/api/contract-changes":
                project_id = parse_qs(parsed.query).get("project_id", [None])[0]
                if not project_id:
                    return self.api_error("请选择项目")
                rows = conn.execute("SELECT ch.*, c.code AS contract_code FROM contract_changes ch JOIN contracts c ON c.id=ch.contract_id WHERE c.project_id=? ORDER BY ch.id DESC", (project_id,))
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path == "/api/ledger":
                sql = "SELECT r.id AS receipt_id, substr(r.received_date,1,7) AS month, p.code AS project_code, p.name AS project_name, n.name AS node_name, r.received_date, r.amount, r.voucher_name FROM receipts r JOIN payment_nodes n ON n.id=r.payment_node_id JOIN contracts c ON c.id=n.contract_id JOIN projects p ON p.id=c.project_id WHERE p.source_lead_id IS NOT NULL ORDER BY r.received_date DESC"
                return self.json_response({"items": [dict(row) for row in conn.execute(sql)]})
            if parsed.path == "/api/profit":
                projects = []
                for project in conn.execute("SELECT id, code, name FROM projects WHERE source_lead_id IS NOT NULL ORDER BY id"):
                    contract = conn.execute("SELECT id FROM contracts WHERE project_id=? AND status='已签署'", (project["id"],)).fetchone()
                    income = contract_dynamic_amount(conn, contract["id"]) if contract else 0
                    material_budget = conn.execute("SELECT COALESCE(SUM(design_quantity*(1+loss_rate/100)*unit_price),0) FROM bom_items WHERE project_id=?", (project["id"],)).fetchone()[0]
                    labor = conn.execute("SELECT COALESCE(SUM(CASE WHEN pricing_mode='直接金额' THEN direct_amount ELSE workers*work_days*daily_rate END),0) FROM labor_reports WHERE project_id=? AND status='已审核'", (project["id"],)).fetchone()[0]
                    actual_invoices = conn.execute("SELECT COALESCE(SUM(amount),0) FROM cost_invoices WHERE project_id=?", (project["id"],)).fetchone()[0]
                    bom_cost_actual = conn.execute("SELECT COALESCE(SUM(CASE WHEN cost_type='人工费' AND pricing_mode='按天' THEN workers*work_days*daily_rate ELSE amount END),0) FROM bom_cost_items WHERE project_id=? AND invoice_name!=''",(project["id"],)).fetchone()[0]
                    equipment = 0
                    subcontract = conn.execute("SELECT COALESCE(SUM(amount),0) FROM subcontracts WHERE project_id=?", (project["id"],)).fetchone()[0]
                    received = conn.execute("SELECT COALESCE(SUM(r.amount),0) FROM receipts r JOIN payment_nodes n ON n.id=r.payment_node_id JOIN contracts c ON c.id=n.contract_id WHERE c.project_id=?", (project["id"],)).fetchone()[0]
                    actual_cost = actual_invoices + bom_cost_actual
                    gross = income - actual_cost
                    projects.append({"id":project["id"],"code":project["code"],"name":project["name"],"income":income,"material_budget":material_budget,"labor":labor,"actual_invoices":actual_invoices,"bom_cost_actual":bom_cost_actual,"equipment":equipment,"subcontract":subcontract,"actual_cost":actual_cost,"received":received,"gross_profit":gross,"gross_margin":round(gross/income*100,2) if income else None})
                return self.json_response({"items":projects})
            if parsed.path == "/api/alerts":
                create_monthly_progress_alerts(conn)
                rows = conn.execute("SELECT a.*, p.name AS project_name FROM project_alerts a JOIN projects p ON p.id=a.project_id WHERE a.status='待处理' ORDER BY a.id DESC")
                return self.json_response({"items":[dict(row) for row in rows]})
            if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/detail"):
                project_id = int(parsed.path.split("/")[3])
                project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                if not project:
                    return self.api_error("项目不存在", HTTPStatus.NOT_FOUND)
                contract = conn.execute("SELECT * FROM contracts WHERE project_id=?", (project_id,)).fetchone()
                milestones = conn.execute("SELECT * FROM milestones WHERE project_id=? ORDER BY planned_date", (project_id,)).fetchall()
                nodes = conn.execute("SELECT n.*, COALESCE((SELECT SUM(amount) FROM receipts r WHERE r.payment_node_id=n.id),0) AS received_amount FROM payment_nodes n JOIN contracts c ON c.id=n.contract_id WHERE c.project_id=? ORDER BY n.planned_date", (project_id,)).fetchall()
                dynamic_amount = contract_dynamic_amount(conn, contract["id"]) if contract else 0
                node_items=[]
                for node in nodes:
                    item=dict(node); item["effective_amount"]=round(dynamic_amount*node["ratio"]/100,2) if node["ratio"] is not None else node["amount"]; node_items.append(item)
                changes = conn.execute("SELECT * FROM contract_changes WHERE contract_id=? ORDER BY id DESC", (contract["id"],)).fetchall() if contract else []
                open_orders = conn.execute("SELECT COUNT(*) FROM work_orders WHERE project_id=? AND status NOT IN ('已销账','已完成')", (project_id,)).fetchone()[0]
                contract_item=dict(contract) if contract else None
                if contract_item: contract_item["dynamic_amount"]=dynamic_amount
                return self.json_response({"project": dict(project), "contract": contract_item, "milestones": [dict(row) for row in milestones], "payment_nodes":node_items, "contract_changes":[dict(row) for row in changes], "open_work_orders": open_orders})
            if parsed.path == "/api/dashboard":
                lead_count = conn.execute("SELECT COUNT(*) FROM leads WHERE status='进行中'").fetchone()[0]
                project_count = conn.execute("SELECT COUNT(*) FROM projects WHERE status != '已完成'").fetchone()[0]
                work_count = conn.execute("SELECT COUNT(*) FROM work_orders WHERE status NOT IN ('已销账','已完成')").fetchone()[0]
                return self.json_response({"active_leads": lead_count, "active_projects": project_count, "open_work_orders": work_count})
            return self.api_error("接口不存在", HTTPStatus.NOT_FOUND)
        finally:
            conn.close()

    def do_POST(self) -> None:
        try:
            data = self.body()
            conn = connection()
            now = datetime.now().isoformat(timespec="seconds")
            if self.path == "/api/project-files":
                if not data.get("project_id") or not data.get("file_name") or not data.get("file_data") or not data.get("file_type"):
                    return self.api_error("请选择项目和文件")
                suffix=Path(data["file_name"]).suffix.lower()
                allowed={"报价表":(".xls",".xlsx"),"CAD图纸":(".dwg",)}
                if data["file_type"] not in allowed or suffix not in allowed[data["file_type"]]:
                    return self.api_error("报价表仅支持 Excel，CAD 图纸仅支持 DWG")
                storage_name=save_upload(data["file_name"],data["file_data"])
                cursor=conn.execute("INSERT INTO project_files(project_id,file_type,file_name,storage_name,created_at) VALUES (?,?,?,?,?)",(data["project_id"],data["file_type"],data["file_name"],storage_name,now))
                log(conn,"project_file",cursor.lastrowid,"上传项目文件",data["file_name"]);conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/lead-notes":
                if not data.get("lead_id") or not str(data.get("content","")).strip():return self.api_error("请填写跟进备注")
                cursor=conn.execute("INSERT INTO lead_notes(lead_id,content,created_at) VALUES (?,?,?)",(data["lead_id"],data["content"].strip(),now));log(conn,"lead_note",cursor.lastrowid,"新增跟进备注",data["content"][:50]);conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/expense-payments":
                required=("project_id","source_type","source_id","payment_date","amount")
                if any(str(data.get(key,"")).strip()=="" for key in required):return self.api_error("请填写付款日期和金额")
                voucher=save_upload(data.get("voucher_name",""),data.get("voucher_data",""))
                cursor=conn.execute("INSERT INTO expense_payments(project_id,source_type,source_id,payment_date,amount,voucher_name,note,created_at) VALUES (?,?,?,?,?,?,?,?)",(data["project_id"],data["source_type"],data["source_id"],data["payment_date"],float(data["amount"]),voucher,data.get("note",""),now));log(conn,"expense_payment",cursor.lastrowid,"登记付款",str(data["amount"]));conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/invoice-ledger":
                required=("direction","invoice_type","invoice_number","invoice_date","counterparty","total_amount")
                if any(str(data.get(key,"")).strip()=="" for key in required):return self.api_error("请完整填写发票类型、号码、日期、往来单位和金额")
                file_name=save_upload(data.get("file_name",""),data.get("file_data",""));total=float(data["total_amount"]);rate=float(data.get("tax_rate") or 0);tax=float(data.get("tax_amount") or (total*rate/(100+rate) if rate else 0))
                cursor=conn.execute("INSERT INTO invoice_ledger(direction,project_id,payment_node_id,source_type,source_id,invoice_type,invoice_number,invoice_date,counterparty,tax_rate,tax_amount,total_amount,file_name,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(data["direction"],data.get("project_id") or None,data.get("payment_node_id") or None,data.get("source_type") or None,data.get("source_id") or None,data["invoice_type"],data["invoice_number"],data["invoice_date"],data["counterparty"],rate,tax,total,file_name,data.get("note",""),now));log(conn,"invoice",cursor.lastrowid,"录入发票",data["invoice_number"]);conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/open-file":
                row=conn.execute("SELECT storage_name FROM project_files WHERE id=?",(data.get("file_id"),)).fetchone()
                if not row:return self.api_error("文件不存在",HTTPStatus.NOT_FOUND)
                target=(UPLOAD_DIR/row["storage_name"]).resolve()
                if not target.exists():return self.api_error("存储文件不存在",HTTPStatus.NOT_FOUND)
                try:os.startfile(str(target))
                except OSError as error:return self.api_error(f"无法调用本机软件打开文件：{error}")
                conn.close();return self.json_response({"ok":True})
            if self.path == "/api/bom-cost-items":
                required=("project_id","cost_type","name")
                if any(not str(data.get(key,"")).strip() for key in required):return self.api_error("请填写项目、费用类型和名称")
                mode=data.get("pricing_mode","");amount=float(data.get("amount") or 0)
                if data["cost_type"]=="人工费" and mode=="按天":amount=float(data.get("workers") or 0)*float(data.get("work_days") or 0)*float(data.get("daily_rate") or 0)
                invoice_name=save_upload(data.get("invoice_name",""),data.get("invoice_data",""))
                cursor=conn.execute("INSERT INTO bom_cost_items(project_id,contract_change_id,cost_type,name,pricing_mode,workers,work_days,daily_rate,amount,cost_date,supplier,invoice_name,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(data["project_id"],data.get("contract_change_id") or None,data["cost_type"],data["name"],mode,float(data.get("workers") or 0),float(data.get("work_days") or 0),float(data.get("daily_rate") or 0),amount,data.get("cost_date"),data.get("supplier",""),invoice_name,data.get("note",""),now))
                log(conn,"bom_cost",cursor.lastrowid,"新增BOM费用",data["name"]);conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/settings":
                conn.executemany("INSERT INTO app_settings(setting_key,setting_value,updated_at) VALUES (?,?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at",[(str(key),str(value),now) for key,value in data.items()]);conn.commit();conn.close();return self.json_response({"ok":True})
            if self.path == "/api/project-alerts":
                if not data.get("project_id") or not data.get("message"):return self.api_error("请选择项目并填写提醒内容")
                cursor=conn.execute("INSERT INTO project_alerts(project_id,alert_type,message,created_at) VALUES (?,?,?,?)",(data["project_id"],data.get("alert_type","回款提醒"),data["message"],now));log(conn,"project_alert",cursor.lastrowid,"发起回款提醒",data["message"]);conn.commit();conn.close();return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/contracts":
                project_id = int(data.get("project_id", 0))
                if not project_id or not data.get("party_a") or not data.get("amount"):
                    return self.api_error("项目、甲方和合同金额为必填项")
                number = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] + 1
                code = f"HT-{datetime.now():%y%m}-{number:04d}"
                file_name = save_upload(data.get("file_name", ""), data.get("file_data", ""))
                cursor = conn.execute("INSERT INTO contracts(code, project_id, party_a, amount, signed_date, completion_date, status, file_name, created_at, warranty_rate, warranty_months) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, project_id, data["party_a"], float(data["amount"]), data.get("signed_date"), data.get("completion_date"), data.get("status", "已签署"), file_name, now, float(data.get("warranty_rate") or 5), int(data.get("warranty_months") or 12)))
                conn.execute("UPDATE projects SET contract_amount=?, status='待排施工计划' WHERE id=?", (float(data["amount"]), project_id))
                log(conn, "contract", cursor.lastrowid, "创建合同", code)
                conn.commit(); conn.close()
                return self.json_response({"id": cursor.lastrowid, "code": code}, HTTPStatus.CREATED)
            if self.path == "/api/payment-nodes":
                required = ("contract_id", "name", "ratio")
                if any(not data.get(key) for key in required):
                    return self.api_error("请填写合同、付款节点名称和付款比例")
                ratio=float(data["ratio"])
                if ratio <= 0 or ratio > 100:
                    return self.api_error("付款比例必须大于 0 且不超过 100")
                used_ratio=conn.execute("SELECT COALESCE(SUM(ratio),0) FROM payment_nodes WHERE contract_id=?",(data["contract_id"],)).fetchone()[0]
                if used_ratio + ratio > 100.0001:
                    return self.api_error("付款节点比例合计不能超过 100%")
                dynamic_amount=contract_dynamic_amount(conn, int(data["contract_id"]))
                cursor = conn.execute("INSERT INTO payment_nodes(contract_id, name, amount, ratio, planned_date, trigger_milestone_id, status) VALUES (?, ?, ?, ?, ?, ?, '未触发')",
                    (data["contract_id"], data["name"], round(dynamic_amount*ratio/100,2), ratio, data.get("planned_date"), data.get("trigger_milestone_id") or None))
                log(conn, "payment_node", cursor.lastrowid, "新增付款节点", data["name"])
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/receipts":
                if not data.get("payment_node_id") or not data.get("amount") or not data.get("received_date"):
                    return self.api_error("请填写付款节点、收款日期和金额")
                voucher = save_upload(data.get("voucher_name", ""), data.get("voucher_data", ""))
                cursor = conn.execute("INSERT INTO receipts(payment_node_id, received_date, amount, voucher_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (data["payment_node_id"], data["received_date"], float(data["amount"]), voucher, now))
                node=conn.execute("SELECT contract_id, amount, ratio, name FROM payment_nodes WHERE id=?", (data["payment_node_id"],)).fetchone()
                expected=round(contract_dynamic_amount(conn,node["contract_id"])*node["ratio"]/100,2) if node and node["ratio"] is not None else node["amount"]
                conn.execute("UPDATE payment_nodes SET status='已收款' WHERE id=? AND (SELECT COALESCE(SUM(amount),0) FROM receipts WHERE payment_node_id=?) >= ?", (data["payment_node_id"], data["payment_node_id"], expected))
                if node and node["name"] == "质保金":
                    project_row=conn.execute("SELECT project_id FROM contracts WHERE id=?",(node["contract_id"],)).fetchone()
                    sub_nodes=conn.execute("SELECT n.id, s.vendor FROM sub_payment_nodes n JOIN subcontracts s ON s.id=n.subcontract_id WHERE s.project_id=? AND n.name='质保金' AND n.status='未付款'",(project_row["project_id"],)).fetchall()
                    for sub_node in sub_nodes:
                        conn.execute("UPDATE sub_payment_nodes SET status='待付款' WHERE id=?",(sub_node["id"],))
                        conn.execute("INSERT INTO project_alerts(project_id, alert_type, message, created_at) VALUES (?, '分包质保金付款提醒', ?, ?)",(project_row["project_id"],f"已收到甲方质保金，请向分包方 {sub_node['vendor']} 支付对应质保金",now))
                log(conn, "receipt", cursor.lastrowid, "确认收款", str(data["amount"]))
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/project-labor-rates":
                required=("project_id","trade","skill_level","daily_rate")
                if any(str(data.get(key,"")).strip()=="" for key in required):return self.api_error("请完整填写项目专属人工价")
                conn.execute("INSERT INTO project_labor_rates(project_id,trade,skill_level,daily_rate,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(project_id,trade,skill_level) DO UPDATE SET daily_rate=excluded.daily_rate,updated_at=excluded.updated_at",(data["project_id"],str(data["trade"]).strip(),data["skill_level"],float(data["daily_rate"]),now))
                conn.commit();conn.close();return self.json_response({"ok":True},HTTPStatus.CREATED)
            if self.path == "/api/labor-report-batches":
                project_id=data.get("project_id");report_date=data.get("report_date");lines=data.get("lines") or []
                if not project_id or not report_date or not lines:return self.api_error("请选择项目、日期并至少添加一个工种")
                if not conn.execute("SELECT 1 FROM projects WHERE id=? AND source_lead_id IS NOT NULL",(project_id,)).fetchone():return self.api_error("项目不存在")
                duplicates=[]
                for line in lines:
                    if not line.get("trade") or float(line.get("workers") or 0)<=0 or float(line.get("work_days") or 0)<=0:return self.api_error("请完整填写每个工种的人数和工作天数")
                    exists=conn.execute("SELECT id FROM labor_reports WHERE project_id=? AND report_date=? AND trade=? AND skill_level=? AND status!='已驳回'",(project_id,report_date,line["trade"],line.get("skill_level") or "大工")).fetchone()
                    if exists:duplicates.append(f"{line['trade']}（{line.get('skill_level') or '大工'}）")
                if duplicates and not data.get("force_duplicate"):return self.api_error("该项目当日以下工种已填报："+"、".join(duplicates),HTTPStatus.CONFLICT)
                batch_code=f"LR-{datetime.now():%y%m%d%H%M%S}";ids=[]
                for line in lines:
                    trade=str(line["trade"]).strip();skill=line.get("skill_level") or "大工";rate=line.get("daily_rate")
                    if str(rate or "").strip()=="":
                        project_rate=conn.execute("SELECT daily_rate FROM project_labor_rates WHERE project_id=? AND trade=? AND skill_level=?",(project_id,trade,skill)).fetchone()
                        global_rate=conn.execute("SELECT daily_rate FROM labor_rate_catalog WHERE (trade_name=? OR trade=?) AND skill_level=? AND pricing_mode='按天' ORDER BY id DESC LIMIT 1",(trade,trade,skill)).fetchone()
                        selected=project_rate or global_rate
                        if not selected:return self.api_error(f"工种“{trade}（{skill}）”未维护人工价格")
                        rate=selected["daily_rate"]
                    cursor=conn.execute("INSERT INTO labor_reports(project_id,report_date,trade,workers,hours,hourly_rate,status,created_at,pricing_mode,work_days,daily_rate,direct_amount,batch_code,skill_level,team_name,leader,reporter) VALUES (?,?,?,?,0,0,'待审核',?,'按天',?,?,0,?,?,?,?, '管理员')",(project_id,report_date,trade,int(line["workers"]),now,float(line["work_days"]),float(rate),batch_code,skill,data.get("team_name",""),data.get("leader","")))
                    ids.append(cursor.lastrowid);log(conn,"labor_report",cursor.lastrowid,"批量新增人力日报",f"{trade}（{skill}）")
                conn.commit();conn.close();return self.json_response({"batch_code":batch_code,"ids":ids,"duplicates":duplicates},HTTPStatus.CREATED)
            if self.path == "/api/labor-reports":
                required = ("project_id", "report_date", "trade")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写项目、日期和工种")
                status = "待审核"
                pricing_mode=data.get("pricing_mode","按天")
                if pricing_mode == "按天" and any(str(data.get(key,"")).strip()=="" for key in ("workers","work_days","daily_rate")):
                    return self.api_error("按天计价请填写人数、天数和日单价")
                if pricing_mode == "直接金额" and str(data.get("direct_amount","")).strip()=="":
                    return self.api_error("请填写人工总金额")
                cursor = conn.execute("INSERT INTO labor_reports(project_id, report_date, trade, workers, hours, hourly_rate, status, created_at, contract_change_id, pricing_mode, work_days, daily_rate, direct_amount) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (data["project_id"], data["report_date"], data["trade"], int(data.get("workers") or 0), status, now, data.get("contract_change_id") or None, pricing_mode, float(data.get("work_days") or 0), float(data.get("daily_rate") or 0), float(data.get("direct_amount") or 0)))
                log(conn, "labor_report", cursor.lastrowid, "新增人力日报", data["trade"])
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/material-catalog":
                required=("material_code","material_name","unit","base_price")
                if any(str(data.get(key," ")).strip()=="" for key in required):
                    return self.api_error("请填写材料编码、名称、单位和基准单价")
                material_code=str(data["material_code"]).strip()
                if conn.execute("SELECT 1 FROM material_catalog WHERE material_code=?",(material_code,)).fetchone():
                    conn.close();return self.api_error("材料编码已存在，请使用其他编码",HTTPStatus.CONFLICT)
                cursor=conn.execute("INSERT INTO material_catalog(material_code, material_name, specification, unit, base_price, created_at, category, primary_category, subcategory, loss_rate, supplier, last_purchase_price, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",(material_code,str(data["material_name"]).strip(),data.get("specification",""),str(data["unit"]).strip(),float(data["base_price"]),now,data.get("subcategory") or data.get("category",""),data.get("primary_category",""),data.get("subcategory") or data.get("category",""),float(data.get("loss_rate") or 0),data.get("supplier",""),float(data.get("last_purchase_price") or 0),data.get("note","")))
                conn.commit();conn.close()
                return self.json_response({"id":cursor.lastrowid},HTTPStatus.CREATED)
            if self.path == "/api/labor-rates":
                pricing_mode=str(data.get("pricing_mode") or "按天").strip()
                if not data.get("trade") or (pricing_mode=="按天" and str(data.get("daily_rate","")).strip()=="") or (pricing_mode!="按天" and str(data.get("piece_rate","")).strip()==""):
                    return self.api_error("请填写工种和对应计价单价")
                trade_name=str(data["trade"]).strip();skill_level=str(data.get("skill_level") or "大工").strip();piece_unit=str(data.get("piece_unit") or "").strip();trade_key=f"{trade_name}（{skill_level}）" if pricing_mode=="按天" else f"{trade_name}（{skill_level}-{pricing_mode}）"
                conn.execute("INSERT INTO labor_rate_catalog(trade, hourly_rate, created_at, pricing_mode, daily_rate, trade_name, skill_level, note, piece_unit, piece_rate) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(trade) DO UPDATE SET pricing_mode=excluded.pricing_mode, daily_rate=excluded.daily_rate, trade_name=excluded.trade_name, skill_level=excluded.skill_level, note=excluded.note, piece_unit=excluded.piece_unit, piece_rate=excluded.piece_rate",(trade_key,now,pricing_mode,float(data.get("daily_rate") or 0),trade_name,skill_level,data.get("note",""),piece_unit,float(data.get("piece_rate") or 0)))
                conn.commit();conn.close()
                return self.json_response({"ok":True})
            if self.path == "/api/equipment-costs":
                required = ("project_id", "name", "amount", "cost_date")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写项目、机械费用名称、金额和日期")
                invoice_name = save_upload(data.get("invoice_name", ""), data.get("invoice_data", ""))
                cursor = conn.execute("INSERT INTO equipment_costs(project_id, name, amount, cost_date, invoice_name, created_at, contract_change_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (data["project_id"], data["name"], float(data["amount"]), data["cost_date"], invoice_name, now, data.get("contract_change_id") or None))
                log(conn, "equipment_cost", cursor.lastrowid, "录入机械费用", data["name"])
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/subcontracts":
                required = ("project_id", "vendor", "scope", "amount")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写项目、分包方、分包内容和合同金额")
                number = conn.execute("SELECT COUNT(*) FROM subcontracts").fetchone()[0] + 1
                code = f"FB-{datetime.now():%y%m}-{number:04d}"
                file_name = save_upload(data.get("file_name", ""), data.get("file_data", ""))
                cursor = conn.execute("INSERT INTO subcontracts(code, project_id, vendor, scope, amount, signed_date, completion_date, warranty_rate, warranty_months, status, file_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '已签署', ?, ?)",
                    (code, data["project_id"], data["vendor"], data["scope"], float(data["amount"]), data.get("signed_date"), data.get("completion_date"), float(data.get("warranty_rate") or 5), int(data.get("warranty_months") or 12), file_name, now))
                warranty_amount = round(float(data["amount"]) * float(data.get("warranty_rate") or 5) / 100, 2)
                warranty_date = add_months(data.get("completion_date"), int(data.get("warranty_months") or 12))
                conn.execute("INSERT INTO sub_payment_nodes(subcontract_id, name, amount, planned_date, status) VALUES (?, '质保金', ?, ?, '未付款')", (cursor.lastrowid, warranty_amount, warranty_date))
                log(conn, "subcontract", cursor.lastrowid, "新建分包合同", code)
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid,"code":code}, HTTPStatus.CREATED)
            if self.path == "/api/sub-payment-nodes":
                required = ("subcontract_id", "name", "amount")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写分包合同、付款节点和金额")
                cursor = conn.execute("INSERT INTO sub_payment_nodes(subcontract_id, name, amount, planned_date, status) VALUES (?, ?, ?, ?, '未付款')", (data["subcontract_id"], data["name"], float(data["amount"]), data.get("planned_date")))
                log(conn, "sub_payment_node", cursor.lastrowid, "新增分包付款节点", data["name"])
                conn.commit(); conn.close()
                return self.json_response({"id":cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/contract-changes":
                required=("contract_id","title","change_type","amount")
                if any(str(data.get(key,"")).strip()=="" for key in required):
                    return self.api_error("请填写主合同、变更名称、增减类型和金额")
                number=conn.execute("SELECT COUNT(*) FROM contract_changes").fetchone()[0]+1
                code=f"BG-{datetime.now():%y%m}-{number:04d}"
                file_name=save_upload(data.get("file_name",""),data.get("file_data",""))
                cursor=conn.execute("INSERT INTO contract_changes(contract_id, code, title, change_type, amount, signed_date, status, file_name, created_at) VALUES (?, ?, ?, ?, ?, ?, '已生效', ?, ?)",
                    (data["contract_id"],code,data["title"],data["change_type"],float(data["amount"]),data.get("signed_date"),file_name,now))
                log(conn,"contract_change",cursor.lastrowid,"新增合同变更",code)
                conn.commit();conn.close()
                return self.json_response({"id":cursor.lastrowid,"code":code},HTTPStatus.CREATED)
            if self.path == "/api/milestones":
                required = ("project_id", "name", "planned_date")
                if any(not data.get(key) for key in required):
                    return self.api_error("项目、关键节点和计划日期为必填项")
                cursor = conn.execute("INSERT INTO milestones(project_id, name, planned_date, progress, status) VALUES (?, ?, ?, 0, '未开始')", (data["project_id"], data["name"], data["planned_date"]))
                log(conn, "milestone", cursor.lastrowid, "新增关键节点", data["name"])
                conn.commit(); conn.close()
                return self.json_response({"id": cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/bom-items":
                required = ("project_id", "material_code", "material_name", "unit", "design_quantity", "unit_price")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写项目、材料编码、名称、单位、用量和单价")
                cursor = conn.execute("INSERT INTO bom_items(project_id, material_code, material_name, specification, unit, design_quantity, loss_rate, unit_price, created_at, contract_change_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["project_id"], data["material_code"], data["material_name"], data.get("specification", ""), data["unit"], float(data["design_quantity"]), float(data.get("loss_rate") or 0), float(data["unit_price"]), now, data.get("contract_change_id") or None))
                log(conn, "bom_item", cursor.lastrowid, "新增材料", data["material_name"])
                conn.commit(); conn.close()
                return self.json_response({"id": cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/invoices":
                required = ("project_id", "invoice_type", "invoice_number", "amount")
                if any(str(data.get(key, "")).strip() == "" for key in required):
                    return self.api_error("请填写项目、费用类型、发票号码和金额")
                file_name=save_upload(data.get("file_name", ""), data.get("file_data", ""))
                cursor = conn.execute("INSERT INTO cost_invoices(project_id, bom_item_id, invoice_type, invoice_number, amount, file_name, created_at, contract_change_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["project_id"], data.get("bom_item_id") or None, data["invoice_type"], data["invoice_number"], float(data["amount"]), file_name, now, data.get("contract_change_id") or None))
                log(conn, "invoice", cursor.lastrowid, "录入发票", data["invoice_number"])
                conn.commit(); conn.close()
                return self.json_response({"id": cursor.lastrowid}, HTTPStatus.CREATED)
            if self.path == "/api/work-orders":
                required = ("project_id", "title", "description", "work_type", "assignee")
                if any(not str(data.get(key, "")).strip() for key in required):
                    return self.api_error("请选择项目并填写问题标题、问题描述、类型和责任人")
                photos=data.get("photos") or []
                if not photos and not (data.get("photo_name") and data.get("photo_data")):
                    return self.api_error("请至少上传 1 张现场图片")
                if not conn.execute("SELECT 1 FROM projects WHERE id=? AND source_lead_id IS NOT NULL",(data["project_id"],)).fetchone():
                    return self.api_error("所属项目不存在")
                number = conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0] + 1
                code = f"WO-{datetime.now():%y%m}-{number:04d}"
                photo_name=save_upload(data.get("photo_name", ""), data.get("photo_data", ""))
                cursor = conn.execute("INSERT INTO work_orders(code, project_id, title, description, work_type, assignee, reporter, status, due_at, created_at, updated_at, contract_change_id, photo_name) VALUES (?, ?, ?, ?, ?, ?, '管理员', '待派单', ?, ?, ?, ?, ?)",
                    (code, data["project_id"], data["title"], data["description"], data["work_type"], data["assignee"], data.get("due_at") or now, now, now, data.get("contract_change_id") or None, photo_name))
                work_id=cursor.lastrowid
                if photo_name:
                    conn.execute("INSERT INTO work_order_photos(work_order_id,stage,file_name,original_name,uploaded_by,created_at) VALUES (?, '整改前', ?, ?, '管理员', ?)",(work_id,photo_name,data.get("photo_name"),now))
                for photo in photos:
                    stored=save_upload(photo.get("file_name",""),photo.get("file_data",""))
                    if stored:conn.execute("INSERT INTO work_order_photos(work_order_id,stage,file_name,original_name,uploaded_by,created_at) VALUES (?, '整改前', ?, ?, '管理员', ?)",(work_id,stored,photo.get("file_name"),now))
                log(conn, "work_order", work_id, "新建工单", data["title"])
                conn.commit(); conn.close()
                return self.json_response({"id": work_id, "code": code}, HTTPStatus.CREATED)
            if self.path != "/api/leads":
                conn.close()
                return self.api_error("接口不存在", HTTPStatus.NOT_FOUND)
            required = [key for key in ("name", "customer", "owner") if not str(data.get(key, "")).strip()]
            if required:
                return self.api_error("缺少必填字段：" + "、".join(required))
            cursor = conn.execute(
                "INSERT INTO leads(name, customer, area, budget, owner, source, stage, stage_since, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (data["name"].strip(), data["customer"].strip(), data.get("area") or None, data.get("budget") or 0, data["owner"].strip(), data.get("source", ""), now, now),
            )
            log(conn, "lead", cursor.lastrowid, "创建线索", data["name"].strip())
            conn.commit()
            result = dict(conn.execute("SELECT * FROM leads WHERE id=?", (cursor.lastrowid,)).fetchone())
            conn.close()
            return self.json_response(result, HTTPStatus.CREATED)
        except ValueError as error:
            return self.api_error(str(error))

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        segments = parsed.path.strip("/").split("/")
        try:
            data = self.body()
            if len(segments) != 4 or segments[0] != "api":
                return self.api_error("接口不存在", HTTPStatus.NOT_FOUND)
            entity, entity_id, action = segments[1], int(segments[2]), segments[3]
            conn = connection()
            if entity == "leads" and action == "advance":
                lead = conn.execute("SELECT * FROM leads WHERE id=?", (entity_id,)).fetchone()
                if not lead:
                    return self.api_error("线索不存在", HTTPStatus.NOT_FOUND)
                if lead["stage"] >= len(STAGES) - 1:
                    return self.api_error("当前已处于商务谈判，请选择赢单或输单")
                next_stage = lead["stage"] + 1
                conn.execute("UPDATE leads SET stage=?, stage_since=? WHERE id=?", (next_stage, datetime.now().isoformat(timespec="seconds"), entity_id))
                log(conn, "lead", entity_id, "推进阶段", f"{STAGES[lead['stage']]} → {STAGES[next_stage]}")
            elif entity == "leads" and action == "lose":
                if not data.get("lost_reason"):
                    return self.api_error("请选择未成交原因")
                conn.execute("UPDATE leads SET status='未成交', lost_reason=?, lost_note=? WHERE id=?", (data["lost_reason"], data.get("lost_note", ""), entity_id))
                log(conn, "lead", entity_id, "标记未成交", data["lost_reason"])
            elif entity == "leads" and action == "restore":
                lead=conn.execute("SELECT status, deleted_from_status FROM leads WHERE id=?",(entity_id,)).fetchone()
                if not lead:
                    return self.api_error("线索不存在",HTTPStatus.NOT_FOUND)
                restored_status=lead["deleted_from_status"] or "进行中"
                conn.execute("UPDATE leads SET status=?, deleted_at=NULL, deleted_from_status=NULL, stage_since=? WHERE id=?", (restored_status, datetime.now().isoformat(timespec="seconds"), entity_id))
                log(conn, "lead", entity_id, "恢复线索", f"恢复为{restored_status}")
            elif entity == "leads" and action == "back":
                lead=conn.execute("SELECT stage FROM leads WHERE id=?",(entity_id,)).fetchone()
                if not lead:return self.api_error("线索不存在",HTTPStatus.NOT_FOUND)
                if lead["stage"]<=0:return self.api_error("已经是第一个阶段")
                conn.execute("UPDATE leads SET stage=?,stage_since=? WHERE id=?",(lead["stage"]-1,datetime.now().isoformat(timespec="seconds"),entity_id));log(conn,"lead",entity_id,"返回上一阶段",f"{STAGES[lead['stage']]} → {STAGES[lead['stage']-1]}")
            elif entity == "leads" and action == "win":
                lead = conn.execute("SELECT * FROM leads WHERE id=?", (entity_id,)).fetchone()
                if not lead:
                    return self.api_error("线索不存在", HTTPStatus.NOT_FOUND)
                count = conn.execute("SELECT COUNT(*) FROM projects WHERE code LIKE ?", (f"XM-{datetime.now():%y%m}-%",)).fetchone()[0] + 1
                code = f"XM-{datetime.now():%y%m}-{count:04d}"
                cursor = conn.execute("INSERT INTO projects(code, name, sales_owner, manager, contract_amount, progress, status, bom_version, created_at, source_lead_id) VALUES (?, ?, ?, ?, ?, 0, '待建合同', 'V0.1', ?, ?)",
                    (code, lead["name"], lead["owner"], data.get("manager", "待分配"), lead["budget"] or 0, datetime.now().isoformat(timespec="seconds"), entity_id))
                conn.execute("UPDATE leads SET status='已赢单' WHERE id=?", (entity_id,))
                log(conn, "lead", entity_id, "赢单立项", code)
                log(conn, "project", cursor.lastrowid, "自动创建项目", f"来源线索 {lead['name']}")
            elif entity == "milestones" and action == "progress":
                progress = max(0, min(100, int(data.get("progress", 0))))
                status = "已完成" if progress == 100 else "进行中" if progress > 0 else "未开始"
                conn.execute("UPDATE milestones SET progress=?, status=? WHERE id=?", (progress, status, entity_id))
                milestone = conn.execute("SELECT project_id, name FROM milestones WHERE id=?", (entity_id,)).fetchone()
                if milestone:
                    average = conn.execute("SELECT ROUND(AVG(progress)) FROM milestones WHERE project_id=?", (milestone["project_id"],)).fetchone()[0] or 0
                    conn.execute("UPDATE projects SET progress=?, status='施工中' WHERE id=?", (average, milestone["project_id"]))
                    contract = conn.execute("SELECT id FROM contracts WHERE project_id=?", (milestone["project_id"],)).fetchone()
                    if contract and progress == 100:
                        nodes = conn.execute("SELECT id, name FROM payment_nodes WHERE contract_id=? AND trigger_milestone_id=? AND status='未触发'", (contract["id"], entity_id)).fetchall()
                        for node in nodes:
                            conn.execute("UPDATE payment_nodes SET status='待收款' WHERE id=?", (node["id"],))
                            conn.execute("INSERT INTO project_alerts(project_id, alert_type, message, created_at) VALUES (?, '收款提醒', ?, ?)", (milestone["project_id"], f"关键节点“{milestone['name']}”已完成，请跟进{node['name']}收款", datetime.now().isoformat(timespec="seconds")))
                    log(conn, "milestone", entity_id, "更新节点进度", f"{milestone['name']}：{progress}%")
            elif entity == "projects" and action == "complete":
                unfinished = conn.execute("SELECT COUNT(*) FROM milestones WHERE project_id=? AND progress<100", (entity_id,)).fetchone()[0]
                open_orders = conn.execute("SELECT COUNT(*) FROM work_orders WHERE project_id=? AND status NOT IN ('已销账','已完成')", (entity_id,)).fetchone()[0]
                if unfinished or open_orders:
                    return self.api_error("仍有未完成关键节点或未闭环工单，不能标记完工")
                conn.execute("UPDATE projects SET progress=100, status='已完工' WHERE id=?", (entity_id,))
                contract = conn.execute("SELECT id, amount, warranty_rate, warranty_months FROM contracts WHERE project_id=?", (entity_id,)).fetchone()
                if contract:
                    exists = conn.execute("SELECT 1 FROM payment_nodes WHERE contract_id=? AND name='质保金'", (contract["id"],)).fetchone()
                    if not exists:
                        completion = conn.execute("SELECT acceptance_date FROM contracts WHERE id=?", (contract["id"],)).fetchone()[0] or conn.execute("SELECT MAX(planned_date) FROM milestones WHERE project_id=?", (entity_id,)).fetchone()[0]
                        amount = round(contract["amount"] * contract["warranty_rate"] / 100, 2)
                        planned = add_months(completion, contract["warranty_months"])
                        cursor = conn.execute("INSERT INTO payment_nodes(contract_id, name, amount, planned_date, status) VALUES (?, '质保金', ?, ?, '未触发')", (contract["id"], amount, planned))
                        log(conn, "payment_node", cursor.lastrowid, "自动生成质保金", f"{contract['warranty_rate']}%，{contract['warranty_months']}个月后催收")
                log(conn, "project", entity_id, "标记完工", "关键节点与工单均已闭环")
            elif entity == "contracts" and action == "acceptance":
                acceptance_date=data.get("acceptance_date")
                if not acceptance_date:
                    return self.api_error("请填写竣工验收日期")
                contract=conn.execute("SELECT warranty_months FROM contracts WHERE id=?",(entity_id,)).fetchone()
                if not contract:
                    return self.api_error("合同不存在",HTTPStatus.NOT_FOUND)
                conn.execute("UPDATE contracts SET acceptance_date=? WHERE id=?",(acceptance_date,entity_id))
                warranty_date=add_months(acceptance_date,contract["warranty_months"])
                conn.execute("UPDATE payment_nodes SET planned_date=? WHERE contract_id=? AND name='质保金' AND status!='已收款'",(warranty_date,entity_id))
                log(conn,"contract",entity_id,"更新竣工验收日",acceptance_date)
            elif entity == "sub-payment-nodes" and action == "paid":
                node=conn.execute("SELECT * FROM sub_payment_nodes WHERE id=?",(entity_id,)).fetchone()
                if not node:
                    return self.api_error("分包付款节点不存在",HTTPStatus.NOT_FOUND)
                conn.execute("UPDATE sub_payment_nodes SET status='已付款' WHERE id=?",(entity_id,))
                log(conn,"sub_payment_node",entity_id,"确认付款",node["name"])
            elif entity == "work-orders" and action == "advance":
                work_order=conn.execute("SELECT status FROM work_orders WHERE id=?",(entity_id,)).fetchone()
                if not work_order:
                    return self.api_error("工单不存在",HTTPStatus.NOT_FOUND)
                next_status={"待派发":"处理中","待派单":"处理中","处理中":"待验收"}.get(work_order["status"])
                if not next_status:
                    return self.api_error("当前状态不能推进")
                if work_order["status"]=="处理中":
                    if not str(data.get("resolution_note","")).strip():return self.api_error("请填写处理说明")
                    photos=data.get("photos") or []
                    if not photos:return self.api_error("请至少上传 1 张整改后图片")
                    conn.execute("UPDATE work_orders SET resolution_note=? WHERE id=?",(data["resolution_note"],entity_id))
                    for photo in photos:
                        stored=save_upload(photo.get("file_name",""),photo.get("file_data",""))
                        if stored:conn.execute("INSERT INTO work_order_photos(work_order_id,stage,file_name,original_name,uploaded_by,created_at) VALUES (?, '整改后', ?, ?, '管理员', ?)",(entity_id,stored,photo.get("file_name"),datetime.now().isoformat(timespec="seconds")))
                conn.execute("UPDATE work_orders SET status=?, updated_at=? WHERE id=?",(next_status,datetime.now().isoformat(timespec="seconds"),entity_id))
                log(conn,"work_order",entity_id,"推进工单",f"{work_order['status']} → {next_status}")
            elif entity == "work-orders" and action == "reject":
                reason=str(data.get("reject_reason","")).strip()
                if not reason:return self.api_error("验收驳回必须填写原因")
                work_order=conn.execute("SELECT status FROM work_orders WHERE id=?",(entity_id,)).fetchone()
                if not work_order or work_order["status"]!="待验收":return self.api_error("仅待验收工单可以驳回")
                conn.execute("UPDATE work_orders SET status='处理中', reject_reason=?, updated_at=? WHERE id=?",(reason,datetime.now().isoformat(timespec="seconds"),entity_id))
                log(conn,"work_order",entity_id,"验收驳回",reason)
            elif entity == "work-orders" and action == "close":
                photos=data.get("photos") or []
                if not photos and not data.get("closure_photo"):return self.api_error("验收必须上传验收图片")
                work_order = conn.execute("SELECT status FROM work_orders WHERE id=?", (entity_id,)).fetchone()
                if not work_order:
                    return self.api_error("工单不存在", HTTPStatus.NOT_FOUND)
                if work_order["status"] != "待验收":
                    return self.api_error("仅待验收工单可以验收")
                now=datetime.now().isoformat(timespec="seconds");legacy=data.get("closure_photo","")
                conn.execute("UPDATE work_orders SET status='已完成', closure_photo=?, completed_at=?, updated_at=? WHERE id=?", (legacy,now,now,entity_id))
                for photo in photos:
                    stored=save_upload(photo.get("file_name",""),photo.get("file_data",""))
                    if stored:conn.execute("INSERT INTO work_order_photos(work_order_id,stage,file_name,original_name,uploaded_by,created_at) VALUES (?, '验收', ?, ?, '管理员', ?)",(entity_id,stored,photo.get("file_name"),now))
                log(conn, "work_order", entity_id, "验收通过", data.get("note","") or "工单已完成")
            elif entity == "labor-reports" and action == "approve":
                report=conn.execute("SELECT status FROM labor_reports WHERE id=?",(entity_id,)).fetchone()
                if not report:return self.api_error("日报不存在",HTTPStatus.NOT_FOUND)
                if report["status"]=="已审核":return self.api_error("日报已经审核通过")
                now=datetime.now().isoformat(timespec="seconds")
                conn.execute("UPDATE labor_reports SET status='已审核',reviewer='管理员',reviewed_at=?,reject_reason=NULL,rejected_at=NULL WHERE id=?",(now,entity_id))
                log(conn, "labor_report", entity_id, "审核通过", "人工成本计入利润看板")
            elif entity == "labor-reports" and action == "reject":
                reason=str(data.get("reject_reason","")).strip()
                if not reason:return self.api_error("驳回必须填写原因")
                report=conn.execute("SELECT status FROM labor_reports WHERE id=?",(entity_id,)).fetchone()
                if not report:return self.api_error("日报不存在",HTTPStatus.NOT_FOUND)
                if report["status"]=="已审核":return self.api_error("已审核日报不能直接驳回")
                now=datetime.now().isoformat(timespec="seconds")
                conn.execute("UPDATE labor_reports SET status='已驳回',reviewer='管理员',reject_reason=?,rejected_at=? WHERE id=?",(reason,now,entity_id))
                log(conn,"labor_report",entity_id,"审核驳回",reason)
            elif entity == "labor-reports" and action == "update":
                required=("report_date","trade","status","pricing_mode")
                if any(str(data.get(key," ")).strip()=="" for key in required):
                    return self.api_error("请完整填写日报字段")
                current=conn.execute("SELECT status FROM labor_reports WHERE id=?",(entity_id,)).fetchone()
                if not current:return self.api_error("日报不存在",HTTPStatus.NOT_FOUND)
                conn.execute("UPDATE labor_reports SET report_date=?, trade=?, skill_level=?, team_name=?, leader=?, workers=?, status='待审核', pricing_mode=?, work_days=?, daily_rate=?, direct_amount=?, reviewer=NULL, reviewed_at=NULL, reject_reason=NULL, rejected_at=NULL WHERE id=?",(data["report_date"],data["trade"],data.get("skill_level","大工"),data.get("team_name",""),data.get("leader",""),int(data.get("workers") or 0),data["pricing_mode"],float(data.get("work_days") or 0),float(data.get("daily_rate") or 0),float(data.get("direct_amount") or 0),entity_id))
                log(conn,"labor_report",entity_id,"编辑日报",data["trade"])
            elif entity == "contracts" and action == "update":
                contract=conn.execute("SELECT project_id FROM contracts WHERE id=?",(entity_id,)).fetchone()
                if not contract:return self.api_error("合同不存在",HTTPStatus.NOT_FOUND)
                if not data.get("party_a") or not data.get("amount"):return self.api_error("甲方和合同金额为必填项")
                conn.execute("UPDATE contracts SET party_a=?,amount=?,signed_date=?,completion_date=?,status=? WHERE id=?",(data["party_a"],float(data["amount"]),data.get("signed_date"),data.get("completion_date"),data.get("status","已签署"),entity_id));conn.execute("UPDATE projects SET contract_amount=? WHERE id=?",(float(data["amount"]),contract["project_id"]));log(conn,"contract",entity_id,"编辑合同",str(data["amount"]))
            elif entity == "receipts" and action == "voucher":
                if not conn.execute("SELECT 1 FROM receipts WHERE id=?",(entity_id,)).fetchone():return self.api_error("收款记录不存在",HTTPStatus.NOT_FOUND)
                voucher=save_upload(data.get("file_name",""),data.get("file_data",""))
                if not voucher:return self.api_error("请选择凭证文件")
                conn.execute("UPDATE receipts SET voucher_name=? WHERE id=?",(voucher,entity_id));log(conn,"receipt",entity_id,"补传收款凭证",voucher)
            elif entity == "invoice-ledger" and action == "status":
                current=conn.execute("SELECT * FROM invoice_ledger WHERE id=?",(entity_id,)).fetchone()
                if not current:return self.api_error("发票不存在",HTTPStatus.NOT_FOUND)
                certification=data.get("certification_status",current["certification_status"] or "未认证");deduction=data.get("deduction_status",current["deduction_status"] or "未申报");booked=data.get("booked_status",current["booked_status"] or "未入账")
                certified_date=current["certified_date"]
                if "certification_status" in data and certification != current["certification_status"]:
                    certified_date=datetime.now().date().isoformat() if certification=="已认证" else None
                booked_date=current["booked_date"]
                if "booked_status" in data and booked != current["booked_status"]:
                    booked_date=datetime.now().date().isoformat() if booked=="已入账" else None
                conn.execute("UPDATE invoice_ledger SET certification_status=?,certified_date=?,deduction_status=?,booked_status=?,booked_date=? WHERE id=?",(certification,certified_date,deduction,booked,booked_date,entity_id));log(conn,"invoice",entity_id,"更新财税状态",f"{certification}/{deduction}/{booked}")
            elif entity == "invoice-ledger" and action == "project":
                conn.execute("UPDATE invoice_ledger SET project_id=? WHERE id=?",(data.get("project_id") or None,entity_id));log(conn,"invoice",entity_id,"修改项目归属",str(data.get("project_id") or "非项目"))
            elif entity == "bom-items" and action == "update":
                conn.execute("UPDATE bom_items SET material_code=?,material_name=?,specification=?,unit=?,design_quantity=?,loss_rate=?,unit_price=? WHERE id=?",(data["material_code"],data["material_name"],data.get("specification",""),data["unit"],float(data["design_quantity"]),float(data.get("loss_rate") or 0),float(data["unit_price"]),entity_id));log(conn,"bom_item",entity_id,"修改BOM材料",data["material_name"])
            elif entity == "material-catalog" and action == "update":
                required=("material_code","material_name","unit","base_price")
                if any(str(data.get(key," ")).strip()=="" for key in required):
                    conn.close();return self.api_error("请填写材料编码、名称、单位和基准单价")
                if not conn.execute("SELECT 1 FROM material_catalog WHERE id=?",(entity_id,)).fetchone():
                    conn.close();return self.api_error("材料不存在",HTTPStatus.NOT_FOUND)
                material_code=str(data["material_code"]).strip()
                if conn.execute("SELECT 1 FROM material_catalog WHERE material_code=? AND id<>?",(material_code,entity_id)).fetchone():
                    conn.close();return self.api_error("材料编码已存在，请使用其他编码",HTTPStatus.CONFLICT)
                category=data.get("subcategory") or data.get("category","")
                conn.execute("UPDATE material_catalog SET material_code=?,material_name=?,specification=?,unit=?,base_price=?,category=?,primary_category=?,subcategory=?,loss_rate=?,supplier=?,last_purchase_price=?,note=? WHERE id=?",(material_code,str(data["material_name"]).strip(),data.get("specification",""),str(data["unit"]).strip(),float(data["base_price"]),category,data.get("primary_category",""),category,float(data.get("loss_rate") or 0),data.get("supplier",""),float(data.get("last_purchase_price") or 0),data.get("note",""),entity_id))
            elif entity == "bom-cost-items" and action == "update":
                amount=float(data.get("amount") or 0)
                if data.get("cost_type")=="人工费" and data.get("pricing_mode")=="按天":amount=float(data.get("workers") or 0)*float(data.get("work_days") or 0)*float(data.get("daily_rate") or 0)
                conn.execute("UPDATE bom_cost_items SET cost_type=?,name=?,pricing_mode=?,workers=?,work_days=?,daily_rate=?,amount=?,cost_date=?,supplier=?,note=? WHERE id=?",(data["cost_type"],data["name"],data.get("pricing_mode",""),float(data.get("workers") or 0),float(data.get("work_days") or 0),float(data.get("daily_rate") or 0),amount,data.get("cost_date"),data.get("supplier",""),data.get("note",""),entity_id));log(conn,"bom_cost",entity_id,"修改BOM费用",data["name"])
            else:
                return self.api_error("接口不存在", HTTPStatus.NOT_FOUND)
            conn.commit()
            conn.close()
            return self.json_response({"ok": True})
        except ValueError as error:
            return self.api_error(str(error))

    def do_DELETE(self) -> None:
        segments = urlparse(self.path).path.strip("/").split("/")
        if len(segments)==4 and segments[:2]==["api","leads"] and segments[3]=="permanent":
            try:
                lead_id=int(segments[2]);conn=connection();project_ids=[row[0] for row in conn.execute("SELECT id FROM projects WHERE source_lead_id=?",(lead_id,))]
                for project_id in project_ids:delete_project_records(conn,project_id)
                conn.execute("DELETE FROM lead_notes WHERE lead_id=?",(lead_id,))
                conn.execute("DELETE FROM leads WHERE id=?",(lead_id,));conn.commit();conn.close();return self.json_response({"ok":True,"deleted_projects":len(project_ids)})
            except ValueError:return self.api_error("线索编号无效")
        if len(segments) != 3 or segments[0] != "api" or segments[1] not in ("projects", "contracts", "leads", "milestones", "material-catalog", "labor-rates", "labor-reports", "bom-items", "bom-cost-items", "project-files", "lead-notes", "expense-payments", "invoice-ledger"):
            return self.api_error("接口不存在", HTTPStatus.NOT_FOUND)
        try:
            entity_id = int(segments[2])
            conn = connection()
            if segments[1] == "labor-reports":
                report=conn.execute("SELECT status,trade FROM labor_reports WHERE id=?",(entity_id,)).fetchone()
                if not report:conn.close();return self.api_error("日报不存在",HTTPStatus.NOT_FOUND)
                conn.execute("DELETE FROM labor_reports WHERE id=?",(entity_id,))
                log(conn,"labor_report",entity_id,"删除人力日报",f"{report['trade']} · 原状态{report['status']}")
                conn.commit();conn.close();return self.json_response({"ok":True})
            if segments[1] in ("lead-notes","expense-payments","invoice-ledger"):
                table={"lead-notes":"lead_notes","expense-payments":"expense_payments","invoice-ledger":"invoice_ledger"}[segments[1]]
                if table=="expense_payments":
                    row=conn.execute("SELECT voucher_name FROM expense_payments WHERE id=?",(entity_id,)).fetchone();delete_upload(row["voucher_name"] if row else None)
                elif table=="invoice_ledger":
                    row=conn.execute("SELECT file_name FROM invoice_ledger WHERE id=?",(entity_id,)).fetchone();delete_upload(row["file_name"] if row else None)
                cursor=conn.execute(f"DELETE FROM {table} WHERE id=?",(entity_id,));conn.commit();conn.close();return self.json_response({"ok":cursor.rowcount>0})
            if segments[1] == "projects":
                exists=conn.execute("SELECT 1 FROM projects WHERE id=?",(entity_id,)).fetchone()
                if not exists:return self.api_error("项目不存在",HTTPStatus.NOT_FOUND)
                delete_project_records(conn,entity_id);conn.commit();conn.close();return self.json_response({"ok":True})
            if segments[1] == "contracts":
                contract=conn.execute("SELECT project_id FROM contracts WHERE id=?",(entity_id,)).fetchone()
                if not contract:conn.close();return self.api_error("合同不存在",HTTPStatus.NOT_FOUND)
                receipt_count=conn.execute("SELECT COUNT(*) FROM receipts r JOIN payment_nodes n ON n.id=r.payment_node_id WHERE n.contract_id=?",(entity_id,)).fetchone()[0]
                if receipt_count:conn.close();return self.api_error("合同已有收款记录，不能删除")
                invoice_count=conn.execute("SELECT COUNT(*) FROM invoice_ledger WHERE payment_node_id IN (SELECT id FROM payment_nodes WHERE contract_id=?)",(entity_id,)).fetchone()[0]
                if invoice_count:conn.close();return self.api_error("合同付款节点已关联销项发票，请先解除发票关联")
                for row in conn.execute("SELECT file_name FROM contract_changes WHERE contract_id=?",(entity_id,)):delete_upload(row["file_name"])
                row=conn.execute("SELECT file_name FROM contracts WHERE id=?",(entity_id,)).fetchone();delete_upload(row["file_name"] if row else None)
                conn.execute("DELETE FROM payment_nodes WHERE contract_id=?",(entity_id,));conn.execute("DELETE FROM contract_changes WHERE contract_id=?",(entity_id,));conn.execute("DELETE FROM contracts WHERE id=?",(entity_id,));conn.execute("UPDATE projects SET contract_amount=0,status='待建合同' WHERE id=?",(contract["project_id"],));log(conn,"contract",entity_id,"删除合同","");conn.commit();conn.close();return self.json_response({"ok":True})
            if segments[1] == "milestones":
                milestone=conn.execute("SELECT project_id,name FROM milestones WHERE id=?",(entity_id,)).fetchone()
                if not milestone:
                    conn.close();return self.api_error("关键节点不存在",HTTPStatus.NOT_FOUND)
                linked=conn.execute("SELECT COUNT(*) FROM payment_nodes WHERE trigger_milestone_id=?",(entity_id,)).fetchone()[0]
                conn.execute("UPDATE payment_nodes SET trigger_milestone_id=NULL WHERE trigger_milestone_id=?",(entity_id,))
                conn.execute("DELETE FROM milestones WHERE id=?",(entity_id,))
                progress=conn.execute("SELECT COALESCE(ROUND(AVG(progress)),0) FROM milestones WHERE project_id=?",(milestone["project_id"],)).fetchone()[0]
                conn.execute("UPDATE projects SET progress=? WHERE id=?",(progress,milestone["project_id"]))
                log(conn,"milestone",entity_id,"删除关键节点",milestone["name"])
                conn.commit();conn.close();return self.json_response({"ok":True,"unlinked_payment_nodes":linked,"project_progress":progress})
            if segments[1] in ("bom-items","bom-cost-items","project-files"):
                table={"bom-items":"bom_items","bom-cost-items":"bom_cost_items","project-files":"project_files"}[segments[1]]
                if table=="project_files":
                    row=conn.execute("SELECT storage_name FROM project_files WHERE id=?",(entity_id,)).fetchone()
                    if row:delete_upload(row["storage_name"])
                elif table=="bom_cost_items":
                    row=conn.execute("SELECT invoice_name FROM bom_cost_items WHERE id=?",(entity_id,)).fetchone()
                    if row:delete_upload(row["invoice_name"])
                cursor=conn.execute(f"DELETE FROM {table} WHERE id=?",(entity_id,));conn.commit();conn.close();return self.json_response({"ok":cursor.rowcount>0})
            if segments[1] == "material-catalog":
                cursor=conn.execute("DELETE FROM material_catalog WHERE id=?",(entity_id,))
                conn.commit();conn.close()
                return self.json_response({"ok":cursor.rowcount>0})
            if segments[1] == "labor-rates":
                cursor=conn.execute("DELETE FROM labor_rate_catalog WHERE id=?",(entity_id,))
                conn.commit();conn.close()
                return self.json_response({"ok":cursor.rowcount>0})
            if segments[1] == "leads":
                lead=conn.execute("SELECT status FROM leads WHERE id=?",(entity_id,)).fetchone()
                if not lead:
                    return self.api_error("线索不存在", HTTPStatus.NOT_FOUND)
                cursor = conn.execute("UPDATE leads SET deleted_from_status=status, status='已删除', deleted_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), entity_id))
                if cursor.rowcount == 0:
                    return self.api_error("线索不存在", HTTPStatus.NOT_FOUND)
                conn.commit(); conn.close()
                return self.json_response({"ok":True})
            project_id = entity_id
            contract_ids = [row[0] for row in conn.execute("SELECT id FROM contracts WHERE project_id=?", (project_id,))]
            if contract_ids:
                placeholders = ",".join("?" for _ in contract_ids)
                node_ids = [row[0] for row in conn.execute(f"SELECT id FROM payment_nodes WHERE contract_id IN ({placeholders})", contract_ids)]
                if node_ids:
                    node_placeholders = ",".join("?" for _ in node_ids)
                    conn.execute(f"DELETE FROM receipts WHERE payment_node_id IN ({node_placeholders})", node_ids)
                conn.execute(f"DELETE FROM payment_nodes WHERE contract_id IN ({placeholders})", contract_ids)
                conn.execute(f"DELETE FROM contract_changes WHERE contract_id IN ({placeholders})", contract_ids)
                conn.execute("DELETE FROM contracts WHERE project_id=?", (project_id,))
            subcontract_ids=[row[0] for row in conn.execute("SELECT id FROM subcontracts WHERE project_id=?",(project_id,))]
            if subcontract_ids:
                sub_placeholders=",".join("?" for _ in subcontract_ids)
                conn.execute(f"DELETE FROM sub_payment_nodes WHERE subcontract_id IN ({sub_placeholders})",subcontract_ids)
                conn.execute("DELETE FROM subcontracts WHERE project_id=?",(project_id,))
            conn.execute("DELETE FROM cost_invoices WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM equipment_costs WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM bom_items WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM work_orders WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM labor_reports WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM milestones WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_alerts WHERE project_id=?", (project_id,))
            cursor = conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            if cursor.rowcount == 0:
                return self.api_error("项目不存在", HTTPStatus.NOT_FOUND)
            conn.commit(); conn.close()
            return self.json_response({"ok":True})
        except ValueError:
            return self.api_error("项目编号无效")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_database()
    print(f"工装项目管理系统已启动：http://127.0.0.1:{port}")
    if "--open" in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
