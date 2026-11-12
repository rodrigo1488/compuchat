"""Botão da config grava o id interno pela listagem que a sync já usa."""
import os
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import product_sync  # noqa: E402


class _FakeDb:
    def __init__(self):
        self.rows = {}

    def get_sync_product(self, codigo):
        return self.rows.get(str(codigo).strip())

    def update_sync_product_state(self, codigo, **kwargs):
        row = self.rows[str(codigo).strip()]
        if kwargs.get("id_produto") is not None:
            row["id_produto"] = kwargs["id_produto"]

    def set_sync_product_enabled(
        self, codigo, enabled, nome="", preco=0.0, id_produto=None
    ):
        self.rows[str(codigo).strip()] = {
            "codigo": str(codigo).strip(),
            "enabled": enabled,
            "nome": nome,
            "preco": preco,
            "id_produto": id_produto,
        }


class SyncUniplusProductIdsTest(unittest.TestCase):
    def test_grava_id_existente_e_novo_e_conta_sem_id(self):
        fake = _FakeDb()
        fake.rows["10"] = {
            "codigo": "10",
            "enabled": True,
            "id_produto": None,
            "nome": "Suco",
        }
        listed = [
            {"codigo": "10", "nome": "Suco", "preco": 1, "id_produto": 2},
            {"codigo": "11", "nome": "Agua", "preco": 2, "id_produto": None},
            {"codigo": "12", "nome": "Bolo", "preco": 3, "id_produto": 4},
            {"codigo": "?sem", "nome": "Sem codigo", "preco": 0, "id_produto": 9},
        ]
        with patch.object(product_sync, "is_uniplus_enabled", return_value=True), patch.object(
            product_sync, "get_erp_target", return_value="uniplus"
        ), patch.object(
            product_sync, "list_uniplus_products", return_value=listed
        ) as lista, patch.object(product_sync, "db", fake):
            result = product_sync.sync_uniplus_product_ids()

        self.assertEqual(lista.call_args.kwargs.get("allow_in_bridge"), True)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["com_id"], 2)
        self.assertEqual(result["sem_id"], 1)
        self.assertEqual(fake.rows["10"]["id_produto"], 2)
        self.assertTrue(fake.rows["10"]["enabled"])
        self.assertEqual(fake.rows["12"]["id_produto"], 4)
        self.assertFalse(fake.rows["12"]["enabled"])
        self.assertNotIn("11", fake.rows)
        self.assertNotIn("?sem", fake.rows)

    def test_uniplus_desativado_nao_lista(self):
        with patch.object(product_sync, "is_uniplus_enabled", return_value=False), patch.object(
            product_sync, "get_erp_target", return_value="uniplus"
        ), patch.object(product_sync, "list_uniplus_products") as lista:
            with self.assertRaises(RuntimeError) as erro:
                product_sync.sync_uniplus_product_ids()
        self.assertIn("desativado", str(erro.exception))
        lista.assert_not_called()

    def test_botao_e_rota_estao_na_config(self):
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as handle:
            app_py = handle.read()
        with open(os.path.join(ROOT, "templates", "config.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('@app.route("/api/uniplus/sync-product-ids", methods=["POST"])', app_py)
        self.assertIn("sync_uniplus_product_ids(allow_in_bridge=True)", app_py)
        self.assertIn('id="btn-sync-product-ids"', html)
        self.assertIn('type="button"', html[html.find("btn-sync-product-ids") - 80 : html.find("btn-sync-product-ids")])
        self.assertIn('id="uniplus-sync-ids-result"', html)
        self.assertIn("fetch('/api/uniplus/sync-product-ids'", html)


if __name__ == "__main__":
    unittest.main()
