import unittest

from inss.concession import parse_concession_text


LETTER = """DATA: 14/10/2026
CARTA DE CONCESSÃO / MEMÓRIA DE CÁLCULO
NOME                    NIT             OL              NB
CLIENTE DE TESTE        1234567890-1    10.001.150      123.456.789-0
COMUNICAMOS QUE LHE FOI CONCEDIDO BEN. ASSISTENCIAL A PESSOA COM DEFICIENCIA(87)
123.456.789-0 REQUERIDO EM 13/03/2025
COM RENDA MENSAL INICIAL DE R$ 1.518,00, CALCULADA CONFORME ABAIXO, COM INÍCIO DO PAGAMENTO A PARTIR DE 13/03/2025.
COMPAREÇA A PARTIR DE 29/07/2026 NA INSTITUIÇÃO BANCÁRIA INDICADA ABAIXO.
OS CRÉDITOS SUBSEQUENTES SERÃO EFETUADOS NO 3o. DIA ÚTIL DE CADA MÊS.
ORGÃO PAGADOR / AGÊNCIA BANCÁRIA:

123456 - BANCO EXEMPLO - AGÊNCIA EXEMPLO
RUA EXEMPLO, 1 - CIDADE - UF
*** NAO HOUVE GERACAO DE CREDITOS ATRASADOS DE EXERCICIO ANTERIOR ***
Renda Mensal do primeiro pagamento proporcional ao periodo de 13/03/2025 a 31/03/2025
"""


class ConcessionTests(unittest.TestCase):
    def test_extracts_identity_dates_exact_amount_bank_and_notices(self):
        result = parse_concession_text(LETTER)
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(result["customer_name"], "CLIENTE DE TESTE")
        self.assertEqual(result["nit"], "12345678901")
        self.assertEqual(result["benefit_number"], "1234567890")
        self.assertEqual(result["benefit_type_code"], 87)
        self.assertEqual(result["request_date"], "2025-03-13")
        self.assertEqual(result["initial_monthly_amount"], "1518.00")
        self.assertEqual(result["bank_attendance_from"], "2026-07-29")
        self.assertEqual(result["paying_bank_address"], "RUA EXEMPLO, 1 - CIDADE - UF")
        self.assertEqual(result["first_payment_period_end"], "2025-03-31")
        self.assertEqual(len(result["credit_notices"]), 1)
        self.assertEqual(result["text"], LETTER.strip())

    def test_missing_or_scanned_text_does_not_invent_values(self):
        result = parse_concession_text("")
        self.assertEqual(result["parse_status"], "partial")
        self.assertIsNone(result["initial_monthly_amount"])
        self.assertIsNone(result["payment_start_date"])
        self.assertIn("benefit_number", result["missing_fields"])

    def test_amount_preserves_cents_and_invalid_date_is_missing(self):
        result = parse_concession_text(LETTER.replace("1.518,00", "12.345,67").replace("13/03/2025", "32/03/2025"))
        self.assertEqual(result["initial_monthly_amount"], "12345.67")
        self.assertIsNone(result["request_date"])
        self.assertEqual(result["parse_status"], "partial")


if __name__ == "__main__":
    unittest.main()
