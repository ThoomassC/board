#!/usr/bin/env python3
"""board-event.py — ecrivain d'evenements de « le board ».

Appele par les hooks Claude Code, qui passent leur JSON sur stdin.

    usage : board-event.py <state>              state ∈ working|blocked|review|error|dead
            board-event.py --from-notification  etat deduit du type de notification

Ecrit state/<sid>.event.json selon docs/SCHEMA.md. Deux invariants portent tous
les chronometres du board :

  * state_since ne bouge QUE si state change reellement (l'ancien fichier est
    relu pour le savoir) ;
  * updated_at bouge a chaque ecriture (c'est l'indicateur de fraicheur).

Contrat de comportement : sortie muette, code retour TOUJOURS 0. Un hook qui
echoue bruyamment pollue la session de l'utilisateur. Les erreurs partent dans
$BOARD_HOME/sensor.log (par defaut ~/.claude/board/sensor.log).

Racine d'ecriture : $BOARD_HOME, sinon ~/.claude/board
(BOARD_HOME sert aux tests ; en production la variable n'est pas definie.)

Champs de payload consommes, verifies sur le bundle 2.1.245 :
  tous les hooks   session_id, transcript_path, cwd, hook_event_name
  PreToolUse       tool_name, tool_input, tool_use_id, permission_mode
  Stop             last_assistant_message, stop_hook_active
  Notification     notification_type, message, title
  SessionEnd       reason
"""

import sys
import os
import json
import re
import time

VALID_STATES = ("working", "blocked", "review", "error", "dead")
LAST_SAY_MAX = 200
LOG_MAX = 200 * 1024

# --- types de Notification -------------------------------------------------
# L'enumeration complete est en dur dans le bundle Claude Code 2.1.245 :
#   permission_prompt, idle_prompt, auth_success, elicitation_dialog,
#   agent_needs_input, agent_completed, elicitation_url_dialog,
#   worker_permission_prompt, push_notification, computer_use_enter,
#   computer_use_exit, quota_auto_resume_{fired,stale,disabled}
# Le champ qui le porte dans le payload de hook est `notification_type`
# (c'est aussi ce champ que Claude Code compare aux `matcher` de settings.json).
STATE_BY_NOTIF = {
    "permission_prompt": "blocked",
    "agent_needs_input": "blocked",
    "elicitation_dialog": "blocked",
    "idle_prompt": "review",
    "agent_completed": "review",
    # --- extension observee sur 2.1.245, absente de la spec initiale ---
    # Ces deux types sont des variantes exactes des precedents ; les ignorer
    # ferait disparaitre du board une session reellement bloquee.
    # Retirable en supprimant ces deux lignes.
    "worker_permission_prompt": "blocked",
    "elicitation_url_dialog": "blocked",
}

IGNORED_NOTIF = frozenset((
    "auth_success",
    "push_notification",
    "computer_use_enter",
    "computer_use_exit",
))

# Le type n'est pas documente formellement : repli sur plusieurs cles plausibles.
NOTIF_KEYS = ("notification_type", "type", "subtype", "hook_event_name")

KNOWN_NOTIF = frozenset(STATE_BY_NOTIF) | IGNORED_NOTIF


def board_home():
    h = os.environ.get("BOARD_HOME")
    if h:
        return os.path.expanduser(h)
    return os.path.join(os.path.expanduser("~"), ".claude", "board")


def log(msg):
    """Journal de derniere chance. Jamais stdout ni stderr. Plafonne a 200 Ko,
    une generation conservee (sensor.log.1)."""
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
            f.write("%s board-event %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def atomic_write(path, obj):
    """`.tmp` + os.replace, arborescence creee au besoin. Le .tmp porte le pid
    pour que deux hooks concurrents ne se marchent pas dessus."""
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


def read_stdin_json():
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        log("stdin illisible en JSON (%d octets)" % len(raw))
        return {}
    return d if isinstance(d, dict) else {}


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


# --- dé-Markdown de la phrase ----------------------------------------------
# Claude écrit du Markdown, la carte affiche du texte nu. Tant que `last_say`
# était vide (le bug de l'appariement Stop/Notification, corrige plus haut), le
# probleme ne se voyait pas ; des que le champ s'est rempli, la seule ligne de
# prose de la carte a affiche sa syntaxe. Mesure le 02/09 sur deux
# conversations vivantes :
#     « ... mon commit `1a2b3c4` **s'applique sans conflit** sur ... »
#     « Reponse a `projeta-71` ... ## Ce que j'ai decide Je n'ai **pas** ... »
# Le titre de section est le pire des trois : il perd son retour a la ligne en
# chemin, donc il ne titre plus rien et colle deux phrases.
#
# LE NETTOYAGE PRECEDE LA TRONCATURE, et l'ordre n'est pas indifferent :
# couper a 200 caracteres d'abord laisserait une paire d'etoiles ouverte, donc
# une etoile orpheline a l'ecran — le defaut qu'on essaie de retirer.
#
# Ce n'est pas un rendu Markdown : c'est un DEBALISAGE. On ne cherche pas a
# restituer une mise en forme dans une ligne de 200 caracteres sans retour a la
# ligne, seulement a ce qu'aucun caractere de balisage n'y survive.
_MD = (
    (re.compile(r"`([^`]+)`"), r"\1"),           # code en ligne
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),   # image -> son texte
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),    # lien  -> son texte
    (re.compile(r"\*\*\*([^*]+)\*\*\*"), r"\1"),      # ***fort***
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),          # **gras**
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]*[^*\s\n])\*(?![\w*])"), r"\1"),  # *italique*
    (re.compile(r"__([^_]+)__"), r"\1"),                 # __gras__
    (re.compile(r"(?<![\w_])_(?!\s)([^_\n]*[^_\s\n])_(?![\w_])"), r"\1"),    # _italique_
    (re.compile(r"~~([^~]+)~~"), r"\1"),                 # ~~barre~~
)


def sans_markdown(v):
    """Retire le balisage Markdown d'un message. Repli : la chaine d'origine."""
    if not isinstance(v, str) or not v:
        return v
    try:
        lignes = []
        for l in v.splitlines():
            l = l.strip()
            if not l or re.fullmatch(r"[-*_]{3,}", l):
                continue                      # ligne vide, ou filet horizontal
            if l.startswith("```") or l.startswith("~~~"):
                continue                      # ouverture/fermeture de bloc de code
            l = re.sub(r"^\s*>+\s*", "", l)   # citation
            # Un titre garde son texte et gagne un deux-points : aplati sur une
            # seule ligne, « Ce que j'ai decide Je n'ai pas... » collait deux
            # phrases sans ponctuation entre elles.
            t = re.match(r"^(#{1,6})\s+(.*)$", l)
            if t:
                l = t.group(2).rstrip(" :") + " :"
            else:
                # Puce ou numero de liste -> point median. Le texte reste sur
                # une ligne unique, il lui faut donc une marque de separation.
                l = re.sub(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+", "\u00b7 ", l)
            lignes.append(l)
        out = " ".join(lignes)
        for motif, remp in _MD:
            out = motif.sub(remp, out)
        out = " ".join(out.split())
        # Un deux-points suivi d'un point median n'apporte rien de plus.
        out = out.replace(": \u00b7", " :").replace(" :  ", " : ")
        return out or v
    except Exception:
        return v


def txt(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def notif_type(data):
    """Type de notification, en cherchant dans plusieurs cles plausibles.

    Observe en pratique sur 2.1.245 : c'est toujours `notification_type` qui
    porte la valeur, `hook_event_name` valant "Notification" et `message`/
    `title` etant du texte destine a l'humain. Les autres cles et le balayage
    du texte sont un repli defensif, jamais declenche dans mes essais.
    """
    for k in NOTIF_KEYS:
        v = txt(data.get(k))
        if v and v in KNOWN_NOTIF:
            return v
    for k in NOTIF_KEYS:
        v = txt(data.get(k))
        if v and v.startswith("quota_auto_resume"):
            return v
    # Repli : le type cite a l'interieur d'un champ texte.
    for k in ("message", "title"):
        v = txt(data.get(k))
        if not v:
            continue
        for t in KNOWN_NOTIF:
            if t in v:
                return t
    # Rien de reconnu : renvoyer la premiere valeur brute trouvee, uniquement
    # pour que le journal dise ce qui est passe.
    for k in NOTIF_KEYS:
        v = txt(data.get(k))
        if v and v != "Notification":
            return v
    return None


def resolve_state(arg, data):
    """Renvoie (state, reason) ou (None, None) s'il n'y a rien a ecrire."""
    if arg != "--from-notification":
        if arg not in VALID_STATES:
            log("etat inconnu en argument: %r" % (arg,))
            return None, None
        return arg, None

    t = notif_type(data)
    if not t:
        return None, None
    if t in IGNORED_NOTIF or t.startswith("quota_auto_resume"):
        return None, None
    state = STATE_BY_NOTIF.get(t)
    if not state:
        log("notification ignoree, type non mappe: %r" % (t,))
        return None, None
    return state, t


def main(argv):
    if len(argv) < 2:
        return
    arg = argv[1]

    data = read_stdin_json()

    sid = txt(data.get("session_id"))
    if not sid or "/" in sid or ".." in sid or os.sep in sid:
        return

    state, reason = resolve_state(arg, data)
    if state is None:
        return

    home = board_home()
    path = os.path.join(home, "state", sid + ".event.json")
    old = read_json_file(path)

    now = int(time.time())

    # L'invariant du board : state_since ne bouge que sur un vrai changement.
    state_since = now
    if old is not None and old.get("state") == state:
        prev = old.get("state_since")
        if isinstance(prev, (int, float)) and not isinstance(prev, bool) and prev > 0:
            state_since = int(prev)

    hook = txt(data.get("hook_event_name"))

    # tool : PreToolUse uniquement, null sur toute autre transition.
    # (hook absent = payload de test ; on accepte alors tool_name tel quel.)
    tool = None
    tool_name = txt(data.get("tool_name"))
    if tool_name and (hook == "PreToolUse" or hook is None):
        tool = tool_name

    # last_say : porte par Stop / StopFailure / SubagentStop, seuls hooks a
    # recevoir last_assistant_message. Retours a la ligne aplatis, 200 car.
    #
    # ATTENTION, piege verifie en production : Stop ecrit la phrase, puis la
    # Notification "idle_prompt" arrive quelques centaines de millisecondes plus
    # tard pour le MEME evenement logique et, sans le repli ci-dessous, la
    # remettait a null. Resultat : la ligne « ce que dit la session » n'a jamais
    # rien affiche. On conserve donc la derniere phrase tant que la session n'a pas
    # REPRIS le travail : c'est exactement sa duree de validite.
    last_say = None
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        # sans_markdown() d'abord, troncature ensuite : cf. son commentaire.
        last_say = " ".join(sans_markdown(msg).split())[:LAST_SAY_MAX]
    elif old is not None and state != "working":
        last_say = txt(old.get("last_say"))

    cwd = txt(data.get("cwd"))
    if cwd is None and old is not None:
        cwd = txt(old.get("cwd"))

    atomic_write(path, {
        "session_id": sid,
        "state": state,
        "state_since": state_since,
        "cwd": cwd,
        "tool": tool,
        "last_say": last_say,
        "reason": reason,
        "updated_at": now,
    })


# --- auto-verification du de-Markdown --------------------------------------
# `board-event.py --autoverif`. Les quinze cas viennent de deux endroits : les
# phrases reellement observees le 02/09 sur les conversations vivantes, et les
# faux positifs trouves en ecrivant la fonction — « 2 * 3 * 4 » que l'italique
# mordait, et un bloc de code que la cloture avalait entierement une fois le
# texte aplati sur une ligne.
#
# SEUL MODE DE CE FICHIER QUI PEUT RENDRE UN CODE NON NUL. Le contrat « code
# retour TOUJOURS 0 » protege la session de l'utilisateur d'un hook bavard ;
# une auto-verification n'est pas un hook, et un test qui ne peut pas echouer
# ne sert a rien.
_CAS_MD = (
    ("Verifie localement : mon commit `1a2b3c4` **s'applique sans conflit** sur la suite",
     "Verifie localement : mon commit 1a2b3c4 s'applique sans conflit sur la suite"),
    ("Reponse a `projeta-71`.\n\n## Ce que j'ai decide\n\nJe n'ai **pas** pris son correctif",
     "Reponse a projeta-71. Ce que j'ai decide : Je n'ai pas pris son correctif"),
    ("- premier point\n- second point", "\u00b7 premier point \u00b7 second point"),
    ("1. un\n2. deux", "\u00b7 un \u00b7 deux"),
    ("Voir [la doc](https://exemple.fr/x) pour la suite", "Voir la doc pour la suite"),
    ("> une citation\n\ntexte", "une citation texte"),
    ("---\n\nApres le filet", "Apres le filet"),
    ("~~annule~~ puis _revu_ et __confirme__", "annule puis revu et confirme"),
    ("```bash\nls -la\n```", "ls -la"),
    ("Un fichier board_event_test.py et snake_case_intact",
     "Un fichier board_event_test.py et snake_case_intact"),
    ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),                 # multiplication, pas italique
    ("un *mot* en italique", "un mot en italique"),
    ("chemin /home/x_y/z_w intact", "chemin /home/x_y/z_w intact"),
    ("", ""),
    ("Rien a nettoyer ici.", "Rien a nettoyer ici."),
)


def autoverif():
    ecarts = 0
    for entree, attendu in _CAS_MD:
        vu = sans_markdown(entree)
        if vu != attendu:
            ecarts += 1
            print("ECART  %r\n       attendu %r" % (vu, attendu))
    print("de-Markdown : %d cas, %s"
          % (len(_CAS_MD), "tout va" if not ecarts else "%d ecart(s)" % ecarts))
    # Les entrees non textuelles ne doivent jamais lever ni convertir.
    for v in (None, 42, [], {}):
        if sans_markdown(v) != v:
            print("ECART  %r modifie" % (v,)); ecarts += 1
    return ecarts


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--autoverif":
        sys.exit(1 if autoverif() else 0)
    try:
        main(sys.argv)
    except BaseException as exc:          # y compris SystemExit / KeyboardInterrupt
        try:
            log("erreur: %r" % (exc,))
        except Exception:
            pass
    sys.exit(0)
