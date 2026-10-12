# Belegeingang: documents from the broker's postbox

gnubook can fetch the documents of a [finanzen.net zero](https://www.finanzen.net/zero/) account from its online
postbox (*Posteingang*) by itself, import them into Portfolio Performance and put a copy into a Nextcloud folder.
Contract notes, tax statements and account statements then arrive in PP without downloading and uploading every
PDF by hand.

A headless browser (Playwright with Firefox) logs in with the stored user name and password, opens the postbox,
clicks *Mehr anzeigen* until it reaches documents it already has, and downloads the new ones. It runs only for the
minute of a run and needs no X server, VNC or other desktop.

```
gnubook-postbox.timer ─┐                      ┌──► finanzen.net zero web app (login, postbox, PDFs)
                       ├─► gnubook postbox-fetch ─┤
web: „Jetzt abrufen“ ──┘   (headless browser)  └──► data/postbox/book-<id>/…   (every PDF, kept)
   (request file + gnubook-postbox-request.path)        │
                                                         ├──► pp-core: PDF import with PP's importers
                                                         └──► Nextcloud folder of the user (WebDAV)
```

## What you get

A menu entry *Belegeingang* with

- the login (user name and password for finanzen.net zero, stored encrypted like the Nextcloud passwords) and the
  settings: *Belege ab*, overlap, automatic retrieval, import into PP, Nextcloud folder;
- *Jetzt abrufen* and *Probelauf* (log in and read the list only), and the last runs with their result;
- every fetched document with its state in Portfolio Performance and in Nextcloud, the PDF itself, and the
  actions *Prüfen* (decide on PP's import page) and *erledigt*.

## Installation

The Belegeingang is optional. Install gnubook first (see [INSTALL.md](INSTALL.md)); Portfolio Performance
([PORTFOLIO-PERFORMANCE.md](PORTFOLIO-PERFORMANCE.md)) is needed for the import, not for fetching and copying. Then,
as root in the gnubook container:

```bash
bash /opt/gnubook/src/deploy/install-postbox.sh            # Firefox
bash /opt/gnubook/src/deploy/install-postbox.sh chromium   # or Chromium
```

The script

- installs Playwright for Python into gnubook's virtualenv (`pip install gnubook[postbox]`) and Playwright's own
  build of the browser to `/opt/gnubook/pw-browsers`, together with the system libraries it needs (Playwright's
  package list for Debian/Ubuntu; where Playwright does not know the distribution, the distribution's own
  `firefox-esr` or `chromium` package is installed for its libraries);
- starts the browser once as user `gnubook` to check that it works;
- writes `[postbox] enabled = true`, `browser` and `browsers_path` into `/opt/gnubook/config.toml`;
- installs `gnubook-postbox.timer` (scheduled runs), `gnubook-postbox-request.path` (runs requested in the web
  interface) and their services, and restarts gnubook.

`gnubook-update` keeps Playwright and its browser build in step with new gnubook versions.

**Resources.** The browser and its libraries take about 250–400 MB of disk. During a run the browser needs about
300–500 MB of memory; between runs nothing is running.

## First steps

1. *Belegeingang*: enter user name and password of finanzen.net zero.
2. *Belege ab*: documents posted before this day are never fetched. Empty: every document down to the oldest one
   in the postbox (the first run clicks *Mehr anzeigen* to the end of the list). For the first run choose the day after the
   last document you imported into PP by hand – otherwise PP finds possible duplicates for every older document
   (it does not import them, but they wait for a decision).
3. *Nextcloud-Ordner*: the folder in your Nextcloud (connected under *Einstellungen → Nextcloud*), e.g.
   `/Finanzen/finanzen.net zero`; *Unterordner*: none, per year (`…/2026/`), per week (`…/2026/KW41/`, ISO week) or per day
   (`…/2026/2026-10-09/` or `…/2026/10/09/`), by posting date, directly below the folder. With a share link the folder
   is inside the shared folder. Empty: no copy.

   Optional: *Eigener Freigabelink für die Belege* – a share link of a folder with edit permission (and its
   password). The documents then go there instead of the Nextcloud of *Einstellungen → Nextcloud* (where the
   GnuCash copy goes); the folder is inside the shared folder, an empty *Nextcloud-Ordner* is its top. The link is
   checked when it is saved; the password is stored encrypted.
4. Save and run a *Probelauf*. It logs in and reads the list down to *Belege ab* without downloading anything. The
   result appears under *Letzte Läufe* after a few seconds (reload the page).
5. *Jetzt abrufen*, check the documents, then switch on *Automatisch abrufen*.

## How a run works

**Where the list stops.** The postbox lists the newest documents first, ten at a time. Within one day the order is
not by recency (the document numbers rise within a day), and documents can be archived or deleted at any time. A run
therefore does not stop at "the document fetched last" – it could miss documents posted later on the same day, or
click through the whole postbox when that document is gone. Instead a run clicks *Mehr anzeigen* until a whole
newly loaded block of ten is older than the **stop day**, and downloads every listed document whose number is not
known yet:

    stop day = max(Belege ab, newest completely listed day − overlap)        (Belege ab before the first run)

The overlap (default 3 days) catches documents that are posted late for an earlier day. A run that hit the click
limit (`[postbox] max_clicks`, if set) or whose *Mehr anzeigen* stopped loading does not move the
"completely listed" day, and a document whose download failed holds it back – the next run lists that range again.
*Einmalig erneut prüfen ab* makes the next complete run list back to an earlier day once.

**Downloads.** A PDF link of the postbox is a plain request that only needs the session; it is fetched inside the
page by the browser itself. Every PDF is stored at once (`data/postbox/book-<id>/finanzen-zero/<year>/`, file name
`2026-10-09_246891282_Wertpapierabrechnung-Kauf-DE0008430026.pdf`) and recorded, so a run that breaks off keeps
what it got.

**Import into Portfolio Performance.** The new PDFs go to pp-core in batches – PP's own importers read them
(finanzen.net zero documents are read by PP's Baader Bank importer: contract notes, dividends and distributions,
Vorabpauschale, Steuerausgleichsrechnung, daily and periodic account statements). As on the *PDF-Import* page,
only what PP's checks find in order is imported; possible duplicates wait for a decision. The file name becomes the
source of the PP transactions (*Beleg: …* in the GnuCash notes). When PP is linked to the book, the new transactions
are booked right after the run, otherwise by the hourly `gnubook-pp.timer`.

| State in PP | Meaning |
|---|---|
| *übernommen* | every transaction of the document was imported |
| *prüfen* | possible duplicate or other warning: *Prüfen* opens the PDF import page with this document, where it can be imported anyway; or mark it *erledigt* |
| *Depot wählen* | PP needs the securities account and cash account for this bank once: *Prüfen*, choose them, *Übernehmen* – PP remembers the choice; the next run imports the waiting documents |
| *nicht erkannt* | PP's importers do not read this kind of document (e.g. *Verlusttopfoptimierung*); it is only kept and copied |
| *teilweise*, *Fehler* | PP could not import (all of) it – the message says why |
| *offen* | not imported yet, e.g. pp-core was not reachable; the next run tries again |
| *erledigt* | marked as done by you |
| *aus* | import switched off, or no Portfolio Performance |

**Copy into Nextcloud.** Every new PDF is uploaded into the Nextcloud folder of the user who saved the settings.
Failed uploads are repeated by every run; *Bisherige Belege … auch in die Nextcloud kopieren* copies documents
fetched before a folder was set.

## When runs happen

- `gnubook-postbox.timer`: Monday to Friday at **23:20** (± 5 minutes) for all postboxes with *Automatisch
  abrufen*; a run missed while the container was down is made up. Change the times with `systemctl edit gnubook-postbox.timer`, e.g.

  ```ini
  [Timer]
  OnCalendar=
  OnCalendar=Mon..Sat *-*-* 21:10:00
  ```

- *Jetzt abrufen* and *Probelauf* in the web interface: the page writes a request into
  `data/postbox/requests/`, `gnubook-postbox-request.path` starts `gnubook postbox-fetch --requests` within
  seconds. The web application itself never starts a browser.
- On the command line: `gnubook postbox-fetch [--book B] [--dry-run]`.

One run per book at a time; a requested run waits for a run that is in progress. A login of gnubook does not end
a web session of finanzen.net zero that is open at the same time (e.g. trading in the browser or with automation).

After a failed login (or when an approval is asked for) the scheduled runs **pause** until the settings are saved
again – a broker may lock an account after a few failed logins. *Jetzt abrufen* still logs in.

## Security

- The password is stored encrypted in `data/system.sqlite` with the key of `[nextcloud] encryption_key` or
  `[app] secret_key` (both in `config.toml`, not in `data/`). The running service can decrypt it – it has to, for
  the scheduled runs.
- The postbox only needs user name and password. If finanzen.net zero asks for an approval in the Secure TAN app
  after the login, the run stops with *Freigabe verlangt* – gnubook never approves anything.
- A run reads the postbox and downloads PDFs; it does not archive documents, mark them as read on purpose or
  change anything else in the account. A cookie banner is answered only before the login and only inside a
  cookie/consent container (*nur notwendige*/*ablehnen* first); after the login no dialog of the app is ever
  clicked. At the end the run logs out when the *Abmelden* button is visible.
- Screenshots and HTML of failed runs have the password fields emptied and every occurrence of the password
  replaced; they still show the page as it was (e.g. the user name), so they are kept with mode 600.

## Troubleshooting

When a run fails, the browser keeps a screenshot and the HTML of the page (password fields emptied) in
`/opt/gnubook/data/postbox/debug/` (the last 10, `[postbox] debug_keep`); the run's message names the file.

- *Anmeldung fehlgeschlagen*: user name or password wrong, or the login form was not recognised. The login page
  could not be inspected while this was written, so its fields are found generically: the visible password field,
  the text or e-mail field before it, the button *Anmelden*/*Login*/*Weiter* (also when the name is asked first and
  the password on a second page); a cookie banner is answered. If the screenshot shows a form that was not filled,
  set CSS selectors in `config.toml`:

  ```toml
  [postbox]
  login_user_selector = "input[formcontrolname='username']"
  login_password_selector = "input[type='password']"
  login_submit_selector = "button[type='submit']"
  ```

- *Freigabe verlangt*: finanzen.net zero wants an approval in the Secure TAN app for this login.
- *nicht installiert*: Playwright or its browser is missing – run `deploy/install-postbox.sh` (again).
- The browser does not start in the container (`journalctl -u gnubook-postbox-request`): try the other browser,
  `install-postbox.sh chromium`.
- *Klickgrenze erreicht*: only when `[postbox] max_clicks` is set – raise it or set it to 0 (no limit).
- `data/postbox/debug/api-paths.txt` lists the web app's own API calls of the last run (method, path and query
  keys; downloads left out, long numbers such as the customer number replaced by `{n}`). They show how the postbox
  could be read without a browser later.
- *Automatischer Abruf pausiert*: the last login failed – check user name and password and save, or fetch by hand.

## Files and services

| Path | Content |
|---|---|
| `/opt/gnubook/data/postbox/book-<id>/<source>/<year>/` | every fetched PDF (mode 600) |
| `/opt/gnubook/data/books/<id>.sqlite` | documents (`postbox_docs`) and runs (`postbox_runs`) of the book |
| `/opt/gnubook/data/system.sqlite` | login and settings of the postbox (`postbox_sources`, password encrypted) |
| `/opt/gnubook/data/postbox/requests/` | run requests from the web interface |
| `/opt/gnubook/data/postbox/debug/` | screenshots and HTML of failed runs, `api-paths.txt` |
| `/opt/gnubook/data/postbox/home/` | HOME of the browser (caches) |
| `/opt/gnubook/pw-browsers/` | the browser build of Playwright |
| `gnubook-postbox.timer`, `gnubook-postbox.service` | scheduled runs (`journalctl -u gnubook-postbox`) |
| `gnubook-postbox-request.path`, `gnubook-postbox-request.service` | runs requested in the web interface |

`config.toml`:

```toml
[postbox]
enabled = true
browser = "firefox"                       # or "chromium"
browsers_path = "/opt/gnubook/pw-browsers"
timeout = 45                              # seconds per step
max_clicks = 0                            # "Mehr anzeigen" per run (10 documents each); 0 = no limit
download_pause_ms = 400                   # pause between two downloads
debug_keep = 10                           # failed runs kept with screenshot and HTML
```

## Limitations

- A first run over a large postbox takes long: about 1–2 s per *Mehr anzeigen* (10 documents) and about 1 s per
  download – for 14,000 documents roughly 4–8 hours (estimate, not measured). Documents are stored as they arrive;
  a run that is stopped (12 hours at most) keeps them, and the next run lists the postbox again and fetches the
  rest.

- Only finanzen.net zero, and only its *Eingang* (inbox). Documents that are archived before a run fetched them
  are not seen; neither is the *MiFID* tab (cost information).
- The login form was recognised generically (see Troubleshooting) and has not been tested against the live site
  while this was written; the postbox list follows the markup of the live site.
- One postbox per book.
