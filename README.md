# Colorificio Pigmenti — Landing SEO

Landing page statica per Colorificio Pigmenti (Padova), con due sedi a Montegrotto Terme e Selvazzano Dentro.

Sito a pagina singola, senza build step: `index.html` con il CSS critico (base, header, hero) inline nell'`<head>` e il resto del foglio di stile in `assets/style.css` (caricato in preload asincrono con fallback `<noscript>`), logo, loghi marchi (`assets/brands/`) e foto sedi (`assets/sedi/`) in `assets/`, foto hero in `uploads/`. Tutte le immagini sono servite in locale (nessuna dipendenza dal CDN Shopify).. Dati strutturati JSON-LD (LocalBusiness ×2 + FAQPage) inclusi nell'head.

## Deploy

Qualsiasi hosting statico. Su Coolify: build pack **Static**, publish directory `/`.
