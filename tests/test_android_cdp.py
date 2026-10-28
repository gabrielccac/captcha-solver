import importlib.util
import unittest
from unittest.mock import Mock


class GoogleReadinessTests(unittest.TestCase):
    def test_all_required_providers_need_a_published_binder(self):
        from experiments.android_cdp.android import google_providers_ready
        dump = ""
        for name in ("AccountContentProvider", "com.google.android.gms.phenotype", "com.google.android.chimera"):
            dump += "\n  * ContentProviderRecord{u0 " + name + "}\n    uid=10145 provider=android.content.ContentProviderProxy@abc\n    authority=" + name + "\n"
        self.assertTrue(google_providers_ready(dump))
        self.assertFalse(google_providers_ready(dump.replace("provider=android.content.ContentProviderProxy@abc", "provider=null", 1)))
        self.assertFalse(google_providers_ready("Launching content providers: " + dump.replace(" provider=android.content.ContentProviderProxy@abc", "")))
        self.assertFalse(google_providers_ready("\n  * ContentProviderRecord{u0 Other}\n    provider=Proxy@abc\n    authority=other\n\n  User 0 authority to provider mappings:\n    AccountContentProvider com.google.android.gms.phenotype com.google.android.chimera"))


@unittest.skipUnless(importlib.util.find_spec("mycdp") and importlib.util.find_spec("websocket"), "optional Android CDP dependencies")
class AndroidLoginTests(unittest.TestCase):
    def test_authenticated_domain_requires_exact_host_and_session_tokens(self):
        from experiments.android_cdp.login import classify
        self.assertEqual(classify({"host":"meu.inss.gov.br","session_tokens_present":True}), "verify_session")
        self.assertIsNone(classify({"host":"meu.inss.gov.br.evil.test","session_tokens_present":True}))
        self.assertIsNone(classify({"host":"meu.inss.gov.br","session_tokens_present":False}))

    def test_visual_captcha_must_persist_and_two_factor_stops(self):
        from experiments.android_cdp.login import classify
        self.assertIsNone(classify({"visual_captcha":True}, 1))
        self.assertEqual(classify({"visual_captcha":True}, 3), "visual_captcha_required")
        self.assertEqual(classify({"two_factor":True}), "two_factor_required")
        self.assertEqual(classify({"invalid_credentials":True}), "invalid_credentials")

    def test_credentials_never_enter_another_origin(self):
        from experiments.android_cdp.login import enter
        for origin in ({"host":"sso.acesso.gov.br.evil.test","https":True},
                       {"host":"sso.acesso.gov.br","https":False}):
            client = Mock()
            client.evaluate.return_value = origin
            with self.assertRaises(AssertionError):
                enter(client, 'input[type="password"]', 'synthetic-secret')
            client.clear.assert_not_called()
            client.press_keys.assert_not_called()
