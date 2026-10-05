import asyncio
import csv
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import closing

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ======================================================================
# SOZLAMALAR  (maxfiy qiymatlar Render -> Environment bo'limida turadi)
# ======================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment o'zgaruvchisi topilmadi!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8208777595"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@testbotschane")

# MUHIM: Render'da bu yo'l Persistent Disk ichida bo'lishi shart (masalan /var/data/bot_database.db),
# aks holda har deployda baza o'chib ketadi.
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

QUIZ_TIME_LIMIT = 3600  # soniyalarda (60 daqiqa)

AVAILABLE_SUBJECTS = [
    "Huquq", "Tarix", "Ona tili", "Ingliz tili",
    "Matematika", "SAT English", "SAT Math",
]
CATEGORIES = [f"{i}-sinf" for i in range(1, 12)] + ["Talaba"]

OPTION_PREFIX = re.compile(r"^[A-Da-d][\)\.]\s*")
ANSWER_RE = re.compile(r"javob\s*[:\-]?\s*([A-D])", re.IGNORECASE)


def esc(value) -> str:
    """Foydalanuvchi matnini HTML uchun xavfsiz qiladi."""
    return html.escape(str(value), quote=False)


def fmt_time(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


# ======================================================================
# BAZA
# ======================================================================
def _run(sql: str, params=(), fetch=None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(sql, params)
        result = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
        conn.commit()
        return result


def db_exec(sql, params=()):
    _run(sql, params)


def db_one(sql, params=()):
    return _run(sql, params, "one")


def db_all(sql, params=()):
    return _run(sql, params, "all")


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT, phone TEXT, category TEXT,
                subject1 TEXT, subject2 TEXT
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT, question_text TEXT,
                option_a TEXT, option_b TEXT, option_c TEXT, option_d TEXT,
                correct_option TEXT
            );
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, subject TEXT,
                score INTEGER, total_questions INTEGER, percentage REAL,
                time_spent INTEGER, user_answers TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            INSERT OR IGNORE INTO settings (key, value) VALUES ('test_status', 'off');
        """)
        conn.commit()


def is_test_active() -> bool:
    row = db_one("SELECT value FROM settings WHERE key = 'test_status'")
    return bool(row) and row[0] == "on"


def set_test_status(status: str):
    db_exec("UPDATE settings SET value = ? WHERE key = 'test_status'", (status,))


# ======================================================================
# KLAVIATURALAR
# ======================================================================
def reply_kb(items, per_row=2, one_time=True) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=t) for t in items[i:i + per_row]]
        for i in range(0, len(items), per_row)
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=one_time)


phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def get_sub_keyboard() -> InlineKeyboardMarkup:
    url = f"https://t.me/{CHANNEL_ID.lstrip('@')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=url)],
        [InlineKeyboardButton(text="✅ A'zo bo'ldim", callback_data="check_sub")],
    ])


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    toggle = "🔴 Testni o'chirish" if is_test_active() else "🟢 Testni yoqish"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=toggle), KeyboardButton(text="➕ Savol qo'shish")],
            [KeyboardButton(text="🗑 Savol o'chirish"), KeyboardButton(text="📄 O'quvchilar ro'yxati")],
            [KeyboardButton(text="🏆 Natijalar (Admin)"), KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📊 Statistikani ko'rish"), KeyboardButton(text="💾 Zaxira (DB)")],
        ],
        resize_keyboard=True,
    )


# ======================================================================
# YORDAMCHI FUNKSIYALAR
# ======================================================================
async def check_subscription(user_id: int, bot: Bot) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception as e:
        # Odatda bot kanalda admin bo'lmasa shu xato chiqadi
        logging.warning("Obunani tekshirib bo'lmadi: %s", e)
        return False


async def send_csv(message: Message, filename: str, header: list, rows: list, caption: str):
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    try:
        await message.answer_document(FSInputFile(filename), caption=caption)
    finally:
        os.remove(filename)


def chunk_text(parts: list, limit: int = 4000) -> list:
    """Matn bo'laklarini Telegram limitiga (4096) sig'adigan xabarlarga yig'adi."""
    chunks, current = [], ""
    for part in parts:
        if current and len(current) + len(part) > limit:
            chunks.append(current)
            current = ""
        current += part
    if current:
        chunks.append(current)
    return chunks


# ======================================================================
# FSM HOLATLARI
# ======================================================================
class Registration(StatesGroup):
    full_name = State()
    phone = State()
    category = State()
    subject1 = State()
    subject2 = State()


class AddQuestion(StatesGroup):
    subject = State()
    raw_data = State()


class Broadcast(StatesGroup):
    message = State()


class Quiz(StatesGroup):
    subject = State()
    solving = State()


try:  # aiogram 3.7+
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
except ImportError:  # eski aiogram 3.x
    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")

dp = Dispatcher(storage=MemoryStorage())

# Admin handlerlari alohida routerda: har birida ADMIN_ID tekshirish shart emas
admin_router = Router()
admin_router.message.filter(F.from_user.id == ADMIN_ID)
user_router = Router()


# ======================================================================
# ADMIN
# ======================================================================
@admin_router.message(Command("admin", "cancel"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Admin panelga xush kelibsiz!", reply_markup=get_admin_keyboard())


@admin_router.message(F.text.in_(["🟢 Testni yoqish", "🔴 Testni o'chirish"]))
async def toggle_test(message: Message):
    new_status = "on" if message.text.startswith("🟢") else "off"
    set_test_status(new_status)
    if new_status == "on":
        text = "🟢 <b>Test tizimi YOQILDI!</b> Faqat savollar mavjud bo'lgan fanlar o'quvchilarga ko'rinadi."
    else:
        text = "🔴 <b>Test tizimi O'CHIRILDI!</b>"
    await message.answer(text, reply_markup=get_admin_keyboard())


# --- Savol qo'shish ---
@admin_router.message(F.text == "➕ Savol qo'shish")
async def add_q_start(message: Message, state: FSMContext):
    await message.answer("Qaysi fan uchun savol qo'shmoqchisiz?", reply_markup=reply_kb(AVAILABLE_SUBJECTS))
    await state.set_state(AddQuestion.subject)


@admin_router.message(AddQuestion.subject)
async def add_q_subject(message: Message, state: FSMContext):
    if message.text not in AVAILABLE_SUBJECTS:
        await message.answer("Iltimos, mavjud fanlardan birini tanlang.")
        return
    await state.update_data(subject=message.text)
    await state.set_state(AddQuestion.raw_data)
    await message.answer(
        "Savol va variantlarni quyidagi formatda 1 ta xabarda yuboring:\n\n"
        "Savol matni\nA) Variant 1\nB) Variant 2\nC) Variant 3\nD) Variant 4\nJavob: A\n\n"
        "Bekor qilish: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@admin_router.message(AddQuestion.raw_data)
async def add_q_process(message: Message, state: FSMContext):
    lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if len(lines) < 6:
        await message.answer("❌ Format noto'g'ri! Kamida 6 ta qator kerak (Savol, 4 ta variant, Javob).")
        return

    # Oxirgi 5 qator = 4 variant + javob; qolgani (bir necha qator bo'lishi mumkin) = savol matni
    *question_lines, a, b, c, d, answer_line = lines
    match = ANSWER_RE.search(answer_line)
    if not match:
        await message.answer("❌ To'g'ri javob topilmadi! Oxirgi qator 'Javob: A' ko'rinishida bo'lishi kerak.")
        return

    options = [OPTION_PREFIX.sub("", x) for x in (a, b, c, d)]
    data = await state.get_data()
    db_exec(
        "INSERT INTO questions (subject, question_text, option_a, option_b, option_c, option_d, correct_option) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (data["subject"], "\n".join(question_lines), *options, match.group(1).upper()),
    )
    await message.answer("✅ Savol muvaffaqiyatli saqlandi!", reply_markup=get_admin_keyboard())
    await state.clear()


# --- Savol o'chirish ---
@admin_router.message(F.text == "🗑 Savol o'chirish")
async def delete_q_start(message: Message):
    questions = db_all("SELECT id, subject, question_text FROM questions ORDER BY id DESC LIMIT 10")
    if not questions:
        await message.answer("O'chirish uchun savollar topilmadi.")
        return
    text = "O'chirmoqchi bo'lgan savolni tanlang (/del_ID):\n\n"
    for q_id, subject, q_text in questions:
        text += f"🆔 /del_{q_id} | [{esc(subject)}] {esc(q_text[:30])}...\n"
    await message.answer(text)


@admin_router.message(F.text.regexp(r"^/del_(\d+)$").as_("m"))
async def delete_q_confirm(message: Message, m: re.Match):
    q_id = int(m.group(1))
    db_exec("DELETE FROM questions WHERE id = ?", (q_id,))
    await message.answer(f"✅ ID: {q_id} bo'lgan savol o'chirildi.")


# --- Hisobotlar ---
@admin_router.message(F.text == "🏆 Natijalar (Admin)")
async def admin_results(message: Message):
    rows = db_all("""
        SELECT u.full_name, u.phone, r.subject, r.score, r.total_questions, r.percentage, r.time_spent
        FROM results r JOIN users u ON r.user_id = u.user_id
        ORDER BY r.percentage DESC, r.time_spent ASC
    """)
    if not rows:
        await message.answer("Hozircha test ishlaganlar yo'q.")
        return
    await send_csv(
        message, "test_natijalari.csv",
        ["F.I.SH", "Telefon", "Fan", "To'g'ri javob", "Jami savol", "Foiz (%)", "Sarflangan vaqt (sek)"],
        rows, f"🏆 Jami ishlangan testlar: {len(rows)} ta",
    )


@admin_router.message(F.text == "📄 O'quvchilar ro'yxati")
async def export_users(message: Message):
    rows = db_all("SELECT user_id, full_name, phone, category, subject1, subject2 FROM users")
    if not rows:
        await message.answer("Hozircha hech kim ro'yxatdan o'tmagan.")
        return
    await send_csv(
        message, "oquvchilar_royxati.csv",
        ["Telegram ID", "Ism Familiya", "Telefon", "Toifa", "1-Fan", "2-Fan"],
        rows, f"📋 Jami ro'yxatdan o'tganlar: {len(rows)} ta",
    )


@admin_router.message(F.text == "📊 Statistikani ko'rish")
async def show_stats(message: Message):
    users = db_one("SELECT COUNT(*) FROM users")[0]
    questions = db_one("SELECT COUNT(*) FROM questions")[0]
    results = db_one("SELECT COUNT(*) FROM results")[0]
    await message.answer(
        f"📊 <b>Statistika:</b>\n\nFoydalanuvchilar: {users} ta\nSavollar: {questions} ta\nTopshirilgan testlar: {results} ta"
    )


@admin_router.message(F.text == "💾 Zaxira (DB)")
async def backup_db(message: Message):
    await message.answer_document(FSInputFile(DB_PATH), caption="💾 Baza nusxasi. Vaqti-vaqti bilan saqlab qo'ying.")


# --- Xabar yuborish ---
@admin_router.message(F.text == "📢 Xabar yuborish")
async def broadcast_start(message: Message, state: FSMContext):
    await message.answer(
        "Barcha foydalanuvchilarga yuboriladigan xabarni kiriting (bekor qilish: /cancel):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Broadcast.message)


@admin_router.message(Broadcast.message)
async def broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    ok = failed = 0
    for (user_id,) in db_all("SELECT user_id FROM users"):
        try:
            await message.copy_to(user_id)  # matn, rasm, fayl - hammasini yuboradi
            ok += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"✅ Xabar yuborildi!\n\nMuvaffaqiyatli: {ok}\nYetib bormadi: {failed}",
        reply_markup=get_admin_keyboard(),
    )


# ======================================================================
# FOYDALANUVCHI: BUYRUQLAR (holatli handlerlardan OLDIN turishi kerak)
# ======================================================================
@user_router.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id, bot):
        await callback.message.delete()
        await callback.message.answer("✅ Rahmat! Obuna tasdiqlandi. /start buyrug'ini yuboring.")
    else:
        await callback.answer("❌ Siz hali kanalga a'zo bo'lmadingiz!", show_alert=True)


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not await check_subscription(message.from_user.id, bot):
        await message.answer(
            "Botdan foydalanish uchun quyidagi kanalimizga a'zo bo'ling:",
            reply_markup=get_sub_keyboard(),
        )
        return

    user = db_one("SELECT full_name FROM users WHERE user_id = ?", (message.from_user.id,))
    if user:
        await message.answer(
            f"Xush kelibsiz, {esc(user[0])}!\n"
            "Test topshirish uchun /test buyrug'ini bosing.\n"
            "Reytingni ko'rish uchun /rating buyrug'ini bosing."
        )
    else:
        await message.answer("Assalomu alaykum! Olimpiadada ishtirok etish uchun ro'yxatdan o'ting.\n\nIsm va familiyangizni kiriting:")
        await state.set_state(Registration.full_name)


@user_router.message(Command("admin"))
async def cmd_admin_denied(message: Message):
    # Admin bu yerga yetib kelmaydi (admin_router oldin ushlaydi)
    await message.answer("Siz admin emassiz!")


@user_router.message(Command("rating"))
async def show_rating(message: Message):
    rows = db_all("""
        SELECT u.full_name, r.subject, r.percentage, r.time_spent
        FROM results r JOIN users u ON r.user_id = u.user_id
        ORDER BY r.percentage DESC, r.time_spent ASC
        LIMIT 10
    """)
    if not rows:
        await message.answer("Hozircha reyting mavjud emas.")
        return
    text = "🏆 <b>Top 10 Ishtirokchilar Reytingi:</b>\n\n"
    for idx, (name, subject, percentage, spent) in enumerate(rows, 1):
        text += f"{idx}. {esc(name)} - {esc(subject)}\n   📊 Natija: {percentage:.1f}% | ⏱ Vaqt: {fmt_time(spent)}\n\n"
    await message.answer(text)


@user_router.message(Command("test"))
async def start_quiz_cmd(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Test o'rtasida qayta /test bosib, savollarni qaytadan boshlash mumkin emas
    if await state.get_state() == Quiz.solving.state:
        await message.answer("Siz hozir testni ishlayapsiz. Savollarga javob bering.")
        return
    if not await check_subscription(user_id, bot):
        await message.answer("Test topshirish uchun avval kanalimizga a'zo bo'ling:", reply_markup=get_sub_keyboard())
        return
    if not is_test_active():
        await message.answer("⚠️ <b>Test hali admin tomonidan faollashtirilmadi!</b>")
        return

    user = db_one("SELECT subject1, subject2 FROM users WHERE user_id = ?", (user_id,))
    if not user:
        await message.answer("Avval /start orqali ro'yxatdan o'ting.")
        return

    with_questions = {row[0] for row in db_all("SELECT DISTINCT subject FROM questions")}
    subjects = [s for s in user if s in with_questions]
    if not subjects:
        await message.answer("Siz tanlagan fanlar bo'yicha hozircha bazada savollar mavjud emas.")
        return

    # Har bir fandan faqat bir marta topshirish mumkin (admin uchun cheklov yo'q)
    if user_id != ADMIN_ID:
        taken = {row[0] for row in db_all("SELECT subject FROM results WHERE user_id = ?", (user_id,))}
        subjects = [s for s in subjects if s not in taken]
        if not subjects:
            await message.answer("✅ Siz tanlagan fanlar bo'yicha testlarni topshirib bo'lgansiz. Reyting: /rating")
            return

    await state.update_data(allowed=subjects)
    await state.set_state(Quiz.subject)
    await message.answer("Test topshirmoqchi bo'lgan fanni tanlang:", reply_markup=reply_kb(subjects, per_row=1))


# ======================================================================
# FOYDALANUVCHI: RO'YXATDAN O'TISH
# ======================================================================
@user_router.message(Registration.full_name)
async def process_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, ism va familiyangizni matn ko'rinishida yozing:")
        return
    await state.update_data(full_name=message.text.strip())
    await state.set_state(Registration.phone)
    await message.answer("Telefon raqamingizni yuboring:", reply_markup=phone_keyboard)


@user_router.message(Registration.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else (message.text or "")
    if not 9 <= len(re.sub(r"\D", "", phone)) <= 15:
        await message.answer("Telefon raqam noto'g'ri. Tugma orqali yuboring yoki +998901234567 ko'rinishida yozing:")
        return
    await state.update_data(phone=phone)
    await state.set_state(Registration.category)
    await message.answer("Toifangizni tanlang:", reply_markup=reply_kb(CATEGORIES, per_row=3))


@user_router.message(Registration.category)
async def process_category(message: Message, state: FSMContext):
    if message.text not in CATEGORIES:
        await message.answer("Iltimos, tugmalardan birini tanlang!")
        return
    await state.update_data(category=message.text)
    await state.set_state(Registration.subject1)
    await message.answer("Olimpiadada qatnashmoqchi bo'lgan 1-fanni tanlang:", reply_markup=reply_kb(AVAILABLE_SUBJECTS))


@user_router.message(Registration.subject1)
async def process_subject1(message: Message, state: FSMContext):
    if message.text not in AVAILABLE_SUBJECTS:
        await message.answer("Iltimos, tugmalardan birini tanlang!")
        return
    await state.update_data(subject1=message.text)
    await state.set_state(Registration.subject2)
    other = [s for s in AVAILABLE_SUBJECTS if s != message.text]
    await message.answer("Olimpiadada qatnashmoqchi bo'lgan 2-fanni tanlang:", reply_markup=reply_kb(other))


@user_router.message(Registration.subject2)
async def process_subject2(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text not in AVAILABLE_SUBJECTS or message.text == data["subject1"]:
        await message.answer("Iltimos, boshqa to'g'ri fanni tanlang!")
        return

    # INSERT OR REPLACE emas: qayta ro'yxatdan o'tish ma'lumotni yangilaydi, natijalarga tegmaydi
    db_exec(
        "INSERT OR REPLACE INTO users (user_id, full_name, phone, category, subject1, subject2) VALUES (?, ?, ?, ?, ?, ?)",
        (message.from_user.id, data["full_name"], data["phone"], data["category"], data["subject1"], message.text),
    )
    await message.answer(
        "Tabriklaymiz siz \"Alif cup\" olimpiadasida qatnashish uchun ro'yxatdan o'tdingiz. "
        "Olimpiadani birinchi online bosqichi 18.10.2026 da boshlanadi. "
        "Alif academy jamoasi sizga omad tilaydi\n\n"
        f"👤 Ism: {esc(data['full_name'])}\n"
        f"📱 Tel: {esc(data['phone'])}\n"
        f"🎓 Toifa: {esc(data['category'])}\n"
        f"📚 Tanlangan fanlar: {esc(data['subject1'])}, {esc(message.text)}\n\n"
        "Test boshlanganda /test buyrug'ini bosing.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


# ======================================================================
# FOYDALANUVCHI: TEST
# ======================================================================
@user_router.message(Quiz.subject)
async def select_subject(message: Message, state: FSMContext):
    data = await state.get_data()
    subject = message.text
    if subject not in data.get("allowed", []):
        await message.answer("Iltimos, tugmalardan birini tanlang!")
        return

    questions = db_all(
        "SELECT id, subject, question_text, option_a, option_b, option_c, option_d, correct_option "
        "FROM questions WHERE subject = ?",
        (subject,),
    )
    if not questions:
        await message.answer("Ushbu fan bo'yicha savollar topilmadi.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    random.shuffle(questions)  # har bir ishtirokchiga savollar boshqa tartibda
    await state.update_data(
        user_id=message.from_user.id,  # natijani aynan shu foydalanuvchiga yozish uchun
        subject=subject,
        questions=questions,
        current_q=0,
        score=0,
        start_time=time.time(),
        answers_log=[],
    )
    await state.set_state(Quiz.solving)
    await send_question(message, state)


async def send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    questions, idx = data["questions"], data["current_q"]

    if idx >= len(questions):
        await finish_quiz(message, state)
        return

    q = questions[idx]  # (id, subject, text, a, b, c, d, correct)
    time_left = max(0, int(QUIZ_TIME_LIMIT - (time.time() - data["start_time"]))) // 60
    options = "\n".join(f"{letter}) {esc(opt)}" for letter, opt in zip("ABCD", q[3:7]))

    # callback_data ichida savol raqami bor: ikki marta bosilsa, ikkinchisi e'tiborga olinmaydi
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=letter, callback_data=f"ans:{idx}:{letter}") for letter in "ABCD"
    ]])
    await message.answer(
        f"⏱ Qolgan vaqt: {time_left} daqiqa\n\n"
        f"<b>Savol {idx + 1}/{len(questions)}</b>\n\n{esc(q[2])}\n\n{options}",
        reply_markup=kb,
    )


@user_router.callback_query(Quiz.solving, F.data.startswith("ans:"))
async def quiz_answer(callback: CallbackQuery, state: FSMContext):
    _, idx, choice = callback.data.split(":")
    data = await state.get_data()
    q_idx = data["current_q"]

    if int(idx) != q_idx:
        await callback.answer("Bu savolga allaqachon javob berdingiz.")
        return

    # Vaqt tugagan bo'lsa, bu javob hisobga olinmaydi
    if time.time() - data["start_time"] > QUIZ_TIME_LIMIT:
        await callback.answer("⏱ Vaqt tugadi!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("⏱ <b>Vaqt tugadi!</b> Test avtomatik yakunlandi.")
        await finish_quiz(callback.message, state)
        return

    q = data["questions"][q_idx]
    correct = q[7]
    options = dict(zip("ABCD", q[3:7]))
    is_correct = choice == correct

    answers_log = data["answers_log"]
    answers_log.append({
        "q_num": q_idx + 1,
        "question": q[2],
        "user_ans": f"{choice}) {options.get(choice, '')}",
        "correct_ans": f"{correct}) {options.get(correct, '')}",
        "is_correct": is_correct,
    })
    await state.update_data(
        current_q=q_idx + 1,
        score=data["score"] + (1 if is_correct else 0),
        answers_log=answers_log,
    )

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_question(callback.message, state)


async def finish_quiz(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()  # darhol tozalaymiz: natija ikki marta yozilib ketmasin

    questions, score, subject = data["questions"], data["score"], data["subject"]
    answers_log = data["answers_log"]
    total = len(questions)
    percentage = score / total * 100 if total else 0
    time_spent = min(int(time.time() - data["start_time"]), QUIZ_TIME_LIMIT)

    # user_id state'dan olinadi: callback.message.from_user bu BOTNING o'zi bo'ladi!
    db_exec(
        "INSERT INTO results (user_id, subject, score, total_questions, percentage, time_spent, user_answers) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (data["user_id"], subject, score, total, percentage, time_spent,
         json.dumps(answers_log, ensure_ascii=False)),
    )

    await message.answer(
        "🎉 <b>Test yakunlandi!</b>\n\n"
        f"📚 Fan: {esc(subject)}\n"
        f"✅ To'g'ri javoblar: {score} / {total} ta\n"
        f"📈 Natija: {percentage:.1f}%\n"
        f"⏱ Sarflangan vaqt: {fmt_time(time_spent)}",
        reply_markup=ReplyKeyboardRemove(),
    )

    parts = ["📊 <b>SAVOLLAR TAHLILI:</b>\n\n"]
    for item in answers_log:
        status = "✅ To'g'ri" if item["is_correct"] else f"❌ Noto'g'ri (To'g'ri javob: {esc(item['correct_ans'])})"
        parts.append(
            f"<b>{item['q_num']}-savol:</b> {esc(item['question'])}\n"
            f"Sizning javobingiz: {esc(item['user_ans'])} - {status}\n\n"
        )
    for chunk in chunk_text(parts):
        await message.answer(chunk)


# ======================================================================
# ISHGA TUSHIRISH
# ======================================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()

    users = db_one("SELECT COUNT(*) FROM users")[0]
    questions = db_one("SELECT COUNT(*) FROM questions")[0]
    logging.info("Baza: %s | foydalanuvchilar: %s | savollar: %s", DB_PATH, users, questions)

    dp.include_router(admin_router)  # admin oldin tekshiriladi
    dp.include_router(user_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
