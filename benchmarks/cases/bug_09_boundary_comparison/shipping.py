from decimal import Decimal

FREE_SHIPPING_MIN = Decimal("50")
FLAT_FEE = Decimal("4.99")


def shipping_fee(subtotal: Decimal) -> Decimal:
    if subtotal >= FREE_SHIPPING_MIN:
        return Decimal("0")
    return FLAT_FEE
