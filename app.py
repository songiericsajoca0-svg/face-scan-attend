from __future__ import annotations

"""Automatic Face Attendance System — pure Python WSGI + MongoDB Atlas.

This module intentionally does not use Flask or another web framework.  The
``app`` callable is a standard WSGI application, which Vercel's Python runtime
can run directly.  MongoDB Atlas provides all durable application storage.
"""

import base64
import binascii
import csv
import hashlib
import hmac
import html
import io
import json
import math
import mimetypes
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode
from wsgiref.simple_server import make_server
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from pymongo import ASCENDING, MongoClient
from pymongo.errors import ConfigurationError, DuplicateKeyError, PyMongoError


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "public" / "static"
DEFAULT_LOCAL_SECRET_KEY = "change-this-local-attendance-secret"
FLASH_COOKIE = "school_attendance_flash"
FLASH_TTL_SECONDS = 600
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_CLOUD_IMAGE_BYTES = 1 * 1024 * 1024
DESCRIPTOR_LENGTH = 128
MIN_REGISTRATION_SAMPLES = 1
MATCH_DISTANCE_THRESHOLD = 0.48


# A dependency-free .env reader is useful for local laptop use. Vercel injects
# its environment variables itself, so a deployment never reads local files.
def load_local_environment() -> None:
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        return
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_environment()

# Windows installations can lack IANA timezone files. The Philippines has a
# fixed UTC+08:00 offset and no daylight-saving time.
try:
    MANILA_TZ = ZoneInfo("Asia/Manila")
except ZoneInfoNotFoundError:
    MANILA_TZ = timezone(timedelta(hours=8), name="Asia/Manila")


class MongoDatabaseError(RuntimeError):
    """Safe wrapper for a MongoDB connection or schema error."""


class RequestBodyTooLarge(RuntimeError):
    """Raised before a request body exceeds Vercel-friendly limits."""


class BadRequest(RuntimeError):
    """Raised for malformed request data."""


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


class Request:
    """Minimal request object built only from the standard WSGI environment."""

    def __init__(self, environ: Mapping[str, Any]) -> None:
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        self.path = str(environ.get("PATH_INFO") or "/")
        self.query = {
            key: values[-1] if values else ""
            for key, values in parse_qs(
                str(environ.get("QUERY_STRING") or ""), keep_blank_values=True
            ).items()
        }
        self._body: bytes | None = None
        self._data: dict[str, Any] | None = None

    def header(self, name: str, default: str = "") -> str:
        key = "HTTP_" + name.upper().replace("-", "_")
        if name.lower() == "content-type":
            key = "CONTENT_TYPE"
        elif name.lower() == "content-length":
            key = "CONTENT_LENGTH"
        return str(self.environ.get(key) or default)

    @property
    def body(self) -> bytes:
        if self._body is not None:
            return self._body
        raw_length = self.environ.get("CONTENT_LENGTH")
        try:
            length = int(raw_length) if raw_length else 0
        except (TypeError, ValueError):
            raise BadRequest("The request length is invalid.")
        if length > MAX_REQUEST_BYTES:
            raise RequestBodyTooLarge()
        stream = self.environ.get("wsgi.input")
        if stream is None:
            self._body = b""
            return self._body
        if length:
            body = stream.read(length)
        else:
            body = stream.read(MAX_REQUEST_BYTES + 1)
        if len(body) > MAX_REQUEST_BYTES:
            raise RequestBodyTooLarge()
        self._body = body
        return body

    @property
    def data(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        content_type = self.header("Content-Type").split(";", 1)[0].strip().lower()
        if not self.body:
            self._data = {}
            return self._data
        if content_type == "application/json":
            try:
                parsed = json.loads(self.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BadRequest("The JSON request is invalid.") from error
            if not isinstance(parsed, dict):
                raise BadRequest("The JSON request must be an object.")
            self._data = parsed
            return self._data
        if content_type in {
            "application/x-www-form-urlencoded",
            "",
        }:
            try:
                parsed_form = parse_qs(
                    self.body.decode("utf-8"), keep_blank_values=True
                )
            except UnicodeDecodeError as error:
                raise BadRequest("The form request is invalid.") from error
            self._data = {
                key: values[-1] if values else "" for key, values in parsed_form.items()
            }
            return self._data
        raise BadRequest("Unsupported request content type.")


# ---------------------------------------------------------------------------
# Runtime configuration and MongoDB Atlas
# ---------------------------------------------------------------------------
def environment_text(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return str(value).strip() if value is not None else default


def app_access_password() -> str:
    # Do not strip user passwords because whitespace may be intentional.
    value = os.environ.get("APP_ACCESS_PASSWORD", "")
    return str(value) if value is not None else ""


def mongodb_uri() -> str:
    return environment_text("MONGODB_URI")


def mongodb_database_name() -> str:
    return environment_text("MONGODB_DB")


def secret_key() -> str:
    return environment_text("SECRET_KEY", DEFAULT_LOCAL_SECRET_KEY)


def is_vercel_deployment() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def valid_mongodb_database_name(name: str) -> bool:
    return bool(name) and len(name.encode("utf-8")) <= 64 and not any(
        character in name for character in ('/', '\\', '.', '"', '$', '\x00')
    )


def mongodb_configuration_missing(include_cloud_security: bool) -> list[str]:
    missing: list[str] = []
    uri = mongodb_uri()
    if not uri or not uri.startswith(("mongodb://", "mongodb+srv://")):
        missing.append("MONGODB_URI")
    if not valid_mongodb_database_name(mongodb_database_name()):
        missing.append("MONGODB_DB")
    if include_cloud_security:
        if secret_key() == DEFAULT_LOCAL_SECRET_KEY:
            missing.append("SECRET_KEY")
        if not app_access_password():
            missing.append("APP_ACCESS_PASSWORD")
    return missing


MONGO_CLIENT: MongoClient | None = None
MONGO_CLIENT_URI: str | None = None
MONGO_CLIENT_LOCK = threading.Lock()
MONGO_SCHEMA_INITIALIZED = False
MONGO_SCHEMA_LOCK = threading.Lock()


def get_mongo_client() -> MongoClient:
    """Reuse one lazy Atlas client per warm Python Function instance."""
    global MONGO_CLIENT, MONGO_CLIENT_URI, MONGO_SCHEMA_INITIALIZED
    uri = mongodb_uri()
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise MongoDatabaseError("MongoDB has not been configured.")
    if MONGO_CLIENT is not None and MONGO_CLIENT_URI == uri:
        return MONGO_CLIENT

    with MONGO_CLIENT_LOCK:
        if MONGO_CLIENT is not None and MONGO_CLIENT_URI == uri:
            return MONGO_CLIENT
        previous_client = MONGO_CLIENT
        try:
            client = MongoClient(
                uri,
                appname="school-attendance",
                connect=False,
                serverSelectionTimeoutMS=10_000,
                connectTimeoutMS=10_000,
                socketTimeoutMS=15_000,
                retryWrites=True,
            )
        except (ConfigurationError, ValueError) as error:
            raise MongoDatabaseError("MongoDB connection settings are invalid.") from error
        MONGO_CLIENT = client
        MONGO_CLIENT_URI = uri
        MONGO_SCHEMA_INITIALIZED = False
        if previous_client is not None:
            try:
                previous_client.close()
            except Exception:
                pass
        return client


def get_db() -> Any:
    name = mongodb_database_name()
    if not valid_mongodb_database_name(name):
        raise MongoDatabaseError("MongoDB database name has not been configured.")
    return get_mongo_client()[name]


def initialize_mongodb() -> None:
    """Create MongoDB collections, indexes, and defaults after authentication."""
    global MONGO_SCHEMA_INITIALIZED
    if MONGO_SCHEMA_INITIALIZED:
        return
    with MONGO_SCHEMA_LOCK:
        if MONGO_SCHEMA_INITIALIZED:
            return
        try:
            client = get_mongo_client()
            client.admin.command("ping")
            db = get_db()
            db.students.create_index(
                [("student_number_key", ASCENDING)],
                unique=True,
                name="student_number_key_unique",
            )
            db.students.create_index(
                [("active", ASCENDING), ("last_name", ASCENDING), ("first_name", ASCENDING)],
                name="active_student_name",
            )
            db.attendance.create_index(
                [("student_id", ASCENDING), ("attendance_date", ASCENDING)],
                unique=True,
                name="one_daily_attendance_per_student",
            )
            db.attendance.create_index(
                [("attendance_date", ASCENDING), ("checked_in_at", ASCENDING)],
                name="attendance_date_time",
            )
            defaults = {
                "school_name": "Class Attendance",
                "class_name": "School Year 2026–2027",
                "late_cutoff": "08:00",
                "checkout_time": "17:00",
            }
            for key, value in defaults.items():
                db.settings.update_one(
                    {"_id": key}, {"$setOnInsert": {"value": value}}, upsert=True
                )
        except (PyMongoError, ValueError) as error:
            # Never put the URI, credentials, or detailed driver message in logs.
            print(f"MongoDB initialization failed: {type(error).__name__}")
            raise MongoDatabaseError("MongoDB could not be reached.") from error
        MONGO_SCHEMA_INITIALIZED = True


# ---------------------------------------------------------------------------
# MongoDB document helpers
# ---------------------------------------------------------------------------
def object_id_from_value(value: object) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        text = str(value)
    except Exception:
        return None
    return ObjectId(text) if ObjectId.is_valid(text) else None


def document_id(document: Mapping[str, Any]) -> str:
    value = document.get("id", document.get("_id", ""))
    return str(value) if value is not None else ""


def full_name_for(student: Mapping[str, Any]) -> str:
    parts = [
        str(student.get("first_name") or "").strip(),
        str(student.get("middle_name") or "").strip(),
        str(student.get("last_name") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def student_value(student: Mapping[str, Any], key: str, fallback: str = "Not specified") -> str:
    value = student.get(key)
    text = str(value).strip() if value is not None else ""
    return text or fallback


def grade_for(student: Mapping[str, Any]) -> str:
    value = student_value(student, "grade_level", "")
    return value or student_value(student, "grade_section")


def section_for(student: Mapping[str, Any]) -> str:
    return student_value(student, "section_name", "Not specified")


def initials(student: Mapping[str, Any]) -> str:
    first = str(student.get("first_name") or "").strip()
    last = str(student.get("last_name") or "").strip()
    return ((first[:1] + last[:1]).upper() or "ST")


def serialize_student(student: Mapping[str, Any]) -> dict[str, Any]:
    photo = student.get("photo_data") or student.get("photo_filename") or ""
    grade_level = str(student.get("grade_level") or "").strip()
    section_name = str(student.get("section_name") or "").strip()
    grade_section = str(student.get("grade_section") or "").strip()
    if not grade_section and grade_level and section_name:
        grade_section = f"{grade_level} · {section_name}"
    return {
        "id": document_id(student),
        "student_number": str(student.get("student_number") or ""),
        "first_name": str(student.get("first_name") or ""),
        "middle_name": str(student.get("middle_name") or ""),
        "last_name": str(student.get("last_name") or ""),
        "full_name": full_name_for(student),
        "gender": str(student.get("gender") or ""),
        "grade_level": grade_level,
        "section_name": section_name,
        "grade_section": grade_section,
        "email": str(student.get("email") or ""),
        "phone": str(student.get("phone") or ""),
        "guardian_name": str(student.get("guardian_name") or ""),
        "address": str(student.get("address") or ""),
        "photo_data": str(photo),
        "photo_filename": str(photo),
        "face_descriptor": student.get("face_descriptor") or [],
        "active": bool(student.get("active", True)),
        "created_at": str(student.get("created_at") or ""),
    }


def get_student(student_id: object, include_archived: bool = True) -> dict[str, Any] | None:
    object_id = object_id_from_value(student_id)
    if object_id is None:
        return None
    query: dict[str, Any] = {"_id": object_id}
    if not include_archived:
        query["active"] = True
    return get_db().students.find_one(query)


def get_setting(key: str, default: str = "") -> str:
    row = get_db().settings.find_one({"_id": key})
    if not row:
        return default
    value = row.get("value")
    return str(value) if value is not None else default


def set_setting(key: str, value: str) -> None:
    get_db().settings.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


def attendance_view(attendance: Mapping[str, Any], student: Mapping[str, Any]) -> dict[str, Any]:
    view = serialize_student(student)
    view.update(
        {
            "attendance_id": str(attendance.get("_id") or attendance.get("attendance_id") or ""),
            "attendance_date": str(attendance.get("attendance_date") or ""),
            "checked_in_at": str(attendance.get("checked_in_at") or ""),
            "checked_out_at": str(attendance.get("checked_out_at") or "") or None,
            "status": str(attendance.get("status") or ""),
            "face_photo_data": str(attendance.get("face_photo_data") or ""),
            "attendance_photo_filename": str(
                attendance.get("face_photo_data") or attendance.get("attendance_photo_filename") or ""
            ),
            "check_out_photo_filename": str(attendance.get("check_out_photo_data") or ""),
            "notes": str(attendance.get("notes") or ""),
        }
    )
    return view


def student_documents_for_attendance(rows: Sequence[Mapping[str, Any]]) -> dict[ObjectId, dict[str, Any]]:
    identifiers = [
        student_id
        for row in rows
        if isinstance((student_id := row.get("student_id")), ObjectId)
    ]
    if not identifiers:
        return {}
    students = get_db().students.find({"_id": {"$in": identifiers}})
    return {student["_id"]: student for student in students}


def attendance_views_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    students = student_documents_for_attendance(rows)
    views: list[dict[str, Any]] = []
    for row in rows:
        identifier = row.get("student_id")
        student = students.get(identifier) if isinstance(identifier, ObjectId) else None
        if student:
            views.append(attendance_view(row, student))
    return views


def student_matches_query(student: Mapping[str, Any], query: str) -> bool:
    needle = query.casefold().strip()
    if not needle:
        return True
    fields = (
        "student_number",
        "first_name",
        "middle_name",
        "last_name",
        "gender",
        "grade_level",
        "section_name",
        "grade_section",
    )
    return any(needle in str(student.get(name) or "").casefold() for name in fields)


def safe_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return today_iso()


def fetch_attendance_records(date_value: str, status: str = "", query: str = "") -> list[dict[str, Any]]:
    database_query: dict[str, Any] = {"attendance_date": safe_date(date_value)}
    if status in ("Present", "Late"):
        database_query["status"] = status
    rows = list(get_db().attendance.find(database_query).sort("checked_in_at", ASCENDING))
    records = attendance_views_from_rows(rows)
    return [record for record in records if student_matches_query(record, query)] if query else records


def recent_attendance_for_student(student_id: ObjectId, limit: int = 6) -> list[dict[str, Any]]:
    rows = list(get_db().attendance.find({"student_id": student_id}).sort("checked_in_at", -1).limit(limit))
    student = get_db().students.find_one({"_id": student_id})
    return [attendance_view(row, student) for row in rows] if student else []


# ---------------------------------------------------------------------------
# Time, image, and face descriptor helpers
# ---------------------------------------------------------------------------
def manila_now() -> datetime:
    return datetime.now(MANILA_TZ)


def today_iso() -> str:
    return manila_now().date().isoformat()


def cutoff_time() -> str:
    value = get_setting("late_cutoff", "08:00")
    try:
        datetime.strptime(value, "%H:%M")
        return value
    except ValueError:
        return "08:00"


def attendance_status(timestamp: datetime) -> str:
    cutoff = datetime.strptime(cutoff_time(), "%H:%M").time()
    return "Late" if timestamp.time() > cutoff else "Present"


def checkout_time() -> str:
    value = get_setting("checkout_time", "17:00")
    try:
        datetime.strptime(value, "%H:%M")
        return value
    except ValueError:
        return "17:00"


def default_scan_mode() -> str:
    cutoff = datetime.strptime(checkout_time(), "%H:%M").time()
    return "time_out" if manila_now().time() >= cutoff else "time_in"


def is_data_image(value: object) -> bool:
    return isinstance(value, str) and value.startswith("data:image/") and "," in value


def save_data_image(data_url: object) -> str:
    """Validate a compact canvas capture before saving it in MongoDB."""
    if not isinstance(data_url, str) or not data_url:
        raise ValueError("Capture a photo with the face scanner first.")
    if not data_url.startswith("data:image/") or "," not in data_url:
        raise ValueError("The camera image is invalid.")
    header, encoded = data_url.split(",", 1)
    mime = header.split(";", 1)[0].lower()
    if mime not in {"data:image/png", "data:image/jpeg", "data:image/jpg", "data:image/webp"}:
        raise ValueError("Only PNG, JPG, or WEBP images are allowed.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("The camera image could not be read.") from error
    if not content or len(content) > MAX_CLOUD_IMAGE_BYTES:
        raise ValueError("The image is too large. Please scan again.")
    return data_url


def stored_image_response(data_url: str) -> HttpResponse:
    if not is_data_image(data_url):
        raise KeyError("Image not found")
    header, encoded = data_url.split(",", 1)
    mime = header.split(";", 1)[0][5:].lower()
    if mime not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise KeyError("Image not found")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise KeyError("Image not found") from error
    return HttpResponse(
        200,
        content,
        mime,
        [("Cache-Control", "private, no-store")],
    )


def student_photo_url(student: Mapping[str, Any]) -> str | None:
    photo = student.get("photo_data") or student.get("photo_filename")
    identifier = document_id(student)
    if is_data_image(photo) and identifier:
        return f"/students/{quote(identifier, safe='')}/face-photo"
    return None


def attendance_photo_url(record: Mapping[str, Any]) -> str | None:
    photo = record.get("face_photo_data") or record.get("attendance_photo_filename")
    identifier = str(record.get("attendance_id") or record.get("_id") or "")
    if is_data_image(photo) and identifier:
        return f"/attendance/{quote(identifier, safe='')}/face-photo"
    return None


def clean_descriptor(value: object) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != DESCRIPTOR_LENGTH:
        raise ValueError("The face profile data is invalid. Capture the face profile again.")
    values: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError) as error:
            raise ValueError("The face profile data is invalid. Capture the face profile again.") from error
        if not math.isfinite(number):
            raise ValueError("The face profile data is invalid. Capture the face profile again.")
        values.append(number)
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude < 0.00001:
        raise ValueError("The face profile data is invalid. Capture the face profile again.")
    return [value / magnitude for value in values]


def face_descriptor_from_capture(raw_value: object) -> list[float]:
    try:
        samples = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("The face profile could not be read. Capture the face profile again.") from error
    if not isinstance(samples, list) or len(samples) != MIN_REGISTRATION_SAMPLES:
        raise ValueError("Exactly one clear, front-facing face capture is required for registration.")
    return clean_descriptor(samples[0])


def descriptor_from_db(raw_value: object) -> list[float] | None:
    if not raw_value:
        return None
    try:
        value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        return clean_descriptor(value)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def descriptor_distance(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def student_payload(student: Mapping[str, Any], include_descriptor: bool = False) -> dict[str, Any]:
    view = serialize_student(student)
    payload: dict[str, Any] = {
        "id": view["id"],
        "student_number": view["student_number"],
        "full_name": view["full_name"],
        "gender": student_value(view, "gender"),
        "grade_level": grade_for(view),
        "section_name": section_for(view),
        "grade_section": student_value(view, "grade_section"),
        "photo_url": student_photo_url(student),
        "initials": initials(view),
    }
    if include_descriptor:
        descriptor = descriptor_from_db(student.get("face_descriptor"))
        if descriptor is not None:
            payload["descriptor"] = descriptor
    return payload


def datetime_ph(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone(MANILA_TZ).strftime("%b %d, %Y · %I:%M %p")
    except (TypeError, ValueError):
        return value


def time_ph(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone(MANILA_TZ).strftime("%I:%M %p")
    except (TypeError, ValueError):
        return value


def date_ph(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%B %d, %Y")
    except (TypeError, ValueError):
        return value


def clock_ph(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Response, security, and HTML rendering helpers
# ---------------------------------------------------------------------------
def escaped(value: object) -> str:
    return html.escape(str(value) if value is not None else "", quote=True)


def html_response(markup: str, status: int = 200, headers: Sequence[tuple[str, str]] = ()) -> HttpResponse:
    return HttpResponse(status, markup.encode("utf-8"), "text/html; charset=utf-8", list(headers))


def json_response(payload: Mapping[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        "application/json; charset=utf-8",
        [("Cache-Control", "no-store")],
    )


def redirect_response(location: str, headers: Sequence[tuple[str, str]] = ()) -> HttpResponse:
    return HttpResponse(302, b"", "text/plain; charset=utf-8", [("Location", location), *headers])


def cookie_attributes() -> str:
    secure = "; Secure" if is_vercel_deployment() else ""
    return f"Path=/; HttpOnly; SameSite=Lax{secure}"


def flash_signature(payload: str) -> str:
    return hmac.new(secret_key().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def make_flash_cookie(category: str, message: str) -> str:
    data = json.dumps(
        {"category": category, "message": message, "created": int(datetime.now(timezone.utc).timestamp())},
        separators=(",", ":"),
    ).encode("utf-8")
    payload = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    return f"{payload}.{flash_signature(payload)}"


def set_flash_header(category: str, message: str) -> tuple[str, str]:
    return ("Set-Cookie", f"{FLASH_COOKIE}={make_flash_cookie(category, message)}; {cookie_attributes()}")


def clear_flash_header() -> tuple[str, str]:
    return ("Set-Cookie", f"{FLASH_COOKIE}=; Max-Age=0; {cookie_attributes()}")


def request_cookies(request: Request) -> dict[str, str]:
    cookies = SimpleCookie()
    try:
        cookies.load(request.header("Cookie"))
    except (AttributeError, ValueError):
        return {}
    return {name: morsel.value for name, morsel in cookies.items()}


def consume_flash(request: Request) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    value = request_cookies(request).get(FLASH_COOKIE)
    if not value or "." not in value:
        return None, []
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, flash_signature(payload)):
        return clear_flash_header(), []
    try:
        padded = payload + "=" * (-len(payload) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        age = int(datetime.now(timezone.utc).timestamp()) - int(parsed.get("created", 0))
        category = str(parsed.get("category", "error"))
        message = str(parsed.get("message", ""))
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        return clear_flash_header(), []
    if age < 0 or age > FLASH_TTL_SECONDS or category not in {"success", "error"} or not message:
        return clear_flash_header(), []
    return clear_flash_header(), [(category, message)]


def basic_authorized(request: Request) -> bool:
    header = request.header("Authorization")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    configured_username = environment_text("APP_ACCESS_USERNAME", "teacher") or "teacher"
    return hmac.compare_digest(username, configured_username) and hmac.compare_digest(
        password, app_access_password()
    )


def setup_page(missing: Sequence[str], cloud: bool) -> HttpResponse:
    items = "".join(f"<li><code>{escaped(name)}</code></li>" for name in missing)
    if cloud:
        title = "Cloud setup required"
        description = (
            "This attendance system needs durable MongoDB storage before it can run on Vercel. "
            "Add these environment variables in <strong>Vercel → Project Settings → "
            "Environment Variables</strong>, then redeploy:"
        )
        note = (
            "<strong>Privacy:</strong> APP_ACCESS_PASSWORD protects student records and "
            "face-profile data with a browser sign-in. Use a strong private password."
        )
        guide = "See <code>README.md</code> for MongoDB Atlas and Vercel setup steps."
    else:
        title = "MongoDB setup required"
        description = (
            "This local attendance system uses MongoDB for permanent storage. Create a private "
            "<code>.env</code> file beside <code>app.py</code> and add these variables:"
        )
        note = (
            "<strong>Privacy:</strong> Keep the MongoDB connection string private. "
            "Never commit your <code>.env</code> file to GitHub."
        )
        guide = "Copy <code>.env.example</code> to <code>.env</code>, add your private values, then restart the app."
    markup = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>body{{margin:0;background:#f4f8f7;color:#17242d;font-family:Arial,sans-serif}}main{{max-width:680px;margin:10vh auto;padding:32px;background:#fff;border:1px solid #dce7e8;border-radius:18px;box-shadow:0 12px 32px rgba(20,56,54,.08)}}h1{{margin-top:0;color:#115e59}}p,li{{line-height:1.6}}code{{padding:2px 5px;background:#edf6f3;border-radius:4px}}.note{{padding:13px 15px;background:#fff5d5;border-radius:10px}}</style></head><body><main><h1>{title}</h1><p>{description}</p><ul>{items}</ul><p class="note">{note}</p><p>{guide}</p></main></body></html>"""
    return html_response(markup, 503)


def database_error_page() -> HttpResponse:
    markup = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cloud database unavailable</title><style>body{margin:0;background:#f4f8f7;color:#17242d;font-family:Arial,sans-serif}main{max-width:680px;margin:10vh auto;padding:32px;background:#fff;border:1px solid #dce7e8;border-radius:18px;box-shadow:0 12px 32px rgba(20,56,54,.08)}h1{margin-top:0;color:#b83b39}p{line-height:1.6}.note{padding:13px 15px;background:#fff5d5;border-radius:10px}</style></head><body><main><h1>Cloud database unavailable</h1><p>The app could not connect to its MongoDB database. Check the MongoDB Atlas network access rule, database-user permissions, and the <strong>MONGODB_URI</strong> and <strong>MONGODB_DB</strong> variables, then redeploy.</p><p class="note">The detailed database error is intentionally hidden to protect connection information. Check the Vercel Function logs if the settings are already correct.</p></main></body></html>"""
    return html_response(markup, 503)


def app_error_page(status: int, title: str, message: str) -> HttpResponse:
    markup = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escaped(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body><main style="max-width:680px;margin:10vh auto;padding:32px"><section class="card card-pad empty"><div class="empty-icon">!</div><h1>{escaped(title)}</h1><p>{escaped(message)}</p><a class="btn btn-primary" href="/">Return to dashboard</a></section></main></body></html>"""
    return html_response(markup, status)


def authorize_and_prepare(request: Request) -> HttpResponse | None:
    if request.path.startswith("/static/"):
        return None

    cloud = is_vercel_deployment()

    missing = mongodb_configuration_missing(include_cloud_security=False)

    if missing:
        return setup_page(missing, cloud)

    try:
        initialize_mongodb()
    except MongoDatabaseError:
        return database_error_page()

    return None


def avatar_markup(student: Mapping[str, Any], class_name: str = "person-avatar") -> str:
    photo = student_photo_url(student)
    if photo:
        return f'<span class="{class_name}"><img src="{escaped(photo)}" alt=""></span>'
    return f'<span class="{class_name}">{escaped(initials(student))}</span>'


def status_markup(status: str) -> str:
    lowered = "late" if status == "Late" else "present" if status == "Present" else "archived"
    return f'<span class="status status-{lowered}">{escaped(status)}</span>'


def page_layout(request: Request, title: str, section: str, content: str, vendor_face_api: bool = False) -> HttpResponse:
    flash_header, flashes = consume_flash(request)
    flash_markup = "".join(
        f'<div class="flash flash-{escaped(category)}"><span>{"✓" if category == "success" else "!"}</span><p>{escaped(message)}</p><button type="button" class="flash-close" aria-label="Close">×</button></div>'
        for category, message in flashes
    )
    school_name = get_setting("school_name", "Class Attendance")
    class_name = get_setting("class_name", "School Year 2026–2027")
    nav = [
        ("dashboard", "/", "⌂", "Dashboard"),
        ("students", "/students", "♙", "Students"),
        ("attendance", "/attendance", "◉", "Face Attendance"),
        ("records", "/records", "▤", "Records"),
        ("settings", "/settings", "⚙", "Settings"),
    ]
    nav_markup = "".join(
        f'<a class="nav-link {"active" if name == section else ""}" href="{href}"><span class="nav-icon">{icon}</span><span>{label}</span></a>'
        for name, href, icon, label in nav
    )
    vendor = '<script src="/static/vendor/face-api.min.js"></script>' if vendor_face_api else ""
    markup = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#115e59"><title>{escaped(title)} · {escaped(school_name)}</title><link rel="stylesheet" href="/static/style.css"></head>
<body><div class="app-shell"><aside class="sidebar" id="sidebar" aria-label="Main navigation"><div class="brand"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M16 3 28 8.5 16 14 4 8.5 16 3Z" fill="currentColor"/><path d="M8 11.5V19c0 2.5 3.6 5 8 5s8-2.5 8-5v-7.5l-8 3.7-8-3.7Z" fill="currentColor" opacity=".72"/><path d="M28 9v9" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span><span class="brand-copy"><strong>{escaped(school_name)}</strong><small>{escaped(class_name)}</small></span></div><nav class="nav-list">{nav_markup}</nav><div class="sidebar-foot"><span class="live-dot"></span><span>MongoDB · durable storage</span></div></aside><section class="page-wrap"><header class="topbar"><button class="menu-button" id="menuButton" type="button" aria-label="Open menu" aria-controls="sidebar" aria-expanded="false">☰</button><div class="topbar-title"><span class="eyebrow">{escaped(class_name)}</span><strong>{escaped(school_name)}</strong></div><div class="date-chip" title="Asia/Manila timezone"><span aria-hidden="true">◷</span><span>{escaped(date_ph(today_iso()))}</span></div></header><main class="main-content"><div class="flash-stack" aria-live="polite">{flash_markup}</div>{content}</main></section></div><div class="nav-scrim" id="navScrim"></div>{vendor}<script src="/static/app.js"></script></body></html>"""
    headers = [flash_header] if flash_header else []
    return html_response(markup, headers=headers)


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------
def render_dashboard(request: Request) -> HttpResponse:
    db = get_db()
    current_date = today_iso()
    total_students = db.students.count_documents({"active": True})
    ready_profiles = db.students.count_documents({"active": True, "face_descriptor.0": {"$exists": True}})
    today_rows = list(db.attendance.find({"attendance_date": current_date}))
    recent_rows = sorted(today_rows, key=lambda row: str(row.get("checked_in_at") or ""), reverse=True)[:8]
    recent = attendance_views_from_rows(recent_rows)
    checked_out = sum(1 for row in today_rows if row.get("checked_out_at"))
    present = sum(1 for row in today_rows if row.get("status") == "Present")
    late = sum(1 for row in today_rows if row.get("status") == "Late")
    cutoff = cutoff_time()
    checkout = checkout_time()
    if recent:
        activities = "".join(
            f'<div class="activity-row">{avatar_markup(record)}<div class="person-meta"><strong>{escaped(record["full_name"])}</strong><small>{escaped(student_value(record, "gender"))} · {escaped(grade_for(record))} · {escaped(section_for(record))}{" · Checked out" if record["checked_out_at"] else ""}</small></div><div>{status_markup(record["status"])}<div class="activity-time">In {escaped(time_ph(record["checked_in_at"]))}{" · Out " + escaped(time_ph(record["checked_out_at"])) if record["checked_out_at"] else ""}</div></div></div>'
            for record in recent
        )
    else:
        activities = '<div class="empty"><div class="empty-icon">◉</div><h3>No Time In records yet</h3><p>Open Face Attendance and let the first registered student face the laptop camera.</p><a class="btn btn-primary" href="/attendance">Open face scanner</a></div>'
    content = f"""<section class="page-header"><div><span class="eyebrow">Automatic face-match overview</span><h1>Good day, teacher.</h1><p>Time In is Present at or before <strong>{escaped(clock_ph(cutoff))}</strong> and Late after that. Use the Check Out face scanner from <strong>{escaped(clock_ph(checkout))}</strong>.</p></div><div class="header-actions"><a class="btn btn-soft" href="/students/new"><span class="icon-plus">＋</span> Register face profile</a><a class="btn btn-primary" href="/attendance">◉ Start face scan</a></div></section><section class="stat-grid" aria-label="Attendance summary"><article class="stat-card teal"><div class="stat-label"><span class="stat-symbol"><i>♙</i></span> Active students</div><div class="stat-value">{total_students}</div><div class="stat-caption">{ready_profiles} ready for face matching</div></article><article class="stat-card blue"><div class="stat-label"><span class="stat-symbol"><i>↓</i></span> Time In</div><div class="stat-value">{len(today_rows)}</div><div class="stat-caption">{present} Present today</div></article><article class="stat-card teal"><div class="stat-label"><span class="stat-symbol"><i>↑</i></span> Check Out</div><div class="stat-value">{checked_out}</div><div class="stat-caption">Available from {escaped(clock_ph(checkout))}</div></article><article class="stat-card yellow"><div class="stat-label"><span class="stat-symbol"><i>◷</i></span> Late</div><div class="stat-value">{late}</div><div class="stat-caption">Time In after {escaped(clock_ph(cutoff))}</div></article></section><section class="dashboard-grid"><article class="card card-pad"><div class="card-title"><div><h2>Latest Time Ins</h2><span>{escaped(date_ph(current_date))}</span></div><a class="link-text" href="/records">View all</a></div><div class="activity-list">{activities}</div></article><aside class="card card-pad guide-card"><div class="card-title"><h2>Daily flow</h2><span>Time In + Out</span></div><div class="guide-list"><div class="guide-step"><b>1</b><div><strong>Register one face profile</strong><span>Capture one clear, front-facing face image with the student details.</span></div></div><div class="guide-step"><b>2</b><div><strong>Morning Time In</strong><span>Open Time In mode. At or before {escaped(clock_ph(cutoff))} is Present; afterward is Late.</span></div></div><div class="guide-step"><b>3</b><div><strong>{escaped(clock_ph(checkout))} Check Out</strong><span>Switch to Check Out mode. Students with a Time In record can record their departure.</span></div></div></div><a class="btn" href="/attendance">Open attendance station</a></aside></section>"""
    return page_layout(request, "Dashboard", "dashboard", content)


def render_students(request: Request) -> HttpResponse:
    query = request.query.get("q", "").strip()
    show_archived = request.query.get("archived") == "1"
    database_query: dict[str, Any] = {} if show_archived else {"active": True}
    students = list(get_db().students.find(database_query).sort([("last_name", ASCENDING), ("first_name", ASCENDING)]))
    if query:
        students = [student for student in students if student_matches_query(student, query)]
    views = [serialize_student(student) for student in students]
    if views:
        rows = "".join(
            f'<tr><td><a class="table-person" href="/students/{escaped(student["id"])}">{avatar_markup(student)}<span><strong>{escaped(student["full_name"])}</strong><small class="mono">{escaped(student["student_number"])}</small></span></a></td><td>{escaped(student_value(student, "gender"))}</td><td>{escaped(grade_for(student))}</td><td>{escaped(section_for(student))}</td><td>{status_markup("Profile ready") if descriptor_from_db(student["face_descriptor"]) else "<span class=\"status status-archived\">Recapture required</span>"}</td><td>{status_markup("Active") if student["active"] else "<span class=\"status status-archived\">Archived</span>"}</td><td><div class="table-actions"><a class="btn btn-plain" href="/students/{escaped(student["id"])}">View</a><a class="btn btn-plain" href="/students/{escaped(student["id"])} /edit">Edit</a></div></td></tr>'
            .replace(f'/{student["id"]} /edit', f'/{student["id"]}/edit')
            for student in views
        )
        table = f'<div class="table-scroll"><table><thead><tr><th>Student</th><th>Gender</th><th>Grade</th><th>Section</th><th>Face Profile</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>'
    else:
        heading = "No matching students" if query else "No students have been registered"
        text = "Try another search term or check archived profiles." if query else "Register a student and capture one face profile for automatic attendance matching."
        table = f'<div class="empty"><div class="empty-icon">◉</div><h3>{heading}</h3><p>{text}</p><a class="btn btn-primary" href="/students/new"><span class="icon-plus">＋</span> Register student</a></div>'
    params = {"archived": "1"} if show_archived else {}
    if query:
        params["q"] = query
    toggle_params = {"q": query} if query else {}
    toggle_params["archived"] = "0" if show_archived else "1"
    clear_href = "/students" + ("?" + urlencode({"archived": "1"}) if show_archived else "")
    content = f"""<section class="page-header"><div><span class="eyebrow">Face recognition profiles</span><h1>Students</h1><p>Register the student details and capture one clear face profile. The attendance scanner uses it to identify the student automatically.</p></div><div class="header-actions"><a class="btn btn-primary" href="/students/new"><span class="icon-plus">＋</span> Register student</a></div></section><section class="toolbar"><form class="search-form" method="get" action="/students">{'<input type="hidden" name="archived" value="1">' if show_archived else ''}<div class="search-box"><span>⌕</span><input type="text" name="q" value="{escaped(query)}" placeholder="Search name, student number, grade, or section"></div><button class="btn" type="submit">Search</button>{f'<a class="btn btn-plain" href="{clear_href}">Clear</a>' if query else ''}</form><a class="btn btn-plain" href="/students?{urlencode(toggle_params)}">{'Active students' if show_archived else 'View archived'}</a></section><section class="card table-card">{table}</section>"""
    return page_layout(request, "Students", "students", content)


def input_value(values: Mapping[str, Any] | None, key: str) -> str:
    return escaped(values.get(key, "") if values else "")


def selected_option(values: Mapping[str, Any] | None, value: str) -> str:
    return " selected" if values and values.get("gender") == value else ""


def render_student_form(
    request: Request,
    student: Mapping[str, Any] | None = None,
    status: int = 200,
    inline_error: str = "",
) -> HttpResponse:
    editing = bool(student and student.get("id"))
    values = dict(student or {})
    identifier = str(values.get("id") or "")
    title = "Edit student profile" if editing else "Register a student"
    action_label = "Save changes" if editing else "Register face profile"
    cancel = f'/students/{quote(identifier, safe="")}' if editing else "/students"
    photo_url = student_photo_url(values) if student else None
    existing_face = bool(values.get("face_descriptor"))
    content = f"""<section class="page-header"><div><span class="eyebrow">{'Update face recognition profile' if editing else 'New face recognition registration'}</span><h1>{title}</h1><p>Enter the student details and capture one clear, front-facing face image. This profile will be used by the automatic attendance camera.</p></div><div class="header-actions"><a class="btn" href="{escaped(cancel)}">← Cancel</a></div></section><form method="post" class="form-layout" id="studentForm"><section class="card card-pad"><div class="card-title"><h2>Student information</h2><span><b style="color:#b83b39">*</b> Required</span></div><div class="form-grid three"><div class="form-field"><label for="student_number">Student Number *</label><input id="student_number" name="student_number" type="text" maxlength="60" required value="{input_value(values, "student_number")}" placeholder="Example: 2026-001"></div><div class="form-field"><label for="first_name">First Name *</label><input id="first_name" name="first_name" type="text" required value="{input_value(values, "first_name")}" placeholder="Juan"></div><div class="form-field"><label for="middle_name">Middle Name</label><input id="middle_name" name="middle_name" type="text" value="{input_value(values, "middle_name")}" placeholder="Santos"></div><div class="form-field"><label for="last_name">Last Name *</label><input id="last_name" name="last_name" type="text" required value="{input_value(values, "last_name")}" placeholder="Dela Cruz"></div><div class="form-field"><label for="gender">Gender *</label><select id="gender" name="gender" required><option value="">Select gender</option><option value="Male"{selected_option(values, "Male")}>Male</option><option value="Female"{selected_option(values, "Female")}>Female</option><option value="Other"{selected_option(values, "Other")}>Other</option><option value="Prefer not to say"{selected_option(values, "Prefer not to say")}>Prefer not to say</option></select></div><div class="form-field"><label for="grade_level">Grade *</label><input id="grade_level" name="grade_level" type="text" required value="{input_value(values, "grade_level")}" placeholder="Example: Grade 10"></div><div class="form-field"><label for="section_name">Section *</label><input id="section_name" name="section_name" type="text" required value="{input_value(values, "section_name")}" placeholder="Example: Rizal"></div><div class="form-field"><label for="phone">Contact Number</label><input id="phone" name="phone" type="tel" value="{input_value(values, "phone")}" placeholder="09XX XXX XXXX"></div><div class="form-field"><label for="email">Email Address</label><input id="email" name="email" type="email" value="{input_value(values, "email")}" placeholder="student@email.com"></div><div class="form-field"><label for="guardian_name">Parent / Guardian</label><input id="guardian_name" name="guardian_name" type="text" value="{input_value(values, "guardian_name")}" placeholder="Parent or guardian name"></div><div class="form-field full"><label for="address">Address</label><textarea id="address" name="address" placeholder="Optional address">{input_value(values, "address")}</textarea></div></div><div class="form-actions"><a class="btn" href="{escaped(cancel)}">Cancel</a><button class="btn btn-primary" type="submit">{action_label}</button></div></section><aside class="card camera-card" id="registrationCamera" data-model-url="/static/models" data-required-samples="1"><div class="card-title"><div><h2>Face registration</h2><span>Laptop camera</span></div></div><div class="sample-progress" id="registrationProgress"><span class="sample-dot"></span></div><p class="sample-instruction" id="registrationInstruction">Capture one clear, front-facing face image.</p><div class="pose-guide" aria-live="polite"><span class="pose-arrow" id="registrationPoseArrow" aria-hidden="true">↑</span><div><strong id="registrationPoseLabel">Look straight ahead</strong><small>Follow the arrow before capturing the face.</small></div></div><div class="camera-stage face-stage"><video id="registrationVideo" class="hidden" autoplay playsinline muted></video><canvas id="registrationOverlay" class="face-overlay hidden"></canvas><img id="registrationPreview" class="{' ' if photo_url else 'hidden'}" {'src="' + escaped(photo_url) + '"' if photo_url else ''} alt="Face profile preview"><div class="camera-placeholder {'hidden' if photo_url else ''}" id="registrationPlaceholder"><b>◉</b><p>Select <strong>Start face capture</strong>. Use good lighting and keep only one face in the frame.</p></div><div class="face-indicator hidden" id="registrationFaceIndicator">Waiting for a face…</div></div><input type="hidden" name="face_photo_data" id="facePhotoData"><input type="hidden" name="face_descriptors" id="faceDescriptorsData"><div class="camera-controls"><button class="btn btn-dark" type="button" id="startRegistrationCamera">◉ Start face capture</button><button class="btn btn-primary hidden" type="button" id="captureRegistrationPhoto" disabled>▣ Capture face</button><button class="btn hidden" type="button" id="retakeRegistrationPhoto">↻ Recapture face</button></div><p class="photo-note">The live video is not saved. One face descriptor and one profile photo are saved for automatic matching. Obtain the student or guardian's consent before registration.</p>{'<p class="field-note">An existing face profile will be kept unless you recapture it.</p>' if existing_face else ''}</aside></form><script>window.FaceScanConfig = window.FaceScanConfig || {{}}; window.FaceScanConfig.modelUrl = "/static/models";</script>"""
    if inline_error:
        content = (
            '<div class="flash flash-error"><span>!</span><p>'
            + escaped(inline_error)
            + '</p><button type="button" class="flash-close" aria-label="Close">×</button></div>'
            + content
        )
    response = page_layout(request, title, "students", content, vendor_face_api=True)
    response.status = status
    return response


def render_student_detail(request: Request, student_id: str) -> HttpResponse:
    student = get_student(student_id)
    if not student:
        raise KeyError("Student not found")
    view = serialize_student(student)
    total = get_db().attendance.count_documents({"student_id": student["_id"]})
    recent = recent_attendance_for_student(student["_id"])
    profile_ready = descriptor_from_db(student.get("face_descriptor")) is not None
    photo = student_photo_url(view)
    if recent:
        history = "".join(
            f'<div class="activity-row"><div class="person-avatar">{index}</div><div class="person-meta"><strong>{escaped(date_ph(record["attendance_date"]))}</strong><small>Time In {escaped(time_ph(record["checked_in_at"]))}{" · Check Out " + escaped(time_ph(record["checked_out_at"])) if record["checked_out_at"] else " · No Check Out yet"}</small></div><div>{status_markup(record["status"])}<div class="activity-time">{escaped(record["notes"] or "Automatic face match")}</div></div></div>'
            for index, record in enumerate(recent, 1)
        )
    else:
        history = '<p class="field-note">This student does not have an attendance record yet.</p>'
    avatar = f'<img src="{escaped(photo)}" alt="Face profile for {escaped(view["full_name"])}">' if photo else escaped(initials(view))
    content = f"""<section class="page-header"><div><span class="eyebrow">Student face-recognition profile</span><h1>Profile and face registration</h1><p>When a live face scan matches this registered face profile, the attendance station shows the student details and saves attendance automatically.</p></div><div class="header-actions"><a class="btn" href="/students">← All students</a><a class="btn btn-primary" href="/students/{escaped(view["id"])}/edit">Update face profile</a></div></section><section class="card profile-hero"><div class="profile-avatar">{avatar}</div><div class="profile-intro"><h1>{escaped(view["full_name"])}</h1><p><span class="mono">{escaped(view["student_number"])}</span></p><div class="profile-tags"><span class="mini-pill">{escaped(student_value(view, "gender"))}</span><span class="mini-pill">{escaped(grade_for(view))}</span><span class="mini-pill">Section {escaped(section_for(view))}</span><span class="mini-pill">{total} recorded Time In{'s' if total != 1 else ''}</span>{'<span class="mini-pill">Face profile ready</span>' if profile_ready else '<span class="status status-archived">Face recapture needed</span>'}</div></div><div class="header-actions"><a class="btn btn-soft" href="/attendance">◉ Open face scanner</a></div></section><section class="detail-grid"><article class="card card-pad"><div class="card-title"><h2>Contact and school information</h2><span>Manual registration details</span></div><div class="info-list"><div class="info-item"><span>Student Number</span><strong class="mono">{escaped(view["student_number"])}</strong></div><div class="info-item"><span>Gender</span><strong>{escaped(student_value(view, "gender"))}</strong></div><div class="info-item"><span>Grade</span><strong>{escaped(grade_for(view))}</strong></div><div class="info-item"><span>Section</span><strong>{escaped(section_for(view))}</strong></div><div class="info-item"><span>Email</span><strong>{escaped(view["email"] or "—")}</strong></div><div class="info-item"><span>Contact Number</span><strong>{escaped(view["phone"] or "—")}</strong></div><div class="info-item"><span>Parent / Guardian</span><strong>{escaped(view["guardian_name"] or "—")}</strong></div><div class="info-item"><span>Registered</span><strong>{escaped(datetime_ph(view["created_at"]))}</strong></div><div class="info-item full"><span>Address</span><strong>{escaped(view["address"] or "—")}</strong></div></div><div class="card-title" style="margin-top:23px"><h3>Latest attendance</h3><a class="link-text" href="/records?q={quote(view["student_number"], safe='')}">View records</a></div><div class="activity-list">{history}</div></article><aside class="card face-profile-panel"><div class="profile-avatar" style="width:142px;height:142px;margin:auto;border-radius:25px">{avatar}</div><h3>{'Face recognition ready' if profile_ready else 'Face profile missing'}</h3><p>{'This profile has a face descriptor for automatic matching. Update the face profile if the student appearance changes substantially.' if profile_ready else 'Capture one new, clear front-facing face profile before this student can use automatic face attendance.'}</p><div class="button-row"><a class="btn btn-primary" href="/students/{escaped(view["id"])}/edit">↻ Capture face profile</a><a class="btn" href="/attendance">Open scanner</a></div></aside></section><section class="danger-zone">{'<h3>Archive profile</h3><p>The face scanner will no longer recognize this student, but previous attendance records remain in the database.</p><form method="post" action="/students/' + escaped(view["id"]) + '/archive" onsubmit="return confirm(\'Archive this student?\')"><button class="btn btn-danger" type="submit">Archive student</button></form>' if view["active"] else '<h3>Archived profile</h3><p>An archived student cannot receive attendance. You may restore the profile at any time.</p><form method="post" action="/students/' + escaped(view["id"]) + '/restore"><button class="btn btn-soft" type="submit">Restore student</button></form>'}</section>"""
    return page_layout(request, view["full_name"], "students", content)


def render_attendance(request: Request) -> HttpResponse:
    records = fetch_attendance_records(today_iso())
    cutoff = cutoff_time()
    checkout = checkout_time()
    default_mode = default_scan_mode()
    if records:
        rows = "".join(
            f'<tr><td><div class="table-person">{avatar_markup(record)}<span><strong>{escaped(record["full_name"])}</strong><small class="mono">{escaped(record["student_number"])}</small></span></div></td><td>{escaped(student_value(record, "gender"))}</td><td>{escaped(grade_for(record))}</td><td>{escaped(section_for(record))}</td><td>{escaped(time_ph(record["checked_in_at"]))}</td><td>{escaped(time_ph(record["checked_out_at"]))}</td><td>{status_markup(record["status"])}</td></tr>'
            for record in records
        )
        table = f'<div class="table-scroll"><table><thead><tr><th>Student</th><th>Gender</th><th>Grade</th><th>Section</th><th>Time In</th><th>Check Out</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>'
    else:
        table = '<div class="empty" style="min-height:180px"><div class="empty-icon">◷</div><h3>No attendance records today</h3><p>Start with Time In mode before 8:00 AM, then use Check Out mode at 5:00 PM.</p></div>'
    time_in_active = "active" if default_mode == "time_in" else ""
    time_out_active = "active" if default_mode == "time_out" else ""
    time_out_text = "Check Out" if default_mode == "time_out" else "Time In"
    content = f"""<section class="page-header"><div><span class="eyebrow">Automatic face-match attendance</span><h1>Face Scan Attendance</h1><p><strong>Time In:</strong> students are Present at or before 8:00 AM and Late after 8:00 AM. <strong>Check Out:</strong> available from 5:00 PM. When a face matches, the student name, gender, grade, and section are shown before the record is saved automatically.</p></div><div class="header-actions"><a class="btn" href="/records">▤ View records</a></div></section><section class="attendance-layout" id="attendanceApp" data-model-url="/static/models" data-profiles="/api/face-profiles" data-checkin="/api/attendance/check-in" data-default-mode="{default_mode}" data-time-in-cutoff="{cutoff}" data-checkout-time="{checkout}"><article class="card scanner-card"><div class="mode-switch" role="group" aria-label="Attendance mode"><button type="button" class="mode-button {time_in_active}" data-mode="time_in" id="timeInMode"><span>↓</span><b>Time In</b><small>Present until {escaped(clock_ph(cutoff))}</small></button><button type="button" class="mode-button {time_out_active}" data-mode="time_out" id="timeOutMode"><span>↑</span><b>Check Out</b><small>From {escaped(clock_ph(checkout))}</small></button></div><div class="scanner-stage face-stage"><video id="attendanceVideo" class="hidden" autoplay playsinline muted></video><canvas id="attendanceOverlay" class="face-overlay hidden"></canvas><div class="camera-placeholder" id="attendancePlaceholder"><b>◉</b><p>Select <strong>Start face scanner</strong>. Current mode: <strong id="scannerModeText">{time_out_text}</strong>.</p></div><div class="face-indicator hidden" id="attendanceFaceIndicator">Waiting for a face…</div></div><div class="scanner-toolbar"><button class="btn btn-primary" type="button" id="startAttendanceCamera">◉ Start face scanner</button><button class="btn hidden" type="button" id="stopAttendanceCamera">■ Stop camera</button><div class="scan-message" id="scanMessage" aria-live="polite">Select Time In or Check Out before starting the scanner.</div></div></article><aside class="scan-side"><div class="clock-banner"><span id="scheduleModeLabel">{'Check Out mode' if default_mode == 'time_out' else 'Time In mode'} · {escaped(date_ph(today_iso()))}</span><strong id="scheduleMainLabel">{'Check Out from ' + escaped(clock_ph(checkout)) if default_mode == 'time_out' else 'Present until ' + escaped(clock_ph(cutoff))}</strong><small id="scheduleSubLabel">{'A student must have a Time In record before Check Out can be saved.' if default_mode == 'time_out' else 'A Time In after ' + escaped(clock_ph(cutoff)) + ' is automatically recorded as Late.'}</small></div><article class="card recognition-guide"><div class="card-title"><div><h2>Automatic scan</h2><span id="guideModeText">{'Dismissal' if default_mode == 'time_out' else 'Arrival'}</span></div></div><div class="recognition-steps"><div><b>1</b><span>Keep only one student in the camera frame.</span></div><div><b>2</b><span>Wait for a stable face match.</span></div><div><b>3</b><span id="guideActionText">{'A successful face match saves Check Out automatically.' if default_mode == 'time_out' else 'A successful match saves Time In and the Present/Late status automatically.'}</span></div></div><p class="field-note">If a student is not recognized, improve the lighting or recapture the face profile in the student page.</p></article><article class="card match-result hidden" id="matchResult" aria-live="polite"><div class="match-result-head" id="matchResultHeading">Face match found</div><div class="match-person"><div class="match-avatar" id="matchAvatar">ST</div><div><h2 id="matchName">Student Name</h2><p id="matchNumber">Student Number</p></div></div><dl class="match-details"><div><dt>Gender</dt><dd id="matchGender">—</dd></div><div><dt>Grade</dt><dd id="matchGrade">—</dd></div><div><dt>Section</dt><dd id="matchSection">—</dd></div><div><dt>Attendance</dt><dd id="matchStatus">Confirming…</dd></div><div><dt>Time In</dt><dd id="matchTimeIn">—</dd></div><div><dt>Check Out</dt><dd id="matchTimeOut">—</dd></div></dl><p class="result-note" id="matchMessage">The face match is being confirmed before attendance is saved.</p><div class="result-actions"><button class="btn btn-primary hidden" type="button" id="scanNextFace">◉ Scan next student</button></div></article></aside></section><section class="card table-card today-records"><div class="card-title" style="padding:20px 20px 0"><div><h2>Today's attendance</h2><span>{escaped(date_ph(today_iso()))}</span></div><a class="link-text" href="/records">Full records</a></div>{table}</section><script>window.FaceScanConfig = window.FaceScanConfig || {{}}; window.FaceScanConfig.modelUrl = "/static/models";</script>"""
    return page_layout(request, "Face Attendance", "attendance", content, vendor_face_api=True)


def render_records(request: Request) -> HttpResponse:
    date_value = safe_date(request.query.get("date", today_iso()))
    status = request.query.get("status", "")
    query = request.query.get("q", "").strip()
    records = fetch_attendance_records(date_value, status, query)
    export_query = urlencode({"date": date_value, "status": status, "q": query})
    if records:
        rows = "".join(
            f'<tr><td><span class="thumbnail">{("<img src=\"" + escaped(attendance_photo_url(record) or "") + "\" alt=\"Time In face capture\">") if attendance_photo_url(record) else escaped(initials(record))}</span></td><td><a class="table-person" href="/students/{escaped(record["id"])}"><span><strong>{escaped(record["full_name"])}</strong><small class="mono">{escaped(record["student_number"])}</small></span></a></td><td>{escaped(student_value(record, "gender"))}</td><td>{escaped(grade_for(record))}</td><td>{escaped(section_for(record))}</td><td>{escaped(time_ph(record["checked_in_at"]))}</td><td>{escaped(time_ph(record["checked_out_at"]))}</td><td>{status_markup(record["status"])}</td></tr>'
            for record in records
        )
        table = f'<div class="table-scroll"><table><thead><tr><th>Face Scan</th><th>Student</th><th>Gender</th><th>Grade</th><th>Section</th><th>Time In</th><th>Check Out</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>'
    else:
        table = '<div class="empty"><div class="empty-icon">▤</div><h3>No records match this filter</h3><p>Change the date or search term, or start automatic face attendance.</p><a class="btn btn-primary" href="/attendance">Open face scanner</a></div>'
    content = f"""<section class="page-header"><div><span class="eyebrow">Automatic face-match history</span><h1>Attendance Records</h1><p>Filter by date, name, student number, gender, grade, section, or status. View Time In and Check Out records, then download a CSV for Excel.</p></div><div class="header-actions"><a class="btn btn-primary" href="/attendance">◉ Open face scanner</a></div></section><form method="get" class="card card-pad" style="margin-bottom:17px"><div class="filters"><div><label for="date">Date</label><input id="date" name="date" type="date" value="{escaped(date_value)}"></div><div><label for="q">Search</label><input id="q" name="q" type="text" value="{escaped(query)}" placeholder="Name, student number, grade, or section"></div><div><label for="status">Status</label><select id="status" name="status"><option value="">All statuses</option><option value="Present"{' selected' if status == 'Present' else ''}>Present</option><option value="Late"{' selected' if status == 'Late' else ''}>Late</option></select></div><button class="btn" type="submit">Apply filters</button></div></form><section class="card table-card"><div class="card-title" style="padding:20px 20px 0"><div><h2>{len(records)} record{'' if len(records) == 1 else 's'}</h2><span>{escaped(date_ph(date_value))}</span></div><a class="btn btn-soft" href="/records/export.csv?{export_query}">⇩ Download CSV</a></div>{table}</section>"""
    return page_layout(request, "Attendance Records", "records", content)


def render_settings(request: Request) -> HttpResponse:
    school = get_setting("school_name", "Class Attendance")
    classroom = get_setting("class_name", "School Year 2026–2027")
    cutoff = cutoff_time()
    checkout = checkout_time()
    content = f"""<section class="page-header"><div><span class="eyebrow">Class configuration</span><h1>Settings</h1><p>Set the school or class name and the time when automatic Time In attendance becomes Late.</p></div></section><section class="settings-layout"><form method="post" class="card card-pad"><div class="card-title"><h2>Attendance configuration</h2><span>For one class</span></div><div class="form-grid"><div class="form-field full"><label for="school_name">School / system name</label><input id="school_name" name="school_name" type="text" maxlength="100" value="{escaped(school)}" placeholder="Example: Dasmariñas National High School"></div><div class="form-field full"><label for="class_name">Class label</label><input id="class_name" name="class_name" type="text" maxlength="100" value="{escaped(classroom)}" placeholder="Example: Grade 10 – Rizal · SY 2026–2027"></div><div class="form-field"><label for="late_cutoff">Time In / Late cutoff</label><input id="late_cutoff" name="late_cutoff" type="time" required value="{escaped(cutoff)}"><p class="field-note">A Time In at this time or earlier is Present. A Time In afterward is Late.</p></div><div class="form-field"><label for="checkout_time">Check Out time</label><input id="checkout_time" name="checkout_time" type="time" required value="{escaped(checkout)}"><p class="field-note">This is the default Check Out mode time. A Check Out cannot be saved before this time.</p></div></div><div class="form-actions"><button class="btn btn-primary" type="submit">Save settings</button></div></form><aside class="setting-note"><h2>Face matching and privacy</h2><p>The system uses a local browser face model to compare a live face scan with the registered face profile. When a face matches, attendance is automatically saved in MongoDB.</p><div class="setting-points"><div><b>1</b><span>Obtain informed consent from the student and parent or guardian before one-face registration.</span></div><div><b>2</b><span>Provide an alternative manual attendance method for a student who cannot or does not want to use face scanning.</span></div><div><b>3</b><span>Review records and do not use an automatic face match as the only basis for disciplinary action.</span></div><div><b>4</b><span>Limit access to authorized teachers, protect the MongoDB account, and keep the application password private.</span></div></div></aside></section>"""
    return page_layout(request, "Settings", "settings", content)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------
def validate_student_form(data: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    fields = {
        "student_number": str(data.get("student_number") or "").strip(),
        "first_name": str(data.get("first_name") or "").strip(),
        "middle_name": str(data.get("middle_name") or "").strip(),
        "last_name": str(data.get("last_name") or "").strip(),
        "gender": str(data.get("gender") or "").strip(),
        "grade_level": str(data.get("grade_level") or "").strip(),
        "section_name": str(data.get("section_name") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "phone": str(data.get("phone") or "").strip(),
        "guardian_name": str(data.get("guardian_name") or "").strip(),
        "address": str(data.get("address") or "").strip(),
    }
    fields["grade_section"] = f"{fields['grade_level']} · {fields['section_name']}" if fields["grade_level"] and fields["section_name"] else ""
    required = {
        "student_number": "Student Number",
        "first_name": "First Name",
        "last_name": "Last Name",
        "gender": "Gender",
        "grade_level": "Grade",
        "section_name": "Section",
    }
    for key, label in required.items():
        if not fields[key]:
            return fields, f"{label} is required."
    if len(fields["student_number"]) > 60:
        return fields, "Student Number must be 60 characters or fewer."
    if fields["email"] and ("@" not in fields["email"] or len(fields["email"]) > 120):
        return fields, "Enter a valid email address."
    for key, value in fields.items():
        if len(value) > 500:
            return fields, f"{key.replace('_', ' ').title()} is too long."
    return fields, None


def handle_student_new(request: Request) -> HttpResponse:
    if request.method == "GET":
        return render_student_form(request)
    if request.method != "POST":
        return app_error_page(405, "Method not allowed", "Use the student registration form.")
    fields, error = validate_student_form(request.data)
    if error:
        return render_student_form(request, fields, 400, error)
    try:
        descriptor = face_descriptor_from_capture(request.data.get("face_descriptors"))
        photo = save_data_image(request.data.get("face_photo_data"))
        document = {
            "student_number": fields["student_number"],
            "student_number_key": fields["student_number"].casefold(),
            "first_name": fields["first_name"],
            "middle_name": fields["middle_name"],
            "last_name": fields["last_name"],
            "gender": fields["gender"],
            "grade_level": fields["grade_level"],
            "section_name": fields["section_name"],
            "grade_section": fields["grade_section"],
            "email": fields["email"],
            "phone": fields["phone"],
            "guardian_name": fields["guardian_name"],
            "address": fields["address"],
            "photo_data": photo,
            "face_descriptor": descriptor,
            "active": True,
            "created_at": manila_now().isoformat(),
        }
        result = get_db().students.insert_one(document)
    except DuplicateKeyError:
        return render_student_form(
            request, fields, 400, "This Student Number is already in use. Enter a unique number."
        )
    except ValueError as error:
        return render_student_form(request, fields, 400, str(error))
    return redirect_response(
        f"/students/{result.inserted_id}",
        [set_flash_header("success", "The student and face profile have been registered.")],
    )


def form_values_with_existing_profile(fields: dict[str, str], student: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = dict(fields)
    view = serialize_student(student)
    values.update({
        "id": view["id"],
        "full_name": view["full_name"],
        "photo_data": view["photo_data"],
        "photo_filename": view["photo_filename"],
        "face_descriptor": view["face_descriptor"],
    })
    return values


def handle_student_edit(request: Request, student_id: str) -> HttpResponse:
    student = get_student(student_id)
    if not student:
        raise KeyError("Student not found")
    if request.method == "GET":
        return render_student_form(request, serialize_student(student))
    if request.method != "POST":
        return app_error_page(405, "Method not allowed", "Use the student update form.")
    fields, error = validate_student_form(request.data)
    values = form_values_with_existing_profile(fields, student)
    if error:
        return render_student_form(request, values, 400, error)
    photo = student.get("photo_data") or ""
    descriptor: object = student.get("face_descriptor") or []
    try:
        fresh = str(request.data.get("face_descriptors") or "").strip()
        if fresh:
            descriptor = face_descriptor_from_capture(fresh)
            photo = save_data_image(request.data.get("face_photo_data"))
        update = {
            "student_number": fields["student_number"],
            "student_number_key": fields["student_number"].casefold(),
            "first_name": fields["first_name"],
            "middle_name": fields["middle_name"],
            "last_name": fields["last_name"],
            "gender": fields["gender"],
            "grade_level": fields["grade_level"],
            "section_name": fields["section_name"],
            "grade_section": fields["grade_section"],
            "email": fields["email"],
            "phone": fields["phone"],
            "guardian_name": fields["guardian_name"],
            "address": fields["address"],
            "photo_data": photo,
            "face_descriptor": descriptor,
            "updated_at": manila_now().isoformat(),
        }
        get_db().students.update_one({"_id": student["_id"]}, {"$set": update})
    except DuplicateKeyError:
        return render_student_form(
            request, values, 400, "This Student Number is already in use. Enter a unique number."
        )
    except ValueError as error:
        return render_student_form(request, values, 400, str(error))
    return redirect_response(
        f"/students/{student_id}",
        [set_flash_header("success", "The student profile has been updated.")],
    )


def handle_student_photo(student_id: str) -> HttpResponse:
    student = get_student(student_id)
    photo = student.get("photo_data") if student else None
    if not student or not is_data_image(photo):
        raise KeyError("Photo not found")
    return stored_image_response(str(photo))


def handle_attendance_photo(attendance_id: str) -> HttpResponse:
    object_id = object_id_from_value(attendance_id)
    if object_id is None:
        raise KeyError("Photo not found")
    record = get_db().attendance.find_one({"_id": object_id})
    photo = record.get("face_photo_data") if record else None
    if not record or not is_data_image(photo):
        raise KeyError("Photo not found")
    return stored_image_response(str(photo))


def handle_student_status(student_id: str, active: bool) -> HttpResponse:
    student = get_student(student_id)
    if not student:
        raise KeyError("Student not found")
    get_db().students.update_one(
        {"_id": student["_id"]},
        {"$set": {"active": active, "updated_at": manila_now().isoformat()}},
    )
    message = "The student has been restored to the active list." if active else "The student has been archived. Previous attendance records remain available."
    destination = f"/students/{student_id}" if active else "/students"
    return redirect_response(destination, [set_flash_header("success", message)])


def handle_api_face_profiles() -> HttpResponse:
    rows = list(get_db().students.find({"active": True, "face_descriptor.0": {"$exists": True}}).sort([("last_name", ASCENDING), ("first_name", ASCENDING)]))
    profiles = []
    for student in rows:
        payload = student_payload(student, include_descriptor=True)
        if "descriptor" in payload:
            profiles.append(payload)
    return json_response({"ok": True, "profiles": profiles, "threshold": MATCH_DISTANCE_THRESHOLD})


def duplicate_time_in_response(student: Mapping[str, Any], previous: Mapping[str, Any]) -> HttpResponse:
    return json_response(
        {
            "ok": False,
            "duplicate": True,
            "action": "time_in",
            "message": f"{full_name_for(student)} already has a Time In record today at {datetime_ph(str(previous.get('checked_in_at') or ''))}.",
            "student": student_payload(student),
        },
        409,
    )


def handle_api_attendance(request: Request) -> HttpResponse:
    data = request.data
    action = str(data.get("action") or "time_in").strip().lower()
    if action not in {"time_in", "time_out"}:
        return json_response({"ok": False, "message": "The attendance mode is invalid."}, 400)
    student = get_student(data.get("student_id"), include_archived=False)
    if not student:
        return json_response({"ok": False, "message": "This student profile is no longer active."}, 404)
    stored = descriptor_from_db(student.get("face_descriptor"))
    if stored is None:
        return json_response({"ok": False, "message": "This student does not have a valid face profile. Recapture the face profile first."}, 409)
    try:
        incoming = clean_descriptor(data.get("face_descriptor"))
        photo = save_data_image(data.get("face_photo_data"))
    except ValueError as error:
        return json_response({"ok": False, "message": str(error)}, 400)
    distance = descriptor_distance(stored, incoming)
    if distance > MATCH_DISTANCE_THRESHOLD:
        return json_response({"ok": False, "message": "The face scan does not match the registered profile. Attendance was not recorded."}, 403)

    timestamp = manila_now()
    date_value = timestamp.date().isoformat()
    db = get_db()
    previous = db.attendance.find_one({"student_id": student["_id"], "attendance_date": date_value})
    if action == "time_in":
        if previous:
            return duplicate_time_in_response(student, previous)
        status = attendance_status(timestamp)
        try:
            db.attendance.insert_one({
                "student_id": student["_id"],
                "attendance_date": date_value,
                "checked_in_at": timestamp.isoformat(),
                "checked_out_at": None,
                "status": status,
                "face_photo_data": photo,
                "check_out_photo_data": None,
                "notes": "Automatic face match · Time In",
                "created_at": timestamp.isoformat(),
            })
        except DuplicateKeyError:
            latest = db.attendance.find_one({"student_id": student["_id"], "attendance_date": date_value})
            return duplicate_time_in_response(student, latest) if latest else json_response({"ok": False, "message": "Time In could not be saved. Please scan again."}, 409)
        return json_response({
            "ok": True,
            "action": "time_in",
            "message": f"Time In recorded: {status}",
            "status": status,
            "checked_in_at": timestamp.strftime("%I:%M %p"),
            "student": student_payload(student),
            "match_distance": round(distance, 3),
        })

    check_out_cutoff = datetime.strptime(checkout_time(), "%H:%M").time()
    if timestamp.time() < check_out_cutoff:
        return json_response({
            "ok": False,
            "action": "time_out",
            "message": f"Check Out is available from {clock_ph(checkout_time())}.",
            "student": student_payload(student),
        }, 403)
    if previous is None:
        return json_response({
            "ok": False,
            "action": "time_out",
            "message": f"{full_name_for(student)} does not have a Time In record today. Check Out cannot be recorded yet.",
            "student": student_payload(student),
        }, 409)
    if previous.get("checked_out_at"):
        return json_response({
            "ok": False,
            "duplicate": True,
            "action": "time_out",
            "message": f"{full_name_for(student)} already has a Check Out record today at {datetime_ph(str(previous.get('checked_out_at') or ''))}.",
            "student": student_payload(student),
        }, 409)
    result = db.attendance.update_one(
        {"_id": previous["_id"], "checked_out_at": None},
        {"$set": {
            "checked_out_at": timestamp.isoformat(),
            "check_out_photo_data": photo,
            "notes": "Automatic face match · Time In + Check Out",
            "updated_at": timestamp.isoformat(),
        }},
    )
    if result.modified_count != 1:
        latest = db.attendance.find_one({"_id": previous["_id"]}) or previous
        return json_response({
            "ok": False,
            "duplicate": True,
            "action": "time_out",
            "message": f"{full_name_for(student)} already has a Check Out record today at {datetime_ph(str(latest.get('checked_out_at') or ''))}.",
            "student": student_payload(student),
        }, 409)
    return json_response({
        "ok": True,
        "action": "time_out",
        "message": "Check Out recorded",
        "status": str(previous.get("status") or "Present"),
        "checked_in_at": datetime.fromisoformat(str(previous["checked_in_at"])).astimezone(MANILA_TZ).strftime("%I:%M %p"),
        "checked_out_at": timestamp.strftime("%I:%M %p"),
        "student": student_payload(student),
        "match_distance": round(distance, 3),
    })


def handle_export_records(request: Request) -> HttpResponse:
    date_value = safe_date(request.query.get("date", today_iso()))
    status = request.query.get("status", "")
    query = request.query.get("q", "").strip()
    records = fetch_attendance_records(date_value, status, query)
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Date", "Student Number", "Student Name", "Gender", "Grade", "Section", "Time In", "Check Out", "Status", "Method"])
    for record in records:
        writer.writerow([
            record["attendance_date"], record["student_number"], record["full_name"],
            student_value(record, "gender"), grade_for(record), section_for(record),
            time_ph(record["checked_in_at"]), time_ph(record["checked_out_at"]),
            record["status"], record["notes"] or "Automatic face match",
        ])
    return HttpResponse(
        200,
        ("\ufeff" + stream.getvalue()).encode("utf-8"),
        "text/csv; charset=utf-8",
        [("Content-Disposition", f'attachment; filename="attendance-{date_value}.csv"')],
    )


def handle_settings_post(request: Request) -> HttpResponse:
    data = request.data
    school = str(data.get("school_name") or "").strip() or "Class Attendance"
    classroom = str(data.get("class_name") or "").strip() or "School Year 2026–2027"
    late_cutoff = str(data.get("late_cutoff") or "")
    checkout_at = str(data.get("checkout_time") or "")
    try:
        datetime.strptime(late_cutoff, "%H:%M")
        datetime.strptime(checkout_at, "%H:%M")
    except ValueError:
        return redirect_response("/settings", [set_flash_header("error", "Choose a valid time for the Time In cutoff and Check Out.")])
    set_setting("school_name", school[:100])
    set_setting("class_name", classroom[:100])
    set_setting("late_cutoff", late_cutoff)
    set_setting("checkout_time", checkout_at)
    return redirect_response("/settings", [set_flash_header("success", "Time In and Check Out settings have been saved.")])


# ---------------------------------------------------------------------------
# Standard WSGI router
# ---------------------------------------------------------------------------
def serve_static(request: Request) -> HttpResponse:
    relative = request.path[len("/static/"):]
    if not relative or "\\" in relative:
        raise KeyError("Static file not found")
    target = (STATIC_DIR / relative).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError as error:
        raise KeyError("Static file not found") from error
    if not target.is_file():
        raise KeyError("Static file not found")
    try:
        content = target.read_bytes()
    except OSError as error:
        raise KeyError("Static file not found") from error
    mime, _ = mimetypes.guess_type(str(target))
    if target.suffix == ".json":
        mime = "application/json"
    return HttpResponse(200, content, mime or "application/octet-stream", [("Cache-Control", "public, max-age=3600")])


def dispatch(request: Request) -> HttpResponse:
    path = request.path.rstrip("/") or "/"
    if path.startswith("/static/"):
        return serve_static(request)
    if path == "/" and request.method == "GET":
        return render_dashboard(request)
    if path == "/students" and request.method == "GET":
        return render_students(request)
    if path == "/students/new":
        return handle_student_new(request)
    if path == "/attendance" and request.method == "GET":
        return render_attendance(request)
    if path == "/records" and request.method == "GET":
        return render_records(request)
    if path == "/records/export.csv" and request.method == "GET":
        return handle_export_records(request)
    if path == "/settings":
        if request.method == "GET":
            return render_settings(request)
        if request.method == "POST":
            return handle_settings_post(request)
        return app_error_page(405, "Method not allowed", "Use the settings form.")
    if path == "/api/face-profiles" and request.method == "GET":
        return handle_api_face_profiles()
    if path == "/api/attendance/check-in" and request.method == "POST":
        return handle_api_attendance(request)

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "students":
        student_id = parts[1]
        if len(parts) == 2 and request.method == "GET":
            return render_student_detail(request, student_id)
        if len(parts) == 3 and parts[2] == "edit":
            return handle_student_edit(request, student_id)
        if len(parts) == 3 and parts[2] == "face-photo" and request.method == "GET":
            return handle_student_photo(student_id)
        if len(parts) == 3 and parts[2] == "archive" and request.method == "POST":
            return handle_student_status(student_id, False)
        if len(parts) == 3 and parts[2] == "restore" and request.method == "POST":
            return handle_student_status(student_id, True)
    if len(parts) == 3 and parts[0] == "attendance" and parts[2] == "face-photo" and request.method == "GET":
        return handle_attendance_photo(parts[1])
    raise KeyError("Page not found")


def send_wsgi_response(start_response: Any, response: HttpResponse, method: str) -> list[bytes]:
    try:
        phrase = HTTPStatus(response.status).phrase
    except ValueError:
        phrase = "OK"
    headers = [("Content-Type", response.content_type), ("Content-Length", str(len(response.body))), *response.headers]
    start_response(f"{response.status} {phrase}", headers)
    return [] if method == "HEAD" else [response.body]


def app(environ: Mapping[str, Any], start_response: Any) -> list[bytes]:
    """Vercel-compatible pure-Python WSGI application entrypoint."""
    request = Request(environ)
    try:
        preflight = authorize_and_prepare(request)
        if preflight is not None:
            return send_wsgi_response(start_response, preflight, request.method)
        response = dispatch(request)
    except RequestBodyTooLarge:
        response = app_error_page(413, "File too large", "Capture the face image again.")
    except BadRequest as error:
        response = app_error_page(400, "Invalid request", str(error))
    except KeyError:
        response = app_error_page(404, "Page not found", "The page may have been deleted or the link is incorrect.")
    except (MongoDatabaseError, PyMongoError):
        response = database_error_page()
    except Exception as error:
        # Keep user pages safe and do not output connection strings or raw errors.
        print(f"Unexpected application error: {type(error).__name__}")
        response = app_error_page(500, "Something went wrong", "The request could not be completed. Please try again.")
    return send_wsgi_response(start_response, response, request.method)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    with make_server("0.0.0.0", port, app) as server:
        print(f"Attendance system running at http://127.0.0.1:{port}")
        server.serve_forever()
