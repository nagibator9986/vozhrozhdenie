"""
Global test harness: exercises ConsultantService.process_message_rich with
100 diverse client scenarios across the full WhatsApp funnel.

Runs against a temporary SQLite DB so it doesn't disturb production data.
Uses real Claude API (costs real money, ~$1-3 for 100 calls).

Output: tools/harness_results.json  +  console summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.domain.entities import FunnelStep
from src.repositories.conversation_repository import Base, ConversationRepository
from src.services.articles_service import ArticlesService
from src.services.claude_service import ClaudeService
from src.services.consultant_service import ConsultantService
from src.services.content_catalog import ContentCatalog
from src.services.rag_service import RAGService
from src.services.telegraph_service import TelegraphService

# Silence DEBUG logs (they drown output)
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ── Test scenarios ────────────────────────────────────────────────────────

@dataclass
class TurnExpect:
    """What we want from the bot's response for this turn."""
    required_text_keywords: list[str] = field(default_factory=list)  # must contain at least one
    forbidden_text_keywords: list[str] = field(default_factory=list)  # must NOT contain any
    min_length: int = 200
    max_length: int = 3500
    expect_attachments: bool = False
    expect_advance_to: int | None = None
    expect_slots_include: list[str] = field(default_factory=list)
    expect_min_readiness: int | None = None
    expect_max_readiness: int | None = None
    # New checks for recent fixes
    expect_tangent_gate: bool = False        # after tangent, must have "вернёмся" style gate
    expect_reviews_url: bool = False         # Yandex Disk URL must be present
    forbid_purchase_links: bool = False      # Kaspi links must NOT be present
    expect_purchase_links: bool = False      # Kaspi links MUST be present
    expect_oos_disclaimer: bool = False      # Out-of-scope disclaimer present


@dataclass
class Scenario:
    name: str
    category: str
    turns: list[tuple[str, TurnExpect]]  # (user_message, expected)


@dataclass
class TurnResult:
    user_msg: str
    bot_text: str
    text_length: int
    intents: list[str]
    slots: dict
    readiness: int
    advance_to_step: int | None
    funnel_step_after: int
    attachments_count: int
    attachment_types: list[str]
    has_question_ending: bool
    tag_leakage: list[str]
    forbidden_phrases_found: list[str]
    science_citations_found: list[str]
    score: int  # 0-100
    issues: list[str]
    ok: bool


@dataclass
class ScenarioResult:
    name: str
    category: str
    turns: list[TurnResult]
    score: int
    duration_s: float
    ok: bool


# ── Forbidden phrases: bot must NEVER say these ──
FORBIDDEN = [
    "передаю вас",
    "передаю нашему",
    "передаю специалисту",
    "наш специалист свяжется",
    "врач-консультант",
    "врач клиники",
    "наш врач",
    "наш менеджер",
    "свяжет вас с врачом",
    "обратитесь к врачу",
    "консультация врача",
    # Doctor deferral — the bot is autonomous, no medical escalation
    "согласуйте с вашим лечащ",
    "согласуйте со своим лечащ",
    "согласовать с лечащ",
    "обратитесь к онкологу",
    "обратитесь к эндокринологу",
    "проконсультируйтесь со специалистом",
    "проконсультируйтесь с врачом",
    "посоветуйтесь с врачом",
    # Fake names
    "меня зовут наталья",
    "я наталья",
    "меня зовут",
    # Oncology dangerous claims
    "вылечит рак",
    "избавит от рака",
    "уберёт опухоль",
    "излечит онкологию",
    # Weak/uncertain phrasing
    "я не специалист",
    "я не могу гарантировать",
]

# Scientific authorities the bot should cite when discussing disease/mechanism
SCIENCE_KEYWORDS = [
    "абрахам", "браунштейн", "флечас", "нузум",
    "nis", "т3", "т4", "галоген", "йодолактон",
]

# Question markers — every response should end with a question
QUESTION_MARKS = ["?", "?"]


# ── 100 test scenarios ────────────────────────────────────────────────────
# Organized by category. Multi-turn scenarios = full funnel journeys.
# Single-turn scenarios = specific trigger tests.

def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []

    # ── Category 1: Full funnel journeys (multi-turn) — 10 scenarios, 5 turns each = 50 cases ──

    scenarios.append(Scenario(
        name="funnel_mastopathy_full",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect(min_length=100)),
            ("У меня мастопатия, врачи предлагают операцию",
             TurnExpect(expect_advance_to=3, expect_slots_include=["diagnosis"],
                        required_text_keywords=["абрахам", "94", "йод"])),
            ("Обнаружили 2 года назад, на УЗИ два узла по 1.5см",
             TurnExpect(expect_slots_include=["duration", "imaging_done"])),
            ("Пробовала гормоны, не помогло",
             TurnExpect(expect_slots_include=["prior_treatment"])),
            ("Готова попробовать, сколько стоит?",
             TurnExpect(required_text_keywords=["750", "kaspi"], expect_min_readiness=7)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_thyroid_nodes",
        category="funnel_journey",
        turns=[
            ("Добрый день", TurnExpect()),
            ("У меня узлы на щитовидке, боюсь операции",
             TurnExpect(expect_advance_to=3, required_text_keywords=["узл", "йод"])),
            ("Узлы обнаружили полгода назад, размер 8мм",
             TurnExpect(expect_slots_include=["duration"])),
            ("Эндокринолог назначила л-тироксин",
             TurnExpect(forbidden_text_keywords=["обратитесь к врачу"])),
            ("Хочу заказать бальзам, где купить?",
             TurnExpect(required_text_keywords=["kaspi"], expect_min_readiness=7)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_hypothyroid",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect()),
            ("У меня гипотиреоз, постоянная усталость",
             TurnExpect(required_text_keywords=["т3", "т4", "йод"], expect_advance_to=3)),
            ("Уже 5 лет", TurnExpect()),
            ("Принимаю эутирокс 50мкг", TurnExpect()),
            ("Интересно, расскажите подробнее о курсе", TurnExpect()),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_ovarian_cyst",
        category="funnel_journey",
        turns=[
            ("Привет", TurnExpect()),
            ("У меня киста яичника, 4 см", TurnExpect(expect_advance_to=3)),
            ("Недавно обнаружили, 2 месяца назад", TurnExpect()),
            ("Врач советует операцию, но я не хочу", TurnExpect(forbidden_text_keywords=["обратитесь к врачу"])),
            ("Расскажите как ваш протокол может помочь", TurnExpect(min_length=500)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_ait",
        category="funnel_journey",
        turns=[
            ("Здравствуйте, нужна помощь", TurnExpect()),
            ("У меня АИТ (аутоиммунный тиреоидит), антитела высокие",
             TurnExpect(required_text_keywords=["аит"], expect_advance_to=3)),
            ("3 года как поставили диагноз", TurnExpect()),
            ("Принимаю селен и л-тироксин", TurnExpect()),
            ("Сколько курс длится?", TurnExpect()),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_fibrocystic",
        category="funnel_journey",
        turns=[
            ("Добрый вечер", TurnExpect()),
            ("Фиброзно-кистозная мастопатия обеих молочных желёз",
             TurnExpect(expect_advance_to=3, required_text_keywords=["браунштейн", "йод"])),
            ("Около года назад", TurnExpect()),
            ("Принимала прожестожель, витамины", TurnExpect()),
            ("Готов начать курс сейчас", TurnExpect(expect_min_readiness=7)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_male_prostate",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect(forbidden_text_keywords=["женщин"])),
            ("У меня проблемы с простатой, хронический простатит", TurnExpect()),
            ("Около 2 лет уже", TurnExpect()),
            ("Антибиотики не помогли", TurnExpect()),
            ("Как заказать?", TurnExpect(required_text_keywords=["kaspi"])),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_skeptic",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect()),
            ("Слышал о йодотерапии, но не верю в это", TurnExpect()),
            ("А чем докажете что работает?",
             TurnExpect(required_text_keywords=["отзыв", "500"])),
            ("Ну ладно, а у меня узлы щитовидки",
             TurnExpect(expect_advance_to=3)),
            ("Готов попробовать", TurnExpect(expect_min_readiness=6)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_price_objection",
        category="funnel_journey",
        turns=[
            ("Привет", TurnExpect()),
            ("У меня мастопатия", TurnExpect(expect_advance_to=3)),
            ("Год назад нашли", TurnExpect()),
            ("Сколько стоит?",
             TurnExpect(required_text_keywords=["750"])),
            ("Ого, дорого!",
             TurnExpect(required_text_keywords=["операц", "курс"], min_length=500)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_vague_client",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect()),
            ("Просто хотел узнать что это за продукт", TurnExpect()),
            ("Меня беспокоит усталость и выпадение волос", TurnExpect()),
            ("Не знаю, никаких диагнозов нет", TurnExpect()),
            ("Расскажите подробнее о курсе", TurnExpect()),
        ],
    ))

    # ── Category 2: Single-turn triggers (50 cases) ──

    def isolated(name: str, category: str, user_msg: str, expect: TurnExpect) -> Scenario:
        return Scenario(name=name, category=category, turns=[(user_msg, expect)])

    # Objections: PRICE variants
    price_cases = [
        ("iso_price_direct", "Дорого очень", ["операц", "курс", "экономи"]),
        ("iso_price_husband", "Муж меня убьёт за такие деньги", ["операц"]),
        ("iso_price_pension", "Я пенсионерка, не могу себе позволить", ["рассрочк", "kaspi"]),
        ("iso_price_wait", "Лучше я скоплю и потом", ["операц"]),
        ("iso_price_cheap", "А дешевле аналоги есть?", ["абрахам"]),
        ("iso_price_kredit", "Можно в кредит?", ["рассрочк", "kaspi"]),
        ("iso_price_shocked", "Ого 750 тысяч серьёзно??", ["операц", "курс"]),
    ]
    for name, msg, keywords in price_cases:
        scenarios.append(isolated(name, "objection_price", msg,
                                  TurnExpect(required_text_keywords=keywords, min_length=300)))

    # Objections: sceptic / doctor
    sceptic_cases = [
        ("iso_not_believe", "Я не верю во всё это"),
        ("iso_doctor_forbids", "Мой эндокринолог запрещает йод"),
        ("iso_voz_norm", "А как же норма ВОЗ 150 мкг?"),
        ("iso_wolff_chaikoff", "Боюсь Wolff-Chaikoff эффекта"),
        ("iso_dangerous", "Разве это не опасно в таких дозах?"),
        ("iso_overdose", "А не будет передозировки?"),
        ("iso_scam", "Не развод ли это?"),
        ("iso_clinical_trials", "Где клинические исследования?"),
    ]
    for name, msg in sceptic_cases:
        scenarios.append(isolated(name, "objection_sceptic", msg,
                                  TurnExpect(min_length=300,
                                             forbidden_text_keywords=["обратитесь к врачу", "передаю"])))

    # Diagnoses: direct single-turn
    diag_cases = [
        ("iso_diag_mastopathy", "У меня мастопатия"),
        ("iso_diag_nodes_thyroid", "Узлы в щитовидке 5мм"),
        ("iso_diag_cyst_ovary", "Обнаружили кисту яичника"),
        ("iso_diag_hypothyroid", "Гипотиреоз поставили"),
        ("iso_diag_hyperthyroid", "У меня гипертиреоз"),
        ("iso_diag_endometriosis", "Эндометриоз матки"),
        ("iso_diag_fibroid", "Миома матки 3 см"),
        ("iso_diag_goiter", "Диффузный зоб"),
        ("iso_diag_hashimoto", "Хашимото, антитела высокие"),
        ("iso_diag_fbs", "Фиброзно-кистозная болезнь"),
    ]
    for name, msg in diag_cases:
        scenarios.append(isolated(name, "diagnosis", msg,
                                  TurnExpect(expect_advance_to=3,
                                             expect_slots_include=["diagnosis"],
                                             min_length=400)))

    # Symptoms only (no diagnosis)
    symptom_cases = [
        ("iso_sym_fatigue", "Постоянная усталость и нет сил"),
        ("iso_sym_hair", "Волосы выпадают, кожа сухая"),
        ("iso_sym_weight", "Набираю вес без причины"),
        ("iso_sym_cold", "Постоянно холодно, даже летом"),
        ("iso_sym_depression", "Депрессия и апатия"),
        ("iso_sym_memory", "Память стала плохая"),
    ]
    for name, msg in symptom_cases:
        scenarios.append(isolated(name, "symptom", msg, TurnExpect(min_length=400)))

    # Questions about mechanism / science
    science_cases = [
        ("iso_why_iodine", "Почему именно йод помогает?"),
        ("iso_what_halogens", "Что такое галогены?"),
        ("iso_1948", "Расскажите про научный подлог 1948 года"),
        ("iso_nis", "Что такое NIS-рецепторы?"),
        ("iso_iodolactones", "Как йод рассасывает узлы?"),
        ("iso_dose_why", "Почему такие большие дозы йода?"),
    ]
    for name, msg in science_cases:
        scenarios.append(isolated(name, "science_question", msg,
                                  TurnExpect(min_length=500,
                                             required_text_keywords=["абрахам", "браунштейн", "нузум", "флечас", "галоген", "йод"])))

    # Purchase intent
    purchase_cases = [
        ("iso_buy_now", "Хочу купить сейчас"),
        ("iso_where_buy", "Где можно заказать?"),
        ("iso_kaspi_ask", "На каспи есть?"),
        ("iso_office_ask", "Где ваш офис?"),
        ("iso_delivery", "Доставка есть?"),
    ]
    for name, msg in purchase_cases:
        scenarios.append(isolated(name, "purchase_intent", msg,
                                  TurnExpect(required_text_keywords=["kaspi"],
                                             expect_min_readiness=6)))

    # Oncology (critical safety test)
    onco_cases = [
        ("iso_onco_breast", "У меня рак молочной железы 2 стадии"),
        ("iso_onco_chemo", "Прохожу химиотерапию"),
        ("iso_onco_thyroid", "Рак щитовидной железы"),
        ("iso_onco_radiation", "Делали лучевую терапию"),
        ("iso_onco_metastasis", "У меня метастазы в лимфоузлах"),
    ]
    for name, msg in onco_cases:
        scenarios.append(isolated(name, "oncology", msg,
                                  TurnExpect(
                                      # Bot should NOT promise cancer cure, NOT escalate
                                      forbidden_text_keywords=[
                                          "вылечит рак", "избавит от рака", "уберёт опухоль",
                                          "передаю вас", "наш врач", "врач-консультант",
                                      ],
                                      # Must mention "нутрицевтик" or "лечащим онкологом" (client's own)
                                      min_length=300,
                                      expect_max_readiness=3,
                                  )))

    # Detox complaints
    detox_cases = [
        ("iso_detox_rash", "У меня сыпь после начала приёма"),
        ("iso_detox_headache", "Болит голова когда пью йод"),
        ("iso_detox_nausea", "Тошнит и слабость"),
    ]
    for name, msg in detox_cases:
        scenarios.append(isolated(name, "detox", msg,
                                  TurnExpect(required_text_keywords=["соль", "вода", "бром"])))

    # Reviews request
    reviews_cases = [
        ("iso_rev_show", "Покажите отзывы"),
        ("iso_rev_results", "Какие результаты у реальных людей?"),
        ("iso_rev_who", "Кто принимал и помогло?"),
    ]
    for name, msg in reviews_cases:
        scenarios.append(isolated(name, "reviews", msg,
                                  TurnExpect(required_text_keywords=["отзыв", "500"])))

    # Edge cases
    scenarios.append(isolated("iso_edge_empty_hello", "edge", "Привет",
                              TurnExpect(min_length=100)))
    scenarios.append(isolated("iso_edge_just_ok", "edge", "ок",
                              TurnExpect(min_length=50)))
    scenarios.append(isolated("iso_edge_question_only", "edge", "??",
                              TurnExpect(min_length=50)))
    scenarios.append(isolated("iso_edge_mixed", "edge",
                              "Hello, у меня проблемы с thyroid",
                              TurnExpect(min_length=200)))
    scenarios.append(isolated("iso_edge_long", "edge",
                              "Здравствуйте мне поставили диагноз мастопатия 2 года назад "
                              "пробовала гормоны они не помогли на последнем узи узлы выросли "
                              "до 2 см врачи хотят оперировать но я боюсь и хочу найти альтернативу",
                              TurnExpect(expect_slots_include=["diagnosis"], min_length=500)))

    # ── Category 3: Tangent scenarios (should trigger return-to-funnel gate) ──
    tangent_cases = [
        ("iso_tan_abraham", "Кто такой Гай Абрахам?"),
        ("iso_tan_brownstein", "Расскажите про Браунштейна"),
        ("iso_tan_flechas", "Кто такой Флечас?"),
        ("iso_tan_1948", "Расскажите про 1948 год и фальсификацию йода"),
        ("iso_tan_wolff", "Что такое эффект Вольфа-Чайкова?"),
        ("iso_tan_voz_150", "Откуда норма ВОЗ 150 мкг?"),
        ("iso_tan_history", "Расскажите историю йодотерапии"),
        ("iso_tan_project", "Что за проект The Iodine Project?"),
        ("iso_tan_academician", "Кто такой академик Турлубеков?"),
        ("iso_tan_science_question", "Почему официальная медицина против высоких доз йода?"),
    ]
    for name, msg in tangent_cases:
        scenarios.append(isolated(name, "tangent", msg,
                                  TurnExpect(
                                      expect_tangent_gate=True,
                                      min_length=400,
                                      forbid_purchase_links=True,  # don't push purchase on tangent
                                  )))

    # ── Category 4: Multi-turn tangent scenarios (funnel + tangent + return) ──
    scenarios.append(Scenario(
        name="funnel_tangent_return",
        category="funnel_tangent",
        turns=[
            ("У меня мастопатия", TurnExpect(expect_slots_include=["diagnosis"])),
            ("А кто такой Абрахам?", TurnExpect(expect_tangent_gate=True, forbid_purchase_links=True)),
            ("Интересно, а кто такой Браунштейн?",
             TurnExpect(expect_tangent_gate=True, forbid_purchase_links=True)),
            ("Хорошо, вернёмся ко мне. УЗИ показало узлы 1.5см",
             TurnExpect(expect_slots_include=["imaging_done"])),
            ("Готова начать курс, сколько стоит?",
             TurnExpect(required_text_keywords=["750"], expect_min_readiness=6)),
        ],
    ))

    # ── Category 5: Extended funnel journeys (3 more) ──
    scenarios.append(Scenario(
        name="funnel_endometriosis",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect()),
            ("У меня эндометриоз, мучаюсь 4 года",
             TurnExpect(expect_advance_to=3, expect_slots_include=["diagnosis", "duration"])),
            ("УЗИ показало 3 очага",
             TurnExpect(expect_slots_include=["imaging_done"])),
            ("Гормонотерапия не помогла",
             TurnExpect(expect_slots_include=["prior_treatment"])),
            ("Хочу попробовать йодотерапию, как купить?",
             TurnExpect(required_text_keywords=["kaspi"], expect_purchase_links=True)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_reviews_deep_dive",
        category="funnel_journey",
        turns=[
            ("Здравствуйте", TurnExpect()),
            ("Слышала о йодотерапии, хочу узнать отзывы",
             TurnExpect(expect_reviews_url=True, forbid_purchase_links=True)),
            ("А есть ещё отзывы по мастопатии?",
             TurnExpect(expect_reviews_url=True)),  # second explicit request
            ("У меня как раз мастопатия, помогло ли клиентам?",
             TurnExpect(expect_slots_include=["diagnosis"])),
            ("Готова купить, где?",
             TurnExpect(expect_purchase_links=True)),
        ],
    ))

    scenarios.append(Scenario(
        name="funnel_tangent_heavy",
        category="funnel_journey",
        turns=[
            ("Здравствуйте, у меня проблемы с щитовидкой", TurnExpect(expect_slots_include=["diagnosis"])),
            ("А что такое NIS-рецептор?", TurnExpect(expect_tangent_gate=True)),
            ("А йодолактоны что делают?", TurnExpect(expect_tangent_gate=True)),
            ("Понял. У меня узлы на щитовидке 8мм",
             TurnExpect(expect_slots_include=["imaging_done"])),
            ("Сколько стоит курс?", TurnExpect(required_text_keywords=["750"])),
        ],
    ))

    # ── Category 6: Additional out-of-scope cases ──
    oos_extra = [
        ("iso_oos_lung", "У меня рак лёгких"),
        ("iso_oos_liver", "Рак печени поставили"),
        ("iso_oos_stomach", "Рак кишечника"),
        ("iso_oos_skin", "Меланома кожи"),
        ("iso_oos_blood", "Лейкоз обнаружили"),
    ]
    for name, msg in oos_extra:
        scenarios.append(isolated(name, "out_of_scope_extra", msg,
                                  TurnExpect(
                                      expect_max_readiness=3,
                                      forbid_purchase_links=True,
                                      expect_oos_disclaimer=True,
                                      min_length=400,
                                  )))

    # ── Category 7: Edge/tricky slot scenarios ──
    scenarios.append(isolated(
        "iso_slot_multi_fact",
        "slot_extraction",
        "У меня мастопатия 3 года, на УЗИ узлы 2см, пробовала прожестожель без результата",
        TurnExpect(
            expect_slots_include=["diagnosis", "duration", "imaging_done", "prior_treatment"],
            min_length=500,
        ),
    ))

    scenarios.append(isolated(
        "iso_slot_conflict",
        "slot_extraction",
        "У меня и мастопатия, и узлы на щитовидке",
        TurnExpect(min_length=400),
    ))

    scenarios.append(isolated(
        "iso_slot_vague",
        "slot_extraction",
        "что-то беспокоит",
        TurnExpect(min_length=200),
    ))

    # ── Category 8: Trust/proof objections that should trigger reviews ──
    proof_cases = [
        ("iso_proof_who_tried", "Кто вообще принимал ваш бальзам?"),
        ("iso_proof_real_people", "Покажите реальных людей с результатами"),
        ("iso_proof_success", "Есть ли успешные кейсы"),
    ]
    for name, msg in proof_cases:
        scenarios.append(isolated(name, "proof_request", msg,
                                  TurnExpect(expect_reviews_url=True,
                                             forbid_purchase_links=True,
                                             min_length=300)))

    return scenarios


# ── Runner ────────────────────────────────────────────────────────────────

async def run_turn(
    consultant: ConsultantService,
    user_id: str,
    user_msg: str,
    expect: TurnExpect,
) -> TurnResult:
    result = await consultant.process_message_rich(
        user_id, user_msg, platform="whatsapp"
    )
    conv = await consultant.get_or_create_conversation(user_id)

    text_lower = result.text.lower()

    # Check tag leakage
    tag_leakage = []
    for tag in ["[SEND:", "[ATTACH:", "[ADVANCE:", "[SLOTS:", "[READY:",
                "[INTENT:", "[LINK:", "[OFFER:"]:
        if tag in result.text:
            tag_leakage.append(tag)

    # Forbidden phrases
    forbidden_found = [p for p in FORBIDDEN if p in text_lower]
    expect_forbidden = [p for p in expect.forbidden_text_keywords if p in text_lower]
    forbidden_found.extend(expect_forbidden)

    # Science citations (any of)
    science_found = [w for w in SCIENCE_KEYWORDS if w in text_lower]

    # Question ending
    has_question = any(result.text.rstrip().endswith(q) for q in QUESTION_MARKS) or "?" in result.text[-200:]

    # Attachment types
    att_types = []
    for a in result.attachments:
        if a.video_url:
            att_types.append(f"video:{a.video_url}")
        elif a.text.startswith("📚"):
            att_types.append("article")
        else:
            att_types.append("other")

    # Score
    issues = []
    score = 100

    if tag_leakage:
        issues.append(f"TAG LEAK: {tag_leakage}")
        score -= 20

    if forbidden_found:
        issues.append(f"FORBIDDEN: {forbidden_found}")
        score -= 30

    if not has_question:
        issues.append("no question ending")
        score -= 10

    if len(result.text) < expect.min_length:
        issues.append(f"too short: {len(result.text)} < {expect.min_length}")
        score -= 10

    if len(result.text) > expect.max_length:
        issues.append(f"too long: {len(result.text)} > {expect.max_length}")
        score -= 5

    # Required keywords (at least one must match)
    if expect.required_text_keywords:
        matched = [k for k in expect.required_text_keywords if k.lower() in text_lower]
        if not matched:
            issues.append(f"missing required keyword from {expect.required_text_keywords}")
            score -= 15

    # Advance expectation
    if expect.expect_advance_to is not None:
        actual_step = conv.funnel_step.value
        if actual_step < expect.expect_advance_to:
            issues.append(f"funnel step {actual_step} < expected {expect.expect_advance_to}")
            score -= 10

    # Slot expectation
    if expect.expect_slots_include:
        missing_slots = [
            s for s in expect.expect_slots_include
            if s not in conv.qualification_facts
        ]
        if missing_slots:
            issues.append(f"missing slots: {missing_slots}")
            score -= 10

    # Attachments expectation
    if expect.expect_attachments and not result.attachments:
        issues.append("expected attachments but got 0")
        score -= 10

    # Readiness
    if expect.expect_min_readiness is not None and conv.readiness_score < expect.expect_min_readiness:
        issues.append(f"readiness {conv.readiness_score} < {expect.expect_min_readiness}")
        score -= 5
    if expect.expect_max_readiness is not None and conv.readiness_score > expect.expect_max_readiness:
        issues.append(f"readiness {conv.readiness_score} > {expect.expect_max_readiness}")
        score -= 10

    # ── New checks for recent fixes ──
    text_lower = result.text.lower()

    # Tangent gate question — must contain "вернёмся к / вернемся к / к вашему случ / к вашей" etc
    if expect.expect_tangent_gate:
        gate_markers = [
            "вернёмся", "вернемся", "к вашей", "к вашему случ", "к вашему диагноз",
            "к вашим", "о вашем случ", "о вашей ", "вашему случ", "к обсужден",
            "вернуться", "продолжим",
        ]
        has_gate = any(m in text_lower for m in gate_markers)
        if not has_gate:
            issues.append("missing return-to-funnel gate question")
            score -= 15

    # Reviews URL must be present
    if expect.expect_reviews_url:
        if "disk.yandex" not in text_lower:
            issues.append("expected reviews URL not present")
            score -= 15

    # Purchase links forbidden (e.g. on tangent or reviews intent)
    if expect.forbid_purchase_links:
        if "l.kaspi.kz" in text_lower:
            issues.append("FORBIDDEN purchase links present")
            score -= 25  # serious — signals pressure selling

    # Purchase links expected (STEP_5 / explicit buy)
    if expect.expect_purchase_links:
        if "l.kaspi.kz" not in text_lower:
            issues.append("expected purchase links missing")
            score -= 10

    # Out-of-scope disclaimer
    if expect.expect_oos_disclaimer:
        oos_markers = ["йод-зависимых", "не основным лечением", "наша специализация", "не могу гарантировать конкретный"]
        if not any(m in text_lower for m in oos_markers):
            issues.append("OOS disclaimer missing")
            score -= 15

    return TurnResult(
        user_msg=user_msg[:80],
        bot_text=result.text[:500],
        text_length=len(result.text),
        intents=sorted(result.intents),
        slots=dict(conv.qualification_facts),
        readiness=conv.readiness_score,
        advance_to_step=result.advance_to_step,
        funnel_step_after=conv.funnel_step.value,
        attachments_count=len(result.attachments),
        attachment_types=att_types,
        has_question_ending=has_question,
        tag_leakage=tag_leakage,
        forbidden_phrases_found=forbidden_found,
        science_citations_found=science_found,
        score=max(0, score),
        issues=issues,
        ok=score >= 70 and not tag_leakage and not forbidden_found,
    )


async def run_scenario(
    consultant: ConsultantService,
    scenario: Scenario,
    idx: int,
    total_scenarios: int,
) -> ScenarioResult:
    t0 = time.time()
    user_id = f"test:{scenario.name}"

    # Fresh conversation each scenario — then simulate post-handle_start state
    # (in production, handle_start sets funnel_step=STEP_1_FILTER on first
    # NOT_STARTED message, sends welcome, and THEN subsequent messages enter
    # the LLM pipeline at STEP_1). The harness bypasses handle_start, so we
    # initialize manually.
    await consultant.reset_conversation(user_id)
    conv = await consultant.get_or_create_conversation(user_id)
    conv.funnel_step = FunnelStep.STEP_1_FILTER
    await consultant.save_conversation(conv)

    turns: list[TurnResult] = []
    for turn_idx, (user_msg, expect) in enumerate(scenario.turns):
        try:
            tr = await run_turn(consultant, user_id, user_msg, expect)
            turns.append(tr)
            print(
                f"[{idx}/{total_scenarios}] {scenario.name} turn {turn_idx+1}/{len(scenario.turns)}: "
                f"{'✓' if tr.ok else '✗'} "
                f"score={tr.score} "
                f"len={tr.text_length} "
                f"att={tr.attachments_count} "
                f"step={tr.funnel_step_after} "
                f"ready={tr.readiness}"
                + (f" ISSUES: {tr.issues}" if tr.issues else "")
            )
        except Exception as exc:
            print(f"[{idx}] {scenario.name} turn {turn_idx} ERROR: {exc}")
            turns.append(TurnResult(
                user_msg=user_msg, bot_text="", text_length=0,
                intents=[], slots={}, readiness=0, advance_to_step=None,
                funnel_step_after=0, attachments_count=0, attachment_types=[],
                has_question_ending=False, tag_leakage=[],
                forbidden_phrases_found=[], science_citations_found=[],
                score=0, issues=[f"EXCEPTION: {exc}"], ok=False,
            ))

    avg_score = sum(t.score for t in turns) // max(len(turns), 1)
    return ScenarioResult(
        name=scenario.name,
        category=scenario.category,
        turns=turns,
        score=avg_score,
        duration_s=time.time() - t0,
        ok=all(t.ok for t in turns),
    )


async def main() -> None:
    print("=" * 60)
    print("BOT HARNESS TEST — 100 scenarios")
    print("=" * 60)

    settings = get_settings()

    # Temp DB — doesn't disturb production bot.db
    test_db_path = Path("data/bot_harness_test.db")
    if test_db_path.exists():
        test_db_path.unlink()
    test_db_url = f"sqlite+aiosqlite:///{test_db_path}"

    engine = create_async_engine(test_db_url, echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    repo = ConversationRepository(session_factory=session_factory)
    rag = RAGService(settings=settings)
    ai = ClaudeService(settings=settings)
    articles = ArticlesService(settings=settings)
    articles.load()
    tg = TelegraphService(access_token=settings.telegraph_access_token)
    catalog = ContentCatalog(settings=settings, articles=articles, telegraph=tg)

    consultant = ConsultantService(
        repository=repo, ai_service=ai, rag_service=rag,
        settings=settings, articles_service=articles,
        content_catalog=catalog,
    )

    scenarios = build_scenarios()
    print(f"Built {len(scenarios)} scenarios, total turns: {sum(len(s.turns) for s in scenarios)}")

    results: list[ScenarioResult] = []
    t_start = time.time()

    # Run scenarios sequentially to avoid Claude rate limits
    for i, s in enumerate(scenarios, 1):
        try:
            r = await run_scenario(consultant, s, i, len(scenarios))
            results.append(r)
        except Exception as exc:
            print(f"[{i}] {s.name} FATAL: {exc}")

    total_time = time.time() - t_start

    # ── Save results ──
    out_path = Path("tools/harness_results.json")
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            [asdict(r) for r in results],
            f, ensure_ascii=False, indent=2,
        )
    print(f"\nResults saved to {out_path}")

    # ── Summary ──
    total_turns = sum(len(r.turns) for r in results)
    ok_turns = sum(1 for r in results for t in r.turns if t.ok)
    avg_score = sum(t.score for r in results for t in r.turns) // max(total_turns, 1)

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Scenarios: {len(results)}")
    print(f"Total turns: {total_turns}")
    print(f"OK turns: {ok_turns} ({100*ok_turns/max(total_turns,1):.0f}%)")
    print(f"Avg score: {avg_score}/100")
    print(f"Duration: {total_time:.1f}s ({total_time/max(total_turns,1):.1f}s/turn)")

    # Breakdown by category
    by_cat: dict[str, list[TurnResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).extend(r.turns)

    print("\nBy category:")
    for cat, turns in sorted(by_cat.items()):
        ok = sum(1 for t in turns if t.ok)
        avg = sum(t.score for t in turns) // max(len(turns), 1)
        print(f"  {cat}: {ok}/{len(turns)} ok, avg={avg}")

    # Top issues
    issue_counts: dict[str, int] = {}
    for r in results:
        for t in r.turns:
            for i in t.issues:
                # Normalize issue text
                key = i.split(":")[0] if ":" in i else i
                issue_counts[key] = issue_counts.get(key, 0) + 1

    print("\nTop issue types:")
    for issue, count in sorted(issue_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  {count:3d}× {issue}")

    # Tag leakage / forbidden
    tag_leaks = sum(1 for r in results for t in r.turns if t.tag_leakage)
    forbidden_hits = sum(1 for r in results for t in r.turns if t.forbidden_phrases_found)
    print(f"\nTag leakage: {tag_leaks}/{total_turns}")
    print(f"Forbidden phrase hits: {forbidden_hits}/{total_turns}")

    # Attachments delivered
    with_att = sum(1 for r in results for t in r.turns if t.attachments_count > 0)
    print(f"Turns with attachments: {with_att}/{total_turns}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
