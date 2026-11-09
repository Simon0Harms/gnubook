#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gnubook Matrix-Bot (Sidecar)
============================
Verbindet gnubook mit Matrix – für die Verknüpfung von Benutzern mit Matrix-Konten,
Login-Links und Benachrichtigungen (z. B. Fehler beim Belegeingang). Übernommen aus
dem Sidecar von Portriga (gleiches Spool-Prinzip, ohne Ankündigungsraum).

  * Nimmt Raum-Einladungen automatisch an (User starten einen 1:1-Chat mit dem Bot).
  * Legt jede eingehende Textnachricht (außer eigenen) als JSON-Datei in die
    INBOX. gnubook wertet sie aus (Verknüpfungscode „GB-…", Befehl „login").
    Der Absender (``sender``) ist vom Homeserver authentifiziert.
  * Sendet von gnubook in die OUTBOX gelegte Nachrichten.
  * Aufträge mit ``"leave": true`` (Verknüpfung gelöst): nach dem Senden – bzw.
    wenn das Senden endgültig scheitert – verlässt der Bot den Raum und
    vergisst ihn (``room_leave`` + ``room_forget``).
  * Verlässt ein User einen Raum (membership leave/ban), wird das als
    ``{"type": "leave", ...}`` in die INBOX gemeldet; ist danach niemand außer
    dem Bot mehr im Raum, verlässt der Bot ihn ebenfalls.

gnubook selbst bleibt dadurch ohne Matrix-/E2EE-Abhängigkeiten.

Konfiguration: Umgebungsvariablen bzw. env-Datei (GNUBOOK_MATRIX_ENV_FILE),
siehe env.example.
"""

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime

try:
    from nio import (
        AsyncClient, AsyncClientConfig, LoginResponse, RoomSendResponse,
        RoomMessageText, RoomMessageNotice, MegolmEvent, InviteMemberEvent,
        RoomMemberEvent,
    )
except ImportError:
    sys.stderr.write(
        "Fehlt: matrix-nio mit E2EE. Bitte installieren:\n"
        "  pip install \"matrix-nio[e2e]\"\n"
        "(matrix-nio >= 0.26 bringt vodozemac mit: deploy/install-matrix.sh --update)\n"
    )
    raise


# ---------------------------------------------------------------------------
# .env-Datei laden (auch Start ohne systemd möglich) – wie im KKk58-Sidecar
# ---------------------------------------------------------------------------
def _parse_env_value(val):
    val = val.strip()
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    # Inline-Kommentar nur nach Leerraum (ein führendes # gehört evtl. zu einem Alias)
    for i in range(1, len(val)):
        if val[i] == "#" and val[i - 1] in " \t":
            return val[:i].rstrip()
    return val


def load_env_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k and k not in os.environ:
                    os.environ[k] = _parse_env_value(v)
    except OSError:
        pass


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and (v is None or v == ""):
        sys.stderr.write(f"Konfiguration fehlt: {name}\n")
        sys.exit(2)
    return v


class Config:
    def __init__(self):
        self.homeserver = env("GNUBOOK_MATRIX_HOMESERVER", required=True)   # https://matrix.example.org
        self.user_id = env("GNUBOOK_MATRIX_USER", required=True)            # @gnubookbot:example.org
        self.password = env("GNUBOOK_MATRIX_PASSWORD")                      # nur beim Erststart nötig
        data_dir = env("GNUBOOK_MATRIX_SPOOL_DIR", "/opt/gnubook/data/matrix")
        self.outbox_dir = env("GNUBOOK_MATRIX_OUTBOX_DIR", os.path.join(data_dir, "outbox"))
        self.inbox_dir = env("GNUBOOK_MATRIX_INBOX_DIR", os.path.join(data_dir, "inbox"))
        self.outbox_ttl = int(env("GNUBOOK_MATRIX_OUTBOX_TTL", "900"))
        self.outbox_max_attempts = int(env("GNUBOOK_MATRIX_OUTBOX_MAX_ATTEMPTS", "5"))
        self.store_path = env("GNUBOOK_MATRIX_STORE", "/opt/gnubook/matrix-store")
        self.state_file = env("GNUBOOK_MATRIX_STATE", os.path.join(self.store_path, "bot-state.json"))
        self.sync_timeout_ms = int(env("GNUBOOK_MATRIX_SYNC_TIMEOUT_MS", "10000"))  # Long-Poll je Sync
        self.device_name = env("GNUBOOK_MATRIX_DEVICE_NAME", "gnubook-Bot")
        # 1 = Login-Links/Antworten NUR in verschlüsselte Räume senden (empfohlen).
        # Eingehende Codes werden unabhängig davon angenommen.
        self.require_encryption = env("GNUBOOK_MATRIX_REQUIRE_ENCRYPTION", "1") != "0"
        # Nur Nachrichten annehmen, die jünger sind als X Sekunden (Schutz vor
        # Wiederverarbeitung alter Timeline beim Erststart).
        self.max_event_age = int(env("GNUBOOK_MATRIX_MAX_EVENT_AGE", "1800"))


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_spool(directory, obj):
    """Atomar eine JSON-Datei in ein Spool-Verzeichnis legen (tmp + rename)."""
    os.makedirs(directory, exist_ok=True)
    fid = "%s-%s" % (format(int(time.time() * 1000), "x"), secrets.token_hex(8))
    tmp = os.path.join(directory, "." + fid + ".tmp")
    fin = os.path.join(directory, fid + ".json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, fin)
    return fid


class _PlaintextRefused(Exception):
    pass


class GnubookBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = load_state(cfg.state_file)
        self.client = None
        self._stop = False
        self._outbox_attempts = {}
        self._seen = set(self.state.get("seen", []))  # zuletzt verarbeitete event_ids

    def log(self, *a):
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

    # ---- eingehend ----
    async def _on_message(self, room, event):
        try:
            sender = getattr(event, "sender", "") or ""
            if sender == self.cfg.user_id:
                return
            eid = getattr(event, "event_id", None)
            if not eid or eid in self._seen:
                return
            ts = int(getattr(event, "server_timestamp", 0) or 0)
            if ts and (time.time() * 1000 - ts) > self.cfg.max_event_age * 1000:
                return
            body = getattr(event, "body", None)
            if not body:
                return
            write_spool(self.cfg.inbox_dir, {
                "eventId": eid,
                "roomId": room.room_id,
                "sender": sender,
                "body": str(body)[:2000],
                "ts": ts,
                "encrypted": bool(getattr(room, "encrypted", False)),
            })
            self._remember(eid)
            self.log("Inbox: Nachricht von", sender, "in", room.room_id)
        except Exception as e:  # Callback-Fehler dürfen den Sync nicht abbrechen
            self.log("Inbox-Fehler (ignoriert):", repr(e))

    async def _on_undecryptable(self, room, event):
        self.log("Nicht entschlüsselbare Nachricht von", getattr(event, "sender", "?"),
                 "in", room.room_id, "– Geräteschlüssel fehlen evtl. noch; der User sollte die Nachricht erneut senden.")

    async def _on_invite(self, room, event):
        # Nur Einladungen AN den Bot annehmen
        if getattr(event, "state_key", None) != self.cfg.user_id:
            return
        if getattr(event, "membership", None) != "invite":
            return
        try:
            await self.client.join(room.room_id)
            self.log("Einladung angenommen:", room.room_id, "von", getattr(event, "sender", "?"))
        except Exception as e:
            self.log("Beitritt fehlgeschlagen:", room.room_id, repr(e))

    async def _on_member(self, room, event):
        """User hat den Raum verlassen (oder wurde gebannt) -> an die gnubook melden."""
        try:
            who = getattr(event, "state_key", "") or ""
            if not who or who == self.cfg.user_id:
                return
            if getattr(event, "membership", None) not in ("leave", "ban"):
                return
            if getattr(event, "prev_membership", None) not in (None, "join", "invite"):
                return
            eid = getattr(event, "event_id", None)
            if not eid or eid in self._seen:
                return
            ts = int(getattr(event, "server_timestamp", 0) or 0)
            if ts and (time.time() * 1000 - ts) > self.cfg.max_event_age * 1000:
                return
            write_spool(self.cfg.inbox_dir, {
                "type": "leave",
                "eventId": eid,
                "roomId": room.room_id,
                "sender": who,
                "ts": ts,
            })
            self._remember(eid)
            self.log("Inbox:", who, "hat", room.room_id, "verlassen")
            others = [u for u in list(getattr(room, "users", {}).keys())
                      if u not in (self.cfg.user_id, who)]
            if not others:
                await self._leave_room(room.room_id)
        except Exception as e:
            self.log("Member-Fehler (ignoriert):", repr(e))

    def _remember(self, eid):
        self._seen.add(eid)
        if len(self._seen) > 2000:
            self._seen = set(list(self._seen)[-1000:])
        self.state["seen"] = list(self._seen)[-1000:]
        save_state(self.cfg.state_file, self.state)

    async def _join_invites(self):
        """Fallback: offene Einladungen annehmen, die der Callback verpasst hat."""
        for rid in list(getattr(self.client, "invited_rooms", {}).keys()):
            try:
                await self.client.join(rid)
                self.log("Einladung angenommen (Nachzügler):", rid)
            except Exception as e:
                self.log("Beitritt fehlgeschlagen:", rid, repr(e))

    # ---- ausgehend ----
    async def _send_to_room(self, rid, body):
        if rid not in self.client.rooms:
            await self.client.join(rid)
            await self.client.sync(timeout=0)
        enc = rid in self.client.rooms and self.client.rooms[rid].encrypted
        if self.cfg.require_encryption and not enc:
            raise _PlaintextRefused(rid)
        resp = await self.client.room_send(
            rid, "m.room.message", {"msgtype": "m.text", "body": body},
            ignore_unverified_devices=True,
        )
        if not isinstance(resp, RoomSendResponse):
            raise RuntimeError("room_send: " + repr(resp))

    async def _leave_room(self, rid):
        try:
            resp = await self.client.room_leave(rid)
            if type(resp).__name__.endswith("Error"):
                raise RuntimeError(repr(resp))
            try:
                await self.client.room_forget(rid)
            except Exception as e:  # Vergessen ist optional
                self.log("room_forget fehlgeschlagen (ignoriert):", rid, repr(e))
            self.log("Raum verlassen:", rid)
        except Exception as e:
            self.log("Raum verlassen fehlgeschlagen:", rid, repr(e))

    async def _finish(self, fpath, mid, msg):
        """Auftrag abschließen: Datei entfernen und ggf. Raum verlassen."""
        self._safe_remove(fpath)
        self._outbox_attempts.pop(mid, None)
        if msg.get("leave") and msg.get("roomId"):
            await self._leave_room(msg["roomId"])

    @staticmethod
    def _safe_remove(p):
        try:
            os.remove(p)
        except OSError:
            pass

    async def process_outbox(self):
        try:
            names = sorted(n for n in os.listdir(self.cfg.outbox_dir)
                           if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return
        if not names:
            return
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        if self.client.should_query_keys:
            await self.client.keys_query()
        for name in names:
            fpath = os.path.join(self.cfg.outbox_dir, name)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    msg = json.load(f)
            except (OSError, ValueError):
                self._safe_remove(fpath)
                continue
            mid = str(msg.get("id") or name)
            age = time.time() - (msg.get("createdAt", 0) / 1000)
            room, body = msg.get("roomId"), msg.get("body")
            if age > self.cfg.outbox_ttl or not room or not body:
                await self._finish(fpath, mid, msg)
                continue
            try:
                await self._send_to_room(room, body)
                self.log("Outbox: gesendet an", room)
                await self._finish(fpath, mid, msg)
            except _PlaintextRefused:
                self.log("Outbox: Raum", room, "ist NICHT verschlüsselt – nicht gesendet "
                         "(GNUBOOK_MATRIX_REQUIRE_ENCRYPTION=1). Auftrag verworfen.")
                await self._finish(fpath, mid, msg)
            except Exception as e:
                n = self._outbox_attempts.get(mid, 0) + 1
                self._outbox_attempts[mid] = n
                self.log("Outbox: Senden fehlgeschlagen (Versuch %d) an %s: %r" % (n, room, e))
                if n >= self.cfg.outbox_max_attempts:
                    await self._finish(fpath, mid, msg)

    # ---- Ablauf ----
    async def connect(self):
        for d in (self.cfg.store_path, os.path.dirname(self.cfg.state_file) or ".",
                  self.cfg.inbox_dir, self.cfg.outbox_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except PermissionError:
                sys.stderr.write(f"Kein Schreibrecht: {d}\n")
                sys.exit(4)
        conf = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        dev_id = self.state.get("device_id")
        self.client = AsyncClient(self.cfg.homeserver, self.cfg.user_id,
                                  device_id=dev_id or "", store_path=self.cfg.store_path, config=conf)
        token = self.state.get("access_token")
        if token and dev_id:
            self.client.restore_login(self.cfg.user_id, dev_id, token)
            self.client.load_store()
            self.log("Angemeldet (wiederhergestellt) als", self.cfg.user_id, "device", dev_id)
        else:
            if not self.cfg.password:
                sys.stderr.write("Erststart benötigt GNUBOOK_MATRIX_PASSWORD (danach Token-basiert).\n")
                sys.exit(2)
            resp = await self.client.login(self.cfg.password, device_name=self.cfg.device_name)
            if not isinstance(resp, LoginResponse):
                sys.stderr.write(f"Login fehlgeschlagen: {resp}\n")
                sys.exit(1)
            self.state["device_id"] = resp.device_id
            self.state["access_token"] = resp.access_token
            save_state(self.cfg.state_file, self.state)
            self.log("Angemeldet (neu) als", self.cfg.user_id, "device", resp.device_id)

        self.client.add_event_callback(self._on_message, (RoomMessageText, RoomMessageNotice))
        self.client.add_event_callback(self._on_undecryptable, (MegolmEvent,))
        self.client.add_event_callback(self._on_invite, (InviteMemberEvent,))
        self.client.add_event_callback(self._on_member, (RoomMemberEvent,))

        await self.client.sync(timeout=30000, full_state=True)
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        await self._join_invites()

    async def loop(self):
        while not self._stop:
            try:
                await self.client.sync(timeout=self.cfg.sync_timeout_ms)
                if self.client.should_upload_keys:
                    await self.client.keys_upload()
                await self._join_invites()
            except Exception as e:
                self.log("Sync-Hinweis (fahre fort):", repr(e))
                await asyncio.sleep(3)
            await self.process_outbox()

    async def run(self):
        await self.connect()
        try:
            await self.loop()
        finally:
            self._stop = True
            await self.client.close()


def main():
    load_env_file(os.environ.get("GNUBOOK_MATRIX_ENV_FILE", "/opt/gnubook/matrix/gnubook-matrix.env"))
    try:
        asyncio.run(GnubookBot(Config()).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
