"""Offline tests: arithmetic, deterministic checks and the decision guard. No API calls.

Run with:  python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from expense_app import config, guard, rules, store  # noqa: E402


def load(name):
    return json.loads((config.DATA_DIR / name).read_text(encoding="utf-8"))


CLAIMS = {c["id"]: c for c in load("claims.json")}
EMPLOYEES = load("employees.json")
PAST = load("past_claims.json")
FX = load("fx_rates.json")


class MoneyTests(unittest.TestCase):
    def test_currency_conversion_rounds_to_cent(self):
        self.assertEqual(rules.to_usd(795, "GBP", FX), Decimal("1033.50"))
        self.assertEqual(rules.to_usd(1450, "INR", FX), Decimal("17.40"))
        self.assertEqual(rules.to_usd(45, "EUR", FX), Decimal("49.50"))

    def test_unsupported_currency_is_a_readable_error(self):
        with self.assertRaisesRegex(rules.RuleError, "unsupported currency"):
            rules.to_usd(10, "JPY", FX)

    def test_bad_amounts_are_rejected(self):
        for bad in [0, -5, "abc", True, None]:
            with self.assertRaises(rules.RuleError, msg=repr(bad)):
                rules.parse_amount(bad)

    def test_days_between(self):
        self.assertEqual(rules.days_between("2025-11-20", "2026-03-02"), 102)
        self.assertEqual(rules.days_between("2026-02-26", "2026-03-02"), 4)
        with self.assertRaises(rules.RuleError):
            rules.days_between("26/02/2026", "2026-03-02")

    def test_calculate_reimbursement_meal_with_alcohol_and_group_cap(self):
        # CLM-3002: 412.50 dinner, 96.00 wine removed, 4 people x $75 = $300 cap
        result = rules.calculate_reimbursement(
            [{"label": "dinner", "amount_usd": "316.50", "kind": "reimbursable"},
             {"label": "wine", "amount_usd": "96.00", "kind": "excluded"}],
            group_cap_usd=300)
        self.assertEqual(result["total_usd"], "300.00")
        self.assertTrue(result["group_cap_applied"])

    def test_calculate_reimbursement_hotel_per_night_caps(self):
        # CLM-3003: 3 nights at GBP 265 = $344.50 each, London cap $320 a night
        lines = [{"label": f"night {i}", "amount_usd": "344.50", "cap_usd": "320"} for i in range(3)]
        self.assertEqual(rules.calculate_reimbursement(lines)["total_usd"], "960.00")

    def test_calculate_reimbursement_rejects_bad_kind(self):
        with self.assertRaises(rules.RuleError):
            rules.calculate_reimbursement([{"label": "x", "amount_usd": "5", "kind": "maybe"}])


class DeterministicCheckTests(unittest.TestCase):
    def checks(self, cid):
        return rules.deterministic_checks(CLAIMS[cid], EMPLOYEES, PAST, FX)

    def test_duplicate_is_found(self):
        f = self.checks("CLM-3007")
        self.assertEqual(f["duplicates_of"], ["C-9004"])
        self.assertEqual(f["settled_by_code"]["decision"], "reject")

    def test_late_submission_settles_to_finance_controller(self):
        f = self.checks("CLM-3009")
        self.assertTrue(f["late_submission"])
        self.assertEqual(f["settled_by_code"]["escalate_to"], "finance-controller")

    def test_split_purchase_detected_across_three_claims(self):
        # CLM-3005 (140) + C-9101 (145) + C-9102 (138) same vendor/day, total 423 > 150
        f = self.checks("CLM-3005")
        self.assertEqual(f["split_group"]["total_usd"], "423.00")
        self.assertEqual(f["settled_by_code"]["escalate_to"], "compliance")

    def test_duplicate_wins_over_late(self):
        # Order of checks: a duplicate is checked before lateness
        claim = dict(CLAIMS["CLM-3007"], submitted_at="2026-12-31T00:00:00Z")
        f = rules.deterministic_checks(claim, EMPLOYEES, PAST, FX)
        self.assertEqual(f["settled_by_code"]["flag"], "duplicate")

    def test_monthly_cap_uses_amount_checked(self):
        f = rules.deterministic_checks(CLAIMS["CLM-3003"], EMPLOYEES, PAST, FX,
                                       amount_usd=Decimal("960.00"))
        self.assertTrue(f["monthly_cap"]["exceeds_cap"])   # 1980 + 960 > 2500

    def test_preapproval_on_another_employee_is_invalid(self):
        # PA-5002 belongs to Asha Verma, not Lucas Meyer
        ok, why = rules.valid_preapproval(EMPLOYEES["Lucas Meyer"], "PA-5002", "equipment",
                                          Decimal("249"))
        self.assertFalse(ok)
        self.assertIn("not listed", why)

    def test_preapproval_on_own_record_is_valid(self):
        ok, _ = rules.valid_preapproval(EMPLOYEES["Mei Tan"], "PA-5001", "equipment", Decimal("500"))
        self.assertTrue(ok)


class GuardTests(unittest.TestCase):
    """The guard must accept correct decisions and refuse hard-stop violations."""

    def validate(self, cid, **changes):
        decision = {
            "claim_id": cid, "decision": "approve", "approved_amount_usd": "0.00",
            "policy_refs": ["EX-201"], "flags": [], "reply_to_employee": "",
            "escalate_to": "none",
        }
        decision.update(changes)
        return guard.validate_decision(decision, CLAIMS[cid], EMPLOYEES, PAST, FX)

    def test_correct_approval_is_accepted(self):
        reply = ("Hi Daniel,\n\nYour lunch claim of $198.00 has been recorded and passed to a finance "
                 "reviewer, who makes the final decision.\n\nFinance Operations")
        errors, out = self.validate("CLM-3001", approved_amount_usd="198.00", reply_to_employee=reply)
        self.assertEqual(errors, [])
        self.assertEqual(out["approved_amount_usd"], "198.00")

    def test_approval_above_claim_is_refused(self):
        reply = "Hi Daniel,\n\nThanks.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3001", approved_amount_usd="250.00", reply_to_employee=reply)
        self.assertTrue(any("more than the claimed" in e for e in errors))

    def test_reply_that_says_paid_is_refused(self):
        reply = "Hi Daniel,\n\nGood news, this will be paid on Friday.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3001", approved_amount_usd="198.00", reply_to_employee=reply)
        self.assertTrue(any("paid" in e for e in errors))

    def test_reply_that_accuses_is_refused(self):
        reply = "Hi Daniel,\n\nThis looks like fraud.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3001", approved_amount_usd="198.00", reply_to_employee=reply)
        self.assertTrue(any("dishonesty" in e for e in errors))

    def test_reply_naming_another_employee_is_refused(self):
        reply = "Hi Daniel,\n\nPlease note Mei Tan's claim was handled too.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3001", approved_amount_usd="198.00", reply_to_employee=reply)
        self.assertTrue(any("another person" in e for e in errors))

    def test_reply_citing_another_employees_claim_is_refused(self):
        reply = "Hi Daniel,\n\nCompare with C-9201.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3001", approved_amount_usd="198.00", reply_to_employee=reply)
        self.assertTrue(any("own claims" in e for e in errors))

    def test_duplicate_must_be_rejected(self):
        reply = "Hi Ravi,\n\nThis repeats your earlier claim C-9004.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3007", decision="approve", approved_amount_usd="17.40",
                                  reply_to_employee=reply)
        self.assertTrue(any("settles this claim as reject" in e for e in errors))

    def test_monthly_cap_breach_cannot_be_approved(self):
        reply = "Hi Asha,\n\nYour London hotel claim is with a manager.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3003", approved_amount_usd="960.00", reply_to_employee=reply)
        self.assertTrue(any("monthly cap" in e for e in errors))

    def test_missing_receipt_cannot_be_approved(self):
        reply = "Hi Omar,\n\nPlease send the receipt.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3006", approved_amount_usd="42.00", reply_to_employee=reply)
        self.assertTrue(any("receipt is required" in e for e in errors))

    def test_escalation_needs_a_reason(self):
        reply = "Hi Daniel,\n\nA finance controller will review this.\n\nFinance Operations"
        errors, _ = self.validate("CLM-3009", decision="escalate", escalate_to="finance-controller",
                                  flags=["late_submission"], reply_to_employee=reply)
        self.assertTrue(any("escalation_reason" in e for e in errors))

    def test_unknown_field_is_refused(self):
        errors, _ = self.validate("CLM-3001", extra="x", reply_to_employee="Hi Daniel,\n\nFinance Operations")
        self.assertTrue(any("unknown field" in e for e in errors))


class StoreTests(unittest.TestCase):
    def test_path_traversal_ids_are_refused(self):
        for bad in ["../secrets", "CLM 1", "", None]:
            with self.assertRaises(store.StoreError, msg=repr(bad)):
                store.check_claim_id(bad)


if __name__ == "__main__":
    unittest.main()
