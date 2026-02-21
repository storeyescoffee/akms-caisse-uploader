#!/bin/bash
set -euo pipefail

### CONFIG (must match config.conf logic) ###
BASE_DIR="$HOME/shared/POINTEX21/CAFEDEROME"     # same as config[local][base_dir]
STORE_ID="2"
API_URL="http://app.storeyes.io:8000/process"
LOG_FILE="./db_mb_watcher.log"
STATUS_FILE="/home/m0hcine24/caisse_status.txt"

SLEEP_INTERVAL=10
STABLE_SECONDS=2

# Status codes
STATUS_PENDING=0        # Waiting for caisse files
STATUS_SUCCESS=1        # Upload success
STATUS_FAILED=2         # Failed
STATUS_FALLBACK=3       # Success after fallback (morning retry after failure)
STATUS_UNKNOWN=5        # Unknown error

# -------------------------
# DATE OFFSET ARGUMENT
# -------------------------
DAY_OFFSET="${1:-0}"   # default = today

if ! [[ "$DAY_OFFSET" =~ ^-?[0-9]+$ ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ Invalid argument: $DAY_OFFSET (must be integer like -1, -2, 0)" >> "$LOG_FILE"
    echo "$STATUS_UNKNOWN" > "$STATUS_FILE"
    exit 1
fi

##########################################




log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

set_status() {
    local status=$1
    local message="${2:-}"
    echo "$status" > "$STATUS_FILE"
    log "📊 Status: $status - $message"
}

is_stable() {
    local file="$1"
    local s1 s2
    s1=$(stat -c%s "$file")
    sleep "$STABLE_SECONDS"
    s2=$(stat -c%s "$file")
    [[ "$s1" == "$s2" ]]
}

### Date logic (SAME as Python) ###
TARGET_DATE=$(date -d "$DAY_OFFSET day" '+%Y-%m-%d')
TODAY_MMDDYY=$(date -d "$DAY_OFFSET day" '+%m%d%y')
YEAR=$(date -d "$DAY_OFFSET day" '+%Y')
YEAR_DIR="${BASE_DIR}/AN${YEAR}"

log "▶️ Watcher started"
log "📂 Watching: $YEAR_DIR"
log "⏪ Date offset: $DAY_OFFSET day(s)"
log "🗓️  Target date (MMDDYY): $TODAY_MMDDYY"

# Check previous status if day offset is set (not 0)
if [[ "$DAY_OFFSET" -ne 0 ]]; then
    if [[ -f "$STATUS_FILE" ]]; then
        PREVIOUS_STATUS=$(cat "$STATUS_FILE")
        if [[ "$PREVIOUS_STATUS" == "$STATUS_SUCCESS" ]]; then
            log "✅ Previous status was SUCCESS, skipping processing"
            exit 0
        else
            log "⚠️  Previous status was $PREVIOUS_STATUS, attempting fallback"
        fi
    else
        log "ℹ️  No previous status file found, proceeding with fallback"
    fi
fi

sudo mount -a

if [[ ! -d "$YEAR_DIR" ]]; then
    log "❌ Year directory not found: $YEAR_DIR"
    set_status $STATUS_FAILED "Year directory not found"
    exit 1
fi

echo "==============================" > "$LOG_FILE"
set_status $STATUS_PENDING "Waiting for caisse files"

while true; do

    DB_FILE=$(ls "$YEAR_DIR"/"VD$TODAY_MMDDYY".DB 2>/dev/null | head -n 1 || true)
    MB_FILE=$(ls "$YEAR_DIR"/"VD$TODAY_MMDDYY".MB 2>/dev/null | head -n 1 || true)

    if [[ -n "$DB_FILE" && -n "$MB_FILE" ]]; then
        log "📄 Found matching files"
        log "   DB: $DB_FILE"
        log "   MB: $MB_FILE"

        if is_stable "$DB_FILE" && is_stable "$MB_FILE"; then
            log "✅ Files are stable, sending API request"

            # Determine if this is a fallback attempt (day offset is set, not 0)
            IS_FALLBACK=false
            if [[ "$DAY_OFFSET" -ne 0 ]]; then
                IS_FALLBACK=true
            fi

            if curl --fail --silent --show-error \
                --location "$API_URL" \
                --form "delta_hour=2" \
                --form "store_id=$STORE_ID" \
                --form "file=@$DB_FILE" \
                --form "mb_file=@$MB_FILE" >> "$LOG_FILE" 2>&1; then

                if [[ "$IS_FALLBACK" == true ]]; then
                    set_status $STATUS_FALLBACK "Success after fallback (morning retry)"
                    log "🚀 API call succeeded (fallback mode)"
                else
                    set_status $STATUS_SUCCESS "Upload success"
                    log "🚀 API call succeeded"
                fi
                exit 0
            else
                set_status $STATUS_FAILED "API call failed"
                log "❌ API call failed"
                exit 1
            fi
        else
            log "⏳ Files exist but still changing"
        fi
    else
        log "⌛ Waiting for DB + MB files for $TODAY_MMDDYY"
    fi

    sleep "$SLEEP_INTERVAL"
done