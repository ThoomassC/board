"""Notifieur de le board.

Module importable, bibliothèque standard uniquement.

    from notifier import Notifier
    n = Notifier(config, state_path)          # ~/.claude/board/notify.state.json
    n.evaluate(sessions, account, board_focused)

`evaluate` est appelée à chaque cycle du serveur (~1 s). Elle détecte les
transitions par rapport à son propre état persisté, applique les règles
anti-spam, et déclenche les toasts Windows via `board-toast.ps1`.

Contrats respectés :
  - ne lève JAMAIS d'exception vers l'appelant ;
  - ne bloque JAMAIS : l'appel PowerShell part dans un thread avec timeout ;
  - écriture d'état atomique (`.tmp` + `os.replace`) ;
  - fonctionne si un fichier est absent ou corrompu.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

# --------------------------------------------------------------------------
# Constantes de la matrice de déclenchement et des règles anti-spam
# --------------------------------------------------------------------------

DEBOUNCE_S = {"blocked": 4.0, "error": 2.0}

SILENT_AT_S = 180.0          # mutisme total, pas la durée dans l'état silent
REVIEW_AFTER_S = 90.0        # non vu depuis
REVIEW_MIN_TURN_S = 60.0     # le tour doit avoir duré plus que ça
REMINDER_AFTER_S = 300.0     # rappel unique du blocage

GROUP_WINDOW_S = 10.0        # regroupement des événements de même type
RATE_S = 20.0                # débit global : 1 toast / 20 s, jeton unique
FOCUS_GRACE_S = 3.0          # grâce après la perte de focus du board

SOUND_COOLDOWN_S = 60.0
SOUND_FATIGUE_N = 3
SOUND_FATIGUE_WINDOW_S = 300.0
SOUND_MUTE_S = 600.0

FIVE_HOUR_S = 5 * 3600.0
FIVE_HOUR_WARN_PCT = 80.0
FIVE_HOUR_DEAD_PCT = 99.0

SESSION_TTL_S = 24 * 3600.0  # oubli des sessions disparues
QUEUE_TTL_S = 900.0          # un événement en file plus vieux que ça est périmé
SAVE_EVERY_S = 60.0
PS_TIMEOUT_S = 20.0
MAX_INFLIGHT = 8             # garde-fou sur les threads PowerShell

GROUP_NAME = "board"

SND_REMINDER = "ms-winsoundevent:Notification.Reminder"
SND_DEFAULT = "ms-winsoundevent:Notification.Default"
SND_ALARM = "ms-winsoundevent:Notification.Looping.Alarm2"

# Priorité de sortie quand plusieurs types attendent le jeton.
# `error` et `limit_exhausted` ne passent pas par le jeton (voir _flush).
PRIORITY = (
    "blocked",
    "reminder",
    "silent",
    "review",
    "ctx_crit",
    "ctx_warn",
    "limit80",
)

STATE_TYPES = ("blocked", "error", "silent", "review")

CTX_ADVICE = {
    "ctx_warn": "encore de la marge, mais pense à faire le point",
    "ctx_crit": "pense à /compact ou à repartir sur une nouvelle conversation",
}

# Raisons d'arrêt connues -> formulation française. Jamais de vocabulaire
# d'implémentation à l'écran, jamais d'excuse.
REASONS_FR = {
    "permission_prompt": "une autorisation est restée sans réponse",
    "permission_denied": "l'autorisation a été refusée",
    "idle": "plus rien ne venait",
    "tool_error": "un outil a échoué",
    "api_error": "la liaison avec l'API a lâché",
    "overloaded": "l'API était saturée",
    "rate_limit": "la limite d'usage a été atteinte",
    "usage_limit": "la limite d'usage a été atteinte",
    "context_overflow": "le contexte était plein",
    "cancelled": "le tour a été interrompu",
    "interrupted": "le tour a été interrompu",
    "killed": "le processus a été tué",
    "crash": "le processus s'est arrêté net",
}


# --------------------------------------------------------------------------
# Utilitaires de mise en forme (français)
# --------------------------------------------------------------------------


def _fmt_duration(seconds) -> str:
    """`45s`, `4m12`, `18m`, `1h04`."""
    try:
        s = int(max(0.0, float(seconds)))
    except Exception:
        return "0s"
    if s < 60:
        return "%ds" % s
    minutes, sec = divmod(s, 60)
    if minutes < 60:
        if minutes < 10:
            return "%dm%02d" % (minutes, sec)
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%dh%02d" % (hours, minutes)


def _fmt_hour(ts) -> str:
    try:
        return time.strftime("%Hh%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def _fmt_ratio(value) -> str:
    try:
        v = float(value)
    except Exception:
        return "1"
    if v >= 10:
        return "%d" % int(round(v))
    return ("%.1f" % v).replace(".", ",")


def _fmt_pct(value) -> str:
    try:
        return "%d" % int(round(float(value)))
    except Exception:
        return "?"


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n <= 1 else plural


# --------------------------------------------------------------------------
# Notifieur
# --------------------------------------------------------------------------


class Notifier:
    def __init__(self, config: dict, state_path: str) -> None:
        self.config = config if isinstance(config, dict) else {}
        try:
            self.state_path = os.path.expanduser(str(state_path))
        except Exception:
            self.state_path = str(state_path)

        here = os.path.dirname(os.path.abspath(__file__))
        self.script_path = os.path.join(here, "board-toast.ps1")

        self._powershell = None      # résolu paresseusement
        self._script_win = None      # chemin windows du script, résolu une fois
        self._inflight = 0
        self._lock = threading.Lock()
        self._cold = True            # premier cycle du processus
        self._dirty = False
        self._last_save = 0.0
        self.state = self._load_state()

    # ---------------------------------------------------------------- état

    def _blank_state(self) -> dict:
        return {
            "version": 1,
            "sessions": {},
            "five_hour": {"key": None, "at80": False, "exhausted": False},
            "rate": {"last_toast": 0.0},
            "sound": {"last": 0.0, "recent": [], "mute_until": 0.0},
            "queue": [],
            "active": {},
            "focus": {"focused": False, "lost_at": 0.0},
            "saved_at": 0.0,
        }

    def _load_state(self) -> dict:
        state = self._blank_state()
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key, default in state.items():
                    value = data.get(key, default)
                    if isinstance(default, dict) and isinstance(value, dict):
                        merged = dict(default)
                        merged.update(value)
                        state[key] = merged
                    elif type(value) is type(default) or default is None:
                        state[key] = value
        except Exception:
            pass
        if not isinstance(state.get("sessions"), dict):
            state["sessions"] = {}
        if not isinstance(state.get("queue"), list):
            state["queue"] = []
        if not isinstance(state.get("active"), dict):
            state["active"] = {}
        return state

    def _save_state(self, now: float, force: bool = False) -> None:
        if not force and not self._dirty and (now - self._last_save) < SAVE_EVERY_S:
            return
        try:
            self.state["saved_at"] = now
            directory = os.path.dirname(self.state_path) or "."
            os.makedirs(directory, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
            os.replace(tmp, self.state_path)
            self._dirty = False
            self._last_save = now
        except Exception:
            pass

    # ------------------------------------------------------------- config

    def _notify_cfg(self) -> dict:
        cfg = self.config.get("notify") if isinstance(self.config, dict) else None
        return cfg if isinstance(cfg, dict) else {}

    def _enabled(self) -> bool:
        return bool(self._notify_cfg().get("enabled", True))

    def _threshold(self, name: str, default):
        thr = self.config.get("thresholds") if isinstance(self.config, dict) else None
        if isinstance(thr, dict):
            try:
                return float(thr.get(name, default))
            except Exception:
                return default
        return default

    def _quiet_hours(self, now: float) -> bool:
        raw = self._notify_cfg().get("quiet_hours", [22, 8])
        try:
            start = int(raw[0]) % 24
            end = int(raw[1]) % 24
        except Exception:
            start, end = 22, 8
        if start == end:
            return False
        hour = time.localtime(now).tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # --------------------------------------------------------- boucle

    def evaluate(self, sessions, account, board_focused) -> None:
        """Point d'entrée unique. Ne lève rien, ne bloque pas."""
        try:
            self._evaluate(sessions, account, board_focused)
        except Exception:
            pass

    def _evaluate(self, sessions, account, board_focused) -> None:
        now = time.time()

        if not self._enabled():
            self._cold = False
            return

        sessions = [e for e in (sessions or []) if isinstance(e, dict) and e.get("sid")]
        account = account if isinstance(account, dict) else {}
        focused = bool(board_focused)

        self._track_focus(focused, now)
        events = self._detect(sessions, account, now)
        if events:
            self.state["queue"].extend(events)
            self._dirty = True
        self._purge(sessions, account, now)
        self._flush(sessions, account, focused, now)
        self._forget(sessions, now)
        self._cold = False
        self._save_state(now)

    def _track_focus(self, focused: bool, now: float) -> None:
        foc = self.state["focus"]
        if focused:
            if not foc.get("focused"):
                self._dirty = True
            foc["focused"] = True
            foc["lost_at"] = 0.0
        elif foc.get("focused"):
            foc["focused"] = False
            foc["lost_at"] = now
            self._dirty = True

    def _muted_by_focus(self, focused: bool, now: float) -> bool:
        if focused:
            return True
        lost = self.state["focus"].get("lost_at") or 0.0
        return bool(lost) and (now - lost) < FOCUS_GRACE_S

    # ------------------------------------------------------- détection

    def _session(self, sid: str, now: float):
        sessions = self.state["sessions"]
        st = sessions.get(sid)
        fresh = False
        if not isinstance(st, dict):
            st = {
                "state": None,
                "notified": {},
                "reminder": False,
                "work_s": 0.0,
                "thr": {},
                "seen_at": now,
            }
            sessions[sid] = st
            fresh = True
            self._dirty = True
        for key in ("notified", "thr"):
            if not isinstance(st.get(key), dict):
                st[key] = {}
        if not isinstance(st.get("work_s"), (int, float)):
            st["work_s"] = 0.0
        st["reminder"] = bool(st.get("reminder"))
        return st, fresh

    def _detect(self, sessions, account, now: float) -> list:
        events = []
        silent_after = self._threshold("silent_after_s", 90.0)
        ctx_warn = self._threshold("ctx_warn", 50.0)
        ctx_crit = self._threshold("ctx_crit", 70.0)

        for el in sessions:
            sid = str(el.get("sid"))
            st, fresh = self._session(sid, now)
            st["seen_at"] = now

            prev = st.get("state")
            state = el.get("state") or "working"
            try:
                since = float(el.get("since_s") or 0.0)
            except Exception:
                since = 0.0

            if state == "working":
                # durée du tour en cours : sert au filtre « le tour a duré > 60 s »
                if prev != "working" or since >= float(st.get("work_s") or 0.0):
                    st["work_s"] = since

            # ne-pas-répéter : réarmement dès que la session quitte l'état
            for kind in STATE_TYPES:
                if kind != state and st["notified"].get(kind):
                    st["notified"][kind] = False
                    self._dirty = True
            if state != "blocked" and st.get("reminder"):
                st["reminder"] = False
                self._dirty = True

            if fresh and self._cold:
                # démarrage à froid : on adopte l'existant sans re-notifier
                # tout l'historique. Le rappel 5 min reste possible.
                if state in STATE_TYPES:
                    st["notified"][state] = True

            st["state"] = state

            # -- seuils de contexte : une seule fois par session, jamais réarmés
            ctx = el.get("ctx_pct")
            if isinstance(ctx, (int, float)):
                for kind, level in (("ctx_warn", ctx_warn), ("ctx_crit", ctx_crit)):
                    if float(ctx) >= float(level) and not st["thr"].get(kind):
                        st["thr"][kind] = True
                        self._dirty = True
                        if not fresh:
                            events.append(
                                self._event(kind, el, now, level=float(level))
                            )

            # -- états
            if state == "blocked":
                if not st["notified"].get("blocked") and since >= DEBOUNCE_S["blocked"]:
                    st["notified"]["blocked"] = True
                    self._dirty = True
                    events.append(self._event("blocked", el, now))
                if since >= REMINDER_AFTER_S and not st.get("reminder"):
                    st["reminder"] = True
                    self._dirty = True
                    events.append(self._event("reminder", el, now))

            elif state == "error":
                if not st["notified"].get("error") and since >= DEBOUNCE_S["error"]:
                    st["notified"]["error"] = True
                    self._dirty = True
                    events.append(self._event("error", el, now))

            elif state == "silent":
                mute_s = since + silent_after   # mutisme total
                if mute_s >= SILENT_AT_S and not st["notified"].get("silent"):
                    st["notified"]["silent"] = True
                    self._dirty = True
                    events.append(self._event("silent", el, now, mute_s=mute_s))

            elif state == "review":
                turn_s = float(st.get("work_s") or 0.0)
                if (
                    since >= REVIEW_AFTER_S
                    and not el.get("seen")
                    and turn_s > REVIEW_MIN_TURN_S
                    and not st["notified"].get("review")
                ):
                    st["notified"]["review"] = True
                    self._dirty = True
                    events.append(self._event("review", el, now, turn_s=turn_s))

        events.extend(self._detect_five_hour(account, len(sessions), now))
        return events

    def _detect_five_hour(self, account, n_sessions: int, now: float) -> list:
        five = account.get("five_hour")
        if not isinstance(five, dict):
            return []
        try:
            used = float(five.get("used_pct"))
        except Exception:
            return []
        resets = five.get("resets_at")
        key = str(resets)

        mem = self.state["five_hour"]
        if not isinstance(mem, dict):
            mem = {"key": None, "at80": False, "exhausted": False}
            self.state["five_hour"] = mem
        if mem.get("key") != key:
            mem.clear()
            mem.update({"key": key, "at80": False, "exhausted": False})
            self._dirty = True

        events = []
        if used >= FIVE_HOUR_DEAD_PCT:
            if not mem.get("exhausted"):
                mem["exhausted"] = True
                mem["at80"] = True
                self._dirty = True
                events.append(
                    {
                        "type": "limit_exhausted",
                        "sid": "_account_",
                        "ts": now,
                        "resets_at": resets,
                        "n_sessions": n_sessions,
                    }
                )
        elif used >= FIVE_HOUR_WARN_PCT and not mem.get("at80"):
            mem["at80"] = True
            self._dirty = True
            events.append(
                {
                    "type": "limit80",
                    "sid": "_account_",
                    "ts": now,
                    "resets_at": resets,
                    "used_pct": used,
                }
            )
        return events

    def _event(self, kind: str, el: dict, now: float, **extra) -> dict:
        ev = {
            "type": kind,
            "sid": str(el.get("sid")),
            "ts": now,
            "ident": self._ident(el),
            "title": self._title(el),
            "project": el.get("project") or "",
            "tool": self._tool(el),
            "reason": self._reason(el),
            "ctx_pct": el.get("ctx_pct"),
            "since_s": el.get("since_s"),
        }
        ev.update(extra)
        return ev

    # -------------------------------------------------------- champs texte

    @staticmethod
    def _ident(el: dict) -> str:
        for key in ("ident", "repo", "project"):
            value = el.get(key)
            if value:
                return str(value)
        return str(el.get("sid", ""))[:8]

    @staticmethod
    def _title(el: dict) -> str:
        for key in ("title", "repo", "ident", "project"):
            value = el.get(key)
            if value:
                return str(value)
        return "sans titre"

    @staticmethod
    def _tool(el: dict) -> str:
        """Le nom d'outil n'est pas un champ de la SESSION : on le prend s'il est
        fourni, sinon on le récupère dans `meta` (« ... · Bash · repo »)."""
        tool = el.get("tool")
        if tool:
            return str(tool)
        meta = el.get("meta")
        if isinstance(meta, str) and "·" in meta:
            parts = [p.strip() for p in meta.split("·")]
            for part in parts[1:]:
                if part and part[0].isupper() and " " not in part:
                    return part
        return ""

    @staticmethod
    def _reason(el: dict) -> str:
        raw = el.get("reason")
        if raw:
            key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
            if key in REASONS_FR:
                return REASONS_FR[key]
        meta = el.get("meta")
        if isinstance(meta, str) and meta.strip():
            first = meta.split("·")[0].strip()
            if first:
                return first
        say = el.get("say")
        if isinstance(say, str) and say.strip():
            return say.strip()[:120]
        return "raison inconnue"

    # ------------------------------------------------------------- purge

    def _resolved(self, ev_type: str, sid: str, by_sid: dict, account: dict,
                  level=None) -> bool:
        """Vrai si l'état qui a motivé l'événement n'existe plus."""
        if ev_type in ("limit80", "limit_exhausted"):
            five = account.get("five_hour")
            if not isinstance(five, dict):
                return False
            try:
                used = float(five.get("used_pct"))
            except Exception:
                return False
            floor = (
                FIVE_HOUR_DEAD_PCT if ev_type == "limit_exhausted"
                else FIVE_HOUR_WARN_PCT
            )
            return used < floor

        el = by_sid.get(sid)
        if el is None:
            return True
        if ev_type in STATE_TYPES:
            return el.get("state") != ev_type
        if ev_type == "reminder":
            return el.get("state") != "blocked"
        if ev_type in ("ctx_warn", "ctx_crit"):
            ctx = el.get("ctx_pct")
            if not isinstance(ctx, (int, float)):
                return False
            try:
                return float(ctx) < float(level)
            except Exception:
                return False
        return False

    def _purge(self, sessions, account, now: float) -> None:
        by_sid = {str(e.get("sid")): e for e in sessions}

        # 1. toasts affichés dont l'état est résolu -> retrait par tag
        for tag, info in list(self.state["active"].items()):
            if not isinstance(info, dict):
                self.state["active"].pop(tag, None)
                continue
            kind = info.get("type") or ""
            level = info.get("level")
            sids = info.get("sids") or ["_account_"]
            alive = [
                s for s in sids
                if not self._resolved(kind, s, by_sid, account, level)
            ]
            if not alive:
                self._remove_toast(tag)
                self.state["active"].pop(tag, None)
                self._dirty = True
            elif len(alive) != len(sids):
                info["sids"] = alive
                self._dirty = True

        # 2. événements en file devenus sans objet
        keep = []
        for ev in self.state["queue"]:
            if not isinstance(ev, dict):
                continue
            if (now - float(ev.get("ts") or now)) > QUEUE_TTL_S:
                continue
            if self._resolved(
                ev.get("type") or "", ev.get("sid") or "", by_sid, account,
                ev.get("level"),
            ):
                continue
            keep.append(ev)
        if len(keep) != len(self.state["queue"]):
            self._dirty = True
        self.state["queue"] = keep

    def _forget(self, sessions, now: float) -> None:
        live = {str(e.get("sid")) for e in sessions}
        for sid, st in list(self.state["sessions"].items()):
            if sid in live:
                continue
            seen = 0.0
            if isinstance(st, dict):
                try:
                    seen = float(st.get("seen_at") or 0.0)
                except Exception:
                    seen = 0.0
            if (now - seen) > SESSION_TTL_S:
                self.state["sessions"].pop(sid, None)
                self._dirty = True

    # ------------------------------------------------------------- sortie

    def _flush(self, sessions, account, focused: bool, now: float) -> None:
        queue = self.state["queue"]
        if not queue:
            return
        by_sid = {str(e.get("sid")): e for e in sessions}

        # 1. limite 5 h épuisée : ignore TOUTES les règles anti-spam.
        rest = []
        for ev in queue:
            if ev.get("type") == "limit_exhausted":
                self._emit("limit_exhausted", [ev], by_sid, now, bypass=True)
            else:
                rest.append(ev)
        if len(rest) != len(queue):
            self._dirty = True
        queue = rest
        self.state["queue"] = queue
        if not queue:
            return

        # 2. heures calmes / board au premier plan : on ne montre rien et on
        #    ne conserve rien (pas d'avalanche au réveil).
        if self._quiet_hours(now) or self._muted_by_focus(focused, now):
            self.state["queue"] = []
            self._dirty = True
            return

        # 3. erreurs : exemptées du débit global, sans consommer le jeton.
        errors = [ev for ev in queue if ev.get("type") == "error"]
        if errors:
            group, later = self._group(errors, now)
            self._emit("error", group, by_sid, now)
            queue = [ev for ev in queue if ev.get("type") != "error"] + later
            self.state["queue"] = queue
            self._dirty = True
            if not queue:
                return

        # 4. débit global : 1 toast / 20 s, jeton unique. Les surnuméraires
        #    restent en file et seront fusionnés dans le prochain toast.
        last = float(self.state["rate"].get("last_toast") or 0.0)
        if (now - last) < RATE_S:
            return

        for kind in PRIORITY:
            same = [ev for ev in queue if ev.get("type") == kind]
            if not same:
                continue
            group, later = self._group(same, now)
            self._emit(kind, group, by_sid, now)
            self.state["rate"]["last_toast"] = now
            self.state["queue"] = [
                ev for ev in queue if ev.get("type") != kind
            ] + later
            self._dirty = True
            return

    def _group(self, events: list, now: float):
        """Regroupement 10 s : les événements de même type dans la fenêtre du
        plus ancien partent ensemble, le reste attend le prochain toast."""
        ordered = sorted(events, key=lambda e: float(e.get("ts") or now))
        first = float(ordered[0].get("ts") or now)
        group, later = [], []
        for ev in ordered:
            if float(ev.get("ts") or now) - first <= GROUP_WINDOW_S:
                group.append(ev)
            else:
                later.append(ev)
        return group, later

    # ------------------------------------------------------- textes toasts

    def _refresh(self, ev: dict, by_sid: dict) -> dict:
        """Rafraîchit un événement en file avec les données vivantes."""
        el = by_sid.get(ev.get("sid"))
        if not el:
            return ev
        out = dict(ev)
        out["ident"] = self._ident(el)
        out["title"] = self._title(el)
        out["project"] = el.get("project") or out.get("project") or ""
        out["ctx_pct"] = el.get("ctx_pct", out.get("ctx_pct"))
        out["since_s"] = el.get("since_s", out.get("since_s"))
        if not out.get("tool"):
            out["tool"] = self._tool(el)
        if out.get("reason") in (None, "", "raison inconnue"):
            out["reason"] = self._reason(el)
        return out

    def _line(self, ev: dict) -> str:
        project = ev.get("project") or ""
        head = "%s — " % project if project else ""
        return "%s« %s %s »" % (head, ev.get("ident") or "", ev.get("title") or "")

    def _aggregate(self, title: str, events: list) -> tuple:
        n = len(events)
        lines = [self._line(ev) for ev in events[:2]]
        if n > 2:
            extra = n - 2
            lines.append("+ %d %s." % (extra, _plural(extra, "autre", "autres")))
        return title, "\n".join(lines)

    def _compose(self, kind: str, events: list, now: float):
        """-> (title, body, scenario, audio_wanted) ou None."""
        n = len(events)
        ev = events[0]
        ident = ev.get("ident") or ""
        title = ev.get("title") or ""

        if kind == "blocked":
            if n == 1:
                tool = ev.get("tool") or ""
                suffix = (" %s." % tool) if tool else "."
                return (
                    "✋ %s attend ton OK" % ident,
                    "« %s » — Claude demande une autorisation%s" % (title, suffix),
                    "default",
                    SND_REMINDER,
                )
            head, body = self._aggregate(
                "✋ %d conversations attendent ton OK" % n, events
            )
            return head, body, "default", SND_REMINDER

        if kind == "reminder":
            if n == 1:
                waited = _fmt_duration(ev.get("since_s") or REMINDER_AFTER_S)
                return (
                    "✋ Toujours en attente — %s" % ident,
                    "« %s » attend depuis %s." % (title, waited),
                    "reminder",
                    SND_REMINDER,
                )
            head, body = self._aggregate(
                "✋ %d conversations attendent toujours ton OK" % n, events
            )
            return head, body, "reminder", SND_REMINDER

        if kind == "error":
            if n == 1:
                return (
                    "✖ Erreur — %s" % ident,
                    "« %s » s'est arrêté : %s. Le détail est dans la board de "
                    "board." % (title, ev.get("reason") or "raison inconnue"),
                    "default",
                    SND_DEFAULT,
                )
            head, body = self._aggregate("✖ %d conversations en erreur" % n, events)
            return head, body, "default", SND_DEFAULT

        if kind == "silent":
            if n == 1:
                dur = _fmt_duration(ev.get("mute_s") or SILENT_AT_S)
                return (
                    "⋯ %s est muette depuis %s" % (ident, dur),
                    "« %s » — le pane est peut-être fermé. Va vérifier." % title,
                    "default",
                    "",
                )
            head, body = self._aggregate("⋯ %d conversations sont muettes" % n, events)
            return head, body, "default", ""

        if kind == "review":
            if n == 1:
                dur = _fmt_duration(ev.get("turn_s") or 0)
                return (
                    "➜ %s a rendu la main" % ident,
                    "« %s » — %s de travail, contexte à %s %%."
                    % (title, dur, _fmt_pct(ev.get("ctx_pct"))),
                    "default",
                    "",
                )
            head, body = self._aggregate(
                "➜ %d conversations ont rendu la main" % n, events
            )
            return head, body, "default", ""

        if kind in ("ctx_warn", "ctx_crit"):
            level = _fmt_pct(ev.get("level"))
            advice = CTX_ADVICE[kind]
            if n == 1:
                return (
                    "Contexte à %s %% — %s" % (level, ident),
                    "« %s » — %s." % (title, advice),
                    "default",
                    "",
                )
            head, body = self._aggregate(
                "Contexte à %s %% — %d conversations" % (level, n), events
            )
            return head, body, "default", ""

        if kind == "limit80":
            resets = ev.get("resets_at")
            ratio, eta = self._pace(ev.get("used_pct"), resets, now)
            return (
                "Limite 5 h à 80 %",
                "Rythme ×%s — au train actuel, épuisement vers %s. Réinit. à %s."
                % (_fmt_ratio(ratio), _fmt_hour(eta), _fmt_hour(resets)),
                "default",
                "",
            )

        if kind == "limit_exhausted":
            resets = ev.get("resets_at")
            try:
                delay = max(0.0, float(resets) - now)
            except Exception:
                delay = 0.0
            n_el = int(ev.get("n_sessions") or 0)
            if n_el == 1:
                tail = "1 conversation est en pause."
            else:
                tail = "%d conversations sont en pause." % n_el
            return (
                "⛔ Limite 5 h épuisée",
                "Réinitialisation à %s, dans %s. %s"
                % (_fmt_hour(resets), _fmt_duration(delay), tail),
                "urgent",
                SND_ALARM,
            )

        return None

    def _pace(self, used_pct, resets_at, now: float):
        """Rythme de consommation et heure d'épuisement estimée."""
        try:
            used = float(used_pct)
            resets = float(resets_at)
        except Exception:
            return 1.0, resets_at
        elapsed = FIVE_HOUR_S - (resets - now)
        if elapsed < 60 or used <= 0:
            return 1.0, resets
        expected = elapsed / FIVE_HOUR_S * 100.0
        ratio = used / expected if expected > 0 else 1.0
        rate = used / elapsed                      # % par seconde
        eta = now + (100.0 - used) / rate if rate > 0 else resets
        return ratio, min(eta, resets)

    # ------------------------------------------------------------ émission

    def _emit(self, kind: str, events: list, by_sid: dict, now: float,
              bypass: bool = False) -> None:
        if not events:
            return
        events = [self._refresh(ev, by_sid) for ev in events]
        composed = self._compose(kind, events, now)
        if not composed:
            return
        title, body, scenario, audio = composed

        silent = True
        sound = ""
        if audio:
            if bypass:
                sound, silent = audio, False
            elif self._sound_allowed(now):
                sound, silent = audio, False
                self._sound_used(now)

        sids = [ev.get("sid") for ev in events]
        tag = self._tag(kind, sids)

        # un agrégat remplace les toasts unitaires du même type
        for other, info in list(self.state["active"].items()):
            if other == tag or not isinstance(info, dict):
                continue
            if info.get("type") != kind:
                continue
            if set(info.get("sids") or []) <= set(sids):
                self._remove_toast(other)
                self.state["active"].pop(other, None)

        self.state["active"][tag] = {
            "type": kind,
            "sids": sids,
            "level": events[0].get("level"),
            "at": now,
        }
        self._dirty = True
        self._show_toast(title, body, tag, scenario, sound, silent)

    @staticmethod
    def _tag(kind: str, sids: list) -> str:
        if len(sids) == 1:
            base = "%s-%s" % (kind, str(sids[0])[:16])
        else:
            base = "%s-multi" % kind
        return base[:60]

    # -- son

    def _sound_allowed(self, now: float) -> bool:
        snd = self.state["sound"]
        if not isinstance(snd, dict):
            self.state["sound"] = {"last": 0.0, "recent": [], "mute_until": 0.0}
            return True
        try:
            if now < float(snd.get("mute_until") or 0.0):
                return False
            if (now - float(snd.get("last") or 0.0)) < SOUND_COOLDOWN_S:
                return False
        except Exception:
            return True
        return True

    def _sound_used(self, now: float) -> None:
        snd = self.state["sound"]
        recent = [
            t for t in (snd.get("recent") or [])
            if isinstance(t, (int, float)) and (now - t) < SOUND_FATIGUE_WINDOW_S
        ]
        recent.append(now)
        snd["last"] = now
        snd["recent"] = recent
        if len(recent) >= SOUND_FATIGUE_N:
            snd["mute_until"] = now + SOUND_MUTE_S   # fatigue sonore
            snd["recent"] = []
        self._dirty = True

    # -- PowerShell

    def _resolve_tools(self) -> bool:
        if self._powershell is None:
            found = shutil.which("powershell.exe")
            if not found:
                fallback = (
                    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                )
                found = fallback if os.path.exists(fallback) else ""
            self._powershell = found
        if self._script_win is None:
            win = ""
            try:
                if os.path.exists(self.script_path):
                    out = subprocess.run(
                        ["wslpath", "-w", self.script_path],
                        capture_output=True, timeout=10,
                    )
                    win = out.stdout.decode("utf-8", "replace").strip()
            except Exception:
                win = ""
            self._script_win = win
        return bool(self._powershell and self._script_win)

    def _spawn(self, args: list) -> None:
        """Lance le script dans un thread, avec timeout. N'échoue jamais."""
        if not self._resolve_tools():
            return
        argv = [
            self._powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", self._script_win,
        ] + args
        with self._lock:
            if self._inflight >= MAX_INFLIGHT:
                return
            self._inflight += 1

        def run():
            try:
                subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PS_TIMEOUT_S,
                )
            except Exception:
                pass
            finally:
                with self._lock:
                    self._inflight -= 1

        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            with self._lock:
                self._inflight -= 1

    def _show_toast(self, title: str, body: str, tag: str, scenario: str,
                    audio: str, silent: bool) -> None:
        args = [
            "-Title", title,
            "-Body", body,
            "-Tag", tag,
            "-Group", GROUP_NAME,
            "-Scenario", scenario or "default",
            "-Audio", audio or "",
        ]
        if silent or not audio:
            args.append("-Silent")
        self._spawn(args)

    def _remove_toast(self, tag: str) -> None:
        self._spawn(["-Tag", tag, "-Group", GROUP_NAME, "-Remove"])
