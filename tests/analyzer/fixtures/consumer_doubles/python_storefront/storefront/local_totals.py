"""Cart totals computed locally; no Demo Payments data involved (negative control)."""


def cart_total_display(line_items: list[dict]) -> str:
    amount = sum(item["unit_price_cents"] * item["quantity"] for item in line_items)
    return f"${amount / 100:.2f}"
