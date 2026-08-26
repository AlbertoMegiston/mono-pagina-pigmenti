# Asset del prototipo

I file vanno messi in questa cartella e collegati dal blocco `ASSETS`
in cima allo script di `code/index.html`.

| Chiave            | Cosa                                              | Formato consigliato |
|-------------------|---------------------------------------------------|---------------------|
| `logo`            | Logo del brand in testata                          | SVG o PNG trasparente, ~68px di altezza reale |
| `serviceName`     | Nome del servizio nella riga "powered by"          | testo |
| `serviceLogo`     | Marchio del servizio nella riga "powered by"       | SVG o PNG trasparente, ~32px |
| `hero`            | Immagine della schermata iniziale                  | JPG, 3:2 su telefono e 16:9 da 700px in su, lato lungo ~1600px |
| `heroVideo`       | Video della schermata iniziale (opzionale)         | MP4 H.264, muto, in loop |
| `background`      | Immagine a tutto schermo dietro gli step successivi| JPG verticale, ~1200x2000 |
| `backgroundVideo` | Video a tutto schermo (opzionale)                  | MP4 H.264, muto, in loop |

Il video, se presente, ha la precedenza sull'immagine corrispondente; l'immagine
resta usata come poster mentre il video carica.

Finché una chiave è vuota resta il segnaposto, quindi il layout è già corretto
anche senza asset.

## Contrasto

Le schermate dopo la prima scrivono in bianco sopra l'immagine. Sopra
l'immagine c'è un velo scuro tarato per reggere anche una foto molto chiara:
con un'immagine bianca piena il testo resta sopra 6:1, ben oltre il minimo
WCAG AA di 4.5:1. Non serve quindi scegliere foto scure, ma se sostituisci il
velo ricontrolla il contrasto.
