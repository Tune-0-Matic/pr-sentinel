from decimal import Decimal

COUPONS = {"SAVE10": Decimal("0.10"), "HALF": Decimal("0.50")}


def apply_coupon(price: Decimal, code: str) -> Decimal:
    rate = COUPONS.get(code.strip().upper(), Decimal("0"))
    return price - price * rate
