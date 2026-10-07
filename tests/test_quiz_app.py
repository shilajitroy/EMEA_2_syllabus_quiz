import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from quiz_service import (
    QuizGenerationError,
    _validate_questions,
    extract_syllabus,
    generate_questions,
)


SAMPLE_QUESTIONS = [
    {
        "question": f"Which item is described in section {number}?",
        "choices": ["Option A", "Option B", "Option C", "Option D"],
        "answer_index": 1,
        "explanation": "The syllabus explicitly describes this item.",
        "evidence_quote": "The course introduces core concepts and foundational principles.",
    }
    for number in range(1, 6)
]
SAMPLE_SYLLABUS = (
    "The course introduces core concepts and foundational principles. "
    "Students study research methods, evidence analysis, and academic writing."
)


class QuizServiceTests(unittest.TestCase):
    def test_text_upload_is_extracted(self):
        upload = io.BytesIO(SAMPLE_SYLLABUS.encode("utf-8"))
        upload.filename = "course.txt"
        self.assertEqual(extract_syllabus(upload, ".txt"), SAMPLE_SYLLABUS)

    def test_question_without_verbatim_source_evidence_is_rejected(self):
        question = {**SAMPLE_QUESTIONS[0], "evidence_quote": "A fact not in the syllabus at all"}
        with self.assertRaises(QuizGenerationError):
            _validate_questions(
                {"questions": [question]},
                SAMPLE_SYLLABUS,
                expected_count=1,
            )

    def test_wrong_question_count_is_rejected(self):
        with self.assertRaises(QuizGenerationError):
            _validate_questions(
                {"questions": SAMPLE_QUESTIONS[:-1]},
                SAMPLE_SYLLABUS,
                expected_count=5,
            )

    @patch("quiz_service.requests.post")
    def test_ollama_uses_local_api_without_openrouter_key(self, post):
        post.return_value.json.return_value = {
            "message": {"content": json.dumps({"questions": SAMPLE_QUESTIONS})}
        }
        post.return_value.raise_for_status.return_value = None

        questions = generate_questions(
            SAMPLE_SYLLABUS,
            5,
            api_key="",
            model="unused/openrouter-model",
            provider="ollama",
            ollama_base_url="http://localhost:11434/",
            ollama_model="llama3.1:8b",
        )

        self.assertEqual(len(questions), 5)
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/chat")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "llama3.1:8b")
        self.assertEqual(post.call_args.kwargs["json"]["format"], "json")
        self.assertNotIn("headers", post.call_args.kwargs)


class QuizAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key",
                "DATABASE": str(Path(self.temp_dir.name) / "quizzes.sqlite3"),
                "OPENROUTER_API_KEY": "test-key",
            }
        )
        self.client = self.app.test_client()
        self.csrf_token = self._get_csrf_token()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _get_csrf_token(self):
        self.client.get("/")
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def _quiz_session(self):
        with self.client.session_transaction() as session:
            session["quiz_id"] = self.quiz_id

    @patch("app.generate_questions", return_value=SAMPLE_QUESTIONS)
    def test_upload_answer_and_review_flow(self, _generate_questions):
        response = self.client.post(
            "/quiz",
            data={
                "_csrf_token": self.csrf_token,
                "question_count": "5",
                "syllabus": (io.BytesIO(SAMPLE_SYLLABUS.encode()), "course.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.quiz_id = session["quiz_id"]

        quiz_page = self.client.get("/quiz")
        self.assertEqual(quiz_page.status_code, 200)
        self.assertIn(b"Option A", quiz_page.data)

        answers = {
            "_csrf_token": self.csrf_token,
            **{f"answer_{index}": str(1 if index < 2 else 0) for index in range(5)},
        }
        result = self.client.post("/quiz/submit", data=answers)
        self.assertEqual(result.status_code, 302)
        review_page = self.client.get("/review")
        self.assertEqual(review_page.status_code, 200)
        self.assertIn(b"2 of 5 questions answered correctly", review_page.data)
        self.assertIn(b"Supporting syllabus excerpt", review_page.data)

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/quiz", data={"question_count": "5"})
        self.assertEqual(response.status_code, 400)

    def test_quiz_cannot_be_submitted_twice_to_change_score(self):
        with self.app.app_context():
            from app import connect_database

            with connect_database(self.app.config["DATABASE"]) as connection:
                connection.execute(
                    "INSERT INTO quizzes (id, syllabus_name, questions_json, created_at, completed_at, answers_json, score) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "existing-quiz",
                        "Course",
                        json.dumps(SAMPLE_QUESTIONS),
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:01:00+00:00",
                        json.dumps([1, 1, 1, 1, 1]),
                        5,
                    ),
                )
        self.quiz_id = "existing-quiz"
        self._quiz_session()
        response = self.client.post(
            "/quiz/submit",
            data={"_csrf_token": self.csrf_token, **{f"answer_{i}": "0" for i in range(5)}},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/review").status_code, 200)


if __name__ == "__main__":
    unittest.main()
