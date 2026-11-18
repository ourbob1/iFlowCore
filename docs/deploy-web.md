# Deploying the iFlow Core web app to iflowcore.com

The web app is **static files**: the whole app (Python via WebAssembly) runs in each visitor's
browser, and the server only serves files. No PHP, Python or MariaDB is used by the app.

Each user's data lives in their own browser, encrypted with their passphrase (AES-256-GCM in the
browser's private storage). It is never sent to the server.

The setup this was checked against:
- `https://iflowcore.com` goes through Cloudflare to Apache 2.4 on Ubuntu (216.250.119.42), in
  `/var/www/iflowcore.com`.
- The current `index.php` is the "Under Maintenance" placeholder. It gets backed up, not deleted.

## 1. Build (on the development machine)

```bash
cd /media/casper/extra/Projects/iFlowCore
venv/bin/python scripts/package_web.py
```

This produces `dist/iflowcore-web-<version>.tar.gz` and a manifest with its SHA-256 checksum.

## 2. Upload (on the development machine)

```bash
scp /media/casper/extra/Projects/iFlowCore/dist/iflowcore-web-0.10.0.tar.gz jasper@216.250.119.42:/tmp/
```

## 3. Install (in your SSH session on the server)

```bash
sha256sum /tmp/iflowcore-web-0.10.0.tar.gz        # compare with the manifest from step 1

# Back up the whole current site, then move the placeholder page out of the web root.
sudo mkdir -p /var/backups/iflowcore
sudo tar czf /var/backups/iflowcore/site-$(date +%Y%m%d-%H%M%S).tar.gz -C /var/www iflowcore.com
sudo mv /var/www/iflowcore.com/index.php /var/backups/iflowcore/index.php.$(date +%Y%m%d-%H%M%S)

# Unpack the app and make it readable by Apache.
sudo tar xzf /tmp/iflowcore-web-0.10.0.tar.gz -C /var/www/iflowcore.com
sudo find /var/www/iflowcore.com -type d -exec chmod 755 {} +
sudo find /var/www/iflowcore.com -type f -exec chmod 644 {} +

# The bundled .htaccess sets MIME types for .wasm/.mjs, caching and security headers.
sudo a2enmod headers mime deflate
sudo apache2ctl -t && sudo systemctl reload apache2

# Check that Apache honours .htaccess in this folder (look for AllowOverride All or FileInfo).
grep -Rn "AllowOverride" /etc/apache2/sites-enabled/ /etc/apache2/apache2.conf
```

If `AllowOverride` is `None` for `/var/www/iflowcore.com`, change it to `All` in that site's config,
then reload Apache. Otherwise `.wasm` files may be served with the wrong type and the app won't start.

## 4. Cloudflare

- **Caching → Purge everything** after each deploy, so browsers get the new `app.tar.gz`.
- **Speed → Rocket Loader: off.** It rewrites scripts and breaks the app's module scripts.
- **SSL/TLS → Always Use HTTPS: on.** Plain `http://iflowcore.com` currently returns 404. The app
  needs HTTPS for its encrypted browser storage.

## 5. Check

Open https://iflowcore.com. The first load downloads about 25 MB plus the Python runtime (10–20 s);
later visits come from the browser cache. You should see the iFlow Core logo and "Create a passphrase
to encrypt your data in this browser".

From your laptop:

```bash
curl -sI https://iflowcore.com/version.json | head -3
curl -sI https://iflowcore.com/main.dart.wasm | grep -i content-type    # expect application/wasm
```

## Roll back

```bash
sudo rm -rf /var/www/iflowcore.com
sudo tar xzf /var/backups/iflowcore/site-<timestamp>.tar.gz -C /var/www
```

Then purge the Cloudflare cache.

## Updating later

Repeat steps 1–4. Users keep their data: it lives in their browser's storage for iflowcore.com, not
in the uploaded files.

## What users should know (web)

- **Each browser is separate.** Data saved in one browser isn't in another until sync is enabled
  (account/sync API deployment, Phase 10.4).
- **Clearing "site data" for iflowcore.com erases it.** Use Finances → Export for a copy. There is
  no way to recover a forgotten passphrase for browser data.
- **One tab at a time.** A second tab shows "already open in another tab" instead of risking the data.
- **Not available on the web yet:**
  - Automatic trip detection (phone app only).
  - Google sign-in (with the account screens, 10.5).
  - Receipt images: their details are saved but the images aren't kept after reload.
- **Third-party requests.** The Python runtime (Pyodide, jsDelivr), the graphics engine (Google's
  CDN) and Python packages (PyPI) are downloaded by the browser. No user data is sent. Self-hosting
  them is a later hardening step.
