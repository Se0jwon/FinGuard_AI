"""Customer self-service and assigned report review."""

import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from rpa.orchestrator_client import OrchestratorError
from services import automation_service as automation, orchestrator_service
from services.auth import _connect, _revoke_session, _session_hash, _verify_csrf, csrf_token, local_enabled
from services.excel_signin import login_tickets
from services.incident_service import IncidentValidationError, RISK_LEVELS

customer = Blueprint("customer", __name__, url_prefix="/customer")
staff = Blueprint("report_staff", __name__, url_prefix="/consultation/customer-reports")

KINDS = {
    "CARD_FRAUD": "카드 부정사용", "VOICE_PHISHING": "보이스피싱",
    "CARD_LOSS": "카드 분실 및 도난", "ACCOUNT_TAKEOVER": "계좌 도용",
    "SMISHING": "스미싱", "OTHER": "기타 금융 상담",
}
STATES = {
    "PENDING_APPROVAL": "상담원 검토 대기", "APPROVED": "RPA 실행 대기",
    "QUEUED": "사고 접수 진행 중", "RUNNING": "사고 접수 진행 중",
    "SUCCEEDED": "사고 접수 완료", "REJECTED": "반려", "FAILED": "처리 실패",
}
REPORT_QUERY = """
    SELECT r.*, a.status, a.incident_id, a.customer_id, a.dispatch_pending,
           c.conversation, u.username AS customer_username, j.job_key, j.job_state, j.error_message
    FROM customer_reports r JOIN automation_requests a USING(request_id)
    JOIN customer_accounts u USING(account_id)
    JOIN consultations c USING(consultation_id)
    LEFT JOIN orchestrator_executions j USING(request_id)
"""
SENSITIVE_NUMBER = re.compile(r"(?<!\d)(?:\d{6}-[1-8]\d{6}|(?:\d[ -]?){12,18}\d)(?!\d)")


def _settings():
    return current_app.extensions["finguard_portal"]


def _page(title, **data):
    return render_template("portal/page.html", title=title, kinds=KINDS, states=STATES, **data)


def _customer_text(text):
    if SENSITIVE_NUMBER.search(text):
        abort(400, "주민등록번호나 카드 전체 번호로 보이는 내용을 지워 주세요.")
    return text


def homepage():
    return _page("금융 피해 신고", view="home")


@customer.errorhandler(400)
@customer.errorhandler(403)
@customer.errorhandler(404)
@customer.errorhandler(503)
@staff.errorhandler(400)
@staff.errorhandler(403)
@staff.errorhandler(404)
def portal_error(error):
    messages = {403: "이 요청을 처리할 권한이 없거나 요청 검증에 실패했습니다.",
                404: "신고를 찾을 수 없습니다."}
    return _page("요청 확인", view="error", error=messages.get(error.code, error.description)), error.code


def _find(conn, report_id, account_id=None):
    row = conn.execute(
        REPORT_QUERY + " WHERE r.report_id=? AND (? IS NULL OR r.account_id=?)",
        (report_id, account_id, account_id),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _event(conn, report_id, action, actor, note=None):
    conn.execute(
        "INSERT INTO customer_report_events(report_id,action,actor_id,note) VALUES (?,?,?,?)",
        (report_id, action, actor, note),
    )


def _events(conn, report_id):
    return conn.execute(
        "SELECT action,note,created_at FROM customer_report_events WHERE report_id=? ORDER BY event_id",
        (report_id,),
    ).fetchall()


@customer.before_request
def customer_identity():
    if not local_enabled():
        abort(503, "고객용 테스트 인증을 설정해 주세요.")
    if request.method == "POST":
        _verify_csrf()
    token = session.get("customer_login_token")
    if token:
        with closing(_connect()) as conn:
            g.customer_account = conn.execute(
                """SELECT a.account_id,a.username,a.customer_id FROM customer_accounts a
                   JOIN customer_sessions s USING(account_id) WHERE s.token_hash=? AND s.expires_at>?""",
                (_session_hash(token), int(time.time())),
            ).fetchone()
    if request.endpoint not in ("customer.register", "customer.login") and not g.get("customer_account"):
        return redirect(url_for("customer.login"))


@customer.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return _page("고객 회원가입", view="register")
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username) or not 12 <= len(password) <= 128:
        return _page("고객 회원가입", view="register", error="계정 ID와 비밀번호 조건을 확인해 주세요."), 400
    account_id = uuid4().hex
    customer_id = f"TEST-{account_id}"
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("INSERT INTO customers(customer_id,name) VALUES (?,?)", (customer_id, username))
            conn.execute(
                "INSERT INTO customer_accounts(account_id,username,customer_id,password_hash) VALUES (?,?,?,?)",
                (account_id, username, customer_id, generate_password_hash(password)),
            )
    except sqlite3.IntegrityError:
        return _page("고객 회원가입", view="register", error="사용할 수 없는 계정 ID입니다."), 409
    return redirect(url_for("customer.login"))


@customer.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return _page("고객 로그인", view="login")
    username, password = request.form.get("username", "").strip().lower(), request.form.get("password", "")
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT account_id,password_hash FROM customer_accounts WHERE username=?", (username,),
        ).fetchone() if len(username) <= 64 else None
        stored = row["password_hash"] if row else current_app.extensions["finguard_dummy_hash"]
        if not 0 < len(password) <= 128 or not check_password_hash(stored, password) or not row:
            return _page("고객 로그인", view="login", error="로그인 정보를 확인해 주세요."), 401
        _revoke(conn)
        _revoke_session(conn)
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        conn.execute("DELETE FROM customer_sessions WHERE expires_at<=?", (now,))
        conn.execute("INSERT INTO customer_sessions VALUES (?,?,?)",
                     (_session_hash(token), row["account_id"],
                      now + int(current_app.permanent_session_lifetime.total_seconds())))
    session.clear()
    session.permanent = True
    session["customer_login_token"] = token
    csrf_token()
    return redirect(url_for("customer.history"))


def _revoke(conn):
    token = session.get("customer_login_token")
    if token:
        conn.execute("DELETE FROM customer_sessions WHERE token_hash=?", (_session_hash(token),))


@customer.post("/logout")
def logout():
    with closing(_connect()) as conn, conn:
        _revoke(conn)
    session.clear()
    return redirect(url_for("customer.login"))


@customer.route("/advice", methods=["GET", "POST"])
def advice():
    if request.method == "POST":
        conversation = request.form.get("conversation", "").strip()
        if not 1 <= len(conversation) <= 10000:
            abort(400, "상담 내용은 1~10000자로 입력해 주세요.")
        result = _settings()["analyze"](_customer_text(conversation))
        advice_id = uuid4().hex
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO customer_advice(advice_id,account_id,conversation,reply) VALUES (?,?,?,?)",
                (advice_id, g.customer_account["account_id"], conversation,
                 result["suggested_reply"] if result else None),
            )
        return redirect(url_for("customer.advice_detail", advice_id=advice_id), code=303)
    return _page("AI 금융 상담", view="advice")


@customer.get("/history/advice/<advice_id>")
def advice_detail(advice_id):
    with closing(_connect()) as conn:
        entry = conn.execute(
            "SELECT conversation,reply FROM customer_advice WHERE advice_id=? AND account_id=?",
            (advice_id, g.customer_account["account_id"]),
        ).fetchone()
    if entry is None:
        abort(404)
    return _page("AI 상담 상세", view="advice-detail", entry=entry)


@customer.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "GET":
        return _page("금융 피해 신고", view="report", report_key=uuid4().hex)
    account = g.customer_account
    form = request.form
    kind, occurred, amount = form.get("kind"), form.get("occurred_at") or None, form.get("amount") or None
    conversation, key = form.get("conversation", "").strip(), form.get("report_key", "")
    if kind not in KINDS or not 1 <= len(conversation) <= 10000 or not re.fullmatch(r"[a-f0-9]{32}", key):
        abort(400, "신고 유형과 신고 내용을 확인해 주세요.")
    _customer_text(conversation)
    if amount is not None:
        if not re.fullmatch(r"[0-9]{1,12}", amount):
            abort(400, "피해 금액은 0 이상의 원 단위 숫자로 입력해 주세요.")
        amount = int(amount)
    if occurred is not None:
        try:
            occurred = datetime.strptime(occurred, "%Y-%m-%dT%H:%M").isoformat(timespec="minutes")
        except ValueError:
            abort(400, "피해 발생 시각을 확인해 주세요.")

    identity = dict(actor=f"customer:{account['account_id']}", idempotency_key=key,
                    customer_id=account["customer_id"], conversation=conversation)
    report_id = f"RPT-{uuid4().hex}"

    def check_retry(conn, request_id):
        row = conn.execute(
            "SELECT kind,occurred_at,amount FROM customer_reports WHERE request_id=? AND account_id=?",
            (request_id, account["account_id"]),
        ).fetchone()
        if row is None or tuple(row) != (kind, occurred, amount):
            raise automation.AutomationConflict("같은 신고 제출 키에 다른 신고 정보를 사용할 수 없습니다.")

    def link_report(conn, request_id):
        conn.execute(
            """INSERT INTO customer_reports(report_id,account_id,request_id,kind,occurred_at,amount)
               VALUES (?,?,?,?,?,?)""",
            (report_id, account["account_id"], request_id, kind, occurred, amount),
        )
        _event(conn, report_id, "FILED", identity["actor"])

    with closing(_connect()) as conn:
        retry = automation.find_retry(conn, **identity)
        if retry:
            check_retry(conn, retry["request_id"])
            result = retry
        else:
            ai = _settings()["analyze"](conversation)
            fds = _settings()["fds"](account["customer_id"])
            result, _ = automation.create_request(conn, **identity, ai=ai, fds=fds,
                                                 on_create=link_report, retry_guard=check_retry)
        row = conn.execute("SELECT report_id FROM customer_reports WHERE request_id=?",
                           (result["request_id"],)).fetchone()
    return redirect(url_for("customer.detail", report_id=row["report_id"]), code=303)


@customer.get("/history")
def history():
    account_id = g.customer_account["account_id"]
    with closing(_connect()) as conn:
        rows = conn.execute(REPORT_QUERY + " WHERE r.account_id=? ORDER BY r.created_at DESC",
                            (account_id,)).fetchall()
        consultations = conn.execute(
            """SELECT advice_id,SUBSTR(conversation,1,80) AS preview,LENGTH(conversation)>80 AS truncated,
                      reply IS NOT NULL AS completed
               FROM customer_advice WHERE account_id=? ORDER BY created_at DESC,rowid DESC""",
            (account_id,),
        ).fetchall()
    return _page("내 신고 내역", view="list", rows=rows, consultations=consultations, staff_view=False)


@customer.get("/history/<report_id>")
def detail(report_id):
    with closing(_connect()) as conn:
        row = _find(conn, report_id, g.customer_account["account_id"])
        information_request = None
        if row["needs_information"] and row["status"] == automation.Status.PENDING_APPROVAL:
            information_request = conn.execute(
                """SELECT note FROM customer_report_events WHERE report_id=? AND action='INFORMATION_REQUESTED'
                   ORDER BY event_id DESC LIMIT 1""",
                (report_id,),
            ).fetchone()
    return _page("신고 상세", view="detail", row=row, information_request=information_request,
                 staff_view=False)


@customer.post("/history/<report_id>/information")
def provide_information(report_id):
    account_id = g.customer_account["account_id"]
    note = _customer_text(_note())
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _find(conn, report_id, account_id)
        if not row["needs_information"] or row["status"] != automation.Status.PENDING_APPROVAL:
            raise automation.AutomationConflict("현재 추가 정보를 제출할 수 없습니다.")
        conn.execute("UPDATE customer_reports SET needs_information=0 WHERE report_id=?", (report_id,))
        _event(conn, report_id, "INFORMATION_PROVIDED", f"customer:{account_id}", note)
    return redirect(url_for("customer.detail", report_id=report_id), code=303)


def _note():
    value = request.form.get("note", "").strip()
    if not 1 <= len(value) <= 2000:
        abort(400, "메모는 1~2000자로 입력해 주세요.")
    return value


def _employee(*roles):
    employee = g.get("local_user")
    if not employee or employee["role"] not in roles:
        abort(403)
    return employee["username"]


def _assigned(conn, report_id, actor, *, allow_unassigned=False):
    row = _find(conn, report_id)
    allowed = (actor, None) if allow_unassigned else (actor,)
    if row["assigned_to"] not in allowed or not conn.execute(
        "SELECT 1 FROM auth_users WHERE username=? AND role='AGENT' AND active=1", (actor,),
    ).fetchone():
        abort(403)
    return row


def _pending(row):
    if row["status"] != automation.Status.PENDING_APPROVAL:
        raise automation.AutomationConflict("검토 가능한 신고만 변경할 수 있습니다.")


@staff.get("")
def queue():
    actor = _employee("AGENT", "ADMIN")
    with closing(_connect()) as conn:
        rows = conn.execute(
            REPORT_QUERY + " WHERE (? OR r.assigned_to=? OR r.assigned_to IS NULL) ORDER BY r.created_at DESC",
            (g.local_user["role"] == "ADMIN", actor),
        ).fetchall()
    return _page("고객 신고 목록", view="list", rows=rows, staff_view=True)


@staff.get("/<report_id>")
def review(report_id):
    actor = _employee("AGENT", "ADMIN")
    with closing(_connect()) as conn:
        row = _find(conn, report_id)
        admin = g.local_user["role"] == "ADMIN"
        if not admin and row["assigned_to"] not in (None, actor):
            abort(403)
        snapshot = automation.get_request(conn, row["request_id"])
        staff_choices = conn.execute(
            "SELECT username FROM auth_users WHERE role='AGENT' AND active=1 ORDER BY username",
        ).fetchall() if admin else []
        events = _events(conn, report_id)
        receipt = conn.execute(
            """SELECT i.incident_id, i.created_at, c.name
               FROM incidents i JOIN customers c USING(customer_id) WHERE i.request_id=?""",
            (row["request_id"],),
        ).fetchone()
    return _page("고객 신고 검토", view="detail", row=row, events=events, staff_view=True,
                 snapshot=snapshot, can_review=not admin and row["assigned_to"] == actor,
                 can_approve=not admin and row["status"] == automation.Status.PENDING_APPROVAL,
                 staff_choices=staff_choices, categories=_settings()["categories"], risks=RISK_LEVELS,
                 actions=_settings()["actions"], receipt=receipt)


@staff.get("/receipt/<incident_id>")
def receipt_detail(incident_id):
    _employee("AGENT", "ADMIN")
    with closing(_connect()) as conn:
        report = conn.execute(
            "SELECT r.report_id FROM customer_reports r JOIN incidents i USING(request_id) WHERE i.incident_id=?",
            (incident_id,),
        ).fetchone()
    if report is None:
        abort(404)
    return review(report["report_id"])


@staff.post("/excel/open")
def open_excel():
    _employee("AGENT", "ADMIN")
    try:
        tickets = login_tickets(os.getenv("FINGUARD_EXCEL_INTAKE_TOKEN", ""))
    except ValueError as error:
        abort(503, str(error))
    ticket = tickets.dumps(secrets.token_urlsafe(32))
    return render_template("portal/excel_signin.html", ticket=ticket), 200, {"Cache-Control": "no-store"}


@staff.post("/<report_id>/assign")
def assign(report_id):
    actor = _employee("AGENT", "ADMIN")
    admin = g.local_user["role"] == "ADMIN"
    assignee = request.form.get("assignee") if admin else actor
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _find(conn, report_id)
        _pending(row)
        if not admin and row["assigned_to"] not in (None, actor):
            abort(403)
        if not conn.execute("SELECT 1 FROM auth_users WHERE username=? AND role='AGENT' AND active=1",
                            (assignee,)).fetchone():
            abort(400, "활성 상담원을 선택해 주세요.")
        if row["assigned_to"] != assignee:
            conn.execute("UPDATE customer_reports SET assigned_to=? WHERE report_id=?", (assignee, report_id))
            _event(conn, report_id, "ASSIGNED", actor, assignee)
    return redirect(url_for("report_staff.review", report_id=report_id), code=303)


@staff.post("/<report_id>/<any(supplement,'request-information'):action>")
def record_review(report_id, action):
    actor = _employee("AGENT")
    note = _note()
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _assigned(conn, report_id, actor)
        _pending(row)
        if action == "request-information":
            conn.execute("UPDATE customer_reports SET needs_information=1 WHERE report_id=?", (report_id,))
        _event(conn, report_id, "INFORMATION_REQUESTED" if action == "request-information" else "SUPPLEMENTED",
               actor, note)
    return redirect(url_for("report_staff.review", report_id=report_id), code=303)


@staff.post("/<report_id>/<any(approve,reject):action>")
def decide_report(report_id, action):
    actor = _employee("AGENT")
    approved = action == "approve"
    if approved and request.form.get("confirmed") != "yes":
        abort(400, "신고 내용과 분석 결과를 확인해 주세요.")
    reason = None if approved else _note()
    fields = {name: request.form.get(name, "").strip() for name in ("category", "summary", "risk_level")}

    def guard(conn):
        current = _assigned(conn, report_id, actor, allow_unassigned=approved)
        if approved and current["needs_information"]:
            raise automation.AutomationConflict("고객의 추가 정보 제출을 기다리고 있습니다.")
        if current["assigned_to"] is None:
            _pending(current)
            conn.execute("UPDATE customer_reports SET assigned_to=? WHERE report_id=?", (actor, report_id))
            _event(conn, report_id, "ASSIGNED", actor, actor)

    def record(conn):
        if reason:
            conn.execute("UPDATE customer_reports SET rejection_reason=? WHERE report_id=?", (reason, report_id))
        _event(conn, report_id, "APPROVED" if approved else "REJECTED", actor, reason)

    with closing(_connect()) as conn:
        row = _assigned(conn, report_id, actor, allow_unassigned=approved)
        try:
            snapshot = automation.decide(
                conn, row["request_id"], automation.Status.APPROVED if approved else automation.Status.REJECTED,
                actor=actor, fields=fields, categories=_settings()["categories"], guard=guard, on_decide=record,
            )
        except IncidentValidationError as exc:
            abort(400, str(exc))
        if approved and snapshot["dispatch_pending"]:
            _run_action(conn, row, actor, orchestrator_service.dispatch)
    return redirect(url_for("report_staff.review", report_id=report_id), code=303)


def _run_action(conn, row, actor, operation):
    try:
        _settings()["execute"](
            conn, row["request_id"], actor, operation,
            guard=lambda db: _assigned(db, row["report_id"], actor),
        )
    except (OrchestratorError, automation.AutomationConflict) as exc:
        flash(str(exc), "error")


@staff.post("/<report_id>/<any(dispatch,refresh):action>")
def execute_report(report_id, action):
    actor = _employee("AGENT")
    with closing(_connect()) as conn:
        row = _assigned(conn, report_id, actor)
        _run_action(conn, row, actor,
                    orchestrator_service.dispatch if action == "dispatch" else orchestrator_service.refresh)
    return redirect(url_for("report_staff.review", report_id=report_id), code=303)


@staff.get("/dashboard")
def dashboard():
    _employee("ADMIN")
    with closing(_connect()) as conn:
        totals = conn.execute(
            "SELECT assigned_to, status, COUNT(*) AS count FROM customer_reports JOIN automation_requests"
            " USING(request_id) GROUP BY assigned_to,status ORDER BY assigned_to,status",
        ).fetchall()
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("consultations", "incidents", "orchestrator_executions")}
    return _page("관리자 업무 현황", view="dashboard", totals=totals, counts=counts)


@staff.get("/dashboard/<any(consultations,jobs):section>")
def admin_history(section):
    _employee("ADMIN")
    page = max(1, min(request.args.get("page", 1, type=int), 100000))
    if section == "consultations":
        headings = ("상담 ID", "고객 ID", "상담 내용", "AI 요약", "등록 시각")
        sql = ("SELECT consultation_id,customer_id,conversation,ai_summary,created_at "
               "FROM consultations ORDER BY created_at DESC,consultation_id")
    else:
        headings = ("요청 ID", "Job ID", "Job 상태", "실행 결과", "접수번호", "오류")
        sql = ("SELECT request_id,job_key,job_state,result_status,o.incident_id,error_message "
               "FROM orchestrator_executions JOIN automation_requests o USING(request_id) "
               "ORDER BY o.created_at DESC,request_id")
    with closing(_connect()) as conn:
        rows = conn.execute(sql + " LIMIT 51 OFFSET ?", ((page - 1) * 50,)).fetchall()
    return _page("전체 상담 이력" if section == "consultations" else "전체 실행 이력",
                 view="admin-history", rows=rows[:50], headings=headings, section=section,
                 page=page, has_next=len(rows) > 50)


@staff.post("/dashboard/connections")
def connections():
    _employee("ADMIN")
    gateway = _settings()["analyze"]("가상의 고객이 카드사 고객센터 운영 시간을 문의합니다.")
    try:
        _settings()["discover"]()
        orchestrator = "조회 성공"
    except OrchestratorError:
        orchestrator = "조회 실패"
    flash(f"Gateway {'분석 성공' if gateway else '분석 실패'} · Orchestrator {orchestrator}", "notice")
    return redirect(url_for("report_staff.dashboard"), code=303)


def configure_portal(app, limiter, **settings):
    app.extensions["finguard_portal"] = settings
    limiter.limit("10 per minute", methods=["POST"], exempt_when=lambda: (
        request.endpoint != "customer.login"
    ))(customer)
    limiter.limit("5 per minute", methods=["POST"], exempt_when=lambda: (
        request.endpoint not in ("customer.register", "customer.report", "customer.advice")
    ))(customer)
    limiter.limit("3 per minute", methods=["POST"], exempt_when=lambda: (
        request.endpoint != "report_staff.connections"
    ))(staff)
    app.register_blueprint(customer)
    app.register_blueprint(staff)

