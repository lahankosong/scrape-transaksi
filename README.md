# MyPertamina Scraper — Render.com + Rumahweb

Menggantikan GitHub Actions sepenuhnya.
Dua mode trigger: otomatis (cPanel cron) + manual (tombol di Laravel).

---

## Struktur File

```
render-scraper/
├── main.py                 → FastAPI service (deploy ke Render)
├── requirements.txt        → Python dependencies
├── build.sh                → Install Playwright + Chromium
├── render.yaml             → Konfigurasi deploy Render
├── laravel_integration.php → Tambahkan ke controller Laravel
├── cron_trigger.php        → Upload ke Rumahweb, pasang di cPanel cron
└── README.md
```

---

## STEP 1 — Deploy ke Render.com

### A. Push ke GitHub
Buat repository baru (boleh private), push semua file di folder ini.

### B. Di dashboard Render
1. New → **Web Service**
2. Connect repository GitHub
3. Render otomatis detect `render.yaml`
4. Set **Environment Variables**:
   - `LARAVEL_API_URL` → `https://namadomain.com` (URL Laravel kamu)
   - `LARAVEL_API_KEY` → isi sama persis dengan `GITHUB_ACTIONS_API_KEY` di `.env` Laravel
5. Klik **Deploy**
6. Tunggu build selesai (~5-10 menit, karena install Chromium)
7. Catat URL service → contoh: `https://mypertamina-scraper.onrender.com`

---

## STEP 2 — Update Laravel

### A. Tambah di `.env`
```
RENDER_SCRAPER_URL=https://mypertamina-scraper.onrender.com
```

### B. Tambah di `config/services.php`
```php
'render' => [
    'url' => env('RENDER_SCRAPER_URL'),
],
```

### C. Copy method dari `laravel_integration.php`
Tambahkan method `triggerScrape()` dan `scrapeStatus()` ke controller kamu.

### D. Tambah route di `routes/web.php`
```php
Route::post('/scrape/trigger', [GithubActionsController::class, 'triggerScrape'])->name('scrape.trigger');
Route::get('/scrape/status',   [GithubActionsController::class, 'scrapeStatus'])->name('scrape.status');
```

---

## STEP 3 — Setup Cron di cPanel Rumahweb

### A. Upload file cron
Upload `cron_trigger.php` ke: `/public_html/cron/cron_trigger.php`

Edit file tersebut, isi:
- `RENDER_SCRAPER_URL` → URL Render kamu
- `API_KEY` → sama dengan `LARAVEL_API_KEY`

### B. Setup di cPanel
Masuk cPanel Rumahweb → **Cron Jobs** → Add New Cron Job

```
Minute  : 5
Hour    : 17
Day     : *
Month   : *
Weekday : *
```
**= Setiap hari jam 00:05 WIB (17:05 UTC)**

Command (ganti USERNAME dengan username cPanel kamu):
```
/usr/local/bin/php /home/USERNAME/public_html/cron/cron_trigger.php
```

---

## Alur Lengkap

```
[Cron cPanel 00:05 WIB]          [Tombol Scrape di Laravel]
         |                                   |
         └──────────── POST /scrape ─────────┘
                              |
                    [Render.com - FastAPI]
                              |
                    Fetch akun dari Laravel
                    GET /api/github-actions/accounts
                              |
                    Login tiap akun ke MyPertamina
                    (Playwright + iPhone user-agent)
                              |
                    Fetch stock + transaksi
                              |
                    POST /api/github-actions/tokens
                              |
                    [Laravel - simpan ke database]
```

---

## Catatan Free Tier Render

- Service **tidur setelah 15 menit** tidak ada request
- Cron cPanel jam 00:05 WIB otomatis **membangunkan** Render
- Cold start ~30 detik, sudah ditangani dengan `/health` wake up di `cron_trigger.php`
- Untuk trigger manual dari tombol Laravel, `triggerScrape()` sudah include wake up `/health`

---

## Test Manual

```bash
# Cek service hidup
curl https://mypertamina-scraper.onrender.com/health

# Trigger scrape (ganti API_KEY)
curl -X POST https://mypertamina-scraper.onrender.com/scrape \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: API_KEY_KAMU" \
  -d '{"date_from":"2026-05-23","date_to":"2026-05-23"}'

# Cek status hasil
curl https://mypertamina-scraper.onrender.com/status \
  -H "X-Api-Key: API_KEY_KAMU"
```
