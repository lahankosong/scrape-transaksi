<?php

/**
 * =====================================================
 * TAMBAHKAN KE: GithubActionsController.php
 * (atau controller apapun yang handle tombol scrape)
 * =====================================================
 *
 * Fungsi ini menggantikan trigger GitHub Actions.
 * Sekarang langsung POST ke Render.com scraper service.
 */

// ── 1. Tambah di .env Laravel ─────────────────────────────────────────────────
//
// RENDER_SCRAPER_URL=https://mypertamina-scraper.onrender.com
// (URL ini muncul di dashboard Render setelah deploy)

// ── 2. Tambah di config/services.php ─────────────────────────────────────────
//
// 'render' => [
//     'url' => env('RENDER_SCRAPER_URL'),
// ],

// ── 3. Method untuk trigger scrape manual (tombol di dashboard) ───────────────

public function triggerScrape(Request $request)
{
    $renderUrl = config('services.render.url');
    $apiKey    = config('services.github_actions.api_key'); // pakai api_key yang sama

    if (!$renderUrl) {
        return back()->withErrors(['msg' => 'RENDER_SCRAPER_URL belum diset di .env']);
    }

    $dateFrom = $request->input('date_from', now()->toDateString());
    $dateTo   = $request->input('date_to',   now()->toDateString());

    try {
        // Wake up Render dulu (free tier bisa tidur)
        Http::timeout(10)->get("{$renderUrl}/health");

        // Trigger scrape
        $response = Http::timeout(30)
            ->withHeaders(['X-Api-Key' => $apiKey])
            ->post("{$renderUrl}/scrape", [
                'date_from' => $dateFrom,
                'date_to'   => $dateTo,
            ]);

        if ($response->successful()) {
            return back()->with('success', 'Scrape dimulai! Proses berjalan di background.');
        }

        return back()->withErrors(['msg' => 'Render error: ' . $response->body()]);

    } catch (\Exception $e) {
        return back()->withErrors(['msg' => 'Gagal koneksi ke scraper: ' . $e->getMessage()]);
    }
}

// ── 4. Method untuk cek status scrape (opsional, untuk polling di frontend) ───

public function scrapeStatus()
{
    $renderUrl = config('services.render.url');
    $apiKey    = config('services.github_actions.api_key');

    try {
        $response = Http::timeout(10)
            ->withHeaders(['X-Api-Key' => $apiKey])
            ->get("{$renderUrl}/status");

        return response()->json($response->json());

    } catch (\Exception $e) {
        return response()->json(['error' => $e->getMessage()], 500);
    }
}

// ── 5. Tambah route di routes/web.php ────────────────────────────────────────
//
// Route::post('/scrape/trigger', [GithubActionsController::class, 'triggerScrape'])->name('scrape.trigger');
// Route::get('/scrape/status',   [GithubActionsController::class, 'scrapeStatus'])->name('scrape.status');

// ── 6. Contoh tombol di Blade template ───────────────────────────────────────
//
// <form method="POST" action="{{ route('scrape.trigger') }}">
//     @csrf
//     <input type="date" name="date_from" value="{{ date('Y-m-d') }}">
//     <input type="date" name="date_to"   value="{{ date('Y-m-d') }}">
//     <button type="submit">Scrape Sekarang</button>
// </form>
