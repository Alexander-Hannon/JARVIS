"""V5 M1a second factors: TOTP (RFC 6238), passkeys (WebAuthn, real verification against a software authenticator),
recovery codes, enforcement by role/policy, admin reset, and pairing only from MFA-verified sessions."""
from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest
from conftest import LOOPBACK, PASSWORD, Api
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:8765"


@pytest.fixture
def strict(monkeypatch):
    """Turn the real policy on (conftest turns it off for feature tests)."""
    from jarvis.config import reset_settings_cache
    monkeypatch.setenv("JARVIS_MFA_POLICY", "privileged")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _setup_owner(app) -> Api:
    c = TestClient(app, client=LOOPBACK)
    r = c.post("/api/auth/setup", json={"username": "aj", "password": PASSWORD, "display_name": "AJ"})
    assert r.status_code == 200, r.text
    return Api(c, r.json()["csrf_token"])


def _enroll_totp(api: Api) -> tuple[str, list[str]]:
    from jarvis.security.mfa import totp_at
    import time
    b = api.post("/api/auth/mfa/totp/begin", {"label": "Phone"}).json()
    assert b["otpauth_uri"].startswith("otpauth://totp/JARVIS:aj?secret=")
    r = api.post("/api/auth/mfa/totp/confirm", {"id": b["id"], "code": totp_at(b["secret"], int(time.time() // 30))})
    assert r.status_code == 200, r.text
    return b["secret"], r.json()["recovery_codes"]


def _login(app, ip="198.51.100.20", username="aj", password=PASSWORD):
    c = TestClient(app, client=(ip, 40100))
    return c, c.post("/api/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------- TOTP
def test_totp_matches_rfc6238_vectors():
    from jarvis.security.mfa import totp_at, totp_match
    secret = base64.b32encode(b"12345678901234567890").decode()
    for t, code in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                    (2000000000, "279037")):
        assert totp_at(secret, t // 30) == code
    assert totp_match(secret, "287082", now=59) == 1
    assert totp_match(secret, "287082", now=59 + 30) == 1            # one step of clock drift
    assert totp_match(secret, "287082", now=59 + 90) is None
    assert totp_match(secret, "287082", now=59, last_step=1) is None  # replay of an accepted step


def test_privileged_user_must_enroll_before_anything_else(app, strict, conn):
    a = _setup_owner(app)
    r = a.get("/api/today")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "mfa_enrollment_required"
    assert a.post("/api/auth/pairing-code").status_code == 403          # no device pairing from a password-only session
    st = a.get("/api/auth/status").json()["mfa"]
    assert st == {"required": True, "enrolled": False, "satisfied": False}
    _secret, codes = _enroll_totp(a)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert a.get("/api/today").status_code == 200                       # this session proved the factor
    assert a.get("/api/auth/status").json()["mfa"]["satisfied"] is True
    stored = conn.execute("SELECT secret_sealed FROM user_mfa").fetchone()[0]
    assert stored.startswith("gAAAA") and _secret not in stored


def test_login_with_totp_then_replay_refused(app, strict):
    import time

    from jarvis.security.mfa import totp_at
    a = _setup_owner(app)
    secret, _ = _enroll_totp(a)
    c, r = _login(app)
    body = r.json()
    assert r.status_code == 200 and body["mfa_required"] and "totp" in body["methods"] and "recovery" in body["methods"]
    assert "jarvis_session" not in c.cookies                            # no session before the second factor
    assert c.post("/api/auth/mfa/verify", json={"challenge_id": body["challenge_id"], "method": "totp",
                                                "code": "000000"}).status_code == 401
    # the failed attempt consumed that challenge
    _, r = _login(app)
    code = totp_at(secret, int(time.time() // 30) + 1)                  # next step: not the one used to enroll
    ok = c.post("/api/auth/mfa/verify", json={"challenge_id": r.json()["challenge_id"], "method": "totp", "code": code})
    assert ok.status_code == 200, ok.text
    assert Api(c, ok.json()["csrf_token"]).get("/api/today").status_code == 200
    c2, r2 = _login(app, ip="198.51.100.21")
    replay = c2.post("/api/auth/mfa/verify", json={"challenge_id": r2.json()["challenge_id"], "method": "totp", "code": code})
    assert replay.status_code == 401                                    # same time step can't be used twice


def test_recovery_code_works_once(app, strict):
    a = _setup_owner(app)
    _, codes = _enroll_totp(a)
    c, r = _login(app)
    assert c.post("/api/auth/mfa/verify", json={"challenge_id": r.json()["challenge_id"], "method": "recovery",
                                                "code": codes[0].lower()}).status_code == 200
    c2, r2 = _login(app, ip="198.51.100.22")
    assert c2.post("/api/auth/mfa/verify", json={"challenge_id": r2.json()["challenge_id"], "method": "recovery",
                                                 "code": codes[0]}).status_code == 401


def test_password_only_sessions_from_before_enrollment_must_sign_in_again(app, strict, conn):
    """AJ's existing sessions (from before V5) are password-level: after he enrolls, they must re-authenticate."""
    a = _setup_owner(app)
    c, r = _login(app)                                                  # second device, before enrollment
    other = Api(c, r.json()["csrf_token"])
    _enroll_totp(a)
    r = other.get("/api/today")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "mfa_required"


def test_only_factor_cant_be_removed_while_required(app, strict):
    a = _setup_owner(app)
    _enroll_totp(a)
    mid = a.get("/api/auth/mfa").json()["methods"][0]["id"]
    assert a.delete(f"/api/auth/mfa/{mid}").status_code == 400


def test_members_need_mfa_only_under_the_all_policy(app, strict, monkeypatch):
    from jarvis.config import reset_settings_cache
    a = _setup_owner(app)
    _enroll_totp(a)
    inv = a.post("/api/users", {"username": "mia", "display_name": "Mia", "role": "member"}).json()
    c = TestClient(app, client=("203.0.113.60", 41000))
    r = c.post("/api/auth/invite/accept", json={"token": inv["invite_url"].rsplit("/", 1)[-1], "password": "mia password 12"})
    mia = Api(c, r.json()["csrf_token"])
    assert mia.get("/api/today").status_code == 200                     # privileged policy: members don't need it
    monkeypatch.setenv("JARVIS_MFA_POLICY", "all")
    reset_settings_cache()
    assert mia.get("/api/today").json()["detail"]["code"] == "mfa_enrollment_required"


def test_admin_reset_ends_sessions_and_respects_roles(app, strict, conn):
    a = _setup_owner(app)
    _enroll_totp(a)
    inv = a.post("/api/users", {"username": "max", "display_name": "Max", "role": "member"}).json()
    c = TestClient(app, client=("203.0.113.61", 41000))
    r = c.post("/api/auth/invite/accept", json={"token": inv["invite_url"].rsplit("/", 1)[-1], "password": "max password 12"})
    mx = Api(c, r.json()["csrf_token"])
    _enroll_totp_for(mx, "max")
    uid = inv["user"]["id"]
    assert a.post(f"/api/users/{uid}/reset-mfa").status_code == 200
    assert mx.get("/api/today").status_code == 401                      # session ended
    assert conn.execute("SELECT COUNT(*) FROM user_mfa WHERE user_id=?", (uid,)).fetchone()[0] == 0


def _enroll_totp_for(api: Api, username: str) -> None:
    import time

    from jarvis.security.mfa import totp_at
    b = api.post("/api/auth/mfa/totp/begin", {"label": "Phone"}).json()
    assert api.post("/api/auth/mfa/totp/confirm", {"id": b["id"], "code": totp_at(b["secret"], int(time.time() // 30))}
                    ).status_code == 200


def test_paired_phone_counts_as_a_verified_device(app, strict):
    a = _setup_owner(app)
    _enroll_totp(a)
    code = a.post("/api/auth/pairing-code").json()["code"]
    phone = TestClient(app, client=("100.64.0.9", 40000))
    r = phone.post("/api/auth/pair", json={"code": code, "label": "Phone"})
    assert Api(phone, r.json()["csrf_token"]).get("/api/today").status_code == 200


# ---------------------------------------------------------------- WebAuthn with a software authenticator
class SoftAuthenticator:
    """Just enough of a platform authenticator to produce real ES256 attestations and assertions."""

    def __init__(self, rp_id: str):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.rp_id = rp_id
        self.count = 0

    @staticmethod
    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    def _cose(self) -> bytes:
        import cbor2
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def register(self, options: dict, origin: str) -> dict:
        import cbor2
        cd = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + (0).to_bytes(4, "big")
                     + b"\x00" * 16 + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": self.b64u(self.cred_id), "rawId": self.b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": self.b64u(cd), "attestationObject": self.b64u(att)}}

    def assert_(self, options: dict, origin: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        self.count += 1
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self.b64u(self.cred_id), "rawId": self.b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": self.b64u(cd), "authenticatorData": self.b64u(auth_data),
                             "signature": self.b64u(sig), "userHandle": None}}


def test_passkey_registration_and_sign_in(app, strict, conn):
    a = _setup_owner(app)
    hdr = {"Origin": ORIGIN}
    beg = a.post("/api/auth/mfa/webauthn/begin", {"label": "Laptop passkey"}, headers=hdr).json()
    assert beg["options"]["rp"]["id"] == "localhost"
    dev = SoftAuthenticator("localhost")
    fin = a.post("/api/auth/mfa/webauthn/finish", {"challenge_id": beg["challenge_id"],
                                                   "credential": dev.register(beg["options"], ORIGIN)}, headers=hdr)
    assert fin.status_code == 200, fin.text
    assert len(fin.json()["recovery_codes"]) == 10
    stored = conn.execute("SELECT public_key, credential_id FROM user_mfa WHERE kind='webauthn'").fetchone()
    assert stored["credential_id"] == dev.b64u(dev.cred_id)
    c = TestClient(app, client=("198.51.100.30", 40100))
    r = c.post("/api/auth/login", json={"username": "aj", "password": PASSWORD}, headers=hdr).json()
    assert "webauthn" in r["methods"] and r["webauthn"]["allowCredentials"][0]["id"] == dev.b64u(dev.cred_id)
    ok = c.post("/api/auth/mfa/verify", json={"challenge_id": r["challenge_id"], "method": "webauthn",
                                              "credential": dev.assert_(r["webauthn"], ORIGIN)}, headers=hdr)
    assert ok.status_code == 200, ok.text
    # a different origin (phishing site) can't produce a usable assertion
    c2 = TestClient(app, client=("198.51.100.31", 40100))
    r2 = c2.post("/api/auth/login", json={"username": "aj", "password": PASSWORD}, headers=hdr).json()
    bad = c2.post("/api/auth/mfa/verify", json={"challenge_id": r2["challenge_id"], "method": "webauthn",
                                                "credential": dev.assert_(r2["webauthn"], "https://evil.example")},
                  headers=hdr)
    assert bad.status_code == 401


def test_cli_break_glass_resets_password_and_second_factors(app, strict, monkeypatch):
    """A sole owner who lost their phone and recovery codes gets back in only from a shell on the host."""
    from jarvis import cli
    api = _setup_owner(app)
    _enroll_totp(api)
    new = "a brand new passphrase"
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": new)
    cli.main(["reset-password", "--user", "aj", "--reset-mfa"])
    assert api.get("/api/auth/me").status_code == 401                 # every session ended
    c, r = _login(app, password=new)
    assert r.status_code == 200 and "mfa_required" not in r.json()     # no second factor left …
    st = c.get("/api/auth/status").json()
    assert st["mfa"] == {"required": True, "enrolled": False, "satisfied": False}   # … so enrollment comes first
