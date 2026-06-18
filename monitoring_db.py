import sqlite3
from datetime import datetime
import re
from typing import Dict, List, Optional, Tuple

from werkzeug.security import check_password_hash, generate_password_hash


DB_PATH = "monitoring.db"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get_connection() -> sqlite3.Connection:
    # Use a fresh connection per call to stay thread-safe with SQLite.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        # Suspicious events logged from video, audio, and browser
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS suspicious_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                student_name TEXT,
                severity TEXT NOT NULL DEFAULT 'warning'
            )
            """
        )

        # Exams and MCQ questions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS exams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL,
                duration_minutes INTEGER,
                duration_seconds INTEGER,
                question_type TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                question_type TEXT NOT NULL DEFAULT 'MCQ',
                option_a TEXT NOT NULL DEFAULT '',
                option_b TEXT NOT NULL DEFAULT '',
                option_c TEXT NOT NULL DEFAULT '',
                option_d TEXT NOT NULL DEFAULT '',
                correct_option TEXT NOT NULL DEFAULT '',
                correct_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS exam_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                student_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                score INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                time_taken_seconds INTEGER,
                FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                selected_option TEXT,
                is_correct INTEGER NOT NULL,
                FOREIGN KEY (attempt_id) REFERENCES exam_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Lightweight migrations for older databases: add new columns if missing.
        try:
            cur.execute("ALTER TABLE exams ADD COLUMN duration_minutes INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("ALTER TABLE exams ADD COLUMN duration_seconds INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("ALTER TABLE exams ADD COLUMN question_type TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute(
                "ALTER TABLE questions ADD COLUMN question_type TEXT NOT NULL DEFAULT 'MCQ'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute(
                "ALTER TABLE questions ADD COLUMN correct_text TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute(
                "ALTER TABLE exam_attempts ADD COLUMN time_taken_seconds INTEGER"
            )
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("ALTER TABLE suspicious_events ADD COLUMN student_name TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute(
                "ALTER TABLE suspicious_events ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'"
            )
        except sqlite3.OperationalError:
            pass

        conn.commit()
    finally:
        conn.close()


def log_event(
    source: str,
    event_type: str,
    details: Optional[str] = None,
    student_name: Optional[str] = None,
    severity: str = "warning",
) -> None:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO suspicious_events (
                created_at, source, event_type, details, student_name, severity
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(timespec="seconds"),
                source,
                event_type,
                details,
                student_name,
                severity,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_events(
    limit: int = 100, student_name: Optional[str] = None
) -> List[sqlite3.Row]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        if student_name:
            cur.execute(
                """
                SELECT id, created_at, source, event_type, details, student_name, severity
                FROM suspicious_events
                WHERE student_name = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (student_name, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, created_at, source, event_type, details, student_name, severity
                FROM suspicious_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        return cur.fetchall()
    finally:
        conn.close()


def ensure_default_exam() -> int:
    """
    Ensure there is at least one exam with some MCQ questions.
    Returns the exam id.
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM exams ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row:
            return int(row["id"])

        created_at = datetime.utcnow().isoformat(timespec="seconds")
        cur.execute(
            """
            INSERT INTO exams (title, description, created_at, duration_minutes, question_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "Remote Proctoring Exam",
                "MCQ exam used in the remote proctoring demo.",
                created_at,
                15,
                "MCQ",
            ),
        )
        exam_id = cur.lastrowid

        questions = [
            (
                exam_id,
                "Which library is commonly used for real-time computer vision in Python?",
                "MCQ",
                "TensorFlow",
                "OpenCV",
                "NumPy",
                "Pandas",
                "B",
                "",
            ),
            (
                exam_id,
                "What is the main goal of remote exam proctoring systems?",
                "MCQ",
                "To increase exam duration",
                "To reduce the number of questions",
                "To ensure fairness and reduce cheating",
                "To disable webcams and microphones",
                "C",
                "",
            ),
            (
                exam_id,
                "Which of the following can indicate suspicious behaviour in online exams?",
                "MCQ",
                "Consistent eye contact with the screen",
                "Multiple faces in front of the camera",
                "Quiet environment",
                "Stable internet connection",
                "B",
                "",
            ),
            (
                exam_id,
                "Which sensor is primarily used to capture the student's voice?",
                "MCQ",
                "Webcam",
                "Graphics card",
                "Microphone",
                "Printer",
                "C",
                "",
            ),
            (
                exam_id,
                "Tab switching during an exam is usually considered:",
                "MCQ",
                "Suspicious / potentially cheating",
                "A required step",
                "Unrelated to cheating",
                "A network issue",
                "A",
                "",
            ),
        ]

        cur.executemany(
            """
            INSERT INTO questions (
                exam_id, text, question_type,
                option_a, option_b, option_c, option_d,
                correct_option, correct_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            questions,
        )

        conn.commit()
        return int(exam_id)
    finally:
        conn.close()


def get_exam_with_questions(exam_id: int) -> Tuple[sqlite3.Row, List[sqlite3.Row]]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM exams WHERE id = ?", (exam_id,))
        exam = cur.fetchone()
        if not exam:
            raise ValueError(f"Exam {exam_id} not found")

        cur.execute(
            """
            SELECT * FROM questions
            WHERE exam_id = ?
            ORDER BY id
            """,
            (exam_id,),
        )
        questions = cur.fetchall()
        return exam, questions
    finally:
        conn.close()


def set_exam_duration(exam_id: int, duration_minutes: int, duration_seconds: int) -> bool:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE exams
            SET duration_minutes = ?, duration_seconds = ?
            WHERE id = ?
            """,
            (duration_minutes, duration_seconds, exam_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_attempt_and_responses(
    exam_id: int, student_name: str, answers: Dict[int, str], time_taken_seconds: int
) -> Tuple[int, int, int]:
    """
    Persist one exam attempt, compute score, and store per-question responses.
    Returns (attempt_id, score, total_questions).
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, question_type, correct_option, correct_text
            FROM questions
            WHERE exam_id = ?
            """,
            (exam_id,),
        )
        rows = cur.fetchall()
        questions_by_qid = {int(r["id"]): r for r in rows}
        total_questions = len(questions_by_qid)

        now = datetime.utcnow().isoformat(timespec="seconds")
        score = 0

        cur.execute(
            """
            INSERT INTO exam_attempts (
                exam_id, student_name, started_at, finished_at,
                score, total_questions, time_taken_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (exam_id, student_name, now, now, 0, total_questions, time_taken_seconds),
        )
        attempt_id = int(cur.lastrowid)

        for qid, question in questions_by_qid.items():
            selected = (answers.get(qid) or "").strip()
            question_type = (question["question_type"] or "MCQ").upper()
            correct_opt = (question["correct_option"] or "").strip().upper()
            correct_text = (question["correct_text"] or "").strip().lower()
            is_correct = 0
            if question_type in {"MCQ", "TRUE_FALSE"}:
                is_correct = int(bool(selected and selected.upper() == correct_opt))
            elif question_type == "SHORT_ANSWER":
                is_correct = int(bool(selected and selected.strip().lower() == correct_text))
            if is_correct:
                score += 1

            cur.execute(
                """
                INSERT INTO responses (
                    attempt_id, question_id, selected_option, is_correct
                ) VALUES (?, ?, ?, ?)
                """,
                (attempt_id, qid, selected, is_correct),
            )

        cur.execute(
            "UPDATE exam_attempts SET score = ? WHERE id = ?",
            (score, attempt_id),
        )

        conn.commit()
        return attempt_id, score, total_questions
    finally:
        conn.close()


def list_attempts(limit: int = 100) -> List[sqlite3.Row]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id,
                   a.student_name,
                   a.score,
                   a.total_questions,
                   a.started_at,
                   a.time_taken_seconds,
                   e.title AS exam_title
            FROM exam_attempts a
            JOIN exams e ON a.exam_id = e.id
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def get_all_questions() -> List[sqlite3.Row]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT q.*, e.title AS exam_title
            FROM questions q
            JOIN exams e ON q.exam_id = e.id
            ORDER BY q.exam_id, q.id
            """
        )
        return cur.fetchall()
    finally:
        conn.close()


def _validate_question_payload(
    text: str,
    question_type: str,
    option_a: str,
    option_b: str,
    option_c: str,
    option_d: str,
    correct_option: str,
    correct_text: str,
) -> Tuple[bool, str]:
    if not (text or "").strip():
        return False, "Question text is required"
    clean_type = (question_type or "").strip().upper()
    if clean_type not in {"MCQ", "TRUE_FALSE", "SHORT_ANSWER"}:
        return False, "Question type must be MCQ, TRUE_FALSE, or SHORT_ANSWER"
    if clean_type == "MCQ":
        options = [option_a, option_b, option_c, option_d]
        if any(not (o or "").strip() for o in options):
            return False, "All four options are required for MCQ"
        if (correct_option or "").strip().upper() not in {"A", "B", "C", "D"}:
            return False, "Correct option must be one of A, B, C, D"
    elif clean_type == "TRUE_FALSE":
        if (correct_option or "").strip().upper() not in {"A", "B"}:
            return False, "True/False questions use option A=True and option B=False"
    elif not (correct_text or "").strip():
        return False, "Correct answer text is required for short answer questions"
    return True, ""


def add_question(
    exam_id: int,
    text: str,
    question_type: str,
    option_a: str,
    option_b: str,
    option_c: str,
    option_d: str,
    correct_option: str,
    correct_text: str,
) -> Tuple[bool, str]:
    is_valid, message = _validate_question_payload(
        text,
        question_type,
        option_a,
        option_b,
        option_c,
        option_d,
        correct_option,
        correct_text,
    )
    if not is_valid:
        return False, message

    clean_type = question_type.strip().upper()
    normalized_option_a = option_a.strip()
    normalized_option_b = option_b.strip()
    normalized_option_c = option_c.strip()
    normalized_option_d = option_d.strip()
    normalized_correct_option = correct_option.strip().upper()
    normalized_correct_text = correct_text.strip()

    if clean_type == "TRUE_FALSE":
        normalized_option_a = "True"
        normalized_option_b = "False"
        normalized_option_c = ""
        normalized_option_d = ""
    elif clean_type == "SHORT_ANSWER":
        normalized_option_a = ""
        normalized_option_b = ""
        normalized_option_c = ""
        normalized_option_d = ""
        normalized_correct_option = ""

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO questions (
                exam_id, text, question_type, option_a, option_b, option_c, option_d,
                correct_option, correct_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exam_id,
                text.strip(),
                clean_type,
                normalized_option_a,
                normalized_option_b,
                normalized_option_c,
                normalized_option_d,
                normalized_correct_option,
                normalized_correct_text,
            ),
        )
        conn.commit()
        return True, "Question added"
    finally:
        conn.close()


def update_question(
    question_id: int,
    text: str,
    question_type: str,
    option_a: str,
    option_b: str,
    option_c: str,
    option_d: str,
    correct_option: str,
    correct_text: str,
) -> Tuple[bool, str]:
    is_valid, message = _validate_question_payload(
        text,
        question_type,
        option_a,
        option_b,
        option_c,
        option_d,
        correct_option,
        correct_text,
    )
    if not is_valid:
        return False, message

    clean_type = question_type.strip().upper()
    normalized_option_a = option_a.strip()
    normalized_option_b = option_b.strip()
    normalized_option_c = option_c.strip()
    normalized_option_d = option_d.strip()
    normalized_correct_option = correct_option.strip().upper()
    normalized_correct_text = correct_text.strip()

    if clean_type == "TRUE_FALSE":
        normalized_option_a = "True"
        normalized_option_b = "False"
        normalized_option_c = ""
        normalized_option_d = ""
    elif clean_type == "SHORT_ANSWER":
        normalized_option_a = ""
        normalized_option_b = ""
        normalized_option_c = ""
        normalized_option_d = ""
        normalized_correct_option = ""

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE questions
            SET text = ?, question_type = ?, option_a = ?, option_b = ?, option_c = ?,
                option_d = ?, correct_option = ?, correct_text = ?
            WHERE id = ?
            """,
            (
                text.strip(),
                clean_type,
                normalized_option_a,
                normalized_option_b,
                normalized_option_c,
                normalized_option_d,
                normalized_correct_option,
                normalized_correct_text,
                question_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, "Question not found"
        return True, "Question updated"
    finally:
        conn.close()


def delete_question(question_id: int) -> bool:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_event(event_id: int) -> bool:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM suspicious_events WHERE id = ?", (event_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_attempts_for_student(name: str) -> List[sqlite3.Row]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id,
                   a.score,
                   a.total_questions,
                   a.started_at,
                   a.time_taken_seconds,
                   e.title AS exam_title
            FROM exam_attempts a
            JOIN exams e ON a.exam_id = e.id
            WHERE a.student_name = ?
            ORDER BY a.id DESC
            """,
            (name,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def register_student(student_email: str, password: str) -> Tuple[bool, str]:
    clean_email = (student_email or "").strip().lower()
    if not clean_email:
        return False, "Please enter a valid email"
    if not EMAIL_RE.match(clean_email):
        return False, "Please enter a valid email format"
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters"

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM students WHERE student_name = ?",
            (clean_email,),
        )
        if cur.fetchone():
            return False, "Email already registered"

        cur.execute(
            """
            INSERT INTO students (student_name, password_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (
                clean_email,
                generate_password_hash(password),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return True, "Registration successful"
    finally:
        conn.close()


def authenticate_student(student_email: str, password: str) -> bool:
    clean_email = (student_email or "").strip().lower()
    if not clean_email or not password:
        return False

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT password_hash FROM students WHERE student_name = ?",
            (clean_email,),
        )
        row = cur.fetchone()
        if not row:
            return False
        return check_password_hash(row["password_hash"], password)
    finally:
        conn.close()


def list_students(limit: int = 200) -> List[sqlite3.Row]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id,
                   s.student_name,
                   s.created_at,
                   COUNT(a.id) AS attempts
            FROM students s
            LEFT JOIN exam_attempts a ON a.student_name = s.student_name
            GROUP BY s.id, s.student_name, s.created_at
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def student_exists(student_email: str) -> bool:
    clean_email = (student_email or "").strip().lower()
    if not clean_email:
        return False
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM students WHERE student_name = ?",
            (clean_email,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def delete_student(student_email: str) -> bool:
    clean_email = (student_email or "").strip().lower()
    if not clean_email:
        return False

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM responses
            WHERE attempt_id IN (
                SELECT id FROM exam_attempts WHERE student_name = ?
            )
            """,
            (clean_email,),
        )
        cur.execute("DELETE FROM exam_attempts WHERE student_name = ?", (clean_email,))
        cur.execute("DELETE FROM suspicious_events WHERE student_name = ?", (clean_email,))
        cur.execute("DELETE FROM students WHERE student_name = ?", (clean_email,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
