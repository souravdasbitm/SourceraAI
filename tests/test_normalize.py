import unittest

from memory.normalize import bindings_for, mask_steps, mask_text, normalize_step, reinject_steps


class NormalizeStepTest(unittest.TestCase):
    def test_emails_share_a_plan_and_keep_original_case(self):
        left = normalize_step("Login as A@X.com")
        right = normalize_step("login as b@y.com")
        self.assertEqual(left, right)
        self.assertEqual(left, "login as {email1}")
        self.assertEqual(bindings_for("Login as A@X.com"), [("{email1}", "A@X.com")])

    def test_quoted_values_numbers_and_whitespace(self):
        text = 'Type  "secret_sauce"  into   field 2'
        self.assertEqual(normalize_step(text), "type {q1} into field {n1}")
        self.assertEqual(
            bindings_for(text),
            [("{q1}", "secret_sauce"), ("{n1}", "2")],
        )

    def test_quoted_number_stays_one_placeholder(self):
        self.assertEqual(normalize_step('Postal Code "560001"'), "postal code {q1}")

    def test_mask_and_reinject_round_trip(self):
        bindings = bindings_for('Type "secret_sauce" into the Password field')
        steps = [{"action": "fill", "selector": "#password", "value": "secret_sauce",
                  "description": 'Type "secret_sauce" into the Password field'}]
        masked = mask_steps(steps, bindings)
        self.assertEqual(masked[0]["value"], "{q1}")
        self.assertNotIn("secret_sauce", masked[0]["description"])
        restored = reinject_steps(masked, bindings)
        self.assertEqual(restored[0]["value"], "secret_sauce")
        self.assertEqual(mask_text('password secret_sauce', bindings), "password {q1}")


if __name__ == "__main__":
    unittest.main()
