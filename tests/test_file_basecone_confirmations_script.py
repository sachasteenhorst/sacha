"""Tests scripts/file_basecone_confirmations.py's dry-run/--apply behavior
-- every Graph call it makes is monkeypatched directly on the script
module (it just imports and calls app.email_client/app.mail_filing
functions, already covered end-to-end in test_mail_filing.py)."""
import sys

import pytest

from app.config import settings
from app.email_client import GraphApiError, GraphAuthError


@pytest.fixture(autouse=True)
def _mailboxes(monkeypatch):
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")


def _run_main(monkeypatch, *, apply: bool):
    import scripts.file_basecone_confirmations as script

    argv = ["file_basecone_confirmations.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    script.main()
    return script


def make_message(**kwargs):
    defaults = dict(
        id="msg-1", subject="Uw document is succesvol verwerkt",
        **{"from": {"emailAddress": {"address": "noreply@mailvanderlaangroep.nl"}}},
        receivedDateTime="2026-06-01T12:00:00Z", isRead=False,
    )
    defaults.update(kwargs)
    return defaults


def test_dry_run_moves_nothing(monkeypatch, capsys):
    import scripts.file_basecone_confirmations as script

    monkeypatch.setattr(script, "list_inbox_messages_since", lambda mailbox, since: [make_message()])
    monkeypatch.setattr(script, "is_basecone_confirmation", lambda m: True)
    move_calls = []
    monkeypatch.setattr(script, "move_message", lambda *a, **k: move_calls.append(1))
    monkeypatch.setattr(script, "mark_message_read", lambda *a, **k: move_calls.append(1))
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: (_ for _ in ()).throw(AssertionError("dry-run must not touch folders")))

    _run_main(monkeypatch, apply=False)

    assert move_calls == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "1 bevestigingsmail" in out


def test_apply_moves_and_marks_read(monkeypatch, capsys):
    import scripts.file_basecone_confirmations as script

    monkeypatch.setattr(script, "list_inbox_messages_since", lambda mailbox, since: [make_message()])
    monkeypatch.setattr(script, "is_basecone_confirmation", lambda m: True)
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: "folder-1")
    moved = []
    read = []
    monkeypatch.setattr(script, "move_message", lambda mailbox, message_id, folder_id: moved.append((message_id, folder_id)))
    monkeypatch.setattr(script, "mark_message_read", lambda mailbox, message_id: read.append(message_id))

    _run_main(monkeypatch, apply=True)

    assert moved == [("msg-1", "folder-1")]
    assert read == ["msg-1"]
    out = capsys.readouterr().out
    assert "1/1 bevestigingsmail" in out


def test_apply_skips_mark_read_for_already_read_message(monkeypatch):
    import scripts.file_basecone_confirmations as script

    monkeypatch.setattr(script, "list_inbox_messages_since", lambda mailbox, since: [make_message(isRead=True)])
    monkeypatch.setattr(script, "is_basecone_confirmation", lambda m: True)
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: "folder-1")
    read = []
    monkeypatch.setattr(script, "move_message", lambda *a, **k: None)
    monkeypatch.setattr(script, "mark_message_read", lambda *a, **k: read.append(1))

    _run_main(monkeypatch, apply=True)

    assert read == []


def test_non_matching_messages_are_never_moved(monkeypatch):
    import scripts.file_basecone_confirmations as script

    monkeypatch.setattr(script, "list_inbox_messages_since", lambda mailbox, since: [make_message()])
    monkeypatch.setattr(script, "is_basecone_confirmation", lambda m: False)
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: "folder-1")
    moved = []
    monkeypatch.setattr(script, "move_message", lambda *a, **k: moved.append(1))

    _run_main(monkeypatch, apply=True)

    assert moved == []


def test_stops_on_auth_error(monkeypatch, capsys):
    import scripts.file_basecone_confirmations as script

    def fail(mailbox, since):
        raise GraphAuthError("niet ingelogd")

    monkeypatch.setattr(script, "list_inbox_messages_since", fail)
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: "folder-1")

    _run_main(monkeypatch, apply=True)

    out = capsys.readouterr().out
    assert "Gestopt" in out


def test_continues_past_single_mailbox_api_error(monkeypatch, capsys):
    import scripts.file_basecone_confirmations as script

    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl,info@vanderlindentweewielers.nl")

    def list_messages(mailbox, since):
        if "facturen@" in mailbox:
            raise GraphApiError("geen rechten")
        return [make_message()]

    monkeypatch.setattr(script, "list_inbox_messages_since", list_messages)
    monkeypatch.setattr(script, "is_basecone_confirmation", lambda m: True)
    monkeypatch.setattr(script, "ensure_confirmation_folder", lambda mailbox: "folder-1")
    moved = []
    monkeypatch.setattr(script, "move_message", lambda *a, **k: moved.append(1))
    monkeypatch.setattr(script, "mark_message_read", lambda *a, **k: None)

    _run_main(monkeypatch, apply=True)

    assert moved == [1]
    out = capsys.readouterr().out
    assert "Fout" in out
