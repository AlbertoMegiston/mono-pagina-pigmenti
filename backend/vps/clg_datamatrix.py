#!/usr/bin/env python3
"""
DataMatrix per i codici caricati a mano.

I cartellini del brand portano un DataMatrix con dentro
"articolo-variante-taglia-identificativo-codice" (i campi finiscono con un
trattino, il codice ha gli spazi: "639 516 540 959"). Un codice caricato da
txt/csv o dal pannello non ha nessun barcode registrato: qui se ne compone
uno con la stessa forma (i campi vuoti si saltano, resta almeno il codice),
lo si disegna e lo si registra come barcode emesso, cosi' vale la stessa
regola "solo il barcode emesso e' valido" della verifica.

Serve zxing-cpp (che qui scrive, oltre a leggere) e Pillow per il PNG: le
stesse librerie facoltative dell'importazione degli Excel. Senza, il modulo
lo dice e non genera nulla. Niente numpy: il bitmap passa per memoryview.
"""
import io
import re

try:
    from PIL import Image
except ImportError:  # dipendenza facoltativa
    Image = None
try:
    import zxingcpp
except ImportError:  # dipendenza facoltativa
    zxingcpp = None

GENERAZIONE_DISPONIBILE = Image is not None and zxingcpp is not None

CODE_RE = re.compile(r"^\d{12}$", re.ASCII)
# I campi A-D: lettere, cifre e pochi segni. Niente spazi (il codice e' l'unico
# pezzo con gli spazi) e niente trattini (separano i campi nel payload).
CAMPO_RE = re.compile(r"^[A-Za-z0-9._/]{1,40}$", re.ASCII)
CAMPI = ("article", "variant", "size", "internal_id")

# Dimensione di stampa: un modulo = MODULO pixel; a 300 dpi 12 px sono circa
# 1 mm per modulo, quindi un simbolo 26x26 misura ~3 cm con la zona quieta.
MODULO = 12
ZONA_QUIETA_EXTRA = 2  # moduli bianchi aggiunti oltre a quella del simbolo


class GenerazioneNonDisponibile(RuntimeError):
    pass


def formatta_codice(code):
    """'639516540959' -> '639 516 540 959' (la forma stampata sui cartellini)."""
    d = re.sub(r"\D+", "", str(code or ""), flags=re.ASCII)
    if not CODE_RE.match(d):
        raise ValueError("il codice deve essere di 12 cifre")
    return " ".join(d[i:i + 3] for i in range(0, 12, 3))


def pulisci_campo(valore):
    """Un campo A-D come lo vuole il payload: senza spazi attorno e senza il
    trattino finale che nei fogli del brand fa da separatore."""
    v = str(valore or "").strip().rstrip("-").strip()
    if v and not CAMPO_RE.match(v):
        raise ValueError("campo non valido (%r): lettere, cifre, punto, barra; "
                         "niente spazi ne' trattini" % v)
    return v


def componi_payload(code, article="", variant="", size="", internal_id=""):
    """Il testo del DataMatrix nella forma dei cartellini del brand.

    Tutti i campi: 'L1S156100062S0051-V0024-M-99PROI20250017229-639 516 540 959'.
    Nessun campo: '639 516 540 959'. In entrambi i casi le ultime dodici
    cifre sono il codice, come vuole la regola di estrazione della pagina e
    del server."""
    parti = [pulisci_campo(v) for v in (article, variant, size, internal_id)]
    return "".join(p + "-" for p in parti if p) + formatta_codice(code)


def genera(payload, modulo=MODULO):
    """Ritorna {"png": bytes, "svg": str, "lato": moduli per lato}.

    Simbolo quadrato (force_square: i cartellini del brand sono quadrati e i
    lettori li preferiscono), zona quieta del simbolo piu' ZONA_QUIETA_EXTRA
    moduli bianchi, scala a MODULO pixel per modulo senza interpolazione."""
    if not GENERAZIONE_DISPONIBILE:
        raise GenerazioneNonDisponibile(
            "generazione non disponibile su questo server (mancano Pillow o zxing-cpp)")
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.DataMatrix,
                                      force_square=True)
    bitmap = barcode.to_image(scale=1, add_quiet_zones=True)
    mv = memoryview(bitmap)
    alto, largo = mv.shape
    piccolo = Image.frombuffer("L", (largo, alto), mv.tobytes(), "raw", "L", 0, 1)
    grande = piccolo.resize((largo * modulo, alto * modulo), Image.NEAREST)
    bordo = ZONA_QUIETA_EXTRA * modulo
    tela = Image.new("L", (grande.width + 2 * bordo, grande.height + 2 * bordo), 255)
    tela.paste(grande, (bordo, bordo))
    out = io.BytesIO()
    tela.save(out, "PNG", optimize=True)
    return {"png": out.getvalue(), "svg": barcode.to_svg(scale=1, add_quiet_zones=True),
            "lato": largo}


def rilegge(png, payload):
    """Autocontrollo: il PNG appena generato si decodifica e dice payload."""
    if not GENERAZIONE_DISPONIBILE:
        return False
    img = Image.open(io.BytesIO(png))
    img.load()
    letti = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.DataMatrix)
    return any(r.text == payload for r in letti)
