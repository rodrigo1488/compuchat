"""Mesa pelo Yoda: mapeamento, total e o ramo que continua no Postgres."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from uniplus_handler import UniplusPermanentError  # noqa: E402
from yoda_gourmet import (  # noqa: E402
    YodaOperationalError,
    build_conta_api,
    clear_read_cache,
    clear_token_cache,
    get_open_mesa_conta_yoda,
    handle_yoda_mesa_job,
    fetch_open_contas,
    list_open_contas_yoda,
    map_mesa_conta,
    map_open_contas,
    projetar_ocupacao_salao,
    render_test_listar_mesas,
    run_uniplus_launch,
    yoda_settings,
)

YODA_LISTA = "/unichef/buscarcontasabertasoubloqueadasoureservadas"
YODA_CONTA = "/public-api/v1/gourmet/conta"
PDV_LISTA = "/pdvrest/unichef/buscarcontasabertasoubloqueadasoureservadas/read"
PDV_CONTA = "/pdvrest/unichef/buscarcontaaberta/read"
PDV_ENVIAR = "/pdvrest/unichef/enviarconta/read"
PDV_PROXIMO = "/pdvrest/unichef/getproximonumerodecontadodia/read"
PDV_HASH = "/pdvrest/unichef/buscarcontaporhash/read"


def _lista_status_array():
    """Array {numero, nome, status}. O filtro de status vale só neste formato."""
    return [
        {
            "numero": 12,
            "nome": "Ana",
            "status": "ABERTO",
            "mesaComanda": 4,
        },
        {
            "numero": 3,
            "nome": "Livre",
            "status": "DISPONIVEL",
            "mesaComanda": 3,
        },
        {
            "numero": 8,
            "nome": "Bia",
            "status": "CONFERENCIA_EMITIDA",
            "mesaComanda": 8,
        },
    ]


def _mapa_contas_abertas():
    """ContainerConta.contasAbertas. A chave não é o número da mesa."""
    return {
        "contasAbertas": {
            "99": {
                "numeroConta": 12,
                "nomeCliente": "Ana",
                "valorTotal": 25,
                "mesaComanda": 4,
            },
            "3": {
                "numeroConta": 8,
                "nomeCliente": "Bia",
                "valorTotal": 10,
                "status": "DISPONIVEL",
            },
        }
    }


def _conta_cliente():
    """ContaCliente / ContaClienteTO."""
    return {
        "numeroConta": 12,
        "nomeCliente": "Ana",
        "valorTotal": 25,
        "itens": [
            {
                "codigoProduto": "10",
                "nomeProduto": "Suco",
                "quantidade": 2,
                "precoUnitario": 12.5,
                "valorTotal": 25,
                "observacao": "laranja",
            }
        ],
    }


def _conta_api():
    """ContaAPI de GET /public-api/v1/gourmet/conta."""
    return {
        "numero": 12,
        "tipo": "MESA",
        "codigoFilial": "1",
        "cliente": {"nome": "Ana"},
        "valorTotalItens": 25,
        "itens": [
            {
                "codigoProduto": "10",
                "nomeProduto": "Suco",
                "quantidade": 2,
                "valorUnitario": 12.5,
                "valorTotal": 25,
                "observacao": "laranja",
            }
        ],
    }


class MemDb:
    def __init__(self, data, produtos=None):
        self.data = dict(data)
        self.produtos = {
            str(codigo): int(valor) for codigo, valor in (produtos or {}).items()
        }

    def get_config(self, key):
        return self.data.get(key)

    def get_sync_product_id(self, codigo):
        return self.produtos.get(str(codigo or "").strip())


def _mesa(**overrides):
    conteudo = {
        "orderType": "mesa",
        "protocol": "abc-1",
        "contamesa": {
            "numeromesa": "Mesa 12",
            "nomecliente": "Ana",
            "valortotal": "25.00",
            "observacao": "sem gelo",
        },
        "itens": [
            {
                "codigoproduto": "10",
                "nomeproduto": "Suco",
                "quantidade": 2,
                "precounitario": "12.50",
                "valortotal": "25.00",
                "observacao": "laranja",
            }
        ],
    }
    conteudo.update(overrides)
    return conteudo


class YodaGourmetTest(unittest.TestCase):
    def setUp(self):
        clear_token_cache()
        clear_read_cache()
        self.db = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_base_url": "https://127.0.0.1:8443",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

    def test_mapeia_conta_api(self):
        conta = build_conta_api(self.db, _mesa())
        self.assertEqual(conta["nomeIntegracao"], "compuchat")
        self.assertEqual(conta["numero"], 12)
        self.assertEqual(conta["tipo"], "MESA")
        self.assertEqual(conta["codigoFilial"], "1")
        self.assertEqual(conta["valorTotalItens"], 25.0)
        self.assertEqual(conta["pagamentos"], [])
        item = conta["itens"][0]
        self.assertEqual(item["codigoProduto"], "10")
        self.assertEqual(item["nomeProduto"], "Suco")
        self.assertEqual(item["codigoUnidadeMedida"], "UN")
        self.assertEqual(item["quantidade"], 2.0)
        self.assertEqual(item["valorUnitario"], 12.5)
        self.assertEqual(item["valorTotal"], 25.0)

    def test_recusa_total_antes_do_http(self):
        def http(*_args, **_kwargs):
            raise AssertionError("HTTP não deveria ser chamado")

        ruim = _mesa()
        ruim["itens"][0]["valortotal"] = "10.00"
        with self.assertRaises(UniplusPermanentError):
            handle_yoda_mesa_job(self.db, ruim, http=http)

    def test_postgres_delivery_e_edicao_nao_chamam_o_yoda(self):
        chamadas = []

        def sql(_db, conteudo):
            if conteudo.get("updateExisting"):
                chamadas.append("edit")
            else:
                chamadas.append(conteudo.get("orderType"))
            return {"action": "inserted", "conta_id": 9}

        def http(*_args, **_kwargs):
            raise AssertionError("Yoda não deveria ser chamado")

        postgres = MemDb({"uniplus_launch_mode": "postgres"})
        run_uniplus_launch(postgres, _mesa(), sql, http=http)
        run_uniplus_launch(
            self.db, _mesa(orderType="delivery"), sql, http=http
        )
        run_uniplus_launch(
            self.db, _mesa(updateExisting=True), sql, http=http
        )
        self.assertEqual(chamadas, ["mesa", "delivery", "edit"])

    def test_mesa_yoda_nao_inventa_conta_id(self):
        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                self.assertNotIn("access_token", headers.get("Authorization", ""))
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            posted = json.loads(body.decode("utf-8"))
            self.assertEqual(posted["tipo"], "MESA")
            self.assertEqual(headers["Authorization"], "Bearer tok-teste")
            return 200, b""

        result = handle_yoda_mesa_job(self.db, _mesa(), http=http)
        self.assertEqual(result["action"], "yoda_mesa")
        self.assertIsNone(result["conta_id"])
        self.assertEqual(result["numeromesa"], 12)
        self.assertNotIn("tok-teste", json.dumps(result))

    def test_servidor_pdv_lista_e_le_consumo_sem_oauth(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_base_url": "https://127.0.0.1:8443",
                "uniplus_yoda_id_filial": "7",
            }
        )
        self.assertEqual(yoda_settings(leitura)["base_url"], "https://127.0.0.1:1099")

        def http(method, url, body, headers, timeout=20):
            posted = json.loads(body.decode("utf-8")) if body else None
            http.chamadas.append((method, url, headers.get("Authorization"), posted))
            self.assertEqual(method, "POST")
            self.assertIsNone(headers.get("Authorization"))
            self.assertNotIn("list_mesa", url)
            self.assertNotIn("/mobile/pos", url)
            self.assertTrue(url.startswith("https://127.0.0.1:1099/"))
            if url.endswith(PDV_LISTA):
                self.assertEqual(posted, {"tipo": 2, "idFilial": 7})
                return 200, json.dumps(_mapa_contas_abertas()).encode()
            if url.endswith(PDV_CONTA):
                self.assertEqual(posted["tipoConta"], 2)
                self.assertEqual(posted["idFilial"], 7)
                if posted["numeroDaConta"] == 9:
                    return 200, b"{}"
                self.assertEqual(posted["numeroDaConta"], 12)
                return 200, json.dumps(_conta_cliente()).encode()
            raise AssertionError(url)

        http.chamadas = []
        mesas = list_open_contas_yoda(leitura, http=http)
        self.assertEqual(
            mesas,
            [
                {"numeromesa": 12, "cliente": "Ana", "valortotal": 25.0},
                {"numeromesa": 8, "cliente": "Bia", "valortotal": 10.0},
            ],
        )
        self.assertEqual(list_open_contas_yoda(leitura, http=http), mesas)
        conta = get_open_mesa_conta_yoda(leitura, 12, http=http)
        self.assertTrue(conta["open"])
        self.assertEqual(conta["cliente"], "Ana")
        self.assertEqual(conta["itens"][0]["codigoproduto"], "10")
        self.assertEqual(conta["valortotal"], 25.0)
        self.assertFalse(get_open_mesa_conta_yoda(leitura, 9, http=http)["open"])
        self.assertEqual(sum(url.endswith(PDV_LISTA) for _, url, _, _ in http.chamadas), 1)
        self.assertEqual(sum("oauth" in url for _, url, _, _ in http.chamadas), 0)

    def test_ver_conta_lista_itens_do_envelope_pdv(self):
        """buscarcontaaberta devolve ContaClienteTO em response.entity."""
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_id_filial": "1",
            }
        )
        corpo = {
            "conta": {
                "numeroConta": 125,
                "nomeCliente": "Ana",
                "valorTotal": 11,
            },
            "itens": [
                {
                    "codigoProduto": "1192",
                    "nomeProduto": "COCA COLA ZERO 1L PET",
                    "quantidade": 1,
                    "precoUnitario": 10,
                    "valorTotal": 10,
                    "confirmado": True,
                    "cancelado": False,
                    "idProduto": 194,
                },
                {
                    "codigoProduto": "1243",
                    "nomeProduto": "AGUA COM GAS 500ML",
                    "quantidade": 1,
                    "precoUnitario": 5,
                    "valorTotal": 5,
                    "confirmado": False,
                    "cancelado": False,
                    "idProduto": 245,
                },
            ],
        }
        envelope = {
            "response": {"statusType": "OK", "entity": json.dumps(corpo)}
        }

        def http(method, url, body, headers, timeout=20):
            self.assertEqual(method, "POST")
            self.assertIsNone(headers.get("Authorization"))
            self.assertTrue(url.endswith(PDV_CONTA))
            posted = json.loads(body.decode("utf-8"))
            if posted["numeroDaConta"] == 9:
                return 200, json.dumps(
                    {"response": {"statusType": "OK", "entity": "{}"}}
                ).encode()
            self.assertEqual(posted["numeroDaConta"], 125)
            if http.objeto:
                return 200, json.dumps(
                    {"response": {"statusType": "OK", "entity": corpo}}
                ).encode()
            return 200, json.dumps(envelope).encode()

        http.objeto = False
        conta = get_open_mesa_conta_yoda(leitura, 125, http=http)
        self.assertTrue(conta["open"])
        self.assertEqual(conta["numeromesa"], 125)
        self.assertEqual(conta["cliente"], "Ana")
        self.assertEqual(conta["valortotal"], 11.0)
        self.assertEqual(
            [item["nomeproduto"] for item in conta["itens"]],
            ["COCA COLA ZERO 1L PET", "AGUA COM GAS 500ML"],
        )
        self.assertEqual(conta["itens"][1]["codigoproduto"], "1243")
        self.assertEqual(conta["itens"][1]["valortotal"], 5.0)
        clear_read_cache()
        http.objeto = True
        objeto = get_open_mesa_conta_yoda(leitura, 125, http=http)
        self.assertEqual(len(objeto["itens"]), 2)
        clear_read_cache()
        self.assertFalse(get_open_mesa_conta_yoda(leitura, 9, http=http)["open"])

    def test_yoda_lista_mesas_e_consumo_com_bearer(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_base_url": "https://127.0.0.1:8443",
                "uniplus_yoda_id_filial": "7",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                http.chamadas.append((method, url, headers.get("Authorization"), None))
                self.assertEqual(method, "POST")
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            posted = json.loads(body.decode("utf-8")) if body else None
            http.chamadas.append((method, url, headers.get("Authorization"), posted))
            self.assertEqual(headers.get("Authorization"), "Bearer tok-teste")
            self.assertNotIn("list_mesa", url)
            self.assertNotIn("/mobile/pos", url)
            self.assertNotIn("/pdvrest", url)
            self.assertTrue(url.startswith("https://127.0.0.1:8443/"))
            path = url.split("?", 1)[0]
            if path.endswith(YODA_LISTA):
                self.assertEqual(method, "POST")
                self.assertEqual(posted, {"sessao": {"idFilial": 7}, "tipo": "MESA"})
                return 200, json.dumps(_mapa_contas_abertas()).encode()
            if path.endswith(YODA_CONTA):
                self.assertEqual(method, "GET")
                self.assertIsNone(body)
                query = url.split("?", 1)[1]
                if "numero=9&" in query or query.startswith("numero=9&"):
                    return 404, b""
                self.assertIn("numero=12", query)
                self.assertIn("tipo=MESA", query)
                self.assertIn("codigoFilial=1", query)
                self.assertIn("incluirItens=true", query)
                return 200, json.dumps(_conta_api()).encode()
            raise AssertionError(url)

        http.chamadas = []
        mesas = list_open_contas_yoda(leitura, http=http)
        self.assertEqual(
            mesas,
            [
                {"numeromesa": 12, "cliente": "Ana", "valortotal": 25.0},
                {"numeromesa": 8, "cliente": "Bia", "valortotal": 10.0},
            ],
        )
        self.assertEqual(list_open_contas_yoda(leitura, http=http), mesas)
        conta = get_open_mesa_conta_yoda(leitura, 12, http=http)
        self.assertTrue(conta["open"])
        self.assertEqual(conta["cliente"], "Ana")
        self.assertEqual(conta["itens"][0]["precounitario"], 12.5)
        self.assertEqual(conta["valortotal"], 25.0)
        self.assertFalse(get_open_mesa_conta_yoda(leitura, 9, http=http)["open"])
        self.assertEqual(sum(url.split("?", 1)[0].endswith(YODA_LISTA) for _, url, _, _ in http.chamadas), 1)

    def test_http_404_na_lista_nao_vira_lista_vazia(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_base_url": "https://127.0.0.1:8443",
                "uniplus_yoda_id_filial": "7",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            self.assertTrue(url.split("?", 1)[0].endswith(YODA_LISTA))
            return 404, b"<title>Error 404 Not Found</title>"

        with self.assertRaises(UniplusPermanentError) as caught:
            list_open_contas_yoda(leitura, http=http)
        self.assertIn("404", str(caught.exception))

    def test_mapa_de_contas_abertas_nao_vira_lista_vazia(self):
        """O array filtra status. O mapa contasAbertas entra inteiro, por numeroConta."""
        mesas = map_open_contas(_lista_status_array())
        self.assertEqual(
            mesas,
            [
                {"numeromesa": 12, "cliente": "Ana", "valortotal": 0.0},
                {"numeromesa": 8, "cliente": "Bia", "valortotal": 0.0},
            ],
        )
        legado = map_open_contas(_mapa_contas_abertas())
        self.assertEqual(
            legado,
            [
                {"numeromesa": 12, "cliente": "Ana", "valortotal": 25.0},
                {"numeromesa": 8, "cliente": "Bia", "valortotal": 10.0},
            ],
        )
        plana = map_mesa_conta(12, _conta_cliente())
        self.assertTrue(plana["open"])
        self.assertEqual(plana["numeromesa"], 12)
        self.assertEqual(plana["cliente"], "Ana")
        envelope = map_mesa_conta(
            12,
            {"conta": {"numeroConta": 12, "nomeCliente": "Ana", "valorTotal": 25}, "itens": []},
        )
        self.assertEqual(envelope["valortotal"], 25.0)
        self.assertFalse(map_mesa_conta(9, {})["open"])

    def test_pagina_mostra_corpo_bruto_e_lista_mapeada(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_base_url": "https://127.0.0.1:8443",
                "uniplus_yoda_id_filial": "7",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            self.assertTrue(url.split("?", 1)[0].endswith(YODA_LISTA))
            self.assertEqual(method, "POST")
            return 200, json.dumps(_mapa_contas_abertas()).encode()

        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn('string "MESA"', pagina)
        self.assertIn(YODA_LISTA, pagina)
        self.assertIn("mapa contasAbertas", pagina)
        self.assertIn("numeroConta", pagina)
        self.assertIn("Ana", pagina)
        self.assertIn(">12<", pagina)
        self.assertIn("https://127.0.0.1:8443", pagina)
        self.assertNotIn("tok-teste", pagina)
        self.assertNotIn("Authorization", pagina)
        self.assertNotIn("Bearer", pagina)

    def test_pagina_filtra_status_do_array_e_mostra_a_conta(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_id_filial": "7",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            return 200, json.dumps(_lista_status_array()).encode()

        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn("Itens antes do filtro: 3", pagina)
        self.assertIn("Descartados pelo status: 1", pagina)
        self.assertIn("DISPONIVEL", pagina)
        self.assertIn("ABERTO", pagina)
        self.assertIn("CONFERENCIA_EMITIDA", pagina)
        self.assertIn("Quantidade mapeada: <strong>2</strong>", pagina)
        self.assertIn("<td>12</td>", pagina)
        self.assertIn("<td>8</td>", pagina)
        self.assertNotIn("<td>3</td>", pagina)
        self.assertIn("Mesa 3: status DISPONIVEL", pagina)

    def test_pagina_http_404_nao_vira_lista_vazia(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_yoda_id_filial": "7",
                "uniplus_yoda_codigo_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(
                    {"access_token": "tok-teste", "expires_in": 3600}
                ).encode()
            return 404, b"<title>Error 404 Not Found</title>"

        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn("A chamada falhou", pagina)
        self.assertIn("404", pagina)
        self.assertIn("Error 404 Not Found", pagina)
        self.assertIn("A lista não foi montada", pagina)
        self.assertNotIn("O que o mapeamento entregou ao POS", pagina)
        self.assertNotIn("Nenhuma mesa depois do mapeamento", pagina)

    def test_pagina_servidor_pdv_mostra_tipo_inteiro(self):
        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_id_filial": "7",
            }
        )

        def http(method, url, body, headers, timeout=20):
            self.assertIsNone(headers.get("Authorization"))
            self.assertTrue(url.endswith(PDV_LISTA))
            return 200, json.dumps(_mapa_contas_abertas()).encode()

        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn("inteiro 2", pagina)
        self.assertIn(PDV_LISTA, pagina)
        self.assertIn("servidor_pdv", pagina)
        self.assertIn("Ana", pagina)
        self.assertNotIn("Authorization", pagina)
        self.assertNotIn("/mobile/pos", pagina)

    def test_pagina_postgres_nao_abre_banco_nem_http(self):
        leitura = MemDb({"uniplus_launch_mode": "postgres"})

        def http(*_args, **_kwargs):
            raise AssertionError("HTTP não deveria ser chamado")

        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn("postgres", pagina)
        self.assertIn("não abre o Postgres", pagina)

    def test_envelope_pdv_entity_string_vira_mesas_1_e_199(self):
        """response.entity é texto. A chave do mapa não é o número da mesa."""
        entity = {
            "contasBloqueadas": [],
            "mesasComComanda": [],
            "contasReservadas": [],
            "contasAbertas": {
                "99": {
                    "numeroConta": "1",
                    "valorTotal": "45.90",
                    "nomeCliente": "teste",
                    "status": "ABERTA",
                    "tipoPedido": "MESA",
                    "fechada": False,
                },
                "88": {
                    "numeroConta": "199",
                    "valorTotal": "70.00",
                    "nomeCliente": "outra",
                    "status": "ABERTA",
                    "tipoPedido": "MESA",
                    "fechada": False,
                },
            },
        }
        envelope = {
            "response": {
                "statusType": "OK",
                "headers": {},
                "entity": json.dumps(entity),
            }
        }
        mesas = map_open_contas(envelope)
        self.assertEqual(
            mesas,
            [
                {"numeromesa": 1, "cliente": "teste", "valortotal": 45.9},
                {"numeromesa": 199, "cliente": "outra", "valortotal": 70.0},
            ],
        )
        objeto = dict(envelope)
        objeto["response"] = dict(envelope["response"], entity=entity)
        self.assertEqual(map_open_contas(objeto), mesas)

        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_id_filial": "1",
            }
        )

        def http(method, url, body, headers, timeout=20):
            self.assertIsNone(headers.get("Authorization"))
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith(PDV_LISTA))
            self.assertEqual(json.loads(body.decode("utf-8")), {"tipo": 2, "idFilial": 1})
            return 200, json.dumps(envelope).encode()

        lista = list_open_contas_yoda(leitura, http=http)
        self.assertEqual(lista, mesas)
        pagina = render_test_listar_mesas(leitura, http=http)
        self.assertIn("mapa contasAbertas", pagina)
        self.assertIn("response.entity string", pagina)
        self.assertIn("desembrulhado", pagina)
        self.assertIn(">1<", pagina)
        self.assertIn(">199<", pagina)
        self.assertIn("teste", pagina)
        self.assertNotIn("tipo do json: outro", pagina.lower())

    def test_ponte_pdv_nao_abre_postgres_nem_faz_fallback(self):
        import uniplus_handler
        from yoda_gourmet import get_open_mesa_conta_yoda, list_open_contas_yoda

        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_enabled": "true",
                "uniplus_yoda_id_filial": "1",
                "uniplus_connection_string": "postgresql://compuchat@127.0.0.1/uniplus",
            },
            produtos={"10": 2},
        )
        conexoes = []
        original = uniplus_handler._connect

        def conectar(dsn):
            conexoes.append(dsn)
            raise AssertionError("psycopg2")

        uniplus_handler._connect = conectar
        try:
            def http(method, url, body, headers, timeout=20):
                return 404, b"Error 404 Not Found"

            with self.assertRaises(UniplusPermanentError) as lista:
                list_open_contas_yoda(leitura, http=http)
            self.assertIn("404", str(lista.exception))
            with self.assertRaises(UniplusPermanentError):
                uniplus_handler.list_open_contas(leitura)
            with self.assertRaises(UniplusPermanentError) as conta:
                get_open_mesa_conta_yoda(leitura, 1, http=http)
            self.assertIn("404", str(conta.exception))
            with self.assertRaises(UniplusPermanentError):
                uniplus_handler.list_pedidos_dia(leitura)
            with self.assertRaises(UniplusPermanentError):
                uniplus_handler.set_item_entregue(leitura, 1, True)
            with self.assertRaises(UniplusPermanentError):
                uniplus_handler.update_open_mesa_cliente_name(leitura, 1, "Ana")

            enviados = []
            fase = {"conta": "nova"}

            def http_mesa(method, url, body, headers, timeout=20):
                self.assertEqual(method, "POST")
                self.assertIsNone(headers.get("Authorization"))
                self.assertNotIn("oauth", url)
                self.assertNotIn("/public-api/", url)
                posted = json.loads(body.decode("utf-8"))
                if url.endswith(PDV_HASH):
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                if url.endswith(PDV_PROXIMO):
                    self.assertEqual(posted, {"sessao": {"idFilial": 1}, "tipoPedido": 0})
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": "77"}}
                    ).encode()
                if url.endswith(PDV_CONTA):
                    if posted.get("tipoConta") == 0:
                        self.assertEqual(posted["idFilial"], 1)
                        self.assertIn(posted["numeroDaConta"], (12, 77))
                        return 200, b"{}"
                    self.assertEqual(posted["tipoConta"], 2)
                    self.assertEqual(posted["idFilial"], 1)
                    self.assertEqual(posted["numeroDaConta"], 12)
                    if fase["conta"] == "nova":
                        return 200, b"{}"
                    if fase["conta"] == "reuso":
                        conta = _conta_cliente()
                        conta["itens"][0]["idProduto"] = 9
                        conta["itens"][0]["idUnidadeMedida"] = 8
                        return 200, json.dumps(conta).encode()
                    return 200, json.dumps(_conta_cliente()).encode()
                if url.endswith(PDV_ENVIAR):
                    enviados.append(posted)
                    numero = posted["contaClienteTOJsonObj"]["conta"]["numeroConta"]
                    entity = {"id": 44, "numeroConta": numero}
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": json.dumps(entity)}}
                    ).encode()
                raise AssertionError(url)

            def sql(_db, _conteudo):
                raise AssertionError("Postgres não deveria ser chamado")

            fase["conta"] = "nova"
            nova = run_uniplus_launch(leitura, _mesa(), sql, http=http_mesa)
            self.assertEqual(nova["action"], "servidor_pdv_mesa")
            self.assertEqual(nova["conta_id"], 44)
            self.assertEqual(nova["numeromesa"], 12)
            self.assertEqual(nova["protocol"], "abc-1")
            self.assertEqual(nova["itens_count"], 1)
            corpo_novo = enviados[-1]
            self.assertEqual(corpo_novo["sessao"], {"idFilial": 1})
            conta_nova = corpo_novo["contaClienteTOJsonObj"]
            self.assertFalse(conta_nova["atualizarComanda"])
            self.assertEqual(conta_nova["conta"]["numeroConta"], 12)
            self.assertEqual(conta_nova["conta"]["tipoPedido"], "COMANDA")
            self.assertEqual(conta_nova["conta"]["nomeCliente"], "Ana")
            self.assertEqual(conta_nova["conta"]["idFilial"], 1)
            self.assertEqual(conta_nova["conta"]["valorTotal"], 25.0)
            self.assertEqual(conta_nova["conta"]["obs"], "sem gelo")
            self.assertEqual(conta_nova["conta"]["status"], "ABERTA")
            self.assertEqual(len(conta_nova["conta"]["data"]), 3)
            self.assertTrue(conta_nova["conta"]["hash"])
            self.assertEqual(len(conta_nova["itens"]), 1)
            self.assertEqual(conta_nova["itens"][0]["codigoProduto"], "10")
            self.assertEqual(conta_nova["itens"][0]["valorUnitario"], 12.5)
            self.assertEqual(conta_nova["itens"][0]["precoUnitario"], 12.5)
            self.assertEqual(conta_nova["itens"][0]["idProduto"], 2)
            self.assertEqual(conta_nova["itens"][0]["idUnidadeMedida"], 30)

            fase["conta"] = "aberta"
            junta = uniplus_handler.handle_uniplus_job(leitura, _mesa(), http=http_mesa)
            self.assertEqual(junta["action"], "servidor_pdv_mesa")
            self.assertEqual(junta["conta_id"], 44)
            self.assertEqual(junta["itens_count"], 2)
            conta_junta = enviados[-1]["contaClienteTOJsonObj"]
            self.assertTrue(conta_junta["atualizarComanda"])
            self.assertEqual(conta_junta["conta"]["valorTotal"], 50.0)
            self.assertEqual(conta_junta["itens"][0]["nomeProduto"], "Suco")
            self.assertEqual(conta_junta["itens"][1]["codigoProduto"], "10")
            self.assertEqual(conta_junta["itens"][1]["idProduto"], 2)
            self.assertNotIn("idproduto", _mesa()["itens"][0])

            sem_coluna = MemDb(dict(leitura.data))
            fase["conta"] = "nova"
            antes = len(enviados)
            with self.assertRaises(UniplusPermanentError) as sem:
                run_uniplus_launch(sem_coluna, _mesa(), sql, http=http_mesa)
            self.assertIn("ERR_PDV_HTTP", str(sem.exception))
            self.assertIn("10", str(sem.exception))
            self.assertEqual(len(enviados), antes)

            fase["conta"] = "reuso"
            reuso = run_uniplus_launch(sem_coluna, _mesa(), sql, http=http_mesa)
            self.assertEqual(reuso["itens_count"], 2)
            item_reuso = enviados[-1]["contaClienteTOJsonObj"]["itens"][1]
            self.assertEqual(item_reuso["idProduto"], 9)
            self.assertEqual(item_reuso["idUnidadeMedida"], 8)

            delivery = _mesa(orderType="delivery")
            delivery["contamesa"]["valorentrega"] = "5.00"
            delivery["contamesa"]["valortotal"] = "30.00"
            delivery["contamesa"]["telefone"] = "11999990000"
            delivery["contamesa"]["endereco"] = "Rua A"
            delivery["contamesa"]["endereconumero"] = "10"
            delivery["contamesa"]["enderecobairro"] = "Centro"
            delivery["contamesa"]["retiradanobalcao"] = 0
            delivery["contamesa"]["valorpix"] = "30.00"
            delivery["contamesa"]["hash"] = (
                "12345678-1234-1234-1234-123456789abc" + " " * 4
            )
            delivery["itens"][0]["hash"] = "abcdefab-1234-5678-1234-abcdefabcdef"
            enviado_delivery = run_uniplus_launch(
                leitura, delivery, sql, http=http_mesa
            )
            self.assertEqual(enviado_delivery["action"], "servidor_pdv_delivery")
            self.assertEqual(enviado_delivery["numeromesa"], 12)
            self.assertEqual(enviado_delivery["protocol"], "abc-1")
            conta_delivery = enviados[-1]["contaClienteTOJsonObj"]["conta"]
            self.assertEqual(conta_delivery["tipoPedido"], "DELIVERY")
            self.assertEqual(conta_delivery["numeroConta"], 12)
            self.assertEqual(conta_delivery["valorEntrega"], 5.0)
            self.assertEqual(conta_delivery["valorTotal"], 30.0)
            self.assertEqual(conta_delivery["telefone"], "11999990000")
            self.assertEqual(conta_delivery["endereco"], "Rua A")
            self.assertEqual(conta_delivery["enderecoNumero"], "10")
            self.assertEqual(conta_delivery["enderecoBairro"], "Centro")
            self.assertFalse(conta_delivery["retiradaNoBalcao"])
            self.assertTrue(conta_delivery["abertaOffline"])
            self.assertEqual(conta_delivery["orderIdIntegracao"], "abc-1")
            self.assertEqual(conta_delivery["nomeIntegracao"], "compuchat")
            self.assertEqual(conta_delivery["valorPix"], 30.0)
            self.assertEqual(
                conta_delivery["hash"], "12345678-1234-1234-1234-123456789abc"
            )
            self.assertEqual(
                enviados[-1]["contaClienteTOJsonObj"]["itens"][0]["hash"],
                "abcdefab-1234-5678-1234-abcdefabcdef",
            )

            sem_numero = _mesa(orderType="delivery")
            sem_numero["contamesa"]["numeromesa"] = None
            alocado = run_uniplus_launch(leitura, sem_numero, sql, http=http_mesa)
            self.assertEqual(alocado["action"], "servidor_pdv_delivery")
            self.assertEqual(alocado["numeromesa"], 77)
            conta_alocada = enviados[-1]["contaClienteTOJsonObj"]["conta"]
            self.assertEqual(conta_alocada["tipoPedido"], "DELIVERY")
            self.assertEqual(conta_alocada["numeroConta"], 77)
            self.assertEqual(conta_alocada["valorTotal"], 25.0)
            self.assertEqual(conta_alocada["valorEntrega"], 0.0)

            with self.assertRaises(UniplusPermanentError) as edicao:
                uniplus_handler.handle_uniplus_job(
                    leitura, _mesa(updateExisting=True), http=http_mesa
                )
            self.assertIn("não tem contrato", str(edicao.exception))
            self.assertIn("Postgres não foi aberto", str(edicao.exception))
            with self.assertRaises(UniplusPermanentError) as edicao_delivery:
                run_uniplus_launch(
                    leitura,
                    _mesa(orderType="delivery", updateExisting=True),
                    sql,
                    http=http_mesa,
                )
            self.assertIn("não tem contrato", str(edicao_delivery.exception))
            self.assertEqual(len(enviados), 5)
            self.assertEqual(conexoes, [])
        finally:
            uniplus_handler._connect = original

        postgres = MemDb({"uniplus_launch_mode": "postgres"})
        chamadas = []

        def sql(_db, _conteudo):
            chamadas.append("sql")
            return {"action": "inserted", "conta_id": 4}

        resultado = run_uniplus_launch(postgres, _mesa(), sql)
        self.assertEqual(resultado["action"], "inserted")
        self.assertEqual(chamadas, ["sql"])

    def test_entity_vazio_busca_conta_id_pelo_hash(self):
        import uniplus_handler

        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_id_filial": "1",
            },
            produtos={"10": 2},
        )
        original = uniplus_handler._connect
        uniplus_handler._connect = lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Postgres não deveria ser chamado")
        )
        try:
            fase = {"hash": "vazia", "enviar": "vazio", "conta": 0}

            def http(method, url, body, headers, timeout=20):
                self.assertEqual(method, "POST")
                self.assertIsNone(headers.get("Authorization"))
                posted = json.loads(body.decode("utf-8"))
                if url.endswith(PDV_HASH):
                    self.assertIn("hashContaCliente", posted)
                    if fase["hash"] == "existe":
                        return 200, json.dumps(
                            {
                                "response": {
                                    "statusType": "OK",
                                    "entity": json.dumps(
                                        {"id": 91, "numeroConta": 77, "valorTotal": 30}
                                    ),
                                }
                            }
                        ).encode()
                    if fase["hash"] == "depois":
                        fase["hash"] = "existe"
                        return 200, json.dumps(
                            {"response": {"statusType": "OK", "entity": ""}}
                        ).encode()
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                if url.endswith(PDV_PROXIMO):
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": "77"}}
                    ).encode()
                if url.endswith(PDV_CONTA):
                    fase["conta"] += 1
                    if fase["hash"] == "aberta" and fase["conta"] > 1:
                        return 200, json.dumps(
                            {"id": 88, "numeroConta": posted["numeroDaConta"]}
                        ).encode()
                    return 200, b"{}"
                if url.endswith(PDV_ENVIAR):
                    http.enviados += 1
                    if fase["enviar"] == "id":
                        return 200, json.dumps(
                            {
                                "response": {
                                    "statusType": "OK",
                                    "entity": json.dumps({"id": 44}),
                                }
                            }
                        ).encode()
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                raise AssertionError(url)

            http.enviados = 0
            delivery = _mesa(orderType="delivery")
            delivery["contamesa"]["numeromesa"] = None
            delivery["contamesa"]["hash"] = (
                "12345678-1234-1234-1234-123456789abc" + " " * 4
            )
            fase["hash"] = "existe"
            ja = run_uniplus_launch(leitura, delivery, lambda *_a: None, http=http)
            self.assertEqual(ja["conta_id"], 91)
            self.assertEqual(ja["numeromesa"], 77)
            self.assertEqual(ja["action"], "servidor_pdv_delivery")
            self.assertEqual(http.enviados, 0)

            http.enviados = 0
            fase["hash"] = "depois"
            fase["conta"] = 0
            criado = run_uniplus_launch(leitura, delivery, lambda *_a: None, http=http)
            self.assertEqual(criado["conta_id"], 91)
            self.assertEqual(criado["numeromesa"], 77)
            self.assertEqual(http.enviados, 1)

            mesa = _mesa()
            mesa["contamesa"]["hash"] = "abcdefab-1234-5678-1234-abcdefabcdef"
            fase["hash"] = "mesa"
            fase["conta"] = 0

            def http_mesa(method, url, body, headers, timeout=20):
                posted = json.loads(body.decode("utf-8")) if body else {}
                if url.endswith(PDV_HASH):
                    if fase["hash"] == "mesa-id":
                        return 200, json.dumps({"id": 55, "numeroConta": 12}).encode()
                    fase["hash"] = "mesa-id"
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                if url.endswith(PDV_CONTA):
                    self.assertEqual(posted["tipoConta"], 2)
                    return 200, b"{}"
                if url.endswith(PDV_ENVIAR):
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                raise AssertionError(url)

            mesa_criada = run_uniplus_launch(
                leitura, mesa, lambda *_a: None, http=http_mesa
            )
            self.assertEqual(mesa_criada["action"], "servidor_pdv_mesa")
            self.assertEqual(mesa_criada["conta_id"], 55)
            self.assertEqual(mesa_criada["numeromesa"], 12)

            fase["hash"] = "aberta"
            fase["conta"] = 0
            sem_hash = _mesa(orderType="delivery")
            sem_hash["contamesa"]["numeromesa"] = None
            sem_hash["contamesa"].pop("hash", None)
            pela_aberta = run_uniplus_launch(
                leitura, sem_hash, lambda *_a: None, http=http
            )
            self.assertEqual(pela_aberta["conta_id"], 88)
            self.assertEqual(pela_aberta["numeromesa"], 77)
        finally:
            uniplus_handler._connect = original

    def test_hash_ausente_500_segue_e_balcao_entra(self):
        import uniplus_handler

        leitura = MemDb(
            {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_yoda_id_filial": "1",
            },
            produtos={"10": 2},
        )
        original = uniplus_handler._connect
        uniplus_handler._connect = lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Postgres não deveria ser chamado")
        )
        hash_conta = "12345678-1234-1234-1234-123456789abc"
        erro_null = (
            "UnicoException: org.json.JSONException: A JSONObject text must begin "
            "with '{' at character 1 (COMR2330)\n"
            "\tat unipaf.servidor.RestUtil.getJSONObject(RestUtil.java:159)\n"
            "\tat unipaf.servidor.service.UnichefService.buscarContaPorHash"
            "(UnichefService.java:319)"
        )
        try:
            fase = {"hash": "ausente"}
            enviados = []

            def http(method, url, body, headers, timeout=20):
                self.assertEqual(method, "POST")
                self.assertEqual(headers.get("Content-Type"), "application/json")
                posted = json.loads(body.decode("utf-8"))
                if url.endswith(PDV_HASH):
                    self.assertEqual(posted, {"hashContaCliente": hash_conta})
                    self.assertTrue(body.startswith(b"{"))
                    if fase["hash"] == "ausente":
                        fase["hash"] = "gravada"
                        return 500, erro_null.encode("utf-8")
                    if fase["hash"] == "gravada":
                        return 200, json.dumps(
                            {"id": 91, "numeroConta": 12, "valorTotal": 25}
                        ).encode()
                    return 500, b"Error 500"
                if url.endswith(PDV_CONTA):
                    self.assertEqual(posted["tipoConta"], 2)
                    self.assertEqual(posted["numeroDaConta"], 12)
                    return 200, b"{}"
                if url.endswith(PDV_ENVIAR):
                    enviados.append(posted)
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": ""}}
                    ).encode()
                raise AssertionError(url)

            mesa = _mesa()
            mesa["contamesa"]["hash"] = hash_conta + " " * 4
            criada = run_uniplus_launch(leitura, mesa, lambda *_a: None, http=http)
            self.assertEqual(criada["action"], "servidor_pdv_mesa")
            self.assertEqual(criada["conta_id"], 91)
            self.assertEqual(criada["numeromesa"], 12)
            self.assertEqual(len(enviados), 1)
            self.assertEqual(
                enviados[0]["contaClienteTOJsonObj"]["conta"]["hash"], hash_conta
            )

            fase["hash"] = "outro"
            with self.assertRaises(YodaOperationalError) as outro:
                run_uniplus_launch(leitura, mesa, lambda *_a: None, http=http)
            self.assertIn("HTTP 500", str(outro.exception))
            self.assertEqual(len(enviados), 1)

            def http_balcao(method, url, body, headers, timeout=20):
                posted = json.loads(body.decode("utf-8"))
                if url.endswith(PDV_HASH):
                    self.assertEqual(list(posted), ["hashContaCliente"])
                    return 500, erro_null.encode("utf-8")
                if url.endswith(PDV_PROXIMO):
                    self.assertEqual(posted["tipoPedido"], 3)
                    self.assertEqual(posted["sessao"], {"idFilial": 1})
                    return 200, json.dumps(
                        {"response": {"statusType": "OK", "entity": "41"}}
                    ).encode()
                if url.endswith(PDV_CONTA):
                    self.assertEqual(posted["tipoConta"], 3)
                    self.assertEqual(posted["numeroDaConta"], 41)
                    return 200, b"{}"
                if url.endswith(PDV_ENVIAR):
                    enviados.append(posted)
                    return 200, json.dumps(
                        {
                            "response": {
                                "statusType": "OK",
                                "entity": json.dumps({"id": 63, "numeroConta": 41}),
                            }
                        }
                    ).encode()
                raise AssertionError(url)

            balcao = _mesa(orderType="balcao")
            balcao["contamesa"]["tipopedido"] = 3
            balcao["contamesa"]["numeromesa"] = None
            balcao["contamesa"]["hash"] = hash_conta
            enviado = run_uniplus_launch(
                leitura, balcao, lambda *_a: None, http=http_balcao
            )
            self.assertEqual(enviado["action"], "servidor_pdv_balcao")
            self.assertEqual(enviado["conta_id"], 63)
            self.assertEqual(enviado["numeromesa"], 41)
            conta = enviados[-1]["contaClienteTOJsonObj"]["conta"]
            self.assertEqual(conta["tipoPedido"], "BALCAO")
            self.assertEqual(conta["numeroConta"], 41)
            self.assertEqual(conta["hash"], hash_conta)

            por_tipo = _mesa()
            por_tipo["contamesa"]["tipopedido"] = 3
            por_tipo["contamesa"]["numeromesa"] = 18
            por_tipo["contamesa"].pop("hash", None)

            def http_tipo(method, url, body, headers, timeout=20):
                posted = json.loads(body.decode("utf-8"))
                if url.endswith(PDV_CONTA):
                    self.assertEqual(posted["tipoConta"], 3)
                    self.assertEqual(posted["numeroDaConta"], 18)
                    return 200, b"{}"
                if url.endswith(PDV_ENVIAR):
                    enviados.append(posted)
                    return 200, json.dumps({"id": 70, "numeroConta": 18}).encode()
                raise AssertionError(url)

            pelo_codigo = run_uniplus_launch(
                leitura, por_tipo, lambda *_a: None, http=http_tipo
            )
            self.assertEqual(pelo_codigo["action"], "servidor_pdv_balcao")
            self.assertEqual(pelo_codigo["conta_id"], 70)
            self.assertEqual(pelo_codigo["numeromesa"], 18)
            self.assertEqual(
                enviados[-1]["contaClienteTOJsonObj"]["conta"]["tipoPedido"],
                "BALCAO",
            )
        finally:
            uniplus_handler._connect = original

    def test_catalogo_na_ponte_pdv_nao_conecta(self):
        import product_sync

        original = product_sync.db.get_config

        def get_config(key):
            return {
                "uniplus_launch_mode": "yoda",
                "uniplus_http_server": "servidor_pdv",
                "uniplus_enabled": "true",
                "uniplus_connection_string": "postgresql://compuchat@127.0.0.1/uniplus",
            }.get(key)

        product_sync.db.get_config = get_config
        try:
            with self.assertRaises(RuntimeError) as erro:
                product_sync._connect_uniplus()
            self.assertIn("Postgres não foi aberto", str(erro.exception))
        finally:
            product_sync.db.get_config = original

    def test_rota_test_listar_mesas_esta_no_flask(self):
        app_py = os.path.join(ROOT, "app.py")
        with open(app_py, encoding="utf-8") as handle:
            fonte = handle.read()
        self.assertIn('@app.route("/test_listar_mesas", methods=["GET"])', fonte)
        self.assertIn("render_test_listar_mesas(", fonte)
        self.assertIn("floor_mesas=db.list_floor_mesas()", fonte)


if __name__ == "__main__":
    unittest.main()
