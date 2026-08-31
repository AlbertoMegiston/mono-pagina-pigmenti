#!/usr/bin/env python3
"""
Servizio di verifica dei codici CLG — variante self-hosted per il VPS.

Alternativa all'edge function Supabase descritta in backend/README.md: stessa
logica, stesso contratto verso la pagina, ma gira sulla macchina del brand.
Usa solo la libreria standard di Python (niente pip, niente virtualenv): su un
server appena installato parte senza dipendenze da scaricare.

Contratto verso la pagina (code/index.html):

    POST /api/verify
    { "code": "558420726815", "context": { "when": …, "where": …, "place": … } }
    →  { "outcome": "genuine" | "suspicious" | "fake" | "not_found" | "invalid" }

Se il servizio non risponde, la pagina ricade da sola sulla simulazione: un
guasto qui non rompe l'esperienza, la riporta solo allo stato dimostrativo.

Ascolta su 127.0.0.1: l'unico modo per raggiungerlo dall'esterno è il reverse
proxy di nginx, che applica anche il limite di richieste.

Configurazione via variabili d'ambiente (vedi autenticatore-api.service):
    CLG_DB          percorso del database SQLite
    CLG_SALT_FILE   file con il sale per l'hash degli indirizzi IP
    CLG_BIND_HOST   default 127.0.0.1
    CLG_BIND_PORT   default 8787
    CLG_DUP_LIMIT   verifiche oltre le quali un codice valido diventa
                    sospetto (default 10)
    CLG_DUP_DAYS    finestra in giorni per quel conteggio (default 30)
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("CLG_DB", "/var/lib/autenticatore/clg.db")
SALT_FILE = os.environ.get("CLG_SALT_FILE", "/var/lib/autenticatore/ip-salt")
HOST = os.environ.get("CLG_BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLG_BIND_PORT", "8787"))
DUP_LIMIT = int(os.environ.get("CLG_DUP_LIMIT", "10"))
DUP_DAYS = int(os.environ.get("CLG_DUP_DAYS", "30"))

CODE_RE = re.compile(r"^\d{12}$")
MAX_BODY = 4096  # il corpo legittimo sta in poche centinaia di byte

SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
  code       TEXT PRIMARY KEY,
  status     TEXT NOT NULL DEFAULT 'valid'
             CHECK (status IN ('valid', 'suspicious', 'revoked')),
  batch      TEXT,
  note       TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  code    TEXT NOT NULL,
  outcome TEXT NOT NULL,
  ts      INTEGER NOT NULL,
  ip_hash TEXT,
  ctx     TEXT
);
CREATE INDEX IF NOT EXISTS checks_code_ts ON checks (code, ts);
"""


def connect():
    """Una connessione per richiesta: a questi volumi è la scelta più semplice
    e toglie ogni problema di condivisione fra thread."""
    cx = sqlite3.connect(DB_PATH, timeout=5)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA busy_timeout=5000")
    return cx


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as cx:
        cx.executescript(SCHEMA)


def load_salt():
    """Il sale rende gli hash degli IP non ricostruibili con una tabella
    precalcolata. Se manca lo creiamo, con permessi ristretti."""
    try:
        with open(SALT_FILE, "rb") as f:
            salt = f.read().strip()
            if salt:
                return salt
    except FileNotFoundError:
        pass
    salt = os.urandom(32).hex().encode()
    os.makedirs(os.path.dirname(SALT_FILE), exist_ok=True)
    fd = os.open(SALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(salt)
    return salt


SALT = load_salt()


def hash_ip(ip):
    """Registriamo un'impronta, non l'indirizzo: basta a contare le verifiche
    ripetute senza conservare un dato personale in chiaro."""
    if not ip:
        return None
    return hmac.new(SALT, ip.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:32]


def verdict(code, ip_hash):
    """L'unico punto in cui si decide un esito.

    - codice assente dalla lista        → not_found
    - marcato revocato                  → fake
    - marcato sospetto                  → suspicious
    - valido ma verificato troppe volte → suspicious (segnale di clonazione:
      un codice autentico circola su un pezzo solo, non su decine)
    - altrimenti                        → genuine
    """
    with connect() as cx:
        row = cx.execute("SELECT status FROM codes WHERE code = ?", (code,)).fetchone()
        if row is None:
            return "not_found"
        status = row[0]
        if status == "revoked":
            return "fake"
        if status == "suspicious":
            return "suspicious"
        since = int(time.time()) - DUP_DAYS * 86400
        seen = cx.execute(
            "SELECT COUNT(DISTINCT ip_hash) FROM checks "
            "WHERE code = ? AND ts >= ? AND ip_hash IS NOT NULL",
            (code, since),
        ).fetchone()[0]
        # L'apparecchio che sta verificando ora conta come uno in più solo se
        # non ha già verificato questo codice nella finestra.
        if ip_hash is not None:
            already = cx.execute(
                "SELECT 1 FROM checks WHERE code = ? AND ts >= ? AND ip_hash = ? LIMIT 1",
                (code, since, ip_hash),
            ).fetchone()
            if already is None:
                seen += 1
        return "suspicious" if seen > DUP_LIMIT else "genuine"


def log_check(code, outcome, ip_hash, ctx):
    try:
        with connect() as cx:
            cx.execute(
                "INSERT INTO checks (code, outcome, ts, ip_hash, ctx) VALUES (?,?,?,?,?)",
                (code, outcome, int(time.time()), ip_hash, ctx),
            )
    except sqlite3.Error as e:
        # Il registro non deve mai impedire una risposta all'utente.
        print("log fallito:", e, file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "autenticatore"
    sys_version = ""

    def log_message(self, fmt, *args):
        # nginx tiene già l'access log; qui teniamo solo gli errori.
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        """Ascoltiamo solo su localhost: l'unico che può valorizzare questa
        intestazione è il nostro nginx."""
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else None

    def do_GET(self):
        if self.path == "/api/health":
            self.send_json({"ok": True})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/verify":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.send_json({"outcome": "invalid"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            self.send_json({"outcome": "invalid"}, 400)
            return
        if not isinstance(payload, dict):
            self.send_json({"outcome": "invalid"}, 400)
            return

        raw = payload.get("code")
        code = re.sub(r"\D+", "", raw) if isinstance(raw, str) else ""
        if not CODE_RE.match(code):
            self.send_json({"outcome": "invalid"})
            return

        ip_hash = hash_ip(self.client_ip())
        try:
            outcome = verdict(code, ip_hash)
        except sqlite3.Error as e:
            # Non inventiamo un esito: senza database la pagina ricade da sola
            # sulla simulazione, che si dichiara.
            print("verifica fallita:", e, file=sys.stderr, flush=True)
            self.send_json({"error": "unavailable"}, 503)
            return

        ctx = payload.get("context")
        log_check(code, outcome, ip_hash,
                  json.dumps(ctx)[:500] if isinstance(ctx, dict) else None)
        self.send_json({"outcome": outcome})


def main():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"autenticatore api in ascolto su {HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
