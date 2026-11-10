from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .state_machine import (
    ORDER_STATUS_LABELS,
    RETURN_STATUS_LABELS,
    validate_order_transition,
    validate_return_transition,
)


DEFAULT_WALLET_BALANCE_CENTS = 50_000
DEFAULT_STORE_ADDRESS = "浙江省杭州市"
DEFAULT_CUSTOMER_ADDRESS = "河南省郑州市"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _money(cents: int) -> float:
    return round(int(cents) / 100, 2)


class CommerceRepositoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DemoCommerceRepository:
    """并发安全的本地 SQLite 演示交易仓储。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS demo_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sandbox_accounts (
                    session_id TEXT PRIMARY KEY REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    initial_balance_cents INTEGER NOT NULL DEFAULT 50000 CHECK(initial_balance_cents >= 0),
                    balance_cents INTEGER NOT NULL DEFAULT 50000 CHECK(balance_cents >= 0),
                    store_address TEXT NOT NULL DEFAULT '浙江省杭州市',
                    customer_address TEXT NOT NULL DEFAULT '河南省郑州市',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sandbox_wallet_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    entry_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    balance_after_cents INTEGER NOT NULL CHECK(balance_after_cents >= 0),
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, entry_type, entity_id)
                );

                CREATE TABLE IF NOT EXISTS cart_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    image_url TEXT,
                    source_url TEXT,
                    source_snapshot_at TEXT,
                    sku_id TEXT NOT NULL DEFAULT '',
                    sku_attributes_json TEXT NOT NULL DEFAULT '{}',
                    specification_pending INTEGER NOT NULL DEFAULT 0,
                    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    quantity INTEGER NOT NULL CHECK(quantity > 0 AND quantity <= 99),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, product_id, sku_id)
                );

                CREATE TABLE IF NOT EXISTS order_previews (
                    token_hash TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    cart_digest TEXT NOT NULL,
                    total_cents INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS demo_orders (
                    order_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
                    item_count INTEGER NOT NULL CHECK(item_count > 0),
                    source_snapshot_at TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS demo_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES demo_orders(order_id) ON DELETE CASCADE,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    image_url TEXT,
                    source_url TEXT,
                    sku_id TEXT,
                    sku_attributes_json TEXT NOT NULL DEFAULT '{}',
                    specification_pending INTEGER NOT NULL DEFAULT 0,
                    unit_price_cents INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    currency TEXT NOT NULL DEFAULT 'CNY'
                );

                CREATE TABLE IF NOT EXISTS demo_logistics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES demo_orders(order_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    description TEXT NOT NULL,
                    event_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS return_previews (
                    token_hash TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    order_id TEXT NOT NULL,
                    order_item_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    reason_code TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS demo_return_requests (
                    return_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    order_id TEXT NOT NULL REFERENCES demo_orders(order_id) ON DELETE CASCADE,
                    order_item_id INTEGER NOT NULL REFERENCES demo_order_items(id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    reason_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cart_session ON cart_items(session_id);
                CREATE INDEX IF NOT EXISTS idx_orders_session ON demo_orders(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_logistics_order ON demo_logistics_events(order_id, id);
                CREATE INDEX IF NOT EXISTS idx_returns_session ON demo_return_requests(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_wallet_ledger_session ON sandbox_wallet_ledger(session_id, id DESC);
                """
            )
            self._ensure_column(connection, "demo_orders", "refunded_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "demo_orders", "payment_status", "TEXT NOT NULL DEFAULT 'sandbox_paid'")
            self._ensure_column(connection, "demo_orders", "ship_from", "TEXT NOT NULL DEFAULT '浙江省杭州市'")
            self._ensure_column(connection, "demo_orders", "ship_to", "TEXT NOT NULL DEFAULT '河南省郑州市'")
            self._ensure_column(connection, "demo_return_requests", "refund_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "demo_return_requests", "refunded_at", "TEXT")
            connection.execute(
                """
                UPDATE demo_orders SET payment_status='legacy_demo_unpaid'
                WHERE payment_status='sandbox_paid'
                  AND NOT EXISTS (
                    SELECT 1 FROM sandbox_wallet_ledger ledger
                    WHERE ledger.session_id=demo_orders.session_id
                      AND ledger.entry_type='order_payment'
                      AND ledger.entity_id=demo_orders.order_id
                  )
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        session_id: str,
        event_type: str,
        entity_id: str | None,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(session_id,event_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?)",
            (session_id, event_type, entity_id, json.dumps(details, ensure_ascii=False), _now()),
        )

    def ensure_session(self, session_id: str) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO demo_sessions(session_id,created_at,last_seen_at) VALUES(?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (session_id, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO sandbox_accounts(
                    session_id,initial_balance_cents,balance_cents,store_address,customer_address,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    DEFAULT_WALLET_BALANCE_CENTS,
                    DEFAULT_WALLET_BALANCE_CENTS,
                    DEFAULT_STORE_ADDRESS,
                    DEFAULT_CUSTOMER_ADDRESS,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _serialize_account(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "wallet_type": "sandbox",
            "initial_balance": _money(row["initial_balance_cents"]),
            "balance": _money(row["balance_cents"]),
            "currency": "CNY",
            "store_address": row["store_address"],
            "customer_address": row["customer_address"],
            "notice": "沙盒钱包仅用于本地流程演示，不代表真实资金或支付账户。",
        }

    def get_account(self, session_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_accounts WHERE session_id=?", (session_id,)
            ).fetchone()
            return self._serialize_account(row)

    def set_wallet_balance_for_tests(self, session_id: str, balance_cents: int) -> dict[str, Any]:
        """测试辅助入口；生产 API 不暴露余额任意修改能力。"""
        self.ensure_session(session_id)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE sandbox_accounts SET balance_cents=?,updated_at=? WHERE session_id=?",
                (int(balance_cents), _now(), session_id),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_accounts WHERE session_id=?", (session_id,)
            ).fetchone()
            return self._serialize_account(row)

    @staticmethod
    def _wallet_entry(
        connection: sqlite3.Connection,
        session_id: str,
        *,
        entry_type: str,
        entity_id: str,
        amount_cents: int,
    ) -> int:
        existing = connection.execute(
            """
            SELECT balance_after_cents FROM sandbox_wallet_ledger
            WHERE session_id=? AND entry_type=? AND entity_id=?
            """,
            (session_id, entry_type, entity_id),
        ).fetchone()
        if existing:
            return int(existing["balance_after_cents"])
        account = connection.execute(
            "SELECT balance_cents FROM sandbox_accounts WHERE session_id=?", (session_id,)
        ).fetchone()
        if not account:
            raise CommerceRepositoryError("account_not_found", "沙盒账户不存在，请刷新页面后重试")
        next_balance = int(account["balance_cents"]) + int(amount_cents)
        if next_balance < 0:
            raise CommerceRepositoryError(
                "insufficient_balance",
                f"沙盒钱包余额不足：当前余额￥{_money(account['balance_cents']):.2f}",
            )
        now = _now()
        connection.execute(
            "UPDATE sandbox_accounts SET balance_cents=?,updated_at=? WHERE session_id=?",
            (next_balance, now, session_id),
        )
        connection.execute(
            """
            INSERT INTO sandbox_wallet_ledger(
                session_id,entry_type,entity_id,amount_cents,balance_after_cents,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (session_id, entry_type, entity_id, int(amount_cents), next_balance, now),
        )
        return next_balance

    @staticmethod
    def _cart_rows(connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT * FROM cart_items WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        )

    @staticmethod
    def _cart_digest(rows: list[sqlite3.Row]) -> str:
        payload = [
            [
                row["product_id"],
                row["sku_id"],
                row["sku_attributes_json"],
                row["quantity"],
                row["unit_price_cents"],
                row["source_snapshot_at"],
            ]
            for row in rows
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _serialize_cart(rows: list[sqlite3.Row]) -> dict[str, Any]:
        items = []
        total_cents = 0
        quantity_total = 0
        for row in rows:
            subtotal_cents = int(row["unit_price_cents"]) * int(row["quantity"])
            total_cents += subtotal_cents
            quantity_total += int(row["quantity"])
            items.append(
                {
                    "cart_item_id": row["id"],
                    "product_id": row["product_id"],
                    "name": row["product_name"],
                    "image_url": row["image_url"],
                    "source_url": row["source_url"],
                    "source_snapshot_at": row["source_snapshot_at"],
                    "sku_id": row["sku_id"] or None,
                    "attributes": json.loads(row["sku_attributes_json"] or "{}"),
                    "specification_pending": bool(row["specification_pending"]),
                    "unit_price": _money(row["unit_price_cents"]),
                    "currency": row["currency"],
                    "quantity": row["quantity"],
                    "subtotal": _money(subtotal_cents),
                }
            )
        return {
            "items": items,
            "item_count": len(items),
            "quantity_total": quantity_total,
            "total": _money(total_cents),
            "currency": "CNY",
            "demo_only": True,
        }

    def get_cart(self, session_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        with self._connection() as connection:
            return self._serialize_cart(self._cart_rows(connection, session_id))

    def set_cart_item(
        self,
        session_id: str,
        product: dict[str, Any],
        quantity: int,
        selected_variant: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_session(session_id)
        quantity = int(quantity)
        if quantity < 0 or quantity > 99:
            raise CommerceRepositoryError("invalid_quantity", "商品数量必须在 0 至 99 之间")
        product_id = str(product["product_id"])
        sku_id = str((selected_variant or {}).get("sku_id") or "")
        attributes = dict((selected_variant or {}).get("attributes") or {})
        specification_pending = bool(product.get("variants")) and not sku_id
        with self._transaction() as connection:
            if quantity == 0:
                connection.execute(
                    "DELETE FROM cart_items WHERE session_id=? AND product_id=? AND sku_id=?",
                    (session_id, product_id, sku_id),
                )
            else:
                price = (selected_variant or {}).get("price")
                if price is None:
                    price = product.get("price")
                if price is None:
                    raise CommerceRepositoryError("price_unavailable", "该商品目录中缺少价格")
                now = _now()
                connection.execute(
                    """
                    INSERT INTO cart_items(
                        session_id,product_id,product_name,image_url,source_url,source_snapshot_at,
                        sku_id,sku_attributes_json,specification_pending,
                        unit_price_cents,currency,quantity,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id,product_id,sku_id) DO UPDATE SET
                        product_name=excluded.product_name,
                        image_url=excluded.image_url,
                        source_url=excluded.source_url,
                        source_snapshot_at=excluded.source_snapshot_at,
                        sku_attributes_json=excluded.sku_attributes_json,
                        specification_pending=excluded.specification_pending,
                        unit_price_cents=excluded.unit_price_cents,
                        currency=excluded.currency,
                        quantity=excluded.quantity,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        product_id,
                        product.get("name") or "未命名商品",
                        product.get("image_url"),
                        product.get("source_url"),
                        product.get("snapshot_at"),
                        sku_id,
                        json.dumps(attributes, ensure_ascii=False, sort_keys=True),
                        int(specification_pending),
                        int(round(float(price) * 100)),
                        product.get("currency") or "CNY",
                        quantity,
                        now,
                        now,
                    ),
                )
            self._audit(
                connection,
                session_id,
                "cart_item_set",
                f"{product_id}:{sku_id or 'PENDING'}",
                {"quantity": quantity, "sku_id": sku_id or None, "demo_only": True},
            )
            rows = self._cart_rows(connection, session_id)
            return self._serialize_cart(rows)

    def create_order_preview(self, session_id: str, ttl_seconds: int = 600) -> dict[str, Any]:
        self.ensure_session(session_id)
        raw_token = secrets.token_urlsafe(32)
        with self._transaction() as connection:
            rows = self._cart_rows(connection, session_id)
            if not rows:
                raise CommerceRepositoryError("empty_cart", "购物车为空")
            total_cents = sum(int(row["unit_price_cents"]) * int(row["quantity"]) for row in rows)
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
            connection.execute(
                "INSERT INTO order_previews(token_hash,session_id,cart_digest,total_cents,expires_at) VALUES(?,?,?,?,?)",
                (_token_hash(raw_token), session_id, self._cart_digest(rows), total_cents, expires_at),
            )
            cart = self._serialize_cart(rows)
            account_row = connection.execute(
                "SELECT * FROM sandbox_accounts WHERE session_id=?", (session_id,)
            ).fetchone()
            account = self._serialize_account(account_row)
        return {
            "confirmation_token": raw_token,
            "expires_at": expires_at,
            "cart": cart,
            "account": account,
            "balance_after": _money(int(account_row["balance_cents"]) - total_cents),
            "can_afford": int(account_row["balance_cents"]) >= total_cents,
            "shipping": {
                "from": account["store_address"],
                "to": account["customer_address"],
            },
            "confirmation_text": "确认使用沙盒钱包付款并生成订单（不会产生真实支付）",
        }

    @staticmethod
    def _new_order_id() -> str:
        return f"DEMO-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"

    @staticmethod
    def _new_return_id() -> str:
        return f"DEMO-RET-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"

    def create_order(self, session_id: str, confirmation_token: str, idempotency_key: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        if not idempotency_key or len(idempotency_key) > 128:
            raise CommerceRepositoryError("invalid_idempotency_key", "需要有效的幂等键")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT order_id FROM demo_orders WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._get_order(connection, session_id, existing["order_id"])

            preview = connection.execute(
                "SELECT * FROM order_previews WHERE token_hash=? AND session_id=?",
                (_token_hash(confirmation_token or ""), session_id),
            ).fetchone()
            if not preview:
                raise CommerceRepositoryError("invalid_confirmation", "订单确认令牌无效")
            if preview["used_at"]:
                raise CommerceRepositoryError("confirmation_used", "订单确认令牌已使用")
            if datetime.fromisoformat(preview["expires_at"]) < datetime.now(timezone.utc):
                raise CommerceRepositoryError("confirmation_expired", "订单确认已过期，请重新预览")

            rows = self._cart_rows(connection, session_id)
            if not rows:
                raise CommerceRepositoryError("empty_cart", "购物车为空")
            total_cents = sum(int(row["unit_price_cents"]) * int(row["quantity"]) for row in rows)
            if self._cart_digest(rows) != preview["cart_digest"] or total_cents != preview["total_cents"]:
                raise CommerceRepositoryError("cart_changed", "购物车已变化，请重新确认订单")

            order_id = self._new_order_id()
            now = _now()
            snapshot_values = [row["source_snapshot_at"] for row in rows if row["source_snapshot_at"]]
            account = connection.execute(
                "SELECT * FROM sandbox_accounts WHERE session_id=?", (session_id,)
            ).fetchone()
            if not account:
                raise CommerceRepositoryError("account_not_found", "沙盒账户不存在，请刷新页面后重试")
            balance_after_cents = self._wallet_entry(
                connection,
                session_id,
                entry_type="order_payment",
                entity_id=order_id,
                amount_cents=-total_cents,
            )
            connection.execute(
                """
                INSERT INTO demo_orders(
                    order_id,session_id,status,currency,total_cents,item_count,source_snapshot_at,
                    idempotency_key,created_at,updated_at,payment_status,ship_from,ship_to
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id,
                    session_id,
                    "awaiting_demo_fulfillment",
                    "CNY",
                    total_cents,
                    sum(int(row["quantity"]) for row in rows),
                    max(snapshot_values) if snapshot_values else None,
                    idempotency_key,
                    now,
                    now,
                    "sandbox_paid",
                    account["store_address"],
                    account["customer_address"],
                ),
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO demo_order_items(
                        order_id,product_id,product_name,image_url,source_url,
                        sku_id,sku_attributes_json,specification_pending,
                        unit_price_cents,quantity,currency
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        order_id,
                        row["product_id"],
                        row["product_name"],
                        row["image_url"],
                        row["source_url"],
                        row["sku_id"] or None,
                        row["sku_attributes_json"],
                        row["specification_pending"],
                        row["unit_price_cents"],
                        row["quantity"],
                        row["currency"],
                    ),
                )
            connection.execute(
                "INSERT INTO demo_logistics_events(order_id,status,description,event_time) VALUES(?,?,?,?)",
                (
                    order_id,
                    "awaiting_demo_fulfillment",
                    f"订单已使用沙盒钱包付款，商家将在{account['store_address']}备货并发往{account['customer_address']}。",
                    now,
                ),
            )
            connection.execute(
                "UPDATE order_previews SET used_at=? WHERE token_hash=?",
                (now, preview["token_hash"]),
            )
            connection.execute("DELETE FROM cart_items WHERE session_id=?", (session_id,))
            self._audit(
                connection,
                session_id,
                "demo_order_created",
                order_id,
                {
                    "total_cents": total_cents,
                    "balance_after_cents": balance_after_cents,
                    "payment_method": "sandbox_wallet",
                    "demo_only": True,
                },
            )
            return self._get_order(connection, session_id, order_id)

    @staticmethod
    def _serialize_order_row(row: sqlite3.Row) -> dict[str, Any]:
        status = row["status"]
        return {
            "order_id": row["order_id"],
            "status": status,
            "status_text": ORDER_STATUS_LABELS.get(status, status),
            "total": _money(row["total_cents"]),
            "currency": row["currency"],
            "item_count": row["item_count"],
            "source_snapshot_at": row["source_snapshot_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "payment_status": row["payment_status"],
            "paid_amount": _money(row["total_cents"]),
            "refunded_amount": _money(row["refunded_cents"]),
            "shipping": {
                "from": row["ship_from"],
                "to": row["ship_to"],
            },
            "demo_only": True,
        }

    def _get_order(self, connection: sqlite3.Connection, session_id: str, order_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM demo_orders WHERE session_id=? AND order_id=?",
            (session_id, order_id),
        ).fetchone()
        if not row:
            raise CommerceRepositoryError("order_not_found", "未找到当前沙盒会话的订单")
        order = self._serialize_order_row(row)
        items = connection.execute(
            "SELECT * FROM demo_order_items WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        order["items"] = [
            {
                "order_item_id": item["id"],
                "product_id": item["product_id"],
                "name": item["product_name"],
                "image_url": item["image_url"],
                "source_url": item["source_url"],
                "sku_id": item["sku_id"],
                "attributes": json.loads(item["sku_attributes_json"] or "{}"),
                "specification_pending": bool(item["specification_pending"]),
                "unit_price": _money(item["unit_price_cents"]),
                "quantity": item["quantity"],
                "subtotal": _money(item["unit_price_cents"] * item["quantity"]),
                "currency": item["currency"],
            }
            for item in items
        ]
        order["logistics"] = self._get_logistics(connection, order_id)
        return order

    def get_order(self, session_id: str, order_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        with self._connection() as connection:
            return self._get_order(connection, session_id, order_id)

    def list_orders(self, session_id: str) -> list[dict[str, Any]]:
        self.ensure_session(session_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM demo_orders WHERE session_id=? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            return [self._get_order(connection, session_id, row["order_id"]) for row in rows]

    @staticmethod
    def _get_logistics(connection: sqlite3.Connection, order_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT status,description,event_time FROM demo_logistics_events WHERE order_id=? ORDER BY id DESC",
            (order_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_logistics(self, session_id: str, order_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            order = self._get_order(connection, session_id, order_id)
            return {
                "order_id": order_id,
                "status": order["status"],
                "status_text": order["status_text"],
                "events": order["logistics"],
                "estimated_delivery": None,
                "route": {
                    "from": order["shipping"]["from"],
                    "to": order["shipping"]["to"],
                },
                "notice": "物流为沙盒流程模拟，不代表真实承运信息，也不承诺送达时间。",
            }

    def transition_order(
        self,
        session_id: str,
        order_id: str,
        target_status: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """内部演示/测试状态推进；不直接暴露给顾客 API。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM demo_orders WHERE session_id=? AND order_id=?",
                (session_id, order_id),
            ).fetchone()
            if not row:
                raise CommerceRepositoryError("order_not_found", "沙盒订单不存在")
            if row["status"] == target_status:
                return self._get_order(connection, session_id, order_id)
            validate_order_transition(row["status"], target_status)
            now = _now()
            connection.execute(
                "UPDATE demo_orders SET status=?,updated_at=? WHERE order_id=?",
                (target_status, now, order_id),
            )
            connection.execute(
                "INSERT INTO demo_logistics_events(order_id,status,description,event_time) VALUES(?,?,?,?)",
                (
                    order_id,
                    target_status,
                    description or self._default_logistics_description(target_status),
                    now,
                ),
            )
            self._audit(
                connection,
                session_id,
                "demo_order_status_changed",
                order_id,
                {"from": row["status"], "to": target_status},
            )
            return self._get_order(connection, session_id, order_id)

    @staticmethod
    def _default_logistics_description(target_status: str) -> str:
        return {
            "awaiting_demo_fulfillment": "订单已付款，等待杭州仓库备货。",
            "shipped": "杭州仓库已出库，包裹发往河南。",
            "in_transit": "包裹已进入运输网络，正在从杭州运往河南。",
            "delivered": "包裹已送达河南收货地址并完成签收。",
            "closed": "订单已完成。",
            "cancelled": "订单已取消，沙盒钱包款项已原路退回。",
        }.get(target_status, ORDER_STATUS_LABELS.get(target_status, target_status))

    def cancel_order(self, session_id: str, order_id: str) -> dict[str, Any]:
        """仅待发货订单可取消；退款与状态更新在同一事务内完成。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM demo_orders WHERE session_id=? AND order_id=?",
                (session_id, order_id),
            ).fetchone()
            if not row:
                raise CommerceRepositoryError("order_not_found", "订单不存在")
            if row["status"] == "cancelled":
                return self._get_order(connection, session_id, order_id)
            if row["status"] not in {"demo_created", "awaiting_demo_fulfillment"}:
                raise CommerceRepositoryError("order_not_cancellable", "订单已发货，不能直接取消，请申请售后")
            refundable_cents = (
                max(0, int(row["total_cents"]) - int(row["refunded_cents"]))
                if str(row["payment_status"]).startswith("sandbox_")
                else 0
            )
            if refundable_cents > 0:
                balance_after = self._wallet_entry(
                    connection,
                    session_id,
                    entry_type="order_cancel_refund",
                    entity_id=order_id,
                    amount_cents=refundable_cents,
                )
                payment_status = "sandbox_refunded"
                refunded_cents = int(row["total_cents"])
            else:
                balance_after = int(
                    connection.execute(
                        "SELECT balance_cents FROM sandbox_accounts WHERE session_id=?",
                        (session_id,),
                    ).fetchone()["balance_cents"]
                )
                payment_status = "legacy_cancelled_unpaid"
                refunded_cents = int(row["refunded_cents"])
            now = _now()
            connection.execute(
                """
                UPDATE demo_orders
                SET status='cancelled',payment_status=?,refunded_cents=?,updated_at=?
                WHERE order_id=?
                """,
                (payment_status, refunded_cents, now, order_id),
            )
            connection.execute(
                "INSERT INTO demo_logistics_events(order_id,status,description,event_time) VALUES(?,?,?,?)",
                (order_id, "cancelled", self._default_logistics_description("cancelled"), now),
            )
            self._audit(
                connection,
                session_id,
                "sandbox_order_cancelled",
                order_id,
                {"refund_cents": refundable_cents, "balance_after_cents": balance_after},
            )
            return self._get_order(connection, session_id, order_id)

    def create_return_preview(
        self,
        session_id: str,
        order_id: str,
        order_item_id: int,
        quantity: int,
        reason_code: str,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        self.ensure_session(session_id)
        quantity = int(quantity)
        if quantity <= 0:
            raise CommerceRepositoryError("invalid_quantity", "退货数量必须大于 0")
        raw_token = secrets.token_urlsafe(32)
        with self._transaction() as connection:
            order = self._get_order(connection, session_id, order_id)
            if order["status"] not in {"delivered", "closed"}:
                raise CommerceRepositoryError("return_not_available", "只有已签收订单可以申请退货")
            item = connection.execute(
                "SELECT * FROM demo_order_items WHERE id=? AND order_id=?",
                (order_item_id, order_id),
            ).fetchone()
            if not item:
                raise CommerceRepositoryError("order_item_not_found", "订单商品不存在")
            used = connection.execute(
                """
                SELECT COALESCE(SUM(quantity),0) AS used FROM demo_return_requests
                WHERE order_item_id=? AND status != 'rejected'
                """,
                (order_item_id,),
            ).fetchone()["used"]
            available = int(item["quantity"]) - int(used)
            if quantity > available:
                raise CommerceRepositoryError("return_quantity_exceeded", "退货数量超过当前可申请数量")
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO return_previews(
                    token_hash,session_id,order_id,order_item_id,quantity,reason_code,expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    _token_hash(raw_token),
                    session_id,
                    order_id,
                    order_item_id,
                    quantity,
                    reason_code,
                    expires_at,
                ),
            )
            return {
                "confirmation_token": raw_token,
                "expires_at": expires_at,
                "order_id": order_id,
                "order_item_id": order_item_id,
                "product_name": item["product_name"],
                "quantity": quantity,
                "reason_code": reason_code,
                "refund_amount": _money(int(item["unit_price_cents"]) * quantity),
                "confirmation_text": "确认提交沙盒退货申请（不会影响真实店铺订单）",
            }

    def create_return_request(
        self,
        session_id: str,
        confirmation_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 128:
            raise CommerceRepositoryError("invalid_idempotency_key", "需要有效的幂等键")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM demo_return_requests WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._serialize_return(existing)
            preview = connection.execute(
                "SELECT * FROM return_previews WHERE token_hash=? AND session_id=?",
                (_token_hash(confirmation_token or ""), session_id),
            ).fetchone()
            if not preview:
                raise CommerceRepositoryError("invalid_confirmation", "退货确认令牌无效")
            if preview["used_at"]:
                raise CommerceRepositoryError("confirmation_used", "退货确认令牌已使用")
            if datetime.fromisoformat(preview["expires_at"]) < datetime.now(timezone.utc):
                raise CommerceRepositoryError("confirmation_expired", "退货确认已过期，请重新预览")

            order = self._get_order(connection, session_id, preview["order_id"])
            if order["status"] not in {"delivered", "closed"}:
                raise CommerceRepositoryError("return_not_available", "当前订单状态不支持退货")
            item = connection.execute(
                "SELECT quantity,unit_price_cents FROM demo_order_items WHERE id=? AND order_id=?",
                (preview["order_item_id"], preview["order_id"]),
            ).fetchone()
            used = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS used FROM demo_return_requests WHERE order_item_id=? AND status != 'rejected'",
                (preview["order_item_id"],),
            ).fetchone()["used"]
            if not item or int(preview["quantity"]) > int(item["quantity"]) - int(used):
                raise CommerceRepositoryError("return_quantity_exceeded", "退货数量超过当前可申请数量")

            return_id = self._new_return_id()
            now = _now()
            connection.execute(
                """
                INSERT INTO demo_return_requests(
                    return_id,session_id,order_id,order_item_id,quantity,reason_code,status,
                    idempotency_key,created_at,updated_at,refund_cents
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    return_id,
                    session_id,
                    preview["order_id"],
                    preview["order_item_id"],
                    preview["quantity"],
                    preview["reason_code"],
                    "submitted",
                    idempotency_key,
                    now,
                    now,
                    int(item["unit_price_cents"]) * int(preview["quantity"]),
                ),
            )
            connection.execute(
                "UPDATE return_previews SET used_at=? WHERE token_hash=?",
                (now, preview["token_hash"]),
            )
            self._audit(
                connection,
                session_id,
                "demo_return_submitted",
                return_id,
                {"order_id": preview["order_id"], "quantity": preview["quantity"], "demo_only": True},
            )
            row = connection.execute(
                "SELECT * FROM demo_return_requests WHERE return_id=?", (return_id,)
            ).fetchone()
            return self._serialize_return(row)

    @staticmethod
    def _serialize_return(row: sqlite3.Row) -> dict[str, Any]:
        status = row["status"]
        return {
            "return_id": row["return_id"],
            "order_id": row["order_id"],
            "order_item_id": row["order_item_id"],
            "quantity": row["quantity"],
            "reason_code": row["reason_code"],
            "status": status,
            "status_text": RETURN_STATUS_LABELS.get(status, status),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "refund_amount": _money(row["refund_cents"]),
            "refunded_at": row["refunded_at"],
            "demo_only": True,
        }

    def get_return_request(self, session_id: str, return_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM demo_return_requests WHERE session_id=? AND return_id=?",
                (session_id, return_id),
            ).fetchone()
            if not row:
                raise CommerceRepositoryError("return_not_found", "未找到当前沙盒会话的退货申请")
            return self._serialize_return(row)

    def list_return_requests(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM demo_return_requests WHERE session_id=? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            return [self._serialize_return(row) for row in rows]

    def transition_return(
        self,
        session_id: str,
        return_id: str,
        target_status: str,
    ) -> dict[str, Any]:
        """内部演示/测试状态推进；不直接暴露给顾客 API。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM demo_return_requests WHERE session_id=? AND return_id=?",
                (session_id, return_id),
            ).fetchone()
            if not row:
                raise CommerceRepositoryError("return_not_found", "沙盒退货申请不存在")
            if row["status"] == target_status:
                return self._serialize_return(row)
            validate_return_transition(row["status"], target_status)
            now = _now()
            refunded_at = row["refunded_at"]
            if target_status == "demo_refund_completed" and not refunded_at:
                order_row = connection.execute(
                    "SELECT total_cents,refunded_cents,payment_status FROM demo_orders WHERE order_id=? AND session_id=?",
                    (row["order_id"], session_id),
                ).fetchone()
                if not order_row:
                    raise CommerceRepositoryError("order_not_found", "订单不存在")
                refund_cents = (
                    int(row["refund_cents"])
                    if str(order_row["payment_status"]).startswith("sandbox_")
                    else 0
                )
                if refund_cents <= 0:
                    if str(order_row["payment_status"]).startswith("sandbox_"):
                        item = connection.execute(
                            "SELECT unit_price_cents FROM demo_order_items WHERE id=? AND order_id=?",
                            (row["order_item_id"], row["order_id"]),
                        ).fetchone()
                        if not item:
                            raise CommerceRepositoryError("order_item_not_found", "订单商品不存在")
                        refund_cents = int(item["unit_price_cents"]) * int(row["quantity"])
                if refund_cents > 0:
                    balance_after = self._wallet_entry(
                        connection,
                        session_id,
                        entry_type="return_refund",
                        entity_id=return_id,
                        amount_cents=refund_cents,
                    )
                    new_refunded_cents = min(
                        int(order_row["total_cents"]),
                        int(order_row["refunded_cents"]) + refund_cents,
                    )
                    connection.execute(
                        "UPDATE demo_orders SET refunded_cents=?,payment_status=?,updated_at=? WHERE order_id=?",
                        (
                            new_refunded_cents,
                            "sandbox_refunded"
                            if new_refunded_cents >= int(order_row["total_cents"])
                            else "sandbox_partially_refunded",
                            now,
                            row["order_id"],
                        ),
                    )
                else:
                    balance_after = int(
                        connection.execute(
                            "SELECT balance_cents FROM sandbox_accounts WHERE session_id=?",
                            (session_id,),
                        ).fetchone()["balance_cents"]
                    )
                connection.execute(
                    "UPDATE demo_return_requests SET refund_cents=?,refunded_at=? WHERE return_id=?",
                    (refund_cents, now, return_id),
                )
                self._audit(
                    connection,
                    session_id,
                    "sandbox_return_refunded",
                    return_id,
                    {"refund_cents": refund_cents, "balance_after_cents": balance_after},
                )
            connection.execute(
                "UPDATE demo_return_requests SET status=?,updated_at=? WHERE return_id=?",
                (target_status, now, return_id),
            )
            updated = connection.execute(
                "SELECT * FROM demo_return_requests WHERE return_id=?", (return_id,)
            ).fetchone()
            return self._serialize_return(updated)
