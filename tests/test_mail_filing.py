"""Tests app/mail_filing.py: Basecone confirmation-mail detection and the
Graph calls that file it away (mocked -- no real Graph API call is ever
made)."""
from datetime import date

import pytest

import app.email_client as email_client
import app.mail_filing as mail_filing
from app.config import settings
from app.email_client import GraphApiError, GraphAuthError
from app.mail_filing import (
    FilingSyncState,
    ensure_confirmation_folder,
    file_basecone_confirmations,
    filing_error_hint,
    is_basecone_confirmation,
    record_filing_errors,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(email_client._auth, "token", lambda: "tok-1")
    mail_filing._reset_folder_cache()
    monkeypatch.setattr(mail_filing, "_state", FilingSyncState())
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")
    monkeypatch.setattr(settings, "basecone_confirmation_folder", "Succesvol naar Basecone")
    monkeypatch.setattr(settings, "basecone_confirmation_sender_domains", "mailvanderlaangroep.nl,basecone.com")
    monkeypatch.setattr(
        settings, "basecone_confirmation_subject_keywords",
        "succesvol verwerkt,successfully processed,is verwerkt,ontvangen en verwerkt",
    )
    monkeypatch.setattr(settings, "basecone_confirmation_max_per_sync", 50)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json


def make_message(**kwargs):
    defaults = dict(
        id="msg-1",
        subject="Uw document is succesvol verwerkt",
        **{"from": {"emailAddress": {"address": "noreply@mailvanderlaangroep.nl"}}},
        receivedDateTime="2026-06-01T12:00:00Z",
        isRead=False,
    )
    defaults.update(kwargs)
    return defaults


# -- is_basecone_confirmation --

def test_matches_known_domain_and_subject_keyword():
    assert is_basecone_confirmation(make_message()) is True


def test_does_not_match_known_domain_with_unrelated_subject():
    msg = make_message(subject="Even een vraag over de rekening")
    assert is_basecone_confirmation(msg) is False


def test_does_not_match_unrelated_domain_even_with_matching_subject():
    msg = make_message(**{"from": {"emailAddress": {"address": "iemand@gmail.com"}}})
    assert is_basecone_confirmation(msg) is False


def test_matches_generic_basecone_domain_too():
    msg = make_message(**{"from": {"emailAddress": {"address": "notifications@basecone.com"}}})
    assert is_basecone_confirmation(msg) is True


def test_sender_without_at_sign_never_matches():
    msg = make_message(**{"from": {"emailAddress": {"address": ""}}})
    assert is_basecone_confirmation(msg) is False


# -- ensure_confirmation_folder --

def test_ensure_confirmation_folder_reuses_existing(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(("get", url, params))
        return FakeResponse(200, {"value": [{"id": "folder-1", "displayName": "Succesvol naar Basecone"}]})

    def fake_post(url, headers=None, json=None, timeout=None):
        raise AssertionError("should not create a folder that already exists")

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "post", fake_post)

    folder_id = ensure_confirmation_folder("facturen@vanderlindentweewielers.nl")

    assert folder_id == "folder-1"


def test_ensure_confirmation_folder_creates_when_missing(monkeypatch):
    monkeypatch.setattr(email_client.requests, "get", lambda *a, **k: FakeResponse(200, {"value": []}))
    monkeypatch.setattr(email_client.requests, "post", lambda *a, **k: FakeResponse(201, {"id": "new-folder"}))

    folder_id = ensure_confirmation_folder("facturen@vanderlindentweewielers.nl")

    assert folder_id == "new-folder"


def test_ensure_confirmation_folder_is_cached_per_mailbox(monkeypatch):
    get_calls = []
    monkeypatch.setattr(
        email_client.requests, "get",
        lambda *a, **k: get_calls.append(1) or FakeResponse(200, {"value": [{"id": "folder-1"}]}),
    )

    ensure_confirmation_folder("facturen@vanderlindentweewielers.nl")
    ensure_confirmation_folder("facturen@vanderlindentweewielers.nl")

    assert len(get_calls) == 1


# -- file_basecone_confirmations --

def test_moves_and_marks_read_a_matching_confirmation(monkeypatch):
    patched = {"marked_read": False, "moved": False}

    def fake_get(url, headers=None, params=None, timeout=None):
        if "mailFolders" in url and "/messages" not in url:
            return FakeResponse(200, {"value": [{"id": "folder-1"}]})
        return FakeResponse(200, {"value": [make_message()]})

    def fake_patch(url, headers=None, json=None, timeout=None):
        assert json == {"isRead": True}
        patched["marked_read"] = True
        return FakeResponse(200, {})

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/messages/msg-1/move")
        assert json == {"destinationId": "folder-1"}
        patched["moved"] = True
        return FakeResponse(201, {"id": "msg-1-moved"})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "patch", fake_patch)
    monkeypatch.setattr(email_client.requests, "post", fake_post)

    result = file_basecone_confirmations(date(2026, 1, 1))

    assert result.moved == 1
    assert result.errors == []
    assert patched == {"marked_read": True, "moved": True}


def test_already_read_confirmation_is_moved_without_a_patch_call(monkeypatch):
    patch_calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if "mailFolders" in url and "/messages" not in url:
            return FakeResponse(200, {"value": [{"id": "folder-1"}]})
        return FakeResponse(200, {"value": [make_message(isRead=True)]})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "patch", lambda *a, **k: patch_calls.append(1) or FakeResponse(200, {}))
    monkeypatch.setattr(email_client.requests, "post", lambda *a, **k: FakeResponse(201, {}))

    result = file_basecone_confirmations(date(2026, 1, 1))

    assert result.moved == 1
    assert patch_calls == []


def test_non_matching_messages_are_left_alone(monkeypatch):
    move_calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if "mailFolders" in url and "/messages" not in url:
            return FakeResponse(200, {"value": [{"id": "folder-1"}]})
        return FakeResponse(200, {"value": [make_message(subject="Gewone factuur, niets bijzonders")]})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "post", lambda *a, **k: move_calls.append(1) or FakeResponse(201, {}))

    result = file_basecone_confirmations(date(2026, 1, 1))

    assert result.moved == 0
    assert move_calls == []


def test_stops_on_auth_error(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        raise GraphAuthError("niet ingelogd")

    monkeypatch.setattr(email_client.requests, "get", fake_get)

    result = file_basecone_confirmations(date(2026, 1, 1))

    assert result.moved == 0
    assert len(result.errors) == 1


def test_continues_past_single_mailbox_api_error(monkeypatch):
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl,info@vanderlindentweewielers.nl")

    def fake_get(url, headers=None, params=None, timeout=None):
        if "facturen@" in url:
            return FakeResponse(403, text="forbidden")
        if "mailFolders" in url and "/messages" not in url:
            return FakeResponse(200, {"value": [{"id": "folder-2"}]})
        return FakeResponse(200, {"value": [make_message()]})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "patch", lambda *a, **k: FakeResponse(200, {}))
    monkeypatch.setattr(email_client.requests, "post", lambda *a, **k: FakeResponse(201, {}))

    result = file_basecone_confirmations(date(2026, 1, 1))

    assert result.moved == 1
    assert len(result.errors) == 1


# -- filing_error_hint / record_filing_errors --

def test_filing_error_hint_permission_issue():
    hint = filing_error_hint("Graph API-fout (403) bij iets: Mail.ReadWrite.Shared ontbreekt")
    assert "Mail.ReadWrite.Shared" in hint


def test_filing_error_hint_none_passthrough():
    assert filing_error_hint(None) is None


def test_record_filing_errors_is_authoritative():
    record_filing_errors(["boom"])
    assert mail_filing.get_state().last_error == "boom"
    record_filing_errors([])
    assert mail_filing.get_state().last_error is None
