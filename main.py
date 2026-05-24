"""
main.py — MyPertamina Scraper Service untuk Railway.com
Repo: lahankosong/scrape-transaksi

ARSITEKTUR:
- Login via Playwright (iPhone UA) → dapat Bearer token
- Fetch stok + transaksi via aiohttp DENGAN cookie session browser
  (bukan langsung dari IP Railway yang diblokir)
- Kirim ke Laravel POST /api/github-actions/transactions per pangkalan

FORMAT DATA ke Laravel (sinkron dengan GithubActionsController.php):
  POST /api/github-actions/transactions
  {
    "pangkalan_id": "uuid",
    "registration_id": "uuid",
    "label": "Nama Pangkalan",
    "date_from": "2026-05-01",
    "date_to": "2026-05-24",
    "transactions": [
      {
        "customerReportId": "xxx",
        "nationalityId": "330xxx",
        "name": "NAMA",
        "categories": ["Rumah Tangga"],   ← array, Laravel yang handle singular
        "total": 1,
        "createdAt": "2026-05-12T15:28:39+07:00"
      }
    ]
  }

  POST /api/github-actions/tokens (untuk stok)
  {
    "tokens": [{
      "email": "...",
      "token": "...",
      "pangkalan_id": "uuid",
      "store_name": "...",
      "stock_available": 10,
      "stock_redeem": 5,
      "sold": 3,
      "stock_date": "...",
      "stock_data": {"stockAvailable":10, "stockRedeem":5, "sold":3}
    }],
    "scrape_after": false,
    "date_from": "...",
    "date_to": "..."
  }
"""

import asyncio
import aiohttp
import json
import os
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="MyPertamina Scraper — Railway")

API_KEY = os.environ.get("LARAVEL_API_KEY", "")


# ── Status tracker ────────────────────────────────────────────────────────────

class ScrapeStatus:
    running     = False
    last_run    = None
    last_result = None


# ── Models ────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    date_from: Optional[str] = ""
    date_to:   Optional[str] = ""


# ── Helper ────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ── Core: Login + ambil token + cookie ───────────────────────────────────────

async def login_one(email: str, pin: str, label: str) -> dict:
    """
    Login ke MyPertamina via Playwright.
    Kembalikan token + cookies untuk dipakai fetch API.
    """
    result = {
        "success":      False,
        "label":        label,
        "email":        email,
        "token":        None,
        "pangkalan_id": None,
        "store_name":   None,
        "cookies":      [],
        "error":        None,
    }

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-extensions",
                "--window-size=390,844",
            ]
        )

        context = await browser.new_context(
            # iPhone UA — terbukti berhasil login di localhost
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.5 Mobile/15E148 Safari/604.1"
            ),
            viewport={"width": 390, "height": 844},
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            extra_http_headers={
                "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['id-ID','id','en-US','en'] });
            window.chrome = { runtime: {} };
            delete window.__playwright;
        """)

        page         = await context.new_page()
        token_holder = {"token": None}

        def handle_request(request):
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ") and not token_holder["token"]:
                token_holder["token"] = auth.replace("Bearer ", "").strip()
                log(f"  [{label}] ✓ Token captured")

        page.on("request", handle_request)

        try:
            log(f"  [{label}] Membuka halaman login...")
            await page.goto(
                "https://subsiditepatlpg.mypertamina.id/merchant-login",
                wait_until="domcontentloaded",
                timeout=60000
            )
            await asyncio.sleep(2)

            # Isi form dengan JS evaluate (React-safe, lebih reliable)
            log(f"  [{label}] Input email...")
            await page.evaluate("""(val) => {
                const sels = ['input[type="text"]','input[type="email"]',
                              'input[placeholder*="Ponsel"]','input[placeholder*="Email"]'];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        return;
                    }
                }
            }""", email)
            await asyncio.sleep(0.3)

            log(f"  [{label}] Input PIN...")
            await page.evaluate("""(val) => {
                const el = document.querySelector('input[type="password"]');
                if (el) {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
            }""", pin)
            await asyncio.sleep(0.3)

            log(f"  [{label}] Klik login...")
            btn_text = await page.evaluate("""() => {
                const keywords = ['MASUK','Masuk','Login','LOGIN'];
                for (const b of document.querySelectorAll('button')) {
                    if (keywords.some(k => b.textContent.includes(k)) && b.offsetParent !== null) {
                        b.click(); return b.textContent.trim();
                    }
                }
                const sub = document.querySelector('button[type="submit"]');
                if (sub) { sub.click(); return 'submit'; }
                return null;
            }""")
            log(f"  [{label}] Tombol diklik: {btn_text}")

            # Tunggu token max 30 detik
            for _ in range(30):
                if token_holder["token"]:
                    break
                # Cek redirect sudah ke dashboard
                if "merchant/app" in page.url or "dashboard" in page.url:
                    # Coba ambil dari localStorage
                    try:
                        tok = await page.evaluate("localStorage.getItem('token')")
                        if tok:
                            token_holder["token"] = tok
                            log(f"  [{label}] Token dari localStorage")
                            break
                    except:
                        pass
                await asyncio.sleep(1)

            if not token_holder["token"]:
                result["error"] = "Token tidak tertangkap — cek kredensial atau halaman tidak load"
                log(f"  [{label}] ✗ {result['error']}")
                log(f"  [{label}] URL akhir: {page.url}")
                return result

            log(f"  [{label}] URL setelah login: {page.url}")

            # Ambil semua cookies dari browser
            cookies = await context.cookies()
            result["cookies"] = [
                {"name": c["name"], "value": c["value"], "domain": c["domain"]}
                for c in cookies
            ]
            result["token"] = token_holder["token"]

            # Decode pangkalan_id dari JWT
            try:
                parts   = token_holder["token"].split(".")
                payload = json.loads(base64.b64decode(
                    parts[1] + "=" * (4 - len(parts[1]) % 4)
                ))
                result["pangkalan_id"] = payload.get("sub")
            except Exception:
                pass

            result["success"] = True
            log(f"  [{label}] ✓ Login berhasil, pangkalan_id={result['pangkalan_id']}")

        except Exception as e:
            result["error"] = f"Browser error: {str(e)}"
            log(f"  [{label}] ✗ {result['error'][:100]}")
        finally:
            await browser.close()

    return result


# ── Core: Fetch stok via aiohttp + session cookie ─────────────────────────────

async def fetch_stok(token: str, cookies: list, label: str) -> dict:
    """Fetch data stok menggunakan token + cookie dari browser session."""
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "User-Agent":    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15",
        "Origin":        "https://subsiditepatlpg.mypertamina.id",
        "Referer":       "https://subsiditepatlpg.mypertamina.id/",
        "Cookie":        cookie_str,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api-map.my-pertamina.id/general/products/v1/products/user",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as res:
                log(f"  [{label}] Stok HTTP: {res.status}")
                if res.status == 200:
                    data = await res.json()
                    if data.get("success") and data.get("data"):
                        d = data["data"]
                        log(f"  [{label}] ✓ Stok: available={d.get('stockAvailable')} sold={d.get('sold')}")
                        return {
                            "success":         True,
                            "store_name":      d.get("storeName"),
                            "registration_id": d.get("registrationId"),
                            "stock_available": d.get("stockAvailable"),
                            "stock_redeem":    d.get("stockRedeem"),
                            "sold":            d.get("sold"),
                            "stock_date":      d.get("stockDate"),
                            "last_stock":      d.get("lastStock"),
                            "last_stock_date": d.get("lastStockDate"),
                        }
                    log(f"  [{label}] ⚠ Stok response tidak sukses: {data.get('message','?')}")
                else:
                    body = await res.text()
                    log(f"  [{label}] ⚠ Stok gagal {res.status}: {body[:80]}")
    except Exception as e:
        log(f"  [{label}] ⚠ Stok error: {str(e)[:80]}")

    return {"success": False}


# ── Core: Fetch transaksi via aiohttp + session cookie ────────────────────────

async def fetch_transactions(token: str, cookies: list, start_date: str, end_date: str, label: str) -> dict:
    """
    Fetch transaksi dengan cookie dari browser session.
    Cookie penting untuk bypass blokir IP datacenter.
    """
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "User-Agent":    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15",
        "Origin":        "https://subsiditepatlpg.mypertamina.id",
        "Referer":       "https://subsiditepatlpg.mypertamina.id/",
        "Cookie":        cookie_str,
    }

    all_customers = []
    summary       = None

    start   = datetime.strptime(start_date, "%Y-%m-%d")
    end     = datetime.strptime(end_date,   "%Y-%m-%d")
    current = start

    async with aiohttp.ClientSession() as session:
        while current <= end:
            batch_end = min(current + timedelta(days=6), end)
            s = current.strftime("%Y-%m-%d")
            e = batch_end.strftime("%Y-%m-%d")

            for attempt in range(3):
                try:
                    async with session.get(
                        "https://api-map.my-pertamina.id/general/v3/transactions/report",
                        headers=headers,
                        params={
                            "search":    "",
                            "sort":      "latest",
                            "startDate": s,
                            "endDate":   e,
                        },
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as res:
                        log(f"  [{label}] Batch {s}~{e} HTTP: {res.status}")
                        if res.status == 200:
                            body = await res.json()
                            if body.get("success"):
                                customers = body["data"].get("customersReport", [])
                                all_customers.extend(customers)
                                if body["data"].get("summaryReport"):
                                    summary = body["data"]["summaryReport"]
                                    summary["date"] = e
                                log(f"  [{label}] Batch {s}~{e}: {len(customers)} transaksi")
                                break
                            else:
                                log(f"  [{label}] Batch {s}~{e} API error: {body.get('message','?')}")
                                break
                        else:
                            body_text = await res.text()
                            log(f"  [{label}] Batch {s}~{e} HTTP {res.status}: {body_text[:60]}")
                            if attempt < 2:
                                await asyncio.sleep(3)
                except Exception as e_err:
                    log(f"  [{label}] Batch {s}~{e} error: {str(e_err)[:60]}, attempt {attempt+1}")
                    if attempt < 2:
                        await asyncio.sleep(2)

            await asyncio.sleep(1)
            current = batch_end + timedelta(days=1)

    log(f"  [{label}] ✓ Total: {len(all_customers)} transaksi")
    return {"success": True, "customers": all_customers, "summary": summary}


# ── Core: Kirim ke Laravel ────────────────────────────────────────────────────

async def send_transactions_to_laravel(
    pangkalan_id: str, registration_id: str, label: str,
    transactions: list, date_from: str, date_to: str,
    api_url: str, api_key: str
) -> bool:
    """
    Kirim transaksi ke POST /api/github-actions/transactions
    Format sinkron dengan GithubActionsController::receiveTransactions()
    """
    payload = {
        "pangkalan_id":    pangkalan_id,
        "registration_id": registration_id or pangkalan_id,
        "label":           label,
        "date_from":       date_from,
        "date_to":         date_to,
        "transactions":    transactions,   # array of customerReportId, nationalityId, etc.
    }

    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.post(
                    f"{api_url}/api/github-actions/transactions",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key":    api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    result = await resp.json()
                    if resp.status == 200 and result.get("success"):
                        log(f"  [{label}] ✓ Transaksi tersimpan: {result.get('saved',0)} baru, {result.get('skipped',0)} skip")
                        return True
                    else:
                        log(f"  [{label}] ✗ Laravel error ({resp.status}): {result.get('message','?')}")
                        if result.get('errors'):
                            for err in result['errors'][:2]:
                                log(f"    → {err}")
            except Exception as e:
                log(f"  [{label}] ✗ Kirim transaksi gagal attempt {attempt+1}: {str(e)[:60]}")
                if attempt < 2:
                    await asyncio.sleep(3)

    return False


async def send_stok_to_laravel(
    tokens_data: list, date_from: str, date_to: str,
    api_url: str, api_key: str
) -> bool:
    """
    Kirim data stok ke POST /api/github-actions/tokens
    Format sinkron dengan GithubActionsController::receiveTokens()
    """
    payload = {
        "tokens":       tokens_data,
        "scrape_after": False,   # jangan trigger scrape lagi
        "date_from":    date_from,
        "date_to":      date_to,
    }

    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.post(
                    f"{api_url}/api/github-actions/tokens",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key":    api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    result = await resp.json()
                    if resp.status == 200:
                        log(f"✓ Stok tersimpan: {result.get('saved',0)} token")
                        return True
                    else:
                        log(f"✗ Stok Laravel error ({resp.status}): {result.get('message','?')}")
            except Exception as e:
                log(f"✗ Kirim stok gagal attempt {attempt+1}: {str(e)[:60]}")
                if attempt < 2:
                    await asyncio.sleep(3)

    return False


# ── Core: Run semua akun ──────────────────────────────────────────────────────

async def run_scrape(date_from: str = "", date_to: str = ""):
    api_url = os.environ.get("LARAVEL_API_URL", "").rstrip("/")
    api_key = os.environ.get("LARAVEL_API_KEY", "")

    if not api_url or not api_key:
        log("ERROR: LARAVEL_API_URL dan LARAVEL_API_KEY belum diset!")
        ScrapeStatus.running     = False
        ScrapeStatus.last_result = {"success": False, "error": "Env vars tidak lengkap"}
        return

    if not date_from: date_from = today_str()
    if not date_to:   date_to   = today_str()

    log("=" * 55)
    log(f"Scrape dimulai | {date_from} s/d {date_to}")
    log("=" * 55)

    # Fetch akun dari Laravel
    accounts = []
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{api_url}/api/github-actions/accounts",
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if body.get("success"):
                accounts = body["accounts"]
                log(f"✓ {len(accounts)} akun dari Laravel")
            else:
                log(f"✗ API error: {body.get('message','?')}")
    except Exception as e:
        log(f"✗ Gagal fetch akun: {e}")

    if not accounts:
        log("ERROR: Tidak ada akun!")
        ScrapeStatus.running     = False
        ScrapeStatus.last_result = {"success": False, "error": "Tidak ada akun"}
        return

    log(f"Memproses {len(accounts)} akun...")
    log("-" * 55)

    berhasil   = 0
    gagal      = 0
    stok_batch = []   # kumpulkan semua stok untuk kirim sekaligus

    for i, acc in enumerate(accounts, 1):
        email = acc.get("email", "")
        pin   = acc.get("pin",   "")
        label = acc.get("label", "") or acc.get("name", "") or email[:20]

        log(f"[{i}/{len(accounts)}] {label}")

        if not email or not pin:
            log(f"  Skipped: kredensial kosong")
            gagal += 1
            continue

        # Step 1: Login + dapat token + cookie
        login = await login_one(email, pin, label)

        if not login["success"]:
            log(f"  ✗ Login gagal: {login.get('error','?')[:80]}")
            gagal += 1
            if i < len(accounts):
                await asyncio.sleep(3)
            continue

        token   = login["token"]
        cookies = login["cookies"]
        pang_id = login["pangkalan_id"]

        # Step 2: Fetch stok
        stok = await fetch_stok(token, cookies, label)

        store_name      = stok.get("store_name") or label
        registration_id = stok.get("registration_id") or pang_id

        # Kumpulkan untuk kirim batch ke /api/github-actions/tokens
        stok_batch.append({
            "email":           email,
            "token":           token,
            "pangkalan_id":    pang_id,
            "store_name":      store_name,
            "stock_available": stok.get("stock_available"),
            "stock_redeem":    stok.get("stock_redeem"),
            "sold":            stok.get("sold"),
            "stock_date":      stok.get("stock_date"),
            "last_stock":      stok.get("last_stock"),
            "last_stock_date": stok.get("last_stock_date"),
            "stock_data": {
                "stockAvailable": stok.get("stock_available"),
                "stockRedeem":    stok.get("stock_redeem"),
                "sold":           stok.get("sold"),
            },
        })

        # Step 3: Fetch transaksi
        tx = await fetch_transactions(token, cookies, date_from, date_to, label)

        customers = tx.get("customers", [])
        log(f"  Total transaksi: {len(customers)}")

        # Step 4: Kirim transaksi ke Laravel (per pangkalan)
        if customers:
            ok = await send_transactions_to_laravel(
                pangkalan_id    = pang_id,
                registration_id = registration_id,
                label           = store_name,
                transactions    = customers,
                date_from       = date_from,
                date_to         = date_to,
                api_url         = api_url,
                api_key         = api_key,
            )
            if ok:
                berhasil += 1
            else:
                gagal += 1
        else:
            log(f"  ⚠ Tidak ada transaksi di periode ini")
            berhasil += 1  # login berhasil, transaksi memang kosong

        if i < len(accounts):
            await asyncio.sleep(3)

    # Step 5: Kirim semua stok sekaligus
    log("-" * 55)
    log(f"Selesai login: {berhasil} berhasil, {gagal} gagal")

    if stok_batch:
        log(f"Mengirim stok {len(stok_batch)} pangkalan ke Laravel...")
        await send_stok_to_laravel(stok_batch, date_from, date_to, api_url, api_key)

    ScrapeStatus.running     = False
    ScrapeStatus.last_run    = datetime.now().isoformat()
    ScrapeStatus.last_result = {
        "success":   gagal == 0,
        "total":     len(accounts),
        "berhasil":  berhasil,
        "gagal":     gagal,
        "date_from": date_from,
        "date_to":   date_to,
        "timestamp": datetime.now().isoformat(),
    }
    log("=" * 55)
    log("DONE!")


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service":  "MyPertamina Scraper v4",
        "status":   "running" if ScrapeStatus.running else "idle",
        "last_run": ScrapeStatus.last_run,
    }

@app.get("/health")
def health():
    """Health check — dipanggil cPanel cron untuk wake up service"""
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.post("/scrape")
async def scrape(
    req:              ScrapeRequest,
    background_tasks: BackgroundTasks,
    x_api_key:        str = Header(default=""),
):
    """
    Trigger scrape — dipanggil dari:
    1. Tombol manual di Laravel
    2. cPanel cron Rumahweb jam 00:05 WIB (17:05 UTC)
    """
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    if ScrapeStatus.running:
        return {"success": False, "message": "Scrape sedang berjalan, tunggu selesai"}

    ScrapeStatus.running = True

    date_from = req.date_from or today_str()
    date_to   = req.date_to   or today_str()

    background_tasks.add_task(run_scrape, date_from, date_to)

    return {
        "success":   True,
        "message":   "Scrape dimulai",
        "date_from": date_from,
        "date_to":   date_to,
    }

@app.get("/status")
def status(x_api_key: str = Header(default="")):
    """Cek status dan hasil scrape terakhir"""
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    return {
        "running":     ScrapeStatus.running,
        "last_run":    ScrapeStatus.last_run,
        "last_result": ScrapeStatus.last_result,
    }
