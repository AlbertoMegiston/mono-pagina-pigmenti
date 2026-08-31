#!/usr/bin/env python3
"""
Pannello di amministrazione della lista codici CLG.

Serve una pagina web protetta (l'autenticazione la fa nginx, con utente e
password su HTTPS) da cui gestire la lista senza toccare la riga di comando:
caricare codici, cambiare lo stato di un codice, svuotare la lista, vedere le
statistiche.

Ascolta solo su 127.0.0.1: l'unico che lo raggiunge e' il reverse proxy nginx,
che espone il pannello sotto /pannello/ dietro login. Scrive sullo stesso
database del servizio di verifica (stesso utente di sistema), quindi le liste
caricate qui valgono subito per le verifiche.

Configurazione via variabili d'ambiente (vedi autenticatore-pannello.service):
    CLG_DB              percorso del database SQLite
    CLG_PANEL_HOST      default 127.0.0.1
    CLG_PANEL_PORT      default 8788
"""

import json
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("CLG_DB", "/var/lib/autenticatore/clg.db")
HOST = os.environ.get("CLG_PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLG_PANEL_PORT", "8788"))

CODE_RE = re.compile(r"^\d{12}$")
STATI = ("valid", "suspicious", "revoked")
MAX_BODY = 8 * 1024 * 1024  # una lista molto grande sta comunque in pochi MB


def connect():
    cx = sqlite3.connect(DB_PATH, timeout=10)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA busy_timeout=10000")
    return cx


def stato_lista():
    """Riepilogo mostrato in cima al pannello."""
    with connect() as cx:
        tot = cx.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
        per_stato = dict(cx.execute(
            "SELECT status, COUNT(*) FROM codes GROUP BY status").fetchall())
        verifiche = cx.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        per_esito = dict(cx.execute(
            "SELECT outcome, COUNT(*) FROM checks GROUP BY outcome").fetchall())
        since = int(time.time()) - 30 * 86400
        piu_visti = cx.execute(
            "SELECT code, COUNT(DISTINCT ip_hash) c FROM checks "
            "WHERE ts >= ? AND ip_hash IS NOT NULL "
            "GROUP BY code ORDER BY c DESC LIMIT 8", (since,)).fetchall()
    return {
        "totale": tot,
        "validi": per_stato.get("valid", 0),
        "sospetti": per_stato.get("suspicious", 0),
        "revocati": per_stato.get("revoked", 0),
        "verifiche": verifiche,
        "esiti": per_esito,
        "piu_visti": [{"code": c, "dispositivi": n} for c, n in piu_visti],
    }


def importa(testo, stato_default, sostituisci):
    """Carica codici incollati o da file. Accetta un elenco semplice (un codice
    per riga) o un CSV con intestazione code[,status][,note]. Ritorna il
    conteggio e qualche esempio di riga scartata."""
    if stato_default not in STATI:
        stato_default = "valid"
    righe = testo.splitlines()
    intestazione = righe[0].lower() if righe else ""
    csv_mode = "code" in intestazione and ("," in intestazione or ";" in intestazione)
    sep = ";" if ";" in intestazione else ","
    colonne = []
    if csv_mode:
        colonne = [c.strip().lower() for c in righe[0].split(sep)]
        righe = righe[1:]

    ora = int(time.time())
    nuovi = agg = saltati = 0
    scarti = []
    with connect() as cx:
        if sostituisci:
            cx.execute("DELETE FROM codes")
        for riga in righe:
            if not riga.strip():
                continue
            stato = stato_default
            nota = None
            grezzo = riga
            if csv_mode:
                celle = [c.strip() for c in riga.split(sep)]
                campi = dict(zip(colonne, celle))
                grezzo = campi.get("code", "")
                s = (campi.get("status") or "").lower()
                if s in STATI:
                    stato = s
                nota = campi.get("note") or None
            codice = re.sub(r"\D+", "", grezzo)
            if not CODE_RE.match(codice):
                saltati += 1
                if len(scarti) < 8:
                    scarti.append(grezzo.strip()[:40])
                continue
            esiste = cx.execute("SELECT 1 FROM codes WHERE code = ?", (codice,)).fetchone()
            cx.execute(
                "INSERT INTO codes (code, status, batch, note, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                "status=excluded.status, note=excluded.note",
                (codice, stato, "pannello", nota, ora))
            if esiste:
                agg += 1
            else:
                nuovi += 1
    return {"nuovi": nuovi, "aggiornati": agg, "saltati": saltati, "scarti": scarti}


def cambia_stato(codice, stato):
    codice = re.sub(r"\D+", "", codice or "")
    if not CODE_RE.match(codice):
        return {"ok": False, "errore": "Il codice deve essere di 12 cifre."}
    if stato not in STATI:
        return {"ok": False, "errore": "Stato non valido."}
    with connect() as cx:
        n = cx.execute("UPDATE codes SET status = ? WHERE code = ?",
                       (stato, codice)).rowcount
    if n:
        return {"ok": True}
    return {"ok": False, "errore": "Questo codice non e' in lista."}


def svuota():
    with connect() as cx:
        n = cx.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
        cx.execute("DELETE FROM codes")
    return {"ok": True, "cancellati": n}


PAGE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Pannello — Autenticatore CLG</title>
<style>
  :root{--bg:#f4f4f5;--card:#fff;--ink:#18181b;--quiet:#6b7280;--line:#e4e4e7;
        --accent:#18181b;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--radius:12px}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{background:var(--ink);color:#fff;padding:18px 20px}
  header h1{margin:0;font-size:1.05rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase}
  header p{margin:4px 0 0;color:#a1a1aa;font-size:.82rem}
  main{max-width:820px;margin:0 auto;padding:20px 16px 60px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
  .stat b{display:block;font-size:1.7rem;line-height:1.1}
  .stat span{color:var(--quiet);font-size:.8rem}
  section{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
          padding:18px;margin:14px 0}
  section h2{margin:0 0 12px;font-size:1rem}
  section p.hint{margin:0 0 12px;color:var(--quiet);font-size:.85rem}
  label{display:block;font-size:.85rem;font-weight:600;margin:10px 0 4px}
  textarea,input[type=text],select{width:100%;padding:10px;border:1px solid var(--line);
    border-radius:8px;font:inherit;background:#fff}
  textarea{min-height:150px;resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9rem}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
  .row>div{flex:1 1 160px}
  button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:10px 18px;
    background:var(--accent);color:#fff;cursor:pointer}
  button:hover{opacity:.9}
  button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
  button.danger{background:var(--bad)}
  .inline{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .chk{display:flex;gap:8px;align-items:center;font-weight:400;margin:12px 0}
  .chk input{width:auto}
  .msg{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:.9rem;display:none}
  .msg.ok{display:block;background:#dcfce7;color:var(--ok)}
  .msg.err{display:block;background:#fee2e2;color:var(--bad)}
  table{width:100%;border-collapse:collapse;font-size:.9rem}
  td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--quiet);font-weight:600}
  code{font-family:ui-monospace,Menlo,Consolas,monospace}
  .pill{font-size:.72rem;padding:2px 8px;border-radius:999px}
  .pill.v{background:#dcfce7;color:var(--ok)} .pill.s{background:#fef3c7;color:var(--warn)}
  .pill.r{background:#fee2e2;color:var(--bad)}
</style>
</head>
<body>
<header>
  <h1>Pannello autenticatore</h1>
  <p>Gestione della lista codici · crtilogo</p>
</header>
<main>
  <div class="cards" id="cards"></div>

  <section>
    <h2>Carica codici</h2>
    <p class="hint">Incolla i codici (uno per riga) oppure scegli un file .txt/.csv.
       Un CSV puo' avere le colonne <code>code,status,note</code>.</p>
    <label for="file">Da file (facoltativo)</label>
    <input type="file" id="file" accept=".txt,.csv">
    <label for="codici">Codici</label>
    <textarea id="codici" placeholder="558420726815&#10;558420726816&#10;..."></textarea>
    <div class="row">
      <div>
        <label for="stato">Stato predefinito</label>
        <select id="stato">
          <option value="valid">valido (autentico)</option>
          <option value="suspicious">sospetto</option>
          <option value="revoked">revocato (falso)</option>
        </select>
      </div>
    </div>
    <div class="chk">
      <input type="checkbox" id="sostituisci">
      <label for="sostituisci" style="margin:0;font-weight:400">Sostituisci l'intera lista attuale (svuota prima di caricare)</label>
    </div>
    <button id="btn-importa">Carica</button>
    <div class="msg" id="msg-importa"></div>
  </section>

  <section>
    <h2>Cambia stato di un codice</h2>
    <p class="hint">Per bruciare un codice (renderlo "falso") mettilo su <b>revocato</b>.</p>
    <div class="row">
      <div>
        <label for="cod-singolo">Codice (12 cifre)</label>
        <input type="text" id="cod-singolo" inputmode="numeric" placeholder="558420726815">
      </div>
      <div>
        <label for="stato-singolo">Nuovo stato</label>
        <select id="stato-singolo">
          <option value="valid">valido</option>
          <option value="suspicious">sospetto</option>
          <option value="revoked">revocato</option>
        </select>
      </div>
      <div style="flex:0 0 auto"><button id="btn-codice">Applica</button></div>
    </div>
    <div class="msg" id="msg-codice"></div>
  </section>

  <section>
    <h2>Codici piu' verificati (ultimi 30 giorni)</h2>
    <table id="tab-visti"><tbody></tbody></table>
  </section>

  <section>
    <h2>Svuota la lista</h2>
    <p class="hint">Rimuove <b>tutti</b> i codici. Il registro delle verifiche resta.
       Utile per togliere i codici demo prima di caricare quelli veri.</p>
    <button class="danger" id="btn-svuota">Svuota tutto</button>
    <div class="msg" id="msg-svuota"></div>
  </section>
</main>
<script>
  var B = "/pannello/api";
  function show(id, ok, txt){var m=document.getElementById(id);m.className="msg "+(ok?"ok":"err");m.textContent=txt;}
  function api(path, body){
    return fetch(B+path, {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body||{})}).then(function(r){return r.json();});
  }
  function pill(s){return s==="valid"?'<span class="pill v">valido</span>':
    s==="suspicious"?'<span class="pill s">sospetto</span>':'<span class="pill r">revocato</span>';}
  function carica(){
    fetch(B+"/stato").then(function(r){return r.json();}).then(function(s){
      document.getElementById("cards").innerHTML =
        '<div class="stat"><b>'+s.totale+'</b><span>codici in lista</span></div>'+
        '<div class="stat"><b>'+s.validi+'</b><span>validi</span></div>'+
        '<div class="stat"><b>'+s.sospetti+'</b><span>sospetti</span></div>'+
        '<div class="stat"><b>'+s.revocati+'</b><span>revocati</span></div>'+
        '<div class="stat"><b>'+s.verifiche+'</b><span>verifiche totali</span></div>';
      var tb = document.querySelector("#tab-visti tbody");
      if(!s.piu_visti.length){tb.innerHTML='<tr><td style="color:#6b7280">Ancora nessuna verifica.</td></tr>';return;}
      tb.innerHTML='<tr><th>Codice</th><th>Dispositivi</th></tr>'+s.piu_visti.map(function(v){
        return '<tr><td><code>'+v.code+'</code></td><td>'+v.dispositivi+'</td></tr>';}).join("");
    }).catch(function(){});
  }
  document.getElementById("file").addEventListener("change", function(e){
    var f=e.target.files[0]; if(!f) return;
    var r=new FileReader(); r.onload=function(){document.getElementById("codici").value=r.result;}; r.readAsText(f);
  });
  document.getElementById("btn-importa").addEventListener("click", function(){
    var txt=document.getElementById("codici").value;
    if(!txt.trim()){show("msg-importa",false,"Non c'e' niente da caricare.");return;}
    this.disabled=true; var self=this;
    api("/importa",{testo:txt, stato:document.getElementById("stato").value,
      sostituisci:document.getElementById("sostituisci").checked}).then(function(res){
      self.disabled=false;
      if(res.errore){show("msg-importa",false,res.errore);return;}
      var m="Caricati: "+res.nuovi+" nuovi, "+res.aggiornati+" aggiornati, "+res.saltati+" saltati.";
      if(res.scarti&&res.scarti.length) m+=" Esempi scartati: "+res.scarti.join(", ");
      show("msg-importa",true,m); carica();
    }).catch(function(){self.disabled=false;show("msg-importa",false,"Errore di comunicazione.");});
  });
  document.getElementById("btn-codice").addEventListener("click", function(){
    api("/codice",{code:document.getElementById("cod-singolo").value,
      stato:document.getElementById("stato-singolo").value}).then(function(res){
      if(res.ok){show("msg-codice",true,"Fatto.");carica();document.getElementById("cod-singolo").value="";}
      else show("msg-codice",false,res.errore||"Errore.");
    }).catch(function(){show("msg-codice",false,"Errore di comunicazione.");});
  });
  document.getElementById("btn-svuota").addEventListener("click", function(){
    if(!confirm("Cancellare TUTTI i codici dalla lista? L'operazione non si annulla.")) return;
    api("/svuota",{}).then(function(res){
      show("msg-svuota",true,"Lista svuotata ("+res.cancellati+" codici rimossi).");carica();
    }).catch(function(){show("msg-svuota",false,"Errore di comunicazione.");});
  });
  carica();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "autenticatore-pannello"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, code=200, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), code)

    def do_GET(self):
        if self.path in ("/pannello", "/pannello/", "/pannello/index.html"):
            self._send(PAGE, 200, "text/html; charset=utf-8")
            return
        if self.path == "/pannello/api/stato":
            try:
                self._json(stato_lista())
            except sqlite3.Error as e:
                self._json({"errore": "database non disponibile: %s" % e}, 503)
            return
        self._json({"errore": "non trovato"}, 404)

    def _leggi_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return None

    def do_POST(self):
        if not self.path.startswith("/pannello/api/"):
            self._json({"errore": "non trovato"}, 404)
            return
        dati = self._leggi_json()
        if not isinstance(dati, dict):
            self._json({"errore": "richiesta non valida"}, 400)
            return
        try:
            if self.path == "/pannello/api/importa":
                self._json(importa(str(dati.get("testo", "")),
                                   str(dati.get("stato", "valid")),
                                   bool(dati.get("sostituisci"))))
            elif self.path == "/pannello/api/codice":
                self._json(cambia_stato(str(dati.get("code", "")),
                                        str(dati.get("stato", ""))))
            elif self.path == "/pannello/api/svuota":
                self._json(svuota())
            else:
                self._json({"errore": "non trovato"}, 404)
        except sqlite3.Error as e:
            self._json({"errore": "database non disponibile: %s" % e}, 503)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("pannello in ascolto su %s:%s" % (HOST, PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
