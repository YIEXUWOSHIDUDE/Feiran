"""A stand-in Cognito for tests only: its own RSA key, JWKS and token endpoint.

Nothing in the app imports this module; the app can only be given it by a test that builds
the app itself. It is never a sign-in route.
"""
import base64
import hashlib
import json
import time
import urllib.parse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
CLIENT_ID = "test-client-id"
DOMAIN = "https://feiran-test.auth.us-east-1.amazoncognito.com"
ORIGIN = "https://feiran.example"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeys(jwt.PyJWKClient):
    """PyJWT's real JWKS client, reading this test's key set instead of the network."""

    def __init__(self, jwks):
        super().__init__("https://keys.invalid/jwks.json", cache_jwk_set=False)
        self.jwks = jwks

    def fetch_data(self):
        return self.jwks


class FakeCognito:
    """Signs ID tokens and answers the token endpoint for codes it handed out, checking PKCE."""

    def __init__(self, *, secret=None):
        self.key = _key()
        self.kid = "test-key-1"
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.keys = FakeKeys({"keys": [{**public, "kid": self.kid, "alg": "RS256", "use": "sig"}]})
        self.secret = secret
        self.codes = {}
        self.requests = []
        self.override = None  # a function(claims) -> claims or a raw token, to break tokens on purpose

    def token(self, claims, key=None, headers=None, algorithm="RS256"):
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": self.kid, **(headers or {})})

    def claims(self, subject, nonce, **extra):
        now = int(time.time())
        return {"iss": ISSUER, "aud": CLIENT_ID, "sub": subject, "token_use": "id", "nonce": nonce,
                "iat": now, "exp": now + 3600, "auth_time": now, "email": f"{subject}@example.com", **extra}

    def authorize(self, url, subject):
        """What the browser gets back after signing in at ``url`` (an authorize URL): the
        callback's query string, with a fresh code bound to the challenge and nonce."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"subject": subject, "nonce": query["nonce"], "challenge": query["code_challenge"],
                            "redirect_uri": query["redirect_uri"]}
        return {"code": code, "state": query["state"]}

    def post(self, url, form, headers):
        self.requests.append((url, form, headers))
        assert url == f"{DOMAIN}/oauth2/token"
        if self.secret is not None:
            expected = base64.b64encode(f"{CLIENT_ID}:{self.secret}".encode()).decode()
            if headers.get("Authorization") != f"Basic {expected}":
                return 400, b'{"error":"invalid_client"}'
        issued = self.codes.pop(form.get("code"), None)
        if issued is None or form.get("grant_type") != "authorization_code":
            return 400, b'{"error":"invalid_grant"}'
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if challenge != issued["challenge"] or form.get("redirect_uri") != issued["redirect_uri"]:
            return 400, b'{"error":"invalid_grant"}'
        claims = self.claims(issued["subject"], issued["nonce"])
        token = self.override(claims) if self.override else self.token(claims)
        if isinstance(token, dict):
            token = self.token(token)
        return 200, json.dumps({"id_token": token, "access_token": "unused", "token_type": "Bearer",
                                "expires_in": 3600}).encode()
