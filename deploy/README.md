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
       └────── /api/verify  →  127.0.0.1:8787                      (il servizio)
                                    │
                                    ▼
                              /var/lib/autenticatore/clg.db
                              (lista codici + registro verifiche)
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
> e poi `nginx -t && systemctl reload nginx`.

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
  /64): frena chi provasse a tentare codici a caso.
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
```

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
