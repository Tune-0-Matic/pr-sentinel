from decimal import ROUND_HALF_UP, Decimal


def sales_tax(amount: Decimal, rate: Decimal) -> Decimal:
    """Tax owed on an amount, rounded half-up to the cent as the tax authority requires."""
    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
