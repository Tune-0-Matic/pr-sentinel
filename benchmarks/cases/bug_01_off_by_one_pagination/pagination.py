def paginate(items: list, page: int, per_page: int = 20) -> list:
    """Return the items on a 1-based page."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page start at 1")
    start = (page - 1) * per_page
    return items[start:start + per_page]
