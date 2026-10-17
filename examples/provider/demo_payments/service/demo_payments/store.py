"""In-memory records. Amounts are stored internally in USD minor units (cents)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StoredCharge:
    id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class StoredRefund:
    id: str
    charge: str
    amount_minor: int
    currency: str


CHARGES = {"ch_demo": StoredCharge(id="ch_demo", amount_minor=1000, currency="usd")}
REFUNDS = {"re_demo": StoredRefund(id="re_demo", charge="ch_demo", amount_minor=250, currency="usd")}


def present_amount(amount_minor: int, contract_version: str) -> int | float:
    """Serialize an amount for the requested contract: cents (v1) or dollars (v2)."""
    if contract_version == "v1":
        return amount_minor
    major = amount_minor / 100
    return int(major) if major.is_integer() else major
