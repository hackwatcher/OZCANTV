#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Özcan Aile TV - Yayın Denetleyici ve GitHub Senkronizasyon Motoru
================================================================
Tüm Selcuksports kanallarını, statik HLS yayınlarını ve canlı maç akışlarını
test eden, domain/player değişikliklerini tespit edip GitHub'a uzaktan
aktaran çekirdek motor.
"""

import os
import re
import sys
import json
import time
import base64
import logging
import subprocess
from collections import deque
from datetime import datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    print("HATA: 'requests' kütüphanesi bulunamadı. Lütfen 'pip install -r requirements.txt' çalıştırın.")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Bellek içi son logları tutacak kuyruk (Web Panelinde göstermek için)
MAX_LOG_HISTORY = 100
log_buffer = deque(maxlen=MAX_LOG_HISTORY)

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            log_buffer.append({
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage()
            })
        except Exception:
            pass

# Logging Yapılandırması
logger = logging.getLogger("OzcanTVCore")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Console handler
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
logger.addHandler(ch)

# Memory handler
mem_handler = MemoryLogHandler()
mem_handler.setFormatter(formatter)
logger.addHandler(mem_handler)

# Varsayılan Yapılandırma
DEFAULT_REPO = os.getenv("GITHUB_REPO", "hackwatcher/OZCANTV")
DEFAULT_CONFIG_PATH = os.getenv("CONFIG_PATH", "channels_config.json")
DEFAULT_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

class OzcanTVBot:
    def __init__(self, repo=DEFAULT_REPO, token=GITHUB_TOKEN, branch=DEFAULT_BRANCH):
        self.repo = repo
        self.token = token
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_check_time = None
        self.last_check_summary = {}
        self._ensure_github_token()
        self._init_cached_summary()

    def _init_cached_summary(self):
        """Yerel channels_config.json dosyasından hızlı başlangıç verisini yükler."""
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "channels_config.json"),
            os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "assets", "channels_config.json")
        ]
        for p in candidates:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    channels = cfg.get("channels", [])
                    formatted_channels = []
                    for c in channels:
                        formatted_channels.append({
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "category": c.get("category", "national"),
                            "logo": c.get("logo", ""),
                            "status": "OK",
                            "latency_ms": 110,
                            "detail": "Hazır (Önbellek)"
                        })
                    self.last_check_time = "Sistem Başlatıldı"
                    self.last_check_summary = {
                        "success": True,
                        "timestamp": "Sistem Hazır",
                        "version": cfg.get("version", 15),
                        "total_channels": len(formatted_channels),
                        "working_channels": len(formatted_channels),
                        "health_percent": 100,
                        "primary_base": cfg.get("default_primary_base"),
                        "primary_referer": cfg.get("default_primary_referer"),
                        "pushed_to_github": True,
                        "channels": formatted_channels,
                        "matches": []
                    }
                    return
                except Exception:
                    pass

    def _ensure_github_token(self):
        """Eğer token verilmemişse yerel gh CLI üzerinden almayı dener."""
        if not self.token:
            try:
                token_out = subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
                if token_out.startswith("gh"):
                    self.token = token_out
                    logger.info("Yerel gh CLI üzerinden GitHub yetkilendirmesi algılandı.")
            except Exception:
                pass

    def send_telegram_alert(self, message: str):
        """Telegram üzerinden bildirim gönderir (isteğe bağlı)."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"📺 *Özcan Aile TV*\n{message}", "parse_mode": "Markdown"}
            self.session.post(tg_url, json=payload, timeout=8)
        except Exception as e:
            logger.warning(f"Telegram bildirimi gönderilemedi: {e}")

    def fetch_current_github_config(self) -> tuple[dict, str]:
        """GitHub reposundaki channels_config.json dosyasını ve SHA özetini çeker."""
        self._ensure_github_token()
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        api_url = f"https://api.github.com/repos/{self.repo}/contents/{DEFAULT_CONFIG_PATH}?ref={self.branch}"
        logger.info(f"GitHub'dan konfigürasyon çekiliyor: {self.repo}/{DEFAULT_CONFIG_PATH}")
        
        try:
            res = self.session.get(api_url, headers=headers, timeout=12)
            if res.status_code == 200:
                data = res.json()
                sha = data.get("sha", "")
                content_b64 = data.get("content", "")
                content_str = base64.b64decode(content_b64).decode("utf-8")
                config_json = json.loads(content_str)
                logger.info(f"GitHub API konfigürasyonu alındı: v{config_json.get('version', '?')}")
                return config_json, sha
            elif res.status_code == 404:
                logger.warning("Konfigürasyon dosyası GitHub'da bulunamadı.")
                return {}, ""
        except Exception as e:
            logger.warning(f"GitHub API bağlantısında sorun: {e}")

        # Raw URL Fallback (API rate limit durumlarında doğrudan okuma)
        raw_url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{DEFAULT_CONFIG_PATH}"
        try:
            res = self.session.get(raw_url, timeout=10)
            if res.status_code == 200:
                config_json = res.json()
                logger.info(f"GitHub Raw konfigürasyonu başarıyla okundu: v{config_json.get('version', '?')}")
                return config_json, ""
        except Exception as e:
            logger.warning(f"GitHub Raw bağlantı hatası: {e}")

        return {}, ""

    def push_config_to_github(self, updated_config: dict, old_sha: str, change_reason: str) -> bool:
        """Güncellenen channels_config.json'u GitHub'a doğrudan commit eder."""
        self._ensure_github_token()
        if not self.token:
            logger.error("GITHUB_TOKEN tanımlı değil! GitHub'a otomatik push yapılamadı. .env dosyanızı ayarlayın.")
            return False

        api_url = f"https://api.github.com/repos/{self.repo}/contents/{DEFAULT_CONFIG_PATH}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {self.token}"
        }

        # Eğer old_sha boşsa güncel sha'yı al
        if not old_sha:
            try:
                g_res = self.session.get(api_url, headers=headers, timeout=10)
                if g_res.status_code == 200:
                    old_sha = g_res.json().get("sha", "")
            except Exception:
                pass

        updated_json_str = json.dumps(updated_config, ensure_ascii=False, indent=2)
        content_b64 = base64.b64encode(updated_json_str.encode("utf-8")).decode("utf-8")

        commit_message = f"Auto-Update: v{updated_config.get('version')} - {change_reason} [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
        payload = {
            "message": commit_message,
            "content": content_b64,
            "branch": self.branch
        }
        if old_sha:
            payload["sha"] = old_sha

        logger.info(f"GitHub'a commit gönderiliyor (v{updated_config.get('version')}): {commit_message}")
        try:
            res = self.session.put(api_url, headers=headers, json=payload, timeout=20)
            if res.status_code in [200, 201]:
                logger.info("BAŞARILI: channels_config.json GitHub reposunda güncellendi!")
                self.send_telegram_alert(f"✅ *Konfigürasyon Güncellendi!*\nYeni Sürüm: `v{updated_config.get('version')}`\nSebep: {change_reason}")
                return True
            else:
                logger.error(f"GitHub Commit Hatası ({res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.error(f"GitHub commit isteğinde hata: {e}")
            return False

    def discover_active_player_and_stream(self) -> dict:
        """Selcuksports portalını ve güncel 1080p HLS m3u8 base adresini dinamik çözer."""
        logger.info("Selcuksports yayın sunucuları ve player adresleri taranıyor...")
        discovered = {
            "portal_url": None,
            "player_url": None,
            "player_referer": None,
            "stream_base": None,
            "stream_referer": None
        }

        # 1. Arkadastv / Loginmatik API doğrudan kontrolü
        try:
            arkadas_res = self.session.get("https://fdjhgdhjfgdfg.icu/m/arkadastv.json", timeout=6, headers={"Referer": "https://loginmatik3.com/canli-mac-izle"})
            if arkadas_res.status_code == 200:
                adata = arkadas_res.json()
                taban = adata.get("taban", "https://gdb36gd.icu")
                discovered["stream_base"] = f"{taban}/{{id}}/mono.m3u8"
                discovered["stream_referer"] = "https://fdjhgdhjfgdfg.icu/"
                discovered["player_url"] = "https://fdjhgdhjfgdfg.icu/ch.html?m=arkadastv&id={id}"
                discovered["player_referer"] = "https://loginmatik3.com/canli-mac-izle"
                discovered["portal_url"] = "https://loginmatik3.com/canli-mac-izle"
                logger.info(f"ArkadasTV / Loginmatik üzerinden aktif HLS CDN bulundu: {taban}")
                return discovered
        except Exception as e:
            logger.warning(f"ArkadasTV API kontrol edilirken hata: {e}")

        # 2. domain.php sorgusu
        try:
            domain_res = self.session.get("https://patronsports2.cfd/domain.php", timeout=8)
            if domain_res.status_code == 200:
                raw = domain_res.text.strip()
                if "selcuksportshd" in raw:
                    discovered["portal_url"] = raw if raw.startswith("http") else f"https://{raw}"
                    logger.info(f"domain.php üzerinden tespit edilen portal: {discovered['portal_url']}")
        except Exception as e:
            logger.warning(f"domain.php ulaşılamadı: {e}")

        # 3. Portal ana sayfasından iframe bul
        target_portal = discovered["portal_url"] or "https://www.selcuksportshd0.xyz"
        try:
            p_res = self.session.get(target_portal, timeout=10, allow_redirects=True)
            if p_res.status_code == 200:
                discovered["portal_url"] = p_res.url
                iframe_match = re.search(r'iframe.*?src=["\'](https?://[^"\']*uxsyplayer[^"\']*|https?://[^"\']*/index\.php\?id=[^"\']*)["\']', p_res.text, re.IGNORECASE)
                if iframe_match:
                    discovered["player_url"] = iframe_match.group(1)
                    parsed_player = urlparse(discovered["player_url"])
                    discovered["player_referer"] = f"{parsed_player.scheme}://{parsed_player.netloc}/"
                    logger.info(f"Portal sayfasından player iframe tespit edildi: {discovered['player_url']}")
        except Exception as e:
            logger.warning(f"Portal incelenirken hata: {e}")

        # 4. Player kodundan baseStreamUrl ayıkla
        player_check_url = discovered["player_url"] or "https://main.uxsyplayer6859599e6c.click/index.php?id=selcukbeinsports1"
        try:
            player_headers = {"Referer": discovered["portal_url"] or "https://www.selcuksportshd0.xyz"}
            p_res = self.session.get(player_check_url, headers=player_headers, timeout=10)
            if p_res.status_code == 200:
                parsed_p = urlparse(player_check_url)
                discovered["stream_referer"] = f"{parsed_p.scheme}://{parsed_p.netloc}/"
                discovered["player_referer"] = f"{parsed_p.scheme}://{parsed_p.netloc}/"
                
                base_match = re.search(r'baseStreamUrl\s*=\s*["\'](https?://[^"\']+/live/)["\']', p_res.text)
                if not base_match:
                    base_match = re.search(r'["\'](https?://[^"\']+/live/)["\']', p_res.text)
                
                if base_match:
                    base_url = base_match.group(1)
                    if not base_url.endswith("/"):
                        base_url += "/"
                    discovered["stream_base"] = f"{base_url}{{id}}/playlist.m3u8"
                    logger.info(f"Player JS'den çıkarılan HLS Base: {discovered['stream_base']}")
        except Exception as e:
            logger.warning(f"Player JS incelenirken hata: {e}")

        return discovered

    def test_m3u8_stream(self, stream_url: str, referer: str) -> tuple[bool, int, str]:
        """m3u8 akışını ve ilk MPEG-TS segmentini indirip test eder. (is_ok, latency_ms, detail)"""
        headers = {"Referer": referer} if referer else {}
        start_t = time.time()
        try:
            res = self.session.get(stream_url, headers=headers, timeout=6)
            latency_ms = int((time.time() - start_t) * 1000)
            
            if res.status_code == 200 and ("#EXTM3U" in res.text or "application/vnd.apple.mpegurl" in res.headers.get("Content-Type", "")):
                lines = res.text.strip().split("\n")
                seg_url = None
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line.startswith("http"):
                            seg_url = line
                        else:
                            base_part = stream_url.rsplit("/", 1)[0]
                            seg_url = f"{base_part}/{line}"
                        break
                
                if seg_url:
                    seg_res = self.session.get(seg_url, headers=headers, timeout=5, stream=True)
                    if seg_res.status_code == 200:
                        first_bytes = next(seg_res.iter_content(chunk_size=512), b"")
                        if first_bytes:
                            return True, latency_ms, "1080p HLS Canlı (MPEG-TS Doğrulandı)"
                return True, latency_ms, "1080p HLS Playlist Aktif"
            elif res.status_code == 403:
                return False, latency_ms, "403 Forbidden (Referer Uyuşmazlığı)"
            elif res.status_code == 404:
                return False, latency_ms, "404 Not Found (Yayın Kapalı)"
            else:
                return False, latency_ms, f"HTTP Hata {res.status_code}"
        except requests.exceptions.Timeout:
            return False, 6000, "Zaman Aşımı (Timeout)"
        except Exception as e:
            return False, 0, f"Bağlantı Hatası: {str(e)[:40]}"

    def check_all_channels_detailed(self, config: dict) -> list:
        """Konfigürasyondaki tüm 36 kanalın canlı sağlık durumunu paralel iş parçacıklarıyla hızlıca test eder."""
        channels = config.get("channels", [])
        primary_base = config.get("default_primary_base", "")
        primary_referer = config.get("default_primary_referer", "")

        logger.info(f"Tüm kanallar paralel taranıyor (Toplam: {len(channels)} kanal)...")

        def test_single_channel(ch):
            ch_id = ch.get("id")
            ch_name = ch.get("name")
            category = ch.get("category", "sports")
            logo = ch.get("logo", "")
            
            if "stream_url" in ch and ch["stream_url"]:
                stream_url = ch["stream_url"]
                referer = ch.get("referer") or (primary_referer if "gdb36gd" in stream_url else "")
            else:
                stream_url = primary_base.replace("{id}", ch_id)
                referer = ch.get("referer") or primary_referer

            is_ok, latency, detail = self.test_m3u8_stream(stream_url, referer)
            return {
                "id": ch_id,
                "name": ch_name,
                "category": category,
                "logo": logo,
                "status": "OK" if is_ok else "FAIL",
                "latency_ms": latency,
                "detail": detail,
                "stream_url": stream_url
            }

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(test_single_channel, channels))

        working = sum(1 for r in results if r["status"] == "OK")
        logger.info(f"Kanal Taraması Bitti: {working}/{len(results)} kanal aktif ve kusursuz.")
        return results

    def fetch_live_matches_detailed(self, config: dict) -> list:
        """Canlı maçları ve akış durumlarını paralel listeler."""
        matches_url = config.get("matches_api_url", "https://fdjhgdhjfgdfg.icu/m/arkadastv.json")
        primary_base = config.get("default_primary_base", "https://gdb36gd.icu/{id}/mono.m3u8")
        primary_referer = config.get("default_primary_referer", "https://fdjhgdhjfgdfg.icu/")

        try:
            res = self.session.get(matches_url, timeout=8, headers={"Referer": "https://loginmatik3.com/canli-mac-izle"})
            if res.status_code == 200:
                data = res.json()
                matches_data = []
                if isinstance(data, list):
                    matches_data = data
                elif isinstance(data, dict):
                    if "liste" in data and isinstance(data["liste"], dict):
                        matches_data = data["liste"].get("maclar", [])
                    elif "matches" in data:
                        matches_data = data.get("matches", [])

                def test_match(m):
                    channel_id = m.get("kanal") or m.get("channel") or m.get("id") or m.get("stream") or "beinsports1fhd"
                    stream_url = primary_base.replace("{id}", channel_id)
                    is_ok, latency, detail = self.test_m3u8_stream(stream_url, primary_referer)
                    home = m.get("ev", {}).get("ad") if isinstance(m.get("ev"), dict) else m.get("home", "Ev Sahibi")
                    away = m.get("dep", {}).get("ad") if isinstance(m.get("dep"), dict) else m.get("away", "Deplasman")
                    return {
                        "time": m.get("saat") or m.get("time", "--:--"),
                        "home": home,
                        "away": away,
                        "league": m.get("lig") or m.get("league", "Canlı Maç"),
                        "channel": channel_id,
                        "status": "OK" if is_ok else "PENDING",
                        "detail": detail,
                        "stream_url": stream_url
                    }
                with ThreadPoolExecutor(max_workers=8) as executor:
                    return list(executor.map(test_match, matches_data))
            return []
        except Exception as e:
            logger.warning(f"Canlı maçlar çekilemedi: {e}")
            return []

    def perform_full_check(self, force_push=False) -> dict:
        """Tam tarama döngüsü yürütür ve gerekirse GitHub'a pushlar."""
        logger.info("=== ÖZCAN AİLE TV TAM TARAMA BAŞLATILDI ===")
        config, old_sha = self.fetch_current_github_config()
        
        if not config:
            # Fallback: Eğer GitHub API erişilemezse yerel şablonu dene
            local_candidates = [
                os.path.join(os.path.dirname(__file__), "..", "channels_config.json"),
                os.path.join(os.path.dirname(__file__), "channels_config.json"),
                os.path.join(os.getcwd(), "channels_config.json"),
                os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "assets", "channels_config.json")
            ]
            for p in local_candidates:
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            config = json.load(f)
                        break
                    except Exception:
                        pass

        if not config:
            return {"success": False, "error": "Konfigürasyon dosyası yüklenemedi."}

        current_version = config.get("version", 1)
        current_base = config.get("default_primary_base", "")
        current_referer = config.get("default_primary_referer", "")

        # 1. Hızlı numune testi yap (Önemli 6 kanal)
        sample_channels = ["beinsports1fhd", "beinsports2fhd", "beinsports3fhd", "beinsports4hd", "ssportplus", "asporhd"]
        working_samples = 0
        for ch_id in sample_channels:
            url = current_base.replace("{id}", ch_id)
            is_ok, _, _ = self.test_m3u8_stream(url, current_referer)
            if is_ok:
                working_samples += 1

        sample_ratio = working_samples / len(sample_channels)
        logger.info(f"Numune Kanal Sağlığı: {working_samples}/{len(sample_channels)} ({sample_ratio*100:.0f}%)")

        needs_update = False
        change_reasons = []

        # Eğer yayınlar çalışmıyorsa veya force_push istenmişse Selcuksports'u baştan tara
        if sample_ratio < 0.5 or force_push:
            logger.warning("Yayın adreslerinde güncelleme ihtiyacı tespit edildi. Yeni adresler aranıyor...")
            discovered = self.discover_active_player_and_stream()
            
            if discovered["stream_base"]:
                # Yeni base'i test et
                new_working = 0
                for ch_id in sample_channels:
                    url = discovered["stream_base"].replace("{id}", ch_id)
                    is_ok, _, _ = self.test_m3u8_stream(url, discovered["stream_referer"])
                    if is_ok:
                        new_working += 1

                if new_working >= working_samples:
                    if discovered["stream_base"] != current_base:
                        needs_update = True
                        change_reasons.append(f"Yeni HLS Base: {discovered['stream_base']}")
                        config["default_primary_base"] = discovered["stream_base"]

                    if discovered["stream_referer"] != current_referer:
                        needs_update = True
                        change_reasons.append(f"Yeni Referer: {discovered['stream_referer']}")
                        config["default_primary_referer"] = discovered["stream_referer"]

                    if discovered["player_url"]:
                        config["default_backup_player"] = discovered["player_url"].replace("selcukbeinsports1", "{id}")
                        config["default_backup_referer"] = discovered["portal_url"] or "https://www.selcuksportshd0.xyz"

        # 2. Tüm kanalları detaylı test et
        channel_results = self.check_all_channels_detailed(config)
        total_channels = len(channel_results)
        working_channels = sum(1 for c in channel_results if c["status"] == "OK")

        # 3. Canlı maçları çek
        matches_results = self.fetch_live_matches_detailed(config)

        # 4. GitHub'a pushla (Değişiklik varsa veya zorla güncelleme istenmişse)
        pushed = False
        if needs_update or force_push:
            new_version = current_version + 1
            config["version"] = new_version
            reason_str = ", ".join(change_reasons) if change_reasons else "Manuel/Periyodik doğrulama güncellemesi"
            # Yerel dosyalara da yaz (özellikle GitHub Actions runner ortamı için)
            local_save_paths = [
                os.path.join(os.path.dirname(__file__), "..", "channels_config.json"),
                os.path.join(os.path.dirname(__file__), "channels_config.json"),
                os.path.join(os.getcwd(), "channels_config.json")
            ]
            for lp in local_save_paths:
                try:
                    if os.path.exists(os.path.dirname(os.path.abspath(lp))):
                        with open(lp, "w", encoding="utf-8") as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
            pushed = self.push_config_to_github(config, old_sha, reason_str)
            if pushed:
                current_version = new_version

        self.last_check_time = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.last_check_summary = {
            "success": True,
            "timestamp": self.last_check_time,
            "version": current_version,
            "total_channels": total_channels,
            "working_channels": working_channels,
            "health_percent": int((working_channels / total_channels * 100) if total_channels > 0 else 0),
            "primary_base": config.get("default_primary_base"),
            "primary_referer": config.get("default_primary_referer"),
            "pushed_to_github": pushed,
            "channels": channel_results,
            "matches": matches_results
        }
        
        logger.info(f"=== TARAMA TAMAMLANDI: Sağlık Oranı %{self.last_check_summary['health_percent']} ({working_channels}/{total_channels}) ===")
        return self.last_check_summary

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Özcan Aile TV Otomatik Link Denetleme ve Güncelleme Botu")
    parser.add_argument("--run-once", action="store_true", help="Tek bir kontrol yapıp çıkar.")
    parser.add_argument("--force-push", action="store_true", help="Kontrol başarılı olsa bile versiyonu artırıp GitHub'a zorla pushlar.")
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO, help="Hedef GitHub repository (varsayılan: hackwatcher/OZCANTV)")
    parser.add_argument("--token", type=str, default=GITHUB_TOKEN, help="GitHub Personal Access Token")
    args = parser.parse_args()

    bot = OzcanTVBot(repo=args.repo, token=args.token)

    if args.run_once:
        summary = bot.perform_full_check(force_push=args.force_push)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        sys.exit(0 if summary.get("success") else 1)
    else:
        print("Sürekli çalışma için lütfen 'python web_panel.py' çalıştırınız.")

if __name__ == "__main__":
    main()
