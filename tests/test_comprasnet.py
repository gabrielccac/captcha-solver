import unittest
from types import SimpleNamespace

from comprasnet.workflow import in_comprasnet


class ComprasnetTests(unittest.TestCase):
    def test_landing_requires_exact_host_and_path(self):
        self.assertTrue(in_comprasnet(SimpleNamespace(
            url="https://www.comprasnet.gov.br/intro.htm")))
        self.assertFalse(in_comprasnet(SimpleNamespace(
            url="https://www.comprasnet.gov.br.evil.test/intro.htm")))
        self.assertFalse(in_comprasnet(SimpleNamespace(
            url="https://www.comprasnet.gov.br/seguro/loginPortalFornecedor.asp")))


if __name__ == "__main__":
    unittest.main()
