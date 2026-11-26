# FinGuard_AI

## CRM 화면에서 엑셀로 접수

`FINGUARD_EXCEL_INTAKE_TOKEN`을 Git에서 제외된 `.env.local`에 32자 이상 ASCII 비밀값으로 설정합니다.
`.\start.bat`으로 CRM(`127.0.0.1:5000`)과 엑셀 사이트(`127.0.0.1:5001`)를 시작하고
`.\stop.bat`으로 함께 종료합니다. CRM에 로그인한 직원이 **엑셀 접수 폼 열기** 버튼을 누르면
일회용 로그인 티켓으로 인증되어 5001의 빈 폼이 팝업 창에서 열립니다.
5001에 직접 접속하는 경우에는 기존 접수 사이트 로그인을 이용합니다.

CRM 사고 접수는 기존 SQLite와 로봇 콜백을 사용합니다. CRM과 엑셀 사이트 사이의 업무 데이터
전송 API와 JSON 초안은 제거됐습니다. 엑셀은 브라우저의 **접수 등록** 입력을
`.uipath/excel-intake/receipts.xlsx`에 기록합니다. 같은 사고 접수번호는 한 번만 저장됩니다.
**접수 현황**은 이 파일을 표로 표시하고 원본 `.xlsx`를 다운로드할 수 있습니다.
Excel 데스크톱 앱에서 파일을 열어 둔 동안 저장 오류가 나면 파일을 닫고 다시 등록합니다.

### UiPath Studio에서 ExcelIntake.xaml 설정

Windows/VB 프로젝트의 `ExcelIntake.xaml`에는 실제 UiPath Activity와 변수 연결이 들어 있습니다.
두 브라우저와 CRM 11개 필드, 엑셀 입력란 11개 및 팝업 열기 버튼을 실제 화면에서
캡처해 연결했습니다. Orchestrator는 배포된 패키지를 실행하므로 프로젝트 변경은
새 버전을 게시하고 기존 프로세스에 연결한 뒤에 적용됩니다.
`Main.xaml`의 **Orchestrator Job** 분기는 기존 CRM 접수 결과가 `SUCCESS` 또는 `DUPLICATE`이고
사고번호가 있을 때만 엑셀 워크플로를 호출합니다. `in_CrmBaseUrl`과 `in_IncidentId`를 넘기고
`out_ExcelPrepared`를 받습니다. 엑셀 입력 오류는 별도로 기록하며 CRM 결과는 보존합니다.

1. 로봇 Windows 세션의 Chrome에서 이 신고를 볼 권한이 있는 직원 계정으로 CRM에 로그인합니다.
   CRM **Use Application/Browser**에서 해당 Chrome 창을 지정하고 **Open = Never**,
   **Close = Never**, **Attach mode = By instance**로 설정합니다. 무인 실행에서 세션이
   만료될 수 있다면 이 스코프 안에서 **Go To URL** 다음에 **Check App State**로
   로그인 화면의 `#username`을 확인하는 분기를 추가합니다. 권한 있는 계정의 Orchestrator
   Credential Asset을 읽고 **Type Into** `#username`, **Type Into (SecureText)** `#password`,
   **Click** `#crm-login-submit` 순으로 로그인한 뒤 같은 **Go To URL**을 다시 실행합니다.
   고객 신고가 배정된 상담원 계정으로 접근할 수 있는지 확인합니다.
2. **Go To URL**은 CRM의 `/consultation/customer-reports/receipt/<사고번호>`로 이동합니다.
   **Check App State**의 대상은 `#crm-incident-id` (`DD`)입니다. 나타났을 때에만 다음
   11개 **Get Text**가 실행됩니다. 값 타깃의 태그는 모두 `DD`이고, 각 출력은 기존 String 변수에
   연결되어 있습니다.

| Get Text CRM HTML ID | String 변수 | Type Into Excel HTML ID |
| --- | --- | --- |
| `crm-customer-name` | `customerName` | `excel-customer-name` |
| `crm-report-id` | `reportId` | `excel-report-id` |
| `crm-incident-id` | `incidentId` | `excel-incident-id` |
| `crm-damage-type` | `damageType` | `excel-damage-type` |
| `crm-damage-amount` | `damageAmount` | `excel-damage-amount` |
| `crm-occurred-at` | `occurredAt` | `excel-occurred-at` |
| `crm-conversation` | `conversation` | `excel-conversation` |
| `crm-ai-summary` | `aiSummary` | `excel-ai-summary` |
| `crm-fds-risk` | `fdsRisk` | `excel-fds-risk` |
| `crm-assigned-agent` | `assignedAgent` | `excel-assigned-agent` |
| `crm-received-at` | `receivedAt` | `excel-received-at` |

3. 읽은 사고번호가 `in_IncidentId`와 일치하면 **Click**으로 CRM의 `#open-excel-form`
   (`BUTTON`)을 누릅니다. 실제 팝업 열기를 위해 이 Activity의 **Input mode = Hardware Events**를
   설정합니다. CRM이 발급한 로그인 티켓으로 5001에 접근합니다.
4. 엑셀 **Use Application/Browser**는 클릭으로 열린 5001번 Chrome 팝업을 지정합니다.
   **Open = Never**, **Attach mode = By instance**, **Close = Never**를 사용하고 동일한
   5001 탭이나 창이 여럿이면 Studio 지정 단계와 로봇 실행 전에 정리합니다.
   첫 **Type Into**가 `#excel-customer-name` (`INPUT`)이 준비될 때까지 기다립니다.
   위 표의 각 **Type Into**를 실제 입력란에 지정합니다. `conversation`과 `aiSummary`의 태그는
   `TEXTAREA`, 나머지는 `INPUT`입니다. 여러 줄 한국어를 채울 때 줄바꿈과
   대괄호가 보존되는지 샘플 값으로 확인합니다.
5. 11개 입력이 끝나면 `out_ExcelPrepared = True`로 설정하고 팝업을 열어 둡니다.
   상담사는 로봇이 폼을 채운 Chrome 화면에서 값을 검토한 뒤 **접수 등록**을 눌러 저장합니다.
6. 각 타깃에서 **Validate**와 **Highlight**를 실행하고 프로젝트를 빌드합니다.
   실제 실행을 승인받은 뒤에는 먼저 팝업의 11개 값과 제출 전 상태를 확인합니다.
   상담사가 등록한 뒤 **접수 현황 → 엑셀 다운로드 (.xlsx)**에서 사고번호와 항목을 확인합니다.
   엑셀 사이트는 파일 잠금 안에서 사고번호 중복을 확인합니다.

#### Windows에서 Helm이 시작되지 않을 때

기본 `C:\Program Files\dotnet`에 .NET 6만 있고 UiPath에 .NET 10이 포함된 환경에서는,
UiPath CLI를 실행하는 PowerShell 세션에서 두 환경변수를 지정합니다. 설치 경로의 버전은
설치된 UiPath Runtime 폴더와 맞춰야 합니다.

```powershell
$env:DOTNET_ROOT_X64 = 'C:\Program Files\UiPathPlatform\Runtimes\27.0.5'
$env:DOTNET_ROOT = $env:DOTNET_ROOT_X64
```

이 설정 후에도 Helm이 준비 상태를 기록하지 못하면
`%LOCALAPPDATA%\UiPath\Logs\Helm`에 현재 Windows 사용자로 로그를 쓸 수 있는지 확인합니다.
이 작업의 제한된 실행 환경에서는 로그 생성 단계의 `UnauthorizedAccessException`과
UiPath IPC의 `EPERM`을 확인했습니다. 일반 사용자 세션에서 허용된 실행 권한으로 CLI를
다시 실행해 검증과 브라우저 캡처 응답을 확인했습니다.

## 고객 서비스와 상담원 직접 승인

루트 `/`에서 고객 회원가입·로그인, 피해 신고, AI 금융 상담, 본인 신고 이력을 이용합니다.
고객 인증은 프로젝트용 테스트 인증입니다. 회원가입은 별도 테스트 고객 ID를 만들며
기존 CRM 고객 ID와 계정을 연결하는 입력은 받지 않습니다. 따라서 신규 가입 계정에
연결된 거래가 없다면 FDS 화면에도 거래 없음으로 표시됩니다. 금융기관 본인 인증이나
카드 정지·환불을 수행하지 않습니다. 실명, 주민등록번호, 카드 전체 번호, 계좌 비밀번호,
인증번호는 상담 및 신고 본문에도 입력하지 마세요.

| 역할 | 권한 |
|---|---|
| CUSTOMER | 테스트 가입·인증, 피해 신고, AI 상담, 본인 신고 조회, 추가 정보 제출 및 확정 접수번호 조회 |
| AGENT | 고객 신고 큐 조회, 미배정 신고 가져오기, 본인 담당 신고 보완·추가 정보 요청·승인·반려, Job 상태 갱신, 본인의 전화 상담 직접 처리 |
| ADMIN | 직원 계정 관리, 전체 고객 신고·상담·사고·실행 이력, 담당 배정, 업무 현황, Gateway 및 Orchestrator 읽기 전용 연결 확인 |
| SUPERVISOR | 기존 계정과 기록을 보존하고 기존 전화 요청의 호환 절차를 유지합니다. 신규 고객 신고와 새 전화 상담 승인 큐에는 참여하지 않습니다. |

마이그레이션 `005_customer_portal.sql`은 `customer_accounts`, `customer_sessions`,
`customer_reports`, `customer_report_events`, `agent_telephone_requests`를 추가합니다.
신고번호 `RPT-…`는 `automation_requests.request_id`와 1:1로 연결됩니다. 기존 승인
스냅샷, 실행 식별자, 사고 번호, 감사 이력은 기존 테이블을 사용합니다. 운영 중인 SQLite
파일은 마이그레이션 전에 SQLite backup API로 별도 백업하고 `python -m database.migrate`를
실행합니다. 백업은 SQLite 파일과 마찬가지로 Git에 포함하지 않습니다.

1. 기존 `start.bat`으로 실행하고 고객 계정을 생성합니다. 테스트 문장으로 피해 신고를
   등록하고 신고번호를 확인합니다. AI 실패 시 상담원이 원문을 볼 수 있습니다.
2. 다른 브라우저 프로필에서 `/login`으로 AGENT 로그인 후 **고객 신고**를 엽니다.
   미배정 신고를 가져오거나 ADMIN이 상담원을 배정할 수 있습니다.
3. 원문, AI 결과, 거래 기반 FDS를 각각 확인합니다. 필요한 경우 고객에게 추가 정보를
   요청하거나 내부 메모를 남깁니다. 반려할 때는 고객에게 표시할 사유를 작성합니다.
4. 실환경 실행이 승인된 테스트에서만 **승인 및 자동 접수 시작**을 누릅니다. 서버는
   담당 권한과 승인 스냅샷을 확인한 뒤 기존 Orchestrator 서비스를 호출합니다.
   실행 확인이 필요하면 상담원 화면에서 **Job 상태 조회**를 누릅니다. 고객 화면에는
   실제 CRM 접수와 Job 출력이 일치해 완료된 경우에만 접수번호가 표시됩니다.

`python -m pytest -m "not gateway and not orchestrator_live"`는 고객 신고에서 담당 상담원
승인과 모의 Job·로봇 접수까지 검증합니다. 테스트는 임시 DB를 사용합니다.

## UiPath Gateway 상담 분석

루트 Flask 앱과 `FinGuard_AI/`의 CRM·FDS 앱은 각각 해당 디렉터리에서 실행합니다.
두 앱 모두 `UiPathAzureChatOpenAI.with_structured_output(ConsultationResult)`로 Gateway를 호출합니다.

### 실행

Python 3.11 이상과 대상 테넌트의 UiPath Gateway 모델 접근 권한이 필요합니다.
사용할 앱 디렉터리에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
uipath auth
python app.py
```

인증에 사용한 `UIPATH_URL`, `UIPATH_ACCESS_TOKEN`, `UIPATH_TENANT_ID`,
`UIPATH_ORGANIZATION_ID`는 실행 환경이나 앱의 로컬 `.env`에 설정합니다.
`uipath auth`로 발급받은 토큰이 만료되면 다시 인증합니다.
`.env`, `.env.*` 및 `.uipath/`는 Git에서 무시합니다.
[UiPath SDK 인증 안내](https://docs.uipath.com/orchestrator/automation-cloud/latest/user-guide/authenticating-with-interactive-login)

`UIPATH_LLM_MODEL`의 기본값은 `gpt-4.1-mini-2025-04-14`입니다.
다른 모델을 사용하려면 앱의 `.env` 또는 실행 환경에 이 변수를 설정합니다.
Gateway 인증에는 UiPath 자격 증명을 사용합니다.

`POST /api/ai/analyze`에 `{"conversation":"카드를 잃어버렸어요."}`를 보냅니다.
HTTP 200 응답에는 `category`, `recommended_action`, `requires_human_review`,
`suggested_reply`, `summary`가 포함됩니다.
인증, 시간 초과, 응답 형식 등의 오류에는 HTTP 502, `error`,
`recommended_action: ESCALATE_TO_AGENT`, `requires_human_review: true`를 반환합니다.

### 테스트

각 앱 디렉터리에서 아래 명령을 실행합니다. Mock 테스트는 UiPath 인증 없이 실행됩니다.
Gateway 테스트는 별도 실행하며, 모델에 보내는 입력은 가상의 상담 문장입니다.

```powershell
python -m pytest -m "not gateway and not orchestrator_live"
$env:RUN_UIPATH_GATEWAY_TESTS = "1"
python -m pytest -m gateway -v -s -o log_cli=true --log-cli-level=WARNING
Remove-Item Env:RUN_UIPATH_GATEWAY_TESTS
```

Gateway 오류 로그에는 `stage`, `reason`, `gateway_http`만 기록됩니다.
`stage=configure reason=token_expired gateway_http=-`이면 SDK 초기화에서 만료를
감지한 상태입니다. 실제 Gateway HTTP 요청은 이 단계 전에 차단됩니다.
앱 디렉터리에서 `uipath auth --force`로 재인증하고 Flask를 재시작합니다.
PowerShell에 오래된 `UIPATH_ACCESS_TOKEN`이 남아 있으면 로컬 `.env`보다 우선합니다.
갱신된 `.env`를 쓰려면 `Remove-Item Env:UIPATH_ACCESS_TOKEN -ErrorAction SilentlyContinue`로
해당 셸의 토큰 변수를 제거합니다. `FINGUARD_ORCHESTRATOR_*`는 별도 설정입니다.

라이브 테스트는 `/api/ai/analyze`와 로그인·CSRF를 거치는 `/api/automation/requests`를
각각 실제 Gateway로 호출합니다. 상담 요청 테스트는 임시 SQLite DB와 임시 상담원 계정을
사용하고, 저장된 AI 결과와 같은 멱등 키의 재전송을 검증합니다. Job 생성은 수행하지 않습니다.

CLI와 pytest의 환경을 맞추려면 CRM 저장소에서 테스트한 같은 PowerShell과 Python을
사용합니다. Flask CLI가 상위 디렉터리의 dotenv 파일을 먼저 읽는 경로를 피하려면
`$env:FLASK_SKIP_DOTENV = "1"`을 설정한 뒤
`python -m flask --app app run --host 127.0.0.1 --port 5000 --no-reload`로 실행합니다.
앱은 자체 CRM 디렉터리의 `.env`를 읽습니다. 시작 전 기존 서버를 종료하고
`netstat -ano -p tcp`에서 5000 포트의 리스너가 하나인지 확인합니다.

같은 상담 입력과 `Idempotency-Key`의 재전송은 이미 저장된 요청을 반환합니다.
토큰을 복구한 뒤 같은 내용으로 AI를 다시 호출하려면 새 상담 화면에서 새 요청을 등록합니다.
테스트 중 중복 승인 방지를 위해 이전 검토 요청은 승인 대기 목록에서 정리할 수 있습니다.

## 모의 CRM (담당 5)

| URL | 설명 |
|---|---|
| `GET /crm/incidents/new` | 사고 접수 양식 (UiPath 입력 대상) |
| `POST /crm/incidents` | 접수 처리 → 상세 화면으로 이동 |
| `GET /crm/incidents/<접수번호>` | 접수 완료/상세 |
| `GET /crm/incidents` | 접수 목록·처리 상태 |

UiPath 고정 HTML ID

- 입력: `request_id`, `customer_id`, `category`, `risk_level`, `summary`, 버튼 `submit-incident`
- `category`, `risk_level`은 드롭다운이다. UiPath는 Select Item으로 **화면에 보이는 한글 라벨**을 고른다. 요청 JSON 값도 이 라벨과 글자까지 같아야 한다.
  - `category`: `카드 분실`, `카드 부정사용 의심`, `결제 문의`, `일반 문의`, `기타`
  - `risk_level`: `낮음`, `보통`, `높음` (선택, 없으면 `없음`)
- AI는 `UNAUTHORIZED_TRANSACTION`, FDS는 `HIGH` 같은 영문 코드를 돌려준다. 요청 JSON을 만들 때 위 한글 라벨로 바꿔 넣는다. (서버에는 영문 코드로 저장된다.)
- 결과 판단 (UiPath는 제출 후 아래 중 어느 ID가 나타나는지로 판단한다)
  | 나타나는 ID | 의미 | UiPath 처리 |
  |---|---|---|
  | `incident-id` | 접수 성공, 접수번호 회수 | 성공 기록 |
  | `incident-id` + `duplicate-notice` | 이미 접수된 요청, 기존 번호 | 성공 기록 |
  | `form-error` | 입력값 오류 (400) | 재시도 금지, 실패 기록 |
  | `crm-error` | DB 일시 오류 (503) | **같은 request_id로** 재시도, 최대 3회. 그래도 실패하면 실패 기록 |
  | `crm-setup-error` | DB 미초기화 또는 예전 스키마 (500) | 재시도 금지, 실패 기록 (DB 초기화 또는 마이그레이션 필요) |
- 응답을 못 받았거나 중간에 끊겨도(타임아웃, 브라우저 종료) **같은 request_id로 다시 접수**하면 된다. 이미 저장됐으면 기존 번호가 나오고, 저장 전이었으면 새로 접수된다. 반쯤 저장되는 경우는 없다.

## 자동화 요청 기반

기존 DB에는 저장소 루트에서 `python -m database.migrate`를 실행합니다.
이 명령은 기존 데이터를 보존하며 반복 실행할 수 있습니다.
구형 사고 스키마에서는 기존 접수번호와 상태, 위험 점수 및 RPA 상태를 보존하고
상담에서 고객·분류·요약을 연결해 현재 CRM 스키마로 옮깁니다.
다른 파일을 지정하려면 `python -m database.migrate --db 경로`를 사용합니다.
새 DB는 `python database/seed.py` 실행 시 같은 마이그레이션이 적용됩니다.

신규 API는 로그인한 상담원 권한을 확인합니다. 외부 WSGI 인증 모드는 서버에서
신뢰된 사용자 식별자와 권한 검증기가 구성된 경우에만 사용할 수 있습니다.
HTTP 사용자 헤더나 JSON의 승인자 값은 신원으로 사용하지 않습니다.

- `create`와 `read` 권한이 있으면 상담 화면에서 자동화 요청을 생성할 수 있습니다.
- `read` 권한으로 본인 요청을 조회할 수 있습니다. `decide` 권한도 있으면 다른 사용자의 요청을 검토할 수 있습니다.
- `read`와 `decide` 권한이 있으면 승인하거나 거부할 수 있습니다.
- 기존 모의 CRM 직접 접수는 계속 사용할 수 있습니다. 신규 `AUTO-` 요청 ID는 기존 폼에서 신규 접수할 수 없습니다.

| API | 요청과 결과 |
|---|---|
| `POST /api/automation/requests` | JSON `customer_id`, `conversation`과 `Idempotency-Key` 헤더. 최초 201, 같은 사용자가 같은 키·입력으로 재전송하면 200과 기존 요청. 같은 키에 다른 입력은 409 |
| `GET /api/automation/requests/<request_id>` | 요청 상태, AI·FDS 스냅샷, 결정·실행 식별자 조회 |
| `GET /api/automation/requests/<request_id>/events` | 생성과 상태 변경 이력 조회 |
| `POST /api/automation/requests/<request_id>/approve` | 최종 CRM `category`, `summary`, 선택적 `risk_level`을 JSON으로 전송 |
| `POST /api/automation/requests/<request_id>/reject` | JSON `{}`로 거부 기록 |

AI 호출이 실패하면 상담 원문과 FDS 결과를 보존하고 `ai_result=null`,
`PENDING_APPROVAL`, `review_required=true`로 저장합니다. 이때 승인하려면 검토자가
접수 분류와 요약을 직접 작성해야 합니다.

`APPROVED`와 `dispatch_pending=true`인 요청은 아래 Orchestrator 연동에서 실행할 수 있습니다.

## 로컬 상담원 로그인

저장소 루트에서 DB 마이그레이션을 적용하고 필요한 시연용 계정만 생성합니다.
계정 생성 명령은 비밀번호를 입력받아 해시로 저장합니다. 비밀번호는 12~128자입니다.

```powershell
python -m database.migrate
python -m flask --app app create-user demo-agent --role AGENT
python -m flask --app app create-user demo-admin --role ADMIN
$env:FINGUARD_SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))").Trim()
python -m flask --app app run --host 127.0.0.1 --port 5000
```

`http://127.0.0.1:5000/login`에서 로그인합니다. AGENT는 요청을 생성하고 자신의
요청을 조회하고 본인이 등록한 신규 전화 상담을 승인·반려할 수 있습니다.
ADMIN은 직원 계정과 전체 업무 이력을 관리합니다. 기존 SUPERVISOR는 이전 전화 상담
처리를 이어갈 수 있습니다.

- 세션 키를 설정한 실행에서는 Flask 로그인만 상담원 신원으로 사용합니다.
  키를 설정하지 않은 기존 실행에서는 로컬 로그인이 비활성화되고, 기존
  `REMOTE_USER` 연동은 별도 권한 검증기가 구성된 경우에만 사용됩니다.
- 세션은 최대 8시간 유지되며 로그아웃하면 서버에서도 즉시 폐기됩니다.
  JSON 변경 요청은 `X-CSRF-Token`을, HTML 폼은 숨겨진 토큰 필드를 사용합니다.
  로컬 HTTP 설정은 루프백 시연용입니다. HTTPS 배포에서는
  `FINGUARD_SECURE_COOKIE=1`을 설정하고 재시작 간 유지할 세션 키를 외부 비밀 저장소에서 주입합니다.
- 로봇은 별도 Bearer 토큰과 요청별 실행 식별자로 전용 API에 인증합니다.
- 인증된 모드에서 기존 Studio 양식을 사용하려면 브라우저가 로그인 상태여야 합니다.
  URL과 고정 HTML ID는 유지했습니다.

## UiPath Orchestrator Jobs 연동

### 실제 E2E 결과

2026-11-25 KST에 DefaultTenant의 Shared 폴더에서 **FinGuard_AI 1.0.3**을 Production
Unattended로 실행했습니다. 상담 분석과 승인 이후 Orchestrator는 `Successful`,
로봇은 `SUCCESS`, CRM은 `SUCCEEDED`를 기록했습니다. RPA 실행 시간은 5.086초였습니다.
SQLite에서 승인·Job·로봇 접수 감사 이력이 연결되고 같은 요청의 사고가 정확히 한 건임을
확인했습니다. 이전 `ROBOT_CONFIG` 실패 요청의 사고는 0건이었습니다.
실행 로그에는 패키지 1.0.3과 `Token=User CrmUrl=User`가 기록됐습니다.

### 로컬 시작

기존 DB에 마이그레이션이 필요하면 저장소 루트에서 `python -m database.migrate`를
실행합니다. 시작 스크립트는 스크립트 위치를 기준으로 CRM 디렉터리를 선택하고
Conda의 `uipath-test` 환경에서 실행합니다. 환경 이름이 다르면 `-CondaEnv <이름>`으로
지정하고, 별도 Python을 쓰는 경우 `-Python 'C:\path\to\python.exe'`를 지정합니다.

Gateway 인증은 CRM 디렉터리의 `.env`에 유지합니다. 기존 `FINGUARD_SECRET_KEY`,
`FINGUARD_ROBOT_TOKEN`, `FINGUARD_ORCHESTRATOR_URL` 및 Orchestrator Client ID·Secret 또는
토큰은 한 번만 `.env.local`에 저장합니다. 기존 셸 값은 파일보다 우선합니다.
로봇 토큰이 셸과 파일에 없으면 현재 Windows 사용자의 User 환경변수를 조회합니다.
두 dotenv 파일은 Git에서 제외된 평문 파일이므로 로그인 계정만 접근할 수 있는
프로젝트 디렉터리에 두고 공유하지 않습니다. `FINGUARD_CRM_BASE_URL`은 로봇의 실행
환경에 설정합니다.

기존 값이 이미 PowerShell에 설정되어 있다면 그 셸에서
`.\start.ps1 -SaveLocalConfig`를 한 번 실행해 지원되는 FinGuard 설정을 `.env.local`에
저장할 수 있습니다. 이 모드는 서버를 시작하지 않으며 Gateway 자격 증명은 복사하지 않습니다.

JWT 만료가 확인된 경우에만 일반 시작에서 UiPath 브라우저 재인증을 엽니다. 성공하면
갱신된 Gateway 인증 정보를 프로젝트 `.env`에 저장합니다. 기존 Flask 세션 키와 로봇
토큰을 새로 생성하지 않습니다. 셸에 이전 Gateway 토큰을 별도로 설정해 뒀다면 제거하고,
최초 인증이나 만료 여부를 판독할 수 없는 토큰의 인증 실패에는 `uipath auth --force`를 사용합니다.

```powershell
.\start.ps1 -CheckOnly
.\start.ps1
.\stop.ps1
```

`start.bat`과 `stop.bat`은 더블클릭으로 실행할 수 있습니다. `-CheckOnly`는 현재
설정만 검증합니다. 일반 시작은 실행 잠금과 Windows 루프백 포트의 배타적 바인딩을
사용해 중복 실행을 막습니다. 이미 5000 포트를 사용하는 서버는 계속 실행됩니다.
검증 오류는 변수 이름과 원인만 표시합니다. `/health`가 정상 응답하면 브라우저에
고객 홈(`/`)을 엽니다. 직원은 홈의 **직원 로그인**에서 접속할 수 있습니다.
`-NoBrowser`로 자동 접속을 끌 수 있습니다. 종료 스크립트는
이 시작 스크립트로 실행한 인스턴스에 정상 종료를 요청합니다.

CRM에서 테스트 상담을 등록·분석하고 승인자가 검토합니다. 실행이 허용된 승인 요청에서
**자동화 실행**을 한 번 누른 뒤 **Job 상태 조회**로 성공과 접수번호를 대조합니다.
이미 실행된 요청에는 기존 Job을 조회합니다. 아래 라이브 pytest는 Gateway와
게시 프로세스를 각각 확인하며 Orchestrator Job을 생성하지 않습니다.

전화 상담 검토 화면에서 **승인된 요청 및 Job 이력**을 엽니다. AGENT는 자신의 전화
상담을 실행하고 조회할 수 있습니다. ADMIN은 기존 전화 상담 실행 권한을 유지합니다.
기존 SUPERVISOR는 기존 전화 요청에만 실행 권한을 갖습니다. 외부 WSGI 인증 모드는
서버 권한 검증기에서 `read`, `decide`, `dispatch`를 부여한 사용자로 운영합니다.
실행할 때 서버가 승인자와 승인 이력을 확인하고 DB에 실행을 선점한 후 Job을 한 번 시작합니다.
실행 응답을 잃으면 `UNKNOWN`으로 기록합니다. 상태 조회는 폴더의 기존 Job에서 요청 ID와
실행 식별자를 대조해 Job을 복구합니다. 검색 결과가 아직 없으면 실행은 잠긴 상태로 유지됩니다.

### 환경변수

| 환경변수 | 설정 |
|---|---|
| `FINGUARD_ORCHESTRATOR_URL` | Flask 환경의 테넌트 URL. 예: `https://cloud.uipath.com/<organization>/DefaultTenant` |
| `FINGUARD_ORCHESTRATOR_FOLDER` | Flask 환경. 기본값 `Shared`. Release와 Job 요청에 폴더 경로를 직접 전달합니다. |
| `FINGUARD_ORCHESTRATOR_FOLDER_KEY` | 선택 설정. 값이 있으면 폴더 경로 대신 이 UUID를 사용합니다. |
| `FINGUARD_ORCHESTRATOR_PROCESS` | Flask 환경. 기본값 `FinGuard_AI`. Shared에 배포된 프로세스 표시 이름과 일치해야 합니다. |
| `FINGUARD_ORCHESTRATOR_CLIENT_ID`, `FINGUARD_ORCHESTRATOR_CLIENT_SECRET` | Flask 환경의 External Application 클라이언트 자격 증명 |
| `FINGUARD_ORCHESTRATOR_SCOPE` | Flask 환경. 기본값 `OR.Execution OR.Jobs.Read OR.Folders OR.Robots.Read`. 앱에 등록된 범위에 맞춥니다. |
| `FINGUARD_ORCHESTRATOR_TOKEN` | Flask 환경. 진단 등에 사용할 단기 토큰. 지정 시 Client ID와 Secret 대신 사용합니다. |
| `FINGUARD_ROBOT_TOKEN` | Flask와 Robot 업무 프로세스 양쪽에 주입할 동일한 32자 이상 ASCII 비밀값 |
| `FINGUARD_CRM_BASE_URL` | Robot 업무 프로세스에서 접근 가능한 CRM 기본 URL. 로컬 시연은 `http://127.0.0.1:5000`, 다른 호스트는 HTTPS를 사용합니다. |

1.0.3의 `Main.xaml`은 로봇용 토큰과 CRM URL을 각각 **Process → User** 순서로 읽습니다.
Process 값이 없거나 공백이면 Executor를 실행하는 Windows 계정의 User 값을 읽습니다.
Machine 범위를 조회하는 별도 호출은 없습니다. 로봇 로그에는 `Token=User CrmUrl=Process`
처럼 조회 범위만 남으며, 값이 없거나 조회 전에 종료되면 `Missing` 또는 `Unread`로 표시합니다.
Flask는 계속 프로세스 환경의 토큰을 쓰므로 로봇과 같은 비밀값을 주입합니다.

로봇 설정 오류는 `ROBOT_REQUEST_ID_MISSING`, `ROBOT_DISPATCH_ID_MISSING`,
`ROBOT_TOKEN_MISSING`, `ROBOT_CRM_URL_INVALID`로 구분됩니다. Flask의 Job 상태 조회는
1.0.1의 `ROBOT_CONFIG`와 새 패키지의 세부 오류 코드를 모두 표시합니다.

토큰과 Secret은 비밀 저장소 또는 실행 환경으로 주입합니다. Windows 로그인 자격 증명은
Orchestrator에서 관리합니다. External Application에 `Shared`의 프로세스 조회 및
Job 생성·조회 권한을 직접 할당합니다. Release 조회 및 `StartJobs`는 인증된 HTTP
클라이언트로 폴더 헤더를 보냅니다. Job 조회와 응답 유실 복구는 SDK 2.15.6의
`jobs.retrieve`와 `jobs.list`에 같은 폴더 경로 또는 Key를 전달합니다. ASCII 경로는
`X-UIPATH-FolderPath`, 그 외 경로는 `X-UIPATH-FolderPath-Encoded`를 사용합니다.
`StartJobs`는 재전송을 차단한 단일 HTTP 요청으로 보내며 Job UUID Key를 저장합니다.

### API 및 DB

| API | 용도 |
|---|---|
| `POST /api/automation/requests/<request_id>/dispatch` | 승인 요청을 실행합니다. JSON `{}`와 실행 권한이 필요합니다. |
| `POST /api/automation/requests/<request_id>/refresh-job` | 기존 Job을 찾거나 상태와 출력을 조회합니다. JSON `{}`와 실행 권한이 필요합니다. |
| `POST /api/automation/robot/requests/<request_id>/receipt` | Bearer 인증과 JSON `{"dispatch_id":"..."}`로 접수합니다. 최초 201, 중복 200이며 동일 접수번호를 반환합니다. |
| `GET /consultation/executions` | 승인된 실행 대기 요청과 Job 이력을 표시합니다. |

로컬 로그인 모드에서 상담원 POST는 기존 CSRF 토큰을 사용합니다. 로봇은 고객 ID, 분류,
요약, 위험도를 서버의 승인 스냅샷에서 읽습니다. Job이 `Successful`이어도 출력의 요청 ID와
접수번호가 CRM의 실제 레코드와 일치해야 업무가 `SUCCEEDED`가 됩니다.

`python -m database.migrate`는 `orchestrator_executions`에 실행 식별자, Job Key,
상태 및 결과를 기록할 구조를 추가합니다. `orchestrator_execution_events`는 기존
`automation_events`에 Job 및 접수 세부 정보를 연결합니다.

### Studio 확인 및 Publish

1. Studio Desktop에서 `uipath/project.json`을 열고 `Main.xaml`의 Arguments를 확인합니다.
   입력은 `in_RequestId`, `in_DispatchId`이고 출력은 `out_RequestId`, `out_IncidentId`,
   `out_Status`, `out_Error`입니다. 모두 String입니다.
2. Validate File과 Validate Project를 실행합니다. 입력이 빈 로컬 테스트에서는
   `Data/Config.json`의 경로와 브라우저 로그인 상태를 확인하고 기존 `request.json`,
   화면 셀렉터, `result.json` 및 이력 기반 중복 방지를 검증합니다. Job 경로에서는
   자동화 요청과 함께 두 입력을 전달하고 Invoke Code VB 컴파일, Robot 환경변수,
   CRM 네트워크 접근 및 출력 인수 반환을 확인합니다.
3. Studio의 **Publish**에서 배포할 버전을 확인하고 **Orchestrator Tenant Processes Feed**로 게시합니다.
   Orchestrator **DefaultTenant → Shared → Automations → Processes**에서 패키지와
   `Main.xaml`을 선택해 프로세스를 추가하거나 기존 `FinGuard_AI`의 패키지 버전을 올립니다.
   `FinGuard-Robot`과 `FinGuard-Machine`의 폴더 할당 및 Production Unattended 슬롯을 확인합니다.
   Job은 사용 가능한 할당을 동적으로 선택합니다.
4. Flask를 실행하고 테스트 상담을 생성·승인한 뒤 CRM에서 자동화 실행을 누릅니다.
   Shared의 Jobs에서 동일 UUID와 Robot 할당을 확인하고, CRM에서 Job 상태를 조회해
   `out_IncidentId`와 사고 상세 화면의 접수번호를 대조합니다. Job 경로는 로컬 JSON 파일을 사용하지 않습니다.

### 테스트 실행

```powershell
python -m pytest -m "not gateway and not orchestrator_live"
$env:FINGUARD_ORCHESTRATOR_SCOPE = "OR.Default"
$env:FINGUARD_ORCHESTRATOR_FOLDER = "Shared"
Remove-Item Env:FINGUARD_ORCHESTRATOR_FOLDER_KEY -ErrorAction SilentlyContinue
python -m rpa.diagnose_orchestrator
$env:FINGUARD_RUN_ORCHESTRATOR_LIVE = "1"
python -m pytest -m orchestrator_live -v -s
```

라이브 pytest는 배포 프로세스 조회만 수행합니다. 실제 Job 발행과 CRM 접수 확인은 위의
Studio·Orchestrator 절차에서 별도로 수행합니다.

### 403 읽기 전용 진단

자격 증명을 설정한 PowerShell에서 `python -m rpa.diagnose_orchestrator`를 실행합니다.
`OR.Default`를 사용한다면 `FINGUARD_ORCHESTRATOR_SCOPE`에 명시합니다. 기존 토큰
환경변수가 설정된 경우 OAuth 발급 단계는 건너뜁니다. Client Credentials 자체를
확인할 때는 해당 변수만 제거한 뒤 진단합니다.

SDK 2.15.6의 `GetFoldersForCurrentUser`는 Client Credentials에서 403을 반환할 수
있습니다. 일반 연결과 기본 진단은 이 API 없이 폴더 경로를 사용합니다. 탐색 API를
별도로 확인하려면 `python -m rpa.diagnose_orchestrator --probe-sdk-folder`를 실행합니다.
선택 설정으로 Shared의 `Key` UUID를 지정하면 진단에서 두 헤더 방식을 비교합니다.
Release 조회의 `Processes: View`는 Shared의 Automation User 역할에 포함됩니다.
`OR.Default`의 역할 기반 접근에는 Confidential 앱에 직접 할당한 역할이 적용됩니다.
스크립트는 OAuth와 선택된 조회 단계를 구분해서 출력하고, 서버 원문 대신
허용된 오류 코드와 고정 메시지를 사용합니다. Job 생성과 실행 API는 호출하지 않습니다.
