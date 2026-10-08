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
nano /opt/gnubook/config.toml
#   [book] url = "postgresql://gnucash:PASSWORD@192.168.1.30:5432/gnucash"
#   [app]  username = "admin"
gnubook hash-password            # paste the output as password_hash
gnubook check                    # must report "Schema unterstützt" and no errors
systemctl restart gnubook
```

Open `http://<gnubook-ip>:8080` and log in.

### Optional: HTTPS via reverse proxy

Behind a reverse proxy such as Nginx Proxy Manager or Caddy:

- in `config.toml`: `behind_proxy = true` and `session_cookie_secure = true`;
- in `/opt/gnubook/gunicorn.conf.py`: `forwarded_allow_ips = "<proxy-ip>"`;
- then `systemctl restart gnubook`.

Do not expose gnubook to the internet without HTTPS. It has a login, but it is your complete bookkeeping.

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
