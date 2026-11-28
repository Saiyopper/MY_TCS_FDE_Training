from shop import add_item, cart_total, apply_discount


def test_add_item():
    cart = add_item([], "pen", 2.0, 3)
    assert cart[0]["qty"] == 3


def test_cart_total():
    cart = add_item([], "pen", 10.0, 1)
    assert cart_total(cart) == 10.8


def test_discount():
    assert apply_discount(100, "10 * 0.01") == 90
