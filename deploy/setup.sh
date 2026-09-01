#!/usr/bin/env bash
#
# Installazione completa dell'autenticatore su un server Debian pulito
# (testato per Debian 12 e 13, accesso come root).
#
#   ./setup.sh crtilogo.com tua@email.it
#
# Fa tutto: pacchetti, sito statico, servizio di verifica, nginx, firewall e
# certificato HTTPS. Si puo' rilanciare senza danni: ogni passo controlla se e'
# gia' a posto. Va eseguito come root (su Debian minimale sudo non c'e', ma il
# login e' gia' root, quindi non serve).
#
set -euo pipefail

DOMINIO="${1:-}"
EMAIL="${2:-}"
FIREWALL="${3:-firewall}"

if [[ -z "$DOMINIO" || -z "$EMAIL" ]]; then
  echo "Uso: ./setup.sh <dominio> <email> [no-firewall]" >&2
  echo "Es.: ./setup.sh crtilogo.com io@dominio.it" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Va eseguito come root." >&2
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
apt-get install -y -qq nginx certbot python3-certbot-nginx python3 ufw curl openssl >/dev/null

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
# clgadmin gira come root: cosi' puo' leggere un file di codici ovunque si trovi
# (anche in /root, che e' 0700), e alla fine restituisce la proprieta' del
# database all'utente del servizio, che deve poterci scrivere.
cat > /usr/local/bin/clgadmin <<EOF
#!/bin/sh
/usr/bin/python3 $APPDIR/clg_import.py "\$@"
rc=\$?
chown -R autenticatore:autenticatore $DATADIR 2>/dev/null || true
exit \$rc
EOF
chmod 755 /usr/local/bin/clgadmin
install -m 644 "$QUI/api/autenticatore-api.service" \
  /etc/systemd/system/autenticatore-api.service
systemctl daemon-reload
systemctl enable autenticatore-api >/dev/null
# restart, non "enable --now": a un rilancio applica anche codice e unit
# aggiornati (enable --now non riavvia un servizio gia' attivo).
systemctl restart autenticatore-api
sleep 1
if systemctl is-active --quiet autenticatore-api; then
  echo "    servizio attivo"
else
  echo "    ATTENZIONE: il servizio non e' partito. Guarda: journalctl -u autenticatore-api -n 30" >&2
fi

echo "==> 4b/8 codici dimostrativi"
# Solo alla PRIMA installazione. Un sentinella evita di riseminare i codici
# demo dopo che l'operatore ha caricato la lista vera (magari svuotando prima
# quella demo): un rilancio non deve reintrodurli. Tutto qui e' non fatale.
SENTINELLA="$DATADIR/.demo-seed-done"
if [[ -e "$SENTINELLA" ]]; then
  echo "    gia' fatto in una installazione precedente, salto"
else
  # Il database lo crea il servizio al primo avvio: aspettiamo che ci sia.
  for _ in $(seq 1 20); do [[ -f "$DATADIR/clg.db" ]] && break; sleep 0.5; done
  if [[ -f "$DATADIR/clg.db" && -f "$QUI/api/codici-dimostrativi.csv" ]]; then
    if clgadmin importa "$QUI/api/codici-dimostrativi.csv" --lotto "dimostrativi" >/dev/null 2>&1; then
      touch "$SENTINELLA"
      echo "    seminati 3 codici di prova (via con: clgadmin svuota --conferma)"
    else
      echo "    ATTENZIONE: seme demo non riuscito (non blocca l'installazione)" >&2
    fi
  else
    echo "    database non ancora pronto, seme demo saltato" >&2
  fi
fi

echo "==> 5/8 nginx"
CONF=/etc/nginx/sites-available/autenticatore
# Una volta che certbot ha aggiunto il blocco HTTPS (listen 443), non
# rigeneriamo il file: lo sovrascriveremmo riportando il sito a solo HTTP.
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

echo "==> 5b/8 pannello di amministrazione"
install -m 755 "$QUI/api/admin_server.py" "$APPDIR/admin_server.py"
install -m 755 "$QUI/api/imposta-password-pannello" /usr/local/bin/imposta-password-pannello
install -m 644 "$QUI/api/autenticatore-pannello.service" \
  /etc/systemd/system/autenticatore-pannello.service
systemctl daemon-reload
systemctl enable autenticatore-pannello >/dev/null
systemctl restart autenticatore-pannello
# Pezzi nginx del pannello: il limite d'accesso (contesto http) e la location
# (inclusa nel server del sito). Restano fuori dal file che tocca certbot.
install -m 644 "$QUI/nginx/pannello.http.conf" /etc/nginx/conf.d/autenticatore-pannello.conf
install -d /etc/nginx/snippets
install -m 644 "$QUI/nginx/pannello.location.conf" /etc/nginx/snippets/autenticatore-pannello.conf
# Password iniziale: se non e' ancora impostata, ne creiamo una casuale forte e
# la salviamo in un file leggibile solo da root. Si cambia con:
#   imposta-password-pannello
HTP=/etc/nginx/.htpasswd-autenticatore
if [[ ! -s "$HTP" ]]; then
  # Password casuale robusta. Niente "tr </dev/urandom | head": sotto
  # set -o pipefail quel costrutto fa uscire tr con SIGPIPE e lo script
  # abortirebbe. La generiamo con python (gia' presente).
  PW="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)))')"
  umask 077
  printf 'admin:%s\n' "$(openssl passwd -apr1 "$PW")" > "$HTP"
  chmod 640 "$HTP"; chown root:www-data "$HTP" 2>/dev/null || true
  printf 'Pannello di amministrazione\nindirizzo: /pannello/\nutente: admin\npassword: %s\n' "$PW" > /root/pannello-password.txt
  chmod 600 /root/pannello-password.txt
  echo "    credenziali iniziali salvate in /root/pannello-password.txt"
  echo "    (cambiale quando vuoi con: imposta-password-pannello)"
else
  echo "    accesso al pannello gia' configurato"
fi
# Includiamo la location del pannello nel server del sito (una volta sola).
if ! grep -q "autenticatore-pannello.conf" "$CONF"; then
  sed -i '/root \/var\/www\/autenticatore;/a\    include snippets/autenticatore-pannello.conf;' "$CONF"
fi
nginx -t
systemctl reload nginx
if systemctl is-active --quiet autenticatore-pannello; then
  echo "    pannello attivo su /pannello/"
else
  echo "    ATTENZIONE: il pannello non e' partito: journalctl -u autenticatore-pannello -n 30" >&2
fi

echo "==> 6/8 firewall"
if [[ "$FIREWALL" == "no-firewall" ]]; then
  echo "    saltato su richiesta"
else
  # La porta SSH da tenere aperta la prendiamo dalla connessione in corso
  # (SSH_CONNECTION: l'ultimo campo e' la porta del server): e' esattamente
  # quella da cui sei entrato, quindi non c'e' modo di chiudersi fuori. In
  # mancanza la chiediamo a sshd (che risolve anche gli Include di
  # sshd_config.d), infine 22.
  SSHPORT="$(awk '{print $4}' <<<"${SSH_CONNECTION:-}")"
  if ! [[ "$SSHPORT" =~ ^[0-9]+$ ]]; then
    SSHPORT="$(sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}' || true)"
  fi
  [[ "$SSHPORT" =~ ^[0-9]+$ ]] || SSHPORT=22
  ufw allow "$SSHPORT/tcp" >/dev/null
  ufw allow 'Nginx Full' >/dev/null
  ufw --force enable >/dev/null
  echo "    attivo (SSH su $SSHPORT, web aperto)"
fi

echo "==> 7/8 certificato HTTPS"
# Stesso criterio del passo 5: se il file nginx non ha ancora il blocco 443,
# chiediamo (o ri-applichiamo) il certificato. Con --keep-until-expiring
# certbot riusa un certificato gia' emesso invece di richiederne un altro.
if grep -q "listen 443" "$CONF"; then
  echo "    HTTPS gia' configurato, rinnovo automatico gestito da certbot"
else
  CERTLOG="$(mktemp)"
  if certbot --nginx -d "$DOMINIO" --non-interactive --agree-tos -m "$EMAIL" \
       --redirect --keep-until-expiring >"$CERTLOG" 2>&1; then
    echo "    certificato ottenuto, rinnovo automatico attivo"
  else
    echo "    NON riuscito. Ultime righe di certbot:" >&2
    tail -n 8 "$CERTLOG" | sed 's/^/      /' >&2
    echo "    (Spesso e' il DNS che non punta ancora qui. Sistemato quello, rilancia:" >&2
    echo "     certbot --nginx -d $DOMINIO --redirect )" >&2
  fi
  rm -f "$CERTLOG"
fi

echo "==> 8/8 verifica finale"
systemctl reload nginx
if curl -fsS --max-time 5 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
  echo "    servizio di verifica attivo"
else
  echo "    servizio non raggiungibile: journalctl -u autenticatore-api -n 30" >&2
fi

PROTO=http
grep -q "listen 443" "$CONF" && PROTO=https
cat <<FINE

Fatto.

  Sito       $PROTO://$DOMINIO
  Test QR    $PROTO://$DOMINIO/?clg=123456789012
  Pannello   $PROTO://$DOMINIO/pannello/   (utente e password in /root/pannello-password.txt)

Dal pannello carichi la lista dei codici e vedi le statistiche, senza riga di
comando. In alternativa, da qui:

  clgadmin svuota --conferma
  clgadmin importa /root/codici.csv --lotto "primo-lotto"
  clgadmin stato-lista

FINE
