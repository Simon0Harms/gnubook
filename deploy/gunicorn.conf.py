# gunicorn settings for gnubook (copied to /opt/gnubook/gunicorn.conf.py by install.sh – edit there)
bind = "0.0.0.0:8080"
# Keep ONE worker: writes to the GnuCash book are serialised inside the process.
workers = 1
threads = 4
timeout = 120
accesslog = "-"
errorlog = "-"
# gunicorn >= 26 opens a control socket in $HOME, which is read-only for the service; older versions ignore this
control_socket_disable = True
# when gnubook runs behind a reverse proxy on another host, add its address here and set
# behind_proxy = true in config.toml
forwarded_allow_ips = "127.0.0.1"
