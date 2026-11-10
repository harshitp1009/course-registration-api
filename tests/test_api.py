import os

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app, courses

CATALOG = b"""
<table>
<tr><th>Course Code</th><th>Title</th><th>Credits</th>
<th>Prerequisites</th><th>Cross-listed</th></tr>
<tr><td>COSC 1000</td><td>Intro</td><td>3</td><td>None</td><td></td></tr>
<tr><td>COSC-2000</td><td>Intermediate</td><td>3.5</td>
<td>cosc - 1000, COSC1000</td><td></td></tr>
<tr><td>COSC 3000</td><td>Advanced</td><td>4</td>
<td>COSC 2000, COSC-1000</td><td>ITEC-3000</td></tr>
<tr><td>ITEC 3000</td><td>Advanced</td><td>4</td><td></td><td></td></tr>
<tr><td>COSC 4000</td><td>Capstone</td><td>3</td>
<td>COSC-3000</td><td></td></tr>
<tr><td>COSC 4999</td><td>Variable credit</td><td>TBD</td><td></td><td></td></tr>
<tr><td></td><td></td><td></td><td></td><td></td></tr>
</table>
"""

TRANSCRIPT = b"""
<table><tr><th>Other table</th></tr></table>
<table>
<tr><th>Status</th><th>Course</th><th>Grade</th><th>Term</th><th>Credits</th></tr>
<tr><td>Attempted</td><td>COSC-1000</td><td>F</td><td>24F</td><td>3</td></tr>
<tr><td>Completed</td><td>COSC-1000</td><td>P</td><td>25W</td><td></td></tr>
<tr><td>Completed</td><td>COSC-1000</td><td>80</td><td>25W</td><td>3</td></tr>
<tr><td>In-Progress</td><td>ITEC-3000</td><td></td><td>26SP</td><td>0</td></tr>
<tr><td>Ignored</td><td>COSC-4000</td><td>A</td><td>26W</td><td>3</td></tr>
<tr><td>Completed</td><td>COSC-4000</td></tr>
</table>
"""


@pytest.fixture
def client():
    base_url = os.getenv("API_BASE_URL")
    if base_url:
        with httpx.Client(base_url=base_url) as client:
            yield client
    else:
        app.state.students.clear()
        courses.clear()
        with TestClient(app) as client:
            yield client


def upload(client, path, content):
    return client.post(path, files={"file": ("sample.html", content, "text/html")})


def course(code, term):
    return {"course_code": code, "term": term}


def attempt(code, term, credits, status="Completed"):
    return {**course(code, term), "credits_earned": credits, "status": status}


@pytest.fixture
def student(client, request):
    assert upload(client, "/api/v1/admin/catalog/import", CATALOG).status_code == 200
    path = f"/api/v1/students/{request.node.name}"
    response = upload(client, f"{path}/history/import", TRANSCRIPT)
    assert response.status_code == 201
    assert response.json()["past_courses_imported"] == 3
    return path


def test_catalog_api(client):
    assert client.get("/").status_code == 200
    path = "/api/v1/admin/catalog/import"
    assert upload(client, path, b"\xff").status_code == 400
    assert upload(client, path, b"<p>No table</p>").status_code == 400
    assert upload(client, path, b"<table></table>").status_code == 400
    assert upload(client, path, CATALOG).json()["courses_imported"] == 6
    response = client.get("/api/v1/catalog/courses/cosc-2000")
    assert response.status_code == 200
    assert response.json()["credits"] == 3.5
    assert set(response.json()["prerequisites"]) == {"COSC1000"}
    assert client.get("/api/v1/catalog/courses/XXXX1000").status_code == 404


def test_history_plan_and_profile(client, student):
    assert (
        upload(client, f"{student}/history/import", b"<table></table>").status_code
        == 400
    )
    history = client.get(f"{student}/profile").json()["history"]
    assert history == [
        attempt("COSC-1000", "24F", 3, "Attempted"),
        attempt("COSC-1000", "25W", 3),
        attempt("ITEC-3000", "26SP", 0, "In-Progress"),
    ]
    payload = {"planned_courses": [course("COSC-2000", "26S")]}
    assert (
        client.post(f"{student}/plan", json=payload).json()["planned_courses_saved"]
        == 1
    )
    assert (
        client.put(f"{student}/plan", json={"planned_courses": []}).json()[
            "planned_courses_saved"
        ]
        == 0
    )
    assert client.delete(f"{student}/plan").status_code == 204
    assert client.delete(f"{student}/history").status_code == 204
    assert client.get(f"{student}/profile").json()["history"] == []
    assert (
        client.get("/api/v1/students/no-such-student/audit-report").status_code == 404
    )


def test_audit_chronology_and_cross_listings(client, student):
    history = [
        attempt("COSC-1000", "24F", 9, "Attempted"),
        attempt("COSC 1000", "25W", 3),
        attempt("cosc-1000", "26F", 3),
        attempt("COSC 1000", "27W", 9, "In-Progress"),
        attempt("cosc-2000", "26S", 3),
        attempt("COSC 3000", "26F", 4),
    ]
    assert (
        client.put(f"{student}/history", json={"history": history}).status_code == 200
    )
    planned = [
        course("COSC-4000", "26F"),
        course("COSC-3000", "26S"),
        course("COSC-3000", "26SP"),
        course("COSC-2000", "25W"),
        course("itec - 3000", "27W"),
        course("COSC-4000", "27W"),
    ]
    assert (
        client.post(f"{student}/plan", json={"planned_courses": planned}).status_code
        == 200
    )
    path = f"{student}/audit-report"
    report = client.get(path).json()
    assert report["student_id"] == student.rsplit("/", 1)[-1]
    assert report["status"] == "warning"
    assert client.get(path, params={"strict": True}).json()["status"] == "failed"
    assert client.get(path, params={"strict": False}).json() == report
    assert [entry["term"] for entry in report["timeline_validation"]] == [
        "25W",
        "26SP",
        "26S",
        "26F",
    ]
    assert [
        error for entry in report["timeline_validation"] for error in entry["errors"]
    ] == [
        {
            "course_code": code,
            "type": "MISSING_PREREQUISITE",
            "message": f"Missing prerequisite: {prereq}",
        }
        for code, prereq in [
            ("COSC-2000", "COSC-1000"),
            ("COSC-3000", "COSC-2000"),
            ("COSC-3000", "COSC-2000"),
            ("COSC-4000", "COSC-3000"),
        ]
    ]
    assert report["cross_list_violations"] == [
        {
            "course_code": "itec - 3000",
            "type": "CROSS_LIST_CONFLICT",
            "message": "Cross-listed with completed course COSC-3000",
        }
    ]
    assert report["credit_summary"] == {
        "total_earned": 10,
        "total_planned": 21.5,
        "total_remaining_for_graduation": 88.5,
    }


def test_clean_report_and_credit_floor(client, student):
    assert (
        client.put(
            f"{student}/history",
            json={
                "history": [
                    attempt("COSC1000", "25W", 3),
                    attempt("COSC-1000", "26F", 118),
                ]
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{student}/plan",
            json={
                "planned_courses": [
                    course("cosc2000", "25SP"),
                    course("COSC4999", "26F"),
                    course("UNKNOWN", "26F"),
                ]
            },
        ).status_code
        == 200
    )
    response = client.get(f"{student}/audit-report", params={"strict": True})
    assert response.status_code == 200
    assert response.json() == {
        "student_id": student.rsplit("/", 1)[-1],
        "status": "ok",
        "timeline_validation": [],
        "cross_list_violations": [],
        "credit_summary": {
            "total_earned": 118,
            "total_planned": 3.5,
            "total_remaining_for_graduation": 0,
        },
    }
