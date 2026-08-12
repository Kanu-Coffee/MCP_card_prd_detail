#!/bin/sh
set -eu

umask 077

read_secret() {
    secret_name=$1
    secret_path=$2
    if [ ! -r "$secret_path" ]; then
        echo "required Keycloak secret is not readable: $secret_name" >&2
        exit 1
    fi
    secret_value=$(tr -d '\r\n' < "$secret_path")
    if [ -z "$secret_value" ]; then
        echo "required Keycloak secret is empty: $secret_name" >&2
        exit 1
    fi
    printf '%s' "$secret_value"
}

KC_DB_PASSWORD=$(read_secret db_password "${KC_DB_PASSWORD_FILE:-/run/secrets/keycloak_db_password}")
export KC_DB_PASSWORD

# Bootstrap credentials are consumed only while Keycloak creates the master
# realm. Keep their files out of image layers and remove the secret mounts from
# long-lived production configuration after the first administrator is rotated.
if [ -n "${KC_BOOTSTRAP_ADMIN_PASSWORD_FILE:-}" ] && [ -r "$KC_BOOTSTRAP_ADMIN_PASSWORD_FILE" ]; then
    KC_BOOTSTRAP_ADMIN_PASSWORD=$(
        read_secret admin_password "$KC_BOOTSTRAP_ADMIN_PASSWORD_FILE"
    )
    export KC_BOOTSTRAP_ADMIN_PASSWORD
fi

exec /opt/keycloak/bin/kc.sh "$@"
