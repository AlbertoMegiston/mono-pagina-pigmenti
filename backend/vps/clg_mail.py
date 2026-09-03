#!/usr/bin/env python3
"""
Invio di email tramite Mailgun (API HTTP, regione US), solo libreria standard.

Lo usano il servizio di verifica (verify_server.py) per i codici di verifica
via email e clgadmin (clg_import.py) per la prova d'invio: un solo posto in
cui stanno endpoint, autenticazione e timeout.

Configurazione da variabili d'ambiente. Sul server le mette systemd leggendo
/etc/autenticatore/mail.env (EnvironmentFile dell'unita'); clgadmin, che non
gira sotto systemd, legge lo stesso file con carica_env().
    MAILGUN_API_KEY    chiave API del dominio: mai nel repo, mai nei log
    MAILGUN_DOMAIN     dominio verificato su Mailgun (default crtilogo.com)
    MAILGUN_API_BASE   default https://api.mailgun.net/v3 (regione US);
                       nei test punta a un server locale
    MAIL_FROM          mittente (default
                       "Stone Island Autenticazione <verifica@crtilogo.com>")
"""

import base64
import http.client
import os
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = "/etc/autenticatore/mail.env"
DEFAULT_DOMAIN = "crtilogo.com"
DEFAULT_API_BASE = "https://api.mailgun.net/v3"
DEFAULT_FROM = "Stone Island Autenticazione <verifica@crtilogo.com>"
# Mailgun risponde in meno di un secondo: oltre questo limite e' meglio dire
# alla pagina "riprova" che tenere appesa la richiesta (e il thread).
TIMEOUT = 8


class MailError(Exception):
    """Invio non riuscito. Il messaggio finisce nei log: non contiene mai la
    chiave ne' il destinatario."""


def carica_env(percorso=None):
    """Mette nell'ambiente le variabili del file (KEY=VALUE come lo legge
    systemd: righe vuote e commenti ignorati, virgolette facoltative) senza
    sovrascrivere quelle gia' presenti. Ritorna True se il file c'era."""
    try:
        with open(percorso or ENV_FILE, encoding="utf-8") as f:
            righe = f.read().splitlines()
    except OSError:
        return False
    for riga in righe:
        riga = riga.strip()
        if not riga or riga[0] in "#;" or "=" not in riga:
            continue
        nome, valore = riga.split("=", 1)
        nome, valore = nome.strip(), valore.strip()
        if len(valore) >= 2 and valore[0] == valore[-1] and valore[0] in "\"'":
            valore = valore[1:-1]
        if nome and nome not in os.environ:
            os.environ[nome] = valore
    return True


def config():
    """Letta a ogni chiamata, non all'import: cosi' clgadmin puo' prima
    caricare il file e i test possono cambiare l'endpoint."""
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": os.environ.get("MAILGUN_DOMAIN", "").strip() or DEFAULT_DOMAIN,
        "api_base": (os.environ.get("MAILGUN_API_BASE", "").strip() or DEFAULT_API_BASE).rstrip("/"),
        "mail_from": os.environ.get("MAIL_FROM", "").strip() or DEFAULT_FROM,
    }


def configurato():
    return bool(config()["api_key"])


def invia(destinatario, oggetto, testo, cfg=None):
    """POST <base>/<dominio>/messages con HTTP Basic api:<chiave> e corpo
    application/x-www-form-urlencoded. o:tracking=no: niente pixel ne' link
    riscritti, l'email contiene solo il codice. Solleva MailError se Mailgun
    non risponde 200 (o non risponde affatto)."""
    cfg = cfg or config()
    if not cfg["api_key"]:
        raise MailError("MAILGUN_API_KEY non impostata")
    url = "%s/%s/messages" % (cfg["api_base"], cfg["domain"])
    corpo = urllib.parse.urlencode({
        "from": cfg["mail_from"],
        "to": destinatario,
        "subject": oggetto,
        "text": testo,
        "o:tracking": "no",
    }).encode()
    credenziali = base64.b64encode(("api:" + cfg["api_key"]).encode()).decode()
    req = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Authorization": "Basic " + credenziali,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read()
            if r.status != 200:
                raise MailError("stato %d" % r.status)
    except urllib.error.HTTPError as e:
        # Solo lo stato: il corpo della risposta potrebbe citare il destinatario.
        raise MailError("stato %d" % e.code) from None
    except (OSError, http.client.HTTPException) as e:
        # URLError e timeout sono OSError; il testo non contiene segreti.
        raise MailError("connessione: %s" % e) from None
