#!/usr/bin/env bash
set -euo pipefail

#############################################
# CONFIG (override via env)
#############################################

BOARD_ID=$(awk '/Serial/ {print $3}' /proc/cpuinfo)

MQTT_HOST="${MQTT_HOST:-18.100.207.236}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_USER="${MQTT_USER:-storeyes}"
MQTT_PASS="${MQTT_PASS:-12345}"
MQTT_TOPIC="${MQTT_TOPIC:-storeyes/$BOARD_ID/caisse}"
QOS="${QOS:-1}"
RETAIN="${RETAIN:-false}"
TIMEOUT="${TIMEOUT:-5}"
RETRIES="${RETRIES:-3}"

#############################################
# CHECK CAISSE STATUS
#############################################

CAISSE_STATUS_FILE="$HOME/caisse_status.txt"

if [[ ! -f "$CAISSE_STATUS_FILE" ]]; then
    echo "ℹ️ Caisse status file not found. Skipping..."
    exit 0
fi

CAISSE_STATUS=$(cat "$CAISSE_STATUS_FILE")

#############################################
# BUILD JSON PAYLOAD
#############################################

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

RAW_PAYLOAD=$(cat <<EOF
{
  "board_id": "$BOARD_ID",
  "timestamp": "$TIMESTAMP",
  "caisse-status": "$CAISSE_STATUS"
}
EOF
)

if command -v jq >/dev/null 2>&1; then
    FINAL_PAYLOAD=$(echo "$RAW_PAYLOAD" | jq -c .)
else
    FINAL_PAYLOAD="$RAW_PAYLOAD"
fi

#############################################
# MQTT RETRY LOOP
#############################################

attempt=1
while [[ $attempt -le $RETRIES ]]; do
    echo "📡 Publishing caisse status (attempt $attempt/$RETRIES)"

    if timeout "$TIMEOUT" mosquitto_pub \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -u "$MQTT_USER" \
        -P "$MQTT_PASS" \
        -t "$MQTT_TOPIC" \
        -m "$FINAL_PAYLOAD" \
        -q "$QOS" \
        $( [[ "$RETAIN" == "true" ]] && echo "-r" ); then

        echo "✅ Caisse status sent"
        exit 0
    fi

    echo "⚠️ Publish failed. Retrying..."
    sleep 2
    ((attempt++))
done

echo "❌ Failed after $RETRIES attempts"
exit 1

