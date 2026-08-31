#!/usr/bin/env python3
"""
Gestione della lista codici CLG dal server (nessuna interfaccia web).

La lista è la parte più delicata del sistema: si amministra da riga di comando,
sul server, da chi ha già accesso alla macchina. Così non esiste una pagina di
amministrazione da proteggere, né una password in giro.

Esempi:

    # carica una lista (txt con un codice per riga, oppure csv con colonna "code")
    sudo clgadmin importa /root/codici-lotto-A.csv --lotto "lotto-A"

    # marca un codice come revocato (da quel momento l'esito è "falso")
    sudo clgadmin stato 558420726815 revoked

    # quanti codici ci sono e quante verifiche sono state fatte
    sudo clgadmin stato-lista

Il CSV può avere le colonne: code (obbligatoria), status, note.
Righe con codici non validi vengono saltate e riepilogate alla fine.
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import time

DB_PATH = os.environ.get("CLG_DB", "/var/lib/autenticatore/clg.db")
CODE_RE = re.compile(r"^\d{12}$")
STATI = ("valid", "suspicious", "revoked")


def connect(path):
    if not os.path.exists(path):
        sys.exit(f"database non trovato: {path}\n"
                 f"Avvia prima il servizio (systemctl start autenticatore-api), "
                 f"che lo crea al primo avvio.")
    cx = sqlite3.connect(path, timeout=10)
    cx.execute("PRAGMA busy_timeout=10000")
    return cx


def leggi_righe(percorso):
    """Accetta sia un elenco semplice sia un CSV con intestazione.
    Restituisce (codice_grezzo, stato_o_None, nota_o_None)."""
    with open(percorso, "r", encoding="utf-8-sig", errors="replace") as f:
        testo = f.read()
    prima = testo.splitlines()[0] if testo.strip() else ""
    if "code" in prima.lower() and ("," in prima or ";" in prima or "\t" in prima):
        dialetto = csv.Sniffer().sniff(prima, delimiters=",;\t")
        for riga in csv.DictReader(io.StringIO(testo), dialect=dialetto):
            chiavi = {(k or "").strip().lower(): v for k, v in riga.items()}
            yield (chiavi.get("code") or "",
                   (chiavi.get("status") or "").strip().lower() or None,
                   (chiavi.get("note") or "").strip() or None)
    else:
        for riga in testo.splitlines():
            if riga.strip():
                yield (riga, None, None)


def cmd_importa(args):
    cx = connect(args.db)
    ora = int(time.time())
    nuovi = aggiornati = saltati = 0
    scarti = []
    with cx:
        for grezzo, stato, nota in leggi_righe(args.file):
            codice = re.sub(r"\D+", "", grezzo)
            if not CODE_RE.match(codice):
                saltati += 1
                if len(scarti) < 10:
                    scarti.append(grezzo.strip()[:40])
                continue
            if stato not in STATI:
                stato = args.stato
            esiste = cx.execute("SELECT 1 FROM codes WHERE code = ?", (codice,)).fetchone()
            cx.execute(
                "INSERT INTO codes (code, status, batch, note, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET status=excluded.status, "
                "batch=excluded.batch, note=excluded.note",
                (codice, stato, args.lotto, nota, ora),
            )
            if esiste:
                aggiornati += 1
            else:
                nuovi += 1
    print(f"inseriti: {nuovi}   aggiornati: {aggiornati}   saltati: {saltati}")
    if scarti:
        print("esempi di righe scartate:", ", ".join(scarti))


def cmd_stato(args):
    if args.nuovo_stato not in STATI:
        sys.exit(f"stato non valido: {args.nuovo_stato} (usa: {', '.join(STATI)})")
    codice = re.sub(r"\D+", "", args.codice)
    if not CODE_RE.match(codice):
        sys.exit("il codice deve essere di 12 cifre")
    cx = connect(args.db)
    with cx:
        n = cx.execute("UPDATE codes SET status = ? WHERE code = ?",
                       (args.nuovo_stato, codice)).rowcount
    if n:
        print(f"{codice} → {args.nuovo_stato}")
    else:
        sys.exit(f"{codice} non è nella lista")


def cmd_svuota(args):
    """Serve soprattutto una volta: togliere i codici dimostrativi seminati
    dall'installazione prima di caricare la lista vera. Il registro delle
    verifiche non viene toccato."""
    cx = connect(args.db)
    tot = cx.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    if not args.conferma:
        sys.exit(f"in lista ci sono {tot} codici. "
                 f"Per cancellarli tutti rilancia con --conferma")
    with cx:
        cx.execute("DELETE FROM codes")
    print(f"cancellati {tot} codici")


def cmd_riepilogo(args):
    cx = connect(args.db)
    tot = cx.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    print(f"codici in lista: {tot}")
    for stato, n in cx.execute("SELECT status, COUNT(*) FROM codes GROUP BY status"):
        print(f"  {stato}: {n}")
    ver = cx.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    print(f"verifiche registrate: {ver}")
    for esito, n in cx.execute(
            "SELECT outcome, COUNT(*) FROM checks GROUP BY outcome ORDER BY 2 DESC"):
        print(f"  {esito}: {n}")
    print("codici più verificati (ultimi 30 giorni):")
    since = int(time.time()) - 30 * 86400
    righe = cx.execute(
        "SELECT code, COUNT(DISTINCT ip_hash) c FROM checks WHERE ts >= ? "
        "GROUP BY code ORDER BY c DESC LIMIT 5", (since,)).fetchall()
    for codice, n in righe:
        print(f"  {codice}: {n} dispositivi")
    if not righe:
        print("  (nessuna)")


def main():
    p = argparse.ArgumentParser(description="Lista codici CLG")
    p.add_argument("--db", default=DB_PATH, help=f"database (default {DB_PATH})")
    sub = p.add_subparsers(dest="comando", required=True)

    imp = sub.add_parser("importa", help="carica una lista di codici")
    imp.add_argument("file")
    imp.add_argument("--lotto", default=None, help="etichetta del lotto")
    imp.add_argument("--stato", default="valid", choices=STATI,
                     help="stato per le righe che non lo indicano")
    imp.set_defaults(func=cmd_importa)

    st = sub.add_parser("stato", help="cambia lo stato di un codice")
    st.add_argument("codice")
    st.add_argument("nuovo_stato", choices=STATI)
    st.set_defaults(func=cmd_stato)

    sv = sub.add_parser("svuota", help="cancella tutti i codici dalla lista")
    sv.add_argument("--conferma", action="store_true")
    sv.set_defaults(func=cmd_svuota)

    ri = sub.add_parser("stato-lista", help="riepilogo di lista e verifiche")
    ri.set_defaults(func=cmd_riepilogo)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
