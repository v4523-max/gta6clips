# GTA 6 Twitch → Dikey Klip Pipeline (telefondan)

Videoları GitHub'ın sunucuları işler, bitince Telegram'dan telefonuna gönderir. Bilgisayar gerekmez.

## Kurulum (hepsi telefondan)
1. **Twitch:** dev.twitch.tv/console → Register Your Application
   (Redirect: `http://localhost`, Category: Application Integration, Client Type: Confidential)
   → Client ID ve New Secret al.
2. **Telegram:** @BotFather → `/newbot` → token al. Sonra kendi botuna `/start` yaz.
3. **GitHub repo** → Settings → Secrets and variables → Actions → New repository secret:
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY` (opsiyonel, Türkçe hook için)
4. **Actions** sekmesi → "I understand… enable" → GTA6 Clips → **Run workflow**.

Sonrasında her 20 dakikada kendisi çalışır. Videolar Telegram'a gelir; Telegram'dan direkt TikTok/Shorts'a paylaşırsın.

## Ayarlar (Run workflow ekranında)
- `top`: tur başına video sayısı
- `lang`: `tr` = sadece Türk yayıncılar
- `hours`: son kaç saatin klipleri

## Ne yapıyor
- Önemli an = Twitch'te GTA VI kategorisinde izleyicilerin kliplediği ve en çok izlenen anlar.
- Facecam varsa: üstte yüz, altta oyun. Yoksa: bulanık arka plan + ortada oyun.
- Kelime kelime sarı altyazı, ilk 4 saniye hook, sağ altta `twitch.tv/yayıncı` kredisi.
- Aynı klibi iki kez işlemez (`seen.json`).
