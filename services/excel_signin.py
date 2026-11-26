"""Sign short-lived browser login tickets without including receipt data."""

import hmac

from itsdangerous import URLSafeTimedSerializer


TICKET_LIFETIME = 30


def login_tickets(token):
    if len(token) < 32 or not token.isascii():
        raise ValueError("엑셀 접수 사이트 접근 토큰을 확인해 주세요.")
    key = hmac.digest(token.encode("ascii"), b"FinGuard CRM to Excel login", "sha256")
    return URLSafeTimedSerializer(key, salt="staff-login")
