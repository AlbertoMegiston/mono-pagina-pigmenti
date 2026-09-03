#!/usr/bin/env python3
"""
Lettura degli Excel del brand (.xlsx/.xlsm) con i DataMatrix dei cartellini.

Ogni riga prodotto ha: A articolo, B variante, C taglia, D identificativo
interno, E codice a 12 cifre ("639 516 540 959"), F l'immagine PNG del
DataMatrix. Il DataMatrix contiene A+B+C+D+codice e' la fonte affidabile: la
colonna E e' una formula casuale che cambia a ogni ricalcolo, quindi in
quasi tutte le righe non coincide con il codice stampato sul cartellino.

Il file e' uno zip di XML: lo leggiamo con la sola libreria standard
(zipfile + xml.etree). Pillow e zxing-cpp servono solo a decodificare le
immagini e sono facoltativi: se mancano, le righe tornano con payload=None e
motivo "decodifica non disponibile", e chi importa ripiega sulla colonna E.

Modulo condiviso da clg_import.py (riga di comando) e admin_server.py
(pannello). Uso tipico:

    righe, riepilogo = analizza_file(dati_o_percorso, origine_codice="barcode")
"""

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

try:
    from PIL import Image
    import zxingcpp
except ImportError:  # dipendenze facoltative: senza, niente decodifica
    Image = None
    zxingcpp = None

DECODIFICA_DISPONIBILE = Image is not None and zxingcpp is not None

# Protezioni contro archivi ostili caricati dal pannello: un Excel di codici
# ha poche decine di voci e pochi MB una volta scompattato.
MAX_VOCI_ZIP = 2000
MAX_BYTE_SCOMPATTATI = 50 * 1024 * 1024

# re.ASCII: in Python \d prende anche le cifre arabe o a larghezza piena, la
# pagina (JavaScript) solo 0-9. Un codice con cifre non ASCII non potrebbe mai
# arrivare da un telefono, quindi non deve nemmeno entrare in lista.
CODE_RE = re.compile(r"^\d{12}$", re.ASCII)
ORIGINI = ("barcode", "colonna")

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


class ExcelNonValido(ValueError):
    """Il file non e' un Excel leggibile (o supera le protezioni)."""


# Tutto cio' che zipfile ed expat sollevano su un archivio danneggiato o
# insolito (XML troncato, CRC errato, flusso deflate corrotto, voce cifrata o
# con compressione non supportata) piu' le conversioni sui valori letti: in
# leggi_excel diventa ExcelNonValido, cosi' pannello e clgadmin rispondono
# "file non leggibile" invece di un traceback.
ERRORI_LETTURA = (ET.ParseError, zipfile.BadZipFile, zlib.error, EOFError,
                  NotImplementedError, RuntimeError, KeyError, ValueError)


# --- regola del codice dentro un payload ------------------------------------
# Deve restare identica a verify_server.extract_code (e alla versione
# JavaScript nella pagina): prima "clg" + al piu' 4 caratteri non numerici +
# 12 cifre (stile URL ?clg=...), altrimenti l'ULTIMO gruppo "ddd ddd ddd ddd"
# (separatori: niente, spazio o punto) non attaccato ad altre cifre.
_CLG_PARAM_RE = re.compile(r"clg\D{0,4}(\d{12})", re.IGNORECASE | re.ASCII)
_GRUPPI_RE = re.compile(r"(?:^|\D)(\d{3})[ .]?(\d{3})[ .]?(\d{3})[ .]?(\d{3})(?!\d)", re.ASCII)


def estrai_codice(testo):
    """Il codice CLG a 12 cifre contenuto in un payload, o "" se non c'e'."""
    if not testo:
        return ""
    m = _CLG_PARAM_RE.search(testo)
    if m:
        return m.group(1)
    ultimo = None
    for ultimo in _GRUPPI_RE.finditer(testo):
        pass
    return "".join(ultimo.groups()) if ultimo else ""


def normalizza_payload(testo):
    """Stessa normalizzazione del server: spazi ripetuti e a capo ridotti a
    uno, cosi' il confronto non dipende da come il lettore rende i bianchi."""
    return " ".join((testo or "").split())


# --- pezzi dello zip ----------------------------------------------------------

def _q(ns, tag):
    return "{%s}%s" % (ns, tag)


def _risolvi(base, target):
    """Percorso di un Target di relazione rispetto alla parte che lo cita
    ("../media/image1.png" da xl/drawings/drawing1.xml → xl/media/image1.png)."""
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def _rels(zf, parte):
    """Relazioni di una parte: {Id: percorso nello zip}. Le relazioni esterne
    (link) non ci interessano."""
    cartella, nome = posixpath.split(parte)
    percorso = posixpath.join(cartella, "_rels", nome + ".rels")
    if percorso not in zf.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(zf.read(percorso)).iter(_q(NS_PKG, "Relationship")):
        if rel.get("TargetMode") == "External":
            continue
        out[rel.get("Id")] = _risolvi(parte, rel.get("Target") or "")
    return out


def _stringhe_condivise(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out = []
    for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(_q(NS_MAIN, "si")):
        # Un testo formattato e' spezzato in piu' run <r><t>: li riuniamo.
        out.append("".join(t.text or "" for t in si.iter(_q(NS_MAIN, "t"))))
    return out


def _numero_a_testo(v):
    """Excel scrive 639516540959 anche come 6.39516540959E+11: riportiamo
    gli interi alla forma piena, il resto lo lasciamo com'e'."""
    if "." in v or "e" in v or "E" in v:
        try:
            f = float(v)
        except ValueError:
            return v
        if f.is_integer():
            return str(int(f))
    return v


def _valore_cella(c, sst):
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(_q(NS_MAIN, "t")))
    v = c.find(_q(NS_MAIN, "v"))
    if v is None or v.text is None:
        return ""
    if t == "s":
        try:
            return sst[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if t in ("str", "b", "e"):
        return v.text
    return _numero_a_testo(v.text)


_RIF_RE = re.compile(r"^([A-Z]+)(\d+)$")
EMU_PER_PUNTO = 12700


def _leggi_foglio(zf, parte, sst):
    """Celle e altezze del foglio: ({riga: {colonna: testo}}, {riga: EMU},
    altezza predefinita in EMU). Le altezze servono a capire in che riga
    cade davvero un'immagine ancorata a cavallo di due righe."""
    root = ET.fromstring(zf.read(parte))
    fmt = root.find(_q(NS_MAIN, "sheetFormatPr"))
    try:
        predefinita = float(fmt.get("defaultRowHeight")) if fmt is not None else 15.0
    except (TypeError, ValueError):
        predefinita = 15.0
    righe, altezze = {}, {}
    for row in root.iter(_q(NS_MAIN, "row")):
        try:
            n = int(row.get("r"))
            if row.get("ht"):
                altezze[n] = int(float(row.get("ht")) * EMU_PER_PUNTO)
        except (TypeError, ValueError):
            continue
        for c in row.iter(_q(NS_MAIN, "c")):
            m = _RIF_RE.match(c.get("r") or "")
            if not m:
                continue
            val = _valore_cella(c, sst)
            if val is None or val == "":
                continue
            righe.setdefault(n, {})[m.group(1)] = str(val).strip()
    return righe, altezze, int(predefinita * EMU_PER_PUNTO)


def _riga_ancora(anc, altezza):
    """Riga (1-based) in cui cade la porzione piu' grande dell'immagine.

    Di norma l'ancora parte e finisce nella stessa riga. Nel file del brand
    pero' alcune immagini sono ancorate alla riga precedente con uno
    scostamento quasi pari all'altezza della riga: visivamente stanno nella
    riga dopo, ed e' quella a cui appartengono (lo confermano taglia e
    identificativo dentro al DataMatrix).
    """
    da = anc.find(_q(NS_XDR, "from"))
    r0 = int(da.find(_q(NS_XDR, "row")).text)
    off0 = int(da.find(_q(NS_XDR, "rowOff")).text or 0)
    a = anc.find(_q(NS_XDR, "to"))
    if a is not None:
        r1 = int(a.find(_q(NS_XDR, "row")).text)
        off1 = int(a.find(_q(NS_XDR, "rowOff")).text or 0)
    else:
        # oneCellAnchor: solo altezza dell'immagine (xdr:ext cy), la riga
        # finale la ricaviamo scendendo lungo le altezze delle righe.
        ext = anc.find(_q(NS_XDR, "ext"))
        resto = int(ext.get("cy") or 0) if ext is not None else 0
        resto -= altezza(r0) - off0
        r1, off1 = r0, 0
        while resto > 0 and r1 - r0 < 1000:
            r1 += 1
            if resto <= altezza(r1):
                off1 = resto
                break
            resto -= altezza(r1)
    if r1 <= r0:
        return r0 + 1
    migliore, porzione = r0, altezza(r0) - off0
    for r in range(r0 + 1, r1):
        if altezza(r) > porzione:
            migliore, porzione = r, altezza(r)
    if off1 > porzione:
        migliore = r1
    return migliore + 1


def lettera_colonna(indice):
    """0 → A, 5 → F, 26 → AA (gli anchor dei disegni contano da zero)."""
    s = ""
    indice += 1
    while indice > 0:
        indice, r = divmod(indice - 1, 26)
        s = chr(65 + r) + s
    return s


def _immagini_foglio(zf, parte_foglio, altezze, predefinita):
    """{riga: {colonna: bytes}} delle immagini ancorate nel foglio. La
    colonna e' quella dell'angolo in alto a sinistra (xdr:from), la riga
    quella in cui l'immagine cade per la parte maggiore."""
    out = {}
    voci = set(zf.namelist())

    def altezza(r0):  # r0 conta da zero, come gli anchor
        return altezze.get(r0 + 1, predefinita)

    for disegno in _rels(zf, parte_foglio).values():
        if not disegno.startswith("xl/drawings/") or disegno not in voci:
            continue
        media = _rels(zf, disegno)
        for anc in ET.fromstring(zf.read(disegno)).iter():
            if not anc.tag.startswith("{%s}" % NS_XDR) or not anc.tag.endswith("Anchor"):
                continue
            da = anc.find(_q(NS_XDR, "from"))
            if da is None:  # absoluteAnchor: nessuna cella di riferimento
                continue
            # Solo le immagini (xdr:pic con blip): i pulsanti delle macro sono
            # xdr:sp e non hanno un'immagine incorporata.
            pic = anc.find(_q(NS_XDR, "pic"))
            blip = pic.find(".//" + _q(NS_A, "blip")) if pic is not None else None
            if blip is None:
                continue
            percorso = media.get(blip.get(_q(NS_REL, "embed")) or "")
            if not percorso or not percorso.startswith("xl/") or percorso not in voci:
                continue
            try:
                col = int(da.find(_q(NS_XDR, "col")).text)
                riga = _riga_ancora(anc, altezza)
            except (AttributeError, TypeError, ValueError):
                continue
            out.setdefault(riga, {}).setdefault(lettera_colonna(col), zf.read(percorso))
    return out


def _apri(dati):
    """Zip dell'Excel da bytes o percorso, con le protezioni di base."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(dati) if isinstance(dati, (bytes, bytearray)) else dati)
    except (zipfile.BadZipFile, OSError) as e:
        raise ExcelNonValido("non e' un file Excel (.xlsx/.xlsm): %s" % e)
    voci = zf.infolist()
    if len(voci) > MAX_VOCI_ZIP:
        raise ExcelNonValido("archivio con troppe voci (%d)" % len(voci))
    if sum(v.file_size for v in voci) > MAX_BYTE_SCOMPATTATI:
        raise ExcelNonValido("archivio troppo grande una volta scompattato")
    if "xl/workbook.xml" not in zf.namelist():
        raise ExcelNonValido("manca xl/workbook.xml: non e' un file Excel")
    return zf


def _fogli(zf):
    """[(nome, parte)] nell'ordine delle linguette."""
    rels = _rels(zf, "xl/workbook.xml")
    out = []
    for sh in ET.fromstring(zf.read("xl/workbook.xml")).iter(_q(NS_MAIN, "sheet")):
        parte = rels.get(sh.get(_q(NS_REL, "id")) or "")
        if parte and parte.startswith("xl/") and parte in zf.namelist():
            out.append((sh.get("name") or "", parte))
    return out


# --- decodifica ---------------------------------------------------------------

def _leggi_barcode(img):
    try:
        return zxingcpp.read_barcodes(
            img, formats=(zxingcpp.BarcodeFormat.DataMatrix, zxingcpp.BarcodeFormat.QRCode))
    except TypeError:  # zxing-cpp 2.x vuole la maschera, non la tupla
        return zxingcpp.read_barcodes(
            img, formats=zxingcpp.BarcodeFormat.DataMatrix | zxingcpp.BarcodeFormat.QRCode)


def decodifica_immagine(dati):
    """Ritorna (payload, formato, motivo): payload e formato ("data_matrix" o
    "qr_code") se l'immagine contiene un barcode, altrimenti il motivo."""
    if not DECODIFICA_DISPONIBILE:
        return None, None, "decodifica non disponibile"
    try:
        img = Image.open(io.BytesIO(dati))
        img.load()
    except Exception:  # Pillow solleva classi diverse a seconda del formato
        return None, None, "immagine non leggibile"
    # Le PNG del brand sono in modalita' palette con trasparenza: i moduli neri
    # stanno su fondo trasparente, che va appiattito su bianco, altrimenti il
    # decodificatore vede nero su nero.
    rgba = img.convert("RGBA")
    fondo = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    fondo.alpha_composite(rgba)
    try:
        trovati = _leggi_barcode(fondo.convert("L"))
    except Exception as e:  # pragma: no cover - dipende dalla libreria nativa
        return None, None, "errore di decodifica: %s" % e
    for r in trovati:
        if not getattr(r, "valid", True) or not r.text:
            continue
        formato = "data_matrix" if r.format == zxingcpp.BarcodeFormat.DataMatrix else "qr_code"
        return r.text, formato, None
    return None, None, "nessun barcode riconosciuto"


# --- lettura e analisi --------------------------------------------------------

def leggi_excel(dati, colonna_codice="E", colonna_barcode="F", decodifica=True):
    """Le righe con un codice o un'immagine, foglio per foglio.

    Ogni riga e' un dict: foglio, riga, A, B, C, D, E (le celle grezze),
    codice_cella (il testo nella colonna del codice), immagine (bytes o None),
    colonna_immagine, payload, formato, motivo. Se la riga ha una sola
    immagine la usiamo qualunque sia la colonna (nel file del brand la prima
    riga di ogni foglio puo' averla spostata); se ne ha piu' d'una vale
    quella nella colonna del barcode.
    """
    zf = _apri(dati)
    # _apri controlla solo che sia uno zip con xl/workbook.xml: le parti
    # dentro possono comunque essere troncate o corrotte (upload interrotto,
    # file rovinato in transito), e i chiamanti gestiscono solo ExcelNonValido.
    try:
        return _leggi_parti(zf, colonna_codice, colonna_barcode, decodifica)
    except ERRORI_LETTURA as e:
        raise ExcelNonValido("contenuto non leggibile: %s" % e)


def _leggi_parti(zf, colonna_codice, colonna_barcode, decodifica):
    sst = _stringhe_condivise(zf)
    righe = []
    for nome, parte in _fogli(zf):
        celle, altezze, predefinita = _leggi_foglio(zf, parte, sst)
        immagini = _immagini_foglio(zf, parte, altezze, predefinita)
        for n in sorted(set(celle) | set(immagini)):
            c = celle.get(n, {})
            imgs = immagini.get(n, {})
            codice_cella = c.get(colonna_codice, "")
            if not codice_cella and not imgs:
                continue
            col_img = None
            if len(imgs) == 1:
                col_img = next(iter(imgs))
            elif colonna_barcode in imgs:
                col_img = colonna_barcode
            riga = {
                "foglio": nome, "riga": n,
                "A": c.get("A", ""), "B": c.get("B", ""), "C": c.get("C", ""),
                "D": c.get("D", ""), "E": c.get("E", ""),
                "codice_cella": codice_cella,
                "immagine": imgs.get(col_img) if col_img else None,
                "colonna_immagine": col_img,
                "payload": None, "formato": None, "motivo": None,
            }
            if riga["immagine"] is None:
                riga["motivo"] = "nessuna immagine"
            elif decodifica:
                riga["payload"], riga["formato"], riga["motivo"] = \
                    decodifica_immagine(riga["immagine"])
            righe.append(riga)
    return righe


def _senza_trattino(s):
    return re.sub(r"-+$", "", (s or "").strip())


def analizza(righe, origine_codice="barcode"):
    """Da ogni riga letta ricava il codice da importare e i campi del prodotto.

    origine_codice="barcode": codice dal payload; se manca l'immagine o non
    si decodifica, ripiego sulla colonna E. "colonna": sempre E (il payload
    viene comunque conservato). Il flag "discordante" segnala che E e il
    barcode portano codici diversi.
    """
    if origine_codice not in ORIGINI:
        raise ValueError("origine_codice deve essere una di %s" % (ORIGINI,))
    out = []
    for r in righe:
        da_colonna = re.sub(r"\D+", "", r["codice_cella"] or "", flags=re.ASCII)
        if not CODE_RE.match(da_colonna):
            da_colonna = ""
        da_barcode = estrai_codice(r["payload"]) if r["payload"] else ""
        if origine_codice == "barcode":
            code = da_barcode or da_colonna
        else:
            code = da_colonna
        out.append({
            "foglio": r["foglio"], "riga": r["riga"],
            "code": code,
            "valido": bool(code),
            "payload": r["payload"],
            "payload_norm": normalizza_payload(r["payload"]) if r["payload"] else None,
            "formato": r["formato"],
            "article": _senza_trattino(r["A"]),
            "variant": _senza_trattino(r["B"]),
            "size": _senza_trattino(r["C"]),
            "internal_id": _senza_trattino(r["D"]),
            "sheet": r["foglio"],
            "codice_colonna": da_colonna,
            "codice_barcode": da_barcode,
            "discordante": bool(da_colonna and da_barcode and da_colonna != da_barcode),
            "immagine": r["immagine"] is not None,
            "motivo": r["motivo"],
        })
    return out


def riepilogo(analizzate):
    """I conteggi mostrati dopo un'importazione o un'anteprima."""
    fogli = []
    for r in analizzate:
        if r["foglio"] not in fogli:
            fogli.append(r["foglio"])
    return {
        "totale": len(analizzate),
        "validi": sum(1 for r in analizzate if r["valido"]),
        "scartati": sum(1 for r in analizzate if not r["valido"]),
        "discordanti": sum(1 for r in analizzate if r["discordante"]),
        "senza_immagine": sum(1 for r in analizzate if not r["immagine"]),
        "non_decodificabili": sum(1 for r in analizzate if r["immagine"] and not r["payload"]),
        "con_payload": sum(1 for r in analizzate if r["payload"]),
        "fogli": fogli,
        "decodifica_disponibile": DECODIFICA_DISPONIBILE,
    }


def analizza_file(dati, origine_codice="barcode", colonna_codice="E", colonna_barcode="F"):
    """Lettura + analisi in un colpo solo: (righe analizzate, riepilogo)."""
    righe = analizza(leggi_excel(dati, colonna_codice, colonna_barcode), origine_codice)
    return righe, riepilogo(righe)


def e_excel(nome, dati=None):
    """Vero se il nome o i primi byte dicono che e' un .xlsx/.xlsm (zip)."""
    if (nome or "").lower().endswith((".xlsx", ".xlsm")):
        return True
    return bool(dati) and bytes(dati[:4]) == b"PK\x03\x04"
