#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPS TIEMPOS LAB — Capa SQLite (stdlib `sqlite3`)

Introduce una base SQLite ligera SOLO para lo nuevo:
  - `sessions`     — identidad de usuario del dashboard (Workstream C)
  - `user_bets`    — apuestas manuales por usuario (Workstream D)
  - `user_audits`  — auditoría post-sorteo por usuario (Workstream D)
  - `meta`         — pares clave/valor (p.ej. el secret HMAC de sesiones)

NO migra los stores existentes (`predictions_log.jsonl`, `bandit_state.json`,
`historical_*`) — esos siguen en JSON. El archivo vive en
`JPS_DATA_DIR/jps.db` (mismo directorio de datos que el resto del proyecto: el
volumen `/data` en Railway/Docker, o el dir del código en local clásico).

Concurrencia: `jps_server.py` es un `ThreadingHTTPServer`, así que varias
requests tocan la DB en paralelo. Usamos UNA conexión compartida
(`check_same_thread=False`) protegida por un `RLock` global para todas las
operaciones, con WAL y `busy_timeout`. Para el volumen de uso real (pocos
usuarios, pocas escrituras por día) esto es más que suficiente y evita errores
"database is locked".
"""

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "jps.db")

_LOCK = threading.RLock()
_CONN = None  # conexión compartida, lazy


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    """Devuelve la conexión compartida, inicializándola (y el schema) una vez."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            _CONN = conn
            _init_schema(_CONN)
        return _CONN


def _init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            last_seen   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);

        CREATE TABLE IF NOT EXISTS user_bets (
            id                        TEXT PRIMARY KEY,   -- YYYY-MM-DD-session-username
            username                  TEXT NOT NULL,
            draw_date                 TEXT NOT NULL,
            session                   TEXT NOT NULL,
            tickets_json              TEXT NOT NULL,       -- [{num, base, rev}, ...]
            amount                    INTEGER NOT NULL,
            rev_ratio                 REAL,
            anomalies_snapshot_json   TEXT,
            architect_snapshot_json   TEXT,
            created_at                TEXT NOT NULL,
            minutes_before_cutoff     INTEGER,
            status                    TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_bets_user ON user_bets(username);
        CREATE INDEX IF NOT EXISTS idx_bets_status ON user_bets(status);

        CREATE TABLE IF NOT EXISTS user_audits (
            username      TEXT NOT NULL,
            draw_date     TEXT NOT NULL,
            session       TEXT NOT NULL,
            bet_id        TEXT,
            result_json   TEXT NOT NULL,   -- esquema audit_result.json
            generated_at  TEXT NOT NULL,
            PRIMARY KEY (username, draw_date, session)
        );
        CREATE INDEX IF NOT EXISTS idx_audits_user ON user_audits(username);
        """
    )
    conn.commit()


def init_db():
    """Fuerza la creación del archivo/schema (útil en arranque del server)."""
    _connect()
    return DB_PATH


# ─── META (clave/valor) ─────────────────────────────────────────────────────────
def get_meta(key, default=None):
    with _LOCK:
        row = _connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key, value):
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def get_or_create_secret(key="session_hmac_secret"):
    """Devuelve un secret persistente; lo genera una vez si no existe. Permite
    que las cookies firmadas sobrevivan reinicios aun sin JPS_SESSION_SECRET."""
    with _LOCK:
        val = get_meta(key)
        if not val:
            val = secrets.token_hex(32)
            set_meta(key, val)
        return val


# ─── SESIONES (Workstream C) ────────────────────────────────────────────────────
def create_session(username, ttl_seconds=43200):
    """Crea una sesión y devuelve (session_id, expires_at_iso)."""
    session_id = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=int(ttl_seconds))
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO sessions(session_id, username, created_at, expires_at, last_seen) "
            "VALUES(?,?,?,?,?)",
            (session_id, username, now.isoformat(), expires.isoformat(), now.isoformat()),
        )
        conn.commit()
    return session_id, expires.isoformat()


def get_session(session_id):
    """Devuelve la fila de sesión (dict) si existe y NO expiró; si no, None.
    Si expiró, la borra. Actualiza last_seen en un hit válido."""
    if not session_id:
        return None
    with _LOCK:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            return None
        try:
            expired = datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)
        except Exception:
            expired = True
        if expired:
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            conn.commit()
            return None
        conn.execute(
            "UPDATE sessions SET last_seen=? WHERE session_id=?",
            (_now_iso(), session_id),
        )
        conn.commit()
        return dict(row)


def delete_session(session_id):
    if not session_id:
        return
    with _LOCK:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        conn.commit()


def purge_expired_sessions():
    with _LOCK:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now_iso(),))
        conn.commit()


def list_active_sessions():
    """Para observabilidad (Workstream B): sesiones vivas, sin exponer tokens."""
    purge_expired_sessions()
    with _LOCK:
        rows = _connect().execute(
            "SELECT username, created_at, expires_at, last_seen FROM sessions "
            "ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── APUESTAS POR USUARIO (Workstream D) ────────────────────────────────────────
def upsert_user_bet(username, draw_date, session, tickets, amount, rev_ratio=None,
                    anomalies_snapshot=None, architect_snapshot=None,
                    minutes_before_cutoff=None):
    """Inserta/reemplaza la apuesta del usuario para (fecha, sesión). Un usuario
    tiene una apuesta 'vigente' por sesión; re-enviar la actualiza. Devuelve el id."""
    bet_id = f"{draw_date}-{session}-{username}"
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO user_bets(id, username, draw_date, session, tickets_json, amount, "
            " rev_ratio, anomalies_snapshot_json, architect_snapshot_json, created_at, "
            " minutes_before_cutoff, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending') "
            "ON CONFLICT(id) DO UPDATE SET "
            " tickets_json=excluded.tickets_json, amount=excluded.amount, "
            " rev_ratio=excluded.rev_ratio, anomalies_snapshot_json=excluded.anomalies_snapshot_json, "
            " architect_snapshot_json=excluded.architect_snapshot_json, created_at=excluded.created_at, "
            " minutes_before_cutoff=excluded.minutes_before_cutoff, status='pending'",
            (bet_id, username, draw_date, session, json.dumps(tickets, ensure_ascii=False),
             int(amount), rev_ratio,
             json.dumps(anomalies_snapshot, ensure_ascii=False) if anomalies_snapshot is not None else None,
             json.dumps(architect_snapshot, ensure_ascii=False) if architect_snapshot is not None else None,
             _now_iso(), minutes_before_cutoff),
        )
        conn.commit()
    return bet_id


def _bet_row_to_dict(row):
    d = dict(row)
    for k_src, k_dst in (("tickets_json", "tickets"),
                         ("anomalies_snapshot_json", "anomalies_snapshot"),
                         ("architect_snapshot_json", "architect_snapshot")):
        raw = d.pop(k_src, None)
        try:
            d[k_dst] = json.loads(raw) if raw else None
        except Exception:
            d[k_dst] = None
    return d


def get_user_bet(bet_id):
    """Devuelve una apuesta por id (dict) o None."""
    with _LOCK:
        row = _connect().execute("SELECT * FROM user_bets WHERE id=?", (bet_id,)).fetchone()
        return _bet_row_to_dict(row) if row else None


def delete_user_bet(bet_id, username):
    """Borra una apuesta SOLO si pertenece al usuario. Devuelve filas borradas (0/1)."""
    with _LOCK:
        conn = _connect()
        cur = conn.execute("DELETE FROM user_bets WHERE id=? AND username=?", (bet_id, username))
        conn.commit()
        return cur.rowcount


def get_user_bets(username, limit=200):
    with _LOCK:
        rows = _connect().execute(
            "SELECT * FROM user_bets WHERE username=? ORDER BY draw_date DESC, created_at DESC LIMIT ?",
            (username, int(limit)),
        ).fetchall()
        return [_bet_row_to_dict(r) for r in rows]


def pending_user_bets():
    with _LOCK:
        rows = _connect().execute(
            "SELECT * FROM user_bets WHERE status='pending' ORDER BY draw_date ASC"
        ).fetchall()
        return [_bet_row_to_dict(r) for r in rows]


def mark_bet_reconciled(bet_id):
    with _LOCK:
        conn = _connect()
        conn.execute("UPDATE user_bets SET status='reconciled' WHERE id=?", (bet_id,))
        conn.commit()


# ─── AUDITORÍAS POR USUARIO (Workstream D) ──────────────────────────────────────
def upsert_user_audit(username, draw_date, session, result, bet_id=None):
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO user_audits(username, draw_date, session, bet_id, result_json, generated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(username, draw_date, session) DO UPDATE SET "
            " bet_id=excluded.bet_id, result_json=excluded.result_json, generated_at=excluded.generated_at",
            (username, draw_date, session, bet_id,
             json.dumps(result, ensure_ascii=False, default=str), _now_iso()),
        )
        conn.commit()


def get_user_audits(username, limit=200):
    with _LOCK:
        rows = _connect().execute(
            "SELECT * FROM user_audits WHERE username=? ORDER BY draw_date DESC LIMIT ?",
            (username, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["result"] = json.loads(d.pop("result_json"))
            except Exception:
                d["result"] = None
            out.append(d)
        return out


if __name__ == "__main__":
    p = init_db()
    print(f"  ✓ SQLite inicializada en {p}")
    for t in ("meta", "sessions", "user_bets", "user_audits"):
        n = _connect().execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        print(f"    {t:<12} filas={n}")
