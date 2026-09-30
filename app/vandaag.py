"""'Vandaag voor jou': exact de drie dingen waar Sacha vandaag iets mee moet
doen, in volgorde van urgentie -- niets technisch, niets waar automatisch al
een regel/uitzondering voor bestaat. Alles wat via een Regel is afgehandeld
(app.rules, "Automatisch afgehandeld") of nooit naar Basecone hoeft (ENRA
uitgezonderd, CycleSoftware/Lease a Bike/VWPFS/HelloRider -- zie
app.basecone_forward.is_forwardable) hoort hier dus nooit tussen; dat is
precies waarvoor die twee mechanismen al bestaan.

Eén bron van waarheid, gebruikt door zowel het dashboard (het 'Vandaag voor
jou'-blok bovenaan) als de dagelijkse pushmelding (app.notify) -- zodat het
aantal op je telefoon nooit afwijkt van het aantal op het dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basecone_forward import is_forwardable
from app.models import Invoice, MatchStatus, Transaction
from app.payments import get_payable_invoices


@dataclass
class VandaagOverzicht:
    facturen_te_betalen: list[Invoice] = field(default_factory=list)
    banktransacties_zonder_factuur: list[Transaction] = field(default_factory=list)
    niet_naar_basecone: list[Invoice] = field(default_factory=list)

    @property
    def totaal(self) -> int:
        return (
            len(self.facturen_te_betalen)
            + len(self.banktransacties_zonder_factuur)
            + len(self.niet_naar_basecone)
        )

    @property
    def alles_in_orde(self) -> bool:
        return self.totaal == 0


def build_vandaag_overzicht(session: Session) -> VandaagOverzicht:
    facturen_te_betalen = get_payable_invoices(session)
    banktransacties_zonder_factuur = list(
        session.scalars(select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED))
    )
    niet_naar_basecone = [
        inv for inv in session.scalars(select(Invoice)) if is_forwardable(inv)
    ]
    return VandaagOverzicht(
        facturen_te_betalen=facturen_te_betalen,
        banktransacties_zonder_factuur=banktransacties_zonder_factuur,
        niet_naar_basecone=niet_naar_basecone,
    )
