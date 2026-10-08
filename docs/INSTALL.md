# Installation on Proxmox (gnubook LXC + PostgreSQL LXC)

This guide assumes two Debian 12/13 containers:

| Container | Purpose | Important data |
|---|---|---|
| `pg` | PostgreSQL with the GnuCash book | nightly dumps in `/opt/pgbackup` |
| `gnubook` | gnubook web app (port 8080) | `/opt/gnubook` (config, data, backups) |

GnuCash Desktop on your PC opens the same database in `pg`. Keep everything important below `/opt`, which can
be the mirrored volume.

> **Before you start:** make a copy of your current GnuCash file. Moving the book into PostgreSQL happens in
> GnuCash Desktop (step 2). Your old file stays untouched.

## 1. PostgreSQL container

```bash
apt-get update && apt-get install -y postgresql
su - postgres -c "createuser --pwprompt gnucash"        # choose a strong password
su - postgres -c "createdb --owner gnucash --encoding UTF8 gnucash"
```

Let PostgreSQL listen on the network. Edit `/etc/postgresql/*/main/postgresql.conf`:

```
listen_addresses = '*'
```

Allow only the gnubook container and your desktop PC. Add these lines to `/etc/postgresql/*/main/pg_hba.conf`
and replace the addresses with your own:

```
host  gnucash  gnucash  192.168.1.31/32  scram-sha-256   # gnubook LXC
host  gnucash  gnucash  192.168.1.50/32  scram-sha-256   # desktop PC with GnuCash
```

Restart PostgreSQL:

```bash
systemctl restart postgresql
```

### Backups of the book

Run a nightly dump into the mirrored `/opt` and keep 30 days:

```bash
install -d -o postgres -g postgres -m 700 /opt/pgbackup
cat > /etc/cron.d/gnucash-backup <<'EOF'
15 3 * * * postgres pg_dump -Fc gnucash > /opt/pgbackup/gnucash-$(date +\%Y\%m\%d).dump && find /opt/pgbackup -name 'gnucash-*.dump' -mtime +30 -delete
EOF
```

To restore a dump, first close GnuCash Desktop and stop gnubook:

```bash
pg_restore --clean --if-exists -d gnucash /opt/pgbackup/gnucash-YYYYMMDD.dump
```

### Backups as .gnucash file (done by gnubook)

The installer also enables the timer `gnubook-backup.timer`. Every night at 03:30 it writes a complete copy of
the book to `/opt/gnubook/backup/book/gnucash-YYYYMMDD-HHMMSS.gnucash` and keeps the newest 30. The files are
SQLite books: GnuCash Desktop opens them directly with *Datei → Öffnen*, even without PostgreSQL.

- Run a backup now: `gnubook backup`
- Change the time: `systemctl edit gnubook-backup.timer` (`OnCalendar=`)
- Change how many are kept: `--keep` in `/etc/systemd/system/gnubook-backup.service`

The copy is taken in one read-only database transaction, so it is consistent even while someone is booking.

## 2. Move the book into PostgreSQL (GnuCash Desktop)

1. Open your current book in GnuCash Desktop.
2. Choose *Datei → Speichern unter …*.
   - *Datenformat*: `postgres`
   - *Host*: the IP of the `pg` container
   - *Datenbank*: `gnucash`
   - *Benutzername* and *Passwort*: the role you created
3. From now on open the book with *Datei → Öffnen* (format `postgres`) or from the recent-files list.
   Keep the old file as a backup.

GnuCash saves every change immediately in SQL mode, so there is no *Speichern* button anymore.

## 3. gnubook container

Run as root:

```bash
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/Simon0Harms/gnubook/main/deploy/install.sh \
  | GNUBOOK_REPO=https://github.com/Simon0Harms/gnubook.git bash
```

The installer does the following:

- installs Python, creates the system user `gnubook`, and clones the code to `/opt/gnubook/src`;
- creates the virtualenv `/opt/gnubook/venv`;
- writes `/opt/gnubook/config.toml` (with a random `secret_key`) and `/opt/gnubook/gunicorn.conf.py`;
- installs the systemd service `gnubook` and the commands `gnubook` and `gnubook-update`.

Then finish the configuration:

```bash
gnubook user-add simon --admin
gnubook book-add Hauptbuch "postgresql://gnucash:PASSWORD@192.168.1.30:5432/gnucash" --user simon
gnubook check                    # must report "Schema unterstützt" and no errors
systemctl restart gnubook
```

Open `http://<gnubook-ip>:8080` and log in.

## More users and books

### Let gnubook create the databases (recommended)

Give gnubook a PostgreSQL role that may create roles and databases but is **not** a superuser. In the `pg`
container:

```bash
su - postgres -c "createuser --createrole --createdb --pwprompt gnubook_admin"
```

Add one rule to `pg_hba.conf` that lets every role reach only the database of the same name. New books then
need no further change there:

```
host  sameuser  all  192.168.1.0/24  scram-sha-256
```

Add this to gnubook's `config.toml` and restart gnubook:

```toml
[postgres]
admin_url = "postgresql://gnubook_admin:PASSWORD@192.168.1.30:5432/postgres"
client_host = "192.168.1.30"
```

Now *Bücher → Neues Buch anlegen*, or *Benutzer → Neuer Benutzer* with *eigenes Buch anlegen*, does the
following:

- creates role and database `gnucash_<name>` with a random password;
- fills the database with one of:
  - an empty book (EUR);
  - a simple German chart of accounts;
  - the content of an uploaded GnuCash file in SQLite format. Convert XML files first in GnuCash with
    *Speichern unter → sqlite3*.
- connects the book and shows the credentials for GnuCash Desktop once.

On the command line: `gnubook book-create Anna --user anna`.

*Buch samt Datenbank löschen* drops the database and the role again, but only for books gnubook created.
You have to type the book's name to confirm, and gnubook writes a last `.gnucash` copy to
`data/backup/deleted/` first.

### Manually

gnubook can serve several users, each with their own GnuCash book. Users and books are related n:m: a
book can be shared (for example a household book), and a user with several books switches between them in
the header.

Give every book its own PostgreSQL role and database, so GnuCash Desktop of one person cannot open another
person's book. In the `pg` container:

```bash
su - postgres -c "createuser --pwprompt gnucash_anna"
su - postgres -c "createdb --owner gnucash_anna --encoding UTF8 gnucash_anna"
# pg_hba.conf: host gnucash_anna gnucash_anna <gnubook-ip>/32 scram-sha-256  (+ Anna's PC)
```

Fill the database:

- **An existing book.** Open Anna's GnuCash file in GnuCash Desktop, then use *Datei → Speichern unter →
  postgres* with database `gnucash_anna`.
- **A new book.** Create a new book in GnuCash Desktop and save it the same way.

Then, as administrator in gnubook:

1. *Bücher → Buch verbinden*. Enter the name and URL
   `postgresql://gnucash_anna:PW@192.168.1.30:5432/gnucash_anna`. gnubook checks the connection before it
   saves.
2. *Benutzer → Neuer Benutzer*. Create the user and tick the books they may use.

On the command line:

```bash
gnubook user-add anna
gnubook book-add Anna "postgresql://gnucash_anna:PW@192.168.1.30:5432/gnucash_anna" --user anna
```

Notes:

- Each user can change their own password via the user menu → *Passwort ändern*.
- gnubook's own data is kept separately for each book: import records, API ids, accepted differences, audit
  log and the `.gnucash` copy.
- The database URLs, including passwords, are stored in `/opt/gnubook/data/system.sqlite` with file mode
  600.

### Optional: HTTPS via reverse proxy

Behind a reverse proxy such as Nginx Proxy Manager or Caddy:

- in `config.toml`: `behind_proxy = true` and `session_cookie_secure = true`;
- in `/opt/gnubook/gunicorn.conf.py`: `forwarded_allow_ips = "<proxy-ip>"`;
- then `systemctl restart gnubook`.

Do not expose gnubook to the internet without HTTPS. It has a login, but it is your complete bookkeeping.

### `.gnucash` copy after every change

With `[backup] gnucash_file = "/opt/gnubook/data/backup/buch.gnucash"` (default in new configs) gnubook writes
a complete copy of the book a few seconds after every change (bookings, imports) and keeps `keep` older
versions (`buch.<timestamp>.gnucash`). Several changes in quick succession, such as an import run, produce one
copy. The file is in GnuCash's SQLite format: open it in GnuCash Desktop with *Datei → Öffnen*. To keep
working with it, use *Speichern unter* to a new name instead of editing the backup itself.
*Einstellungen* shows when the last copy was written. It is an additional copy, not a replacement for the
PostgreSQL dumps.

## 4. Updates

```bash
gnubook-update            # newest version of the installed branch
gnubook-update v0.2.0     # a specific tag
```

The update does the following:

- saves `config.toml`, `gunicorn.conf.py` and `data/` to `/opt/gnubook/backup/` (the last 10 are kept);
- installs the new version and restarts the service;
- returns to the previous version if gnubook does not come up again.

The GnuCash book itself is not touched by updates. Its backups are the PostgreSQL dumps.

## 5. Daily use with GnuCash Desktop

- gnubook writes only while GnuCash Desktop has the book closed. The badge in gnubook's header shows the
  state.
- Close GnuCash Desktop when you are done. After a crash a stale lock may remain. GnuCash offers
  *Trotzdem öffnen* (open anyway), and *Einstellungen* in gnubook can remove it.
- Bookings made in gnubook appear in GnuCash Desktop the next time you open the book.

## Files and services

| Path | Content |
|---|---|
| `/opt/gnubook/config.toml` | configuration (mode 640, readable by the service) |
| `/opt/gnubook/gunicorn.conf.py` | port and worker settings |
| `/opt/gnubook/data/gnubook.sqlite` | API ids, import records, accepted differences, audit log |
| `/opt/gnubook/backup/` | backups made by `gnubook-update` |
| `/etc/systemd/system/gnubook.service` | service (`journalctl -u gnubook` for logs) |
