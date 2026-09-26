"""RentPing data layer — SQLite locally, Postgres in production.

Backend is chosen by environment: if DATABASE_URL is set (Railway Postgres),
everything runs on Postgres via psycopg; otherwise it falls back to the local
SQLite file. The rest of the app is backend-agnostic: queries are written with
? placeholders (translated to %s on Postgres) and rows behave like dicts.

Tables:
  landlords      - account holders (email + password hash)
  sessions       - login session tokens (cookie based)
  properties     - a landlord's buildings ("Maple St Duplex")
  units          - individual rentals inside a property ("Unit A")
  tenants        - one tenant per unit: name, phone, rent, due day
  message_log    - every SMS in/out, with status ("sent (demo)" in demo mode)
  reminder_log   - which reminder stages already fired (dedupe: never text twice)
  templates      - each landlord's editable message wording
  subscriptions  - plan, status, trial end date
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rentping.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS landlords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    landlord_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    landlord_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    address TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL,
    label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    rent_amount REAL NOT NULL,
    due_day INTEGER NOT NULL,          -- day of month rent is due (1-31)
    paid_period TEXT,                  -- "YYYY-MM" of the rent period marked paid
    opted_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    landlord_id INTEGER NOT NULL,
    tenant_id INTEGER,
    direction TEXT NOT NULL,           -- "out" or "in"
    body TEXT NOT NULL,
    status TEXT NOT NULL,              -- e.g. "sent (demo)", "received"
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL,
    period TEXT NOT NULL,              -- "YYYY-MM" the reminder belongs to
    stage TEXT NOT NULL,               -- before / due / late3 / late7
    sent_at TEXT NOT NULL,
    UNIQUE(tenant_id, period, stage)   -- never send the same reminder twice
);
CREATE TABLE IF NOT EXISTS templates (
    landlord_id INTEGER PRIMARY KEY,
    tpl_before TEXT NOT NULL,
    tpl_due TEXT NOT NULL,
    tpl_late3 TEXT NOT NULL,
    tpl_late7 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    landlord_id INTEGER NOT NULL UNIQUE,
    plan TEXT NOT NULL,                -- starter / growth / portfolio
    status TEXT NOT NULL,              -- trialing / active / canceled
    trial_ends_at TEXT,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_TEMPLATES = {
    "tpl_before": "Hi {name}, friendly reminder that rent of ${amount} for {property} is due on {due_date}. Thanks!",
    "tpl_due": "Hi {name}, just a reminder that rent of ${amount} for {property} is due today ({due_date}). Thanks!",
    "tpl_late3": "Hi {name}, we haven't received your rent of ${amount} for {property} yet (was due {due_date}). Please send it when you can — reply PAID once sent.",
    "tpl_late7": "Hi {name}, your rent of ${amount} for {property} is now 7 days overdue (due {due_date}). Please remit right away to avoid a late fee — reply PAID once sent.",
}


# Postgres variant of the schema: the only SQLite-ism in the DDL is
# AUTOINCREMENT, which becomes SERIAL. (INSERT OR IGNORE is handled in code.)
SCHEMA_PG = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


class _PGConn:
    """Thin wrapper around a psycopg connection so the rest of the code can
    keep writing ? placeholders and calling conn.execute(sql, params)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        # ? never appears inside a string literal in our SQL, so a plain
        # replace is safe; data values travel in params, untouched.
        return self._conn.execute(sql.replace("?", "%s"), params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@contextmanager
def db():
    """One short-lived connection per call. Commits on success."""
    if USE_PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = _PGConn(psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                       connect_timeout=10))
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert_and_get_id(conn, sql, params):
    """Run an INSERT and return the new row's id, on either backend."""
    if USE_PG:
        return conn.execute(sql + " RETURNING id", params).fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def init_db():
    with db() as conn:
        if USE_PG:
            for stmt in (s.strip() for s in SCHEMA_PG.split(";")):
                if stmt:
                    conn.execute(stmt)
        else:
            conn.executescript(SCHEMA)


def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- passwords ---
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(check, digest)


# ---------------------------------------------------------------- landlords --
def create_landlord(name, email, password):
    with db() as conn:
        landlord_id = _insert_and_get_id(
            conn,
            "INSERT INTO landlords (name, email, password_hash, created_at) VALUES (?,?,?,?)",
            (name, email.strip().lower(), hash_password(password), now_iso()),
        )
        # Everyone starts with the default message wording; editable in Settings.
        conn.execute(
            "INSERT INTO templates (landlord_id, tpl_before, tpl_due, tpl_late3, tpl_late7)"
            " VALUES (?,?,?,?,?)",
            (landlord_id, DEFAULT_TEMPLATES["tpl_before"], DEFAULT_TEMPLATES["tpl_due"],
             DEFAULT_TEMPLATES["tpl_late3"], DEFAULT_TEMPLATES["tpl_late7"]),
        )
        return landlord_id


def get_landlord_by_email(email):
    with db() as conn:
        return conn.execute("SELECT * FROM landlords WHERE email = ?",
                            (email.strip().lower(),)).fetchone()


def get_landlord(landlord_id):
    with db() as conn:
        return conn.execute("SELECT * FROM landlords WHERE id = ?",
                            (landlord_id,)).fetchone()


def all_landlords():
    with db() as conn:
        return conn.execute("SELECT * FROM landlords ORDER BY id").fetchall()


# ---------------------------------------------------------------- sessions ---
def create_session(landlord_id):
    token = secrets.token_hex(32)
    with db() as conn:
        conn.execute("INSERT INTO sessions (token, landlord_id, created_at) VALUES (?,?,?)",
                     (token, landlord_id, now_iso()))
    return token


def get_landlord_by_session(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT l.* FROM landlords l JOIN sessions s ON s.landlord_id = l.id"
            " WHERE s.token = ?", (token,)).fetchone()
        return row


def delete_session(token):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --------------------------------------------------------------- properties --
def create_property(landlord_id, name, address=""):
    with db() as conn:
        return _insert_and_get_id(
            conn,
            "INSERT INTO properties (landlord_id, name, address) VALUES (?,?,?)",
            (landlord_id, name, address))


def list_properties(landlord_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM properties WHERE landlord_id = ? ORDER BY id",
            (landlord_id,)).fetchall()


def get_property(property_id, landlord_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM properties WHERE id = ? AND landlord_id = ?",
            (property_id, landlord_id)).fetchone()


# -------------------------------------------------------------------- units --
def create_unit(property_id, label):
    with db() as conn:
        return _insert_and_get_id(
            conn,
            "INSERT INTO units (property_id, label) VALUES (?,?)",
            (property_id, label))


def list_units(property_id):
    with db() as conn:
        return conn.execute("SELECT * FROM units WHERE property_id = ? ORDER BY id",
                            (property_id,)).fetchall()


# ------------------------------------------------------------------ tenants --
def create_tenant(unit_id, name, phone, rent_amount, due_day):
    with db() as conn:
        return _insert_and_get_id(
            conn,
            "INSERT INTO tenants (unit_id, name, phone, rent_amount, due_day, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (unit_id, name, phone.strip(), float(rent_amount), int(due_day), now_iso()))


def get_tenant(tenant_id):
    with db() as conn:
        return conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


def get_tenant_for_landlord(tenant_id, landlord_id):
    """A tenant row only if it belongs to this landlord (stops ID tampering)."""
    with db() as conn:
        return conn.execute(
            """SELECT t.* FROM tenants t
               JOIN units u ON u.id = t.unit_id
               JOIN properties p ON p.id = u.property_id
               WHERE t.id = ? AND p.landlord_id = ?""",
            (tenant_id, landlord_id)).fetchone()


def get_unit_for_landlord(unit_id, landlord_id):
    """A unit row only if it belongs to this landlord (stops ID tampering)."""
    with db() as conn:
        return conn.execute(
            """SELECT u.* FROM units u
               JOIN properties p ON p.id = u.property_id
               WHERE u.id = ? AND p.landlord_id = ?""",
            (unit_id, landlord_id)).fetchone()


def all_tenants(landlord_id):
    """Every tenant of a landlord, with unit + property context attached."""
    with db() as conn:
        return conn.execute(
            """SELECT t.*, u.label AS unit_label, p.name AS property_name, p.id AS property_id
               FROM tenants t
               JOIN units u ON u.id = t.unit_id
               JOIN properties p ON p.id = u.property_id
               WHERE p.landlord_id = ? ORDER BY t.id""",
            (landlord_id,)).fetchall()


def list_tenants(unit_id):
    with db() as conn:
        return conn.execute("SELECT * FROM tenants WHERE unit_id = ? ORDER BY id",
                            (unit_id,)).fetchall()


def get_tenant_by_phone(landlord_id, phone):
    """Match an inbound text to a tenant. Compares last 10 digits so
    +1 (555) 123-4567 matches 5551234567."""
    digits = "".join(c for c in phone if c.isdigit())[-10:]
    for t in all_tenants(landlord_id):
        t_digits = "".join(c for c in t["phone"] if c.isdigit())[-10:]
        if t_digits and t_digits == digits:
            return t
    return None


def mark_tenant_paid(tenant_id, period):
    with db() as conn:
        conn.execute("UPDATE tenants SET paid_period = ? WHERE id = ?", (period, tenant_id))


def set_tenant_opt_out(tenant_id, opted_out):
    with db() as conn:
        conn.execute("UPDATE tenants SET opted_out = ? WHERE id = ?",
                     (1 if opted_out else 0, tenant_id))


def delete_tenant(tenant_id):
    with db() as conn:
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))


# -------------------------------------------------------------- message log --
def log_message(landlord_id, tenant_id, direction, body, status):
    with db() as conn:
        conn.execute(
            "INSERT INTO message_log (landlord_id, tenant_id, direction, body, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (landlord_id, tenant_id, direction, body, status, now_iso()))


def list_messages(landlord_id, limit=50):
    with db() as conn:
        return conn.execute(
            """SELECT m.*, t.name AS tenant_name FROM message_log m
               LEFT JOIN tenants t ON t.id = m.tenant_id
               WHERE m.landlord_id = ? ORDER BY m.id DESC LIMIT ?""",
            (landlord_id, limit)).fetchall()


# ------------------------------------------------------------- reminder log --
def reminder_already_sent(tenant_id, period, stage):
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminder_log WHERE tenant_id = ? AND period = ? AND stage = ?",
            (tenant_id, period, stage)).fetchone()
        return row is not None


def mark_reminder_sent(tenant_id, period, stage):
    with db() as conn:
        if USE_PG:
            conn.execute(
                "INSERT INTO reminder_log (tenant_id, period, stage, sent_at)"
                " VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                (tenant_id, period, stage, now_iso()))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO reminder_log (tenant_id, period, stage, sent_at)"
                " VALUES (?,?,?,?)",
                (tenant_id, period, stage, now_iso()))


# ---------------------------------------------------------------- templates --
def get_templates(landlord_id):
    with db() as conn:
        return conn.execute("SELECT * FROM templates WHERE landlord_id = ?",
                            (landlord_id,)).fetchone()


def save_templates(landlord_id, tpl_before, tpl_due, tpl_late3, tpl_late7):
    with db() as conn:
        conn.execute(
            "UPDATE templates SET tpl_before=?, tpl_due=?, tpl_late3=?, tpl_late7=?"
            " WHERE landlord_id = ?",
            (tpl_before, tpl_due, tpl_late3, tpl_late7, landlord_id))


# ------------------------------------------------------------ subscriptions --
def get_subscription(landlord_id):
    with db() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE landlord_id = ?",
                            (landlord_id,)).fetchone()


def set_subscription(landlord_id, plan, status, trial_ends_at=None):
    with db() as conn:
        conn.execute(
            """INSERT INTO subscriptions (landlord_id, plan, status, trial_ends_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(landlord_id) DO UPDATE SET
                 plan=excluded.plan, status=excluded.status,
                 trial_ends_at=excluded.trial_ends_at, updated_at=excluded.updated_at""",
            (landlord_id, plan, status, trial_ends_at, now_iso()))


def ensure_stripe_columns():
    """Add stripe_customer_id / stripe_subscription_id to subscriptions if missing."""
    with db() as conn:
        for col in ("stripe_customer_id", "stripe_subscription_id"):
            try:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} TEXT")
            except Exception:
                pass  # already exists


def set_stripe_ids(landlord_id, customer_id=None, subscription_id=None):
    ensure_stripe_columns()
    with db() as conn:
        if not get_subscription(landlord_id):
            conn.execute(
                "INSERT INTO subscriptions (landlord_id, plan, status, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(landlord_id) DO NOTHING",
                (landlord_id, "starter", "none", now_iso()))
        if customer_id is not None:
            conn.execute("UPDATE subscriptions SET stripe_customer_id = ? WHERE landlord_id = ?",
                         (customer_id, landlord_id))
        if subscription_id is not None:
            conn.execute("UPDATE subscriptions SET stripe_subscription_id = ? WHERE landlord_id = ?",
                         (subscription_id, landlord_id))


def get_landlord_by_stripe_customer(customer_id):
    ensure_stripe_columns()
    with db() as conn:
        row = conn.execute("SELECT landlord_id FROM subscriptions WHERE stripe_customer_id = ?",
                           (customer_id,)).fetchone()
        if not row:
            return None
        return conn.execute("SELECT * FROM landlords WHERE id = ?",
                            (row["landlord_id"],)).fetchone()
