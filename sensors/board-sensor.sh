#!/usr/bin/env bash
# board-sensor.sh — enveloppe de la statusline Claude Code.
#
#   usage :  board-sensor.sh <chemin-statusline-origine> [args...]
#
# Le stdin de la commande de statusline est le SEUL endroit ou Claude Code
# expose context_window.used_percentage et rate_limits. Plutot que de modifier
# la statusline de l'utilisateur, on l'enveloppe :
#
#   statusLine.command -> board-sensor.sh -+-> ecrit state/<sid>.meas.json
#                                          |    et account.json  (arriere-plan)
#                                          `-> exec la statusline d'origine
#
# Regle numero un : NE JAMAIS DEGRADER LA STATUSLINE. L'extraction tourne dans
# un sous-shell detache dont stdout et stderr sont fermes sur /dev/null ; elle
# n'est donc ni dans le chemin d'affichage, ni capable de polluer la sortie.
# Le surcout mesurable sur le chemin critique = demarrage de bash + `cat` +
# un fork (~4 ms), sans aucun demarrage de Python synchrone.
#
# Racine d'ecriture : $BOARD_HOME, sinon ~/.claude/board
# (BOARD_HOME sert aux tests ; en production la variable n'est pas definie)

orig="$1"
shift 2>/dev/null

# 1. Lire la TOTALITE du stdin, une seule fois (il n'est lisible qu'une fois).
payload="$(cat 2>/dev/null)"

# ---------------------------------------------------------------------------
# Le corps de l'extraction. Sort de la definition de fonction pour rester
# lisible ; `cat` d'un heredoc quote : aucune interpolation shell.
# ---------------------------------------------------------------------------
board_extract_py() {
cat <<'BOARD_PY_EOF'
import sys, os, json, time

LOG_MAX = 200 * 1024


def board_home():
    h = os.environ.get("BOARD_HOME")
    if h:
        return os.path.expanduser(h)
    return os.path.join(os.path.expanduser("~"), ".claude", "board")


def log(msg):
    """Journal de derniere chance. Jamais sur stderr : l'utilisateur ne doit
    rien voir. Un fichier, plafonne a 200 Ko, une generation conservee."""
    try:
        home = board_home()
        os.makedirs(home, exist_ok=True)
        p = os.path.join(home, "sensor.log")
        try:
            if os.path.getsize(p) >= LOG_MAX:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s board-sensor %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def atomic_write(path, obj):
    """`.tmp` + os.replace. Le .tmp porte le pid : deux sessions qui ecrivent
    account.json en concurrence ne se marchent pas dessus, et le fichier final
    n'est jamais visible a moitie ecrit."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lire_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def plus_recent(ancien, neuf):
    """Arbitre deux releves de quota et rend celui qui est le plus recent.

    LE PROBLEME. `rate_limits` vient de la derniere reponse API recue PAR CETTE
    SESSION. Une session inactive depuis deux heures porte donc un chiffre
    vieux de deux heures — et sa statusline, elle, continue de se rendre a
    chaque frappe. Sans arbitrage, chaque session reecrit account.json avec SA
    valeur : mesure du 28/08 sur ce poste, 5 valeurs distinctes (37, 78, 82, 83,
    84 %) en 120 secondes et 26 descentes. La jauge tombait et remontait sans
    qu'aucune consommation reelle ne bouge.

    L'ARBITRE. On ne dispose d'aucun horodatage de MESURE dans le payload : ni
    l'heure de la reponse API, ni un numero de sequence. Il faut donc un
    invariant. C'est la fenetre elle-meme qui le fournit : `resets_at` tombe sur
    une heure ronde (16:00:00 ici, soit 11:00 + 5 h), donc la fenetre est FIXE
    et non glissante — la consommation ne peut qu'y croitre jusqu'a la remise a
    zero. A fenetre egale, le plus grand pourcentage est donc le plus recent.

        resets_at neuf  >  ancien  ->  fenetre suivante, on prend (la chute est
                                       vraie : c'est la remise a zero)
        resets_at neuf  <  ancien  ->  releve perime, on refuse
        resets_at egaux            ->  on garde le plus grand pourcentage

    SI L'INVARIANT TOMBE. Si Anthropic passait a une fenetre glissante, ce code
    figerait la jauge sur le maximum jusqu'a la remise a zero : il surestimerait
    la consommation, jamais l'inverse. C'est le seul sens tolerable — une jauge
    trop haute fait lever le pied, une jauge trop basse fait taper le plafond.
    """
    if not isinstance(neuf, dict):
        return ancien
    if not isinstance(ancien, dict):
        return neuf
    ra, rn = ancien.get("resets_at"), neuf.get("resets_at")
    if isinstance(ra, (int, float)) and isinstance(rn, (int, float)):
        if rn > ra:
            return neuf
        if rn < ra:
            return ancien
    pa, pn = ancien.get("used_pct"), neuf.get("used_pct")
    if not isinstance(pa, (int, float)):
        return neuf
    if not isinstance(pn, (int, float)):
        return ancien
    return neuf if pn >= pa else ancien


def obj(d, key):
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def txt(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def window(rl, key):
    w = rl.get(key)
    if not isinstance(w, dict):
        return None
    return {"used_pct": num(w.get("used_percentage")),
            "resets_at": w.get("resets_at")}


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return
    data = json.loads(raw)
    if not isinstance(data, dict):
        return

    home = board_home()
    now = int(time.time())

    ws = obj(data, "workspace")
    wt = obj(data, "worktree")
    sid = txt(data.get("session_id"))

    # ---- state/<sid>.meas.json --------------------------------------------
    # Pas de session_id : rien a nommer, on ne devine pas de chemin.
    if sid and "/" not in sid and ".." not in sid:
        meas = {
            "session_id": sid,
            "title": txt(data.get("session_name")),
            "cwd": txt(data.get("cwd")) or txt(ws.get("current_dir")),
            "project_dir": txt(ws.get("project_dir")),
            # worktree.branch UNIQUEMENT. On n'utilise PLUS
            # workspace.git_worktree en repli : ce champ porte un NOM DE DOSSIER,
            # jamais une branche, et il produisait des identifiants de 44
            # caracteres du genre « ProjetB.Automatisation-fix-failed-to-passed ».
            # Le serveur resout la vraie branche avec git ; mieux vaut null ici
            # qu'une valeur d'un autre type.
            "branch": txt(wt.get("branch")),
            "ctx_pct": num(obj(data, "context_window").get("used_percentage")),
            "model": txt(obj(data, "model").get("display_name")),
            "effort": txt(obj(data, "effort").get("level")),
            # absent du payload de statusline (verifie sur 2.1.245) : null
            "permission_mode": None,
            "cost_usd": num(obj(data, "cost").get("total_cost_usd")),
            "updated_at": now,
        }
        atomic_write(os.path.join(home, "state", sid + ".meas.json"), meas)

    # ---- account.json ----------------------------------------------------
    # rate_limits n'apparait qu'apres la premiere reponse API et seulement pour
    # les abonnements. Absent => on n'ecrit rien, plutot que d'ecraser des
    # valeurs valables ecrites par une autre session.
    rl = obj(data, "rate_limits")
    fh, sd = window(rl, "five_hour"), window(rl, "seven_day")
    if fh or sd:
        chemin = os.path.join(home, "account.json")
        ancien = lire_json(chemin)
        fusion = {"five_hour": plus_recent(ancien.get("five_hour"), fh),
                  "seven_day": plus_recent(ancien.get("seven_day"), sd)}
        # On n'ecrit QUE si quelque chose a bouge. Quatre sessions actives
        # rendaient leur statusline en continu : le fichier etait reecrit
        # plusieurs fois par seconde pour y remettre la meme valeur. `updated_at`
        # prend donc son vrai sens — l'instant du dernier CHANGEMENT, pas celui
        # de la derniere ecriture.
        #
        # La lecture-modification-ecriture n'est pas verrouillee, et c'est sans
        # consequence ici : deux sessions simultanees peuvent faire perdre une
        # mise a jour, mais la suivante la reapplique — l'arbitre est monotone,
        # donc le maximum finit toujours par gagner.
        if (fusion["five_hour"] != ancien.get("five_hour")
                or fusion["seven_day"] != ancien.get("seven_day")):
            fusion["updated_at"] = now
            atomic_write(chemin, fusion)


try:
    main()
except Exception as e:
    log("extraction: %r" % (e,))
BOARD_PY_EOF
}

# 2. Extraction en arriere-plan. stdout et stderr sur /dev/null : indispensable,
#    sinon le fd de sortie herite reste ouvert et Claude Code attend l'EOF du
#    fils avant d'afficher la ligne.
if [ -n "$payload" ]; then
  (
    printf '%s' "$payload" | python3 -c "$(board_extract_py)"
  ) >/dev/null 2>&1 </dev/null &
fi

# 3. La statusline d'origine reprend la main, sortie inchangee.
#    Rien n'a ete ecrit sur stdout avant ce point.
[ -n "$orig" ] || exit 0
[ -r "$orig" ] || exit 0
if [ -x "$orig" ]; then
  exec "$orig" "$@" <<<"$payload"
fi
exec python3 "$orig" "$@" <<<"$payload"
