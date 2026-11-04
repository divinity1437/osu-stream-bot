#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

readonly PROJECT_DIR=$PWD
readonly ENV_FILE="$PROJECT_DIR/.env"
readonly EXAMPLE_FILE="$PROJECT_DIR/.env.example"
readonly BACKTITLE="osu! replay stream — .env configuration"

declare -a SETTINGS=()
declare -A DEFAULTS=()
declare -A VALUES=()
declare -A TYPES=()
declare -A LABELS=()
declare -A HINTS=()
declare -A MIN_VALUES=()
declare -A MAX_VALUES=()
declare -A KNOWN=()

declare -a ORDR_KEYS=()
declare -a DANSER_KEYS=()
declare -a SOURCE_KEYS=()
declare -a QUEUE_KEYS=()
declare -a WEB_KEYS=()
declare -a DISCORD_KEYS=()
declare -a BROADCASTER_KEYS=()

DIRTY=0
ACTIVE_TMP=""
VALIDATION_ERROR=""
declare -a VALIDATION_ERRORS=()
declare -a VALIDATION_WARNINGS=()

cleanup() {
    if [[ -n "$ACTIVE_TMP" && -e "$ACTIVE_TMP" ]]; then
        rm -f -- "$ACTIVE_TMP"
    fi
}

trap cleanup EXIT
trap 'exit 130' HUP INT TERM

define_setting() {
    local key=$1
    local default=$2
    local type=$3
    local label=$4
    local hint=$5
    local minimum=${6-}
    local maximum=${7-}

    SETTINGS+=("$key")
    KNOWN["$key"]=1
    DEFAULTS["$key"]=$default
    VALUES["$key"]=$default
    TYPES["$key"]=$type
    LABELS["$key"]=$label
    HINTS["$key"]=$hint
    MIN_VALUES["$key"]=$minimum
    MAX_VALUES["$key"]=$maximum
}

define_setting "RUN_MODE" "standalone" "enum" \
    "Operation mode" \
    "standalone: all-in-one local stream; controller: multi-node coordinator & web queue; render-worker: distributed danser rendering node; broadcaster: dedicated RTMP stream broadcaster node."

define_setting "RENDER_BACKEND" "ordr" "enum" \
    "Render backend" \
    "ordr sends replays to the hosted o!rdr service; danser renders locally on this computer."

define_setting "ORDR_DEVELOPER_MODE" "true" "bool" \
    "o!rdr developer mode" \
    "Creates simulated jobs without rendering real videos. Keep it enabled for the first interface test."
define_setting "ORDR_API_KEY" "" "secret" \
    "o!rdr API key" \
    "Key for production rendering. Never share it or commit your .env file to Git."
define_setting "ORDR_SKIN" "whitecatCK1.0" "text" \
    "Standard o!rdr skin" \
    "Skin name used for replays without DT or NC."
define_setting "ORDR_CUSTOM_SKIN" "false" "bool" \
    "Standard skin is custom" \
    "Enable this when ORDR_SKIN contains a custom skin ID instead of a public skin name."
define_setting "ORDR_DT_SKIN" "RafisHDDT2018-03-26" "text" \
    "o!rdr DT/NC skin" \
    "Skin name used for replays with Double Time or Nightcore."
define_setting "ORDR_DT_CUSTOM_SKIN" "false" "bool" \
    "DT/NC skin is custom" \
    "Enable this when ORDR_DT_SKIN contains a custom skin ID."
define_setting "ORDR_RESOLUTION" "1280x720" "enum" \
    "o!rdr resolution" \
    "Resolution of rendered videos. 1280x720 is recommended for this project's Browser Source."
define_setting "ORDR_RENDER_TIMEOUT_SECONDS" "1800" "integer" \
    "o!rdr render timeout" \
    "Maximum number of seconds to wait for one o!rdr job." "60" ""
define_setting "ORDR_MIN_SUBMIT_INTERVAL_SECONDS" "0" "decimal" \
    "o!rdr submission interval" \
    "Minimum delay between jobs. 0 selects a delay automatically based on developer mode and the API key." "0" ""

define_setting "DANSER_DIR" "tools/danser" "text" \
    "danser directory" \
    "Path to the local danser installation. Relative paths are resolved from the project directory."
define_setting "DANSER_SETTINGS" "stream" "profile" \
    "danser settings profile" \
    "JSON profile name without path or extension (default: stream). Use menu option [D] to auto-detect system capabilities and generate an optimal profile."
define_setting "DANSER_SKINS_DIR" "auto" "skin_dir" \
    "osu! skins directory" \
    "Use auto to detect common osu!stable/Wine locations, or enter an explicit directory containing skin folders."
define_setting "DANSER_SKIN" "default" "skin" \
    "danser skin" \
    "Fixed local skin. The TUI lists skins from DANSER_SKINS_DIR; default uses danser's built-in skin."
define_setting "DANSER_RANDOM_SKIN" "false" "bool" \
    "Random danser skin" \
    "Select a different detected custom skin for every replay. DANSER_SKIN remains the fallback."
define_setting "DANSER_USE_SKIN_CURSOR" "true" "bool" \
    "Use cursor from skin" \
    "Use the selected osu! skin's cursor and cursor trail instead of danser's built-in cursor."
define_setting "DANSER_REPLAY_OVERLAY" "true" "bool" \
    "Replay information overlay" \
    "Show mod-adjusted star rating, replay username, and replay mods in the rendered video."
define_setting "DANSER_BACKGROUND_DIM_PERCENT" "95" "integer" \
    "Gameplay background dim" \
    "Background darkness during gameplay, from 0 (no dim) to 100 (black)." "0" "100"
define_setting "DANSER_PARALLAX_ENABLED" "true" "bool" \
    "Background parallax" \
    "Move and slightly scale the beatmap background based on cursor movement."
define_setting "DANSER_PARALLAX_AMOUNT_PERCENT" "10" "integer" \
    "Parallax amount" \
    "Background movement and extra scale in percent. danser's recommended default is 10." "0" "100"
define_setting "DANSER_PARALLAX_SPEED_PERCENT" "50" "integer" \
    "Parallax response speed" \
    "How quickly the background follows cursor movement, in percent. danser's recommended default is 50." "0" "100"
define_setting "DANSER_RENDER_TIMEOUT_SECONDS" "3600" "integer" \
    "danser render timeout" \
    "Maximum local render time for one replay, in seconds." "60" ""
define_setting "DANSER_MAX_MAP_MIB" "300" "integer" \
    "danser beatmap limit" \
    "Maximum downloaded beatmap archive size in MiB." "10" ""
define_setting "DANSER_MAX_SONGS_MIB" "5120" "integer" \
    "danser songs directory quota" \
    "Maximum size of tools/danser/songs in MiB (minimum: 512). Old beatmaps are pruned when exceeded." "512" ""

define_setting "OSU_REPLAY_URL" "https://api.osuokayu.pw/v1/get_replay" "url" \
    "Replay download API" \
    "HTTP(S) API URL used by the producer to fetch a random .osr file."
define_setting "OSU_SCORES_URL" "https://api.osuokayu.pw/v2/scores" "url" \
    "Score list API" \
    "HTTP(S) API URL used to discover the range of available scores."
define_setting "SCORE_ID_MIN" "1" "integer" \
    "Minimum score ID" \
    "Lower bound for random score selection; must be at least 1." "1" ""
define_setting "SCORE_ID_MAX" "0" "integer" \
    "Maximum score ID" \
    "Upper score bound. 0 discovers the latest score automatically through OSU_SCORES_URL." "0" ""
define_setting "RANDOM_REPLAY_ATTEMPTS" "150" "integer" \
    "Replay lookup attempts" \
    "Number of random scores to try before the producer pauses temporarily." "1" ""

define_setting "BUFFER_SIZE" "2" "integer" \
    "Video buffer size" \
    "Number of ready videos including the currently playing one. 2 means one playing and one prepared." "1" ""
define_setting "DELETE_AFTER_PLAY" "true" "bool" \
    "Delete videos after playback" \
    "When disabled, played MP4 files are moved to archive/."
define_setting "PRODUCER_ENABLED" "true" "bool" \
    "Automatic producer" \
    "Automatically finds and renders replays to fill the buffer. Manual uploads work independently."

define_setting "WEB_HOST" "127.0.0.1" "text" \
    "Web server address" \
    "127.0.0.1 allows access only from this computer. 0.0.0.0 exposes the server to the local network."
define_setting "WEB_PORT" "8787" "integer" \
    "Web server port" \
    "TCP port used by the player, control panel, and API." "1" "65535"
define_setting "MAX_UPLOAD_MIB" "20" "integer" \
    "Replay upload limit" \
    "Maximum size of an .osr file uploaded through /control, in MiB." "1" ""
define_setting "MAX_BACKGROUND_MIB" "20" "integer" \
    "Temporary background limit" \
    "Maximum waiting-background image size in MiB." "1" ""
define_setting "PLAYER_VOLUME" "20" "integer" \
    "Initial player volume" \
    "Volume from 0 to 100 on first launch. Later changes are stored in state.json." "0" "100"

define_setting "DISCORD_ENABLED" "false" "bool" \
    "Discord replay intake" \
    "Starts the Discord bot with /submit, /status, /queue, and /cancel slash commands."
define_setting "DISCORD_TOKEN" "" "secret" \
    "Discord bot token" \
    "Bot token from the Discord Developer Portal. Never share it."
define_setting "DISCORD_GUILD_ID" "0" "integer" \
    "Discord server (guild) ID" \
    "Numeric server ID where slash commands will be registered." "0" ""
define_setting "DISCORD_CHANNEL_ID" "0" "integer" \
    "Discord channel ID" \
    "Numeric channel ID that accepts replay submissions." "0" ""
define_setting "DISCORD_QUEUE_LIMIT" "50" "integer" \
    "Discord queue limit" \
    "Maximum number of active Discord submissions." "1" ""
define_setting "DISCORD_USER_COOLDOWN_SECONDS" "300" "integer" \
    "Discord user cooldown" \
    "Delay between submissions from one user, in seconds. 0 disables the cooldown." "0" ""
define_setting "DISCORD_MAX_FILE_MIB" "10" "integer" \
    "Discord file limit" \
    "Maximum size of a Discord .osr attachment in MiB." "1" ""

define_setting "RENDER_API_TOKEN" "" "secret" \
    "Cluster API authentication token" \
    "Bearer token used between Controller and Workers. Required when RUN_MODE is controller or render-worker."
define_setting "RENDER_API_HOST" "127.0.0.1" "text" \
    "Controller internal API host" \
    "Host/IP on Controller for internal worker API (e.g. 127.0.0.1 or WireGuard IP). Do not expose publicly!"
define_setting "RENDER_API_PORT" "8790" "integer" \
    "Controller internal API port" \
    "Port on Controller for internal worker API." "1" "65535"
define_setting "MAX_ARTIFACT_MIB" "1024" "integer" \
    "Max render artifact size (MiB)" \
    "Maximum size in MiB of a completed video artifact uploaded to the controller." "10" ""
define_setting "CONTROLLER_URL" "" "optional_text" \
    "Controller API URL (for worker)" \
    "URL of Controller internal API (for example http://127.0.0.1:8790 or http://10.10.0.1:8790). Required on worker."
define_setting "WORKER_ID" "" "optional_text" \
    "Worker identifier" \
    "Custom unique identifier for this render worker node (leave empty for worker-<hostname>)."
define_setting "RENDER_HEARTBEAT_SECONDS" "15.0" "decimal" \
    "Worker lease heartbeat interval" \
    "Seconds between worker lease heartbeats while rendering a replay (range: 1.0 to 20.0)."

define_setting "STREAM_TWITCH_KEY" "" "secret" \
    "Twitch Stream Key" \
    "Stream key for broadcasting to Twitch. Leave empty if Twitch is not used."
define_setting "STREAM_TWITCH_URL" "rtmp://live.twitch.tv/app/" "text" \
    "Twitch Ingest Server URL" \
    "RTMP ingest server URL for Twitch (default: rtmp://live.twitch.tv/app/)."
define_setting "STREAM_YOUTUBE_KEY" "" "secret" \
    "YouTube Stream Key" \
    "Stream key for broadcasting to YouTube Live. Leave empty if YouTube is not used."
define_setting "STREAM_YOUTUBE_URL" "rtmp://a.rtmp.youtube.com/live2/" "text" \
    "YouTube Ingest Server URL" \
    "RTMP ingest server URL for YouTube (default: rtmp://a.rtmp.youtube.com/live2/)."
define_setting "STREAM_CUSTOM_RTMP_URL" "" "optional_text" \
    "Custom RTMP Output URL" \
    "Optional third RTMP destination URL (for example local relay, Kick, or nginx-rtmp)."
define_setting "STREAM_API_URL" "http://127.0.0.1:8787" "text" \
    "Controller API URL" \
    "HTTP address of the Controller web API used to retrieve replays and report completion."
define_setting "STREAM_STANDBY_PATH" "tools/standby.mp4" "text" \
    "Standby Video Path" \
    "Path to the 60 FPS H.264 standby video loop played when the replay queue is empty."
define_setting "STREAM_POLL_INTERVAL_SECONDS" "1.0" "decimal" \
    "Broadcaster Poll Interval" \
    "Seconds between polling the controller API during playback and standby." "0.5" "10.0"
define_setting "STREAM_DRY_RUN" "false" "bool" \
    "Broadcaster Dry-Run" \
    "Test mode without sending stream to RTMP servers (outputs to null sink)."

BROADCASTER_KEYS=(
    STREAM_TWITCH_KEY STREAM_TWITCH_URL STREAM_YOUTUBE_KEY STREAM_YOUTUBE_URL
    STREAM_CUSTOM_RTMP_URL STREAM_API_URL STREAM_STANDBY_PATH
    STREAM_POLL_INTERVAL_SECONDS STREAM_DRY_RUN
)

ORDR_KEYS=(
    ORDR_DEVELOPER_MODE ORDR_API_KEY ORDR_SKIN ORDR_CUSTOM_SKIN
    ORDR_DT_SKIN ORDR_DT_CUSTOM_SKIN ORDR_RESOLUTION
    ORDR_RENDER_TIMEOUT_SECONDS ORDR_MIN_SUBMIT_INTERVAL_SECONDS
)
DANSER_KEYS=(
    DANSER_DIR DANSER_SETTINGS DANSER_SKINS_DIR DANSER_SKIN DANSER_RANDOM_SKIN DANSER_USE_SKIN_CURSOR
    DANSER_REPLAY_OVERLAY DANSER_BACKGROUND_DIM_PERCENT DANSER_PARALLAX_ENABLED
    DANSER_PARALLAX_AMOUNT_PERCENT DANSER_PARALLAX_SPEED_PERCENT
    DANSER_RENDER_TIMEOUT_SECONDS DANSER_MAX_MAP_MIB DANSER_MAX_SONGS_MIB
)
SOURCE_KEYS=(
    OSU_REPLAY_URL OSU_SCORES_URL SCORE_ID_MIN SCORE_ID_MAX
    RANDOM_REPLAY_ATTEMPTS
)
QUEUE_KEYS=(BUFFER_SIZE DELETE_AFTER_PLAY PRODUCER_ENABLED)
WEB_KEYS=(WEB_HOST WEB_PORT MAX_UPLOAD_MIB MAX_BACKGROUND_MIB PLAYER_VOLUME)
DISCORD_KEYS=(
    DISCORD_ENABLED DISCORD_TOKEN DISCORD_GUILD_ID DISCORD_CHANNEL_ID
    DISCORD_QUEUE_LIMIT DISCORD_USER_COOLDOWN_SECONDS DISCORD_MAX_FILE_MIB
)
CLUSTER_KEYS=(
    RUN_MODE RENDER_API_TOKEN RENDER_API_HOST RENDER_API_PORT MAX_ARTIFACT_MIB
    CONTROLLER_URL WORKER_ID RENDER_HEARTBEAT_SECONDS
    DANSER_BACKGROUND_DIM_PERCENT DANSER_PARALLAX_ENABLED
    DANSER_PARALLAX_AMOUNT_PERCENT DANSER_PARALLAX_SPEED_PERCENT
    DANSER_SKIN DANSER_RANDOM_SKIN
)

trim_whitespace() {
    local value=$1
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

parse_assignment() {
    local line=$1
    PARSED_KEY=""
    PARSED_VALUE=""

    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
        PARSED_KEY=${BASH_REMATCH[1]}
        PARSED_VALUE=$(trim_whitespace "${BASH_REMATCH[2]}")
        if (( ${#PARSED_VALUE} >= 2 )); then
            local first=${PARSED_VALUE:0:1}
            local last=${PARSED_VALUE: -1}
            if [[ ( "$first" == '"' && "$last" == '"' ) || ( "$first" == "'" && "$last" == "'" ) ]]; then
                PARSED_VALUE=${PARSED_VALUE:1:${#PARSED_VALUE}-2}
            fi
        fi
        return 0
    fi
    return 1
}

load_values() {
    local path=$1
    local line
    [[ -f "$path" ]] || return 0

    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if parse_assignment "$line" && [[ -n "${KNOWN[$PARSED_KEY]+x}" ]]; then
            VALUES["$PARSED_KEY"]=$PARSED_VALUE
        fi
    done < "$path"
}

load_values "$EXAMPLE_FILE"
for key in "${SETTINGS[@]}"; do
    DEFAULTS["$key"]=${VALUES[$key]}
done
load_values "$ENV_FILE"

lowercase() {
    printf '%s' "${1,,}"
}

bool_is_valid() {
    case "$(lowercase "$1")" in
        true|false|1|0|yes|no|on|off) return 0 ;;
        *) return 1 ;;
    esac
}

bool_is_true() {
    case "$(lowercase "$1")" in
        true|1|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

normalize_decimal_integer() {
    local value=$1
    while (( ${#value} > 1 )) && [[ ${value:0:1} == "0" ]]; do
        value=${value:1}
    done
    printf '%s' "$value"
}

decimal_integer_less() {
    local left right
    left=$(normalize_decimal_integer "$1")
    right=$(normalize_decimal_integer "$2")
    if (( ${#left} < ${#right} )); then
        return 0
    fi
    if (( ${#left} > ${#right} )); then
        return 1
    fi
    [[ "$left" < "$right" ]]
}

skin_directory_has_skins() {
    local directory=$1
    [[ -d "$directory" ]] || return 1
    [[ -n "$(find "$directory" -mindepth 2 -maxdepth 2 -type f -iname 'skin.ini' -print -quit 2>/dev/null)" ]]
}

resolve_skin_directory_value() {
    local configured=$1
    local candidate danser_directory
    if [[ "${configured,,}" == "auto" ]]; then
        local -a candidates=(
            "$HOME/.local/share/osu-wine/osu!/Skins"
            "$HOME/.wine/drive_c/Program Files/osu!/Skins"
            "$HOME/.wine/drive_c/Program Files (x86)/osu!/Skins"
            "$HOME/Games/osu!/Skins"
            "$HOME/osu!/Skins"
            "$HOME/.osu/Skins"
            "$HOME/.local/share/osu/Skins"
            "$HOME/.var/app/sh.ppy.osu/data/osu/Skins"
        )
        for candidate in "${candidates[@]}"; do
            if skin_directory_has_skins "$candidate"; then
                printf '%s' "$candidate"
                return 0
            fi
        done
        danser_directory=${VALUES[DANSER_DIR]}
        if [[ "$danser_directory" == /* ]]; then
            printf '%s' "$danser_directory/skins"
        else
            printf '%s' "$PROJECT_DIR/$danser_directory/skins"
        fi
        return 0
    fi

    candidate=$configured
    if [[ "$candidate" == "~/"* ]]; then
        candidate="$HOME/${candidate:2}"
    elif [[ "$candidate" != /* ]]; then
        candidate="$PROJECT_DIR/$candidate"
    fi
    printf '%s' "$candidate"
}

validate_value() {
    local key=$1
    local value=$2
    local type=${TYPES[$key]}
    local minimum=${MIN_VALUES[$key]}
    local maximum=${MAX_VALUES[$key]}
    VALIDATION_ERROR=""

    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        VALIDATION_ERROR="the value must not contain a line break"
        return 1
    fi

    case "$type" in
        secret)
            return 0
            ;;
        bool)
            if ! bool_is_valid "$value"; then
                VALIDATION_ERROR="enter true or false"
                return 1
            fi
            ;;
        enum)
            case "$key:$value" in
                RUN_MODE:standalone|RUN_MODE:controller|RUN_MODE:render-worker|\
                RENDER_BACKEND:ordr|RENDER_BACKEND:danser|\
                ORDR_RESOLUTION:960x540|ORDR_RESOLUTION:1280x720|\
                ORDR_RESOLUTION:1920x1080|ORDR_RESOLUTION:3840x2160) ;;
                *)
                    VALIDATION_ERROR="unsupported value"
                    return 1
                    ;;
            esac
            ;;
        integer)
            if [[ ! "$value" =~ ^[0-9]+$ ]]; then
                VALIDATION_ERROR="enter a non-negative integer"
                return 1
            fi
            if [[ -n "$minimum" ]] && decimal_integer_less "$value" "$minimum"; then
                VALIDATION_ERROR="minimum value: $minimum"
                return 1
            fi
            if [[ -n "$maximum" ]] && decimal_integer_less "$maximum" "$value"; then
                VALIDATION_ERROR="maximum value: $maximum"
                return 1
            fi
            ;;
        decimal)
            if [[ ! "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
                VALIDATION_ERROR="enter a non-negative number (for example 0, 5, or 2.5)"
                return 1
            fi
            ;;
        url)
            if [[ "$value" != http://* && "$value" != https://* ]]; then
                VALIDATION_ERROR="the URL must start with http:// or https://"
                return 1
            fi
            ;;
        profile)
            if [[ -z "$value" || "$value" == *'/'* || "$value" == *'\'* ]]; then
                VALIDATION_ERROR="enter a simple profile name without / or \\"
                return 1
            fi
            ;;
        skin_dir)
            if [[ -z "$value" ]]; then
                VALIDATION_ERROR="enter auto or a directory path"
                return 1
            fi
            if [[ "${value,,}" != "auto" ]]; then
                local skin_directory
                skin_directory=$(resolve_skin_directory_value "$value")
                if [[ ! -d "$skin_directory" ]]; then
                    VALIDATION_ERROR="directory does not exist: $skin_directory"
                    return 1
                fi
            fi
            ;;
        skin)
            if [[ -z "$value" || "$value" == *'/'* || "$value" == *'\'* ]]; then
                VALIDATION_ERROR="enter a skin directory name without / or \\"
                return 1
            fi
            ;;
        text)
            if [[ -z "$value" ]]; then
                VALIDATION_ERROR="the value must not be empty"
                return 1
            fi
            ;;
        optional_text)
            return 0
            ;;
        *)
            VALIDATION_ERROR="unknown setting type: $type"
            return 1
            ;;
    esac
    return 0
}

validate_all() {
    local key value danser_dir skins_dir skin_count
    VALIDATION_ERRORS=()
    VALIDATION_WARNINGS=()

    for key in "${SETTINGS[@]}"; do
        value=${VALUES[$key]}
        if ! validate_value "$key" "$value"; then
            VALIDATION_ERRORS+=("$key: $VALIDATION_ERROR")
        fi
    done

    if [[ ${VALUES[SCORE_ID_MAX]} =~ ^[0-9]+$ && ${VALUES[SCORE_ID_MIN]} =~ ^[0-9]+$ ]] \
        && [[ ${VALUES[SCORE_ID_MAX]} != "0" ]] \
        && decimal_integer_less "${VALUES[SCORE_ID_MAX]}" "${VALUES[SCORE_ID_MIN]}"; then
        VALIDATION_ERRORS+=("SCORE_ID_MAX: must be 0 or greater than or equal to SCORE_ID_MIN")
    fi

    if [[ ${VALUES[RUN_MODE]} == "controller" ]]; then
        [[ -n "${VALUES[RENDER_API_TOKEN]}" ]] || VALIDATION_ERRORS+=("RENDER_API_TOKEN: required when RUN_MODE is controller")
    fi

    if [[ ${VALUES[RUN_MODE]} == "render-worker" ]]; then
        [[ -n "${VALUES[CONTROLLER_URL]}" ]] || VALIDATION_ERRORS+=("CONTROLLER_URL: required when RUN_MODE is render-worker")
        [[ -n "${VALUES[RENDER_API_TOKEN]}" ]] || VALIDATION_ERRORS+=("RENDER_API_TOKEN: required when RUN_MODE is render-worker")
    fi

    if bool_is_true "${VALUES[DISCORD_ENABLED]}"; then
        [[ -n "${VALUES[DISCORD_TOKEN]}" ]] || VALIDATION_ERRORS+=("DISCORD_TOKEN: required when Discord is enabled")
        [[ "${VALUES[DISCORD_GUILD_ID]}" != "0" ]] || VALIDATION_ERRORS+=("DISCORD_GUILD_ID: required when Discord is enabled")
        [[ "${VALUES[DISCORD_CHANNEL_ID]}" != "0" ]] || VALIDATION_ERRORS+=("DISCORD_CHANNEL_ID: required when Discord is enabled")
    fi

    if [[ ${VALUES[RENDER_BACKEND]} == "ordr" ]] \
        && ! bool_is_true "${VALUES[ORDR_DEVELOPER_MODE]}" \
        && [[ -z "${VALUES[ORDR_API_KEY]}" ]]; then
        VALIDATION_WARNINGS+=("o!rdr has a very strict request rate limit without an API key.")
    fi

    if [[ ${VALUES[RENDER_BACKEND]} == "danser" && ${VALUES[RUN_MODE]} != "controller" ]]; then
        danser_dir=${VALUES[DANSER_DIR]}
        [[ "$danser_dir" == /* ]] || danser_dir="$PROJECT_DIR/$danser_dir"
        if [[ ! -x "$danser_dir/danser-cli" && ! -f "$danser_dir/danser-cli.exe" ]]; then
            VALIDATION_WARNINGS+=("Executable $danser_dir/danser-cli (or danser-cli.exe) was not found. Run ./install-danser.sh (or install-danser.bat) first.")
        fi
        skins_dir=$(resolve_skin_directory_value "${VALUES[DANSER_SKINS_DIR]}")
        skin_count=$(find "$skins_dir" -mindepth 2 -maxdepth 2 -type f -iname 'skin.ini' -printf . 2>/dev/null | wc -c)
        if bool_is_true "${VALUES[DANSER_RANDOM_SKIN]}" && (( skin_count == 0 )); then
            VALIDATION_WARNINGS+=("Random danser skin is enabled, but no custom skins were found in $skins_dir.")
        elif [[ ${VALUES[DANSER_SKIN]} != "default" ]]; then
            if [[ -z "$(find "$skins_dir/${VALUES[DANSER_SKIN]}" -maxdepth 1 -type f -iname 'skin.ini' -print -quit 2>/dev/null)" ]]; then
                VALIDATION_WARNINGS+=("Configured danser skin ${VALUES[DANSER_SKIN]} was not found in $skins_dir; danser will use default.")
            fi
        fi
    fi

    if [[ ${VALUES[WEB_HOST]} != "127.0.0.1" && ${VALUES[WEB_HOST]} != "localhost" && ${VALUES[WEB_HOST]} != "::1" ]]; then
        VALIDATION_WARNINGS+=("WEB_HOST=${VALUES[WEB_HOST]} may expose /control to other devices. This project does not have authentication yet.")
    fi

    (( ${#VALIDATION_ERRORS[@]} == 0 ))
}

display_value() {
    local key=$1
    local value=${VALUES[$key]}
    case ${TYPES[$key]} in
        secret)
            if [[ -n "$value" ]]; then
                printf '%s' "set (hidden)"
            else
                printf '%s' "not set"
            fi
            ;;
        bool)
            if ! bool_is_valid "$value"; then
                printf '%s' "invalid: $value"
            elif bool_is_true "$value"; then
                printf '%s' "enabled"
            else
                printf '%s' "disabled"
            fi
            ;;
        *)
            if [[ -n "$value" ]]; then
                printf '%s' "$value"
            else
                printf '%s' "empty"
            fi
            ;;
    esac
}

format_list() {
    local result=""
    local item
    for item in "$@"; do
        result+="• $item"$'\n'
    done
    printf '%s' "$result"
}

print_validation() {
    if (( ${#VALIDATION_ERRORS[@]} > 0 )); then
        printf 'Errors:\n'
        printf '  - %s\n' "${VALIDATION_ERRORS[@]}"
    fi
    if (( ${#VALIDATION_WARNINGS[@]} > 0 )); then
        printf 'Warnings:\n'
        printf '  - %s\n' "${VALIDATION_WARNINGS[@]}"
    fi
}

save_env() {
    local source_file line key
    local wrote_header=0
    declare -A written=()

    if [[ -f "$ENV_FILE" ]]; then
        source_file=$ENV_FILE
    else
        source_file=$EXAMPLE_FILE
    fi

    if ! ACTIVE_TMP=$(mktemp "$PROJECT_DIR/.env.tmp.XXXXXX"); then
        return 1
    fi

    if [[ -f "$source_file" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            line=${line%$'\r'}
            if parse_assignment "$line" && [[ -n "${KNOWN[$PARSED_KEY]+x}" ]]; then
                key=$PARSED_KEY
                if [[ -z "${written[$key]+x}" ]]; then
                    printf '%s=%s\n' "$key" "${VALUES[$key]}" >> "$ACTIVE_TMP" || return 1
                    written["$key"]=1
                fi
            else
                printf '%s\n' "$line" >> "$ACTIVE_TMP" || return 1
            fi
        done < "$source_file"
    fi

    for key in "${SETTINGS[@]}"; do
        if [[ -z "${written[$key]+x}" ]]; then
            if (( wrote_header == 0 )); then
                printf '\n# Added by configure.sh\n' >> "$ACTIVE_TMP" || return 1
                wrote_header=1
            fi
            printf '%s=%s\n' "$key" "${VALUES[$key]}" >> "$ACTIVE_TMP" || return 1
        fi
    done

    chmod 600 "$ACTIVE_TMP" || return 1
    if [[ -f "$ENV_FILE" ]]; then
        cp -- "$ENV_FILE" "$ENV_FILE.bak" || return 1
        chmod 600 "$ENV_FILE.bak" || return 1
    fi
    mv -f -- "$ACTIVE_TMP" "$ENV_FILE" || return 1
    ACTIVE_TMP=""
    DIRTY=0
}

show_usage() {
    cat <<'EOF'
Usage:
  ./configure.sh             open the interactive TUI
  ./configure.sh --validate  validate the current .env without changing it
  ./configure.sh --help      show this help
EOF
}

case ${1-} in
    --validate)
        if validate_all; then
            echo ".env: no errors found."
            print_validation
            exit 0
        fi
        print_validation >&2
        exit 1
        ;;
    --help|-h)
        show_usage
        exit 0
        ;;
    "") ;;
    *)
        show_usage >&2
        exit 2
        ;;
esac

if [[ ! -t 0 || ! -t 1 ]]; then
    echo "The interactive menu must be run in a terminal: ./configure.sh" >&2
    echo "To validate without the TUI, use: ./configure.sh --validate" >&2
    exit 1
fi

if command -v whiptail >/dev/null 2>&1; then
    UI_BIN=$(command -v whiptail)
    UI_OK_OPTION="--ok-button"
    UI_CANCEL_OPTION="--cancel-button"
    UI_YES_OPTION="--yes-button"
    UI_NO_OPTION="--no-button"
elif command -v dialog >/dev/null 2>&1; then
    UI_BIN=$(command -v dialog)
    UI_OK_OPTION="--ok-label"
    UI_CANCEL_OPTION="--cancel-label"
    UI_YES_OPTION="--yes-label"
    UI_NO_OPTION="--no-label"
else
    echo "Neither whiptail nor dialog was found. Install one of them and try again." >&2
    exit 1
fi

TERM_LINES=$(tput lines 2>/dev/null || echo 24)
TERM_COLS=$(tput cols 2>/dev/null || echo 80)
[[ "$TERM_LINES" =~ ^[0-9]+$ ]] || TERM_LINES=24
[[ "$TERM_COLS" =~ ^[0-9]+$ ]] || TERM_COLS=80
if (( TERM_LINES < 20 || TERM_COLS < 70 )); then
    echo "The terminal is too small. Minimum: 70x20; current: ${TERM_COLS}x${TERM_LINES}." >&2
    exit 1
fi

DIALOG_HEIGHT=$(( TERM_LINES - 2 ))
(( DIALOG_HEIGHT > 28 )) && DIALOG_HEIGHT=28
DIALOG_WIDTH=$(( TERM_COLS - 4 ))
(( DIALOG_WIDTH > 110 )) && DIALOG_WIDTH=110
MENU_HEIGHT=$(( DIALOG_HEIGHT - 8 ))

ui_menu() {
    local title=$1
    local text=$2
    shift 2
    local result status

    set +e
    result=$("$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_OK_OPTION" "Select" \
        "$UI_CANCEL_OPTION" "Back" \
        --menu "$text" "$DIALOG_HEIGHT" "$DIALOG_WIDTH" "$MENU_HEIGHT" \
        "$@" 3>&1 1>&2 2>&3)
    status=$?
    set -e
    if (( status != 0 )); then
        return "$status"
    fi
    printf '%s' "$result"
}

ui_input() {
    local title=$1
    local text=$2
    local initial=$3
    local result status

    set +e
    result=$("$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_OK_OPTION" "Apply" \
        "$UI_CANCEL_OPTION" "Cancel" \
        --inputbox "$text" 16 "$DIALOG_WIDTH" "$initial" \
        3>&1 1>&2 2>&3)
    status=$?
    set -e
    if (( status != 0 )); then
        return "$status"
    fi
    printf '%s' "$result"
}

ui_password() {
    local title=$1
    local text=$2
    local result status

    set +e
    result=$("$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_OK_OPTION" "Apply" \
        "$UI_CANCEL_OPTION" "Cancel" \
        --passwordbox "$text" 15 "$DIALOG_WIDTH" \
        3>&1 1>&2 2>&3)
    status=$?
    set -e
    if (( status != 0 )); then
        return "$status"
    fi
    printf '%s' "$result"
}

ui_message() {
    local title=$1
    local text=$2
    "$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_OK_OPTION" "OK" \
        --msgbox "$text" "$DIALOG_HEIGHT" "$DIALOG_WIDTH" || true
}

ui_yes_no() {
    local title=$1
    local text=$2
    local yes_label=${3:-Yes}
    local no_label=${4:-No}
    local default=${5:-yes}
    local -a default_option=()
    local status
    [[ "$default" == "no" ]] && default_option=(--defaultno)

    set +e
    "$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_YES_OPTION" "$yes_label" \
        "$UI_NO_OPTION" "$no_label" \
        "${default_option[@]}" \
        --yesno "$text" 16 "$DIALOG_WIDTH"
    status=$?
    set -e
    return "$status"
}

ui_textbox() {
    local title=$1
    local file=$2
    "$UI_BIN" \
        --backtitle "$BACKTITLE" \
        --title "$title" \
        "$UI_OK_OPTION" "Back" \
        --textbox "$file" "$DIALOG_HEIGHT" "$DIALOG_WIDTH" || true
}

edit_bool() {
    local key=$1
    local current answer
    current=$(display_value "$key")
    if ui_yes_no "${LABELS[$key]}" \
        "${HINTS[$key]}\n\nVariable: $key\nCurrent value: $current\n\nEnable this setting?" \
        "Enable" "Disable" "$(bool_is_true "${VALUES[$key]}" && echo yes || echo no)"; then
        VALUES["$key"]="true"
        DIRTY=1
    else
        answer=$?
        if (( answer == 1 )); then
            VALUES["$key"]="false"
            DIRTY=1
        fi
    fi
}

edit_enum() {
    local key=$1
    local choice
    case "$key" in
        RUN_MODE)
            if choice=$(ui_menu "${LABELS[$key]}" \
                "${HINTS[$key]}\n\nCurrent value: $(display_value "$key")" \
                "standalone" "All-in-one local stream instance (single machine)" \
                "controller" "Controller node (web queue, player, worker API)" \
                "render-worker" "Render worker node (dedicated danser machine)" \
                "broadcaster" "Dedicated stream broadcaster node (RTMP to Twitch/YouTube via FFmpeg)"); then
                VALUES["$key"]=$choice
                DIRTY=1
            fi
            ;;
        RENDER_BACKEND)
            if choice=$(ui_menu "${LABELS[$key]}" \
                "${HINTS[$key]}\n\nCurrent value: $(display_value "$key")" \
                "ordr" "Hosted o!rdr service" \
                "danser" "Local danser renderer"); then
                VALUES["$key"]=$choice
                DIRTY=1
            fi
            ;;
        ORDR_RESOLUTION)
            if choice=$(ui_menu "${LABELS[$key]}" \
                "${HINTS[$key]}\n\nCurrent value: $(display_value "$key")" \
                "960x540" "540p — smallest option" \
                "1280x720" "720p — recommended for this project" \
                "1920x1080" "1080p" \
                "3840x2160" "2160p / 4K"); then
                VALUES["$key"]=$choice
                DIRTY=1
            fi
            ;;
    esac
}

edit_skin_directory() {
    local key=$1
    local choice detected manual_path local_path
    detected=$(resolve_skin_directory_value "auto")
    local_path=${VALUES[DANSER_DIR]}
    local_path="$local_path/skins"

    if ! choice=$(ui_menu "${LABELS[$key]}" \
        "${HINTS[$key]}\n\nCurrent value: ${VALUES[$key]}\nAuto-detected: $detected" \
        "auto" "Auto-detect an osu!stable/Wine skins directory" \
        "local" "Use the local danser skins directory: $local_path" \
        "manual" "Enter a directory path manually"); then
        return 0
    fi

    case "$choice" in
        auto)
            VALUES["$key"]="auto"
            DIRTY=1
            ;;
        local)
            VALUES["$key"]=$local_path
            DIRTY=1
            ;;
        manual)
            manual_path=${VALUES[$key]}
            [[ "${manual_path,,}" == "auto" ]] && manual_path=$detected
            while true; do
                if ! manual_path=$(ui_input "${LABELS[$key]}" \
                    "Enter the directory that contains osu! skin folders.\n\nExample: ~/.local/share/osu-wine/osu!/Skins" \
                    "$manual_path"); then
                    return 0
                fi
                manual_path=$(trim_whitespace "$manual_path")
                if validate_value "$key" "$manual_path"; then
                    VALUES["$key"]=$manual_path
                    DIRTY=1
                    return 0
                fi
                ui_message "Invalid directory" "$VALIDATION_ERROR"
            done
            ;;
    esac
}

edit_skin() {
    local key=$1
    local directory choice index skin manual_skin
    local -a skins=("default")
    local -a items=()
    declare -A seen=([default]=1)

    directory=$(resolve_skin_directory_value "${VALUES[DANSER_SKINS_DIR]}")
    while IFS= read -r -d '' skin_ini; do
        skin=$(basename "$(dirname "$skin_ini")")
        if [[ -z "${seen[$skin]+x}" ]]; then
            skins+=("$skin")
            seen["$skin"]=1
        fi
    done < <(find "$directory" -mindepth 2 -maxdepth 2 -type f -iname 'skin.ini' -print0 2>/dev/null | sort -z)

    for index in "${!skins[@]}"; do
        skin=${skins[$index]}
        if [[ "$skin" == "default" ]]; then
            items+=("$((index + 1))" "default — built-in danser skin")
        else
            items+=("$((index + 1))" "$skin")
        fi
    done
    items+=("M" "Enter a skin name manually")

    if ! choice=$(ui_menu "${LABELS[$key]}" \
        "Skins directory: $directory\nDetected custom skins: $((${#skins[@]} - 1))\nCurrent value: ${VALUES[$key]}" \
        "${items[@]}"); then
        return 0
    fi

    if [[ "$choice" == "M" ]]; then
        if manual_skin=$(ui_input "${LABELS[$key]}" "Enter the exact skin directory name." "${VALUES[$key]}"); then
            manual_skin=$(trim_whitespace "$manual_skin")
            if validate_value "$key" "$manual_skin"; then
                VALUES["$key"]=$manual_skin
                DIRTY=1
            else
                ui_message "Invalid skin" "$VALIDATION_ERROR"
            fi
        fi
    elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#skins[@]} )); then
        VALUES["$key"]=${skins[$((choice - 1))]}
        DIRTY=1
    fi
}

edit_secret() {
    local key=$1
    local choice secret
    if ! choice=$(ui_menu "${LABELS[$key]}" \
        "${HINTS[$key]}\n\nCurrent value: $(display_value "$key")" \
        "set" "Enter a new value (characters are hidden)" \
        "clear" "Clear the stored value"); then
        return 0
    fi

    case "$choice" in
        set)
            if secret=$(ui_password "${LABELS[$key]}" \
                "Enter a new value for $key.\n\nTyped characters are not displayed."); then
                secret=$(trim_whitespace "$secret")
                if [[ -z "$secret" ]]; then
                    ui_message "Empty value" "Nothing was changed. Select Clear to remove the secret."
                elif validate_value "$key" "$secret"; then
                    VALUES["$key"]=$secret
                    DIRTY=1
                fi
            fi
            ;;
        clear)
            if ui_yes_no "Clear secret?" \
                "Remove the $key value from the new .env file?" "Clear" "Keep" "no"; then
                VALUES["$key"]=""
                DIRTY=1
            fi
            ;;
    esac
}

edit_input() {
    local key=$1
    local value
    while true; do
        if ! value=$(ui_input "${LABELS[$key]}" \
            "${HINTS[$key]}\n\nVariable: $key\nDefault value: ${DEFAULTS[$key]}" \
            "${VALUES[$key]}"); then
            return 0
        fi
        value=$(trim_whitespace "$value")
        if validate_value "$key" "$value"; then
            if [[ ${TYPES[$key]} == "integer" ]]; then
                value=$(normalize_decimal_integer "$value")
            fi
            VALUES["$key"]=$value
            DIRTY=1
            return 0
        fi
        ui_message "Invalid value" "$key: $VALIDATION_ERROR"
    done
}

edit_setting() {
    local key=$1
    case ${TYPES[$key]} in
        bool) edit_bool "$key" ;;
        enum) edit_enum "$key" ;;
        skin_dir) edit_skin_directory "$key" ;;
        skin) edit_skin "$key" ;;
        secret) edit_secret "$key" ;;
        *) edit_input "$key" ;;
    esac
}

open_category() {
    local title=$1
    local description=$2
    shift 2
    local -a keys=("$@")
    local -a items=()
    local choice index key

    while true; do
        items=()
        for index in "${!keys[@]}"; do
            key=${keys[$index]}
            items+=("$((index + 1))" "$key — ${LABELS[$key]} [$(display_value "$key")]")
        done
        if ! choice=$(ui_menu "$title" "$description" "${items[@]}"); then
            return 0
        fi
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#keys[@]} )); then
            edit_setting "${keys[$((choice - 1))]}"
        fi
    done
}

show_summary() {
    local temp key
    temp=$(mktemp "${TMPDIR:-/tmp}/osu-stream-config.XXXXXX")
    ACTIVE_TMP=$temp
    {
        printf 'File: %s\n' "$ENV_FILE"
        printf 'Status: %s\n\n' "$([[ $DIRTY == 1 ]] && echo 'unsaved changes' || echo 'no pending changes')"
        for key in "${SETTINGS[@]}"; do
            printf '%-38s %s\n' "$key" "$(display_value "$key")"
        done
    } > "$temp"
    ui_textbox "Current settings" "$temp"
    rm -f -- "$temp"
    ACTIVE_TMP=""
}

confirm_valid_configuration() {
    local errors warnings
    if ! validate_all; then
        errors=$(format_list "${VALIDATION_ERRORS[@]}")
        ui_message "Fix the errors" "The file was not saved:\n\n$errors"
        return 1
    fi

    if (( ${#VALIDATION_WARNINGS[@]} > 0 )); then
        warnings=$(format_list "${VALIDATION_WARNINGS[@]}")
        if ! ui_yes_no "Warnings" \
            "$warnings\nContinue and save?" "Save" "Go back" "no"; then
            return 1
        fi
    fi
    return 0
}

save_from_tui() {
    if ! confirm_valid_configuration; then
        return 1
    fi
    if ! save_env; then
        ui_message "Save failed" "Could not write $ENV_FILE."
        return 1
    fi
    return 0
}

autodetect_danser_profile() {
    local temp output status
    temp=$(mktemp "${TMPDIR:-/tmp}/osu-danser-detect.XXXXXX")
    ACTIVE_TMP=$temp

    set +e
    ./venv/bin/python -c "
import json
from pathlib import Path
from danser_renderer import generate_optimal_danser_profile

target = Path('tools/danser/settings/stream.json')
result = generate_optimal_danser_profile(target)
print('--- Danser Profile Autodetect ---')
print(f'Target Profile:  {result[\"path\"]}')
print(f'Detected Encoder: {result[\"encoder\"]}')
print(f'Mode:             {result[\"mode\"]}')
print(f'Total CPU Cores:  {result[\"cpu_count\"]}')
print(f'Virtual Machine:  {result[\"is_vm\"]}')
print('\nProfile tools/danser/settings/stream.json has been generated successfully.')
" > "$temp" 2>&1
    status=$?
    set -e

    if (( status == 0 )); then
        VALUES["DANSER_SETTINGS"]="stream"
        DIRTY=1
        ui_textbox "Danser Profile Auto-Configuration" "$temp"
    else
        ui_textbox "Auto-Configuration Failed" "$temp"
    fi

    rm -f -- "$temp"
    ACTIVE_TMP=""
}

run_project_check() {
    local temp status
    temp=$(mktemp "${TMPDIR:-/tmp}/osu-stream-check.XXXXXX")
    ACTIVE_TMP=$temp
    set +e
    ./check.sh > "$temp" 2>&1
    status=$?
    set -e
    if (( status == 0 )); then
        printf '\nValidation completed successfully.\n' >> "$temp"
        ui_textbox "Validation successful" "$temp"
    else
        printf '\nValidation exited with status %d. Fix the problems shown above.\n' "$status" >> "$temp"
        ui_textbox "Validation found problems" "$temp"
    fi
    rm -f -- "$temp"
    ACTIVE_TMP=""
}

reset_to_defaults() {
    local key
    if ui_yes_no "Restore defaults?" \
        "All values in the menu will be replaced with values from .env.example.\n\nThe .env file will not change until you save." \
        "Restore" "Cancel" "no"; then
        for key in "${SETTINGS[@]}"; do
            VALUES["$key"]=${DEFAULTS[$key]}
        done
        DIRTY=1
    fi
}

confirm_exit_without_save() {
    if (( DIRTY == 0 )); then
        return 0
    fi
    ui_yes_no "Exit without saving?" \
        "There are unsaved changes. Close the menu and discard them?" \
        "Exit" "Stay" "no"
}

main_menu() {
    local choice state
    while true; do
        if (( DIRTY == 1 )); then
            state="There are unsaved changes"
        elif [[ -f "$ENV_FILE" ]]; then
            state="Existing .env loaded"
        else
            state="No .env exists yet; values from .env.example are loaded"
        fi

        if ! choice=$(ui_menu "Project configuration" \
            "$state\n\nSelect a section. Every setting includes a detailed hint." \
            "0" "Operation mode [$(display_value RUN_MODE)]" \
            "1" "Render backend [$(display_value RENDER_BACKEND)]" \
            "2" "Hosted renderer o!rdr" \
            "3" "Local danser renderer" \
            "4" "Random replay source" \
            "5" "Queue and playback" \
            "6" "Web player and uploads" \
            "7" "Discord bot [$(display_value DISCORD_ENABLED)]" \
            "B" "Stream Broadcaster (Twitch & YouTube RTMP)" \
            "C" "Controller & Worker Cluster" \
            "D" "Auto-detect system & configure danser profile" \
            "8" "Show all current values" \
            "9" "Restore values from .env.example" \
            "K" "Save and run ./check.sh" \
            "S" "Save .env and exit" \
            "Q" "Exit without saving"); then
            if confirm_exit_without_save; then
                return 0
            fi
            continue
        fi

        case "$choice" in
            0) edit_setting RUN_MODE ;;
            1) edit_setting RENDER_BACKEND ;;
            2) open_category "o!rdr" "Hosted o!rdr renderer settings." "${ORDR_KEYS[@]}" ;;
            3) open_category "danser" "Local danser renderer settings." "${DANSER_KEYS[@]}" ;;
            4) open_category "Random replays" "APIs and the score ID range used for random selection." "${SOURCE_KEYS[@]}" ;;
            5) open_category "Queue" "Buffer, producer, and rendered video storage settings." "${QUEUE_KEYS[@]}" ;;
            6) open_category "Web" "Server address, upload limits, and initial volume." "${WEB_KEYS[@]}" ;;
            7) open_category "Discord" "Accept .osr files through Discord slash commands." "${DISCORD_KEYS[@]}" ;;
            B|b) open_category "Broadcaster" "Headless 24/7 RTMP stream broadcaster settings (Twitch & YouTube)." "${BROADCASTER_KEYS[@]}" ;;
            C|c) open_category "Cluster" "Multi-node Controller & Render Worker cluster settings." "${CLUSTER_KEYS[@]}" ;;
            D|d) autodetect_danser_profile ;;
            8) show_summary ;;
            9) reset_to_defaults ;;
            K|k)
                if save_from_tui; then
                    run_project_check
                fi
                ;;
            S|s)
                if save_from_tui; then
                    ui_message "Settings saved" \
                        ".env was written with file mode 600.\n\nThe previous version, if any, was saved as .env.bak."
                    return 0
                fi
                ;;
            Q|q)
                if confirm_exit_without_save; then
                    return 0
                fi
                ;;
        esac
    done
}

main_menu
