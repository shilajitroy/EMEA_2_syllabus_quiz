import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from quiz_service import (
    QuizGenerationError,
    QuizInputError,
    extract_syllabus,
    generate_questions,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
MIN_QUESTIONS = 5
MAX_QUESTIONS = 15


@contextmanager
def connect_database(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_path):
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with connect_database(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id TEXT PRIMARY KEY,
                syllabus_name TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                answers_json TEXT,
                score INTEGER
            )
            """
        )


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get(
            "SYLLABUS_QUIZ_DATABASE",
            str(Path(app.instance_path) / "quizzes.sqlite3"),
        ),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        OPENROUTER_API_KEY=os.environ.get("OPENROUTER_API_KEY", ""),
        OPENROUTER_MODEL=os.environ.get(
            "OPENROUTER_MODEL", "openai/gpt-4o-mini"
        ),
        LLM_PROVIDER=os.environ.get("LLM_PROVIDER", "openrouter").lower(),
        OLLAMA_BASE_URL=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        OLLAMA_MODEL=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
    )
    if test_config:
        app.config.update(test_config)

    initialize_database(app.config["DATABASE"])

    @app.context_processor
    def inject_template_helpers():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["_csrf_token"]}

    @app.before_request
    def check_csrf_token():
        if request.method == "POST":
            submitted = request.form.get("_csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not expected or not hmac.compare_digest(submitted, expected):
                abort(400, description="The form expired. Please try again.")

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/quiz")
    def create_quiz():
        upload = request.files.get("syllabus")
        if not upload or not upload.filename:
            flash("Choose a syllabus file before generating a quiz.", "error")
            return redirect(url_for("index"))

        safe_name = secure_filename(upload.filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            flash("Upload a PDF, DOCX, or TXT syllabus.", "error")
            return redirect(url_for("index"))

        try:
            question_count = int(request.form.get("question_count", "10"))
            if not MIN_QUESTIONS <= question_count <= MAX_QUESTIONS:
                raise ValueError
        except (TypeError, ValueError):
            flash(
                f"Choose between {MIN_QUESTIONS} and {MAX_QUESTIONS} questions.",
                "error",
            )
            return redirect(url_for("index"))

        try:
            syllabus_text = extract_syllabus(upload, extension)
            questions = generate_questions(
                syllabus_text,
                question_count,
                api_key=app.config["OPENROUTER_API_KEY"],
                model=app.config["OPENROUTER_MODEL"],
                provider=app.config["LLM_PROVIDER"],
                ollama_base_url=app.config["OLLAMA_BASE_URL"],
                ollama_model=app.config["OLLAMA_MODEL"],
            )
        except (QuizInputError, QuizGenerationError) as error:
            flash(str(error), "error")
            return redirect(url_for("index"))

        quiz_id = secrets.token_urlsafe(24)
        with connect_database(app.config["DATABASE"]) as connection:
            connection.execute(
                """
                INSERT INTO quizzes (id, syllabus_name, questions_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    quiz_id,
                    Path(safe_name).stem[:100] or "Syllabus",
                    json.dumps(questions, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        session["quiz_id"] = quiz_id
        return redirect(url_for("take_quiz"))

    @app.get("/quiz")
    def take_quiz():
        quiz = _get_current_quiz(app.config["DATABASE"])
        if not quiz:
            flash("Upload a syllabus to start a new quiz.", "info")
            return redirect(url_for("index"))
        if quiz["completed_at"]:
            return redirect(url_for("review_quiz"))
        questions = json.loads(quiz["questions_json"])
        return render_template("quiz.html", quiz=quiz, questions=questions)

    @app.post("/quiz/submit")
    def submit_quiz():
        quiz = _get_current_quiz(app.config["DATABASE"])
        if not quiz:
            flash("This quiz is no longer available. Start a new quiz.", "error")
            return redirect(url_for("index"))
        if quiz["completed_at"]:
            return redirect(url_for("review_quiz"))

        questions = json.loads(quiz["questions_json"])
        answers = []
        score = 0
        for index, question in enumerate(questions):
            selected = request.form.get(f"answer_{index}", "")
            selected_index = None
            if selected.isdecimal():
                candidate = int(selected)
                if 0 <= candidate < len(question["choices"]):
                    selected_index = candidate
            answers.append(selected_index)
            if selected_index == question["answer_index"]:
                score += 1

        with connect_database(app.config["DATABASE"]) as connection:
            connection.execute(
                """
                UPDATE quizzes
                SET answers_json = ?, score = ?, completed_at = ?
                WHERE id = ? AND completed_at IS NULL
                """,
                (
                    json.dumps(answers),
                    score,
                    datetime.now(timezone.utc).isoformat(),
                    quiz["id"],
                ),
            )
        return redirect(url_for("review_quiz"))

    @app.get("/review")
    def review_quiz():
        quiz = _get_current_quiz(app.config["DATABASE"])
        if not quiz:
            flash("Upload a syllabus to start a new quiz.", "info")
            return redirect(url_for("index"))
        if not quiz["completed_at"]:
            return redirect(url_for("take_quiz"))

        questions = json.loads(quiz["questions_json"])
        answers = json.loads(quiz["answers_json"])
        reviewed_questions = []
        for question, selected_index in zip(questions, answers):
            reviewed_questions.append(
                {
                    **question,
                    "selected_index": selected_index,
                    "is_correct": selected_index == question["answer_index"],
                }
            )
        return render_template(
            "review.html",
            quiz=quiz,
            questions=reviewed_questions,
            score=quiz["score"],
            total=len(questions),
        )

    @app.post("/new")
    def new_quiz():
        session.pop("quiz_id", None)
        return redirect(url_for("index"))

    @app.errorhandler(413)
    def file_too_large(_error):
        flash("That file is too large. The maximum upload size is 10 MB.", "error")
        return redirect(url_for("index"))

    return app


def _get_current_quiz(database_path):
    quiz_id = session.get("quiz_id")
    if not quiz_id:
        return None
    with connect_database(database_path) as connection:
        return connection.execute(
            "SELECT * FROM quizzes WHERE id = ?", (quiz_id,)
        ).fetchone()


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
