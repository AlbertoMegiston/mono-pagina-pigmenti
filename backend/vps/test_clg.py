#!/usr/bin/env python3
"""
Test del backend: schema e migrazione, lettura dell'Excel del brand, regola
di verifica con scan, importazione da clgadmin e dal pannello.

    cd backend/vps && python3 -m unittest -v test_clg

Nessun servizio in ascolto e' richiesto: il database e' temporaneo (CLG_DB e
CLG_SALT_FILE vengono impostate prima di importare i moduli, che le leggono
all'import) e il server di verifica viene avviato su una porta libera solo
per i test HTTP. I test sull'Excel reale saltano se il file non c'e' o se
Pillow/zxing-cpp non sono installati; il confronto con la regola JavaScript
della pagina salta senza node.
"""

import base64
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from argparse import Namespace
from http.server import ThreadingHTTPServer

QUI = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="clg-test-")
DB = os.path.join(TMP, "clg.db")
os.environ["CLG_DB"] = DB
os.environ["CLG_SALT_FILE"] = os.path.join(TMP, "ip-salt")
sys.path.insert(0, QUI)

import admin_server  # noqa: E402
import clg_excel  # noqa: E402
import clg_import  # noqa: E402
import verify_server  # noqa: E402

EXCEL = os.path.normpath(os.path.join(QUI, "..", "..", "riferimenti",
                                      "Barcode_DataMatrix_Office2019.xlsm"))
# Mappa (foglio, riga, testo decodificato) estratta a parte: se indicata,
# controlliamo che i payload letti da noi la contengano.
RIFERIMENTO = os.environ.get("CLG_TEST_RIFERIMENTO", "")
HA_EXCEL = os.path.exists(EXCEL)
DECODIFICA = clg_excel.DECODIFICA_DISPONIBILE
# La terza copia della regola di estrazione e' in JavaScript, nella pagina:
# con node a disposizione la eseguiamo sugli stessi casi.
PAGINA = os.path.normpath(os.path.join(QUI, "..", "..", "code", "index.html"))
NODE = shutil.which("node")

# Schema della prima versione, senza le colonne del DataMatrix.
SCHEMA_VECCHIO = """
CREATE TABLE codes (
  code TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'suspicious', 'revoked')),
  batch TEXT, note TEXT, created_at INTEGER NOT NULL);
CREATE TABLE checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, outcome TEXT NOT NULL,
  ts INTEGER NOT NULL, ip_hash TEXT, ctx TEXT);
CREATE INDEX checks_code_ts ON checks (code, ts);
"""

PAYLOAD_ESEMPIO = "L1S156100062S0051-V0024-M-99PROI20250017229-739 184 173 203"
PNG_BIANCO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGP8//8/AwwwMSAB3BwAlm4DBfIlvvkAAAAASUVORK5CYII=")


def db_nuovo():
    for suffisso in ("", "-wal", "-shm"):
        try:
            os.remove(DB + suffisso)
        except FileNotFoundError:
            pass
    verify_server.init_db()


def db_vecchio():
    for suffisso in ("", "-wal", "-shm"):
        try:
            os.remove(DB + suffisso)
        except FileNotFoundError:
            pass
    with sqlite3.connect(DB) as cx:
        cx.executescript(SCHEMA_VECCHIO)


def colonne(tabella):
    with sqlite3.connect(DB) as cx:
        return [r[1] for r in cx.execute("PRAGMA table_info(%s)" % tabella)]


def query(sql, par=()):
    with sqlite3.connect(DB) as cx:
        return cx.execute(sql, par).fetchall()


def zip_di(voci, compressione=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compressione) as z:
        for nome, dati in voci.items():
            z.writestr(nome, dati)
    return buf.getvalue()


def con_voce(dati_zip, nome, nuovo):
    """Lo stesso zip con una voce sostituita (o aggiunta)."""
    voci = {}
    with zipfile.ZipFile(io.BytesIO(dati_zip)) as z:
        for n in z.namelist():
            voci[n] = z.read(n)
    voci[nome] = nuovo
    return zip_di(voci)


def importa_cli(file, **kw):
    args = Namespace(db=DB, file=file, lotto=kw.get("lotto", "test"),
                     stato=kw.get("stato"), codice_da=kw.get("codice_da", "barcode"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        clg_import.cmd_importa(args)
    return out.getvalue()


def xlsx_sintetico(celle, ancore, altezze=None):
    """Un .xlsx minimo: un foglio, celle inline ({"E1": "..."}), immagini
    ancorate come [(col0, riga0, off0, riga1, off1, png)] e un pulsante
    (xdr:sp) in H1 come nel file del brand, che va ignorato."""
    NS_M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    NS_P = "http://schemas.openxmlformats.org/package/2006/relationships"
    NS_X = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    altezze = altezze or {}
    per_riga = {}
    for rif, val in celle.items():
        n = int("".join(ch for ch in rif if ch.isdigit()))
        per_riga.setdefault(n, []).append((rif, val))
    righe_xml = ""
    for n in sorted(set(per_riga) | set(altezze)):
        ht = ' ht="%s" customHeight="1"' % altezze[n] if n in altezze else ""
        cs = "".join('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (rif, val)
                     for rif, val in per_riga.get(n, []))
        righe_xml += '<row r="%d"%s>%s</row>' % (n, ht, cs)
    sheet = ('<worksheet xmlns="%s" xmlns:r="%s"><sheetFormatPr defaultRowHeight="14.4"/>'
             '<sheetData>%s</sheetData><drawing r:id="rId1"/></worksheet>'
             % (NS_M, NS_R, righe_xml))

    def anc(col, r0, off0, r1, off1, corpo):
        return ('<xdr:twoCellAnchor><xdr:from><xdr:col>%d</xdr:col><xdr:colOff>0</xdr:colOff>'
                '<xdr:row>%d</xdr:row><xdr:rowOff>%d</xdr:rowOff></xdr:from>'
                '<xdr:to><xdr:col>%d</xdr:col><xdr:colOff>0</xdr:colOff>'
                '<xdr:row>%d</xdr:row><xdr:rowOff>%d</xdr:rowOff></xdr:to>%s'
                '<xdr:clientData/></xdr:twoCellAnchor>' % (col, r0, off0, col, r1, off1, corpo))

    pulsante = anc(7, 0, 0, 0, 381000,
                   '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="2" name="btn"/><xdr:cNvSpPr/></xdr:nvSpPr>'
                   '<xdr:spPr/></xdr:sp>')
    pics, rels, media = "", "", {}
    for i, (col, r0, off0, r1, off1, png) in enumerate(ancore, start=1):
        pics += anc(col, r0, off0, r1, off1,
                    '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="%d" name="DM_%d"/><xdr:cNvPicPr/>'
                    '</xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId%d"/></xdr:blipFill>'
                    '<xdr:spPr/></xdr:pic>' % (10 + i, i, i))
        rels += ('<Relationship Id="rId%d" Type="%s/image" Target="../media/image%d.png"/>'
                 % (i, NS_R, i))
        media["xl/media/image%d.png" % i] = png
    drawing = '<xdr:wsDr xmlns:xdr="%s" xmlns:a="%s" xmlns:r="%s">%s%s</xdr:wsDr>' % (
        NS_X, NS_A, NS_R, pulsante, pics)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="png" ContentType="image/png"/>'
                   '<Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                   '<sheet name="prova" sheetId="1" r:id="rId1"/></sheets></workbook>' % (NS_M, NS_R))
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="%s"><Relationship Id="rId1" '
                   'Type="%s/worksheet" Target="worksheets/sheet1.xml"/></Relationships>' % (NS_P, NS_R))
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/worksheets/_rels/sheet1.xml.rels", '<Relationships xmlns="%s"><Relationship '
                   'Id="rId1" Type="%s/drawing" Target="../drawings/drawing1.xml"/></Relationships>'
                   % (NS_P, NS_R))
        z.writestr("xl/drawings/drawing1.xml", drawing)
        z.writestr("xl/drawings/_rels/drawing1.xml.rels",
                   '<Relationships xmlns="%s">%s</Relationships>' % (NS_P, rels))
        for nome, png in media.items():
            z.writestr(nome, png)
    return buf.getvalue()


class TestEstrazioneCodice(unittest.TestCase):
    CASI = [
        (PAYLOAD_ESEMPIO, "739184173203"),
        ("99PROI20250017229", ""),
        ("https://crtilogo.com/?clg=123456789012", "123456789012"),
        ("1234567890123", ""),
        ("123.456.789.012", "123456789012"),
        ("123456789012", "123456789012"),
        ("a 111 222 333 444 b 555 666 777 888", "555666777888"),
        ("x1234 567 890 123", ""),
        # Il doppio spazio spezza il secondo gruppo: vale il primo.
        ("739 184 173 203 x 555  666 777 888", "739184173203"),
        # Cifre non ASCII (arabe, a larghezza piena): per JavaScript non sono
        # cifre, quindi nemmeno per noi (re.ASCII).
        ("clg=\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660\u0661\u0662", ""),
        ("x\u0661\u0662\u0663 \u0664\u0665\u0666 \u0667\u0668\u0669 \u0660\u0661\u0662", ""),
        ("\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10\uff11\uff12", ""),
        ("\u0663123456789012", "123456789012"),
        ("clg=\u0663123456789012", "123456789012"),
        ("", ""),
        (None, ""),
    ]

    def test_esempi_della_specifica(self):
        for testo, atteso in self.CASI:
            with self.subTest(testo=testo):
                self.assertEqual(verify_server.extract_code(testo), atteso)

    def test_le_due_implementazioni_coincidono(self):
        # verify_server resta un file autonomo, quindi la regola e' scritta
        # due volte: qui ci accertiamo che non divergano.
        for testo, _ in self.CASI:
            with self.subTest(testo=testo):
                self.assertEqual(clg_excel.estrai_codice(testo), verify_server.extract_code(testo))

    @unittest.skipUnless(NODE and os.path.exists(PAGINA), "servono node e code/index.html")
    def test_coincide_con_la_pagina(self):
        with open(PAGINA, encoding="utf-8") as f:
            m = re.search(r"function extractCode\(text\) \{.*?\n  \}\n", f.read(), re.S)
        self.assertIsNotNone(m, "extractCode non trovata nella pagina")
        casi = [t for t, _ in self.CASI]
        script = (m.group(0) +
                  "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(extractCode)));")
        out = subprocess.run([NODE, "-e", script, json.dumps(casi)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [verify_server.extract_code(t) for t in casi])

    def test_normalizzazione(self):
        self.assertEqual(verify_server.normalize_payload("  a \n b\t\tc "), "a b c")
        self.assertEqual(clg_excel.normalizza_payload("a  b"), verify_server.normalize_payload("a  b"))


class TestMigrazione(unittest.TestCase):
    def test_schema_vecchio_viene_migrato(self):
        db_vecchio()
        with sqlite3.connect(DB) as cx:
            cx.execute("INSERT INTO codes (code, status, batch, created_at) VALUES ('123456789012','revoked','v1',1)")
            cx.execute("INSERT INTO checks (code, outcome, ts) VALUES ('123456789012','fake',1)")
        self.assertNotIn("payload_norm", colonne("codes"))
        verify_server.init_db()
        for c in ("payload", "payload_norm", "article", "variant", "size", "internal_id", "sheet"):
            self.assertIn(c, colonne("codes"))
        self.assertIn("scanned", colonne("checks"))
        self.assertIn("payload_ok", colonne("checks"))
        self.assertTrue(query("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_codes_payload'"))
        # I dati esistenti restano, con i default per le colonne nuove.
        self.assertEqual(query("SELECT status, batch, payload FROM codes"), [("revoked", "v1", None)])
        self.assertEqual(query("SELECT scanned, payload_ok FROM checks"), [(0, None)])
        # Rilanciare la migrazione non fa danni.
        verify_server.init_db()
        self.assertEqual(colonne("codes").count("payload"), 1)

    def test_schema_nuovo_e_identico_a_quello_migrato(self):
        db_vecchio()
        verify_server.init_db()
        migrato = (set(colonne("codes")), set(colonne("checks")))
        db_nuovo()
        self.assertEqual((set(colonne("codes")), set(colonne("checks"))), migrato)

    def test_clgadmin_e_pannello_assicurano_le_colonne(self):
        db_vecchio()
        cx = sqlite3.connect(DB)
        clg_import.assicura_colonne(cx)
        cx.close()
        self.assertIn("payload_norm", colonne("codes"))


class TestExcelSintetico(unittest.TestCase):
    """Casi che non dipendono dal file del brand."""

    def _png_datamatrix(self):
        if not (HA_EXCEL and DECODIFICA):
            return PNG_BIANCO, None
        riga = next(r for r in clg_excel.leggi_excel(EXCEL, decodifica=False)
                    if r["foglio"] == "6100062" and r["riga"] == 2)
        return riga["immagine"], clg_excel.decodifica_immagine(riga["immagine"])[0]

    def test_immagine_in_colonna_diversa_e_pulsante_ignorato(self):
        png, atteso = self._png_datamatrix()
        dati = xlsx_sintetico({"A1": "ART-", "B1": "V1-", "C1": "S-", "D1": "ID1-", "E1": "000 000 000 001"},
                              [(7, 0, 0, 0, 781050, png)], {1: 64.5})
        righe = clg_excel.leggi_excel(dati)
        self.assertEqual(len(righe), 1)
        r = righe[0]
        self.assertEqual((r["foglio"], r["riga"], r["colonna_immagine"]), ("prova", 1, "H"))
        self.assertEqual(r["immagine"], png)
        if atteso:
            self.assertEqual(r["payload"], atteso)
        an = clg_excel.analizza(righe)[0]
        self.assertEqual((an["article"], an["variant"], an["size"], an["internal_id"]),
                         ("ART", "V1", "S", "ID1"))
        self.assertEqual(an["codice_colonna"], "000000000001")
        if atteso:
            self.assertEqual(an["code"], clg_excel.estrai_codice(atteso))
            self.assertTrue(an["discordante"])

    def test_due_immagini_vince_la_colonna_del_barcode(self):
        dati = xlsx_sintetico({"E1": "000 000 000 001"},
                              [(7, 0, 0, 0, 781050, PNG_BIANCO), (5, 0, 0, 0, 781050, PNG_BIANCO + b"x")])
        r = clg_excel.leggi_excel(dati, decodifica=False)[0]
        self.assertEqual(r["colonna_immagine"], "F")
        self.assertEqual(r["immagine"], PNG_BIANCO + b"x")

    def test_immagine_a_cavallo_va_nella_riga_dove_sta_per_la_parte_maggiore(self):
        # Come nel file del brand: ancorata alla riga 1 con uno scostamento
        # quasi pari all'altezza (64.5 pt = 819150 EMU), finisce nella riga 2.
        dati = xlsx_sintetico({"E1": "000 000 000 001", "E2": "000 000 000 002"},
                              [(5, 0, 0, 0, 781050, PNG_BIANCO),
                               (5, 0, 815339, 1, 781049, PNG_BIANCO + b"2")],
                              {1: 64.5, 2: 64.5})
        righe = clg_excel.leggi_excel(dati, decodifica=False)
        self.assertEqual([(r["riga"], r["immagine"]) for r in righe],
                         [(1, PNG_BIANCO), (2, PNG_BIANCO + b"2")])

    def test_riga_senza_immagine_e_senza_codice_viene_saltata(self):
        dati = xlsx_sintetico({"A1": "x", "E2": "000 000 000 002"}, [])
        righe = clg_excel.leggi_excel(dati, decodifica=False)
        self.assertEqual([r["riga"] for r in righe], [2])
        self.assertEqual(righe[0]["motivo"], "nessuna immagine")

    def test_origine_colonna_e_ripiego(self):
        dati = xlsx_sintetico({"E1": "111 222 333 444", "E2": "abc"}, [(5, 1, 0, 1, 781050, PNG_BIANCO)])
        righe = clg_excel.leggi_excel(dati, decodifica=False)
        an = clg_excel.analizza(righe, "barcode")
        self.assertEqual([r["code"] for r in an], ["111222333444", ""])
        self.assertEqual([r["valido"] for r in an], [True, False])
        rp = clg_excel.riepilogo(an)
        self.assertEqual((rp["totale"], rp["validi"], rp["scartati"], rp["senza_immagine"]), (2, 1, 1, 1))
        self.assertEqual(clg_excel.analizza(righe, "colonna")[0]["code"], "111222333444")
        with self.assertRaises(ValueError):
            clg_excel.analizza(righe, "altro")

    def test_protezioni(self):
        with self.assertRaises(clg_excel.ExcelNonValido):
            clg_excel.leggi_excel(b"non sono uno zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.txt", "x")
        with self.assertRaises(clg_excel.ExcelNonValido):  # manca xl/workbook.xml
            clg_excel.leggi_excel(buf.getvalue())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(2001):
                z.writestr("xl/v%d" % i, "")
        with self.assertRaisesRegex(clg_excel.ExcelNonValido, "troppe voci"):
            clg_excel.leggi_excel(buf.getvalue())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", "<x/>")
            z.writestr("xl/grande", b"\0" * (51 * 1024 * 1024))
        with self.assertRaisesRegex(clg_excel.ExcelNonValido, "troppo grande"):
            clg_excel.leggi_excel(buf.getvalue())

    def test_xml_malformato_o_voce_corrotta(self):
        # Zip valido ma con una parte rovinata (upload interrotto, file
        # danneggiato): errore pulito, non un ParseError/BadZipFile che
        # risale fino al pannello o a clgadmin.
        buono = xlsx_sintetico({"E1": "111 222 333 444"}, [])
        rotti = {
            "workbook troncato": zip_di({"xl/workbook.xml": "<workbook><sheets><sheet"}),
            "foglio troncato": con_voce(buono, "xl/worksheets/sheet1.xml", "<worksheet><sheetData><row"),
            "sharedStrings troncato": con_voce(buono, "xl/sharedStrings.xml", "<sst><si><t>oops"),
            "rels rotto": con_voce(buono, "xl/_rels/workbook.xml.rels", "<Relationships"),
            # CRC errato: zip non compresso con un byte cambiato nel contenuto.
            "CRC errato": zip_di({"xl/workbook.xml": "<workbook/>"}, zipfile.ZIP_STORED)
                          .replace(b"<workbook/>", b"<workbooX/>"),
        }
        for nome, dati in rotti.items():
            with self.subTest(nome):
                with self.assertRaisesRegex(clg_excel.ExcelNonValido, "non leggibile"):
                    clg_excel.leggi_excel(dati)
                risp = admin_server.importa_file("lista.xlsx", dati, "barcode", "valid", False, "", True)
                self.assertIn("File non leggibile", risp.get("errore", ""))
        percorso = os.path.join(TMP, "rotto.xlsx")
        with open(percorso, "wb") as f:
            f.write(rotti["workbook troncato"])
        db_nuovo()
        with self.assertRaises(SystemExit) as cm:
            importa_cli(percorso)
        self.assertIn("file non leggibile", str(cm.exception))
        # Controllo: lo stesso zip integro si legge.
        self.assertEqual(len(clg_excel.leggi_excel(buono, decodifica=False)), 1)

    def test_cifre_non_ascii_non_sono_un_codice(self):
        # La pagina tiene solo 0-9: un codice con cifre a larghezza piena non
        # potrebbe mai essere verificato, quindi non entra in lista (ne'
        # dall'Excel ne' dal testo).
        dati = xlsx_sintetico({"E1": "\uff11\uff12\uff13 \uff14\uff15\uff16 \uff17\uff18\uff19 \uff10\uff11\uff12",
                               "E2": "123 456 789 012"}, [])
        an = clg_excel.analizza(clg_excel.leggi_excel(dati, decodifica=False))
        self.assertEqual([(r["code"], r["valido"]) for r in an], [("", False), ("123456789012", True)])
        db_nuovo()
        self.assertEqual(admin_server.importa("\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10\uff11\uff12\n",
                                              "valid", False)["saltati"], 1)
        self.assertEqual(query("SELECT COUNT(*) FROM codes"), [(0,)])

    def test_e_excel(self):
        self.assertTrue(clg_excel.e_excel("lista.XLSM"))
        self.assertTrue(clg_excel.e_excel("lista.bin", b"PK\x03\x04..."))
        self.assertFalse(clg_excel.e_excel("lista.csv", b"code\n1234"))


@unittest.skipUnless(HA_EXCEL, "Excel del brand non presente in riferimenti/")
class TestExcelReale(unittest.TestCase):
    def test_lettura_senza_decodifica(self):
        righe = clg_excel.leggi_excel(EXCEL, decodifica=False)
        self.assertEqual(len(righe), 95)
        self.assertEqual(sorted({r["foglio"] for r in righe}), ["2100027", "6100001", "6100060", "6100062"])
        # Ogni riga ha la sua immagine: le "senza immagine" della prima
        # estrazione erano un artefatto degli anchor a cavallo di due righe.
        self.assertEqual(sum(1 for r in righe if r["immagine"]), 95)
        self.assertEqual({r["colonna_immagine"] for r in righe}, {"F"})
        self.assertEqual({r["riga"] for r in righe if r["riga"] == 1}, {1})
        self.assertEqual(len({r["immagine"] for r in righe}), 95)  # nessun doppione
        self.assertTrue(all(r["payload"] is None for r in righe))

    @unittest.skipUnless(DECODIFICA, "Pillow/zxing-cpp non installati")
    def test_decodifica_e_analisi(self):
        righe, rp = clg_excel.analizza_file(EXCEL)
        self.assertEqual(rp["totale"], 95)
        self.assertEqual(rp["validi"], 95)
        self.assertEqual(rp["con_payload"], 95)
        self.assertEqual(rp["senza_immagine"], 0)
        self.assertEqual(rp["non_decodificabili"], 0)
        self.assertEqual(rp["discordanti"], 90)
        self.assertEqual(rp["fogli"], ["6100062", "6100001", "6100060", "2100027"])
        self.assertEqual(len({r["code"] for r in righe}), 95)
        for r in righe:
            with self.subTest(foglio=r["foglio"], riga=r["riga"]):
                # Il DataMatrix e' A+B+C+D+codice: taglia e identificativo
                # dentro al payload devono essere quelli della riga (e' la
                # prova che l'immagine e' stata attribuita alla riga giusta).
                self.assertIn("-%s-%s-" % (r["size"], r["internal_id"]), r["payload"])
                self.assertEqual(r["code"], r["codice_barcode"])
                self.assertEqual(r["formato"], "data_matrix")
                self.assertFalse(r["article"].endswith("-"))
        r2 = next(r for r in righe if r["foglio"] == "6100062" and r["riga"] == 2)
        self.assertEqual(r2["payload"], PAYLOAD_ESEMPIO)
        self.assertEqual((r2["code"], r2["codice_colonna"], r2["discordante"]),
                         ("739184173203", "678104772975", True))
        self.assertEqual((r2["article"], r2["variant"], r2["size"], r2["internal_id"]),
                         ("L1S156100062S0051", "V0024", "M", "99PROI20250017229"))
        # La prima riga di ogni foglio ha E costante (non formula): concorda.
        for r in righe:
            if r["riga"] == 1:
                self.assertFalse(r["discordante"])

    @unittest.skipUnless(DECODIFICA and RIFERIMENTO and os.path.exists(RIFERIMENTO),
                         "mappa di riferimento non indicata (CLG_TEST_RIFERIMENTO)")
    def test_contro_la_mappa_di_riferimento(self):
        righe, _ = clg_excel.analizza_file(EXCEL)
        with open(RIFERIMENTO, encoding="utf-8") as f:
            rif = json.load(f)
        attesi = {v for r in rif for v in (r.get("barcodes") or {}).values() if v}
        self.assertGreaterEqual(len(attesi), 91)
        self.assertTrue(attesi <= {r["payload"] for r in righe})

    @unittest.skipUnless(DECODIFICA, "Pillow/zxing-cpp non installati")
    def test_origine_colonna(self):
        righe, rp = clg_excel.analizza_file(EXCEL, origine_codice="colonna")
        self.assertEqual(rp["validi"], 95)
        self.assertTrue(all(r["code"] == r["codice_colonna"] for r in righe))
        self.assertEqual(rp["con_payload"], 95)  # il barcode si salva comunque


@unittest.skipUnless(HA_EXCEL and DECODIFICA, "servono l'Excel del brand e Pillow/zxing-cpp")
class TestImportazione(unittest.TestCase):
    def setUp(self):
        db_nuovo()

    def test_clgadmin_importa_excel(self):
        out = importa_cli(EXCEL, lotto="lotto-A")
        self.assertIn("importati: 95   aggiornati: 0   scartati: 0", out)
        self.assertIn("discordanti (colonna E diversa dal barcode): 90", out)
        self.assertIn("senza immagine: 0", out)
        self.assertEqual(query("SELECT COUNT(*), COUNT(payload_norm) FROM codes"), [(95, 95)])
        riga = query("SELECT status, batch, payload, payload_norm, article, variant, size, "
                     "internal_id, sheet FROM codes WHERE code = '739184173203'")[0]
        self.assertEqual(riga, ("valid", "lotto-A", PAYLOAD_ESEMPIO, PAYLOAD_ESEMPIO,
                                "L1S156100062S0051", "V0024", "M", "99PROI20250017229", "6100062"))
        # Secondo passaggio: tutto aggiornato, niente doppioni.
        out = importa_cli(EXCEL, lotto="lotto-B")
        self.assertIn("importati: 0   aggiornati: 95", out)
        self.assertEqual(query("SELECT COUNT(*) FROM codes"), [(95,)])
        self.assertEqual(query("SELECT batch FROM codes WHERE code = '739184173203'"), [("lotto-B",)])

    def test_reimportare_non_cambia_lo_stato_se_non_indicato(self):
        importa_cli(EXCEL)
        with sqlite3.connect(DB) as cx:
            cx.execute("UPDATE codes SET status = 'revoked' WHERE code = '739184173203'")
        importa_cli(EXCEL)
        self.assertEqual(query("SELECT status FROM codes WHERE code = '739184173203'"), [("revoked",)])
        importa_cli(EXCEL, stato="valid")
        self.assertEqual(query("SELECT status FROM codes WHERE code = '739184173203'"), [("valid",)])

    def test_importare_da_testo_non_cancella_il_barcode(self):
        importa_cli(EXCEL)
        txt = os.path.join(TMP, "lista.txt")
        with open(txt, "w") as f:
            f.write("739184173203\n000000000009\nabc\n")
        out = importa_cli(txt)
        self.assertIn("inseriti: 1   aggiornati: 1   saltati: 1", out)
        self.assertEqual(query("SELECT payload FROM codes WHERE code = '739184173203'"), [(PAYLOAD_ESEMPIO,)])
        self.assertEqual(query("SELECT status, payload FROM codes WHERE code = '000000000009'"),
                         [("valid", None)])

    def test_codice_da_colonna(self):
        importa_cli(EXCEL, codice_da="colonna")
        self.assertEqual(query("SELECT COUNT(*) FROM codes WHERE code = '678104772975'"), [(1,)])
        self.assertEqual(query("SELECT payload FROM codes WHERE code = '678104772975'"), [(PAYLOAD_ESEMPIO,)])

    def test_pannello_anteprima_poi_importa(self):
        with open(EXCEL, "rb") as f:
            dati = f.read()
        ant = admin_server.importa_file("Barcode.xlsm", dati, "barcode", "valid", False, "lotto-P", True)
        self.assertTrue(ant.get("ok"), ant)
        self.assertTrue(ant["anteprima"])
        self.assertEqual((ant["totale"], ant["validi"], ant["discordanti"], ant["senza_immagine"],
                          ant["non_decodificabili"]), (95, 95, 90, 0, 0))
        self.assertEqual(len(ant["righe"]), 10)
        self.assertEqual(ant["righe"][1]["codice_barcode"], "739184173203")
        self.assertEqual(ant["righe"][1]["codice_colonna"], "678104772975")
        self.assertEqual(ant["righe"][1]["taglia"], "M")
        self.assertEqual(ant["righe"][1]["articolo"], "L1S156100062S0051")
        self.assertEqual(query("SELECT COUNT(*) FROM codes"), [(0,)])  # niente scritto
        imp = admin_server.importa_file("Barcode.xlsm", dati, "barcode", "valid", False, "lotto-P", False)
        self.assertEqual((imp["nuovi"], imp["aggiornati"]), (95, 0))
        self.assertEqual(query("SELECT COUNT(*), COUNT(payload_norm) FROM codes WHERE batch = 'lotto-P'"),
                         [(95, 95)])
        # Elenco e statistiche mostrano i campi nuovi.
        el = admin_server.elenco_codici("99PROI20250017229")
        self.assertEqual(el["totale"], 1)
        self.assertEqual(el["righe"][0]["taglia"], "M")
        self.assertTrue(el["righe"][0]["barcode"])
        self.assertEqual(admin_server.elenco_codici("", 90)["righe"].__len__(), 5)
        self.assertEqual(admin_server.stato_lista()["con_barcode"], 95)
        # Sostituisci: svuota e ricarica.
        imp = admin_server.importa_file("b.xlsm", dati, "colonna", "suspicious", True, "", False)
        self.assertEqual(query("SELECT COUNT(*) FROM codes WHERE status = 'suspicious' AND batch = 'pannello'"),
                         [(95,)])

    def test_reimportare_dopo_aver_installato_la_decodifica(self):
        # Prima importazione senza Pillow/zxing-cpp: i codici vengono dalla
        # colonna E (casuali). Reimportando con la decodifica attiva i codici
        # dei barcode sono altri: l'upsert non tocca i vecchi, che resterebbero
        # in lista come validi. L'importazione deve dirlo, e "Sostituisci" li
        # toglie.
        vecchio = clg_excel.DECODIFICA_DISPONIBILE
        clg_excel.DECODIFICA_DISPONIBILE = False
        try:
            out = importa_cli(EXCEL)
        finally:
            clg_excel.DECODIFICA_DISPONIBILE = vecchio
        self.assertIn("non decodificabili: 95", out)
        self.assertIn("svuota la lista (clgadmin svuota --conferma) prima di reimportare", out)
        self.assertEqual(query("SELECT COUNT(*), COUNT(payload_norm) FROM codes"), [(95, 0)])
        out = importa_cli(EXCEL)
        self.assertIn("importati: 90   aggiornati: 5", out)
        self.assertEqual(query("SELECT COUNT(*) FROM codes WHERE payload_norm IS NULL"), [(90,)])
        self.assertIn("ATTENZIONE: 90 codici degli stessi fogli restano in lista senza barcode", out)
        with open(EXCEL, "rb") as f:
            dati = f.read()
        ant = admin_server.importa_file("b.xlsm", dati, "barcode", "valid", False, "", True)
        self.assertEqual(ant["residui_senza_barcode"], 90)
        self.assertEqual(query("SELECT COUNT(*) FROM codes"), [(185,)])  # anteprima: niente scritto
        imp = admin_server.importa_file("b.xlsm", dati, "barcode", "valid", True, "", False)
        self.assertEqual(imp["residui_senza_barcode"], 0)
        self.assertEqual(query("SELECT COUNT(*), COUNT(payload_norm) FROM codes"), [(95, 95)])
        # Reimportare una lista pulita non segnala nulla.
        self.assertNotIn("ATTENZIONE", importa_cli(EXCEL))

    def test_pannello_rifiuta_file_non_excel(self):
        self.assertIn("errore", admin_server.importa_file("lista.txt", b"123456789012\n", "barcode",
                                                          "valid", False, "", True))
        self.assertIn("errore", admin_server.importa_file("x.xlsx", b"PK\x03\x04rotto", "barcode",
                                                          "valid", False, "", True))


class TestVerdetti(unittest.TestCase):
    C1, P1 = "739184173203", PAYLOAD_ESEMPIO
    C2, P2 = "169103536752", "L1S156100062S0051-V0024-L-99PROI20250017220-169 103 536 752"
    C3, P3 = "111222333444", "L1S156100062S0051-V0024-XL-99PROI20250017221-111 222 333 444"
    C4 = "555666777888"  # importato da testo: nessun barcode registrato

    def setUp(self):
        db_nuovo()
        with sqlite3.connect(DB) as cx:
            for code, status, p in ((self.C1, "valid", self.P1), (self.C2, "valid", self.P2),
                                    (self.C3, "revoked", self.P3), (self.C4, "valid", None)):
                cx.execute("INSERT INTO codes (code, status, created_at, payload, payload_norm) "
                           "VALUES (?,?,1,?,?)", (code, status, p, p))

    def test_payload_giusto(self):
        self.assertEqual(verify_server.verdict(self.C1, "h1", self.P1), ("genuine", "scan", self.C1, 1))

    def test_payload_di_un_altro_codice_registrato(self):
        # Il payload identifica la riga: il codice inviato non conta.
        self.assertEqual(verify_server.verdict(self.C1, "h1", self.P2), ("genuine", "scan", self.C2, 1))
        self.assertEqual(verify_server.verdict(self.C1, "h1", self.P3), ("fake", "scan", self.C3, 1))

    def test_payload_contraffatto_con_codice_in_lista(self):
        falso = "ALTRO-ARTICOLO-739 184 173 203"
        self.assertEqual(verify_server.verdict(self.C1, "h1", falso), ("fake", "scan", self.C1, 0))
        # E anche se il codice inviato e' un altro.
        self.assertEqual(verify_server.verdict("000000000001", "h1", falso), ("fake", "scan", self.C1, 0))
        # Un codice revocato con barcode registrato: falso comunque.
        self.assertEqual(verify_server.verdict(self.C3, "h1", "X-111 222 333 444"), ("fake", "scan", self.C3, 0))

    def test_codice_senza_barcode_registrato(self):
        # Caricato da testo (come i codici dimostrativi): non c'e' nessun
        # barcode emesso con cui confrontare, quindi scansionare l'etichetta
        # vale come digitare il codice (via scan, payload_ok 0). Prima dava
        # "fake" su un articolo autentico.
        self.assertEqual(verify_server.verdict(self.C4, "h1", "X-555 666 777 888"),
                         ("genuine", "scan", self.C4, 0))
        self.assertEqual(verify_server.verdict("000000000001", "h1", "ART-V1-M-ID1-555 666 777 888"),
                         ("genuine", "scan", self.C4, 0))
        # Le regole per codice valgono anche qui.
        with sqlite3.connect(DB) as cx:
            cx.execute("UPDATE codes SET status = 'suspicious' WHERE code = ?", (self.C4,))
        self.assertEqual(verify_server.verdict(self.C4, "h1", "X-555 666 777 888")[0], "suspicious")

    def test_qr_della_pagina(self):
        # Il QR del pezzo e' l'indirizzo della pagina (?clg=): non e' un
        # barcode contraffatto, nemmeno per un codice con il DataMatrix
        # registrato. Decide il codice nel QR, non quello inviato.
        qr = "https://crtilogo.com/?clg=%s"
        self.assertEqual(verify_server.verdict(self.C1, "h1", qr % self.C1), ("genuine", "scan", self.C1, 0))
        self.assertEqual(verify_server.verdict(self.C4, "h1", qr % self.C4), ("genuine", "scan", self.C4, 0))
        self.assertEqual(verify_server.verdict(self.C3, "h1", qr % self.C3), ("fake", "scan", self.C3, 0))
        self.assertEqual(verify_server.verdict("000000000001", "h1", qr % "000000000001"),
                         ("not_found", "scan", "000000000001", 0))
        self.assertEqual(verify_server.verdict("000000000001", "h1", qr % self.C1), ("genuine", "scan", self.C1, 0))

    def test_candidato_estratto_dal_testo_grezzo(self):
        # La pagina estrae il codice dal testo cosi' com'e' letto: il doppio
        # spazio spezza il secondo gruppo e il codice e' il primo. Il server
        # deve decidere sullo stesso codice, non su quello che emergerebbe
        # dopo la normalizzazione dei bianchi.
        grezzo = "739 184 173 203 x 555  666 777 888"
        self.assertEqual(verify_server.extract_code(grezzo), self.C1)
        self.assertEqual(verify_server.extract_code(verify_server.normalize_payload(grezzo)), self.C4)
        self.assertEqual(verify_server.verdict(self.C1, "h1", grezzo), ("fake", "scan", self.C1, 0))

    def test_payload_sconosciuto_con_codice_sconosciuto(self):
        self.assertEqual(verify_server.verdict("000000000001", "h1", "X-000 000 000 001"),
                         ("not_found", "scan", "000000000001", 0))
        # Payload senza nessun codice dentro: not_found sul codice inviato.
        self.assertEqual(verify_server.verdict("000000000002", "h1", "solo testo"),
                         ("not_found", "scan", "000000000002", 0))

    def test_normalizzazione_degli_spazi(self):
        # A verdict arriva il testo grezzo: il confronto con payload_norm non
        # dipende da come il lettore rende i bianchi.
        grezzo = self.P1.replace(" ", "  ") + "\n"
        self.assertEqual(verify_server.verdict(self.C1, "h1", grezzo), ("genuine", "scan", self.C1, 1))
        p = verify_server.normalize_payload(grezzo)
        self.assertEqual(verify_server.verdict(self.C1, "h1", p)[0], "genuine")

    def test_senza_scan_come_prima(self):
        self.assertEqual(verify_server.verdict(self.C1, "h1"), ("genuine", "code", self.C1, None))
        self.assertEqual(verify_server.verdict(self.C3, "h1"), ("fake", "code", self.C3, None))
        self.assertEqual(verify_server.verdict("000000000001", "h1"), ("not_found", "code", "000000000001", None))

    def test_troppi_dispositivi_anche_con_scan(self):
        vecchio = verify_server.DUP_LIMIT
        verify_server.DUP_LIMIT = 2
        try:
            for h in ("a", "b", "c"):
                verify_server.log_check(self.C1, "genuine", h, None, 1, 1)
            self.assertEqual(verify_server.verdict(self.C1, "d", self.P1)[0], "suspicious")
            self.assertEqual(verify_server.verdict(self.C1, "a", self.P1)[0], "suspicious")
        finally:
            verify_server.DUP_LIMIT = vecchio


class TestServerHTTP(unittest.TestCase):
    """Il servizio vero, su una porta libera: contratto della richiesta e
    colonne nuove nello storico."""

    @classmethod
    def setUpClass(cls):
        db_nuovo()
        with sqlite3.connect(DB) as cx:
            cx.execute("INSERT INTO codes (code, status, created_at, payload, payload_norm) VALUES "
                       "('739184173203','valid',1,?,?)", (PAYLOAD_ESEMPIO, PAYLOAD_ESEMPIO))
            cx.execute("INSERT INTO codes (code, status, created_at, payload, payload_norm) VALUES "
                       "('169103536752','valid',1,'P2','P2')")
            # Come i codici dimostrativi o una lista da txt: nessun barcode.
            cx.execute("INSERT INTO codes (code, status, created_at) VALUES ('123456789012','valid',1)")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), verify_server.Handler)
        cls.srv.daemon_threads = True
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/api/verify" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, corpo, ip="10.0.0.1"):
        req = urllib.request.Request(self.url, data=json.dumps(corpo).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-Forwarded-For": ip}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def ultimo_check(self):
        return query("SELECT code, outcome, scanned, payload_ok, ctx FROM checks ORDER BY id DESC LIMIT 1")[0]

    def test_scan_giusto(self):
        st, r = self.post({"code": "739184173203", "scan": {"payload": PAYLOAD_ESEMPIO, "format": "data_matrix"},
                           "context": {"when": "x"}})
        self.assertEqual((st, r), (200, {"outcome": "genuine", "via": "scan"}))
        self.assertEqual(self.ultimo_check(), ("739184173203", "genuine", 1, 1, '{"when": "x"}'))

    def test_scan_contraffatto(self):
        st, r = self.post({"code": "739184173203", "scan": {"payload": "X-739 184 173 203", "format": "qr_code"}})
        self.assertEqual((st, r), (200, {"outcome": "fake", "via": "scan"}))
        self.assertEqual(self.ultimo_check()[:4], ("739184173203", "fake", 1, 0))

    def test_scan_di_codice_senza_barcode_registrato(self):
        # Il DataMatrix o il QR ?clg= di un codice caricato da txt/csv: vale
        # come il codice digitato, non e' un falso.
        for payload, fmt in (("ART-V1-M-ID1-123 456 789 012", "data_matrix"),
                             ("https://crtilogo.com/?clg=123456789012", "qr_code")):
            st, r = self.post({"code": "123456789012", "scan": {"payload": payload, "format": fmt}})
            self.assertEqual((st, r), (200, {"outcome": "genuine", "via": "scan"}), payload)
            self.assertEqual(self.ultimo_check()[:4], ("123456789012", "genuine", 1, 0))

    def test_scan_qr_della_pagina_con_barcode_registrato(self):
        st, r = self.post({"code": "739184173203",
                           "scan": {"payload": "https://crtilogo.com/?clg=739184173203", "format": "qr_code"}})
        self.assertEqual((st, r), (200, {"outcome": "genuine", "via": "scan"}))
        self.assertEqual(self.ultimo_check()[:4], ("739184173203", "genuine", 1, 0))

    def test_candidato_dal_testo_grezzo(self):
        # Corpo identico a quello della pagina: code e' quello che lei ha
        # estratto dal testo grezzo (il doppio spazio spezza il secondo
        # gruppo). Il server decide sullo stesso codice, che ha un barcode
        # registrato diverso: falso. Estraendo dal testo normalizzato avrebbe
        # deciso su 123456789012 (genuine), un codice che la pagina non ha visto.
        grezzo = "739 184 173 203 x 123  456 789 012"
        st, r = self.post({"code": "739184173203", "scan": {"payload": grezzo, "format": "data_matrix"}})
        self.assertEqual((st, r), (200, {"outcome": "fake", "via": "scan"}))
        self.assertEqual(self.ultimo_check()[:4], ("739184173203", "fake", 1, 0))

    def test_scan_di_altra_riga_registra_il_codice_inviato(self):
        st, r = self.post({"code": "739184173203", "scan": {"payload": "P2", "format": "qr_code"}, "context": {}})
        self.assertEqual((st, r), (200, {"outcome": "genuine", "via": "scan"}))
        code, _, scanned, ok, ctx = self.ultimo_check()
        self.assertEqual((code, scanned, ok), ("169103536752", 1, 1))
        self.assertEqual(json.loads(ctx)["code_sent"], "739184173203")

    def test_senza_scan(self):
        st, r = self.post({"code": "739 184 173 203", "scan": None, "context": {"when": "x"}})
        self.assertEqual((st, r), (200, {"outcome": "genuine", "via": "code"}))
        self.assertEqual(self.ultimo_check(), ("739184173203", "genuine", 0, None, '{"when": "x"}'))
        st, r = self.post({"code": "000000000001"})
        self.assertEqual(r, {"outcome": "not_found", "via": "code"})

    def test_scan_ignorato_se_vuoto_o_troppo_lungo(self):
        st, r = self.post({"code": "739184173203", "scan": {"payload": "   "}})
        self.assertEqual(r, {"outcome": "genuine", "via": "code"})
        st, r = self.post({"code": "739184173203", "scan": {"payload": "x" * 600}})
        self.assertEqual(r, {"outcome": "genuine", "via": "code"})
        st, r = self.post({"code": "739184173203", "scan": "non un oggetto"})
        self.assertEqual(r, {"outcome": "genuine", "via": "code"})

    def test_codice_non_valido(self):
        st, r = self.post({"code": "12", "scan": {"payload": PAYLOAD_ESEMPIO}})
        self.assertEqual((st, r), (200, {"outcome": "invalid"}))
        st, r = self.post({"code": None})
        self.assertEqual(r, {"outcome": "invalid"})
        # Cifre a larghezza piena: non sono un codice.
        st, r = self.post({"code": "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10\uff11\uff12"})
        self.assertEqual(r, {"outcome": "invalid"})


if __name__ == "__main__":
    unittest.main()
