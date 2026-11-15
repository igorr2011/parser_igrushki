"""
GUI for product description parser with pause/resume/stop controls.
This version saves/loads user settings (xml_path, proxy, headless, user_agent) using functions from descs.py.
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import re
from bs4 import BeautifulSoup
import os
import atexit
import signal
import sys

from pathlib import Path

# Import functions and constants from descs.py
from descs import (
    get_offer_id, load_parsed_ids, fetch_html_via_chrome,
    NEW_FILE, PARSED_FILE, emergency_save, init_global_driver,
    wrap_descriptions_in_cdata  # НОВИЙ ІМПОРТ
)
import descs

# Get Downloads folder for file browser default directory
DOWNLOADS_DIR = str(Path.home() / "Downloads")


class ParserGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Product Description Parser 6.3")  # Оновили версію
        self.root.geometry("1000x750")

        # Load settings (will create defaults if not present)
        self.settings = descs.load_settings()

        # State variables
        self.parsing_thread = None
        self.is_running = False
        self.is_paused = False
        self.should_stop = False

        # Use saved xml_path if present, otherwise module-level default NEW_FILE
        saved_xml = self.settings.get("xml_path") or ""
        self.selected_file = saved_xml if saved_xml else NEW_FILE

        self.total_offers = 0
        self.parsed_count = 0
        self.skipped_count = 0

        # Data for saving on interrupt
        self.old_soup = None
        self.parsed_offers = []

        # Register cleanup handlers
        atexit.register(self.cleanup)
        signal.signal(signal.SIGINT, self.signal_handler)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, self.signal_handler)

        # UI Setup
        self.setup_ui()

        # --- Prefill Fields ---
        
        # Proxy
        proxy_saved = (self.settings.get("proxy") or "").strip()
        if proxy_saved and ":" in proxy_saved:
            addr, port = proxy_saved.split(":", 1)
            self.proxy_addr_entry.insert(0, addr)
            self.proxy_port_entry.insert(0, port)
        elif proxy_saved:
            self.proxy_addr_entry.insert(0, proxy_saved)
            self.proxy_port_entry.insert(0, "8080")
        else:
            self.proxy_port_entry.insert(0, "8080")

        # Headless
        self.headless_var.set(bool(self.settings.get("headless", False)))

        # User-Agent
        ua_saved = self.settings.get("user_agent", "")
        self.ua_entry.insert(0, ua_saved)

        # DeepSeek API Key
        api_key_saved = self.settings.get("deepseek_api_key", "")
        self.api_key_entry.insert(0, api_key_saved)

        # Check pictures (prefer existing non-watermarked URLs; otherwise use parsed)
        self.check_pictures_var.set(bool(self.settings.get("check_pictures", False)))

        # Update file path label with current selection
        self.file_path_label.config(text=self.selected_file)

        # Загружаем сохраненный баланс
        saved_balance = self.settings.get("deepseek_balance", "0.00")
        if saved_balance and saved_balance != "0.00":
            self.balance_label.config(text=f"${saved_balance}", foreground="green")

    def signal_handler(self, signum, frame):
        """Handle system signals (Ctrl+C, etc)."""
        print('Signal received, saving progress...')
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Save progress before exiting."""
        try:
            self._save_current_settings()
        except Exception:
            pass

        descs._old_soup_backup = self.old_soup
        descs._parsed_offers_backup = self.parsed_offers
        emergency_save()
        if self.old_soup and self.parsed_offers:
            self.log(f"Автосохранение: {len(self.parsed_offers)} товаров сохранено")

    def setup_ui(self):
        """Create the GUI layout."""
        # Title
        title = ttk.Label(self.root, text="Product Parser", font=("Arial", 16, "bold"))
        title.pack(pady=10)

        # File selection frame
        file_frame = ttk.LabelFrame(self.root, text="Select Input File", padding=10)
        file_frame.pack(padx=10, pady=10, fill="x")

        self.file_path_label = ttk.Label(file_frame, text=self.selected_file, font=("Arial", 10), foreground="blue")
        self.file_path_label.pack(side="left", fill="x", expand=True, padx=5)

        browse_btn = ttk.Button(file_frame, text="Browse...", command=self.browse_file, width=15)
        browse_btn.pack(side="right", padx=5)

        reset_btn = ttk.Button(file_frame, text="Default", command=self.reset_file, width=10)
        reset_btn.pack(side="right", padx=5)

        # Settings frame (Proxy, User-Agent, Headless)
        settings_frame = ttk.LabelFrame(self.root, text="Settings", padding=10)
        settings_frame.pack(padx=10, pady=10, fill="x")

        # Row 0: Proxy
        ttk.Label(settings_frame, text="Proxy Address:").grid(row=0, column=0, sticky="w", padx=5)
        self.proxy_addr_entry = ttk.Entry(settings_frame, width=30)
        self.proxy_addr_entry.grid(row=0, column=1, padx=5, sticky="ew")

        ttk.Label(settings_frame, text="Port:").grid(row=0, column=2, sticky="w", padx=5)
        self.proxy_port_entry = ttk.Entry(settings_frame, width=10)
        self.proxy_port_entry.grid(row=0, column=3, padx=5)

        clear_proxy_btn = ttk.Button(settings_frame, text="Clear", command=self.clear_proxy, width=8)
        clear_proxy_btn.grid(row=0, column=4, padx=5)

        # Row 1: User-Agent
        ttk.Label(settings_frame, text="User-Agent:").grid(row=1, column=0, sticky="w", padx=5, pady=10)
        self.ua_entry = ttk.Entry(settings_frame)
        self.ua_entry.grid(row=1, column=1, columnspan=4, sticky="ew", padx=5, pady=10)

        # Row 2: Headless
        self.headless_var = tk.BooleanVar(value=False)
        headless_cb = ttk.Checkbutton(settings_frame, text="Headless browser (no UI)", variable=self.headless_var, command=self._on_headless_toggle)
        headless_cb.grid(row=2, column=1, sticky="w", pady=5)

        # Row 3: DeepSeek API Key
        ttk.Label(settings_frame, text="DeepSeek API Key:").grid(row=3, column=0, sticky="w", padx=5, pady=10)
        self.api_key_entry = ttk.Entry(settings_frame, show="*")
        self.api_key_entry.grid(row=3, column=1, columnspan=4, sticky="ew", padx=5, pady=10)

        # Row 4: Pictures checking
        self.check_pictures_var = tk.BooleanVar(value=False)
        check_pictures_cb = ttk.Checkbutton(
            settings_frame,
            text="Check pictures (avoid watermark)",
            variable=self.check_pictures_var,
            command=self._on_check_pictures_toggle,
        )
        check_pictures_cb.grid(row=4, column=1, sticky="w", pady=5)

        settings_frame.columnconfigure(1, weight=1)

        # Stats Frame
        stats_frame = ttk.LabelFrame(self.root, text="Statistics", padding=10)
        stats_frame.pack(padx=10, pady=10, fill="x")

        # Total items
        ttk.Label(stats_frame, text="Total items in file:").grid(row=0, column=0, sticky="w")
        self.total_label = ttk.Label(stats_frame, text="0", font=("Arial", 12, "bold"))
        self.total_label.grid(row=0, column=1, sticky="w", padx=20)

        # Parsed items
        ttk.Label(stats_frame, text="Parsed items:").grid(row=1, column=0, sticky="w")
        self.parsed_label = ttk.Label(stats_frame, text="0", font=("Arial", 12, "bold"), foreground="green")
        self.parsed_label.grid(row=1, column=1, sticky="w", padx=20)

        # Skipped items
        ttk.Label(stats_frame, text="Skipped items:").grid(row=2, column=0, sticky="w")
        self.skipped_label = ttk.Label(stats_frame, text="0", font=("Arial", 12, "bold"), foreground="orange")
        self.skipped_label.grid(row=2, column=1, sticky="w", padx=20)

        # Progress bar
        ttk.Label(stats_frame, text="Progress:").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(stats_frame, variable=self.progress_var, maximum=100, length=300)
        self.progress_bar.grid(row=3, column=1, sticky="ew", padx=20, pady=(10, 0))

        # Status
        ttk.Label(stats_frame, text="Status:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.status_label = ttk.Label(stats_frame, text="Ready", font=("Arial", 10), foreground="blue")
        self.status_label.grid(row=4, column=1, sticky="w", padx=20, pady=(10, 0))

        # DeepSeek Tokens Balance
        ttk.Label(stats_frame, text="DeepSeek Balance:").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.balance_label = ttk.Label(stats_frame, text="--", font=("Arial", 10), foreground="gray")
        self.balance_label.grid(row=5, column=1, sticky="w", padx=20, pady=(10, 0))

        # Buttons Frame
        button_frame = ttk.Frame(self.root)
        button_frame.pack(padx=10, pady=20, fill="x")

        self.start_btn = ttk.Button(button_frame, text="Start", command=self.start_parsing, width=15)
        self.start_btn.grid(row=0, column=0, padx=5)

        self.pause_btn = ttk.Button(button_frame, text="Pause", command=self.toggle_pause, width=15, state="disabled")
        self.pause_btn.grid(row=0, column=1, padx=5)

        self.stop_btn = ttk.Button(button_frame, text="Stop", command=self.stop_parsing, width=15, state="disabled")
        self.stop_btn.grid(row=0, column=2, padx=5)

        self.export_btn = ttk.Button(button_frame, text="Export Languages", command=self.export_languages, width=15)
        self.export_btn.grid(row=0, column=3, padx=5)

        # Log area
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=5)
        log_frame.pack(padx=10, pady=10, fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=12, width=90, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set)

    def log(self, message):
        """Add message to log."""
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self.root.update()

    def _save_current_settings(self):
        """Save all UI settings to json file."""
        self.settings["xml_path"] = self.selected_file or ""
        
        addr = self.proxy_addr_entry.get().strip()
        port = self.proxy_port_entry.get().strip()
        if addr:
            proxy_value = f"{addr}:{port if port else '8080'}"
        else:
            proxy_value = ""
        self.settings["proxy"] = proxy_value
        
        self.settings["headless"] = bool(self.headless_var.get())
        self.settings["user_agent"] = self.ua_entry.get().strip()
        self.settings["deepseek_api_key"] = self.api_key_entry.get().strip()
        self.settings["check_pictures"] = bool(getattr(self, 'check_pictures_var', tk.BooleanVar(value=False)).get())

        try:
            descs.save_settings(self.settings)
            self.log("Settings saved")
        except Exception as e:
            self.log(f"Ошибка сохранения настроек: {e}")

    def _on_headless_toggle(self):
        """Called when headless checkbox toggled."""
        self._save_current_settings()
        try:
            descs.cleanup_global_driver()
            self.log(f"Headless переключен. Браузер будет перезапущен при следующем запросе.")
        except Exception as e:
            self.log(f"Ошибка при сбросе браузера: {e}")

    def _on_check_pictures_toggle(self):
        """Called when 'Check pictures (avoid watermark)' checkbox toggled."""
        self._save_current_settings()

    def browse_file(self):
        initial_dir = DOWNLOADS_DIR
        if self.selected_file and os.path.exists(self.selected_file):
            initial_dir = os.path.dirname(self.selected_file)
        file_path = filedialog.askopenfilename(
            title="Select XML file with URLs",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")],
            initialdir=initial_dir
        )
        if file_path:
            self.selected_file = file_path
            self.file_path_label.config(text=file_path)
            self.log(f"Selected file: {file_path}")
            self._save_current_settings()

    def reset_file(self):
        self.selected_file = NEW_FILE
        self.file_path_label.config(text=NEW_FILE)
        self.log(f"Reset to default: {NEW_FILE}")
        self._save_current_settings()

    def clear_proxy(self):
        self.proxy_addr_entry.delete(0, tk.END)
        self.proxy_port_entry.delete(0, tk.END)
        self.proxy_port_entry.insert(0, "8080")
        self.log("Proxy cleared")
        self._save_current_settings()

    def update_stats(self):
        self.total_label.config(text=str(self.total_offers))
        self.parsed_label.config(text=str(self.parsed_count))
        self.skipped_label.config(text=str(self.skipped_count))
        if self.total_offers > 0:
            progress = (self.parsed_count + self.skipped_count) / self.total_offers * 100
            self.progress_var.set(progress)
        self.root.update()

    def process_offers_batch_parallel(self, batch_offers, batch_start, total_offers, proxy_val, headless_val, ua_val):
        """
        Обрабатывает партию из 3 товаров параллельно в 3 вкладках браузера.
        """
        import threading
        
        tabs = []
        for idx, offer in enumerate(batch_offers):
            url_tag = offer.find('url')
            if not url_tag:
                continue
            url = url_tag.get_text().strip()
            if not url or url == 'https://igrushki7.ua/':
                continue
            
            try:
                tab = descs._driver.new_tab(url)
                tabs.append({'tab': tab, 'offer': offer, 'url': url, 'idx': batch_start + idx})
                self.log(f"[Tab {idx+1}] Loading: {url}")
            except Exception as e:
                self.log(f"Ошибка открытия вкладки: {e}")
                tabs.append({'tab': None, 'offer': offer, 'url': url, 'idx': batch_start + idx})
        
        time.sleep(3)
        
        results = []
        threads = []
        
        for tab_data in tabs:
            if tab_data['tab'] is None:
                results.append({'offer': None, 'idx': tab_data['idx']})
                continue
            
            thread = threading.Thread(
                target=self._process_single_offer_tab,
                args=(tab_data, results, len(tabs)),
                daemon=True
            )
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join(timeout=120)
        
        for tab_data in tabs:
            if tab_data['tab'] is not None:
                try:
                    tab_data['tab'].close()
                except:
                    pass
        
        return results

    def _process_single_offer_tab(self, tab_data, results, batch_size):
        """
        Обрабатывает один товар из вкладки браузера в отдельном потоке.
        """
        try:
            tab = tab_data['tab']
            offer = tab_data['offer']
            url = tab_data['url']
            idx = tab_data['idx']
            
            html = tab.html
            
            if not html:
                self.log(f"Failed to load: {url}")
                results.append({'offer': None, 'idx': idx})
                return
            
            page_soup = BeautifulSoup(html, 'html.parser')
            desc = page_soup.find('div', class_='section description')
            
            desc_text = desc.get_text(separator="\n") if desc else ""
            if not desc or not desc_text.strip():
                self.log(f"No description for {idx} - skipped")
                results.append({'offer': None, 'idx': idx})
                return
            
            desc_text_lines = []
            old_description_lines = []
            existing_desc_tag = offer.find('description')
            if existing_desc_tag:
                old_text = existing_desc_tag.get_text(separator="\n")
                for line in old_text.splitlines():
                    line = line.strip()
                    if line:
                        old_description_lines.append(line)

            for line in desc_text.splitlines():
                line = line.strip()
                if line:
                    for part in line.split('. '):
                        part = part.strip()
                        if part:
                            if not part.endswith('.'):
                                part += '.'
                            desc_text_lines.append(part)

            # Normalize helper
            def normalize_line(l: str) -> str:
                return re.sub(r"\s+", " ", l.strip()).rstrip('.').strip().lower()

            parsed_norm_lines = [normalize_line(l) for l in desc_text_lines if l]
            parsed_norm_set = set(parsed_norm_lines)

            # If an existing old line already contains all parsed description fragments,
            # prefer that existing old line as the main description and avoid duplicate fragments.
            selected_old_combined = None
            for i, old_line in enumerate(old_description_lines):
                if not old_line or old_line.strip().startswith('*'):
                    continue
                old_norm = normalize_line(old_line)
                if not old_norm:
                    continue

                all_in_old = True
                for parsed_norm in parsed_norm_lines:
                    if len(parsed_norm) < 5:
                        continue
                    if parsed_norm not in old_norm:
                        all_in_old = False
                        break

                if all_in_old and len(old_norm) > 20:
                    selected_old_combined = old_line
                    old_description_lines.pop(i)
                    break

            if selected_old_combined is not None:
                desc_text_lines = [selected_old_combined]

            # Avoid appending old description fragments that are already present in parsed description lines.
            filtered_old = []
            for line in old_description_lines:
                if not line:
                    continue
                n_line = normalize_line(line)
                if n_line in parsed_norm_set:
                    continue

                # If line is a combined old line and all sentence pieces already exist in parsed, skip.
                pieces = [normalize_line(p) for p in re.split(r'\.\s*', line) if p.strip()]
                if pieces and all(piece in parsed_norm_set for piece in pieces if piece and len(piece) >= 5):
                    continue

                filtered_old.append(line)
            old_description_lines = filtered_old

            def reorder_old_lines(lines):
                normal = [l for l in lines if not l.strip().startswith('*')]
                footnotes = [l for l in lines if l.strip().startswith('*')]
                return normal + footnotes

            old_description_lines = reorder_old_lines(old_description_lines)

            # Overwrite description with freshly parsed content, but append existing input description text in normalized order.
            desc_tag = offer.find('description')
            if not desc_tag:
                desc_tag = self.old_soup.new_tag('description')
                offer.append(desc_tag)

            desc_p = "\n".join(f"<p>{line}</p>" for line in desc_text_lines)
            desc_tag.clear()
            desc_tag.append(BeautifulSoup(desc_p, "html.parser"))
            tail_lines = reorder_old_lines(old_description_lines)
            tail_html = "\n".join(f"<p>{line}</p>" for line in tail_lines)

            # ===== PICTURES CHECKING (optional) =====
            # If enabled:
            # - parse picture links from page (often watermarked)
            # - compare with existing <picture> tags in offer by index
            # - if basenames match (allow parsed *_s) => keep existing URL (prefer non-watermarked)
            # - else => keep existing directory (e.g. /images/prom or /images/promadd)
            #         but replace ONLY the filename with the parsed one.
            #   Special rule: for the FIRST parsed image, if basename ends with '_s', strip it.
            if bool(self.settings.get('check_pictures', False)):
                existing_tags = offer.find_all('picture')
                existing_values = [(t.get_text() or '').strip() for t in existing_tags]

                add_pic_url = page_soup.find_all('a', class_='thumbs')
                parsed_values = [a.get('href') for a in add_pic_url if a.get('href')]

                if parsed_values:
                    changed = False
                    max_len = max(len(existing_values), len(parsed_values))

                    for i in range(max_len):
                        parsed_src = parsed_values[i] if i < len(parsed_values) else None
                        existing_url = existing_values[i] if i < len(existing_values) else ""

                        if not parsed_src:
                            continue

                        if existing_url and descs.images_match_by_basename(parsed_src, existing_url):
                            final_url = existing_url
                        else:
                            # If we already have a non-watermarked URL in XML, keep its directory
                            # and update only the filename from the parsed one.
                            # For the first parsed image, strip trailing '_s' in basename.
                            if existing_url:
                                final_url = descs.replace_url_filename_keep_dir(
                                    existing_url,
                                    parsed_src,
                                    strip_first_suffix_s=(i == 0),
                                )
                            else:
                                # No existing value -> fall back to normalized parsed URL
                                final_url = descs.normalize_site_image_url(parsed_src)

                        if i < len(existing_tags):
                            if (existing_tags[i].get_text() or '').strip() != final_url:
                                existing_tags[i].string = final_url
                                changed = True
                        else:
                            new_tag = self.old_soup.new_tag('picture')
                            new_tag.string = final_url
                            offer.append(new_tag)
                            changed = True

                    if changed:
                        self.log(f"🖼 Pictures checked for {idx}")

                # Final step: remove duplicate picture URLs inside this offer
                # (keep first occurrence, remove the rest).
                try:
                    seen = set()
                    removed = 0
                    for pic in list(offer.find_all('picture')):
                        val = (pic.get_text() or '').strip()
                        if not val:
                            continue
                        if val in seen:
                            pic.extract()
                            removed += 1
                        else:
                            seen.add(val)
                    if removed:
                        self.log(f"🧹 Removed duplicate <picture> for {idx}: {removed}")
                except Exception:
                    pass

            # ===== VIDEO/IFRAME HANDLING =====
            # Requirement:
            # - DO NOT create <video> tag
            # - If page has an iframe (youtube embed), append iframe markup to the end of
            #   <description> and <description_ua> (same iframe string for both languages)
            # - iframe must end up inside CDATA (handled later by wrap_descriptions_in_cdata)
            parsed_iframe_html = ""
            video_iframe = page_soup.find('iframe', {'id': 'widget2'})
            if not video_iframe:
                video_iframe = page_soup.find('iframe')

            if video_iframe:
                video_src = video_iframe.get('src', '')
                if video_src and 'youtube.com/embed/' in video_src:
                    video_id = video_src.split('youtube.com/embed/')[1].split('?')[0].split('&')[0]
                    video_title = video_iframe.get('title', '')
                    parsed_iframe_html = (
                        f'<iframe width="640" height="480" '
                        f'src="https://www.youtube.com/embed/{video_id}" '
                        f'title="{video_title}" frameborder="0" '
                        f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
                        f'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>'
                    )
                    self.log(f"✓ Iframe для товара {idx}: youtube.com/embed/{video_id}")
            
            api_key = self.settings.get("deepseek_api_key") or ""
            product_name_tag = offer.find('name')
            product_name = product_name_tag.get_text().strip() if product_name_tag else ""
            
            article = None
            if product_name:
                if api_key:
                    article = descs.call_deepseek_api(product_name, api_key, timeout=30.0)
                    if article:
                        self.log(f"✓ Товар {idx} - артикль: {article}")
                    else:
                        self.log(f"⚠ Товар {idx} - артикль не найден")
                else:
                    article = descs.extract_article_local(product_name)
                    if article:
                        self.log(f"🔍 Товар {idx} - локальный артикль: {article}")
            
            if article:
                vendor_tag = offer.find('vendorCode')
                if not vendor_tag:
                    vendor_tag = self.old_soup.new_tag('vendorCode')
                    offer.append(vendor_tag)
                vendor_tag.string = article
            
            if api_key and product_name:
                translated_name = descs.translate_text_deepseek(product_name, api_key, timeout=30.0)
                
                if translated_name:
                    self.log(f"🌍 Товар {idx} - название на укр: {translated_name[:40]}")
                    
                    name_ua_tag = offer.find('name_ua')
                    if not name_ua_tag:
                        name_ua_tag = self.old_soup.new_tag('name_ua')
                        offer.append(name_ua_tag)
                    
                    name_ua_tag.string = translated_name.strip()
                    
                    translated_name_ru = descs.translate_text_deepseek_to_russian(translated_name.strip(), api_key, timeout=30.0)
                    
                    if translated_name_ru:
                        self.log(f"🌍 Товар {idx} - название на рус: {translated_name_ru[:40]}")
                        product_name_tag.string = translated_name_ru.strip()
            
            if api_key and desc_text_lines:
                full_description_text = "\n".join(desc_text_lines)
                translated_text = descs.translate_text_deepseek(full_description_text, api_key, timeout=30.0)
                
                if translated_text:
                    self.log(f"🌍 Товар {idx} - описание на укр")
                    
                    translated_lines = []
                    for line in translated_text.splitlines():
                        line = line.strip()
                        if line:
                            for part in line.split('. '):
                                part = part.strip()
                                if part and not part.endswith('.'):
                                    part += '.'
                                if part:
                                    translated_lines.append(part)
                    
                    desc_ua_tag = offer.find('description_ua')
                    if not desc_ua_tag:
                        desc_ua_tag = self.old_soup.new_tag('description_ua')
                        offer.append(desc_ua_tag)
                    
                    desc_ua_p = "\n".join(f"<p>{line}</p>" for line in translated_lines)
                    desc_ua_tag.clear()
                    desc_ua_tag.append(BeautifulSoup(desc_ua_p, "html.parser"))
                    if old_description_lines:
                        old_description_lines_ua = []
                        for line in old_description_lines:
                            if line.strip():
                                old_description_lines_ua.append(line)
                        normal_ua = [l for l in old_description_lines_ua if not l.strip().startswith('*')]
                        footnotes_ua = [l for l in old_description_lines_ua if l.strip().startswith('*')]
                        ordered_ua = normal_ua + footnotes_ua
                        if ordered_ua:
                            old_desc_p_ua = "\n".join(f"<p>{line}</p>" for line in ordered_ua)
                            desc_ua_tag.append(BeautifulSoup("\n" + old_desc_p_ua, "html.parser"))
                    
                    desc_ua_text = desc_ua_tag.get_text().strip()
                    
                    if desc_ua_text:
                        translated_to_russian = descs.translate_text_deepseek_to_russian(desc_ua_text, api_key, timeout=30.0)
                        
                        if translated_to_russian:
                            self.log(f"🌍 Товар {idx} - описание на рус")
                            
                            russian_lines = []
                            for line in translated_to_russian.splitlines():
                                line = line.strip()
                                if line:
                                    for part in line.split('. '):
                                        part = part.strip()
                                        if part and not part.endswith('.'):
                                            part += '.'
                                        if part:
                                            russian_lines.append(part)
                            
                            desc_tag.clear()
                            desc_ru_p = "\n".join(f"<p>{line}</p>" for line in russian_lines)
                            desc_tag.append(BeautifulSoup(desc_ru_p, "html.parser"))
                            if old_description_lines:
                                old_ordered_ru = reorder_old_lines(old_description_lines)
                                old_desc_p = "\n".join(f"<p>{line}</p>" for line in old_ordered_ru)
                                desc_tag.append(BeautifulSoup("\n" + old_desc_p, "html.parser"))

            # Append iframe to BOTH descriptions (same iframe markup for RU/UA).
            # We append it as plain text so it survives BeautifulSoup XML serialization
            # and then gets restored as real HTML inside CDATA by wrap_descriptions_in_cdata().
            if parsed_iframe_html:
                try:
                    if desc_tag:
                        desc_tag.append("\n" + parsed_iframe_html)

                    desc_ua_tag = offer.find('description_ua')
                    if desc_ua_tag:
                        desc_ua_tag.append("\n" + parsed_iframe_html)
                except Exception as _e:
                    # Non-fatal: description exists anyway
                    pass
            
            results.append({'offer': offer, 'idx': idx})
            
        except Exception as e:
            self.log(f"Ошибка обработки товара {tab_data['idx']}: {e}")
            results.append({'offer': None, 'idx': tab_data['idx']})

    def start_parsing(self):
        """Start the parsing process."""
        if self.parsing_thread and self.parsing_thread.is_alive():
            messagebox.showwarning("Already Running", "Parsing is already in progress.")
            return

        self._save_current_settings()

        proxy_value = self.settings.get("proxy") or None
        headless_value = bool(self.settings.get("headless", False))
        ua_value = self.settings.get("user_agent") or None
        
        try:
            init_global_driver(proxy=proxy_value, headless=headless_value, user_agent=ua_value)
        except Exception as e:
            self.log(f"Не удалось инициализировать браузер: {e}")
            messagebox.showerror("Browser Error", f"Не удалось инициализировать браузер: {e}")
            return

        self.is_running = True
        self.is_paused = False
        self.should_stop = False
        self.parsed_count = 0
        self.skipped_count = 0

        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        self.pause_btn.config(text="Pause")

        self.log("Starting parser...")
        self.status_label.config(text="Running...", foreground="green")
        
        api_key = self.settings.get("deepseek_api_key") or ""
        if api_key:
            balance_info = descs.get_deepseek_balance(api_key)
            if balance_info:
                try:
                    balance_dollars = balance_info.get('balance_dollars', 0)
                    balance_str = f"{balance_dollars:.2f}"
                    self.balance_label.config(text=f"${balance_str}", foreground="green")
                    self.log(f"DeepSeek balance: ${balance_str}")
                    
                    self.settings["deepseek_balance"] = balance_str
                    descs.save_settings(self.settings)
                except Exception as e:
                    self.log(f"Ошибка при обработке информации о балансе: {e}")
            else:
                self.balance_label.config(text="Failed to fetch", foreground="red")
                self.log("Не удалось получить информацию о балансе")
        else:
            self.balance_label.config(text="No API key", foreground="orange")

        self.parsing_thread = threading.Thread(target=self.parse_worker, daemon=True)
        self.parsing_thread.start()

    def parse_worker(self):
        try:
            self.parse_products()
        except Exception as e:
            self.log(f"Error: {e}")
            self.status_label.config(text="Error", foreground="red")
            self.save_parsed_offers()
        finally:
            self.is_running = False
            self.start_btn.config(state="normal")
            self.pause_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")

    def parse_products(self):
        if not self.selected_file or not os.path.exists(self.selected_file):
            self.log(f"Input file not found: {self.selected_file}")
            self.status_label.config(text="Error", foreground="red")
            return
        
        new_soup = BeautifulSoup(open(self.selected_file, encoding='utf-8'), 'xml')
        parsed_ids = load_parsed_ids(PARSED_FILE)
        
        if os.path.exists(PARSED_FILE):
            self.old_soup = BeautifulSoup(open(PARSED_FILE, encoding='utf-8'), 'xml')
        else:
            self.old_soup = BeautifulSoup(str(new_soup), 'xml')
            for o in self.old_soup.find_all('offer'):
                o.extract()
            self.log("Created new parsed file base")

        def _move_iframe_from_video_to_descriptions(offer) -> bool:
            """
            If offer has <video><iframe .../></video>, remove iframe from <video>
            and append iframe markup to the end of <description> and <description_ua>.

            Requirement: keep iframe inside CDATA later (handled by wrap_descriptions_in_cdata).
            So we append iframe as a plain text node (it will become escaped in XML,
            then unescaped inside CDATA by wrap_descriptions_in_cdata()).
            """
            video_tag = offer.find("video")
            if not video_tag:
                return False
            iframe_tag = video_tag.find("iframe")
            if not iframe_tag:
                return False

            iframe_html = str(iframe_tag)
            iframe_tag.extract()

            # Remove <video> if it became empty (no nested tags and no meaningful text)
            try:
                if not (video_tag.get_text(strip=True) or video_tag.find(True)):
                    video_tag.extract()
            except Exception:
                pass

            # Ensure both description tags exist, then append iframe at the end
            for tag_name in ("description", "description_ua"):
                desc_tag = offer.find(tag_name)
                if not desc_tag:
                    desc_tag = new_soup.new_tag(tag_name)
                    offer.append(desc_tag)
                desc_tag.append("\n" + iframe_html)

            return True

        new_offers = []
        local_only_offers = []
        offers_to_parse = []

        for offer in new_soup.find_all('offer'):
            offer_id = get_offer_id(offer)
            if not offer_id or offer_id in parsed_ids:
                continue
            new_offers.append(offer)

            # Fast-path check (NO web parsing):
            # If iframe is inside <video> -> move it to description end.
            # IMPORTANT: existing <description> in input XML must NOT skip parsing.
            had_iframe_in_video = _move_iframe_from_video_to_descriptions(offer)
            if had_iframe_in_video:
                local_only_offers.append(offer)
            else:
                offers_to_parse.append(offer)

        self.total_offers = len(new_offers)
        self.update_stats()

        if not new_offers:
            self.log("No new offers to parse.")
            self.status_label.config(text="Complete", foreground="blue")
            return

        # If some offers already have descriptions, we can just move/wrap them without web parsing.
        if local_only_offers:
            self.log(
                f"Fast-path: {len(local_only_offers)} offers already contain descriptions "
                f"and/or <video><iframe>. They will be copied and normalized without web parsing."
            )

        if offers_to_parse:
            self.log(f"Found {len(offers_to_parse)} offers to parse from web")
        else:
            self.log("No offers require web parsing")

        self.parsed_offers = []
        skipped_offers_count = 0

        proxy_val = self.settings.get("proxy") or None
        headless_val = bool(self.settings.get("headless", False))
        ua_val = self.settings.get("user_agent") or None

        # 1) Copy & normalize offers that already contain descriptions (no web parsing)
        for offer in local_only_offers:
            self.parsed_offers.append(offer)
            self.parsed_count += 1
            self.update_stats()

            if len(self.parsed_offers) >= 5:
                self.log("Interim save: 5 items reached...")
                self.save_parsed_offers()
                self.parsed_offers = []
                descs._parsed_offers_backup = []

            if self.should_stop:
                self.log("Parsing stopped by user")
                break

        # 2) Web-parse only the remaining offers
        batch_size = 3
        for batch_start in range(0, len(offers_to_parse), batch_size):
            batch_end = min(batch_start + batch_size, len(offers_to_parse))
            batch_offers = offers_to_parse[batch_start:batch_end]
            
            # Keep human-readable numbering consistent with the full "new_offers" list
            # (local-only offers are processed first).
            batch_start_number = batch_start + len(local_only_offers)

            processed_batch = self.process_offers_batch_parallel(
                batch_offers, batch_start_number, len(new_offers),
                proxy_val, headless_val, ua_val
            )
            
            for offer_data in processed_batch:
                if offer_data['offer'] is not None:
                    self.parsed_offers.append(offer_data['offer'])
                    self.parsed_count += 1
                else:
                    self.skipped_count += 1
                    skipped_offers_count += 1
                
                self.update_stats()

            if len(self.parsed_offers) >= 5:
                self.log("Interim save: 5 items reached...")
                self.save_parsed_offers()
                self.parsed_offers = [] 
                descs._parsed_offers_backup = []
            
            if self.should_stop:
                self.log("Parsing stopped by user")
                break

        if self.parsed_offers:
            self.save_parsed_offers()
            self.parsed_offers = []

        # ===== АВТОМАТИЧНЕ ОБГОРТАННЯ В CDATA ПІСЛЯ ЗАВЕРШЕННЯ ПАРСИНГУ =====
        self.log("=" * 60)
        self.log("Обгортання описів у CDATA...")
        success, message = wrap_descriptions_in_cdata(PARSED_FILE)
        if success:
            self.log(f"✓ {message}")
        else:
            self.log(f"✗ {message}")
        self.log("=" * 60)

        self.status_label.config(text="Complete", foreground="blue")
        self.log(f"Parsing complete! Total parsed: {self.parsed_count}")

    def save_parsed_offers(self):
        if not self.parsed_offers or not self.old_soup:
            return

        container = None
        for name in ('offers', 'catalog', 'channel', 'shop', 'yml_catalog', 'root'):
            container = self.old_soup.find(name)
            if container:
                break
        if container is None:
            existing_offers = self.old_soup.find_all('offer')
            if existing_offers:
                container = existing_offers[0].parent
        if container is None:
            container = self.old_soup

        for offer in self.parsed_offers:
            offer_copy = BeautifulSoup(str(offer), 'xml').find('offer')
            if offer_copy is not None:
                container.append(offer_copy)

        self.log(f"Added {len(self.parsed_offers)} offers to {PARSED_FILE}")

        xml_str = str(self.old_soup)
        xml_str = xml_str.replace('</offer>', '</offer>\n')

        with open(PARSED_FILE, "w", encoding="utf-8") as f:
            f.write(xml_str)
        self.log(f"Saved {PARSED_FILE}")

    def toggle_pause(self):
        if not self.is_running:
            return

        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="Resume")
            self.status_label.config(text="Paused", foreground="orange")
            self.log("Paused")
        else:
            self.pause_btn.config(text="Pause")
            self.status_label.config(text="Running...", foreground="green")
            self.log("Resumed")

    def stop_parsing(self):
        if not messagebox.askyesno("Confirm Stop", "Are you sure you want to stop? Parsed data will be saved."):
            return

        self.should_stop = True
        self.is_paused = False
        self.log("Stopping...")
        self.status_label.config(text="Stopping...", foreground="orange")

    def export_languages(self):
        """Экспортирует данные в два XML файла (украинский и русский)."""
        success, message = descs.export_to_language_files()
        if success:
            self.log(message)
            messagebox.showinfo("Export Success", message)
        else:
            self.log(message)
            messagebox.showerror("Export Error", message)


def main():
    root = tk.Tk()
    gui = ParserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
