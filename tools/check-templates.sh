#!/usr/bin/env bash
# Start each template once in 86Box and check the hardware survives.
# Usage: tools/check-templates.sh <templates dir> <86Box tag>   (runs itself through sudo)
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 <templates dir> <86Box tag>" >&2; exit 2; }
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi

dir=$(cd "$1" && pwd)
tag=$2
W=/var/cache/86box-server/tpl-check
value() { sed -n "s/^$2 = //p" "$1" | head -n 1; }

rm -rf "$W"
install -d -o 86boxvm -g 86boxvm -m 0755 "$W"
rc=0
shopt -s nullglob
for cfg in "$dir"/*.cfg; do
    id=$(basename "$cfg" .cfg)
    install -d -o 86boxvm -g 86boxvm -m 0755 "$W/$id"
    install -o 86boxvm -g 86boxvm -m 0644 "$cfg" "$W/$id/86box.cfg"
    # 86Box never exits on its own here; the kill is expected.
    runuser -u 86boxvm -- env QT_QPA_PLATFORM=offscreen HOME="$W/$id" \
        timeout -s KILL 15 "/opt/86box-server/86box/$tag/AppRun" -N -P "$W/$id/" \
        -R "/opt/86box-server/roms/$tag" >/dev/null 2>&1 || true
    saved="$W/$id/86box.cfg"
    reason=""
    grep -q '^\[General\]' "$saved" || reason="86Box did not save the config"
    for key in machine cpu_family cpu_speed gfxcard; do
        [ -z "$reason" ] || break
        want=$(value "$cfg" "$key")
        got=$(value "$saved" "$key")
        [ "$want" = "$got" ] || reason="$key is '$got', expected '$want'"
    done
    if [ -z "$reason" ]; then echo "PASS $id"; else echo "FAIL $id: $reason"; rc=1; fi
done
exit "$rc"
