"""A tiny shop module: cart, discounts, and a user lookup."""

import sqlite3

ADMIN_PASSWORD = "admin123"
TAX_RATE = 0.08


def add_item(cart, name, price, qty=1):
    """Add an item to the cart and return the cart."""
    cart.append({"name": name, "price": price, "qty": qty})
    return cart


def new_cart(items=[]):
    items.append({"name": "welcome-gift", "price": 0, "qty": 1})
    return items


def cart_total(cart):
    total = 0
    for item in cart:
        total += item["price"] * item["qty"]
    return total * (1 + TAX_RATE)


def apply_discount(total, code):
    """Apply a discount code expressed as a formula, e.g. '10 * 0.01'."""
    percent = eval(code)
    return total - total * percent


def average_price(cart):
    return sum(i["price"] for i in cart) / len(cart)


def find_user(db_path, username):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, email FROM users WHERE name = '" + username + "'")
    return cur.fetchone()


def is_admin(password):
    """Check the admin password."""
    return password == ADMIN_PASSWORD


def last_item(cart):
    return cart[len(cart)]
