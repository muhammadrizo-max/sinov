import io
import random
import re
from PIL import Image, ImageDraw, ImageFont

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import LEVEL_ACCESS, BOT_VERSION, BOT_AUTHOR, LANG_LABELS, LANG_FLAGS, SUPPORTED_LANGUAGES, IELTS_LANGUAGE

router = Router()

FOOTER = f"\n\n<i>v{BOT_VERSION} · {BOT_AUTHOR}</i>"

# ── Premium labels ─────────────────────────────────────────────────────────────
PREMIUM_LABELS = {
    "free": "🆓 Free",
    "basic": "🥈 Basic",
    "intermediate": "🥇 Intermediate",
    "ielts": "💎 IELTS",
}

# Which package unlocks which level
LEVEL_REQUIRED = {
    "A1": "free",
    "A2": "basic",
    "B1": "basic",
    "B2": "intermediate",
    "C1": "intermediate",
    "mixed": "intermediate",
    "C2": "ielts",
    "IELTS": "ielts",
}
PACKAGE_ORDER = ["free", "basic", "intermediate", "ielts"]


def has_access(user_premium: str, level: str) -> bool:
    return level in LEVEL_ACCESS.get(user_premium, [])


def _row_val(row, key: str, default=None):
    """aiosqlite.Row da .get() yo'q, shu sabab xavfsiz o'qish uchun yordamchi."""
    if row is None:
        return default
    try:
        val = row[key]
        return val if val is not None else default
    except (KeyError, IndexError):
        return default


async def get_effective_premium(u: dict | None, user_id: int) -> str:
    """Foydalanuvchining 'amaldagi' premium darajasini qaytaradi:
    1) Agar trial faol bo'lsa — to'liq huquq ('ielts') beriladi.
    2) Agar premium muddati tugagan bo'lsa — avtomatik 'free'ga tushiradi.
    3) Agar global Premium tizimi admin tomonidan OFF qilingan bo'lsa —
       hamma uchun eng yuqori daraja ('ielts') qaytariladi (premium talab qilinmaydi)."""
    if not await db.get_premium_mode_enabled():
        return "ielts"

    if _row_val(u, "trial_expires_at"):
        expired = await db.check_and_expire_trial(user_id)
        if not expired:
            return "ielts"
        u = await db.get_user(user_id)

    if _row_val(u, "premium", "free") != "free" and _row_val(u, "premium_expires_at"):
        expired = await db.check_and_expire_premium(user_id)
        if expired:
            u = await db.get_user(user_id)

    return _row_val(u, "premium", "free")


# ══════════════════════════════════════════════════════════════════════════════
#  States
# ══════════════════════════════════════════════════════════════════════════════

class WordsState(StatesGroup):
    browsing = State()


class TestState(StatesGroup):
    choosing_level = State()
    choosing_type  = State()
    in_test_choice = State()
    in_test_write  = State()


class SavedState(StatesGroup):
    browsing = State()


class PremiumState(StatesGroup):
    choosing_package = State()
    waiting_receipt  = State()


# ══════════════════════════════════════════════════════════════════════════════
#  Keyboards
# ══════════════════════════════════════════════════════════════════════════════

def main_menu_kb(premium: str = "free", lang: str = "en") -> ReplyKeyboardMarkup:
    flag = LANG_FLAGS.get(lang, "🌐")
    base = [
        [KeyboardButton(text="📚 Words"), KeyboardButton(text="🧪 Test")],
        [KeyboardButton(text="💎 Premium"), KeyboardButton(text="📊 Status")],
        [KeyboardButton(text="⏰ Eslatma"), KeyboardButton(text=f"{flag} Til")],
    ]
    if premium in ("basic", "intermediate", "ielts"):
        base[0].append(KeyboardButton(text="🔖 Saved"))
    if premium in ("intermediate", "ielts"):
        base.append([KeyboardButton(text="📘 IELTS Section")])
    if premium == "ielts":
        base[-1].append(KeyboardButton(text="🤖 AI Mentor"))
    return ReplyKeyboardMarkup(keyboard=base, resize_keyboard=True, persistent=True)


def level_inline_kb(prefix: str, user_premium: str, include_mixed: bool = False):
    """Show levels; locked ones display 🔒 and can't be used."""
    LEVELS_GRID = [
        ("A1", "A2"),
        ("B1", "B2"),
        ("C1", "C2"),
    ]
    b = InlineKeyboardBuilder()
    for pair in LEVELS_GRID:
        for lvl in pair:
            if has_access(user_premium, lvl):
                b.button(text=lvl, callback_data=f"{prefix}:{lvl}")
            else:
                req = LEVEL_REQUIRED.get(lvl, "basic")
                b.button(text=f"🔒 {lvl}", callback_data=f"locked:{req}")
    # IELTS row
    if has_access(user_premium, "IELTS"):
        b.button(text="IELTS", callback_data=f"{prefix}:IELTS")
    else:
        b.button(text="🔒 IELTS", callback_data="locked:ielts")
    # Mixed
    if include_mixed:
        if has_access(user_premium, "mixed"):
            b.button(text="🔀 Mixed", callback_data=f"{prefix}:mixed")
        else:
            b.button(text="🔒 Mixed", callback_data="locked:intermediate")
    b.button(text="🔙 Back", callback_data="u:back")
    b.adjust(2, 2, 2, 2 if include_mixed else 1, 1)
    return b.as_markup()


def word_nav_kb(word_id: int, level: str, idx: int, total: int, is_saved: bool):
    b = InlineKeyboardBuilder()
    b.button(text="➡️ Next", callback_data=f"wn:{level}:{idx+1}")
    b.button(text="🔊 Listen", callback_data=f"wa:{word_id}")
    save_text = "❌ Unsave" if is_saved else "🔖 Save"
    b.button(text=save_text, callback_data=f"wsv:{'rm' if is_saved else 'add'}:{word_id}:{level}:{idx}")
    b.button(text="⛔ Stop", callback_data="ws:stop")
    b.adjust(2, 2)
    return b.as_markup()


def test_type_kb(user_premium: str):
    b = InlineKeyboardBuilder()
    b.button(text="🇺🇿 Uzbek translation (3 options)", callback_data="tt:uz_translate")
    b.button(text="✍️ Write in English", callback_data="tt:en_write")
    b.button(text="📝 Fill in the blank", callback_data="tt:fill_blank")
    b.button(text="🔙 Back", callback_data="u:back")
    b.adjust(1)
    return b.as_markup()


def answer_kb(options, correct_idx, word_id, test_type, level):
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=opt, callback_data=f"ans:{int(i==correct_idx)}:{word_id}:{test_type}:{level}")
    b.button(text="⛔ Stop", callback_data="ts:stop")
    b.adjust(1)
    return b.as_markup()


def test_stop_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⛔ Stop", callback_data="ts:stop")
    b.adjust(1)
    return b.as_markup()


def saved_nav_kb(word_id: int, idx: int, total: int):
    b = InlineKeyboardBuilder()
    if idx > 0:
        b.button(text="⬅️ Prev", callback_data=f"svd:prev:{idx-1}")
    b.button(text="🔊 Listen", callback_data=f"wa:{word_id}")
    b.button(text="❌ Remove", callback_data=f"svd:rm:{word_id}:{idx}")
    if idx < total - 1:
        b.button(text="➡️ Next", callback_data=f"svd:next:{idx+1}")
    b.button(text="⛔ Close", callback_data="svd:stop")
    b.adjust(2, 2, 1)
    return b.as_markup()


def premium_packages_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🥈 Basic", callback_data="pkg:basic")
    b.button(text="🥇 Intermediate", callback_data="pkg:intermediate")
    b.button(text="💎 IELTS", callback_data="pkg:ielts")
    b.adjust(1)
    return b.as_markup()


def locked_kb():
    b = InlineKeyboardBuilder()
    b.button(text="💎 View Premium Plans", callback_data="u:show_premium")
    b.adjust(1)
    return b.as_markup()


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def fmt_word(word, current: int, total: int) -> str:
    return (
        f"📖 <b>{word['word']}</b>  —  <i>{word['translate']}</i>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>Example:</b>\n"
        f"   🔹 {word['example1_en']}\n"
        f"   🔸 {word['example1_uz']}\n\n"
        f"📌 <b>Type:</b> {word['tur']}\n"
        f"📁 <b>Group:</b> {word['guruh']}\n"
        f"🎯 <b>Level:</b> {word['daraja']}\n\n"
        f"<i>{current}/{total}</i>"
    )


async def make_audio(word_text: str) -> bytes:
    from gtts import gTTS
    tts = gTTS(text=word_text, lang="en")
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()


async def send_next_test(target, state: FSMContext):
    data = await state.get_data()
    level     = data["t_level"]
    test_type = data["t_type"]
    word_ids  = data["t_ids"]
    idx       = data.get("t_idx", 0)
    correct_n = data.get("t_correct", 0)
    wrong_n   = data.get("t_wrong", 0)
    user_id   = data["t_user"]
    lang      = data.get("t_lang", "en")

    is_cb   = isinstance(target, CallbackQuery)
    msg     = target.message if is_cb else target

    if idx >= len(word_ids):
        total = correct_n + wrong_n
        text = (
            f"✅ <b>Test completed!</b>\n\n"
            f"✅ Correct: <b>{correct_n}</b>\n"
            f"❌ Wrong: <b>{wrong_n}</b>\n"
            f"📊 Total: <b>{total}</b>\n"
            f"🎯 Score: <b>{round(correct_n/total*100) if total else 0}%</b>"
            + FOOTER
        )
        await state.clear()
        u = await db.get_user(user_id)
        lang = await db.get_user_language(user_id)
        premium = await get_effective_premium(u, user_id)
        if is_cb:
            await msg.edit_text(text, parse_mode="HTML")
        else:
            await msg.answer(text, parse_mode="HTML")
        await msg.answer("Main menu:", reply_markup=main_menu_kb(premium, lang))
        return

    word_id = word_ids[idx]
    word = await db.get_word_by_id(word_id)
    if not word:
        await state.update_data(t_idx=idx+1)
        await send_next_test(target, state)
        return

    await db.mark_word_learned(user_id, word_id)
    progress = f"{idx+1}/{len(word_ids)}"

    if test_type == "uz_translate":
        wrongs  = await db.get_random_wrong_options(word["translate"], 2, lang)
        options = [word["translate"]] + wrongs
        random.shuffle(options)
        cidx    = options.index(word["translate"])
        text    = (
            f"❓ <b>{word['word']}</b>\n\n"
            f"Find the Uzbek translation:\n"
            f"🎯 {word['daraja']}  |  {progress}"
        )
        kb = answer_kb(options, cidx, word_id, test_type, level)
        await state.set_state(TestState.in_test_choice)
        if is_cb:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")

    elif test_type == "en_write":
        text = (
            f"✍️ Write this word in <b>English</b>:\n\n"
            f"🇺🇿 <b>{word['translate']}</b>\n\n"
            f"🎯 {word['daraja']}  |  {progress}"
        )
        await state.update_data(t_cur_id=word_id, t_cur_word=word["word"])
        await state.set_state(TestState.in_test_write)
        if is_cb:
            await msg.edit_text(text, reply_markup=test_stop_kb(), parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=test_stop_kb(), parse_mode="HTML")

    elif test_type == "fill_blank":
        example = word["example1_en"] or ""
        blank   = example.replace(word["word"], "______", 1)
        text = (
            f"📝 Fill in the blank:\n\n"
            f"<i>{blank}</i>\n\n"
            f"🇺🇿 {word['example1_uz']}\n"
            f"🎯 {word['daraja']}  |  {progress}"
        )
        await state.update_data(t_cur_id=word_id, t_cur_word=word["word"])
        await state.set_state(TestState.in_test_write)
        if is_cb:
            await msg.edit_text(text, reply_markup=test_stop_kb(), parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=test_stop_kb(), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_new = await db.register_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    if await db.is_banned(message.from_user.id):
        await message.answer("🚫 You are banned. Contact admin.")
        return

    trial_notice = ""
    if is_new:
        trial_days = await db.get_trial_days()
        if trial_days > 0:
            granted = await db.grant_trial(message.from_user.id, trial_days)
            if granted:
                trial_notice = (
                    f"\n\n🎁 <b>Sizga {trial_days} kunlik bepul TRIAL berildi!</b>\n"
                    f"Shu muddat davomida botning barcha funksiyalaridan to'liq foydalanishingiz mumkin."
                )

    missing = await get_missing_channels(message.bot, message.from_user.id)
    if missing:
        await state.update_data(pending_trial_notice=trial_notice)
        await send_subscribe_prompt(message, missing)
        return

    await show_main_menu(message, trial_notice)


# ── Majburiy obuna (forced subscription) ───────────────────────────────────────

async def get_missing_channels(bot, user_id: int) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallar ro'yxatini qaytaradi."""
    channels = await db.get_channels()
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(int(ch["channel_id"]), user_id)
            if member.status in ("left", "kicked"):
                missing.append(dict(ch))
        except Exception:
            # Tekshirib bo'lmadi (masalan, bot kanalda admin emas) — xavfsizlik uchun
            # obuna bo'lmagan deb hisoblaymiz.
            missing.append(dict(ch))
    return missing


def subscribe_kb(channels: list):
    b = InlineKeyboardBuilder()
    for ch in channels:
        title = ch["title"] or ch["channel_id"]
        link = f"https://t.me/{ch['username']}" if ch.get("username") else ch.get("invite_link")
        if link:
            b.button(text=f"📡 {title}", url=link)
        else:
            b.button(text=f"📡 {title}", callback_data="noop")
    b.button(text="✅ Obunani tekshirish", callback_data="u:check_sub")
    b.adjust(1)
    return b.as_markup()


async def attach_invite_links(bot, channels: list):
    for ch in channels:
        if not ch.get("username") and not ch.get("invite_link"):
            try:
                ch["invite_link"] = await bot.export_chat_invite_link(int(ch["channel_id"]))
            except Exception:
                ch["invite_link"] = None
    return channels


async def send_subscribe_prompt(message: Message, channels: list):
    await attach_invite_links(message.bot, channels)
    await message.answer(
        "🔒 <b>Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:</b>\n\n"
        "Obuna bo'lgandan so'ng, pastdagi <b>\"✅ Obunani tekshirish\"</b> tugmasini bosing.",
        reply_markup=subscribe_kb(channels), parse_mode="HTML"
    )


@router.callback_query(F.data == "u:check_sub")
async def cb_check_subscription(cb: CallbackQuery, state: FSMContext):
    missing = await get_missing_channels(cb.bot, cb.from_user.id)
    if missing:
        await attach_invite_links(cb.bot, missing)
        await cb.answer(
            "❌ Siz hali barcha kanallarga obuna bo'lmadingiz. Iltimos, obuna bo'lib, qaytadan tekshiring.",
            show_alert=True
        )
        try:
            await cb.message.edit_reply_markup(reply_markup=subscribe_kb(missing))
        except Exception:
            pass
        return
    await cb.answer("✅ Rahmat! Endi botdan to'liq foydalanishingiz mumkin.", show_alert=True)
    data = await state.get_data()
    trial_notice = data.get("pending_trial_notice", "")
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await show_main_menu(cb.message, trial_notice, user_id=cb.from_user.id)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


async def show_main_menu(message: Message, trial_notice: str = "", user_id: int | None = None):
    uid = user_id or message.from_user.id
    u = await db.get_user(uid)
    lang = await db.get_user_language(uid)
    premium = await get_effective_premium(u, uid)
    await message.answer(
        f"👋 Welcome to <b>Lingua Go Words</b>!\n\n"
        f"📚 Learn English vocabulary step by step.\n"
        f"Your plan: <b>{PREMIUM_LABELS.get(premium, premium)}</b>\n"
        f"Choose a section below:"
        + trial_notice
        + FOOTER,
        reply_markup=main_menu_kb(premium, lang),
        parse_mode="HTML"
    )


# ── back ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "u:back")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(cb.from_user.id)
    lang = await db.get_user_language(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "👋 <b>Lingua Go Words</b>\n\nChoose a section:" + FOOTER,
        reply_markup=main_menu_kb(premium, lang),
        parse_mode="HTML"
    )


# ── locked level ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("locked:"))
async def cb_locked(cb: CallbackQuery):
    req = cb.data.split(":")[1]
    labels = {"basic": "🥈 Basic", "intermediate": "🥇 Intermediate", "ielts": "💎 IELTS"}
    await cb.answer(
        f"🔒 This requires {labels.get(req, req)} plan.\nTap 💎 Premium to upgrade!",
        show_alert=True
    )


@router.callback_query(F.data == "u:show_premium")
async def cb_show_premium(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    card = await db.get_card_settings()
    text = (
        f"💎 <b>Premium Plans</b>\n\n"
        f"🥈 <b>{card['basic_name']}</b> — {card['basic_price']}\n"
        f"   • A2 & B1 levels\n   • Save words feature\n\n"
        f"🥇 <b>{card['intermediate_name']}</b> — {card['intermediate_price']}\n"
        f"   • B2 & C1 levels\n   • Mixed tests\n   • IELTS Section\n\n"
        f"💎 <b>{card['ielts_name']}</b> — {card['ielts_price']}\n"
        f"   • All levels (C2 + IELTS)\n   • AI Mentor\n   • Full access"
        + FOOTER
    )
    await cb.message.answer(text, reply_markup=premium_packages_kb(), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  📚 Words
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text == "📚 Words")
async def btn_words(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
    await state.clear()
    u = await db.get_user(message.from_user.id)
    premium = await get_effective_premium(u, message.from_user.id)
    await message.answer(
        "📚 <b>Learn Words</b>\n\nChoose a level:",
        reply_markup=level_inline_kb("wl", premium),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("wl:"))
async def cb_words_level(cb: CallbackQuery, state: FSMContext):
    level = cb.data[3:]
    u = await db.get_user(cb.from_user.id)
    lang = await db.get_user_language(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    if not has_access(premium, level):
        await cb.answer("🔒 Upgrade your plan to access this level!", show_alert=True)
        return

    ids = await db.get_word_ids_by_level(level, lang)
    if not ids:
        await cb.answer("❌ No words in this level yet!", show_alert=True)
        return

    await state.update_data(w_level=level, w_ids=ids)
    await state.set_state(WordsState.browsing)

    word = await db.get_word_by_id(ids[0])
    await db.mark_word_learned(cb.from_user.id, ids[0])
    saved_ids = await db.get_saved_word_ids(cb.from_user.id)
    is_saved  = ids[0] in saved_ids

    await cb.message.edit_text(
        fmt_word(word, 1, len(ids)),
        reply_markup=word_nav_kb(ids[0], level, 0, len(ids), is_saved),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("wn:"))
async def cb_word_next(cb: CallbackQuery, state: FSMContext):
    _, level, idx_str = cb.data.split(":", 2)
    idx = int(idx_str)
    data = await state.get_data()
    ids: list = data.get("w_ids", [])

    if not ids or idx >= len(ids):
        await cb.answer("🎉 You've reviewed all words!", show_alert=True)
        await state.clear()
        u = await db.get_user(cb.from_user.id)
        lang = await db.get_user_language(cb.from_user.id)
        premium = await get_effective_premium(u, cb.from_user.id)
        await cb.message.edit_text("✅ <b>All words reviewed!</b>", parse_mode="HTML")
        await cb.message.answer("Main menu:", reply_markup=main_menu_kb(premium, lang))
        return

    word = await db.get_word_by_id(ids[idx])
    await db.mark_word_learned(cb.from_user.id, ids[idx])
    saved_ids = await db.get_saved_word_ids(cb.from_user.id)
    is_saved  = ids[idx] in saved_ids

    await cb.message.edit_text(
        fmt_word(word, idx+1, len(ids)),
        reply_markup=word_nav_kb(ids[idx], level, idx, len(ids), is_saved),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("wsv:"))
async def cb_word_save(cb: CallbackQuery, state: FSMContext):
    parts    = cb.data.split(":")
    action   = parts[1]
    word_id  = int(parts[2])
    level    = parts[3]
    idx      = int(parts[4])
    data     = await state.get_data()
    ids      = data.get("w_ids", [])

    u = await db.get_user(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    if premium not in ("basic", "intermediate", "ielts"):
        await cb.answer("🔒 Save feature requires Basic plan or higher!", show_alert=True)
        return

    if action == "add":
        await db.save_word(cb.from_user.id, word_id)
        await cb.answer("🔖 Word saved!", show_alert=False)
        is_saved = True
    else:
        await db.unsave_word(cb.from_user.id, word_id)
        await cb.answer("❌ Removed from saved!", show_alert=False)
        is_saved = False

    word = await db.get_word_by_id(word_id)
    await cb.message.edit_text(
        fmt_word(word, idx+1, len(ids)),
        reply_markup=word_nav_kb(word_id, level, idx, len(ids), is_saved),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("wa:"))
async def cb_word_audio(cb: CallbackQuery):
    word_id = int(cb.data[3:])
    word = await db.get_word_by_id(word_id)
    if not word:
        await cb.answer("Word not found!", show_alert=True)
        return
    await cb.answer("🔊 Generating audio...")
    try:
        audio = await make_audio(word["word"])
        af = BufferedInputFile(audio, filename=f"{word['word']}.mp3")
        await cb.message.answer_audio(
            af,
            caption=f"🔊 <b>{word['word']}</b>  —  <i>{word['translate']}</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.answer(f"❌ Audio error: {e}")


@router.callback_query(F.data == "ws:stop")
async def cb_word_stop(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(cb.from_user.id)
    lang = await db.get_user_language(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "👋 <b>Lingua Go Words</b>\n\nChoose a section:" + FOOTER,
        reply_markup=main_menu_kb(premium, lang),
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  🧪 Test
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text == "🧪 Test")
async def btn_tests(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
    await state.clear()
    u = await db.get_user(message.from_user.id)
    premium = await get_effective_premium(u, message.from_user.id)
    await state.set_state(TestState.choosing_level)
    await message.answer(
        "🧪 <b>Test</b>\n\nChoose a level:",
        reply_markup=level_inline_kb("tl", premium, include_mixed=True),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("tl:"))
async def cb_test_level(cb: CallbackQuery, state: FSMContext):
    level = cb.data[3:]
    u = await db.get_user(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    if not has_access(premium, level):
        await cb.answer("🔒 Upgrade your plan to access this level!", show_alert=True)
        return
    await state.update_data(t_level=level, t_user=cb.from_user.id)
    await state.set_state(TestState.choosing_type)
    await cb.message.edit_text(
        f"🧪 <b>Test</b>  —  Level: <b>{level}</b>\n\nChoose test type:",
        reply_markup=test_type_kb(premium),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("tt:"))
async def cb_test_type(cb: CallbackQuery, state: FSMContext):
    test_type = cb.data[3:]
    data = await state.get_data()
    level = data.get("t_level", "A1")
    lang  = await db.get_user_language(cb.from_user.id)
    ids = await db.get_word_ids_by_level(level, lang)
    if not ids:
        await cb.answer("❌ No words in this level!", show_alert=True)
        return
    await state.update_data(t_type=test_type, t_ids=ids, t_idx=0, t_correct=0, t_wrong=0, t_lang=lang)
    await send_next_test(cb, state)


@router.callback_query(F.data.startswith("ans:"))
async def cb_answer(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    is_correct = parts[1] == "1"
    word_id    = int(parts[2])
    test_type  = parts[3]

    data      = await state.get_data()
    correct_n = data.get("t_correct", 0)
    wrong_n   = data.get("t_wrong", 0)
    idx       = data.get("t_idx", 0)

    await db.save_test_result(cb.from_user.id, word_id, is_correct, test_type)

    if is_correct:
        correct_n += 1
        await cb.answer("✅ Correct!", show_alert=False)
    else:
        wrong_n += 1
        word = await db.get_word_by_id(word_id)
        await cb.answer(f"❌ Wrong! Correct: {word['translate']}", show_alert=True)

    await state.update_data(t_idx=idx+1, t_correct=correct_n, t_wrong=wrong_n)
    await send_next_test(cb, state)


@router.message(TestState.in_test_write)
async def handle_write_answer(message: Message, state: FSMContext):
    data         = await state.get_data()
    correct_word = data.get("t_cur_word", "")
    cur_id       = data.get("t_cur_id")
    test_type    = data.get("t_type")
    correct_n    = data.get("t_correct", 0)
    wrong_n      = data.get("t_wrong", 0)
    idx          = data.get("t_idx", 0)

    user_ans   = message.text.strip().lower()
    is_correct = user_ans == correct_word.lower()

    await db.save_test_result(message.from_user.id, cur_id, is_correct, test_type)

    if is_correct:
        correct_n += 1
        await message.answer(f"✅ <b>Correct!</b>  {correct_word}", parse_mode="HTML")
    else:
        wrong_n += 1
        await message.answer(
            f"❌ <b>Wrong!</b>\n"
            f"Your answer: <i>{user_ans}</i>\n"
            f"Correct: <b>{correct_word}</b>",
            parse_mode="HTML"
        )

    await state.update_data(t_idx=idx+1, t_correct=correct_n, t_wrong=wrong_n)
    await send_next_test(message, state)


@router.callback_query(F.data == "ts:stop")
async def cb_test_stop(cb: CallbackQuery, state: FSMContext):
    data      = await state.get_data()
    correct_n = data.get("t_correct", 0)
    wrong_n   = data.get("t_wrong", 0)
    total     = correct_n + wrong_n
    user_id   = data.get("t_user", cb.from_user.id)
    await state.clear()
    u = await db.get_user(user_id)
    lang = await db.get_user_language(user_id)
    premium = await get_effective_premium(u, user_id)
    await cb.message.edit_text(
        f"⛔ <b>Test stopped</b>\n\n"
        f"✅ Correct: <b>{correct_n}</b>\n"
        f"❌ Wrong: <b>{wrong_n}</b>\n"
        f"📊 Total: <b>{total}</b>\n"
        f"🎯 Score: <b>{round(correct_n/total*100) if total else 0}%</b>"
        + FOOTER,
        parse_mode="HTML"
    )
    await cb.message.answer("Main menu:", reply_markup=main_menu_kb(premium, lang))


# ══════════════════════════════════════════════════════════════════════════════
#  🔖 Saved
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text == "🔖 Saved")
async def btn_saved(message: Message, state: FSMContext):
    u = await db.get_user(message.from_user.id)
    premium = await get_effective_premium(u, message.from_user.id)
    if premium not in ("basic", "intermediate", "ielts"):
        await message.answer("🔒 <b>Saved Words</b> requires Basic plan or higher!\n\nTap 💎 Premium to upgrade.", parse_mode="HTML")
        return
    await state.clear()
    ids = await db.get_saved_word_ids(message.from_user.id)
    if not ids:
        await message.answer("📭 You have no saved words yet.\n\nTap 🔖 Save while browsing words!", parse_mode="HTML")
        return
    await state.update_data(sv_ids=ids)
    await state.set_state(SavedState.browsing)
    word = await db.get_word_by_id(ids[0])
    await message.answer(
        fmt_word(word, 1, len(ids)),
        reply_markup=saved_nav_kb(ids[0], 0, len(ids)),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("svd:"))
async def cb_saved_nav(cb: CallbackQuery, state: FSMContext):
    parts  = cb.data.split(":")
    action = parts[1]
    data   = await state.get_data()
    ids    = data.get("sv_ids", [])

    if action in ("next", "prev"):
        idx  = int(parts[2])
        word = await db.get_word_by_id(ids[idx])
        await cb.message.edit_text(
            fmt_word(word, idx+1, len(ids)),
            reply_markup=saved_nav_kb(ids[idx], idx, len(ids)),
            parse_mode="HTML"
        )
    elif action == "rm":
        word_id = int(parts[2])
        idx     = int(parts[3])
        await db.unsave_word(cb.from_user.id, word_id)
        await cb.answer("❌ Removed!", show_alert=False)
        ids = await db.get_saved_word_ids(cb.from_user.id)
        if not ids:
            await state.clear()
            await cb.message.edit_text("📭 No saved words left.")
            return
        new_idx = min(idx, len(ids)-1)
        await state.update_data(sv_ids=ids)
        word = await db.get_word_by_id(ids[new_idx])
        await cb.message.edit_text(
            fmt_word(word, new_idx+1, len(ids)),
            reply_markup=saved_nav_kb(ids[new_idx], new_idx, len(ids)),
            parse_mode="HTML"
        )
    elif action == "stop":
        await state.clear()
        u = await db.get_user(cb.from_user.id)
        lang = await db.get_user_language(cb.from_user.id)
        premium = await get_effective_premium(u, cb.from_user.id)
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer("Main menu:", reply_markup=main_menu_kb(premium, lang))


# ══════════════════════════════════════════════════════════════════════════════
#  📊 Status
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text == "📊 Status")
async def btn_status(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
        
    user_id = message.from_user.id  # Shuni qo'shdik, endi xato bermaydi
    u = await db.get_user(user_id)
    lang = await db.get_user_language(user_id)
    premium = await get_effective_premium(u, user_id)
    u = await db.get_user(user_id)  
    stats = await db.get_user_stats(user_id)

    expiry_line = ""
    if _row_val(u, "trial_expires_at"):
        expiry_line = f"\n🎁 Trial tugaydi: <b>{str(u['trial_expires_at'])[:16]}</b>"
    elif _row_val(u, "premium_expires_at") and _row_val(u, "premium", "free") != "free":
        expiry_line = f"\n⏳ Premium tugaydi: <b>{str(u['premium_expires_at'])[:16]}</b>"

    # HTML formatiga moslab qalinlashtirish (<b>) va kod formatiga (<code>) o'tkazildi
    await message.answer(
        f"📊 <b>Your Status</b>\n\n"
        f"🆔 <b>Foydalanuvchi ID:</b> <code>{user_id}</code>\n"
        f"💎 Plan: <b>{PREMIUM_LABELS.get(premium, premium)}</b>{expiry_line}\n\n"
        f"📖 Words learned: <b>{stats['learned']}</b>\n"
        f"🔖 Saved words: <b>{stats['saved']}</b>\n"
        f"🧪 Total tests: <b>{stats['total_tests']}</b>\n"
        f"✅ Correct answers: <b>{stats['correct']}</b>\n"
        f"🎯 Accuracy: <b>{stats['accuracy']}%</b>\n"
        f"🔥 Streak words (3+): <b>{stats['streak_words']}</b>\n"
        f"⭐ Best streak: <b>{stats['max_streak']}</b>"
        + FOOTER,
        parse_mode="HTML",
        reply_markup=main_menu_kb(premium, lang)
    )
    

# ══════════════════════════════════════════════════════════════════════════════
#  💎 Premium
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text == "💎 Premium")
async def btn_premium(message: Message, state: FSMContext):
    await state.clear()
    card = await db.get_card_settings()
    u = await db.get_user(message.from_user.id)
    premium = await get_effective_premium(u, message.from_user.id)

    text = (
        f"💎 <b>Premium Plans</b>\n\n"
        f"Your current plan: <b>{PREMIUM_LABELS.get(premium, premium)}</b>\n\n"
        f"🥈 <b>{card['basic_name']}</b> — {card['basic_price']}\n"
        f"   • Unlocks A2 & B1 levels\n"
        f"   • Save words feature\n\n"
        f"🥇 <b>{card['intermediate_name']}</b> — {card['intermediate_price']}\n"
        f"   • Unlocks B2 & C1 levels\n"
        f"   • Mixed tests (A1–C2)\n"
        f"   • IELTS Section\n\n"
        f"💎 <b>{card['ielts_name']}</b> — {card['ielts_price']}\n"
        f"   • All levels (C2 + IELTS)\n"
        f"   • AI Mentor\n"
        f"   • Full access"
        + FOOTER
    )
    await message.answer(text, reply_markup=premium_packages_kb(), parse_mode="HTML")


@router.callback_query(F.data.startswith("pkg:"))
async def cb_package_detail(cb: CallbackQuery, state: FSMContext):
    pkg  = cb.data[4:]
    card = await db.get_card_settings()

    pkg_info = {
        "basic": {
            "name": card["basic_name"],
            "price": card["basic_price"],
            "features": "✅ A2 & B1 levels\n✅ Save words",
        },
        "intermediate": {
            "name": card["intermediate_name"],
            "price": card["intermediate_price"],
            "features": "✅ B2 & C1 levels\n✅ Mixed tests\n✅ IELTS Section",
        },
        "ielts": {
            "name": card["ielts_name"],
            "price": card["ielts_price"],
            "features": "✅ All levels (C2 + IELTS)\n✅ AI Mentor\n✅ Full access",
        },
    }
    info = pkg_info.get(pkg, {})
    text = (
        f"💳 <b>{info['name']}</b> — {info['price']}\n\n"
        f"{info['features']}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Card: <code>{card['card_number']}</code>\n"
        f"👤 Owner: <b>{card['card_owner']}</b>\n\n"
        f"Please transfer the payment and send us the <b>receipt screenshot</b>."
        + FOOTER
    )
    b = InlineKeyboardBuilder()
    b.button(text="📤 Send Receipt", callback_data=f"pay:receipt:{pkg}")
    b.button(text="🔙 Back", callback_data="pay:back")
    b.adjust(1)
    await cb.message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "pay:back")
async def cb_pay_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay:receipt:"))
async def cb_pay_receipt(cb: CallbackQuery, state: FSMContext):
    pkg = cb.data.split(":")[2]
    await state.update_data(pay_pkg=pkg)
    await state.set_state(PremiumState.waiting_receipt)
    await cb.message.answer(
        f"📸 Please send your <b>payment receipt</b> (photo/screenshot)\n"
        f"Package: <b>{pkg.capitalize()}</b>\n\n"
        f"<i>Only image files accepted.</i>",
        parse_mode="HTML"
    )


@router.message(PremiumState.waiting_receipt, F.photo)
async def handle_receipt(message: Message, state: FSMContext):
    data    = await state.get_data()
    pkg     = data.get("pay_pkg", "basic")
    file_id = message.photo[-1].file_id

    pay_id = await db.create_payment(message.from_user.id, pkg, file_id)
    await state.clear()

    await message.answer(
        f"✅ <b>Receipt received!</b>\n\n"
        f"Your payment has been submitted for review.\n"
        f"Please wait for admin confirmation.\n\n"
        f"Payment ID: <code>#{pay_id}</code>" + FOOTER,
        parse_mode="HTML"
    )

    # Notify all admins
    from config import ADMIN_IDS
    u = await db.get_user(message.from_user.id)
    uname = f"@{u['username']}" if u and u["username"] else str(message.from_user.id)
    notif = (
        f"💳 <b>New Payment Request!</b>\n\n"
        f"👤 User: {uname} (<code>{message.from_user.id}</code>)\n"
        f"📦 Package: <b>{pkg.capitalize()}</b>\n"
        f"🆔 Payment ID: <code>#{pay_id}</code>\n\n"
        f"Check admin panel → 💳 Premium → ⏳ Pending"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_photo(
                admin_id, file_id,
                caption=notif, parse_mode="HTML"
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  ⏰ Eslatma (Reminder)
# ══════════════════════════════════════════════════════════════════════════════

REMINDER_MESSAGES = [
    "📚 Bugun hali so'z o'rganmadingiz! Bir necha daqiqa ajratsangiz bo'ldi.",
    "🔥 Bilim — kichik qadamlardan yig'iladi. Hoziroq bir nechta so'z o'rganib qo'ying!",
    "⏰ Vaqt keldi! Ingliz tilingizni rivojlantirish uchun mukammal payt.",
    "🌟 Har kuni ozgina — yiliga ko'p! Keling, davom etamiz.",
    "💪 Siz buni boshlagansiz — endi davom ettirish vaqti keldi!",
    "🧠 Miyangiz yangi so'zlarni kutmoqda. Kelinglar, ishni boshlaylik!",
]

REMINDER_COLORS = [
    ((91, 134, 229), (255, 255, 255)),   # ko'k fon, oq matn
    ((46, 184, 138), (255, 255, 255)),   # yashil fon
    ((230, 126, 70), (255, 255, 255)),   # to'q sariq fon
    ((142, 84, 212), (255, 255, 255)),   # siyohrang fon
]


EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE
)


def _strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text).strip()


def generate_reminder_image(text: str) -> BufferedInputFile:
    """Eslatma uchun chiroyli rasm-karta generatsiya qiladi (tashqi internetga bog'liq emas)."""
    width, height = 800, 450
    bg, fg = random.choice(REMINDER_COLORS)
    img = Image.new("RGB", (width, height), color=bg)
    draw = ImageDraw.Draw(img)

    # Yumshoq dekorativ doira (fon naqshi)
    draw.ellipse([width - 220, -80, width + 80, 220], fill=tuple(min(c + 25, 255) for c in bg))
    draw.ellipse([-100, height - 180, 180, height + 100], fill=tuple(max(c - 20, 0) for c in bg))

    def load_font(size, bold=False):
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for p in paths:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return ImageFont.load_default()

    title_font = load_font(34, bold=True)
    body_font  = load_font(26)

    # Kichik kvadrat-dekor (kitob belgisi o'rnida, emoji shrift yo'qligi uchun)
    draw.rounded_rectangle([50, 48, 86, 84], radius=8, fill=fg)
    draw.rectangle([58, 58, 78, 74], fill=bg)
    draw.text((100, 50), "LinguaGo Words", font=title_font, fill=fg)

    clean_text = _strip_emoji(text)
    # Matnni qatorlarga bo'lib markazga joylashtirish
    words = clean_text.split(" ")
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=body_font)
        if bbox[2] - bbox[0] > width - 100:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)

    y = height // 2 - (len(lines) * 36) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=body_font)
        x = (width - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=body_font, fill=fg)
        y += 42

    footer_font = load_font(18)
    draw.text((50, height - 45), "@LinguaGoWordsBot · VisionCore Group", font=footer_font, fill=fg)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename="reminder.png")


def reminder_settings_kb(enabled: bool, interval: int):
    b = InlineKeyboardBuilder()
    toggle_text = "✅ Eslatma: YOQILGAN" if enabled else "🚫 Eslatma: O'CHIRILGAN"
    b.button(text=toggle_text, callback_data="rem:toggle")
    for h in db.REMINDER_INTERVAL_CHOICES:
        per_day = round(24 / h, 1)
        label = f"⏱ Har {h} soatda (kuniga ~{per_day:g} marta)"
        if h == interval:
            label = f"• {label} •"
        b.button(text=label, callback_data=f"rem:int:{h}")
    b.adjust(1)
    return b.as_markup()


@router.message(F.text == "⏰ Eslatma")
async def btn_reminder(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
    enabled, interval = await db.get_reminder_settings(message.from_user.id)
    await message.answer(
        f"⏰ <b>Eslatma sozlamalari</b>\n\n"
        f"Bot sizga belgilangan oraliqda so'z o'rganishni eslatib turadi.\n"
        f"Hozirgi oraliq: <b>har kuni soat 18:00 da</b>.\n\n"
        f"O'zgartirish uchun tanlang:",
        reply_markup=reminder_settings_kb(enabled, interval), parse_mode="HTML"
    )


@router.callback_query(F.data == "rem:toggle")
async def cb_reminder_toggle(cb: CallbackQuery):
    enabled, interval = await db.get_reminder_settings(cb.from_user.id)
    new_state = not enabled
    await db.set_reminder_enabled(cb.from_user.id, new_state)
    await cb.answer("✅ Eslatma yoqildi!" if new_state else "🚫 Eslatma o'chirildi!", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=reminder_settings_kb(new_state, interval))
    except Exception:
        pass


@router.callback_query(F.data.startswith("rem:int:"))
async def cb_reminder_interval(cb: CallbackQuery):
    hours = int(cb.data.split(":")[2])
    await db.set_reminder_interval(cb.from_user.id, hours)
    enabled, _ = await db.get_reminder_settings(cb.from_user.id)
    await cb.answer(f"✅ Endi har {hours} soatda eslatiladi!", show_alert=True)
    try:
        await cb.message.edit_text(
            f"⏰ <b>Eslatma sozlamalari</b>\n\n"
            f"Bot sizga belgilangan oraliqda so'z o'rganishni eslatib turadi.\n"
            f"Hozirgi oraliq: <b>har {hours} soatda</b>.\n\n"
            f"O'zgartirish uchun tanlang:",
            reply_markup=reminder_settings_kb(enabled, hours), parse_mode="HTML"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  🌐 Til tanlash
# ══════════════════════════════════════════════════════════════════════════════

def lang_select_kb():
    b = InlineKeyboardBuilder()
    for code, label in LANG_LABELS.items():
        b.button(text=label, callback_data=f"lang:{code}")
    b.adjust(1)
    return b.as_markup()


@router.message(F.text.regexp(r"^[🇬🇧🇷🇺🇰🇷].* Til$"))
async def btn_lang(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
    lang = await db.get_user_language(message.from_user.id)
    current = LANG_LABELS.get(lang, lang)
    await message.answer(
        f"🌐 <b>Til tanlash</b>\n\nHozirgi til: <b>{current}</b>\n\nYangi tilni tanlang:",
        reply_markup=lang_select_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(cb: CallbackQuery, state: FSMContext):
    lang = cb.data[5:]
    if lang not in SUPPORTED_LANGUAGES:
        await cb.answer("❌ Noto'g'ri til!", show_alert=True)
        return
    await db.set_user_language(cb.from_user.id, lang)
    label = LANG_LABELS.get(lang, lang)
    await cb.answer(f"✅ Til o'zgartirildi: {label}", show_alert=True)
    u = await db.get_user(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        f"✅ <b>Til o'zgartirildi: {label}</b>\n\nEndi so'zlar shu tilda ko'rsatiladi.",
        reply_markup=main_menu_kb(premium, lang), parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  🌐 Til tanlash
# ══════════════════════════════════════════════════════════════════════════════

def lang_select_kb():
    b = InlineKeyboardBuilder()
    for code, label in LANG_LABELS.items():
        b.button(text=label, callback_data=f"lang:{code}")
    b.adjust(1)
    return b.as_markup()


@router.message(F.text.regexp(r"^[🇬🇧🇷🇺🇰🇷].* Til$"))
async def btn_lang(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        return
    lang = await db.get_user_language(message.from_user.id)
    current = LANG_LABELS.get(lang, lang)
    await message.answer(
        f"🌐 <b>Til tanlash</b>\n\nHozirgi til: <b>{current}</b>\n\nYangi tilni tanlang:",
        reply_markup=lang_select_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(cb: CallbackQuery, state: FSMContext):
    lang = cb.data[5:]
    if lang not in SUPPORTED_LANGUAGES:
        await cb.answer("❌ Noto'g'ri til!", show_alert=True)
        return
    await db.set_user_language(cb.from_user.id, lang)
    label = LANG_LABELS.get(lang, lang)
    await cb.answer(f"✅ Til o'zgartirildi: {label}", show_alert=True)
    u = await db.get_user(cb.from_user.id)
    premium = await get_effective_premium(u, cb.from_user.id)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        f"✅ <b>Til o'zgartirildi: {label}</b>\n\nEndi so'zlar shu tilda ko'rsatiladi.",
        reply_markup=main_menu_kb(premium, lang), parse_mode="HTML"
    )
