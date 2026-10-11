"""
KINODAGILA — kodli kino boti
aiogram 3.x + SQLite
Hamma narsa bot ichidan boshqariladi: kinolar, majburiy obuna, rassilka, sozlamalar.
"""
import asyncio
import html
import logging
import os
import random
import re
import time
from urllib.parse import quote

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


ENV_FOUND = {}  # .env dan o'qilgan kalitlar: nom -> qiymat uzunligi (qiymatning o'zi saqlanmaydi)


def load_env(path: Path):
    """.env ni o'zi o'qiydi: BOM, UTF-16, qo'shtirnoq va bo'sh joylarga chidamli."""
    if not path.is_file():
        return
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="ignore")
    else:
        text = raw.decode("utf-8-sig", errors="ignore")
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip().strip("\"'").strip()
        if key:
            os.environ[key] = val
            ENV_FOUND[key] = len(val)


load_env(ENV_PATH)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kinodagila")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}  # set[int] — .env dagi ADMIN_IDS
DB_PATH = os.getenv("DB_PATH") or "kinodagila.db"
if not os.path.isabs(DB_PATH):
    DB_PATH = str(BASE_DIR / DB_PATH)
PAGE_SIZE = 8
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")

DEFAULT_WELCOME = (
    "🎬 <b>KINODAGILA</b> olamiga xush kelibsiz, {name}!\n\n"
    "🔑 Kino <b>kodini</b> yuboring — kino darhol chiqadi.\n"
    "🔎 Yoki kino <b>nomini</b> yozing.\n\n"
    "👇 Pastdagi menyudan ham foydalanishingiz mumkin."
)
DEFAULTS = {
    "welcome": DEFAULT_WELCOME,
    "instagram": "",
    "footer": "",
    "protect": "0",
    "sub_on": "1",
}

# ───────────────────────────── DATABASE ─────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
    joined_at INTEGER, last_seen INTEGER, banned INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS movies(
    code TEXT PRIMARY KEY COLLATE NOCASE, title TEXT, title_lc TEXT, year TEXT, genre TEXT,
    lang TEXT, quality TEXT, description TEXT, file_id TEXT, file_type TEXT,
    views INTEGER DEFAULT 0, added_at INTEGER, added_by INTEGER);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, chat_id INTEGER, title TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, added_at INTEGER);
CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, code TEXT, PRIMARY KEY(user_id, code));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def fetchall(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return await cur.fetchall()


async def fetchone(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return await cur.fetchone()


async def run(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def get_setting(key):
    row = await fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else DEFAULTS.get(key, "")


async def set_setting(key, value):
    await run("INSERT INTO settings(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


async def touch_user(u):
    now = int(time.time())
    await run(
        "INSERT INTO users(id,full_name,username,joined_at,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, "
        "username=excluded.username, last_seen=excluded.last_seen, active=1",
        (u.id, u.full_name, u.username, now, now))


async def is_admin(uid):
    if uid in OWNER_IDS:
        return True
    return bool(await fetchone("SELECT 1 FROM admins WHERE id=?", (uid,)))


async def get_movie(code):
    return await fetchone("SELECT * FROM movies WHERE code=?", (code.strip(),))


_me = None


async def bot_username(bot):
    global _me
    if _me is None:
        _me = await bot.me()
    return _me.username


# ───────────────────────────── HELPERS ─────────────────────────────
esc = html.escape


class MovieCb(CallbackData, prefix="mv"):
    action: str
    code: str


class EditCb(CallbackData, prefix="ed"):
    field: str
    code: str


class PageCb(CallbackData, prefix="pg"):
    page: int


class ChDelCb(CallbackData, prefix="chd"):
    id: int


class AdmDelCb(CallbackData, prefix="amd"):
    id: int


class SetCb(CallbackData, prefix="st"):
    key: str


def movie_caption(m, footer=""):
    lines = [f"🎬 <b>{esc(m['title'])}</b>" + (f" ({esc(m['year'])})" if m["year"] else ""),
             "━━━━━━━━━━━━━━"]
    if m["genre"]:
        lines.append(f"🎭 <b>Janr:</b> {esc(m['genre'])}")
    if m["lang"]:
        lines.append(f"🌐 <b>Til:</b> {esc(m['lang'])}")
    if m["quality"]:
        lines.append(f"📀 <b>Sifat:</b> {esc(m['quality'])}")
    lines.append(f"🔑 <b>Kod:</b> <code>{esc(m['code'])}</code>")
    lines.append(f"👁 <b>Ko'rishlar:</b> {m['views']}")
    if m["description"]:
        lines.append("\n" + esc(m["description"][:300]))
    if footer:
        lines.append("\n" + esc(footer[:100]))
    return "\n".join(lines)


def movie_kb(code, fav, username, title):
    kb = InlineKeyboardBuilder()
    if fav:
        kb.button(text="💔 Saqlanganlardan olib tashlash", callback_data=MovieCb(action="unfav", code=code))
    else:
        kb.button(text="⭐ Saqlab qo'yish", callback_data=MovieCb(action="fav", code=code))
    link = f"https://t.me/{username}?start={code}"
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎬 ' + title)}"
    kb.button(text="📤 Do'stlarga yuborish", url=share)
    kb.adjust(1)
    return kb.as_markup()


async def send_movie(bot, chat_id, uid, m):
    await run("UPDATE movies SET views=views+1 WHERE code=?", (m["code"],))
    m = await get_movie(m["code"])
    footer = await get_setting("footer")
    protect = (await get_setting("protect")) == "1"
    fav = bool(await fetchone("SELECT 1 FROM favorites WHERE user_id=? AND code=?", (uid, m["code"])))
    kb = movie_kb(m["code"], fav, await bot_username(bot), m["title"])
    cap = movie_caption(m, footer)
    if m["file_type"] == "video":
        await bot.send_video(chat_id, m["file_id"], caption=cap, reply_markup=kb, protect_content=protect)
    else:
        await bot.send_document(chat_id, m["file_id"], caption=cap, reply_markup=kb, protect_content=protect)


async def show(ev, text, kb=None):
    """Callback bo'lsa xabarni tahrirlaydi, aks holda yangi xabar yuboradi."""
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=kb)
        try:
            await ev.answer()
        except TelegramBadRequest:
            pass
    else:
        await ev.answer(text, reply_markup=kb)


def cancel_kb(extra=None):
    kb = InlineKeyboardBuilder()
    if extra:
        for text, data in extra:
            kb.button(text=text, callback_data=data)
    kb.button(text="❌ Bekor qilish", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


def main_menu(admin=False):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔎 Kino qidirish")
    kb.button(text="🔥 Top kinolar")
    kb.button(text="🆕 Yangi kinolar")
    kb.button(text="🎲 Tasodifiy kino")
    kb.button(text="⭐ Saqlanganlar")
    kb.button(text="ℹ️ Bot haqida")
    if admin:
        kb.button(text="🛠 Admin panel")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, input_field_placeholder="Kino kodini yoki nomini yozing…")


async def welcome(bot, ev_message: Message, user):
    text = (await get_setting("welcome")).replace("{name}", esc(user.full_name))
    insta = await get_setting("instagram")
    await ev_message.answer(text, reply_markup=main_menu(await is_admin(user.id)))
    if insta:
        ib = InlineKeyboardBuilder()
        ib.button(text="📸 Kodlar Instagramda", url=insta)
        await ev_message.answer("Yangi kino kodlari birinchi bo'lib Instagramda 👇", reply_markup=ib.as_markup())


# ───────────────────────────── SUBSCRIPTION ─────────────────────────────
pending_codes = {}


async def missing_channels(bot, uid):
    missing = []
    for ch in await fetchall("SELECT * FROM channels WHERE kind='chat'"):
        try:
            m = await bot.get_chat_member(ch["chat_id"], uid)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("Obuna tekshiruvi xato (%s): %s", ch["title"], e)
            continue
        if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
            missing.append(ch)
        elif m.status == ChatMemberStatus.RESTRICTED and not m.is_member:
            missing.append(ch)
    return missing


async def sub_prompt(target: Message, missing):
    kb = InlineKeyboardBuilder()
    for ch in missing:
        kb.button(text=f"📢 {ch['title']}", url=ch["url"])
    for ln in await fetchall("SELECT * FROM channels WHERE kind='link'"):
        kb.button(text=f"🔗 {ln['title']}", url=ln["url"])
    kb.button(text="✅ Tekshirish", callback_data="check_sub")
    kb.adjust(1)
    await target.answer(
        "🔐 <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling</b>\n\n"
        "Obuna bo'lgach, <b>«✅ Tekshirish»</b> tugmasini bosing.",
        reply_markup=kb.as_markup())


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if user is None:
            return await handler(event, data)
        bot = data["bot"]
        await touch_user(user)
        row = await fetchone("SELECT banned FROM users WHERE id=?", (user.id,))
        if row and row["banned"]:
            return
        if await is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        if (await get_setting("sub_on")) == "1":
            missing = await missing_channels(bot, user.id)
            if missing:
                target = event if isinstance(event, Message) else event.message
                if isinstance(event, Message) and event.text and event.text.startswith("/start "):
                    pending_codes[user.id] = event.text.split(maxsplit=1)[1].strip()
                await sub_prompt(target, missing)
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and await is_admin(event.from_user.id)


# ───────────────────────────── ROUTERS ─────────────────────────────
admin = Router()
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())

user = Router()
user.message.middleware(Guard())
user.callback_query.middleware(Guard())


# ═════════════════════════════ USER PART ═════════════════════════════
@user.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    if command.args:
        movie = await get_movie(command.args)
        if movie:
            await m.answer("🍿 Kino tayyorlanmoqda…", reply_markup=main_menu(await is_admin(m.from_user.id)))
            return await send_movie(bot, m.chat.id, m.from_user.id, movie)
    await welcome(bot, m, m.from_user)


@user.message(Command("help"))
@user.message(F.text == "ℹ️ Bot haqida")
async def about(m: Message):
    await m.answer(
        "🎬 <b>KINODAGILA</b>\n\n"
        "Bu bot orqali sevimli kinolaringizni <b>kod</b> yoki <b>nom</b> bilan topasiz.\n\n"
        "🔑 Kodni yuboring — kino chiqadi\n"
        "🔎 Nomini yozing — qidiruv ishlaydi\n"
        "⭐ Yoqqanini saqlab qo'ying\n"
        "🎲 Nimani ko'rishni bilmasangiz — tasodifiy kino\n\n"
        "Yaxshi tomosha! 🍿")


@user.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    missing = await missing_channels(bot, c.from_user.id)
    if missing:
        return await c.answer("❌ Hali hamma kanalga obuna bo'lmagansiz!", show_alert=True)
    await c.answer("✅ Rahmat! Obuna tasdiqlandi")
    try:
        await c.message.delete()
    except TelegramBadRequest:
        pass
    code = pending_codes.pop(c.from_user.id, None)
    if code and (movie := await get_movie(code)):
        await c.message.answer("🍿 Kino tayyorlanmoqda…",
                               reply_markup=main_menu(await is_admin(c.from_user.id)))
        return await send_movie(bot, c.message.chat.id, c.from_user.id, movie)
    await welcome(bot, c.message, c.from_user)


async def movies_list_kb(rows, action="show"):
    kb = InlineKeyboardBuilder()
    for r in rows:
        label = f"🎬 {r['title']}" + (f" ({r['year']})" if r["year"] else "") + f" · {r['code']}"
        kb.button(text=label[:60], callback_data=MovieCb(action=action, code=r["code"]))
    kb.adjust(1)
    return kb.as_markup()


@user.message(F.text == "🔥 Top kinolar")
async def top_movies(m: Message):
    rows = await fetchall("SELECT * FROM movies ORDER BY views DESC LIMIT 10")
    if not rows:
        return await m.answer("Hozircha kinolar yo'q 🎞")
    await m.answer("🔥 <b>Eng ko'p ko'rilgan kinolar</b>", reply_markup=await movies_list_kb(rows))


@user.message(F.text == "🆕 Yangi kinolar")
async def new_movies(m: Message):
    rows = await fetchall("SELECT * FROM movies ORDER BY added_at DESC LIMIT 10")
    if not rows:
        return await m.answer("Hozircha kinolar yo'q 🎞")
    await m.answer("🆕 <b>Yangi qo'shilgan kinolar</b>", reply_markup=await movies_list_kb(rows))


@user.message(F.text == "🎲 Tasodifiy kino")
async def random_movie(m: Message, bot: Bot):
    rows = await fetchall("SELECT * FROM movies")
    if not rows:
        return await m.answer("Hozircha kinolar yo'q 🎞")
    await send_movie(bot, m.chat.id, m.from_user.id, random.choice(rows))


@user.message(F.text == "⭐ Saqlanganlar")
async def saved_movies(m: Message):
    rows = await fetchall(
        "SELECT mv.* FROM favorites f JOIN movies mv ON mv.code=f.code "
        "WHERE f.user_id=? ORDER BY mv.title", (m.from_user.id,))
    if not rows:
        return await m.answer("⭐ Saqlangan kinolar yo'q.\nKino tagidagi «Saqlab qo'yish» tugmasini bosing.")
    await m.answer("⭐ <b>Saqlangan kinolaringiz</b>", reply_markup=await movies_list_kb(rows))


@user.message(F.text == "🔎 Kino qidirish")
async def search_hint(m: Message):
    await m.answer("🔎 Kino <b>nomini</b> yoki <b>kodini</b> yozib yuboring:")


@user.callback_query(MovieCb.filter(F.action == "show"))
async def cb_show(c: CallbackQuery, callback_data: MovieCb, bot: Bot):
    movie = await get_movie(callback_data.code)
    if not movie:
        return await c.answer("Kino topilmadi", show_alert=True)
    await c.answer()
    await send_movie(bot, c.message.chat.id, c.from_user.id, movie)


@user.callback_query(MovieCb.filter(F.action.in_({"fav", "unfav"})))
async def cb_fav(c: CallbackQuery, callback_data: MovieCb, bot: Bot):
    movie = await get_movie(callback_data.code)
    if not movie:
        return await c.answer("Kino topilmadi", show_alert=True)
    if callback_data.action == "fav":
        await run("INSERT OR IGNORE INTO favorites(user_id,code) VALUES(?,?)", (c.from_user.id, movie["code"]))
        await c.answer("⭐ Saqlandi")
    else:
        await run("DELETE FROM favorites WHERE user_id=? AND code=?", (c.from_user.id, movie["code"]))
        await c.answer("Olib tashlandi")
    kb = movie_kb(movie["code"], callback_data.action == "fav", await bot_username(bot), movie["title"])
    try:
        await c.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass


# Eng oxirida — kod yoki nom bo'yicha qidiruv
@user.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def by_text(m: Message, bot: Bot):
    q = m.text.strip()
    movie = await get_movie(q)
    if movie:
        return await send_movie(bot, m.chat.id, m.from_user.id, movie)
    rows = await fetchall(
        "SELECT * FROM movies WHERE title_lc LIKE ? OR genre LIKE ? ORDER BY views DESC LIMIT 10",
        (f"%{q.lower()}%", f"%{q}%"))
    if not rows:
        return await m.answer("😔 Bunday kod yoki nomli kino topilmadi.\n"
                              "Kodni to'g'ri yozganingizni tekshiring.")
    if len(rows) == 1:
        return await send_movie(bot, m.chat.id, m.from_user.id, rows[0])
    await m.answer(f"🔎 <b>«{esc(q)}»</b> bo'yicha topilganlar:", reply_markup=await movies_list_kb(rows))


# ═════════════════════════════ ADMIN PART ═════════════════════════════
class AddMovie(StatesGroup):
    video = State()
    code = State()
    title = State()
    fields = State()


class EditMovie(StatesGroup):
    value = State()


class AddChannel(StatesGroup):
    chat = State()
    link = State()


class Broadcast(StatesGroup):
    content = State()


class AdminAdd(StatesGroup):
    uid = State()


class BanUser(StatesGroup):
    uid = State()


class SettingEdit(StatesGroup):
    value = State()


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistika", callback_data="adm:stats")
    kb.button(text="🎬 Kino qo'shish", callback_data="adm:addmovie")
    kb.button(text="🗂 Kinolar", callback_data=PageCb(page=0))
    kb.button(text="🔐 Majburiy obuna", callback_data="adm:subs")
    kb.button(text="📣 Rassilka", callback_data="adm:bc")
    kb.button(text="🚫 Ban / Unban", callback_data="adm:ban")
    kb.button(text="⚙️ Sozlamalar", callback_data="adm:settings")
    kb.button(text="👥 Adminlar", callback_data="adm:admins")
    kb.adjust(2)
    return kb.as_markup()


ADMIN_TITLE = "🛠 <b>KINODAGILA — Admin panel</b>\n\nKerakli bo'limni tanlang:"


@admin.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_menu(True))


@admin.message(Command("admin"))
@admin.message(F.text == "🛠 Admin panel")
async def open_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_TITLE, reply_markup=admin_menu_kb())


@admin.callback_query(F.data == "adm:menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, ADMIN_TITLE, admin_menu_kb())


def back_kb(target="adm:menu", text="⬅️ Orqaga"):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=target)
    return kb.as_markup()


# ───────── Statistika ─────────
@admin.callback_query(F.data == "adm:stats")
async def cb_stats(c: CallbackQuery):
    now = int(time.time())
    day = now - now % 86400
    total = (await fetchone("SELECT COUNT(*) n FROM users"))["n"]
    today = (await fetchone("SELECT COUNT(*) n FROM users WHERE joined_at>=?", (day,)))["n"]
    active = (await fetchone("SELECT COUNT(*) n FROM users WHERE last_seen>=?", (now - 86400,)))["n"]
    blocked = (await fetchone("SELECT COUNT(*) n FROM users WHERE active=0"))["n"]
    movies = (await fetchone("SELECT COUNT(*) n, COALESCE(SUM(views),0) v FROM movies"))
    chans = (await fetchone("SELECT COUNT(*) n FROM channels"))["n"]
    top = await fetchall("SELECT title, code, views FROM movies ORDER BY views DESC LIMIT 3")
    medals = ["🥇", "🥈", "🥉"]
    top_txt = "\n".join(f"{medals[i]} {esc(t['title'])} — {t['views']} ta" for i, t in enumerate(top)) or "—"
    text = (
        "📊 <b>Statistika</b>\n━━━━━━━━━━━━━━\n"
        f"👥 Foydalanuvchilar: <b>{total}</b>\n"
        f"🆕 Bugun qo'shilgan: <b>{today}</b>\n"
        f"⚡️ So'nggi 24 soatda faol: <b>{active}</b>\n"
        f"🚪 Botni bloklaganlar: <b>{blocked}</b>\n\n"
        f"🎬 Kinolar: <b>{movies['n']}</b>\n"
        f"👁 Jami ko'rishlar: <b>{movies['v']}</b>\n"
        f"🔐 Obuna kanallari: <b>{chans}</b>\n\n"
        f"🔥 <b>Top 3:</b>\n{top_txt}")
    await show(c, text, back_kb())


# ───────── Kino qo'shish ─────────
FIELDS = [
    ("year", "📅 Kino chiqqan <b>yilni</b> yuboring (masalan: 2024)"),
    ("genre", "🎭 <b>Janrni</b> yuboring (masalan: Jangari, Drama)"),
    ("lang", "🌐 <b>Tilni</b> yuboring (masalan: O'zbek tilida)"),
    ("quality", "📀 <b>Sifatni</b> yuboring (masalan: 1080p)"),
    ("description", "📝 Qisqa <b>tavsif</b> yuboring"),
]


@admin.callback_query(F.data == "adm:addmovie")
async def add_movie_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddMovie.video)
    await show(c, "🎬 <b>Yangi kino qo'shish</b>\n\n1️⃣ Kinoni <b>video</b> yoki <b>fayl</b> ko'rinishida yuboring:",
               cancel_kb())


@admin.message(AddMovie.video, F.video | F.document)
async def add_movie_video(m: Message, state: FSMContext):
    if m.video:
        file_id, ftype = m.video.file_id, "video"
    else:
        file_id, ftype = m.document.file_id, "document"
    await state.update_data(file_id=file_id, file_type=ftype)
    await state.set_state(AddMovie.code)
    await m.answer("2️⃣ Kino <b>kodini</b> yuboring (harf/raqam, masalan: <code>245</code>)\n"
                   "yoki avtomatik kod oling:",
                   reply_markup=cancel_kb([("🎲 Avto kod", "add:autocode")]))


@admin.message(AddMovie.video)
async def add_movie_video_wrong(m: Message):
    await m.answer("❗️ Iltimos, kinoni video yoki fayl sifatida yuboring.")


async def next_code():
    row = await fetchone("SELECT COALESCE(MAX(CAST(code AS INTEGER)),0)+1 n FROM movies")
    return str(row["n"])


async def accept_code(ev, state, code):
    if not CODE_RE.match(code):
        return await (ev.answer if isinstance(ev, Message) else ev.message.answer)(
            "❗️ Kod faqat harf, raqam, «-» va «_» dan iborat bo'lsin (20 tagacha).")
    if await get_movie(code):
        return await (ev.answer if isinstance(ev, Message) else ev.message.answer)(
            "⚠️ Bu kod band. Boshqa kod yuboring.")
    await state.update_data(code=code)
    await state.set_state(AddMovie.title)
    target = ev if isinstance(ev, Message) else ev.message
    if isinstance(ev, CallbackQuery):
        await ev.answer()
    await target.answer(f"✅ Kod: <code>{esc(code)}</code>\n\n3️⃣ Kino <b>nomini</b> yuboring:",
                        reply_markup=cancel_kb())


@admin.callback_query(AddMovie.code, F.data == "add:autocode")
async def add_autocode(c: CallbackQuery, state: FSMContext):
    await accept_code(c, state, await next_code())


@admin.message(AddMovie.code, F.text)
async def add_code(m: Message, state: FSMContext):
    await accept_code(m, state, m.text.strip())


@admin.message(AddMovie.title, F.text)
async def add_title(m: Message, state: FSMContext):
    title = m.text.strip()
    if len(title) > 100:
        return await m.answer("❗️ Nom juda uzun (100 belgigacha).")
    await state.update_data(title=title, idx=0)
    await state.set_state(AddMovie.fields)
    await ask_field(m, state)


async def ask_field(target: Message, state: FSMContext):
    idx = (await state.get_data())["idx"]
    _, prompt = FIELDS[idx]
    await target.answer(f"{idx + 4}️⃣ {prompt}",
                        reply_markup=cancel_kb([("⏭ O'tkazib yuborish", "add:skip")]))


async def advance(ev, state: FSMContext, value, bot: Bot):
    data = await state.get_data()
    idx = data["idx"]
    key = FIELDS[idx][0]
    if value and key == "year" and not (value.isdigit() and len(value) == 4):
        target = ev if isinstance(ev, Message) else ev.message
        return await target.answer("❗️ Yilni 4 xonali raqam bilan yozing (masalan: 2024).")
    data[key] = value or ""
    data["idx"] = idx + 1
    await state.set_data(data)
    target = ev if isinstance(ev, Message) else ev.message
    if isinstance(ev, CallbackQuery):
        await ev.answer()
    if data["idx"] < len(FIELDS):
        return await ask_field(target, state)
    # Saqlash
    await run(
        "INSERT INTO movies(code,title,title_lc,year,genre,lang,quality,description,file_id,file_type,added_at,added_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (data["code"], data["title"], data["title"].lower(), data.get("year", ""), data.get("genre", ""),
         data.get("lang", ""), data.get("quality", ""), data.get("description", ""),
         data["file_id"], data["file_type"], int(time.time()), ev.from_user.id))
    await state.clear()
    uname = await bot_username(bot)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Yana kino qo'shish", callback_data="adm:addmovie")
    kb.button(text="🛠 Admin panel", callback_data="adm:menu")
    kb.adjust(1)
    await target.answer(
        "✅ <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🎬 {esc(data['title'])}\n🔑 Kod: <code>{esc(data['code'])}</code>\n"
        f"🔗 Havola: <code>https://t.me/{uname}?start={esc(data['code'])}</code>\n\n"
        "Havolani Instagramga qo'yishingiz mumkin — bosilishi bilan kino ochiladi.",
        reply_markup=kb.as_markup())


@admin.message(AddMovie.fields, F.text)
async def add_field(m: Message, state: FSMContext, bot: Bot):
    await advance(m, state, m.text.strip(), bot)


@admin.callback_query(AddMovie.fields, F.data == "add:skip")
async def add_skip(c: CallbackQuery, state: FSMContext, bot: Bot):
    await advance(c, state, "", bot)


# ───────── Kinolar ro'yxati / tahrirlash ─────────
@admin.callback_query(PageCb.filter())
async def cb_movie_page(c: CallbackQuery, callback_data: PageCb, state: FSMContext):
    await state.clear()
    page = max(callback_data.page, 0)
    total = (await fetchone("SELECT COUNT(*) n FROM movies"))["n"]
    rows = await fetchall("SELECT * FROM movies ORDER BY added_at DESC LIMIT ? OFFSET ?",
                          (PAGE_SIZE, page * PAGE_SIZE))
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🎬 {r['title']} · {r['code']}"[:60], callback_data=MovieCb(action="card", code=r["code"]))
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=PageCb(page=page - 1))
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=PageCb(page=page + 1))
    nav.button(text="🛠 Panel", callback_data="adm:menu")
    kb.attach(nav)
    await show(c, f"🗂 <b>Kinolar</b> — jami: {total}\nSahifa: {page + 1}", kb.as_markup())


def admin_card(m):
    return (f"🎬 <b>{esc(m['title'])}</b>\n━━━━━━━━━━━━━━\n"
            f"🔑 Kod: <code>{esc(m['code'])}</code>\n"
            f"📅 Yil: {esc(m['year'] or '—')}\n🎭 Janr: {esc(m['genre'] or '—')}\n"
            f"🌐 Til: {esc(m['lang'] or '—')}\n📀 Sifat: {esc(m['quality'] or '—')}\n"
            f"👁 Ko'rishlar: {m['views']}\n📝 {esc((m['description'] or '—')[:200])}")


EDIT_LABELS = {"title": "✏️ Nomi", "year": "📅 Yil", "genre": "🎭 Janr",
               "lang": "🌐 Til", "quality": "📀 Sifat", "description": "📝 Tavsif"}


async def show_card(ev, code):
    m = await get_movie(code)
    if not m:
        return await show(ev, "Kino topilmadi.", back_kb("adm:menu"))
    kb = InlineKeyboardBuilder()
    for f, label in EDIT_LABELS.items():
        kb.button(text=label, callback_data=EditCb(field=f, code=code))
    kb.adjust(3)
    more = InlineKeyboardBuilder()
    more.button(text="▶️ Ko'rish", callback_data=MovieCb(action="show", code=code))
    more.button(text="🗑 O'chirish", callback_data=MovieCb(action="del", code=code))
    more.button(text="⬅️ Ro'yxat", callback_data=PageCb(page=0))
    more.adjust(2, 1)
    kb.attach(more)
    await show(ev, admin_card(m), kb.as_markup())


@admin.callback_query(MovieCb.filter(F.action == "card"))
async def cb_card(c: CallbackQuery, callback_data: MovieCb):
    await show_card(c, callback_data.code)


@admin.callback_query(MovieCb.filter(F.action == "del"))
async def cb_del(c: CallbackQuery, callback_data: MovieCb):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=MovieCb(action="delok", code=callback_data.code))
    kb.button(text="❌ Yo'q", callback_data=MovieCb(action="card", code=callback_data.code))
    kb.adjust(2)
    await show(c, f"🗑 <code>{esc(callback_data.code)}</code> kodli kinoni o'chirishni tasdiqlaysizmi?", kb.as_markup())


@admin.callback_query(MovieCb.filter(F.action == "delok"))
async def cb_delok(c: CallbackQuery, callback_data: MovieCb):
    await run("DELETE FROM movies WHERE code=?", (callback_data.code,))
    await run("DELETE FROM favorites WHERE code=?", (callback_data.code,))
    await c.answer("🗑 O'chirildi")
    await show(c, "🗑 Kino o'chirildi.", back_kb(PageCb(page=0).pack(), "⬅️ Ro'yxat"))


@admin.callback_query(EditCb.filter())
async def cb_edit(c: CallbackQuery, callback_data: EditCb, state: FSMContext):
    await state.set_state(EditMovie.value)
    await state.update_data(code=callback_data.code, field=callback_data.field)
    await c.answer()
    await c.message.answer(f"{EDIT_LABELS[callback_data.field]} uchun yangi qiymatni yuboring:",
                           reply_markup=cancel_kb())


@admin.message(EditMovie.value, F.text)
async def edit_value(m: Message, state: FSMContext):
    data = await state.get_data()
    field, code, val = data["field"], data["code"], m.text.strip()
    if field == "year" and not (val.isdigit() and len(val) == 4):
        return await m.answer("❗️ Yilni 4 xonali raqam bilan yozing.")
    if field == "title":
        await run("UPDATE movies SET title=?, title_lc=? WHERE code=?", (val, val.lower(), code))
    else:
        await run(f"UPDATE movies SET {field}=? WHERE code=?", (val, code))
    await state.clear()
    await m.answer("✅ Yangilandi.")
    await show_card(m, code)


# ───────── Majburiy obuna ─────────
async def subs_view(ev):
    on = (await get_setting("sub_on")) == "1"
    chans = await fetchall("SELECT * FROM channels ORDER BY id")
    lines = []
    kb = InlineKeyboardBuilder()
    for i, ch in enumerate(chans, 1):
        icon = "📢" if ch["kind"] == "chat" else "🔗"
        lines.append(f"{i}. {icon} {esc(ch['title'])}")
        kb.button(text=f"🗑 {ch['title']}"[:40], callback_data=ChDelCb(id=ch["id"]))
    kb.adjust(1)
    ctl = InlineKeyboardBuilder()
    ctl.button(text="➕ Kanal / guruh qo'shish", callback_data="adm:addch")
    ctl.button(text="🔗 Havola qo'shish (Instagram va h.k.)", callback_data="adm:addlink")
    ctl.button(text="🔴 O'chirish" if on else "🟢 Yoqish", callback_data="adm:subtoggle")
    ctl.button(text="⬅️ Orqaga", callback_data="adm:menu")
    ctl.adjust(1)
    kb.attach(ctl)
    text = (f"🔐 <b>Majburiy obuna</b>\nHolat: {'🟢 Yoqilgan' if on else '🔴 O`chirilgan'}\n\n"
            + ("\n".join(lines) if lines else "Hozircha kanal qo'shilmagan.")
            + "\n\n<i>📢 — obuna tekshiriladi (bot kanalda admin bo'lishi kerak)\n"
              "🔗 — faqat tugma sifatida ko'rsatiladi</i>")
    await show(ev, text, kb.as_markup())


@admin.callback_query(F.data == "adm:subs")
async def cb_subs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await subs_view(c)


@admin.callback_query(F.data == "adm:subtoggle")
async def cb_subtoggle(c: CallbackQuery):
    on = (await get_setting("sub_on")) == "1"
    await set_setting("sub_on", "0" if on else "1")
    await subs_view(c)


@admin.callback_query(ChDelCb.filter())
async def cb_chdel(c: CallbackQuery, callback_data: ChDelCb):
    await run("DELETE FROM channels WHERE id=?", (callback_data.id,))
    await c.answer("🗑 O'chirildi")
    await subs_view(c)


@admin.callback_query(F.data == "adm:addch")
async def cb_addch(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.chat)
    await show(c, "📢 <b>Kanal / guruh qo'shish</b>\n\n"
                  "Avval botni kanalga <b>admin</b> qiling, so'ng quyidagilardan birini yuboring:\n"
                  "• kanaldan <b>forward</b> qilingan xabar\n"
                  "• kanal <b>@username</b>\n"
                  "• kanal ID (<code>-100…</code>)",
               cancel_kb())


@admin.message(AddChannel.chat)
async def add_channel(m: Message, state: FSMContext, bot: Bot):
    chat = None
    try:
        origin = getattr(m, "forward_origin", None)
        if origin is not None and getattr(origin, "chat", None):
            chat = await bot.get_chat(origin.chat.id)
        else:
            t = (m.text or "").strip()
            if "t.me/" in t:
                t = "@" + t.split("t.me/")[1].split("/")[0].split("?")[0]
            if t.lstrip("-").isdigit():
                chat = await bot.get_chat(int(t))
            elif t.startswith("@"):
                chat = await bot.get_chat(t)
    except TelegramBadRequest:
        return await m.answer("❗️ Kanal topilmadi. Username/ID ni tekshiring va qayta yuboring.")
    if chat is None:
        return await m.answer("❗️ Tushunmadim. Forward xabar, @username yoki ID yuboring.")
    me = await bot.me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest:
        return await m.answer("❗️ Bot bu kanalda topilmadi. Avval botni kanalga admin qiling.")
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return await m.answer("❗️ Bot kanalda <b>admin</b> emas. Admin qilib, qayta yuboring.")
    if await fetchone("SELECT 1 FROM channels WHERE chat_id=?", (chat.id,)):
        await state.clear()
        return await m.answer("⚠️ Bu kanal allaqachon qo'shilgan.", reply_markup=back_kb("adm:subs"))
    url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not url:
        try:
            url = await bot.export_chat_invite_link(chat.id)
        except TelegramBadRequest:
            return await m.answer("❗️ Havola olib bo'lmadi. Botga «Havola yaratish» huquqini bering.")
    await run("INSERT INTO channels(kind,chat_id,title,url) VALUES('chat',?,?,?)", (chat.id, chat.title, url))
    await state.clear()
    await m.answer(f"✅ <b>{esc(chat.title)}</b> majburiy obunaga qo'shildi!", reply_markup=back_kb("adm:subs"))


@admin.callback_query(F.data == "adm:addlink")
async def cb_addlink(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.link)
    await show(c, "🔗 <b>Havola qo'shish</b>\n\nFormat: <code>Tugma nomi | https://havola</code>\n"
                  "Masalan: <code>Instagram | https://instagram.com/kinodagila</code>\n\n"
                  "<i>Bunday havolada obuna tekshirilmaydi, faqat tugma sifatida chiqadi.</i>",
               cancel_kb())


@admin.message(AddChannel.link, F.text)
async def add_link(m: Message, state: FSMContext):
    if "|" not in m.text:
        return await m.answer("❗️ Format: <code>Nomi | https://havola</code>")
    title, url = [x.strip() for x in m.text.split("|", 1)]
    if not url.startswith(("http://", "https://")) or not title:
        return await m.answer("❗️ Havola http:// yoki https:// bilan boshlansin.")
    await run("INSERT INTO channels(kind,chat_id,title,url) VALUES('link',0,?,?)", (title, url))
    await state.clear()
    await m.answer("✅ Havola qo'shildi!", reply_markup=back_kb("adm:subs"))


# ───────── Rassilka ─────────
bg_tasks = set()


@admin.callback_query(F.data == "adm:bc")
async def cb_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.content)
    await show(c, "📣 <b>Rassilka</b>\n\nHamma foydalanuvchiga yuboriladigan xabarni yuboring "
                  "(matn, rasm, video — istalgan turdagi).", cancel_kb())


@admin.message(Broadcast.content)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id)
    n = (await fetchone("SELECT COUNT(*) n FROM users WHERE banned=0 AND active=1"))["n"]
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Yuborish ({n} ta)", callback_data="bc:go")
    kb.button(text="❌ Bekor qilish", callback_data="adm:menu")
    kb.adjust(1)
    await m.reply(f"Yuqoridagi xabar <b>{n}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
                  reply_markup=kb.as_markup())


async def do_broadcast(bot: Bot, admin_id, src_chat, src_msg):
    users = await fetchall("SELECT id FROM users WHERE banned=0 AND active=1")
    ok = fail = 0
    for u in users:
        try:
            await bot.copy_message(u["id"], src_chat, src_msg)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(u["id"], src_chat, src_msg)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            await run("UPDATE users SET active=0 WHERE id=?", (u["id"],))
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    await bot.send_message(admin_id, f"📣 <b>Rassilka tugadi</b>\n\n✅ Yetkazildi: <b>{ok}</b>\n❌ Xato: <b>{fail}</b>")


@admin.callback_query(Broadcast.content, F.data == "bc:go")
async def bc_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await c.answer("Rassilka boshlandi")
    await c.message.edit_text("⏳ Rassilka yuborilmoqda… Tugagach xabar beraman.")
    task = asyncio.create_task(do_broadcast(bot, c.from_user.id, data["src_chat"], data["src_msg"]))
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


# ───────── Ban ─────────
@admin.callback_query(F.data == "adm:ban")
async def cb_ban(c: CallbackQuery, state: FSMContext):
    await state.set_state(BanUser.uid)
    await show(c, "🚫 <b>Ban / Unban</b>\n\nFoydalanuvchi <b>ID</b> raqamini yuboring "
                  "(holati almashtiriladi):", cancel_kb())


@admin.message(BanUser.uid, F.text)
async def ban_toggle(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("❗️ Faqat raqamli ID yuboring.")
    uid = int(m.text.strip())
    row = await fetchone("SELECT banned FROM users WHERE id=?", (uid,))
    if not row:
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    if uid in OWNER_IDS:
        return await m.answer("Egasini bloklab bo'lmaydi.")
    new = 0 if row["banned"] else 1
    await run("UPDATE users SET banned=? WHERE id=?", (new, uid))
    await state.clear()
    await m.answer(("🚫 Bloklandi" if new else "✅ Blokdan chiqarildi") + f": <code>{uid}</code>",
                   reply_markup=back_kb())


# ───────── Adminlar (faqat egalar) ─────────
@admin.callback_query(F.data == "adm:admins")
async def cb_admins(c: CallbackQuery, state: FSMContext):
    await state.clear()
    if c.from_user.id not in OWNER_IDS:
        return await c.answer("Bu bo'lim faqat bot egasi uchun", show_alert=True)
    rows = await fetchall("SELECT id FROM admins")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 {r['id']}", callback_data=AdmDelCb(id=r["id"]))
    kb.adjust(2)
    ctl = InlineKeyboardBuilder()
    ctl.button(text="➕ Admin qo'shish", callback_data="adm:addadmin")
    ctl.button(text="⬅️ Orqaga", callback_data="adm:menu")
    ctl.adjust(1)
    kb.attach(ctl)
    owners = "\n".join(f"👑 <code>{i}</code>" for i in OWNER_IDS)
    adm = "\n".join(f"👤 <code>{r['id']}</code>" for r in rows) or "—"
    await show(c, f"👥 <b>Adminlar</b>\n\n<b>Egalar:</b>\n{owners}\n\n<b>Adminlar:</b>\n{adm}", kb.as_markup())


@admin.callback_query(F.data == "adm:addadmin")
async def cb_addadmin(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in OWNER_IDS:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(AdminAdd.uid)
    await show(c, "👤 Yangi adminning <b>Telegram ID</b> raqamini yuboring:", cancel_kb())


@admin.message(AdminAdd.uid, F.text)
async def add_admin(m: Message, state: FSMContext):
    if m.from_user.id not in OWNER_IDS:
        return
    if not m.text.strip().isdigit():
        return await m.answer("❗️ Faqat raqamli ID yuboring.")
    await run("INSERT OR IGNORE INTO admins(id,added_at) VALUES(?,?)", (int(m.text.strip()), int(time.time())))
    await state.clear()
    await m.answer("✅ Admin qo'shildi.", reply_markup=back_kb("adm:admins"))


@admin.callback_query(AdmDelCb.filter())
async def cb_admdel(c: CallbackQuery, callback_data: AdmDelCb, state: FSMContext):
    if c.from_user.id not in OWNER_IDS:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await run("DELETE FROM admins WHERE id=?", (callback_data.id,))
    await c.answer("O'chirildi")
    await cb_admins(c, state)


# ───────── Sozlamalar ─────────
TEXT_SETTINGS = {
    "welcome": "📝 Salomlashuv matni",
    "instagram": "📸 Instagram havolasi",
    "footer": "🏷 Kino tagidagi imzo",
}


@admin.callback_query(F.data == "adm:settings")
async def cb_settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    protect = (await get_setting("protect")) == "1"
    kb = InlineKeyboardBuilder()
    for k, label in TEXT_SETTINGS.items():
        kb.button(text=label, callback_data=SetCb(key=k))
    kb.button(text=f"🔒 Kontent himoyasi: {'ON' if protect else 'OFF'}", callback_data="adm:protect")
    kb.button(text="⬅️ Orqaga", callback_data="adm:menu")
    kb.adjust(1)
    await show(c, "⚙️ <b>Sozlamalar</b>\n\n"
                  "🔒 Kontent himoyasi yoqilsa, kinoni boshqalarga forward qilib va saqlab bo'lmaydi.",
               kb.as_markup())


@admin.callback_query(F.data == "adm:protect")
async def cb_protect(c: CallbackQuery, state: FSMContext):
    cur = (await get_setting("protect")) == "1"
    await set_setting("protect", "0" if cur else "1")
    await cb_settings(c, state)


@admin.callback_query(SetCb.filter())
async def cb_setedit(c: CallbackQuery, callback_data: SetCb, state: FSMContext):
    key = callback_data.key
    cur = await get_setting(key)
    await state.set_state(SettingEdit.value)
    await state.update_data(key=key)
    extra = "\n\nSalomlashuvda <code>{name}</code> yozsangiz, foydalanuvchi ismi chiqadi." if key == "welcome" else ""
    await show(c, f"{TEXT_SETTINGS[key]}\n\nHozirgi qiymat:\n<code>{esc(cur or '—')}</code>\n\n"
                  f"Yangi qiymatni yuboring (tozalash uchun <code>-</code>).{extra}", cancel_kb())


@admin.message(SettingEdit.value, F.text)
async def setedit_save(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    val = m.text.strip()
    if val == "-":
        val = DEFAULTS[key] if key == "welcome" else ""
    if key == "instagram" and val and not val.startswith(("http://", "https://")):
        return await m.answer("❗️ Havola http:// yoki https:// bilan boshlansin.")
    await set_setting(key, val)
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=back_kb("adm:settings"))


# ───────────────────────────── MAIN ─────────────────────────────
async def main():
    if not BOT_TOKEN:
        hint = [f"BOT_TOKEN topilmadi.", f"Qidirilgan fayl: {ENV_PATH}"]
        if not ENV_PATH.is_file():
            hint.append("→ Bu joyda .env fayli YO'Q.")
            for alt in (BASE_DIR / ".env.txt", BASE_DIR / ".env.example"):
                if alt.is_file():
                    hint.append(f"→ Lekin «{alt.name}» bor. Faylni «.env» deb qayta nomlang (oxirida .txt bo'lmasin).")
        else:
            hint.append("→ Fayl bor, lekin ichida «BOT_TOKEN=...» qatori yo'q yoki bo'sh.")
            found = ", ".join(f"{k} ({n} belgi)" for k, n in ENV_FOUND.items()) or "hech narsa o'qilmadi"
            hint.append(f"→ Fayldan o'qilgan kalitlar: {found}")
            hint.append("→ Faylni Notepad'da Ctrl+S bilan SAQLAGANINGIZni tekshiring.")
        raise SystemExit("\n".join(hint))
    if not OWNER_IDS:
        raise SystemExit("ADMIN_IDS topilmadi. .env ga o'z Telegram ID'ingizni yozing.")
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)
    dp.include_router(user)
    await bot.set_my_commands([
        BotCommand(command="start", description="🎬 Botni ishga tushirish"),
        BotCommand(command="help", description="ℹ️ Yordam"),
        BotCommand(command="admin", description="🛠 Admin panel"),
    ])
    log.info("KINODAGILA ishga tushdi: @%s  (.env: %s)", await bot_username(bot), ENV_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
