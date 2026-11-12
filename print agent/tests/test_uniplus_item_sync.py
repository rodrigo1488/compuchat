"""Edição de delivery deve acrescentar, alterar quantidade e cancelar itens."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from uniplus_handler import _plan_item_sync  # noqa: E402


class UniplusItemSyncTest(unittest.TestCase):
    def test_add_remove_and_quantity(self):
        existing = [
            {
                "id": 1,
                "codigoproduto": "10",
                "nomeproduto": "X-Burger",
                "observacao": "",
                "quantidade": 1,
                "precounitario": 20,
                "valortotal": 20,
            },
            {
                "id": 2,
                "codigoproduto": "11",
                "nomeproduto": "Refrigerante",
                "observacao": "",
                "quantidade": 1,
                "precounitario": 8,
                "valortotal": 8,
            },
        ]
        incoming = [
            {
                "codigoproduto": "10",
                "nomeproduto": "X-Burger",
                "observacao": "",
                "quantidade": 2,
                "precounitario": 20,
                "valortotal": 40,
            },
            {
                "codigoproduto": "12",
                "nomeproduto": "Batata",
                "observacao": "",
                "quantidade": 1,
                "precounitario": 12,
                "valortotal": 12,
            },
        ]

        plan = _plan_item_sync(existing, incoming)

        self.assertEqual(plan["cancel_ids"], [2])
        self.assertEqual(len(plan["inserts"]), 1)
        self.assertEqual(plan["inserts"][0]["nomeproduto"], "Batata")
        self.assertEqual(plan["updates"][0]["id"], 1)
        self.assertEqual(plan["updates"][0]["quantidade"], 2)
        self.assertEqual(plan["updates"][0]["valortotal"], 40)

    def test_unchanged_line_is_kept(self):
        existing = [
            {
                "id": 7,
                "codigoproduto": "10",
                "nomeproduto": "X-Burger",
                "observacao": "sem cebola",
                "quantidade": 1,
                "precounitario": 20,
                "valortotal": 20,
            }
        ]
        incoming = [
            {
                "codigoproduto": "10",
                "nomeproduto": "X-Burger",
                "observacao": "sem cebola",
                "quantidade": 1,
                "precounitario": 20,
                "valortotal": 20,
            }
        ]

        plan = _plan_item_sync(existing, incoming)

        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["cancel_ids"], [])
        self.assertEqual(plan["inserts"], [])


if __name__ == "__main__":
    unittest.main()
