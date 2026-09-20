from fastapi import FastAPI, UploadFile, File, HTTPException
from bs4 import BeautifulSoup
import re

app = FastAPI()

# Stores imported courses in memory
courses = {}


def normalize_course_code(code):
    """Convert codes like 'COSC 3506' and 'COSC3506' to 'COSC3506'."""
    return re.sub(r"\s+", "", code).upper()


def extract_course_codes(text):
    """Extract course codes from prerequisite/cross-listed text."""
    if not text or text.strip().lower() == "none":
        return []

    # Matches codes such as COSC 2006, ITEC 3506, ABCD 1234
    matches = re.findall(r"\b[A-Za-z]{2,}\s*\d{3,4}[A-Za-z]?\b", text)

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
            "cross_listed": extract_course_codes(cross_listed_text)
        }

        courses[course_code] = course
        imported += 1

    if imported == 0:
        raise HTTPException(status_code=400, detail="No courses found")

    return {
        "message": "Catalog imported successfully",
        "courses_imported": imported
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    normalized_code = normalize_course_code(course_code)

    if normalized_code not in courses:
        raise HTTPException(status_code=404, detail="Course not found")

    return courses[normalized_code]