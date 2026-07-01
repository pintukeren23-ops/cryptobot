import os
import re
import json
import asyncio
import aiohttp
import sqlite3
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from groq import Groq

TOKEN = os.environ.get("TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

groq_client = Groq(api_key=GROQ_API_KEY)

# Cache sederhana: {contract: {"data": ..., "timestamp": ...}}
_cache = {}

def init_db():
    """Bikin tabel SQLite kalau belum ada. Dipanggil sekali saat bot start."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS analisis_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        contract TEXT,
        chain TEXT,
        nama TEXT,
        symbol TEXT,
        harga_awal REAL,
        tp1_harga REAL,
        tp2_harga REAL,
        sl_harga REAL,
        status TEXT DEFAULT 'pending',
        timestamp INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        contract TEXT,
        chain TEXT,
        nama TEXT,
        symbol TEXT,
        harga_awal REAL,
        tp_harga REAL,
        sl_harga REAL,
        active INTEGER DEFAULT 1,
        timestamp INTEGER
    )""")
    conn.commit()
    conn.close()

def simpan_riwayat_analisis(user_id, contract, chain, nama, symbol, harga, tp1, tp2, sl):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO analisis_history (user_id, contract, chain, nama, symbol, harga_awal, tp1_harga, tp2_harga, sl_harga, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, contract, chain, nama, symbol, harga, tp1, tp2, sl, int(time.time()))
    )
    conn.commit()
    conn.close()

async def fetch_json(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except:
        pass
    return None

async def get_token_data(contract):
    cache_key = "token_" + contract
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["timestamp"] < 60:
        return _cache[cache_key]["data"]
    url = "https://api.dexscreener.com/latest/dex/tokens/" + contract
    data = await fetch_json(url)
    result = None
    if data and "pairs" in data and len(data["pairs"]) > 0:
        result = data["pairs"][0]
    _cache[cache_key] = {"data": result, "timestamp": now}
    return result

async def get_rugcheck(contract):
    cache_key = "rug_" + contract
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["timestamp"] < 300:
        return _cache[cache_key]["data"]
    url = "https://api.rugcheck.xyz/v1/tokens/" + contract + "/report/summary"
    data = await fetch_json(url)
    _cache[cache_key] = {"data": data, "timestamp": now}
    return data

def ekstrak_holder_info(rug_data):
    """Ambil info konsentrasi holder dari report RugCheck. Return None kalau data gak ada."""
    if not rug_data:
        return None
    try:
        top_holders = rug_data.get("topHolders", [])
        if not top_holders:
            return None
        top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
        holder_terbesar_pct = top_holders[0].get("pct", 0) if top_holders else 0
        return {
            "top10_pct": round(top10_pct, 1),
            "holder_terbesar_pct": round(holder_terbesar_pct, 1),
            "jumlah_holder_terdaftar": len(top_holders)
        }
    except (KeyError, IndexError, TypeError):
        return None

async def get_new_tokens():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    data = await fetch_json(url)
    return data if data else []

async def get_trending():
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    data = await fetch_json(url)
    return data if data else []

async def get_candle_data(network, pool_address, timeframe="hour", aggregate=1, limit=48):
    """Ambil data candle (OHLCV) dari GeckoTerminal. Gratis, tanpa API key."""
    cache_key = "candle_" + network + "_" + pool_address + "_" + timeframe + "_" + str(aggregate)
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["timestamp"] < 60:
        return _cache[cache_key]["data"]
    url = (
        "https://api.geckoterminal.com/api/v2/networks/" + network +
        "/pools/" + pool_address + "/ohlcv/" + timeframe +
        "?aggregate=" + str(aggregate) + "&limit=" + str(limit) + "&currency=usd"
    )
    data = await fetch_json(url)
    result = None
    if data:
        try:
            result = data["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError):
            pass
    _cache[cache_key] = {"data": result, "timestamp": now}
    return result

def hitung_rsi(closes, periode=14):
    """Hitung RSI dengan Wilder's Smoothing Method (standar industri)."""
    if len(closes) < periode + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        selisih = closes[i] - closes[i - 1]
        gains.append(max(selisih, 0))
        losses.append(max(-selisih, 0))
    # Wilder's smoothing: rata-rata awal pakai simple average, lalu smoothed
    avg_gain = sum(gains[:periode]) / periode
    avg_loss = sum(losses[:periode]) / periode
    for i in range(periode, len(gains)):
        avg_gain = (avg_gain * (periode - 1) + gains[i]) / periode
        avg_loss = (avg_loss * (periode - 1) + losses[i]) / periode
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def hitung_sma(closes, periode=20):
    """Hitung Simple Moving Average dari list harga close."""
    if len(closes) < periode:
        periode = len(closes)
    if periode == 0:
        return None
    return sum(closes[-periode:]) / periode

def cari_support_resistance(highs, lows, closes, toleransi_pct=2.0):
    """
    Cari support & resistance multi-touch (level yang disentuh minimal 2x).
    Lebih akurat dari sekedar ambil max/min.
    """
    if not highs or not lows or len(highs) < 5:
        return min(lows) if lows else None, max(highs) if highs else None

    def cluster_levels(levels, toleransi_pct):
        """Kelompokkan level yang berdekatan, ambil yang paling sering disentuh."""
        if not levels:
            return []
        levels_sorted = sorted(levels)
        clusters = []
        current_cluster = [levels_sorted[0]]
        for level in levels_sorted[1:]:
            if (level - current_cluster[0]) / current_cluster[0] * 100 <= toleransi_pct:
                current_cluster.append(level)
            else:
                clusters.append(current_cluster)
                current_cluster = [level]
        clusters.append(current_cluster)
        # Filter hanya cluster yang disentuh 2x atau lebih, ambil rata-ratanya
        multi_touch = [sum(c) / len(c) for c in clusters if len(c) >= 2]
        return multi_touch

    resistance_levels = cluster_levels(highs, toleransi_pct)
    support_levels = cluster_levels(lows, toleransi_pct)

    # Fallback ke simple max/min kalau tidak ada multi-touch
    harga_sekarang = closes[-1] if closes else 0
    resistance = min((r for r in resistance_levels if r > harga_sekarang), default=max(highs))
    support = max((s for s in support_levels if s < harga_sekarang), default=min(lows))

    return support, resistance

def hitung_volume_anomali(volumes):
    """Deteksi apakah volume candle terakhir anomali dibanding rata-rata."""
    if not volumes or len(volumes) < 5:
        return None
    rata2 = sum(volumes[:-1]) / len(volumes[:-1])
    vol_sekarang = volumes[-1]
    if rata2 == 0:
        return None
    rasio = vol_sekarang / rata2
    return {
        "rasio": round(rasio, 1),
        "status": "SPIKE TINGGI" if rasio > 5 else "TINGGI" if rasio > 2 else "NORMAL" if rasio > 0.5 else "RENDAH",
        "rata2": rata2,
        "sekarang": vol_sekarang
    }

def deteksi_pola_candle(candles):
    """
    Deteksi 3 pola candle dasar dari 3 candle terakhir.
    Input: list candle [timestamp, open, high, low, close, volume]
    """
    if not candles or len(candles) < 3:
        return []
    pola_ditemukan = []
    for c in candles[-3:]:
        o, h, l, close = c[1], c[2], c[3], c[4]
        body = abs(close - o)
        total_range = h - l
        if total_range == 0:
            continue
        body_ratio = body / total_range

        # Doji: body sangat kecil (< 10% range) = pasar ragu-ragu
        if body_ratio < 0.1:
            pola_ditemukan.append("Doji (pasar ragu-ragu, potensi reversal)")

        # Hammer: body kecil di atas, ekor bawah panjang = potensi reversal naik
        lower_shadow = min(o, close) - l
        upper_shadow = h - max(o, close)
        if lower_shadow > body * 2 and upper_shadow < body * 0.5 and body_ratio < 0.4:
            pola_ditemukan.append("Hammer (potensi reversal naik)")

        # Shooting Star: body kecil di bawah, ekor atas panjang = potensi reversal turun
        if upper_shadow > body * 2 and lower_shadow < body * 0.5 and body_ratio < 0.4:
            pola_ditemukan.append("Shooting Star (potensi reversal turun)")

    return list(set(pola_ditemukan))  # deduplikasi

def hitung_indikator(ohlcv_list, ohlcv_15m=None):
    """Olah raw candle GeckoTerminal jadi dict indikator siap pakai. Support dual timeframe."""
    if not ohlcv_list or len(ohlcv_list) < 5:
        return None
    # GeckoTerminal urutkan candle dari terbaru ke terlama, balik dulu jadi lama->baru
    candles = list(reversed(ohlcv_list))
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    volumes = [c[5] for c in candles]

    rsi_1h = hitung_rsi(closes, periode=min(14, len(closes) - 1))
    sma20 = hitung_sma(closes, periode=20)
    support, resistance = cari_support_resistance(highs, lows, closes)
    volume_anomali = hitung_volume_anomali(volumes)
    pola_candle = deteksi_pola_candle(candles)

    result = {
        "rsi": rsi_1h,
        "sma20": sma20,
        "support": support,
        "resistance": resistance,
        "jumlah_candle": len(candles),
        "volume_anomali": volume_anomali,
        "pola_candle": pola_candle,
        "rsi_15m": None
    }

    # Dual timeframe: hitung RSI 15M kalau ada data
    if ohlcv_15m and len(ohlcv_15m) >= 5:
        candles_15m = list(reversed(ohlcv_15m))
        closes_15m = [c[4] for c in candles_15m]
        result["rsi_15m"] = hitung_rsi(closes_15m, periode=min(14, len(closes_15m) - 1))

    return result

def format_harga(harga):
    """Format harga dengan desimal yang sesuai skala token."""
    if harga == 0:
        return "$0"
    if harga < 0.000001:
        return f"${harga:.10f}".rstrip('0').rstrip('.')
    elif harga < 0.001:
        return f"${harga:.8f}".rstrip('0').rstrip('.')
    elif harga < 1:
        return f"${harga:.6f}".rstrip('0').rstrip('.')
    else:
        return f"${harga:.4f}".rstrip('0').rstrip('.')

def hitung_tp_sl(harga_sekarang, pct_tp1, pct_tp2, pct_sl):
    """Hitung harga TP/SL dari persentase yang diberikan AI."""
    tp1 = harga_sekarang * (1 + pct_tp1 / 100)
    tp2 = harga_sekarang * (1 + pct_tp2 / 100) if pct_tp2 else None
    sl  = harga_sekarang * (1 - abs(pct_sl) / 100)
    return tp1, tp2, sl

def ai_analisis(token_info, harga, indikator=None, rug_data=None, holder_info=None):
    try:
        harga_float = float(harga)

        if indikator and indikator.get("support") and indikator.get("resistance"):
            vol_anomali = indikator.get("volume_anomali")
            vol_text = ""
            if vol_anomali:
                vol_text = (
                    "Volume sekarang: " + str(round(vol_anomali["sekarang"])) +
                    " (rata-rata: " + str(round(vol_anomali["rata2"])) + ")" +
                    " → STATUS: " + vol_anomali["status"] + " (" + str(vol_anomali["rasio"]) + "x rata-rata)\n"
                )

            pola = indikator.get("pola_candle", [])
            pola_text = ("Pola candle terdeteksi: " + ", ".join(pola) + "\n") if pola else ""

            rsi_15m = indikator.get("rsi_15m")
            rsi_15m_text = ("RSI(14) 15M: " + str(rsi_15m) + "\n") if rsi_15m else ""

            indikator_text = (
                "\nDATA TEKNIKAL (dihitung dari candle historis riil, " + str(indikator["jumlah_candle"]) + " candle 1H terakhir):\n"
                "RSI(14) 1H: " + (str(indikator["rsi"]) if indikator["rsi"] is not None else "N/A") + " (>70 overbought, <30 oversold)\n"
                + rsi_15m_text +
                "SMA20: " + format_harga(indikator["sma20"]) + "\n"
                "Support (multi-touch): " + format_harga(indikator["support"]) + "\n"
                "Resistance (multi-touch): " + format_harga(indikator["resistance"]) + "\n"
                + vol_text + pola_text +
                "\nPENTING: Gunakan level Support dan Resistance sebagai acuan utama TP dan entry. "
                "Perhatikan status volume anomali dan pola candle sebagai konfirmasi sinyal. "
                "Jika RSI 1H overbought (>70) tapi RSI 15M oversold (<30), itu potensi entry lebih baik.\n"
            )
        else:
            indikator_text = (
                "\nCATATAN: Data candle historis token ini belum cukup (token kemungkinan masih sangat baru). "
                "Tidak ada data teknikal yang bisa dihitung. "
                "Beri peringatan eksplisit di bagian alasan bahwa prediksi sangat tidak pasti.\n"
            )

        keamanan_text = ""
        if rug_data:
            skor_rugcheck = rug_data.get("score")
            keamanan_text += "\nSKOR KEAMANAN RUGCHECK (data riil): " + str(skor_rugcheck) + "\n"
        if holder_info:
            keamanan_text += (
                "Top 1 holder: " + str(holder_info["holder_terbesar_pct"]) + "% supply\n"
                "Top 10 holder: " + str(holder_info["top10_pct"]) + "% supply\n"
                "(>50% = risiko whale dump tinggi)\n"
            )
        if keamanan_text:
            keamanan_text += "WAJIB dasarkan status_keamanan pada data RugCheck/holder di atas.\n"
        else:
            keamanan_text = "\nData RugCheck tidak tersedia. Nilai status_keamanan dengan hati-hati.\n"

        prompt = (
            "Kamu adalah analis trading meme coin profesional. Analisis token berikut secara mendalam.\n\n"
            "Data Token:\n" + token_info + "\n"
            "Harga saat ini: " + format_harga(harga_float) + "\n"
            + indikator_text + keamanan_text + "\n"
            "Tentukan entry ideal, target profit (TP), dan stop loss (SL) secara realistis berdasarkan semua data di atas.\n\n"
            "Jawab HANYA dengan JSON murni:\n"
            "{\n"
            '  "status_keamanan": "Aman/Waspada/Bahaya",\n'
            '  "potensi_pump": "Rendah/Sedang/Tinggi",\n'
            '  "entry_pct": -5,\n'
            '  "entry_note": "alasan entry",\n'
            '  "tp1_harga": 0.00025,\n'
            '  "tp1_alasan": "alasan TP1",\n'
            '  "tp2_harga": 0.00035,\n'
            '  "tp2_alasan": "alasan TP2 atau kosong jika tidak ada",\n'
            '  "sl_pct": 15,\n'
            '  "alasan": "2-3 kalimat analisis keseluruhan"\n'
            "}\n"
            "entry_pct negatif = tunggu turun, 0 = entry sekarang. tp/sl dalam USD. Bahasa Indonesia."
        )
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)

        entry_pct  = float(data.get("entry_pct", 0))
        entry_note = data.get("entry_note", "")
        tp1_harga  = float(data.get("tp1_harga", 0))
        tp1_alasan = data.get("tp1_alasan", "")
        tp2_harga  = float(data.get("tp2_harga", 0))
        tp2_alasan = data.get("tp2_alasan", "")
        sl_pct     = float(data.get("sl_pct", 15))
        sl_harga   = harga_float * (1 - abs(sl_pct) / 100)
        harga_entry = harga_float * (1 + entry_pct / 100)

        # Sanity check: cap TP yang kelewat liar dari resistance riil
        if indikator and indikator.get("resistance"):
            batas_atas = max(indikator["resistance"], harga_float) * 1.5
            if tp1_harga > batas_atas:
                tp1_harga = indikator["resistance"]
                tp1_alasan = "(disesuaikan ke level resistance historis)"
            if tp2_harga > batas_atas:
                tp2_harga = indikator["resistance"] * 1.15
                tp2_alasan = "(disesuaikan ke atas resistance historis)"

        tp1_pct = ((tp1_harga - harga_float) / harga_float * 100) if tp1_harga else 0
        tp2_pct = ((tp2_harga - harga_float) / harga_float * 100) if tp2_harga else 0

        if entry_pct == 0:
            saran_entry_teks = "Entry sekarang di " + format_harga(harga_float)
        else:
            saran_entry_teks = (
                "Entry di " + format_harga(harga_entry) +
                " (" + ("tunggu turun " if entry_pct < 0 else "breakout naik ") + str(abs(int(entry_pct))) + "%)"
            )
        if entry_note:
            saran_entry_teks += "\n" + entry_note

        hasil = (
            "STATUS KEAMANAN: " + data.get("status_keamanan", "-") + "\n"
            "POTENSI PUMP: " + data.get("potensi_pump", "-") + "\n\n"
            "SARAN ENTRY:\n" + saran_entry_teks + "\n\n"
            "TARGET PROFIT:\n"
        )
        if tp1_harga:
            hasil += "  TP1: " + format_harga(tp1_harga) + " (+" + str(int(tp1_pct)) + "%)\n"
            if tp1_alasan:
                hasil += "  " + tp1_alasan + "\n"
        if tp2_harga:
            hasil += "\n  TP2: " + format_harga(tp2_harga) + " (+" + str(int(tp2_pct)) + "%)\n"
            if tp2_alasan:
                hasil += "  " + tp2_alasan + "\n"
        hasil += (
            "\nSTOP LOSS (-" + str(int(sl_pct)) + "%): " + format_harga(sl_harga) + "\n\n"
            "ALASAN:\n" + data.get("alasan", "-")
        )

        raw_data = {
            "harga_awal": harga_float,
            "tp1_harga": tp1_harga,
            "tp2_harga": tp2_harga if tp2_harga else None,
            "sl_harga": sl_harga
        }
        return hasil, raw_data

    except Exception as e:
        try:
            return response.choices[0].message.content, None
        except:
            return "AI analisis tidak tersedia: " + str(e), None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = (
        "Selamat datang di Crypto Radar AI!\n\n"
        "Bot analisis meme coin powered by AI dengan saran trading lengkap.\n\n"
        "PERINTAH TERSEDIA:\n\n"
        "/scan - Scan token baru\n"
        "/trending - Token yang lagi trending\n"
        "/analisis <contract> - Analisis lengkap + saran entry/TP/SL\n"
        "/cek <contract> - Cek keamanan token\n"
        "/help - Bantuan\n\n"
        "DISCLAIMER: Bukan saran investasi. Trading crypto mengandung risiko tinggi!"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Scan Token Baru", callback_data="scan")],
        [InlineKeyboardButton("Trending Token", callback_data="trending")],
        [InlineKeyboardButton("Bantuan", callback_data="help")]
    ])
    await update.message.reply_text(teks, reply_markup=keyboard)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Sedang scan token baru... Mohon tunggu!")
    try:
        tokens = await get_new_tokens()
        if not tokens:
            await msg.edit_text("Tidak ada data token baru saat ini.")
            return
        teks = "TOKEN BARU TERDETEKSI\n\n"
        count = 0
        for token in tokens[:10]:
            if count >= 5:
                break
            try:
                contract = token.get("tokenAddress", "")
                chain = token.get("chainId", "")
                if not contract:
                    continue
                pair_data = await get_token_data(contract)
                if not pair_data:
                    continue
                nama = pair_data.get("baseToken", {}).get("name", "Unknown")
                symbol = pair_data.get("baseToken", {}).get("symbol", "?")
                harga = pair_data.get("priceUsd", "0")
                liquidity = pair_data.get("liquidity", {}).get("usd", 0)
                volume_1h = pair_data.get("volume", {}).get("h1", 0)
                perubahan_1h = pair_data.get("priceChange", {}).get("h1", 0)
                dex_url = pair_data.get("url", "")
                if float(liquidity) < 5000:
                    continue
                teks += (
                    "Token: " + nama + " (" + symbol + ")\n"
                    "Chain: " + chain + "\n"
                    "Harga: $" + str(harga) + "\n"
                    "Liquidity: $" + str(int(liquidity)) + "\n"
                    "Volume 1H: $" + str(int(volume_1h)) + "\n"
                    "Perubahan 1H: " + str(perubahan_1h) + "%\n"
                    "Link: " + dex_url + "\n\n"
                )
                count += 1
            except:
                continue
        if count == 0:
            await msg.edit_text("Tidak ada token baru yang memenuhi kriteria.")
            return
        teks += "Gunakan /analisis <contract> untuk analisis AI lengkap!"
        await msg.edit_text(teks)
    except Exception as e:
        await msg.edit_text("Terjadi error saat scan.")

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Sedang ambil data trending... Mohon tunggu!")
    try:
        tokens = await get_trending()
        if not tokens:
            await msg.edit_text("Tidak ada data trending saat ini.")
            return
        teks = "TOKEN TRENDING SEKARANG\n\n"
        count = 0
        for token in tokens[:8]:
            if count >= 5:
                break
            try:
                contract = token.get("tokenAddress", "")
                chain = token.get("chainId", "")
                if not contract:
                    continue
                pair_data = await get_token_data(contract)
                if not pair_data:
                    continue
                nama = pair_data.get("baseToken", {}).get("name", "Unknown")
                symbol = pair_data.get("baseToken", {}).get("symbol", "?")
                harga = pair_data.get("priceUsd", "0")
                perubahan_1h = pair_data.get("priceChange", {}).get("h1", 0)
                perubahan_24h = pair_data.get("priceChange", {}).get("h24", 0)
                dex_url = pair_data.get("url", "")
                teks += (
                    "Token: " + nama + " (" + symbol + ")\n"
                    "Chain: " + chain + "\n"
                    "Harga: $" + str(harga) + "\n"
                    "Perubahan 1H: " + str(perubahan_1h) + "%\n"
                    "Perubahan 24H: " + str(perubahan_24h) + "%\n"
                    "Link: " + dex_url + "\n\n"
                )
                count += 1
            except:
                continue
        if count == 0:
            await msg.edit_text("Tidak ada data trending.")
            return
        await msg.edit_text(teks)
    except Exception as e:
        await msg.edit_text("Terjadi error.")

async def analisis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /analisis <contract_address>")
        return
    contract = context.args[0].strip()
    msg = await update.message.reply_text("Sedang analisis token... Mohon tunggu 10-15 detik!")
    try:
        pair_data = await get_token_data(contract)
        if not pair_data:
            await msg.edit_text("Token tidak ditemukan.")
            return
        nama = pair_data.get("baseToken", {}).get("name", "Unknown")
        symbol = pair_data.get("baseToken", {}).get("symbol", "?")
        harga = pair_data.get("priceUsd", "0")
        market_cap = pair_data.get("marketCap", 0)
        liquidity = pair_data.get("liquidity", {}).get("usd", 0)
        volume_1h = pair_data.get("volume", {}).get("h1", 0)
        volume_24h = pair_data.get("volume", {}).get("h24", 0)
        perubahan_5m = pair_data.get("priceChange", {}).get("m5", 0)
        perubahan_1h = pair_data.get("priceChange", {}).get("h1", 0)
        perubahan_24h = pair_data.get("priceChange", {}).get("h24", 0)
        txns_buy = pair_data.get("txns", {}).get("h1", {}).get("buys", 0)
        txns_sell = pair_data.get("txns", {}).get("h1", {}).get("sells", 0)
        dex_url = pair_data.get("url", "")
        chain = pair_data.get("chainId", "")
        pool_address = pair_data.get("pairAddress", "")

        # Ambil candle historis dari GeckoTerminal & hitung indikator teknikal
        indikator = None
        if chain and pool_address:
            ohlcv_1h, ohlcv_15m = await asyncio.gather(
                get_candle_data(chain, pool_address, timeframe="hour", aggregate=1, limit=48),
                get_candle_data(chain, pool_address, timeframe="minute", aggregate=15, limit=48)
            )
            indikator = hitung_indikator(ohlcv_1h, ohlcv_15m)

        # Ambil data keamanan & holder dari RugCheck (khusus Solana)
        rug_data = None
        holder_info = None
        if chain == "solana":
            rug_data = await get_rugcheck(contract)
            holder_info = ekstrak_holder_info(rug_data)

        token_info = (
            "Nama: " + nama + " (" + symbol + ")\n"
            "Chain: " + chain + "\n"
            "Harga: $" + str(harga) + "\n"
            "Market Cap: $" + str(int(market_cap)) + "\n"
            "Liquidity: $" + str(int(liquidity)) + "\n"
            "Volume 1H: $" + str(int(volume_1h)) + "\n"
            "Volume 24H: $" + str(int(volume_24h)) + "\n"
            "Perubahan 5M: " + str(perubahan_5m) + "%\n"
            "Perubahan 1H: " + str(perubahan_1h) + "%\n"
            "Perubahan 24H: " + str(perubahan_24h) + "%\n"
            "Buy 1H: " + str(txns_buy) + "\n"
            "Sell 1H: " + str(txns_sell) + "\n"
        )

        # Jalankan AI di executor supaya tidak blocking bot
        loop = asyncio.get_event_loop()
        ai_result, raw_data = await loop.run_in_executor(
            None, ai_analisis, token_info, harga, indikator, rug_data, holder_info
        )

        # Simpan ke riwayat buat tracking akurasi
        if raw_data:
            simpan_riwayat_analisis(
                update.effective_user.id, contract, chain, nama, symbol,
                raw_data["harga_awal"], raw_data["tp1_harga"], raw_data["tp2_harga"], raw_data["sl_harga"]
            )

        teknikal_text = ""
        if indikator:
            rsi_text = "RSI(14) 1H: " + (str(indikator["rsi"]) if indikator["rsi"] else "N/A")
            if indikator.get("rsi_15m"):
                rsi_text += " | RSI 15M: " + str(indikator["rsi_15m"])
            vol = indikator.get("volume_anomali")
            vol_text = ("\nVolume: " + vol["status"] + " (" + str(vol["rasio"]) + "x rata-rata)") if vol else ""
            pola = indikator.get("pola_candle", [])
            pola_text = ("\nPola: " + ", ".join(pola)) if pola else ""
            teknikal_text = (
                "DATA TEKNIKAL (" + str(indikator["jumlah_candle"]) + " candle 1H):\n"
                + rsi_text + "\n"
                "Support: " + format_harga(indikator["support"]) + "\n"
                "Resistance: " + format_harga(indikator["resistance"])
                + vol_text + pola_text + "\n\n"
            )
        else:
            teknikal_text = "DATA TEKNIKAL: Belum tersedia (token terlalu baru)\n\n"

        holder_text = ""
        if holder_info:
            holder_text = (
                "KONSENTRASI HOLDER:\n"
                "Top 1: " + str(holder_info["holder_terbesar_pct"]) + "% | "
                "Top 10: " + str(holder_info["top10_pct"]) + "% supply\n\n"
            )

        teks = (
            "HASIL ANALISIS AI\n\n"
            "Token: " + nama + " (" + symbol + ")\n"
            "Chain: " + chain + "\n\n"
            "DATA MARKET:\n"
            "Harga: $" + str(harga) + "\n"
            "Market Cap: $" + str(int(market_cap)) + "\n"
            "Liquidity: $" + str(int(liquidity)) + "\n"
            "Volume 1H: $" + str(int(volume_1h)) + "\n"
            "Volume 24H: $" + str(int(volume_24h)) + "\n\n"
            "PERUBAHAN HARGA:\n"
            "5 Menit: " + str(perubahan_5m) + "%\n"
            "1 Jam: " + str(perubahan_1h) + "%\n"
            "24 Jam: " + str(perubahan_24h) + "%\n\n"
            "TRANSAKSI 1 JAM:\n"
            "Buy: " + str(txns_buy) + " | Sell: " + str(txns_sell) + "\n\n"
            + teknikal_text + holder_text +
            "ANALISIS DAN SARAN TRADING AI:\n" + ai_result + "\n\n"
            "Link DEX: " + dex_url + "\n\n"
            "DISCLAIMER: Bukan saran investasi. DYOR!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Cek Keamanan", callback_data="rugcheck|" + contract)],
            [InlineKeyboardButton("Pantau Token Ini (Watchlist)", callback_data="watch|" + contract)],
            [InlineKeyboardButton("Lihat di DEXScreener", url=dex_url)]
        ])
        await msg.edit_text(teks, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text("Terjadi error saat analisis.")

async def cek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /cek <contract_address>")
        return
    contract = context.args[0].strip()
    msg = await update.message.reply_text("Sedang cek keamanan token... Mohon tunggu!")
    try:
        rug_data = await get_rugcheck(contract)
        if not rug_data:
            await msg.edit_text("Tidak bisa cek keamanan token ini. Mungkin bukan token Solana.")
            return
        score = rug_data.get("score", "N/A")
        risks = rug_data.get("risks", [])
        teks = "HASIL CEK KEAMANAN\n\nContract: " + contract + "\nSkor Risiko: " + str(score) + "\n\n"
        if risks:
            teks += "RISIKO DITEMUKAN:\n"
            for risk in risks[:5]:
                teks += "- " + risk.get("name", "") + " (" + risk.get("level", "") + ")\n  " + risk.get("description", "") + "\n"
        else:
            teks += "Tidak ada risiko signifikan.\n"
        if score != "N/A":
            if int(score) < 500:
                teks += "\nSTATUS: RELATIF AMAN"
            elif int(score) < 1000:
                teks += "\nSTATUS: RISIKO SEDANG - Hati-hati"
            else:
                teks += "\nSTATUS: RISIKO TINGGI - Hindari!"
        teks += "\n\nDISCLAIMER: Bukan saran investasi!"
        await msg.edit_text(teks)
    except Exception as e:
        await msg.edit_text("Terjadi error cek keamanan.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = (
        "PANDUAN CRYPTO RADAR AI\n\n"
        "/scan - Scan token baru\n"
        "/trending - Token trending\n"
        "/analisis <contract> - Analisis AI lengkap dengan saran entry, TP, SL\n"
        "/cek <contract> - Cek keamanan (Solana)\n"
        "/akurasi - Lihat track record akurasi analisis bot\n"
        "/watch <contract> - Pantau token, dapat notif kalau harga gerak signifikan\n"
        "/unwatch <contract> - Stop pantau token\n"
        "/watchlist - Lihat token yang lagi dipantau\n\n"
        "TIPS TRADING AMAN:\n"
        "1. Selalu cek keamanan sebelum beli\n"
        "2. Liquidity minimal $50.000\n"
        "3. Jangan all in satu token\n"
        "4. Selalu pakai stop loss\n"
        "5. DYOR - Do Your Own Research\n\n"
        "DISCLAIMER: Bukan saran investasi!"
    )
    await update.message.reply_text(teks)

async def akurasi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM analisis_history GROUP BY status")
    rows = c.fetchall()
    conn.close()

    total = sum(r[1] for r in rows)
    if total == 0:
        await update.message.reply_text(
            "Belum ada data riwayat analisis. Track record akan mulai terkumpul setelah ada yang pakai /analisis dan sistem ngecek hasilnya secara berkala."
        )
        return

    counts = {status: jumlah for status, jumlah in rows}
    tp_hit = counts.get("tp1_hit", 0) + counts.get("tp2_hit", 0)
    sl_hit = counts.get("sl_hit", 0)
    pending = counts.get("pending", 0)
    expired = counts.get("expired", 0)
    selesai = tp_hit + sl_hit
    win_rate = (tp_hit / selesai * 100) if selesai > 0 else 0

    teks = (
        "TRACK RECORD AKURASI BOT\n\n"
        "Total analisis tercatat: " + str(total) + "\n"
        "TP tercapai: " + str(tp_hit) + "\n"
        "SL tercapai: " + str(sl_hit) + "\n"
        "Masih dipantau: " + str(pending) + "\n"
        "Kadaluarsa (>48 jam tanpa hasil): " + str(expired) + "\n\n"
    )
    if selesai > 0:
        teks += "Win rate (dari yang sudah selesai): " + str(round(win_rate, 1)) + "%\n\n"
    else:
        teks += "Belum ada yang selesai dipantau, win rate belum bisa dihitung.\n\n"
    teks += "Catatan: angka ini dihitung otomatis dari histori bot, bukan jaminan performa ke depan. DYOR!"
    await update.message.reply_text(teks)

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /watch <contract_address>")
        return
    contract = context.args[0].strip()
    msg = await update.message.reply_text("Menambahkan ke watchlist...")
    pair_data = await get_token_data(contract)
    if not pair_data:
        await msg.edit_text("Token tidak ditemukan.")
        return
    nama = pair_data.get("baseToken", {}).get("name", "Unknown")
    symbol = pair_data.get("baseToken", {}).get("symbol", "?")
    chain = pair_data.get("chainId", "")
    try:
        harga = float(pair_data.get("priceUsd", "0"))
    except (ValueError, TypeError):
        await msg.edit_text("Gagal baca harga token.")
        return
    tp_harga = harga * 1.20
    sl_harga = harga * 0.85

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO watchlist (user_id, contract, chain, nama, symbol, harga_awal, tp_harga, sl_harga, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
        (update.effective_user.id, contract, chain, nama, symbol, harga, tp_harga, sl_harga, int(time.time()))
    )
    conn.commit()
    conn.close()

    await msg.edit_text(
        "Ditambahkan ke watchlist!\n\n"
        "Token: " + nama + " (" + symbol + ")\n"
        "Harga saat ini: " + format_harga(harga) + "\n"
        "Akan notif kalau harga capai +20% (" + format_harga(tp_harga) + ") atau -15% (" + format_harga(sl_harga) + ")\n\n"
        "Pakai /unwatch " + contract + " buat berhenti pantau."
    )

async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /unwatch <contract_address>")
        return
    contract = context.args[0].strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE watchlist SET active=0 WHERE user_id=? AND contract=? AND active=1",
        (update.effective_user.id, contract)
    )
    jumlah = c.rowcount
    conn.commit()
    conn.close()
    if jumlah > 0:
        await update.message.reply_text("Token udah di-stop dari watchlist.")
    else:
        await update.message.reply_text("Token ini gak ada di watchlist aktif kamu.")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT contract, nama, symbol, harga_awal, tp_harga, sl_harga FROM watchlist WHERE user_id=? AND active=1",
        (update.effective_user.id,)
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Watchlist kamu masih kosong. Pakai /watch <contract> buat mulai pantau token.")
        return
    teks = "WATCHLIST KAMU\n\n"
    for contract, nama, symbol, harga_awal, tp_harga, sl_harga in rows:
        teks += (
            nama + " (" + symbol + ")\n"
            "Harga saat ditambah: " + format_harga(harga_awal) + "\n"
            "Alert TP: " + format_harga(tp_harga) + " | Alert SL: " + format_harga(sl_harga) + "\n"
            "Contract: " + contract + "\n\n"
        )
    await update.message.reply_text(teks)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "scan":
        msg = await query.edit_message_text("Sedang scan token baru... Mohon tunggu!")
        try:
            tokens = await get_new_tokens()
            if not tokens:
                await msg.edit_text("Tidak ada data token baru saat ini.")
                return
            teks = "TOKEN BARU TERDETEKSI\n\n"
            count = 0
            for token in tokens[:10]:
                if count >= 5:
                    break
                try:
                    contract = token.get("tokenAddress", "")
                    chain = token.get("chainId", "")
                    if not contract:
                        continue
                    pair_data = await get_token_data(contract)
                    if not pair_data:
                        continue
                    nama = pair_data.get("baseToken", {}).get("name", "Unknown")
                    symbol = pair_data.get("baseToken", {}).get("symbol", "?")
                    harga = pair_data.get("priceUsd", "0")
                    liquidity = pair_data.get("liquidity", {}).get("usd", 0)
                    perubahan_1h = pair_data.get("priceChange", {}).get("h1", 0)
                    dex_url = pair_data.get("url", "")
                    if float(liquidity) < 5000:
                        continue
                    teks += "Token: " + nama + " (" + symbol + ")\nChain: " + chain + "\nHarga: $" + str(harga) + "\nLiquidity: $" + str(int(liquidity)) + "\nPerubahan 1H: " + str(perubahan_1h) + "%\nLink: " + dex_url + "\n\n"
                    count += 1
                except:
                    continue
            if count == 0:
                await msg.edit_text("Tidak ada token baru yang memenuhi kriteria.")
                return
            teks += "Gunakan /analisis <contract> untuk analisis AI lengkap!"
            await msg.edit_text(teks)
        except:
            await msg.edit_text("Terjadi error.")

    elif data == "trending":
        msg = await query.edit_message_text("Sedang ambil data trending... Mohon tunggu!")
        try:
            tokens = await get_trending()
            if not tokens:
                await msg.edit_text("Tidak ada data trending.")
                return
            teks = "TOKEN TRENDING SEKARANG\n\n"
            count = 0
            for token in tokens[:8]:
                if count >= 5:
                    break
                try:
                    contract = token.get("tokenAddress", "")
                    chain = token.get("chainId", "")
                    if not contract:
                        continue
                    pair_data = await get_token_data(contract)
                    if not pair_data:
                        continue
                    nama = pair_data.get("baseToken", {}).get("name", "Unknown")
                    symbol = pair_data.get("baseToken", {}).get("symbol", "?")
                    harga = pair_data.get("priceUsd", "0")
                    perubahan_1h = pair_data.get("priceChange", {}).get("h1", 0)
                    perubahan_24h = pair_data.get("priceChange", {}).get("h24", 0)
                    dex_url = pair_data.get("url", "")
                    teks += "Token: " + nama + " (" + symbol + ")\nChain: " + chain + "\nHarga: $" + str(harga) + "\nPerubahan 1H: " + str(perubahan_1h) + "%\nPerubahan 24H: " + str(perubahan_24h) + "%\nLink: " + dex_url + "\n\n"
                    count += 1
                except:
                    continue
            if count == 0:
                await msg.edit_text("Tidak ada data trending.")
                return
            await msg.edit_text(teks)
        except:
            await msg.edit_text("Terjadi error.")

    elif data == "help":
        teks = "PANDUAN CRYPTO RADAR AI\n\n/scan - Scan token baru\n/trending - Token trending\n/analisis <contract> - Analisis AI lengkap\n/cek <contract> - Cek keamanan\n\nDISCLAIMER: Bukan saran investasi!"
        await query.edit_message_text(teks)

    elif data.startswith("rugcheck|"):
        contract = data.split("|")[1]
        await query.edit_message_text("Sedang cek keamanan... Mohon tunggu!")
        try:
            rug_data = await get_rugcheck(contract)
            if not rug_data:
                await query.edit_message_text("Tidak bisa cek keamanan token ini.")
                return
            score = rug_data.get("score", "N/A")
            risks = rug_data.get("risks", [])
            teks = "HASIL CEK KEAMANAN\n\nSkor Risiko: " + str(score) + "\n\n"
            if risks:
                teks += "RISIKO:\n"
                for risk in risks[:5]:
                    teks += "- " + risk.get("name", "") + " (" + risk.get("level", "") + ")\n"
            else:
                teks += "Tidak ada risiko signifikan.\n"
            if score != "N/A":
                if int(score) < 500:
                    teks += "\nSTATUS: RELATIF AMAN"
                elif int(score) < 1000:
                    teks += "\nSTATUS: RISIKO SEDANG"
                else:
                    teks += "\nSTATUS: RISIKO TINGGI - Hindari!"
            await query.edit_message_text(teks)
        except:
            await query.edit_message_text("Terjadi error.")

    elif data.startswith("watch|"):
        contract = data.split("|")[1]
        pair_data = await get_token_data(contract)
        if not pair_data:
            await query.answer("Token tidak ditemukan.", show_alert=True)
            return
        nama = pair_data.get("baseToken", {}).get("name", "Unknown")
        symbol = pair_data.get("baseToken", {}).get("symbol", "?")
        chain = pair_data.get("chainId", "")
        try:
            harga = float(pair_data.get("priceUsd", "0"))
        except (ValueError, TypeError):
            await query.answer("Gagal baca harga token.", show_alert=True)
            return
        tp_harga = harga * 1.20
        sl_harga = harga * 0.85
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO watchlist (user_id, contract, chain, nama, symbol, harga_awal, tp_harga, sl_harga, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
            (query.from_user.id, contract, chain, nama, symbol, harga, tp_harga, sl_harga, int(time.time()))
        )
        conn.commit()
        conn.close()
        await query.answer("Ditambahkan ke watchlist! Bakal dinotif kalau harga gerak signifikan.", show_alert=True)

async def cek_riwayat_job(context: ContextTypes.DEFAULT_TYPE):
    """Jalan tiap 30 menit. Cek harga sekarang vs TP/SL yang disaranin AI dulu, update status."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, contract, tp1_harga, tp2_harga, sl_harga, timestamp FROM analisis_history WHERE status='pending'")
    rows = c.fetchall()
    conn.close()

    sekarang = int(time.time())
    for id_, contract, tp1, tp2, sl, ts in rows:
        try:
            pair_data = await get_token_data(contract)
            if not pair_data:
                continue
            harga_sekarang = float(pair_data.get("priceUsd", "0"))
            status_baru = None
            if tp2 and harga_sekarang >= tp2:
                status_baru = "tp2_hit"
            elif tp1 and harga_sekarang >= tp1:
                status_baru = "tp1_hit"
            elif sl and harga_sekarang <= sl:
                status_baru = "sl_hit"
            elif sekarang - ts > 172800:  # 48 jam tanpa hasil = dianggap kadaluarsa
                status_baru = "expired"

            if status_baru:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE analisis_history SET status=? WHERE id=?", (status_baru, id_))
                conn.commit()
                conn.close()
        except Exception:
            continue

async def cek_watchlist_job(context: ContextTypes.DEFAULT_TYPE):
    """Jalan tiap 5 menit. Cek harga token yang dipantau, kirim notif kalau kena TP/SL."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, contract, nama, symbol, tp_harga, sl_harga FROM watchlist WHERE active=1")
    rows = c.fetchall()
    conn.close()

    for id_, user_id, contract, nama, symbol, tp_harga, sl_harga in rows:
        try:
            pair_data = await get_token_data(contract)
            if not pair_data:
                continue
            harga_sekarang = float(pair_data.get("priceUsd", "0"))
            pesan = None
            if tp_harga and harga_sekarang >= tp_harga:
                pesan = (
                    "ALERT TARGET TERCAPAI!\n\n" + nama + " (" + symbol + ") udah nyentuh "
                    + format_harga(harga_sekarang) + " (target: " + format_harga(tp_harga) + ")\n\n"
                    "Token ini otomatis berhenti dipantau. Pakai /watch lagi kalau mau lanjut pantau."
                )
            elif sl_harga and harga_sekarang <= sl_harga:
                pesan = (
                    "ALERT STOP LOSS!\n\n" + nama + " (" + symbol + ") turun ke "
                    + format_harga(harga_sekarang) + " (batas: " + format_harga(sl_harga) + ")\n\n"
                    "Token ini otomatis berhenti dipantau. Pakai /watch lagi kalau mau lanjut pantau."
                )

            if pesan:
                try:
                    await context.bot.send_message(chat_id=user_id, text=pesan)
                except Exception:
                    pass  # user mungkin udah block bot, dilewati
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE watchlist SET active=0 WHERE id=?", (id_,))
                conn.commit()
                conn.close()
        except Exception:
            continue

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("trending", trending_command))
    app.add_handler(CommandHandler("analisis", analisis_command))
    app.add_handler(CommandHandler("cek", cek_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("akurasi", akurasi_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    if app.job_queue:
        app.job_queue.run_repeating(cek_riwayat_job, interval=1800, first=60)
        app.job_queue.run_repeating(cek_watchlist_job, interval=300, first=30)
    else:
        print("PERINGATAN: job_queue tidak aktif. Install dengan: pip install \"python-telegram-bot[job-queue]\"")
        print("Fitur cek otomatis riwayat & alert watchlist TIDAK akan jalan tanpa ini.")
    print("Crypto Radar AI Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
