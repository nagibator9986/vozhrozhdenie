"""
Compact 30-scenario harness for gpt-4o under TPM 30000 limits.

Strategy:
- 30 carefully chosen scenarios covering all critical paths
- Adaptive throttling: track cumulative input+output tokens per minute
- After each call, sleep enough to stay under 30000 TPM
- Picks the highest-value scenarios from the full 149-turn matrix:
  1. Three multi-turn journeys (15 calls) — full funnel × 3 archetypes
  2. Tangent scenarios (5 calls) — return-to-funnel gate
  3. Out-of-scope (3 calls) — disclaimer + readiness cap
  4. Purchase/reviews/price/sceptic (4 calls) — gating logic
  5. Edge cases (3 calls) — slot extraction, multi-fact, vague

Output: tools/harness_30_results.json + console summary.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.domain.entities import FunnelStep
from src.repositories.conversation_repository import Base, ConversationRepository
from src.services.ai_factory import build_ai_service
from src.services.articles_service import ArticlesService
from src.services.consultant_service import ConsultantService
from src.services.content_catalog import ContentCatalog
from src.services.rag_service import RAGService
from src.services.telegraph_service import TelegraphService

logger.remove()
logger.add(sys.stderr, level="WARNING")


# ── Throttling: stay under 30 000 TPM ──────────────────────────────────
TPM_LIMIT = 28_000  # leave 2k headroom
ESTIMATED_TOKENS_PER_CALL = 18_000  # conservative: prompt+RAG+output
_call_log: deque[tuple[float, int]] = deque()  # (timestamp, tokens)


async def throttle() -> None:
    """Wait if necessary to keep cumulative tokens under TPM_LIMIT."""
    now = time.time()
    # Drop entries older than 60s
    while _call_log and now - _call_log[0][0] > 60:
        _call_log.popleft()
    used = sum(t for _, t in _call_log)
    if used + ESTIMATED_TOKENS_PER_CALL > TPM_LIMIT:
        # Wait until oldest entry is 61s old
        if _call_log:
            wait_until = _call_log[0][0] + 61
            sleep_for = max(0, wait_until - now)
            if sleep_for > 0:
                print(f"  [throttle: sleeping {sleep_for:.0f}s, used={used}/{TPM_LIMIT}]")
                await asyncio.sleep(sleep_for)


def record_call(tokens: int = ESTIMATED_TOKENS_PER_CALL) -> None:
    _call_log.append((time.time(), tokens))


# ── Scenarios ──────────────────────────────────────────────────────────

@dataclass
class TurnExpect:
    keywords_any: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    min_length: int = 800
    expect_tangent_gate: bool = False
    expect_reviews_url: bool = False
    forbid_purchase: bool = False
    expect_purchase: bool = False
    expect_oos: bool = False
    expect_slots: list[str] = field(default_factory=list)
    expect_advance_min: int | None = None


@dataclass
class Scenario:
    name: str
    category: str
    turns: list[tuple[str, TurnExpect]]


SCENARIOS: list[Scenario] = [
    # ── Three full-funnel journeys (15 calls) ────────────────────────
    Scenario("J1_mastopathy", "funnel", [
        ("Здравствуйте, у меня мастопатия 2 года, на УЗИ узлы 1.5см",
         TurnExpect(keywords_any=["абрахам", "94"], expect_slots=["diagnosis", "duration", "imaging_done"], expect_advance_min=3)),
        ("Пробовала прожестожель, не помогло",
         TurnExpect(expect_slots=["prior_treatment"])),
        ("Расскажи как именно ваш бальзам помогает",
         TurnExpect(keywords_any=["йодолактон", "галоген", "механизм"])),
        ("Слишком дорого",
         TurnExpect(keywords_any=["операц", "750"], min_length=800, forbid_purchase=True)),  # PRICE intent — no Kaspi
        ("Хорошо, готова попробовать. Где купить?",
         TurnExpect(expect_purchase=True, expect_advance_min=5)),
    ]),

    Scenario("J2_thyroid_with_tangent", "funnel", [
        ("Меня беспокоит щитовидная железа",
         TurnExpect(keywords_any=["щитовид", "т3", "т4", "97"])),
        ("А кто такой Гай Абрахам?",  # TANGENT
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True)),
        ("А что такое NIS-рецептор?",  # TANGENT 2
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True)),
        ("Понятно. У меня узлы 8мм, год назад нашли",
         TurnExpect(expect_slots=["imaging_done", "duration"])),
        ("Готов начать курс",
         TurnExpect(expect_purchase=True)),
    ]),

    Scenario("J3_skeptic_to_buyer", "funnel", [
        ("Не верю в йодотерапию, это шарлатанство",
         TurnExpect(keywords_any=["абрахам", "браунштейн", "500", "25 лет"])),
        ("Где доказательства?",
         TurnExpect(expect_reviews_url=True, forbid_purchase=True)),
        ("У меня АИТ, антитела высокие",
         TurnExpect(expect_slots=["diagnosis"], keywords_any=["аит", "аутоиммунн", "йод"])),
        ("Сколько стоит?",
         TurnExpect(keywords_any=["750"], forbid_purchase=True)),  # Just answering price question, not closing
        ("Готов оформить",
         TurnExpect(expect_purchase=True)),
    ]),

    # ── Tangent scenarios (5 calls) ──────────────────────────────────
    Scenario("T1_who_abraham", "tangent", [
        ("Кто такой Гай Абрахам?",
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True, keywords_any=["абрахам"])),
    ]),
    Scenario("T2_who_brownstein", "tangent", [
        ("Расскажите про доктора Браунштейна",
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True)),
    ]),
    Scenario("T3_1948_history", "tangent", [
        ("Что было в 1948 году с нормой йода?",
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True, keywords_any=["1948", "200", "4500"])),
    ]),
    Scenario("T4_wolff_chaikoff", "tangent", [
        ("Что такое эффект Вольфа-Чайкова?",
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True)),
    ]),
    Scenario("T5_iodine_project", "tangent", [
        ("Что за The Iodine Project?",
         TurnExpect(expect_tangent_gate=True, forbid_purchase=True)),
    ]),

    # ── Out-of-scope (3 calls) ──────────────────────────────────────
    Scenario("O1_stomach_cancer", "out_of_scope", [
        ("У меня рак желудка 2 стадии",
         TurnExpect(expect_oos=True, forbid_purchase=True, min_length=600)),
    ]),
    Scenario("O2_lung_cancer", "out_of_scope", [
        ("Поставили рак лёгких",
         TurnExpect(expect_oos=True, forbid_purchase=True)),
    ]),
    Scenario("O3_leukemia", "out_of_scope", [
        ("У меня лейкоз",
         TurnExpect(expect_oos=True, forbid_purchase=True)),
    ]),

    # ── Critical singles (4 calls) ──────────────────────────────────
    Scenario("S1_reviews_request", "reviews", [
        ("Покажите отзывы реальных людей",
         TurnExpect(expect_reviews_url=True, forbid_purchase=True)),
    ]),
    Scenario("S2_price_objection", "objection_price", [
        ("Очень дорого 750 тысяч это много",
         TurnExpect(keywords_any=["операц", "1 000 000", "пожизненн"], min_length=800, forbid_purchase=True)),
    ]),
    Scenario("S3_sceptic", "objection_sceptic", [
        ("Эндокринолог запрещает йод, говорит опасно",
         TurnExpect(keywords_any=["абрахам", "браунштейн", "80"], min_length=800,
                    forbid=["обратитесь к врачу", "согласуйте"])),
    ]),
    Scenario("S4_breast_cancer_in_scope", "oncology_in_scope", [
        ("У меня рак молочной железы 2 стадии",
         TurnExpect(keywords_any=["абрахам", "йодолактон", "варбург", "94"], min_length=1000,
                    forbid=["передаю", "обратитесь к врачу", "лечащим врачом"])),
    ]),

    # ── Slot extraction edge cases (3 calls) ────────────────────────
    Scenario("E1_multi_fact", "slot_extraction", [
        ("Мастопатия 3 года, узлы 2см, прожестожель не помог",
         TurnExpect(expect_slots=["diagnosis", "duration", "imaging_done", "prior_treatment"])),
    ]),
    Scenario("E2_vague", "slot_extraction", [
        ("Что-то меня беспокоит, не знаю что именно",
         TurnExpect(min_length=400)),
    ]),
    Scenario("E3_long_msg", "slot_extraction", [
        ("Здравствуйте у меня поставили диагноз гипотиреоз пять лет назад принимаю эутирокс 75 мкг "
         "симптомы все равно остаются усталость волосы выпадают холодно постоянно набираю вес "
         "хочу узнать поможет ли йодотерапия",
         TurnExpect(expect_slots=["diagnosis", "duration", "prior_treatment"], min_length=800)),
    ]),
]


# ── Validation logic ───────────────────────────────────────────────────

GATE_MARKERS = [
    "вернёмся", "вернемся", "к вашей", "к вашему случ", "о вашем случ",
    "о вашей ", "вашему случ", "к обсужден", "вернуться", "продолжим",
    "о ваших", "по вашему", "о вашей", "вашему диагноз",
]

OOS_MARKERS = [
    "йод-зависимых", "не основным лечением", "наша специализация",
    "не могу гарантировать конкретный", "общей поддержкой",
]

FORBIDDEN_GLOBAL = [
    "передаю вас", "передаю нашему", "врач-консультант", "наш врач",
    "наш менеджер", "согласуйте с лечащ", "обратитесь к врачу",
    "вылечит рак", "избавит от рака", "уберёт опухоль",
]


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
    has_question: bool
    tag_leak: list[str]
    forbidden_hits: list[str]
    score: int
    issues: list[str]
    duration_s: float
    ok: bool


@dataclass
class ScenarioResult:
    name: str
    category: str
    turns: list[TurnResult]
    score: int
    duration_s: float
    ok: bool


def validate_turn(text: str, expect: TurnExpect, conv, result, dt: float) -> TurnResult:
    text_lower = text.lower()
    issues: list[str] = []
    score = 100

    tag_leak = [t for t in ["[LINK:", "[SEND:", "[ATTACH:", "[ADVANCE:", "[SLOTS:", "[READY:", "[INTENT:", "[OFFER:"] if t in text]
    if tag_leak:
        issues.append(f"TAG LEAK: {tag_leak}")
        score -= 30

    forbidden_hits = [p for p in FORBIDDEN_GLOBAL if p in text_lower]
    forbidden_hits.extend(p for p in expect.forbid if p in text_lower)
    if forbidden_hits:
        issues.append(f"FORBIDDEN: {forbidden_hits}")
        score -= 30

    has_question = "?" in text[-300:]
    if not has_question:
        issues.append("no question")
        score -= 10

    if len(text) < expect.min_length:
        issues.append(f"too short: {len(text)} < {expect.min_length}")
        score -= 10

    if expect.keywords_any:
        matched = [k for k in expect.keywords_any if k.lower() in text_lower]
        if not matched:
            issues.append(f"missing required keywords {expect.keywords_any}")
            score -= 15

    if expect.expect_tangent_gate:
        if not any(m in text_lower for m in GATE_MARKERS):
            issues.append("missing tangent gate question")
            score -= 20

    if expect.expect_reviews_url and "disk.yandex" not in text_lower:
        issues.append("expected reviews URL missing")
        score -= 15

    if expect.forbid_purchase and "l.kaspi.kz" in text_lower:
        issues.append("FORBIDDEN purchase links present")
        score -= 25

    if expect.expect_purchase and "l.kaspi.kz" not in text_lower:
        issues.append("expected purchase links missing")
        score -= 15

    if expect.expect_oos and not any(m in text_lower for m in OOS_MARKERS):
        issues.append("OOS disclaimer missing")
        score -= 20

    visible_slots = {k: v for k, v in conv.qualification_facts.items() if not k.startswith("_")}
    missing_slots = [s for s in expect.expect_slots if s not in visible_slots]
    if missing_slots:
        issues.append(f"missing slots: {missing_slots}")
        score -= 5 * len(missing_slots)

    if expect.expect_advance_min and conv.funnel_step.value < expect.expect_advance_min:
        issues.append(f"funnel step {conv.funnel_step.value} < {expect.expect_advance_min}")
        score -= 10

    return TurnResult(
        user_msg=text[:1] and expect and "" or "",  # placeholder
        bot_text=text[:600],
        text_length=len(text),
        intents=sorted(result.intents),
        slots=dict(visible_slots),
        readiness=conv.readiness_score,
        advance_to_step=result.advance_to_step,
        funnel_step_after=conv.funnel_step.value,
        attachments_count=len(result.attachments),
        has_question=has_question,
        tag_leak=tag_leak,
        forbidden_hits=forbidden_hits,
        score=max(0, score),
        issues=issues,
        duration_s=dt,
        ok=score >= 75 and not tag_leak and not forbidden_hits,
    )


# ── Runner ──────────────────────────────────────────────────────────────

async def run_scenario(consultant: ConsultantService, sc: Scenario, idx: int, total: int) -> ScenarioResult:
    t0 = time.time()
    user_id = f"h30:{sc.name}"
    await consultant.reset_conversation(user_id)
    conv = await consultant.get_or_create_conversation(user_id)
    conv.funnel_step = FunnelStep.STEP_1_FILTER
    await consultant.save_conversation(conv)

    turns: list[TurnResult] = []
    for ti, (msg, expect) in enumerate(sc.turns):
        await throttle()
        t_call = time.time()
        try:
            r = await consultant.process_message_rich(user_id, msg, platform="whatsapp")
            dt = time.time() - t_call
            record_call()
            conv2 = await consultant.get_or_create_conversation(user_id)
            tr = validate_turn(r.text, expect, conv2, r, dt)
            tr.user_msg = msg[:80]
            turns.append(tr)
            mark = "✓" if tr.ok else "✗"
            print(
                f"[{idx}/{total}] {sc.name} t{ti+1}/{len(sc.turns)}: {mark} "
                f"score={tr.score} len={tr.text_length} att={tr.attachments_count} "
                f"step={tr.funnel_step_after} ready={tr.readiness}"
                + (f" ISSUES: {tr.issues}" if tr.issues else "")
            )
        except Exception as exc:
            dt = time.time() - t_call
            record_call(15_000)
            print(f"[{idx}] {sc.name} t{ti+1} ERROR: {exc}")
            turns.append(TurnResult(
                user_msg=msg, bot_text="", text_length=0, intents=[],
                slots={}, readiness=0, advance_to_step=None, funnel_step_after=0,
                attachments_count=0, has_question=False, tag_leak=[],
                forbidden_hits=[], score=0, issues=[f"EXC: {exc}"], duration_s=dt, ok=False,
            ))

    avg = sum(t.score for t in turns) // max(len(turns), 1)
    return ScenarioResult(name=sc.name, category=sc.category, turns=turns,
                           score=avg, duration_s=time.time() - t0,
                           ok=all(t.ok for t in turns))


async def main() -> None:
    print("=" * 60)
    print(f"HARNESS-30 — {len(SCENARIOS)} scenarios, {sum(len(s.turns) for s in SCENARIOS)} turns")
    print("=" * 60)

    settings = get_settings()
    dbp = Path("data/bot_h30.db")
    if dbp.exists(): dbp.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{dbp}", echo=False, future=True)
    async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)

    repo = ConversationRepository(session_factory=async_sessionmaker(engine, expire_on_commit=False))
    rag = RAGService(settings=settings)
    ai = build_ai_service(settings=settings)
    articles = ArticlesService(settings=settings); articles.load()
    tg = TelegraphService(access_token=settings.telegraph_access_token); tg._load_cache()
    catalog = ContentCatalog(settings, articles, tg)
    consultant = ConsultantService(
        repository=repo, ai_service=ai, rag_service=rag, settings=settings,
        articles_service=articles, content_catalog=catalog, telegraph_service=tg,
    )

    results: list[ScenarioResult] = []
    t_start = time.time()
    for i, sc in enumerate(SCENARIOS, 1):
        r = await run_scenario(consultant, sc, i, len(SCENARIOS))
        results.append(r)

    total_time = time.time() - t_start

    out = Path("tools/harness_30_results.json")
    with out.open("w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    # ── Summary ──
    total_turns = sum(len(r.turns) for r in results)
    ok_turns = sum(1 for r in results for t in r.turns if t.ok)
    avg_score = sum(t.score for r in results for t in r.turns) // max(total_turns, 1)
    avg_len = sum(t.text_length for r in results for t in r.turns) // max(total_turns, 1)
    tag_leaks = sum(1 for r in results for t in r.turns if t.tag_leak)
    forbidden = sum(1 for r in results for t in r.turns if t.forbidden_hits)
    with_att = sum(1 for r in results for t in r.turns if t.attachments_count > 0)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Scenarios: {len(results)} / Turns: {total_turns}")
    print(f"OK turns: {ok_turns}/{total_turns} ({100*ok_turns//max(total_turns,1)}%)")
    print(f"Avg score: {avg_score}/100")
    print(f"Avg length: {avg_len} chars")
    print(f"Tag leakage: {tag_leaks}/{total_turns}")
    print(f"Forbidden phrases: {forbidden}/{total_turns}")
    print(f"Turns with attachments: {with_att}/{total_turns} ({100*with_att//max(total_turns,1)}%)")
    print(f"Duration: {total_time:.0f}s ({total_time/max(total_turns,1):.1f}s/turn)")

    by_cat: dict[str, list[TurnResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).extend(r.turns)
    print("\nBy category:")
    for cat, turns in sorted(by_cat.items()):
        ok = sum(1 for t in turns if t.ok)
        avg = sum(t.score for t in turns) // max(len(turns), 1)
        avg_l = sum(t.text_length for t in turns) // max(len(turns), 1)
        print(f"  {cat:25s} ok={ok}/{len(turns)} avg={avg} len={avg_l}")

    print(f"\nResults saved to {out}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
