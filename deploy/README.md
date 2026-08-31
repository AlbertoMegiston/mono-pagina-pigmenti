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
installare, niente database server da amministrare.

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
comando**: carichi i codici (incollandoli o da file), vedi le statistiche,
revochi un singolo codice, svuoti la lista.

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

L'ultima riga è il segnale anti-clonazione: un codice autentico vive su un pezzo
solo, quindi non viene verificato da decine di telefoni diversi. Le soglie si
cambiano in `/etc/systemd/system/autenticatore-api.service`
(`CLG_DUP_LIMIT`, `CLG_DUP_DAYS`), poi `systemctl daemon-reload && systemctl
restart autenticatore-api`.

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

## Privacy e sicurezza, in breve

- Gli **indirizzi IP non vengono conservati**: nel registro finisce solo
  un'impronta calcolata con un sale segreto, che basta a contare i dispositivi
  diversi ma non permette di risalire a chi ha verificato. Il registro viene
  potato automaticamente (righe più vecchie di 180 giorni).
- L'API accetta **20 richieste al minuto per indirizzo** (per IPv6 per blocco
  /64): frena chi provasse a tentare codici a caso.
- La lista codici **si amministra solo dal server**, da riga di comando. Non
  esiste una pagina di amministrazione da proteggere né una password in giro.
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
```

Se il certificato non è stato rilasciato al primo colpo, quasi sempre è il DNS
che non puntava ancora al server. Sistemato quello:

```bash
certbot --nginx -d iltuodominio.it --redirect
```
