"""Invoice line and total calculations for the billing service."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def line_total(unit_price: Decimal, quantity: int, discount: Decimal = Decimal("0")) -> Decimal:
    """Price of one invoice line after a flat discount, rounded to the cent."""
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    gross = unit_price * quantity
    discount = min(discount, gross)
    return (gross - discount).quantize(CENT, rounding=ROUND_HALF_UP)


def tax(amount: Decimal, rate: Decimal) -> Decimal:
    return (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def invoice_total(lines: list, tax_rate: Decimal = Decimal("0.08")) -> Decimal:
    """Sum of line totals plus tax. `lines` holds (unit_price, quantity, discount) tuples."""
    subtotal = sum((line_total(*line) for line in lines), Decimal("0"))
    return subtotal + tax(subtotal, tax_rate)
