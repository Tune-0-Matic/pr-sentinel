def invoice_label(number: int, prefix: str = "INV") -> str:
    return "%s-%06d" % (prefix, number)
