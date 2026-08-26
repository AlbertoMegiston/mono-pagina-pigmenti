# Backend di verifica — lista codici CLG

Confronto reale dei codici contro una lista caricata dal brand, su Supabase.
Sostituisce la simulazione della pagina quando l'endpoint viene configurato.

## Architettura

```
pagina (code/index.html)
   └── POST /functions/v1/verify-clg   { "code": "558420726815" }
          └── tabella clg_codes  → esito: genuine | suspicious | fake | not_found
          └── tabella clg_checks → registro delle verifiche (per i controlli anti-abuso)
```

- **`clg_codes`** e la lista dei codici validi. La carica il brand: dalla
  dashboard Supabase, Table Editor → `clg_codes` → Insert → Import data from
  CSV. Formato CSV: colonna `code` (12 cifre), opzionale `status`
  (`valid` | `suspicious` | `revoked`) e `note`.
- **`clg_checks`** registra ogni verifica (codice, esito, contesto d'acquisto).
  Serve anche al segnale anti-contraffazione piu semplice: un codice valido
  verificato troppe volte diventa `suspicious` (soglia nella funzione, default
  10 verifiche in 30 giorni).
- **`verify-clg`** e una edge function pubblica (i consumatori verificano senza
  account). CORS aperto, risponde in ~50 ms dalla regione eu-central-1.

## Esiti

| Caso                                  | Esito       |
|---------------------------------------|-------------|
| Codice in lista, status `valid`       | `genuine`   |
| Codice in lista ma verificato troppe volte | `suspicious` |
| Codice in lista, status `suspicious`  | `suspicious`|
| Codice in lista, status `revoked`     | `fake`      |
| Codice non in lista                   | `not_found` |
| Input non valido (non 12 cifre)       | `invalid`   |

Nota di merito: nel mondo anti-contraffazione "non in lista" NON equivale a
"contraffatto" — puo essere un errore di lettura o una lista incompleta. La
pagina lo presenta gia cosi ("Articolo non registrato"), distinto dall'esito
negativo.

## Sicurezza

- RLS attiva su entrambe le tabelle: nessun accesso diretto dal client.
  Solo la edge function legge/scrive, con la service role key (mai esposta).
- La funzione accetta solo POST con JSON `{code}`; nessun dato personale.
- Rate limiting: quello di piattaforma sulle edge functions; per andare in
  produzione vera serve un limite per IP (Supabase lo offre via config).

## Deploy

1. Scegliere il progetto Supabase (vedi domanda aperta col team).
2. Applicare `migrations/001_clg_codes.sql`.
3. Deployare `functions/verify-clg/index.ts`.
4. Nella pagina, impostare `VERIFY_URL` con l'URL della funzione: da quel
   momento `computeVerdict` chiama il backend e la simulazione resta solo come
   fallback dichiarato quando la rete manca.
5. Caricare la lista via CSV dalla dashboard.

Tutti i passi 2-4 possono essere eseguiti direttamente da questa sessione col
connettore Supabase, una volta scelto il progetto.
