import asyncio
import csv
import logging
import os
import re
import sqlite3
import time
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Configuration Settings
BOT_TOKEN = "8930443652:AAHxryi2LCo5e2BCzH8RD1K7aDgjyKTx0l0"  # Bot tokeningiz
ADMIN_ID = 8208777595  # Telegram ID'ingiz
CHANNEL_ID = "@alif_academy_lc"  # Majburiy kanal username

QUIZ_TIME_LIMIT = 3600  # Test uchun vaqt cheklovi (soniyalarda): 3600s = 60 daqiqa

AVAILABLE_SUBJECTS = [
    "Huquq",
    "Tarix",
    "Ona tili",
    "Ingliz tili",
    "Matematika",
    "SAT English",
    "SAT Math"
]

# Database Setup
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            category TEXT,
            subject1 TEXT,
            subject2 TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            question_text TEXT,
            option_a TEXT,
            option_b TEXT,
            option_c TEXT,
            option_d TEXT,
            correct_option TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            score INTEGER,
            total_questions INTEGER,
            percentage REAL,
            time_spent INTEGER,
            user_answers TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_status', 'off')")
    conn.commit()
    conn.close()

init_db()

# FSM States
class Registration(StatesGroup):
    full_name = State()
    phone = State()
    category = State()
    subject1 = State()
    subject2 = State()

class AddQuestionSimple(StatesGroup):
    subject = State()
    raw_data = State()

class Broadcast(StatesGroup):
    message = State()

class Quiz(StatesGroup):
    subject = State()
    solving = State()

# Helper Functions
async def check_subscription(user_id: int, bot: Bot) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception:
        return False

def get_sub_keyboard():
    channel_url = f"https://t.me/{CHANNEL_ID.replace('@', '')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=channel_url)],
            [InlineKeyboardButton(text="✅ A'zo bo'ldim", callback_data="check_sub")]
        ]
    )

def is_test_active() -> bool:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'test_status'")
    status = cursor.fetchone()
    conn.close()
    return status and status[0] == "on"

def set_test_status(status: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'test_status'", (status,))
    conn.commit()
    conn.close()

# Keyboards
def get_category_keyboard():
    buttons = []
    for i in range(1, 12, 3):
        row = [KeyboardButton(text=f"{i}-sinf")]
        if i+1 <= 11:
            row.append(KeyboardButton(text=f"{i+1}-sinf"))
        if i+2 <= 11:
            row.append(KeyboardButton(text=f"{i+2}-sinf"))
        buttons.append(row)
    buttons.append([KeyboardButton(text="Talaba")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True
)

def get_subjects_reply_keyboard(exclude: str = None):
    buttons = []
    row = []
    for sub in AVAILABLE_SUBJECTS:
        if sub != exclude:
            row.append(KeyboardButton(text=sub))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

def get_admin_keyboard():
    status = is_test_active()
    toggle_btn_text = "🔴 Testni o'chirish" if status else "🟢 Testni yoqish"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=toggle_btn_text), KeyboardButton(text="➕ Savol qo'shish")],
            [KeyboardButton(text="🗑 Savol o'chirish"), KeyboardButton(text="📄 O'quvchilar ro'yxati")],
            [KeyboardButton(text="🏆 Natijalar (Admin)"), KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📊 Statistikani ko'rish")]
        ],
        resize_keyboard=True
    )

def get_active_subjects_keyboard(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT subject1, subject2 FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    user_subjects = [user[0], user[1]] if user else []

    cursor.execute("SELECT DISTINCT subject FROM questions")
    available = [row[0] for row in cursor.fetchall()]
    conn.close()

    active_user_subjects = [s for s in user_subjects if s in available]
    
    if not active_user_subjects:
        return None

    buttons = [[KeyboardButton(text=sub)] for sub in active_user_subjects]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Subscription Handler
@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: types.CallbackQuery):
    is_sub = await check_subscription(callback.from_user.id, bot)
    if is_sub:
        await callback.message.delete()
        await callback.message.answer("✅ Rahmat! Obuna tasdiqlandi. /start buyrug'ini yuboring.")
    else:
        await callback.answer("❌ Siz hali kanalga a'zo bo'lmadingiz!", show_alert=True)

# Registration Handler
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not await check_subscription(message.from_user.id, bot):
        await message.answer(
            "Botdan foydalanish uchun quyidagi kanalimizga a'zo bo'ling:",
            reply_markup=get_sub_keyboard()
        )
        return

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()

    if user:
        await message.answer(
            f"Xush kelibsiz, {user[1]}!\n"
            "Test topshirish uchun /test buyrug'ini bosing.\n"
            "Reytingni ko'rish uchun /rating buyrug'ini bosing."
        )
    else:
        await message.answer("Assalomu alaykum! Olimpiadada ishtirok etish uchun ro'yxatdan o'ting.\n\nIsm va familiyangizni kiriting:")
        await state.set_state(Registration.full_name)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Admin panelga xush kelibsiz!", reply_markup=get_admin_keyboard())
    else:
        await message.answer("Siz admin emassiz!")

@dp.message(Registration.full_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await message.answer("Telefon raqamingizni yuboring:", reply_markup=phone_keyboard)
    await state.set_state(Registration.phone)

@dp.message(Registration.phone, F.contact)
async def process_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await message.answer("Toifangizni tanlang:", reply_markup=get_category_keyboard())
    await state.set_state(Registration.category)

@dp.message(Registration.phone)
async def process_phone_text(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("Toifangizni tanlang:", reply_markup=get_category_keyboard())
    await state.set_state(Registration.category)

@dp.message(Registration.category)
async def process_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text)
    await message.answer("Olimpiadada qatnashmoqchi bo'lgan 1-fanni tanlang:", reply_markup=get_subjects_reply_keyboard())
    await state.set_state(Registration.subject1)

@dp.message(Registration.subject1)
async def process_subject1(message: Message, state: FSMContext):
    if message.text not in AVAILABLE_SUBJECTS:
        await message.answer("Iltimos, tugmalardan birini tanlang!")
        return
    await state.update_data(subject1=message.text)
    await message.answer("Olimpiadada qatnashmoqchi bo'lgan 2-fanni tanlang:", reply_markup=get_subjects_reply_keyboard(exclude=message.text))
    await state.set_state(Registration.subject2)

@dp.message(Registration.subject2)
async def process_subject2(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text not in AVAILABLE_SUBJECTS or message.text == data['subject1']:
        await message.answer("Iltimos, boshqa to'g'ri fanni tanlang!")
        return
    await state.update_data(subject2=message.text)
    data = await state.get_data()

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, full_name, phone, category, subject1, subject2)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (message.from_user.id, data['full_name'], data['phone'], data['category'], data['subject1'], data['subject2']))
    conn.commit()
    conn.close()

    await message.answer(
        "Tabriklaymiz siz \"Alif cup\" olimpiadasida qatnashish uchun ro'yxatdan o'tdingiz. "
        "Olimpiadani birinchi online bosqichi 18.10.2026 da boshlanadi. "
        "Alif academy jamoasi sizga omad tilaydi\n\n"
        f"👤 Ism: {data['full_name']}\n"
        f"📱 Tel: {data['phone']}\n"
        f"🎓 Toifa: {data['category']}\n"
        f"📚 Tanlangan fanlar: {data['subject1']}, {data['subject2']}\n\n"
        "Test boshlanganda /test buyrug'ini bosing.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.clear()

# Admin Control: Toggle Test
@dp.message(F.text.in_(["🟢 Testni yoqish", "🔴 Testni o'chirish"]))
async def toggle_test(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    current_status = is_test_active()
    new_status = "off" if current_status else "on"
    set_test_status(new_status)

    if new_status == "on":
        await message.answer("🟢 **Test tizimi YOQILDI!** Faqat savollar mavjud bo'lgan fanlar o'quvchilarga ko'rinadi.", reply_markup=get_admin_keyboard())
    else:
        await message.answer("🔴 **Test tizimi O'CHIRILDI!**", reply_markup=get_admin_keyboard())

# Admin: Simplified Question Addition
@dp.message(F.text == "➕ Savol qo'shish")
async def add_q_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Qaysi fan uchun savol qo'shmoqchisiz?", reply_markup=get_subjects_reply_keyboard())
    await state.set_state(AddQuestionSimple.subject)

@dp.message(AddQuestionSimple.subject)
async def add_q_subject(message: Message, state: FSMContext):
    if message.text not in AVAILABLE_SUBJECTS:
        await message.answer("Iltimos, mavjud fanlardan birini tanlang.")
        return
    await state.update_data(subject=message.text)
    await message.answer(
        "Savol va variantlarni quyidagi formatda 1 ta xabarda yuboring:\n\n"
        "Savol matni\n"
        "A) Variant 1\n"
        "B) Variant 2\n"
        "C) Variant 3\n"
        "D) Variant 4\n"
        "Javob: A",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AddQuestionSimple.raw_data)

@dp.message(AddQuestionSimple.raw_data)
async def add_q_process(message: Message, state: FSMContext):
    lines = [line.strip() for line in message.text.strip().split("\n") if line.strip()]
    if len(lines) < 6:
        await message.answer("❌ Format noto'g'ri! Kamida 6 ta qator bo'lishi kerak (Savol, 4 ta variant, Javob).")
        return

    try:
        q_text = lines[0]
        opt_a = re.sub(r"^[A-Da-d][\)\.]\s*", "", lines[1])
        opt_b = re.sub(r"^[A-Da-d][\)\.]\s*", "", lines[2])
        opt_c = re.sub(r"^[A-Da-d][\)\.]\s*", "", lines[3])
        opt_d = re.sub(r"^[A-Da-d][\)\.]\s*", "", lines[4])
        
        # Javob kalitini ajratib olish (A, B, C yoki D)
        ans_match = re.search(r"JAVOB:\s*([A-D])", lines[5], re.IGNORECASE)
        if not ans_match:
            await message.answer("❌ To'g'ri javob belgilanmadi! Oxirgi qator 'Javob: A' ko'rinishida bo'lishi kerak.")
            return

        correct_letter = ans_match.group(1).upper()

        data = await state.get_data()
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO questions (subject, question_text, option_a, option_b, option_c, option_d, correct_option)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data['subject'], q_text, opt_a, opt_b, opt_c, opt_d, correct_letter))
        conn.commit()
        conn.close()

        await message.answer("✅ Savol muvaffaqiyatli saqlandi!", reply_markup=get_admin_keyboard())
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {e}\nFormatni tekshirib qayta yuboring.")

# Admin: Delete Question
@dp.message(F.text == "🗑 Savol o'chirish")
async def delete_q_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, subject, question_text FROM questions ORDER BY id DESC LIMIT 10")
    questions = cursor.fetchall()
    conn.close()

    if not questions:
        await message.answer("O'chirish uchun savollar topilmadi.")
        return

    text = "O'chirmoqchi bo'lgan savolning ID raqamini yuboring (/del_ID):\n\n"
    for q in questions:
        text += f"🆔 /del_{q[0]} | [{q[1]}] {q[2][:30]}...\n"
    await message.answer(text)

@dp.message(F.text.startswith("/del_"))
async def delete_q_confirm(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        q_id = int(message.text.replace("/del_", ""))
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM questions WHERE id = ?", (q_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ ID: {q_id} bo'lgan savol o'chirildi.")
    except Exception:
        await message.answer("❌ Noto'g'ri ID format.")

# Admin: View All Results
@dp.message(F.text == "🏆 Natijalar (Admin)")
async def admin_results(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.full_name, u.phone, r.subject, r.score, r.total_questions, r.percentage, r.time_spent
        FROM results r
        JOIN users u ON r.user_id = u.user_id
        ORDER BY r.percentage DESC, r.time_spent ASC
    """)
    results = cursor.fetchall()
    conn.close()

    if not results:
        await message.answer("Hozircha test ishlaganlar yo'q.")
        return

    file_path = "test_natijalari.csv"
    with open(file_path, mode="w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["F.I.SH", "Telefon", "Fan", "To'g'ri javob", "Jami savol", "Foiz (%)", "Sarflangan vaqt (sek)"])
        writer.writerows(results)

    await message.answer_document(
        document=FSInputFile(file_path),
        caption=f"🏆 Jami ishlangan testlar: {len(results)} ta"
    )
    os.remove(file_path)

# Admin: Export Users List
@dp.message(F.text == "📄 O'quvchilar ro'yxati")
async def export_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, full_name, phone, category, subject1, subject2 FROM users")
    users = cursor.fetchall()
    conn.close()

    if not users:
        await message.answer("Hozircha hech kim ro'yxatdan o'tmagan.")
        return

    file_path = "oquvchilar_royxati.csv"
    with open(file_path, mode="w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["Telegram ID", "Ism Familiya", "Telefon", "Toifa", "1-Fan", "2-Fan"])
        writer.writerows(users)

    await message.answer_document(
        document=FSInputFile(file_path),
        caption=f"📋 Jami ro'yxatdan o'tganlar: {len(users)} ta"
    )
    os.remove(file_path)

# Admin Broadcast
@dp.message(F.text == "📢 Xabar yuborish")
async def broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni kiriting:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Broadcast.message)

@dp.message(Broadcast.message)
async def broadcast_send(message: Message, state: FSMContext):
    broadcast_text = message.text
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()

    count_success = 0
    count_failed = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user[0], text=broadcast_text)
            count_success += 1
            await asyncio.sleep(0.05)
        except Exception:
            count_failed += 1

    await message.answer(f"✅ Xabar yuborildi!\n\nMuvaffaqiyatli: {count_success}\nYetib bormadi: {count_failed}", reply_markup=get_admin_keyboard())
    await state.clear()

@dp.message(F.text == "📊 Statistikani ko'rish")
async def show_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    u_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM questions")
    q_cnt = cursor.fetchone()[0]
    conn.close()

    await message.answer(f"📊 **Statistika:**\n\nFoydalanuvchilar: {u_cnt} ta\nSavollar: {q_cnt} ta")

# Top Ranking System
@dp.message(Command("rating"))
async def show_rating(message: Message):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.full_name, r.subject, r.percentage, r.time_spent
        FROM results r
        JOIN users u ON r.user_id = u.user_id
        ORDER BY r.percentage DESC, r.time_spent ASC
        LIMIT 10
    """)
    top_list = cursor.fetchall()
    conn.close()

    if not top_list:
        await message.answer("Hozircha reyting mavjud emas.")
        return

    text = "🏆 **Top 10 Ishtirokchilar Reytingi:**\n\n"
    for idx, item in enumerate(top_list, 1):
        minutes = item[3] // 60
        seconds = item[3] % 60
        text += f"{idx}. {item[0]} - {item[1]}\n   📊 Natija: {item[2]:.1f}% | ⏱ Vaqt: {minutes}m {seconds}s\n\n"

    await message.answer(text)

# Quiz System
@dp.message(Command("test"))
async def start_quiz_cmd(message: Message, state: FSMContext):
    if not await check_subscription(message.from_user.id, bot):
        await message.answer("Test topshirish uchun avval kanalimizga a'zo bo'ling:", reply_markup=get_sub_keyboard())
        return

    if not is_test_active():
        await message.answer("⚠️ **Test hali admin tomonidan faollashtirilmadi!**")
        return

    kb = get_active_subjects_keyboard(message.from_user.id)
    if not kb:
        await message.answer("Siz tanlagan fanlar bo'yicha hozircha bazada savollar mavjud emas.")
        return

    await message.answer("Test topshirmoqchi bo'lgan fanni tanlang:", reply_markup=kb)
    await state.set_state(Quiz.subject)

@dp.message(Quiz.subject)
async def select_subject(message: Message, state: FSMContext):
    subject = message.text
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM questions WHERE subject = ?", (subject,))
    questions = cursor.fetchall()
    conn.close()

    if not questions:
        await message.answer("Ushbu fan bo'yicha savollar topilmadi.")
        await state.clear()
        return

    await state.update_data(
        subject=subject,
        questions=questions,
        current_q=0,
        score=0,
        start_time=time.time(),
        answers_log=[]
    )
    await state.set_state(Quiz.solving)
    await send_question(message, state)

async def send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    questions = data['questions']
    q_idx = data['current_q']
    start_time = data['start_time']

    # Timer Check
    elapsed_time = time.time() - start_time
    if elapsed_time > QUIZ_TIME_LIMIT:
        await message.answer("⏱ **Vaqt tugadi!** Test avtomatik ravishda yakunlandi.")
        await finish_quiz(message, state)
        return

    if q_idx < len(questions):
        q = questions[q_idx]  # (id, subject, text, opt_a, opt_b, opt_c, opt_d, correct_opt)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"A) {q[3]}", callback_data="ans_A")],
            [InlineKeyboardButton(text=f"B) {q[4]}", callback_data="ans_B")],
            [InlineKeyboardButton(text=f"C) {q[5]}", callback_data="ans_C")],
            [InlineKeyboardButton(text=f"D) {q[6]}", callback_data="ans_D")]
        ])
        
        time_left = int(QUIZ_TIME_LIMIT - elapsed_time) // 60
        await message.answer(
            f"⏱ Qolgan vaqt: {time_left} daqiqa\n\n"
            f"**Savol {q_idx + 1}/{len(questions)}:**\n\n{q[2]}", 
            reply_markup=kb
        )
    else:
        await finish_quiz(message, state)

@dp.callback_query(Quiz.solving, F.data.startswith("ans_"))
async def process_quiz_answer_callback(callback: types.CallbackQuery, state: FSMContext):
    user_choice = callback.data.replace("ans_", "")  # 'A', 'B', 'C' yoki 'D'
    
    data = await state.get_data()
    questions = data['questions']
    q_idx = data['current_q']
    score = data['score']
    answers_log = data['answers_log']

    current_q = questions[q_idx]
    correct_answer = current_q[7]  # Bazadagi 'A', 'B', 'C' yoki 'D'

    is_correct = (user_choice == correct_answer)
    if is_correct:
        score += 1

    opt_map = {"A": current_q[3], "B": current_q[4], "C": current_q[5], "D": current_q[6]}

    answers_log.append({
        "q_num": q_idx + 1,
        "question": current_q[2],
        "user_ans": f"{user_choice}) {opt_map.get(user_choice, '')}",
        "correct_ans": f"{correct_answer}) {opt_map.get(correct_answer, '')}",
        "is_correct": is_correct
    })

    await state.update_data(current_q=q_idx + 1, score=score, answers_log=answers_log)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    
    await send_question(callback.message, state)

async def finish_quiz(message: Message, state: FSMContext):
    data = await state.get_data()
    questions = data['questions']
    score = data['score']
    subject = data['subject']
    start_time = data['start_time']
    answers_log = data['answers_log']
    user_id = message.from_user.id

    time_spent = int(time.time() - start_time)
    total_q = len(questions)
    percentage = (score / total_q) * 100 if total_q > 0 else 0

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO results (user_id, subject, score, total_questions, percentage, time_spent)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, subject, score, total_q, percentage, time_spent))
    conn.commit()
    conn.close()

    # Detailed Analysis Text
    analysis_text = "📊 **SAVOLLAR TAHLILI:**\n\n"
    for item in answers_log:
        status = "✅ To'g'ri" if item["is_correct"] else f"❌ Noto'g'ri (To'g'ri javob: {item['correct_ans']})"
        analysis_text += f"**{item['q_num']}-savol:** {item['question']}\nSizning javobingiz: {item['user_ans']} - {status}\n\n"

    minutes = time_spent // 60
    seconds = time_spent % 60

    await message.answer(
        f"🎉 **Test yakunlandi!**\n\n"
        f"📚 Fan: {subject}\n"
        f"✅ To'g'ri javoblar: {score} / {total_q} ta\n"
        f"📈 Natija: {percentage:.1f}%\n"
        f"⏱ Sarflangan vaqt: {minutes}m {seconds}s",
        reply_markup=ReplyKeyboardRemove()
    )

    if len(analysis_text) < 4000:
        await message.answer(analysis_text)
    else:
        for chunk in [analysis_text[i:i+4000] for i in range(0, len(analysis_text), 4000)]:
            await message.answer(chunk)

    await state.clear()

# Main Execution
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
