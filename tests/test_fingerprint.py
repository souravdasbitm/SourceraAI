import unittest

from memory.fingerprint import cache_key, normalize_path, page_fingerprint

BASE = """
<html><body>
<header id="top"></header>
<main>
  <h1>Inventory</h1>
  <form name="cart">
    <input id="user-name" type="text" value="standard_user">
    <button id="login-button" type="submit">Login</button>
  </form>
  <span>Updated 10:41:00</span>
  <div class="price">$9.99</div>
</main>
</body></html>
"""


class FingerprintTest(unittest.TestCase):
    def test_ignores_dynamic_text(self):
        other = BASE.replace("$9.99", "$15.99").replace("10:41:00", "11:02:33").replace(
            ">Inventory<", ">Inventory — refreshed<"
        )
        url = "https://www.saucedemo.com/inventory.html"
        self.assertEqual(page_fingerprint(url, BASE), page_fingerprint(url, other))

    def test_changes_when_layout_changes(self):
        url = "https://www.saucedemo.com/inventory.html"
        expanded = BASE.replace("</main>", '<form name="checkout"></form></main>')
        self.assertNotEqual(page_fingerprint(url, BASE), page_fingerprint(url, expanded))

    def test_path_drops_query_and_ids(self):
        a = "https://shop.example/cart/12345?session=abc"
        b = "https://shop.example/cart/99?x=1"
        self.assertEqual(normalize_path(a), "/cart")
        self.assertEqual(normalize_path(a), normalize_path(b))
        uuid = "https://shop.example/item/550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(normalize_path(uuid), "/item")

    def test_cache_key_changes_with_step_or_page(self):
        page = page_fingerprint("https://shop.example/cart", BASE)
        self.assertEqual(cache_key("click login", page), cache_key("click login", page))
        self.assertNotEqual(cache_key("click login", page), cache_key("click checkout", page))


if __name__ == "__main__":
    unittest.main()
