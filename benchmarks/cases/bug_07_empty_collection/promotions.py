def best_discount(discounts: list) -> float:
    """Largest discount rate on offer, 0 when there are none."""
    return max(discounts, default=0.0)
