import os
import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from groq import Groq

TOKEN = os.environ.get("8235001475:AAHcfZmSleGDXtTTj37tSSxZd9zI_F91mUY")
GROQ_API_KEY = os.environ.get("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")

groq_client = Groq(api_key=GROQ_API_KEY)

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
    url = "https://api.dexscreener.com/latest/dex/tokens/" + contract
    data = await fetch_json(url)
    if data and "pairs" in data and len(data["pairs"]) > 0:
        return data["pairs"][0]
    return None

async def get_rugcheck(contract):
    url = "https://api.rugcheck.xyz/v1/tokens/" + contract + "/report/summary"
    data = await fetch_json(url)
    return data

async def get_new_tokens():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    data = await fetch_json(url)
    return data if data else []

async def get_trending():
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    data = await fetch_json(url)
    return data if data else []

def ai_analisis(token_info):
    try:
        prompt = (
            "Kamu adalah analis crypto meme coin profesional. Analisis token berikut dan berikan penilaian singkat.\n\n"
            "Data Token:\n" + token_info + "\n\n"
            "Berikan analisis dalam format:\n"
            "SKOR KEAMANAN: (1-10)\n"
            "POTENSI PUMP: (Rendah/Sedang/Tinggi)\n"
            "RISIKO: (Rendah/Sedang/Tinggi)\n"
            "REKOMENDASI: (Beli/Hindari/Perlu Riset Lebih)\n"
            "ALASAN: (2-3 kalimat singkat)\n\n"
            "Jawab dalam Bahasa Indonesia dan singkat."
        )
        response = groq_client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        return "AI analisis tidak tersedia: " + str(e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = (
        "Selamat datang di Crypto Radar AI!\n\n"
        "Bot analisis meme coin powered by AI.\n\n"
        "PERINTAH TERSEDIA:\n\n"
        "/scan - Scan token baru\n"
        "/trending - Token yang lagi trending\n"
        "/analisis <contract> - Analisis token\n"
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
        teks += "Gunakan /analisis <contract> untuk analisis AI!"
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
        ai_result = ai_analisis(token_info)
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
            "ANALISIS AI:\n" + ai_result + "\n\n"
            "Link DEX: " + dex_url + "\n\n"
            "DISCLAIMER: Bukan saran investasi. DYOR!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Cek Keamanan", callback_data="rugcheck|" + contract)],
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
        "/analisis <contract> - Analisis AI lengkap\n"
        "/cek <contract> - Cek keamanan (Solana)\n\n"
        "TIPS TRADING AMAN:\n"
        "1. Selalu cek keamanan sebelum beli\n"
        "2. Liquidity minimal $50.000\n"
        "3. Jangan all in satu token\n"
        "4. Set target profit dan stop loss\n"
        "5. DYOR - Do Your Own Research\n\n"
        "DISCLAIMER: Bukan saran investasi!"
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
            teks += "Gunakan /analisis <contract> untuk analisis AI!"
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
        teks = "PANDUAN CRYPTO RADAR AI\n\n/scan - Scan token baru\n/trending - Token trending\n/analisis <contract> - Analisis AI\n/cek <contract> - Cek keamanan\n\nDISCLAIMER: Bukan saran investasi!"
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

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("trending", trending_command))
    app.add_handler(CommandHandler("analisis", analisis_command))
    app.add_handler(CommandHandler("cek", cek_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("Crypto Radar AI Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
