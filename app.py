from typing import Generator, Optional
import os
import csv
import io
import re

import cv2
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
)

from audio_monitor import AudioMonitor
from monitoring_db import (
    add_question,
    authenticate_student,
    delete_event,
    delete_question,
    delete_student,
    ensure_default_exam,
    get_all_questions,
    get_exam_with_questions,
    get_recent_events,
    init_db,
    list_attempts,
    list_attempts_for_student,
    list_students,
    log_event,
    register_student,
    save_attempt_and_responses,
    set_exam_duration,
    student_exists,
    update_question,
    _get_connection,
)
from video_analyzer import BehaviourAnalyzer


app = Flask(__name__)
app.secret_key = "change-me-in-production"

ADMIN_PASSWORD = "admin123"

behaviour_analyzer = BehaviourAnalyzer()
audio_monitor = AudioMonitor()
MONITORING_CONFIG = {
    "student_mic_enabled": True,
    "auto_stop_enabled": False,
    "auto_stop_warning_limit": 5,
    "auto_stop_critical_limit": 2,
    "allow_submit_exam": True,
    "tab_switch_limit": 1,
}


@app.template_filter("date_only")
def date_only(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _attempt_summary(attempts) -> tuple[int, int, int]:
    count = len(attempts)
    total_score = sum(int(a["score"] or 0) for a in attempts)
    total_questions = sum(int(a["total_questions"] or 0) for a in attempts)
    return count, total_score, total_questions


def _history_metrics(attempts, warnings) -> dict:
    attempt_count, total_score, total_questions = _attempt_summary(attempts)
    score_percent = round((total_score / total_questions) * 100, 1) if total_questions else 0
    best_attempt = None
    best_percent = -1.0
    total_time = 0
    timed_attempts = 0

    for attempt in attempts:
        score = int(attempt["score"] or 0)
        questions = int(attempt["total_questions"] or 0)
        percent = (score / questions) * 100 if questions else 0
        if percent > best_percent:
            best_percent = percent
            best_attempt = attempt
        if attempt["time_taken_seconds"] is not None:
            total_time += int(attempt["time_taken_seconds"] or 0)
            timed_attempts += 1

    warning_count = len(warnings)
    critical_warning_count = sum(1 for w in warnings if w["severity"] == "critical")
    average_time_seconds = round(total_time / timed_attempts) if timed_attempts else 0

    return {
        "attempt_count": attempt_count,
        "total_score": total_score,
        "total_questions": total_questions,
        "score_percent": score_percent,
        "best_score": int(best_attempt["score"] or 0) if best_attempt else 0,
        "best_total": int(best_attempt["total_questions"] or 0) if best_attempt else 0,
        "best_percent": round(best_percent, 1) if best_attempt else 0,
        "average_time_seconds": average_time_seconds,
        "warning_count": warning_count,
        "critical_warning_count": critical_warning_count,
    }


def _extract_pdf_text(file_storage) -> tuple[bool, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return False, "PDF upload needs pypdf. Run: pip install -r requirements.txt"

    try:
        reader = PdfReader(file_storage.stream)
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        return False, f"Could not read PDF file: {exc}"

    text = "\n".join(pages).strip()
    if not text:
        return False, "No readable text found in the PDF"
    return True, text


def _parse_pdf_questions(text: str) -> tuple[list[dict], list[str]]:
    normalized = re.sub(r"\r\n?", "\n", text or "")
    blocks = re.split(r"(?im)^\s*(?:q(?:uestion)?\.?\s*)?\d+\s*[\).:-]\s+", normalized)
    questions = []
    errors = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = [line.strip() for line in block.splitlines() if line.strip()]
        answer = ""
        answer_index = None
        type_hint = ""
        type_index = None

        for index, line in enumerate(lines):
            answer_match = re.match(r"(?i)^(?:answer|correct(?:\s+answer)?|ans)\s*[:.-]\s*(.+)$", line)
            if answer_match:
                answer = answer_match.group(1).strip()
                answer_index = index
                continue
            type_match = re.match(r"(?i)^type\s*[:.-]\s*(MCQ|TRUE_FALSE|SHORT_ANSWER|SHORT ANSWER|TRUE/FALSE)$", line)
            if type_match:
                type_hint = type_match.group(1).replace(" ", "_").replace("/", "_").upper()
                type_index = index

        content_lines = [
            line
            for index, line in enumerate(lines)
            if index not in {answer_index, type_index}
        ]
        if not content_lines:
            errors.append("Skipped an empty question block")
            continue

        option_map = {}
        question_lines = []
        for line in content_lines:
            option_match = re.match(r"(?i)^([A-D])\s*[\).:-]\s*(.+)$", line)
            if option_match:
                option_map[option_match.group(1).upper()] = option_match.group(2).strip()
            else:
                question_lines.append(line)

        question_text = " ".join(question_lines).strip()
        if not question_text:
            errors.append("Skipped a question without question text")
            continue

        clean_answer = answer.strip()
        upper_answer = clean_answer.upper()
        if type_hint == "SHORT_ANSWER" or (clean_answer and not option_map and upper_answer not in {"TRUE", "FALSE"}):
            question_type = "SHORT_ANSWER"
            parsed = {
                "text": question_text,
                "question_type": question_type,
                "option_a": "",
                "option_b": "",
                "option_c": "",
                "option_d": "",
                "correct_option": "",
                "correct_text": clean_answer,
            }
        elif type_hint == "TRUE_FALSE" or upper_answer in {"TRUE", "FALSE"}:
            question_type = "TRUE_FALSE"
            parsed = {
                "text": question_text,
                "question_type": question_type,
                "option_a": "True",
                "option_b": "False",
                "option_c": "",
                "option_d": "",
                "correct_option": "A" if upper_answer == "TRUE" else "B",
                "correct_text": "",
            }
        else:
            question_type = "MCQ"
            parsed = {
                "text": question_text,
                "question_type": question_type,
                "option_a": option_map.get("A", ""),
                "option_b": option_map.get("B", ""),
                "option_c": option_map.get("C", ""),
                "option_d": option_map.get("D", ""),
                "correct_option": upper_answer[:1],
                "correct_text": "",
            }

        questions.append(parsed)

    return questions, errors


def _exam_total_duration_seconds(exam) -> int:
    minutes = int(exam["duration_minutes"] or 0)
    seconds = int(exam["duration_seconds"] or 0)
    return (minutes * 60) + seconds


def generate_video_stream(student_name: Optional[str] = None) -> Generator[bytes, None, None]:
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("Could not open webcam.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = behaviour_analyzer.analyze_and_annotate(frame, student_name=student_name)
            ret, buffer = cv2.imencode(".jpg", frame)
            if not ret:
                continue

            jpg_bytes = buffer.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n"
            )
    finally:
        cap.release()


@app.get("/")
def index():
    # Require student login before attending the exam
    student_name = session.get("student_name")
    if not student_name:
        audio_monitor.set_active_student(None)
        return redirect(url_for("student_login"))
    audio_monitor.set_active_student(student_name)

    exam_id = ensure_default_exam()
    exam, questions = get_exam_with_questions(exam_id)
    exam_duration_seconds = _exam_total_duration_seconds(exam)
    return render_template(
        "index.html", 
        exam=exam, 
        questions=questions, 
        student_name=student_name,
        exam_duration_seconds=exam_duration_seconds,
        allow_submit=bool(MONITORING_CONFIG.get("allow_submit_exam", True)),
        err=request.args.get("err"),
    )


@app.route("/video_feed")
def video_feed():
    student_name = session.get("student_name")
    if not student_name:
        audio_monitor.set_active_student(None)
        return redirect(url_for("student_login"))
    audio_monitor.set_active_student(student_name)
    return Response(
        generate_video_stream(student_name=student_name),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/event")
def client_event():
    data = request.get_json(silent=True) or {}
    event_type = str(data.get("type", "unknown"))
    details = str(data.get("details", "")) or None
    severity = str(data.get("severity", "warning")).lower()
    if severity not in {"info", "warning", "critical"}:
        severity = "warning"
    log_event(
        "client",
        event_type,
        details,
        student_name=session.get("student_name"),
        severity=severity,
    )
    return jsonify({"status": "ok"})


@app.get("/api/events")
def list_events():
    if not session.get("is_admin"):
        return jsonify([])
    rows = get_recent_events(limit=200)
    return jsonify(
        [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "source": r["source"],
                "event_type": r["event_type"],
                "details": r["details"],
                "student_name": r["student_name"],
                "severity": r["severity"],
            }
            for r in rows
        ]
    )


@app.get("/api/student_events")
def list_student_events():
    student_name = session.get("student_name")
    if not student_name:
        return jsonify([])
    rows = get_recent_events(limit=200, student_name=student_name)
    return jsonify(
        [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "source": r["source"],
                "event_type": r["event_type"],
                "details": r["details"],
                "student_name": r["student_name"],
                "severity": r["severity"],
            }
            for r in rows
        ]
    )


@app.get("/api/live_video_warnings")
def live_video_warnings():
    student_name = session.get("student_name")
    if not student_name:
        return jsonify([])
    return jsonify(behaviour_analyzer.get_live_warnings())


@app.get("/api/monitoring_config")
def monitoring_config():
    # Student clients poll this to apply admin controls in real time.
    return jsonify(
        {
            "student_mic_enabled": bool(MONITORING_CONFIG["student_mic_enabled"]),
            "auto_stop_enabled": bool(MONITORING_CONFIG["auto_stop_enabled"]),
            "auto_stop_warning_limit": int(MONITORING_CONFIG["auto_stop_warning_limit"]),
            "auto_stop_critical_limit": int(MONITORING_CONFIG["auto_stop_critical_limit"]),
            "tab_switch_limit": int(MONITORING_CONFIG["tab_switch_limit"]),
        }
    )


@app.get("/report")
@app.get("/admin/events")
def report():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    rows = get_recent_events(limit=200)
    return render_template("report.html", events=rows)


@app.get("/admin/history")
def admin_history():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    attempts = list_attempts(limit=500)
    return render_template(
        "admin_history.html",
        attempts=attempts,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.get("/export/events.csv")
def export_events_csv():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    rows = get_recent_events(limit=2000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "created_at", "student_name", "source", "event_type", "severity", "details"]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["created_at"],
                r["student_name"] or "",
                r["source"],
                r["event_type"],
                r["severity"] or "warning",
                r["details"] or "",
            ]
        )

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(
        mem,
        as_attachment=True,
        download_name="events_export.csv",
        mimetype="text/csv",
    )


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    error = None
    if request.method == "GET" and not session.get("student_name"):
        audio_monitor.set_active_student(None)
    if request.method == "POST":
        email = (request.form.get("student_email") or "").strip().lower()
        password = request.form.get("password", "")
        if authenticate_student(email, password):
            session["student_name"] = email
            audio_monitor.set_active_student(email)
            return redirect(url_for("index"))
        error = "Invalid student email or password"
    return render_template("student_login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    success = None
    if request.method == "POST":
        email = (request.form.get("student_email") or "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if password != confirm_password:
            error = "Passwords do not match"
        else:
            ok, message = register_student(email, password)
            if ok:
                success = message
            else:
                error = message

    return render_template("register.html", error=error, success=success)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            audio_monitor.set_active_student(None)
            return redirect(url_for("admin_dashboard"))
        error = "Invalid password"
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.pop("is_admin", None)
    session.pop("student_name", None)
    audio_monitor.set_active_student(None)
    return redirect(url_for("student_login"))


@app.get("/student_logout")
def student_logout():
    session.pop("student_name", None)
    audio_monitor.set_active_student(None)
    return redirect(url_for("student_login"))


@app.get("/admin_logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("login"))


@app.route("/submit_exam", methods=["POST"])
def submit_exam():
    student_name = session.get("student_name")
    if not student_name:
        audio_monitor.set_active_student(None)
        return redirect(url_for("student_login"))
    audio_monitor.set_active_student(student_name)

    # Check if admin has disabled submission
    if not MONITORING_CONFIG.get("allow_submit_exam", True):
        return redirect(url_for("index", err="Exam submission is currently disabled by admin."))

    exam_id = ensure_default_exam()
    try:
        time_taken_seconds = int(request.form.get("time_taken_seconds", "0"))
    except ValueError:
        time_taken_seconds = 0

    # Collect submitted answers keyed by question id
    _, questions = get_exam_with_questions(exam_id)
    answers: dict[int, str] = {}
    for q in questions:
        qid = int(q["id"])
        field_name = f"q_{qid}"
        selected = request.form.get(field_name)
        if selected is not None and str(selected).strip():
            answers[qid] = str(selected).strip()

    attempt_id, score, total = save_attempt_and_responses(
        exam_id, student_name, answers, time_taken_seconds
    )

    return render_template(
        "submit_result.html",
        student_name=student_name,
        score=score,
        total=total,
        attempt_id=attempt_id,
    )


@app.get("/admin")
@app.get("/dashboard")
def admin_dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    exam_id = ensure_default_exam()
    exam, _ = get_exam_with_questions(exam_id)
    attempts = list_attempts(limit=100)
    students = list_students(limit=200)
    total_duration_seconds = _exam_total_duration_seconds(exam)
    return render_template(
        "admin.html",
        attempts=attempts,
        students=students,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
        student_mic_enabled=bool(MONITORING_CONFIG["student_mic_enabled"]),
        exam_duration_minutes=total_duration_seconds // 60,
        exam_duration_seconds=total_duration_seconds % 60,
        auto_stop_enabled=bool(MONITORING_CONFIG["auto_stop_enabled"]),
        auto_stop_warning_limit=int(MONITORING_CONFIG["auto_stop_warning_limit"]),
        auto_stop_critical_limit=int(MONITORING_CONFIG["auto_stop_critical_limit"]),
        allow_submit_exam=bool(MONITORING_CONFIG.get("allow_submit_exam", True)),
        tab_switch_limit=int(MONITORING_CONFIG.get("tab_switch_limit", 1)),
    )


@app.post("/admin/monitoring/mic")
def admin_set_mic_monitoring():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    enabled = (request.form.get("enabled") or "").strip() == "1"
    MONITORING_CONFIG["student_mic_enabled"] = enabled
    state_label = "enabled" if enabled else "disabled"
    return redirect(url_for("admin_dashboard", msg=f"Student mic monitoring {state_label}"))


@app.post("/admin/monitoring/auto_stop")
def admin_set_auto_stop():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    enabled = (request.form.get("enabled") or "").strip() == "1"
    raw_warning_limit = (request.form.get("warning_limit") or "").strip()
    raw_critical_limit = (request.form.get("critical_limit") or "").strip()
    raw_tab_switch_limit = (request.form.get("tab_switch_limit") or "").strip()

    try:
        warning_limit = int(raw_warning_limit or MONITORING_CONFIG["auto_stop_warning_limit"])
        critical_limit = int(raw_critical_limit or MONITORING_CONFIG["auto_stop_critical_limit"])
        tab_switch_limit = int(raw_tab_switch_limit or MONITORING_CONFIG["tab_switch_limit"])
    except ValueError:
        return redirect(url_for("admin_dashboard", err="Monitoring limits must be numbers"))

    if warning_limit < 1 or critical_limit < 1 or tab_switch_limit < 1:
        return redirect(url_for("admin_dashboard", err="Monitoring limits must be at least 1"))

    MONITORING_CONFIG["auto_stop_enabled"] = enabled
    MONITORING_CONFIG["auto_stop_warning_limit"] = warning_limit
    MONITORING_CONFIG["auto_stop_critical_limit"] = critical_limit
    MONITORING_CONFIG["tab_switch_limit"] = tab_switch_limit
    state_label = "enabled" if enabled else "disabled"
    return redirect(
        url_for(
            "admin_dashboard",
            msg=(
                f"Auto-stop on violations {state_label}. "
                f"Limits: {warning_limit} warnings, {critical_limit} critical warnings, "
                f"{tab_switch_limit} tab switches."
            ),
        )
    )


@app.post("/admin/exam/duration")
def admin_set_exam_duration():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    raw_minutes = (request.form.get("duration_minutes") or "").strip()
    raw_seconds = (request.form.get("duration_seconds") or "").strip()
    try:
        duration_minutes = int(raw_minutes)
        duration_seconds = int(raw_seconds or "0")
    except ValueError:
        return redirect(url_for("admin_dashboard", err="Exam duration must be numbers"))

    if duration_minutes < 0 or duration_minutes > 300:
        return redirect(
            url_for("admin_dashboard", err="Minutes must be between 0 and 300")
        )
    if duration_seconds < 0 or duration_seconds > 59:
        return redirect(url_for("admin_dashboard", err="Seconds must be between 0 and 59"))
    if duration_minutes == 0 and duration_seconds == 0:
        return redirect(url_for("admin_dashboard", err="Exam duration must be at least 1 second"))

    exam_id = ensure_default_exam()
    updated = set_exam_duration(exam_id, duration_minutes, duration_seconds)
    if not updated:
        return redirect(url_for("admin_dashboard", err="Could not update exam duration"))
    return redirect(
        url_for(
            "admin_dashboard",
            msg=f"Exam duration updated to {duration_minutes}m {duration_seconds}s",
        )
    )


@app.post("/admin/exam/type")
def admin_set_exam_type():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    
    question_type = (request.form.get("question_type") or "").strip().upper()
    if question_type not in ["MCQ", "MIXED"]:
        return redirect(url_for("admin_dashboard", err="Invalid exam type"))
    
    exam_id = ensure_default_exam()
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE exams SET question_type = ? WHERE id = ?",
            (question_type, exam_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return redirect(url_for("admin_dashboard", err="Could not update exam type"))
    finally:
        conn.close()
    
    return redirect(url_for("admin_dashboard", msg=f"Exam type updated to {question_type}"))


@app.post("/admin/exam/toggle_submit")
def admin_toggle_submit():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    
    MONITORING_CONFIG["allow_submit_exam"] = not MONITORING_CONFIG.get("allow_submit_exam", True)
    state_label = "enabled" if MONITORING_CONFIG["allow_submit_exam"] else "disabled"
    return redirect(url_for("admin_dashboard", msg=f"Exam submission {state_label}"))


@app.get("/admin/questions")
def admin_questions():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    questions = get_all_questions()
    return render_template(
        "admin_questions.html",
        questions=questions,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.get("/admin/students")
def admin_students():
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    students = list_students(limit=500)
    return render_template(
        "admin_students.html",
        students=students,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.post("/admin/students/<student_name>/delete")
def admin_delete_student(student_name: str):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    normalized_student = (student_name or "").strip().lower()
    if not normalized_student or not student_exists(normalized_student):
        return redirect(url_for("admin_students", err="Student not found"))

    deleted = delete_student(normalized_student)
    if not deleted:
        return redirect(url_for("admin_students", err="Could not delete student"))
    return redirect(url_for("admin_students", msg="Student deleted"))


@app.post("/admin/questions/add")
def admin_add_question():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    exam_id = ensure_default_exam()
    ok, message = add_question(
        exam_id=exam_id,
        text=request.form.get("text", ""),
        question_type=request.form.get("question_type", ""),
        option_a=request.form.get("option_a", ""),
        option_b=request.form.get("option_b", ""),
        option_c=request.form.get("option_c", ""),
        option_d=request.form.get("option_d", ""),
        correct_option=request.form.get("correct_option", ""),
        correct_text=request.form.get("correct_text", ""),
    )
    if not ok:
        return redirect(url_for("admin_questions", err=message))
    return redirect(url_for("admin_questions", msg=message))


@app.post("/admin/questions/upload_pdf")
def admin_upload_questions_pdf():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    upload = request.files.get("questions_pdf")
    if not upload or not upload.filename:
        return redirect(url_for("admin_questions", err="Please choose a PDF file"))
    if not upload.filename.lower().endswith(".pdf"):
        return redirect(url_for("admin_questions", err="Only PDF files are supported"))

    ok, text_or_error = _extract_pdf_text(upload)
    if not ok:
        return redirect(url_for("admin_questions", err=text_or_error))

    parsed_questions, parse_errors = _parse_pdf_questions(text_or_error)
    if not parsed_questions:
        return redirect(
            url_for(
                "admin_questions",
                err="No questions found. Use blocks like: Q1. Question, A) option, B) option, Answer: A",
            )
        )

    exam_id = ensure_default_exam()
    added = 0
    failed = []
    for index, question in enumerate(parsed_questions, start=1):
        ok, message = add_question(exam_id=exam_id, **question)
        if ok:
            added += 1
        else:
            failed.append(f"Question {index}: {message}")

    if added == 0:
        detail = failed[0] if failed else "Could not import questions"
        return redirect(url_for("admin_questions", err=detail))

    suffix = ""
    if failed or parse_errors:
        issue_count = len(failed) + len(parse_errors)
        suffix = f" ({issue_count} skipped)"
    return redirect(
        url_for(
            "admin_questions",
            msg=f"Imported {added} question(s) from PDF{suffix}. Exam page is updated.",
        )
    )


@app.post("/admin/questions/<int:question_id>/update")
def admin_update_question(question_id: int):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    ok, message = update_question(
        question_id=question_id,
        text=request.form.get("text", ""),
        question_type=request.form.get("question_type", ""),
        option_a=request.form.get("option_a", ""),
        option_b=request.form.get("option_b", ""),
        option_c=request.form.get("option_c", ""),
        option_d=request.form.get("option_d", ""),
        correct_option=request.form.get("correct_option", ""),
        correct_text=request.form.get("correct_text", ""),
    )
    if not ok:
        return redirect(url_for("admin_questions", err=message))
    return redirect(url_for("admin_questions", msg=message))


@app.post("/admin/questions/<int:question_id>/delete")
def admin_delete_question(question_id: int):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    deleted = delete_question(question_id)
    if not deleted:
        return redirect(url_for("admin_questions", err="Question not found"))
    return redirect(url_for("admin_questions", msg="Question deleted"))


@app.get("/admin/clear_history")
def clear_history():
    """Clear all exam history and events (admin only)"""
    if not session.get("is_admin"):
        return redirect(url_for("login"))
    
    # Clear all data from database tables
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM exam_attempts")
        cur.execute("DELETE FROM responses")
        cur.execute("DELETE FROM suspicious_events")
        conn.commit()
    finally:
        conn.close()
    
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/events/<int:event_id>/delete")
def admin_delete_event(event_id: int):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    deleted = delete_event(event_id)
    next_url = request.form.get("next") or url_for("report")
    if not deleted:
        return redirect(next_url)
    return redirect(next_url)


@app.post("/admin/events/clear")
def admin_clear_events():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM suspicious_events")
        conn.commit()
    finally:
        conn.close()

    next_url = request.form.get("next") or url_for("report")
    return redirect(next_url)


@app.post("/admin/history/<student_name>/clear")
def admin_clear_student_history(student_name: str):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    normalized_student = (student_name or "").strip().lower()
    if not normalized_student or not student_exists(normalized_student):
        return redirect(url_for("admin_dashboard", err="Student not found"))

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
            (normalized_student,),
        )
        cur.execute(
            "DELETE FROM exam_attempts WHERE student_name = ?",
            (normalized_student,),
        )
        cur.execute(
            "DELETE FROM suspicious_events WHERE student_name = ?",
            (normalized_student,),
        )
        conn.commit()
    finally:
        conn.close()

    return redirect(
        url_for(
            "history",
            student_name=normalized_student,
            msg="Student exam history deleted",
        )
    )


@app.get("/history/<student_name>")
def history(student_name: str):
    normalized_student = (student_name or "").strip().lower()
    if not student_exists(normalized_student):
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("student_login"))

    current_student = session.get("student_name")
    if not session.get("is_admin") and current_student != normalized_student:
        return redirect(url_for("student_login"))
    attempts = list_attempts_for_student(normalized_student)
    warnings = get_recent_events(limit=300, student_name=normalized_student)
    metrics = _history_metrics(attempts, warnings)
    return render_template(
        "history.html",
        student_name=normalized_student,
        attempts=attempts,
        warnings=warnings,
        is_student_view=not session.get("is_admin"),
        msg=request.args.get("msg"),
        **metrics,
    )


@app.get("/my_history")
def my_history():
    student_name = session.get("student_name")
    if not student_name:
        return redirect(url_for("student_login"))
    if not student_exists(student_name):
        session.pop("student_name", None)
        return redirect(url_for("student_login"))
    attempts = list_attempts_for_student(student_name)
    warnings = get_recent_events(limit=300, student_name=student_name)
    metrics = _history_metrics(attempts, warnings)
    return render_template(
        "history.html",
        student_name=student_name,
        attempts=attempts,
        warnings=warnings,
        is_student_view=True,
        **metrics,
    )


@app.get("/export/my_history.csv")
def export_my_history_csv():
    student_name = session.get("student_name")
    if not student_name or not student_exists(student_name):
        return redirect(url_for("student_login"))

    attempts = list_attempts_for_student(student_name)
    warnings = get_recent_events(limit=3000, student_name=student_name)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "id", "created_at", "source_or_exam", "type_or_score", "details"])
    for a in attempts:
        writer.writerow(
            [
                "attempt",
                a["id"],
                a["started_at"],
                a["exam_title"],
                f'{a["score"]}/{a["total_questions"]}',
                f'time_taken_seconds={a["time_taken_seconds"] or 0}',
            ]
        )
    for w in warnings:
        writer.writerow(
            [
                "warning",
                w["id"],
                w["created_at"],
                w["source"],
                f'{w["event_type"]} ({w["severity"] or "warning"})',
                w["details"] or "",
            ]
        )

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(
        mem,
        as_attachment=True,
        download_name="my_history_export.csv",
        mimetype="text/csv",
    )


def main() -> None:
    init_db()
    # Student-side mic monitoring runs in browser.
    # Keep server-side audio monitor optional to avoid logging server-room noise.
    if os.getenv("ENABLE_SERVER_AUDIO_MONITOR", "0") == "1":
        audio_monitor.start()
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
