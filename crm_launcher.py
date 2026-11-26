"""Windows local CRM startup. Credentials stay in the process or ignored dotenv files."""

import argparse
import msvcrt
import os
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import UUID, uuid4

from dotenv import dotenv_values, set_key
from uipath.llm_client.settings.platform.utils import try_parse_access_token
from werkzeug.serving import ThreadedWSGIServer


ROOT = Path(__file__).resolve().parent
GATEWAY_KEYS = ("UIPATH_ACCESS_TOKEN", "UIPATH_URL", "UIPATH_TENANT_ID", "UIPATH_ORGANIZATION_ID")
LOCAL_KEYS = (
    "FINGUARD_SECRET_KEY", "FINGUARD_ROBOT_TOKEN", "FINGUARD_ORCHESTRATOR_URL",
    "FINGUARD_ORCHESTRATOR_CLIENT_ID", "FINGUARD_ORCHESTRATOR_CLIENT_SECRET",
    "FINGUARD_ORCHESTRATOR_TOKEN", "FINGUARD_ORCHESTRATOR_SCOPE",
    "FINGUARD_ORCHESTRATOR_FOLDER", "FINGUARD_ORCHESTRATOR_FOLDER_KEY",
    "FINGUARD_ORCHESTRATOR_PROCESS",
    "FINGUARD_EXCEL_INTAKE_TOKEN",
)


def load_local_environment():
    overrides = {key: value for key, value in dotenv_values(ROOT / ".env.local").items()
                 if key not in GATEWAY_KEYS}
    config = {**dotenv_values(ROOT / ".env"), **overrides}
    for name, value in config.items():
        if value is not None:
            os.environ.setdefault(name, value)
    if "FINGUARD_ROBOT_TOKEN" not in os.environ:
        # Reuse the dedicated Windows user setting when running as that user.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token, _ = winreg.QueryValueEx(key, "FINGUARD_ROBOT_TOKEN")
        except OSError:
            return
        if isinstance(token, str):
            os.environ["FINGUARD_ROBOT_TOKEN"] = token


def valid_url(raw, *, tenant_only=False):
    try:
        url = urlsplit(raw)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.query or url.fragment):
            return False
        if not tenant_only:
            return True
        segments = url.path.strip("/").split("/")
        return len(segments) == 2 and all(segments)
    except ValueError:
        return False


def save_local_config():
    values = {key: os.environ[key] for key in LOCAL_KEYS if os.environ.get(key, "").strip()}
    if not values:
        print("No FinGuard process settings to save.", file=sys.stderr)
        return 1
    for key, value in values.items():
        set_key(ROOT / ".env.local", key, value)
    print("FinGuard process settings saved to the local .env.local file.")
    return 0


def configuration_issues():
    value = os.environ.get
    issues = []
    for name in (*GATEWAY_KEYS, "FINGUARD_SECRET_KEY", "FINGUARD_ORCHESTRATOR_URL"):
        if not value(name, "").strip():
            issues.append(f"{name}: missing")
    for name, tenant_only in (("UIPATH_URL", False), ("FINGUARD_ORCHESTRATOR_URL", True)):
        if value(name, "").strip() and not valid_url(value(name), tenant_only=tenant_only):
            issues.append(f"{name}: invalid HTTPS URL")

    token = value("FINGUARD_ROBOT_TOKEN", "")
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        issues.append("FINGUARD_ROBOT_TOKEN: expected 32 or more printable non-space ASCII characters")
    if not value("FINGUARD_ORCHESTRATOR_TOKEN", "").strip():
        for name in ("FINGUARD_ORCHESTRATOR_CLIENT_ID", "FINGUARD_ORCHESTRATOR_CLIENT_SECRET"):
            if not value(name, "").strip():
                issues.append(f"{name}: missing")
    if value("FINGUARD_ORCHESTRATOR_FOLDER_KEY"):
        try:
            UUID(value("FINGUARD_ORCHESTRATOR_FOLDER_KEY"))
        except ValueError:
            issues.append("FINGUARD_ORCHESTRATOR_FOLDER_KEY: invalid UUID")
    if value("FINGUARD_SECURE_COOKIE") == "1":
        issues.append("FINGUARD_SECURE_COOKIE: disable for local HTTP login")
    return issues


def gateway_expired():
    claims = try_parse_access_token(os.environ.get("UIPATH_ACCESS_TOKEN", ""))
    expires = claims.get("exp") if isinstance(claims, dict) else None
    return type(expires) in (int, float) and expires <= time.time()


def refresh_gateway():
    print("UiPath Gateway token expired. Complete sign-in in the browser.", flush=True)
    # Keep the tenant URL for host selection, but do not pass the stale token.
    child_env = {key: value for key, value in os.environ.items() if key != "UIPATH_ACCESS_TOKEN"}
    with TemporaryDirectory(prefix="finguard-uipath-auth-") as temporary:
        result = subprocess.run(
            [sys.executable, "-c", "from uipath._cli import cli; cli()", "auth", "--force"],
            cwd=temporary, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode:
            return False
        fresh = dotenv_values(Path(temporary) / ".env")
    if any(not fresh.get(key) for key in GATEWAY_KEYS):
        return False
    if fresh["UIPATH_ACCESS_TOKEN"] == os.environ.get("UIPATH_ACCESS_TOKEN"):
        return False
    for key in GATEWAY_KEYS:
        os.environ[key] = fresh[key]
    if gateway_expired():
        return False
    for key in GATEWAY_KEYS:
        set_key(ROOT / ".env", key, fresh[key])
    return True


def check_settings(check_only):
    issues = configuration_issues()
    if gateway_expired():
        if check_only or issues:
            issues.append("UIPATH_ACCESS_TOKEN: expired")
        elif not refresh_gateway():
            issues.append("UiPath reauthentication did not produce a fresh token. Run uipath auth --force.")
        else:
            issues = configuration_issues()
    for issue in issues:
        print(issue, file=sys.stderr)
    return not issues


class LocalServer(ThreadedWSGIServer):
    allow_reuse_address = False
    daemon_threads = False

    def server_bind(self):
        import socket

        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def open_when_ready(url):
    try:
        with build_opener(ProxyHandler({})).open(url + "/health", timeout=5) as response:
            if response.status == 200:
                webbrowser.open(url + "/")
    except OSError:
        print("Open the CRM in your browser using the displayed URL.")


def start_server(state_dir, port, no_browser):
    # Keep the lock during validation, sign-in and server lifetime.
    with (state_dir / "lock").open("a+b") as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("Another FinGuard launcher is already running.", file=sys.stderr)
            return 1
        return serve(state_dir, port, no_browser)


def serve(state_dir, port, no_browser):
    import socket

    # Fail before starting interactive authentication when another process owns the port.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            print(f"TCP port {port} is already in use. The existing process was left running.",
                  file=sys.stderr)
            return 1
    if not check_settings(check_only=False):
        return 1

    from app import app

    excel_token = os.getenv("FINGUARD_EXCEL_INTAKE_TOKEN", "")

    run_id = uuid4().hex
    active = state_dir / "active"
    temporary = state_dir / f"active-{run_id}.tmp"
    stop = state_dir / f"stop-{run_id}"
    url = f"http://127.0.0.1:{port}"
    try:
        with ExitStack() as servers:
            server = servers.enter_context(LocalServer("127.0.0.1", port, app))
            server.timeout = 0.25
            if excel_token:
                start_local_excel(servers, excel_token)
            temporary.write_text(run_id, encoding="ascii")
            temporary.replace(active)
            print(f"FinGuard CRM ready at {url}", flush=True)
            if not no_browser:
                threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
            while not stop.exists():
                server.handle_request()
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if active.exists() and active.read_text(encoding="ascii") == run_id:
            active.unlink()
        stop.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    return 0


def start_local_excel(servers, token):
    from excel_intake import create_app

    excel_app = create_app(
        token=token, workbook_path=ROOT / ".uipath" / "excel-intake" / "receipts.xlsx",
    )
    excel_server = servers.enter_context(LocalServer("127.0.0.1", 5001, excel_app))
    worker = threading.Thread(target=excel_server.serve_forever)
    worker.start()
    servers.callback(worker.join)
    servers.callback(excel_server.shutdown)
    print("FinGuard Excel intake ready at http://127.0.0.1:5001", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start",))
    parser.add_argument("--port", type=int, default=5000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--save-local-config", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.save_local_config:
        return save_local_config()
    load_local_environment()
    if args.check_only:
        if not check_settings(check_only=True):
            return 1
        print("FinGuard CRM environment check passed.")
        return 0
    state_dir = ROOT / ".uipath" / f"crm-server-{args.port}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return start_server(state_dir, args.port, args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
