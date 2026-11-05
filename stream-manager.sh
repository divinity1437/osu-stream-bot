#!/usr/bin/env bash
# Stream Manager TUI for osu! replay stream broadcaster

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export NCURSES_NO_UTF8_ACS=1

SERVICE="stream-broadcaster.service"

get_status_info() {
    local active_state sub_state uptime ffmpeg_count
    active_state=$(systemctl is-active "$SERVICE" 2>/dev/null || echo "unknown")
    sub_state=$(systemctl show -p SubState --value "$SERVICE" 2>/dev/null || echo "unknown")

    if [ "$active_state" = "active" ]; then
        uptime=$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" 2>/dev/null)
        ffmpeg_count=$(pgrep -fc ffmpeg 2>/dev/null || echo "0")
        STATUS_LABEL="[ РАБОТАЕТ / ONLINE ]"
        STATUS_DESC="Служба активна: $sub_state\nЗапущена: $uptime\nПроцессов FFmpeg: $ffmpeg_count"
    else
        STATUS_LABEL="[ ВЫКЛЮЧЕН / OFFLINE ]"
        STATUS_DESC="Служба остановлена: $sub_state\nТрансляция не идет."
    fi
}

view_logs() {
    local tmp_log
    tmp_log=$(mktemp)
    journalctl -u "$SERVICE" -n 50 --no-pager > "$tmp_log" 2>&1
    whiptail --title "Последние логи стрима (50 строк)" --textbox "$tmp_log" 20 80 --scrolltext
    rm -f "$tmp_log"
}

manage_renders() {
    local r_stat
    if tmux has-session -t stream 2>/dev/null; then
        r_stat="Рендерер активен в tmux-сессии 'stream'."
    else
        r_stat="tmux-сессия 'stream' не найдена."
    fi
    whiptail --title "Статус рендера реплеев" --msgbox "$r_stat\n\nДля подключения к консоли рендера:\n  tmux attach -t stream" 12 70
}

while true; do
    get_status_info

    CHOICE=$(whiptail --title "Управление стримом (osu! stream)" \
        --menu "Текущий статус стрима:\n$STATUS_LABEL\n\n$STATUS_DESC\n\nВыберите действие:" \
        20 72 6 \
        "1" "Запустить стрим (START)" \
        "2" "Остановить стрим (STOP)" \
        "3" "Перезапустить стрим (RESTART)" \
        "4" "Посмотреть логи трансляции (LOGS)" \
        "5" "Статус фонового рендера (RENDER)" \
        "6" "Выход" \
        3>&1 1>&2 2>&3)

    EXIT_STATUS=$?
    if [ $EXIT_STATUS -ne 0 ] || [ "$CHOICE" = "6" ]; then
        clear
        echo "Выход из Stream Manager."
        exit 0
    fi

    case "$CHOICE" in
        1)
            sudo systemctl start "$SERVICE"
            whiptail --title "Запуск" --msgbox "Команда запуска отправлена в systemd." 8 50
            ;;
        2)
            sudo systemctl stop "$SERVICE"
            whiptail --title "Остановка" --msgbox "Стрим остановлен." 8 50
            ;;
        3)
            sudo systemctl restart "$SERVICE"
            whiptail --title "Перезапуск" --msgbox "Служба стрима перезапущена." 8 50
            ;;
        4)
            view_logs
            ;;
        5)
            manage_renders
            ;;
    esac
done
