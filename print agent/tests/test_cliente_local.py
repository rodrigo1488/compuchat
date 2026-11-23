import unittest

from db import escolher_cliente_local


def _cliente(cliente_id, nome, tel="", logradouro="", num_casa="", cpf="", obs=""):
    return {
        "id": cliente_id,
        "nome": nome,
        "tel": tel,
        "logradouro": logradouro,
        "numCasa": num_casa,
        "cpf": cpf,
        "obs": obs,
    }


class EscolherClienteLocalTest(unittest.TestCase):
    def setUp(self):
        self.clientes = [
            _cliente(1, "Ana Souza", "11988887777", "Rua A", "10", "12345678901"),
            _cliente(2, "José", "", "Rua B", "2"),
            _cliente(3, "Jose", "21911112222"),
            _cliente(4, "Maria Lima", "11977776666"),
        ]

    def test_telefone_com_55_acha_o_cadastro(self):
        achou = escolher_cliente_local(self.clientes, "Ana", "5511988887777")
        self.assertEqual(achou["cliente"]["id"], 1)
        self.assertFalse(achou["ambiguo"])

    def test_telefone_ganha_mesmo_com_nome_diferente(self):
        achou = escolher_cliente_local(self.clientes, "Outra pessoa", "21911112222")
        self.assertEqual(achou["cliente"]["id"], 3)

    def test_nome_unico_ignora_acento(self):
        achou = escolher_cliente_local(
            [_cliente(4, "Maria Lima", "11977776666")],
            "maria lima",
            "",
        )
        self.assertEqual(achou["cliente"]["id"], 4)
        self.assertFalse(achou["ambiguo"])

    def test_nomes_iguais_nao_escolhem_um_cadastro(self):
        achou = escolher_cliente_local(self.clientes, "José", "")
        self.assertIsNone(achou["cliente"])
        self.assertTrue(achou["ambiguo"])

    def test_sem_cadastro(self):
        achou = escolher_cliente_local(self.clientes, "Pedro", "")
        self.assertIsNone(achou["cliente"])
        self.assertFalse(achou["ambiguo"])


if __name__ == "__main__":
    unittest.main()
