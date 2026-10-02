"""Sign-in through Amazon Cognito's managed login: the OAuth 2.0 authorization code grant with
PKCE, finished by verifying the OpenID Connect ID token with PyJWT.

This module answers only "who is this": a verified (issuer, subject) pair. Which account that
is, whether it may still sign in and what it may read are the store's and the web layer's
decisions (tenant_store.sign_in, UserWorkspace). No password is ever seen here, and nothing
here is a way to sign in without Cognito: tests replace the token endpoint and signing keys
with their own, never through a route of the app.

Official references (checked 2026-09-30): Cognito authorize, token and logout endpoints and
"Verifying JSON web tokens" (iss, aud, token_use, exp, RS256 keys from the pool's JWKS); PKCE
S256 is RFC 7636 section 4.2; PyJWT 2.15.1 jwt.decode and PyJWKClient.
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import jwt

import tenant_store


TOKEN_TIMEOUT_SECONDS = 10
MAX_TOKEN_RESPONSE_BYTES = 64_000
CLOCK_SKEW_SECONDS = 60
MAX_RETURN_TO = 512


class IdentityError(Exception):
    """Sign-in could not be completed; ``reason`` is safe to log and show, the message too."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _https(value: str, name: str) -> str:
    """An https:// URL, or http://localhost, which Cognito accepts only for testing."""
    parsed = urllib.parse.urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.fragment or parsed.query:
        raise ValueError(f"{name} must be an https:// URL without query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class OIDCSettings:
    """Where sign-in happens and how this app is registered there."""

    issuer: str        # https://cognito-idp.<region>.amazonaws.com/<user pool id>
    client_id: str
    domain: str        # the user pool's managed login domain, https://<prefix>.auth.<region>.amazoncognito.com
    public_origin: str  # the address users open, e.g. https://dxxxx.cloudfront.net
    client_secret: str | None = field(default=None, repr=False)
    scopes: str = "openid email"

    def __post_init__(self) -> None:
        _https(self.issuer, "issuer")
        _https(self.domain, "domain")
        _https(self.public_origin, "public origin")
        if not self.client_id or any(character.isspace() for character in self.client_id):
            raise ValueError("client_id is required")
        if "openid" not in self.scopes.split():
            raise ValueError("scopes must include openid")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_origin}/auth/callback"

    @property
    def logout_uri(self) -> str:
        return f"{self.public_origin}/signed-out"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "OIDCSettings | None":
        """None when V2 sign-in is not configured. The client secret is read from a file (on
        AWS the host fetches it from Secrets Manager), never from an environment variable."""
        issuer = environ.get("WORKBENCH_OIDC_ISSUER", "").strip()
        if not issuer:
            return None
        secret_file = environ.get("WORKBENCH_OIDC_CLIENT_SECRET_FILE", "").strip()
        secret = None
        if secret_file:
            try:
                secret = Path(secret_file).read_text(encoding="utf-8").strip() or None
            except OSError:
                raise ValueError("WORKBENCH_OIDC_CLIENT_SECRET_FILE cannot be read") from None
            if secret is None:
                raise ValueError("WORKBENCH_OIDC_CLIENT_SECRET_FILE is empty")
        return cls(issuer=issuer, client_id=environ.get("WORKBENCH_OIDC_CLIENT_ID", "").strip(),
                   domain=environ.get("WORKBENCH_OIDC_DOMAIN", "").strip(),
                   public_origin=environ.get("WORKBENCH_PUBLIC_ORIGIN", "").strip(), client_secret=secret)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A PKCE code verifier and its S256 challenge (RFC 7636 section 4.2)."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, within the 43-128 the RFC allows
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def safe_return_to(value: Any) -> str:
    """A path on this site to go back to after signing in; anything else becomes "/", so the
    sign-in can never send a browser to another site."""
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or "\\" in value
            or len(value) > MAX_RETURN_TO or any(ord(character) < 32 for character in value)):
        return "/"
    return value


def _post_form(url: str, form: dict[str, str], headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TOKEN_TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(MAX_TOKEN_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IdentityError("The sign-in service could not be reached", "unreachable") from exc


class Identity:
    """Starts and finishes sign-ins for one app. ``post`` sends the token request and ``keys``
    finds the key that signed a token; both default to the real network clients."""

    def __init__(self, settings: OIDCSettings, database: Path, *,
                 post: Callable[[str, dict[str, str], dict[str, str]], tuple[int, bytes]] = _post_form,
                 keys: Any = None) -> None:
        self.settings = settings
        self.database = Path(database)
        self.post = post
        self.keys = keys or jwt.PyJWKClient(settings.jwks_uri, cache_jwk_set=True, lifespan=3600, timeout=10)

    def begin(self, return_to: Any) -> tuple[str, str]:
        """The Cognito address to send the browser to, and the value this browser must present
        again at the callback (the caller keeps it in a short-lived HttpOnly cookie)."""
        state, nonce, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        tenant_store.save_login_attempt(self.database, state=state, binding=binding, nonce=nonce,
                                        code_verifier=verifier, return_to=safe_return_to(return_to))
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_uri,
            "scope": self.settings.scopes, "state": state, "nonce": nonce,
            "code_challenge_method": "S256", "code_challenge": challenge,
        }, quote_via=urllib.parse.quote)
        return f"{self.settings.domain}/oauth2/authorize?{query}", binding

    def finish(self, *, code: Any, state: Any, binding: Any) -> tuple[str, str, str]:
        """Verify the callback and return (issuer, subject, return_to). The attempt is used up
        whatever happens next, so a callback URL can never be replayed."""
        attempt = tenant_store.take_login_attempt(self.database, state=state, binding=binding)
        if attempt is None:
            raise IdentityError("This sign-in link has expired or was opened in another browser; sign in again",
                                "state_mismatch")
        if not isinstance(code, str) or not 0 < len(code) <= 2048:
            raise IdentityError("The sign-in service returned no code", "no_code")
        tokens = self._exchange(code, attempt["code_verifier"])
        claims = self.verify_id_token(tokens.get("id_token"), attempt["nonce"])
        return claims["iss"], claims["sub"], attempt["return_to"]

    def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        form = {"grant_type": "authorization_code", "client_id": self.settings.client_id, "code": code,
                "redirect_uri": self.settings.redirect_uri, "code_verifier": verifier}
        headers = {}
        if self.settings.client_secret:
            # RFC 6749 section 2.3.1: form-encode both, then Base64 "id:secret".
            pair = f"{urllib.parse.quote_plus(self.settings.client_id)}:{urllib.parse.quote_plus(self.settings.client_secret)}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        status, body = self.post(f"{self.settings.domain}/oauth2/token", form, headers)
        if len(body) > MAX_TOKEN_RESPONSE_BYTES:
            raise IdentityError("The sign-in service's answer was too large", "bad_response")
        try:
            tokens = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            tokens = None
        if status != 200 or not isinstance(tokens, dict):
            error = tokens.get("error") if isinstance(tokens, dict) else None
            raise IdentityError("The sign-in could not be completed; sign in again",
                                error if error in ("invalid_grant", "invalid_client", "unauthorized_client",
                                                   "invalid_request") else "token_refused")
        return tokens

    def verify_id_token(self, token: Any, nonce: str) -> dict[str, Any]:
        """The ID token's claims, only if Cognito signed it (RS256, a key from this pool's JWKS)
        for this app client, it is unexpired, it is an ID token, and it carries this sign-in's nonce."""
        if not isinstance(token, str) or not token:
            raise IdentityError("The sign-in service returned no ID token", "no_id_token")
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.settings.client_id,
                                issuer=self.settings.issuer, leeway=CLOCK_SKEW_SECONDS,
                                options={"require": ["exp", "iat", "iss", "aud", "sub", "token_use", "nonce"]})
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise IdentityError("The sign-in could not be verified; sign in again", "invalid_token") from exc
        if claims.get("token_use") != "id":
            raise IdentityError("The sign-in could not be verified; sign in again", "invalid_token")
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
            raise IdentityError("The sign-in could not be verified; sign in again", "nonce_mismatch")
        if not isinstance(claims.get("sub"), str) or not claims["sub"].strip() or len(claims["sub"]) > 256:
            raise IdentityError("The sign-in could not be verified; sign in again", "invalid_token")
        return claims

    def logout_url(self) -> str:
        """Cognito's sign-out, which then returns the browser to this site's signed-out page.
        The app's own session is ended before the browser goes there."""
        query = urllib.parse.urlencode({"client_id": self.settings.client_id, "logout_uri": self.settings.logout_uri},
                                       quote_via=urllib.parse.quote)
        return f"{self.settings.domain}/logout?{query}"
