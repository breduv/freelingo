"""A separate, blind semantic review before newly generated teaching content is saved."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from app.core.config import settings
from app.schemas.lessons import ExerciseContent, LessonContent, LessonVocabularyItem
from app.services.llm_adapter import LLMResponseError, llm_adapter
from app.services.llm_config import lesson_review_enabled, llm_operation
from app.services.prompts.lesson import (
    EXERCISES_PART_INSTRUCTIONS,
    OUTLINE_PART_INSTRUCTIONS,
    REVIEW_INSTRUCTIONS,
    WRITING_PART_INSTRUCTIONS,
)

ShortPoint = Annotated[str, Field(min_length=1, max_length=240)]


class OptionReview(BaseModel):
    index: int = Field(ge=0)
    valid: bool
    reason: str = Field(min_length=1)


class ExerciseReview(BaseModel):
    index: int = Field(ge=0)
    valid: bool
    issues: list[str]
    options: list[OptionReview]


class LessonReview(BaseModel):
    explanation_issues: list[str]
    exercises: list[ExerciseReview]


class ExplanationExample(BaseModel):
    sentence: str = Field(min_length=1, max_length=400)
    note: str = Field(min_length=1, max_length=240)


class ExplanationContent(BaseModel):
    text: str = Field(min_length=1, max_length=1200)
    key_points: list[ShortPoint] = Field(min_length=1, max_length=4)
    examples: list[ExplanationExample] = Field(min_length=1, max_length=3)


class CommonTrap(BaseModel):
    mistake: str = Field(min_length=1, max_length=240)
    fix: str = Field(min_length=1, max_length=240)


class GlossaryEntry(BaseModel):
    term: str = Field(min_length=1, max_length=120)
    meaning: str = Field(min_length=1, max_length=240)
    note: str | None = Field(default=None, max_length=160)


class NativeExplanationContent(ExplanationContent):
    common_traps: list[CommonTrap] = Field(min_length=1, max_length=2)
    mini_glossary: list[GlossaryEntry] = Field(min_length=2, max_length=4)


class OutlineVocabularyItem(LessonVocabularyItem):
    word: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1, max_length=240)
    example: str = Field(min_length=1, max_length=400)
    translation: str | None = Field(default=None, max_length=240)
    example_translation: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=240)
    reading: str | None = Field(default=None, max_length=120)


class LessonOutline(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    explanation: ExplanationContent
    native_explanation: NativeExplanationContent | None = None
    vocabulary: list[OutlineVocabularyItem] = Field(min_length=3, max_length=8)
    grammar_refs: list[str] = Field(default_factory=list)


class NativeLessonOutline(LessonOutline):
    native_explanation: NativeExplanationContent


class LessonExercises(BaseModel):
    exercises: list[ExerciseContent] = Field(min_length=3, max_length=5)


class ListeningLessonExercises(LessonExercises):
    @model_validator(mode="after")
    def require_listening(self) -> Self:
        if not any(exercise.type == "listening" for exercise in self.exercises):
            raise ValueError("listening lessons must include at least one listening exercise")
        return self


class WritingTask(BaseModel):
    question: str
    criteria: list[str] = Field(
        min_length=2,
        max_length=4,
        description="Grading requirements, not answer choices or sample sentences",
    )
    model_answer: str
    explanation: str
    native_explanation: str | None = None
    native_hint: str | None = None
    question_translation: str | None = None


async def generate_lesson_parts(
    context: dict,
    schema: type[LessonContent],
    *,
    deadline: float,
    feedback: list[str] | None = None,
) -> LessonContent:
    """Keep each task small enough for local models; do not regenerate a whole mega-prompt."""
    context_json = json.dumps(context, ensure_ascii=False)
    outline_schema = (
        NativeLessonOutline if context.get("native_language", "none") != "none" else LessonOutline
    )
    correction = (
        "\nPrevious independent review found these issues. Avoid every one:\n" + "\n".join(feedback)
        if feedback
        else ""
    )
    outline = await llm_adapter.structured_output(
        [
            {
                "role": "system",
                "content": OUTLINE_PART_INSTRUCTIONS,
            },
            {"role": "user", "content": context_json + correction},
        ],
        outline_schema,
        deadline=deadline,
    )
    exercise_schema = (
        ListeningLessonExercises if context["lesson_type"] == "listening" else LessonExercises
    )
    exercises = await llm_adapter.structured_output(
        [
            {
                "role": "system",
                "content": EXERCISES_PART_INSTRUCTIONS,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"context": context, "lesson": outline.model_dump()}, ensure_ascii=False
                )
                + correction,
            },
        ],
        exercise_schema,
        deadline=deadline,
    )
    for index, exercise in enumerate(exercises.exercises):
        if exercise.type != "free_write":
            continue
        # A dedicated contract keeps small models from turning writing criteria
        # into another list of multiple-choice answers.
        writing = await llm_adapter.structured_output(
            [
                {
                    "role": "system",
                    "content": WRITING_PART_INSTRUCTIONS,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "context": context,
                            "lesson": outline.model_dump(),
                            "preceding_exercises": [
                                {
                                    "type": item.type,
                                    "question": item.question,
                                    "options": item.options,
                                }
                                for item in exercises.exercises[:index]
                            ],
                            "is_final_exercise": index == len(exercises.exercises) - 1,
                            "draft_question": exercise.question,
                        },
                        ensure_ascii=False,
                    )
                    + correction,
                },
            ],
            WritingTask,
            deadline=deadline,
        )
        exercises.exercises[index] = ExerciseContent(
            type="free_write",
            question=writing.question,
            options=writing.criteria,
            correct=writing.model_answer,
            explanation=writing.explanation,
            native_explanation=writing.native_explanation,
            native_hint=writing.native_hint,
            question_translation=writing.question_translation,
        )
    return schema.model_validate(
        {
            **outline.model_dump(),
            "lesson_type": context["lesson_type"],
            "cefr_level": context["cefr_level"],
            "unit_id": context["unit_id"],
            "exercises": [item.model_dump() for item in exercises.exercises],
        }
    )


def review_issues(lesson: LessonContent, review: LessonReview) -> list[str]:
    issues = list(review.explanation_issues)
    expected = list(range(len(lesson.exercises)))
    if sorted(item.index for item in review.exercises) != expected:
        return issues + ["Quality review did not cover every exercise exactly once"]
    for judgment in review.exercises:
        exercise = lesson.exercises[judgment.index]
        prefix = f"Exercise {judgment.index + 1}: "
        issues.extend(prefix + issue for issue in judgment.issues)
        if not judgment.valid:
            issues.append(prefix + "Reviewer rejected the exercise")
        if exercise.type not in ("multiple_choice", "listening"):
            continue
        options = exercise.options or []
        if sorted(item.index for item in judgment.options) != list(range(len(options))):
            issues.append(prefix + "Review must evaluate each option exactly once")
            continue
        accepted = [item.index for item in judgment.options if item.valid]
        if len(accepted) != 1:
            reasons = "; ".join(f"{item.index}: {item.reason}" for item in judgment.options)
            issues.append(
                prefix + f"Expected exactly one valid option, found {len(accepted)}. {reasons}"
            )
        elif options[accepted[0]] != exercise.correct:
            issues.append(prefix + "The answer key disagrees with independent option evaluation")
    return issues


async def review_lesson(
    lesson: LessonContent,
    *,
    target_language: str,
    deadline: float,
    exercise_only: bool = False,
    context: dict | None = None,
) -> list[str]:
    source = lesson.model_dump()
    if exercise_only:
        source["explanation"] = {}
        source["native_explanation"] = None
        source["vocabulary"] = None
    # Blind option evaluation avoids anchoring the reviewer to the generated answer key.
    for exercise in source["exercises"]:
        if exercise["type"] == "pronunciation":
            exercise["target_phrase"] = exercise["correct"]
        for key in ("correct", "explanation", "native_explanation", "native_hint"):
            exercise.pop(key, None)
    prompt = REVIEW_INSTRUCTIONS
    if exercise_only:
        prompt += "\nAudit only the supplied exercise. Explanation/vocabulary are intentionally omitted; return explanation_issues: []."
    result = await llm_adapter.structured_output(
        [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"language": target_language, "context": context, "lesson": source},
                    ensure_ascii=False,
                ),
            },
        ],
        LessonReview,
        deadline=deadline,
    )
    if exercise_only:
        result.explanation_issues = []
    return review_issues(lesson, result)


@llm_operation()
async def generate_verified_lesson(
    schema: type[LessonContent],
    *,
    target_language: str,
    context: dict,
) -> LessonContent:
    verify = lesson_review_enabled()
    deadline = asyncio.get_running_loop().time() + settings.EXERCISE_GENERATION_TIMEOUT_SECONDS
    async with asyncio.timeout_at(deadline):
        issues = []
        for _ in range(settings.LESSON_QUALITY_MAX_ATTEMPTS):
            lesson = await generate_lesson_parts(context, schema, deadline=deadline, feedback=issues)
            if not verify:
                return lesson
            issues = await review_lesson(
                lesson, target_language=target_language, deadline=deadline, context=context
            )
            if not issues:
                return lesson
    raise LLMResponseError(
        "Lesson failed independent quality review. Regenerate it or select another model."
    )


async def review_regenerated_exercise(
    exercise: ExerciseContent, *, explanation: dict, cefr_level: str, target_language: str
) -> None:
    if not lesson_review_enabled():
        return
    lesson = LessonContent(
        lesson_type="review",
        title="Exercise review",
        cefr_level=cefr_level,
        explanation=explanation,
        exercises=[exercise],
    )
    deadline = asyncio.get_running_loop().time() + settings.EXERCISE_GENERATION_TIMEOUT_SECONDS
    issues = await review_lesson(
        lesson, target_language=target_language, deadline=deadline, exercise_only=True
    )
    if issues:
        raise LLMResponseError("Regenerated exercise failed quality review: " + "; ".join(issues))
