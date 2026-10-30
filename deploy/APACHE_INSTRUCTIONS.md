# Deploying the MRP planner next to westridgeApp

This app is served by the same Apache as westridgeApp. It runs in its own mod_wsgi daemon process with its own venv, database and cookies, and is mounted at **http://10.1.1.19/mrp/**. westridgeApp keeps `/`.

These steps change system files, so you run them yourself. Everything inside `/home/bc/westridgeApp_mrp` can be prepared first.

## 1. Prepare the app (no root needed)

```bash
cd /home/bc/westridgeApp_mrp
cp .env.example .env && chmod 600 .env
# Edit .env: SECRET_KEY, DATABASE_URL, STATIC_URL=/mrp/static/, COOKIE_PATH=/mrp,
# ALLOWED_HOSTS=10.1.1.19, CSRF_TRUSTED_ORIGINS=http://10.1.1.19, email settings, etc.
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(50))"   # use this for SECRET_KEY

.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --no-input
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py seed_demo          # until Acumatica is connected
```

## 2. Create the database (once, as postgres)

```bash
sudo -u postgres psql -v mrp_password='choose-a-strong-password' -f /home/bc/westridgeApp_mrp/deploy/create_db.sql
```

Put the same password in `DATABASE_URL` in `.env`. This creates a new role `mrp_user` and database `mrp`. It doesn't touch `mydb` or the `lucas` role.

## 3. Apache

1. Back up the current vhost:
   `sudo cp /etc/apache2/sites-enabled/000-default.conf /root/000-default.conf.bak-$(date +%F)`
2. Open `/etc/apache2/sites-enabled/000-default.conf`. Paste the contents of `deploy/apache-mrp.conf` inside `<VirtualHost *:80>`, **above** the existing `WSGIDaemonProcess westridgeApp ...` line.
3. Test the config:
   `sudo apachectl configtest` should print `Syntax OK`.
4. Reload (this is graceful and doesn't drop westridgeApp requests):
   `sudo systemctl reload apache2`
5. Check both apps:
   - `curl -sI http://10.1.1.19/` should be westridgeApp, as before.
   - `curl -sI http://10.1.1.19/mrp/` should return `302` to `/mrp/login/?next=/mrp/`.
   - `curl -sI http://10.1.1.19/mrp/static/mrp/app.css` should return `200`.
   - Errors go to the shared `/var/log/apache2/error.log`. Lines from this app show the process name `(wsgi:mrpApp)`.

**Rollback:** delete the pasted block (or restore the backup), then `sudo apachectl configtest && sudo systemctl reload apache2`.

## 4. Updating the app later

```bash
cd /home/bc/westridgeApp_mrp
git pull                                   # or copy the new code
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --no-input
touch config/wsgi.py                       # restarts only the mrpApp daemon process
```

Touching `config/wsgi.py` reloads only the `mrpApp` daemon, not westridgeApp. Daemon mode does this automatically when the WSGI script file changes.

## 5. Cron

See `deploy/cron.example`. Add those lines with `crontab -e` as user `bc`.

## Notes

- mod_wsgi on this server is built for Python 3.10, which is why the venv uses `/usr/bin/python3` (3.10.12).
- The site is HTTP only. When HTTPS is added, set `SECURE_COOKIES=True` and change `CSRF_TRUSTED_ORIGINS` to `https://...`.
- Session and CSRF cookies are named `mrp_sessionid` / `mrp_csrftoken` with path `/mrp`, so logging in here does not affect westridgeApp sessions.
