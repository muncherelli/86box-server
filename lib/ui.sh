# shellcheck shell=bash
# Progress display for ./update.sh. Reads the events written by
# ansible/callback_plugins/progress.py and draws one line per phase.

PHASES=()
P_STATE=()   # pending | running | done | failed
P_START=()
P_TIME=()
CUR=-1
LAST_TASK=""
FAIL_TASK=""
FAIL_HOST=""
FAIL_MSG=""
LINES_READ=0
TICK=0
DRAWN=0
USE_TTY=0
PID=""
PROG=""

fmt_time() {
  local s=$1
  if [ "$s" -lt 60 ]; then
    echo "${s}s"
  else
    printf '%d:%02d' $((s / 60)) $((s % 60))
  fi
}

load_phases() {
  mapfile -t PHASES < <(grep -E '^- name:' "$REPO/ansible/site.yml" | sed 's/^- name: //')
  local i
  for i in "${!PHASES[@]}"; do
    P_STATE[i]=pending
    P_START[i]=0
    P_TIME[i]=""
  done
}

setup_glyphs() {
  if [ "$(locale charmap 2>/dev/null)" = UTF-8 ]; then
    G_OK="✓"; G_FAIL="✗"; G_PEND="·"
    SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  else
    G_OK="+"; G_FAIL="x"; G_PEND="-"
    SPIN=('|' '/' '-' "\\")
  fi
}

start_phase() {
  local i=$1
  P_STATE[i]=running
  P_START[i]=$SECONDS
  if [ "$USE_TTY" -eq 0 ]; then
    echo "[$(date +%T)] ${PHASES[i]} ..."
  fi
}

end_phase() {
  local i=$1
  P_STATE[i]=$2
  P_TIME[i]=$(fmt_time $((SECONDS - P_START[i])))
  if [ "$USE_TTY" -eq 0 ]; then
    if [ "$2" = "done" ]; then
      echo "[$(date +%T)] ${PHASES[i]} done (${P_TIME[i]})"
    else
      echo "[$(date +%T)] ${PHASES[i]} FAILED"
    fi
  fi
}

handle_event() {
  local line=$1 ev name i re
  re='"ev": "([a-z]+)"'
  [[ $line =~ $re ]] || return 0
  ev=${BASH_REMATCH[1]}
  case $ev in
    play)
      re='"name": "(.*)"\}$'
      [[ $line =~ $re ]] || return 0
      name=${BASH_REMATCH[1]}
      if [ "$CUR" -ge 0 ] && [ "${P_STATE[CUR]}" = running ]; then
        end_phase "$CUR" "done"
      fi
      for i in "${!PHASES[@]}"; do
        if [ "${PHASES[i]}" = "$name" ]; then
          CUR=$i
          start_phase "$i"
          break
        fi
      done
      ;;
    task)
      re='"name": "(.*)"\}$'
      if [[ $line =~ $re ]]; then LAST_TASK=${BASH_REMATCH[1]}; fi
      ;;
    failed)
      re='"task": "(.*)", "host": "(.*)", "msg": "(.*)"\}$'
      if [[ $line =~ $re ]]; then
        FAIL_TASK=${BASH_REMATCH[1]}
        FAIL_HOST=${BASH_REMATCH[2]}
        FAIL_MSG=${BASH_REMATCH[3]//\\n/$'\n'}
        FAIL_MSG=${FAIL_MSG//\\\"/\"}
      fi
      ;;
  esac
}

# Handle the event lines written since the last call.
read_events() {
  local total line
  total=$(wc -l <"$PROG")
  [ "$total" -gt "$LINES_READ" ] || return 0
  while IFS= read -r line; do
    handle_event "$line"
  done < <(sed -n "$((LINES_READ + 1)),${total}p" "$PROG")
  LINES_READ=$total
}

# Redraw all phase lines in place.
draw() {
  local cols line i spin
  cols=$(tput cols 2>/dev/null || echo 80)
  if [ "$DRAWN" -eq 1 ]; then
    printf '\033[%dA' "${#PHASES[@]}"
  fi
  DRAWN=1
  TICK=$((TICK + 1))
  for i in "${!PHASES[@]}"; do
    case ${P_STATE[i]} in
      done) line="  $G_OK ${PHASES[i]}  ${P_TIME[i]}" ;;
      failed) line="  $G_FAIL ${PHASES[i]}  FAILED" ;;
      running)
        spin=${SPIN[$((TICK % ${#SPIN[@]}))]}
        line="  $spin ${PHASES[i]}  $(fmt_time $((SECONDS - P_START[i])))  ${LAST_TASK:0:40}"
        ;;
      *) line="  $G_PEND ${PHASES[i]}" ;;
    esac
    # Cut to the terminal width so a long line never wraps and breaks the redraw.
    printf '%s\033[K\n' "${line:0:$((cols - 1))}"
  done
}

cleanup_ui() {
  [ -z "$PROG" ] || rm -f "$PROG"
  if [ "$USE_TTY" -eq 1 ]; then tput cnorm 2>/dev/null || true; fi
}

on_interrupt() {
  [ -z "$PID" ] || kill "$PID" 2>/dev/null || true
  wait 2>/dev/null || true
  echo
  echo "Interrupted. Full log: $LOG"
  exit 130
}

finish_ui() {
  local rc=$1
  if [ "$CUR" -ge 0 ] && [ "${P_STATE[CUR]}" = running ]; then
    if [ "$rc" -eq 0 ]; then end_phase "$CUR" "done"; else end_phase "$CUR" failed; fi
  fi
  if [ "$USE_TTY" -eq 1 ]; then draw; fi
  echo
  if [ "$rc" -eq 0 ]; then
    echo "log: $LOG"
    return 0
  fi
  if [ -n "$FAIL_TASK" ]; then
    echo "task:  $FAIL_TASK"
    echo "host:  $FAIL_HOST"
    echo "error: $FAIL_MSG"
  else
    echo "ansible-playbook exited with code $rc"
  fi
  echo "Full log: $LOG"
}

# True when Ansible would have to ask for a password.
needs_prompt() {
  local a
  for a in "${BECOME_ARGS[@]}" "${VAULT_ARGS[@]}"; do
    case $a in --ask-*) return 0 ;; esac
  done
  return 1
}

# Raw Ansible output on screen and in the log (--verbose, or when it must prompt).
run_verbose() {
  local rc=0
  set +e
  (cd "$REPO/ansible" && ansible-playbook site.yml "${BECOME_ARGS[@]}" "${VAULT_ARGS[@]}" "$@") 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  set -e
  echo
  echo "log: $LOG"
  exit "$rc"
}

run_playbook() {
  local rc=0
  if [ "$VERBOSE" -eq 1 ] || needs_prompt; then
    run_verbose "$@"
  fi
  load_phases
  setup_glyphs
  if [ -t 1 ]; then USE_TTY=1; fi
  PROG=$(mktemp "$LOG_DIR/.progress.XXXXXX")
  trap cleanup_ui EXIT
  trap on_interrupt INT
  export B86_PROGRESS_FILE="$PROG"
  (cd "$REPO/ansible" && exec ansible-playbook site.yml "${BECOME_ARGS[@]}" "${VAULT_ARGS[@]}" "$@" >>"$LOG" 2>&1) &
  PID=$!
  echo "86server $G_PEND host $(uname -n)"
  echo
  if [ "$USE_TTY" -eq 1 ]; then
    tput civis 2>/dev/null || true
    draw
  fi
  while kill -0 "$PID" 2>/dev/null; do
    read_events
    if [ "$USE_TTY" -eq 1 ]; then draw; fi
    sleep 0.2
  done
  wait "$PID" || rc=$?
  read_events
  finish_ui "$rc"
  exit "$rc"
}
