"""Cognito sign-in (authorization code + PKCE + ID token) against a test-only stand-in IdP."""
import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization

from identity import Identity, IdentityError, OIDCSettings, pkce_pair, safe_return_to
from tenant_store import initialize_store
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, ORIGIN, FakeCognito, _key


def forged_hmac(claims, secret, kid):
    """An HS256 token keyed with the RSA public key, built by hand (PyJWT refuses to make one)."""
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
    signing_input = part({"alg": "HS256", "typ": "JWT", "kid": kid}) + b"." + part(claims)
    signature = base64.urlsafe_b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + signature).decode()


def settings(secret=None):
    return OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN, client_secret=secret)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "v2.db"
        initialize_store(self.database)
        self.cognito = FakeCognito(secret="shh")
        self.identity = Identity(settings("shh"), self.database, post=self.cognito.post, keys=self.cognito.keys)

    def sign_in(self, subject="user-1", return_to="/#jobs"):
        url, binding = self.identity.begin(return_to)
        callback = self.cognito.authorize(url, subject)
        return self.identity.finish(code=callback["code"], state=callback["state"], binding=binding)

    def test_authorize_request_uses_code_pkce_state_and_nonce(self):
        url, binding = self.identity.begin("/#facts")
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", f"{DOMAIN}/oauth2/authorize")
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["redirect_uri"], f"{ORIGIN}/auth/callback")
        self.assertEqual(query["scope"], "openid email")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertGreaterEqual(len(query["state"]), 40)
        self.assertGreaterEqual(len(query["nonce"]), 40)
        self.assertNotIn(binding, url)
        verifier, challenge = pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertNotEqual(verifier, challenge)

    def test_a_verified_callback_names_the_issuer_and_subject(self):
        self.assertEqual(self.sign_in(), (ISSUER, "user-1", "/#jobs"))
        url, form, headers = self.cognito.requests[-1]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn("client_secret", form)

    def test_a_callback_works_once_and_only_in_the_browser_that_started_it(self):
        url, binding = self.identity.begin("/")
        callback = self.cognito.authorize(url, "user-1")
        with self.assertRaises(IdentityError) as other:
            self.identity.finish(code=callback["code"], state=callback["state"], binding="another-browser")
        self.assertEqual(other.exception.reason, "state_mismatch")
        self.assertEqual(len(self.cognito.requests), 0)  # refused before any token request
        # The foreign attempt could not use it up: the browser that started it still finishes, once.
        self.identity.finish(code=callback["code"], state=callback["state"], binding=binding)
        with self.assertRaises(IdentityError) as replay:
            self.identity.finish(code=callback["code"], state=callback["state"], binding=binding)
        self.assertEqual(replay.exception.reason, "state_mismatch")
        with self.assertRaises(IdentityError):
            self.identity.finish(code="x", state="made-up", binding=binding)

    def test_tokens_that_are_not_this_sign_ins_id_token_are_refused(self):
        other_key = _key()
        public_pem = self.cognito.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cases = {
            "wrong nonce": lambda claims: {**claims, "nonce": "someone-elses"},
            "no nonce": lambda claims: {key: value for key, value in claims.items() if key != "nonce"},
            "other audience": lambda claims: {**claims, "aud": "another-client"},
            "other issuer": lambda claims: {**claims, "iss": "https://cognito-idp.us-east-1.amazonaws.com/other"},
            "expired": lambda claims: {**claims, "exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
            "issued in the future": lambda claims: {**claims, "iat": int(time.time()) + 3600},
            "access token": lambda claims: {**claims, "token_use": "access"},
            "empty subject": lambda claims: {**claims, "sub": " "},
            "other key": lambda claims: self.cognito.token(claims, key=other_key),
            "unsigned": lambda claims: jwt.encode(claims, None, algorithm="none", headers={"kid": self.cognito.kid}),
            "HMAC with the public key": lambda claims: forged_hmac(claims, public_pem, self.cognito.kid),
            "unknown key id": lambda claims: self.cognito.token(claims, headers={"kid": "nope"}),
            "garbage": lambda claims: "not.a.token",
        }
        for label, break_token in cases.items():
            with self.subTest(label):
                self.cognito.override = break_token
                with self.assertRaises(IdentityError):
                    self.sign_in()

    def test_token_endpoint_refusals_are_reported_without_detail(self):
        url, binding = self.identity.begin("/")
        callback = self.cognito.authorize(url, "user-1")
        wrong_secret = Identity(settings("wrong"), self.database, post=self.cognito.post, keys=self.cognito.keys)
        url, binding = wrong_secret.begin("/")
        callback = self.cognito.authorize(url, "user-1")
        with self.assertRaises(IdentityError) as refused:
            wrong_secret.finish(code=callback["code"], state=callback["state"], binding=binding)
        self.assertEqual(refused.exception.reason, "invalid_client")
        for status, body, reason in [(500, b"<html>oops</html>", "token_refused"), (200, b"[]", "token_refused"),
                                     (200, b'{"access_token":"x"}', "no_id_token")]:
            with self.subTest(reason):
                identity = Identity(settings(), self.database, post=lambda *_: (status, body), keys=self.cognito.keys)
                url, binding = identity.begin("/")
                state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
                with self.assertRaises(IdentityError) as error:
                    identity.finish(code="c", state=state, binding=binding)
                self.assertEqual(error.exception.reason, reason)

    def test_return_to_stays_on_this_site(self):
        for value, expected in [("/#job/1", "/#job/1"), ("//evil.example/x", "/"), ("https://evil.example", "/"),
                                ("/\\evil.example", "/"), ("relative", "/"), (None, "/"), ("/" + "a" * 600, "/"),
                                ("/\nx", "/")]:
            self.assertEqual(safe_return_to(value), expected)

    def test_settings_come_from_the_environment_with_the_secret_in_a_file(self):
        self.assertIsNone(OIDCSettings.from_environment({}))
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("s3cret\n")
        self.addCleanup(Path(handle.name).unlink)
        loaded = OIDCSettings.from_environment({
            "WORKBENCH_OIDC_ISSUER": ISSUER, "WORKBENCH_OIDC_CLIENT_ID": CLIENT_ID, "WORKBENCH_OIDC_DOMAIN": DOMAIN,
            "WORKBENCH_PUBLIC_ORIGIN": ORIGIN, "WORKBENCH_OIDC_CLIENT_SECRET_FILE": handle.name})
        self.assertEqual(loaded.client_secret, "s3cret")
        self.assertNotIn("s3cret", repr(loaded))
        for bad in [{"domain": "http://example.com"}, {"public_origin": "https://x.example/?q=1"},
                    {"client_id": ""}, {"scopes": "email"}]:
            with self.assertRaises(ValueError):
                OIDCSettings(**{"issuer": ISSUER, "client_id": CLIENT_ID, "domain": DOMAIN, "public_origin": ORIGIN, **bad})
        OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin="http://localhost:8765")
        self.assertEqual(self.identity.logout_url(),
                         f"{DOMAIN}/logout?client_id={CLIENT_ID}&logout_uri=https%3A%2F%2Ffeiran.example%2Fsigned-out")


if __name__ == "__main__":
    unittest.main()
