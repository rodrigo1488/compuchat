"""API local do POS (tablet) — /pos/*."""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request, send_file

import db
from pos_catalog import media_dir, sync_catalog_from_cloud
from product_sync import _compuchat_request
from uniplus_dsn import current_uniplus_dsn
from uniplus_handler import (
    configured_mesa_tipopedido,
    get_open_mesa_conta,
    handle_uniplus_job,
    is_uniplus_enabled,
    list_open_contas,
    list_pedidos_dia,
    parse_numeromesa,
    set_item_entregue,
    update_open_mesa_cliente_name,
)

pos_bp = Blueprint("pos", __name__)


_MONEY = Decimal("0.01")


def normalize_taxa_servico(raw: Any) -> str:
    """Percentual de 0 a 100, com até 2 casas. Valor inválido vira 0."""
    text = str(raw if raw is not None else "0").strip().replace(",", ".")
    if not text:
        text = "0"
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal("0")
    if value < 0:
        value = Decimal("0")
    if value > 100:
        value = Decimal("100")
    value = value.quantize(_MONEY, rounding=ROUND_HALF_UP)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def taxa_servico_percent() -> Decimal:
    return Decimal(normalize_taxa_servico(db.get_config("pos_taxa_servico")))


def _decimal_nao_negativo(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw if raw is not None else "0").strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal("0")
    if value < 0:
        return Decimal("0")
    return value


def somar_conferencia(
    itens: Any,
    valortotal: Any,
    percent: Decimal,
    desconto_percent: Any = None,
    desconto_valor: Any = None,
    taxa_valor: Any = None,
) -> Dict[str, float]:
    """Subtotal dos itens + taxa configurada − desconto. Sem itens, usa o total da conta.

    O percentual de desconto incide sobre o subtotal. O valor em reais é um
    abatimento direto. O total não fica abaixo de zero.
    """
    subtotal = Decimal("0")
    rows = [item for item in (itens or []) if isinstance(item, dict)]
    for item in rows:
        try:
            subtotal += Decimal(str(item.get("valortotal") or 0))
        except InvalidOperation:
            continue
    if not rows:
        try:
            subtotal = Decimal(str(valortotal or 0))
        except InvalidOperation:
            subtotal = Decimal("0")
    subtotal = subtotal.quantize(_MONEY, rounding=ROUND_HALF_UP)
    informada = _decimal_nao_negativo(taxa_valor)
    if informada > 0:
        taxa = informada.quantize(_MONEY, rounding=ROUND_HALF_UP)
    else:
        taxa = (subtotal * percent / Decimal("100")).quantize(_MONEY, rounding=ROUND_HALF_UP)
    teto = subtotal + taxa
    desconto_pct = Decimal("0")
    valor = _decimal_nao_negativo(desconto_valor)
    if valor > 0:
        desconto = min(valor.quantize(_MONEY, rounding=ROUND_HALF_UP), teto)
    else:
        desconto_pct = _decimal_nao_negativo(desconto_percent)
        if desconto_pct > 100:
            desconto_pct = Decimal("100")
        desconto_pct = desconto_pct.quantize(_MONEY, rounding=ROUND_HALF_UP)
        desconto = (subtotal * desconto_pct / Decimal("100")).quantize(
            _MONEY, rounding=ROUND_HALF_UP
        )
        if desconto > teto:
            desconto = teto
    total = (teto - desconto).quantize(_MONEY, rounding=ROUND_HALF_UP)
    return {
        "subtotal": float(subtotal),
        "taxa": float(taxa),
        "percent": float(percent),
        "desconto": float(desconto),
        "descontoPercent": float(desconto_pct),
        "total": float(total),
    }


def _conferencia_receipt(conta: Dict[str, Any], totais: Dict[str, float]) -> Dict[str, Any]:
    itens = []
    for raw in conta.get("itens") or []:
        if not isinstance(raw, dict):
            continue
        try:
            qty = float(raw.get("quantidade") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            line_total = float(raw.get("valortotal") or 0)
        except (TypeError, ValueError):
            line_total = 0
        if qty <= 0:
            qty = 1
        itens.append(
            {
                "name": raw.get("nomeproduto") or "Item",
                "quantity": qty,
                "productValue": line_total / qty,
                "observation": raw.get("observacao") or "",
                "addons": [],
            }
        )
    cliente = str(conta.get("cliente") or "").strip() or "Consumidor"
    return {
        "form_name": "CONFERENCIA",
        "conference": True,
        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "table_number": str(conta.get("numeromesa") or ""),
        "customer": {"name": cliente, "phone": "", "email": ""},
        "items_by_group": {"Conta": itens},
        "subtotal": totais["subtotal"],
        "service_fee": totais["taxa"],
        "service_fee_percent": totais["percent"],
        "discount": totais.get("desconto") or 0,
        "discount_percent": totais.get("descontoPercent") or 0,
        "delivery_fee": 0,
        "total": totais["total"],
    }


def _printer_cfg(index: Any) -> Optional[Dict[str, Any]]:
    printers = db.get_printers()
    if not printers:
        return None
    try:
        chosen = int(index)
    except (TypeError, ValueError):
        chosen = 0
    if chosen < 0 or chosen >= len(printers):
        return None
    return printers[chosen]


def _print_conferencia(printer_cfg: Dict[str, Any], receipt: Dict[str, Any]) -> bool:
    from printer_service import PrinterService

    paper = printer_cfg.get("paper_width") or "32"
    encoding = printer_cfg.get("printer_encoding") or "cp850"
    if (printer_cfg.get("connection_type") or "network") == "local":
        printer = PrinterService(
            connection_type="local",
            printer_name_local=printer_cfg.get("printer_name_local") or "",
            paper_width=paper,
            printer_encoding=encoding,
        )
    else:
        printer = PrinterService(
            printer_ip=printer_cfg.get("printer_ip") or "",
            printer_port=int(printer_cfg.get("printer_port") or 9100),
            printer_type=printer_cfg.get("printer_type") or "raw",
            paper_width=paper,
            printer_encoding=encoding,
            connection_type="network",
        )
    return bool(printer.print_receipt(receipt))


def _require_pos_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = (db.get_config("pos_api_token") or "").strip()
        if expected:
            got = (
                request.headers.get("X-Pos-Token")
                or request.args.get("token")
                or request.headers.get("Authorization", "").replace("Bearer ", "")
            ).strip()
            if got != expected:
                return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def _find_product(product_id: int) -> Optional[Dict[str, Any]]:
    return db.get_pos_product(int(product_id))


def _option_codigo(product: Dict[str, Any], option_id: Any) -> str:
    if not option_id:
        return str(product.get("idUniplus") or "")
    for variation in product.get("variations") or []:
        for opt in variation.get("options") or []:
            if int(opt.get("id") or 0) == int(option_id):
                return str(opt.get("idUniplus") or product.get("idUniplus") or "")
    return str(product.get("idUniplus") or "")


def _addon_codigo(addon_id: Any) -> str:
    if not addon_id:
        return ""
    for group in db.list_pos_addon_groups():
        for item in group.get("items") or []:
            if int(item.get("id") or 0) == int(addon_id):
                return str(item.get("idUniplus") or "")
        for sg in group.get("subgroups") or []:
            for item in sg.get("items") or []:
                if int(item.get("id") or 0) == int(addon_id):
                    return str(item.get("idUniplus") or "")
    return ""


def _user_observation(item: Dict[str, Any]) -> str:
    raw = item.get("observation") or item.get("observacao") or ""
    return " ".join(str(raw).split())[:180]


def _build_uniplus_items(menu_items: List[Dict[str, Any]], protocol: str) -> List[Dict[str, Any]]:
    itens = []
    for item in menu_items or []:
        qty = float(item.get("quantity") or 1)
        if qty <= 0:
            continue
        product = _find_product(int(item.get("productId") or 0)) if item.get("productId") else None
        nome = str(item.get("productName") or (product or {}).get("name") or "Item")[:120]
        codigo = str(item.get("idUniplus") or "")
        if not codigo and product:
            codigo = _option_codigo(
                product,
                item.get("optionId")
                or item.get("variationOptionId")
                or item.get("baseOptionId"),
            )
        unit = float(item.get("productValue") or (product or {}).get("value") or 0)
        addons = item.get("addons") or []
        addon_obs = []
        embedded = 0.0
        for addon in addons:
            if not isinstance(addon, dict):
                continue
            aval = float(addon.get("value") or 0)
            aqty = max(1.0, float(addon.get("quantity") or 1))
            addon_unit = aval * aqty
            addon_obs.append(addon.get("label") or "Adicional")
            acodigo = str(
                addon.get("idUniplus")
                or _addon_codigo(addon.get("addOnItemId") or addon.get("addonItemId"))
            ).strip()
            if acodigo:
                itens.append(
                    {
                        "codigoproduto": acodigo[:20],
                        "nomeproduto": str(addon.get("label") or "Adicional")[:120],
                        "quantidade": qty,
                        "precounitario": addon_unit,
                        "valortotal": round(addon_unit * qty, 2),
                        "unidademedida": "UN",
                        "observacao": f"Adicional de {nome}"[:255],
                        "orderidintegracao": protocol,
                        "hash": str(uuid.uuid4()),
                    }
                )
            else:
                embedded += addon_unit
        obs_parts = []
        if item.get("type") == "halfAndHalf":
            h1 = item.get("half1Name") or item.get("half1ProductId")
            h2 = item.get("half2Name") or item.get("half2ProductId")
            obs_parts.append(f"Meio a meio: {h1} / {h2}")
        if addon_obs:
            obs_parts.append("Adicionais: " + ", ".join(addon_obs[:8]))
        user_obs = _user_observation(item)
        if user_obs:
            obs_parts.append(user_obs)
        pizza_unit = unit + embedded
        itens.append(
            {
                "codigoproduto": (codigo or "")[:20],
                "nomeproduto": nome,
                "quantidade": qty,
                "precounitario": pizza_unit,
                "valortotal": round(pizza_unit * qty, 2),
                "unidademedida": "UN",
                "observacao": " | ".join(obs_parts)[:255],
                "orderidintegracao": protocol,
                "hash": str(uuid.uuid4()),
            }
        )
    return itens


def _item_grupo(item: Dict[str, Any]) -> str:
    grupo = str(item.get("grupo") or "").strip()
    if grupo:
        return grupo
    product_id = item.get("productId")
    if product_id:
        product = _find_product(int(product_id))
        if product and product.get("grupo"):
            return str(product.get("grupo")).strip()
    return "Outros"


def _print_routes() -> Dict[str, List[str]]:
    rows = db.list_pos_print_routes()
    if not isinstance(rows, list):
        return {}
    by_device: Dict[str, List[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("deviceId") or "").strip().lower()
        if not device_id:
            continue
        names = [
            str(g).strip()
            for g in (row.get("groupNames") or [])
            if str(g or "").strip()
        ]
        if not names:
            continue
        current = by_device.setdefault(device_id, [])
        for name in names:
            if name not in current:
                current.append(name)
    return by_device


def _items_for_printer(items: List[Dict[str, Any]], group_names: List[str]) -> List[Dict[str, Any]]:
    if any(name == "*" for name in group_names):
        return items
    want = {name.lower() for name in group_names}
    return [item for item in items if str(item.get("grupo") or "Outros").strip().lower() in want]


def _grupo_by_codigo() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for product in db.list_pos_products():
        if not isinstance(product, dict):
            continue
        grupo = str(product.get("grupo") or "Outros").strip() or "Outros"
        codigo = str(product.get("idUniplus") or "").strip()
        if codigo:
            out[codigo] = grupo
        for variation in product.get("variations") or []:
            if not isinstance(variation, dict):
                continue
            for opt in variation.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                oc = str(opt.get("idUniplus") or "").strip()
                if oc:
                    out[oc] = grupo
        name = str(product.get("name") or "").strip().lower()
        if name and name not in out:
            out[f"name:{name}"] = grupo
    return out


def _printer_catalog() -> List[Dict[str, Any]]:
    printers = []
    for idx, p in enumerate(db.get_printers() or []):
        device_id = str(p.get("device_id") or "").strip()
        name = str(p.get("name") or "").strip() or device_id or f"Impressora {idx + 1}"
        printers.append({"deviceId": device_id, "name": name})
    return printers


def _assign_printer(grupo: str, printers: List[Dict[str, Any]], routes: Dict[str, List[str]]) -> Dict[str, str]:
    """Só atribui impressora se o grupo estiver na rota (ou se a rota for '*'). Sem fallback."""
    g = (grupo or "Outros").strip().lower()
    if not printers:
        return {"printerDeviceId": "", "printerName": ""}
    if not routes:
        first = printers[0]
        return {"printerDeviceId": first["deviceId"], "printerName": first["name"]}
    for p in printers:
        did = str(p.get("deviceId") or "").strip().lower()
        groups = routes.get(did) or []
        names = {str(x).strip().lower() for x in groups}
        if "*" in names or g in names:
            return {"printerDeviceId": p["deviceId"], "printerName": p["name"]}
    return {"printerDeviceId": "", "printerName": ""}


def _send_receipt(printer_cfg: Dict[str, Any], payload: Dict[str, Any], *, timeout: int = 4) -> bool:
    from printer_service import PrinterService, apply_printer_font_mode
    from receipt_formatter import format_order_receipt

    printer = PrinterService(
        printer_ip=printer_cfg.get("printer_ip"),
        printer_port=int(printer_cfg.get("printer_port") or 9100),
        printer_type=printer_cfg.get("printer_type") or "raw",
        paper_width=int(printer_cfg.get("paper_width") or 32),
        printer_encoding=printer_cfg.get("printer_encoding") or "cp850",
        connection_type=printer_cfg.get("connection_type") or "network",
        printer_name_local=printer_cfg.get("printer_name_local") or None,
        timeout=timeout,
        max_retries=0,
    )
    receipt = apply_printer_font_mode(format_order_receipt(payload), printer_cfg, payload)
    return bool(printer.print_receipt(receipt))


def _cancel_printer_queue(printer_cfg: Dict[str, Any]) -> Dict[str, Any]:
    from printer_service import PrinterService

    name = str(printer_cfg.get("name") or printer_cfg.get("device_id") or "Impressora").strip()
    device_id = str(printer_cfg.get("device_id") or "").strip()
    try:
        ok, message = PrinterService.from_config(printer_cfg, timeout=4, max_retries=0).cancel_queue()
    except Exception as exc:
        ok, message = False, str(exc)
    return {
        "deviceId": device_id,
        "name": name,
        "ok": bool(ok),
        "message": message or ("Fila cancelada" if ok else "Falha ao cancelar fila"),
    }


def _printers_matching(device_id: str = "") -> List[Dict[str, Any]]:
    printers = list(db.get_printers() or [])
    want = str(device_id or "").strip().lower()
    if not want:
        return printers
    return [
        p for p in printers
        if str(p.get("device_id") or "").strip().lower() == want
    ]


def _print_kitchen(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Tenta imprimir sem bloquear o pedido. Sempre devolve printed true/false."""
    info = {"printed": False, "attempted": 0, "failed": 0, "error": ""}
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        printers = db.get_printers()
        if not printers:
            info["error"] = "Nenhuma impressora configurada"
            return info
        items = []
        for raw in payload.get("menuItems") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["grupo"] = _item_grupo(item)
            items.append(item)
        if not items:
            info["printed"] = True
            return info

        routes = _print_routes()
        jobs: List[tuple] = []
        if not routes:
            # Sem rotas configuradas: mantém comportamento antigo (todas as impressoras).
            jobs = [(p, items) for p in printers]
        else:
            # Só imprime itens cujo grupo está na rota da impressora.
            # Grupos sem impressora (e sem rota "*") são ignorados — sem fallback.
            for p in printers:
                device_id = str(p.get("device_id") or "").strip().lower()
                groups = routes.get(device_id)
                if not groups:
                    continue
                subset = _items_for_printer(items, groups)
                if not subset:
                    continue
                jobs.append((p, subset))
            skipped = [
                it
                for it in items
                if not any(
                    (str(g).strip() == "*" or str(g).strip().lower() == str(it.get("grupo") or "Outros").strip().lower())
                    for groups in routes.values()
                    for g in groups
                )
            ]
            if skipped:
                nomes = ", ".join(
                    str(it.get("productName") or it.get("name") or "?") for it in skipped[:8]
                )
                print(
                    f"[POS] {len(skipped)} item(ns) sem rota de impressão (grupo fora das impressoras): {nomes}"
                )

        if not jobs:
            # Pedido só com grupos sem impressora: nada a imprimir (não é erro).
            info["printed"] = True
            return info

        def _run_job(printer_cfg: Dict[str, Any], subset: List[Dict[str, Any]]) -> bool:
            job = dict(payload)
            job["menuItems"] = subset
            try:
                return _send_receipt(printer_cfg, job, timeout=4)
            except Exception as exc:
                print(f"[POS] impressora falhou: {exc}")
                return False

        ok = 0
        fail = 0
        errors: List[str] = []
        # Espera o suficiente para o POS receber printed=true/false real (não async).
        wait_timeout = max(12, 4 * len(jobs) + 4)
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            futures = [pool.submit(_run_job, cfg, subset) for cfg, subset in jobs]
            done, pending = wait(futures, timeout=wait_timeout)
            for fut in pending:
                fail += 1
                errors.append("impressora não respondeu")
            for fut in done:
                info["attempted"] += 1
                try:
                    if fut.result():
                        ok += 1
                    else:
                        fail += 1
                except Exception:
                    fail += 1
        info["failed"] = fail
        # Sucesso se ao menos uma impressora concluiu (cupom saiu).
        info["printed"] = ok > 0
        if fail and not ok:
            info["error"] = errors[0] if errors else "Falha ao imprimir. Verifique o papel da impressora."
        elif fail:
            info["error"] = "Pedido impresso, mas uma impressora não concluiu."
        return info
    except Exception as exc:
        print(f"[POS] impressão cozinha falhou: {exc}")
        info["error"] = str(exc)
        return info


@pos_bp.route("/pos/health", methods=["GET"])
@_require_pos_token
def pos_health():
    uniplus_ok = False
    uniplus_msg = "disabled"
    if is_uniplus_enabled(db):
        dsn = current_uniplus_dsn(db)
        uniplus_ok = bool(dsn)
        uniplus_msg = "configured" if dsn else "missing_dsn"
    return jsonify(
        {
            "ok": True,
            "catalogVersion": int(db.get_config("pos_catalog_version") or 0),
            "updatedAt": db.get_config("pos_catalog_updated_at") or "",
            "uniplus": {"ok": uniplus_ok, "status": uniplus_msg},
        }
    )


@pos_bp.route("/pos/sync", methods=["GET"])
@_require_pos_token
def pos_sync():
    refresh = str(request.args.get("refresh") or "").lower() in ("1", "true", "yes")
    if refresh:
        try:
            sync_catalog_from_cloud()
        except Exception as exc:
            db.set_config("pos_last_sync_error", str(exc))
            return jsonify({"error": str(exc), "catalog": db.build_pos_sync_payload()}), 502
    since = request.args.get("since")
    payload = db.build_pos_sync_payload()
    if since and str(payload.get("catalogVersion")) == str(since):
        return jsonify({"unchanged": True, "catalogVersion": payload["catalogVersion"]})
    return jsonify(payload)


@pos_bp.route("/pos/sync", methods=["POST"])
@_require_pos_token
def pos_sync_now():
    try:
        result = sync_catalog_from_cloud()
        return jsonify(result)
    except Exception as exc:
        db.set_config("pos_last_sync_error", str(exc))
        return jsonify({"error": str(exc)}), 502


@pos_bp.route("/pos/media/<image_id>", methods=["GET"])
@_require_pos_token
def pos_media(image_id: str):
    row = db.get_pos_image(image_id)
    path = (row or {}).get("path") or os.path.join(media_dir(), image_id)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "not_found"}), 404
    return send_file(path)


@pos_bp.route("/pos/login", methods=["POST"])
@_require_pos_token
def pos_login():
    body = request.get_json(silent=True) or {}
    user_id = body.get("userId")
    pin = str(body.get("pin") or "").strip()
    user = db.get_pos_user(int(user_id)) if user_id is not None else None
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    expected = str(user.get("pin") or "").strip()
    if expected and expected != pin:
        return jsonify({"error": "invalid_pin"}), 401
    return jsonify({"ok": True, "user": {"id": user["id"], "name": user["name"]}})


def _tipopedido_from_request() -> int:
    """Tipo pedido pelo query do APK, ou a config do agente (padrão 2)."""
    raw = (request.args.get("tipopedido") or "").strip()
    if not raw:
        return configured_mesa_tipopedido(db)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return configured_mesa_tipopedido(db)
    if value == 0:
        return configured_mesa_tipopedido(db)
    return value


def _mesas_com_status_uniplus(tipopedido: Optional[int] = None) -> List[Dict[str, Any]]:
    """Catálogo local + conta aberta no Uniplus (fonte da verdade quando disponível)."""
    mesas = db.list_floor_mesas()
    balcao_inicio = db.balcao_mesa_inicio()
    if not is_uniplus_enabled(db):
        return mesas

    mesa_tipo = configured_mesa_tipopedido(db) if tipopedido is None else int(tipopedido)
    try:
        contas = list_open_contas(db, tipopedido=mesa_tipo)
    except Exception as exc:
        print(f"[POS] status Uniplus indisponível: {exc}")
        # Erro, 404 ou JSON não reconhecido não liberam o salão.
        return mesas

    from yoda_gourmet import projetar_ocupacao_salao

    mesas, changes, _info = projetar_ocupacao_salao(mesas, contas, balcao_inicio)
    db.apply_pos_mesa_occupancy(changes)
    return mesas


@pos_bp.route("/pos/mesas", methods=["GET"])
@_require_pos_token
def pos_mesas():
    return jsonify({"mesas": _mesas_com_status_uniplus(_tipopedido_from_request())})


def _contato_local_da_mesa(numeromesa: int) -> Dict[str, str]:
    """Nome e telefone gravados na comanda do salão, quando existem."""
    for mesa in db.list_floor_mesas():
        slot = parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))
        if slot == int(numeromesa):
            return {
                "nome": str(mesa.get("contactName") or "").strip(),
                "tel": str(mesa.get("contactPhone") or "").strip(),
            }
    return {"nome": "", "tel": ""}


def anexar_cliente_da_mesa(conta: Dict[str, Any], numeromesa: int) -> Dict[str, Any]:
    """Junta o cadastro local de quem está na mesa à conta aberta."""
    if not conta.get("open"):
        return conta
    try:
        local = _contato_local_da_mesa(numeromesa)
        nome = str(conta.get("cliente") or "").strip() or local["nome"]
        if nome and not str(conta.get("cliente") or "").strip():
            conta["cliente"] = nome
        match = db.match_cliente_local(nome, local["tel"])
        cliente = match.get("cliente")
        conta["clienteDados"] = cliente
        conta["clienteAmbiguo"] = bool(match.get("ambiguo"))
        conta["telefone"] = str((cliente or {}).get("tel") or local["tel"] or "")
    except Exception as exc:
        print(f"[POS] cadastro do cliente da mesa {numeromesa} indisponível: {exc}")
        conta["clienteDados"] = None
        conta["clienteAmbiguo"] = False
        conta["telefone"] = ""
    return conta


@pos_bp.route("/pos/conta", methods=["GET"])
@_require_pos_token
def pos_conta():
    numeromesa = parse_numeromesa(request.args.get("numeromesa"))
    if not numeromesa:
        return jsonify({"error": "numeromesa_required"}), 400
    try:
        conta = get_open_mesa_conta(db, numeromesa, tipopedido=_tipopedido_from_request())
    except Exception as exc:
        return jsonify({"error": str(exc), "open": False, "itens": []}), 502
    return jsonify(anexar_cliente_da_mesa(conta, numeromesa))


@pos_bp.route("/pos/conta/conferencia", methods=["POST"])
@_require_pos_token
def pos_conta_conferencia():
    """Imprime a conferência da mesma conta que GET /pos/conta devolve."""
    numeromesa = parse_numeromesa(request.args.get("numeromesa"))
    if not numeromesa:
        body = request.get_json(silent=True) or {}
        numeromesa = parse_numeromesa(body.get("numeromesa"))
    if not numeromesa:
        return jsonify({"error": "numeromesa_required"}), 400
    body = request.get_json(silent=True) or {}
    printer_cfg = _printer_cfg(body.get("printerIndex"))
    if not printer_cfg:
        return jsonify({"error": "Impressora não configurada"}), 400
    if (printer_cfg.get("connection_type") or "network") == "local" and not (
        printer_cfg.get("printer_name_local") or ""
    ).strip():
        return jsonify({"error": "Impressora local sem nome"}), 400
    try:
        conta = get_open_mesa_conta(db, numeromesa, tipopedido=_tipopedido_from_request())
    except Exception as exc:
        return jsonify({"error": str(exc), "open": False}), 502
    if not conta.get("open"):
        return jsonify({"error": "Mesa sem conta aberta", "open": False}), 404
    taxa_uniplus = _decimal_nao_negativo(conta.get("valortaxaservico"))
    percent = Decimal("0") if taxa_uniplus > 0 else taxa_servico_percent()
    tipo = str(body.get("descontoTipo") or "percent").strip().lower()
    if tipo == "valor":
        totais = somar_conferencia(
            conta.get("itens"),
            conta.get("valortotal"),
            percent,
            desconto_valor=body.get("desconto"),
            taxa_valor=taxa_uniplus if taxa_uniplus > 0 else None,
        )
    else:
        totais = somar_conferencia(
            conta.get("itens"),
            conta.get("valortotal"),
            percent,
            desconto_percent=body.get("desconto"),
            taxa_valor=taxa_uniplus if taxa_uniplus > 0 else None,
        )
    receipt = _conferencia_receipt(conta, totais)
    try:
        ok = _print_conferencia(printer_cfg, receipt)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not ok:
        return jsonify({"error": "Falha ao imprimir a conferência"}), 500
    return jsonify({"ok": True, "numeromesa": numeromesa, **totais})


@pos_bp.route("/pos/conta/couvert", methods=["POST"])
@_require_pos_token
def pos_conta_couvert():
    """Grava valorcouvertartistico na conta aberta pelo Servidor PDV."""
    body = request.get_json(silent=True) or {}
    numeromesa = parse_numeromesa(request.args.get("numeromesa") or body.get("numeromesa"))
    if not numeromesa:
        return jsonify({"error": "numeromesa_required"}), 400
    from yoda_gourmet import definir_taxa_servico_pdv

    valor = body.get("valor")
    if valor is None:
        valor = body.get("valorUnitario")
    pessoas = body.get("pessoas")
    if valor in (None, "") and pessoas in (None, ""):
        return jsonify({"error": "valor_required"}), 400
    if pessoas in (None, ""):
        pessoas = 1
    try:
        return jsonify(
            definir_taxa_servico_pdv(
                db,
                numeromesa,
                valor,
                pessoas,
                tipopedido=_tipopedido_from_request(),
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "open": False}), 502


@pos_bp.route("/pos/printers/cancel-queue", methods=["POST"])
@_require_pos_token
def pos_cancel_print_queue():
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("deviceId") or body.get("device_id") or "").strip()
    from agent import mark_print_queue_done

    info = mark_print_queue_done(device_id)
    if device_id and not info.get("drained"):
        return jsonify({"ok": False, "error": "printer_not_found", "results": []}), 404
    if not info.get("ok"):
        return jsonify({"ok": False, "error": info.get("message") or "no_printers", "results": []}), 400
    return jsonify({"ok": True, "message": info.get("message"), "drained": info.get("drained") or 0})


@pos_bp.route("/pos/pedidos", methods=["GET"])
@_require_pos_token
def pos_pedidos():
    try:
        itens = list_pedidos_dia(db, tipopedido=_tipopedido_from_request())
    except Exception as exc:
        return jsonify({"error": str(exc), "itens": [], "printers": []}), 502
    grupos = _grupo_by_codigo()
    printers = _printer_catalog()
    routes = _print_routes()
    enriched = []
    for item in itens:
        codigo = str(item.get("codigoproduto") or "").strip()
        nome = str(item.get("nomeproduto") or "").strip().lower()
        grupo = grupos.get(codigo) or grupos.get(f"name:{nome}") or "Outros"
        assigned = _assign_printer(grupo, printers, routes)
        enriched.append(
            {
                "id": item["id"],
                "numeromesa": item["numeromesa"],
                "cliente": item.get("cliente") or "",
                "nomeproduto": item.get("nomeproduto") or "",
                "quantidade": item.get("quantidade") or 0,
                "observacao": item.get("observacao") or "",
                "hora": item.get("hora") or "",
                "grupo": grupo,
                "entregue": bool(item.get("entregue")),
                "printerDeviceId": assigned["printerDeviceId"],
                "printerName": assigned["printerName"],
            }
        )
    return jsonify({"printers": printers, "itens": enriched})


@pos_bp.route("/pos/pedidos/<int:item_id>/entregue", methods=["POST"])
@_require_pos_token
def pos_pedido_entregue(item_id: int):
    body = request.get_json(silent=True) or {}
    flag = body.get("entregue")
    if flag is None:
        flag = True
    try:
        ok = set_item_entregue(db, item_id, bool(flag))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if not ok:
        return jsonify({"error": "item_not_found"}), 404
    return jsonify({"ok": True, "id": item_id, "entregue": bool(flag)})


def _normalize_phone(raw: Any) -> Optional[str]:
    """Guarda o que foi digitado. Não recusa formato nem consulta o Compuchat."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) in (10, 11):
        digits = "55" + digits
    return digits


def _cloud_error_response(exc: Exception):
    text = str(exc)
    code = 400 if "HTTP 4" in text else 502
    return jsonify({"error": text}), code


@pos_bp.route("/pos/order-settings", methods=["GET"])
@_require_pos_token
def pos_order_settings():
    return jsonify(
        {
            "tipopedido": configured_mesa_tipopedido(db),
            "taxaServico": float(taxa_servico_percent()),
        }
    )


@pos_bp.route("/pos/occupation-settings", methods=["GET"])
@_require_pos_token
def pos_occupation_settings():
    return jsonify(db.pos_occupation_settings())


@pos_bp.route("/pos/customers", methods=["GET"])
@_require_pos_token
def pos_customers():
    """Compat: lista clientes locais (cliente_local) no formato antigo name/phone."""
    q = request.args.get("q") or ""
    try:
        limit = int(request.args.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    clientes = db.search_cliente_local(q, limit=max(1, min(limit, 100)))
    return jsonify({
        "customers": [
            {
                "id": c.get("id"),
                "name": c.get("nome") or "",
                "phone": c.get("tel") or "",
            }
            for c in clientes
        ]
    })


def _cliente_local_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Aceita campos em PT (tabela) e aliases usados pelo POS."""
    return {
        "nome": body.get("nome") if "nome" in body else body.get("name"),
        "tel": body.get("tel")
        if "tel" in body
        else body.get("phone") or body.get("telefone"),
        "logradouro": body.get("logradouro")
        if "logradouro" in body
        else body.get("address") or body.get("rua"),
        "num_casa": body.get("num_casa")
        if "num_casa" in body
        else body.get("numCasa") or body.get("houseNumber"),
        "cpf": body.get("cpf"),
        "obs": body.get("obs")
        if "obs" in body
        else body.get("observation") or body.get("observacao"),
    }


@pos_bp.route("/pos/clientes", methods=["GET"])
@_require_pos_token
def pos_listar_clientes():
    q = request.args.get("q") or ""
    try:
        limit = int(request.args.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    return jsonify({"clientes": db.search_cliente_local(q, limit=max(1, min(limit, 100)))})


@pos_bp.route("/pos/clientes/<int:cliente_id>", methods=["GET"])
@_require_pos_token
def pos_obter_cliente(cliente_id: int):
    cliente = db.get_cliente_local(cliente_id)
    if not cliente:
        return jsonify({"error": "cliente_not_found"}), 404
    return jsonify({"cliente": cliente})


@pos_bp.route("/pos/clientes", methods=["POST"])
@_require_pos_token
def pos_cadastrar_cliente():
    body = request.get_json(silent=True) or {}
    fields = _cliente_local_payload(body)
    phone = _normalize_phone(fields.get("tel"))
    try:
        cliente = db.create_cliente_local(
            nome=str(fields.get("nome") or ""),
            tel=phone or (str(fields.get("tel") or "").strip() or None),
            logradouro=fields.get("logradouro"),
            num_casa=fields.get("num_casa"),
            cpf=fields.get("cpf"),
            obs=fields.get("obs"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if phone:
        try:
            db.upsert_pos_customer(cliente["nome"], phone)
        except ValueError:
            pass
    return jsonify({"cliente": cliente}), 201


@pos_bp.route("/pos/clientes/<int:cliente_id>", methods=["PUT", "PATCH"])
@_require_pos_token
def pos_editar_cliente(cliente_id: int):
    body = request.get_json(silent=True) or {}
    fields = _cliente_local_payload(body)
    kwargs: Dict[str, Any] = {}
    if "nome" in body or "name" in body:
        kwargs["nome"] = fields.get("nome")
    if any(k in body for k in ("tel", "phone", "telefone")):
        phone = _normalize_phone(fields.get("tel"))
        kwargs["tel"] = phone or (str(fields.get("tel") or "").strip() or "")
    if any(k in body for k in ("logradouro", "address", "rua")):
        kwargs["logradouro"] = fields.get("logradouro")
    if any(k in body for k in ("num_casa", "numCasa", "houseNumber")):
        kwargs["num_casa"] = fields.get("num_casa")
    if "cpf" in body:
        kwargs["cpf"] = fields.get("cpf")
    if any(k in body for k in ("obs", "observation", "observacao")):
        kwargs["obs"] = fields.get("obs")
    try:
        cliente = db.update_cliente_local(cliente_id, **kwargs)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not cliente:
        return jsonify({"error": "cliente_not_found"}), 404
    if cliente.get("tel"):
        try:
            db.upsert_pos_customer(cliente["nome"], str(cliente["tel"]))
        except ValueError:
            pass
    return jsonify({"cliente": cliente})


@pos_bp.route("/pos/clientes/sync", methods=["POST"])
@_require_pos_token
def pos_sync_clientes():
    """Envia clientes locais ao Compuchat e puxa a lista atualizada da nuvem."""
    locais = db.list_cliente_local()
    payload = {
        "clientes": [
            {
                "cloudContactId": c.get("cloudContactId"),
                "nome": c.get("nome") or "",
                "tel": c.get("tel") or "",
                "logradouro": c.get("logradouro") or "",
                "numCasa": c.get("numCasa") or "",
                "cpf": c.get("cpf") or "",
                "obs": c.get("obs") or "",
            }
            for c in locais
            if str(c.get("nome") or "").strip()
        ]
    }
    try:
        data = _compuchat_request(
            "POST",
            "/agent/pos/clientes/sync",
            body=payload,
            timeout=60,
        )
    except Exception as exc:
        return _cloud_error_response(exc)

    cloud_clientes = data.get("clientes") if isinstance(data, dict) else None
    if not isinstance(cloud_clientes, list):
        return jsonify({"error": "sync_invalid_response"}), 502

    merged = db.replace_cliente_local_from_cloud(cloud_clientes)
    return jsonify({
        "ok": True,
        "pushed": int((data or {}).get("pushed") or 0),
        "pulled": int((data or {}).get("pulled") or len(cloud_clientes)),
        "localCount": len(merged),
        "clientes": merged[:100],
    })


@pos_bp.route("/pos/mesas/<int:mesa_id>/ocupar", methods=["POST"])
@_require_pos_token
def pos_ocupar(mesa_id: int):
    body = request.get_json(silent=True) or {}
    customer_name = str(body.get("customerName") or "").strip() or "Cliente"
    mesa = db.get_pos_mesa(mesa_id)
    if not mesa:
        return jsonify({"error": "mesa_not_found"}), 404
    phone = _normalize_phone(body.get("phone"))
    if phone:
        db.upsert_pos_customer(customer_name, phone)
    claimed = db.try_occupy_pos_mesa(
        mesa_id, contact_name=customer_name, contact_phone=phone
    )
    if claimed.get("error") == "not_found":
        return jsonify({"error": "mesa_not_found"}), 404
    if claimed.get("error") == "already_occupied":
        return jsonify({
            "error": "ERR_MESA_ALREADY_OCCUPIED",
            "message": "Esta comanda já está ocupada.",
        }), 409
    if claimed.get("error") == "balcao_reserved":
        return jsonify({
            "error": "ERR_MESA_BALCAO",
            "message": "Esta comanda é do balcão.",
        }), 409
    return jsonify({
        "status": "occupied",
        "mesa": claimed.get("mesa"),
        "contactName": customer_name,
        "contactPhone": phone,
    })


@pos_bp.route("/pos/mesas/<int:mesa_id>/confirmar-ocupacao", methods=["POST"])
@_require_pos_token
def pos_confirmar_ocupacao(mesa_id: int):
    body = request.get_json(silent=True) or {}
    keyword = str(body.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "ERR_MESA_KEYWORD_REQUIRED"}), 400
    mesa = db.get_pos_mesa(mesa_id)
    if not mesa:
        return jsonify({"error": "mesa_not_found"}), 404
    phone = _normalize_phone(body.get("phone"))
    customer_name = str(body.get("customerName") or "").strip()
    try:
        data = _compuchat_request(
            "POST",
            f"/agent/pos/mesas/{mesa_id}/confirmar-ocupacao",
            body={"keyword": keyword},
            timeout=30,
        )
    except Exception as exc:
        return _cloud_error_response(exc)

    contact_name = (
        (data.get("contactName") if isinstance(data, dict) else None)
        or customer_name
        or mesa.get("contactName")
        or "Cliente"
    )
    contact_phone = (
        (data.get("contactPhone") if isinstance(data, dict) else None) or phone
    )
    if contact_phone:
        db.upsert_pos_customer(str(contact_name), str(contact_phone))
    updated = db.update_pos_mesa(
        mesa_id,
        status="ocupada",
        contact_name=str(contact_name),
        contact_phone=str(contact_phone) if contact_phone else None,
    )
    return jsonify({"status": "confirmed", "mesa": updated, "contactName": contact_name, "contactPhone": contact_phone})


@pos_bp.route("/pos/mesas/<int:mesa_id>/contato-nome", methods=["PUT"])
@_require_pos_token
def pos_trocar_nome(mesa_id: int):
    body = request.get_json(silent=True) or {}
    customer_name = str(
        body.get("customerName") or body.get("contactName") or ""
    ).strip()
    if not customer_name:
        return jsonify({"error": "customer_name_required"}), 400

    mesa = db.get_pos_mesa(mesa_id)
    numeromesa = None
    updated = None

    if mesa:
        numeromesa = parse_numeromesa(mesa.get("number")) or parse_numeromesa(
            mesa.get("name")
        )
        updated = db.update_pos_mesa_contact_name(mesa_id, customer_name)
        if updated:
            updated["status"] = updated.get("status") or "ocupada"
            phone = str(updated.get("contactPhone") or "").strip()
            if phone:
                db.upsert_pos_customer(customer_name, phone)
    elif mesa_id < 0:
        numeromesa = abs(int(mesa_id))
    else:
        numeromesa = parse_numeromesa(body.get("tableNumber"))

    uniplus_updated = False
    if numeromesa is not None:
        tipopedido = configured_mesa_tipopedido(db)
        try:
            uniplus_updated = update_open_mesa_cliente_name(
                db, int(numeromesa), customer_name, tipopedido=tipopedido
            )
        except Exception as exc:
            print(f"[POS] Falha ao trocar nome Uniplus mesa {numeromesa}: {exc}")

    if not mesa and not uniplus_updated:
        return jsonify({"error": "mesa_not_found"}), 404

    if updated is None and uniplus_updated:
        updated = {
            "id": mesa_id,
            "number": str(numeromesa),
            "name": f"Mesa {numeromesa}",
            "status": "ocupada",
            "contactName": customer_name,
        }

    return jsonify(
        {
            "ok": True,
            "contactName": customer_name,
            "uniplusUpdated": uniplus_updated,
            "mesa": updated,
        }
    )


def _pending_is_stale(existing: Dict[str, Any]) -> bool:
    """Reserva antiga ou sem horário ficou presa depois de falha/timeout."""
    started = existing.get("startedAt")
    if started is None:
        return True
    try:
        return time.time() - float(started) > 45
    except (TypeError, ValueError):
        return True


def _reuse_finished_order(client_order_id: str, body: Dict[str, Any], existing: Dict[str, Any]):
    if existing.get("ok") and not existing.get("printed"):
        print_info = _print_kitchen(_order_print_payload(body, existing))
        existing["printed"] = bool(print_info.get("printed"))
        existing["printError"] = print_info.get("error") or ""
        db.save_pos_order(client_order_id, existing)
    return {"ok": True, "reused": True, **existing}


def run_pos_order(body: Dict[str, Any]):
    """Grava a conta no Uniplus e imprime. Devolve (payload, http_status)."""
    body = body or {}
    client_order_id = str(body.get("clientOrderId") or "").strip() or str(uuid.uuid4())
    owned = False
    for _ in range(3):
        existing = db.claim_pos_order(client_order_id)
        if existing is None:
            owned = True
            break
        if not existing.get("pending"):
            return _reuse_finished_order(client_order_id, body, existing), 200
        if _pending_is_stale(existing):
            db.release_pos_order(client_order_id)
            continue
        finished = db.wait_pos_order(client_order_id, timeout=35)
        if finished is None:
            continue
        if not finished.get("pending"):
            return _reuse_finished_order(client_order_id, body, finished), 200
        if _pending_is_stale(finished):
            db.release_pos_order(client_order_id)
            continue
        return {
            "error": "O pedido ainda está sendo enviado. Aguarde e toque em Enviar de novo.",
            "clientOrderId": client_order_id,
        }, 409

    if not owned:
        return {
            "error": "Não foi possível enviar o pedido agora. Tente de novo.",
            "clientOrderId": client_order_id,
        }, 409

    menu_items = body.get("menuItems") or []
    if not menu_items:
        db.release_pos_order(client_order_id)
        return {"error": "empty_order"}, 400

    mesa = None
    table_id = body.get("tableId")
    if table_id is not None:
        mesa = db.get_pos_mesa(int(table_id))
    table_number = str(
        body.get("tableNumber")
        or (mesa or {}).get("number")
        or (mesa or {}).get("name")
        or ""
    ).strip()
    table_num_int = parse_numeromesa(table_number) or parse_numeromesa(
        (mesa or {}).get("name")
    )
    if table_num_int and db.is_balcao_number(table_num_int) and not body.get("balcao"):
        db.release_pos_order(client_order_id)
        return {
            "error": "Esta comanda é do balcão.",
            "clientOrderId": client_order_id,
        }, 409
    customer_name = str(body.get("customerName") or (mesa or {}).get("contactName") or "Cliente").strip()
    garcom_name = str(body.get("garcomName") or "").strip()
    protocol = str(
        body.get("protocol")
        or f"POS-{table_num_int or table_number or 'M'}-{int(datetime.now().timestamp())}"
    )[:40]

    itens = _build_uniplus_items(menu_items, protocol)
    if not itens:
        db.release_pos_order(client_order_id)
        return {"error": "no_items"}, 400

    total = round(sum(float(it.get("valortotal") or 0) for it in itens), 2)
    now = datetime.now(timezone.utc)
    # 0 é delivery e some da sala. Mesa usa o tipopedido da config (padrão 2).
    tipopedido = configured_mesa_tipopedido(db)
    conteudo = {
        "event": "uniplus.delivery",
        "protocol": protocol,
        "orderType": "mesa",
        "contamesa": {
            "tipopedido": tipopedido,
            "numeromesa": table_num_int,
            "nome": customer_name[:60],
            "nomecliente": customer_name[:60],
            "valortotal": total,
            "valorcombinado": total,
            "valorentrega": 0,
            "valoroutros": total,
            "obs": "",
            "hash": str(uuid.uuid4()),
            "orderidintegracao": protocol,
            "data": now.date().isoformat(),
            "horaabertura": now.isoformat(),
            "horaultimoconsumo": now.isoformat(),
            "horapedidoefetuado": now.isoformat(),
        },
        "itens": itens,
    }
    if body.get("couvertValor") not in (None, ""):
        conteudo["contamesa"]["couvertvalor"] = body.get("couvertValor")

    try:
        uniplus = handle_uniplus_job(db, conteudo)
    except Exception as exc:
        db.release_pos_order(client_order_id)
        return {"error": str(exc), "clientOrderId": client_order_id}, 502

    print_payload = _with_fulfillment(
        {
            "formName": str(body.get("formName") or "Pedido mesa"),
            "protocol": protocol,
            "tableNumber": table_number,
            "garcomName": garcom_name,
            "orderType": "mesa",
            "menuItems": menu_items,
            "responder": {"name": customer_name},
            "submittedAt": now.isoformat(),
        },
        body,
    )
    # Imprime antes de responder: o tablet usa `printed` na UI e interpretava
    # "Impressão em andamento" (async) como "não impresso".
    print_info = _print_kitchen(print_payload)
    result = {
        "ok": True,
        "printed": bool(print_info.get("printed")),
        "printError": print_info.get("error") or "",
        "clientOrderId": client_order_id,
        "protocol": protocol,
        "tableNumber": table_number,
        "garcomName": garcom_name,
        "customerName": customer_name,
        "uniplus": {
            "contaId": uniplus.get("conta_id"),
            "numeromesa": uniplus.get("numeromesa"),
            "action": uniplus.get("action"),
        },
    }
    db.save_pos_order(client_order_id, result)
    return result, 200


@pos_bp.route("/pos/orders", methods=["POST"])
@_require_pos_token
def pos_orders():
    payload, code = run_pos_order(request.get_json(silent=True) or {})
    return jsonify(payload), code


def _fulfillment_mode(body: Dict[str, Any]) -> str:
    raw = str((body or {}).get("fulfillmentMode") or "").strip().lower()
    if raw in ("pickup", "takeaway", "levar", "para_levar", "retirada"):
        return "pickup"
    if (body or {}).get("pickup") is True or (body or {}).get("retirada") is True:
        return "pickup"
    if raw in ("dine_in", "local", "consumo"):
        return "dine_in"
    return ""


def _with_fulfillment(payload: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    mode = _fulfillment_mode(body)
    payload["fulfillmentMode"] = mode
    payload["pickup"] = mode == "pickup"
    payload["retirada"] = mode == "pickup"
    return payload


def _order_print_payload(body: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    return _with_fulfillment(
        {
            "formName": "Pedido mesa",
            "protocol": existing.get("protocol") or "",
            "tableNumber": existing.get("tableNumber") or body.get("tableNumber") or "",
            "garcomName": existing.get("garcomName") or body.get("garcomName") or "",
            "orderType": "mesa",
            "menuItems": body.get("menuItems") or [],
            "responder": {"name": existing.get("customerName") or body.get("customerName") or "Cliente"},
            "submittedAt": datetime.now(timezone.utc).isoformat(),
        },
        body,
    )
