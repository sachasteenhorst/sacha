#!/usr/bin/env python3
"""One-off (and re-runnable) pass that files away existing Basecone
confirmation mails of the last 12 months (see app/mail_filing.py) into
their folder (created if it doesn't exist yet), marking them read. Needed
once after this feature shipped (there's a backlog of confirmation mails
already sitting in the inbox), and safe to re-run any time afterwards --
once a mail is moved out of the inbox it's never matched again, so a
second run only picks up whatever arrived since.

DRY-RUN by default: prints exactly what it would move and changes nothing
on Graph. Pass --apply to actually move/mark the mails.
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.email_client import GraphApiError, GraphAuthError, list_inbox_messages_since, mark_message_read, move_message
from app.mail_filing import ensure_confirmation_folder, is_basecone_confirmation

MONTHS_BACK = 12


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Voer het verplaatsen echt uit (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    since = date.today() - timedelta(days=MONTHS_BACK * 30)
    total_found = 0
    total_moved = 0
    errors: list[str] = []

    for mailbox in settings.graph_mailboxes:
        print(f"\n{mailbox}:")
        try:
            folder_id = ensure_confirmation_folder(mailbox) if args.apply else None
            messages = [m for m in list_inbox_messages_since(mailbox, since) if is_basecone_confirmation(m)]
        except GraphAuthError as exc:
            print(f"  Gestopt: {exc}")
            break
        except GraphApiError as exc:
            print(f"  Fout: {exc}")
            errors.append(str(exc))
            continue

        print(f"  {len(messages)} bevestigingsmail(s) gevonden.")
        total_found += len(messages)
        for message in messages:
            subject = message.get("subject", "")
            sender = (message.get("from") or {}).get("emailAddress", {}).get("address", "")
            received = message.get("receivedDateTime", "")
            print(f"    {received}  {sender:<40.40}  {subject}")
            if args.apply:
                try:
                    if not message.get("isRead"):
                        mark_message_read(mailbox, message["id"])
                    move_message(mailbox, message["id"], folder_id)
                    total_moved += 1
                except (GraphAuthError, GraphApiError) as exc:
                    print(f"      Fout bij verplaatsen: {exc}")
                    errors.append(str(exc))

    if args.apply:
        print(f"\n{total_moved}/{total_found} bevestigingsmail(s) verplaatst, {len(errors)} fout(en).")
    else:
        print(f"\n(DRY-RUN) {total_found} bevestigingsmail(s) zouden verplaatst worden.")
        print("Draai met --apply om dit echt uit te voeren.")


if __name__ == "__main__":
    main()
