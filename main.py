"""
main.py — MyPertamina Scraper Service
Deploy ke Render.com (Free Tier)

Dua mode trigger:
1. Terjadwal  → dipanggil cPanel cron Rumahweb jam 00:05 WIB
2. Manual     → dipanggil tombol scrape di Laravel
"""

import asyncio
import aiohttp
import json
import os
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="MyPertamina Scraper Service")

API_KEY = os.environ.get("LARAVEL_API_KEY", "")


# ── Status tracker in-memory ──────────────────────────────────────────────────

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
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def default_dates():
    """Default: hari ini"""
    today = datetime.now().strftime("%Y-%m-%d")
    return today, today


# ── Core: Login ───────────────────────────────────────────────────────────────

async def login_one(email: str, pin: str, label: str = "") -> dict:
    result = {
        "success":         False,
        "label":           label,
        "email":           email,
        "token":           None,
        "pangkalan_id":    None,
        "store_name":      None,
        "stock_available": None,
        "stock_redeem":    None,
        "sold":            None,
        "stock_date":      None,
        "last_stock":      None,
        "last_stock_date": None,
        "error":           None,
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
            # Pakai iPhone user-agent — terbukti work di localhost
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
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
                log(f"  [{label}] Token captured")

        page.on("request", handle_request)

        try:
            log(f"  [{label}] Membuka halaman login...")
            await page.goto(
                "https://subsiditepatlpg.mypertamina.id/merchant-login",
                wait_until="domcontentloaded",
                timeout=60000
            )
            await asyncio.sleep(2)

            log(f"  [{label}] Input email...")
            await page.locator("input").nth(0).fill(email)
            await asyncio.sleep(0.3)

            log(f"  [{label}] Input PIN...")
            await page.locator("input").nth(1).fill(pin)
            await asyncio.sleep(0.3)

            log(f"  [{label}] Klik login...")
            await page.locator("button").filter(has_text="MASUK").click()

            # Tunggu token max 30 detik
            for _ in range(30):
                if token_holder["token"]:
                    break
                await asyncio.sleep(1)

            if not token_holder["token"]:
                result["error"] = "Token tidak tertangkap — cek kredensial"
                log(f"  [{label}] ✗ {result['error']}")
                return result

            # Decode pangkalan_id dari JWT
            try:
                parts   = token_holder["token"].split(".")
                payload = json.loads(base64.b64decode(
                    parts[1] + "=" * (4 - len(parts[1]) % 4)
                ))
                result["pangkalan_id"] = payload.get("sub")
            except Exception:
                pass

            result["token"]   = token_holder["token"]
            result["success"] = True
            log(f"  [{label}] ✓ Login berhasil")

        except Exception as e:
            result["error"] = f"Browser error: {str(e)}"
            log(f"  [{label}] ✗ {result['error']}")
        finally:
            await browser.close()

    # Ambil info stok
    if result["token"]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api-map.my-pertamina.id/general/products/v1/products/user",
                    headers={
                        "Authorization": f"Bearer {result['token']}",
                        "Accept":        "application/json",
                        "User-Agent":    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X)",
                        "Origin":        "https://subsiditepatlpg.mypertamina.id",
                        "Referer":       "https://subsiditepatlpg.mypertamina.id/",
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as res:
                    if res.status == 200:
                        data = await res.json()
                        if data.get("success") and data.get("data"):
                            d = data["data"]
                            result.update({
                                "store_name":      d.get("storeName"),
                                "stock_available": d.get("stockAvailable"),
                                "stock_redeem":    d.get("stockRedeem"),
                                "sold":            d.get("sold"),
                                "stock_date":      d.get("stockDate"),
                                "last_stock":      d.get("lastStock"),
                                "last_stock_date": d.get("lastStockDate"),
                            })
                            if not result["label"]:
                                result["label"] = d.get("storeName", email)
                            log(f"  [{label}] ✓ Stok: available={d.get('stockAvailable')} redeem={d.get('stockRedeem')} sold={d.get('sold')}")
        except Exception as e:
            log(f"  [{label}] ⚠ Stok gagal: {str(e)}")

    return result


# ── Core: Fetch Transaksi ─────────────────────────────────────────────────────

async def fetch_transactions(token: str, start_date: str, end_date: str, label: str = "") -> dict:
    all_customers = []
    summary       = None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "User-Agent":    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X)",
        "Origin":        "https://subsiditepatlpg.mypertamina.id",
        "Referer":       "https://subsiditepatlpg.mypertamina.id/",
    }

    start   = datetime.strptime(start_date, "%Y-%m-%d")
    end     = datetime.strptime(end_date,   "%Y-%m-%d")
    current = start

    async with aiohttp.ClientSession() as session:
        while current <= end:
            batch_end = min(current + timedelta(days=6), end)

            for attempt in range(3):
                try:
                    async with session.get(
                        "https://api-map.my-pertamina.id/general/v3/transactions/report",
                        headers=headers,
                        params={
                            "search":    "",
                            "sort":      "latest",
                            "startDate": current.strftime("%Y-%m-%d"),
                            "endDate":   batch_end.strftime("%Y-%m-%d"),
                        },
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as res:
                        if res.status == 200:
                            body = await res.json()
                            if body.get("success"):
                                customers = body["data"].get("customersReport", [])
                                all_customers.extend(customers)
                                if body["data"].get("summaryReport"):
                                    summary = body["data"]["summaryReport"]
                                    summary["date"] = batch_end.strftime("%Y-%m-%d")
                                break
                        else:
                            log(f"  [{label}] Transaksi HTTP {res.status}, attempt {attempt+1}")
                except Exception as e:
                    log(f"  [{label}] Transaksi error: {str(e)[:60]}, attempt {attempt+1}")
                    if attempt < 2:
                        await asyncio.sleep(2)

            await asyncio.sleep(1)
            current = batch_end + timedelta(days=1)

    log(f"  [{label}] ✓ {len(all_customers)} transaksi ditemukan")
    return {"success": True, "customers": all_customers, "summary": summary}


# ── Core: Kirim ke Laravel ────────────────────────────────────────────────────

async def send_to_laravel(tokens: list, api_url: str, api_key: str, date_from: str, date_to: str) -> bool:
    log(f"Mengirim {len(tokens)} hasil ke Laravel...")

    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.post(
                    f"{api_url}/api/github-actions/tokens",
                    json={
                        "tokens":       tokens,
                        "scrape_after": True,
                        "date_from":    date_from,
                        "date_to":      date_to,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key":    api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    result = await resp.json()
                    if resp.status == 200:
                        log(f"✓ Laravel response: {result.get('message', 'OK')}")
                        return True
                    else:
                        log(f"✗ Laravel error ({resp.status}): {result}")
            except Exception as e:
                log(f"✗ Kirim ke Laravel gagal attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)

    return False


# ── Core: Run Semua Akun ──────────────────────────────────────────────────────

async def run_scrape(date_from: str = "", date_to: str = ""):
    api_url = os.environ.get("LARAVEL_API_URL", "").rstrip("/")
    api_key = os.environ.get("LARAVEL_API_KEY", "")

    if not api_url or not api_key:
        log("ERROR: LARAVEL_API_URL dan LARAVEL_API_KEY belum diset!")
        ScrapeStatus.running     = False
        ScrapeStatus.last_result = {"success": False, "error": "Env vars tidak lengkap"}
        return

    if not date_from:
        date_from, _ = default_dates()
    if not date_to:
        _, date_to = default_dates()

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
                log(f"✗ API error: {body.get('message')}")
    except Exception as e:
        log(f"✗ Gagal fetch akun: {e}")

    if not accounts:
        log("ERROR: Tidak ada akun!")
        ScrapeStatus.running     = False
        ScrapeStatus.last_result = {"success": False, "error": "Tidak ada akun"}
        return

    log(f"Memproses {len(accounts)} akun...")
    log("-" * 55)

    successful = []
    failed     = []

    for i, acc in enumerate(accounts, 1):
        email = acc.get("email", "")
        pin   = acc.get("pin", "")
        label = acc.get("label", "") or acc.get("name", "") or email[:20]

        log(f"[{i}/{len(accounts)}] {label}")

        if not email or not pin:
            log(f"  Skipped: kredensial kosong")
            failed.append({"email": email, "error": "Kredensial kosong"})
            continue

        # Login + ambil stok
        login_result = await login_one(email, pin, label)

        if not login_result["success"]:
            failed.append({"email": email, "error": login_result.get("error")})
            log(f"  ✗ Gagal: {login_result.get('error','')[:80]}")
            if i < len(accounts):
                await asyncio.sleep(3)
            continue

        # Fetch transaksi
        tx_result = await fetch_transactions(
            login_result["token"], date_from, date_to, label
        )

        successful.append({
            "email":           email,
            "token":           login_result["token"],
            "pangkalan_id":    login_result["pangkalan_id"],
            "store_name":      login_result["store_name"],
            "stock_available": login_result["stock_available"],
            "stock_redeem":    login_result["stock_redeem"],
            "sold":            login_result["sold"],
            "stock_date":      login_result.get("stock_date"),
            "last_stock":      login_result.get("last_stock"),
            "last_stock_date": login_result.get("last_stock_date"),
            "stock_data": {
                "stockAvailable": login_result["stock_available"],
                "stockRedeem":    login_result["stock_redeem"],
                "sold":           login_result["sold"],
            },
            "transactions": tx_result.get("customers", []),
            "summary":      tx_result.get("summary"),
        })

        log(f"  ✓ {label} selesai ({len(tx_result.get('customers',[]))} transaksi)")

        if i < len(accounts):
            await asyncio.sleep(3)

    log("-" * 55)
    log(f"Selesai: {len(successful)} berhasil, {len(failed)} gagal")

    # Kirim ke Laravel
    ok = False
    if successful:
        ok = await send_to_laravel(successful, api_url, api_key, date_from, date_to)
    else:
        log("Tidak ada data untuk dikirim ke Laravel")

    ScrapeStatus.running     = False
    ScrapeStatus.last_run    = datetime.now().isoformat()
    ScrapeStatus.last_result = {
        "success":   ok,
        "total":     len(accounts),
        "berhasil":  len(successful),
        "gagal":     len(failed),
        "date_from": date_from,
        "date_to":   date_to,
        "timestamp": datetime.now().isoformat(),
    }
    log("=" * 55)
    log("DONE!")


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    """Info service + status"""
    return {
        "service":  "MyPertamina Scraper",
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
    2. cPanel cron Rumahweb jam 00:05 WIB
    """
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    if ScrapeStatus.running:
        return {
            "success": False,
            "message": "Scrape sedang berjalan, tunggu selesai",
        }

    ScrapeStatus.running = True

    date_from = req.date_from or datetime.now().strftime("%Y-%m-%d")
    date_to   = req.date_to   or datetime.now().strftime("%Y-%m-%d")

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
