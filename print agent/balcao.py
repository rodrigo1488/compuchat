"""Venda de balcão na interface web do Print Agent."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from flask import Blueprint, jsonify, render_template, request

import db
from pos_api import _build_uniplus_items, _find_product, _option_codigo, run_pos_order
from uniplus_handler import configured_mesa_tipopedido, is_uniplus_enabled, list_open_contas

balcao_bp = Blueprint("balcao", __name__)


def _grupo_addon_map() -> Dict[str, int]:
    raw = db.get_config("pos_grupo_addon") or "[]"
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        rows = []
    out: Dict[str, int] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        grupo = str(row.get("grupo") or "").strip()
        try:
            group_id = int(row.get("addOnGroupId") or 0)
        except (TypeError, ValueError):
            group_id = 0
        if grupo and group_id > 0:
            out[grupo] = group_id
    return out


def _slim_option(option: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": option.get("id"),
        "label": option.get("label") or option.get("name") or "Opção",
        "value": float(option.get("value") or 0),
        "idUniplus": option.get("idUniplus") or "",
    }


def _slim_product(product: Dict[str, Any], grupo_addon: Dict[str, int]) -> Dict[str, Any]:
    variations = []
    for variation in product.get("variations") or []:
        if not isinstance(variation, dict):
            continue
        options = [
            _slim_option(option)
            for option in (variation.get("options") or [])
            if isinstance(option, dict)
        ]
        if not options:
            continue
        variations.append(
            {
                "id": variation.get("id"),
                "name": variation.get("name") or "Opção",
                "options": options,
            }
        )
    addon_id = product.get("addOnGroupId")
    try:
        addon_id = int(addon_id) if addon_id else 0
    except (TypeError, ValueError):
        addon_id = 0
    if addon_id <= 0:
        addon_id = grupo_addon.get(str(product.get("grupo") or "").strip(), 0)
    return {
        "id": product.get("id"),
        "name": product.get("name") or "Produto",
        "value": float(product.get("value") or 0),
        "grupo": product.get("grupo") or "",
        "idUniplus": product.get("idUniplus") or "",
        "variablePrice": bool(product.get("variablePrice")),
        "addOnGroupId": addon_id or None,
        "variations": variations,
    }


def _slim_addon_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "label": item.get("label") or item.get("name") or "Adicional",
        "value": float(item.get("value") or 0),
        "idUniplus": item.get("idUniplus") or "",
    }


def _slim_group(group: Dict[str, Any]) -> Dict[str, Any]:
    items = [
        _slim_addon_item(item)
        for item in (group.get("items") or [])
        if isinstance(item, dict)
    ]
    subgroups = []
    for subgroup in group.get("subgroups") or []:
        if not isinstance(subgroup, dict):
            continue
        sub_items = [
            _slim_addon_item(item)
            for item in (subgroup.get("items") or [])
            if isinstance(item, dict)
        ]
        subgroups.append(
            {
                "id": subgroup.get("id"),
                "name": subgroup.get("name") or "Adicionais",
                "required": bool(subgroup.get("required")),
                "minItems": int(subgroup.get("minItems") or 0),
                "maxItems": subgroup.get("maxItems"),
                "items": sub_items,
            }
        )
    return {
        "id": group.get("id"),
        "name": group.get("name") or "Adicionais",
        "required": bool(group.get("required")),
        "minItems": int(group.get("minItems") or 0),
        "maxItems": group.get("maxItems"),
        "items": items,
        "subgroups": subgroups,
    }


def _open_numbers() -> Set[int]:
    if not is_uniplus_enabled(db):
        raise RuntimeError("UniPlus está desligado na configuração do agente.")
    contas = list_open_contas(db, tipopedido=configured_mesa_tipopedido(db))
    return {int(conta["numeromesa"]) for conta in contas}


def _next_number(open_numbers: Set[int], sales: List[Dict[str, Any]]) -> Optional[int]:
    start = db.balcao_mesa_inicio()
    busy = {int(number) for number in open_numbers if int(number) >= start}
    for sale in sales:
        if sale.get("status") in ("open", "reserving"):
            busy.add(int(sale["numeromesa"]))
    number = start
    while number in busy:
        number += 1
        if number > start + 500:
            return None
    return number


def _missing_codes(menu_items: List[Dict[str, Any]]) -> List[str]:
    missing = []
    for item in menu_items:
        if not isinstance(item, dict):
            continue
        product = _find_product(int(item.get("productId") or 0)) if item.get("productId") else None
        codigo = str(item.get("idUniplus") or "").strip()
        if not codigo and product:
            codigo = _option_codigo(
                product,
                item.get("optionId") or item.get("variationOptionId") or item.get("baseOptionId"),
            )
        if not str(codigo or "").strip():
            missing.append(str(item.get("productName") or (product or {}).get("name") or "Item"))
    return missing


def _item_summary(menu_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary = []
    for item in menu_items:
        if not isinstance(item, dict):
            continue
        try:
            qty = float(item.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        summary.append(
            {
                "name": str(item.get("productName") or "Item")[:120],
                "quantity": qty,
            }
        )
    return summary


@balcao_bp.route("/balcao", methods=["GET"])
def balcao_page():
    return render_template(
        "balcao.html",
        active_nav="balcao",
        inicio=db.balcao_mesa_inicio(),
    )


@balcao_bp.route("/balcao/catalog", methods=["GET"])
def balcao_catalog():
    grupo_addon = _grupo_addon_map()
    products = [
        _slim_product(product, grupo_addon)
        for product in db.list_pos_products()
        if isinstance(product, dict)
    ]
    products.sort(key=lambda product: str(product.get("name") or "").lower())
    groups = [
        _slim_group(group)
        for group in db.list_pos_addon_groups()
        if isinstance(group, dict)
    ]
    return jsonify({"products": products, "groups": groups})


@balcao_bp.route("/balcao/open", methods=["GET"])
def balcao_open():
    error = ""
    numbers: Set[int] = set()
    try:
        numbers = _open_numbers()
        sales = db.refresh_balcao_sales(numbers)
    except Exception as exc:
        error = str(exc)
        sales = db.refresh_balcao_sales(None)
    visible = [sale for sale in sales if sale.get("status") == "open"]
    return jsonify(
        {
            "inicio": db.balcao_mesa_inicio(),
            "next": _next_number(numbers, sales) if not error else None,
            "sales": visible,
            "error": error,
        }
    )


@balcao_bp.route("/balcao/settings", methods=["POST"])
def balcao_settings():
    body = request.get_json(silent=True) or {}
    try:
        value = int(body.get("inicio"))
    except (TypeError, ValueError):
        return jsonify({"error": "Informe o número inicial da faixa de balcão."}), 400
    if value < 1 or value > 99999:
        return jsonify({"error": "Use um número entre 1 e 99999."}), 400
    db.set_config("balcao_mesa_inicio", str(value))
    return jsonify({"inicio": value})


@balcao_bp.route("/balcao/sales", methods=["POST"])
def balcao_sales():
    body = request.get_json(silent=True) or {}
    menu_items = body.get("menuItems") or []
    if not isinstance(menu_items, list) or not menu_items:
        return jsonify({"error": "Adicione itens na venda."}), 400
    missing = _missing_codes(menu_items)
    if missing:
        names = ", ".join(missing[:4])
        return jsonify({"error": f"Sem código Uniplus: {names}."}), 400
    probe = _build_uniplus_items(menu_items, "balcao")
    if not probe:
        return jsonify({"error": "Nenhum item válido na venda."}), 400

    try:
        numbers = _open_numbers()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    db.refresh_balcao_sales(numbers)

    try:
        reserved = db.reserve_balcao_number(numbers)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409

    sale_id = int(reserved["id"])
    number = int(reserved["numeromesa"])
    customer = str(body.get("customerName") or "").strip() or "Balcão"
    protocol = f"BAL-{number}-{int(datetime.now().timestamp())}"[:40]
    order_body = {
        "balcao": True,
        "clientOrderId": str(uuid.uuid4()),
        "tableNumber": str(number),
        "customerName": customer,
        "garcomName": "Balcão",
        "formName": "Venda balcão",
        "protocol": protocol,
        "menuItems": menu_items,
    }
    try:
        result, code = run_pos_order(order_body)
    except Exception as exc:
        db.release_balcao_reservation(sale_id)
        return jsonify({"error": str(exc)}), 502
    if code != 200 or not result.get("ok"):
        db.release_balcao_reservation(sale_id)
        return jsonify(result), code

    uniplus = result.get("uniplus") or {}
    actual = uniplus.get("numeromesa") or number
    try:
        actual = int(actual)
    except (TypeError, ValueError):
        actual = number
    total = round(sum(float(item.get("valortotal") or 0) for item in probe), 2)
    db.complete_balcao_sale(
        sale_id,
        numeromesa=actual,
        protocol=str(result.get("protocol") or protocol),
        customer_name=customer,
        conta_id=uniplus.get("contaId"),
        total=total,
        items=_item_summary(menu_items),
        print_error=str(result.get("printError") or ""),
    )
    return jsonify(
        {
            "ok": True,
            "numeromesa": actual,
            "protocol": result.get("protocol") or protocol,
            "printed": bool(result.get("printed")),
            "printError": result.get("printError") or "",
            "total": total,
            "customerName": customer,
        }
    )
