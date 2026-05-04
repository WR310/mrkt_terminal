# tg_bot.py
import os
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from controller import TerminalController
from config_manager import load_config

log = logging.getLogger("TG_BOT")


# ============================================================
#                     ENV HELPERS
# ============================================================
def _get_admin_id() -> int:
    raw = os.getenv("TG_ADMIN_ID", "").strip()
    if not raw:
        raise RuntimeError("TG_ADMIN_ID не задан в .env")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"TG_ADMIN_ID должен быть числом, получено: {raw!r}")


def _get_token() -> str:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TG_BOT_TOKEN не задан в .env")
    return token


# ============================================================
#                     LABELS / KEYBOARDS
# ============================================================
BTN_STATUS = "📊 Статус"
BTN_ENGINE = "⚙️ Движок"
BTN_SNIPER = "🎯 Снайпер"
BTN_OPS = "🛠 Операции"
BTN_CALC = "🧮 Калькулятор"


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_ENGINE)],
            [KeyboardButton(text=BTN_SNIPER), KeyboardButton(text=BTN_OPS)],
            [KeyboardButton(text=BTN_CALC)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери раздел…",
    )


def engine_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Старт", callback_data="engine:start"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="engine:stop"),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Настроить скидку", callback_data="engine:setdisc"
                )
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="engine:refresh")],
        ]
    )


def sniper_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Старт", callback_data="sniper:start"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="sniper:stop"),
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Настроить цель", callback_data="sniper:setdisc"
                )
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="sniper:refresh")],
        ]
    )


def ops_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔎 Сканер", callback_data="ops:scan"),
                InlineKeyboardButton(
                    text="📤 Масс. офферы", callback_data="ops:mass_offers"
                ),
            ],
            [
                InlineKeyboardButton(text="♻️ Авто-флип", callback_data="ops:flip"),
                InlineKeyboardButton(text="🧹 Снять офферы", callback_data="ops:clear"),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Дисконт офферов", callback_data="ops:setdisc_offers"
                )
            ],
        ]
    )


def calc_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧮 Рассчитать профит", callback_data="calc:run"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Настроить дисконт", callback_data="calc:setdisc"
                )
            ],
        ]
    )


def discount_inline_kb(target: str) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура подстройки скидок (target ∈ engine|sniper|offers|calc).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="-5%", callback_data=f"disc:{target}:-5"),
                InlineKeyboardButton(text="-1%", callback_data=f"disc:{target}:-1"),
                InlineKeyboardButton(text="+1%", callback_data=f"disc:{target}:+1"),
                InlineKeyboardButton(text="+5%", callback_data=f"disc:{target}:+5"),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Готово", callback_data=f"disc:{target}:done"
                )
            ],
        ]
    )


# ============================================================
#                     FSM STATES
# ============================================================
class DiscountFSM(StatesGroup):
    waiting_value = State()  # ожидание ручного ввода скидки


class CalcFSM(StatesGroup):
    waiting_floor = State()  # ожидание ввода флор-цены


# ============================================================
#                     FORMAT HELPERS
# ============================================================
def _fmt(x, suffix=""):
    return f"{x:.3f}{suffix}" if isinstance(x, (int, float)) else "—"


def _disc_field(cfg_key: str, default: float = 15.0) -> float:
    cfg = load_config()
    try:
        return float(cfg.get(cfg_key, default))
    except (TypeError, ValueError):
        return default


# ============================================================
#                     TELEGRAM BOT (TRON)
# ============================================================
class TelegramBot:
    """
    aiogram 3.x — единый Трон управления через кнопки.
    Никаких ручных команд: только Reply-клавиатура + Inline-кнопки.
    """

    def __init__(self, controller: TerminalController):
        self.controller = controller
        self.admin_id: int = _get_admin_id()
        self.token: str = _get_token()

        self.bot = Bot(
            token=self.token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.router = Router()
        self.dp.include_router(self.router)

        self._polling_task: Optional[asyncio.Task] = None
        self._polling_future = None

        self._register_security_middleware()
        self._register_handlers()

    # ---------- SECURITY ----------
    def _register_security_middleware(self) -> None:
        admin_id = self.admin_id

        @self.dp.message.middleware()
        async def admin_only_msg(handler, event: Message, data):
            uid = event.from_user.id if event.from_user else None
            if uid != admin_id:
                log.warning("Отбит чужой запрос от user_id=%s text=%r", uid, event.text)
                return
            return await handler(event, data)

        @self.dp.callback_query.middleware()
        async def admin_only_cb(handler, event: CallbackQuery, data):
            uid = event.from_user.id if event.from_user else None
            if uid != admin_id:
                log.warning(
                    "Отбит чужой callback от user_id=%s data=%r", uid, event.data
                )
                try:
                    await event.answer("⛔️", show_alert=False)
                except Exception:
                    pass
                return
            return await handler(event, data)

    # ============================================================
    #                     HANDLERS
    # ============================================================
    def _register_handlers(self) -> None:
        r = self.router
        ctrl = self.controller

        # ---------- /start ----------
        @r.message(CommandStart())
        async def on_start(m: Message, state: FSMContext):
            await state.clear()
            await m.answer(
                "🤖 <b>MRKT Terminal — Трон</b>\n"
                "Управление через нижние кнопки. Никаких ручных команд.\n\n"
                "Снизу выбери раздел.",
                reply_markup=main_kb(),
            )

        # ============================================================
        #                     STATUS
        # ============================================================
        async def render_status(m: Message):
            await m.answer("⏳ Собираю данные...")
            s = await ctrl.status()
            engine_state = "🟢 ON" if s["engine"] else "🔴 OFF"
            sniper_state = "🟢 ON" if s["sniper"] else "🔴 OFF"
            txt = (
                "<b>📊 STATUS</b>\n"
                f"Баланс: <b>{_fmt(s['balance'], ' TON')}</b>\n"
                f"Портфель: <b>{_fmt(s['portfolio_ton'], ' TON')}</b>\n"
                f"Активов: <b>{s['items'] if s['items'] is not None else '—'}</b>\n\n"
                f"Движок: {engine_state} (скидка {s['engine_discount']}%)\n"
                f"Снайпер: {sniper_state} (цель {s['sniper_discount']}%)\n"
                f"Офферы: дисконт {s['offers_discount']}%"
            )
            await m.answer(txt, reply_markup=main_kb())

        @r.message(F.text == BTN_STATUS)
        async def on_status(m: Message, state: FSMContext):
            await state.clear()
            await render_status(m)

        # ============================================================
        #                     ENGINE PANEL
        # ============================================================
        async def render_engine_panel(m_or_cb):
            s = await ctrl.status()
            engine_state = "🟢 ON" if s["engine"] else "🔴 OFF"
            txt = (
                "<b>⚙️ ОРДЕР-ДВИЖОК</b>\n"
                f"Состояние: {engine_state}\n"
                f"Текущая скидка: <b>{s['engine_discount']}%</b>\n\n"
                "Выбери действие ниже."
            )
            kb = engine_inline_kb()
            if isinstance(m_or_cb, CallbackQuery):
                try:
                    await m_or_cb.message.edit_text(txt, reply_markup=kb)
                except Exception:
                    await m_or_cb.message.answer(txt, reply_markup=kb)
            else:
                await m_or_cb.answer(txt, reply_markup=kb)

        @r.message(F.text == BTN_ENGINE)
        async def on_engine_btn(m: Message, state: FSMContext):
            await state.clear()
            await render_engine_panel(m)

        @r.callback_query(F.data == "engine:start")
        async def cb_engine_start(cb: CallbackQuery):
            res = await ctrl.engine_start()
            await cb.answer("Старт")
            await cb.message.answer(res, reply_markup=main_kb())
            await render_engine_panel(cb)

        @r.callback_query(F.data == "engine:stop")
        async def cb_engine_stop(cb: CallbackQuery):
            res = await ctrl.engine_stop()
            await cb.answer("Стоп")
            await cb.message.answer(res, reply_markup=main_kb())
            await render_engine_panel(cb)

        @r.callback_query(F.data == "engine:refresh")
        async def cb_engine_refresh(cb: CallbackQuery):
            await cb.answer("Обновлено")
            await render_engine_panel(cb)

        @r.callback_query(F.data == "engine:setdisc")
        async def cb_engine_setdisc(cb: CallbackQuery, state: FSMContext):
            await cb.answer()
            await self._open_discount_editor(cb, state, target="engine")

        # ============================================================
        #                     SNIPER PANEL
        # ============================================================
        async def render_sniper_panel(m_or_cb):
            s = await ctrl.status()
            sniper_state = "🟢 ON" if s["sniper"] else "🔴 OFF"
            txt = (
                "<b>🎯 СНАЙПЕР</b>\n"
                f"Состояние: {sniper_state}\n"
                f"Целевой дисконт: <b>{s['sniper_discount']}%</b>\n\n"
                "Выбери действие ниже."
            )
            kb = sniper_inline_kb()
            if isinstance(m_or_cb, CallbackQuery):
                try:
                    await m_or_cb.message.edit_text(txt, reply_markup=kb)
                except Exception:
                    await m_or_cb.message.answer(txt, reply_markup=kb)
            else:
                await m_or_cb.answer(txt, reply_markup=kb)

        @r.message(F.text == BTN_SNIPER)
        async def on_sniper_btn(m: Message, state: FSMContext):
            await state.clear()
            await render_sniper_panel(m)

        @r.callback_query(F.data == "sniper:start")
        async def cb_sniper_start(cb: CallbackQuery):
            res = await ctrl.sniper_start()
            await cb.answer("Старт")
            await cb.message.answer(res, reply_markup=main_kb())
            await render_sniper_panel(cb)

        @r.callback_query(F.data == "sniper:stop")
        async def cb_sniper_stop(cb: CallbackQuery):
            res = await ctrl.sniper_stop()
            await cb.answer("Стоп")
            await cb.message.answer(res, reply_markup=main_kb())
            await render_sniper_panel(cb)

        @r.callback_query(F.data == "sniper:refresh")
        async def cb_sniper_refresh(cb: CallbackQuery):
            await cb.answer("Обновлено")
            await render_sniper_panel(cb)

        @r.callback_query(F.data == "sniper:setdisc")
        async def cb_sniper_setdisc(cb: CallbackQuery, state: FSMContext):
            await cb.answer()
            await self._open_discount_editor(cb, state, target="sniper")

        # ============================================================
        #                     OPS PANEL
        # ============================================================
        async def render_ops_panel(m_or_cb):
            s = await ctrl.status()
            txt = (
                "<b>🛠 ОПЕРАЦИИ</b>\n"
                f"Дисконт офферов: <b>{s['offers_discount']}%</b>\n\n"
                "Выбери операцию."
            )
            kb = ops_inline_kb()
            if isinstance(m_or_cb, CallbackQuery):
                try:
                    await m_or_cb.message.edit_text(txt, reply_markup=kb)
                except Exception:
                    await m_or_cb.message.answer(txt, reply_markup=kb)
            else:
                await m_or_cb.answer(txt, reply_markup=kb)

        @r.message(F.text == BTN_OPS)
        async def on_ops_btn(m: Message, state: FSMContext):
            await state.clear()
            await render_ops_panel(m)

        @r.callback_query(F.data == "ops:scan")
        async def cb_ops_scan(cb: CallbackQuery):
            await cb.answer("Сканер запущен")
            await cb.message.answer("🔎 Запускаю сканер ликвидности...")
            lines: list[str] = []

            def collect(s: str):
                lines.append(s)

            result = await ctrl.run_scanner(log_func=collect)
            tail = "\n".join(lines[-20:]) if lines else "(пусто)"
            await cb.message.answer(
                f"{result}\n\n<pre>{tail}</pre>", reply_markup=main_kb()
            )

        @r.callback_query(F.data == "ops:mass_offers")
        async def cb_ops_offers(cb: CallbackQuery):
            await cb.answer("Офферы")
            await cb.message.answer("📤 Ставлю массовые офферы...")
            res = await ctrl.run_mass_offers()
            await cb.message.answer(res, reply_markup=main_kb())

        @r.callback_query(F.data == "ops:flip")
        async def cb_ops_flip(cb: CallbackQuery):
            await cb.answer("Флип")
            await cb.message.answer("♻️ Запускаю авто-флип...")
            res = await ctrl.run_flip()
            await cb.message.answer(res, reply_markup=main_kb())

        @r.callback_query(F.data == "ops:clear")
        async def cb_ops_clear(cb: CallbackQuery):
            await cb.answer("Чищу")
            await cb.message.answer("🧹 Чищу офферы...")
            res = await ctrl.clear_offers()
            await cb.message.answer(res, reply_markup=main_kb())

        @r.callback_query(F.data == "ops:setdisc_offers")
        async def cb_ops_setdisc(cb: CallbackQuery, state: FSMContext):
            await cb.answer()
            await self._open_discount_editor(cb, state, target="offers")

        # ============================================================
        #                     CALCULATOR
        # ============================================================
        @r.message(F.text == BTN_CALC)
        async def on_calc_btn(m: Message, state: FSMContext):
            await state.clear()
            calc_disc = _disc_field("calc_discount", 15.0)
            txt = (
                "<b>🧮 КАЛЬКУЛЯТОР ПРОФИТА</b>\n"
                f"Текущий дисконт расчёта: <b>{calc_disc}%</b>\n\n"
                "Нажми «Рассчитать профит» — бот спросит флор-цену."
            )
            await m.answer(txt, reply_markup=calc_inline_kb())

        @r.callback_query(F.data == "calc:run")
        async def cb_calc_run(cb: CallbackQuery, state: FSMContext):
            await cb.answer()
            await state.set_state(CalcFSM.waiting_floor)
            await cb.message.answer(
                "Введи <b>флор-цену</b> в TON (например: <code>3.25</code>):",
                reply_markup=main_kb(),
            )

        @r.callback_query(F.data == "calc:setdisc")
        async def cb_calc_setdisc(cb: CallbackQuery, state: FSMContext):
            await cb.answer()
            await self._open_discount_editor(cb, state, target="calc")

        @r.message(CalcFSM.waiting_floor, F.text)
        async def calc_floor_input(m: Message, state: FSMContext):
            # Если пользователь ткнул в нижнюю клавиатуру — отменяем FSM
            if m.text in {BTN_STATUS, BTN_ENGINE, BTN_SNIPER, BTN_OPS, BTN_CALC}:
                await state.clear()
                # пробрасываем дальше: вызовем соответствующий хендлер вручную
                return await self._reroute_main_buttons(m, state)

            raw = (m.text or "").replace(",", ".").strip()
            try:
                floor = float(raw)
                if floor <= 0:
                    raise ValueError
            except ValueError:
                await m.answer(
                    "❌ Введи положительное число, например <code>3.25</code>."
                )
                return

            disc = _disc_field("calc_discount", 15.0)
            buy_price = floor * (1 - disc / 100.0)
            # Прикинем чистую прибыль с учётом комиссии маркета 5% (стандарт MRKT)
            fee = 0.05
            sell_net = floor * (1 - fee)
            profit = sell_net - buy_price
            roi = (profit / buy_price * 100.0) if buy_price > 0 else 0.0

            txt = (
                "<b>🧮 РАСЧЁТ ПРОФИТА</b>\n"
                f"Флор: <b>{floor:.3f} TON</b>\n"
                f"Дисконт покупки: <b>{disc:.1f}%</b>\n"
                f"Цена покупки: <b>{buy_price:.3f} TON</b>\n"
                f"Продажа по флору (за вычетом 5% комиссии): <b>{sell_net:.3f} TON</b>\n"
                f"Профит: <b>{profit:.3f} TON</b>\n"
                f"ROI: <b>{roi:.2f}%</b>"
            )
            await state.clear()
            await m.answer(txt, reply_markup=main_kb())

        # ============================================================
        #                     DISCOUNT FSM (универсальный редактор)
        # ============================================================
        @r.callback_query(F.data.startswith("disc:"))
        async def cb_disc_step(cb: CallbackQuery, state: FSMContext):
            try:
                _, target, action = cb.data.split(":")
            except ValueError:
                await cb.answer("Битый callback", show_alert=True)
                return

            if action == "done":
                await state.clear()
                await cb.answer("Сохранено")
                try:
                    await cb.message.edit_text(
                        f"✅ Дисконт <b>{target}</b> зафиксирован на "
                        f"<b>{_disc_field(self._target_to_cfg(target))}%</b>"
                    )
                except Exception:
                    pass
                return

            try:
                delta = int(action)
            except ValueError:
                await cb.answer("Неизвестный шаг", show_alert=True)
                return

            cur = _disc_field(self._target_to_cfg(target))
            new_val = max(1.0, min(95.0, cur + delta))
            res = ctrl.set_discount(target, new_val)
            await cb.answer(res)
            await self._render_discount_editor(cb.message, target, edit=True)

        @r.message(DiscountFSM.waiting_value, F.text)
        async def disc_manual_input(m: Message, state: FSMContext):
            # Перехват главных кнопок — выходим из FSM и роутим
            if m.text in {BTN_STATUS, BTN_ENGINE, BTN_SNIPER, BTN_OPS, BTN_CALC}:
                await state.clear()
                return await self._reroute_main_buttons(m, state)

            data = await state.get_data()
            target = data.get("disc_target")
            if not target:
                await state.clear()
                await m.answer("Сессия настройки потеряна.", reply_markup=main_kb())
                return

            raw = (m.text or "").replace(",", ".").strip().rstrip("%")
            try:
                value = float(raw)
            except ValueError:
                await m.answer(
                    "❌ Введи число, например <code>15</code> или <code>22.5</code>."
                )
                return

            res = ctrl.set_discount(target, value)
            await m.answer(res, reply_markup=main_kb())
            await self._render_discount_editor(m, target, edit=False)
            # Остаёмся в FSM, чтобы можно было крутить инлайн-кнопки и ввести ещё число.
            # Но для UX-чистоты лучше выйти — пользователь нажмёт «Готово» при необходимости.
            await state.clear()

        # ---------- FALLBACK ----------
        @r.message(F.text)
        async def fallback(m: Message, state: FSMContext):
            # Если активен FSM — он перехватит выше. Сюда падают любые случайные тексты.
            await m.answer("Используй кнопки снизу 👇", reply_markup=main_kb())

    # ============================================================
    #                 INTERNAL UTILITIES
    # ============================================================
    @staticmethod
    def _target_to_cfg(target: str) -> str:
        return {
            "engine": "engine_discount",
            "sniper": "sniper_discount",
            "offers": "offers_discount",
            "calc": "calc_discount",
        }.get(target, "engine_discount")

    @staticmethod
    def _target_to_title(target: str) -> str:
        return {
            "engine": "⚙️ Скидка Движка",
            "sniper": "🎯 Цель Снайпера",
            "offers": "📤 Дисконт офферов",
            "calc": "🧮 Дисконт калькулятора",
        }.get(target, target)

    async def _render_discount_editor(self, msg: Message, target: str, edit: bool):
        cur = _disc_field(self._target_to_cfg(target))
        title = self._target_to_title(target)
        txt = (
            f"<b>{title}</b>\n"
            f"Текущее значение: <b>{cur:.1f}%</b>\n\n"
            "Жми ±1 / ±5 или введи число с клавиатуры (1..95)."
        )
        kb = discount_inline_kb(target)
        if edit:
            try:
                await msg.edit_text(txt, reply_markup=kb)
                return
            except Exception:
                pass
        await msg.answer(txt, reply_markup=kb)

    async def _open_discount_editor(
        self, cb: CallbackQuery, state: FSMContext, target: str
    ):
        await state.set_state(DiscountFSM.waiting_value)
        await state.update_data(disc_target=target)
        await self._render_discount_editor(cb.message, target, edit=False)

    async def _reroute_main_buttons(self, m: Message, state: FSMContext):
        """
        Если пользователь во время FSM нажал на главную нижнюю кнопку —
        корректно переходим в нужный раздел.
        """
        text = m.text
        # Имитируем поведение хендлеров
        if text == BTN_STATUS:
            s = await self.controller.status()
            engine_state = "🟢 ON" if s["engine"] else "🔴 OFF"
            sniper_state = "🟢 ON" if s["sniper"] else "🔴 OFF"
            await m.answer(
                "<b>📊 STATUS</b>\n"
                f"Баланс: <b>{_fmt(s['balance'], ' TON')}</b>\n"
                f"Портфель: <b>{_fmt(s['portfolio_ton'], ' TON')}</b>\n"
                f"Активов: <b>{s['items'] if s['items'] is not None else '—'}</b>\n\n"
                f"Движок: {engine_state} (скидка {s['engine_discount']}%)\n"
                f"Снайпер: {sniper_state} (цель {s['sniper_discount']}%)\n"
                f"Офферы: дисконт {s['offers_discount']}%",
                reply_markup=main_kb(),
            )
        elif text == BTN_ENGINE:
            s = await self.controller.status()
            engine_state = "🟢 ON" if s["engine"] else "🔴 OFF"
            await m.answer(
                "<b>⚙️ ОРДЕР-ДВИЖОК</b>\n"
                f"Состояние: {engine_state}\n"
                f"Текущая скидка: <b>{s['engine_discount']}%</b>",
                reply_markup=engine_inline_kb(),
            )
        elif text == BTN_SNIPER:
            s = await self.controller.status()
            sniper_state = "🟢 ON" if s["sniper"] else "🔴 OFF"
            await m.answer(
                "<b>🎯 СНАЙПЕР</b>\n"
                f"Состояние: {sniper_state}\n"
                f"Целевой дисконт: <b>{s['sniper_discount']}%</b>",
                reply_markup=sniper_inline_kb(),
            )
        elif text == BTN_OPS:
            s = await self.controller.status()
            await m.answer(
                "<b>🛠 ОПЕРАЦИИ</b>\n"
                f"Дисконт офферов: <b>{s['offers_discount']}%</b>",
                reply_markup=ops_inline_kb(),
            )
        elif text == BTN_CALC:
            calc_disc = _disc_field("calc_discount", 15.0)
            await m.answer(
                "<b>🧮 КАЛЬКУЛЯТОР ПРОФИТА</b>\n"
                f"Текущий дисконт расчёта: <b>{calc_disc}%</b>",
                reply_markup=calc_inline_kb(),
            )

    # ============================================================
    #                     LIFECYCLE
    # ============================================================
    async def _set_commands(self) -> None:
        # Чистим список команд — у нас Трон на кнопках, не на /-командах.
        try:
            await self.bot.set_my_commands([])
        except Exception as e:
            log.warning("set_my_commands failed: %s", e)

    async def start(self) -> None:
        await self._set_commands()
        log.info("Telegram bot polling started. Admin=%s", self.admin_id)
        try:
            await self.dp.start_polling(self.bot, handle_signals=False)
        finally:
            await self.bot.session.close()
            log.info("Telegram bot polling stopped.")

    def schedule(self, loop: asyncio.AbstractEventLoop):
        """Запускает polling как фоновую задачу в указанном loop."""
        coro = self.start()
        fut = asyncio.run_coroutine_threadsafe(self._wrap(coro), loop)
        self._polling_future = fut
        return fut

    async def _wrap(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            log.info("TG bot cancelled")
        except Exception:
            log.exception("TG bot crashed")

    async def stop(self) -> None:
        log.info("Stopping TG bot...")
        await self.dp.stop_polling()
