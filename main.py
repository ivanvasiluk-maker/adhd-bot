import asyncio
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import gspread


# =========================
# НАСТРОЙКИ (всё в этом файле)
# =========================
# Заполните нужные значения ниже. Минимум: BOT_TOKEN и ADMIN_CHAT_ID.
BOT_TOKEN = "8740574590:AAGTdq4iO8m1EokEfWIUKIY5g6suEvOtfwg"  # токен бота из BotFather
ADMIN_CHAT_ID =312112015  # ваш Telegram ID (число)

# Google Sheets (опционально)
GOOGLE_CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_ID = "1CdsojmvRe1-ylb8b5s1C6FBnd1mljCXaC28ZpcMs1Uc"
SHEET_TAB_NAME: Optional[str] = None
ENABLE_SHEETS = True

# Webhook (обычно не нужен, если запускаете локально — оставайтесь на polling)
USE_WEBHOOK = False
WEBHOOK_URL = ""
WEBHOOK_PATH = "/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = 8080

FAQ_URL = "https://vasiliuk.pro/sdvg1"
YOUR_TELEGRAM = "@Ivan_Vasiliuk"
CONSULT_LINK = "https://calendly.com/ivan-vasiluk/meet-with-me"

# =========================
# ЛОГИ
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("adhd_bot")


# =========================
# GOOGLE SHEETS (optional)
# =========================
@contextmanager
def _temp_credentials_file(creds_json: str):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        tmp.write(creds_json)
        tmp.close()
        yield tmp.name
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def init_sheet() -> Optional[object]:
    if not ENABLE_SHEETS:
        return None

    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID)

    try:
        if creds_json:
            json.loads(creds_json)
            with _temp_credentials_file(creds_json) as temp_path:
                gc = gspread.service_account(filename=temp_path)
        else:
            gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)

        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is not set")

        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(SHEET_TAB_NAME) if SHEET_TAB_NAME else sh.sheet1
        return ws
    except Exception:
        log.exception("Sheets init failed, disabling sheets")
        return None


sheet = init_sheet()


# =========================
# SKILL TIMER CONTROL
# =========================
skill_timers: dict[int, asyncio.Task] = {}
lead_reminders: dict[int, asyncio.Task] = {}


def cancel_skill_timer(chat_id: int) -> None:
    """Cancel pending skill timer reminder for chat if exists."""
    task = skill_timers.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def schedule_skill_timer(chat_id: int) -> None:
    """Schedule skill reminder; replace any existing one."""
    cancel_skill_timer(chat_id)
    skill_timers[chat_id] = asyncio.create_task(send_skill_reminder(chat_id))


def cancel_lead_reminder(chat_id: int) -> None:
    """Cancel pending lead reminder if exists."""
    task = lead_reminders.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def schedule_lead_reminder(chat_id: int) -> None:
    """Schedule a 1-hour reminder to finish the form."""
    cancel_lead_reminder(chat_id)
    lead_reminders[chat_id] = asyncio.create_task(send_lead_reminder(chat_id))


# =========================
# BOT / DISPATCHER
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# =========================
# ТЕКСТЫ
# =========================
TEST_INTRO_TEXT = (
    "Привет 👋\n\n"
    "Если вы:\n\n"
    "— знаете что нужно делать, но не начинаете\n"
    "— можете часами собираться с мыслями\n"
    "— постоянно откладываете даже важные дела\n"
    "— живёте между «надо» и «потом»\n\n"
    "это часто не лень.\n\n"
    "У взрослых с СДВГ система запуска задач работает иначе.\n\n"
    "Я сделал короткий тест,\n"
    "который помогает понять,\n"
    "что именно происходит с прокрастинацией.\n\n"
    "3 вопроса\n"
    "30 секунд\n\n"
    "Он покажет, какой тип прокрастинации у вас сейчас.\n\n"
    "Обычно люди узнают себя уже\n"
    "в первом вопросе.\n\n"
    "Если это действительно про вас —\n"
    "покажу, что можно сделать.\n\n"
    "Тест занимает 30 секунд.\n\n"
    "Готовы?"
)

TEST_Q1 = (
    "Вопрос 1 из 3\n\n"
    "Когда вы думаете о задаче,\n"
    "что происходит чаще всего?"
)

TEST_Q2 = (
    "Вопрос 2 из 3\n\n"
    "Как давно это происходит?"
)

TEST_Q3 = (
    "Вопрос 3 из 3\n\n"
    "Вы пробовали:\n\n"
    "— таймеры\n"
    "— планирование\n"
    "— мотивацию\n\n"
    "но это работает несколько дней\n"
    "и потом снова откат?"
)

TEST_RESULT_TEXT = (
    "Судя по ответам,\n"
    "вы уже пробовали:\n\n"
    "— таймеры\n"
    "— планирование\n"
    "— мотивацию\n\n"
    "Но эффект держится несколько дней.\n\n"
    "Это типичная ситуация при СДВГ.\n\n"
    "Здесь нужна не мотивация,\n"
    "а тренировка навыков.\n\n"
    "Короче = сильнее.\n"
    "Давай посмотрим всего один навык, а на курсе их 48!\n"
)

STATE_QUESTION = "Выберите, что сейчас ближе всего — пришлю памятку:"

STATE_TO_MEMO = {
    "anx": (
        "😰 *Тревожная прокрастинация*\n\n"
        "Что происходит: мозг видит задачу как источник напряжения → включает избегание.\n\n"
        "Что помогает сегодня:\n"
        "1) Разбейте задачу до шага на 2–5 минут (\"открыть файл\").\n"
        "2) Таймер на 5 минут (\"начать\", а не \"сделать\").\n"
        "3) Разрешите сделать плохо — это снимает страх.\n"
    ),
    "stuck": (
        "🔁 *Откладываю и застреваю*\n\n"
        "Что происходит: внимание не фиксируется, мозг ищет быстрый дофамин.\n\n"
        "Что помогает:\n"
        "1) Правило одного шага — только первый кирпич.\n"
        "2) 10 минут работы → можно остановиться.\n"
        "3) Уберите один отвлекающий фактор (1 вкладка/телефон).\n"
    ),
    "apathy": (
        "😴 *Усталость / апатия*\n\n"
        "Когда мозг перегружен, он отключает запуск задач — это не лень.\n\n"
        "Что помогает:\n"
        "1) Самое маленькое действие, которое возможно.\n"
        "2) Начать с самой лёгкой задачи.\n"
        "3) 2 минуты движения — сильно повышают запуск внимания.\n"
    ),
    "mix": (
        "🌪️ *Смешанное состояние*\n\n"
        "Часто это смесь тревоги, перегрузки и отвлечений.\n\n"
        "Попробуйте:\n"
        "1) Самая маленькая задача.\n"
        "2) Таймер 5 минут.\n"
        "3) Убрать 1 отвлекающий фактор.\n"
    ),
}

DIAG_INTRO = (
    "Важно: одна техника редко меняет систему.\n"
    "Прокрастинация при СДВГ — это не про силу воли, а про работу внимания.\n\n"
    "Ответьте честно на 2 вопроса — подберу формат."
)

DIAG_Q1 = "Как давно проблемы с началом задач?"
DIAG_Q2 = (
    "Пробовали таймеры/планы/мотивацию — и это работает 2–3 дня, потом откат?"
)

PITCH_TEXT = (
    "Если прокрастинация тянется годами —\n"
    "она редко проходит сама.\n\n"
    "Проблема обычно не в мотивации.\n\n"
    "Это вопрос навыков внимания.\n\n"
    "Поэтому я запускаю\n"
    "небольшую группу\n"
    "тренинга навыков внимания\n"
    "для взрослых с СДВГ.\n\n"
    "Это программа,\n"
    "где мы учим мозг запускать задачи\n"
    "без насилия над собой.\n\n"
)

RECOGNITION_TEXT = (
    "Иногда люди думают,\n"
    "что у них просто слабая дисциплина.\n\n"
    "Но у взрослых с СДВГ\n"
    "часто происходит другое.\n\n"
    "Момент узнавания выглядит так:\n\n"
    "— вы знаете, что нужно делать  \n"
    "— задача не сложная  \n"
    "— времени вроде хватает  \n\n"
    "но мозг как будто\n"
    "не включает \"старт\".\n\n"
    "Вы можете:\n\n"
    "— долго собираться\n"
    "— прокручивать задачу в голове\n"
    "— делать что-то второстепенное\n"
    "— или откладывать «на чуть позже»\n\n"
    "И это может тянуться часами.\n\n"
    "Если это происходит часто —\n"
    "это не слабость.\n\n"
    "Это особенность работы внимания.\n\n"
    "Это снимает стыд → повышает покупку.\n\n"
    "Знакомо? Жмите «Дальше» — расскажу, как мы работаем."
)

HOST_INTRO_TEXT = (
    "Кстати, представлюсь.\n\n"
    "Меня зовут Иван Василюк.\n"
    "Я психолог и работаю с прокрастинацией и СДВГ у взрослых.\n\n"
    "Через группы и консультации\n"
    "уже прошли более 50 человек.\n\n"
    "Этот бот — выжимка техник,\n"
    "которые мы тренируем на программе.\n\n"
    "Они помогают запускать задачи\n"
    "без постоянного давления на себя.\n"
)

PROGRAM_INSIDE_TEXT = (
    "Что происходит на программе:\n\n"
    "8 недель  \n"
    "1 встреча в неделю\n\n"
    "Мы тренируем навыки:\n\n"
    "• микро-старт задач  \n"
    "• управление вниманием  \n"
    "• как не срываться после отката  \n"
    "• body doubling (групповой запуск задач)  \n"
    "• снижение самокритики  \n\n"
    "Каждую неделю:\n\n"
    "— онлайн встреча  \n"
    "— конкретные навыки  \n"
    "— маленькие задания  \n"
    "— поддержка в чате\n"
)

SOCIAL_PROOF_TEXT = (
    "Отзыв участника прошлой группы:\n\n"
    "«Я годами откладывал даже простые задачи.\n\n"
    "Знал, что нужно делать,\n"
    "но не мог начать.\n\n"
    "Через несколько недель\n"
    "я начал запускать задачи намного быстрее.\n\n"
    "Самое важное —\n"
    "стало намного меньше самокритики».\n"
)

SELF_ASSESSMENT_TEXT = (
    "Если честно: по шкале от 1 до 10 —\n"
    "насколько прокрастинация сейчас мешает вашей жизни?\n\n"
    "Если больше 6 — вы точно не один.\n"
    "И с этим можно работать системно."
)

OFFER_TEXT = (
    "Я собираю небольшую группу\n"
    "тренинга навыков для взрослых с СДВГ.\n\n"
    "Обычно люди приходят,\n"
    "когда уже устали\n"
    "от бесконечного «потом».\n\n"
    "Формат:\n\n"
    "8 недель  \n"
    "1 встреча в неделю  \n"
    "маленькие задания  \n"
    "поддержка\n\n"
    "240 € за всю программу\n"
    "(30 € в неделю)\n"
)

STRONG_SCREEN_TEXT = (
    "Судя по ответам,\n"
    "вы уже пробовали:\n\n"
    "— таймеры\n"
    "— планирование\n"
    "— мотивацию\n\n"
    "Но эффект держится несколько дней.\n\n"
    "Это типичная ситуация при СДВГ.\n\n"
    "Здесь нужна не мотивация,\n"
    "а тренировка навыков.\n\n"
    "Короче = сильнее.\n"
)

FAQ_TEXT = (
    "📌 *FAQ (коротко)*\n\n"
    "• *Как понять, что мне нужен тренинг?*\n"
    "Если вы регулярно сталкиваетесь с прокрастинацией, хаосом в делах, "
    "потерей фокуса, импульсивностью и хотите системные навыки.\n\n"
    "• *У меня нет диагноза — подойдёт?*\n"
    "Да. Техники работают вне ярлыка “СДВГ”, если проблемы похожи.\n\n"
    "• *Какой подход?*\n"
    "КПТ для внимания/планирования/прокрастинации + ДБТ для эмоций и импульсов.\n\n"
    "• *Материалы останутся?*\n"
    "Да, материалы можно пересматривать.\n\n"
    f"Полная страница: {FAQ_URL}"
)

FORMAT_DETAILS = {
    "async": (
        "*Асинхронный в чат-боте*\n"
        "— Темп свой, всё в Telegram\n"
        "— Практики и разборы в боте\n"
        "— Геймификация + обратная связь по запросу\n"
        "— Вводная неделя бесплатно\n"
        "— от 18.99 € в неделю"
    ),
    "group": (
        "*Групповой тренинг (8 недель)*\n"
        "— Онлайн 1×неделя\n"
        "— Домашки, поддержка, записи\n"
        "— Консультация включена\n"
        "— Геймификация и обратная связь\n"
        "— 240 € (120 € за 4 занятия), есть рассрочка"
    ),
    "coach": (
        "*Индивидуально с куратором*\n"
        "— План под ваши задачи\n"
        "— Асинхронные разборы\n"
        "— Максимальная гибкость и внимание к вашему темпу\n"
        "— 360 €, есть рассрочка"
    ),
}

FORMAT_LABELS = {
    "async": "Асинхронный",
    "group": "Групповой (8 недель)",
    "coach": "С куратором",
}

STATE_LABELS = {
    "anx": "Тревожная прокрастинация",
    "stuck": "Откладываю и застреваю",
    "apathy": "Усталость / апатия",
    "mix": "Смешанное состояние",
}


def get_format_label(key: str) -> str:
    """Human-friendly label for selected format."""
    return FORMAT_LABELS.get(key, key or "не выбран")


def get_state_label(key: str) -> str:
    """Human-friendly label for selected procrastination type."""
    return STATE_LABELS.get(key, key or "не выбрано")

FAQ_SHORT = {
    "need": "Нужен ли тренинг? Если часто прокрастинация/хаос/рывки мотивации — тренинг поможет выстроить систему.",
    "help": "Чем помогает? Практики для запуска задач, фокуса и снижения тревоги при СДВГ/похожих симптомах.",
    "approach": "Какой подход? КПТ для внимания и планирования + ДБТ для эмоций и импульсивности.",
    "access": "Доступ после курса: материалы остаются, можно пересматривать и задавать вопросы позже.",
}


# =========================
# КЛАВИАТУРЫ (aiogram 3)
# =========================
def kb_states() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="😰 Тревожная прокрастинация", callback_data="state:anx")
    b.button(text="🔁 Откладываю и застреваю", callback_data="state:stuck")
    b.button(text="😔 Усталость / апатия", callback_data="state:apathy")
    b.button(text="🌪️ Смешанное состояние", callback_data="state:mix")
    b.adjust(1)
    return b.as_markup()


def kb_start_test() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Пройти тест (30 секунд)", callback_data="test:start")
    return b.as_markup()


def kb_test_q1() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="😰 становится тревожно", callback_data="t1:anx")
    b.button(text="😴 нет энергии", callback_data="t1:apathy")
    b.button(text="🔁 откладываю снова и снова", callback_data="t1:stuck")
    b.button(text="🌪 всё сразу", callback_data="t1:mix")
    b.adjust(1)
    return b.as_markup()


def kb_test_q2() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Несколько месяцев", callback_data="t2:months")
    b.button(text="Несколько лет", callback_data="t2:years")
    b.button(text="Сколько себя помню", callback_data="t2:always")
    b.adjust(1)
    return b.as_markup()


def kb_test_q3() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data="t3:yes")
    b.button(text="Иногда", callback_data="t3:maybe")
    b.button(text="Нет", callback_data="t3:no")
    b.adjust(1)
    return b.as_markup()


def kb_try_skill() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Запустить внимание (2 минуты)", callback_data="skill:microstart")
    b.adjust(1)
    return b.as_markup()


def kb_recognition() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="😐 Да, это про меня", callback_data="recog:yes")
    b.button(text="🤔 Иногда бывает", callback_data="recog:maybe")
    b.button(text="❌ Нет", callback_data="recog:no")
    b.adjust(1)
    return b.as_markup()


def kb_intro_next() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Дальше", callback_data="intro:next")
    b.adjust(1)
    return b.as_markup()


def kb_program_ack() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Понятно", callback_data="intro:program")
    b.adjust(1)
    return b.as_markup()


def kb_program_show() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Показать программу", callback_data="intro:show")
    b.adjust(1)
    return b.as_markup()


def kb_skill_actions() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Сделал", callback_data="skill:done")
    b.button(text="⏱️ Ещё 5 минут", callback_data="skill:again")
    b.adjust(1)
    return b.as_markup()


def kb_main_cta() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Хочу в группу", callback_data="cta:join")
    b.button(text="📞 Созвон", callback_data="cta:call")
    b.button(text="🩺 Консультация", callback_data="cta:consult")
    b.button(text="❓ Есть вопрос", callback_data="cta:ask")
    b.adjust(1)
    return b.as_markup()


def kb_formats() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Групповой (8 недель)", callback_data="fmt:group")
    b.button(text="Асинхронный в боте", callback_data="fmt:async")
    b.button(text="С куратором", callback_data="fmt:coach")
    b.adjust(1)
    return b.as_markup()


def kb_format_actions(fmt_key: str) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    label = get_format_label(fmt_key)
    b.button(text=f"📝 Записаться ({label})", callback_data="cta:join")
    b.button(text="❓ У меня есть вопросы", callback_data="faq:open")
    b.adjust(1)
    return b.as_markup()


def kb_contact_type() -> types.ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Telegram")
    b.button(text="WhatsApp")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_question_options() -> types.ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="созвон")
    b.button(text="нет")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_diag_next() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Посмотреть программу", callback_data="diag:next")
    b.adjust(1)
    return b.as_markup()


def kb_call_or_next() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📞 Созвон", callback_data="cta:consult")
    b.button(text="🚀 Посмотреть программу", callback_data="diag:next")
    b.adjust(1)
    return b.as_markup()


def kb_offer_next() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Посмотреть программу", callback_data="offer:next")
    b.adjust(1)
    return b.as_markup()


def kb_offer_primary() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Посмотреть программу", callback_data="offer:next")
    b.button(text="✍ Записаться", callback_data="cta:join")
    b.button(text="❓ Задать вопрос", callback_data="cta:ask")
    b.adjust(1)
    return b.as_markup()


def kb_faq_short() -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Нужен ли тренинг?", callback_data="faq:need")
    b.button(text="Как помогает?", callback_data="faq:help")
    b.button(text="Какой подход?", callback_data="faq:approach")
    b.button(text="Доступ после курса?", callback_data="faq:access")
    b.button(text="Я не нашёл ответов", callback_data="faq:noanswer")
    b.button(text="🚀 Посмотреть программу", callback_data="faq:next")
    b.adjust(1)
    return b.as_markup()


# =========================
# FSM
# =========================
class Lead(StatesGroup):
    test_q1 = State()
    test_q2 = State()
    test_q3 = State()
    diag1 = State()
    diag2 = State()
    contact_type = State()
    contact_value = State()
    name = State()
    country = State()
    question = State()


# =========================
# START
# =========================
@router.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(TEST_INTRO_TEXT, reply_markup=kb_start_test())
    await message.answer_photo(photo=FSInputFile("image/1th.png"))


@router.callback_query(F.data == "test:start")
async def on_test_start(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Lead.test_q1)
    await call.message.answer(TEST_Q1, reply_markup=kb_test_q1())
    await call.answer()


@router.callback_query(F.data.startswith("t1:"))
async def on_test_q1(call: types.CallbackQuery, state: FSMContext):
    selected = call.data.split(":", 1)[1]
    await state.update_data(selected_state=selected)
    await state.set_state(Lead.test_q2)
    await call.message.answer(TEST_Q2, reply_markup=kb_test_q2())
    await call.answer()


@router.callback_query(F.data.startswith("t2:"))
async def on_test_q2(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(diag1=call.data.split(":", 1)[1])
    await state.set_state(Lead.test_q3)
    await call.message.answer(TEST_Q3, reply_markup=kb_test_q3())
    await call.answer()


@router.callback_query(F.data.startswith("t3:"))
async def on_test_q3(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(diag2=call.data.split(":", 1)[1])
    await send_test_result(call.message, state)
    await call.answer()


@router.callback_query(F.data == "skill:microstart")
async def on_skill_microstart(call: types.CallbackQuery, state: FSMContext):
    # Быстро убираем индикатор загрузки, чтобы не было задержки
    await call.answer()

    await call.message.answer(
        "🧠 Навык «микро-старт» (5 минут):\n"
        "1) Выберите один микрошаг задачи (до 2 минут).\n"
        "2) Делайте только его. Таймер 5 минут пошёл.\n"
        "3) По сигналу жмите «Сделал» или запускайте ещё 5 минут.",
        reply_markup=kb_skill_actions(),
    )
    schedule_skill_timer(call.message.chat.id)


@router.callback_query(F.data == "skill:done")
async def on_skill_done(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    cancel_skill_timer(call.message.chat.id)
    await call.message.answer(RECOGNITION_TEXT)
    await call.message.answer(HOST_INTRO_TEXT, reply_markup=kb_intro_next())


@router.callback_query(F.data == "intro:next")
async def on_intro_next(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(PROGRAM_INSIDE_TEXT, reply_markup=kb_program_ack())


@router.callback_query(F.data == "intro:program")
async def on_intro_program(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(SOCIAL_PROOF_TEXT, reply_markup=kb_program_show())


@router.callback_query(F.data == "intro:show")
async def on_intro_show(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await send_offer(call.message, state)


async def send_test_result(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("selected_state", "mix")
    memo = STATE_TO_MEMO.get(key, "Ок.")

    # Показываем тип прокрастинации сразу после теста
    memo_title = memo.split("\n", 1)[0].replace("*", "").strip()

    await message.answer(f"Похоже, у вас {memo_title}.")
    await message.answer(memo)
    await message.answer(TEST_RESULT_TEXT)
    await message.answer(
        "Навык: микро-старт\n\n"
        "выберите самую лёгкую задачу\n"
        "и сделайте только первый шаг.\n\n"
        "Не «написать отчёт»,\n"
        "а «открыть документ».\n\n"
        "После того как попробуете — жмите кнопку ниже, продолжим.",
        reply_markup=kb_try_skill(),
    )


async def send_offer(message: types.Message, state: FSMContext):
    await state.update_data(offer_sent=True, offer_actions_shown=False)
    await message.answer_photo(photo=FSInputFile("image/2th.png"))
    await message.answer(PITCH_TEXT)
    await message.answer(SELF_ASSESSMENT_TEXT)
    await message.answer(OFFER_TEXT, reply_markup=kb_offer_primary())

    await message.answer(
        "Есть также:\n"
        "— асинхронный\n"
        "— индивидуальный\n\n"
        "Выберите формат ниже — отправлю детали и как записаться.",
        reply_markup=kb_formats(),
    )


async def send_skill_reminder(chat_id: int) -> None:
    try:
        await asyncio.sleep(300)
        await bot.send_message(
            chat_id,
            "⏰ 5 минут прошло. Если закончили — жмите «Сделал». Нужен ещё раунд — «Ещё 5 минут».",
            reply_markup=kb_skill_actions(),
        )
    except asyncio.CancelledError:
        # Таймер отменён, если человек пошёл дальше
        return
    except Exception:
        log.exception("skill reminder failed")


async def send_lead_reminder(chat_id: int) -> None:
    try:
        await asyncio.sleep(3600)
        await bot.send_message(
            chat_id,
            "Напоминаю: если что-то не успели заполнить в заявке — можно дописать сюда. Я на связи!",
        )
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("lead reminder failed")


# =========================
# STATE SELECT -> MEMО -> DIAG -> CTA
# =========================
@router.callback_query(F.data.startswith("state:"))
async def on_state(call: types.CallbackQuery, state: FSMContext):
    try:
        # Прячем вопрос с кнопками, чтобы не висел в чате
        await call.message.delete()
    except Exception:
        # Если удалить нельзя (ограничения Telegram) — хотя бы убираем кнопки
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    key = call.data.split(":", 1)[1]
    await state.update_data(selected_state=key)
    memo = STATE_TO_MEMO.get(key, "Ок.")
    await call.message.answer(memo)
    await call.message.answer(
        "Если хотите решить это системно — жмите «🚀 Посмотреть программу».",
        reply_markup=kb_diag_next(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("d1:"))
async def on_diag1(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(diag1=call.data.split(":", 1)[1])

    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data="d2:yes")
    kb.button(text="Иногда", callback_data="d2:maybe")
    kb.button(text="Нет", callback_data="d2:no")
    kb.adjust(1)
    await state.set_state(Lead.diag2)
    await call.message.answer(DIAG_Q2, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "diag:next")
async def on_diag_next(call: types.CallbackQuery, state: FSMContext):
    cancel_skill_timer(call.message.chat.id)
    data = await state.get_data()

    if data.get("offer_sent"):
        await call.answer("Мы уже отправили предложение")
        return

    if data.get("diag1") and data.get("diag2"):
        await send_offer(call.message, state)
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Несколько месяцев", callback_data="d1:months")
    kb.button(text="Несколько лет", callback_data="d1:years")
    kb.button(text="Сколько себя помню", callback_data="d1:always")
    kb.adjust(1)
    await state.set_state(Lead.diag1)
    await call.message.answer(DIAG_Q1, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("d2:"))
async def on_diag2(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(diag2=call.data.split(":", 1)[1])
    await send_offer(call.message, state)
    await call.answer()


@router.callback_query(F.data == "offer:next")
async def on_offer_next(call: types.CallbackQuery, state: FSMContext):
    cancel_skill_timer(call.message.chat.id)
    data = await state.get_data()

    if not data.get("offer_sent"):
        await call.answer("Дождитесь предложения")
        return

    if data.get("offer_actions_shown"):
        await call.answer("Уже отправил варианты")
        return

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.update_data(offer_actions_shown=True, faq_next_sent=False)
    await call.message.answer("Выберите действие:", reply_markup=kb_main_cta())

    # Небольшая пауза, чтобы сообщения не падали одним блоком
    await asyncio.sleep(1)
    await call.message.answer("FAQ (коротко):", reply_markup=kb_faq_short())
    await call.answer()


@router.callback_query(F.data.startswith("recog:"))
async def on_recognition(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split(":", 1)[1]
    await call.answer()

    if choice != "yes":
        await call.message.answer("Ок, тогда покажу формат программы.")

    data = await state.get_data()
    if data.get("offer_sent"):
        return

    await send_offer(call.message, state)


@router.callback_query(F.data.startswith("fmt:"))
async def on_format(call: types.CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.update_data(selected_format=key)
    text = FORMAT_DETAILS.get(key)
    if text:
        await call.message.answer(text, reply_markup=kb_format_actions(key))
    else:
        await call.message.answer("Формат скоро будет добавлен.")
    await call.answer()


# =========================
# CTA HANDLERS
# =========================
@router.callback_query(F.data == "cta:ask")
async def on_ask(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(wants_call="нет", ready_to_pay="нет")
    await state.set_state(Lead.question)
    await call.message.answer(
        "Напишите ваш вопрос одним сообщением.\n"
        f"Если хотите сразу в личку — можно написать мне: {YOUR_TELEGRAM}",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    await call.answer()


@router.callback_query(F.data == "cta:join")
async def on_join(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(wants_call="нет", ready_to_pay="нет")
    await state.set_state(Lead.contact_type)
    await call.message.answer(
        "Где удобнее связаться?",
        reply_markup=kb_contact_type(),
    )
    await call.answer()


@router.callback_query(F.data == "cta:pay")
async def on_pay(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(wants_call="нет", ready_to_pay="да")
    await state.set_state(Lead.contact_type)
    await call.message.answer(
        "Понял, свяжусь для оплаты. Где удобнее связаться?",
        reply_markup=kb_contact_type(),
    )
    await call.answer()


@router.callback_query(F.data == "cta:call")
async def on_call(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(wants_call="да", ready_to_pay="нет")
    await state.set_state(Lead.contact_type)
    await call.message.answer(
        "Созвон сразу сюда: "
        f"{CONSULT_LINK}\n\n"
        "Где удобнее связаться?",
        reply_markup=kb_contact_type(),
    )
    await call.answer()


@router.callback_query(F.data == "cta:consult")
async def on_consult(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(wants_call="да", ready_to_pay="нет")
    await state.set_state(Lead.contact_type)
    await call.message.answer(
        "Ок, консультация и созвон сразу сюда: "
        f"{CONSULT_LINK}\n\n"
        "Где удобнее связаться?",
        reply_markup=kb_contact_type(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("faq:"))
async def on_faq(call: types.CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]

    if key == "next":
        await on_faq_next(call, state)
        return

    if key == "open":
        await call.message.answer("FAQ (коротко):", reply_markup=kb_faq_short())
        await call.answer()
        return

    if key == "noanswer":
        kb = InlineKeyboardBuilder()
        kb.button(text="Задать вопрос", callback_data="cta:ask")
        kb.button(text="Созвон", callback_data="cta:consult")
        kb.adjust(1)
        await call.message.answer("Чем помочь?", reply_markup=kb.as_markup())
        await call.answer()
        return

    text = FAQ_SHORT.get(key)
    if text:
        await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "faq:next")
async def on_faq_next(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("faq_next_sent"):
        await call.answer("Уже отправил продолжение")
        return

    # Если формат не выбран, напоминаем выбрать перед продолжением
    selected_format = data.get("selected_format")
    if not selected_format:
        await call.message.answer("Выберите формат, чтобы продолжить:", reply_markup=kb_formats())
        await call.answer()
        return

    await state.update_data(faq_next_sent=True)
    await call.message.answer(f"Подробнее о тренинге: {FAQ_URL}")
    await call.message.answer("Доступные форматы (кнопки ниже):", reply_markup=kb_formats())
    await call.answer()


# =========================
# LEAD FLOW
# =========================
@router.message(Lead.contact_type)
async def lead_contact_type(message: types.Message, state: FSMContext):
    ct = (message.text or "").strip().lower()
    if ct not in ["telegram", "whatsapp"]:
        await message.reply("Выберите кнопкой: Telegram или WhatsApp.", reply_markup=kb_contact_type())
        return

    await state.update_data(contact_type=ct)
    await state.set_state(Lead.contact_value)

    if ct == "telegram":
        await message.answer(
            "Ок. Напишите ваш Telegram @username (если нет — напишите «нет»).",
            reply_markup=types.ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Ок. Напишите ваш WhatsApp номер в формате +370...",
            reply_markup=types.ReplyKeyboardRemove(),
        )


@router.message(Lead.contact_value)
async def lead_contact_value(message: types.Message, state: FSMContext):
    val = (message.text or "").strip()
    data = await state.get_data()
    ct = data.get("contact_type")

    if ct == "telegram":
        if val.lower() in ["нет", "no", "-"]:
            val = message.from_user.username or ""
        if val.startswith("@"):
            val = val[1:]
        if not val:
            await message.answer("Понял. У вас нет username — я свяжусь через этот чат.")
            val = ""
    else:
        if not (val.startswith("+") and len(val) >= 8):
            await message.reply("Номер лучше в формате +370..., попробуйте ещё раз.")
            return

    await state.update_data(contact_value=val)
    await state.set_state(Lead.name)
    await message.answer("Как вас зовут?", reply_markup=types.ReplyKeyboardRemove())


@router.message(Lead.name)
async def lead_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.reply("Напишите имя (2+ символа).")
        return

    await state.update_data(name=name)
    await state.set_state(Lead.country)
    await message.answer("Из какой вы страны?")


@router.message(Lead.country)
async def lead_country(message: types.Message, state: FSMContext):
    country = (message.text or "").strip()
    if len(country) < 2:
        await message.reply("Напишите страну (2+ символа).")
        return

    await state.update_data(country=country)
    await finalize_lead_submission(message, state, q_text=None)


async def finalize_lead_submission(
    message: types.Message, state: FSMContext, q_text: Optional[str]
) -> None:
    data = await state.get_data()
    wants_call = data.get("wants_call", "нет")

    q = "нет"
    if q_text:
        low = q_text.strip().lower()
        if low in ["созвон", "call", "да, созвон", "хочу созвон", "нужен созвон"]:
            wants_call = "да"
        elif low in ["нет", "no", "-"]:
            q = "нет"
        else:
            q = q_text.strip()

    name = data.get("name", "")
    country = data.get("country", "")
    contact_type = data.get("contact_type", "")
    contact_value = data.get("contact_value", "")
    diag1 = data.get("diag1", "")
    diag2 = data.get("diag2", "")
    selected_state = data.get("selected_state", "")
    ready_to_pay = data.get("ready_to_pay", "нет")
    selected_format = data.get("selected_format", "")
    selected_format_label = get_format_label(selected_format)
    selected_state_label = get_state_label(selected_state)

    tg_username = message.from_user.username or ""
    user_id = message.from_user.id
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    admin_text = (
        "🧾 *Заявка (бот)*\n"
        f"Время: {ts}\n"
        f"Имя: {name}\n"
        f"Страна: {country}\n"
        f"Контакт: {contact_type} | {contact_value or '(пусто)'}\n"
        f"TG: @{tg_username if tg_username else '(нет)'}\n"
        f"Формат: {selected_format_label}\n"
        f"Вопрос: {q}\n"
        f"Созвон: {wants_call}\n"
        f"Готов оплатить: {ready_to_pay}\n"
        f"Состояние: {selected_state}\n"
        f"Q1: {diag1}\n"
        f"Q2: {diag2}\n"
        f"UserID: {user_id}"
    )

    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode=None)
    except Exception:
        log.exception("admin send failed")

    if sheet is not None:
        try:
            sheet.append_row(
                [
                    ts,
                    name,
                    country,
                    tg_username,
                    contact_type,
                    contact_value,
                    selected_format_label,
                    q,
                    wants_call,
                    ready_to_pay,
                    selected_state,
                    diag1,
                    diag2,
                ]
            )
        except Exception:
            log.exception("sheets append failed")

    # Запланировать напоминание, если контактные данные пусты
    if contact_value:
        cancel_lead_reminder(message.chat.id)
    else:
        schedule_lead_reminder(message.chat.id)

    await state.clear()

    summary_lines = [
        "Коротко итог:",
        f"— Формат: {selected_format_label}",
        f"— Тип прокрастинации: {selected_state_label}",
        f"— Контакт: {contact_type or '(не выбран)'} {contact_value or ''}",
    ]

    await message.answer(
        "Спасибо! Мы с Вами свяжемся в течение 24 часов.\n\n"
        + "\n".join(summary_lines)
        + "\n\n"
        f"Если нужно быстрее — напишите мне: {YOUR_TELEGRAM}\n\n"
        "Если что-то не успели заполнить — напишите, напомню через час.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@router.message(Lead.question)
async def lead_question(message: types.Message, state: FSMContext):
    await finalize_lead_submission(message, state, q_text=(message.text or ""))


# =========================
# CANCEL
# =========================
@router.message(Command("cancel"))
async def cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил. Чтобы начать заново — /start", reply_markup=types.ReplyKeyboardRemove())


# =========================
# RUN
# =========================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Provide it via environment or config.py")

    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL must be set when USE_WEBHOOK is true")

        app = web.Application()
        webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_handler.register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        await bot.set_webhook(WEBHOOK_URL)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
        await site.start()

        log.info(
            "Webhook running at %s on http://%s:%s%s",
            WEBHOOK_URL,
            WEBAPP_HOST,
            WEBAPP_PORT,
            WEBHOOK_PATH,
        )

        # Keep the server alive
        await asyncio.Event().wait()
    else:
        # чтобы не было конфликтов webhook/polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


async def _run():
    try:
        await main()
    finally:
        # Закрываем HTTP-сессию, чтобы аккуратно выключать бота
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем (Ctrl+C)")