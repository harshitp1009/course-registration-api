import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI()

# Stores imported courses in memory
courses = {}


def normalize_course_code(code):
    """Convert codes like 'COSC 3506' and 'COSC-3506' to 'COSC3506'."""
    return re.sub(r"[\s-]+", "", code).upper()


def extract_course_codes(text):
    """Extract course codes from prerequisite/cross-listed text."""
    if not text or text.strip().lower() == "none":
        return []

    # Matches codes such as COSC 2006, ITEC 3506, ABCD 1234
    matches = re.findall(r"\b[A-Za-z]{2,}[\s-]*\d{3,4}[A-Za-z]?\b", text)

    return [normalize_course_code(code) for code in matches]


@app.get("/")
def home():
    return {"message": "Course Registration API is running"}


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    content = await file.read()

    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid HTML file")

    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")

    if table is None:
        raise HTTPException(status_code=400, detail="No course table found")

    rows = table.find_all("tr")
    imported = 0

    for row in rows:
        cells = row.find_all("td")

        # Course rows must contain the five expected columns
        if len(cells) < 5:
            continue

        course_code_text = cells[0].get_text(" ", strip=True)
        title = cells[1].get_text(" ", strip=True)
        credits_text = cells[2].get_text(" ", strip=True)
        prerequisites_text = cells[3].get_text(" ", strip=True)
        cross_listed_text = cells[4].get_text(" ", strip=True)

        if not course_code_text:
            continue

        course_code = normalize_course_code(course_code_text)

        try:
            credits = int(credits_text)
        except ValueError:
            try:
                credits = float(credits_text)
            except ValueError:
                credits = credits_text

        course = {
            "course_code": course_code,
            "title": title,
            "credits": credits,
            "prerequisites": extract_course_codes(prerequisites_text),
            "cross_listed": extract_course_codes(cross_listed_text),
        }

        courses[course_code] = course
        imported += 1

    if imported == 0:
        raise HTTPException(status_code=400, detail="No courses found")

    return {"message": "Catalog imported successfully", "courses_imported": imported}


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    normalized_code = normalize_course_code(course_code)

    if normalized_code not in courses:
        raise HTTPException(status_code=404, detail="Course not found")

    return courses[normalized_code]


# Phase 2: each student has an independent history and future plan.
# In-memory state is cleared when the service restarts; use one server worker.
app.state.students = {}


class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryUpdate(BaseModel):
    history: list[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanUpdate(BaseModel):
    planned_courses: list[PlannedCourse]


def grade_priority(grade):
    """Numeric grade > letter grade > P or blank. Grade is not returned."""
    grade = grade.strip()
    try:
        if Decimal(grade).is_finite():
            return 2
    except InvalidOperation:
        pass
    return 1 if grade and grade.upper() != "P" else 0


def transcript_credits(text):
    """Return integer credits, including zero for blank or nonnumeric cells."""
    try:
        value = Decimal(text.strip())
        return int(value) if value.is_finite() else 0
    except (InvalidOperation, ValueError, OverflowError):
        return 0


def parse_transcript(content):
    soup = BeautifulSoup(content, "html.parser")
    selected = {}
    found_table = False
    required = {"status", "course", "grade", "term", "credits"}

    for table in soup.find_all("table"):
        # Ignore rows belonging to nested tables when examining this table.
        rows = [
            row for row in table.find_all("tr") if row.find_parent("table") is table
        ]
        columns = None
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            values = [cell.get_text(" ", strip=True) for cell in cells]
            names = [value.casefold() for value in values]
            if required.issubset(names):
                columns = {name: names.index(name) for name in required}
                found_table = True
                continue
            if columns is None or len(values) <= max(columns.values()):
                continue

            status = values[columns["status"]]
            term = values[columns["term"]]
            if status not in {"Completed", "In-Progress", "Attempted"} or not term:
                continue

            # Preserve the transcript code and term, including hyphens.
            course_code = values[columns["course"]]
            credits = transcript_credits(values[columns["credits"]])
            priority = (grade_priority(values[columns["grade"]]), credits)
            key = (course_code, term)
            if key not in selected or priority > selected[key][0]:
                selected[key] = (
                    priority,
                    {
                        "course_code": course_code,
                        "term": term,
                        "credits_earned": credits,
                        "status": status,
                    },
                )

    if not found_table:
        raise HTTPException(status_code=400, detail="No transcript tables found")
    return [record for priority, record in selected.values()]


def existing_student(student_id: str):
    student = app.state.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    content = await file.read()
    history = parse_transcript(content)
    student = app.state.students.setdefault(student_id, {"history": [], "plan": []})
    student["history"] = history
    return {"status": "success", "past_courses_imported": len(history)}


@app.put("/api/v1/students/{student_id}/history")
async def update_history(
    body: HistoryUpdate, student: dict = Depends(existing_student)
):
    student["history"] = [course.model_dump() for course in body.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history", status_code=204)
async def delete_history(student: dict = Depends(existing_student)):
    student["history"] = []


@app.post("/api/v1/students/{student_id}/plan")
async def create_plan(body: PlanUpdate, student: dict = Depends(existing_student)):
    student["plan"] = [course.model_dump() for course in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(student["plan"])}


@app.put("/api/v1/students/{student_id}/plan")
async def replace_plan(body: PlanUpdate, student: dict = Depends(existing_student)):
    student["plan"] = [course.model_dump() for course in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(student["plan"])}


@app.delete("/api/v1/students/{student_id}/plan", status_code=204)
async def delete_plan(student: dict = Depends(existing_student)):
    student["plan"] = []


@app.get("/api/v1/students/{student_id}/profile")
async def get_profile(student_id: str, student: dict = Depends(existing_student)):
    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }


SEASON_ORDER = {"W": 0, "SP": 1, "S": 2, "F": 3}
GRADUATION_CREDITS = 120
UNKNOWN_TERM = (100, 0)


def term_key(term):
    match = re.fullmatch(r"([0-9]{2})(W|SP|S|F)", term.strip().upper())
    if match is None:
        return UNKNOWN_TERM
    year, season = match.groups()
    return int(year), SEASON_ORDER[season]


def display_course_code(code):
    return re.sub(r"^([A-Z]+)([0-9])", r"\1-\2", code)


@app.get("/api/v1/students/{student_id}/audit-report")
async def audit_report(
    student_id: str, strict: bool = False, student: dict = Depends(existing_student)
):
    first_completion = {}
    latest_credit = {}
    for attempt in student["history"]:
        if attempt["status"] != "Completed":
            continue
        code = normalize_course_code(attempt["course_code"])
        when = term_key(attempt["term"])
        first_completion[code] = min(when, first_completion.get(code, UNKNOWN_TERM))
        # Prefer the latest completed attempt, using the higher credits to break ties.
        credit = (when != UNKNOWN_TERM, when, attempt["credits_earned"])
        latest_credit[code] = max(credit, latest_credit.get(code, credit))

    cross_listed = defaultdict(set)
    for code, course in courses.items():
        for alias in course["cross_listed"]:
            if alias != code:
                cross_listed[code].add(alias)
                cross_listed[alias].add(code)

    errors_by_term = defaultdict(list)
    cross_list_violations = []
    total_planned = 0
    for planned in student["plan"]:
        course_code = planned["course_code"]
        code = normalize_course_code(course_code)
        term = planned["term"]
        when = term_key(term)
        course = courses.get(code, {})
        credits = course.get("credits", 0)
        if isinstance(credits, (int, float)):
            total_planned += credits

        for prerequisite in dict.fromkeys(course.get("prerequisites", [])):
            completed = first_completion.get(prerequisite, UNKNOWN_TERM)
            if when == UNKNOWN_TERM or completed >= when:
                errors_by_term[term].append(
                    {
                        "course_code": course_code,
                        "type": "MISSING_PREREQUISITE",
                        "message": f"Missing prerequisite: {display_course_code(prerequisite)}",
                    }
                )

        for alias in sorted(cross_listed[code].intersection(first_completion)):
            cross_list_violations.append(
                {
                    "course_code": course_code,
                    "type": "CROSS_LIST_CONFLICT",
                    "message": f"Cross-listed with completed course {display_course_code(alias)}",
                }
            )

    timeline = [
        {"term": term, "errors": errors_by_term[term]}
        for term in sorted(errors_by_term, key=term_key)
    ]
    total_earned = sum(credits for _, _, credits in latest_credit.values())
    status = "ok"
    if timeline or cross_list_violations:
        status = "failed" if strict else "warning"

    return {
        "student_id": student_id,
        "status": status,
        "timeline_validation": timeline,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": max(
                0, GRADUATION_CREDITS - total_earned - total_planned
            ),
        },
    }
