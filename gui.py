import asyncio
import logging
import threading
import customtkinter as ctk

from watcher import run_watcher
from core import MRKTClient
from scanner import scan_liquidity
from order_bot import run_mass_offers, clear_all_offers
from flipper import run_auto_flip
from sniper import run_sniper
from engine import run_engine
from config_manager import load_config, save_config, update_value
from controller import TerminalController
from tg_bot import TelegramBot
from db import SELL_FIXED_COST_TON

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class MRKTTerminal(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("MRKT Terminal PRO | Tank Edition")
        self.geometry("1150x780")
        self.minsize(1000, 600)

        # Флаг для контроля бесконечного цикла Снайпера
        self.sniper_active = False

        # [ENGINE] Флаг состояния фонового Ордер-Движка
        self.is_engine_running = False

        # [ENGINE] Словарь BooleanVar для фильтра фонов
        self.engine_bg_vars: dict[str, ctk.BooleanVar] = {}

        # asyncio loop в отдельном потоке
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.loop_thread.start()

        self.client = MRKTClient()

        # === Контроллер (общий фасад для GUI и Telegram) ===
        self.controller = TerminalController(self.client, self.loop)

        # === Telegram-бот (опционально, если есть .env) ===
        self.tg_bot: TelegramBot | None = None
        try:
            self.tg_bot = TelegramBot(self.controller)
            self.run_async(self.tg_bot.start())
            logging.getLogger("MRKT").info("Telegram-бот запущен в фоне")
        except Exception as e:
            logging.getLogger("MRKT").warning(f"Telegram-бот не запущен: {e}")

        # === Загружаем сохранённые настройки ===
        self.config = load_config()

        # Сетка: сайдбар + контент
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_frames()
        self.show_frame("dashboard")

        # Прокидываем GUI-логгеры в контроллер, чтобы из бота тоже шло в Textbox
        self.controller.engine_logger = self._log_engine
        self.controller.sniper_logger = self._log_sniper
        self.controller.offers_logger = self._log_offers
        self.controller.scan_logger = self._log

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- asyncio bridge ----------
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _on_close(self):
        self.sniper_active = False
        self.is_engine_running = False

        async def shutdown_tasks():
            # Сначала останавливаем Telegram-бота, чтобы он не цеплял уже закрытый клиент
            if self.tg_bot is not None:
                try:
                    await self.tg_bot.stop()
                except Exception:
                    pass

            await self.client.close()
            tasks = [
                t
                for t in asyncio.all_tasks(self.loop)
                if t is not asyncio.current_task(self.loop)
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            fut = asyncio.run_coroutine_threadsafe(shutdown_tasks(), self.loop)
            fut.result(timeout=3)
        except Exception:
            pass

        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()

    # ---------- UI: Сайдбар ----------
    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_rowconfigure(10, weight=1)

        title = ctk.CTkLabel(
            self.sidebar,
            text="MRKT\nTERMINAL",
            font=ctk.CTkFont(family="Consolas", size=24, weight="bold"),
            justify="center",
        )
        title.grid(row=0, column=0, padx=20, pady=(30, 30))

        buttons_config = [
            ("Дашборд", "dashboard"),
            ("Сканер ликвидности", "scanner"),
            ("Ордер-Движок", "engine"),
            ("Массовые операции", "mass_ops"),
            ("СНАЙПЕР", "sniper"),
            ("Калькулятор", "calculator"),
        ]

        self.nav_buttons = {}
        for idx, (text, frame_name) in enumerate(buttons_config, start=1):
            btn = ctk.CTkButton(
                self.sidebar,
                text=text,
                command=lambda f=frame_name: self.show_frame(f),
                anchor="w",
                height=45,
                font=ctk.CTkFont(
                    size=14,
                    weight="bold" if text in ["СНАЙПЕР", "Калькулятор"] else "normal",
                ),
                fg_color=(
                    "#1f538d"
                    if text not in ["СНАЙПЕР", "Калькулятор"]
                    else ("#27ae60" if text == "СНАЙПЕР" else "#8e44ad")
                ),
                hover_color=(
                    "#14375e"
                    if text not in ["СНАЙПЕР", "Калькулятор"]
                    else ("#1e8449" if text == "СНАЙПЕР" else "#732d91")
                ),
            )
            btn.grid(row=idx, column=0, padx=15, pady=8, sticky="ew")
            self.nav_buttons[frame_name] = btn

        self.status_label = ctk.CTkLabel(
            self.sidebar,
            text="● Система активна",
            text_color="#3ddc84",
            font=ctk.CTkFont(size=12),
        )
        self.status_label.grid(row=11, column=0, padx=15, pady=20, sticky="sw")

    # ---------- UI: Фреймы ----------
    def _build_frames(self):
        self.frames: dict[str, ctk.CTkFrame] = {}

        self.frames["dashboard"] = self._build_dashboard()
        self.frames["scanner"] = self._build_scanner()
        self.frames["engine"] = self._build_engine()
        self.frames["mass_ops"] = self._build_mass_ops()
        self.frames["sniper"] = self._build_sniper()
        self.frames["calculator"] = self._build_calculator()

        for f in self.frames.values():
            f.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
            f.grid_remove()

    def show_frame(self, name: str):
        for f in self.frames.values():
            f.grid_remove()
        self.frames[name].grid()

    # --- Дашборд ---
    def _build_dashboard(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        header = ctk.CTkLabel(
            frame, text="Дашборд и Портфель", font=ctk.CTkFont(size=26, weight="bold")
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        stats_card = ctk.CTkFrame(frame, corner_radius=10)
        stats_card.grid(row=1, column=0, padx=24, pady=12, sticky="ew")
        stats_card.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(
            stats_card,
            text="Свободный баланс",
            font=ctk.CTkFont(size=14),
            text_color="#9aa0a6",
        ).grid(row=0, column=0, padx=20, pady=(18, 4), sticky="w")
        self.balance_value = ctk.CTkLabel(
            stats_card, text="— TON", font=ctk.CTkFont(size=26, weight="bold")
        )
        self.balance_value.grid(row=1, column=0, padx=20, pady=(0, 18), sticky="w")

        ctk.CTkLabel(
            stats_card,
            text="Оценка портфеля",
            font=ctk.CTkFont(size=14),
            text_color="#9aa0a6",
        ).grid(row=0, column=1, padx=20, pady=(18, 4), sticky="w")
        self.portfolio_value = ctk.CTkLabel(
            stats_card,
            text="— TON",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#f1c40f",
        )
        self.portfolio_value.grid(row=1, column=1, padx=20, pady=(0, 18), sticky="w")

        ctk.CTkLabel(
            stats_card,
            text="Активов в холде",
            font=ctk.CTkFont(size=14),
            text_color="#9aa0a6",
        ).grid(row=0, column=2, padx=20, pady=(18, 4), sticky="w")
        self.items_count = ctk.CTkLabel(
            stats_card,
            text="— шт",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#3ddc84",
        )
        self.items_count.grid(row=1, column=2, padx=20, pady=(0, 18), sticky="w")

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.grid(row=2, column=0, padx=24, pady=4, sticky="ew")

        self.refresh_btn = ctk.CTkButton(
            controls,
            text="ОБНОВИТЬ ДАННЫЕ",
            command=self._refresh_dashboard,
            height=42,
            width=220,
        )
        self.refresh_btn.grid(row=0, column=0, sticky="w")

        self.dashboard_status = ctk.CTkLabel(controls, text="", text_color="#9aa0a6")
        self.dashboard_status.grid(row=0, column=1, padx=16, sticky="w")

        self.inventory_log = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=13)
        )
        self.inventory_log.grid(row=3, column=0, padx=24, pady=(12, 24), sticky="nsew")
        self.inventory_log.configure(state="disabled")

        return frame

    # --- Сканер ---
    def _build_scanner(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        header = ctk.CTkLabel(
            frame, text="Сканер ликвидности", font=ctk.CTkFont(size=26, weight="bold")
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        self.scan_btn = ctk.CTkButton(
            frame,
            text="Запустить сканирование",
            command=self._run_scan,
            height=42,
            width=240,
        )
        self.scan_btn.grid(row=1, column=0, padx=24, pady=8, sticky="w")

        self.scan_log = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.scan_log.grid(row=2, column=0, padx=24, pady=(12, 24), sticky="nsew")
        self.scan_log.configure(state="disabled")

        return frame

    # --- Ордер-Движок ---
    def _build_engine(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        header = ctk.CTkLabel(
            frame,
            text="Боевой Ордер-Движок",
            font=ctk.CTkFont(size=26, weight="bold"),
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        # Ползунок скидки для движка
        slider_box = ctk.CTkFrame(frame, corner_radius=10)
        slider_box.grid(row=1, column=0, padx=24, pady=8, sticky="ew")
        slider_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            slider_box, text="Скидка от floor:", font=ctk.CTkFont(size=14)
        ).grid(row=0, column=0, padx=(16, 12), pady=14, sticky="w")
        self.engine_discount_value = float(self.config.get("engine_discount", 15.0))
        self.engine_discount_label = ctk.CTkLabel(
            slider_box,
            text=f"{self.engine_discount_value:.0f} %",
            font=ctk.CTkFont(size=16, weight="bold"),
            width=70,
        )
        self.engine_discount_label.grid(
            row=0, column=2, padx=(12, 16), pady=14, sticky="e"
        )
        self.engine_discount_slider = ctk.CTkSlider(
            slider_box,
            from_=1,
            to=90,
            number_of_steps=89,
            command=self._on_engine_discount_change,
        )
        self.engine_discount_slider.set(self.engine_discount_value)
        self.engine_discount_slider.grid(row=0, column=1, padx=8, pady=14, sticky="ew")

        # Блок фильтра фонов
        bg_box = ctk.CTkFrame(frame, corner_radius=10)
        bg_box.grid(row=2, column=0, padx=24, pady=8, sticky="ew")
        bg_box.grid_columnconfigure((1, 2, 3, 4), weight=0)

        ctk.CTkLabel(
            bg_box,
            text="Фильтр фонов (Backdrops):",
            font=ctk.CTkFont(size=14),
        ).grid(row=0, column=0, padx=(16, 12), pady=14, sticky="w")

        bg_options = ["Любой", "Black", "Gold", "Satin Gold"]
        saved_bgs = self.config.get("engine_backdrops", {})
        for idx, bg_name in enumerate(bg_options):
            default_val = bool(saved_bgs.get(bg_name, bg_name == "Любой"))
            var = ctk.BooleanVar(value=default_val)
            self.engine_bg_vars[bg_name] = var

            cb = ctk.CTkCheckBox(
                bg_box,
                text=bg_name,
                variable=var,
                onvalue=True,
                offvalue=False,
                command=lambda b=bg_name: self._on_bg_toggle(b),
                font=ctk.CTkFont(size=13),
            )
            cb.grid(row=0, column=idx + 1, padx=10, pady=14, sticky="w")

        # Тогл-кнопка движка + статус
        engine_box = ctk.CTkFrame(frame, corner_radius=10, fg_color="transparent")
        engine_box.grid(row=3, column=0, padx=24, pady=(8, 4), sticky="ew")

        self.engine_btn = ctk.CTkButton(
            engine_box,
            text="Запустить Движок",
            command=self._toggle_engine,
            height=46,
            width=260,
            fg_color="#27ae60",
            hover_color="#1e8449",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.engine_btn.grid(row=0, column=0, padx=(0, 12), pady=0, sticky="w")

        self.engine_status = ctk.CTkLabel(
            engine_box,
            text="Движок: выключен",
            text_color="#9aa0a6",
            font=ctk.CTkFont(size=13),
        )
        self.engine_status.grid(row=0, column=1, padx=(8, 0), pady=0, sticky="w")

        hint = ctk.CTkLabel(
            frame,
            text="Движок работает в фоне: автоматически держит офферы по заданной скидке.",
            font=ctk.CTkFont(size=12),
            text_color="#9aa0a6",
        )
        hint.grid(row=4, column=0, padx=24, pady=(0, 4), sticky="w")

        self.engine_log = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.engine_log.grid(row=5, column=0, padx=24, pady=(12, 24), sticky="nsew")
        self.engine_log.configure(state="disabled")

        return frame

    # Логика взаимоисключения чекбоксов фильтра фонов + персист
    def _on_bg_toggle(self, changed_bg: str):
        any_var = self.engine_bg_vars.get("Любой")

        if changed_bg == "Любой":
            if any_var is None:
                return
            if any_var.get():
                for bg, var in self.engine_bg_vars.items():
                    if bg != "Любой":
                        var.set(False)
        else:
            changed_var = self.engine_bg_vars.get(changed_bg)
            if changed_var is not None and changed_var.get():
                if any_var is not None:
                    any_var.set(False)

        # Персист одной транзакцией
        snapshot = {name: var.get() for name, var in self.engine_bg_vars.items()}
        cfg = load_config()
        cfg["engine_backdrops"] = snapshot
        save_config(cfg)
        self.config = cfg

    # --- Массовые операции ---
    def _build_mass_ops(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(4, weight=1)

        header = ctk.CTkLabel(
            frame,
            text="Массовые операции",
            font=ctk.CTkFont(size=26, weight="bold"),
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        slider_box = ctk.CTkFrame(frame, corner_radius=10)
        slider_box.grid(row=1, column=0, padx=24, pady=8, sticky="ew")
        slider_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            slider_box, text="Скидка от floor:", font=ctk.CTkFont(size=14)
        ).grid(row=0, column=0, padx=(16, 12), pady=14, sticky="w")
        self.offers_discount_value = float(self.config.get("offers_discount", 15.0))
        self.offers_discount_label = ctk.CTkLabel(
            slider_box,
            text=f"{self.offers_discount_value:.0f} %",
            font=ctk.CTkFont(size=16, weight="bold"),
            width=70,
        )
        self.offers_discount_label.grid(
            row=0, column=2, padx=(12, 16), pady=14, sticky="e"
        )
        self.offers_discount_slider = ctk.CTkSlider(
            slider_box,
            from_=1,
            to=90,
            number_of_steps=89,
            command=self._on_offers_discount_change,
        )
        self.offers_discount_slider.set(self.offers_discount_value)
        self.offers_discount_slider.grid(row=0, column=1, padx=8, pady=14, sticky="ew")

        controls_box = ctk.CTkFrame(frame, corner_radius=10, fg_color="transparent")
        controls_box.grid(row=2, column=0, padx=24, pady=(8, 4), sticky="ew")

        self.offers_btn = ctk.CTkButton(
            controls_box,
            text="Запустить массовые офферы",
            command=self._run_mass_offers,
            height=42,
            width=260,
        )
        self.offers_btn.grid(row=0, column=0, padx=(0, 12), pady=0, sticky="w")

        self.clear_btn = ctk.CTkButton(
            controls_box,
            text="ОТМЕНИТЬ ВСЕ ОФФЕРЫ",
            command=self._exec_clear,
            height=42,
            width=260,
            fg_color="#c0392b",
            hover_color="#922b21",
        )
        self.clear_btn.grid(row=0, column=1, padx=(0, 12), pady=0, sticky="w")

        self.flip_btn = ctk.CTkButton(
            controls_box,
            text="АВТО-ФЛИП ИНВЕНТАРЯ",
            command=self._exec_flip,
            height=42,
            width=260,
            fg_color="#2980b9",
            hover_color="#1f6391",
        )
        self.flip_btn.grid(row=0, column=2, padx=0, pady=0, sticky="w")

        self.offers_status = ctk.CTkLabel(frame, text="", text_color="#9aa0a6")
        self.offers_status.grid(row=3, column=0, padx=24, pady=4, sticky="w")

        self.offers_log = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.offers_log.grid(row=4, column=0, padx=24, pady=(12, 24), sticky="nsew")
        self.offers_log.configure(state="disabled")

        return frame

    # --- Снайпер ---
    def _build_sniper(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(4, weight=1)

        header = ctk.CTkLabel(
            frame,
            text="Снайпер (Агрессивный выкуп)",
            font=ctk.CTkFont(size=26, weight="bold"),
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        slider_box = ctk.CTkFrame(frame, corner_radius=10)
        slider_box.grid(row=1, column=0, padx=24, pady=8, sticky="ew")
        slider_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            slider_box, text="Целевая скидка (от флора):", font=ctk.CTkFont(size=14)
        ).grid(row=0, column=0, padx=(16, 12), pady=14, sticky="w")
        self.sniper_discount_val = float(self.config.get("sniper_discount", 20.0))
        self.sniper_discount_lbl = ctk.CTkLabel(
            slider_box,
            text=f"{self.sniper_discount_val:.0f} %",
            font=ctk.CTkFont(size=16, weight="bold"),
            width=70,
        )
        self.sniper_discount_lbl.grid(
            row=0, column=2, padx=(12, 16), pady=14, sticky="e"
        )
        self.sniper_slider = ctk.CTkSlider(
            slider_box,
            from_=1,
            to=80,
            number_of_steps=79,
            command=self._on_sniper_discount_change,
        )
        self.sniper_slider.set(self.sniper_discount_val)
        self.sniper_slider.grid(row=0, column=1, padx=8, pady=14, sticky="ew")

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.grid(row=2, column=0, padx=24, pady=(8, 4), sticky="ew")

        self.sniper_start_btn = ctk.CTkButton(
            controls,
            text="ЗАПУСТИТЬ СНАЙПЕРА",
            command=self._start_sniper,
            height=50,
            width=260,
            fg_color="#27ae60",
            hover_color="#1e8449",
            font=ctk.CTkFont(weight="bold"),
        )
        self.sniper_start_btn.grid(row=0, column=0, padx=(0, 15), pady=0, sticky="w")

        self.sniper_stop_btn = ctk.CTkButton(
            controls,
            text="ОСТАНОВИТЬ",
            command=self._stop_sniper,
            height=50,
            width=180,
            fg_color="#c0392b",
            hover_color="#922b21",
            state="disabled",
            font=ctk.CTkFont(weight="bold"),
        )
        self.sniper_stop_btn.grid(row=0, column=1, padx=0, pady=0, sticky="w")

        self.sniper_status = ctk.CTkLabel(
            frame, text="Ожидание запуска...", text_color="#9aa0a6"
        )
        self.sniper_status.grid(row=3, column=0, padx=24, pady=4, sticky="w")

        self.sniper_log = ctk.CTkTextbox(
            frame, wrap="none", font=ctk.CTkFont(family="Consolas", size=13)
        )
        self.sniper_log.grid(row=4, column=0, padx=24, pady=(12, 24), sticky="nsew")
        self.sniper_log.configure(state="disabled")

        return frame

    # --- Калькулятор ---
    def _build_calculator(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid_columnconfigure(0, weight=1)

        self.calc_undercut_ton = float(self.config.get("calc_undercut_ton", 0.001))

        header = ctk.CTkLabel(
            frame, text="Калькулятор Снайпера", font=ctk.CTkFont(size=26, weight="bold")
        )
        header.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")

        subtitle = ctk.CTkLabel(
            frame,
            text="Расчёт прибыли: выкуп по дисконту → продажа с подрезкой флора",
            font=ctk.CTkFont(size=13),
            text_color="#9aa0a6",
        )
        subtitle.grid(row=1, column=0, padx=24, pady=(0, 12), sticky="w")

        input_box = ctk.CTkFrame(frame, corner_radius=10)
        input_box.grid(row=2, column=0, padx=24, pady=8, sticky="ew")
        input_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            input_box, text="Текущий Флор (TON):", font=ctk.CTkFont(size=14)
        ).grid(row=0, column=0, padx=(16, 12), pady=(16, 8), sticky="w")
        self.calc_floor_entry = ctk.CTkEntry(
            input_box,
            placeholder_text="Например: 12.345",
            height=36,
            font=ctk.CTkFont(size=14),
        )
        self.calc_floor_entry.grid(
            row=0, column=1, columnspan=2, padx=(0, 16), pady=(16, 8), sticky="ew"
        )

        ctk.CTkLabel(
            input_box, text="Целевой дисконт (%):", font=ctk.CTkFont(size=14)
        ).grid(row=1, column=0, padx=(16, 12), pady=8, sticky="w")
        self.calc_discount_value = float(self.config.get("calc_discount", 15.0))
        self.calc_discount_slider = ctk.CTkSlider(
            input_box,
            from_=1,
            to=80,
            number_of_steps=79,
            command=self._on_calc_discount_change,
        )
        self.calc_discount_slider.set(self.calc_discount_value)
        self.calc_discount_slider.grid(row=1, column=1, padx=8, pady=8, sticky="ew")

        self.calc_discount_label = ctk.CTkLabel(
            input_box,
            text=f"{self.calc_discount_value:.0f} %",
            font=ctk.CTkFont(size=16, weight="bold"),
            width=70,
        )
        self.calc_discount_label.grid(
            row=1, column=2, padx=(12, 16), pady=8, sticky="e"
        )

        ctk.CTkLabel(
            input_box, text="Подрезка конкурентов:", font=ctk.CTkFont(size=14)
        ).grid(row=2, column=0, padx=(16, 12), pady=(8, 16), sticky="w")
        ctk.CTkLabel(
            input_box,
            text=f"{self.calc_undercut_ton:.3f} TON",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#f1c40f",
        ).grid(row=2, column=1, columnspan=2, padx=(0, 16), pady=(8, 16), sticky="w")

        self.calc_btn = ctk.CTkButton(
            frame,
            text="РАССЧИТАТЬ ПРОФИТ",
            command=self._exec_calc,
            height=46,
            width=280,
            fg_color="#8e44ad",
            hover_color="#732d91",
            text_color="#ffffff",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.calc_btn.grid(row=3, column=0, padx=24, pady=(12, 8), sticky="w")

        result_box = ctk.CTkFrame(frame, corner_radius=10)
        result_box.grid(row=4, column=0, padx=24, pady=(12, 24), sticky="ew")
        result_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            result_box,
            text="Цена выкупа:",
            font=ctk.CTkFont(size=15),
            text_color="#9aa0a6",
        ).grid(row=0, column=0, padx=(20, 12), pady=(18, 6), sticky="w")
        self.calc_buy_value = ctk.CTkLabel(
            result_box,
            text="— TON",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#ffffff",
        )
        self.calc_buy_value.grid(
            row=0, column=1, padx=(0, 20), pady=(18, 6), sticky="w"
        )

        ctk.CTkLabel(
            result_box,
            text="Цена продажи:",
            font=ctk.CTkFont(size=15),
            text_color="#9aa0a6",
        ).grid(row=1, column=0, padx=(20, 12), pady=6, sticky="w")
        self.calc_sell_value = ctk.CTkLabel(
            result_box,
            text="— TON",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#ffffff",
        )
        self.calc_sell_value.grid(row=1, column=1, padx=(0, 20), pady=6, sticky="w")

        ctk.CTkLabel(
            result_box,
            text="Чистая прибыль:",
            font=ctk.CTkFont(size=15),
            text_color="#9aa0a6",
        ).grid(row=2, column=0, padx=(20, 12), pady=6, sticky="w")
        self.calc_profit_value = ctk.CTkLabel(
            result_box,
            text="— TON",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#3ddc84",
        )
        self.calc_profit_value.grid(row=2, column=1, padx=(0, 20), pady=6, sticky="w")

        ctk.CTkLabel(
            result_box,
            text="ROI (Окупаемость):",
            font=ctk.CTkFont(size=15),
            text_color="#9aa0a6",
        ).grid(row=3, column=0, padx=(20, 12), pady=(6, 18), sticky="w")
        self.calc_roi_value = ctk.CTkLabel(
            result_box,
            text="— %",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#3ddc84",
        )
        self.calc_roi_value.grid(
            row=3, column=1, padx=(0, 20), pady=(6, 18), sticky="w"
        )

        self.calc_status = ctk.CTkLabel(frame, text="", text_color="#9aa0a6")
        self.calc_status.grid(row=5, column=0, padx=24, pady=(0, 12), sticky="w")

        return frame

    # ---------- Логика интерфейса ----------
    def _log(self, text: str):
        self.after(
            0,
            lambda: (
                self.scan_log.configure(state="normal"),
                self.scan_log.insert("end", text + "\n"),
                self.scan_log.see("end"),
                self.scan_log.configure(state="disabled"),
            ),
        )

    def _log_offers(self, text: str):
        self.after(
            0,
            lambda: (
                self.offers_log.configure(state="normal"),
                self.offers_log.insert("end", text + "\n"),
                self.offers_log.see("end"),
                self.offers_log.configure(state="disabled"),
            ),
        )

    def _log_engine(self, text: str):
        self.after(
            0,
            lambda: (
                self.engine_log.configure(state="normal"),
                self.engine_log.insert("end", text + "\n"),
                self.engine_log.see("end"),
                self.engine_log.configure(state="disabled"),
            ),
        )

    def _log_sniper(self, text: str):
        self.after(
            0,
            lambda: (
                self.sniper_log.configure(state="normal"),
                self.sniper_log.insert("end", text + "\n"),
                self.sniper_log.see("end"),
                self.sniper_log.configure(state="disabled"),
            ),
        )

    def _on_engine_discount_change(self, value: float):
        self.engine_discount_value = float(value)
        self.engine_discount_label.configure(text=f"{self.engine_discount_value:.0f} %")
        update_value("engine_discount", self.engine_discount_value)

    def _on_offers_discount_change(self, value: float):
        self.offers_discount_value = float(value)
        self.offers_discount_label.configure(text=f"{self.offers_discount_value:.0f} %")
        update_value("offers_discount", self.offers_discount_value)

    def _on_sniper_discount_change(self, value: float):
        self.sniper_discount_val = float(value)
        self.sniper_discount_lbl.configure(text=f"{self.sniper_discount_val:.0f} %")
        update_value("sniper_discount", self.sniper_discount_val)

    def _on_calc_discount_change(self, value: float):
        self.calc_discount_value = float(value)
        self.calc_discount_label.configure(text=f"{self.calc_discount_value:.0f} %")
        update_value("calc_discount", self.calc_discount_value)

    # ---------- Исполнительные методы ----------
    def _refresh_dashboard(self):
        self.refresh_btn.configure(state="disabled", text="СКАНИРУЮ СЕЙФ...")
        self.dashboard_status.configure(
            text="Связь с сервером...", text_color="#9aa0a6"
        )

        self.inventory_log.configure(state="normal")
        self.inventory_log.delete("1.0", "end")
        self.inventory_log.configure(state="disabled")

        async def task():
            try:
                bal = await self.client.get_balance()
                self.after(
                    0, lambda: self.balance_value.configure(text=f"{bal:.3f} TON")
                )

                gifts = await self.client.get_inventory()

                total_value_nano = 0
                log_text = "[*] СОСТАВ ПОРТФЕЛЯ (На балансе):\n" + "-" * 60 + "\n"

                if not gifts:
                    log_text += "Инвентарь пуст. Запускай Ордер-Движок!\n"
                else:
                    for idx, gift in enumerate(gifts, 1):
                        name = gift.get("collectionName", "Unknown")
                        num = gift.get("number", "—")
                        model = gift.get("modelName", "—")

                        floor_nano = gift.get("floorPriceNanoTONsByCollection", 0) or 0
                        total_value_nano += floor_nano
                        floor_ton = floor_nano / 10**9

                        log_text += f"{idx:02d}. {name:<15} #{num:<6} | Model: {model:<10} | Флор: {floor_ton:.2f} TON\n"

                total_value_ton = total_value_nano / 10**9

                self.after(
                    0,
                    lambda: self.portfolio_value.configure(
                        text=f"~ {total_value_ton:.2f} TON"
                    ),
                )
                self.after(
                    0, lambda: self.items_count.configure(text=f"{len(gifts)} шт")
                )

                self.after(0, lambda: self.inventory_log.configure(state="normal"))
                self.after(0, lambda: self.inventory_log.insert("end", log_text))
                self.after(0, lambda: self.inventory_log.configure(state="disabled"))

                self.after(
                    0,
                    lambda: self.dashboard_status.configure(
                        text="Данные синхронизированы", text_color="#3ddc84"
                    ),
                )

            except Exception as e:
                self.after(
                    0,
                    lambda: self.dashboard_status.configure(
                        text=f"Ошибка: {e}", text_color="#ff6b6b"
                    ),
                )
            finally:
                self.after(
                    0,
                    lambda: self.refresh_btn.configure(
                        state="normal", text="ОБНОВИТЬ ДАННЫЕ"
                    ),
                )

        self.run_async(task())

    def _run_scan(self):
        self.scan_btn.configure(state="disabled", text="Сканирование...")
        self.scan_log.configure(state="normal")
        self.scan_log.delete("1.0", "end")
        self.scan_log.configure(state="disabled")

        async def task():
            try:
                await scan_liquidity(self.client, log=self._log)
                self._log("[✓] Сканирование завершено.")
            except Exception as e:
                self._log(f"[!] Ошибка сканирования: {e}")
            finally:
                self.after(
                    0,
                    lambda: self.scan_btn.configure(
                        state="normal", text="Запустить сканирование"
                    ),
                )

        self.run_async(task())

    def _run_mass_offers(self):
        discount = float(self.offers_discount_value)
        self.offers_btn.configure(state="disabled", text="Работаю...")
        self.offers_status.configure(
            text=f"Постановка офферов с дисконтом {discount:.0f}%...",
            text_color="#9aa0a6",
        )

        async def task():
            try:
                await run_mass_offers(
                    self.client, discount_percent=discount, log=self._log_offers
                )
                self.after(
                    0,
                    lambda: self.offers_status.configure(
                        text="Готово", text_color="#3ddc84"
                    ),
                )
            except Exception as e:
                self.after(
                    0,
                    lambda: self.offers_status.configure(
                        text=f"Ошибка: {e}", text_color="#ff6b6b"
                    ),
                )
            finally:
                self.after(
                    0,
                    lambda: self.offers_btn.configure(
                        state="normal", text="Запустить массовые офферы"
                    ),
                )

        self.run_async(task())

    def _exec_clear(self):
        self.clear_btn.configure(state="disabled", text="Чищу...")

        async def task():
            try:
                await clear_all_offers(self.client, log_func=self._log_offers)
            finally:
                self.after(
                    0,
                    lambda: self.clear_btn.configure(
                        state="normal", text="ОТМЕНИТЬ ВСЕ ОФФЕРЫ"
                    ),
                )

        self.run_async(task())

    def _exec_flip(self):
        self.flip_btn.configure(state="disabled", text="Флипаю...")

        async def task():
            try:
                await run_auto_flip(self.client, log_func=self._log_offers)
            finally:
                self.after(
                    0,
                    lambda: self.flip_btn.configure(
                        state="normal", text="АВТО-ФЛИП ИНВЕНТАРЯ"
                    ),
                )

        self.run_async(task())

    # Тогл-кнопка фонового Ордер-Движка
    def _toggle_engine(self):
        if not self.is_engine_running:
            self.is_engine_running = True
            self.engine_btn.configure(
                text="Остановить Движок",
                fg_color="#c0392b",
                hover_color="#922b21",
            )
            self.engine_status.configure(text="Движок: работает", text_color="#3ddc84")
            self._log_engine("[*] Ордер-Движок запущен в фоне.")

            async def task():
                try:
                    discount = float(self.engine_discount_value)

                    if (
                        self.engine_bg_vars.get("Любой")
                        and self.engine_bg_vars["Любой"].get()
                    ):
                        selected_bgs: list[str] = []
                    else:
                        selected_bgs = [
                            name
                            for name, var in self.engine_bg_vars.items()
                            if name != "Любой" and var.get()
                        ]

                    if selected_bgs:
                        self._log_engine(f"[i] Фильтр фонов: {', '.join(selected_bgs)}")
                    else:
                        self._log_engine("[i] Фильтр фонов: Любой (без ограничений)")

                    asyncio.create_task(
                        run_watcher(
                            self.client, is_running_flag=lambda: self.is_engine_running
                        )
                    )

                    await run_engine(
                        self.client,
                        discount_percent=discount,
                        log=self._log_engine,
                        is_running_flag=lambda: self.is_engine_running,
                        target_backdrops=selected_bgs,
                    )
                except Exception as e:
                    self._log_engine(f"[!] Критическая ошибка Движка: {e}")
                finally:
                    self.is_engine_running = False
                    self.after(
                        0,
                        lambda: self.engine_btn.configure(
                            text="Запустить Движок",
                            fg_color="#27ae60",
                            hover_color="#1e8449",
                        ),
                    )
                    self.after(
                        0,
                        lambda: self.engine_status.configure(
                            text="Движок: выключен", text_color="#9aa0a6"
                        ),
                    )

            self.run_async(task())
        else:
            self.is_engine_running = False
            self.engine_btn.configure(
                text="Запустить Движок",
                fg_color="#27ae60",
                hover_color="#1e8449",
            )
            self.engine_status.configure(
                text="Движок: останавливается...", text_color="#f1c40f"
            )
            self._log_engine("[i] Сигнал на остановку Движка отправлен.")

    def _start_sniper(self):
        self.sniper_active = True
        self.sniper_start_btn.configure(state="disabled", text="СНАЙПЕР В ЗАСАДЕ...")
        self.sniper_slider.configure(state="disabled")
        self.sniper_stop_btn.configure(state="normal")
        self.sniper_status.configure(
            text="Радар активирован. Поиск целей...", text_color="#3ddc84"
        )

        self.sniper_log.configure(state="normal")
        self.sniper_log.delete("1.0", "end")
        self.sniper_log.configure(state="disabled")

        discount = float(self.sniper_discount_val)

        async def task():
            try:
                await run_sniper(
                    self.client,
                    target_discount_percent=discount,
                    log=self._log_sniper,
                    is_running_flag=lambda: self.sniper_active,
                )
            except Exception as e:
                self._log_sniper(f"[!] Критическая ошибка радара: {e}")
            finally:
                self.after(
                    0,
                    lambda: self.sniper_start_btn.configure(
                        state="normal", text="ЗАПУСТИТЬ СНАЙПЕРА"
                    ),
                )
                self.after(0, lambda: self.sniper_slider.configure(state="normal"))
                self.after(0, lambda: self.sniper_stop_btn.configure(state="disabled"))
                self.after(
                    0,
                    lambda: self.sniper_status.configure(
                        text="Снайпер остановлен", text_color="#9aa0a6"
                    ),
                )

        self.run_async(task())

    def _stop_sniper(self):
        self.sniper_active = False
        self.sniper_stop_btn.configure(state="disabled", text="ОСТАНАВЛИВАЮ...")
        self.sniper_status.configure(
            text="Сигнал на отбой отправлен. Сворачиваю оборудование...",
            text_color="#f1c40f",
        )

    def _exec_calc(self):
        raw = self.calc_floor_entry.get().strip().replace(",", ".")
        try:
            floor = float(raw)
            if floor <= 0:
                raise ValueError("Флор должен быть > 0")
        except Exception as e:
            self.calc_status.configure(
                text=f"Ошибка ввода флора: {e}", text_color="#ff6b6b"
            )
            self.calc_buy_value.configure(text="— TON")
            self.calc_sell_value.configure(text="— TON")
            self.calc_profit_value.configure(text="— TON", text_color="#3ddc84")
            self.calc_roi_value.configure(text="— %", text_color="#3ddc84")
            return

        discount = float(self.calc_discount_value)
        multiplier = 1.0 - (discount / 100.0)

        buy_price = floor * multiplier
        sell_price = floor - self.calc_undercut_ton
        profit = sell_price - buy_price - SELL_FIXED_COST_TON
        roi = (profit / buy_price * 100.0) if buy_price > 0 else 0.0

        profit_color = "#3ddc84" if profit >= 0 else "#ff6b6b"
        roi_color = "#3ddc84" if roi >= 0 else "#ff6b6b"

        self.calc_buy_value.configure(text=f"{buy_price:.4f} TON")
        self.calc_sell_value.configure(text=f"{sell_price:.4f} TON")
        self.calc_profit_value.configure(
            text=f"{profit:+.4f} TON", text_color=profit_color
        )
        self.calc_roi_value.configure(text=f"{roi:+.2f} %", text_color=roi_color)
        self.calc_status.configure(
            text=(
                f"Floor={floor:.4f} | Discount={discount:.0f}% | "
                f"Undercut={self.calc_undercut_ton:.3f} | "
                f"FixCost={SELL_FIXED_COST_TON:.2f} TON"
            ),
            text_color="#9aa0a6",
        )


def main():
    app = MRKTTerminal()
    app.mainloop()


if __name__ == "__main__":
    main()
