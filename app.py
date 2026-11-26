
import hmac
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from ai.consultation_analyzer import Category, analyze_consultation
from ai.uipath_client import UiPathLLMError
from fds import analyze_customer
from rpa.orchestrator_client import OrchestratorClient, OrchestratorError
from services import automation_service, orchestrator_service
from services.automation_service import AutomationConflict, AutomationNotFound, Status
from services.auth import configure_auth, local_enabled, local_permission
from services.customer_portal import configure_portal, homepage
from services.incident_service import (
    RISK_LEVELS, IncidentValidationError, create_incident,
)


app = Flask(__name__)
app.json.ensure_ascii = False
# Production integration must supply a trusted WSGI identity and a server-side authorizer.
app.config["AUTOMATION_AUTHORIZER"] = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "finguard.db"


# ==========================================
# API Rate Limit 설정
# ==========================================

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


@app.errorhandler(429)
def rate_limit_exceeded(error):
    if request.endpoint == "auth.login":
        return render_template("crm/login.html",
                               error="로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."), 429
    if request.blueprint == "customer":
        return render_template("portal/page.html", title="요청 제한", view="error",
                               error="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."), 429
    return jsonify({
        "error": "요청 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요."
    }), 429


@app.errorhandler(sqlite3.Error)
def database_error(error):
    # 트랜잭션은 커밋 전이면 통째로 취소되므로 반쯤 저장된 데이터는 남지 않는다.
    app.logger.exception("DB 오류")
    # 테이블·컬럼이 없는 구조 문제는 재시도해도 해결되지 않으므로 별도 ID로 구분한다.
    if any(text in str(error) for text in ("no such table", "no such column")):
        message = ("DB가 초기화되지 않았거나 구조가 최신이 아닙니다. "
                   "기존 DB에는 python -m database.migrate를 실행하세요. "
                   "최초 설치에는 python database/seed.py를 실행하세요.")
        error_id, status = "crm-setup-error", 500
    else:
        message = "일시적인 DB 오류입니다. 같은 요청 ID로 다시 시도해 주세요."
        error_id, status = "crm-error", 503

    if request.path.startswith("/crm"):
        return render_template("crm/error.html", message=message, error_id=error_id), status
    return jsonify({"error": message, "retryable": status == 503}), status


# ==========================================
# Database
# ==========================================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


configure_auth(app, lambda: get_db_connection(), limiter)

# ==========================================
# 기본 API
# ==========================================

@app.get("/")
def index():
    return homepage()


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.svg")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ==========================================
# 고객 조회 API
# ==========================================

@app.get("/api/customers")
def get_customers():
    conn = get_db_connection()

    try:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY customer_id"
        ).fetchall()

        return jsonify([dict(row) for row in rows])

    finally:
        conn.close()


# ==========================================
# 거래 내역 조회 API
# ==========================================

@app.get("/api/transactions")
def get_transactions():
    conn = get_db_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                t.transaction_id,
                t.customer_id,
                c.name AS customer_name,
                t.amount,
                t.country,
                t.transaction_at,
                t.status
            FROM transactions t
            JOIN customers c
              ON t.customer_id = c.customer_id
            ORDER BY t.transaction_at DESC
            """
        ).fetchall()

        return jsonify([dict(row) for row in rows])

    finally:
        conn.close()


# ==========================================
# AI 상담 분석 API
# ==========================================

@app.post("/api/ai/analyze")
@limiter.limit("5 per minute")
def analyze_ai():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "JSON 요청 본문이 필요합니다."
        }), 400

    conversation = data.get("conversation")

    if not isinstance(conversation, str) or not conversation.strip():
        return jsonify({
            "error": "상담 내용을 입력해 주세요."
        }), 400

    if len(conversation) > 10000:
        return jsonify({
            "error": "상담 내용은 10000자 이하로 입력해 주세요."
        }), 400

    try:
        result = analyze_consultation(conversation)

        return jsonify(result), 200

    except UiPathLLMError as exc:
        app.logger.warning("AI 상담 분석 실패: %s", exc.diagnostic)
        error = str(exc)
    except Exception as exc:
        app.logger.error("AI 상담 분석 실패: %s", type(exc).__name__)
        error = "AI 상담 분석 중 오류가 발생했습니다."

    return jsonify({
        "error": f"{error} 상담원 검토가 필요합니다.",
        "recommended_action": "ESCALATE_TO_AGENT",
        "requires_human_review": True,
    }), 502


# ==========================================
# 모의 CRM (사고 접수) - UiPath는 템플릿의 고정 HTML ID를 사용
# ==========================================

CATEGORY_LABELS = {
    Category.LOST_CARD.value: "카드 분실",
    Category.UNAUTHORIZED_TRANSACTION.value: "카드 부정사용 의심",
    Category.PAYMENT_INQUIRY.value: "결제 문의",
    Category.GENERAL_INQUIRY.value: "일반 문의",
    Category.OTHER.value: "기타",
}
STATUS_LABELS = {"RECEIVED": "접수", "IN_PROGRESS": "처리중", "DONE": "처리완료"}
ACTION_LABELS = {
    "VERIFY_CUSTOMER": "본인 확인 필요",
    "VERIFY_AND_ESCALATE": "본인 확인 후 담당 부서 이관",
    "PROVIDE_INFORMATION": "정보 안내",
    "ESCALATE_TO_AGENT": "상담원 직접 처리",
}


# ==========================================
# 상담원 화면 - 상담 입력 → AI 결과 → 위험 분석 → 승인
# ==========================================

@app.template_filter("korean_time")
def korean_time(value):
    if not value:
        return "—"
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


@app.get("/consultation")
def consultation_form():
    actor = _current_actor()
    state = None
    conversation = None
    request_id = request.args.get("request_id")
    if request_id:
        actor = _automation_actor("read")
        with closing(get_db_connection()) as conn:
            state = _automation_visible(conn, request_id, actor)
            report = conn.execute("SELECT report_id FROM customer_reports WHERE request_id=?",
                                  (request_id,)).fetchone()
            if report:
                return redirect(url_for("report_staff.review", report_id=report["report_id"]))
            conversation = conn.execute(
                "SELECT conversation FROM consultations WHERE consultation_id = ?",
                (state["consultation_id"],),
            ).fetchone()["conversation"]
    return render_template(
        "crm/consultation.html",
        categories=CATEGORY_LABELS, risk_levels=RISK_LEVELS, actions=ACTION_LABELS,
        automation_enabled=(
            _automation_allowed(actor, "create") and _automation_allowed(actor, "read")
        ),
        can_decide=(_automation_allowed(actor, "decide") or _automation_allowed(actor, "decide_own")),
        can_dispatch=(_automation_allowed(actor, "dispatch") or _automation_allowed(actor, "dispatch_own")),
        saved_request=state,
        saved_conversation=conversation,
    )


@app.get("/consultation/review")
def consultation_review():
    actor = _telephone_actor("decide")
    own = not _automation_allowed(actor, "decide")
    legacy_only = bool(g.get("local_user") and g.local_user["role"] == "SUPERVISOR")
    with closing(get_db_connection()) as conn:
        pending = conn.execute(
            """
            SELECT request_id, customer_id, created_by, created_at, review_required, status
            FROM automation_requests
            WHERE status = 'PENDING_APPROVAL'
              AND NOT EXISTS (SELECT 1 FROM customer_reports r WHERE r.request_id=automation_requests.request_id)
              AND (?=0 OR created_by=?)
              AND (?=0 OR NOT EXISTS (
                  SELECT 1 FROM agent_telephone_requests t WHERE t.request_id=automation_requests.request_id
              ))
            ORDER BY created_at, request_id
            """, (own, actor, legacy_only),
        ).fetchall()
    return render_template("crm/review.html", pending=pending, can_dispatch=(
        _automation_allowed(actor, "dispatch") or _automation_allowed(actor, "dispatch_own")
    ))


@app.get("/consultation/executions")
def consultation_executions():
    actor = _telephone_actor("dispatch")
    own = not _automation_allowed(actor, "dispatch")
    legacy_only = bool(g.get("local_user") and g.local_user["role"] == "SUPERVISOR")
    with closing(get_db_connection()) as conn:
        executions = conn.execute(
            """
            SELECT request_id, customer_id, created_by, created_at, review_required, status
            FROM automation_requests
            WHERE (dispatch_pending = 1 OR request_id IN (SELECT request_id FROM orchestrator_executions))
              AND (?=0 OR created_by=?)
              AND (?=0 OR NOT EXISTS (
                  SELECT 1 FROM agent_telephone_requests t WHERE t.request_id=automation_requests.request_id
              ))
              AND NOT EXISTS (SELECT 1 FROM customer_reports r WHERE r.request_id=automation_requests.request_id)
            ORDER BY created_at DESC, request_id
            """, (own, actor, legacy_only),
        ).fetchall()
    return render_template("crm/review.html", pending=executions, show_executions=True)


@app.get("/api/fds/customers/<customer_id>")
def fds_customer_risk(customer_id):
    return jsonify(analyze_customer(customer_id, db_path=DB_PATH))


@app.get("/crm/incidents/new")
def crm_incident_form():
    return render_template(
        "crm/incident_new.html",
        categories=CATEGORY_LABELS, risk_levels=RISK_LEVELS,
        form={}, error=None,
    )


@app.post("/crm/incidents")
def crm_incident_create():
    form = {k: request.form.get(k, "").strip() for k in
            ("request_id", "customer_id", "category", "summary", "risk_level")}

    def fail(message):
        return render_template(
            "crm/incident_new.html",
            categories=CATEGORY_LABELS, risk_levels=RISK_LEVELS,
            form=form, error=message,
        ), 400

    try:
        with closing(get_db_connection()) as conn:
            incident_id, duplicate = create_incident(
                conn, form, categories=CATEGORY_LABELS,
                uuid_factory=uuid4,
            )
    except IncidentValidationError as exc:
        return fail(str(exc))

    return redirect(url_for(
        "crm_incident_detail", incident_id=incident_id, dup=1 if duplicate else None,
    ))


INCIDENT_SELECT = """
    SELECT i.*, c.name AS customer_name
    FROM incidents i JOIN customers c ON i.customer_id = c.customer_id
"""


@app.get("/crm/incidents/<incident_id>")
def crm_incident_detail(incident_id):
    conn = get_db_connection()
    try:
        row = conn.execute(INCIDENT_SELECT + " WHERE i.incident_id = ?",
                           (incident_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    return render_template(
        "crm/incident_detail.html", incident=row,
        duplicate=request.args.get("dup") == "1",
        categories=CATEGORY_LABELS, statuses=STATUS_LABELS, risk_levels=RISK_LEVELS,
    )


@app.get("/crm/incidents")
def crm_incident_list():
    conn = get_db_connection()
    try:
        rows = conn.execute(INCIDENT_SELECT + " ORDER BY i.created_at DESC").fetchall()
    finally:
        conn.close()
    return render_template(
        "crm/incident_list.html", incidents=rows,
        categories=CATEGORY_LABELS, statuses=STATUS_LABELS, risk_levels=RISK_LEVELS,
    )



# Automation API. Identity comes from trusted WSGI middleware, never request headers or JSON.
def _current_actor():
    if local_enabled():
        user = g.get("local_user")
        return user["username"] if user else None
    return request.remote_user


def _automation_allowed(actor, permission):
    if local_enabled():
        return local_permission(permission)
    authorize = app.config["AUTOMATION_AUTHORIZER"]
    return bool(actor and callable(authorize) and authorize(actor, permission) is True)


def _automation_actor(*permissions):
    actor = _current_actor()
    if not actor:
        abort(401)
    if not all(_automation_allowed(actor, permission) for permission in permissions):
        abort(403)
    return actor


def _automation_visible(conn, request_id, actor):
    current = automation_service.get_request(conn, request_id)
    report = conn.execute("SELECT assigned_to FROM customer_reports WHERE request_id=?",
                          (request_id,)).fetchone()
    employee = g.get("local_user")
    if report:
        if not employee or not (
            employee["role"] == "ADMIN" or
            (employee["role"] == "AGENT" and report["assigned_to"] == actor)
        ):
            raise AutomationNotFound("자동화 요청을 찾을 수 없습니다.")
    elif current["created_by"] != actor and not _automation_allowed(actor, "decide"):
        raise AutomationNotFound("자동화 요청을 찾을 수 없습니다.")
    job = orchestrator_service.execution(conn, request_id)
    if job:
        current["orchestrator"] = {
            name: job[name] for name in ("job_key", "job_state", "result_status", "error_message")
        }
        receipt = orchestrator_service.confirmed_receipt(conn, current)
        current["orchestrator"]["receipt_id"] = receipt["incident_id"] if receipt else None
    return current


@app.after_request
def automation_response_headers(response):
    if local_enabled() or request.path.startswith(("/consultation", "/api/automation/")):
        response.headers["Cache-Control"] = "no-store"
    return response


def _automation_http_error(error):
    if not request.path.startswith("/api/automation/"):
        return error
    return jsonify({"error": error.description}), error.code


app.register_error_handler(401, _automation_http_error)
app.register_error_handler(403, _automation_http_error)


@app.errorhandler(AutomationNotFound)
def automation_not_found(error):
    return jsonify({"error": str(error)}), 404


@app.errorhandler(AutomationConflict)
def automation_conflict(error):
    if request.blueprint in ("customer", "report_staff"):
        return render_template("portal/page.html", title="처리 상태 확인",
                               view="error", error=str(error)), 409
    return jsonify({"error": str(error)}), 409


@app.post("/api/automation/requests")
@limiter.limit("5 per minute")
def automation_request_create():
    actor = _automation_actor("create", "read")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON 요청 본문이 필요합니다."}), 400
    customer_id = data.get("customer_id")
    conversation = data.get("conversation")
    key = request.headers.get("Idempotency-Key", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        return jsonify({"error": "유효한 Idempotency-Key 헤더가 필요합니다."}), 400
    if not isinstance(customer_id, str) or not customer_id.strip() or len(customer_id) > 50:
        return jsonify({"error": "고객 ID를 확인해 주세요."}), 400
    if not isinstance(conversation, str) or not conversation.strip() or len(conversation) > 10000:
        return jsonify({"error": "상담 내용을 1~10000자로 입력해 주세요."}), 400
    customer_id, conversation = customer_id.strip(), conversation.strip()
    identity = dict(actor=actor, idempotency_key=key, customer_id=customer_id,
                    conversation=conversation)
    with closing(get_db_connection()) as conn:
        retry = automation_service.find_retry(conn, **identity)
        if retry is not None:
            return jsonify(retry), 200
        if not conn.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone():
            return jsonify({"error": "존재하지 않는 고객 ID입니다."}), 400

    fds = analyze_customer(customer_id, db_path=DB_PATH)
    ai = _analyze_for_review(conversation)
    def mark_telephone(conn, request_id):
        if g.get("local_user") and g.local_user["role"] in ("AGENT", "ADMIN"):
            conn.execute("INSERT INTO agent_telephone_requests VALUES (?)", (request_id,))

    with closing(get_db_connection()) as conn:
        result, created = automation_service.create_request(conn, **identity, ai=ai, fds=fds,
                                                             on_create=mark_telephone)
    return jsonify(result), 201 if created else 200


def _analyze_for_review(conversation):
    try:
        return analyze_consultation(conversation)
    except UiPathLLMError as exc:
        app.logger.warning("자동화 요청의 AI 상담 분석 실패: %s", exc.diagnostic)
    except Exception as exc:
        app.logger.warning("자동화 요청의 AI 상담 분석 실패: %s", type(exc).__name__)
    return None


@app.get("/api/automation/requests/<request_id>")
def automation_request_detail(request_id):
    actor = _automation_actor("read")
    with closing(get_db_connection()) as conn:
        return jsonify(_automation_visible(conn, request_id, actor))


@app.get("/api/automation/requests/<request_id>/events")
def automation_request_events(request_id):
    actor = _automation_actor("read")
    with closing(get_db_connection()) as conn:
        _automation_visible(conn, request_id, actor)
        return jsonify(automation_service.get_events(conn, request_id))


def _decide_automation(request_id, target):
    actor = _telephone_actor("decide")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON 요청 본문이 필요합니다."}), 400
    fields = {}
    if target == Status.APPROVED:
        for name in ("category", "summary", "risk_level"):
            value = data.get(name, "")
            if not isinstance(value, str):
                return jsonify({"error": "접수 내용을 확인해 주세요."}), 400
            fields[name] = value.strip()
    try:
        with closing(get_db_connection()) as conn:
            _telephone_scope(conn, request_id, actor, "decide")
            result = automation_service.decide(
                conn, request_id, target, actor=actor, fields=fields, categories=CATEGORY_LABELS,
                guard=lambda db: _telephone_scope(db, request_id, actor, "decide"),
            )
    except IncidentValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/automation/requests/<request_id>/approve")
def automation_request_approve(request_id):
    return _decide_automation(request_id, Status.APPROVED)


@app.post("/api/automation/requests/<request_id>/reject")
def automation_request_reject(request_id):
    return _decide_automation(request_id, Status.REJECTED)


class RobotConfigurationError(OrchestratorError):
    pass


@app.errorhandler(OrchestratorError)
def orchestrator_error(error):
    return jsonify({"error": str(error)}), 503 if isinstance(error, RobotConfigurationError) else 502


def _telephone_actor(permission):
    actor = _automation_actor("read")
    if not (_automation_allowed(actor, permission) or _automation_allowed(actor, permission + "_own")):
        abort(403)
    return actor


def _telephone_scope(conn, request_id, actor, permission):
    if conn.execute("SELECT 1 FROM customer_reports WHERE request_id=?", (request_id,)).fetchone():
        abort(403, "고객 신고 검토 화면에서 처리해 주세요.")
    current = automation_service.get_request(conn, request_id)
    if (g.get("local_user") and g.local_user["role"] == "SUPERVISOR" and conn.execute(
        "SELECT 1 FROM agent_telephone_requests WHERE request_id=?", (request_id,),
    ).fetchone()):
        abort(403, "신규 상담은 담당 상담원이 처리합니다.")
    if not _automation_allowed(actor, permission) and current["created_by"] != actor:
        abort(403)
    return current


def _execute_automation(conn, request_id, actor, action, *, guard=None):
    current = automation_service.get_request(conn, request_id)
    if action is orchestrator_service.dispatch:
        if current["status"] != Status.APPROVED or not current["dispatch_pending"]:
            raise AutomationConflict("실행 대기 중인 승인 요청만 시작할 수 있습니다.")
        robot_token = os.getenv("FINGUARD_ROBOT_TOKEN", "")
        if len(robot_token) < 32 or not robot_token.isascii():
            raise RobotConfigurationError("로봇 접수 토큰 설정을 확인하세요.")
    else:
        if not orchestrator_service.execution(conn, request_id):
            raise AutomationConflict("조회할 Orchestrator 실행이 없습니다.")
        if current["status"] in (Status.SUCCEEDED, Status.FAILED):
            return
    with httpx.Client(timeout=15, transport=httpx.HTTPTransport(retries=0)) as http:
        client = OrchestratorClient.connect(http)
        if guard and action is orchestrator_service.dispatch:
            action(conn, request_id, actor, client, guard=guard)
        else:
            action(conn, request_id, actor, client)


def _discover_process():
    with httpx.Client(timeout=15, transport=httpx.HTTPTransport(retries=0)) as http:
        return OrchestratorClient.connect(http).release_key()


def _orchestrator_action(request_id, action):
    actor = _telephone_actor("dispatch")
    with closing(get_db_connection()) as conn:
        _telephone_scope(conn, request_id, actor, "dispatch")
        _execute_automation(conn, request_id, actor, action)
        return jsonify(_automation_visible(conn, request_id, actor))


@app.post("/api/automation/requests/<request_id>/dispatch")
def automation_dispatch(request_id):
    return _orchestrator_action(request_id, orchestrator_service.dispatch)


@app.post("/api/automation/requests/<request_id>/refresh-job")
def automation_refresh_job(request_id):
    return _orchestrator_action(request_id, orchestrator_service.refresh)


@app.post("/api/automation/robot/requests/<request_id>/receipt")
def automation_robot_receipt(request_id):
    token = os.getenv("FINGUARD_ROBOT_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if (len(token) < 32 or not token.isascii() or not supplied.isascii()
            or not hmac.compare_digest(supplied, f"Bearer {token}")):
        abort(401, "로봇 인증이 필요합니다.")
    payload = request.get_json(silent=True)
    dispatch_id = payload.get("dispatch_id") if isinstance(payload, dict) else None
    if not isinstance(dispatch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", dispatch_id):
        return jsonify({"error": "실행 식별자를 확인하세요."}), 400
    try:
        with closing(get_db_connection()) as conn:
            incident_id, duplicate = orchestrator_service.submit_receipt(
                conn, request_id, dispatch_id, CATEGORY_LABELS,
            )
    except IncidentValidationError as exc:
        raise AutomationConflict(str(exc)) from None
    return jsonify(request_id=request_id, incident_id=incident_id,
                   status="DUPLICATE" if duplicate else "SUCCESS"), 200 if duplicate else 201


# ==========================================
# Flask 실행
# ==========================================

configure_portal(app, limiter, analyze=_analyze_for_review,
                 fds=lambda customer_id: analyze_customer(customer_id, db_path=DB_PATH),
                 execute=_execute_automation, discover=_discover_process, categories=CATEGORY_LABELS,
                 actions=ACTION_LABELS)


if __name__ == "__main__":
    app.run(port=5000)
