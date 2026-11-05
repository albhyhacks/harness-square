#!/usr/bin/env bash
# Hold off idle/sleep and screen lock until this script is stopped (kill its PID or run: keepawake.sh stop).
# Original GNOME values are restored on exit.
PIDF=/home/albhy/harness/.keepawake.pid
if [ "$1" = stop ]; then [ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null; exit 0; fi
S=org.gnome.desktop.screensaver
orig_lock=$(gsettings get $S lock-enabled)
restore() { gsettings set $S lock-enabled "$orig_lock"; rm -f $PIDF; exit 0; }
trap restore TERM INT EXIT
echo $$ > $PIDF
gsettings set $S lock-enabled false
systemd-inhibit --what=idle:sleep:handle-lid-switch --who=harness-run --why="long experiment running" sleep infinity &
wait
