"""
Runs a single scenario by index from harness_30.SCENARIOS.

Usage: python3 tools/run_one.py <index 1-18>

Prints full bot text per turn, all enforcement metadata, and saves
the result to tools/h30_results/turn_<index>.json so we can build a
final aggregated report after all 18 are done.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict
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

from tools.harness_30 import SCENARIOS, validate_turn

logger.remove()
logger.add(sys.stderr, level="WARNING")

OUT_DIR = Path("tools/h30_results")
OUT_DIR.mkdir(exist_ok=True)


async def main(idx: int) -> None:
    if not 1 <= idx <= len(SCENARIOS):
        print(f"Invalid index {idx}. Valid range: 1..{len(SCENARIOS)}")
        sys.exit(1)
    sc = SCENARIOS[idx - 1]

    settings = get_settings()
    # One DB per scenario so each starts fresh
    dbp = Path(f"data/h30_sc{idx:02d}.db")
    if dbp.exists():
        dbp.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{dbp}", echo=False, future=True)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    repo = ConversationRepository(session_factory=async_sessionmaker(engine, expire_on_commit=False))
    rag = RAGService(settings=settings)
    ai = build_ai_service(settings=settings)
    articles = ArticlesService(settings=settings)
    articles.load()
    tg = TelegraphService(access_token=settings.telegraph_access_token)
    tg._load_cache()
    catalog = ContentCatalog(settings, articles, tg)
    consultant = ConsultantService(
        repository=repo, ai_service=ai, rag_service=rag, settings=settings,
        articles_service=articles, content_catalog=catalog, telegraph_service=tg,
    )

    user_id = f"h30:{sc.name}"
    await consultant.reset_conversation(user_id)
    conv = await consultant.get_or_create_conversation(user_id)
    conv.funnel_step = FunnelStep.STEP_1_FILTER
    await consultant.save_conversation(conv)

    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"SCENARIO {idx}/{len(SCENARIOS)}: {sc.name}  [{sc.category}]")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    turn_results = []
    for ti, (msg, expect) in enumerate(sc.turns, 1):
        t0 = time.time()
        print(f"\n┌─── Turn {ti}/{len(sc.turns)} ───")
        print(f"│ USER: {msg}")
        try:
            r = await consultant.process_message_rich(user_id, msg, platform="whatsapp")
            dt = time.time() - t0
            conv2 = await consultant.get_or_create_conversation(user_id)
            tr = validate_turn(r.text, expect, conv2, r, dt)
            tr.user_msg = msg

            print(f"│ BOT [{dt:.1f}s, {len(r.text)} chars, {len(r.attachments)} att]:")
            for line in r.text.split("\n"):
                print(f"│   {line}")
            for a in r.attachments:
                if a.video_url:
                    print(f"│   📎 VIDEO: {a.text} ({a.video_url})")
                else:
                    print(f"│   📎 TEXT: {a.text[:80]}")
            visible_slots = {k: v for k, v in conv2.qualification_facts.items() if not k.startswith("_")}
            print(f"│ STATE: step={conv2.funnel_step.value} ready={conv2.readiness_score} "
                  f"intents={r.intents} slots={visible_slots}")
            mark = "✓" if tr.ok else "✗"
            print(f"│ SCORE: {mark} {tr.score}/100"
                  + (f"  ISSUES: {tr.issues}" if tr.issues else ""))
            turn_results.append(asdict(tr))
        except Exception as exc:
            dt = time.time() - t0
            print(f"│ ERROR after {dt:.1f}s: {exc}")
            import traceback
            traceback.print_exc()
            turn_results.append({
                "user_msg": msg, "score": 0, "ok": False,
                "issues": [f"EXC: {exc}"], "duration_s": dt,
            })
        print(f"└───")

    avg_score = sum(t.get("score", 0) for t in turn_results) // max(len(turn_results), 1)
    out_path = OUT_DIR / f"turn_{idx:02d}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({
            "name": sc.name,
            "category": sc.category,
            "turns": turn_results,
            "score": avg_score,
            "ok": all(t.get("ok", False) for t in turn_results),
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[Saved → {out_path}]  Avg score: {avg_score}/100")
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 tools/run_one.py <1..18>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))
