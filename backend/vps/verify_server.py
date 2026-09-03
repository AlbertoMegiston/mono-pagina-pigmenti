#!/usr/bin/env python3
"""
Servizio di verifica dei codici CLG — variante self-hosted per il VPS.

Alternativa all'edge function Supabase descritta in backend/README.md: stessa
logica, stesso contratto verso la pagina, ma gira sulla macchina del brand.
Usa solo la libreria standard di Python (niente pip, niente virtualenv): su un
server appena installato parte senza dipendenze da scaricare.

Contratto verso la pagina (code/index.html):

    POST /api/verify
    { "code": "558420726815",
      "scan": { "payload": "<testo letto dal barcode>", "format": "data_matrix" | "qr_code" } | null,
      "context": { "when": …, "where": …, "place": … } }
    →  { "outcome": "genuine" | "suspicious" | "fake" | "not_found" | "invalid",
         "via": "scan" | "code" }

Con uno scan vale la regola "solo il barcode emesso e' valido": il payload
letto deve coincidere con quello registrato per la riga; un DataMatrix diverso
che porta dentro un codice per cui un barcode e' registrato e' un falso. Per i
codici senza barcode registrato (liste da txt/csv) e per il QR ?clg= del
pezzo lo scan vale come il codice digitato (vedi verdict).

Se il servizio non risponde (o risponde con un errore), la pagina mostra
"Verifica non disponibile" con un tasto Riprova: non inventa mai un esito.

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
    CLG_RETENTION_DAYS  eta' oltre la quale il registro viene potato (default 180)
"""

import hashlib
import hmac
import json
import os
import random
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
# Oltre questa eta' le righe del registro vengono potate: il conteggio
# anti-clonazione guarda solo gli ultimi CLG_DUP_DAYS, quindi tenerne una
# manciata di mesi basta e impedisce al registro di crescere all'infinito.
RETENTION_DAYS = int(os.environ.get("CLG_RETENTION_DAYS", "180"))

# re.ASCII ovunque si cercano cifre: in Python \d prende anche le cifre
# arabe o a larghezza piena, in JavaScript (la pagina) solo 0-9. Senza, la
# regola non sarebbe davvero identica nelle due implementazioni.
CODE_RE = re.compile(r"^\d{12}$", re.ASCII)
MAX_BODY = 4096  # il corpo legittimo sta in poche centinaia di byte
MAX_PAYLOAD = 512  # un DataMatrix di cartellino sta in poche decine di caratteri

# payload:      testo del DataMatrix stampato sul cartellino (fonte affidabile)
# payload_norm: lo stesso con i bianchi normalizzati, e' quello confrontato
# article, variant, size, internal_id, sheet: i campi dell'Excel del brand
SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
  code         TEXT PRIMARY KEY,
  status       TEXT NOT NULL DEFAULT 'valid'
               CHECK (status IN ('valid', 'suspicious', 'revoked')),
  batch        TEXT,
  note         TEXT,
  created_at   INTEGER NOT NULL,
  payload      TEXT,
  payload_norm TEXT,
  article      TEXT,
  variant      TEXT,
  size         TEXT,
  internal_id  TEXT,
  sheet        TEXT
);
CREATE TABLE IF NOT EXISTS checks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  ip_hash    TEXT,
  ctx        TEXT,
  scanned    INTEGER NOT NULL DEFAULT 0,
  payload_ok INTEGER
);
CREATE INDEX IF NOT EXISTS checks_code_ts ON checks (code, ts);
"""

# Colonne arrivate dopo la prima versione: su un database gia' in uso le
# aggiungiamo all'avvio con ALTER TABLE, cosi' l'aggiornamento non richiede
# passi manuali. Gli indici su queste colonne vanno creati dopo, quando la
# colonna c'e' di sicuro (per questo non stanno in SCHEMA).
MIGRAZIONI = {
    "codes": (("payload", "TEXT"), ("payload_norm", "TEXT"), ("article", "TEXT"),
              ("variant", "TEXT"), ("size", "TEXT"), ("internal_id", "TEXT"),
              ("sheet", "TEXT")),
    "checks": (("scanned", "INTEGER NOT NULL DEFAULT 0"), ("payload_ok", "INTEGER")),
}
INDICI = "CREATE INDEX IF NOT EXISTS idx_codes_payload ON codes (payload_norm);"

# Regola del codice dentro un payload. Deve restare identica a
# clg_excel.estrai_codice e alla versione JavaScript nella pagina: prima
# "clg" + al piu' 4 caratteri non numerici + 12 cifre (stile URL ?clg=...),
# altrimenti l'ULTIMO gruppo "ddd ddd ddd ddd" (separatori: niente, spazio o
# punto) non attaccato ad altre cifre.
CLG_PARAM_RE = re.compile(r"clg\D{0,4}(\d{12})", re.IGNORECASE | re.ASCII)
GROUPS_RE = re.compile(r"(?:^|\D)(\d{3})[ .]?(\d{3})[ .]?(\d{3})[ .]?(\d{3})(?!\d)", re.ASCII)


def extract_code(text):
    """Il codice CLG a 12 cifre contenuto in un payload, o "" se non c'e'."""
    if not text:
        return ""
    m = CLG_PARAM_RE.search(text)
    if m:
        return m.group(1)
    last = None
    for last in GROUPS_RE.finditer(text):
        pass
    return "".join(last.groups()) if last else ""


def normalize_payload(text):
    """Stessa normalizzazione fatta all'importazione: i bianchi ripetuti
    diventano uno spazio, cosi' il confronto non dipende dal lettore."""
    return " ".join((text or "").split())


def connect():
    """Una connessione per richiesta: a questi volumi è la scelta più semplice
    e toglie ogni problema di condivisione fra thread."""
    cx = sqlite3.connect(DB_PATH, timeout=5)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA busy_timeout=5000")
    return cx


def migrate(cx):
    """Aggiunge le colonne che mancano (PRAGMA table_info + ALTER TABLE).
    Rilanciarla e' innocuo: su uno schema gia' aggiornato non fa nulla."""
    for tabella, colonne in MIGRAZIONI.items():
        presenti = {r[1] for r in cx.execute("PRAGMA table_info(%s)" % tabella)}
        for nome, tipo in colonne:
            if nome not in presenti:
                cx.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tabella, nome, tipo))
    cx.executescript(INDICI)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as cx:
        cx.executescript(SCHEMA)
        migrate(cx)


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


def verdict(code, ip_hash, payload=None):
    """L'unico punto in cui si decide un esito.

    Con uno scan (payload: il testo grezzo del barcode letto dal telefono)
    vale prima la regola "solo il barcode emesso e' valido":
    - payload uguale (a bianchi normalizzati) a quello registrato per una
      riga → si prosegue con il codice di quella riga (quello inviato dalla
      pagina non conta)
    - payload sconosciuto ma con dentro un codice per cui e' registrato un
      barcode → fake: il barcode sull'articolo non e' quello emesso per quel
      codice. Eccezione: il QR ?clg= del pezzo (l'indirizzo della pagina)
      non e' un barcode contraffatto, e vale come il codice digitato
    - payload sconosciuto con dentro un codice senza barcode registrato
      (lista caricata da txt/csv) → si prosegue con quel codice: non c'e'
      nessun barcode emesso con cui confrontarlo
    - payload sconosciuto e codice sconosciuto → not_found

    Il codice candidato si estrae dal testo grezzo, lo stesso su cui la
    pagina ha calcolato il suo: la normalizzazione serve solo al confronto
    con payload_norm (i bianchi ripetuti cambierebbero i gruppi trovati).

    Poi, per codice (con o senza scan):
    - codice assente dalla lista        → not_found
    - marcato revocato                  → fake
    - marcato sospetto                  → suspicious
    - valido ma verificato troppe volte → suspicious (segnale di clonazione:
      un codice autentico circola su un pezzo solo, non su decine)
    - altrimenti                        → genuine

    Ritorna (esito, via, codice su cui si e' deciso, payload_ok): via e'
    "scan" o "code"; payload_ok e' None senza scan, 1 se il payload
    coincideva, 0 se no.
    """
    via, payload_ok = "code", None
    with connect() as cx:
        if payload:
            via = "scan"
            row = cx.execute("SELECT code FROM codes WHERE payload_norm = ?",
                             (normalize_payload(payload),)).fetchone()
            if row is not None:
                code, payload_ok = row[0], 1
            else:
                payload_ok = 0
                candidate = extract_code(payload)
                cand = cx.execute("SELECT payload_norm FROM codes WHERE code = ?",
                                  (candidate,)).fetchone() if candidate else None
                if cand is None:
                    return "not_found", via, candidate or code, payload_ok
                # Fake solo se per quel codice un barcode e' stato emesso e
                # quello letto e' un altro DataMatrix. Senza barcode registrato
                # (lista da txt/csv, codici demo) o con il QR ?clg= della
                # pagina lo scan non dice piu' del codice digitato: si
                # prosegue per codice, su quello letto.
                if cand[0] is not None and not CLG_PARAM_RE.search(payload):
                    return "fake", via, candidate, payload_ok
                code = candidate
        row = cx.execute("SELECT status FROM codes WHERE code = ?", (code,)).fetchone()
        if row is None:
            return "not_found", via, code, payload_ok
        status = row[0]
        if status == "revoked":
            return "fake", via, code, payload_ok
        if status == "suspicious":
            return "suspicious", via, code, payload_ok
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
        return ("suspicious" if seen > DUP_LIMIT else "genuine"), via, code, payload_ok


def log_check(code, outcome, ip_hash, ctx, scanned=0, payload_ok=None):
    try:
        with connect() as cx:
            cx.execute(
                "INSERT INTO checks (code, outcome, ts, ip_hash, ctx, scanned, payload_ok) "
                "VALUES (?,?,?,?,?,?,?)",
                (code, outcome, int(time.time()), ip_hash, ctx, scanned, payload_ok),
            )
    except sqlite3.Error as e:
        # Il registro non deve mai impedire una risposta all'utente.
        print("log fallito:", e, file=sys.stderr, flush=True)
    # Ogni tanto (circa una verifica su 200) potiamo le righe piu' vecchie di
    # RETENTION_DAYS: tiene il registro limitato senza un processo pianificato.
    if random.random() < 0.005:
        try:
            with connect() as cx:
                cx.execute("DELETE FROM checks WHERE ts < ?",
                           (int(time.time()) - RETENTION_DAYS * 86400,))
        except sqlite3.Error:
            pass


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
            req = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            self.send_json({"outcome": "invalid"}, 400)
            return
        if not isinstance(req, dict):
            self.send_json({"outcome": "invalid"}, 400)
            return

        raw = req.get("code")
        code = re.sub(r"\D+", "", raw, flags=re.ASCII) if isinstance(raw, str) else ""
        if not CODE_RE.match(code):
            self.send_json({"outcome": "invalid"})
            return

        # Il payload dello scan, se c'e'. Uno troppo lungo lo ignoriamo e si
        # verifica per codice: chi volesse aggirare il confronto puo' gia'
        # omettere lo scan, quindi essere severi qui non aggiungerebbe nulla.
        # A verdict va il testo grezzo: e' quello su cui la pagina ha estratto
        # il codice, e la normalizzazione la fa verdict solo per il confronto.
        scan = req.get("scan")
        payload = None
        if isinstance(scan, dict) and isinstance(scan.get("payload"), str):
            if 0 < len(normalize_payload(scan["payload"])) <= MAX_PAYLOAD:
                payload = scan["payload"]

        ip_hash = hash_ip(self.client_ip())
        try:
            outcome, via, decided, payload_ok = verdict(code, ip_hash, payload)
        except sqlite3.Error as e:
            # Non inventiamo un esito: con un 503 la pagina mostra "Verifica
            # non disponibile" e offre Riprova.
            print("verifica fallita:", e, file=sys.stderr, flush=True)
            self.send_json({"error": "unavailable"}, 503)
            return

        # Nello storico va il codice su cui si e' deciso (e' quello che conta
        # per il conteggio anti-clonazione); se la pagina ne aveva mandato un
        # altro lo conserviamo nel contesto.
        ctx = req.get("context")
        ctx = dict(ctx) if isinstance(ctx, dict) else None
        if decided != code:
            ctx = dict(ctx or {}, code_sent=code)
        log_check(decided, outcome, ip_hash,
                  json.dumps(ctx)[:500] if ctx is not None else None,
                  1 if via == "scan" else 0, payload_ok)
        self.send_json({"outcome": outcome, "via": via})


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
