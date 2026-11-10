import json
import unittest
from ai_assistant import hh_browser_provider as bp

class NavigationGateTests(unittest.TestCase):
    def test_timeout_does_not_evaluate_stale_login_page(self):
        calls = []
        def evaluate(js):
            calls.append(js)
            if js == bp._READY_JS:
                return json.dumps({'href': 'https://hh.ru/account/login', 'ready': 'complete'})
            if js == bp._AUTH_JS:
                return json.dumps({'login_card_visible': True})
            return ''
        evidence = bp.settle_hh_page(evaluate, delay=0, ready_timeout=0.001)
        self.assertIsNone(evidence)
        self.assertNotIn(bp._AUTH_JS, calls)
        self.assertEqual(bp.auth_gate({'authenticated': False, 'verified': False})['status'], bp.HH_PAGE_NOT_READY)

    def test_loaded_login_is_still_rejected(self):
        states = iter(['about:blank', bp.AUTH_URL])
        def evaluate(js):
            if js == bp._READY_JS:
                return json.dumps({'href': next(states), 'ready': 'complete'})
            if js == bp._AUTH_JS:
                return json.dumps({'path': '/applicant/profile/me', 'login_card_visible': True})
            return ''
        evidence = bp.settle_hh_page(evaluate, delay=0, ready_timeout=1)
        self.assertEqual(bp.auth_gate(bp.decide_authentication(evidence))['status'], bp.HH_AUTH_REQUIRED)

if __name__ == '__main__':
    unittest.main()
