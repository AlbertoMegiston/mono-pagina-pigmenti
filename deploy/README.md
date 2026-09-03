# Messa online sul server

Tutto quello che serve per mettere l'autenticatore su un VPS Debian (12 o 13)
con dominio, HTTPS e verifica reale dei codici. Un solo script fa il lavoro.

Il login sul server è come **root**, quindi i comandi si danno senza `sudo`
(su Debian minimale `sudo` non è nemmeno installato).

## Cosa finisce sul server

```
    visitatore
       │  https
       ▼
    nginx ──── /            →  /var/www/autenticatore/index.html   (la pagina)
       ├────── /api/verify  →  127.0.0.1:8787                      (il servizio)
       └────── /api/otp/*   →  127.0.0.1:8787 ──→ api.mailgun.net  (codice via email)
                                    │
                                    ▼
                              /var/lib/autenticatore/clg.db
                              (lista codici + registro verifiche
                               + codici email, solo come impronte)
```

La pagina è **un unico file**: font, logo e fotografie sono dentro. Non ci sono
cartelle di risorse da tenere allineate.

Il servizio di verifica usa solo Python di sistema e SQLite: niente pacchetti da
installare, niente database server da amministrare. Le uniche aggiunte, e sono
facoltative, servono a leggere i DataMatrix dentro agli Excel del brand
(Pillow e zxing-cpp): le installa lo script, e se non ci riesce lo dice e va
avanti.

## Prima di cominciare

Servono due cose:

1. **Il server**, con il suo indirizzo IP pubblico.
2. **Record DNS** che puntino il dominio a quell'indirizzo. Nel pannello dove
   hai comprato il dominio:

   | Tipo | Nome | Valore |
   |------|------|--------|
   | A    | `@` (o il sottodominio scelto) | l'IPv4 del server |
   | AAAA | lo stesso nome                 | l'IPv6 del server, se il server ne ha uno |

   Aspetta che il DNS si propaghi prima del passo 3, altrimenti il certificato
   HTTPS non si può ottenere. Controlla dal server con
   `getent hosts iltuodominio.it` (deve rispondere l'IP giusto).

   > Attenzione all'AAAA: se lo imposti, deve essere **corretto**. Let's Encrypt
   > prova prima l'IPv6, e un AAAA sbagliato fa fallire il certificato. Se non
   > vuoi l'IPv6, meglio non mettere l'AAAA che metterlo errato.

## I tre passi

**1. Copia il pacchetto sul server** (dal tuo computer):

```bash
scp -i ~/.ssh/LA_TUA_CHIAVE autenticatore-deploy.tar.gz root@IP_DEL_SERVER:/root/
```

**2. Entra e scompatta**:

```bash
ssh -i ~/.ssh/LA_TUA_CHIAVE root@IP_DEL_SERVER
tar xzf autenticatore-deploy.tar.gz
cd autenticatore-deploy
```

**3. Lancia l'installazione**:

```bash
./setup.sh iltuodominio.it tua@email.it
```

Lo script installa i pacchetti, mette la pagina al suo posto, avvia il servizio
di verifica, configura nginx, apre il firewall (lasciando aperta la porta SSH da
cui sei collegato) e chiede il certificato HTTPS. Si può rilanciare quante volte
vuoi: ogni passo controlla se è già a posto, e un rilancio non tocca l'HTTPS già
configurato né rimette i codici demo.

Alla fine il sito risponde su `https://iltuodominio.it`.

## Il pannello di amministrazione

Da `https://iltuodominio.it/pannello/` gestisci la lista **senza riga di
comando**: carichi i codici (incollandoli, da file txt/csv o dall'Excel del
brand con i DataMatrix), vedi le statistiche e l'elenco dei codici con taglia,
articolo e identificativo, revochi un singolo codice, svuoti la lista.

Con un Excel il pannello mostra prima un'**anteprima** (quante righe, quante
con il codice della colonna E diverso da quello nel DataMatrix, quante senza
immagine, le prime dieci righe) e scrive solo quando premi **Importa**.

È protetto da **utente e password** (su HTTPS), con un limite ai tentativi di
accesso. All'installazione viene creata una password casuale, salvata sul
server in `/root/pannello-password.txt` (leggibile solo da root):

```bash
cat /root/pannello-password.txt
```

Per impostarne una tua:

```bash
imposta-password-pannello
```

Il servizio del pannello ascolta solo su localhost: si raggiunge unicamente
attraverso nginx, dopo il login. Non è indicizzato dai motori di ricerca.

## La lista dei codici

Appena installato ci sono solo i **tre codici dimostrativi** (`…012` autentico,
`…013` sospetto, `…014` falso), così il sito è subito mostrabile. Quando arriva
la lista vera:

```bash
# via i codici di prova
clgadmin svuota --conferma

# carica la lista del brand (il file può stare anche in /root)
clgadmin importa /root/codici.csv --lotto "lotto-2026-01"

# controlla
clgadmin stato-lista
```

Il file può essere un elenco semplice (un codice per riga) oppure un CSV con
intestazione `code,status,note`. I codici sono di 12 cifre; trattini e spazi
vengono ignorati, le righe non valide vengono saltate e riepilogate alla fine.
Un codice già in lista **mantiene il suo stato** (per esempio `revoked`) a meno
che non venga indicato, nel CSV o con `--stato`.

### L'Excel del brand (DataMatrix)

Il brand fornisce un Excel (`.xlsx`/`.xlsm`) con un foglio per modello e, per
ogni riga: articolo (A), variante (B), taglia (C), identificativo interno (D),
codice a 12 cifre (E) e l'**immagine del DataMatrix** (F) che finisce sul
cartellino. Il DataMatrix contiene `A+B+C+D+codice`, ed è lui la fonte
affidabile: la colonna E è una formula casuale che cambia a ogni ricalcolo e
in quasi tutte le righe non coincide con il codice stampato.

```bash
clgadmin importa /root/Barcode_DataMatrix.xlsm --lotto "lotto-2026-01"
```

Per ogni riga viene letto il DataMatrix (ripiegando su E solo se l'immagine
manca o non si decodifica) e si salvano codice, testo del barcode, articolo,
variante, taglia e identificativo. Alla fine il riepilogo dice quante righe
sono state importate, aggiornate o scartate, quante hanno E diverso dal
barcode, quante sono senza immagine o non decodificabili. Con
`--codice-da colonna` il codice viene sempre da E (il barcode si salva lo
stesso). Per leggere le immagini servono Pillow e zxing-cpp, che `setup.sh`
installa; senza, l'importazione avvisa e prende i codici dalla colonna E, che
sono casuali: una volta installate le librerie va **svuotata la lista** prima
di reimportare (vedi "Se qualcosa non va").

Per bruciare un singolo codice (da lì in poi l'esito è "falso"):

```bash
clgadmin stato 558420726815 revoked
```

Stati possibili: `valid`, `suspicious`, `revoked`.

## DataMatrix per i codici caricati a mano

I codici del brand arrivano dall'Excel con il loro DataMatrix gia' letto e
registrato. Un codice caricato da txt, csv o dal riquadro del pannello non ha
nessun barcode: finche' resta cosi', allo scan vale come il codice digitato.
Per stamparlo su un cartellino con la stessa regola dei prodotti del brand
("solo il barcode emesso e' valido") il DataMatrix si genera qui, e nel
farlo viene registrato come barcode emesso per quel codice.

Il contenuto ha la stessa forma dei cartellini del brand:
`articolo-variante-taglia-identificativo-codice`, con il codice a gruppi di
tre cifre ("555 666 777 888"). I campi sono facoltativi e si saltano se
vuoti: senza campi il DataMatrix contiene solo il codice. Nei campi vanno
lettere, cifre, punto e barra; niente spazi ne' trattini (separano i campi).

**Dal pannello.** Nella tabella "Codici in lista" ogni codice senza barcode
ha il bottone "Genera DataMatrix": si compilano i campi, si preme "Genera e
registra" e si scaricano il PNG (anteprima, 12 px per modulo) o l'SVG (per
la stampa, si ridimensiona senza perdere nitidezza). I codici che il barcode
ce l'hanno gia' mostrano "Scarica": ridisegna quello registrato per una
ristampa, senza cambiare nulla. Per cambiarne il contenuto bisogna spuntare
"Rigenera e sostituisci": i cartellini gia' stampati con il vecchio barcode
smettono di valere, per questo serve la spunta. Il bottone "Genera i
DataMatrix mancanti (zip)" fa tutto in una volta per i codici senza barcode
(campi presi da quelli gia' salvati, se ci sono) e scarica uno zip con PNG e
SVG per ogni codice.

**Dalla riga di comando.**

```
clgadmin datamatrix 555666777888 --articolo ART01 --taglia M --out /root/555666777888.svg
clgadmin datamatrix 555666777888                      # ristampa quello registrato
clgadmin datamatrix 555666777888 --taglia L --sostituisci
clgadmin datamatrix-mancanti /root/datamatrix         # PNG e SVG per ogni codice senza barcode
```

Servono le stesse librerie facoltative dell'importazione degli Excel (Pillow
e zxing-cpp, installate dal passo 1b di setup.sh). Il simbolo e' quadrato,
con la zona quieta gia' inclusa; ogni immagine viene riletta prima di essere
consegnata. Stampa consigliata: almeno 1 mm per modulo (un simbolo di 26
moduli misura circa 3 cm con i margini).

## Come vengono decisi gli esiti

| Situazione | Esito mostrato |
|---|---|
| codice non in lista | non trovato |
| stato `revoked` | falso |
| stato `suspicious` | sospetto |
| valido, ma verificato da più di 10 dispositivi diversi in 30 giorni | sospetto |
| valido | autentico |

Se la pagina ha **letto un barcode** (DataMatrix o QR), prima di tutto vale la
regola "solo il barcode emesso è valido":

| Barcode letto | Esito |
|---|---|
| uguale a quello registrato per una riga | si prosegue con il codice di quella riga (tabella sopra) |
| un altro DataMatrix, con dentro un codice per cui **è registrato** un barcode | falso: sull'articolo c'è un barcode che non è quello emesso per quel codice |
| diverso, ma il codice che contiene **non ha** un barcode registrato (lista da txt/csv, codici demo), oppure è il QR `?clg=` del pezzo | si prosegue con quel codice (tabella sopra): lo scan vale come il codice digitato |
| diverso, e il codice che contiene non è in lista | non trovato |

Solo l'Excel del brand registra i barcode: finché la lista arriva da un
elenco di codici, scansionare l'etichetta dà lo stesso esito che digitare il
codice. Il QR del pezzo (`?clg=`, vedi sotto) è l'indirizzo della pagina, non
un barcode da confrontare: non fa mai scattare il "falso".

L'ultima riga della prima tabella è il segnale anti-clonazione: un codice
autentico vive su un pezzo solo, quindi non viene verificato da decine di
telefoni diversi. Le soglie si cambiano in
`/etc/systemd/system/autenticatore-api.service` (`CLG_DUP_LIMIT`,
`CLG_DUP_DAYS`), poi `systemctl daemon-reload && systemctl restart
autenticatore-api`.

### Il contratto dell'API

```
POST /api/verify
{ "code": "739184173203",
  "scan": { "payload": "L1S156100062S0051-V0024-M-99PROI20250017229-739 184 173 203",
            "format": "data_matrix" },          ← null se il codice è stato digitato
  "context": { "when": …, "where": …, "place": … } }

→ { "outcome": "genuine" | "suspicious" | "fake" | "not_found" | "invalid",
    "via": "scan" | "code" }
```

Il codice dentro a un barcode è, nell'ordine: le 12 cifre dopo `clg=` (stile
URL), altrimenti l'ultimo gruppo `ddd ddd ddd ddd` (separato da niente, spazio
o punto) non attaccato ad altre cifre. Nel registro delle verifiche ogni riga
dice se è arrivata da uno scan (`scanned`) e se il barcode coincideva
(`payload_ok`).

**Se il servizio non risponde**, la pagina non si blocca e non inventa un esito
"vero": ripiega sull'esito simulato, e l'avviso in testa alla pagina lo dichiara.

## Email di verifica (Mailgun)

Nel passaggio **Accedi** la pagina chiede un indirizzo email (facoltativo: si
può saltare) e vi spedisce un **codice a 6 cifre** da ricopiare. Il codice
vale 10 minuti, si può sbagliare 5 volte, e dopo 30 secondi se ne può chiedere
un altro. Le email partono da `verifica@crtilogo.com` tramite **Mailgun**
(regione US), senza tracciamento di aperture o clic.

Non ha valore legale: è un passaggio di contatto, come nel servizio di
riferimento. Il server non rilascia token. Finché la chiave Mailgun non è
impostata la pagina dice "non siamo riusciti a inviare il codice" e lascia
saltare il passaggio: il resto del sito funziona lo stesso.

### Cosa c'è già (fatto dal cliente su Mailgun)

Il dominio `crtilogo.com` è **verificato** su Mailgun, regione US, con i
record DNS che Mailgun richiede già inseriti nel pannello del dominio:

| Tipo | Nome | Serve a |
|---|---|---|
| TXT | `crtilogo.com` — SPF (`v=spf1 include:mailgun.org ~all`) | autorizzare Mailgun a spedire per il dominio |
| TXT | `<selettore>._domainkey.crtilogo.com` — DKIM | firmare le email (senza, finiscono nello spam) |
| MX | `mxa.mailgun.org`, `mxb.mailgun.org` | ricevere risposte e rimbalzi (facoltativo) |
| CNAME | `email.crtilogo.com` → `mailgun.org` | tracciamento: qui è spento, il record può anche mancare |

Il server **non** ha bisogno di record DNS propri per spedire: parla con l'API
di Mailgun in HTTPS. Se il dominio di invio cambia, i record vanno rifatti dal
pannello Mailgun (Sending → Domains → DNS records) e va aggiornato
`MAILGUN_DOMAIN` nel file qui sotto.

### Il file di configurazione

`setup.sh` crea, **solo la prima volta**, `/etc/autenticatore/mail.env`
(proprietario root, gruppo `autenticatore`, permessi 640: lo leggono solo root
e il servizio) e non lo sovrascrive mai. Lo legge systemd all'avvio del
servizio (`EnvironmentFile` dell'unità) e `clgadmin mail-test`. Contiene:

```
MAILGUN_API_KEY=                                        ← la chiave di invio del dominio
MAILGUN_DOMAIN=crtilogo.com
MAIL_FROM=Stone Island Autenticazione <verifica@crtilogo.com>
```

più, commentati, i valori facoltativi (`MAILGUN_API_BASE` per la regione, e i
limiti `OTP_*` descritti sotto). Formato `NOME=valore`, un valore per riga,
niente commenti a fine riga.

**Impostare la chiave** (Mailgun: Sending → Domain settings → Sending API
keys) senza mostrarla a video né lasciarla nella cronologia dei comandi:

```bash
read -rs -p "Chiave Mailgun: " K; echo; sed -i "s|^MAILGUN_API_KEY=.*|MAILGUN_API_KEY=$K|" /etc/autenticatore/mail.env; unset K
systemctl restart autenticatore-api
clgadmin mail-test tua@email.it
```

`clgadmin mail-test` spedisce un'email di prova con la stessa funzione che usa
il servizio e dice se Mailgun ha accettato l'invio; la chiave non viene mai
stampata. Se l'email non arriva, guarda nella posta indesiderata e nei log di
Mailgun (Sending → Logs). Dopo ogni modifica al file:
`systemctl restart autenticatore-api`.

### Limiti

| Limite | Valore | Dove si cambia |
|---|---|---|
| validità del codice | 10 minuti | `OTP_TTL` (secondi) |
| tentativi per codice | 5, poi serve un codice nuovo | `OTP_MAX_ATTEMPTS` |
| attesa fra un invio e il successivo | 30 secondi | `OTP_RESEND_AFTER` |
| invii all'ora per indirizzo email | 5 | `OTP_MAX_PER_EMAIL_HOUR` |
| invii all'ora per indirizzo IP | 20 | `OTP_MAX_PER_IP_HOUR` |
| richieste di invio accettate da nginx | 6 al minuto per IP, con una tolleranza di 3 | zona `clgotp` nel file nginx |

I valori `OTP_*` si mettono in `/etc/autenticatore/mail.env`, poi
`systemctl restart autenticatore-api`. Un invio che Mailgun rifiuta non conta
nei limiti: si può riprovare subito.

### Privacy

- Nel database non c'è **nessun indirizzo email**: solo un'impronta con sale
  (come per gli IP), che serve a ritrovare il codice al momento della
  verifica. Anche il codice è conservato come impronta.
- La riga viene cancellata quando il codice è verificato, e comunque un'ora
  dopo la scadenza.
- Nei log del servizio finiscono solo gli esiti ("codice inviato", "invio
  fallito", "codice giusto/sbagliato"): mai indirizzi né codici.
- Le email non contengono link né immagini di tracciamento. Mailgun conserva
  i propri log di invio secondo le impostazioni dell'account.

### Il contratto dell'API

```
POST /api/otp/invia     { "email": "...", "lang": "it" | "en" }
→ 200 { "ok": true, "ttl": 600, "retry_in": 30 }
  400 { "error": "invalid_email" }
  429 { "error": "too_soon", "retry_in": 12 }   |   { "error": "too_many" }
  503 { "error": "not_configured" }   |   { "error": "send_failed" }

POST /api/otp/verifica  { "email": "...", "code": "123456" }
→ { "ok": true }
  { "ok": false, "reason": "wrong", "left": 4 }   |   "expired"   |   "locked"
```

## Aggiornare la pagina

Quando cambia qualcosa nel sito, si rigenera il file unico e si ricarica:

```bash
# sul tuo computer, dentro il repository
python3 deploy/build-standalone.py
scp -i ~/.ssh/LA_TUA_CHIAVE deploy/site/index.html root@IP_DEL_SERVER:/var/www/autenticatore/index.html
```

Non serve riavviare niente: la pagina non è messa in cache a lungo, il
cambiamento si vede subito.

> Nota: dopo il primo HTTPS lo script non riscrive più la configurazione di
> nginx (per non cancellare il blocco che aggiunge certbot). Se cambi header o
> CSP nel template, applicali a mano in `/etc/nginx/sites-available/autenticatore`
> e poi `nginx -t && systemctl reload nginx`. Fanno eccezione le rotte
> `/api/otp/*` dei codici via email: se mancano, un rilancio di `setup.sh` le
> aggiunge da solo anche a una configurazione già passata da certbot.

## I QR code dei prodotti

Ogni QR deve puntare al dominio con il codice del pezzo:

```
https://iltuodominio.it/?clg=558420726815
```

Chi arriva così entra direttamente nell'esperienza. Chi apre il dominio senza
parametro — o ricarica la pagina — trova la schermata bianca con il bottone
**Verifica codice**, com'è stato voluto.

Lo scanner della pagina legge anche il **DataMatrix del cartellino**: il testo
letto viene mandato al servizio insieme al codice, e vale la regola "solo il
barcode emesso è valido" descritta sopra. La regola scatta solo per i codici
caricati dall'Excel del brand (che contiene le immagini dei DataMatrix): per
quelli da un semplice elenco, e per il QR `?clg=` qui sopra, lo scan vale
come il codice digitato.

## Privacy e sicurezza, in breve

- Gli **indirizzi IP non vengono conservati**: nel registro finisce solo
  un'impronta calcolata con un sale segreto, che basta a contare i dispositivi
  diversi ma non permette di risalire a chi ha verificato. Il registro viene
  potato automaticamente (righe più vecchie di 180 giorni).
- L'API accetta **20 richieste al minuto per indirizzo** (per IPv6 per blocco
  /64): frena chi provasse a tentare codici a caso. L'invio del codice via
  email è più stretto ancora (6 al minuto), e il servizio aggiunge i suoi
  limiti per indirizzo email e per IP.
- Per il codice via email **nessun indirizzo in chiaro**, né nel database né
  nei log: solo impronte con sale, cancellate a verifica avvenuta (vedi
  "Email di verifica"). La chiave Mailgun sta in `/etc/autenticatore/mail.env`,
  leggibile solo da root e dal servizio, fuori dal repository.
- La lista codici si amministra **dal server** (riga di comando) o dal
  **pannello** protetto da utente e password su HTTPS, raggiungibile solo
  attraverso nginx e con un limite ai tentativi di accesso. I file caricati
  dal pannello sono limitati a 25 MB e gli Excel vengono letti con protezioni
  contro archivi anomali (troppe voci, troppo grandi una volta scompattati).
- Il servizio gira con un utente dedicato senza privilegi e può scrivere solo
  nella cartella del proprio database.
- La pagina resta `noindex`: non finisce nei motori di ricerca.

## Se qualcosa non va

```bash
systemctl status autenticatore-api      # il servizio è vivo?
journalctl -u autenticatore-api -n 50   # cosa dice
curl -s localhost:8787/api/health       # risponde?
nginx -t && systemctl reload nginx      # la configurazione web è valida?
certbot certificates                    # stato del certificato
python3 -c "import PIL, zxingcpp"       # lettura dei DataMatrix disponibile?
clgadmin mail-test tua@email.it         # le email dei codici partono?
journalctl -u autenticatore-api | grep otp   # esiti di invii e verifiche (mai email o codici)
```

Se `mail-test` dice "stato 401" la chiave è sbagliata o non è quella del
dominio; "stato 404" o "stato 400" quasi sempre vuol dire dominio sbagliato in
`MAILGUN_DOMAIN` oppure regione diversa (per un dominio creato nella regione
EU serve `MAILGUN_API_BASE=https://api.eu.mailgun.net/v3`).

Se l'importazione di un Excel dice che i barcode non sono stati letti, manca
una delle due librerie: `apt-get install python3-pil` e
`pip3 install --break-system-packages zxing-cpp`. Poi **svuota la lista**
(`clgadmin svuota --conferma`, oppure la casella "Sostituisci" nel pannello) e
rilancia l'importazione: i codici presi dalla colonna E non coincidono con
quelli dei barcode, quindi un semplice rilancio li lascerebbe in lista come
validi (l'importazione lo segnala, contando i codici senza barcode rimasti).

Se il certificato non è stato rilasciato al primo colpo, quasi sempre è il DNS
che non puntava ancora al server. Sistemato quello:

```bash
certbot --nginx -d iltuodominio.it --redirect
```
