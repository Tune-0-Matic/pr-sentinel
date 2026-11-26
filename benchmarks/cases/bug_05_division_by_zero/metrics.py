def average_order_value(revenue: float, orders: int) -> float:
    if orders == 0:
        return 0.0
    return revenue / orders
