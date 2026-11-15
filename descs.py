#!/usr/bin/env python3
import time
import random
from bs4 import BeautifulSoup
import os
import signal
import sys
import atexit
import json
import configparser
import re
import requests
import threading
from typing import Optional, Tuple, List
from pathlib import Path
from DrissionPage import ChromiumPage, ChromiumOptions

SITE_BASE_URL = "https://igrushki7.ua"


def _img_basename(path_or_url: str) -> str:
    """Return image basename without extension, ignoring querystring."""
    if not path_or_url:
        return ""
    s = path_or_url.strip()
    s = s.split('?', 1)[0].split('#', 1)[0]
    s = s.replace('\\', '/')
    return os.path.splitext(os.path.basename(s))[0]


def normalize_site_image_url(parsed_src: str) -> str:
    """Convert relative/./images paths to absolute site URL."""
    if not parsed_src:
        return ""
    s = parsed_src.strip()
    if s.startswith('http://') or s.startswith('https://'):
        return s
    if s.startswith('./'):
        s = s[1:]
    if s.startswith('/'):
        return f"{SITE_BASE_URL}{s}"
    # e.g. images/...
    if s.startswith('images/'):
        return f"{SITE_BASE_URL}/{s}"
    return s


def replace_url_filename_keep_dir(existing_url: str, parsed_src: str, *, strip_first_suffix_s: bool = False) -> str:
    """Keep directory + extension from existing_url, replace only basename using parsed_src.

    This is used to keep 'prom/promadd' non-watermarked directories while updating
    the filename based on what we parse from the product page.

    Args:
        existing_url: URL already in XML (preferred non-watermarked directory)
        parsed_src: image src/href parsed from page
        strip_first_suffix_s: if True and parsed basename ends with '_s', remove it
            (requirement: for the FIRST parsed image only).
    """
    if not existing_url:
        return normalize_site_image_url(parsed_src)

    ex = existing_url.strip().split('?', 1)[0].split('#', 1)[0]
    pr = (parsed_src or '').strip().split('?', 1)[0].split('#', 1)[0]
    if not pr:
        return existing_url

    ex = ex.replace('\\', '/')
    pr = pr.replace('\\', '/')

    # Split existing into dir + ext
    ex_dir = ex.rsplit('/', 1)[0] if '/' in ex else ''
    ex_file = ex.rsplit('/', 1)[1] if '/' in ex else ex
    ex_ext = os.path.splitext(ex_file)[1]

    pr_base = _img_basename(pr)
    if strip_first_suffix_s and pr_base.endswith('_s'):
        pr_base = pr_base[:-2]
    if not pr_base:
        return existing_url

    new_file = f"{pr_base}{ex_ext}" if ex_ext else pr_base

    if ex_dir:
        return f"{ex_dir}/{new_file}"
    return new_file


def images_match_by_basename(parsed_src: str, existing_url: str) -> bool:
    """Compare by basename, allowing a trailing '_s' on parsed image name."""
    p = _img_basename(parsed_src)
    e = _img_basename(existing_url)
    if not p or not e:
        return False
    if p == e:
        return True
    if p.endswith('_s') and p[:-2] == e:
        return True
    return False

# --- Настройки путей ---
DOWNLOADS_DIR = Path.home() / "Downloads"
if not DOWNLOADS_DIR.exists():
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

def get_app_dir():
    if sys.platform == "win32":
        return Path(os.getenv("APPDATA")) / "ProductParser"
    return Path.home() / ".product_parser"

APP_DIR = get_app_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = APP_DIR / "user_settings.ini"

PARSED_FILE = 'parsed_descs.xml'
EXPORT_FILE_UA = 'parsed_descs_ua.xml'
EXPORT_FILE_RU = 'parsed_descs_ru.xml'

def load_offer_ids_from_xml(filepath: str) -> set[str]:
    """Load offer IDs from an XML/YML file.

    Used to compare the currently uploaded input file with PARSED_FILE.
    """
    if not filepath or not os.path.exists(filepath):
        return set()
    try:
        soup = BeautifulSoup(open(filepath, encoding='utf-8'), 'xml')
        ids: set[str] = set()
        for offer in soup.find_all('offer'):
            oid = get_offer_id(offer)
            if oid:
                ids.add(oid)
        return ids
    except Exception:
        return set()

_old_soup_backup = None
_parsed_offers_backup = []
_driver = None
_driver_config = {"proxy": None, "headless": False, "user_agent": None}

def load_settings():
    defaults = {"proxy": "", "xml_path": "", "headless": False, "user_agent": "", "deepseek_api_key": "", "deepseek_balance": "0.00", "check_pictures": False}
    try:
        json_path = Path(__file__).resolve().parent / "user_settings.json"
        if json_path.exists() and not SETTINGS_FILE.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                save_settings(data)
            except Exception:
                pass

        if SETTINGS_FILE.exists():
            cfg = configparser.ConfigParser()
            cfg.read(SETTINGS_FILE, encoding='utf-8')
            if 'settings' in cfg:
                s = cfg['settings']
                return {
                    'proxy': s.get('proxy', defaults['proxy']),
                    'xml_path': s.get('xml_path', defaults['xml_path']),
                    'headless': s.getboolean('headless', fallback=defaults['headless']),
                    'user_agent': s.get('user_agent', defaults['user_agent']),
                    'deepseek_api_key': s.get('deepseek_api_key', defaults['deepseek_api_key']),
                    'deepseek_balance': s.get('deepseek_balance', defaults['deepseek_balance']),
                    'check_pictures': s.getboolean('check_pictures', fallback=defaults['check_pictures'])
                }
        return defaults
    except Exception:
        return defaults

def save_settings(settings: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg['settings'] = {
        'proxy': settings.get('proxy', '') or '',
        'xml_path': settings.get('xml_path', '') or '',
        'headless': str(bool(settings.get('headless', False))),
        'user_agent': settings.get('user_agent', '') or '',
        'deepseek_api_key': settings.get('deepseek_api_key', '') or '',
        'deepseek_balance': settings.get('deepseek_balance', '') or '0.00',
        'check_pictures': str(bool(settings.get('check_pictures', False))),
    }
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        cfg.write(f)

_SETTINGS = load_settings()
NEW_FILE = (str(Path(_SETTINGS.get("xml_path")).expanduser()) if _SETTINGS.get("xml_path")
            else str(Path(__file__).resolve().parent / 'export_yml.yml.xml'))

def init_global_driver(proxy: Optional[str] = None, headless: bool = False, user_agent: Optional[str] = None):
    global _driver, _driver_config
    
    if user_agent and not user_agent.strip():
        user_agent = None

    if _driver is not None:
        if (_driver_config.get("proxy") == (proxy or None) and 
            _driver_config.get("headless") == bool(headless) and
            _driver_config.get("user_agent") == user_agent):
            return _driver
        
        try: 
            _driver.quit()
        except: pass
        _driver = None

    co = ChromiumOptions()
    co.set_argument('--start-minimized')
    co.set_argument('--window-position=-2000,-2000')
    co.set_argument('--disable-notifications')
    co.set_argument('--disable-infobars')
    co.set_argument('--disable-extensions')

    if headless:
        co.headless(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')

    if proxy:
        co.set_proxy(f'http://{proxy}')

    if user_agent:
        co.set_user_agent(user_agent)

    print(f"Запуск браузера (UA: {'Custom' if user_agent else 'System'})...")
    try:
        _driver = ChromiumPage(co)
        _driver_config["proxy"] = proxy or None
        _driver_config["headless"] = bool(headless)
        _driver_config["user_agent"] = user_agent
        return _driver
    except Exception as e:
        print(f"Ошибка при запуске браузера: {e}")
        raise

def cleanup_global_driver():
    global _driver
    if _driver:
        try:
            _driver.quit()
        except: pass
        finally: _driver = None

def get_offer_id(offer) -> Optional[str]:
    offer_id = offer.get('id')
    return offer_id if offer_id else (offer.find('id').get_text().strip() if offer.find('id') else None)

def solve_cloudflare(tab):
    try:
        challenge = tab.ele('@class:challenge', timeout=1) 
        if challenge:
            print("Попытка клика по защите...")
            challenge.click()
            time.sleep(2)
            return

        iframe = tab.ele('tag:iframe', timeout=1)
        if iframe:
            body = iframe.ele('tag:body', timeout=1)
            if body and "verify" in body.text.lower():
                print("Найдена капча во фрейме, кликаем...")
                body.click()
                time.sleep(2)
    except Exception:
        pass

def fetch_html_via_chrome(url: str, timeout: float = 30.0, proxy: Optional[str] = None, headless: bool = False, user_agent: Optional[str] = None) -> Optional[str]:
    global _driver
    try:
        if _driver is None:
            _driver = init_global_driver(proxy=proxy, headless=headless, user_agent=user_agent)

        tab = _driver.new_tab(url)
        
        start_time = time.time()
        while time.time() - start_time < 30:
            title = tab.title.lower()
            if "just a moment" in title or "challenge" in title or "attention" in title:
                print("Обнаружена защита. Пробую пройти...")
                solve_cloudflare(tab)
                time.sleep(2)
            else:
                break
        
        tab.wait.load_start()
        time.sleep(1) 
        
        html = tab.html
        tab.close()
        return html

    except Exception as e:
        print(f'Ошибка загрузки {url}: {e}')
        return None

def load_parsed_ids(filepath: str) -> set[str]:
    if not os.path.exists(filepath):
        return set()
    try:
        soup = BeautifulSoup(open(filepath, encoding='utf-8'), 'xml')
        return {get_offer_id(offer) for offer in soup.find_all('offer') if get_offer_id(offer)}
    except Exception:
        return set()

def emergency_save():
    global _old_soup_backup, _parsed_offers_backup
    if not _old_soup_backup or not _parsed_offers_backup:
        return
    try:
        container = _old_soup_backup.find('offers') or _old_soup_backup.find('shop') or _old_soup_backup
        for offer in _parsed_offers_backup:
            offer_copy = BeautifulSoup(str(offer), 'xml').find('offer')
            if offer_copy:
                container.append(offer_copy)
        
        xml_str = str(_old_soup_backup).replace('</offer>', '</offer>\n')
        with open(PARSED_FILE, "w", encoding="utf-8") as f:
            f.write(xml_str)
        print(f"Экстренное сохранение: {len(_parsed_offers_backup)} товаров.")
    except Exception as e:
        print(f'Ошибка сохранения: {e}')

def wrap_descriptions_in_cdata(filepath: str = PARSED_FILE) -> Tuple[bool, str]:
    """
    Обгортає вміст тегів <description> та <description_ua> у CDATA
    якщо вони ще не обгорнуті.
    
    Args:
        filepath: шлях до XML файлу
    
    Returns:
        Tuple[bool, str]: (успішність операції, повідомлення)
    """
    if not os.path.exists(filepath):
        return False, f"Файл {filepath} не знайдено"
    
    try:
        # Читання файлу як текст (не парсимо через BeautifulSoup!)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Лічильники
        desc_wrapped = 0
        desc_ua_wrapped = 0
        
        # Функція для обгортання одного тегу
        def wrap_tag(match):
            nonlocal desc_wrapped, desc_ua_wrapped
            tag_name = match.group(1)  # 'description' або 'description_ua'
            inner_content = match.group(2)  # вміст між тегами
            
            # Перевіряємо чи вже обгорнуто в CDATA
            if inner_content.strip().startswith('<![CDATA['):
                return match.group(0)  # повертаємо без змін
            
            # Обгортаємо в CDATA
            if tag_name == 'description':
                desc_wrapped += 1
            else:
                desc_ua_wrapped += 1
            
            return f'<{tag_name}><![CDATA[{inner_content}]]></{tag_name}>'
        
        # Обробляємо <description>...</description>
        content = re.sub(
            r'<(description)>(.*?)</\1>',
            wrap_tag,
            content,
            flags=re.DOTALL
        )
        
        # Обробляємо <description_ua>...</description_ua>
        content = re.sub(
            r'<(description_ua)>(.*?)</\1>',
            wrap_tag,
            content,
            flags=re.DOTALL
        )

        # ===== Ensure iframe/html is not entity-escaped inside CDATA =====
        # When descriptions are built via BeautifulSoup and then serialized as XML,
        # embedded HTML (like <iframe>) often becomes &lt;iframe ...&gt;.
        # User requirement: iframe must be written as a real tag inside CDATA.
        def _unescape_inside_cdata(match: re.Match) -> str:
            tag_name = match.group(1)
            inner = match.group(2)
            inner = inner.replace('&lt;', '<').replace('&gt;', '>')
            return f'<{tag_name}><![CDATA[{inner}]]></{tag_name}>'

        content = re.sub(
            r'<(description|description_ua)>\s*<!\[CDATA\[(.*?)\]\]>\s*</\1>',
            _unescape_inside_cdata,
            content,
            flags=re.DOTALL,
        )

        # Ensure footnote paragraph lines starting with '*' are moved to end in both description fields.
        # Also remove duplicated fragments when one paragraph contains all earlier paragraphs.
        def _strip_p_text(p_tag: str) -> str:
            inner = re.sub(r'^<p>\s*', '', p_tag.strip())
            inner = re.sub(r'\s*</p>$', '', inner.strip())
            return inner

        def _normalize_txt(txt: str) -> str:
            txt = re.sub(r'<.*?>', '', txt)
            txt = txt.replace('•', ' ')
            txt = re.sub(r'[^0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ\s\.:]', ' ', txt)
            txt = re.sub(r'\s+', ' ', txt.strip())
            return txt.lower().rstrip('.').strip()

        def _reorder_footnote_in_cdata(match: re.Match) -> str:
            tag_name = match.group(1)
            inner = match.group(2)
            p_tags = re.findall(r'<p>.*?</p>', inner, flags=re.DOTALL)
            if not p_tags:
                return match.group(0)

            p_lines = []
            for p in p_tags:
                txt = _strip_p_text(p)
                norm = _normalize_txt(txt)
                p_lines.append((p, txt, norm))

            # Keep non-footnotes first, then footnotes.
            normal = [item for item in p_lines if not item[1].strip().startswith('*')]
            footnotes = [item for item in p_lines if item[1].strip().startswith('*')]

            # Remove duplicated non-footnote lines when one line contains all earlier ones.
            keep = [True] * len(normal)
            for i in range(len(normal)):
                if not keep[i]:
                    continue
                for j in range(i + 1, len(normal)):
                    if not keep[j]:
                        continue
                    if normal[i][2] and normal[i][2] in normal[j][2] and len(normal[j][2]) > len(normal[i][2]) + 5:
                        keep[i] = False
                        break

            deduped_normal = [normal[i][0] for i in range(len(normal)) if keep[i]]
            if not deduped_normal and normal:
                deduped_normal = [normal[-1][0]]

            reordered = '\n'.join(deduped_normal + [item[0] for item in footnotes])
            return f'<{tag_name}><![CDATA[{reordered}]]></{tag_name}>'

        content = re.sub(
            r'<(description|description_ua)>\s*<!\[CDATA\[(.*?)\]\]>\s*</\1>',
            _reorder_footnote_in_cdata,
            content,
            flags=re.DOTALL,
        )

        # Збереження результату
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        if desc_wrapped == 0 and desc_ua_wrapped == 0:
            return True, "Всі описи вже обгорнуті в CDATA"
        message = f"✓ Обгорнуто в CDATA:\n  • <description>: {desc_wrapped}\n  • <description_ua>: {desc_ua_wrapped}"
        return True, message
        
    except Exception as e:
        return False, f"✗ Помилка обгортання в CDATA: {str(e)}"

def call_deepseek_api(product_name: str, api_key: str, timeout: float = 30.0) -> Optional[str]:
    if not api_key or not api_key.strip():
        return None
    
    try:
        api_url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}"
        }
        
        prompt = f"""Ты обрабатываешь название товара. Из названия нужно извлечь артикул товара.

ВАЖНЫЕ ПРАВИЛА:
1. Возвращай ОБЯЗАТЕЛЬНО валидный JSON
2. JSON должен быть В ТОЧНОСТИ такого формата:
{{
    "article": "АРТИКУЛ"
}}

3. Артикул - это ЛЮБОЙ код/модель товара, обычно состоит из букв, цифр, тире.
4. Ищи ЛЮБЫЕ буквенно-цифровые коды: "26166 NP", "48402 NP", "52548", "933-31 E", "C 62140", "ZG 3002-D", "TSQ-55", "999 B 82A", "FXB 50", "AH 1234-E"
5. Артикули могут быть: 
   - В начале названия
   - В конце названия
   - Посередине названия
   - На английском, украинском или русском языке
6. Если артикул не найден, пиши: "article": ""
7. НЕ добавляй никакого текста кроме JSON!
8. Убедись, что в JSON нет синтаксических ошибок!

Примеры артикулей (найди похожие в названии):
- Буквы+цифры+тире: "TSQ-55", "FXB-50", "ZG 3002-D"
- Цифры+буквы+цифры: "999 B 82A"
- Просто буквы и цифры подряд: "FXB50", "TSQ55"
- Модельные коды: "28108 NP", "52548"

Название товара:
{product_name}

Верни ТОЛЬКО JSON с извлеченным артиклем!"""
        
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "system",
                    "content": "Ты опытный обработчик данных. Твоя задача - извлекать артикулы товаров и возвращать результат в ВАЛИДНОМ JSON формате. НИКАКОГО дополнительного текста, ТОЛЬКО JSON!"
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 6000,
            "stream": False
        }
        
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                content = data['choices'][0].get('message', {}).get('content', '')
                article = extract_article_from_response(content)
                return article if article else None
        
        return None
        
    except requests.exceptions.Timeout:
        print(f"⏱ DeepSeek API таймаут для: {product_name[:50]}")
        return None
    except Exception as e:
        print(f"❌ Ошибка DeepSeek API: {str(e)[:100]}")
        return None

def extract_article_from_response(content: str) -> Optional[str]:
    try:
        json_start = content.find('{')
        json_end = content.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = content[json_start:json_end]
            json_str = json_str.replace(',}', '}')
            json_str = json_str.replace(',]', ']')
            json_str = json_str.replace("'", '"')
            
            data = json.loads(json_str)
            article = data.get('article', '').strip()
            return article if article else None
    
    except Exception as e:
        pass
    
    return None

def extract_article_local(product_name: str) -> Optional[str]:
    patterns = [
        r'\b([A-Z]{2,}\s+\d{2,}-\d+\s*[A-Z]?)\b',
        r'\b(\d{4,}\s+[A-Z]{2,})\b',
        r'\b(\d{2,}-\d+\s*[A-Z]?)\b',
        r'\b([A-Z]\s+\d{4,})\b',
        r'\b([A-Z]{2,}\s+\d{4,})\b',
        r'\b(\d{4,}[A-Z]{2,})\b',
        r'(?<!\d)(\d{4,})(?!\d)',
        r'\b([A-Z]+\d{3,})\b',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, product_name)
        if matches:
            return matches[0].strip()
    
    return None

def translate_text_deepseek(text: str, api_key: str, timeout: float = 30.0) -> Optional[str]:
    if not api_key or not api_key.strip() or not text or not text.strip():
        return None
    
    try:
        api_url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}"
        }
        
        prompt = f"""Переведи следующее описание товара с русского на украинский язык.

ВАЖНЫЕ ПРАВИЛА:
1. Переводи только текст, сохрани его структуру
2. Каждый абзац (предложение, строка) переводи отдельно
3. Не добавляй никаких предисловий, только перевод
4. Сохрани точный смысл и технические термины
5. Если есть артикули/коды, оставляй их как есть

Текст для перевода:
{text}

Верни ТОЛЬКО переведенный текст, без дополнительных комментариев!"""
        
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "system",
                    "content": "Ты профессиональный переводчик. Твоя задача - точно переводить описания товаров с русского на украинский язык. Возвращай ТОЛЬКО переведенный текст без комментариев!"
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
            "stream": False
        }
        
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                translated = data['choices'][0].get('message', {}).get('content', '').strip()
                return translated if translated else None
        
        return None
        
    except requests.exceptions.Timeout:
        print(f"⏱ DeepSeek API таймаут при переводе")
        return None
    except Exception as e:
        print(f"❌ Ошибка DeepSeek API при переводе: {str(e)[:100]}")
        return None

def translate_text_deepseek_to_russian(text: str, api_key: str, timeout: float = 30.0) -> Optional[str]:
    if not api_key or not api_key.strip() or not text or not text.strip():
        return None
    
    try:
        api_url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}"
        }
        
        prompt = f"""Переведи следующее описание товара с украинского на русский язык.

ВАЖНЫЕ ПРАВИЛА:
1. Переводи только текст, сохрани его структуру
2. Каждый абзац (предложение, строка) переводи отдельно
3. Не добавляй никаких предисловий, только перевод
4. Сохрани точный смысл и технические термины
5. Если есть артикули/коды, оставляй их как есть

Текст для перевода:
{text}

Верни ТОЛЬКО переведенный текст, без дополнительных комментариев!"""
        
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "system",
                    "content": "Ты профессиональный переводчик. Твоя задача - точно переводить описания товаров с украинского на русский язык. Возвращай ТОЛЬКО переведенный текст без комментариев!"
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
            "stream": False
        }
        
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                translated = data['choices'][0].get('message', {}).get('content', '').strip()
                return translated if translated else None
        
        return None
        
    except requests.exceptions.Timeout:
        print(f"⏱ DeepSeek API таймаут при переводе на русский")
        return None
    except Exception as e:
        print(f"❌ Ошибка DeepSeek API при переводе на русский: {str(e)[:100]}")
        return None

def get_deepseek_balance(api_key: str, timeout: float = 10.0) -> Optional[dict]:
    if not api_key or not api_key.strip():
        return None
    
    try:
        api_url = "https://api.deepseek.com/v1/user/balance"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(
            api_url,
            headers=headers,
            timeout=timeout
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if 'balance_infos' in data and data['balance_infos']:
                balance_info = data['balance_infos'][0]
                
                total_balance_str = balance_info.get('total_balance', '0')
                try:
                    total_balance = float(total_balance_str)
                except (ValueError, TypeError):
                    total_balance = 0
                
                return {
                    'balance_dollars': total_balance,
                    'balance_cents': int(total_balance * 100),
                    'currency': balance_info.get('currency', 'USD'),
                    'is_available': data.get('is_available', True)
                }
        
        return None
        
    except Exception as e:
        print(f"Ошибка получения информации о балансе: {str(e)[:100]}")
        return None

def export_to_language_files() -> Tuple[bool, str]:
    def format_xml_output(xml_str: str) -> str:
        import re
        
        def clean_description(match):
            desc = match.group(1)
            # Ensure each <p> starts on a new line even if tags are adjacent: </p><p>
            desc = re.sub(r'</p>\s*<p>', '</p>\n<p>', desc)
            desc = re.sub(r'^\s*', '', desc)
            desc = re.sub(r'\s*$', '', desc)
            return f'<description>{desc}</description>'
        
        xml_str = re.sub(r'<description>(.*?)</description>', clean_description, xml_str, flags=re.DOTALL)
        
        def clean_offer(match):
            offer = match.group(1)
            offer = re.sub(r'\n\s*\n+', '\n', offer)
            offer = offer.strip()
            return f'<offer>\n{offer}\n</offer>'
        
        xml_str = re.sub(r'<offer>(.*?)</offer>', clean_offer, xml_str, flags=re.DOTALL)
        xml_str = re.sub(r'</offer>(\n)<offer>', r'</offer>\n\n<offer>', xml_str)
        
        return xml_str

    def wrap_description_in_cdata(xml_str: str) -> str:
        """Ensure <description>...</description> is wrapped in <![CDATA[...]]>.

        We do this on the final XML string because XML parsers (including BeautifulSoup)
        can escape embedded HTML tags (<p>, <ul>, etc.) unless they are inside CDATA.
        """
        import re

        def _wrap(match: re.Match) -> str:
            inner = match.group(1)
            # Already CDATA
            if inner.strip().startswith('<![CDATA['):
                return match.group(0)
            return f'<description><![CDATA[{inner}]]></description>'

        return re.sub(r'<description>(.*?)</description>', _wrap, xml_str, flags=re.DOTALL)

    def unescape_cdata_html_entities_in_description(xml_str: str) -> str:
        """Convert &lt; and &gt; back to < and > inside description CDATA blocks.

        This is safe because it only touches the CDATA payload, not the XML markup.
        """
        import re

        def _fix(match: re.Match) -> str:
            inner = match.group(1)
            inner = inner.replace('&lt;', '<').replace('&gt;', '>')
            return f'<description><![CDATA[{inner}]]></description>'

        return re.sub(
            r'<description><!\[CDATA\[(.*?)\]\]></description>',
            _fix,
            xml_str,
            flags=re.DOTALL,
        )

    def unescape_video_iframe(xml_str: str) -> str:
        """Unescape <video>...</video> content when it contains an embedded iframe.

        BeautifulSoup XML serializer often escapes angle brackets inside <video>,
        producing &lt;iframe ...&gt;.
        We convert those back to real tags.
        """
        import re

        def _fix_video(match: re.Match) -> str:
            inner = match.group(1)
            if '&lt;iframe' not in inner:
                return match.group(0)
            # Convert escaped iframe markup back to real tags.
            inner = inner.replace('&lt;', '<').replace('&gt;', '>')
            return f'<video>{inner}</video>'

        # Process any <video>...</video> blocks.
        return re.sub(r'<video>(.*?)</video>', _fix_video, xml_str, flags=re.DOTALL)

    def normalize_video_iframe_markup(xml_str: str) -> str:
        """Normalize iframe inside <video> to a canonical HTML snippet.

        Problem:
        - BeautifulSoup XML serializer may turn iframe into a self-closing tag and/or
          reorder attributes.

        Requirement (user):
        <video><iframe width="640" height="480" src="..." title="..." frameborder="0" allow="..." referrerpolicy="..." allowfullscreen></iframe></video>
        """
        import re

        CANON_ALLOW = (
            'accelerometer; autoplay; clipboard-write; encrypted-media; '
            'gyroscope; picture-in-picture; web-share'
        )
        CANON_REFERRER = 'strict-origin-when-cross-origin'

        def _fix(match: re.Match) -> str:
            inner = match.group(1)
            if 'iframe' not in inner:
                return match.group(0)

            # If iframe is still escaped, unescape it here.
            inner_unescaped = inner.replace('&lt;', '<').replace('&gt;', '>')

            src_m = re.search(r'src\s*=\s*"([^"]+)"', inner_unescaped)
            title_m = re.search(r'title\s*=\s*"([^"]*)"', inner_unescaped)

            src = src_m.group(1) if src_m else ''
            title = title_m.group(1) if title_m else ''

            # Only normalize youtube embeds (to avoid breaking other potential video markup).
            if 'youtube.com/embed/' not in src:
                return f'<video>{inner_unescaped}</video>'

            iframe_html = (
                f'<iframe width="640" height="480" '
                f'src="{src}" '
                f'title="{title}" '
                f'frameborder="0" '
                f'allow="{CANON_ALLOW}" '
                f'referrerpolicy="{CANON_REFERRER}" '
                f'allowfullscreen></iframe>'
            )
            return f'<video>{iframe_html}</video>'

        return re.sub(r'<video>(.*?)</video>', _fix, xml_str, flags=re.DOTALL)

    def normalize_description_cdata_formatting(xml_str: str) -> str:
        """Pretty-format HTML inside description CDATA.

        Goal:
        - paragraphs separated with newlines: </p>\n<p>
        - no leading spaces at the beginning of lines inside CDATA
        """
        import re

        def _fix(match: re.Match) -> str:
            inner = match.group(1)
            inner = inner.replace('\r\n', '\n').replace('\r', '\n')
            # Put each paragraph on a new line (handles both </p><p> and </p> <p> etc.)
            inner = re.sub(r'</p>\s*<p>', '</p>\n<p>', inner)
            # Remove any indentation at the beginning of CDATA lines (e.g. "\n <p>")
            inner = re.sub(r'\n[ \t]+', '\n', inner)
            # Also trim leading spaces on the very first line if present
            inner = inner.lstrip(' \t')
            # Remove leading/trailing blank lines
            inner = inner.strip('\n')
            return f'<description><![CDATA[{inner}]]></description>'

        # Be tolerant to possible whitespace/newlines around the CDATA wrapper.
        return re.sub(
            r'<description>\s*<!\[CDATA\[(.*?)\]\]>\s*</description>',
            _fix,
            xml_str,
            flags=re.DOTALL,
        )

    def ensure_iframe_on_new_line_in_description_cdata(xml_str: str) -> str:
        """Ensure <iframe ...> inside description CDATA starts on a new line.

        User requirement: in the Ukrainian export file, the iframe tag must be moved
        to a new line.
        """
        import re

        def _fix(match: re.Match) -> str:
            inner = match.group(1)

            # Common case: paragraph ends and iframe follows immediately.
            inner = re.sub(r'</p>\s*<iframe\b', '</p>\n<iframe', inner)

            # If iframe is appended after plain text without a newline, force a newline.
            inner = re.sub(r'(?<!\n)\s*<iframe\b', '\n<iframe', inner)

            # Avoid leading blank line if iframe is the first thing.
            inner = inner.lstrip('\n')
            return f'<description><![CDATA[{inner}]]></description>'

        return re.sub(
            r'<description>\s*<!\[CDATA\[(.*?)\]\]>\s*</description>',
            _fix,
            xml_str,
            flags=re.DOTALL,
        )
    
    try:
        if not os.path.exists(PARSED_FILE):
            return False, f"Файл {PARSED_FILE} не найден. Запустите парсер сначала."

        # ===== FILTER REMOVED PRODUCTS =====
        # User requirement: the uploaded input file can change (offers can be added/removed).
        # Before exporting UA/RU, compare PARSED_FILE with the current input file and
        # remove offers that are no longer present in the input.
        try:
            current_input_path = (load_settings().get('xml_path') or '').strip()
            if current_input_path and os.path.exists(current_input_path):
                current_ids = load_offer_ids_from_xml(current_input_path)
                if current_ids:
                    parsed_ids = load_offer_ids_from_xml(PARSED_FILE)
                    removed_ids = parsed_ids - current_ids
                else:
                    removed_ids = set()
            else:
                removed_ids = set()
        except Exception:
            removed_ids = set()
        
        with open(PARSED_FILE, 'r', encoding='utf-8') as f:
            original_xml = f.read()
        
        original_soup = BeautifulSoup(original_xml, 'xml')

        # Build a mapping of offer_id -> price from the user's input XML (if provided).
        price_map = {}
        try:
            input_path = Path(_SETTINGS.get('xml_path') or NEW_FILE)
            if input_path and input_path.exists():
                input_soup = BeautifulSoup(open(input_path, encoding='utf-8'), 'xml')
                for of in input_soup.find_all('offer'):
                    oid = get_offer_id(of)
                    if not oid:
                        continue
                    ptag = of.find('price')
                    if ptag and ptag.get_text(strip=True):
                        price_map[oid] = ptag.get_text().strip()
        except Exception:
            price_map = {}

        if removed_ids:
            removed_count = 0
            for offer in list(original_soup.find_all('offer')):
                oid = get_offer_id(offer)
                if oid and oid in removed_ids:
                    offer.extract()
                    removed_count += 1
            # Refresh xml after filtering for further processing.
            original_xml = str(original_soup)
        else:
            removed_count = 0
        
        # ===== УКРАИНСКИЙ ФАЙЛ =====
        ua_soup = BeautifulSoup(original_xml, 'xml')
        
        for offer in ua_soup.find_all('offer'):
            name_tag = offer.find('name')
            name_ua_tag = offer.find('name_ua')
            
            if name_ua_tag and name_tag:
                name_tag.string = name_ua_tag.get_text().strip()
            
            desc_tag = offer.find('description')
            desc_ua_tag = offer.find('description_ua')
            
            if desc_ua_tag and desc_tag:
                # IMPORTANT:
                # Preserve full HTML content from <description_ua> to <description>.
                # We intentionally keep tags (<p>, <iframe>, etc.) and later wrap in CDATA.
                desc_ua_html = desc_ua_tag.decode_contents()
                desc_tag.clear()
                desc_tag.append(BeautifulSoup(desc_ua_html, "html.parser"))
            
            if name_ua_tag:
                name_ua_tag.extract()
            if desc_ua_tag:
                desc_ua_tag.extract()
        # Insert prices from the original input XML (if available)
        for offer in ua_soup.find_all('offer'):
            try:
                oid = get_offer_id(offer)
                if oid and oid in price_map:
                    price_value = price_map.get(oid)
                    if price_value is not None:
                        price_tag = offer.find('price')
                        if not price_tag:
                            price_tag = ua_soup.new_tag('price')
                            name_tag = offer.find('name')
                            if name_tag:
                                name_tag.insert_after(price_tag)
                            else:
                                offer.append(price_tag)
                        price_tag.string = price_value
            except Exception:
                pass
        
        ua_xml_str = str(ua_soup)
        ua_xml_str = format_xml_output(ua_xml_str)
        ua_xml_str = wrap_description_in_cdata(ua_xml_str)
        ua_xml_str = unescape_cdata_html_entities_in_description(ua_xml_str)
        ua_xml_str = normalize_description_cdata_formatting(ua_xml_str)
        ua_xml_str = ensure_iframe_on_new_line_in_description_cdata(ua_xml_str)
        with open(EXPORT_FILE_UA, 'w', encoding='utf-8') as f:
            f.write(ua_xml_str)
        
        # ===== РУССКИЙ ФАЙЛ =====
        ru_soup = BeautifulSoup(original_xml, 'xml')
        
        for offer in ru_soup.find_all('offer'):
            name_ua_tag = offer.find('name_ua')
            desc_ua_tag = offer.find('description_ua')
            
            if name_ua_tag:
                name_ua_tag.extract()
            if desc_ua_tag:
                desc_ua_tag.extract()
        # Insert prices from the original input XML (if available)
        for offer in ru_soup.find_all('offer'):
            try:
                oid = get_offer_id(offer)
                if oid and oid in price_map:
                    price_value = price_map.get(oid)
                    if price_value is not None:
                        price_tag = offer.find('price')
                        if not price_tag:
                            price_tag = ru_soup.new_tag('price')
                            name_tag = offer.find('name')
                            if name_tag:
                                name_tag.insert_after(price_tag)
                            else:
                                offer.append(price_tag)
                        price_tag.string = price_value
            except Exception:
                pass
        
        ru_xml_str = str(ru_soup)
        ru_xml_str = format_xml_output(ru_xml_str)
        ru_xml_str = wrap_description_in_cdata(ru_xml_str)
        ru_xml_str = unescape_cdata_html_entities_in_description(ru_xml_str)
        ru_xml_str = normalize_description_cdata_formatting(ru_xml_str)
        with open(EXPORT_FILE_RU, 'w', encoding='utf-8') as f:
            f.write(ru_xml_str)

        if removed_count:
            return True, (
                f"✓ Успешно создано:\n• {EXPORT_FILE_UA} (украинский)\n• {EXPORT_FILE_RU} (русский)"
                f"\n\n⚠ Исключено удалённых товаров (не найдено в текущем входном файле): {removed_count}"
            )
        return True, f"✓ Успешно создано:\n• {EXPORT_FILE_UA} (украинский)\n• {EXPORT_FILE_RU} (русский)"
        
    except Exception as e:
        return False, f"✗ Ошибка при экспорте: {str(e)}"

atexit.register(cleanup_global_driver)
atexit.register(emergency_save)

if __name__ == "__main__":
    print("Запустите gui.py")
