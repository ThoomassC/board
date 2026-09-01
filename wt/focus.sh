#!/usr/bin/env bash
# focus.sh — ramène la fenêtre Windows Terminal au premier plan et focalise le
# pane d'indice donné. Appelé sur un clic dans le board : il doit être rapide,
# idempotent, et ne jamais planter.
#
# Pourquoi cette mécanique
# ------------------------
# Windows Terminal 1.24 n'expose PAS de « focus-pane --target <n> » en ligne de
# commande. Les seules sous-commandes disponibles sont focus-tab, move-focus,
# move-pane et swap-pane. Mais :
#   * « -w <id> » envoie la commande à une fenêtre DÉJÀ OUVERTE ;
#   * « move-focus » accepte first / nextInOrder / previousInOrder, qui suivent
#     l'ordre de CRÉATION des panes, pas leur position à l'écran.
# Le pane d'indice k (0 = premier créé) est donc atteignable de façon
# déterministe par : move-focus first, puis k fois move-focus nextInOrder.
#
# Depuis WSL, l'alias d'exécution wt.exe de WindowsApps n'est pas lançable
# directement : il faut passer par cmd.exe /c. Les « ; » séparateurs de wt
# doivent être échappés en « \; » pour survivre au shell.
#
# La fenêtre est ramenée devant par WScript.Shell.AppActivate (même approche que
# ~/.claude/lanceur-panes.vbs), via powershell.exe.
#
# Réglages (environnement, tous optionnels — ce dépôt ne connaît aucun nom de
# projet, cf. README) :
#   BOARD_WT_WINDOW      id de fenêtre par défaut          (défaut : 0)
#   BOARD_WT_TITLE       préfixe de titre pour AppActivate (défaut : déduit)
#   BOARD_WT_PANE_COUNT  nombre de panes de la fenêtre     (défaut : 4)
#   BOARD_PANE_STATE     dossier des <PROJ>.tty          (défaut :
#                        ~/.claude/pane-state, écrit par claude-pane.sh)

set -uo pipefail
shopt -s nullglob

PROG=${0##*/}

readonly E_USAGE=2      # arguments invalides, indice hors bornes
readonly E_NOWINDOW=3   # aucune fenêtre Windows Terminal vivante
readonly E_NOCMD=4      # cmd.exe introuvable (pas d'interop WSL)
readonly E_WTFAIL=5     # l'appel wt.exe a échoué

STATE_DIR=${BOARD_PANE_STATE:-$HOME/.claude/pane-state}
PANE_COUNT=${BOARD_WT_PANE_COUNT:-4}

die() { printf '%s: %s\n' "$PROG" "$1" >&2; exit "${2:-$E_USAGE}"; }
warn() { printf '%s: %s\n' "$PROG" "$1" >&2; }

usage() {
  cat >&2 <<EOF
usage: $PROG <index_pane> [--window <id>] [--dry-run]

  <index_pane>   indice du pane dans l'ORDRE DE CRÉATION, 0 = premier créé.
                 Bornes : 0..$((PANE_COUNT - 1)) (BOARD_WT_PANE_COUNT).
  --window <id>  fenêtre cible : « 0 » = la plus récemment utilisée, ou le nom
                 donné à « wt -w <nom> » au lancement. Un nom est plus sûr
                 qu'un « 0 » qui peut désigner une autre fenêtre du terminal.
  --dry-run      imprime les commandes exactes sur stdout, n'exécute rien.

Codes de retour : 0 ok, $E_USAGE usage/bornes, $E_NOWINDOW pas de fenêtre,
$E_NOCMD cmd.exe absent, $E_WTFAIL échec de wt.exe.
EOF
}

# --- arguments ---------------------------------------------------------------

index=''
window=${BOARD_WT_WINDOW:-0}
dry=0

while (($#)); do
  case $1 in
    --dry-run) dry=1 ;;
    --window)
      shift
      (($#)) || die "--window attend une valeur"
      window=$1
      ;;
    --window=*) window=${1#--window=} ;;
    -h | --help)
      usage
      exit 0
      ;;
    --) ;;
    -[0-9]*) die "indice de pane invalide : « $1 » (entier >= 0 attendu)" ;;
    -*) die "option inconnue : $1" ;;
    *)
      [[ -z $index ]] || die "un seul indice de pane attendu (déjà : $index)"
      index=$1
      ;;
  esac
  shift
done

[[ -n $index ]] || {
  usage
  exit $E_USAGE
}

[[ $PANE_COUNT =~ ^[1-9][0-9]*$ ]] || {
  warn "BOARD_WT_PANE_COUNT invalide ($PANE_COUNT), 4 par défaut"
  PANE_COUNT=4
}

[[ $index =~ ^(0|[1-9][0-9]*)$ ]] ||
  die "indice de pane invalide : « $index » (entier >= 0 attendu)"
((index < PANE_COUNT)) ||
  die "indice de pane hors bornes : $index (attendu 0..$((PANE_COUNT - 1)))"

[[ -n $window ]] || die "--window ne peut pas être vide"

# --- construction des commandes ---------------------------------------------

# Titre pour AppActivate : par défaut le nom de fenêtre, quand c'en est un.
# « 0 », « -1 », « new » et « last » sont des mots-clés wt, pas des titres :
# dans ce cas on retombe sur le process WindowsTerminal.
title=${BOARD_WT_TITLE-__unset__}
if [[ $title == __unset__ ]]; then
  case $window in
    0 | -1 | new | last) title='' ;;
    *[!0-9]*) title=$window ;;
    *) title='' ;;
  esac
fi

# rendu lisible d'un argument pour --dry-run (identique à ce qui est exécuté)
q() {
  if [[ $1 =~ ^[A-Za-z0-9._@%+=:,/-]+$ ]]; then
    printf '%s' "$1"
  else
    printf "'%s'" "${1//\'/\'\\\'\'}"
  fi
}

wt_args=(-w "$window" move-focus first)
wt_show="cmd.exe /c wt.exe -w $(q "$window") move-focus first"
for ((i = 0; i < index; i++)); do
  wt_args+=(';' move-focus nextInOrder)
  wt_show+=' \; move-focus nextInOrder'
done

ps='$s=New-Object -ComObject WScript.Shell;$ok=$false;'
if [[ -n $title ]]; then
  # ' doublée : échappement des littéraux PowerShell
  ps+="\$ok=\$s.AppActivate('${title//\'/\'\'}');"
fi
ps+='if(-not $ok){$p=Get-Process WindowsTerminal -ErrorAction SilentlyContinue|'
ps+='Select-Object -First 1;if($p){$s.AppActivate($p.Id)|Out-Null}}'
ps_show="powershell.exe -NoProfile -NonInteractive -Command $(q "$ps")"

# --- la fenêtre est-elle vivante ? ------------------------------------------

# On ne « sonde » pas Windows Terminal : « wt -w 0 ... » sur un terminal fermé
# OUVRIRAIT une fenêtre, ce qui est exactement ce qu'il ne faut pas faire sur un
# clic. On se fie aux <PROJ>.tty que claude-pane.sh écrit dans chaque pane : un
# tty encore présent dans /dev prouve qu'un pane est vivant. Test local, ~0 ms.
window_alive() {
  local f dev
  [[ -d $STATE_DIR ]] || return 1
  for f in "$STATE_DIR"/*.tty; do
    [[ -s $f ]] || continue
    dev=$(cat -- "$f" 2>/dev/null) || continue
    [[ -n $dev && -e $dev ]] && return 0
  done
  return 1
}

# --- dry-run ----------------------------------------------------------------

if ((dry)); then
  # stdout = les commandes exactes, rien d'autre ; les réserves vont sur stderr.
  printf '%s\n%s\n' "$ps_show" "$wt_show"
  command -v cmd.exe >/dev/null 2>&1 ||
    warn "cmd.exe introuvable : l'exécution réelle échouerait ($E_NOCMD)"
  window_alive ||
    warn "aucun pane vivant sous $STATE_DIR : l'exécution réelle échouerait ($E_NOWINDOW)"
  exit 0
fi

# --- exécution --------------------------------------------------------------

command -v cmd.exe >/dev/null 2>&1 ||
  die "cmd.exe introuvable — interop WSL indisponible" $E_NOCMD

window_alive ||
  die "aucune fenêtre Windows Terminal suivie (aucun tty vivant dans $STATE_DIR)" $E_NOWINDOW

# Fenêtre au premier plan. powershell.exe coûte ~0,6 s à démarrer : on le
# détache pour que le clic rende la main tout de suite. L'ordre entre les deux
# actions est indifférent, l'état final est le même.
if command -v powershell.exe >/dev/null 2>&1; then
  setsid powershell.exe -NoProfile -NonInteractive -Command "$ps" \
    >/dev/null 2>&1 </dev/null &
  disown 2>/dev/null || true
else
  warn "powershell.exe introuvable : la fenêtre ne sera pas ramenée devant"
fi

# Focus du pane. On se place sur un chemin Windows : lancer cmd.exe depuis un
# cwd \\wsl.localhost\... déclenche un avertissement UNC bruyant.
out=$(cd /mnt/c 2>/dev/null || cd /; cmd.exe /c wt.exe "${wt_args[@]}" 2>&1)
rc=$?
if ((rc != 0)); then
  [[ -n $out ]] && warn "wt.exe: $out"
  die "échec du focus du pane $index (wt.exe a rendu $rc)" $E_WTFAIL
fi

exit 0
