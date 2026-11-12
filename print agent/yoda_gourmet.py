"""Lançamento de mesa pelo Yoda ou pelo Servidor PDV.

No Servidor PDV a mesa nova e o delivery vão em POST /pdvrest/unichef/enviarconta/read.
Delivery usa tipoPedido DELIVERY (id 0). Sem número de card, o próximo sai de
POST /pdvrest/unichef/getproximonumerodecontadodia/read.
O Postgres não abre nesse modo. O token do Yoda é um JWT emitido em
POST /oauth/token. Não existe tabela para lê-lo. Fica só na memória deste
processo e é renovado cerca de um minuto antes de vencer.
"""
from __future__ import annotations

import base64
import html
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Dict, List, Optional, Tuple

from ssl_compat import unverified_ssl_context
from uniplus_handler import UniplusPermanentError, parse_numeromesa

DEFAULT_BASE_URL = "https://127.0.0.1:8443"
DEFAULT_SERVIDOR_PDV_URL = "https://127.0.0.1:1099"
# yoda.jar: UnichefController @RequestMapping("/unichef")
# + "/buscarcontasabertasoubloqueadasoureservadas" (@RequestBody).
# Consumo: GourmetAPIController GET /public-api/v1/gourmet/conta.
# Não há /mobile/pos neste jar.
OPEN_CONTAS_PATH = "/unichef/buscarcontasabertasoubloqueadasoureservadas"
# TipoPedidoUnichef no Yoda vai como o nome do enum.
TIPO_CONTA_MESA = "MESA"
# uniplus.jar: UnichefService @Path("/unichef") @POST
# "/buscarcontasabertasoubloqueadasoureservadas/read" e
# "/buscarcontaaberta/read". Contexto Jetty /pdvrest. tipo é int (MESA = 1).
SERVIDOR_PDV_PREFIX = "/pdvrest"
SERVIDOR_OPEN_CONTAS_PATH = (
    "/pdvrest/unichef/buscarcontasabertasoubloqueadasoureservadas/read"
)
SERVIDOR_CONTA_PATH = "/pdvrest/unichef/buscarcontaaberta/read"
# UnichefService.buscarContaPorHash. O corpo é {"hashContaCliente": "<uuid>"}.
# Conta ausente volta HTTP 500: getJSONObject(null) vira o texto "null".
SERVIDOR_CONTA_HASH_PATH = "/pdvrest/unichef/buscarcontaporhash/read"
# UnichefService.enviarConta. O mesmo contexto /pdvrest, sem OAuth.
# O merge não filtra o tipo: DELIVERY entra no mesmo POST, com o nome do enum.
SERVIDOR_ENVIAR_CONTA_PATH = "/pdvrest/unichef/enviarconta/read"
# UltimoCodigoDoDiaRN.gerar. tipoPedido é o id (DELIVERY = 0), não o nome.
SERVIDOR_PROXIMO_NUMERO_PATH = "/pdvrest/unichef/getproximonumerodecontadodia/read"
# TipoPedidoUnichef. 0 é delivery. O mapa de mesas lista só 1; o de comandas só 2.
SERVIDOR_TIPO_NOME = {
    0: "DELIVERY",
    1: "MESA",
    2: "COMANDA",
    3: "BALCAO",
    4: "DRIVE_THRU",
}
_PAGAMENTO_JSON = (
    ("valordinheiro", "valorDinheiro"),
    ("valorcartao", "valorCartao"),
    ("valorpix", "valorPix"),
    ("valorcarteiradigital", "valorCarteiraDigital"),
    ("valoroutros", "valorOutros"),
    ("valorcheque", "valorCheque"),
)
# O mesmo fallback do INSERT SQL quando o produto não traz unidade.
SERVIDOR_ID_UNIDADE_MEDIDA = 30
# StatusContaMesaUniplusPos. A aba Aberto também mostra conferência emitida.
_OCCUPIED_STATUS = frozenset(
    {"ABERTO", "ABERTA", "CONFERENCIA_EMITIDA", "1", "6"}
)
_IDLE_STATUS = frozenset(
    {"DISPONIVEL", "RESERVADO", "BLOQUEADO", "TODOS", "0", "2", "3", "999"}
)
DEFAULT_CLIENT_ID = "uniplus"
# Cliente compilado em YodaClientDetailsService deste yoda.jar.
DEFAULT_CLIENT_SECRET = "l4gtr1ck2rspr3ngcl3ent"
DEFAULT_SCOPE = "public-api"
TOKEN_PATH = "/oauth/token"
CONTA_PATH = "/public-api/v1/gourmet/conta"
OPEN_CONTAS_PATH = "/unichef/buscarcontasabertasoubloqueadasoureservadas"
NOME_INTEGRACAO = "compuchat"
MONEY = Decimal("0.01")
TOKEN_REFRESH_MARGIN_SEC = 60
HTTP_TIMEOUT_SEC = 20
READ_CACHE_TTL_SEC = 3
_DIAGNOSTIC_BODY_LIMIT = 4000

_token_lock = threading.Lock()
_cached_token = ""
_cached_token_deadline = 0.0
_read_lock = threading.Lock()
_list_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
_conta_cache: Dict[Tuple[str, str, int], Dict[str, Any]] = {}

HttpCaller = Callable[..., Tuple[int, bytes]]


class YodaOperationalError(RuntimeError):
    """Falha de rede ou resposta 5xx. O job pode tentar de novo."""


def launch_mode(db_module) -> str:
    raw = (db_module.get_config("uniplus_launch_mode") or "postgres").strip().lower()
    if raw not in ("postgres", "yoda"):
        return "postgres"
    return raw


def _order_type(conteudo: Dict[str, Any]) -> str:
    contamesa = conteudo.get("contamesa") or {}
    return str(
        conteudo.get("orderType") or contamesa.get("orderType") or ""
    ).strip().lower()


def http_server(db_module) -> str:
    raw = (db_module.get_config("uniplus_http_server") or "yoda").strip().lower()
    if raw != "servidor_pdv":
        return "yoda"
    return raw


def uses_pdv_http_bridge(db_module) -> bool:
    """Servidor PDV como ponte. Nesse modo o agente não abre o Postgres."""
    return launch_mode(db_module) == "yoda" and http_server(db_module) == "servidor_pdv"


PDV_BRIDGE_SQL_BLOCKED = (
    "ERR_PDV_HTTP: operação no Servidor PDV ainda não tem contrato HTTP. "
    "O Postgres não foi aberto."
)


def is_yoda_mesa_launch(db_module, conteudo: Dict[str, Any]) -> bool:
    """Mesa nova pelo Yoda. Delivery e updateExisting ficam de fora."""
    if launch_mode(db_module) != "yoda":
        return False
    if http_server(db_module) != "yoda":
        return False
    if conteudo.get("updateExisting"):
        return False
    return _order_type(conteudo or {}) == "mesa"


def yoda_settings(db_module) -> Dict[str, str]:
    server = http_server(db_module)
    base = (db_module.get_config("uniplus_yoda_base_url") or "").strip()
    if not base or (server == "servidor_pdv" and base.rstrip("/") == DEFAULT_BASE_URL):
        base = DEFAULT_SERVIDOR_PDV_URL if server == "servidor_pdv" else DEFAULT_BASE_URL
    base = base.rstrip("/")
    secret = db_module.get_config("uniplus_yoda_client_secret") or ""
    if not secret:
        secret = DEFAULT_CLIENT_SECRET
    client_id = (db_module.get_config("uniplus_yoda_client_id") or "").strip()
    if not client_id:
        client_id = DEFAULT_CLIENT_ID
    scope = (db_module.get_config("uniplus_yoda_scope") or "").strip() or DEFAULT_SCOPE
    filial = (db_module.get_config("uniplus_yoda_codigo_filial") or "").strip()
    id_filial = (db_module.get_config("uniplus_yoda_id_filial") or "").strip()
    from uniplus_handler import configured_mesa_tipopedido

    tipo_conta, _nome = _nome_tipo_conta(configured_mesa_tipopedido(db_module))
    return {
        "base_url": base,
        "client_id": client_id,
        "client_secret": secret,
        "scope": scope,
        "codigo_filial": filial,
        "id_filial": id_filial,
        "http_server": server,
        "tipo_conta": str(tipo_conta),
    }


def _nome_tipo_conta(tipo: int) -> Tuple[int, str]:
    """Id que o PDV usa no WHERE de tipopedido e o nome que o JSON aceita."""
    try:
        codigo = int(tipo)
    except (TypeError, ValueError):
        codigo = 2
    nome = SERVIDOR_TIPO_NOME.get(codigo)
    if not nome:
        return 2, "COMANDA"
    return codigo, nome


def tipo_conta_servidor(settings: Dict[str, str]) -> int:
    codigo, _nome = _nome_tipo_conta(settings.get("tipo_conta") or 2)
    return codigo


def parse_id_filial(raw: Any) -> int:
    text = str(raw or "").strip()
    if not text.isdigit():
        raise UniplusPermanentError("ERR_YODA_CONFIG: idFilial obrigatório")
    value = int(text)
    if value <= 0:
        raise UniplusPermanentError("ERR_YODA_CONFIG: idFilial obrigatório")
    return value


def clear_token_cache() -> None:
    global _cached_token, _cached_token_deadline
    with _token_lock:
        _cached_token = ""
        _cached_token_deadline = 0.0


def clear_read_cache() -> None:
    with _read_lock:
        _list_cache.clear()
        _conta_cache.clear()


def _money(value: Any, *, default: str = "0") -> Decimal:
    raw = default if value is None or value == "" else value
    try:
        return Decimal(str(raw)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise UniplusPermanentError(
            f"ERR_YODA_PAYLOAD: valor numérico inválido ({value})"
        ) from exc


def _clip(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _uuid_limpo(value: Any) -> Optional[str]:
    """UUID com hífen. O payload pad de 40 caracteres vem com espaços."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError):
        return None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def build_conta_api(db_module, conteudo: Dict[str, Any]) -> Dict[str, Any]:
    """Monta o JSON do POST. Recusa total divergente antes de qualquer HTTP."""
    settings = yoda_settings(db_module)
    filial = settings["codigo_filial"]
    if not filial or len(filial) > 4:
        raise UniplusPermanentError(
            "ERR_YODA_CONFIG: codigoFilial obrigatório, com no máximo 4 caracteres"
        )

    contamesa = conteudo.get("contamesa") or {}
    numero = parse_numeromesa(contamesa.get("numeromesa"))
    if not numero:
        numero = parse_numeromesa(conteudo.get("numeromesa"))
    if not numero:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: mesa sem número")

    itens_in = conteudo.get("itens") or []
    if not itens_in:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: sem itens")

    itens: List[Dict[str, Any]] = []
    total = Decimal("0.00")
    for raw in itens_in:
        codigo = str(raw.get("codigoproduto") or raw.get("codigoProduto") or "").strip()
        if not codigo or len(codigo) > 20:
            raise UniplusPermanentError(
                "ERR_YODA_PAYLOAD: codigoProduto obrigatório, com no máximo 20 caracteres"
            )
        nome = _clip(raw.get("nomeproduto") or raw.get("nomeProduto") or codigo, 120)
        unidade = _clip(
            raw.get("codigoUnidadeMedida")
            or raw.get("unidademedida")
            or "UN",
            6,
        )
        if not unidade:
            unidade = "UN"
        quantidade = _money(raw.get("quantidade") or 1)
        valor_unitario = _money(raw.get("precounitario") or raw.get("valorUnitario"))
        desconto = _money(
            raw.get("valorDescontoSubtotal") or raw.get("valordesconto") or 0
        )
        if quantidade <= 0 or valor_unitario <= 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: quantidade e valorUnitario devem ser maiores que zero ({codigo})"
            )
        if desconto < 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: desconto negativo no item {codigo}"
            )
        esperado = (quantidade * valor_unitario - desconto).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        if esperado <= 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: total do item {codigo} deve ser maior que zero"
            )
        declarado = raw.get("valortotal")
        if declarado is None or declarado == "":
            declarado = raw.get("valorTotal")
        if declarado is not None and declarado != "":
            informado = _money(declarado)
            if informado != esperado:
                raise UniplusPermanentError(
                    f"ERR_YODA_PAYLOAD: total do item {codigo} é {informado}, "
                    f"esperado {esperado}"
                )
        total += esperado
        itens.append(
            {
                "codigoProduto": codigo,
                "nomeProduto": nome,
                "codigoUnidadeMedida": unidade,
                "observacao": _clip(raw.get("observacao"), 255),
                "valorUnitario": float(valor_unitario),
                "quantidade": float(quantidade),
                "valorDescontoSubtotal": float(desconto),
                "valorTotal": float(esperado),
                "opcionais": [],
            }
        )

    total = total.quantize(MONEY, rounding=ROUND_HALF_UP)
    declarado_conta = contamesa.get("valortotal")
    if declarado_conta is not None and declarado_conta != "":
        informado_conta = _money(declarado_conta)
        if informado_conta != total:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: valorTotalItens é {informado_conta}, esperado {total}"
            )

    return {
        "nomeIntegracao": NOME_INTEGRACAO,
        "numero": int(numero),
        "tipo": "MESA",
        "codigoFilial": filial,
        "observacao": _clip(contamesa.get("observacao"), 255),
        "valorDescontoSubtotal": 0.0,
        "valorTotalItens": float(total),
        "itens": itens,
        "pagamentos": [],
    }


def _http(
    method: str,
    url: str,
    body: bytes,
    headers: Dict[str, str],
    timeout: int = HTTP_TIMEOUT_SEC,
) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=unverified_ssl_context()
        ) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read() if exc.fp is not None else b""
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        raise YodaOperationalError(f"ERR_YODA_NETWORK: {exc.reason}") from exc


def _decode_error(status: int, payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    if len(text) > 500:
        text = text[:500] + "…"
    return text or f"HTTP {status}"


def fetch_access_token(
    settings: Dict[str, str],
    http: HttpCaller = _http,
    *,
    force: bool = False,
) -> str:
    global _cached_token, _cached_token_deadline
    now = time.time()
    with _token_lock:
        if (
            not force
            and _cached_token
            and now < _cached_token_deadline - TOKEN_REFRESH_MARGIN_SEC
        ):
            return _cached_token

    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": settings["scope"]}
    ).encode("utf-8")
    basic = base64.b64encode(
        f"{settings['client_id']}:{settings['client_secret']}".encode("utf-8")
    ).decode("ascii")
    status, payload = http(
        "POST",
        settings["base_url"] + TOKEN_PATH,
        body,
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    if status >= 500:
        raise YodaOperationalError(
            f"ERR_YODA_TOKEN: HTTP {status} {_decode_error(status, payload)}"
        )
    if status != 200:
        raise UniplusPermanentError(
            f"ERR_YODA_TOKEN: HTTP {status} {_decode_error(status, payload)}"
        )
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UniplusPermanentError("ERR_YODA_TOKEN: resposta sem JSON") from exc
    token = str(parsed.get("access_token") or "")
    if not token:
        raise UniplusPermanentError("ERR_YODA_TOKEN: access_token ausente")
    try:
        expires_in = int(parsed.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    if expires_in < 1:
        expires_in = 3600
    with _token_lock:
        _cached_token = token
        _cached_token_deadline = time.time() + expires_in
    return token


def _bearer_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _uses_servidor_pdv(settings: Dict[str, str]) -> bool:
    return (settings.get("http_server") or "yoda") == "servidor_pdv"


def _call_plain(
    http: HttpCaller,
    method: str,
    url: str,
    body: Optional[bytes],
) -> Tuple[int, bytes]:
    status, payload = http(
        method,
        url,
        body,
        {"Accept": "application/json", "Content-Type": "application/json"},
    )
    if status >= 500:
        raise YodaOperationalError(
            f"ERR_YODA_HTTP: HTTP {status} {_decode_error(status, payload)}"
        )
    return status, payload


def _call_yoda(
    settings: Dict[str, str],
    http: HttpCaller,
    method: str,
    url: str,
    body: Optional[bytes],
) -> Tuple[int, bytes]:
    token = fetch_access_token(settings, http)

    def _once(current: str) -> Tuple[int, bytes]:
        return http(method, url, body, _bearer_headers(current))

    status, payload = _once(token)
    if status == 401:
        clear_token_cache()
        token = fetch_access_token(settings, http, force=True)
        status, payload = _once(token)
    if status >= 500:
        raise YodaOperationalError(
            f"ERR_YODA_HTTP: HTTP {status} {_decode_error(status, payload)}"
        )
    return status, payload


def _json_body(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UniplusPermanentError("ERR_YODA_HTTP: resposta sem JSON") from exc


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_name(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("id") or raw.get("descricao") or ""
    return str(raw or "").strip().upper()


def _mesa_ocupada(raw: Dict[str, Any]) -> bool:
    """ContaMesaStatusPos traz mesa livre e ocupada. Sem status, mantém."""
    status = _status_name(raw.get("status"))
    if not status:
        return True
    if status in _OCCUPIED_STATUS:
        return True
    if status in _IDLE_STATUS:
        return False
    return True


def _desembrulhar_entity(payload: Any) -> Tuple[Any, str]:
    """Servidor PDV devolve ContainerConta dentro de response.entity.

    entity pode ser a string JSON ou o objeto já parseado. Sem esse
    envelope, o corpo segue como veio.
    """
    if not isinstance(payload, dict):
        return payload, ""
    response = payload.get("response")
    if not isinstance(response, dict) or "entity" not in response:
        return payload, ""
    entity = response.get("entity")
    if isinstance(entity, str):
        try:
            return json.loads(entity), "response.entity string"
        except json.JSONDecodeError:
            return None, "response.entity string inválida"
    if isinstance(entity, (dict, list)):
        return entity, "response.entity objeto"
    return payload, ""


def _colecao_contas(payload: Any) -> Tuple[str, List[Any], bool, bool]:
    """Forma, itens, filtrar status, e se o JSON é uma coleção conhecida.

    {} sem contasAbertas não é lista vazia: é formato não reconhecido.
    """
    if isinstance(payload, list):
        return "lista", payload, True, True
    if isinstance(payload, dict):
        key = None
        if "contasAbertas" in payload:
            key = "contasAbertas"
        elif "contasabertas" in payload:
            key = "contasabertas"
        if key is None:
            return "outro", [], False, False
        contas = payload[key]
        if isinstance(contas, dict):
            return "mapa contasAbertas", list(contas.values()), False, True
        if isinstance(contas, list):
            return "lista em contasAbertas", contas, False, True
        return "outro", [], False, False
    return "outro", [], False, False


def _numero_da_conta(raw: Dict[str, Any]) -> Optional[int]:
    numero = parse_numeromesa(raw.get("numeromesa"))
    if not numero:
        numero = parse_numeromesa(raw.get("numeroConta") or raw.get("numero"))
    return numero


def _ler_contas_abertas(payload: Any) -> Dict[str, Any]:
    interno, envelope = _desembrulhar_entity(payload)
    if envelope == "response.entity string inválida":
        return {
            "forma": "outro",
            "antes": 0,
            "filtrar_status": False,
            "status": [],
            "descartados_status": 0,
            "descartes": [
                {
                    "mesa": "?",
                    "motivo": "response.entity é texto, mas não é JSON",
                }
            ],
            "mesas": [],
            "reconhecido": False,
            "envelope": envelope,
        }
    forma, brutos, filtrar, reconhecido = _colecao_contas(interno)
    itens = [raw for raw in brutos if isinstance(raw, dict)]
    status = []
    descartes: List[Dict[str, str]] = []
    for raw in brutos:
        if not isinstance(raw, dict):
            descartes.append({"mesa": "?", "motivo": "item não é um objeto"})
    if not reconhecido:
        return {
            "forma": forma,
            "antes": 0,
            "filtrar_status": False,
            "status": [],
            "descartados_status": 0,
            "descartes": [
                {
                    "mesa": "?",
                    "motivo": "JSON sem lista e sem mapa contasAbertas; não é lista vazia",
                }
            ],
            "mesas": [],
            "reconhecido": False,
            "envelope": envelope,
        }
    seen = set()
    out: List[Dict[str, Any]] = []
    descartados_status = 0
    for raw in itens:
        nome_status = _status_name(raw.get("status")) or "(sem status)"
        status.append(nome_status)
        numero = _numero_da_conta(raw)
        rotulo = str(numero or raw.get("numeroConta") or raw.get("numero") or "?")
        if filtrar and not _mesa_ocupada(raw):
            descartados_status += 1
            descartes.append(
                {
                    "mesa": rotulo,
                    "motivo": (
                        "status "
                        + nome_status
                        + ": no array só entram ABERTO, ABERTA e CONFERENCIA_EMITIDA"
                    ),
                }
            )
            continue
        if not numero:
            descartes.append(
                {
                    "mesa": rotulo,
                    "motivo": "sem número de mesa (numeroConta ou numero)",
                }
            )
            continue
        if numero in seen:
            descartes.append({"mesa": str(numero), "motivo": "número repetido"})
            continue
        seen.add(numero)
        cliente = raw.get("nomeCliente") or raw.get("nomecliente") or raw.get("nome") or ""
        total = raw.get("valorTotal")
        if total is None:
            total = raw.get("valortotal")
        out.append(
            {
                "numeromesa": int(numero),
                "cliente": str(cliente or "").strip(),
                "valortotal": _as_float(total),
            }
        )
    return {
        "forma": forma,
        "antes": len(itens),
        "filtrar_status": filtrar,
        "status": status,
        "descartados_status": descartados_status,
        "descartes": descartes,
        "mesas": out,
        "reconhecido": True,
        "envelope": envelope,
    }


def map_open_contas(payload: Any) -> List[Dict[str, Any]]:
    """Array {numero, nome, status} ou mapa contasAbertas.

    No array, só ABERTO e CONFERENCIA_EMITIDA entram. No mapa, o item
    presente já é conta aberta: o número é numeroConta, não a chave.
    JSON que não é nem lista nem mapa contasAbertas devolve [] aqui;
    a leitura HTTP trata isso como erro, não como salão vazio.
    """
    return _ler_contas_abertas(payload)["mesas"]


def projetar_ocupacao_salao(
    mesas: List[Dict[str, Any]],
    contas: List[Dict[str, Any]],
    balcao_inicio: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """O que GET /pos/mesas grava quando a lista remota foi reconhecida.

    Não grava nada. Lista não reconhecida não deve chegar aqui: nesse
    caso o POS mantém a ocupação local.
    """
    saida = [dict(mesa) for mesa in mesas]
    open_by_num: Dict[int, Dict[str, Any]] = {}
    for conta in contas or []:
        try:
            open_by_num[int(conta["numeromesa"])] = conta
        except (KeyError, TypeError, ValueError):
            continue
    changes: List[Dict[str, Any]] = []
    motivos: List[str] = []
    for mesa in saida:
        num = parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))
        conta = open_by_num.get(num) if num is not None else None
        if conta:
            mesa["status"] = "ocupada"
            if conta.get("cliente"):
                mesa["contactName"] = conta["cliente"]
            mesa["valortotal"] = float(conta.get("valortotal") or 0)
            mesa["uniplusSeen"] = True
            changes.append(
                {
                    "id": mesa["id"],
                    "status": "ocupada",
                    "contactName": mesa.get("contactName"),
                    "contactPhone": mesa.get("contactPhone"),
                    "uniplusSeen": True,
                }
            )
            continue
        needs_clear = (
            str(mesa.get("status") or "") != "livre"
            or mesa.get("contactName")
            or mesa.get("contactPhone")
            or mesa.get("uniplusSeen")
        )
        rotulo = str(num if num is not None else mesa.get("name") or mesa.get("id"))
        if needs_clear:
            motivos.append(
                "Mesa "
                + rotulo
                + " do salão seria marcada livre: a lista reconhecida não traz esse número."
            )
        mesa["status"] = "livre"
        mesa["contactName"] = None
        mesa["contactPhone"] = None
        mesa["valortotal"] = 0
        mesa["uniplusSeen"] = False
        if needs_clear:
            changes.append(
                {
                    "id": mesa["id"],
                    "status": "livre",
                    "contactName": None,
                    "contactPhone": None,
                    "uniplusSeen": False,
                }
            )
    used_nums = set()
    for mesa in saida:
        num = parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))
        if num is not None and num in open_by_num:
            used_nums.add(num)
    sinteticas: List[int] = []
    for num, conta in open_by_num.items():
        if num in used_nums:
            continue
        if num >= int(balcao_inicio):
            motivos.append(
                "Mesa "
                + str(num)
                + " mapeada não entra no salão: número na faixa de balcão (a partir de "
                + str(balcao_inicio)
                + ")."
            )
            continue
        motivos.append(
            "Mesa "
            + str(num)
            + " não está cadastrada no salão; o POS só acrescenta uma linha nesta resposta."
        )
        sinteticas.append(num)
        saida.append(
            {
                "id": -num,
                "number": str(num),
                "name": f"Mesa {num}",
                "type": "mesa",
                "status": "ocupada",
                "formId": None,
                "contactName": conta.get("cliente") or None,
                "valortotal": float(conta.get("valortotal") or 0),
                "displayOrder": num,
                "section": None,
                "askPhone": False,
                "keywordValidation": False,
                "uniplusSeen": True,
            }
        )
    ocupadas = sum(1 for mesa in saida if mesa.get("status") == "ocupada")
    return saida, changes, {
        "ocupadas": ocupadas,
        "sinteticas": sinteticas,
        "motivos": motivos,
    }


def _map_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "nomeproduto": str(
            raw.get("nomeProduto") or raw.get("nomeproduto") or raw.get("nome") or ""
        ).strip(),
        "quantidade": _as_float(raw.get("quantidade")),
        "valortotal": _as_float(raw.get("valorTotal") or raw.get("valortotal")),
        "precounitario": _as_float(
            raw.get("valorUnitario")
            or raw.get("precoUnitario")
            or raw.get("precounitario")
        ),
        "observacao": str(raw.get("observacao") or "").strip(),
        "hora": "",
        "idproduto": None,
        "codigoproduto": str(
            raw.get("codigoProduto") or raw.get("codigoproduto") or ""
        ).strip(),
    }


def map_mesa_conta(numero: int, payload: Any) -> Dict[str, Any]:
    """ContaCliente plana, ou o envelope conta/itens e o ContaAPI do gourmet."""
    data = payload if isinstance(payload, dict) else {}
    if not data:
        return _closed_mesa(numero)
    nested = data.get("conta") if isinstance(data.get("conta"), dict) else None
    flat_conta = any(
        key in data for key in ("numeroConta", "numeromesa", "nomeCliente", "valorTotal")
    )
    if nested is not None or flat_conta or (
        "itens" in data and "valorTotalItens" not in data and "numero" not in data
    ):
        itens_raw = data.get("itens") or []
        if not itens_raw and nested is not None and isinstance(nested.get("itens"), list):
            itens_raw = nested.get("itens") or []
        if nested is None and not itens_raw and not (
            data.get("numeroConta") or data.get("numeromesa")
        ):
            return _closed_mesa(numero)
        source = nested if nested is not None else data
        if not source and not itens_raw:
            return _closed_mesa(numero)
        mesa = parse_numeromesa(source.get("numeromesa")) or parse_numeromesa(
            source.get("numeroConta")
        )
        cliente = source.get("nomeCliente") or source.get("nomecliente") or source.get("nome") or ""
        total = source.get("valorTotal")
        if total is None:
            total = source.get("valortotal")
        return {
            "open": True,
            "numeromesa": int(mesa or numero),
            "cliente": str(cliente or "").strip(),
            "valortotal": _as_float(total),
            "valortaxaservico": _taxa_da_conta(source),
            "itens": [_map_item(raw) for raw in itens_raw if isinstance(raw, dict)],
        }
    if not data:
        return _closed_mesa(numero)
    cliente = ""
    raw_cliente = data.get("cliente")
    if isinstance(raw_cliente, dict):
        cliente = str(
            raw_cliente.get("nome") or raw_cliente.get("nomeCliente") or ""
        ).strip()
    elif raw_cliente:
        cliente = str(raw_cliente).strip()
    itens = [_map_item(raw) for raw in (data.get("itens") or []) if isinstance(raw, dict)]
    total = data.get("valorTotalItens")
    if total is None:
        total = sum(item["valortotal"] for item in itens)
    return {
        "open": True,
        "numeromesa": int(numero),
        "cliente": cliente,
        "valortotal": _as_float(total),
        "valortaxaservico": _taxa_da_conta(data),
        "itens": itens,
    }


def _taxa_da_conta(source: Dict[str, Any]) -> float:
    """Couvert artístico da conta. A coluna no Uniplus é valorcouvertartistico."""
    for chave in (
        "valorCouvertArtistico",
        "valorcouvertartistico",
        "valorTaxaServico",
        "valortaxaservico",
    ):
        if chave in source and source.get(chave) not in (None, ""):
            return _as_float(source.get(chave))
    return 0.0


def _closed_mesa(numero: int) -> Dict[str, Any]:
    return {
        "open": False,
        "numeromesa": int(numero),
        "cliente": "",
        "valortotal": 0.0,
        "valortaxaservico": 0.0,
        "itens": [],
    }


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _cached_read(cache: Dict[Any, Dict[str, Any]], key: Any, loader: Callable[[], Any]) -> Any:
    """Uma ida ao Yoda por vez. Quem chega junto reusa a mesma resposta."""
    now = time.time()
    with _read_lock:
        hit = cache.get(key)
        if hit and now - float(hit["at"]) < READ_CACHE_TTL_SEC:
            if hit.get("error") is not None:
                raise hit["error"]
            return _copy_json(hit["value"])
        try:
            value = loader()
        except (UniplusPermanentError, YodaOperationalError) as exc:
            cache[key] = {"at": time.time(), "value": None, "error": exc}
            raise
        cache[key] = {"at": time.time(), "value": value, "error": None}
        return _copy_json(value)


def _sessao_filial(id_filial: int) -> Dict[str, Any]:
    """idFilial que o agente já tem. Não inventa usuário nem device do PDV."""
    return {"idFilial": id_filial}


def _http_error(status: int, payload: bytes) -> UniplusPermanentError:
    return UniplusPermanentError(
        f"ERR_YODA_HTTP: HTTP {status} {_decode_error(status, payload)}"
    )


def _yoda_conta_url(settings: Dict[str, str], numero: int) -> str:
    filial = (settings.get("codigo_filial") or "").strip()
    if not filial or len(filial) > 4:
        raise UniplusPermanentError(
            "ERR_YODA_CONFIG: codigoFilial obrigatório, com no máximo 4 caracteres"
        )
    query = urllib.parse.urlencode(
        [
            ("numero", str(numero)),
            ("tipo", TIPO_CONTA_MESA),
            ("codigoFilial", filial),
            ("incluirItens", "true"),
        ]
    )
    return settings["base_url"] + CONTA_PATH + "?" + query


def open_contas_request(settings: Dict[str, str]) -> Tuple[str, str, Dict[str, Any]]:
    """O mesmo POST que list_open_contas usa no modo Yoda. Não abre o Postgres."""
    id_filial = parse_id_filial(settings.get("id_filial"))
    if _uses_servidor_pdv(settings):
        return (
            "POST",
            SERVIDOR_OPEN_CONTAS_PATH,
            {"tipo": tipo_conta_servidor(settings), "idFilial": id_filial},
        )
    return (
        "POST",
        OPEN_CONTAS_PATH,
        {"sessao": _sessao_filial(id_filial), "tipo": TIPO_CONTA_MESA},
    )


def _post_open_contas(
    settings: Dict[str, str],
    http: HttpCaller,
) -> Tuple[str, str, Dict[str, Any], int, bytes]:
    method, path, body = open_contas_request(settings)
    encoded = json.dumps(body).encode("utf-8")
    url = settings["base_url"] + path
    if _uses_servidor_pdv(settings):
        status, payload = _call_plain(http, method, url, encoded)
    else:
        status, payload = _call_yoda(settings, http, method, url, encoded)
    return method, path, body, status, payload


def fetch_open_contas(
    settings: Dict[str, str],
    http: HttpCaller = _http,
) -> List[Dict[str, Any]]:
    _method, _path, _body, status, payload = _post_open_contas(settings, http)
    if status != 200:
        raise _http_error(status, payload)
    resumo = _ler_contas_abertas(_json_body(payload))
    if not resumo["reconhecido"]:
        raise UniplusPermanentError(
            "ERR_YODA_HTTP: JSON de contas abertas não reconhecido"
        )
    return resumo["mesas"]


def _resumo_mapeamento(payload: Any) -> Dict[str, Any]:
    return _ler_contas_abertas(payload)


def fetch_mesa_conta(
    settings: Dict[str, str],
    numeromesa: int,
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    numero = parse_numeromesa(numeromesa)
    if not numero:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: mesa sem número")
    id_filial = parse_id_filial(settings.get("id_filial"))
    if _uses_servidor_pdv(settings):
        encoded = json.dumps(
            {
                "numeroDaConta": int(numero),
                "tipoConta": tipo_conta_servidor(settings),
                "idFilial": id_filial,
            }
        ).encode("utf-8")
        status, payload = _call_plain(
            http, "POST", settings["base_url"] + SERVIDOR_CONTA_PATH, encoded
        )
        if status != 200:
            raise _http_error(status, payload)
    else:
        status, payload = _call_yoda(
            settings, http, "GET", _yoda_conta_url(settings, int(numero)), None
        )
        if status == 404:
            return _closed_mesa(numero)
        if status != 200:
            raise _http_error(status, payload)
    if not payload.strip():
        return _closed_mesa(numero)
    parsed = _json_body(payload)
    interno, envelope = _desembrulhar_entity(parsed)
    if envelope == "response.entity string inválida":
        raise UniplusPermanentError("ERR_YODA_HTTP: response.entity sem JSON")
    data = interno if isinstance(interno, dict) else {}
    return map_mesa_conta(numero, data)


def list_open_contas_yoda(
    db_module,
    http: HttpCaller = _http,
) -> List[Dict[str, Any]]:
    settings = yoda_settings(db_module)
    id_filial = parse_id_filial(settings.get("id_filial"))
    key = (settings.get("http_server") or "yoda", settings["base_url"], id_filial)

    def _load() -> List[Dict[str, Any]]:
        return fetch_open_contas(settings, http)

    return _cached_read(_list_cache, key, _load)


def get_open_mesa_conta_yoda(
    db_module,
    numeromesa: int,
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    settings = yoda_settings(db_module)
    numero = parse_numeromesa(numeromesa)
    if not numero:
        return _closed_mesa(0)
    key = (
        settings.get("http_server") or "yoda",
        settings["base_url"],
        settings.get("id_filial") or "",
        settings.get("codigo_filial") or "",
        int(numero),
    )

    def _load() -> Dict[str, Any]:
        return fetch_mesa_conta(settings, int(numero), http)

    return _cached_read(_conta_cache, key, _load)


def probe_yoda_open_contas(
    settings: Dict[str, str],
    http: HttpCaller = _http,
) -> List[Dict[str, Any]]:
    """Testa a listagem de verdade. O cache não conta como prova na hora de salvar."""
    clear_read_cache()
    return fetch_open_contas(settings, http)


def validate_yoda_token(db_module) -> Tuple[bool, str]:
    """Confere só o oauth/token. Não abre o Postgres."""
    clear_token_cache()
    try:
        fetch_access_token(yoda_settings(db_module), force=True)
    except (UniplusPermanentError, YodaOperationalError) as exc:
        return False, str(exc)
    return True, "token ok"


def _itens_mesa_servidor(
    conteudo: Dict[str, Any],
    *,
    exigir_numero: bool = True,
    taxa_entrega: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """Itens no formato do Servidor PDV. Não chama o Yoda nem o Postgres.

    Delivery pode vir sem número: o card sai de getproximonumerodecontadodia.
    valortotal do delivery pode incluir a taxa de entrega.
    """
    contamesa = conteudo.get("contamesa") or {}
    numero = parse_numeromesa(contamesa.get("numeromesa"))
    if not numero:
        numero = parse_numeromesa(conteudo.get("numeromesa"))
    if exigir_numero and not numero:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: mesa sem número")

    itens_in = conteudo.get("itens") or []
    if not itens_in:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: sem itens")

    itens: List[Dict[str, Any]] = []
    total = Decimal("0.00")
    for raw in itens_in:
        codigo = str(raw.get("codigoproduto") or raw.get("codigoProduto") or "").strip()
        if not codigo or len(codigo) > 20:
            raise UniplusPermanentError(
                "ERR_YODA_PAYLOAD: codigoProduto obrigatório, com no máximo 20 caracteres"
            )
        nome = _clip(raw.get("nomeproduto") or raw.get("nomeProduto") or codigo, 120)
        quantidade = _money(raw.get("quantidade") or 1)
        valor_unitario = _money(raw.get("precounitario") or raw.get("valorUnitario"))
        desconto = _money(
            raw.get("valorDescontoSubtotal") or raw.get("valordesconto") or 0
        )
        if quantidade <= 0 or valor_unitario <= 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: quantidade e valorUnitario devem ser maiores que zero ({codigo})"
            )
        if desconto < 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: desconto negativo no item {codigo}"
            )
        esperado = (quantidade * valor_unitario - desconto).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        if esperado <= 0:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: total do item {codigo} deve ser maior que zero"
            )
        declarado = raw.get("valortotal")
        if declarado is None or declarado == "":
            declarado = raw.get("valorTotal")
        if declarado is not None and declarado != "":
            informado = _money(declarado)
            if informado != esperado:
                raise UniplusPermanentError(
                    f"ERR_YODA_PAYLOAD: total do item {codigo} é {informado}, "
                    f"esperado {esperado}"
                )
        total += esperado
        item = {
            "codigoProduto": codigo,
            "nomeProduto": nome,
            "observacao": _clip(raw.get("observacao"), 255),
            "valorUnitario": float(valor_unitario),
            "precoUnitario": float(valor_unitario),
            "quantidade": float(quantidade),
            "valorTotal": float(esperado),
        }
        id_unidade = _id_inteiro(raw, ("idunidademedida", "idUnidadeMedida"))
        if id_unidade is not None:
            item["idUnidadeMedida"] = id_unidade
        hash_item = _uuid_limpo(raw.get("hash"))
        if hash_item:
            item["hash"] = hash_item
        itens.append(item)

    total = total.quantize(MONEY, rounding=ROUND_HALF_UP)
    declarado_conta = contamesa.get("valortotal")
    if declarado_conta is not None and declarado_conta != "":
        informado_conta = _money(declarado_conta)
        aceito = {total}
        if taxa_entrega is not None:
            aceito.add(
                (total + taxa_entrega).quantize(MONEY, rounding=ROUND_HALF_UP)
            )
        if informado_conta not in aceito:
            raise UniplusPermanentError(
                f"ERR_YODA_PAYLOAD: valorTotalItens é {informado_conta}, esperado {total}"
            )
    return {
        "numero": int(numero) if numero else None,
        "cliente": _clip(contamesa.get("nomecliente") or contamesa.get("nome"), 60),
        "obs": _clip(contamesa.get("obs") or contamesa.get("observacao"), 255),
        "itens": itens,
        "total": total,
    }


def _conta_e_itens(bruto: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Separa o objeto conta da lista de itens, no envelope ou na conta plana."""
    aninhada = bruto.get("conta") if isinstance(bruto.get("conta"), dict) else None
    if aninhada is not None:
        conta = dict(aninhada)
        itens = bruto.get("itens")
        if not isinstance(itens, list):
            itens = conta.get("itens") if isinstance(conta.get("itens"), list) else []
    else:
        itens = bruto.get("itens") if isinstance(bruto.get("itens"), list) else []
        conta = {chave: valor for chave, valor in bruto.items() if chave != "itens"}
    conta.pop("itens", None)
    return conta, [item for item in itens if isinstance(item, dict)]


def _total_dos_itens(itens: List[Dict[str, Any]]) -> Decimal:
    total = Decimal("0.00")
    for item in itens:
        bruto = item.get("valorTotal")
        if bruto is None or bruto == "":
            bruto = item.get("valortotal")
        if bruto is None or bruto == "":
            continue
        total += _money(bruto)
    return total.quantize(MONEY, rounding=ROUND_HALF_UP)


def _id_inteiro(raw: Dict[str, Any], chaves: Tuple[str, ...]) -> Optional[int]:
    for chave in chaves:
        valor = raw.get(chave)
        if valor is None or valor == "":
            continue
        try:
            return int(valor)
        except (TypeError, ValueError):
            continue
    return None


def _hoje_servidor() -> List[int]:
    agora = datetime.now()
    return [agora.year, agora.month, agora.day]


def _mapa_produto_conhecido(itens: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """codigoProduto -> item que o Servidor PDV já devolveu, com idProduto."""
    mapa: Dict[str, Dict[str, Any]] = {}
    for item in itens:
        codigo = str(item.get("codigoProduto") or item.get("codigoproduto") or "").strip()
        if codigo and _id_inteiro(item, ("idProduto", "idproduto")) is not None:
            mapa[codigo] = item
    return mapa


def _id_produto_local(db_module, codigo: str) -> Optional[int]:
    """Id gravado na sync. Não abre Postgres e ignora id do cardápio."""
    getter = getattr(db_module, "get_sync_product_id", None)
    if not callable(getter):
        return None
    return _id_inteiro({"id": getter(codigo)}, ("id",))


def _completar_item_novo(
    item: Dict[str, Any],
    conhecidos: Dict[str, Dict[str, Any]],
    id_local: Optional[int] = None,
) -> Dict[str, Any]:
    """O merge exige id do produto, unidade, data e hash. Sem isso a conta não grava."""
    codigo = str(item.get("codigoProduto") or "")
    conhecido = conhecidos.get(codigo) or {}
    id_produto = id_local
    if id_produto is None:
        id_produto = _id_inteiro(conhecido, ("idProduto", "idproduto"))
    if id_produto is None:
        raise UniplusPermanentError(
            f"ERR_PDV_HTTP: produto {codigo} sem id interno no Servidor PDV"
        )
    id_unidade = _id_inteiro(item, ("idUnidadeMedida", "idunidademedida"))
    if id_unidade is None:
        id_unidade = _id_inteiro(conhecido, ("idUnidadeMedida", "idunidademedida"))
    if id_unidade is None:
        id_unidade = SERVIDOR_ID_UNIDADE_MEDIDA
    saida = dict(item)
    saida["idProduto"] = id_produto
    saida["idUnidadeMedida"] = id_unidade
    saida["unidadeMedida"] = str(
        conhecido.get("unidadeMedida") or item.get("unidadeMedida") or "UN"
    )
    saida["precoUnitario"] = item.get("precoUnitario", item.get("valorUnitario"))
    saida["imprimir"] = True
    saida["data"] = _hoje_servidor()
    saida["hash"] = _uuid_limpo(saida.get("hash")) or str(uuid.uuid4())
    return saida


def _corpo_enviar_conta(
    pedido: Dict[str, Any],
    id_filial: int,
    aberta: Dict[str, Any],
    bruto: Optional[Dict[str, Any]],
    ids_locais: Optional[Dict[str, int]] = None,
    tipo_nome: str = "COMANDA",
) -> Dict[str, Any]:
    """Junta os itens novos aos que a mesa já tem. Conta nova vai só com o pedido."""
    ids_locais = ids_locais or {}
    if aberta.get("open") and isinstance(bruto, dict) and bruto:
        conta, existentes = _conta_e_itens(bruto)
        conhecidos = _mapa_produto_conhecido(existentes)
        novos = [
            _completar_item_novo(
                item, conhecidos, ids_locais.get(str(item.get("codigoProduto") or ""))
            )
            for item in pedido["itens"]
        ]
        itens = existentes + novos
        atualizar = True
        combinados = bruto.get("itensCombinados") if isinstance(bruto.get("itensCombinados"), list) else []
    else:
        conta = {}
        conhecidos = {}
        itens = [
            _completar_item_novo(
                item, conhecidos, ids_locais.get(str(item.get("codigoProduto") or ""))
            )
            for item in pedido["itens"]
        ]
        atualizar = False
        combinados = []
    conta["numeroConta"] = int(pedido["numero"])
    if not conta.get("tipoPedido"):
        conta["tipoPedido"] = tipo_nome
    conta["nomeCliente"] = pedido["cliente"]
    conta["idFilial"] = id_filial
    total_itens = _total_dos_itens(itens)
    conta["valorTotal"] = float(total_itens)
    _aplicar_couvert_valor(conta, pedido.get("couvert_valor"))
    conta["obs"] = pedido["obs"]
    if not conta.get("status"):
        conta["status"] = "ABERTA"
    if not isinstance(conta.get("data"), list):
        conta["data"] = _hoje_servidor()
    if not conta.get("hash"):
        conta["hash"] = pedido.get("hash") or str(uuid.uuid4())
    conta["atualizarComanda"] = atualizar
    return {
        "sessao": {"idFilial": id_filial},
        "contaClienteTOJsonObj": {
            "atualizarComanda": atualizar,
            "itensCombinados": combinados,
            "conta": conta,
            "itens": itens,
        },
    }


def _pessoas_couvert(raw: Any) -> int:
    if raw is None or raw == "":
        return 0
    try:
        pessoas = int(Decimal(str(raw).replace(",", ".")))
    except Exception:
        return 0
    if pessoas < 0:
        return 0
    if pessoas > 99:
        return 99
    return pessoas


def calcular_couvert(unitario: Any, pessoas: Any) -> Decimal:
    """Couvert em reais: valor por pessoa vezes a quantidade de pessoas."""
    if isinstance(unitario, str):
        unitario = unitario.strip().replace(",", ".")
    if unitario is None or unitario == "":
        unit = Decimal("0.00")
    else:
        unit = _money(unitario)
    if unit < 0:
        unit = Decimal("0.00")
    if unit > Decimal("99999.99"):
        unit = Decimal("99999.99")
    return (unit * Decimal(_pessoas_couvert(pessoas))).quantize(MONEY, rounding=ROUND_HALF_UP)


def _aplicar_couvert_valor(conta: Dict[str, Any], bruto: Any) -> None:
    """Grava o couvert em valorCouvertArtistico, separado do valor dos produtos."""
    if bruto is None or bruto == "":
        return
    valor = bruto if isinstance(bruto, Decimal) else _money(bruto)
    if valor < 0:
        valor = Decimal("0.00")
    valor = valor.quantize(MONEY, rounding=ROUND_HALF_UP)
    conta["valorCouvertArtistico"] = float(valor)
    conta["semCouvert"] = valor == 0


def definir_taxa_servico_pdv(
    db_module,
    numeromesa: int,
    valor_unitario: Any,
    pessoas: Any = 1,
    tipopedido: Optional[int] = None,
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    """Atualiza valorCouvertArtistico da conta aberta. Reenvia os itens com o mesmo hash."""
    settings = yoda_settings(db_module)
    if not _uses_servidor_pdv(settings):
        raise UniplusPermanentError(PDV_BRIDGE_SQL_BLOCKED)
    total_couvert = calcular_couvert(valor_unitario, pessoas)
    pessoas_n = _pessoas_couvert(pessoas)
    tipo_conta = tipo_conta_servidor(settings) if tipopedido is None else int(tipopedido)
    if tipo_conta == 0:
        tipo_conta = tipo_conta_servidor(settings)
    _aberta, bruto = _ler_conta_servidor(
        settings, int(numeromesa), http, tipo_conta=tipo_conta
    )
    if not isinstance(bruto, dict) or not bruto:
        raise UniplusPermanentError("ERR_PDV_HTTP: mesa sem conta aberta")
    conta, itens = _conta_e_itens(bruto)
    if not itens:
        raise UniplusPermanentError("ERR_PDV_HTTP: mesa sem itens para lançar a taxa")
    id_filial = parse_id_filial(settings.get("id_filial"))
    total_itens = _total_dos_itens(itens)
    conta["numeroConta"] = int(numeromesa)
    conta["idFilial"] = id_filial
    conta["valorTotal"] = float(total_itens)
    _aplicar_couvert_valor(conta, total_couvert)
    conta["atualizarComanda"] = True
    if not conta.get("status"):
        conta["status"] = "ABERTA"
    combinados = bruto.get("itensCombinados") if isinstance(bruto.get("itensCombinados"), list) else []
    corpo = {
        "sessao": {"idFilial": id_filial},
        "contaClienteTOJsonObj": {
            "atualizarComanda": True,
            "itensCombinados": combinados,
            "conta": conta,
            "itens": itens,
        },
    }
    encoded = json.dumps(corpo).encode("utf-8")
    status, payload = _call_plain(
        http, "POST", settings["base_url"] + SERVIDOR_ENVIAR_CONTA_PATH, encoded
    )
    if status not in (200, 204):
        raise _http_error(status, payload)
    clear_read_cache()
    return {
        "open": True,
        "numeromesa": int(numeromesa),
        "valortotal": float(total_itens),
        "valortaxaservico": float(conta["valorCouvertArtistico"]),
        "valorcouvertartistico": float(conta["valorCouvertArtistico"]),
        "valor": float(total_couvert / Decimal(pessoas_n)) if pessoas_n else 0.0,
        "pessoas": pessoas_n,
    }


def _ler_conta_servidor(
    settings: Dict[str, str],
    numero: int,
    http: HttpCaller,
    tipo_conta: Optional[int] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Lê a conta aberta sem cache, para o merge não usar uma lista velha."""
    id_filial = parse_id_filial(settings.get("id_filial"))
    if tipo_conta is None:
        tipo_conta = tipo_conta_servidor(settings)
    encoded = json.dumps(
        {
            "numeroDaConta": int(numero),
            "tipoConta": int(tipo_conta),
            "idFilial": id_filial,
        }
    ).encode("utf-8")
    status, payload = _call_plain(
        http, "POST", settings["base_url"] + SERVIDOR_CONTA_PATH, encoded
    )
    if status == 404 or not payload.strip():
        return _closed_mesa(numero), None
    if status != 200:
        raise _http_error(status, payload)
    parsed = _json_body(payload)
    interno, envelope = _desembrulhar_entity(parsed)
    if envelope == "response.entity string inválida":
        raise UniplusPermanentError("ERR_YODA_HTTP: response.entity sem JSON")
    data = interno if isinstance(interno, dict) else {}
    if not data:
        return _closed_mesa(numero), None
    return map_mesa_conta(numero, data), data


def _conta_id_resposta(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    fontes = [payload]
    aninhada = payload.get("conta")
    if isinstance(aninhada, dict):
        fontes.append(aninhada)
    for fonte in fontes:
        for chave in ("id", "contaId", "idContaCliente", "idcontamesa"):
            bruto = fonte.get(chave)
            if bruto is None or bruto == "":
                continue
            try:
                return int(bruto)
            except (TypeError, ValueError):
                continue
    return None


def _inteiro_positivo(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value == int(value) and int(value) > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) > 0:
            return int(text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if parsed is value:
            return None
        return _inteiro_positivo(parsed)
    return None


def _numero_da_resposta(payload: bytes) -> Optional[int]:
    """Número devolvido por getproximonumerodecontadodia, nu ou em response.entity."""
    if not payload or not payload.strip():
        return None
    parsed = _json_body(payload)
    candidatos = [parsed]
    if isinstance(parsed, dict):
        response = parsed.get("response")
        if isinstance(response, dict) and "entity" in response:
            candidatos.insert(0, response.get("entity"))
        interno, _envelope = _desembrulhar_entity(parsed)
        candidatos.append(interno)
    for candidato in candidatos:
        numero = _inteiro_positivo(candidato)
        if numero:
            return numero
    return None


def _alocar_numero_delivery(
    settings: Dict[str, str], http: HttpCaller, tipo_pedido: int = 0
) -> int:
    """Próximo card do dia. tipoPedido 0 é delivery, 3 é balcão. O PDV commita a sequência."""
    id_filial = parse_id_filial(settings.get("id_filial"))
    encoded = json.dumps(
        {"sessao": {"idFilial": id_filial}, "tipoPedido": int(tipo_pedido)}
    ).encode("utf-8")
    status, payload = _call_plain(
        http, "POST", settings["base_url"] + SERVIDOR_PROXIMO_NUMERO_PATH, encoded
    )
    if status not in (200, 204):
        raise _http_error(status, payload)
    numero = _numero_da_resposta(payload)
    if not numero:
        rotulo = {0: "delivery", 3: "balcão"}.get(int(tipo_pedido), str(tipo_pedido))
        raise UniplusPermanentError(
            f"ERR_PDV_HTTP: Servidor PDV não devolveu o número do {rotulo}"
        )
    return numero


def _aplicar_campos_delivery(
    conta: Dict[str, Any], pedido: Dict[str, Any], contamesa: Dict[str, Any]
) -> None:
    """Campos que o merge de enviarConta copia para a conta DELIVERY."""
    conta["tipoPedido"] = "DELIVERY"
    conta["telefone"] = _clip(contamesa.get("telefone"), 20)
    conta["documento"] = _clip(contamesa.get("documento"), 18)
    conta["endereco"] = _clip(contamesa.get("endereco"), 60)
    conta["enderecoNumero"] = _clip(contamesa.get("endereconumero"), 12)
    conta["enderecoBairro"] = _clip(contamesa.get("enderecobairro"), 255)
    conta["enderecoComplemento"] = _clip(contamesa.get("enderecocomplemento"), 255)
    conta["enderecoReferencia"] = _clip(contamesa.get("enderecoreferencia"), 255)
    fee = _money(contamesa.get("valorentrega") or 0)
    itens_total = _money(conta.get("valorTotal") or 0)
    declarado = contamesa.get("valortotal")
    if declarado is not None and declarado != "" and _money(declarado) == itens_total:
        conta["valorTotal"] = float(itens_total)
    else:
        conta["valorTotal"] = float(
            (itens_total + fee).quantize(MONEY, rounding=ROUND_HALF_UP)
        )
    conta["valorEntrega"] = float(fee)
    conta["retiradaNoBalcao"] = _flag(contamesa.get("retiradanobalcao"))
    conta["abertaOffline"] = True
    if pedido.get("order_id"):
        conta["orderIdIntegracao"] = pedido["order_id"]
    nome_integracao = _clip(contamesa.get("nomeintegracao"), 64)
    conta["nomeIntegracao"] = nome_integracao or NOME_INTEGRACAO
    for origem, destino in _PAGAMENTO_JSON:
        if origem not in contamesa or contamesa.get(origem) in (None, ""):
            continue
        conta[destino] = float(_money(contamesa.get(origem)))


def _numero_conta_payload(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    fontes = [data]
    aninhada = data.get("conta")
    if isinstance(aninhada, dict):
        fontes.append(aninhada)
    for fonte in fontes:
        for chave in ("numeroConta", "numeromesa", "numero"):
            numero = parse_numeromesa(fonte.get(chave))
            if numero:
                return numero
    return None


def _conta_do_json(parsed: Any) -> Optional[Dict[str, Any]]:
    """Conta no corpo ou em response.entity. Entity vazio não é conta."""
    if not isinstance(parsed, dict):
        return None
    interno, envelope = _desembrulhar_entity(parsed)
    if envelope == "response.entity string inválida":
        return None
    if isinstance(interno, dict) and interno:
        return interno
    if _conta_id_resposta(parsed) or _numero_conta_payload(parsed):
        return parsed
    return None


def _hash_ainda_nao_existe(exc: BaseException) -> bool:
    """buscarPorHash(null) vira getJSONObject("null") e o PDV responde 500 COMR2330."""
    texto = str(exc).lower()
    return "jsonobject text must begin" in texto or "comr2330" in texto


def _ler_conta_por_hash(
    settings: Dict[str, str],
    hash_conta: str,
    http: HttpCaller,
) -> Optional[Dict[str, Any]]:
    """POST /pdvrest/unichef/buscarcontaporhash/read.

    O método lê JSONObject.getString("hashContaCliente"). 400 é corpo inválido.
    Conta que ainda não existe responde 500, porque o resultado null vira o texto "null".
    """
    encoded = json.dumps({"hashContaCliente": hash_conta}).encode("utf-8")
    try:
        status, payload = _call_plain(
            http, "POST", settings["base_url"] + SERVIDOR_CONTA_HASH_PATH, encoded
        )
    except YodaOperationalError as exc:
        if _hash_ainda_nao_existe(exc):
            return None
        raise
    if status != 200 or not payload or not payload.strip():
        return None
    try:
        parsed = _json_body(payload)
    except UniplusPermanentError:
        return None
    return _conta_do_json(parsed)


def _id_conta_aberta(
    settings: Dict[str, str],
    numero: int,
    http: HttpCaller,
    tipo_conta: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    _aberta, bruto = _ler_conta_servidor(
        settings, int(numero), http, tipo_conta=tipo_conta
    )
    if not isinstance(bruto, dict):
        return None, None
    return _conta_id_resposta(bruto), _numero_conta_payload(bruto)


def _resumo_servidor_pdv(
    acao: str,
    conta_id: int,
    numero: int,
    protocol: str,
    cliente: str,
    valortotal: Any,
    itens: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "action": acao,
        "message": acao,
        "conta_id": int(conta_id),
        "numeromesa": int(numero),
        "protocol": str(protocol)[:40],
        "cliente": cliente,
        "valortotal": valortotal,
        "itens_count": len(itens),
        "itens": [
            {
                "codigo": item.get("codigoProduto") or item.get("codigoproduto") or item.get("codigo") or "",
                "nome": item.get("nomeProduto") or item.get("nomeproduto") or item.get("nome") or "",
                "qtd": item.get("quantidade") if item.get("quantidade") is not None else item.get("qtd"),
                "total": item.get("valorTotal") if item.get("valorTotal") is not None else item.get("valortotal") if item.get("valortotal") is not None else item.get("total"),
            }
            for item in itens
        ],
    }


def _tipopedido_inteiro(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _canal_pdv(conteudo: Dict[str, Any]) -> str:
    """delivery, mesa ou balcao. tipopedido 3 é o enum BALCAO."""
    tipo = _order_type(conteudo)
    if tipo in ("balcao", "balcão"):
        return "balcao"
    if tipo == "delivery":
        return "delivery"
    codigo = _tipopedido_inteiro((conteudo.get("contamesa") or {}).get("tipopedido"))
    if codigo == 3:
        return "balcao"
    if tipo == "mesa":
        return "mesa"
    return tipo


def handle_servidor_pdv_mesa_job(
    db_module,
    conteudo: Dict[str, Any],
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    """Mesa, delivery ou balcão em POST /pdvrest/unichef/enviarconta/read. Sem Postgres."""
    conteudo = conteudo or {}
    canal = _canal_pdv(conteudo)
    if conteudo.get("updateExisting") or canal not in ("mesa", "delivery", "balcao"):
        raise UniplusPermanentError(PDV_BRIDGE_SQL_BLOCKED)
    delivery = canal == "delivery"
    balcao = canal == "balcao"
    contamesa = conteudo.get("contamesa") or {}
    taxa = _money(contamesa.get("valorentrega") or 0) if delivery else None
    if taxa is not None and taxa < 0:
        raise UniplusPermanentError("ERR_YODA_PAYLOAD: valorentrega negativo")
    pedido = _itens_mesa_servidor(
        conteudo, exigir_numero=canal == "mesa", taxa_entrega=taxa
    )
    settings = yoda_settings(db_module)
    if not _uses_servidor_pdv(settings):
        raise UniplusPermanentError(PDV_BRIDGE_SQL_BLOCKED)
    acao = {
        "delivery": "servidor_pdv_delivery",
        "balcao": "servidor_pdv_balcao",
    }.get(canal, "servidor_pdv_mesa")
    protocol = str(
        conteudo.get("protocol") or contamesa.get("orderidintegracao") or ""
    ).strip()[:40]
    hash_conta = _uuid_limpo(contamesa.get("hash"))
    if hash_conta:
        pedido["hash"] = hash_conta
        ja_gravada = _ler_conta_por_hash(settings, hash_conta, http)
        conta_existente = _conta_id_resposta(ja_gravada)
        if conta_existente:
            numero_existente = _numero_conta_payload(ja_gravada) or pedido["numero"]
            if not numero_existente:
                raise UniplusPermanentError(
                    "ERR_PDV_HTTP: conta encontrada sem número para o ACK"
                )
            total_existente = None
            if isinstance(ja_gravada, dict):
                total_existente = ja_gravada.get("valorTotal")
                if total_existente is None and isinstance(ja_gravada.get("conta"), dict):
                    total_existente = ja_gravada["conta"].get("valorTotal")
            if total_existente is None:
                total_existente = float(pedido["total"])
            return _resumo_servidor_pdv(
                acao,
                conta_existente,
                int(numero_existente),
                protocol,
                pedido["cliente"],
                total_existente,
                pedido["itens"],
            )
    if (delivery or balcao) and not pedido["numero"]:
        pedido["numero"] = _alocar_numero_delivery(
            settings, http, 0 if delivery else 3
        )
    if delivery:
        pedido["order_id"] = protocol
    if canal == "mesa":
        bruto_couvert = contamesa.get("couvertvalor")
        if bruto_couvert is None:
            bruto_couvert = contamesa.get("couvertValor")
        if bruto_couvert not in (None, ""):
            pedido["couvert_valor"] = bruto_couvert
    id_filial = parse_id_filial(settings.get("id_filial"))
    tipo_leitura = 0 if delivery else 3 if balcao else None
    aberta, bruto = _ler_conta_servidor(
        settings, int(pedido["numero"]), http, tipo_conta=tipo_leitura
    )
    ids_locais: Dict[str, int] = {}
    for item in pedido["itens"]:
        codigo_item = str(item.get("codigoProduto") or "")
        achado = _id_produto_local(db_module, codigo_item)
        if achado is not None:
            ids_locais[codigo_item] = achado
    if delivery:
        tipo_nome = "DELIVERY"
    elif balcao:
        tipo_nome = "BALCAO"
    else:
        tipo_nome = _nome_tipo_conta(settings.get("tipo_conta") or 2)[1]
    corpo = _corpo_enviar_conta(
        pedido, id_filial, aberta, bruto, ids_locais, tipo_nome
    )
    if delivery:
        _aplicar_campos_delivery(
            corpo["contaClienteTOJsonObj"]["conta"], pedido, contamesa
        )
    encoded = json.dumps(corpo).encode("utf-8")
    status, payload = _call_plain(
        http, "POST", settings["base_url"] + SERVIDOR_ENVIAR_CONTA_PATH, encoded
    )
    if status not in (200, 204):
        raise _http_error(status, payload)
    conta_id = None
    if status != 204 and payload.strip():
        parsed = _json_body(payload)
        interno, envelope = _desembrulhar_entity(parsed)
        if envelope == "response.entity string inválida":
            entity = ""
            if isinstance(parsed, dict) and isinstance(parsed.get("response"), dict):
                entity = parsed["response"].get("entity") or ""
            if isinstance(entity, str) and not str(entity).strip():
                interno = {}
            else:
                raise UniplusPermanentError("ERR_YODA_HTTP: response.entity sem JSON")
        fonte = interno if isinstance(interno, dict) else parsed
        if isinstance(fonte, dict) and isinstance(fonte.get("contaClienteTOJsonObj"), dict):
            fonte = fonte["contaClienteTOJsonObj"]
        conta_id = _conta_id_resposta(fonte if isinstance(fonte, dict) else parsed)
    enviado = corpo["contaClienteTOJsonObj"]
    hash_enviado = _uuid_limpo(enviado["conta"].get("hash"))
    if not conta_id and hash_enviado:
        achada = _ler_conta_por_hash(settings, hash_enviado, http)
        conta_id = _conta_id_resposta(achada)
        numero_achado = _numero_conta_payload(achada)
        if numero_achado:
            pedido["numero"] = numero_achado
    if not conta_id:
        conta_id, numero_achado = _id_conta_aberta(
            settings, int(pedido["numero"]), http, tipo_leitura
        )
        if numero_achado:
            pedido["numero"] = numero_achado
    if not conta_id:
        raise UniplusPermanentError(
            "ERR_PDV_HTTP: conta gravada no Servidor PDV sem id para o ACK"
        )
    clear_read_cache()
    return _resumo_servidor_pdv(
        acao,
        conta_id,
        int(pedido["numero"]),
        protocol,
        pedido["cliente"],
        enviado["conta"]["valorTotal"],
        enviado["itens"],
    )


def run_uniplus_launch(
    db_module,
    conteudo: Dict[str, Any],
    sql_handler: Callable[..., Dict[str, Any]],
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    """Mesa, delivery e balcão no Servidor PDV vão por HTTP. A ponte não abre Postgres."""
    if is_yoda_mesa_launch(db_module, conteudo or {}):
        return handle_yoda_mesa_job(db_module, conteudo or {}, http=http)
    if uses_pdv_http_bridge(db_module):
        return handle_servidor_pdv_mesa_job(db_module, conteudo or {}, http=http)
    return sql_handler(db_module, conteudo or {})


def handle_yoda_mesa_job(
    db_module,
    conteudo: Dict[str, Any],
    http: HttpCaller = _http,
) -> Dict[str, Any]:
    conta = build_conta_api(db_module, conteudo)
    settings = yoda_settings(db_module)
    protocol = (
        conteudo.get("protocol")
        or (conteudo.get("contamesa") or {}).get("orderidintegracao")
        or ""
    )
    body = json.dumps(conta).encode("utf-8")

    def _post(token: str) -> Tuple[int, bytes]:
        return http(
            "POST",
            settings["base_url"] + CONTA_PATH,
            body,
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    token = fetch_access_token(settings, http)
    status, payload = _post(token)
    if status == 401:
        clear_token_cache()
        token = fetch_access_token(settings, http, force=True)
        status, payload = _post(token)
    if status >= 500:
        raise YodaOperationalError(
            f"ERR_YODA_HTTP: HTTP {status} {_decode_error(status, payload)}"
        )
    if status not in (200, 204):
        raise UniplusPermanentError(
            f"ERR_YODA_HTTP: HTTP {status} {_decode_error(status, payload)}"
        )

    contamesa = conteudo.get("contamesa") or {}
    return {
        "action": "yoda_mesa",
        "message": "yoda_mesa",
        "conta_id": None,
        "numeromesa": conta["numero"],
        "protocol": str(protocol)[:40],
        "cliente": str(contamesa.get("nomecliente") or "")[:60],
        "valortotal": conta["valorTotalItens"],
        "itens_count": len(conta["itens"]),
        "itens": [
            {
                "codigo": item["codigoProduto"],
                "nome": item["nomeProduto"],
                "qtd": item["quantidade"],
                "total": item["valorTotal"],
            }
            for item in conta["itens"]
        ],
    }


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _recorte(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    if len(text) > _DIAGNOSTIC_BODY_LIMIT:
        return text[:_DIAGNOSTIC_BODY_LIMIT] + "\n… recorte truncado"
    return text


def _pagina_diagnostico(corpo: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
        "<title>Teste listar mesas</title>"
        "<style>body{font-family:sans-serif;max-width:52rem;margin:1.5rem;line-height:1.4}"
        "pre{white-space:pre-wrap;background:#f4f4f4;padding:.8rem}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem}"
        "h1{font-size:1.4rem}</style></head><body>"
        "<h1>Teste de listagem de mesas</h1>"
        + corpo
        + "</body></html>"
    )


def render_test_listar_mesas(
    db_module,
    http: HttpCaller = _http,
    floor_mesas: Optional[List[Dict[str, Any]]] = None,
    balcao_inicio: int = 900,
) -> str:
    """HTML do GET /test_listar_mesas. No modo Yoda não abre o Postgres."""
    modo = launch_mode(db_module)
    if modo != "yoda":
        return _pagina_diagnostico(
            "<p>Modo de lançamento: <strong>postgres</strong>.</p>"
            "<p>Esta página não abre o Postgres. Nesse modo o POS lista as mesas "
            "pelo SQL, e essa consulta fecha o PDV. Para ver o que a API devolve, "
            "use o modo de lançamento Yoda.</p>"
        )
    from uniplus_handler import configured_mesa_tipopedido

    settings = yoda_settings(db_module)
    servidor = settings.get("http_server") or "yoda"
    tipo_cfg = configured_mesa_tipopedido(db_module)
    try:
        method, path, body = open_contas_request(settings)
    except (UniplusPermanentError, ValueError) as exc:
        return _pagina_diagnostico(
            _bloco_pedido(settings, servidor, tipo_cfg, None, None, None)
            + "<h2>A chamada falhou</h2><pre>"
            + _e(exc)
            + "</pre><p>A lista não foi montada. Erro não vira lista vazia. "
            "O /pos/mesas mantém a ocupação local.</p>"
        )
    pedido = _bloco_pedido(settings, servidor, tipo_cfg, method, path, body)
    try:
        method, path, body, status, payload = _post_open_contas(settings, http)
    except (UniplusPermanentError, YodaOperationalError, OSError, ValueError) as exc:
        return _pagina_diagnostico(
            pedido
            + "<h2>A chamada falhou</h2><pre>"
            + _e(exc)
            + "</pre><p>A lista não foi montada. Erro não vira lista vazia. "
            "O /pos/mesas mantém a ocupação local.</p>"
            + _bloco_salao_mantido(floor_mesas)
        )
    pedido = _bloco_pedido(settings, servidor, tipo_cfg, method, path, body)
    if status != 200:
        erro = _http_error(status, payload)
        return _pagina_diagnostico(
            pedido
            + "<h2>A chamada falhou</h2><p>Status HTTP: <strong>"
            + _e(status)
            + "</strong></p><pre>"
            + _e(erro)
            + "</pre><h2>Recorte do corpo</h2><pre>"
            + _e(_recorte(payload))
            + "</pre><p>A lista não foi montada. Erro não vira lista vazia. "
            "O /pos/mesas mantém a ocupação local.</p>"
            + _bloco_salao_mantido(floor_mesas)
        )
    try:
        parsed = _json_body(payload)
    except UniplusPermanentError as exc:
        return _pagina_diagnostico(
            pedido
            + "<h2>A chamada falhou</h2><pre>"
            + _e(exc)
            + "</pre><h2>Recorte do corpo</h2><pre>"
            + _e(_recorte(payload))
            + "</pre><p>A lista não foi montada. Erro não vira lista vazia. "
            "O /pos/mesas mantém a ocupação local.</p>"
            + _bloco_salao_mantido(floor_mesas)
        )
    resumo = _resumo_mapeamento(parsed)
    return _pagina_diagnostico(
        pedido
        + _bloco_resposta(status, payload, resumo, floor_mesas, balcao_inicio)
    )


def _bloco_pedido(settings, servidor, tipo_cfg, method, path, body) -> str:
    if _uses_servidor_pdv(settings):
        tipo_id, tipo_nome = _nome_tipo_conta(settings.get("tipo_conta") or 2)
        tipo_txt = f"inteiro {tipo_id} ({tipo_nome} no Servidor PDV)"
    else:
        tipo_txt = 'string "MESA"'
    linhas = [
        "<h2>Configuração</h2><ul>",
        "<li>Modo de lançamento: yoda</li>",
        "<li>Servidor HTTP: " + _e(servidor) + "</li>",
        "<li>URL base: " + _e(settings.get("base_url") or "") + "</li>",
        "<li>Id da filial: " + _e(settings.get("id_filial") or "") + "</li>",
        "<li>uniplus_mesa_tipopedido configurado: " + _e(tipo_cfg) + ". "
        "A listagem e a conta nova usam esse tipo. O mapa de mesas do PDV "
        "só lista tipopedido 1; o de comandas só lista tipopedido 2.</li>",
        "</ul>",
    ]
    if method is None:
        return "".join(linhas)
    linhas.extend(
        [
            "<h2>Pedido</h2><ul>",
            "<li>Verbo: " + _e(method) + "</li>",
            "<li>Path: " + _e(path) + "</li>",
            "<li>Tipo de conta enviado: " + tipo_txt + "</li>",
            "</ul><h2>Corpo JSON</h2><pre>"
            + _e(json.dumps(body, ensure_ascii=False, indent=2))
            + "</pre>",
        ]
    )
    return "".join(linhas)


def _bloco_salao_mantido(floor_mesas: Optional[List[Dict[str, Any]]]) -> str:
    if floor_mesas is None:
        return ""
    linhas = [
        "<h2>Cruzamento com o salão</h2>",
        "<p>Quantidade que o /pos/mesas marcaria agora: nenhuma alteração. "
        "A ocupação local permanece.</p>",
    ]
    if not floor_mesas:
        linhas.append("<p>Não há mesas cadastradas no salão local.</p>")
        return "".join(linhas)
    linhas.append("<table><tr><th>Número</th><th>Status local mantido</th></tr>")
    for mesa in floor_mesas:
        numero = parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))
        linhas.append(
            "<tr><td>"
            + _e(numero if numero is not None else mesa.get("name"))
            + "</td><td>"
            + _e(mesa.get("status") or "")
            + "</td></tr>"
        )
    linhas.append("</table>")
    return "".join(linhas)


def _bloco_resposta(
    status: int,
    payload: bytes,
    resumo: Dict[str, Any],
    floor_mesas: Optional[List[Dict[str, Any]]] = None,
    balcao_inicio: int = 900,
) -> str:
    vistos = ", ".join(resumo["status"]) if resumo["status"] else "(nenhum)"
    if resumo["filtrar_status"]:
        filtro = (
            "Sim. O array só entrega ABERTO, ABERTA e CONFERENCIA_EMITIDA. "
            "Itens antes do filtro: "
            + str(resumo["antes"])
            + ". Descartados pelo status: "
            + str(resumo["descartados_status"])
            + ". Status que apareceram: "
            + _e(vistos)
            + "."
        )
    else:
        filtro = (
            "Não. No mapa contasAbertas o item presente já é conta aberta. "
            "Itens no JSON: "
            + str(resumo["antes"])
            + ". Status que apareceram: "
            + _e(vistos)
            + "."
        )
    linhas = [
        "<h2>Resposta da API, antes do mapeamento</h2><ul>",
        "<li>Status HTTP: " + _e(status) + "</li>",
        "<li>Tipo do JSON: " + _e(resumo["forma"]) + "</li>",
        "<li>Quantidade de itens: " + _e(resumo["antes"]) + "</li>",
    ]
    if resumo.get("envelope"):
        linhas.append(
            "<li>Envelope desembrulhado: "
            + _e(resumo["envelope"])
            + " antes do mapeamento.</li>"
        )
    linhas.extend(
        [
        "</ul><h2>Recorte do corpo</h2><pre>" + _e(_recorte(payload)) + "</pre>",
        "<h2>Filtro de status</h2><p>" + filtro + "</p>",
        "<h2>O que o mapeamento entregou ao POS</h2>",
        "<p>Quantidade crua: <strong>"
        + _e(resumo["antes"])
        + "</strong>. Quantidade mapeada: <strong>"
        + _e(len(resumo["mesas"]))
        + "</strong>.</p>",
        ]
    )
    if resumo["descartes"]:
        linhas.append("<h2>Itens descartados no mapeamento</h2><ul>")
        for item in resumo["descartes"]:
            linhas.append(
                "<li>Mesa "
                + _e(item.get("mesa"))
                + ": "
                + _e(item.get("motivo"))
                + "</li>"
            )
        linhas.append("</ul>")
    if not resumo["mesas"]:
        linhas.append("<p>Nenhuma mesa depois do mapeamento.</p>")
    else:
        linhas.append("<table><tr><th>Número</th><th>Nome</th><th>Valor</th></tr>")
        for mesa in resumo["mesas"]:
            linhas.append(
                "<tr><td>"
                + _e(mesa.get("numeromesa"))
                + "</td><td>"
                + _e(mesa.get("cliente"))
                + "</td><td>"
                + _e(mesa.get("valortotal"))
                + "</td></tr>"
            )
        linhas.append("</table>")
    linhas.append(_bloco_cruzamento(resumo, floor_mesas, balcao_inicio))
    return "".join(linhas)


def _bloco_cruzamento(
    resumo: Dict[str, Any],
    floor_mesas: Optional[List[Dict[str, Any]]],
    balcao_inicio: int,
) -> str:
    if not resumo.get("reconhecido"):
        texto = (
            "<h2>Cruzamento com o salão</h2>"
            "<p>Quantidade crua: "
            + _e(resumo["antes"])
            + ". Quantidade mapeada: "
            + _e(len(resumo["mesas"]))
            + ". O /pos/mesas não marcaria mesas como livres: "
            "este JSON não é uma lista vazia reconhecida.</p>"
        )
        return texto + _bloco_salao_mantido(floor_mesas).replace(
            "<h2>Cruzamento com o salão</h2>", ""
        )
    if floor_mesas is None:
        return (
            "<h2>Cruzamento com o salão</h2>"
            "<p>Salão local não foi carregado nesta chamada.</p>"
        )
    _saida, _changes, info = projetar_ocupacao_salao(
        floor_mesas, resumo["mesas"], balcao_inicio
    )
    linhas = [
        "<h2>Cruzamento com o salão</h2>",
        "<p>Quantidade crua: <strong>"
        + _e(resumo["antes"])
        + "</strong>. Quantidade mapeada: <strong>"
        + _e(len(resumo["mesas"]))
        + "</strong>. Quantidade que o /pos/mesas marcaria como ocupada: <strong>"
        + _e(info["ocupadas"])
        + "</strong>.</p>",
    ]
    if not resumo["mesas"]:
        linhas.append(
            "<p>Lista reconhecida e vazia. O /pos/mesas marcaria como livres "
            "as mesas do salão que ainda aparecem ocupadas.</p>"
        )
    if info["motivos"]:
        linhas.append("<ul>")
        for motivo in info["motivos"]:
            linhas.append("<li>" + _e(motivo) + "</li>")
        linhas.append("</ul>")
    else:
        linhas.append("<p>Nenhuma mesa do salão seria descartada nem liberada.</p>")
    return "".join(linhas)
