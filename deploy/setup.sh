#!/usr/bin/env bash
#
# Installazione completa dell'autenticatore su un server Debian/Ubuntu pulito.
#
#   sudo ./setup.sh autenticatore.tuodominio.it tua@email.it
#
# Fa tutto: pacchetti, sito statico, servizio di verifica, nginx, firewall e
# certificato HTTPS. Si puo' rilanciare senza danni: ogni passo controlla se e'
# gia' a posto.
#
set -euo pipefail

DOMINIO="${1:-}"
EMAIL="${2:-}"
FIREWALL="${3:-firewall}"

if [[ -z "$DOMINIO" || -z "$EMAIL" ]]; then
  echo "Uso: sudo ./setup.sh <dominio> <email> [no-firewall]" >&2
  echo "Es.: sudo ./setup.sh autenticatore.miodominio.it io@miodominio.it" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Serve root: rilancia con sudo." >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "Questo script e' scritto per Debian/Ubuntu (apt)." >&2
  exit 1
fi

QUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBROOT=/var/www/autenticatore
APPDIR=/opt/autenticatore
DATADIR=/var/lib/autenticatore

echo "==> 1/8 pacchetti"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx certbot python3-certbot-nginx python3 ufw >/dev/null

echo "==> 2/8 utente e cartelle"
id -u autenticatore >/dev/null 2>&1 || \
  useradd --system --home "$DATADIR" --shell /usr/sbin/nologin autenticatore
mkdir -p "$WEBROOT" "$APPDIR" "$DATADIR"
chown autenticatore:autenticatore "$DATADIR"
chmod 750 "$DATADIR"

echo "==> 3/8 pagina"
if [[ -f "$QUI/site/index.html" ]]; then
  install -m 644 "$QUI/site/index.html" "$WEBROOT/index.html"
  echo "    pagina installata ($(du -h "$WEBROOT/index.html" | cut -f1))"
else
  echo "    ATTENZIONE: $QUI/site/index.html non trovato, la pagina non e' stata aggiornata." >&2
fi

echo "==> 4/8 servizio di verifica"
install -m 755 "$QUI/api/verify_server.py" "$APPDIR/verify_server.py"
install -m 755 "$QUI/api/clg_import.py" "$APPDIR/clg_import.py"
cat > /usr/local/bin/clgadmin <<'EOF'
#!/bin/sh
# Gestione della lista codici. Gira come l'utente del servizio, cosi' i file
# del database restano suoi.
exec setpriv --reuid=autenticatore --regid=autenticatore --init-groups \
     /usr/bin/python3 /opt/autenticatore/clg_import.py "$@"
EOF
chmod 755 /usr/local/bin/clgadmin
install -m 644 "$QUI/api/autenticatore-api.service" \
  /etc/systemd/system/autenticatore-api.service
systemctl daemon-reload
systemctl enable --now autenticatore-api >/dev/null
sleep 1
if systemctl is-active --quiet autenticatore-api; then
  echo "    servizio attivo"
else
  echo "    ATTENZIONE: il servizio non e' partito. Guarda: journalctl -u autenticatore-api -n 30" >&2
fi

echo "==> 4b/8 codici dimostrativi"
# Con la lista vuota ogni codice risulterebbe "non trovato", demo compresi.
# Seminiamo i tre codici di prova solo se la lista e' ancora vuota, cosi' il
# sito e' subito mostrabile; si tolgono con "clgadmin svuota --conferma".
# Il database lo crea il servizio al primo avvio: aspettiamo che ci sia,
# altrimenti non sapremmo distinguere "lista vuota" da "non ancora pronto".
for _ in $(seq 1 20); do [[ -f "$DATADIR/clg.db" ]] && break; sleep 0.5; done
if [[ ! -f "$DATADIR/clg.db" ]]; then
  echo "    database non ancora creato, seme saltato" >&2
elif [[ -f "$QUI/api/codici-dimostrativi.csv" ]]; then
  CONTA="$(clgadmin stato-lista 2>/dev/null | awk '/codici in lista:/ {print $4}')"
  if [[ "$CONTA" == "0" ]]; then
    clgadmin importa "$QUI/api/codici-dimostrativi.csv" --lotto "dimostrativi" >/dev/null
    echo "    seminati 3 codici di prova (svuota con: sudo clgadmin svuota --conferma)"
  else
    echo "    lista gia' popolata (${CONTA:-?} codici), nessun seme aggiunto"
  fi
fi

echo "==> 5/8 nginx"
CONF=/etc/nginx/sites-available/autenticatore
# Al rilancio non riscriviamo una configurazione che certbot ha gia' completato
# con il blocco HTTPS: la sovrascrittura riporterebbe il sito su solo HTTP.
if [[ -f "$CONF" ]] && grep -q "listen 443" "$CONF"; then
  echo "    configurazione con HTTPS gia' presente, lasciata com'e'"
else
  sed "s/__DOMAIN__/$DOMINIO/g" "$QUI/nginx/autenticatore.conf.template" > "$CONF"
  # Su una macchina senza IPv6 la riga "listen [::]" impedisce a nginx di
  # partire del tutto: se non c'e' un indirizzo v6 globale, la togliamo.
  if ! ip -6 addr show scope global 2>/dev/null | grep -q inet6; then
    sed -i '/listen \[::\]:80;/d' "$CONF"
    echo "    nessun IPv6 sulla macchina: rimosso il listen IPv6"
  fi
fi
ln -sf "$CONF" /etc/nginx/sites-enabled/autenticatore
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> 6/8 firewall"
if [[ "$FIREWALL" == "no-firewall" ]]; then
  echo "    saltato su richiesta"
else
  # La porta SSH viene letta dalla configurazione reale: mai chiudersi fuori.
  SSHPORT="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/ {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null || true)"
  ufw allow "${SSHPORT:-22}/tcp" >/dev/null
  ufw allow 'Nginx Full' >/dev/null
  ufw --force enable >/dev/null
  echo "    attivo (SSH su ${SSHPORT:-22}, web aperto)"
fi

echo "==> 7/8 certificato HTTPS"
if [[ -d "/etc/letsencrypt/live/$DOMINIO" ]]; then
  echo "    certificato gia' presente, rinnovo automatico gestito da certbot"
elif certbot --nginx -d "$DOMINIO" --non-interactive --agree-tos -m "$EMAIL" --redirect >/dev/null 2>&1; then
  echo "    certificato ottenuto, rinnovo automatico attivo"
else
  echo "    NON riuscito. Quasi sempre e' il DNS che non punta ancora qui." >&2
  echo "    Controlla il record A e rilancia:  sudo certbot --nginx -d $DOMINIO --redirect" >&2
fi

echo "==> 8/8 verifica finale"
systemctl reload nginx
if curl -fsS --max-time 5 http://127.0.0.1/api/health >/dev/null 2>&1; then
  echo "    API raggiungibile"
else
  echo "    API non raggiungibile da nginx: journalctl -u autenticatore-api -n 30" >&2
fi

cat <<FINE

Fatto.

  Sito       https://$DOMINIO
  Test QR    https://$DOMINIO/?clg=123456789012

Finche' la lista codici e' vuota ogni codice risulta "non trovato".
Carica la lista vera con:

  sudo clgadmin importa /percorso/codici.csv --lotto "primo-lotto"
  sudo clgadmin stato-lista

FINE
