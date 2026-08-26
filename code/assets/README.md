# Asset del prototipo

Metti i file in questa cartella con i nomi qui sotto: la pagina li cerca gia a
questi percorsi, quindi **non serve toccare il codice**. Finche un file manca
resta il segnaposto e il layout e comunque corretto.

| File                   | Cosa                                                   | Formato consigliato |
|------------------------|--------------------------------------------------------|---------------------|
| `logo.svg`             | Logo del brand in testata                               | SVG o PNG trasparente |
| `hero.jpg`             | Immagine della schermata iniziale                       | 3:2 su telefono, 16:9 da 700px in su, lato lungo ~1600px |
| `background.jpg`       | Immagine a tutto schermo dietro gli step successivi     | verticale, ~1200x2000 |
| `background.mp4`       | Video a tutto schermo (opzionale)                       | MP4 H.264, muto, in loop |

Se `background.jpg` manca, lo sfondo degli step ripiega automaticamente su
`hero.jpg`. Se `background.mp4` manca o l'autoplay viene bloccato, si ripiega
sull'immagine. Un file assente non rompe mai nulla.

## Chiavi facoltative

Nel blocco `ASSETS` in cima allo script di `../index.html`:

- `logoLight` — versione chiara del logo. **Serve solo se il logo non e nero.**
  Un logo nero viene invertito in automatico sulle schermate scure; se il tuo
  ha piu colori l'inversione li falsa, quindi fornisci una versione bianca.
- `heroPosition` / `backgroundPosition` — punto di ancoraggio del ritaglio
  (`center`, `top`, `30% 50%`...). Utile perche su telefono un'immagine
  orizzontale viene tagliata parecchio: serve a decidere cosa resta in campo.
- `serviceName` / `serviceLogo` — nome e marchio nella riga "powered by" in
  fondo alle schermate scure. Finche sono vuoti quella riga **non compare**,
  cosi non resta un'attribuzione finta.

## Contrasto

Le schermate dopo la prima scrivono in bianco sopra l'immagine. Il velo scuro
sopra la foto e tarato per reggere qualsiasi luminosita: misurato con
un'immagine quasi bianca, il testo resta sopra 5.3:1, oltre il minimo WCAG AA
di 4.5:1. Non serve quindi scegliere foto scure. Se cambi il velo, rimisura.

## Font

Il carattere del progetto e PF Din Text Pro (Parachute). E un font commerciale:
i file NON sono nel repository e vanno forniti dal titolare della licenza.
Deposita i woff2 licenziati in `fonts/` con questi nomi e vengono presi in
automatico:

- `fonts/PFDinTextPro-Regular.woff2` (peso 400)
- `fonts/PFDinTextPro-Medium.woff2` (peso 500)
- `fonts/PFDinTextPro-Bold.woff2` (peso 700)

Finche mancano, la pagina usa Barlow (Google Fonts), il carattere in stile DIN
piu vicino disponibile gratuitamente. I bottoni sono gia maiuscoli e bold come
da indicazione.
