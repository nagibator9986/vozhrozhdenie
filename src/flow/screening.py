from __future__ import annotations

import time
from dataclasses import dataclass, field


# Symptom groups matching the screening.html quiz
SCREENING_GROUPS = [
    {
        "title": "⚡ Энергия и нервная система",
        "symptoms": [
            "Хроническая усталость, сонливость",
            "Депрессия, апатия",
            "Тревожность, беспокойство",
            "Туман в голове, плохая память",
            "Нарушения сна",
        ],
    },
    {
        "title": "🪞 Внешность и физиология",
        "symptoms": [
            "Сухая кожа, шелушение",
            "Выпадение волос",
            "Ломкие ногти",
            "Отёчность лица или конечностей",
            "Набор веса при нормальном питании",
        ],
    },
    {
        "title": "🏥 Внутренние органы",
        "symptoms": [
            "Узлы или кисты щитовидной железы",
            "Мастопатия / уплотнения в груди",
            "Кисты яичников",
            "Проблемы с ЖКТ (запоры, вздутие)",
            "Частые простуды, слабый иммунитет",
        ],
    },
    {
        "title": "👩 Женское здоровье",
        "symptoms": [
            "Нарушения менструального цикла",
            "ПМС, болезненные месячные",
            "Бесплодие или невынашивание",
            "Снижение либидо",
        ],
    },
]

CAPILLARY_GROUPS = [
    {
        "title": "👁️ Белок глаз (осмотрите в зеркале)",
        "symptoms": [
            "Красные прожилки на белках глаз",
            "Жёлтый или сероватый оттенок белков",
            "Тёмные точки или пятна на белке",
            "Расширенные сосуды на белках",
            "Мутный, нечёткий белок глаз",
            "Разница в цвете белков левого и правого глаза",
        ],
    },
    {
        "title": "👂 Слух и уши",
        "symptoms": [
            "Шум или звон в ушах",
            "Снижение слуха",
            "Частые заложенности ушей",
        ],
    },
    {
        "title": "🩸 Косвенные признаки сосудов",
        "symptoms": [
            "Холодные руки или ноги",
            "Онемение пальцев",
            "Сосудистые звёздочки на коже",
            "Варикоз",
            "Головокружения",
            "Частые головные боли",
        ],
    },
]


@dataclass
class ScreeningSession:
    """Tracks state for a conversational screening quiz."""
    test_type: str  # "screening" or "capillary"
    groups: list[dict]
    current_group: int = 0
    selected_symptoms: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class ConversationalScreening:
    """Manages conversational screening sessions for platforms without WebApp."""

    _TTL = 1800  # 30 min session timeout

    def __init__(self) -> None:
        self._sessions: dict[str, ScreeningSession] = {}

    def start_screening(self, user_id: str) -> str:
        """Start a new screening session. Returns the first question text."""
        groups = SCREENING_GROUPS
        session = ScreeningSession(test_type="screening", groups=groups)
        self._sessions[user_id] = session
        return self._format_group(session)

    def start_capillary(self, user_id: str) -> str:
        """Start a new capillary test session. Returns the first question text."""
        groups = CAPILLARY_GROUPS
        session = ScreeningSession(test_type="capillary", groups=groups)
        self._sessions[user_id] = session
        return self._format_group(session)

    def has_active_session(self, user_id: str) -> bool:
        session = self._sessions.get(user_id)
        if not session:
            return False
        if time.time() - session.created_at > self._TTL:
            del self._sessions[user_id]
            return False
        return True

    def process_answer(self, user_id: str, text: str) -> tuple[str | None, dict | None]:
        """Process user answer. Returns (next_question, final_result).

        If next_question is not None, send it. If final_result is not None, quiz is done.
        """
        session = self._sessions.get(user_id)
        if not session:
            return None, None

        group = session.groups[session.current_group]
        symptoms = group["symptoms"]

        # Parse selected numbers
        text = text.strip().lower()
        if text not in ("нет", "0", "-", "ничего", "пропустить"):
            for part in text.replace(",", " ").replace(".", " ").split():
                try:
                    idx = int(part) - 1
                    if 0 <= idx < len(symptoms):
                        session.selected_symptoms.append(symptoms[idx])
                except ValueError:
                    continue

        # Move to next group
        session.current_group += 1
        if session.current_group < len(session.groups):
            return self._format_group(session), None

        # All groups done — calculate result
        total = sum(len(g["symptoms"]) for g in session.groups)
        score = len(session.selected_symptoms)

        if session.test_type == "screening":
            level = "high" if score >= 9 else "medium" if score >= 4 else "low"
        else:
            level = "high" if score >= 7 else "medium" if score >= 3 else "low"

        result = {
            "type": f"{session.test_type}_result",
            "symptoms" if session.test_type == "screening" else "signs": session.selected_symptoms,
            "score": score,
            "total": total,
            "level": level,
        }

        del self._sessions[user_id]
        return None, result

    def cancel(self, user_id: str) -> None:
        self._sessions.pop(user_id, None)

    def _format_group(self, session: ScreeningSession) -> str:
        group = session.groups[session.current_group]
        total_groups = len(session.groups)
        current = session.current_group + 1

        lines = [f"📋 *{group['title']}* ({current}/{total_groups})\n"]
        for i, symptom in enumerate(group["symptoms"], 1):
            lines.append(f"{i}. {symptom}")
        lines.append("")
        lines.append("Напишите номера через запятую (например: 1, 3, 5)")
        lines.append("Или напишите «нет» если ничего не подходит.")
        return "\n".join(lines)

    def cleanup_expired(self) -> None:
        now = time.time()
        expired = [uid for uid, s in self._sessions.items() if now - s.created_at > self._TTL]
        for uid in expired:
            del self._sessions[uid]
