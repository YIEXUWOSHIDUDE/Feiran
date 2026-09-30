"""Optional single-owner HTTP authentication, outside the app's per-start CSRF token.

CloudFront supplies HTTPS and forwards Authorization without caching. The origin hostname
is explicit; no forwarded header grants access. Secrets are read once at startup, never logged.
"""
import base64
import binascii
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class OwnerAccess:
    host: str
    credential: bytes = field(repr=False)

    @classmethod
    def from_environment(cls):
        host = os.environ.get("WORKBENCH_PUBLIC_HOST", "").strip().lower()
        if not host:
            return None
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host):
            raise ValueError("WORKBENCH_PUBLIC_HOST must be one origin hostname")
        try:
            value = json.loads(Path(os.environ["WORKBENCH_LOGIN_FILE"]).read_text())
            username, password = value["username"], value["password"]
            if username != "feiran" or not isinstance(password, str) or len(password) < 32 or ":" in username:
                raise ValueError()
            credential = (username + ":" + password).encode("utf-8")
        except (KeyError, OSError, ValueError, TypeError):
            raise ValueError("Public access requires a valid owner login secret") from None
        return cls(host, credential)

    def refusal(self, request):
        # A health response contains only service status. All documents and APIs need login,
        # including requests through localhost/SSM once public mode is enabled.
        if request.url.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return None
        authorization = request.headers.get("authorization", "")
        supplied = b""
        if len(authorization) <= 1024:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "basic":
                try:
                    supplied = base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error):
                    pass
        if secrets.compare_digest(supplied, self.credential):
            return None
        return JSONResponse({"error": "请先登录 Feiran"}, status_code=401, headers={
            "WWW-Authenticate": 'Basic realm="Feiran", charset="UTF-8"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })
