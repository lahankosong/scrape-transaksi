"""
main.py — MyPertamina Scraper Service
Deploy ke Railway.com

Dua mode trigger:
1. Terjadwal  → dipanggil cPanel cron Rumahweb jam 00:05 WIB
2. Manual     → dipanggil tombol scrape di Laravel

v2: fetch transaksi & stok via browser (bukan aiohttp langsung)
    agar tidak kena blokir IP datacenter Railway
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
    today = datetime.now().strftime("%Y-%m-%d")
    return today, today


# ── Core: Login + Stok + Transaksi (semua via browser) ───────────────────────

async def scrape_one(email: str, pin: str, label: str,
                     start_date: str, end_date: str) -> dict:
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
        "transactions":    [],
        "summary":         None,
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
            # ── LOGIN ──────────────────────────────────────────────────────────
            log(f"  [{label}] Membuka halaman login...")
            await page.goto(
                "https://subsiditepatlpg.mypertamina.id/merchant-login",
                wait_until="domcontentloaded",
                timeout=60000
            )
            await asyncio.sleep(2)

            log(f"  [{label}] Input email & PIN...")
            await page.locator("input").nth(0).fill(email)
            await asyncio.sleep(0.3)
            await page.locator("input").nth(1).fill(pin)
            await asyncio.sleep(0.3)

            log(f"  [{label}] Klik login...")
            await page.locator("button").filter(has_text="MASUK").click()

            for _ in range(30):
                if token_holder["token"]:
                    break
                await asyncio.sleep(1)

            if not token_holder["token"]:
                result["error"] = "Token tidak tertangkap — cek kredensial"
                log(f"  [{label}] ✗ {result['error']}")
                return result

            # Decode pangkalan_id
            try:
                parts   = token_holder["token"].split(".")
                payload = json.loads(base64.b64decode(
                    parts[1] + "=" * (4 - len(parts[1]) % 4)
                ))
                result["pangkalan_id"] = payload.get("sub")
            except Exception:
                pass

            result["token"]   = token_holder["token"]
            log(f"  [{label}] ✓ Login berhasil")

            # ── FETCH STOK via browser ─────────────────────────────────────────
            log(f"  [{label}] Fetch stok via browser...")
            try:
                stok_data = await page.evaluate("""
                    async (token) => {
                        const res = await fetch(
                            'https://api-map.my-pertamina.id/general/products/v1/products/user',
                            {
                                headers: {
                                    'Authorization': 'Bearer ' + token,
                                    'Accept': 'application/json',
                                }
                            }
                        );
                        return await res.json();
                    }
                """, token_holder["token"])

                if stok_data.get("success") and stok_data.get("data"):
                    d = stok_data["data"]
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
                else:
                    log(f"  [{label}] ⚠ Stok response: {stok_data}")
            except Exception as e:
                log(f"  [{label}] ⚠ Stok gagal: {str(e)[:80]}")

            # ── FETCH TRANSAKSI via browser (per batch 7 hari) ─────────────────
            log(f"  [{label}] Fetch transaksi {start_date} s/d {end_date} via browser...")
            all_customers = []
            summary       = None

            start   = datetime.strptime(start_date, "%Y-%m-%d")
            end     = datetime.strptime(end_date,   "%Y-%m-%d")
            current = start

            while current <= end:
                batch_end = min(current + timedelta(days=6), end)
                s = current.strftime("%Y-%m-%d")
                e = batch_end.strftime("%Y-%m-%d")

                try:
                    tx_data = await page.evaluate("""
                        async ([token, startDate, endDate]) => {
                            const params = new URLSearchParams({
                                search: '',
                                sort: 'latest',
                                startDate: startDate,
                                endDate: endDate,
                            });
                            const res = await fetch(
                                'https://api-map.my-pertamina.id/general/v3/transactions/report?' + params,
                                {
                                    headers: {
                                        'Authorization': 'Bearer ' + token,
                                        'Accept': 'application/json',
                                    }
                                }
                            );
                            return await res.json();
                        }
                    """, [token_holder["token"], s, e])

                    if tx_data.get("success"):
                        customers = tx_data["data"].get("customersReport", [])
                        all_customers.extend(customers)
                        if tx_data["data"].get("summaryReport"):
                            summary = tx_data["data"]["summaryReport"]
                            summary["date"] = e
                        log(f"  [{label}] Batch {s}~{e}: {len(customers)} transaksi")
                    else:
                        log(f"  [{label}] Batch {s}~{e} gagal: {tx_data}")

                except Exception as e_err:
                    log(f"  [{label}] Batch {s}~{e} error: {str(e_err)[:80]}")

                await asyncio.sleep(1)
                current = batch_end + timedelta(days=1)

            result["transactions"] = all_customers
            result["summary"]      = summary
            result["success"]      = True
            log(f"  [{label}] ✓ Total transaksi: {len(all_customers)}")

        except Exception as e:
            result["error"] = f"Browser error: {str(e)}"
            log(f"  [{label}] ✗ {result['error']}")
        finally:
            await browser.close()

    return result


# ── Core: Kirim ke Laravel ────────────────────────────────────────────────────

async def send_to_laravel(tokens: list, api_url: str, api_key: str,
                          date_from: str, date_to: str) -> bool:
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

        # Scrape: login + stok + transaksi semuanya via browser
        res = await scrape_one(email, pin, label, date_from, date_to)

        if not res["success"]:
            failed.append({"email": email, "error": res.get("error")})
            log(f"  ✗ Gagal: {res.get('error','')[:80]}")
        else:
            successful.append({
                "email":           email,
                "token":           res["token"],
                "pangkalan_id":    res["pangkalan_id"],
                "store_name":      res["store_name"],
                "stock_available": res["stock_available"],
                "stock_redeem":    res["stock_redeem"],
                "sold":            res["sold"],
                "stock_date":      res.get("stock_date"),
                "last_stock":      res.get("last_stock"),
                "last_stock_date": res.get("last_stock_date"),
                "stock_data": {
                    "stockAvailable": res["stock_available"],
                    "stockRedeem":    res["stock_redeem"],
                    "sold":           res["sold"],
                },
                "transactions": res["transactions"],
                "summary":      res["summary"],
            })
            log(f"  ✓ {label} selesai ({len(res['transactions'])} transaksi)")

        if i < len(accounts):
            await asyncio.sleep(3)

    log("-" * 55)
    log(f"Selesai: {len(successful)} berhasil, {len(failed)} gagal")

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
    return {
        "service":  "MyPertamina Scraper v2",
        "status":   "running" if ScrapeStatus.running else "idle",
        "last_run": ScrapeStatus.last_run,
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.post("/scrape")
async def scrape(
    req:              ScrapeRequest,
    background_tasks: BackgroundTasks,
    x_api_key:        str = Header(default=""),
):
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
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    return {
        "running":     ScrapeStatus.running,
        "last_run":    ScrapeStatus.last_run,
        "last_result": ScrapeStatus.last_result,
    }
