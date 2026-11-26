import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest

import app as web
from database.migrate import apply_migrations
from test_local_auth import FIELDS, PASSWORD, csrf, demo, login, post
from test_orchestrator import inputs, orchestrator, robot, sdk_job

QUEUE = "/consultation/customer-reports"
BODY = {
    "kind": "CARD_FRAUD",
    "conversation": "가상 고객이 해외 결제 피해를 신고했습니다.",
    "occurred_at": "2026-11-25T12:30",
    "amount": "25000",
}


def form(client, path, **data):
    return client.post(path, data={**data, "csrf_token": csrf(client)})


def customer(username):
    client = web.app.test_client()
    assert client.get("/customer/register").status_code == 200
    assert form(client, "/customer/register", username=username, password=PASSWORD).status_code == 302
    assert form(client, "/customer/login", username=username, password=PASSWORD).status_code == 302
    return client


def employee(username="agent"):
    client = web.app.test_client()
    response, _ = login(client, username)
    expected = "/consultation/review" if username == "supervisor" else QUEUE
    assert (response.status_code, response.location) == (302, expected)
    return client


def test_advice_history_saves_result_for_customer_only(demo, monkeypatch):
    owner, outsider = customer("advice-owner"), customer("advice-outsider")
    message = "가상의 해외 결제에 관해 상담하고 싶습니다."
    for path in ("/customer/advice", "/customer/report"):
        page = owner.get(path)
        assert 'id="voice-input" type="button"' in page.text
        assert 'src="/static/voice-input.js"' in page.text
    response = form(owner, "/customer/advice", conversation=message)
    assert response.status_code == 303
    details = response.location
    assert "거래 내역 확인을 안내합니다." in owner.get(details).text
    assert message in owner.get("/customer/history").text
    assert message not in outsider.get("/customer/history").text
    assert outsider.get(details).status_code == 404
    monkeypatch.setattr(web, "analyze_consultation", lambda _: None)
    failure = form(owner, "/customer/advice", conversation="별도의 가상 상담")
    assert failure.status_code == 303
    assert "AI 상담에 연결할 수 없습니다" in owner.get(failure.location).text
    history = owner.get("/customer/history").text
    assert "분석 완료" in history and "분석 연결 실패" in history
    with closing(web.get_db_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM automation_requests").fetchone()[0] == 0


def submit(client, **changes):
    page = client.get("/customer/report")
    key = re.search(r'name="report_key" value="([a-f0-9]+)"', page.text)[1]
    fields = {**BODY, **changes, "report_key": key}
    response = form(client, "/customer/report", **fields)
    assert response.status_code == 303
    return response.location, fields


def approve(agent, path):
    return form(agent, path + "/approve", confirmed="yes", **FIELDS)


def test_customer_registration_report_replay_and_isolation(demo):
    home = demo.get("/")
    assert home.status_code == 200
    assert "금융 피해 신고" in home.text
    assert 'href="/static/portal.css"' in home.text
    for path in ("/customer/report", "/customer/advice", "/customer/history", "/login"):
        assert f'href="{path}"' in home.text
    assert demo.get("/static/portal.css").status_code == 200
    assert demo.get("/favicon.ico").status_code == 200
    owner, outsider = customer("owner"), customer("outsider")
    path, fields = submit(owner, customer_id="C001")
    retry = form(owner, "/customer/report", **fields)
    assert (retry.status_code, retry.location) == (303, path)
    assert form(owner, "/customer/report", **{**fields, "amount": "10"}).status_code == 409
    assert path in owner.get("/customer/history").text
    assert "신고 시각" not in owner.get("/customer/history").text
    detail = owner.get(path).text
    assert "신고 시각" not in detail and "처리 이력" not in detail
    assert "피해 발생 시각" in detail and "2026-11-25 12:30" in detail
    assert "한국 시간" not in detail
    assert path not in outsider.get("/customer/history").text
    assert outsider.get(path).status_code == 404
    assert outsider.get("/api/customers").status_code == 401
    report_id = path.rsplit("/", 1)[-1]
    assert outsider.get(QUEUE + "/" + report_id).status_code == 302
    with closing(web.get_db_connection()) as conn:
        row, = conn.execute("SELECT customer_id,request_id FROM automation_requests").fetchall()
        assert row["customer_id"].startswith("TEST-")
    assert post(owner, f"/api/automation/requests/{row['request_id']}/approve", FIELDS).status_code == 401
    assert post(owner, f"/api/automation/requests/{row['request_id']}/dispatch", {}).status_code == 401
    assert form(owner, "/customer/logout").status_code == 302
    assert owner.get(path).status_code == 302


def test_customer_report_full_job_and_receipt_flow(demo, orchestrator):
    owner = customer("e2e")
    customer_path, _ = submit(owner)
    staff_path = QUEUE + "/" + customer_path.rsplit("/", 1)[-1]
    agent, admin, supervisor = employee(), employee("admin"), employee("supervisor")
    assert staff_path in agent.get(QUEUE).text
    with closing(web.get_db_connection()) as conn, conn:
        conn.execute("UPDATE customer_reports SET created_at='2026-11-25 08:17:34'")
    review = agent.get(staff_path)
    assert review.status_code == 200
    assert 'href="/static/crm.css"' in review.text
    assert 'class="crm-insights"' in review.text
    assert agent.get("/static/crm.css").status_code == 200
    assert "신고 시각" in review.text and "2026-11-25 17:17" in review.text
    assert "한국 시간" not in review.text
    assert "거래 내역 이상 징후 확인" in review.text
    assert "본인 확인 후 담당 부서 이관" in review.text
    assert "승인 및 자동 접수 시작" in review.text
    assert approve(admin, staff_path).status_code == 403
    assert approve(supervisor, staff_path).status_code == 403
    assert form(agent, staff_path + "/assign").status_code == 303
    assert form(agent, staff_path + "/dispatch").status_code == 303
    assert not orchestrator.calls
    assert form(agent, staff_path + "/approve", **FIELDS).status_code == 400
    assert form(agent, staff_path + "/request-information", note="거래 시각을 확인해 주세요.").status_code == 303
    assert "거래 시각을 확인해 주세요." in owner.get(customer_path).text
    assert approve(agent, staff_path).status_code == 409
    assert form(owner, customer_path + "/information", note="정오 무렵입니다.").status_code == 303
    assert form(agent, staff_path + "/supplement", note="내부 검토 메모").status_code == 303
    assert "내부 검토 메모" not in owner.get(customer_path).text
    assert approve(agent, staff_path).status_code == 303
    dispatched = inputs(orchestrator)
    request_id = dispatched["in_RequestId"]
    assert approve(agent, staff_path).status_code == 409
    assert len([call for call in orchestrator.calls if call.method == "POST"]) == 1
    assert post(admin, f"/api/automation/requests/{request_id}/dispatch", {}).status_code == 403

    first = robot(owner, request_id, dispatched["in_DispatchId"])
    replay = robot(owner, request_id, dispatched["in_DispatchId"])
    assert (first.status_code, replay.status_code) == (201, 200)
    receipt_id = first.json["incident_id"]
    assert replay.json["incident_id"] == receipt_id
    assert receipt_id not in owner.get(customer_path).text
    orchestrator.jobs.retrieve.return_value = sdk_job(orchestrator, "Successful", output={
        "out_RequestId": request_id, "out_Status": "SUCCESS", "out_IncidentId": receipt_id,
    })
    assert form(agent, staff_path + "/refresh").status_code == 303
    page = owner.get(customer_path).text
    assert receipt_id in page and "사고 접수 완료" in page
    assert orchestrator.job_key not in page
    assert admin.get(QUEUE + "/dashboard").status_code == 200
    assert admin.get(QUEUE + "/dashboard/jobs").status_code == 200
    assert agent.get(QUEUE + "/dashboard").status_code == 403
    with closing(web.get_db_connection()) as conn:
        assert conn.execute("SELECT status,decision_by FROM automation_requests WHERE request_id=?",
                            (request_id,)).fetchone()[:] == ("SUCCEEDED", "agent")
        assert conn.execute("SELECT COUNT(*) FROM incidents WHERE request_id=?", (request_id,)).fetchone()[0] == 1


def test_rejection_preserves_original_report_when_ai_fails(demo, orchestrator, monkeypatch):
    def fail(_):
        raise web.UiPathLLMError("test failure")

    monkeypatch.setattr(web, "analyze_consultation", fail)
    owner = customer("fallback")
    path, _ = submit(owner)
    staff_path = QUEUE + "/" + path.rsplit("/", 1)[-1]
    agent = employee()
    assert "AI 분석에 실패" in agent.get(staff_path).text
    assert form(agent, staff_path + "/assign").status_code == 303
    assert form(agent, staff_path + "/reject", note="거래 정보가 부족합니다.").status_code == 303
    assert "거래 정보가 부족합니다." in owner.get(path).text
    assert BODY["conversation"] in owner.get(path).text
    assert not orchestrator.calls
    with closing(web.get_db_connection()) as conn:
        row, = conn.execute("SELECT status,ai_result_json,review_required FROM automation_requests")
        assert tuple(row) == ("REJECTED", None, 1)


def test_only_assigned_agent_can_resolve_report(demo):
    owner = customer("assigned")
    path, _ = submit(owner)
    staff_path = QUEUE + "/" + path.rsplit("/", 1)[-1]
    creator = web.app.test_cli_runner().invoke(
        args=["create-user", "other-agent", "--role", "AGENT"],
        input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert creator.exit_code == 0
    agent, other, admin = employee(), employee("other-agent"), employee("admin")
    assert form(admin, staff_path + "/assign", assignee="other-agent").status_code == 303
    assert approve(agent, staff_path).status_code == 403
    assert form(agent, staff_path + "/assign").status_code == 403
    assert form(other, staff_path + "/reject", note="검토 결과 반려합니다.").status_code == 303


def test_legacy_approval_and_migration_remain_available(demo):
    from test_local_auth import create
    login(demo)
    state = create(demo, uuid4().hex).json
    request_id = state["request_id"]
    with closing(web.get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM agent_telephone_requests WHERE request_id=?", (request_id,))
        apply_migrations(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    supervisor = employee("supervisor")
    assert post(supervisor, f"/api/automation/requests/{request_id}/approve", FIELDS).json["decision_by"] == "supervisor"


def test_concurrent_report_approval_starts_only_one_job(demo, orchestrator):
    path, _ = submit(customer("parallel"))
    staff_path = QUEUE + "/" + path.rsplit("/", 1)[-1]
    agent = employee()
    cookie = agent.get_cookie("finguard_session").value
    token = csrf(agent)

    def send(_):
        client = web.app.test_client()
        client.set_cookie("finguard_session", cookie)
        return client.post(staff_path + "/approve",
                           data={**FIELDS, "confirmed": "yes", "csrf_token": token}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(send, range(2)))
    assert 303 in codes and set(codes) <= {303, 409}
    assert sum(call.method == "POST" for call in orchestrator.calls) == 1
    with closing(web.get_db_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customer_report_events WHERE action='APPROVED'").fetchone()[0] == 1
        assert conn.execute("SELECT assigned_to FROM customer_reports").fetchone()[0] == "agent"
        assert conn.execute("SELECT COUNT(*) FROM customer_report_events WHERE action='ASSIGNED'").fetchone()[0] == 1


@pytest.mark.parametrize("login_path", ["/login", "/customer/login"])
def test_login_get_does_not_spend_post_rate_limit(demo, login_path):
    for _ in range(14):
        assert demo.get(login_path).status_code == 200
    for _ in range(10):
        assert form(demo, login_path, username="missing", password="not-the-password").status_code == 401
    limited = form(demo, login_path, username="missing", password="bad")
    assert limited.status_code == 429 and "잠시 후 다시 시도" in limited.text
    assert demo.get(login_path).status_code == 200

