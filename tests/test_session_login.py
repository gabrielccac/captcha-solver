import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from inss.session import LoginError, harvest_tokens, login_failure, submit_password


class LoginTests(unittest.TestCase):
    def page(self, url):
        page = Mock(url=url)
        page.locator.return_value.first.bounding_box.return_value = {
            "x": 0, "y": 0, "width": 100, "height": 30,
        }
        page.locator.return_value.all_inner_texts.return_value = []
        page.locator.return_value.first.is_visible.return_value = False
        return page

    def submit(self, page, landing_host="meu.inss.gov.br"):
        with patch("inss.session.human_type"), \
             patch("inss.session.time.monotonic", side_effect=[0, 1, 31]):
            submit_password(page, "test-password", landing_host)

    def test_accepts_meu_inss_consent_before_waiting_for_app(self):
        page = self.page("https://sso.acesso.gov.br/consent?redirect_uri=https://meu.inss.gov.br/")
        page.get_by_text.return_value.is_visible.return_value = True
        page.get_by_role.return_value.is_visible.return_value = True
        self.submit(page)
        page.get_by_text.assert_called_once_with("Serviço: Meu INSS - Site", exact=True)
        page.get_by_role.assert_called_once_with("button", name="Autorizar", exact=True)
        page.get_by_role.return_value.click.assert_called_once_with(timeout=10000)
        landed = page.wait_for_url.call_args.args[0]
        self.assertTrue(landed("https://meu.inss.gov.br/"))
        self.assertFalse(landed(page.url))
        self.assertFalse(landed("https://meu.inss.gov.br.evil.test/"))

    def test_already_in_app_does_not_request_consent(self):
        page = self.page("https://meu.inss.gov.br/")
        self.submit(page)
        page.get_by_text.assert_not_called()
        page.get_by_role.assert_not_called()

    def test_other_services_and_hosts_do_not_receive_consent(self):
        for url, service, landing in (
            ("https://sso.acesso.gov.br/consent", False, "meu.inss.gov.br"),
            ("https://sso.acesso.gov.br.evil.test/consent", True, "meu.inss.gov.br"),
            ("https://sso.acesso.gov.br/consent", True, "www.comprasnet.gov.br"),
        ):
            with self.subTest(url=url, service=service, landing=landing):
                page = self.page(url)
                page.get_by_text.return_value.is_visible.return_value = service
                self.submit(page, landing)
                page.get_by_role.assert_not_called()

    def test_waits_when_consent_button_is_not_visible(self):
        page = self.page("https://sso.acesso.gov.br/consent")
        page.get_by_text.return_value.is_visible.return_value = True
        page.get_by_role.return_value.is_visible.return_value = False
        self.submit(page)
        page.get_by_role.return_value.click.assert_not_called()
        page.wait_for_url.assert_not_called()

    def test_captured_invalid_credentials_is_detected_on_cpf_page(self):
        page = self.page("https://sso.acesso.gov.br/login")
        page.locator.return_value.all_inner_texts.return_value = [
            "Usuário e/ou senha inválidos. (ERL0003900)"]
        failure = login_failure(page)
        self.assertEqual(failure.code, "invalid_credentials")
        self.assertEqual(failure.evidence["provider_error_code"], "ERL0003900")
        with self.assertRaises(LoginError):
            self.submit(page)

    def test_captured_two_factor_requires_visible_otp_and_heading(self):
        page = self.page("https://sso.acesso.gov.br/login")
        page.locator.return_value.first.is_visible.return_value = True
        page.get_by_role.return_value.first.is_visible.return_value = True
        self.assertEqual(login_failure(page).code, "two_factor_required")
        page.get_by_role.assert_called_with("heading", exact=True,
            name="Confirme sua identidade com a Verificação em duas etapas")
        page.get_by_role.return_value.first.is_visible.return_value = False
        self.assertIsNone(login_failure(page))
        page.get_by_role.return_value.first.is_visible.return_value = True
        page.locator.return_value.first.is_visible.return_value = False
        self.assertIsNone(login_failure(page))

    def test_unknown_delay_foreign_host_and_navigation_are_not_bad_credentials(self):
        page = self.page("https://sso.acesso.gov.br/login")
        page.locator.return_value.all_inner_texts.return_value = ["Aguarde", "Esqueci minha senha"]
        self.assertIsNone(login_failure(page))
        page.locator.return_value.all_inner_texts.side_effect = PlaywrightError("Navigation")
        self.assertIsNone(login_failure(page))
        page.url = "https://sso.acesso.gov.br.evil.test/login"
        self.assertIsNone(login_failure(page))

    def test_token_wait_still_detects_late_credential_rejection(self):
        page = self.page("https://sso.acesso.gov.br/login")
        page.locator.return_value.all_inner_texts.return_value = [
            "Usuário e/ou senha inválidos. (ERL0003900)"]
        with patch("inss.session.api_session") as connect:
            with self.assertRaises(LoginError):
                harvest_tokens(page, "12345678901")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
