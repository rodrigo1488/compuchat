"""Módulo de banco de dados SQLite para o Print Agent."""
import sqlite3
import json
import os
import time
import unicodedata
from datetime import datetime
from typing import List, Dict, Any, Optional

from error_recovery import (
    DatabaseRecovery,
    retry_with_backoff,
    RetryConfig,
)

DB_FILE = "agent.db"


def _fold(text: str) -> str:
    """Minúsculas sem acento, para busca no estilo ILIKE."""
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.casefold().strip()


def _like_contains(text: str) -> str:
    raw = str(text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{raw}%"
DEFAULT_CONFIG = {
    "printer_ip": "192.168.1.100",
    "printer_port": "9100",
    "printer_type": "raw",
    "paper_width": "32",
    "printer_encoding": "cp850",
    "ws_url": "ws://localhost:4000/ws/print",
    "token": "",
    "device_id": "",
    "restart_service_on_save": "true",
    "uniplus_enabled": "false",
    "uniplus_host": "",
    "uniplus_port": "5432",
    "uniplus_user": "",
    "uniplus_password": "",
    "uniplus_database": "unico",
    "uniplus_connection_string": "",
    "uniplus_produto_table": "produto",
    "uniplus_produto_codigo_column": "codigo",
    "uniplus_produto_id_column": "id",
    "uniplus_contamesa_table": "contamesa",
    "uniplus_contamesaitem_table": "contamesaitem",
    "uniplus_produto_preco_column": "preco",
    "uniplus_produto_nome_column": "nome",
    "uniplus_last_error": "",
    "pos_api_token": "",
    "pos_ask_phone": "false",
    "pos_keyword_validation": "false",
    "pos_catalog_version": "0",
    "pos_catalog_updated_at": "",
    "pos_last_sync_error": "",
    "uniplus_mesa_tipopedido": "2",
    "uniplus_launch_mode": "postgres",
    "uniplus_http_server": "yoda",
    "uniplus_yoda_base_url": "https://127.0.0.1:8443",
    "uniplus_yoda_client_id": "",
    "uniplus_yoda_client_secret": "",
    "uniplus_yoda_codigo_filial": "",
    "uniplus_yoda_id_filial": "",
    "balcao_mesa_inicio": "900",
    "erp_target": "uniplus",
    "pdv_lan_url": "http://127.0.0.1:5050",
    "pdv_lan_email": "",
    "pdv_lan_password": "",
    "pdv_lan_token": "",
    "maisgestao_product_sync_poll": "true",
    "maisgestao_last_error": "",
}
PRINTER_KEYS = ("device_id", "token", "printer_ip", "printer_port", "printer_type", "paper_width", "printer_encoding", "name", "connection_type", "printer_name_local")


def _get_connection():
    """Retorna conexão com o banco."""
    # Validar conexão antes de retornar
    if not DatabaseRecovery.validate_db_connection(DB_FILE):
        # Tentar criar backup e recriar banco se necessário
        print(f"[WARN] Problema detectado no banco de dados. Tentando recuperar...")
        DatabaseRecovery.backup_db(DB_FILE)
    
    conn = sqlite3.connect(DB_FILE, timeout=10.0)  # Timeout aumentado
    conn.row_factory = sqlite3.Row
    # Habilitar WAL mode para melhor concorrência
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Inicializa tabelas do banco de dados."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS print_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                kind TEXT DEFAULT 'print',
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migração leve: colunas novas em bancos antigos
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(print_logs)").fetchall()
        }
        if "kind" not in cols:
            conn.execute("ALTER TABLE print_logs ADD COLUMN kind TEXT DEFAULT 'print'")
        if "detail" not in cols:
            conn.execute("ALTER TABLE print_logs ADD COLUMN detail TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uniplus_sync_products (
                codigo TEXT PRIMARY KEY,
                nome TEXT,
                preco REAL,
                fingerprint TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_synced_at TIMESTAMP,
                last_error TEXT,
                id_produto INTEGER
            )
        """)
        sync_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(uniplus_sync_products)").fetchall()
        }
        if "id_produto" not in sync_cols:
            conn.execute(
                "ALTER TABLE uniplus_sync_products ADD COLUMN id_produto INTEGER"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                pin TEXT,
                profile TEXT,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cliente_local (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                tel TEXT,
                logradouro TEXT,
                num_casa TEXT,
                cpf TEXT,
                obs TEXT,
                cloud_contact_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cliente_local_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cliente_local)").fetchall()
        }
        if "updated_at" not in cliente_local_cols:
            conn.execute(
                "ALTER TABLE cliente_local ADD COLUMN updated_at "
                "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            )
        if "cloud_contact_id" not in cliente_local_cols:
            conn.execute(
                "ALTER TABLE cliente_local ADD COLUMN cloud_contact_id INTEGER"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cliente_local_cloud "
            "ON cliente_local(cloud_contact_id) "
            "WHERE cloud_contact_id IS NOT NULL"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_mesas (
                id INTEGER PRIMARY KEY,
                number TEXT,
                name TEXT,
                type TEXT,
                status TEXT,
                form_id INTEGER,
                contact_name TEXT,
                display_order INTEGER,
                section TEXT,
                contact_phone TEXT,
                ask_phone INTEGER NOT NULL DEFAULT 0,
                keyword_validation INTEGER NOT NULL DEFAULT 0
            )
        """)
        mesa_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pos_mesas)").fetchall()
        }
        if "contact_phone" not in mesa_cols:
            conn.execute("ALTER TABLE pos_mesas ADD COLUMN contact_phone TEXT")
        if "ask_phone" not in mesa_cols:
            conn.execute(
                "ALTER TABLE pos_mesas ADD COLUMN ask_phone INTEGER NOT NULL DEFAULT 0"
            )
        if "keyword_validation" not in mesa_cols:
            conn.execute(
                "ALTER TABLE pos_mesas ADD COLUMN keyword_validation INTEGER NOT NULL DEFAULT 0"
            )
        if "uniplus_seen" not in mesa_cols:
            conn.execute(
                "ALTER TABLE pos_mesas ADD COLUMN uniplus_seen INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                name_fold TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        customer_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pos_customers)").fetchall()
        }
        if "name_fold" not in customer_cols:
            conn.execute("ALTER TABLE pos_customers ADD COLUMN name_fold TEXT")
        for row in conn.execute(
            "SELECT id, name FROM pos_customers WHERE name_fold IS NULL OR name_fold = ''"
        ):
            conn.execute(
                "UPDATE pos_customers SET name_fold = ? WHERE id = ?",
                (_fold(row[1]), row[0]),
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_products (
                id INTEGER PRIMARY KEY,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_groups (
                id INTEGER PRIMARY KEY,
                kind TEXT,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_printers (
                id INTEGER PRIMARY KEY,
                device_id TEXT,
                name TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_images (
                id TEXT PRIMARY KEY,
                url TEXT,
                hash TEXT,
                path TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_orders_queue (
                client_order_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                result_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balcao_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numeromesa INTEGER NOT NULL,
                protocol TEXT,
                customer_name TEXT,
                status TEXT NOT NULL DEFAULT 'reserving',
                conta_id INTEGER,
                total REAL,
                items_json TEXT,
                print_error TEXT,
                uniplus_seen INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                closed_at TEXT
            )
        """)
        conn.commit()

        cursor = conn.execute("SELECT COUNT(*) FROM config")
        if cursor.fetchone()[0] == 0:
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (key, str(value)),
                )
            conn.commit()
        else:
            # Garante chaves novas (ex.: UniPlus) em bancos já existentes
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (key, str(value)),
                )
            conn.commit()
        # 0 é delivery e some da sala. O tipo de mesa usado aqui é 2.
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'uniplus_mesa_tipopedido'"
        ).fetchone()
        if row is not None and str(row[0]).strip() == "0":
            conn.execute(
                "UPDATE config SET value = '2' WHERE key = 'uniplus_mesa_tipopedido'"
            )
            conn.commit()
        # Telefone/palavra-chave no POS ficam sempre desligados neste agent.
        for key in ("pos_ask_phone", "pos_keyword_validation"):
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, 'false') "
                "ON CONFLICT(key) DO UPDATE SET value = 'false'",
                (key,),
            )
        conn.execute(
            "UPDATE pos_mesas SET ask_phone = 0, keyword_validation = 0"
        )
        conn.commit()
    finally:
        conn.close()
    _migrate_uniplus_connection_fields()


def _migrate_uniplus_connection_fields() -> None:
    """Instalações antigas só têm o DSN. Separa os campos sem alterar a string salva."""
    try:
        from uniplus_dsn import ensure_uniplus_fields_from_connection_string

        ensure_uniplus_fields_from_connection_string(get_config, set_config)
    except Exception as exc:
        print(f"[WARN] UniPlus: falha ao ler a conexão já salva ({type(exc).__name__})")


# --- Sync de produtos UniPlus → Compuchat ---


def _id_produto_salvo(valor: Any) -> Optional[int]:
    """Id interno da tabela produto. Não é o id do cardápio nem o código visível."""
    if isinstance(valor, bool) or valor is None or valor == "":
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def list_sync_products(enabled_only: bool = False) -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        sql = (
            "SELECT codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error, "
            "id_produto FROM uniplus_sync_products"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY nome COLLATE NOCASE, codigo"
        rows = conn.execute(sql).fetchall()
        return [
            {
                "codigo": r[0],
                "nome": r[1] or "",
                "preco": float(r[2] or 0),
                "fingerprint": r[3] or "",
                "enabled": bool(r[4]),
                "last_synced_at": r[5],
                "last_error": r[6] or "",
                "id_produto": _id_produto_salvo(r[7]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_sync_product(codigo: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error, "
            "id_produto FROM uniplus_sync_products WHERE codigo = ?",
            (str(codigo).strip(),),
        ).fetchone()
        if not row:
            return None
        return {
            "codigo": row[0],
            "nome": row[1] or "",
            "preco": float(row[2] or 0),
            "fingerprint": row[3] or "",
            "enabled": bool(row[4]),
            "last_synced_at": row[5],
            "last_error": row[6] or "",
            "id_produto": _id_produto_salvo(row[7]),
        }
    finally:
        conn.close()


def get_sync_product_id(codigo: str) -> Optional[int]:
    """Id interno do Uniplus gravado na sync, pelo código visível. Só lê o sqlite."""
    codigo = str(codigo or "").strip()
    if not codigo:
        return None
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id_produto FROM uniplus_sync_products WHERE codigo = ?",
            (codigo,),
        ).fetchone()
        if not row:
            return None
        return _id_produto_salvo(row[0])
    finally:
        conn.close()


def set_sync_product_enabled(
    codigo: str,
    enabled: bool,
    *,
    nome: str = "",
    preco: float = 0.0,
    id_produto: Optional[int] = None,
) -> None:
    codigo = str(codigo).strip()
    if not codigo:
        return
    conn = _get_connection()
    try:
        existing = conn.execute(
            "SELECT codigo FROM uniplus_sync_products WHERE codigo = ?", (codigo,)
        ).fetchone()
        id_salvo = _id_produto_salvo(id_produto)
        if existing:
            if id_salvo is None:
                conn.execute(
                    "UPDATE uniplus_sync_products SET enabled = ?, nome = COALESCE(NULLIF(?, ''), nome), "
                    "preco = CASE WHEN ? >= 0 THEN ? ELSE preco END WHERE codigo = ?",
                    (1 if enabled else 0, nome or "", float(preco), float(preco), codigo),
                )
            else:
                conn.execute(
                    "UPDATE uniplus_sync_products SET enabled = ?, nome = COALESCE(NULLIF(?, ''), nome), "
                    "preco = CASE WHEN ? >= 0 THEN ? ELSE preco END, id_produto = ? WHERE codigo = ?",
                    (
                        1 if enabled else 0,
                        nome or "",
                        float(preco),
                        float(preco),
                        id_salvo,
                        codigo,
                    ),
                )
        else:
            conn.execute(
                "INSERT INTO uniplus_sync_products "
                "(codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error, id_produto) "
                "VALUES (?, ?, ?, '', ?, NULL, '', ?)",
                (codigo, nome or "", float(preco or 0), 1 if enabled else 0, id_salvo),
            )
        conn.commit()
    finally:
        conn.close()


def update_sync_product_state(
    codigo: str,
    *,
    nome: Optional[str] = None,
    preco: Optional[float] = None,
    fingerprint: Optional[str] = None,
    last_error: Optional[str] = None,
    synced: bool = False,
    id_produto: Optional[int] = None,
) -> None:
    codigo = str(codigo).strip()
    if not codigo:
        return
    conn = _get_connection()
    try:
        sets = []
        params: list = []
        if nome is not None:
            sets.append("nome = ?")
            params.append(nome)
        if preco is not None:
            sets.append("preco = ?")
            params.append(float(preco))
        if fingerprint is not None:
            sets.append("fingerprint = ?")
            params.append(fingerprint)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        id_salvo = _id_produto_salvo(id_produto)
        if id_salvo is not None:
            sets.append("id_produto = ?")
            params.append(id_salvo)
        if synced:
            sets.append("last_synced_at = CURRENT_TIMESTAMP")
        if not sets:
            return
        params.append(codigo)
        conn.execute(
            f"UPDATE uniplus_sync_products SET {', '.join(sets)} WHERE codigo = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def get_enabled_sync_codigos() -> List[str]:
    return [p["codigo"] for p in list_sync_products(enabled_only=True)]


def get_config(key: str) -> str:
    """Retorna valor de uma chave de configuração."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        default = DEFAULT_CONFIG.get(key, "")
        return default
    finally:
        conn.close()


def set_config(key: str, value: str) -> None:
    """Define valor de uma chave de configuração."""
    @retry_with_backoff(RetryConfig(
        max_retries=3,
        initial_delay=0.5,
        max_delay=5.0,
        retryable_exceptions=(sqlite3.OperationalError, sqlite3.DatabaseError)
    ))
    def _save_config():
        conn = _get_connection()
        try:
            print(f"[DEBUG] set_config: key={key}, value_length={len(str(value))}")
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()
            print(f"[DEBUG] set_config: valor salvo com sucesso")
        finally:
            conn.close()
    
    try:
        _save_config()
    except Exception as e:
        print(f"[ERROR] set_config: erro ao salvar após múltiplas tentativas - {str(e)}")
        raise


def get_all_config() -> dict:
    """Retorna todas as configurações como dicionário."""
    conn = _get_connection()
    try:
        cursor = conn.execute("SELECT key, value FROM config")
        rows = cursor.fetchall()
        result = dict(DEFAULT_CONFIG)
        for row in rows:
            result[row[0]] = row[1]
        return result
    finally:
        conn.close()


def add_print_log(
    job_id: int,
    status: str,
    message: str = None,
    kind: str = "print",
    detail: Any = None,
) -> None:
    """Adiciona registro de impressão/UniPlus ao log."""
    kind_val = (kind or "print").strip().lower() or "print"
    if isinstance(detail, (dict, list)):
        detail_val = json.dumps(detail, ensure_ascii=False, default=str)
    elif detail is None:
        detail_val = None
    else:
        detail_val = str(detail)

    @retry_with_backoff(RetryConfig(
        max_retries=2,
        initial_delay=0.3,
        max_delay=2.0,
        retryable_exceptions=(sqlite3.OperationalError, sqlite3.DatabaseError)
    ))
    def _save_log():
        conn = _get_connection()
        try:
            conn.execute(
                "INSERT INTO print_logs (job_id, status, message, kind, detail) VALUES (?, ?, ?, ?, ?)",
                (job_id, status, message or "", kind_val, detail_val),
            )
            conn.commit()
        finally:
            conn.close()
    
    try:
        _save_log()
    except Exception as e:
        # Log de erro mas não falhar completamente
        print(f"[ERROR] Falha ao salvar log no banco (job_id={job_id}): {e}")


def get_printers() -> List[Dict[str, Any]]:
    """Retorna lista de impressoras. Se não houver lista salva, retorna uma impressora a partir das chaves legadas."""
    raw = get_config("printers")
    if raw and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [
                    {
                        "device_id": p.get("device_id", ""),
                        "token": p.get("token", ""),
                        "printer_ip": p.get("printer_ip", "192.168.1.100"),
                        "printer_port": int(p.get("printer_port") or 9100),
                        "printer_type": p.get("printer_type") or "raw",
                        "paper_width": str(p.get("paper_width") or "32"),
                        "printer_encoding": p.get("printer_encoding") or "cp850",
                        "name": p.get("name") or "",
                        "connection_type": p.get("connection_type") or "network",
                        "printer_name_local": p.get("printer_name_local") or "",
                        "font_mode": p.get("font_mode") or "auto",
                        "font_mode_dine_in": p.get("font_mode_dine_in") or "auto",
                        "font_mode_takeaway": p.get("font_mode_takeaway") or "auto",
                    }
                    for p in data
                    if isinstance(p, dict)
                ]
        except (json.JSONDecodeError, TypeError):
            pass
    device_id = get_config("device_id")
    token = get_config("token")
    if device_id and token:
        return [
            {
                "device_id": device_id,
                "token": token,
                "printer_ip": get_config("printer_ip") or "192.168.1.100",
                "printer_port": int(get_config("printer_port") or 9100),
                "printer_type": get_config("printer_type") or "raw",
                "paper_width": get_config("paper_width") or "32",
                "printer_encoding": get_config("printer_encoding") or "cp850",
                "name": "",
                "connection_type": "network",
                "printer_name_local": "",
                "font_mode": "auto",
                "font_mode_dine_in": "auto",
                "font_mode_takeaway": "auto",
            }
        ]
    return []


def set_printers(printers: List[Dict[str, Any]]) -> None:
    """Salva lista de impressoras como JSON."""
    from error_recovery import DataValidator
    
    print(f"[DEBUG] set_printers chamado com {len(printers)} impressora(s)")
    
    # Sanitizar configurações antes de salvar
    sanitized_printers = []
    for p in printers:
        sanitized = DataValidator.sanitize_printer_config(p)
        sanitized_printers.append(sanitized)
    
    list_ = [
        {
            "device_id": p.get("device_id", ""),
            "token": p.get("token", ""),
            "printer_ip": p.get("printer_ip", "192.168.1.100"),
            "printer_port": int(p.get("printer_port") or 9100),
            "printer_type": p.get("printer_type") or "raw",
            "paper_width": str(p.get("paper_width") or "32"),
            "printer_encoding": p.get("printer_encoding") or "cp850",
            "name": p.get("name") or "",
            "connection_type": p.get("connection_type") or "network",
            "printer_name_local": p.get("printer_name_local") or "",
            "font_mode": p.get("font_mode") or "auto",
            "font_mode_dine_in": p.get("font_mode_dine_in") or "auto",
            "font_mode_takeaway": p.get("font_mode_takeaway") or "auto",
        }
        for p in sanitized_printers
    ]
    json_data = json.dumps(list_)
    print(f"[DEBUG] JSON a ser salvo: {json_data}")
    set_config("printers", json_data)
    print(f"[DEBUG] Configuração salva no banco de dados")


def _print_log_where(status: str = None, q: str = None, kind: str = None):
    """Filtros compartilhados da listagem e da contagem de logs."""
    sql = ""
    params = []
    if status and str(status).strip() and str(status).strip().lower() != "all":
        sql += " AND lower(status) = ?"
        params.append(str(status).strip().lower())
    if kind and str(kind).strip() and str(kind).strip().lower() != "all":
        sql += " AND lower(ifnull(kind, 'print')) = ?"
        params.append(str(kind).strip().lower())
    if q and str(q).strip():
        sql += (
            " AND (CAST(job_id AS TEXT) LIKE ? OR ifnull(message,'') LIKE ?"
            " OR ifnull(detail,'') LIKE ?)"
        )
        like = f"%{str(q).strip()}%"
        params.extend([like, like, like])
    return sql, params


def count_print_logs(status: str = None, q: str = None, kind: str = None) -> int:
    """Total de logs que passam nos mesmos filtros da listagem."""
    where, params = _print_log_where(status=status, q=q, kind=kind)
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM print_logs WHERE 1=1" + where, params
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def get_print_logs(
    limit: int = 50,
    status: str = None,
    q: str = None,
    kind: str = None,
    offset: int = 0,
) -> list:
    """Retorna registros com filtro opcional de status, texto e tipo (print|uniplus)."""
    conn = _get_connection()
    try:
        where, params = _print_log_where(status=status, q=q, kind=kind)
        sql = (
            "SELECT id, job_id, status, message, created_at, "
            "ifnull(kind, 'print') AS kind, detail "
            "FROM print_logs WHERE 1=1"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.append(max(1, min(int(limit or 50), 500)))
        params.append(max(0, int(offset or 0)))
        cursor = conn.execute(sql, params)
        logs = []
        for row in cursor.fetchall():
            detail_raw = row[6]
            detail_obj = None
            if detail_raw:
                try:
                    detail_obj = json.loads(detail_raw)
                except Exception:
                    detail_obj = {"raw": detail_raw}
            logs.append(
                {
                    "id": row[0],
                    "job_id": row[1],
                    "status": row[2],
                    "message": row[3] or "",
                    "created_at": row[4],
                    "kind": row[5] or "print",
                    "detail": detail_obj,
                }
            )
        return logs
    finally:
        conn.close()


def get_print_log_stats() -> dict:
    """Contadores rápidos para o painel de logs."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN lower(status) = 'done' THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN lower(status) = 'error' THEN 1 ELSE 0 END) AS error,
              SUM(CASE WHEN lower(status) NOT IN ('done','error') THEN 1 ELSE 0 END) AS other
            FROM print_logs
            """
        )
        row = cursor.fetchone()
        return {
            "total": int(row[0] or 0),
            "done": int(row[1] or 0),
            "error": int(row[2] or 0),
            "other": int(row[3] or 0),
        }
    finally:
        conn.close()


def _json_load(raw: Optional[str], default=None):
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def replace_pos_catalog(catalog: Dict[str, Any]) -> None:
    """Substitui o snapshot POS local pelo catálogo da cloud."""
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM pos_users")
        previous_occupied = {}
        try:
            for row in conn.execute(
                "SELECT id, status, contact_name, contact_phone, uniplus_seen FROM pos_mesas"
            ):
                if str(row[1] or "") == "ocupada":
                    previous_occupied[int(row[0])] = row
        except sqlite3.OperationalError:
            previous_occupied = {}
        conn.execute("DELETE FROM pos_mesas")
        conn.execute("DELETE FROM pos_products")
        conn.execute("DELETE FROM pos_groups")
        conn.execute("DELETE FROM pos_printers")
        for u in catalog.get("users") or []:
            conn.execute(
                "INSERT INTO pos_users (id, name, pin, profile, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    int(u.get("id")),
                    str(u.get("name") or ""),
                    str(u.get("pin") or ""),
                    str(u.get("profile") or ""),
                    json.dumps(u, ensure_ascii=False),
                ),
            )
        for m in catalog.get("mesas") or []:
            mesa_id = int(m.get("id"))
            held = previous_occupied.get(mesa_id)
            if held:
                status = "ocupada"
                contact_name = held[2]
                contact_phone = held[3]
                uniplus_seen = int(held[4] or 0)
            else:
                status = str(m.get("status") or "livre")
                contact_name = m.get("contactName")
                contact_phone = m.get("contactPhone")
                uniplus_seen = 0
            conn.execute(
                "INSERT INTO pos_mesas (id, number, name, type, status, form_id, contact_name, contact_phone, display_order, section, ask_phone, keyword_validation, uniplus_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mesa_id,
                    str(m.get("number") or ""),
                    str(m.get("name") or ""),
                    str(m.get("type") or "mesa"),
                    status,
                    m.get("formId"),
                    contact_name,
                    contact_phone,
                    int(m.get("displayOrder") or 0),
                    m.get("section"),
                    # Telefone/palavra-chave desligados no agent (não herdar do catálogo cloud).
                    0,
                    0,
                    uniplus_seen,
                ),
            )
        for p in catalog.get("products") or []:
            conn.execute(
                "INSERT INTO pos_products (id, payload) VALUES (?, ?)",
                (int(p.get("id")), json.dumps(p, ensure_ascii=False)),
            )
        for g in catalog.get("groups") or []:
            conn.execute(
                "INSERT INTO pos_groups (id, kind, payload) VALUES (?, ?, ?)",
                (int(g.get("id")), "addon", json.dumps(g, ensure_ascii=False)),
            )
        for pr in catalog.get("printers") or []:
            conn.execute(
                "INSERT INTO pos_printers (id, device_id, name) VALUES (?, ?, ?)",
                (int(pr.get("id")), str(pr.get("deviceId") or ""), str(pr.get("name") or "")),
            )
        conn.commit()
        set_config("pos_catalog_version", str(catalog.get("catalogVersion") or 0))
        set_config("pos_catalog_updated_at", str(catalog.get("updatedAt") or ""))
        set_config(
            "pos_product_groups",
            json.dumps(catalog.get("productGroups") or [], ensure_ascii=False),
        )
        set_config(
            "pos_grupo_addon",
            json.dumps(catalog.get("grupoAddOn") or [], ensure_ascii=False),
        )
        set_config(
            "pos_print_routes",
            json.dumps(catalog.get("printRoutes") or [], ensure_ascii=False),
        )
    finally:
        conn.close()


def upsert_pos_image(image_id: str, url: str, hash_val: str, path: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pos_images (id, url, hash, path) VALUES (?, ?, ?, ?)",
            (image_id, url, hash_val, path),
        )
        conn.commit()
    finally:
        conn.close()


def get_pos_image(image_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, url, hash, path FROM pos_images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "url": row[1], "hash": row[2], "path": row[3]}
    finally:
        conn.close()


def list_pos_images() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT id, url, hash, path FROM pos_images").fetchall()
        return [{"id": r[0], "url": r[1], "hash": r[2], "path": r[3]} for r in rows]
    finally:
        conn.close()


def list_pos_users() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, pin, profile, payload FROM pos_users ORDER BY name COLLATE NOCASE"
        ).fetchall()
        out = []
        for r in rows:
            payload = _json_load(r[4], {})
            payload.update({"id": r[0], "name": r[1] or "", "pin": r[2] or "", "profile": r[3] or ""})
            out.append(payload)
        return out
    finally:
        conn.close()


def get_pos_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, pin, profile, payload FROM pos_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None
        payload = _json_load(row[4], {})
        payload.update({"id": row[0], "name": row[1] or "", "pin": row[2] or "", "profile": row[3] or ""})
        return payload
    finally:
        conn.close()


def _config_flag(key: str) -> bool:
    return (get_config(key) or "").strip().lower() in ("true", "1", "yes", "on")


def pos_occupation_settings() -> Dict[str, bool]:
    """Interruptores do Print Agent para ocupação no POS.

    Pedido/validação de telefone e palavra-chave ficam desligados no agent:
    estavam gerando falhas no POS. O telefone, se enviado, ainda pode ser
    guardado na base local de clientes.
    """
    return {
        "askPhone": False,
        "keywordValidation": False,
    }


def _pos_mesa_from_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "number": row[1] or "",
        "name": row[2] or "",
        "type": row[3] or "mesa",
        "status": row[4] or "livre",
        "formId": row[5],
        "contactName": row[6],
        "displayOrder": row[7] or 0,
        "section": row[8],
        "contactPhone": row[9],
        # Força desligado no agent (ignora flags do catálogo/config).
        "askPhone": False,
        "keywordValidation": False,
        "uniplusSeen": bool(row[12]) if len(row) > 12 and row[12] is not None else False,
    }


_POS_MESA_COLUMNS = (
    "id, number, name, type, status, form_id, contact_name, display_order, section, "
    "contact_phone, ask_phone, keyword_validation, uniplus_seen"
)


def list_pos_mesas() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            f"SELECT {_POS_MESA_COLUMNS} FROM pos_mesas ORDER BY display_order, number COLLATE NOCASE"
        ).fetchall()
        return [_pos_mesa_from_row(r) for r in rows]
    finally:
        conn.close()


def get_pos_mesa(mesa_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            f"SELECT {_POS_MESA_COLUMNS} FROM pos_mesas WHERE id = ?",
            (int(mesa_id),),
        ).fetchone()
        if not row:
            return None
        return _pos_mesa_from_row(row)
    finally:
        conn.close()


def _mesa_slot_number(mesa: Dict[str, Any]) -> Optional[int]:
    from uniplus_handler import parse_numeromesa

    return parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))


def balcao_mesa_inicio() -> int:
    """Primeira comanda da faixa de balcão. O POS não usa esses números."""
    raw = get_config("balcao_mesa_inicio") or "900"
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 900
    if value < 1:
        value = 900
    return value


def is_balcao_number(number: Optional[int]) -> bool:
    return number is not None and int(number) >= balcao_mesa_inicio()


def list_floor_mesas() -> List[Dict[str, Any]]:
    """Comandas do salão. A faixa de balcão não aparece no POS."""
    start = balcao_mesa_inicio()
    out = []
    for mesa in list_pos_mesas():
        slot = _mesa_slot_number(mesa)
        if slot is not None and slot >= start:
            continue
        out.append(mesa)
    return out


def try_occupy_pos_mesa(
    mesa_id: int,
    *,
    contact_name: str,
    contact_phone: Optional[str] = None,
) -> Dict[str, Any]:
    """Ocupa a comanda só se o slot ainda estiver livre. Duas pessoas não entram juntas."""
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_POS_MESA_COLUMNS} FROM pos_mesas WHERE id = ?",
            (int(mesa_id),),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"error": "not_found"}
        mesa = _pos_mesa_from_row(row)
        if str(mesa.get("status") or "") == "ocupada":
            conn.rollback()
            return {"error": "already_occupied", "mesa": mesa}
        slot = _mesa_slot_number(mesa)
        if is_balcao_number(slot):
            conn.rollback()
            return {"error": "balcao_reserved", "mesa": mesa}
        if slot is not None:
            others = conn.execute(f"SELECT {_POS_MESA_COLUMNS} FROM pos_mesas").fetchall()
            for other_row in others:
                other = _pos_mesa_from_row(other_row)
                if int(other["id"]) == int(mesa_id):
                    continue
                if str(other.get("status") or "") != "ocupada":
                    continue
                if _mesa_slot_number(other) == slot:
                    conn.rollback()
                    return {"error": "already_occupied", "mesa": other}
        conn.execute(
            "UPDATE pos_mesas SET status = ?, contact_name = ?, contact_phone = ? WHERE id = ?",
            ("ocupada", contact_name, contact_phone, int(mesa_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"mesa": get_pos_mesa(mesa_id)}


def apply_pos_mesa_occupancy(changes: List[Dict[str, Any]]) -> None:
    """Grava status vindo da conta aberta ou do fechamento no Uniplus."""
    if not changes:
        return
    conn = _get_connection()
    try:
        for change in changes:
            conn.execute(
                """
                UPDATE pos_mesas
                SET status = ?, contact_name = ?, contact_phone = ?, uniplus_seen = ?
                WHERE id = ?
                """,
                (
                    change["status"],
                    change.get("contactName"),
                    change.get("contactPhone"),
                    1 if change.get("uniplusSeen") else 0,
                    int(change["id"]),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def update_pos_mesa(
    mesa_id: int,
    *,
    status: str,
    contact_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE pos_mesas SET status = ?, contact_name = ?, contact_phone = ? WHERE id = ?",
            (status, contact_name, contact_phone, int(mesa_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_pos_mesa(mesa_id)


def update_pos_mesa_contact_name(
    mesa_id: int, contact_name: str
) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE pos_mesas SET contact_name = ? WHERE id = ?",
            (str(contact_name or "").strip(), int(mesa_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_pos_mesa(mesa_id)


def upsert_pos_customer(name: str, phone: str) -> Dict[str, Any]:
    clean_name = str(name or "").strip() or "Cliente"
    clean_phone = str(phone or "").strip()
    if not clean_phone:
        raise ValueError("telefone_obrigatorio")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO pos_customers (name, phone, name_fold, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
              name = excluded.name,
              name_fold = excluded.name_fold,
              updated_at = CURRENT_TIMESTAMP
            """,
            (clean_name, clean_phone, _fold(clean_name)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, phone FROM pos_customers WHERE phone = ?",
            (clean_phone,),
        ).fetchone()
        return {"id": row[0], "name": row[1], "phone": row[2]}
    finally:
        conn.close()


def _cliente_local_from_row(row) -> Dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "nome": row["nome"] or "",
        "tel": row["tel"] or "",
        "logradouro": row["logradouro"] or "",
        "numCasa": row["num_casa"] or "",
        "cpf": row["cpf"] or "",
        "obs": row["obs"] or "",
        "cloudContactId": row["cloud_contact_id"]
        if "cloud_contact_id" in keys and row["cloud_contact_id"] is not None
        else None,
        "updatedAt": row["updated_at"] if "updated_at" in keys else None,
    }


_CLIENTE_LOCAL_SELECT = (
    "id, nome, tel, logradouro, num_casa, cpf, obs, cloud_contact_id, updated_at"
)


def _normalize_cpf(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits


def get_cliente_local(cliente_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT {_CLIENTE_LOCAL_SELECT}
            FROM cliente_local WHERE id = ?
            """,
            (int(cliente_id),),
        ).fetchone()
        if not row:
            return None
        return _cliente_local_from_row(row)
    finally:
        conn.close()


def list_cliente_local(limit: int = 5000) -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT {_CLIENTE_LOCAL_SELECT}
            FROM cliente_local
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [_cliente_local_from_row(r) for r in rows]
    finally:
        conn.close()


def create_cliente_local(
    *,
    nome: str,
    tel: Optional[str] = None,
    logradouro: Optional[str] = None,
    num_casa: Optional[str] = None,
    cpf: Optional[str] = None,
    obs: Optional[str] = None,
    cloud_contact_id: Optional[int] = None,
) -> Dict[str, Any]:
    clean_nome = str(nome or "").strip()
    if not clean_nome:
        raise ValueError("nome_obrigatorio")
    clean_tel = str(tel or "").strip() or None
    clean_logradouro = str(logradouro or "").strip() or None
    clean_num = str(num_casa or "").strip() or None
    clean_cpf = _normalize_cpf(cpf) or None
    clean_obs = str(obs or "").strip() or None
    cloud_id = int(cloud_contact_id) if cloud_contact_id else None
    conn = _get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO cliente_local
                (nome, tel, logradouro, num_casa, cpf, obs, cloud_contact_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                clean_nome,
                clean_tel,
                clean_logradouro,
                clean_num,
                clean_cpf,
                clean_obs,
                cloud_id,
            ),
        )
        conn.commit()
        return get_cliente_local(int(cur.lastrowid)) or {
            "id": int(cur.lastrowid),
            "nome": clean_nome,
            "tel": clean_tel or "",
            "logradouro": clean_logradouro or "",
            "numCasa": clean_num or "",
            "cpf": clean_cpf or "",
            "obs": clean_obs or "",
            "cloudContactId": cloud_id,
            "updatedAt": None,
        }
    finally:
        conn.close()


def update_cliente_local(
    cliente_id: int,
    *,
    nome: Optional[str] = None,
    tel: Optional[str] = None,
    logradouro: Optional[str] = None,
    num_casa: Optional[str] = None,
    cpf: Optional[str] = None,
    obs: Optional[str] = None,
    cloud_contact_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    current = get_cliente_local(cliente_id)
    if not current:
        return None

    clean_nome = (
        str(nome).strip() if nome is not None else current["nome"]
    )
    if not clean_nome:
        raise ValueError("nome_obrigatorio")

    clean_tel = (
        str(tel).strip() if tel is not None else current["tel"]
    ) or None
    clean_logradouro = (
        str(logradouro).strip() if logradouro is not None else current["logradouro"]
    ) or None
    clean_num = (
        str(num_casa).strip() if num_casa is not None else current["numCasa"]
    ) or None
    if cpf is not None:
        clean_cpf = _normalize_cpf(cpf) or None
    else:
        clean_cpf = current["cpf"] or None
    clean_obs = (
        str(obs).strip() if obs is not None else current["obs"]
    ) or None
    if cloud_contact_id is not None:
        cloud_id = int(cloud_contact_id) if cloud_contact_id else None
    else:
        cloud_id = current.get("cloudContactId")

    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE cliente_local
            SET nome = ?, tel = ?, logradouro = ?, num_casa = ?,
                cpf = ?, obs = ?, cloud_contact_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                clean_nome,
                clean_tel,
                clean_logradouro,
                clean_num,
                clean_cpf,
                clean_obs,
                cloud_id,
                int(cliente_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_cliente_local(cliente_id)


def upsert_cliente_local_from_cloud(cliente: Dict[str, Any]) -> Dict[str, Any]:
    """Mescla um contato da nuvem na tabela local (por cloud_contact_id ou tel)."""
    cloud_id = cliente.get("id") or cliente.get("cloudContactId")
    nome = str(cliente.get("nome") or "").strip()
    if not nome:
        raise ValueError("nome_obrigatorio")
    tel = str(cliente.get("tel") or "").strip() or None
    logradouro = str(cliente.get("logradouro") or "").strip() or None
    num_casa = str(
        cliente.get("numCasa") or cliente.get("num_casa") or ""
    ).strip() or None
    cpf = _normalize_cpf(cliente.get("cpf")) or None
    obs = str(cliente.get("obs") or "").strip() or None
    cloud_id_int = int(cloud_id) if cloud_id else None

    conn = _get_connection()
    try:
        row = None
        if cloud_id_int:
            row = conn.execute(
                f"SELECT {_CLIENTE_LOCAL_SELECT} FROM cliente_local WHERE cloud_contact_id = ?",
                (cloud_id_int,),
            ).fetchone()
        if not row and tel:
            row = conn.execute(
                f"SELECT {_CLIENTE_LOCAL_SELECT} FROM cliente_local WHERE tel = ?",
                (tel,),
            ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE cliente_local
                SET nome = ?, tel = COALESCE(?, tel), logradouro = ?,
                    num_casa = ?, cpf = ?, obs = ?,
                    cloud_contact_id = COALESCE(?, cloud_contact_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    nome,
                    tel,
                    logradouro,
                    num_casa,
                    cpf,
                    obs,
                    cloud_id_int,
                    int(row["id"]),
                ),
            )
            conn.commit()
            return get_cliente_local(int(row["id"]))  # type: ignore[return-value]
        cur = conn.execute(
            """
            INSERT INTO cliente_local
                (nome, tel, logradouro, num_casa, cpf, obs, cloud_contact_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (nome, tel, logradouro, num_casa, cpf, obs, cloud_id_int),
        )
        conn.commit()
        return get_cliente_local(int(cur.lastrowid))  # type: ignore[return-value]
    finally:
        conn.close()


def replace_cliente_local_from_cloud(clientes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Atualiza/insere todos os contatos retornados pela nuvem."""
    out: List[Dict[str, Any]] = []
    for cliente in clientes or []:
        try:
            saved = upsert_cliente_local_from_cloud(cliente)
            if saved:
                out.append(saved)
        except ValueError:
            continue
    return out


def _digitos(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _telefones_iguais(a: Any, b: Any) -> bool:
    """Mesmo número, com ou sem o 55 na frente."""
    left = _digitos(a)
    right = _digitos(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if left.startswith("55") and left[2:] == right:
        return True
    return right.startswith("55") and right[2:] == left


def escolher_cliente_local(
    clientes: List[Dict[str, Any]],
    nome: str = "",
    tel: str = "",
) -> Dict[str, Any]:
    """Escolhe o cadastro de quem ocupa a mesa.

    Telefone igual ganha, mesmo que o nome da conta seja mais curto.
    Sem telefone, só um nome exatamente igual (sem acento) entra.
    Vários nomes iguais não escolhem um cadastro.
    A lista já vem da mais recente para a mais antiga.
    """
    por_telefone = [
        cliente
        for cliente in clientes or []
        if _telefones_iguais(cliente.get("tel"), tel)
    ]
    if por_telefone:
        return {"cliente": por_telefone[0], "ambiguo": False}

    folded = _fold(nome)
    if not folded:
        return {"cliente": None, "ambiguo": False}
    por_nome = [
        cliente
        for cliente in clientes or []
        if _fold(cliente.get("nome")) == folded
    ]
    if len(por_nome) == 1:
        return {"cliente": por_nome[0], "ambiguo": False}
    if len(por_nome) > 1:
        return {"cliente": None, "ambiguo": True}
    return {"cliente": None, "ambiguo": False}


def match_cliente_local(nome: str = "", tel: str = "") -> Dict[str, Any]:
    """Cadastro local do ocupante, ou nenhum quando o nome se repete."""
    return escolher_cliente_local(list_cliente_local(limit=10000), nome, tel)


def search_cliente_local(q: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Busca parcial por nome, telefone, CPF ou logradouro."""
    term = str(q or "").strip()
    folded = _fold(term)
    digits = "".join(ch for ch in term if ch.isdigit())
    conn = _get_connection()
    try:
        if not folded and not digits:
            rows = conn.execute(
                f"""
                SELECT {_CLIENTE_LOCAL_SELECT}
                FROM cliente_local
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {_CLIENTE_LOCAL_SELECT}
                FROM cliente_local
                WHERE lower(nome) LIKE ? ESCAPE '\\'
                   OR tel LIKE ? ESCAPE '\\'
                   OR cpf LIKE ? ESCAPE '\\'
                   OR lower(COALESCE(logradouro, '')) LIKE ? ESCAPE '\\'
                   OR (? != '' AND tel LIKE ? ESCAPE '\\')
                   OR (? != '' AND cpf LIKE ? ESCAPE '\\')
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (
                    _like_contains(folded or term.casefold()),
                    _like_contains(term),
                    _like_contains(digits or term),
                    _like_contains(folded or term.casefold()),
                    digits,
                    _like_contains(digits),
                    digits,
                    _like_contains(digits),
                    int(limit),
                ),
            ).fetchall()
        return [_cliente_local_from_row(r) for r in rows]
    finally:
        conn.close()


def search_pos_customers(q: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Busca parcial, sem diferenciar maiúsculas nem acentos, em nome e telefone."""
    term = str(q or "").strip()
    folded = _fold(term)
    digits = "".join(ch for ch in term if ch.isdigit())
    conn = _get_connection()
    try:
        if not folded and not digits:
            rows = conn.execute(
                """
                SELECT id, name, phone FROM pos_customers
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, phone FROM pos_customers
                WHERE name_fold LIKE ? ESCAPE '\\'
                   OR phone LIKE ? ESCAPE '\\'
                   OR (? != '' AND phone LIKE ? ESCAPE '\\')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (
                    _like_contains(folded),
                    _like_contains(term),
                    digits,
                    _like_contains(digits),
                    int(limit),
                ),
            ).fetchall()
        return [{"id": r[0], "name": r[1], "phone": r[2]} for r in rows]
    finally:
        conn.close()


def list_pos_products() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT payload FROM pos_products").fetchall()
        return [_json_load(r[0], {}) for r in rows]
    finally:
        conn.close()


def get_pos_product(product_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT payload FROM pos_products WHERE id = ?", (int(product_id),)
        ).fetchone()
        return _json_load(row[0], {}) if row else None
    finally:
        conn.close()


def list_pos_addon_groups() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT payload FROM pos_groups WHERE kind = 'addon'"
        ).fetchall()
        return [_json_load(r[0], {}) for r in rows]
    finally:
        conn.close()


def list_pos_print_routes() -> List[Dict[str, Any]]:
    rows = _json_load(get_config("pos_print_routes"), [])
    return rows if isinstance(rows, list) else []


def list_pos_printers() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT id, device_id, name FROM pos_printers").fetchall()
        return [{"id": r[0], "deviceId": r[1], "name": r[2]} for r in rows]
    finally:
        conn.close()


def get_pos_order(client_order_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT result_json FROM pos_orders_queue WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return _json_load(row[0], None) if row else None
    finally:
        conn.close()


def claim_pos_order(client_order_id: str) -> Optional[Dict[str, Any]]:
    """Reserva clientOrderId com lock imediato — evita duplicata sob retry paralelo."""
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT result_json FROM pos_orders_queue WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row:
            conn.commit()
            data = _json_load(row[0], None)
            return data if isinstance(data, dict) else None
        conn.execute(
            "INSERT INTO pos_orders_queue (client_order_id, result_json) VALUES (?, ?)",
            (
                client_order_id,
                json.dumps(
                    {"pending": True, "startedAt": time.time()},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        return None
    finally:
        conn.close()


def release_pos_order(client_order_id: str) -> None:
    """Apaga a reserva só enquanto o pedido ainda está pendente."""
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT result_json FROM pos_orders_queue WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row:
            data = _json_load(row[0], {})
            if isinstance(data, dict) and data.get("pending"):
                conn.execute(
                    "DELETE FROM pos_orders_queue WHERE client_order_id = ?",
                    (client_order_id,),
                )
        conn.commit()
    finally:
        conn.close()


def wait_pos_order(client_order_id: str, timeout: float = 35.0) -> Optional[Dict[str, Any]]:
    """Espera o envio em andamento terminar. None = a reserva foi liberada."""
    deadline = time.time() + timeout
    while True:
        current = get_pos_order(client_order_id)
        if not isinstance(current, dict):
            return None
        if not current.get("pending"):
            return current
        if time.time() >= deadline:
            return current
        time.sleep(0.4)


def save_pos_order(client_order_id: str, result: Dict[str, Any]) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pos_orders_queue (client_order_id, result_json) VALUES (?, ?)",
            (client_order_id, json.dumps(result, ensure_ascii=False, default=str)),
        )
        conn.commit()
    finally:
        conn.close()


def build_pos_sync_payload() -> Dict[str, Any]:
    return {
        "catalogVersion": int(get_config("pos_catalog_version") or 0),
        "updatedAt": get_config("pos_catalog_updated_at") or "",
        "users": [
            {"id": u["id"], "name": u["name"], "pin": bool(u.get("pin")), "profile": u.get("profile")}
            for u in list_pos_users()
        ],
        "mesas": list_floor_mesas(),
        "products": list_pos_products(),
        "groups": list_pos_addon_groups(),
        "productGroups": _json_load(get_config("pos_product_groups"), []),
        "grupoAddOn": _json_load(get_config("pos_grupo_addon"), []),
        "printRoutes": _json_load(get_config("pos_print_routes"), []),
        "printers": list_pos_printers(),
        "images": [
            {"id": i["id"], "hash": i["hash"], "url": f"/pos/media/{i['id']}"}
            for i in list_pos_images()
        ],
    }


def _balcao_row(row) -> Dict[str, Any]:
    items = _json_load(row["items_json"], [])
    if not isinstance(items, list):
        items = []
    return {
        "id": int(row["id"]),
        "numeromesa": int(row["numeromesa"]),
        "protocol": row["protocol"] or "",
        "customerName": row["customer_name"] or "",
        "status": row["status"] or "",
        "contaId": row["conta_id"],
        "total": float(row["total"] or 0),
        "items": items,
        "printError": row["print_error"] or "",
        "uniplusSeen": bool(row["uniplus_seen"]),
        "createdAt": row["created_at"] or "",
    }


def list_open_balcao_sales() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, numeromesa, protocol, customer_name, status, conta_id,
                   total, items_json, print_error, uniplus_seen, created_at
            FROM balcao_sales
            WHERE status IN ('open', 'reserving')
            ORDER BY numeromesa
            """
        ).fetchall()
        return [_balcao_row(row) for row in rows]
    finally:
        conn.close()


def refresh_balcao_sales(open_numbers: Optional[set]) -> List[Dict[str, Any]]:
    """Solta a comanda só depois que o Uniplus já mostrou a conta e ela fechou."""
    if open_numbers is None:
        return list_open_balcao_sales()
    open_set = {int(n) for n in open_numbers}
    now = datetime.utcnow().isoformat() + "Z"
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, numeromesa, status, uniplus_seen, created_at
            FROM balcao_sales
            WHERE status IN ('open', 'reserving')
            """
        ).fetchall()
        for row in rows:
            number = int(row["numeromesa"])
            status = row["status"]
            if status == "open" and number in open_set and not row["uniplus_seen"]:
                conn.execute(
                    "UPDATE balcao_sales SET uniplus_seen = 1 WHERE id = ?",
                    (int(row["id"]),),
                )
            elif status == "open" and row["uniplus_seen"] and number not in open_set:
                conn.execute(
                    """
                    UPDATE balcao_sales
                    SET status = 'closed', closed_at = ?
                    WHERE id = ?
                    """,
                    (now, int(row["id"])),
                )
            elif status == "reserving" and number not in open_set:
                created = str(row["created_at"] or "")
                try:
                    started = datetime.fromisoformat(created.replace("Z", ""))
                    age = (datetime.utcnow() - started).total_seconds()
                except ValueError:
                    age = 9999
                if age > 180:
                    conn.execute(
                        "DELETE FROM balcao_sales WHERE id = ? AND status = 'reserving'",
                        (int(row["id"]),),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return list_open_balcao_sales()


def reserve_balcao_number(open_numbers: set) -> Dict[str, Any]:
    """Escolhe a primeira comanda livre da faixa e segura até a venda terminar."""
    start = balcao_mesa_inicio()
    busy = {int(n) for n in open_numbers if int(n) >= start}
    now = datetime.utcnow().isoformat() + "Z"
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT numeromesa FROM balcao_sales
            WHERE status IN ('open', 'reserving')
            """
        ).fetchall()
        for row in rows:
            busy.add(int(row["numeromesa"]))
        number = start
        while number in busy:
            number += 1
            if number > start + 500:
                conn.rollback()
                raise RuntimeError("Não há comanda de balcão livre.")
        cur = conn.execute(
            """
            INSERT INTO balcao_sales (
                numeromesa, protocol, customer_name, status, created_at
            ) VALUES (?, '', '', 'reserving', ?)
            """,
            (number, now),
        )
        conn.commit()
        sale_id = int(cur.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": sale_id, "numeromesa": number}


def complete_balcao_sale(
    sale_id: int,
    *,
    numeromesa: int,
    protocol: str,
    customer_name: str,
    conta_id: Optional[int],
    total: float,
    items: List[Dict[str, Any]],
    print_error: str,
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE balcao_sales
            SET numeromesa = ?, protocol = ?, customer_name = ?, status = 'open',
                conta_id = ?, total = ?, items_json = ?, print_error = ?
            WHERE id = ?
            """,
            (
                int(numeromesa),
                str(protocol or "")[:40],
                str(customer_name or "")[:60],
                int(conta_id) if conta_id else None,
                float(total or 0),
                json.dumps(items, ensure_ascii=False),
                str(print_error or "")[:300],
                int(sale_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def release_balcao_reservation(sale_id: int) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM balcao_sales WHERE id = ? AND status = 'reserving'",
            (int(sale_id),),
        )
        conn.commit()
    finally:
        conn.close()
