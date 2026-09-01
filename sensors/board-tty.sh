#!/usr/bin/env bash
# Enregistre le tty du pane de cette session, pour que le board puisse y ecrire
# ses sequences d'echappement (fond, titre).
#
#   usage : board-tty.sh          (hook SessionStart, JSON sur stdin)
#
# Pourquoi ce n'est pas trivial : un hook recoit son JSON sur stdin, donc `tty`
# repond « not a tty » et /dev/tty n'est pas ouvrable. En revanche le processus
# PARENT est le processus claude, qui a bien un terminal de controle. On remonte
# donc la chaine des parents jusqu'a trouver un tty.
#
# On note aussi si Claude Code a la main sur le titre du terminal : quand
# CLAUDE_CODE_DISABLE_TERMINAL_TITLE est vide, il le reecrit en continu et toute
# tentative du board serait ecrasee. Le board doit le savoir plutot que de se
# battre en silence.
RACINE="${BOARD_HOME:-$HOME/.claude/board}"

charge=$(timeout 2 cat 2>/dev/null)
sid=$(printf '%s' "$charge" | timeout 2 python3 -c 'import sys,json; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)
# Le cwd est indispensable : c'est lui qui rattache la conversation a un projet.
# Sans lui, une session neuve apparaitrait dans le groupe de repli au lieu de sa
# colonne, le temps que la statusline prenne le relais.
cwd=$(printf '%s' "$charge" | timeout 2 python3 -c 'import sys,json; print(json.load(sys.stdin).get("cwd","") or "")' 2>/dev/null)
[ -n "$cwd" ] || cwd="$PWD"
[ -n "$sid" ] || exit 0
case "$sid" in */*|*..*) exit 0 ;; esac      # jamais de chemin dans un nom de fichier

# Remonte les parents jusqu'a un tty (au plus 6 niveaux : hook -> bash -> claude)
pid=$PPID; dev=""
for _ in 1 2 3 4 5 6; do
  [ -n "$pid" ] && [ "$pid" != "1" ] || break
  t=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$t" ] && [ "$t" != "?" ]; then dev="/dev/$t"; break; fi
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
done
[ -n "$dev" ] && [ -w "$dev" ] || exit 0

libre=false
[ -n "$CLAUDE_CODE_DISABLE_TERMINAL_TITLE" ] && libre=true

mkdir -p "$RACINE/state" 2>/dev/null
tmp="$RACINE/state/$sid.tty.$$.tmp"
printf '{"tty":"%s","titre_libre":%s,"pid":%s,"cwd":"%s","at":%s}\n' \
  "$dev" "$libre" "${pid:-0}" "$cwd" "$(date +%s)" > "$tmp" 2>/dev/null \
  && mv -f "$tmp" "$RACINE/state/$sid.tty" 2>/dev/null
rm -f "$tmp" 2>/dev/null
exit 0
