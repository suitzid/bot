"""Orenix Bot — aiogram 3 + Supabase (REST). Секреты берутся ТОЛЬКО из переменных окружения."""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import time
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qsl
from datetime import datetime, timedelta, timezone

import httpx
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup,
                           LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orenix")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не засорять логи запросами к базе

# ───────────────────────── настройки (env) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://plysapfztvihxkioxtch.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # secret / service_role ключ
MAIN_ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "5570425300").replace(" ", "").split(",") if x}
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004491870504"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+2QGm_H2UchgwNDJi")
WEB_ADMIN_EMAIL = os.getenv("WEB_ADMIN_EMAIL", "")
WEB_ADMIN_PASSWORD = os.getenv("WEB_ADMIN_PASSWORD", "")
PORT = int(os.getenv("PORT", "10000"))
_ext = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
APP_URL = os.getenv("MINIAPP_URL") or (_ext + "/app" if _ext else "")  # мини-апп отдаётся самим ботом

esc = html.escape


# ───────────────────────── база (PostgREST) ─────────────────────────
class DB:
    def __init__(self):
        h = {"apikey": SUPABASE_KEY}
        if SUPABASE_KEY.startswith("eyJ"):
            h["Authorization"] = f"Bearer {SUPABASE_KEY}"
        self.c = httpx.AsyncClient(base_url=SUPABASE_URL, headers=h, timeout=20)

    async def _req(self, method, path, **kw):
        r = await self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    async def select(self, table, params=None):
        return await self._req("GET", f"/rest/v1/{table}", params=params or {})

    async def insert(self, table, row, upsert=False, conflict=None):
        prefer = "return=representation" + (",resolution=merge-duplicates" if upsert else "")
        params = {"on_conflict": conflict} if conflict else {}
        return await self._req("POST", f"/rest/v1/{table}", json=row, params=params, headers={"Prefer": prefer})

    async def update(self, table, match, values):
        params = {k: f"eq.{v}" for k, v in match.items()}
        return await self._req("PATCH", f"/rest/v1/{table}", json=values, params=params,
                               headers={"Prefer": "return=representation"})

    async def delete(self, table, match):
        return await self._req("DELETE", f"/rest/v1/{table}", params={k: f"eq.{v}" for k, v in match.items()})

    async def rpc(self, fn, args=None):
        return await self._req("POST", f"/rest/v1/rpc/{fn}", json=args or {})


db = DB()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ───────────────────────── конфиг (хранится в ox_settings) ─────────────────────────
DEFAULT_CFG = {
    "maintenance": {"on": False, "text": "🛠 В боте идут технические работы. Скоро вернёмся!"},
    "hidden": {},
    "messenger_url": "",
    "old": {},  # прежние цены для показа скидки
    "links": {},  # tg_id -> id профиля на сайте (ручная привязка существующих аккаунтов)
    "prices": {
        "num_rand": None, "num_custom": None, "username": None,
        "premium": {"7": 45, "30": 100, "90": 180, "180": 250, "360": 400},
        "coins": [{"coins": c, "stars": c // 100} for c in (100, 300, 500, 1000, 2500, 5000)],
    },
}
CFG: dict = {}
DB_ADMINS: set = set()
BANNED: set = set()
KNOWN: set = set()

SECTIONS = {
    "get_number": "📱 Получить номер", "profile": "👤 Профиль", "shop": "🛒 Магазин", "support": "💬 Поддержка",
    "shop_number": "📞 Номер", "shop_username": "🏷 NFT-юз", "shop_coins": "🪙 Cat Coin", "shop_premium": "💎 Premium",
}


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


CFG_KEY = "orenix_config"  # свой ключ, чтобы не пересекаться со старыми записями в ox_settings


async def load_cfg():
    rows = await db.select("ox_settings", {"key": f"eq.{CFG_KEY}"})
    saved = rows[0]["value"] if rows else {}
    if isinstance(saved, str):  # если колонка value текстовая
        try:
            saved = json.loads(saved)
        except Exception:
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    CFG.clear()
    CFG.update(deep_merge(DEFAULT_CFG, saved))
    # списки не мёрджим — берём сохранённые целиком
    if isinstance(saved.get("prices", {}).get("coins"), list):
        CFG["prices"]["coins"] = saved["prices"]["coins"]


async def save_cfg():
    await db.insert("ox_settings", {"key": CFG_KEY, "value": CFG}, upsert=True, conflict="key")


def set_price(key: str, new):
    """Меняет цену и ведёт «старую цену»: если новая ниже — в мини-аппе покажется скидка."""
    P, old = CFG["prices"], CFG.setdefault("old", {})
    if key.startswith("prem:"):
        cur, okey = P["premium"].get(key[5:]), key
    elif key.startswith("coin:"):
        pack = P["coins"][int(key[5:])]
        cur, okey = pack["stars"], f"coin:{pack['coins']}"
    else:
        cur, okey = P.get(key), key
    base = old.get(okey) or cur
    if new is not None and base is not None and new < base:
        old[okey] = base
    else:
        old.pop(okey, None)
    if key.startswith("prem:"):
        P["premium"][key[5:]] = new
    elif key.startswith("coin:"):
        pack["stars"] = new
    else:
        P[key] = new


def add_pack(coins: int, stars):
    if not 1 <= coins <= 100_000_000:
        raise ValueError("Количество кк: от 1 до 100 000 000")
    if any(p["coins"] == coins for p in CFG["prices"]["coins"]):
        raise ValueError("Такой пак уже есть")
    if stars is not None and not 1 <= stars <= 100000:
        raise ValueError("Цена: от 1 до 100000 или прочерк")
    CFG["prices"]["coins"].append({"coins": coins, "stars": stars})
    CFG["prices"]["coins"].sort(key=lambda p: p["coins"])


def is_admin(uid: int) -> bool:
    return uid in MAIN_ADMINS or uid in DB_ADMINS


def hidden(key: str) -> bool:
    return bool(CFG["hidden"].get(key))


def ps(v) -> str:
    return "—" if v is None else f"{v} ⭐"


# ───────────────────────── утилиты ─────────────────────────
def B(text, cb=None, url=None):
    return Btn(text=text, callback_data=cb, url=url)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[b for b in r if b] for r in rows if any(r)])


def vb(key, uid, text, cb):
    """Кнопка с учётом скрытия: у обычных пользователей пропадает, у админов получает 🔒."""
    if hidden(key):
        return B("🔒 " + text, cb) if is_admin(uid) else None
    return B(text, cb)


def tg_name(u) -> str:
    return f"@{u.username}" if u.username else (u.first_name or str(u.id))


def grouped(n: str) -> str:  # как formatNumberGrouped в мессенджере
    if len(n) <= 3:
        return n
    parts, rest = [n[:3]], n[3:]
    while len(rest) > 2:
        parts.append(rest[:3])
        rest = rest[3:]
    if rest:
        parts.append(rest)
    return " ".join(parts)


def label_of(code: str, digits: str) -> str:
    return f"+{code} {grouped(digits)}"


def gen_digits(n, max_same, no_adjacent=False, first="0123456789"):
    for _ in range(1000):
        out, cnt = [], Counter()
        for i in range(n):
            ch = [d for d in "0123456789" if cnt[d] < max_same and (not no_adjacent or not out or out[-1] != d)
                  and (i > 0 or d in first)]
            if not ch:
                break
            d = random.choice(ch)
            out.append(d)
            cnt[d] += 1
        if len(out) == n:
            return "".join(out)
    raise RuntimeError("cannot generate")


async def item_free(kind, value) -> bool:
    return bool(await db.rpc("ox_item_free", {"p_kind": kind, "p_value": value}))


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


async def show(ev, text, markup=None):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    if isinstance(ev, CallbackQuery):
        try:
            return await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
    return await msg.answer(text, reply_markup=markup)


async def touch_user(u, force=False):
    if u.id in KNOWN and not force:
        return
    KNOWN.add(u.id)
    try:
        await db.insert("ox_users", {"tg_id": u.id, "username": u.username, "first_name": u.first_name,
                                      "last_seen": datetime.now(timezone.utc).isoformat()},
                        upsert=True, conflict="tg_id")
    except Exception as e:
        log.warning("touch_user: %s", e)


_sub_cache: dict = {}


async def is_subscribed(uid: int) -> bool:
    if time.time() - _sub_cache.get(uid, 0) < 60:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        ok = m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and getattr(m, "is_member", False))
    except Exception as e:
        log.warning("get_chat_member: %s (бот должен быть админом канала)", e)
        ok = False
    if ok:
        _sub_cache[uid] = time.time()
    return ok


def sub_kb():
    return kb([B("📢 Подписаться на канал", url=CHANNEL_LINK)], [B("✅ Я подписался", "chk_sub")])


SUB_TEXT = "Для использования бота подпишись на наш канал, затем нажми «Я подписался»."


PENDING = "orenix_pending"   # та же таблица, что читает сайт (id, user_id, phone, code, entered_at, notified)
PROFILE_FIELDS = "id,phone,username,display_name,avatar_url,balance,is_premium,premium_until,is_deleted"
CODE_AT: dict = {}   # tg_id -> время выдачи кода
REG_MSG: dict = {}   # tg_id -> (chat_id, message_id) экрана с номером


async def get_pending(uid: int):
    rows = await db.select(PENDING, {"user_id": f"eq.{uid}", "order": "id.desc", "limit": "1"})
    return rows[0] if rows else None


def pend_parts(row):
    phone = row["phone"]  # '+7' + 10 цифр
    return phone[:2], phone[2:]


def pend_label(row) -> str:  # как сайт пишет номер в profiles.phone: '+7 912 345 678 9'
    region, body = pend_parts(row)
    return f"{region} {grouped(body)}"


async def get_profile(tg_id: int):
    """Есть заявка на номер от этого tg-аккаунта И на сайте уже создан профиль с этим номером."""
    try:
        pid = CFG.get("links", {}).get(str(tg_id))
        if pid:  # аккаунт привязан вручную через админ-панель
            r = await db.select("profiles", {"id": f"eq.{pid}", "limit": "1", "select": PROFILE_FIELDS})
            if r:
                return r[0]
        row = await get_pending(tg_id)
        if not row:
            return None
        r = await db.select("profiles", {"phone": f"eq.{pend_label(row)}", "limit": "1", "select": PROFILE_FIELDS})
        return r[0] if r else None
    except Exception as e:
        log.error("get_profile: %s", e)
        return None


async def number_taken(region: str, body: str) -> bool:
    if await db.select(PENDING, {"phone": f"eq.{region}{body}", "select": "id", "limit": "1"}):
        return True
    label = f"{region} {grouped(body)}"
    if await db.select("profiles", {"phone": f"eq.{label}", "select": "id", "limit": "1"}):
        return True
    return not await item_free("phone", label)


# ───────────────────────── middleware: бан / тех.работы / подписка ─────────────────────────
class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if not u or u.is_bot:
            return await handler(event, data)
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)  # оплаченное выдаём всегда
        await touch_user(u)
        if is_admin(u.id):
            return await handler(event, data)

        async def deny(text, markup=None):
            if isinstance(event, CallbackQuery):
                await event.answer()
                await event.message.answer(text, reply_markup=markup)
            else:
                await event.answer(text, reply_markup=markup)

        if u.id in BANNED:
            return await deny("🚫 Вы заблокированы.")
        if CFG["maintenance"]["on"]:
            return await deny(esc(CFG["maintenance"]["text"]))
        is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
        is_chk = isinstance(event, CallbackQuery) and event.data == "chk_sub"
        if not (is_start or is_chk) and not await is_subscribed(u.id):
            return await deny(SUB_TEXT, sub_kb())
        return await handler(event, data)


# ───────────────────────── главное меню ─────────────────────────
def welcome_text(u):
    return (f"Здравствуй, {esc(tg_name(u))}!\n"
            "Добро пожаловать в Orenix!\n\n"
            "📱 Получай номер для регистрации в Orenix Messenger\n"
            "👤 Смотри профиль: номера, юзернеймы, баланс и Premium\n"
            "🛒 Покупай красивые номера, NFT-юзернеймы, Cat Coin и Premium за Telegram Stars ⭐\n"
            "💬 Пиши в поддержку прямо здесь\n\n"
            "Выбери действие:")


def main_kb(uid):
    rows = [[vb("get_number", uid, "📱 Получить номер", "gn"), vb("profile", uid, "👤 Профиль", "pf")],
            [vb("shop", uid, "🛒 Магазин", "shop")],
            [vb("support", uid, "💬 Поддержка", "support")]]
    if APP_URL:
        rows.insert(0, [Btn(text="🚀 Открыть Mini App", web_app=WebAppInfo(url=APP_URL))])
    if is_admin(uid):
        rows.append([B("⚙️ Админ-панель", "ad:main")])
    return kb(*rows)


async def guard(c: CallbackQuery, *keys) -> bool:
    if not is_admin(c.from_user.id) and any(hidden(k) for k in keys):
        await c.answer("Раздел временно недоступен", show_alert=True)
        return False
    return True


sr = Router()  # /start — первым, чтобы сбрасывал любые состояния
r = Router()


class S(StatesGroup):
    rand_region = State(); cust_region = State(); cust_digits = State(); username = State(); support = State()
    adm_reply = State(); adm_price = State(); adm_ban = State(); adm_unban = State()
    adm_addadm = State(); adm_rmadm = State(); adm_mt_text = State(); adm_bc = State(); adm_link = State()
    adm_lnk_add = State(); adm_lnk_del = State(); adm_coinadd = State()


CANCEL = kb([B("✖️ Отмена", "m:main")])


@sr.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await touch_user(m.from_user, force=True)
    if not is_admin(m.from_user.id) and not await is_subscribed(m.from_user.id):
        return await m.answer(SUB_TEXT, reply_markup=sub_kb())
    await m.answer(welcome_text(m.from_user), reply_markup=main_kb(m.from_user.id))


@r.callback_query(F.data == "chk_sub")
async def chk_sub(c: CallbackQuery):
    if is_admin(c.from_user.id) or await is_subscribed(c.from_user.id):
        await c.answer()
        return await show(c, welcome_text(c.from_user), main_kb(c.from_user.id))
    await c.answer("Подписка не найдена. Подпишись и нажми ещё раз.", show_alert=True)


@r.callback_query(F.data == "m:main")
async def to_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show(c, welcome_text(c.from_user), main_kb(c.from_user.id))


# ───────────────────────── номер / профиль ─────────────────────────
@r.callback_query(F.data.in_({"gn", "pf"}))
async def account_entry(c: CallbackQuery):
    if not await guard(c, "get_number" if c.data == "gn" else "profile"):
        return
    await c.answer()
    uid = c.from_user.id
    prof = await get_profile(uid)
    if prof:
        return await show_profile(c, prof)
    row = await get_pending(uid)
    if row:  # номер уже выдан, регистрация на сайте ещё не завершена
        REG_MSG[uid] = (c.message.chat.id, c.message.message_id)
        return await show(c, reg_text(row), reg_kb(row))
    await show(c, "🌍 <b>Выберите регион номера:</b>",
               kb([B("🇷🇺 +7", "reg:7"), B("🇺🇸 +1", "reg:1")], [B("← Меню", "m:main")]))


def premium_text(p) -> str:
    if not p.get("is_premium"):
        return "❌ не активен"
    until = parse_dt(p.get("premium_until"))
    if not until:
        return "✅ активен (бессрочно)"
    left = until - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "❌ не активен (срок истёк)"
    return f"✅ активен · осталось {left.days} д {left.seconds // 3600} ч {left.seconds % 3600 // 60} мин"


async def show_profile(ev, prof):
    phones, names = [prof.get("phone")], [prof.get("username")]
    try:
        for x in await db.select("ox_owned", {"user_id": f"eq.{prof['id']}", "select": "kind,value", "order": "id"}):
            (phones if x["kind"] == "phone" else names).append(x["value"])
    except Exception as e:
        log.warning("ox_owned: %s", e)
    for fn, args, field, dst in (("extra_phones_for", {"p_ids": [prof["id"]]}, "phone", phones),
                                 ("extra_usernames_for", {"p_type": "user", "p_ids": [prof["id"]]}, "username", names)):
        try:
            dst += [x[field] for x in (await db.rpc(fn, args) or [])]
        except Exception:
            pass

    def uniq(a):
        seen, out = set(), []
        for x in a:
            k = re.sub(r"[\s@+]", "", str(x or "")).lower()
            if k and k not in seen:
                seen.add(k); out.append(str(x))
        return out

    ph = "\n".join(f"• <code>{esc(x)}</code>" for x in uniq(phones)) or "—"
    nm = "\n".join(f"• <code>{esc(x)}</code>" for x in uniq(names)) or "—"
    text = (f"👤 <b>Профиль Orenix</b>\n\nИмя: {esc(prof.get('display_name') or '—')}\n\n"
            f"📞 Номера:\n{ph}\n\n🏷 Юзернеймы:\n{nm}\n\n"
            f"🪙 Баланс: <b>{int(float(prof.get('balance') or 0))}</b> кк\n💎 Premium: {premium_text(prof)}")
    await show(ev, text, kb([B("🛒 Магазин", "shop")], [B("← Меню", "m:main")]))


def reg_text(row):
    region, body = pend_parts(row)
    return ("📱 <b>Ваш номер</b>\n\n"
            f"Регион: <code>{region}</code>\nНомер: <code>{body}</code>\n\n"
            "1️⃣ Откройте Orenix Messenger → «Регистрация»\n"
            "2️⃣ Введите регион и номер, нажмите «Далее»\n"
            "3️⃣ Вернитесь сюда — кнопка «Получить код» станет активной.")


def reg_kb(row):
    url = CFG.get("messenger_url")
    return kb([B("🔑 Получить код", "code:get") if row.get("entered_at") else B("🔒 Получить код", "code:wait")],
              [B("🌐 Открыть мессенджер", url=url)] if url else [],
              [B("← Меню", "m:main")])


@r.callback_query(F.data.startswith("reg:"))
async def reg_number(c: CallbackQuery):
    if not await guard(c, "get_number"):
        return
    uid = c.from_user.id
    if await get_profile(uid):
        await c.answer()
        return await show(c, "У вас уже есть аккаунт 👍", kb([B("👤 Профиль", "pf")]))
    row = await get_pending(uid)  # один номер на аккаунт — как в рабочей версии
    if not row:
        code = c.data.split(":")[1]
        if code not in ("7", "1"):
            return await c.answer()
        region = "+" + code
        for _ in range(40):
            body = gen_digits(10, 3, no_adjacent=True, first="23456789")
            if await number_taken(region, body):
                continue
            try:
                row = (await db.insert(PENDING, {"user_id": uid, "phone": region + body, "code": None,
                                                 "entered_at": None, "notified": False}))[0]
                break
            except RuntimeError as e:
                if any(x in str(e).lower() for x in ("409", "duplicate", "unique")):
                    row = await get_pending(uid)
                    if row:
                        break
                    continue
                raise
        if not row:
            return await c.answer("Не удалось подобрать номер, попробуйте ещё раз", show_alert=True)
    REG_MSG[uid] = (c.message.chat.id, c.message.message_id)
    await c.answer()
    await show(c, reg_text(row), reg_kb(row))


@r.callback_query(F.data == "code:wait")
async def code_wait(c: CallbackQuery):
    row = await get_pending(c.from_user.id)
    if not row:
        return await c.answer("Сначала получите номер", show_alert=True)
    if row.get("entered_at"):
        await c.answer()
        return await show(c, reg_text(row), reg_kb(row))
    await c.answer("Сначала введите номер на сайте мессенджера и нажмите «Далее».", show_alert=True)


@r.callback_query(F.data == "code:back")
async def code_back(c: CallbackQuery):
    row = await get_pending(c.from_user.id)
    await c.answer()
    if row:
        await show(c, reg_text(row), reg_kb(row))


@r.callback_query(F.data == "code:get")
async def code_get(c: CallbackQuery):
    uid = c.from_user.id
    row = await get_pending(uid)
    if not row:
        return await c.answer("Сначала получите номер", show_alert=True)
    if not row.get("entered_at"):
        return await c.answer("Сначала введите номер на сайте и нажмите «Далее».", show_alert=True)
    code, issued = row.get("code"), CODE_AT.get(uid)
    if not code or (issued and time.time() - issued > 300):  # код живёт 5 минут
        code = f"{random.randint(0, 9999):04d}"
        await db.update(PENDING, {"id": row["id"]}, {"code": code})
        CODE_AT[uid] = issued = time.time()
    elif not issued:
        CODE_AT[uid] = issued = time.time()
    left = max(0, 300 - int(time.time() - issued))
    await c.answer()
    await show(c, f"🔑 <b>Код подтверждения</b>\n\n<code>{code}</code>\n\n"
                  f"Номер: <code>{esc(pend_label(row))}</code>\n"
                  f"⏱ Действует ещё {left // 60} мин {left % 60:02d} с. Он появится на сайте автоматически — "
                  "если нет, введите его вручную.",
               kb([B("← К номеру", "code:back"), B("← Меню", "m:main")]))


async def watcher():
    """Как poll_pending в рабочем боте: сайт поставил entered_at -> включаем кнопку «Получить код»."""
    while True:
        try:
            rows = await db.select(PENDING, {"entered_at": "not.is.null", "code": "is.null", "notified": "eq.false",
                                             "order": "id.asc", "limit": "50"})
            for row in rows:
                await db.update(PENDING, {"id": row["id"]}, {"notified": True})
                uid = int(row["user_id"])
                m = REG_MSG.get(uid)
                try:
                    if not m:
                        raise LookupError
                    await bot.edit_message_text(reg_text(row), chat_id=m[0], message_id=m[1], reply_markup=reg_kb(row))
                except Exception:
                    try:
                        await bot.send_message(uid, "✅ Номер принят! Нажмите «Получить код».", reply_markup=reg_kb(row))
                    except Exception as e:
                        log.warning("notify %s: %s", uid, e)
        except Exception as e:
            log.warning("watcher: %s", e)
        await asyncio.sleep(3)


# ───────────────────────── магазин ─────────────────────────
def shop_kb(uid):
    return kb([vb("shop_number", uid, "📞 Номер", "sh:number"), vb("shop_username", uid, "🏷 NFT-юз", "sh:username")],
              [vb("shop_coins", uid, "🪙 Cat Coin", "sh:coins"), vb("shop_premium", uid, "💎 Orenix Premium", "sh:premium")],
              [B("← Меню", "m:main")])


@r.callback_query(F.data == "shop")
async def shop(c: CallbackQuery, state: FSMContext):
    if not await guard(c, "shop"):
        return
    await state.clear()
    await c.answer()
    await show(c, "🛒 <b>Выберите услугу:</b>", shop_kb(c.from_user.id))


async def need_account(c: CallbackQuery):
    prof = await get_profile(c.from_user.id)
    if not prof:
        await c.answer("Сначала получите номер и зарегистрируйтесь в мессенджере", show_alert=True)
    return prof


async def pay(chat_id, title, desc, payload, stars):
    await bot.send_invoice(chat_id=chat_id, title=title[:32], description=desc[:255], payload=payload,
                           currency="XTR", prices=[LabeledPrice(label=title[:32], amount=int(stars))], provider_token="")


@r.callback_query(F.data == "sh:number")
async def sh_number(c: CallbackQuery):
    if not await guard(c, "shop", "shop_number") or not await need_account(c):
        return
    p = CFG["prices"]
    await c.answer()
    await show(c, f"📞 <b>Номер из 4 цифр</b>\n\n🎲 Рандом — {ps(p['num_rand'])}\n✍️ Свой номер — {ps(p['num_custom'])}\n\nВыберите:",
               kb([B("🎲 Рандом", "sn:r"), B("✍️ Ввести самому", "sn:c")], [B("← Назад", "shop")]))


@r.callback_query(F.data.in_({"sn:r", "sn:c"}))
async def sn_start(c: CallbackQuery, state: FSMContext):
    if not await guard(c, "shop", "shop_number") or not await need_account(c):
        return
    key = "num_rand" if c.data == "sn:r" else "num_custom"
    if CFG["prices"][key] is None:
        return await c.answer("⏳ Цена пока не установлена", show_alert=True)
    await state.set_state(S.rand_region if c.data == "sn:r" else S.cust_region)
    await c.answer()
    await show(c, "🌍 Введите код региона (например <code>7</code> или <code>1</code>):", CANCEL)


def valid_code(t):
    return bool(re.fullmatch(r"\d{1,3}", t or "")) and not t.startswith("0")


@r.message(S.rand_region, F.text)
async def rand_region(m: Message, state: FSMContext):
    code = m.text.strip().lstrip("+")
    if not valid_code(code):
        return await m.answer("Введите код региона цифрами, например <code>7</code>:", reply_markup=CANCEL)
    price = CFG["prices"]["num_rand"]
    if price is None:
        await state.clear()
        return await m.answer("⏳ Цена пока не установлена", reply_markup=kb([B("← Магазин", "shop")]))
    for _ in range(60):
        d = gen_digits(4, 2)
        if await item_free("phone", label_of(code, d)):
            await state.clear()
            lb = label_of(code, d)
            return await pay(m.chat.id, f"Номер {lb}", f"Случайный номер {lb} придёт на ваш аккаунт в мессенджере",
                             f"numr:{code}:{d}", price)
    await m.answer("Свободных номеров не нашлось, попробуйте другой регион.", reply_markup=CANCEL)


@r.message(S.cust_region, F.text)
async def cust_region(m: Message, state: FSMContext):
    code = m.text.strip().lstrip("+")
    if not valid_code(code):
        return await m.answer("Введите код региона цифрами, например <code>7</code>:", reply_markup=CANCEL)
    await state.update_data(code=code)
    await state.set_state(S.cust_digits)
    await m.answer(f"Введите 4 цифры номера (регион +{code}):", reply_markup=CANCEL)


@r.message(S.cust_digits, F.text)
async def cust_digits(m: Message, state: FSMContext):
    d = m.text.strip()
    code = (await state.get_data()).get("code")
    if not re.fullmatch(r"\d{4}", d):
        return await m.answer("Нужно ровно 4 цифры. Введите ещё раз:", reply_markup=CANCEL)
    lb = label_of(code, d)
    if not await item_free("phone", lb):
        return await m.answer(f"❌ Номер <code>{esc(lb)}</code> занят. Введите другие 4 цифры:", reply_markup=CANCEL)
    price = CFG["prices"]["num_custom"]
    if price is None:
        await state.clear()
        return await m.answer("⏳ Цена пока не установлена", reply_markup=kb([B("← Магазин", "shop")]))
    await state.clear()
    await pay(m.chat.id, f"Номер {lb}", f"Номер {lb} придёт на ваш аккаунт в мессенджере", f"numc:{code}:{d}", price)


@r.callback_query(F.data == "sh:username")
async def sh_username(c: CallbackQuery, state: FSMContext):
    if not await guard(c, "shop", "shop_username") or not await need_account(c):
        return
    price = CFG["prices"]["username"]
    if price is None:
        return await c.answer("⏳ Цена пока не установлена", show_alert=True)
    await state.set_state(S.username)
    await c.answer()
    await show(c, f"🏷 <b>NFT-юзернейм</b> — {ps(price)}\n\nВведите желаемый юзернейм (латиница, цифры, _; от 4 до 10 символов):", CANCEL)


@r.message(S.username, F.text)
async def username_in(m: Message, state: FSMContext):
    name = m.text.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,10}", name):
        return await m.answer("Только латиница, цифры и _, от 4 до 10 символов. Введите ещё раз:", reply_markup=CANCEL)
    if not await item_free("username", name):
        return await m.answer(f"❌ Юзернейм <code>@{esc(name)}</code> занят. Введите другой:", reply_markup=CANCEL)
    price = CFG["prices"]["username"]
    if price is None:
        await state.clear()
        return await m.answer("⏳ Цена пока не установлена", reply_markup=kb([B("← Магазин", "shop")]))
    await state.clear()
    await pay(m.chat.id, f"Юзернейм @{name}", f"Юзернейм @{name} придёт на ваш аккаунт в мессенджере", f"usr:{name}", price)


@r.callback_query(F.data == "sh:coins")
async def sh_coins(c: CallbackQuery):
    if not await guard(c, "shop", "shop_coins") or not await need_account(c):
        return
    await c.answer()
    rows = [[B(f"🪙 {p['coins']} кк — {ps(p['stars'])}", f"buy:coin:{i}")] for i, p in enumerate(CFG["prices"]["coins"])]
    await show(c, "🪙 <b>Выберите пакет Cat Coin</b>\nКурс: 1 ⭐ = 100 кк", kb(*rows, [B("← Назад", "shop")]))


@r.callback_query(F.data == "sh:premium")
async def sh_premium(c: CallbackQuery):
    if not await guard(c, "shop", "shop_premium") or not await need_account(c):
        return
    await c.answer()
    plans = sorted(CFG["prices"]["premium"].items(), key=lambda x: int(x[0]))
    rows = [[B(f"💎 {d} дн — {ps(v)}", f"buy:prem:{d}")] for d, v in plans]
    await show(c, "💎 <b>Orenix Premium</b>\nВыберите срок:", kb(*rows, [B("← Назад", "shop")]))


@r.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    _, kind, arg = c.data.split(":")
    if not await guard(c, "shop", "shop_coins" if kind == "coin" else "shop_premium") or not await need_account(c):
        return
    if kind == "coin":
        p = CFG["prices"]["coins"][int(arg)]
        price, title, payload = p["stars"], f"{p['coins']} Cat Coin", f"coin:{arg}:{p['coins']}"
    else:
        price, title, payload = CFG["prices"]["premium"].get(arg), f"Orenix Premium · {arg} дн", f"prem:{arg}"
    if price is None:
        return await c.answer("⏳ Цена пока не установлена", show_alert=True)
    await c.answer()
    await pay(c.message.chat.id, title, f"{title} — зачислится сразу после оплаты", payload, price)


# ───────────────────────── оплата Stars ─────────────────────────
async def check_payload(payload: str, uid: int):
    """-> (ok, error, stars). Сверяет цену и доступность с актуальными данными."""
    k, *a = payload.split(":")
    P = CFG["prices"]
    try:
        if k in ("numr", "numc"):
            price = P["num_rand" if k == "numr" else "num_custom"]
            if not await item_free("phone", label_of(a[0], a[1])):
                return False, "Этот номер уже занят", 0
        elif k == "usr":
            price = P["username"]
            if not await item_free("username", a[0]):
                return False, "Этот юзернейм уже занят", 0
        elif k == "coin":
            pack = P["coins"][int(a[0])]
            price = pack["stars"] if pack["coins"] == int(a[1]) else None
        elif k == "prem":
            price = P["premium"].get(a[0])
        else:
            return False, "Неизвестный товар", 0
    except Exception:
        return False, "Товар недоступен", 0
    if price is None:
        return False, "Цена изменилась, откройте магазин заново", 0
    if not await get_profile(uid):
        return False, "Аккаунт в мессенджере не найден", 0
    return True, "", price


@r.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    if q.from_user.id in BANNED:
        return await q.answer(ok=False, error_message="Вы заблокированы")
    ok, err, price = await check_payload(q.invoice_payload, q.from_user.id)
    if ok and price != q.total_amount:
        ok, err = False, "Цена изменилась, откройте магазин заново"
    await q.answer(ok=ok, error_message=err or None)


@r.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    uid, payload, stars = m.from_user.id, sp.invoice_payload, sp.total_amount
    k, *a = payload.split(":")
    try:
        await db.insert("ox_orders", {"charge_id": sp.telegram_payment_charge_id, "tg_id": uid, "kind": k,
                                       "payload": payload, "stars": stars, "status": "paid"})
    except Exception as e:
        if "409" in str(e) or "duplicate" in str(e).lower():
            return  # уже обработано
        log.error("order insert: %s", e)
    prof = await get_profile(uid)
    try:
        if not prof:
            raise RuntimeError("profile not found")
        if k in ("numr", "numc"):
            lb = label_of(a[0], a[1])
            if not await db.rpc("ox_grant_item", {"p_user": prof["id"], "p_tg": uid, "p_kind": "phone", "p_value": lb, "p_stars": stars}):
                raise RuntimeError("number taken")
            text = f"✅ Номер <code>{esc(lb)}</code> теперь на вашем аккаунте в Orenix Messenger!"
        elif k == "usr":
            if not await db.rpc("ox_grant_item", {"p_user": prof["id"], "p_tg": uid, "p_kind": "username", "p_value": "@" + a[0], "p_stars": stars}):
                raise RuntimeError("username taken")
            text = f"✅ Юзернейм <code>@{esc(a[0])}</code> теперь на вашем аккаунте в Orenix Messenger!"
        elif k == "coin":
            bal = await db.rpc("ox_add_balance", {"p_user": prof["id"], "p_amount": int(a[1])})
            text = f"✅ Зачислено <b>{a[1]} кк</b>. Баланс: <b>{bal}</b> кк"
        elif k == "prem":
            until = await db.rpc("ox_grant_premium", {"p_user": prof["id"], "p_days": int(a[0])})
            text = f"✅ Orenix Premium активен до <b>{parse_dt(until).strftime('%d.%m.%Y %H:%M')} UTC</b>"
        else:
            raise RuntimeError("unknown kind")
        await db.update("ox_orders", {"charge_id": sp.telegram_payment_charge_id}, {"status": "granted"})
        await m.answer(text, reply_markup=kb([B("👤 Профиль", "pf"), B("🛒 Магазин", "shop")]))
    except Exception as e:
        log.error("fulfill failed: %s", e)
        try:
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=sp.telegram_payment_charge_id)
            status = "refunded"
            await m.answer("⚠️ Не удалось выдать товар, звёзды возвращены. Попробуйте ещё раз или напишите в поддержку.")
        except Exception as e2:
            status = "failed"
            await m.answer("⚠️ Ошибка выдачи. Напишите в поддержку — всё исправим.")
            log.error("refund failed: %s", e2)
        await db.update("ox_orders", {"charge_id": sp.telegram_payment_charge_id}, {"status": status, "error": str(e)[:300]})
        for aid in MAIN_ADMINS | DB_ADMINS:
            try:
                await bot.send_message(aid, f"⚠️ Ошибка выдачи заказа ({k}) у <code>{uid}</code>: {esc(str(e)[:200])} → {status}")
            except Exception:
                pass


# ───────────────────────── поддержка ─────────────────────────
@r.callback_query(F.data == "support")
async def support(c: CallbackQuery, state: FSMContext):
    if not await guard(c, "support"):
        return
    await state.set_state(S.support)
    await c.answer()
    await show(c, "💬 <b>Поддержка</b>\n\nОпишите проблему одним сообщением — администраторы ответят прямо в этом чате.", CANCEL)


@r.message(S.support, F.text)
async def support_msg(m: Message, state: FSMContext):
    u = m.from_user
    tickets = await db.select("ox_tickets", {"tg_id": f"eq.{u.id}", "status": "eq.open", "limit": "1"})
    t = tickets[0] if tickets else (await db.insert("ox_tickets", {"tg_id": u.id, "name": u.full_name}))[0]
    await db.insert("ox_ticket_msgs", {"ticket_id": t["id"], "from_admin": False, "body": m.text})
    await db.update("ox_tickets", {"id": t["id"]}, {"updated_at": datetime.now(timezone.utc).isoformat()})
    await state.clear()
    text = (f"📩 <b>Обращение #{t['id']}</b>\nОт: {esc(u.full_name)} {('@' + u.username) if u.username else ''} "
            f"(<code>{u.id}</code>)\n\n{esc(m.text)}")
    markup = kb([B("✍️ Ответить", f"tk:r:{t['id']}"), B("✅ Закрыть", f"tk:c:{t['id']}")])
    for aid in MAIN_ADMINS | DB_ADMINS:
        try:
            await bot.send_message(aid, text, reply_markup=markup)
        except Exception:
            pass
    await m.answer(f"✅ Обращение #{t['id']} отправлено. Ответ придёт сюда.", reply_markup=kb([B("← Меню", "m:main")]))


# ───────────────────────── админ-панель ─────────────────────────
ar = Router()
ar.message.filter(lambda m: is_admin(m.from_user.id))
ar.callback_query.filter(lambda c: is_admin(c.from_user.id))
BACK_AD = kb([B("← Админ-панель", "ad:main")])


def admin_kb():
    return kb([B("💰 Цены", "ad:prices"), B("👁 Видимость", "ad:vis")],
              [B("🛠 Тех. работы", "ad:mt"), B("📊 Статистика", "ad:stats")],
              [B("🚫 Баны", "ad:ban"), B("👮 Админы", "ad:adm")],
              [B("📢 Рассылка", "ad:bc"), B("🌐 Ссылка на МС", "ad:link")],
              [B("🔗 Привязка аккаунтов", "ad:links")],
              [B("← Меню", "m:main")])


@ar.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@ar.callback_query(F.data == "ad:main")
async def ad_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show(c, "⚙️ <b>Админ-панель</b>", admin_kb())


# --- цены ---
def prices_kb():
    p = CFG["prices"]
    rows = [[B(f"🎲 Номер (рандом): {ps(p['num_rand'])}", "pr:num_rand")],
            [B(f"✍️ Номер (свой): {ps(p['num_custom'])}", "pr:num_custom")],
            [B(f"🏷 NFT-юз: {ps(p['username'])}", "pr:username")]]
    for d, v in sorted(p["premium"].items(), key=lambda x: int(x[0])):
        rows.append([B(f"💎 Premium {d} дн: {ps(v)}", f"pr:prem:{d}")])
    for i, cpk in enumerate(p["coins"]):
        rows.append([B(f"🪙 {cpk['coins']} кк: {ps(cpk['stars'])}", f"pr:coin:{i}")])
    rows.append([B("➕ Новый пак кк", "pc:add")])
    rows.append([B("← Админ-панель", "ad:main")])
    return kb(*rows)


@ar.callback_query(F.data == "ad:prices")
async def ad_prices(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show(c, "💰 <b>Цены</b> (нажмите, чтобы изменить)", prices_kb())


@ar.callback_query(F.data.startswith("pr:"))
async def pr_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_price)
    await state.update_data(key=c.data[3:])
    await c.answer()
    await show(c, "Введите новую цену в ⭐ (целое число) или «-», чтобы поставить прочерк (покупка будет закрыта).",
               kb([B("✖️ Отмена", "ad:prices")]))


@ar.callback_query(F.data == "pc:add")
async def pc_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_coinadd)
    await c.answer()
    await show(c, "Отправьте количество кк и цену в ⭐ через пробел.\n\n<code>3000 30</code> — пак 3000 кк за 30 ⭐\n"
                  "<code>3000</code> — цена по курсу (1 ⭐ = 100 кк)\n<code>3000 -</code> — без цены (прочерк)", kb([B("✖️ Отмена", "ad:prices")]))


@ar.message(S.adm_coinadd, F.text)
async def pc_add_do(m: Message, state: FSMContext):
    parts = m.text.split()
    try:
        coins = int(parts[0])
        stars = None if len(parts) > 1 and parts[1] in ("-", "—") else (int(parts[1]) if len(parts) > 1 else (coins // 100 or None))
        add_pack(coins, stars)
    except (ValueError, IndexError) as e:
        return await m.answer(str(e) if isinstance(e, ValueError) and not str(e).startswith("invalid") else "Формат: <code>3000 30</code>")
    await save_cfg()
    await state.clear()
    await m.answer("✅ Пак добавлен", reply_markup=prices_kb())


@ar.message(S.adm_price, F.text)
async def pr_set(m: Message, state: FSMContext):
    t = m.text.strip()
    if t in ("-", "—"):
        val = None
    elif t.isdigit() and 1 <= int(t) <= 100000:
        val = int(t)
    else:
        return await m.answer("Введите число от 1 до 100000 или «-».")
    key = (await state.get_data())["key"]
    set_price(key, val)
    await save_cfg()
    await state.clear()
    await m.answer("✅ Сохранено", reply_markup=prices_kb())


# --- видимость ---
def vis_kb():
    rows = [[B(("🔒 скрыт" if hidden(k) else "✅ показан") + f" · {name}", f"vs:{k}")] for k, name in SECTIONS.items()]
    return kb(*rows, [B("← Админ-панель", "ad:main")])


@ar.callback_query(F.data == "ad:vis")
async def ad_vis(c: CallbackQuery):
    await c.answer()
    await show(c, "👁 <b>Видимость разделов</b>\nСкрытые разделы видят только админы (с 🔒).", vis_kb())


@ar.callback_query(F.data.startswith("vs:"))
async def vs_toggle(c: CallbackQuery):
    k = c.data[3:]
    if k in SECTIONS:
        CFG["hidden"][k] = not hidden(k)
        await save_cfg()
    await c.answer()
    await show(c, "👁 <b>Видимость разделов</b>\nСкрытые разделы видят только админы (с 🔒).", vis_kb())


# --- тех. работы ---
def mt_kb():
    on = CFG["maintenance"]["on"]
    return kb([B("🔴 Выключить тех. работы" if on else "🟢 Включить тех. работы", "mt:toggle")],
              [B("✏️ Текст сообщения", "mt:text")], [B("← Админ-панель", "ad:main")])


def mt_text():
    mt = CFG["maintenance"]
    return f"🛠 <b>Тех. работы:</b> {'ВКЛ' if mt['on'] else 'выкл'}\n\nТекст для пользователей:\n{esc(mt['text'])}"


@ar.callback_query(F.data == "ad:mt")
async def ad_mt(c: CallbackQuery):
    await c.answer()
    await show(c, mt_text(), mt_kb())


@ar.callback_query(F.data == "mt:toggle")
async def mt_toggle(c: CallbackQuery):
    CFG["maintenance"]["on"] = not CFG["maintenance"]["on"]
    await save_cfg()
    await c.answer()
    await show(c, mt_text(), mt_kb())


@ar.callback_query(F.data == "mt:text")
async def mt_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_mt_text)
    await c.answer()
    await show(c, "Отправьте новый текст сообщения о тех. работах:", kb([B("✖️ Отмена", "ad:mt")]))


@ar.message(S.adm_mt_text, F.text)
async def mt_text_set(m: Message, state: FSMContext):
    CFG["maintenance"]["text"] = m.text.strip()[:1000]
    await save_cfg()
    await state.clear()
    await m.answer(mt_text(), reply_markup=mt_kb())


# --- ссылка на мессенджер ---
@ar.callback_query(F.data == "ad:link")
async def ad_link(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_link)
    await c.answer()
    await show(c, f"Текущая ссылка: {esc(CFG.get('messenger_url') or '—')}\n\nОтправьте https-ссылку на мессенджер или «-» чтобы убрать кнопку.",
               kb([B("✖️ Отмена", "ad:main")]))


@ar.message(S.adm_link, F.text)
async def link_set(m: Message, state: FSMContext):
    t = m.text.strip()
    if t != "-" and not t.startswith("https://"):
        return await m.answer("Ссылка должна начинаться с https://")
    CFG["messenger_url"] = "" if t == "-" else t
    await save_cfg()
    await state.clear()
    await m.answer("✅ Сохранено", reply_markup=BACK_AD)


# --- баны ---
async def find_user(q: str):
    q = q.strip().lstrip("@")
    if q.isdigit():
        return int(q)
    rows = await db.select("ox_users", {"username": f"ilike.{q}", "limit": "1"})
    return rows[0]["tg_id"] if rows else None


@ar.callback_query(F.data == "ad:ban")
async def ad_ban(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show(c, f"🚫 <b>Баны</b>\nЗаблокировано: {len(BANNED)}",
               kb([B("🚫 Забанить", "bn:add"), B("♻️ Разбанить", "bn:del")], [B("📋 Список", "bn:list")], [B("← Админ-панель", "ad:main")]))


@ar.callback_query(F.data == "bn:list")
async def bn_list(c: CallbackQuery):
    await c.answer()
    rows = await db.select("ox_users", {"banned": "eq.true", "limit": "50"})
    txt = "\n".join(f"• <code>{x['tg_id']}</code> {('@' + x['username']) if x.get('username') else ''}" for x in rows) or "Список пуст"
    await show(c, f"🚫 <b>Забанены:</b>\n{txt}", kb([B("← Назад", "ad:ban")]))


@ar.callback_query(F.data.in_({"bn:add", "bn:del"}))
async def bn_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_ban if c.data == "bn:add" else S.adm_unban)
    await c.answer()
    await show(c, "Отправьте ID или @username пользователя:", kb([B("✖️ Отмена", "ad:ban")]))


@ar.message(S.adm_ban, F.text)
@ar.message(S.adm_unban, F.text)
async def bn_do(m: Message, state: FSMContext):
    ban = (await state.get_state()) == S.adm_ban.state
    uid = await find_user(m.text)
    if not uid:
        return await m.answer("Пользователь не найден. Отправьте числовой ID или @username из базы бота.")
    if is_admin(uid):
        return await m.answer("Нельзя банить администратора.")
    await db.insert("ox_users", {"tg_id": uid, "banned": ban}, upsert=True, conflict="tg_id")
    (BANNED.add if ban else BANNED.discard)(uid)
    await state.clear()
    await m.answer(f"{'🚫 Забанен' if ban else '♻️ Разбанен'}: <code>{uid}</code>", reply_markup=kb([B("← Баны", "ad:ban")]))
    try:
        await bot.send_message(uid, "🚫 Вы заблокированы." if ban else "✅ Доступ к боту восстановлен.")
    except Exception:
        pass


# --- админы ---
@ar.callback_query(F.data == "ad:adm")
async def ad_adm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    txt = "\n".join([f"• <code>{x}</code> (главный)" for x in sorted(MAIN_ADMINS)] + [f"• <code>{x}</code>" for x in sorted(DB_ADMINS)])
    await show(c, f"👮 <b>Админы</b>\n{txt}", kb([B("➕ Добавить", "am:add"), B("➖ Удалить", "am:del")], [B("← Админ-панель", "ad:main")]))


@ar.callback_query(F.data.in_({"am:add", "am:del"}))
async def am_ask(c: CallbackQuery, state: FSMContext):
    if c.data == "am:del" and c.from_user.id not in MAIN_ADMINS:
        return await c.answer("Удалять админов может только главный админ", show_alert=True)
    await state.set_state(S.adm_addadm if c.data == "am:add" else S.adm_rmadm)
    await c.answer()
    await show(c, "Отправьте ID или @username (пользователь должен был запускать бота):", kb([B("✖️ Отмена", "ad:adm")]))


@ar.message(S.adm_addadm, F.text)
@ar.message(S.adm_rmadm, F.text)
async def am_do(m: Message, state: FSMContext):
    add = (await state.get_state()) == S.adm_addadm.state
    uid = await find_user(m.text)
    if not uid:
        return await m.answer("Не найден. Отправьте числовой ID или @username.")
    if add:
        await db.insert("ox_admins", {"tg_id": uid, "added_by": m.from_user.id}, upsert=True, conflict="tg_id")
        DB_ADMINS.add(uid)
    else:
        if uid in MAIN_ADMINS:
            return await m.answer("Главного админа удалить нельзя.")
        await db.delete("ox_admins", {"tg_id": uid})
        DB_ADMINS.discard(uid)
    await state.clear()
    await m.answer(f"{'✅ Добавлен' if add else '✅ Удалён'}: <code>{uid}</code>", reply_markup=kb([B("← Админы", "ad:adm")]))


# --- статистика ---
@ar.callback_query(F.data == "ad:stats")
async def ad_stats(c: CallbackQuery):
    await c.answer()
    users = await db.select("ox_users", {"select": "tg_id", "limit": "100000"})
    orders = await db.select("ox_orders", {"select": "stars,status", "limit": "100000"})
    tickets = await db.select("ox_tickets", {"status": "eq.open", "select": "id"})
    done = [o for o in orders if o["status"] == "granted"]
    await show(c, f"📊 <b>Статистика</b>\n\n👥 Пользователей: {len(users)}\n🚫 В бане: {len(BANNED)}\n"
                  f"🧾 Заказов: {len(done)}\n⭐ Звёзд получено: {sum(o['stars'] or 0 for o in done)}\n💬 Открытых обращений: {len(tickets)}",
               BACK_AD)


# --- привязка существующих аккаунтов ---
async def links_text():
    links = CFG.get("links", {})
    if not links:
        return "🔗 <b>Привязка аккаунтов</b>\n\nПривязок нет."
    rows = await db.select("profiles", {"id": "in.(" + ",".join(links.values()) + ")", "select": "id,phone,username"})
    by_id = {x["id"]: x for x in rows}
    lines = []
    for tg, pid in links.items():
        p = by_id.get(pid)
        lines.append(f"• <code>{tg}</code> → {esc(p['phone'])} {esc(p.get('username') or '')}" if p
                     else f"• <code>{tg}</code> → профиль не найден")
    return "🔗 <b>Привязка аккаунтов</b>\n\n" + "\n".join(lines)


def links_kb():
    return kb([B("➕ Привязать", "lk:add"), B("➖ Убрать", "lk:del")], [B("← Админ-панель", "ad:main")])


@ar.callback_query(F.data == "ad:links")
async def ad_links(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show(c, await links_text(), links_kb())


@ar.callback_query(F.data == "lk:add")
async def lk_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_lnk_add)
    await c.answer()
    await show(c, "Отправьте одним сообщением: <b>Telegram ID</b> и <b>номер или @юзернейм</b> аккаунта на сайте.\n\n"
                  "Пример:\n<code>5570425300 +7 912 345 678 9</code>\n<code>5570425300 @username</code>",
               kb([B("✖️ Отмена", "ad:links")]))


@ar.message(S.adm_lnk_add, F.text)
async def lk_add_do(m: Message, state: FSMContext):
    parts = m.text.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return await m.answer("Формат: <code>ID номер</code>, например <code>5570425300 +7 912 345 678 9</code>")
    tid, q = parts[0], parts[1].strip()
    prof = None
    if q.startswith("@"):
        rows = await db.select("profiles", {"username": f"ilike.{q}", "select": "id,phone,username", "limit": "5"})
        prof = next((x for x in rows if (x.get("username") or "").lower() == q.lower()), None)
    else:
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            rows = await db.select("profiles", {"phone": "like.*" + "*".join(digits) + "*",
                                                "select": "id,phone,username", "limit": "50"})
            prof = next((x for x in rows if re.sub(r"\D", "", x.get("phone") or "") == digits), None)
    if not prof:
        return await m.answer("Профиль на сайте не найден. Проверьте номер или @юзернейм и отправьте ещё раз.")
    CFG.setdefault("links", {})[tid] = prof["id"]
    await save_cfg()
    await state.clear()
    await m.answer(f"✅ Telegram <code>{tid}</code> привязан к {esc(prof['phone'])} {esc(prof.get('username') or '')}",
                   reply_markup=kb([B("← Привязки", "ad:links")]))


@ar.callback_query(F.data == "lk:del")
async def lk_del(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_lnk_del)
    await c.answer()
    await show(c, "Отправьте Telegram ID, у которого нужно убрать привязку:", kb([B("✖️ Отмена", "ad:links")]))


@ar.message(S.adm_lnk_del, F.text)
async def lk_del_do(m: Message, state: FSMContext):
    tid = m.text.strip()
    if tid not in CFG.get("links", {}):
        return await m.answer("Такой привязки нет. Отправьте Telegram ID из списка.")
    CFG["links"].pop(tid)
    await save_cfg()
    await state.clear()
    await m.answer("✅ Привязка убрана", reply_markup=kb([B("← Привязки", "ad:links")]))


# --- рассылка ---
@ar.callback_query(F.data == "ad:bc")
async def ad_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_bc)
    await c.answer()
    await show(c, "📢 Отправьте текст рассылки (HTML-разметка поддерживается):", kb([B("✖️ Отмена", "ad:main")]))


@ar.message(S.adm_bc, F.text)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(text=m.html_text)
    await m.answer(f"Предпросмотр:\n\n{m.html_text}", reply_markup=kb([B("🚀 Отправить всем", "bc:go")], [B("✖️ Отмена", "ad:main")]))


@ar.callback_query(F.data == "bc:go")
async def bc_go(c: CallbackQuery, state: FSMContext):
    text = (await state.get_data()).get("text")
    await state.clear()
    if not text:
        return await c.answer("Нет текста", show_alert=True)
    await c.answer("Рассылка запущена")
    users = await db.select("ox_users", {"banned": "eq.false", "select": "tg_id", "limit": "100000"})
    ok = 0
    for u in users:
        try:
            await bot.send_message(u["tg_id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await c.message.answer(f"✅ Доставлено: {ok} из {len(users)}", reply_markup=BACK_AD)


# --- тикеты (ответ админа) ---
@ar.callback_query(F.data.startswith("tk:r:"))
async def tk_reply(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adm_reply)
    await state.update_data(tid=int(c.data.split(":")[2]))
    await c.answer()
    await c.message.answer(f"✍️ Введите ответ на обращение #{c.data.split(':')[2]}:", reply_markup=CANCEL)


@ar.message(S.adm_reply, F.text)
async def tk_send(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    rows = await db.select("ox_tickets", {"id": f"eq.{tid}", "limit": "1"})
    if not rows:
        await state.clear()
        return await m.answer("Обращение не найдено.")
    await db.insert("ox_ticket_msgs", {"ticket_id": tid, "from_admin": True, "admin_id": m.from_user.id, "body": m.text})
    await db.update("ox_tickets", {"id": tid}, {"updated_at": datetime.now(timezone.utc).isoformat()})
    await state.clear()
    try:
        await bot.send_message(rows[0]["tg_id"], f"💬 <b>Ответ поддержки (#{tid}):</b>\n\n{esc(m.text)}",
                               reply_markup=kb([B("✉️ Написать ещё", "support")]))
        await m.answer(f"✅ Ответ на #{tid} отправлен.")
    except Exception:
        await m.answer("⚠️ Не удалось доставить (пользователь заблокировал бота?). Ответ сохранён.")


@ar.callback_query(F.data.startswith("tk:c:"))
async def tk_close(c: CallbackQuery):
    tid = int(c.data.split(":")[2])
    await db.update("ox_tickets", {"id": tid}, {"status": "closed"})
    await c.answer(f"Обращение #{tid} закрыто")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ───────────────────────── запуск ─────────────────────────
async def ensure_web_admin():
    """Создаёт вход в веб-админку (admin.html): почта/пароль из WEB_ADMIN_EMAIL / WEB_ADMIN_PASSWORD."""
    if not (WEB_ADMIN_EMAIL and WEB_ADMIN_PASSWORD):
        return
    try:
        resp = await db.c.post("/auth/v1/admin/users", json={"email": WEB_ADMIN_EMAIL, "password": WEB_ADMIN_PASSWORD, "email_confirm": True})
        uid = resp.json().get("id") if resp.status_code < 300 else None
        if not uid:
            lst = await db.c.get("/auth/v1/admin/users", params={"per_page": 1000})
            uid = next((u["id"] for u in lst.json().get("users", []) if (u.get("email") or "").lower() == WEB_ADMIN_EMAIL.lower()), None)
        if not uid:
            return log.warning("web admin: пользователь не создан")
        for row in ({"user_id": uid, "role": "super_admin", "is_active": True, "email": WEB_ADMIN_EMAIL},
                    {"user_id": uid, "role": "super_admin", "is_active": True}):
            try:
                await db.insert("admins", row, upsert=True, conflict="user_id")
                return log.info("web admin готов: %s", WEB_ADMIN_EMAIL)
            except Exception as e:
                log.warning("admins upsert: %s", e)
    except Exception as e:
        log.warning("ensure_web_admin: %s", e)


SCHEMA = {
    "ox_users": "tg_id,username,first_name,banned,last_seen", "ox_settings": "key,value", "ox_admins": "tg_id,added_by",
    "orenix_pending": "id,user_id,phone,code,entered_at,notified",
    "ox_owned": "id,user_id,tg_id,kind,value,stars", "ox_orders": "id,charge_id,tg_id,kind,payload,stars,status,error",
    "ox_tickets": "id,tg_id,name,status,updated_at", "ox_ticket_msgs": "id,ticket_id,from_admin,admin_id,body",
}


async def check_schema():
    bad = []
    for t, cols in SCHEMA.items():
        try:
            await db.select(t, {"select": cols, "limit": "1"})
        except Exception as e:
            bad.append(f"{t}: {str(e)[:160]}")
    try:
        await db.select("profiles", {"select": "id,phone,username,display_name,avatar_url,balance,is_premium,premium_until,is_deleted", "limit": "1"})
    except Exception as e:
        bad.append(f"profiles: {str(e)[:160]}")
    try:
        await db.rpc("ox_item_free", {"p_kind": "phone", "p_value": "+0 0"})
    except Exception as e:
        bad.append(f"ox_item_free: {str(e)[:160]}")
    if bad:
        log.error("СХЕМА БД НЕ ГОТОВА (запустите SQL-части 1-4):\n%s", "\n".join(bad))
    else:
        log.info("Схема БД проверена: всё на месте")


# ───────────────────────── Mini App API ─────────────────────────
SUPPORT_AT: dict = {}
BG_TASKS: set = set()
APP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")


class ApiError(Exception):
    pass


def verify_init(init: str):
    """Проверка подписи Telegram WebApp initData. Возвращает dict пользователя или None."""
    try:
        pairs = dict(parse_qsl(init, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got) or time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


async def profile_lists(prof):
    phones, names = [prof.get("phone")], [prof.get("username")]
    try:
        for x in await db.select("ox_owned", {"user_id": f"eq.{prof['id']}", "select": "kind,value", "order": "id"}):
            (phones if x["kind"] == "phone" else names).append(x["value"])
    except Exception as e:
        log.warning("ox_owned: %s", e)
    for fn, args, field, dst in (("extra_phones_for", {"p_ids": [prof["id"]]}, "phone", phones),
                                 ("extra_usernames_for", {"p_type": "user", "p_ids": [prof["id"]]}, "username", names)):
        try:
            dst += [x[field] for x in (await db.rpc(fn, args) or [])]
        except Exception:
            pass

    def uniq(a):
        seen, out = set(), []
        for x in a:
            k = re.sub(r"[\s@+]", "", str(x or "")).lower()
            if k and k not in seen:
                seen.add(k); out.append(str(x))
        return out
    return uniq(phones), uniq(names)


async def build_state(uid: int) -> dict:
    prof = await get_profile(uid)
    st = {"ok": True, "is_admin": is_admin(uid), "hidden": CFG["hidden"], "prices": CFG["prices"],
          "messenger_url": CFG.get("messenger_url", ""), "profile": None, "reg": None, "old": CFG.get("old", {})}
    if prof:
        phones, names = await profile_lists(prof)
        until = parse_dt(prof.get("premium_until"))
        active = bool(prof.get("is_premium")) and (not until or until > datetime.now(timezone.utc))
        st["profile"] = {"name": prof.get("display_name") or "Пользователь Orenix", "phones": phones, "names": names,
                         "balance": int(float(prof.get("balance") or 0)), "is_premium": active,
                         "premium_until": prof.get("premium_until"), "avatar": prof.get("avatar_url") or ""}
        return st
    row = await get_pending(uid)
    if row:
        region, body = pend_parts(row)
        code, left, expired = row.get("code"), 0, False
        if code:
            issued = CODE_AT.setdefault(uid, time.time())
            left = max(0, 300 - int(time.time() - issued))
            if left == 0:
                code, expired = None, True
        st["reg"] = {"region": region, "body": body, "entered": bool(row.get("entered_at")),
                     "code": code, "left": left, "expired": expired}
    return st


async def create_pending(uid: int, code: str):
    region = "+" + code
    for _ in range(40):
        body = gen_digits(10, 3, no_adjacent=True, first="23456789")
        if await number_taken(region, body):
            continue
        try:
            return (await db.insert(PENDING, {"user_id": uid, "phone": region + body, "code": None,
                                              "entered_at": None, "notified": False}))[0]
        except RuntimeError as e:
            if any(x in str(e).lower() for x in ("409", "duplicate", "unique")):
                row = await get_pending(uid)
                if row:
                    return row
                continue
            raise
    raise ApiError("Не удалось подобрать номер, попробуйте ещё раз")


def api_route(fn):
    async def wrap(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user = verify_init(body.get("init", ""))
        if not user:
            return web.json_response({"error": "Откройте Mini App из Telegram"}, status=401)
        uid = user["id"]
        try:
            await touch_user(SimpleNamespace(id=uid, username=user.get("username"), first_name=user.get("first_name")))
            if not is_admin(uid):
                if uid in BANNED:
                    return web.json_response({"blocked": "banned", "text": "Вы заблокированы."})
                if CFG["maintenance"]["on"]:
                    return web.json_response({"blocked": "maintenance", "text": CFG["maintenance"]["text"]})
                if not await is_subscribed(uid):
                    return web.json_response({"blocked": "sub", "link": CHANNEL_LINK})
            return web.json_response(await fn(uid, user, body))
        except ApiError as e:
            return web.json_response({"error": str(e)})
        except Exception as e:
            log.error("api %s: %s", fn.__name__, e)
            return web.json_response({"error": "Ошибка сервера, попробуйте позже"})
    return wrap


@api_route
async def api_state(uid, user, body):
    return await build_state(uid)


@api_route
async def api_reg_start(uid, user, body):
    if await get_profile(uid) or await get_pending(uid):
        return await build_state(uid)
    if hidden("get_number") and not is_admin(uid):
        raise ApiError("Раздел временно недоступен")
    code = str(body.get("region"))
    if code not in ("7", "1"):
        raise ApiError("Неверный регион")
    await create_pending(uid, code)
    return await build_state(uid)


@api_route
async def api_reg_code(uid, user, body):
    row = await get_pending(uid)
    if not row:
        raise ApiError("Сначала получите номер")
    if not row.get("entered_at"):
        raise ApiError("Сначала введите номер на сайте и нажмите «Далее»")
    issued = CODE_AT.get(uid)
    if not row.get("code") or (issued and time.time() - issued > 300):
        await db.update(PENDING, {"id": row["id"]}, {"code": f"{random.randint(0, 9999):04d}"})
        CODE_AT[uid] = time.time()
    return await build_state(uid)


@api_route
async def api_pay(uid, user, body):
    if not await get_profile(uid):
        raise ApiError("Сначала зарегистрируйтесь в мессенджере")
    kind, P = body.get("kind"), CFG["prices"]
    sect = {"numr": "shop_number", "numc": "shop_number", "usr": "shop_username", "coin": "shop_coins", "prem": "shop_premium"}
    if kind not in sect:
        raise ApiError("Неизвестный товар")
    if not is_admin(uid) and (hidden("shop") or hidden(sect[kind])):
        raise ApiError("Раздел временно недоступен")
    if kind in ("numr", "numc"):
        code = str(body.get("code", "")).strip().lstrip("+")
        if not valid_code(code):
            raise ApiError("Введите код региона цифрами, например 7")
        price = P["num_rand"] if kind == "numr" else P["num_custom"]
        if price is None:
            raise ApiError("Цена пока не установлена")
        if kind == "numr":
            for _ in range(60):
                d = gen_digits(4, 2)
                if await item_free("phone", label_of(code, d)):
                    break
            else:
                raise ApiError("Свободных номеров не нашлось, попробуйте другой регион")
        else:
            d = str(body.get("digits", "")).strip()
            if not re.fullmatch(r"\d{4}", d):
                raise ApiError("Нужно ровно 4 цифры")
            if not await item_free("phone", label_of(code, d)):
                raise ApiError(f"Номер {label_of(code, d)} занят")
        title, payload = f"Номер {label_of(code, d)}", f"{kind}:{code}:{d}"
    elif kind == "usr":
        name = str(body.get("name", "")).strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,10}", name):
            raise ApiError("Юзернейм: 4–10 символов, латиница, цифры и _")
        price = P["username"]
        if price is None:
            raise ApiError("Цена пока не установлена")
        if not await item_free("username", name):
            raise ApiError(f"@{name} занят")
        title, payload = f"Юзернейм @{name}", f"usr:{name}"
    elif kind == "coin":
        idx = int(body.get("idx", -1))
        if not 0 <= idx < len(P["coins"]):
            raise ApiError("Пак не найден")
        pack = P["coins"][idx]
        price, title, payload = pack["stars"], f"{pack['coins']} Cat Coin", f"coin:{idx}:{pack['coins']}"
    else:
        days = str(body.get("days"))
        price, title, payload = P["premium"].get(days), f"Orenix Premium · {days} дн", f"prem:{days}"
    if price is None:
        raise ApiError("Цена пока не установлена")
    link = await bot.create_invoice_link(title=title[:32], description=f"{title} — зачислится сразу после оплаты"[:255],
                                         payload=payload, currency="XTR", provider_token="",
                                         prices=[LabeledPrice(label=title[:32], amount=int(price))])
    return {"ok": True, "link": link}


@api_route
async def api_support(uid, user, body):
    text = str(body.get("text", "")).strip()[:2000]
    if len(text) < 3:
        raise ApiError("Напишите сообщение")
    if time.time() - SUPPORT_AT.get(uid, 0) < 20:
        raise ApiError("Подождите немного перед следующим обращением")
    SUPPORT_AT[uid] = time.time()
    tickets = await db.select("ox_tickets", {"tg_id": f"eq.{uid}", "status": "eq.open", "limit": "1"})
    name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip() or str(uid)
    t = tickets[0] if tickets else (await db.insert("ox_tickets", {"tg_id": uid, "name": name}))[0]
    await db.insert("ox_ticket_msgs", {"ticket_id": t["id"], "from_admin": False, "body": text})
    await db.update("ox_tickets", {"id": t["id"]}, {"updated_at": datetime.now(timezone.utc).isoformat()})
    msg = (f"📩 <b>Обращение #{t['id']}</b> (Mini App)\nОт: {esc(name)} {('@' + user['username']) if user.get('username') else ''} "
           f"(<code>{uid}</code>)\n\n{esc(text)}")
    markup = kb([B("✍️ Ответить", f"tk:r:{t['id']}"), B("✅ Закрыть", f"tk:c:{t['id']}")])
    for aid in MAIN_ADMINS | DB_ADMINS:
        try:
            await bot.send_message(aid, msg, reply_markup=markup)
        except Exception:
            pass
    return {"ok": True}


async def admin_payload(extra=None):
    users = await db.select("ox_users", {"select": "tg_id", "limit": "100000"})
    orders = await db.select("ox_orders", {"select": "stars,status", "limit": "100000"})
    tickets = await db.select("ox_tickets", {"status": "eq.open", "select": "id"})
    banned = await db.select("ox_users", {"banned": "eq.true", "select": "tg_id,username", "limit": "50"})
    links = []
    if CFG.get("links"):
        rows = await db.select("profiles", {"id": "in.(" + ",".join(CFG["links"].values()) + ")", "select": "id,phone,username"})
        by = {x["id"]: x for x in rows}
        for tg_id, pid in CFG["links"].items():
            p = by.get(pid)
            links.append({"tg": tg_id, "label": f"{p['phone']} {p.get('username') or ''}".strip() if p else "профиль не найден"})
    done = [o for o in orders if o["status"] == "granted"]
    return {"ok": True, "prices": CFG["prices"], "hidden": CFG["hidden"], "old": CFG.get("old", {}), "admin": {
        "maintenance": CFG["maintenance"], "messenger_url": CFG.get("messenger_url", ""),
        "stats": {"users": len(users), "banned": len(BANNED), "orders": len(done),
                  "stars": sum(o["stars"] or 0 for o in done), "tickets": len(tickets)},
        "banned": banned, "admins": {"main": sorted(MAIN_ADMINS), "extra": sorted(DB_ADMINS)}, "links": links}}


async def broadcast(text: str):
    users = await db.select("ox_users", {"banned": "eq.false", "select": "tg_id", "limit": "100000"})
    for u in users:
        try:
            await bot.send_message(u["tg_id"], esc(text))
        except Exception:
            pass
        await asyncio.sleep(0.05)


@api_route
async def api_admin(uid, user, body):
    if not is_admin(uid):
        raise ApiError("Нет доступа")
    op, P = body.get("op"), CFG["prices"]
    if op == "price":
        key, v = str(body.get("key")), body.get("value")
        if v is not None:
            v = int(v)
            if not 1 <= v <= 100000:
                raise ApiError("Цена: от 1 до 100000 или прочерк")
        if not ((key.startswith("prem:") and key[5:] in P["premium"]) or key.startswith("coin:")
                or key in ("num_rand", "num_custom", "username")):
            raise ApiError("Неизвестная цена")
        set_price(key, v)
        await save_cfg()
    elif op == "coin_add":
        try:
            raw = body.get("stars")
            add_pack(int(body.get("coins")), None if raw in (None, "") else int(raw))
        except (ValueError, TypeError) as e:
            raise ApiError(str(e) if "кк" in str(e) or "пак" in str(e) or "Цена" in str(e) else "Введите числа")
        await save_cfg()
    elif op == "coin_del":
        pack = P["coins"].pop(int(body.get("idx")))
        CFG.setdefault("old", {}).pop(f"coin:{pack['coins']}", None)
        await save_cfg()
    elif op == "hide":
        if body.get("key") not in SECTIONS:
            raise ApiError("Неизвестный раздел")
        CFG["hidden"][body["key"]] = not hidden(body["key"])
        await save_cfg()
    elif op == "mt":
        if "on" in body:
            CFG["maintenance"]["on"] = bool(body["on"])
        if body.get("text"):
            CFG["maintenance"]["text"] = str(body["text"]).strip()[:1000]
        await save_cfg()
    elif op == "url":
        u = str(body.get("url", "")).strip()
        if u and not u.startswith("https://"):
            raise ApiError("Ссылка должна начинаться с https://")
        CFG["messenger_url"] = u
        await save_cfg()
    elif op in ("ban", "unban"):
        tid = await find_user(str(body.get("q", "")))
        if not tid:
            raise ApiError("Пользователь не найден (он должен был запускать бота)")
        if op == "ban" and is_admin(tid):
            raise ApiError("Нельзя банить администратора")
        await db.insert("ox_users", {"tg_id": tid, "banned": op == "ban"}, upsert=True, conflict="tg_id")
        (BANNED.add if op == "ban" else BANNED.discard)(tid)
    elif op in ("adm_add", "adm_del"):
        tid = await find_user(str(body.get("q", "")))
        if not tid:
            raise ApiError("Пользователь не найден (он должен был запускать бота)")
        if op == "adm_add":
            await db.insert("ox_admins", {"tg_id": tid, "added_by": uid}, upsert=True, conflict="tg_id")
            DB_ADMINS.add(tid)
        else:
            if uid not in MAIN_ADMINS:
                raise ApiError("Удалять админов может только главный админ")
            if tid in MAIN_ADMINS:
                raise ApiError("Главного админа удалить нельзя")
            await db.delete("ox_admins", {"tg_id": tid})
            DB_ADMINS.discard(tid)
    elif op == "lk_add":
        tid, q = str(body.get("id", "")).strip(), str(body.get("q", "")).strip()
        if not tid.isdigit() or not q:
            raise ApiError("Укажите Telegram ID и номер или @юзернейм")
        prof = None
        if q.startswith("@"):
            rows = await db.select("profiles", {"username": f"ilike.{q}", "select": "id,username", "limit": "5"})
            prof = next((x for x in rows if (x.get("username") or "").lower() == q.lower()), None)
        else:
            digits = re.sub(r"\D", "", q)
            if len(digits) >= 3:
                rows = await db.select("profiles", {"phone": "like.*" + "*".join(digits) + "*", "select": "id,phone", "limit": "50"})
                prof = next((x for x in rows if re.sub(r"\D", "", x.get("phone") or "") == digits), None)
        if not prof:
            raise ApiError("Профиль на сайте не найден")
        CFG.setdefault("links", {})[tid] = prof["id"]
        await save_cfg()
    elif op == "lk_del":
        if CFG.get("links", {}).pop(str(body.get("id")), None) is None:
            raise ApiError("Такой привязки нет")
        await save_cfg()
    elif op == "bc":
        text = str(body.get("text", "")).strip()[:3500]
        if len(text) < 2:
            raise ApiError("Введите текст")
        t = asyncio.create_task(broadcast(text))
        BG_TASKS.add(t)
        t.add_done_callback(BG_TASKS.discard)
    elif op != "get":
        raise ApiError("Неизвестная операция")
    return await admin_payload()


async def serve_app(_):
    try:
        with open(APP_FILE, encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html", headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return web.Response(status=404, text="miniapp.html не найден рядом с bot.py")


async def health(_):
    return web.Response(text="ok")


async def main():
    await check_schema()
    await load_cfg()
    for x in await db.select("ox_admins", {"select": "tg_id"}):
        DB_ADMINS.add(x["tg_id"])
    for x in await db.select("ox_users", {"banned": "eq.true", "select": "tg_id"}):
        BANNED.add(x["tg_id"])
    await ensure_web_admin()

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(sr)
    dp.include_router(ar)   # админ-хендлеры раньше пользовательских
    dp.include_router(r)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/app", serve_app)
    for name, fn in (("state", api_state), ("reg_start", api_reg_start), ("reg_code", api_reg_code),
                     ("pay", api_pay), ("support", api_support), ("admin", api_admin)):
        app.router.add_post(f"/api/{name}", fn)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    asyncio.create_task(watcher())
    if APP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Orenix", web_app=WebAppInfo(url=APP_URL)))
        except Exception as e:
            log.warning("menu button: %s", e)
        log.info("Mini App: %s", APP_URL)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Orenix bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
