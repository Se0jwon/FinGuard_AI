import io
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from openpyxl import Workbook, load_workbook

from excel_intake import FIELDS, create_app, normalize, save_row
from test_customer_portal import QUEUE, approve, customer, employee, submit
from test_local_auth import demo
from test_orchestrator import inputs, orchestrator, robot

TOKEN = "fixture-excel-intake-token-for-testing"


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)[1]


def payload():
    return {
        "customer-name": "테스트 고객", "report-id": f"RPT-{uuid4().hex}",
        "incident-id": "INC-20261126-424550", "damage-type": "보이스피싱",
        "damage-amount": "5000000", "occurred-at": "미입력", "conversation": "=2+3",
        "ai-summary": "피해 신고 요약", "fds-risk": "미분석", "assigned-agent": "agent1",
        "received-at": "2026-11-26 10:00:00",
    }


def test_excel_starts_empty_and_saves_ui_submission(tmp_path):
    path = tmp_path / "receipts.xlsx"
    client = create_app(token=TOKEN, workbook_path=path).test_client()
    assert client.get("/records.xlsx").status_code == 302
    assert client.post("/api/drafts", json=payload()).status_code == 404
    assert client.post("/login", data={"password": TOKEN, "csrf": csrf(client, "/login")}).status_code == 303
    form = client.get("/").text
    for key, _, _ in FIELDS:
        assert f'id="excel-{key}"' in form
    assert "테스트 고객" not in form and "INC-20261126-424550" not in form
    assert not path.exists()
    data = payload()
    assert client.post("/", data=data).status_code == 403
    data["csrf"] = csrf(client, "/")
    first = client.post("/", data=data, follow_redirects=True)
    assert 'id="excel-save-success"' in first.text
    assert "<table>" in first.text and "conic-gradient(" not in first.text
    assert data["incident-id"] in first.text
    assert client.post("/", data=data, follow_redirects=True).status_code == 200
    assert client.post("/", data={**data, "conversation": "다른 내용"}).status_code == 409
    download = client.get("/records.xlsx")
    assert download.status_code == 200 and "attachment" in download.headers["Content-Disposition"]
    workbook = load_workbook(io.BytesIO(download.data))
    try:
        sheet = workbook["접수 목록"]
        assert sheet.max_row == 2
        assert sheet["F2"].value == "=2+3" and sheet["F2"].data_type == "s"
        assert sheet["G2"].value == data["customer-name"]
    finally:
        workbook.close()


def test_workbook_preserves_legacy_rows_and_serializes_duplicate_writes(tmp_path):
    path = tmp_path / "receipts.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "접수 목록"
    sheet.append(("저장 시각 (한국 시간)", "요청 ID", "사고 접수번호", "분류", "위험도", "상담 내용"))
    sheet.append(("2026-11-25 12:00:00", "legacy", "INC-20261125-123ABC", "기존 분류", "", "기존 상담"))
    workbook.save(path)
    workbook.close()

    client = create_app(token=TOKEN, workbook_path=path).test_client()
    assert client.post("/login", data={"password": TOKEN, "csrf": csrf(client, "/login")}).status_code == 303
    downloaded = load_workbook(io.BytesIO(client.get("/records.xlsx").data))
    try:
        assert downloaded.active["A1"].value == "저장 시각"
        assert downloaded.active["C2"].value == "INC-20261125-123ABC"
    finally:
        downloaded.close()

    old = normalize({**payload(), "incident-id": "INC-20261125-123ABC"})
    assert save_row(path, old) is False
    new = normalize(payload())
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: save_row(path, new), range(2))) == [False, True]
    workbook = load_workbook(path)
    try:
        assert workbook.active.max_row == 3
        assert workbook.active["F2"].value == "기존 상담"
        assert workbook.active["G3"].value == "테스트 고객"
    finally:
        workbook.close()


def test_staff_can_read_confirmed_receipt_without_excel_handoff(demo, orchestrator, monkeypatch):
    monkeypatch.setenv("FINGUARD_EXCEL_INTAKE_URL", "http://127.0.0.1:1")
    owner = customer("excel-test")
    report_path, _ = submit(owner)
    staff_path = QUEUE + "/" + report_path.rsplit("/", 1)[-1]
    agent = employee()
    assert approve(agent, staff_path).status_code == 303
    assert 'id="crm-receipt"' not in agent.get(staff_path).text
    args = inputs(orchestrator)
    receipt = robot(owner, args["in_RequestId"], args["in_DispatchId"])
    assert receipt.status_code == 201
    assert robot(owner, args["in_RequestId"], args["in_DispatchId"]).status_code == 200
    receipt_path = f"{QUEUE}/receipt/{receipt.json['incident_id']}"
    assert owner.get(receipt_path).status_code in (302, 403)
    detail = agent.get(receipt_path)
    assert detail.status_code == 200
    for key, _, _ in FIELDS:
        assert f'id="crm-{key}"' in detail.text
    assert receipt.json["incident_id"] in detail.text
    assert 'id="open-excel-form"' in detail.text
    assert 'action="/consultation/customer-reports/excel/open"' in detail.text
    assert 'excel-popup.js' in detail.text
    assert "기업 내부 사이트" in detail.text
    assert "한국 시간" not in detail.text
    assert agent.get(staff_path + "/excel-ready").status_code == 404


def test_staff_can_open_empty_excel_form_without_entering_token(demo, monkeypatch, tmp_path):
    monkeypatch.setenv("FINGUARD_EXCEL_INTAKE_TOKEN", TOKEN)
    agent = employee()
    path = QUEUE + "/excel/open"
    assert agent.post(path).status_code == 403
    assert customer("excel-outsider").post(path).status_code in (302, 403)
    token = re.search(r'name="csrf_token" value="([^"]+)"', agent.get(QUEUE).text)[1]
    handoff = agent.post(path, data={"csrf_token": token})
    assert handoff.status_code == 200
    assert handoff.headers["Cache-Control"] == "no-store"
    assert TOKEN not in handoff.text
    assert 'action="http://127.0.0.1:5001/login/crm"' in handoff.text
    ticket = re.search(r'name="ticket" value="([^"]+)"', handoff.text)[1]

    workbook = tmp_path / "receipts.xlsx"
    excel = create_app(token=TOKEN, workbook_path=workbook).test_client()
    assert excel.post("/login/crm", data={"ticket": "invalid"}).status_code == 403
    form = excel.post("/login/crm", data={"ticket": ticket}, follow_redirects=True)
    assert form.status_code == 200
    assert 'id="excel-intake-form"' in form.text
    assert "기업 외부 사이트" in form.text
    assert "한국 시간" not in form.text
    assert 'id="excel-customer-name" name="customer-name" maxlength="200" value=""' in form.text
    assert not workbook.exists()
    assert excel.get("/records").status_code == 200
    assert excel.post("/login/crm", data={"ticket": ticket}).status_code == 403

    # Reopening Excel from CRM must not invalidate a form in another popup.
    first_csrf = re.search(r'name="csrf" value="([^"]+)"', form.text)[1]
    second_handoff = agent.post(path, data={"csrf_token": token})
    second_ticket = re.search(r'name="ticket" value="([^"]+)"', second_handoff.text)[1]
    assert excel.post("/login/crm", data={"ticket": second_ticket}, follow_redirects=True).status_code == 200
    assert excel.post("/", data={**payload(), "csrf": first_csrf}).status_code == 303
