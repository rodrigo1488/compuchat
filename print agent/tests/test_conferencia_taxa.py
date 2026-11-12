import unittest
from decimal import Decimal

from pos_api import normalize_taxa_servico, somar_conferencia
from printer_service import PrinterService
from yoda_gourmet import _corpo_enviar_conta, calcular_couvert


class ConferenciaTaxaTest(unittest.TestCase):
    def test_percentual_aceita_virgula_e_limita_em_100(self):
        self.assertEqual(normalize_taxa_servico("10,5"), "10.5")
        self.assertEqual(normalize_taxa_servico(""), "0")
        self.assertEqual(normalize_taxa_servico("abc"), "0")
        self.assertEqual(normalize_taxa_servico("-3"), "0")
        self.assertEqual(normalize_taxa_servico("150"), "100")

    def test_soma_usa_itens_e_percentual(self):
        totais = somar_conferencia(
            [{"valortotal": 8.80}, {"valortotal": "1.20"}],
            99,
            Decimal("10"),
        )
        self.assertEqual(totais["subtotal"], 10.0)
        self.assertEqual(totais["taxa"], 1.0)
        self.assertEqual(totais["total"], 11.0)

    def test_desconto_percentual_abate_o_subtotal(self):
        totais = somar_conferencia(
            [{"valortotal": 100}],
            0,
            Decimal("10"),
            desconto_percent="10",
        )
        self.assertEqual(totais["taxa"], 10.0)
        self.assertEqual(totais["desconto"], 10.0)
        self.assertEqual(totais["total"], 100.0)

    def test_desconto_em_reais_nao_deixa_total_negativo(self):
        totais = somar_conferencia(
            [{"valortotal": 8.80}],
            0,
            Decimal("10"),
            desconto_valor="50",
        )
        self.assertEqual(totais["taxa"], 0.88)
        self.assertEqual(totais["desconto"], 9.68)
        self.assertEqual(totais["total"], 0.0)

    def test_couvert_multiplica_valor_por_pessoa(self):
        self.assertEqual(calcular_couvert("4,50", 2), Decimal("9.00"))
        self.assertEqual(calcular_couvert(5, 3), Decimal("15.00"))
        self.assertEqual(calcular_couvert(5, 0), Decimal("0.00"))

    def test_envio_grava_couvert_separado_do_total(self):
        corpo = _corpo_enviar_conta(
            {
                "numero": 2,
                "cliente": "Ana",
                "obs": "",
                "itens": [
                    {
                        "codigoProduto": "1",
                        "nomeProduto": "Suco",
                        "valorUnitario": 10,
                        "precoUnitario": 10,
                        "quantidade": 1,
                        "valorTotal": 10,
                    }
                ],
                "couvert_valor": "15.00",
            },
            1,
            {"open": False},
            None,
            {"1": 9},
            "MESA",
        )
        conta = corpo["contaClienteTOJsonObj"]["conta"]
        self.assertEqual(conta["valorTotal"], 10.0)
        self.assertEqual(conta["valorCouvertArtistico"], 15.0)
        self.assertFalse(conta["semCouvert"])
        self.assertNotIn("valorTaxaServico", conta)

    def test_sem_itens_usa_total_da_conta(self):
        totais = somar_conferencia([], 8.80, Decimal("10"))
        self.assertEqual(totais["subtotal"], 8.80)
        self.assertEqual(totais["taxa"], 0.88)
        self.assertEqual(totais["total"], 9.68)

    def test_cupom_da_conferencia_mostra_a_taxa(self):
        printer = PrinterService(printer_ip="127.0.0.1", printer_port=9, paper_width=32)
        text = printer._generate_receipt_text(
            {
                "form_name": "CONFERENCIA",
                "conference": True,
                "date": "06/11/2026 18:00",
                "table_number": "2",
                "customer": {"name": "Ana", "phone": "", "email": ""},
                "items_by_group": {
                    "Conta": [{"name": "Suco", "quantity": 1, "productValue": 8.8, "addons": []}]
                },
                "subtotal": 8.8,
                "service_fee": 0.88,
                "service_fee_percent": 10,
                "total": 9.68,
            }
        )
        self.assertIn("CONFERENCIA", text)
        self.assertIn("TAXA SERVICO 10,00%", text)
        self.assertIn("R$ 9,68", text)
        self.assertIn("Conferencia sem valor fiscal", text)

    def test_cupom_comum_nao_ganha_taxa(self):
        printer = PrinterService(printer_ip="127.0.0.1", printer_port=9, paper_width=32)
        text = printer._generate_receipt_text(
            {
                "form_name": "COZINHA",
                "date": "06/11/2026 18:00",
                "customer": {"name": "Ana", "phone": "", "email": ""},
                "items_by_group": {
                    "Pedido": [{"name": "Suco", "quantity": 1, "productValue": 8.8, "addons": []}]
                },
                "total": 8.8,
            }
        )
        self.assertNotIn("TAXA SERVICO", text)
        self.assertNotIn("DESCONTO", text)
        self.assertNotIn("Conferencia sem valor fiscal", text)

    def test_cupom_mostra_desconto(self):
        printer = PrinterService(printer_ip="127.0.0.1", printer_port=9, paper_width=32)
        text = printer._generate_receipt_text(
            {
                "form_name": "CONFERENCIA",
                "conference": True,
                "date": "06/11/2026 18:00",
                "table_number": "2",
                "customer": {"name": "Ana", "phone": "", "email": ""},
                "items_by_group": {
                    "Conta": [{"name": "Suco", "quantity": 1, "productValue": 10, "addons": []}]
                },
                "subtotal": 10,
                "service_fee": 1,
                "service_fee_percent": 10,
                "discount": 2,
                "discount_percent": 0,
                "total": 9,
            }
        )
        self.assertIn("DESCONTO:", text)
        self.assertIn("R$ 2,00", text)
        self.assertIn("R$ 9,00", text)


if __name__ == "__main__":
    unittest.main()
