#!/usr/bin/env python3
"""
Gestione della lista codici CLG dal server (nessuna interfaccia web).

La lista è la parte più delicata del sistema: si amministra da riga di comando,
sul server, da chi ha già accesso alla macchina. Così non esiste una pagina di
amministrazione da proteggere, né una password in giro.

Esempi:

    # carica una lista (txt con un codice per riga, oppure csv con colonna "code")
    sudo clgadmin importa /root/codici-lotto-A.csv --lotto "lotto-A"

    # carica l'Excel del brand (.xlsx/.xlsm): il codice viene letto dal
    # DataMatrix in colonna F, la colonna E vale solo come ripiego
    sudo clgadmin importa /root/Barcode_DataMatrix.xlsm --lotto "lotto-A"
    sudo clgadmin importa /root/Barcode_DataMatrix.xlsm --codice-da colonna

    # marca un codice come revocato (da quel momento l'esito è "falso")
    sudo clgadmin stato 558420726815 revoked

    # quanti codici ci sono e quante verifiche sono state fatte
    sudo clgadmin stato-lista

    # prova l'invio email dei codici di verifica (chiave in /etc/autenticatore/mail.env)
    sudo clgadmin mail-test tua@email.it

Il CSV può avere le colonne: code (obbligatoria), status, note.
Righe con codici non validi vengono saltate e riepilogate alla fine.
Un codice già in lista mantiene il suo stato, a meno che non venga indicato
(colonna status del CSV oppure --stato).
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import time

try:
    import clg_datamatrix  # accanto a questo script; DataMatrix per i codici a mano
except ImportError:
    clg_datamatrix = None
try:
    import clg_excel  # accanto a questo script; serve solo per gli Excel
except ImportError:
    clg_excel = None
try:
    import clg_mail  # idem; serve solo a mail-test
except ImportError:
    clg_mail = None

DB_PATH = os.environ.get("CLG_DB", "/var/lib/autenticatore/clg.db")
# re.ASCII come nel servizio: le cifre non ASCII (arabe, a larghezza piena)
# non sono cifre per la pagina, quindi nemmeno qui.
CODE_RE = re.compile(r"^\d{12}$", re.ASCII)
STATI = ("valid", "suspicious", "revoked")

# Colonne aggiunte dopo la prima versione. Le crea il servizio di verifica
# all'avvio (verify_server.init_db); qui le controlliamo di nuovo perche'
# clgadmin puo' girare prima che il servizio sia stato riavviato.
COLONNE_CODICI = (("payload", "TEXT"), ("payload_norm", "TEXT"), ("article", "TEXT"),
                  ("variant", "TEXT"), ("size", "TEXT"), ("internal_id", "TEXT"),
                  ("sheet", "TEXT"))

# Upsert unico per txt/csv ed Excel. Lo stato cambia solo se indicato
# (:stato non nullo); payload, nota e campi si aggiornano solo con un valore
# nuovo, cosi' un ricaricamento da txt non cancella i barcode gia' registrati.
SQL_UPSERT = (
    "INSERT INTO codes (code, status, batch, note, created_at, payload, payload_norm, "
    "article, variant, size, internal_id, sheet) "
    "VALUES (:code, COALESCE(:stato, 'valid'), :lotto, :nota, :ora, :payload, :payload_norm, "
    ":article, :variant, :size, :internal_id, :sheet) "
    "ON CONFLICT(code) DO UPDATE SET "
    "status=COALESCE(:stato, codes.status), batch=excluded.batch, "
    "note=COALESCE(excluded.note, codes.note), "
    "payload=COALESCE(excluded.payload, codes.payload), "
    "payload_norm=COALESCE(excluded.payload_norm, codes.payload_norm), "
    "article=COALESCE(excluded.article, codes.article), "
    "variant=COALESCE(excluded.variant, codes.variant), "
    "size=COALESCE(excluded.size, codes.size), "
    "internal_id=COALESCE(excluded.internal_id, codes.internal_id), "
    "sheet=COALESCE(excluded.sheet, codes.sheet)"
)


def connect(path):
    if not os.path.exists(path):
        sys.exit(f"database non trovato: {path}\n"
                 f"Avvia prima il servizio (systemctl start autenticatore-api), "
                 f"che lo crea al primo avvio.")
    cx = sqlite3.connect(path, timeout=10)
    cx.execute("PRAGMA busy_timeout=10000")
    return cx


def assicura_colonne(cx):
    presenti = {r[1] for r in cx.execute("PRAGMA table_info(codes)")}
    with cx:
        for nome, tipo in COLONNE_CODICI:
            if nome not in presenti:
                cx.execute(f"ALTER TABLE codes ADD COLUMN {nome} {tipo}")
        cx.execute("CREATE INDEX IF NOT EXISTS idx_codes_payload ON codes (payload_norm)")


def parametri(codice, stato, lotto, nota, ora, riga=None):
    """I parametri di SQL_UPSERT; riga e' una riga analizzata da clg_excel."""
    r = riga or {}
    return {
        "code": codice, "stato": stato, "lotto": lotto, "nota": nota, "ora": ora,
        "payload": r.get("payload") or None,
        "payload_norm": r.get("payload_norm") or None,
        "article": r.get("article") or None, "variant": r.get("variant") or None,
        "size": r.get("size") or None, "internal_id": r.get("internal_id") or None,
        "sheet": r.get("sheet") or None,
    }


def normalizza_payload(payload):
    """Come verify_server.normalize_payload: bianchi ripetuti ridotti a uno."""
    return " ".join(str(payload).split())


def registra_barcode(cx, codice, payload, campi=None, sostituisci=False):
    """Registra payload come barcode emesso per codice: da quel momento allo
    scan vale solo quello. Ritorna "ok", "assente" (codice non in lista) o
    "gia_registrato" (c'e' gia' un barcode e sostituisci e' falso: i
    cartellini gia' stampati con il vecchio smetterebbero di valere)."""
    campi = campi or {}
    riga = cx.execute("SELECT payload FROM codes WHERE code = ?", (codice,)).fetchone()
    if riga is None:
        return "assente"
    if riga[0] and not sostituisci:
        return "gia_registrato"
    with cx:
        cx.execute(
            "UPDATE codes SET payload = ?, payload_norm = ?, "
            "article = COALESCE(?, article), variant = COALESCE(?, variant), "
            "size = COALESCE(?, size), internal_id = COALESCE(?, internal_id) "
            "WHERE code = ?",
            (payload, normalizza_payload(payload),
             campi.get("article") or None, campi.get("variant") or None,
             campi.get("size") or None, campi.get("internal_id") or None, codice))
    return "ok"


def senza_barcode(cx):
    """[(code, article, variant, size, internal_id)] dei codici senza barcode."""
    return cx.execute(
        "SELECT code, article, variant, size, internal_id FROM codes "
        "WHERE payload_norm IS NULL ORDER BY code").fetchall()


def residui_senza_barcode(cx, righe):
    """Codici gia' in lista degli stessi fogli, senza barcode registrato e non
    toccati da questa importazione. Sono quasi sempre i codici casuali della
    colonna E lasciati da un'importazione fatta senza Pillow/zxing-cpp: hanno
    un codice diverso da quello dei barcode, quindi l'upsert non li aggiorna e
    resterebbero in lista come validi. Chi importa deve saperlo."""
    fogli = sorted({r["sheet"] for r in righe if r["valido"] and r["sheet"]})
    if not fogli:
        return 0
    importati = {r["code"] for r in righe if r["valido"]}
    presenti = cx.execute(
        "SELECT code FROM codes WHERE payload_norm IS NULL AND sheet IN (%s)"
        % ",".join("?" * len(fogli)), fogli).fetchall()
    return sum(1 for (c,) in presenti if c not in importati)


AVVISO_RESIDUI = ("ATTENZIONE: {n} codici degli stessi fogli restano in lista senza barcode "
                  "(importazione precedente senza lettura dei DataMatrix?): non coincidono "
                  "con quelli dei barcode, quindi non sono stati aggiornati. Per toglierli: "
                  "clgadmin svuota --conferma, poi reimporta")
AVVISO_SENZA_DECODIFICA = (
    "ATTENZIONE: Pillow/zxing-cpp non installati: i barcode non sono stati letti e i "
    "codici vengono dalla colonna E, che sono casuali (vedi deploy/setup.sh). Quando le "
    "librerie ci saranno, svuota la lista (clgadmin svuota --conferma) prima di "
    "reimportare: i codici presi da E non coincidono con quelli dei barcode e "
    "resterebbero in lista")


def e_excel(percorso):
    if percorso.lower().endswith((".xlsx", ".xlsm")):
        return True
    try:
        with open(percorso, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


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


def importa_excel(cx, args):
    """Excel del brand: un prodotto per riga, DataMatrix in colonna F.
    Il codice viene dal barcode (o da E con --codice-da colonna); la riga
    salva anche payload, articolo, variante, taglia e identificativo."""
    try:
        righe, riep = clg_excel.analizza_file(args.file, origine_codice=args.codice_da)
    except clg_excel.ExcelNonValido as e:
        sys.exit(f"file non leggibile: {e}")
    ora = int(time.time())
    nuovi = aggiornati = 0
    scarti = []
    with cx:
        for r in righe:
            if not r["valido"]:
                if len(scarti) < 10:
                    dove = f"{r['foglio']}!{r['riga']}"
                    scarti.append(f"{dove} ({r['motivo']})" if r["motivo"] else dove)
                continue
            esiste = cx.execute("SELECT 1 FROM codes WHERE code = ?", (r["code"],)).fetchone()
            cx.execute(SQL_UPSERT, parametri(r["code"], args.stato, args.lotto, None, ora, r))
            if esiste:
                aggiornati += 1
            else:
                nuovi += 1
        residui = residui_senza_barcode(cx, righe)
    print(f"fogli: {len(riep['fogli'])}   righe: {riep['totale']}")
    print(f"importati: {nuovi}   aggiornati: {aggiornati}   scartati: {riep['scartati']}")
    print(f"discordanti (colonna E diversa dal barcode): {riep['discordanti']}   "
          f"senza immagine: {riep['senza_immagine']}   "
          f"non decodificabili: {riep['non_decodificabili']}")
    if residui:
        print(AVVISO_RESIDUI.format(n=residui))
    if not riep["decodifica_disponibile"]:
        print(AVVISO_SENZA_DECODIFICA)
    if scarti:
        print("righe scartate:", ", ".join(scarti))


def cmd_importa(args):
    cx = connect(args.db)
    assicura_colonne(cx)
    if e_excel(args.file):
        if clg_excel is None:
            sys.exit("per leggere gli Excel serve clg_excel.py accanto a questo script")
        importa_excel(cx, args)
        return
    ora = int(time.time())
    nuovi = aggiornati = saltati = 0
    scarti = []
    with cx:
        for grezzo, stato, nota in leggi_righe(args.file):
            codice = re.sub(r"\D+", "", grezzo, flags=re.ASCII)
            if not CODE_RE.match(codice):
                saltati += 1
                if len(scarti) < 10:
                    scarti.append(grezzo.strip()[:40])
                continue
            if stato not in STATI:
                stato = args.stato
            esiste = cx.execute("SELECT 1 FROM codes WHERE code = ?", (codice,)).fetchone()
            cx.execute(SQL_UPSERT, parametri(codice, stato, args.lotto, nota, ora))
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
    codice = re.sub(r"\D+", "", args.codice, flags=re.ASCII)
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
    presenti = {r[1] for r in cx.execute("PRAGMA table_info(codes)")}
    if "payload_norm" in presenti:
        con_barcode = cx.execute(
            "SELECT COUNT(*) FROM codes WHERE payload_norm IS NOT NULL").fetchone()[0]
        print(f"  con barcode registrato: {con_barcode}")
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


def cmd_datamatrix(args):
    """Genera (e registra) il DataMatrix di un codice caricato a mano."""
    if clg_datamatrix is None or not clg_datamatrix.GENERAZIONE_DISPONIBILE:
        sys.exit("generazione dei DataMatrix non disponibile: servono clg_datamatrix.py, "
                 "Pillow e zxing-cpp (vedi setup.sh, passo 1b)")
    codice = re.sub(r"\D+", "", args.codice, flags=re.ASCII)
    if not CODE_RE.match(codice):
        sys.exit("il codice deve essere di 12 cifre")
    campi = {"article": args.articolo, "variant": args.variante,
             "size": args.taglia, "internal_id": args.identificativo}
    cx = connect(args.db)
    assicura_colonne(cx)
    riga = cx.execute("SELECT payload FROM codes WHERE code = ?", (codice,)).fetchone()
    if riga is None:
        sys.exit(f"{codice} non e' in lista: importalo prima (clgadmin importa)")
    if riga[0] and not args.sostituisci and not any(campi.values()):
        payload = riga[0]        # ristampa del barcode gia' registrato
        esito = "esistente"
    else:
        try:
            payload = clg_datamatrix.componi_payload(codice, **campi)
        except ValueError as e:
            sys.exit(str(e))
        esito = registra_barcode(cx, codice, payload, campi, sostituisci=args.sostituisci)
        if esito == "gia_registrato":
            sys.exit(f"{codice} ha gia' un barcode registrato ({riga[0]}): per sostituirlo "
                     "aggiungi --sostituisci (i cartellini gia' stampati non varranno piu')")
    img = clg_datamatrix.genera(payload)
    out = args.out or f"{codice}.png"
    with open(out, "wb") as f:
        f.write(img["svg"].encode("utf-8") if out.lower().endswith(".svg") else img["png"])
    print(f"{codice}: barcode {esito}\n  contenuto: {payload}\n  file: {out}")


def cmd_datamatrix_mancanti(args):
    """Un DataMatrix (PNG e SVG) per ogni codice senza barcode, in una cartella."""
    if clg_datamatrix is None or not clg_datamatrix.GENERAZIONE_DISPONIBILE:
        sys.exit("generazione dei DataMatrix non disponibile: servono clg_datamatrix.py, "
                 "Pillow e zxing-cpp (vedi setup.sh, passo 1b)")
    cx = connect(args.db)
    assicura_colonne(cx)
    os.makedirs(args.cartella, exist_ok=True)
    n = 0
    for code, article, variant, size, internal_id in senza_barcode(cx):
        campi = {"article": article, "variant": variant, "size": size, "internal_id": internal_id}
        try:
            payload = clg_datamatrix.componi_payload(code, **campi)
        except ValueError as e:
            print(f"  {code} saltato: {e}", file=sys.stderr)
            continue
        if registra_barcode(cx, code, payload, campi) != "ok":
            continue
        img = clg_datamatrix.genera(payload)
        with open(os.path.join(args.cartella, f"{code}.png"), "wb") as f:
            f.write(img["png"])
        with open(os.path.join(args.cartella, f"{code}.svg"), "w", encoding="utf-8") as f:
            f.write(img["svg"])
        n += 1
    print(f"generati e registrati {n} DataMatrix in {args.cartella}")


def cmd_mail_test(args):
    """Una email di prova con la stessa funzione del servizio: se arriva,
    arrivano anche i codici di verifica. La chiave non viene mai stampata."""
    if clg_mail is None:
        sys.exit("per l'invio email serve clg_mail.py accanto a questo script")
    # clgadmin non gira sotto systemd: il file di ambiente lo leggiamo noi,
    # senza sovrascrivere variabili gia' presenti.
    clg_mail.carica_env()
    cfg = clg_mail.config()
    if not cfg["api_key"]:
        sys.exit(f"MAILGUN_API_KEY non impostata: mettila in {clg_mail.ENV_FILE} "
                 f"(vedi deploy/README.md, sezione \"Email di verifica\")")
    print(f"invio a {args.destinatario} da {cfg['mail_from']} "
          f"tramite {cfg['api_base']}/{cfg['domain']} ...")
    try:
        clg_mail.invia(args.destinatario, "Prova di invio dall'autenticatore",
                       "Se leggi questa email, l'invio dei codici di verifica funziona.")
    except clg_mail.MailError as e:
        sys.exit(f"invio NON riuscito: {e}\n"
                 f"Controlla chiave e dominio in {clg_mail.ENV_FILE} e, su Mailgun, "
                 f"che il dominio sia verificato e la regione sia quella dell'endpoint.")
    print("inviata: controlla la casella (anche la posta indesiderata)")


def main():
    p = argparse.ArgumentParser(description="Lista codici CLG")
    p.add_argument("--db", default=DB_PATH, help=f"database (default {DB_PATH})")
    sub = p.add_subparsers(dest="comando", required=True)

    imp = sub.add_parser("importa", help="carica una lista di codici (txt, csv, xlsx, xlsm)")
    imp.add_argument("file")
    imp.add_argument("--lotto", default=None, help="etichetta del lotto")
    imp.add_argument("--stato", default=None, choices=STATI,
                     help="stato per le righe che non lo indicano (senza: valid per "
                          "i codici nuovi, invariato per quelli gia' in lista)")
    imp.add_argument("--codice-da", dest="codice_da", default="barcode",
                     choices=("barcode", "colonna"),
                     help="solo Excel: codice dal DataMatrix (default, con ripiego "
                          "sulla colonna E) oppure sempre dalla colonna E")
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

    dm = sub.add_parser("datamatrix", help="genera e registra il DataMatrix di un codice caricato a mano")
    dm.add_argument("codice")
    dm.add_argument("--articolo", default=None)
    dm.add_argument("--variante", default=None)
    dm.add_argument("--taglia", default=None)
    dm.add_argument("--identificativo", default=None)
    dm.add_argument("--out", default=None, help="file da scrivere (.png, oppure .svg per la stampa)")
    dm.add_argument("--sostituisci", action="store_true",
                    help="sostituisce un barcode gia' registrato (i cartellini stampati non varranno piu')")
    dm.set_defaults(func=cmd_datamatrix)

    dmm = sub.add_parser("datamatrix-mancanti",
                         help="un DataMatrix (PNG e SVG) per ogni codice senza barcode, in una cartella")
    dmm.add_argument("cartella")
    dmm.set_defaults(func=cmd_datamatrix_mancanti)

    mt = sub.add_parser("mail-test", help="invia una email di prova con Mailgun")
    mt.add_argument("destinatario")
    mt.set_defaults(func=cmd_mail_test)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
