<?php

/**
 * cron_trigger.php
 * ================
 * Letakkan file ini di: /public_html/cron/cron_trigger.php
 * (atau folder manapun di hosting Rumahweb)
 *
 * Setup di cPanel Rumahweb:
 * Cron Jobs → Add New Cron Job
 * Minute  : 5
 * Hour    : 17       ← 17:05 UTC = 00:05 WIB (UTC+7)
 * Day     : *
 * Month   : *
 * Weekday : *
 * Command : /usr/local/bin/php /home/USERNAME/public_html/cron/cron_trigger.php
 *
 * Ganti USERNAME dengan username cPanel Rumahweb kamu.
 */

// ── Konfigurasi ───────────────────────────────────────────────────────────────

define('RENDER_SCRAPER_URL', 'https://scrape-transaksi-production.up.railway.app'); // ganti dengan URL Render kamu
define('API_KEY',            'isi_dengan_LARAVEL_API_KEY_kamu');
define('LOG_FILE',           __DIR__ . '/cron.log');

// ── Helper log ────────────────────────────────────────────────────────────────

function cron_log(string $msg): void
{
    $line = '[' . date('Y-m-d H:i:s') . '] ' . $msg . PHP_EOL;
    file_put_contents(LOG_FILE, $line, FILE_APPEND);
    echo $line;
}

// ── Mulai ─────────────────────────────────────────────────────────────────────

cron_log('=== Cron trigger dimulai ===');

$dateToday = date('Y-m-d');

// Step 1: Wake up Render (free tier bisa tidur)
cron_log('Wake up Render...');
$wake = @file_get_contents(RENDER_SCRAPER_URL . '/health');
if ($wake === false) {
    cron_log('WARN: Health check gagal, tetap lanjut...');
} else {
    cron_log('Render aktif: ' . $wake);
}

sleep(3); // tunggu 3 detik setelah wake up

// Step 2: Trigger scrape
cron_log("Trigger scrape untuk tanggal {$dateToday}...");

$payload = json_encode([
    'date_from' => $dateToday,
    'date_to'   => $dateToday,
]);

$context = stream_context_create([
    'http' => [
        'method'  => 'POST',
        'header'  => implode("\r\n", [
            'Content-Type: application/json',
            'X-Api-Key: ' . API_KEY,
            'Content-Length: ' . strlen($payload),
        ]),
        'content' => $payload,
        'timeout' => 30,
    ],
]);

$response = @file_get_contents(RENDER_SCRAPER_URL . '/scrape', false, $context);

if ($response === false) {
    cron_log('ERROR: Gagal koneksi ke Render scraper!');
    exit(1);
}

$result = json_decode($response, true);

if ($result && $result['success']) {
    cron_log('✓ Scrape berhasil dimulai: ' . ($result['message'] ?? 'OK'));
} else {
    cron_log('✗ Scrape gagal: ' . $response);
}

cron_log('=== Cron trigger selesai ===');
