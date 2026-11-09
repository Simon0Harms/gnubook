# Matrix: login links and notifications

gnubook can talk to its users through a Matrix bot – the same set-up as in
[Portriga](https://github.com/Simon0Harms/Portriga):

- **Link a Matrix account** with a gnubook user (*Konto und Sprache → Matrix-Konto verknüpfen*): gnubook shows a
  code `GB-XXXX-XXXX`, the user sends it to the bot in a direct chat. The sender of that message is authenticated by
  the homeserver, which proves that the Matrix ID belongs to the user.
- **Log in with a link**: on the login page (*Login-Link per Matrix*, user name or Matrix ID) or by writing `login`
  to the bot. The bot sends a one-time link, valid `[matrix] login_minutes` (5). Opening it shows a button; only that
  button logs in, so the link preview of a Matrix client cannot use the link up.
- **Notifications** of the Belegeingang: a failed run (error, failed login, approval required) or processing errors
  (Portfolio Performance import, Nextcloud copy) go to every user of the book who has Matrix linked and
  notifications on. The same failure is reported once, not after every timer run; when it works again, the users
  get one message about that.
- *Testnachricht senden* on the account page checks the whole way.

gnubook itself has no Matrix dependency. A sidecar with [matrix-nio](https://github.com/matrix-nio/matrix-nio)
(end-to-end encryption) is logged in as the bot; both exchange one JSON file per message:

```
browser ──HTTP──▶ gnubook ──▶ data/matrix/outbox/ ──▶ gnubook_matrix_bot.py ──▶ Matrix (E2EE DM)
gnubook postbox-fetch ─┘ ◀── data/matrix/inbox/  ◀──       (matrix-nio)     ◀── DM of the user
```

The web app reads the inbox every `[matrix] poll_seconds` (2) in a background thread. Postbox runs (timer, requests
from the web) write their notifications into the outbox themselves.

## Installation

1. Create a Matrix account for the bot, e.g. `@gnubookbot:example.org`.
2. `bash /opt/gnubook/src/deploy/install-matrix.sh @gnubookbot:example.org https://buch.example.org`
   (3rd parameter, optional: the homeserver's client URL, default `https://<server of the Matrix ID>`).
   This installs `libolm-dev` and matrix-nio into `/opt/gnubook/matrix-venv`, writes `[matrix] bot_mxid` and
   `public_url` into `config.toml`, creates `/opt/gnubook/matrix/gnubook-matrix.env` and the unit
   `gnubook-matrix.service`.
3. Put the bot's password into `/opt/gnubook/matrix/gnubook-matrix.env` (`GNUBOOK_MATRIX_PASSWORD`), then
   `systemctl enable --now gnubook-matrix` and watch `journalctl -fu gnubook-matrix`.
4. After the first login the bot keeps an access token in `/opt/gnubook/matrix-store/bot-state.json`; remove the
   password from the env file again.
5. In gnubook: *Konto und Sprache → Matrix-Konto verknüpfen*, start a direct chat with the bot and send the code.

`gnubook-update` runs `install-matrix.sh --update` when the bot is installed and backs up `matrix/` and
`matrix-store/` with the rest. Keep `/opt/gnubook/matrix-store` in your backups: it holds the bot's encryption
keys; without it the bot has to log in again as a new device.

## Configuration

`[matrix]` in `config.toml`:

| Key | Meaning |
|---|---|
| `bot_mxid` | Matrix ID of the bot; empty = Matrix off. Must equal `GNUBOOK_MATRIX_USER` of the sidecar |
| `public_url` | Address of gnubook as users open it, incl. a path prefix; needed for login links |
| `login` | Allow logging in with a link (default `true`) |
| `notify` | Belegeingang notifications (default `true`; every user can turn them off for themselves) |
| `link_minutes`, `login_minutes` | Validity of a link code (15) and of a login link (5) |
| `poll_seconds` | Seconds between two looks into the inbox (2; 0 = no background thread) |

The sidecar is configured in `/opt/gnubook/matrix/gnubook-matrix.env` (see `deploy/matrix/env.example`). With
`GNUBOOK_MATRIX_REQUIRE_ENCRYPTION=1` (default) it sends only into end-to-end encrypted chats – a login link never
leaves the homeserver in plain text. Element and other clients encrypt new direct chats by default.

## Bot commands

| Message to the bot | Effect |
|---|---|
| `GB-XXXX-XXXX` | Links the sender's Matrix ID and this chat with the gnubook user who asked for the code |
| `login` | Sends a login link (at most one every 30 seconds); from a new chat, later messages go there |
| `hilfe` / `help` | Lists the commands |

## Security notes

- A login link is as good as the password: whoever reads the user's Matrix chat can log in. Turn it off with
  `[matrix] login = false` if that is not acceptable; linking and notifications keep working.
- Codes and login tokens are stored only as HMAC (key: `[app] secret_key`) in `system.sqlite` (`matrix_codes`).
  Login links are single use; failed attempts count towards the login lock-out (8 per IP in 15 minutes).
- The login page answers a link request the same way whether the user exists or not.
- One Matrix ID can be linked with one gnubook user only. *Verknüpfung lösen* tells the old chat and the bot leaves
  it. If the user leaves the chat, gnubook forgets the chat (the Matrix ID stays linked); `login` from a new chat
  restores the delivery.
- The demo users never get Matrix.
