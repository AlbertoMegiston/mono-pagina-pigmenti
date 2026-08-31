#!/usr/bin/env python3
"""
Genera una pagina di produzione autoconsistente da code/index.html.

A differenza della build per l'Artifact (che rimuove doctype/head perché
l'host li reinietta), qui produciamo un documento HTML COMPLETO, pronto da
servire da un web server. Font, logo e foto vengono incorporati come data
URI, così il file è unico e non dipende dalla cartella assets/. Il cancello
d'ingresso resta ATTIVO: su un dominio reale il parametro ?clg= viene
passato alla pagina e apre l'esperienza; una visita diretta mostra la
schermata di verifica. Gli avvisi "prototipo / esito simulato" restano
invariati finché non c'è un backend che verifica i codici reali.

Uso:  python3 deploy/build-standalone.py
Esce: deploy/site/index.html
"""
import base64, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
src = (ROOT / "code/index.html").read_text(encoding="utf-8")

def uri(rel, mime):
    b = (ROOT / rel).read_bytes()
    return f"data:{mime};base64," + base64.b64encode(b).decode()

repl = {
    "url('assets/fonts/PFDinTextPro-Regular.ttf')":
        "url('" + uri("code/assets/fonts/PFDinTextPro-Regular.ttf", "font/ttf") + "')",
    "url('assets/fonts/PFDinTextPro-Bold.ttf')":
        "url('" + uri("code/assets/fonts/PFDinTextPro-Bold.ttf", "font/ttf") + "')",
    'logo: "assets/logo.png"':
        'logo: "' + uri("code/assets/logo.png", "image/png") + '"',
    'hero: "assets/hero.jpg"':
        'hero: "' + uri("code/assets/hero.jpg", "image/jpeg") + '"',
    'background: "assets/background.jpg"': 'background: ""',
    'backgroundVideo: "assets/background.mp4"': 'backgroundVideo: ""',
    # Il backend gira sulla stessa macchina, dietro lo stesso nginx: stessa
    # origine, quindi nessun CORS. Se il servizio non risponde la pagina
    # ricade da sola sulla simulazione, e l'avviso lo dichiara.
    'var VERIFY = { url: "" };': 'var VERIFY = { url: "/api/verify" };',
}
for old, new in repl.items():
    if old not in src:
        sys.exit(f"ANCORA MANCANTE: {old}")
    src = src.replace(old, new, 1)

# Sicurezza: la build di produzione NON deve disattivare il cancello.
assert "GATE_DISABLED = true" not in src, "il cancello risulta disattivato!"

out = ROOT / "deploy/site/index.html"
out.write_text(src, encoding="utf-8")
print("scritto", out, out.stat().st_size, "byte")
