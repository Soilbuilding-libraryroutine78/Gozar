#!/usr/bin/env bash
# Install the generic Gozar nginx vhost and request a Let's Encrypt certificate.
#
# Run as root from the repository root or any path:
#     sudo GOZAR_DOMAIN=gozar.example.com CERTBOT_EMAIL=admin@example.com \
#       bash deploy/nginx/setup-domain.sh
#
# The script installs deploy/nginx/gozar-http.conf first, reloads nginx, then lets
# certbot upgrade the site to HTTPS. The Gozar containers should already be running
# on the loopback ports from compose.prod.yml.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SITES_AVAIL=/etc/nginx/sites-available
SITES_EN=/etc/nginx/sites-enabled

GOZAR_DOMAIN="${GOZAR_DOMAIN:-gozar.example.com}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@example.com}"
SITE_NAME="gozar-${GOZAR_DOMAIN}"

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must run as root (use: sudo bash $0)" >&2
    exit 1
fi

if [ "$GOZAR_DOMAIN" = "gozar.example.com" ]; then
    echo "Set GOZAR_DOMAIN to your real domain before requesting a certificate." >&2
    exit 1
fi

sed "s/gozar.example.com/${GOZAR_DOMAIN}/g" \
    "$REPO_DIR/deploy/nginx/gozar-http.conf" > "$SITES_AVAIL/$SITE_NAME"
ln -sf "$SITES_AVAIL/$SITE_NAME" "$SITES_EN/$SITE_NAME"

nginx -t
systemctl reload nginx

certbot --nginx --redirect --non-interactive --agree-tos \
    -m "$CERTBOT_EMAIL" -d "$GOZAR_DOMAIN"

nginx -t
systemctl reload nginx
echo "Done. Try: https://${GOZAR_DOMAIN}"
