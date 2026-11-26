def reserve(stock: int, qty: int) -> int:
    """Reserve qty units and return the stock left."""
    if qty <= 0 or qty > stock:
        raise ValueError("invalid reservation")
    return stock - qty
