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

Codice di verifica via email (il passaggio "Accedi" della pagina; le email
partono con Mailgun, vedi clg_mail.py):

    POST /api/otp/invia     { "email": "...", "lang": "it" | "en" }
    →  200 { "ok": true, "ttl": 600, "retry_in": 30 }
       400 { "error": "invalid_email" }
       429 { "error": "too_soon", "retry_in": n } | { "error": "too_many" }
       503 { "error": "not_configured" | "send_failed" }
    POST /api/otp/verifica  { "email": "...", "code": "123456" }
    →  { "ok": true }
     | { "ok": false, "reason": "expired" | "locked" | "wrong", "left": n }

Il server non rilascia token: la pagina si limita a segnare il passaggio
come fatto. Ne' l'indirizzo ne' il codice finiscono nel database o nei log,
solo le loro impronte.

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
    OTP_TTL                validita' del codice in secondi (default 600)
    OTP_MAX_ATTEMPTS       tentativi di verifica per codice (default 5)
    OTP_RESEND_AFTER       secondi fra un invio e il successivo (default 30)
    OTP_MAX_PER_EMAIL_HOUR invii all'ora per indirizzo (default 5)
    OTP_MAX_PER_IP_HOUR    invii all'ora per IP (default 20)
    MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_BASE, MAIL_FROM: vedi
    clg_mail.py; arrivano da /etc/autenticatore/mail.env. Senza chiave il
    servizio parte lo stesso e /api/otp/invia risponde not_configured.
"""

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Invio email: modulo accanto a questo file, installato da setup.sh. Senza,
# il servizio parte lo stesso e /api/otp/invia risponde not_configured (la
# pagina offre "salta questo passaggio").
try:
    import clg_mail
except ImportError:
    clg_mail = None

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
# Codice di verifica via email: validita', tentativi e limiti di invio (ogni
# invio costa un'email, e un indirizzo altrui non va tempestato).
OTP_TTL = int(os.environ.get("OTP_TTL", "600"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_AFTER = int(os.environ.get("OTP_RESEND_AFTER", "30"))
OTP_MAX_PER_EMAIL_HOUR = int(os.environ.get("OTP_MAX_PER_EMAIL_HOUR", "5"))
OTP_MAX_PER_IP_HOUR = int(os.environ.get("OTP_MAX_PER_IP_HOUR", "20"))

# re.ASCII ovunque si cercano cifre: in Python \d prende anche le cifre
# arabe o a larghezza piena, in JavaScript (la pagina) solo 0-9. Senza, la
# regola non sarebbe davvero identica nelle due implementazioni.
CODE_RE = re.compile(r"^\d{12}$", re.ASCII)
MAX_BODY = 4096  # il corpo legittimo sta in poche centinaia di byte
MAX_PAYLOAD = 512  # un DataMatrix di cartellino sta in poche decine di caratteri
OTP_MAX_BODY = 2048  # email e codice: ancora meno
# Stessa regola "semplice" della pagina: qualcosa@qualcosa.qualcosa. Il vero
# controllo e' l'email che arriva.
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OTP_CODE_RE = re.compile(r"^\d{6}$", re.ASCII)

# payload:      testo del DataMatrix stampato sul cartellino (fonte affidabile)
# payload_norm: lo stesso con i bianchi normalizzati, e' quello confrontato
# article, variant, size, internal_id, sheet: i campi dell'Excel del brand
# otp: un codice di verifica per indirizzo (impronte, mai email o codice in
# chiaro); sends/first_send_at contano gli invii nell'ora corrente
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
CREATE TABLE IF NOT EXISTS otp (
  email_hash    TEXT PRIMARY KEY,
  code_hash     TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  sends         INTEGER NOT NULL DEFAULT 1,
  first_send_at INTEGER NOT NULL,
  ip_hash       TEXT
);
CREATE INDEX IF NOT EXISTS idx_otp_ip ON otp (ip_hash);
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


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --- Codice di verifica via email ------------------------------------------

MESSAGGI_OTP = {
    "it": ("Il tuo codice di verifica: {code}",
           "Il tuo codice per la verifica dell'autenticità è {code}. Vale {min} minuti. "
           "Se non l'hai richiesto tu, ignora questa email."),
    "en": ("Your verification code: {code}",
           "Your code for the authenticity check is {code}. It is valid for {min} minutes. "
           "If you did not request it, please ignore this email."),
}


def valid_email(email):
    return (isinstance(email, str) and len(email.strip()) <= 254
            and EMAIL_RE.match(email.strip()) is not None)


def hash_email(email):
    """Nel database l'indirizzo non compare mai: solo questa impronta (con lo
    stesso sale degli IP), che basta a ritrovare la riga alla verifica.
    Minuscolo, cosi' Mario@ e mario@ sono la stessa casella."""
    return hashlib.sha256(SALT + email.strip().lower().encode("utf-8")).hexdigest()


def hash_code(email_hash, code):
    """Anche il codice si conserva come impronta, legata all'indirizzo: chi
    leggesse il database non potrebbe usarlo."""
    return hashlib.sha256(SALT + email_hash.encode() + code.encode()).hexdigest()


def new_code():
    # secrets, non random: il codice e' un segreto e dev'essere imprevedibile.
    return "%06d" % secrets.randbelow(1000000)


def otp_message(lang, code):
    subject, text = MESSAGGI_OTP["en" if lang == "en" else "it"]
    minuti = max(1, round(OTP_TTL / 60))
    return subject.format(code=code), text.format(code=code, min=minuti)


class OtpRefused(Exception):
    """Invio rifiutato da un limite: error e' la parola per la pagina."""

    def __init__(self, error, retry_in=None):
        super().__init__(error)
        self.error, self.retry_in = error, retry_in


def otp_issue(email_hash, ip_hash):
    """Applica i limiti e registra un nuovo codice per l'indirizzo.

    Ritorna (codice, riga precedente o None): la riga serve a otp_rollback
    se poi l'email non parte. Solleva OtpRefused (too_soon con i secondi
    che mancano, too_many) se un limite e' raggiunto. La riga si scrive
    PRIMA di inviare, cosi' due richieste contemporanee per lo stesso
    indirizzo non producono due email.
    """
    now = int(time.time())
    code = new_code()
    with connect() as cx:
        # Pulizia delle righe scadute da piu' di un'ora, committata a parte:
        # vale anche se subito dopo la richiesta viene rifiutata da un limite
        # (che annulla la transazione).
        cx.execute("DELETE FROM otp WHERE expires_at < ?", (now - 3600,))
        cx.commit()
        # Lettura e scrittura devono essere un'unica transazione: con BEGIN
        # IMMEDIATE due richieste contemporanee si mettono in fila invece di
        # leggere entrambe "nessuna riga".
        cx.execute("BEGIN IMMEDIATE")
        prev = cx.execute(
            "SELECT code_hash, created_at, expires_at, attempts, sends, first_send_at, ip_hash "
            "FROM otp WHERE email_hash = ?", (email_hash,)).fetchone()
        sends, first = 0, now
        if prev is not None:
            if prev[1] > now - OTP_RESEND_AFTER:
                raise OtpRefused("too_soon", prev[1] + OTP_RESEND_AFTER - now)
            # Il contatore vale per l'ora che parte da first_send_at: passata
            # quella, riparte da zero.
            if prev[5] > now - 3600:
                sends, first = prev[4], prev[5]
                if sends >= OTP_MAX_PER_EMAIL_HOUR:
                    raise OtpRefused("too_many")
        if ip_hash is not None:
            per_ip = cx.execute(
                "SELECT COALESCE(SUM(sends), 0) FROM otp WHERE ip_hash = ? AND first_send_at > ?",
                (ip_hash, now - 3600)).fetchone()[0]
            if per_ip >= OTP_MAX_PER_IP_HOUR:
                raise OtpRefused("too_many")
        cx.execute(
            "INSERT OR REPLACE INTO otp (email_hash, code_hash, created_at, expires_at, "
            "attempts, sends, first_send_at, ip_hash) VALUES (?,?,?,?,0,?,?,?)",
            (email_hash, hash_code(email_hash, code), now, now + OTP_TTL, sends + 1, first, ip_hash))
    return code, prev


def otp_rollback(email_hash, prev):
    """L'email non e' partita: il tentativo non deve contare, ne' come invio
    ne' per il too_soon. Torna la riga di prima (il vecchio codice vale
    ancora) o nessuna riga."""
    with connect() as cx:
        if prev is None:
            cx.execute("DELETE FROM otp WHERE email_hash = ?", (email_hash,))
        else:
            cx.execute(
                "UPDATE otp SET code_hash=?, created_at=?, expires_at=?, attempts=?, sends=?, "
                "first_send_at=?, ip_hash=? WHERE email_hash = ?", tuple(prev) + (email_hash,))


def otp_check(email_hash, code):
    """L'esito della verifica, gia' nella forma della risposta."""
    now = int(time.time())
    with connect() as cx:
        cx.execute("BEGIN IMMEDIATE")
        row = cx.execute("SELECT code_hash, expires_at, attempts FROM otp WHERE email_hash = ?",
                         (email_hash,)).fetchone()
        if row is None or row[1] <= now:
            if row is not None:
                cx.execute("DELETE FROM otp WHERE email_hash = ?", (email_hash,))
            return {"ok": False, "reason": "expired"}
        if row[2] >= OTP_MAX_ATTEMPTS:
            return {"ok": False, "reason": "locked"}
        if hmac.compare_digest(row[0], hash_code(email_hash, code)):
            cx.execute("DELETE FROM otp WHERE email_hash = ?", (email_hash,))
            return {"ok": True}
        cx.execute("UPDATE otp SET attempts = attempts + 1 WHERE email_hash = ?", (email_hash,))
        return {"ok": False, "reason": "wrong", "left": OTP_MAX_ATTEMPTS - row[2] - 1}


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
        if self.path == "/api/verify":
            self.post_verify()
        elif self.path == "/api/otp/invia":
            self.post_otp_send()
        elif self.path == "/api/otp/verifica":
            self.post_otp_check()
        else:
            self.send_json({"error": "not found"}, 404)

    def read_body(self, max_body):
        """Il corpo JSON come dict. Ritorna (dict, None) oppure (None,
        "too_large" | "bad_request"): la risposta la decide il chiamante,
        perche' /api/verify e le rotte OTP hanno contratti diversi."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > max_body:
            # Se e' piccolo lo leggiamo e buttiamo: cosi' il client riceve il
            # 413 invece di un reset della connessione (nginx ferma comunque
            # tutto cio' che supera client_max_body_size).
            if length <= 65536:
                self.rfile.read(length)
            return None, "too_large"
        if length <= 0:
            return None, "bad_request"
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return None, "bad_request"
        if not isinstance(req, dict):
            return None, "bad_request"
        return req, None

    def post_otp_send(self):
        req, err = self.read_body(OTP_MAX_BODY)
        if err:
            self.send_json({"error": err}, 413 if err == "too_large" else 400)
            return
        email = req.get("email")
        if not valid_email(email):
            self.send_json({"error": "invalid_email"}, 400)
            return
        if clg_mail is None or not clg_mail.configurato():
            self.send_json({"error": "not_configured"}, 503)
            return
        email_hash = hash_email(email)
        try:
            code, prev = otp_issue(email_hash, hash_ip(self.client_ip()))
        except OtpRefused as e:
            body = {"error": e.error}
            if e.retry_in is not None:
                body["retry_in"] = e.retry_in
            self.send_json(body, 429)
            return
        except sqlite3.Error as e:
            log("otp: database non disponibile: %s" % e)
            self.send_json({"error": "unavailable"}, 503)
            return
        subject, text = otp_message(req.get("lang"), code)
        try:
            clg_mail.invia(email.strip(), subject, text)
        except clg_mail.MailError as e:
            log("otp: invio fallito (%s)" % e)
            try:
                otp_rollback(email_hash, prev)
            except sqlite3.Error as e2:
                log("otp: ripristino fallito: %s" % e2)
            self.send_json({"error": "send_failed"}, 503)
            return
        log("otp: codice inviato")
        self.send_json({"ok": True, "ttl": OTP_TTL, "retry_in": OTP_RESEND_AFTER})

    def post_otp_check(self):
        req, err = self.read_body(OTP_MAX_BODY)
        if err:
            self.send_json({"error": err}, 413 if err == "too_large" else 400)
            return
        email, code = req.get("email"), req.get("code")
        if not valid_email(email):
            self.send_json({"error": "invalid_email"}, 400)
            return
        code = code.strip() if isinstance(code, str) else ""
        if not OTP_CODE_RE.match(code):
            self.send_json({"error": "invalid_code"}, 400)
            return
        try:
            esito = otp_check(hash_email(email), code)
        except sqlite3.Error as e:
            log("otp: database non disponibile: %s" % e)
            self.send_json({"error": "unavailable"}, 503)
            return
        parola = "giusto" if esito["ok"] else {"wrong": "sbagliato", "expired": "scaduto",
                                              "locked": "bloccato"}[esito["reason"]]
        log("otp: codice %s" % parola)
        self.send_json(esito)

    def post_verify(self):
        req, err = self.read_body(MAX_BODY)
        if err:
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
    if clg_mail is None:
        log("ATTENZIONE: clg_mail.py non trovato: i codici via email non partiranno")
    elif not clg_mail.configurato():
        log("MAILGUN_API_KEY non impostata: i codici via email non partiranno "
            "(vedi /etc/autenticatore/mail.env)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
