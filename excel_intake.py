"""Local Excel-backed intake site, run independently on port 5001."""

import hmac
import io
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4
from zipfile import BadZipFile

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import BadSignature
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from services.excel_signin import TICKET_LIFETIME, login_tickets


ROOT = Path(__file__).resolve().parent
FIELDS = (
    ("customer-name", "고객명", 200),
    ("report-id", "고객 신고번호", 37),
    ("incident-id", "사고 접수번호", 50),
    ("damage-type", "금융 피해 유형", 64),
    ("damage-amount", "피해 금액", 32),
    ("occurred-at", "피해 발생 시각", 32),
    ("conversation", "상담 내용", 10000),
    ("ai-summary", "AI 상담 요약", 32767),
    ("fds-risk", "FDS 위험도", 32),
    ("assigned-agent", "담당 상담원", 100),
    ("received-at", "CRM 접수 시각", 32),
)
# Keep the first six columns readable in workbooks written by earlier releases.
COLUMNS = ("엑셀 저장 시각", "요청 ID", "사고 접수번호", "금융 피해 유형",
           "FDS 위험도", "상담 내용", "고객명", "고객 신고번호", "피해 금액",
           "피해 발생 시각", "AI 상담 요약", "담당 상담원", "CRM 접수 시각")
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def normalize(data):
    values = {}
    for key, _, limit in FIELDS:
        value = data.get(key, "").strip()
        if not value or len(value) > limit or ILLEGAL_CHARACTERS_RE.search(value):
            raise ValueError
        values[key] = value
    if (not re.fullmatch(r"RPT-[a-f0-9]{32}", values["report-id"])
            or not re.fullmatch(r"INC-[0-9]{8}-[A-Z0-9]+", values["incident-id"])
            or not re.fullmatch(r"(?:[0-9]+|미입력)", values["damage-amount"])):
        raise ValueError
    for key in ("occurred-at", "received-at"):
        if key == "occurred-at" and values[key] == "미입력":
            continue
        datetime.fromisoformat(values[key])
    return tuple(values[key] for key in (
        "incident-id", "damage-type", "fds-risk", "conversation", "customer-name", "report-id",
        "damage-amount", "occurred-at", "ai-summary", "assigned-agent", "received-at",
    ))


@contextmanager
def workbook_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expires = time.monotonic() + 1
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= expires:
                    raise
                time.sleep(0.05)
        yield


def format_columns(worksheet):
    for index, (label, width) in enumerate(zip(COLUMNS, (24, 43, 25, 24, 16, 64, 24, 43, 16, 27, 64, 25, 28)), 1):
        cell = worksheet.cell(row=1, column=index, value=label)
        cell.value = label
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
        worksheet.column_dimensions[cell.column_letter].width = width
    worksheet.freeze_panes = "B2"


def new_workbook():
    workbook = Workbook()
    workbook.active.title = "접수 목록"
    format_columns(workbook.active)
    return workbook


def save_row(path, values):
    with workbook_lock(path):
        workbook = load_workbook(path) if path.exists() else new_workbook()
        try:
            sheet = workbook["접수 목록"]
            for row in sheet.iter_rows(min_row=2, max_col=len(COLUMNS), values_only=True):
                if row[2] == values[0]:
                    if row[7] is not None and row[2:] != values:
                        abort(409, "같은 사고 접수번호에 다른 내용이 이미 저장되어 있습니다.")
                    return False

            timestamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
            format_columns(sheet)
            sheet.append((timestamp, f"XLS-{uuid4().hex}", *values))
            for cell in sheet[sheet.max_row]:
                # Text beginning with =, +, - or @ must stay literal in Excel.
                cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=cell.column in (6, 11))
            sheet.auto_filter.ref = sheet.dimensions
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temp:
                temp_path = Path(temp.name)
            try:
                workbook.save(temp_path)
                temp_path.replace(path)
            finally:
                temp_path.unlink(missing_ok=True)
            return True
        finally:
            workbook.close()


def read_rows(path):
    with workbook_lock(path):
        if not path.exists():
            return []
        workbook = load_workbook(path, read_only=True)
        try:
            return list(workbook["접수 목록"].iter_rows(min_row=2, max_col=len(COLUMNS), values_only=True))[::-1]
        finally:
            workbook.close()


def create_app(*, token, workbook_path):
    if not token or len(token) < 32 or not token.isascii():
        raise ValueError("FINGUARD_EXCEL_INTAKE_TOKEN에는 32자 이상의 ASCII 값을 설정하세요.")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=hmac.digest(token.encode(), b"FinGuard Excel intake session", "sha256"),
        SESSION_COOKIE_NAME="finguard_excel_session", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=524288,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")
    path = Path(workbook_path)
    tickets = login_tickets(token)
    redeemed = {}
    redemption_lock = Lock()

    @app.context_processor
    def template_context():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf": session["csrf"], "fields": FIELDS}

    @app.before_request
    def access():
        if request.endpoint in ("static", None, "health", "crm_login"):
            return
        if request.method == "POST":
            supplied = request.form.get("csrf", "")
            expected = session.get("csrf", "")
            if not expected or not supplied.isascii() or not hmac.compare_digest(supplied, expected):
                abort(403)
        if request.endpoint != "login" and not session.get("signed_in"):
            return redirect(url_for("login"))

    @app.after_request
    def private_page(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(BadZipFile)
    @app.errorhandler(InvalidFileException)
    @app.errorhandler(OSError)
    def workbook_unavailable(_):
        message = "엑셀 파일을 사용할 수 없습니다. 파일이 Excel에서 열려 있다면 닫고 다시 시도해 주세요."
        return render_template("excel_intake/page.html", view="error", error=message), 503

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        if request.method == "POST":
            supplied = request.form.get("password", "")
            if supplied.isascii() and hmac.compare_digest(supplied, token):
                return sign_in()
            return render_template("excel_intake/page.html", view="login", error="접근 토큰을 확인해 주세요."), 401
        return render_template("excel_intake/page.html", view="login")

    def sign_in():
        session.clear()
        session.permanent = True
        session["signed_in"] = True
        session["csrf"] = secrets.token_urlsafe(32)
        return redirect(url_for("intake"), code=303)

    @app.post("/login/crm")
    def crm_login():
        try:
            nonce = tickets.loads(request.form.get("ticket", ""), max_age=TICKET_LIFETIME)
        except BadSignature:
            abort(403, "CRM에서 엑셀 접수 폼 열기 버튼을 다시 눌러 주세요.")
        with redemption_lock:
            now = time.monotonic()
            for used in tuple(redeemed):
                if redeemed[used] <= now:
                    del redeemed[used]
            if nonce in redeemed:
                abort(403, "CRM에서 엑셀 접수 폼 열기 버튼을 다시 눌러 주세요.")
            redeemed[nonce] = now + TICKET_LIFETIME + 1
        if session.get("signed_in"):
            return redirect(url_for("intake"), code=303)
        return sign_in()

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.route("/", methods=["GET", "POST"])
    def intake():
        if request.method == "GET":
            return render_template("excel_intake/page.html", view="form")
        try:
            values = normalize(request.form)
        except ValueError:
            return render_template("excel_intake/page.html", view="form",
                                   form=request.form, error="접수 항목과 번호 형식을 확인해 주세요."), 400
        saved = save_row(path, values)
        flash("엑셀에 저장했습니다." if saved else "이미 저장된 사고 접수입니다.", "saved")
        return redirect(url_for("records"), code=303)

    @app.errorhandler(409)
    def duplicate_conflict(error):
        return render_template("excel_intake/page.html", view="form", form=request.form,
                               error=error.description), 409

    @app.get("/records")
    def records():
        rows = read_rows(path)
        return render_template("excel_intake/page.html", view="records", rows=rows, total=len(rows), columns=COLUMNS)

    @app.get("/records.xlsx")
    def download():
        with workbook_lock(path):
            if path.exists():
                content = io.BytesIO(path.read_bytes())
            else:
                content = None
        workbook = load_workbook(content) if content is not None else new_workbook()
        try:
            changed = content is None
            for cell in workbook["접수 목록"][1]:
                if isinstance(cell.value, str) and cell.value.endswith(" (한국 시간)"):
                    cell.value = cell.value.removesuffix(" (한국 시간)")
                    changed = True
            if changed:
                content = io.BytesIO()
                workbook.save(content)
            content.seek(0)
        finally:
            workbook.close()
        return send_file(content, mimetype=XLSX_TYPE, as_attachment=True, download_name="FinGuard-접수목록.xlsx")

    return app


if __name__ == "__main__":
    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    try:
        app = create_app(token=os.getenv("FINGUARD_EXCEL_INTAKE_TOKEN"),
                         workbook_path=ROOT / ".uipath" / "excel-intake" / "receipts.xlsx")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    app.run(host="127.0.0.1", port=5001)
