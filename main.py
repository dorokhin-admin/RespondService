import os
import io
import json
import uuid
import shutil
import re
import sqlite3
import secrets
import hashlib
import base64
from contextvars import ContextVar
from pathlib import Path
from typing import List, Dict, Any
from enum import Enum
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from fastapi.responses import HTMLResponse, RedirectResponse

import asyncio
import subprocess
import httpx

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError as exc:
    raise RuntimeError(
        "Установите cryptography для шифрования токенов социальных аккаунтов"
    ) from exc

from PIL import Image, ImageDraw, ImageFont


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# Загружаем .env именно из папки, где находится main.py
load_dotenv(BASE_DIR / ".env")

GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = BASE_DIR / "respondservice.sqlite3"

_selected_account: ContextVar[dict | None] = ContextVar(
    "selected_social_account",
    default=None
)


def _token_cipher() -> Fernet:
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY не задан. Сгенерируйте Fernet-ключ и добавьте его в .env"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY имеет неверный формат Fernet") from exc


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with _db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS social_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_name TEXT NOT NULL,
                access_token_encrypted TEXT NOT NULL,
                refresh_token_encrypted TEXT,
                expires_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, platform, account_id)
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                created_at TEXT NOT NULL,
                frontend_url TEXT
            );
        """)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(oauth_states)")
        }
        if "frontend_url" not in columns:
            connection.execute("ALTER TABLE oauth_states ADD COLUMN frontend_url TEXT")
        if "code_verifier" not in columns:
            connection.execute(
        "ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT"
    )


initialize_database()


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="SMM AI Backend",
    version="1.1.0"
)

@app.on_event("startup")
async def startup_threads_refresh():
    asyncio.create_task(
        refresh_threads_tokens_background()
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def handle_unexpected_error(
    request: Request,
    exc: Exception
):
    print(
        f"Unhandled error in {request.method} {request.url.path}: "
        f"{type(exc).__name__}: {exc}"
    )

    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Внутренняя ошибка backend при генерации Reels. "
                "Подробности записаны в консоль сервера."
            )
        }
    )


# ============================================================
# STATIC GENERATED FILES
# ============================================================

app.mount(
    "/generated",
    StaticFiles(directory=str(GENERATED_DIR)),
    name="generated"
)

# ============================================================
# DESIGN / CAROUSEL RENDERING
# ============================================================

IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1350

# GPT Image 2 требует размеры, кратные 16.
# Поэтому генерируем в 1024x1280 (точный 4:5),
# затем технически увеличиваем до 1080x1350.
IMAGE_GENERATION_WIDTH = 1024
IMAGE_GENERATION_HEIGHT = 1280
IMAGE_GENERATION_SIZE = (
    f"{IMAGE_GENERATION_WIDTH}x{IMAGE_GENERATION_HEIGHT}"
)

# Модель можно менять через .env.
OPENAI_IMAGE_MODEL = os.getenv(
    "OPENAI_IMAGE_MODEL",
    "gpt-image-2"
).strip()

OPENAI_IMAGE_QUALITY = os.getenv(
    "OPENAI_IMAGE_QUALITY",
    "high"
).strip()

# Vision-проверка результата.
CAROUSEL_IMAGE_REVIEW_ENABLED = (
    os.getenv(
        "CAROUSEL_IMAGE_REVIEW",
        "true"
    ).strip().lower()
    not in {"0", "false", "no", "off"}
)

CAROUSEL_IMAGE_REVIEW_MODEL = os.getenv(
    "CAROUSEL_IMAGE_REVIEW_MODEL",
    "gpt-5.6-luna"
).strip()

CAROUSEL_IMAGE_MAX_ATTEMPTS = int(
    os.getenv(
        "CAROUSEL_IMAGE_MAX_ATTEMPTS",
        "3"
    )
)

CAROUSEL_IMAGE_CONCURRENCY = int(
    os.getenv(
        "CAROUSEL_IMAGE_CONCURRENCY",
        "3"
    )
)

CAROUSEL_IMAGE_TIMEOUT = float(
    os.getenv(
        "CAROUSEL_IMAGE_TIMEOUT",
        "180"
    )
)

CAROUSEL_IMAGE_SEMAPHORE = asyncio.Semaphore(
    max(1, CAROUSEL_IMAGE_CONCURRENCY)
)

# Эти значения НЕ являются дизайном.
# Это только аварийные fallback, если AI не вернул layout.
FALLBACK_TITLE_SIZE = 92
FALLBACK_BODY_SIZE = 44
FALLBACK_TEXT_COLOR = "#FFFFFF"


# ============================================================
# FONTS
# ============================================================

FONT_REGULAR_PATHS = [
    # Windows
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/calibri.ttf",

    # Linux / Render
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
]

FONT_BOLD_PATHS = [
    # Windows
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/calibrib.ttf",

    # Linux / Render
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
]


def find_font(paths: List[str]) -> str:
    for path in paths:
        if os.path.exists(path):
            return path

    raise RuntimeError(
        "Не найден шрифт с поддержкой кириллицы"
    )


REGULAR_FONT_PATH = find_font(FONT_REGULAR_PATHS)
BOLD_FONT_PATH = find_font(FONT_BOLD_PATHS)


def get_font(size: int, bold: bool = False):
    path = BOLD_FONT_PATH if bold else REGULAR_FONT_PATH
    return ImageFont.truetype(path, size)


# ============================================================
# ENUMS
# ============================================================

class AllowedLanguage(str, Enum):
    EN = "en"
    ES = "es"
    PT = "pt"
    FR = "fr"
    RU = "ru"
    DE = "de"


class AllowedFormat(str, Enum):
    POST = "post"
    REELS = "reels"
    CAROUSEL = "carousel"


# ============================================================
# REQUEST MODELS
# ============================================================

class GenerateRequest(BaseModel):
    language: AllowedLanguage

    selected_formats: List[AllowedFormat] = Field(
        ...,
        min_length=1
    )

    # Без max_length — можно передавать весь исходный текст.
    page_text: str = Field(
        ...,
        min_length=10
    )

    tov: str = Field(
        default="",
        max_length=1000
    )

    provider: str = Field(default="grok", min_length=2, max_length=20)

    @field_validator("selected_formats")
    @classmethod
    def validate_unique_formats(
        cls,
        v: List[AllowedFormat]
    ) -> List[AllowedFormat]:
        if len(v) != len(set(v)):
            raise ValueError(
                "Форматы не должны повторяться"
            )
        return v


class PublishRequest(BaseModel):
    content: Dict[str, Any]


class CredentialsRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=200)


class SocialAccountRequest(BaseModel):
    platform: str
    account_id: str
    account_name: str
    access_token: str
    expires_at: str | None = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _prepare_publish_account(
    authorization: str | None,
    account_header: str | None,
    platform: str
) -> None:
    user = _user_from_authorization(authorization)
    if not account_header:
        raise HTTPException(
            status_code=400,
            detail="Передайте X-Account-Id выбранного аккаунта"
        )
    try:
        account_id = int(account_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="X-Account-Id должен быть числом") from exc
    _select_account(user["id"], account_id, platform)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        310_000
    )
    return f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        encoded_salt, encoded_digest = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(encoded_salt.encode())
        expected = base64.urlsafe_b64decode(encoded_digest.encode())
    except (ValueError, UnicodeDecodeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return secrets.compare_digest(actual, expected)


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _user_from_authorization(authorization: str | None) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Требуется Bearer-сессия RespondService")
    token = authorization[7:].strip()
    with _db() as connection:
        user = connection.execute(
            """
            SELECT users.* FROM users
            JOIN sessions ON sessions.user_id = users.id
            WHERE sessions.token_hash = ?
            """,
            (_session_hash(token),)
        ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Сессия недействительна")
    return user


def _account_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "platform": row["platform"],
        "account_id": row["account_id"],
        "account_name": row["account_name"],
        "expires_at": row["expires_at"],
        "metadata": json.loads(row["metadata_json"] or "{}")
    }


def _select_account(user_id: int, account_id: int, platform: str) -> None:
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM social_accounts WHERE id = ? AND user_id = ? AND platform = ?",
            (account_id, user_id, platform)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Социальный аккаунт не найден")
    _selected_account.set(dict(row))


def _social_credentials(platform: str) -> tuple[str, str]:
    account = _selected_account.get()
    if not account or account["platform"] != platform:
        raise HTTPException(
            status_code=400,
            detail=f"Сначала выберите подключённый аккаунт {platform}"
        )
    try:
        token = _token_cipher().decrypt(
            account["access_token_encrypted"].encode()
        ).decode()
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Не удалось расшифровать токен аккаунта") from exc
    return account["account_id"], token


def _store_social_account(
    user_id: int,
    platform: str,
    account_id: str,
    account_name: str,
    access_token: str,
    expires_at: str | None = None,
    metadata: dict | None = None,
    refresh_token: str | None = None
) -> dict:
    now = _now()

    encrypted_access_token = (
        _token_cipher()
        .encrypt(access_token.encode())
        .decode()
    )

    encrypted_refresh_token = None

    if refresh_token:
        encrypted_refresh_token = (
            _token_cipher()
            .encrypt(refresh_token.encode())
            .decode()
        )

    metadata_json = json.dumps(
        metadata or {},
        ensure_ascii=True
    )

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO social_accounts (
                user_id,
                platform,
                account_id,
                account_name,
                access_token_encrypted,
                refresh_token_encrypted,
                expires_at,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_id, platform, account_id)
            DO UPDATE SET
                account_name = excluded.account_name,
                access_token_encrypted = excluded.access_token_encrypted,
                refresh_token_encrypted = COALESCE(
                    excluded.refresh_token_encrypted,
                    social_accounts.refresh_token_encrypted
                ),
                expires_at = excluded.expires_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                platform,
                account_id,
                account_name,
                encrypted_access_token,
                encrypted_refresh_token,
                expires_at,
                metadata_json,
                now,
                now
            )
        )

        row = connection.execute(
            """
            SELECT *
            FROM social_accounts
            WHERE user_id = ?
              AND platform = ?
              AND account_id = ?
            """,
            (
                user_id,
                platform,
                account_id
            )
        ).fetchone()

    return _account_view(row)


# ============================================================
# PUBLIC BASE URL
# ============================================================

def get_public_base_url(request: Request) -> str:
    public_base_url = (
        os.getenv("PUBLIC_BASE_URL") or ""
    ).strip().rstrip("/")

    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail=(
                "PUBLIC_BASE_URL не задан. "
                "Добавьте в .env публичный HTTPS URL, "
                "например: "
                "PUBLIC_BASE_URL=https://your-ngrok-url.ngrok-free.app"
            )
        )

    if not public_base_url.startswith("https://"):
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL должен начинаться с https://"
        )

    return public_base_url


# ============================================================
# TEXT HELPERS
# ============================================================

def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int
) -> List[str]:
    if not text:
        return []

    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        test_line = (
            word
            if not current_line
            else f"{current_line} {word}"
        )

        bbox = draw.textbbox(
            (0, 0),
            test_line,
            font=font
        )

        width = bbox[2] - bbox[0]

        if width <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word

    if current_line:
        lines.append(current_line)

    return lines


def draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy=None,
    font=None,
    fill=None,
    max_width: int = 0,
    line_spacing: int = 12,
    *,
    center_x: int = None,
    y: int = None,
    align: str = "left"
):
    lines = wrap_text(
        draw,
        text,
        font,
        max_width
    )

    if center_x is not None:
        x = center_x
        y = 0 if y is None else y
    else:
        x, y = xy

    if align == "center":
        for line in lines:
            bbox = draw.textbbox(
                (0, 0),
                line,
                font=font
            )
            line_x = x - (bbox[2] - bbox[0]) / 2
            draw.text(
                (line_x, y),
                line,
                font=font,
                fill=fill
            )
            y += (bbox[3] - bbox[1]) + line_spacing
        return y

    for line in lines:
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill
        )

        bbox = draw.textbbox(
            (x, y),
            line,
            font=font
        )

        height = bbox[3] - bbox[1]
        y += height + line_spacing

    return y


def get_text_block_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    line_spacing: int = 12
) -> int:
    lines = wrap_text(
        draw,
        text,
        font,
        max_width
    )

    if not lines:
        return 0

    return sum(
        draw.textbbox((0, 0), line, font=font)[3]
        - draw.textbbox((0, 0), line, font=font)[1]
        for line in lines
    ) + line_spacing * (len(lines) - 1)


def fit_text_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int = 28,
    bold: bool = False
):
    size = start_size

    while size >= min_size:
        font = get_font(size, bold=bold)
        lines = wrap_text(
            draw,
            text,
            font,
            max_width
        )

        line_height = size + 14
        total_height = len(lines) * line_height

        if total_height <= max_height:
            return font

        size -= 2

    return get_font(
        min_size,
        bold=bold
    )


# ============================================================
# OPENAI IMAGE GENERATION
# ============================================================

def _clamp(
    value: float,
    minimum: float,
    maximum: float
) -> float:
    return max(
        minimum,
        min(
            maximum,
            value
        )
    )


def _safe_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int
) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return default

    return max(
        minimum,
        min(
            maximum,
            value
        )
    )


def _normalize_hex_color(
    value: Any,
    fallback: str = FALLBACK_TEXT_COLOR
) -> str:
    value = str(
        value or ""
    ).strip()

    if re.fullmatch(
        r"#[0-9a-fA-F]{6}",
        value
    ):
        return value

    return fallback


def _hex_to_rgb(
    value: str,
    fallback=(255, 255, 255)
):
    value = _normalize_hex_color(
        value,
        FALLBACK_TEXT_COLOR
    )

    try:
        return tuple(
            int(
                value[index:index + 2],
                16
            )
            for index in (1, 3, 5)
        )
    except (ValueError, TypeError):
        return fallback


def _normalize_layout(
    slide: Dict[str, Any]
) -> Dict[str, Any]:

    raw_layout = slide.get("layout")

    if not isinstance(
        raw_layout,
        dict
    ):
        raw_layout = {}

    text_position = str(
        raw_layout.get(
            "text_position",
            slide.get(
                "text_position",
                "left"
            )
        )
        or "left"
    ).strip().lower()

    allowed_positions = {
        "left",
        "right",
        "top",
        "bottom",
        "center"
    }

    if text_position not in allowed_positions:
        text_position = "left"

    align = str(
        raw_layout.get(
            "align",
            "left"
        )
        or "left"
    ).strip().lower()

    if align not in {
        "left",
        "center",
        "right"
    }:
        align = "left"

    # AI normally returns these values.
    # We only clamp them so malformed JSON cannot break rendering.

    x = _clamp(
        float(
            raw_layout.get(
                "x",
                0.08
            )
            or 0.08
        ),
        0.03,
        0.90
    )

    y = _clamp(
        float(
            raw_layout.get(
                "y",
                0.10
            )
            or 0.10
        ),
        0.03,
        0.90
    )

    width = _clamp(
        float(
            raw_layout.get(
                "width",
                0.55
            )
            or 0.55
        ),
        0.25,
        0.88
    )

    title_size = _safe_int(
        raw_layout.get(
            "title_size",
            FALLBACK_TITLE_SIZE
        ),
        FALLBACK_TITLE_SIZE,
        56,
        120
    )

    body_size = _safe_int(
        raw_layout.get(
            "body_size",
            FALLBACK_BODY_SIZE
        ),
        FALLBACK_BODY_SIZE,
        30,
        64
    )

    line_spacing = _safe_int(
        raw_layout.get(
            "line_spacing",
            14
        ),
        14,
        8,
        28
    )

    title_color = _normalize_hex_color(
        raw_layout.get(
            "title_color"
        ),
        FALLBACK_TEXT_COLOR
    )

    body_color = _normalize_hex_color(
        raw_layout.get(
            "body_color"
        ),
        FALLBACK_TEXT_COLOR
    )

    accent_color = _normalize_hex_color(
        raw_layout.get(
            "accent_color"
        ),
        title_color
    )

    accent_words = raw_layout.get(
        "accent_words",
        []
    )

    if not isinstance(
        accent_words,
        list
    ):
        accent_words = []

    accent_words = [
        str(word).strip()
        for word in accent_words
        if str(word).strip()
    ]

    text_panel = raw_layout.get(
        "text_panel"
    )

    if not isinstance(
        text_panel,
        dict
    ):
        text_panel = {}

    panel_enabled = bool(
        text_panel.get(
            "enabled",
            False
        )
    )

    panel_color = _normalize_hex_color(
        text_panel.get(
            "color"
        ),
        "#000000"
    )

    try:
        panel_opacity = float(
            text_panel.get(
                "opacity",
                0.0
            )
            or 0.0
        )
    except (TypeError, ValueError):
        panel_opacity = 0.0

    panel_opacity = _clamp(
        panel_opacity,
        0.0,
        0.88
    )

    panel_radius = _safe_int(
        text_panel.get(
            "radius",
            28
        ),
        28,
        0,
        80
    )

    panel_padding = _safe_int(
        text_panel.get(
            "padding",
            28
        ),
        28,
        8,
        80
    )

    try:
        shadow_opacity = float(
            raw_layout.get(
                "shadow_opacity",
                0.0
            )
            or 0.0
        )
    except (TypeError, ValueError):
        shadow_opacity = 0.0

    shadow_opacity = _clamp(
        shadow_opacity,
        0.0,
        0.7
    )

    return {
        "text_position": text_position,
        "x": x,
        "y": y,
        "width": width,
        "align": align,
        "title_size": title_size,
        "body_size": body_size,
        "line_spacing": line_spacing,
        "title_color": title_color,
        "body_color": body_color,
        "accent_color": accent_color,
        "accent_words": accent_words,
        "text_panel": {
            "enabled": panel_enabled,
            "color": panel_color,
            "opacity": panel_opacity,
            "radius": panel_radius,
            "padding": panel_padding
        },
        "shadow_opacity": shadow_opacity
    }


async def generate_carousel_image(
    visual_prompt: str,
    art_direction: Dict[str, Any] | None = None,
    visual_direction: Dict[str, Any] | None = None,
    slide_number: int | None = None,
    all_slide_visuals: List[Dict[str, Any]] | None = None,
    retry_feedback: str = ""
) -> Dict[str, Any]:

    api_key = os.getenv(
        "OPENAI_API_KEY"
    )

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY не найден"
        )

    art_direction = (
        art_direction
        if isinstance(
            art_direction,
            dict
        )
        else {}
    )

    visual_direction = (
        visual_direction
        if isinstance(
            visual_direction,
            dict
        )
        else {}
    )

    all_slide_visuals = (
        all_slide_visuals
        if isinstance(
            all_slide_visuals,
            list
        )
        else []
    )

    # --------------------------------------------------------
    # НЕ ПОВТОРЯЕМ КОМПОЗИЦИИ ДРУГИХ СЛАЙДОВ
    # --------------------------------------------------------

    other_slide_context = []

    for item in all_slide_visuals:

        if not isinstance(
            item,
            dict
        ):
            continue

        item_number = item.get(
            "number"
        )

        if (
            slide_number is not None
            and item_number == slide_number
        ):
            continue

        composition = str(
            item.get(
                "composition",
                ""
            )
        ).strip()

        visual_role = str(
            item.get(
                "visual_role",
                ""
            )
        ).strip()

        if composition or visual_role:
            other_slide_context.append(
                {
                    "number": item_number,
                    "visual_role": visual_role,
                    "composition": composition
                }
            )

    other_slides_text = (
        json.dumps(
            other_slide_context,
            ensure_ascii=False,
            indent=2
        )
        if other_slide_context
        else "Нет данных."
    )

    art_direction_text = json.dumps(
        art_direction,
        ensure_ascii=False,
        indent=2
    )

    visual_direction_text = json.dumps(
        visual_direction,
        ensure_ascii=False,
        indent=2
    )

    feedback_text = (
        f"""
PREVIOUS GENERATION REVIEW:
{retry_feedback}

Fix these issues in the new generation.
Do not merely make a cosmetic variation.
Actually change the problematic visual decision.
"""
        if retry_feedback
        else ""
    )

    prompt = f"""
Create the visual artwork for ONE specific slide of a premium
social-media carousel.

This is NOT a template.
Do NOT create a generic social-media graphic.
Do NOT imitate a recurring card design.

The image must look like a deliberately art-directed visual
created specifically for this subject and this slide.

CAROUSEL ART DIRECTION:
{art_direction_text}

THIS SLIDE'S VISUAL DIRECTION:
{visual_direction_text}

FINAL VISUAL PROMPT:
{visual_prompt}

OTHER SLIDES — DO NOT REPEAT THEIR COMPOSITION:
{other_slides_text}

SLIDE NUMBER:
{slide_number if slide_number is not None else "unknown"}

STRICT VISUAL REQUIREMENTS:

- communicate the exact meaning of this slide;
- the main subject must be recognizable immediately;
- use a composition appropriate specifically to this slide;
- change the camera angle, framing, perspective, subject arrangement
  or visual metaphor when appropriate;
- do not reuse a generic centered-object composition;
- do not create a generic abstract background;
- do not create a presentation template;
- do not create boxes, UI panels, cards, infographic layouts,
  decorative circles or stock-style placeholders unless the
  visual direction explicitly requires them;
- maintain the overall art direction of the carousel;
- preserve visual continuity with the carousel without repeating
  the same composition;
- leave intentional negative space for typography;
- the negative space must make visual sense in the scene;
- use professional composition, lighting and depth;
- make the image feel commercially polished and premium;
- the image must be 4:5 portrait;
- no text;
- no typography;
- no captions;
- no subtitles;
- no letters;
- no numbers;
- no logos;
- no watermarks;
- no interface elements;
- no fake labels;
- no random symbols.

{feedback_text}
"""

    request_payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": IMAGE_GENERATION_SIZE,
        "quality": OPENAI_IMAGE_QUALITY,
        "output_format": "png",
        "background": "opaque",
        "n": 1
    }

    last_response_text = ""

    for attempt in range(
        1,
        4
    ):

        try:

            async with httpx.AsyncClient(
                timeout=CAROUSEL_IMAGE_TIMEOUT
            ) as client:

                response = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={
                        "Authorization": (
                            f"Bearer {api_key}"
                        ),
                        "Content-Type": (
                            "application/json"
                        )
                    },
                    json=request_payload
                )

        except (
            httpx.TimeoutException,
            httpx.NetworkError
        ) as exc:

            if attempt >= 3:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "error": (
                            "OpenAI Image API "
                            "не ответил"
                        ),
                        "attempt": attempt,
                        "detail": str(exc)
                    }
                ) from exc

            await asyncio.sleep(
                attempt * 3
            )
            continue

        last_response_text = response.text

        if response.status_code == 200:
            try:
                data = response.json()
                encoded = data["data"][0]["b64_json"]

                return {
                    "bytes": base64.b64decode(encoded),
                    "usage": data.get("usage") or {},
                    "model": OPENAI_IMAGE_MODEL,
                    "quality": OPENAI_IMAGE_QUALITY,
                    "size": IMAGE_GENERATION_SIZE,
                }

            except (
                KeyError,
                IndexError,
                ValueError,
                TypeError
            ) as exc:

                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "OpenAI не вернул "
                            "корректное изображение"
                        ),
                        "response": response.text
                    }
                ) from exc

        # Retry на временные ошибки.
        if (
            response.status_code == 429
            or response.status_code >= 500
        ):
            if attempt < 3:
                await asyncio.sleep(
                    attempt * 3
                )
                continue

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "Ошибка генерации "
                    "изображения OpenAI"
                ),
                "status": response.status_code,
                "response": last_response_text
            }
        )

    raise HTTPException(
        status_code=502,
        detail="Не удалось получить изображение OpenAI"
    )


async def review_carousel_image(
    image_bytes: bytes,
    slide: Dict[str, Any]
) -> Dict[str, Any]:

    api_key = os.getenv(
        "OPENAI_API_KEY"
    )

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY не найден"
        )

    image_base64 = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    visual_prompt = str(
        slide.get(
            "visual_prompt",
            ""
        )
    ).strip()

    visual_direction = slide.get(
        "visual_direction",
        {}
    )

    if not isinstance(
        visual_direction,
        dict
    ):
        visual_direction = {}

    layout = slide.get(
        "layout",
        {}
    )

    if not isinstance(
        layout,
        dict
    ):
        layout = {}

    text_position = str(
        layout.get(
            "text_position",
            slide.get(
                "text_position",
                "left"
            )
        )
    ).strip()

    review_prompt = f"""
You are reviewing an AI-generated visual that will be used as
ONE slide in a professional social-media carousel.

Review only the IMAGE itself.

SLIDE TITLE:
{slide.get("title", "")}

SLIDE TEXT:
{slide.get("text", "")}

TEXT POSITION:
{text_position}

PLANNED VISUAL DIRECTION:
{json.dumps(
    visual_direction,
    ensure_ascii=False,
    indent=2
)}

PLANNED VISUAL PROMPT:
{visual_prompt}

Return JSON only:

{{
  "pass": true,
  "relevance_score": 0,
  "composition_score": 0,
  "text_safe_area_score": 0,
  "professional_quality_score": 0,
  "contains_text": false,
  "contains_logo_or_watermark": false,
  "repeats_generic_template": false,
  "matches_subject": true,
  "issues": ["Vision review unavailable"],
  "regeneration_instruction": ""
}}

Evaluation rules:

- The visual must directly show or convincingly communicate
  the subject of the slide.
- The image must not feel like a generic template.
- The image must not look like a random stock background.
- The composition must create meaningful negative space for
  text at the planned text position.
- The scene must look professionally art-directed.
- Reject visible text, fake labels, logos and watermarks.
- Reject major contradictions with the visual prompt.
- Reject repeated or lazy composition.
- Minor stylistic imperfections are acceptable.
- Be strict about subject relevance.
"""

    payload = {
        "model": CAROUSEL_IMAGE_REVIEW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict visual art director. "
                    "Return valid JSON only."
                )
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": review_prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                "data:image/png;base64,"
                                + image_base64
                            ),
                            "detail": "high"
                        }
                    }
                ]
            }
        ],
        "response_format": {
            "type": "json_object"
        },
        "max_completion_tokens": 700
    }

    try:

        async with httpx.AsyncClient(
            timeout=120.0
        ) as client:

            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": (
                        f"Bearer {api_key}"
                    ),
                    "Content-Type": (
                        "application/json"
                    )
                },
                json=payload
            )

    except (
        httpx.TimeoutException,
        httpx.NetworkError
    ) as exc:

        # Review не должна ломать всю генерацию.
        print(
            "CAROUSEL IMAGE REVIEW ERROR:",
            type(exc).__name__,
            str(exc)
        )

        return {
            "review": {
                "pass": True,
                "review_unavailable": True,
                "issues": [
                    "Vision review unavailable"
                ]
            },
            "usage": {},
            "model": CAROUSEL_IMAGE_REVIEW_MODEL
        }

    if response.status_code != 200:

        print(
            "CAROUSEL IMAGE REVIEW HTTP ERROR:",
            response.status_code,
            response.text
        )

    return {
        "review": {
            "pass": True,
            "review_unavailable": True,
            "issues": [
                (
                    "Vision review returned HTTP "
                    f"{response.status_code}"
                )
            ]
        },
        "usage": {},
        "model": CAROUSEL_IMAGE_REVIEW_MODEL
    }

    try:

        result_data = response.json()

        content = (
            result_data["choices"][0]["message"]["content"]
        )

        review = json.loads(
            content
        )

    except (
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError
    ) as exc:

        print(
            "CAROUSEL IMAGE REVIEW PARSE ERROR:",
            repr(exc)
        )

        return {
            "review": {
                "pass": True,
                "review_unavailable": True,
                "issues": [
                    "Vision review JSON could not be parsed"
                ]
            },
            "usage": {},
            "model": CAROUSEL_IMAGE_REVIEW_MODEL
        }

    if not isinstance(
        review,
        dict
    ):
        return {
            "review": {
                "pass": True,
                "review_unavailable": True,
                "issues": [
                    "Vision review returned invalid object"
                ]
            },
            "usage": {},
            "model": CAROUSEL_IMAGE_REVIEW_MODEL
        }

    return {
        "review": review,
        "usage": result_data.get("usage") or {},
        "model": CAROUSEL_IMAGE_REVIEW_MODEL
    }

def _draw_dynamic_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    x: float,
    y: float,
    font,
    max_width: int,
    align: str,
    title_color: str,
    accent_color: str,
    accent_words: List[str],
    line_spacing: int
) -> int:

    title = str(
        title or ""
    ).strip()

    if not title:
        return int(y)

    words = title.upper().split()

    accent_set = {
        word.upper()
        for word in accent_words
    }

    lines = []
    current = []

    for word in words:

        candidate = (
            word
            if not current
            else " ".join(
                current + [word]
            )
        )

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font
        )

        candidate_width = (
            bbox[2] - bbox[0]
        )

        if (
            candidate_width <= max_width
            or not current
        ):
            current.append(word)
        else:
            lines.append(
                current
            )
            current = [word]

    if current:
        lines.append(
            current
        )

    for line_words in lines:

        line_text = " ".join(
            line_words
        )

        line_bbox = draw.textbbox(
            (0, 0),
            line_text,
            font=font
        )

        line_width = (
            line_bbox[2]
            - line_bbox[0]
        )

        if align == "center":
            cursor_x = (
                x
                - line_width / 2
            )

        elif align == "right":
            cursor_x = (
                x
                - line_width
            )

        else:
            cursor_x = x

        for index, word in enumerate(
            line_words
        ):

            suffix = (
                " "
                if index < len(line_words) - 1
                else ""
            )

            token = (
                word
                + suffix
            )

            token_bbox = draw.textbbox(
                (0, 0),
                token,
                font=font
            )

            token_width = (
                token_bbox[2]
                - token_bbox[0]
            )

            color = (
                accent_color
                if word.upper() in accent_set
                else title_color
            )

            draw.text(
                (
                    cursor_x,
                    y
                ),
                token,
                font=font,
                fill=color
            )

            cursor_x += token_width

        line_height = (
            line_bbox[3]
            - line_bbox[1]
        )

        y += (
            line_height
            + line_spacing
        )

    return int(y)

def add_slide_text(
    image: Image.Image,
    slide: Dict[str, Any]
) -> Image.Image:

    image = image.convert("RGBA")
    draw = ImageDraw.Draw(image)

    title = str(
        slide.get(
            "title",
            ""
        )
        or ""
    ).strip()

    text = str(
        slide.get(
            "text",
            ""
        )
        or ""
    ).strip()

    layout = _normalize_layout(
        slide
    )

    x = (
        layout["x"]
        * IMAGE_WIDTH
    )

    y = (
        layout["y"]
        * IMAGE_HEIGHT
    )

    max_width = int(
        layout["width"]
        * IMAGE_WIDTH
    )

    align = layout["align"]

    # --------------------------------------------------------
    # БЕЗОПАСНАЯ ГОРИЗОНТАЛЬНАЯ ЗОНА
    # --------------------------------------------------------

    safe_left_margin = 80
    safe_right_margin = 80

    if align == "left":
        min_x = safe_left_margin
        max_x = IMAGE_WIDTH - safe_right_margin - max_width

    elif align == "right":
        min_x = safe_left_margin + max_width
        max_x = IMAGE_WIDTH - safe_right_margin

    elif align == "center":
        min_x = (
            safe_left_margin
            + max_width / 2
        )
        max_x = (
            IMAGE_WIDTH
            - safe_right_margin
            - max_width / 2
        )

    else:
        min_x = safe_left_margin
        max_x = (
            IMAGE_WIDTH
            - safe_right_margin
            - max_width
        )

    if max_x < min_x:
        max_x = min_x

    x = _clamp(
        x,
        min_x,
        max_x
    )

    # --------------------------------------------------------
    # РАСЧЕТ ДОСТУПНОЙ ВЕРТИКАЛЬНОЙ ВЫСОТЫ БЕЗ ВЫХОДА ЗА РАМКИ
    # --------------------------------------------------------

    safe_top_margin = 60
    safe_bottom_margin = 60

    panel = layout["text_panel"]
    
    # ПРИНУДИТЕЛЬНО ВКЛЮЧАЕМ ПОДЛОЖКУ НА ВСЕХ СЛАЙДАХ ДЛЯ ЧИТАЕМОСТИ
    panel["enabled"] = True
    if panel["opacity"] < 0.35:
        panel["opacity"] = 0.75  # Делаем подложку плотной и заметной

    panel_padding = panel["padding"]

    min_text_y = (
        safe_top_margin
        + panel_padding
    )

    max_available_height = (
        IMAGE_HEIGHT
        - safe_bottom_margin
        - y
        - (panel_padding * 2)
    )

    if max_available_height < 200:
        max_available_height = 200

    title_font = fit_text_font(
        draw,
        title,
        max_width=max_width - 20, # Небольшой запас от краев
        max_height=int(max_available_height * 0.45),
        start_size=layout["title_size"],
        min_size=40,
        bold=True
    )

    body_font = fit_text_font(
        draw,
        text,
        max_width=max_width - 20,
        max_height=int(max_available_height * 0.55),
        start_size=layout["body_size"],
        min_size=26,
        bold=False
    )

    title_height = get_text_block_height(
        draw,
        title,
        title_font,
        max_width,
        line_spacing=layout["line_spacing"]
    )

    body_height = get_text_block_height(
        draw,
        text,
        body_font,
        max_width,
        line_spacing=layout["line_spacing"]
    )

    total_height = (
        title_height
        + (
            28
            if title
            and text
            else 0
        )
        + body_height
    )

    max_text_y = (
        IMAGE_HEIGHT
        - safe_bottom_margin
        - panel_padding
        - total_height
    )

    if max_text_y < min_text_y:
        max_text_y = min_text_y

    y = _clamp(
        y,
        min_text_y,
        max_text_y
    )

    # --------------------------------------------------------
    # ОПЦИОНАЛЬНАЯ AI-ПАНЕЛЬ ПОД ТЕКСТ (С ИСПРАВЛЕННЫМИ ОТСТУПАМИ)
    # --------------------------------------------------------

    if panel["enabled"]:

        padding = panel["padding"]

        panel_left = (
            x
            if align != "right"
            else x - max_width
        )

        if align == "center":
            panel_left = (
                x
                - max_width / 2
            )

        panel_top = y

        panel_right = (
            panel_left
            + max_width
            + padding * 2
        )

        panel_bottom = (
            panel_top
            + total_height
            + padding * 2
        )

        # Безопасные отступы рамки от границ картинки, чтобы текст не выпирал вправо
        panel_left = _clamp(panel_left - padding, 40, IMAGE_WIDTH - 40)
        panel_top = _clamp(panel_top - padding, 40, IMAGE_HEIGHT - 40)
        panel_right = _clamp(panel_right, 40, IMAGE_WIDTH - 40)
        panel_bottom = _clamp(panel_bottom, 40, IMAGE_HEIGHT - 40)

        overlay = Image.new(
            "RGBA",
            image.size,
            (
                0,
                0,
                0,
                0
            )
        )

        overlay_draw = ImageDraw.Draw(
            overlay
        )

        r, g, b = _hex_to_rgb(
            panel["color"]
        )

        alpha = int(
            panel["opacity"] * 255
        )

        overlay_draw.rounded_rectangle(
            (
                panel_left,
                panel_top,
                panel_right,
                panel_bottom
            ),
            radius=panel["radius"],
            fill=(
                r,
                g,
                b,
                alpha
            )
        )

        image = Image.alpha_composite(
            image,
            overlay
        )

        draw = ImageDraw.Draw(
            image
        )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    current_y = y

    if title:

        current_y = _draw_dynamic_title(
            draw,
            title,
            x,
            current_y,
            title_font,
            max_width,
            align,
            layout["title_color"],
            layout["accent_color"],
            layout["accent_words"],
            layout["line_spacing"]
        )

    # --------------------------------------------------------
    # GAP
    # --------------------------------------------------------

    if (
        title
        and text
    ):
        current_y += 28

    # --------------------------------------------------------
    # BODY
    # --------------------------------------------------------

    if text:

        body_lines = wrap_text(
            draw,
            text,
            body_font,
            max_width
        )

        for line in body_lines:

            bbox = draw.textbbox(
                (0, 0),
                line,
                font=body_font
            )

            line_width = (
                bbox[2]
                - bbox[0]
            )

            if align == "center":
                draw_x = (
                    x
                    - line_width / 2
                )
            elif align == "right":
                draw_x = (
                    x
                    - line_width
                )
            else:
                draw_x = x

            draw.text(
                (
                    draw_x,
                    current_y
                ),
                line,
                font=body_font,
                fill=layout["body_color"]
            )

            current_y += (
                bbox[3]
                - bbox[1]
                + layout["line_spacing"]
            )

    return image.convert(
        "RGB"
    )

async def _generate_single_slide(
    slide: Dict[str, Any],
    carousel: Dict[str, Any],
    all_slide_visuals: List[Dict[str, Any]]
) -> Dict[str, Any]:

    async with CAROUSEL_IMAGE_SEMAPHORE:

        visual_prompt = str(
            slide.get(
                "visual_prompt",
                ""
            )
        ).strip()

        if not visual_prompt:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"У слайда "
                    f"{slide.get('number', '?')} "
                    "отсутствует visual_prompt"
                )
            )

        art_direction = carousel.get(
            "art_direction",
            {}
        )

        if not isinstance(
            art_direction,
            dict
        ):
            art_direction = {}

        visual_direction = slide.get(
            "visual_direction",
            {}
        )

        if not isinstance(
            visual_direction,
            dict
        ):
            visual_direction = {}

        last_feedback = ""
        last_image = None
        last_review = None

        max_attempts = max(
            1,
            CAROUSEL_IMAGE_MAX_ATTEMPTS
        )

        for attempt in range(
            1,
            max_attempts + 1
        ):

            print(
                f"Generating carousel image "
                f"{slide.get('number', '?')} "
                f"attempt {attempt}/{max_attempts}..."
            )

            image_result = await generate_carousel_image(
                visual_prompt=visual_prompt,
                art_direction=art_direction,
                visual_direction=visual_direction,
                slide_number=slide.get(
                    "number"
                ),
                all_slide_visuals=all_slide_visuals,
                retry_feedback=last_feedback
            )

            image_bytes = image_result["bytes"]
            image_usage = image_result.get("usage") or {}
            image_model = image_result.get("model")

            last_image = image_bytes

                       # ------------------------------------------------
            # OPTIONAL VISION REVIEW
            # ------------------------------------------------

            if not CAROUSEL_IMAGE_REVIEW_ENABLED:

                return {
                    "bytes": image_bytes,
                    "review": None,
                    "attempt": attempt,
                    "usage": {
                        "image": image_usage,
                        "vision": {}
                    },
                    "models": {
                        "image": image_model,
                        "vision": None
                    }
                }

            review_result = await review_carousel_image(
                image_bytes,
                slide
            )

            review = review_result.get(
                "review"
            ) or {}

            review_usage = review_result.get(
                "usage"
            ) or {}

            review_model = review_result.get(
                "model"
            )

            last_review = review

            if review.get(
                "review_unavailable",
                False
            ):

                print(
                    "CAROUSEL IMAGE REVIEW: unavailable; "
                    "accepting generated image"
                )

                return {
    "bytes": image_bytes,
    "review": review,
    "attempt": attempt,
    "usage": {
        "image": image_usage,
        "vision": review_usage,
    },
    "models": {
        "image": image_model,
        "vision": review_model,
    },
}

            passed = bool(
                review.get(
                    "pass",
                    False
                )
            )

            print(
                "CAROUSEL IMAGE REVIEW:",
                slide.get("number"),
                review
            )

            if passed:

                return {
                    "bytes": image_bytes,
                    "review": review,
                    "attempt": attempt,
                    "usage": {
                        "image": image_usage,
                        "vision": review_usage
                    },
                    "models": {
                        "image": image_model,
                        "vision": review_model
                    }
                }

            issues = review.get(
                "issues",
                []
            )

            regeneration_instruction = str(
                review.get(
                    "regeneration_instruction",
                    ""
                )
                or ""
            ).strip()

            feedback_parts = []

            if isinstance(
                issues,
                list
            ):
                feedback_parts.extend(
                    str(issue).strip()
                    for issue in issues
                    if str(issue).strip()
                )

            if regeneration_instruction:
                feedback_parts.append(
                    regeneration_instruction
                )

            last_feedback = "\n".join(
                feedback_parts
            ).strip()

        # ----------------------------------------------------
        # Если все попытки не прошли review,
        # НЕ публикуем потенциально плохой результат.
        # ----------------------------------------------------

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "AI visual не прошёл "
                    "визуальную проверку"
                ),
                "slide": slide.get(
                    "number"
                ),
                "attempts": max_attempts,
                "review": last_review,
                "last_feedback": last_feedback
            }
        )


# ============================================================
# RENDER CAROUSEL
# ============================================================

async def render_carousel(
    carousel: Dict[str, Any],
    base_url: str
) -> Dict[str, Any]:

    slides = carousel.get(
        "slides"
    )

    if not isinstance(
        slides,
        list
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "В carousel отсутствует "
                "массив slides"
            )
        )

    if not slides:
        raise HTTPException(
            status_code=400,
            detail=(
                "Карусель не содержит "
                "слайдов"
            )
        )

    if len(slides) < 2:
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации нужно "
                "минимум 2 слайда"
            )
        )

    if len(slides) > 10:
        raise HTTPException(
            status_code=400,
            detail=(
                "Максимальное количество "
                "слайдов — 10"
            )
        )

    base_url = (
        base_url
        .strip()
        .rstrip("/")
    )

    if not base_url:
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось определить "
                "BASE URL"
            )
        )

    carousel_id = (
        uuid.uuid4().hex
    )

    carousel_dir = (
        GENERATED_DIR
        / carousel_id
    )

    carousel_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # ========================================================
    # КОНТЕКСТ ВИЗУАЛОВ ВСЕЙ КАРУСЕЛИ
    # ========================================================

    all_slide_visuals = []

    for index, slide in enumerate(
        slides,
        start=1
    ):

        if not isinstance(
            slide,
            dict
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Слайд {index} "
                    "имеет неправильный формат"
                )
            )

        visual_direction = slide.get(
            "visual_direction",
            {}
        )

        if not isinstance(
            visual_direction,
            dict
        ):
            visual_direction = {}

        all_slide_visuals.append(
            {
                "number": slide.get(
                    "number",
                    index
                ),
                "visual_role": (
                    visual_direction.get(
                        "visual_role",
                        ""
                    )
                ),
                "composition": (
                    visual_direction.get(
                        "composition",
                        ""
                    )
                )
            }
        )

    # ========================================================
    # ГЕНЕРИРУЕМ ВИЗУАЛЫ ПАРАЛЛЕЛЬНО
    # ========================================================

    tasks = [
        _generate_single_slide(
            slide=slide,
            carousel=carousel,
            all_slide_visuals=all_slide_visuals
        )
        for slide in slides
    ]

    generated_assets = await asyncio.gather(
        *tasks
    )


    image_total_input_tokens = 0
    image_total_output_tokens = 0

    vision_total_input_tokens = 0
    vision_total_output_tokens = 0

    image_model = OPENAI_IMAGE_MODEL
    vision_model = CAROUSEL_IMAGE_REVIEW_MODEL

    for asset in generated_assets:

        asset_usage = asset.get(
            "usage"
        ) or {}

        image_usage = asset_usage.get(
            "image"
        ) or {}

        vision_usage = asset_usage.get(
            "vision"
        ) or {}

        image_total_input_tokens += int(
            image_usage.get(
                "input_tokens",
                0
            ) or 0
        )

        image_total_output_tokens += int(
            image_usage.get(
                "output_tokens",
                0
            ) or 0
        )

        vision_total_input_tokens += int(
            vision_usage.get(
                "input_tokens",
                0
            ) or 0
        )

        vision_total_output_tokens += int(
            vision_usage.get(
                "output_tokens",
                0
            ) or 0
        )

    rendered_slides = []

    # ========================================================
    # ФИНАЛЬНЫЙ RENDER
    # ========================================================

    for index, (
        slide,
        asset
    ) in enumerate(
        zip(
            slides,
            generated_assets
        ),
        start=1
    ):

        image_bytes = asset[
            "bytes"
        ]

        try:

            image = Image.open(
                io.BytesIO(
                    image_bytes
                )
            )

            image = image.convert(
                "RGB"
            )

        except Exception as exc:

            raise HTTPException(
                status_code=502,
                detail=(
                    f"Не удалось открыть "
                    f"изображение слайда {index}: "
                    f"{str(exc)}"
                )
            ) from exc

        # GPT Image 2 отдаёт 1024x1280.
        # Здесь только технический resize.
        image = image.resize(
            (
                IMAGE_WIDTH,
                IMAGE_HEIGHT
            ),
            Image.Resampling.LANCZOS
        )

        # ====================================================
        # ТОЛЬКО ТУТ PYTHON НАКЛАДЫВАЕТ ТЕКСТ
        # ====================================================

        image = add_slide_text(
            image,
            slide
        )

        output_path = (
            carousel_dir
            / f"slide-{index:02d}.png"
        )

        image.save(
            output_path,
            "PNG",
            optimize=True
        )

        relative_url = (
            f"/generated/"
            f"{carousel_id}/"
            f"slide-{index:02d}.png"
        )

        review = asset.get(
            "review"
        )

        layout = _normalize_layout(
            slide
        )

        rendered_slides.append(
            {
                "number": slide.get(
                    "number",
                    index
                ),
                "type": slide.get(
                    "type",
                    "content"
                ),
                "title": slide.get(
                    "title",
                    ""
                ),
                "text": slide.get(
                    "text",
                    ""
                ),
                "visual_prompt": str(
                    slide.get(
                        "visual_prompt",
                        ""
                    )
                ).strip(),
                "visual_direction": slide.get(
                    "visual_direction",
                    {}
                ),
                "text_position": layout[
                    "text_position"
                ],
                "layout": layout,
                "image_generation": {
                    "model": OPENAI_IMAGE_MODEL,
                    "quality": OPENAI_IMAGE_QUALITY,
                    "attempt": asset.get(
                        "attempt",
                        1
                    ),
                    "review": review
                },
                "url": (
                    base_url
                    + relative_url
                ),
                "path": str(
                    output_path
                )
            }
        )

    return {
        "carousel_id": carousel_id,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "image_model": OPENAI_IMAGE_MODEL,
        "image_quality": OPENAI_IMAGE_QUALITY,
        "art_direction": carousel.get(
            "art_direction",
            {}
        ),
        "slides": rendered_slides,
        "usage": {
            "image_generation": {
                "model": image_model,
                "slides": len(generated_assets),
                "total_input_tokens": image_total_input_tokens,
                "total_output_tokens": image_total_output_tokens
            },
            "vision_review": {
                "model": vision_model,
                "reviews": len(generated_assets),
                "input_tokens": vision_total_input_tokens,
                "output_tokens": vision_total_output_tokens
            }
        }
    }

# ============================================================
# VIDEO GENERATION
# ============================================================

LAST_CLIP_DURATION = 5
VIDEO_RESOLUTION = "720p"
VIDEO_ASPECT_RATIO = "9:16"
VIDEO_POLL_INTERVAL = 5
VIDEO_MAX_POLL_ATTEMPTS = 180


# ============================================================
# VIDEO HELPERS
# ============================================================

def get_ffmpeg_path() -> str:
    configured_path = os.getenv("FFMPEG_PATH", "").strip()
    candidates = [
        configured_path,
        shutil.which("ffmpeg") or "",
        str(
            Path.home()
            / "AppData/Local/Microsoft/WinGet/Packages"
            / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-9.0.1-full_build/bin/ffmpeg.exe"
        )
    ]

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))

    raise HTTPException(
        status_code=500,
        detail=(
            "FFmpeg не найден. Установите его через winget или задайте "
            "FFMPEG_PATH в .env. Проверка: ffmpeg -version"
        )
    )


def parse_timeline_second(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a second")

    if isinstance(value, (int, float)):
        second = int(value)
    else:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("empty second")

        timestamp_match = re.fullmatch(r"(\d+):(\d{1,2})", text)
        if timestamp_match:
            second = (
                int(timestamp_match.group(1)) * 60
                + int(timestamp_match.group(2))
            )
        else:
            number_match = re.search(r"\d+(?:[.,]\d+)?", text)
            if not number_match:
                raise ValueError("second is not numeric")
            second = int(float(number_match.group(0).replace(",", ".")))

    if second < 0:
        raise ValueError("second cannot be negative")

    return second


async def download_video(
    video_url: str,
    output_path: Path
):
    timeout = httpx.Timeout(
        connect=30.0,
        read=300.0,
        write=300.0,
        pool=30.0
    )

    try:
        async with httpx.AsyncClient(
            timeout=timeout
        ) as client:
            async with client.stream(
                "GET",
                video_url
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Не удалось скачать "
                            "сгенерированное видео: "
                            f"HTTP {response.status_code}"
                        )
                    )

                with output_path.open(
                    "wb"
                ) as file:
                    async for chunk in response.aiter_bytes(
                        chunk_size=1024 * 1024
                    ):
                        file.write(chunk)

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Ошибка скачивания видео: "
                f"{type(e).__name__}: {str(e)}"
            )
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise HTTPException(
            status_code=502,
            detail="Скачанный видеофайл пуст"
        )


async def concat_video_clips(
    clips: List[Path],
    final_path: Path,
    concat_file: Path
):
    ffmpeg_path = get_ffmpeg_path()

    with concat_file.open(
        "w",
        encoding="utf-8"
    ) as file:
        for clip in clips:
            safe_path = (
                str(clip.resolve())
                .replace("\\", "/")
                .replace("'", "'\\''")
            )
            file.write(
                f"file '{safe_path}'\n"
            )

    command = [
        ffmpeg_path,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-shortest",
        str(final_path)
    ]

    def run_ffmpeg():
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False
        )

    process = await asyncio.to_thread(run_ffmpeg)

    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "FFmpeg не смог собрать видео",
                "stderr": process.stderr.decode(
                    "utf-8",
                    errors="ignore"
                )
            }
        )

    if not final_path.exists() or final_path.stat().st_size == 0:
        raise HTTPException(
            status_code=500,
            detail="FFmpeg создал пустой итоговый файл"
        )


async def generate_reels_video_fallback(
    reels: Dict[str, Any],
    base_url: str,
    video_factory=None
) -> Dict[str, Any]:
    """Keep script generation successful when MP4 rendering is unavailable."""
    if video_factory is None:
        return {
            "status": "video_generation_unavailable",
            "error": "OpenAI не предоставляет доступную генерацию видео",
            "detail": (
                "OpenAI Videos API остановлена. Сценарий Reels создан, "
                "но MP4 не сформирован."
            )
        }

    generator = video_factory

    try:
        video = await asyncio.wait_for(
            generator(reels, base_url),
            timeout=90.0
        )
        return {
            "status": "success",
            "video": video
        }
    except asyncio.TimeoutError:
        return {
            "status": "video_generation_timeout",
            "error": "Генерация Reels превысила лимит времени",
            "detail": "Видео генерировалось дольше 90 секунд."
        }
    except (HTTPException, Exception) as exc:
        message = (
            exc.detail
            if isinstance(exc, HTTPException)
            else str(exc)
        )
        return {
            "status": "video_generation_failed",
            "error": "Не удалось сгенерировать Reels-video",
            "detail": message
        }


async def generate_reels_video(
    reels: Dict[str, Any],
    base_url: str
) -> Dict[str, Any]:
    raise HTTPException(
        status_code=503,
        detail=(
            "Генерация MP4 сейчас недоступна: OpenAI Videos API "
            "остановлена. Сценарий Reels можно сгенерировать отдельно."
        )
    )


# ============================================================
# YOUTUBE VIDEO UPLOAD
# ============================================================

async def upload_video_to_youtube(
    video: Dict[str, Any],
    access_token: str,
    title: str,
    description: str = ""
) -> dict:

    video_path = str(
        video.get("path", "")
    ).strip()

    if not video_path:
        raise HTTPException(
            status_code=400,
            detail="У видео отсутствует локальный path"
        )

    video_file = Path(video_path)

    if not video_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Файл видео не найден"
        )

    generated_root = (
        GENERATED_DIR.resolve()
    )

    try:
        video_file.resolve().relative_to(
            generated_root
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Недопустимый путь к видео"
        ) from exc

    title = str(
        title or "RespondService Video"
    ).strip()

    description = str(
        description or ""
    ).strip()

    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "unlisted"
        }
    }

    file_size = video_file.stat().st_size

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        # ----------------------------------------------------
        # 1. СОЗДАЁМ RESUMABLE UPLOAD SESSION
        # ----------------------------------------------------

     init_response = await client.post(
    "https://www.googleapis.com/upload/youtube/v3/videos",
    params={
        "uploadType": "resumable",
        "part": "snippet,status"
    },
    headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4"
    },
    json=metadata
)

    if init_response.status_code not in (
        200,
        201
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "YouTube не смог "
                    "создать upload session"
                ),
                "status": init_response.status_code,
                "response": init_response.text
            }
        )

    upload_url = (
        init_response.headers.get(
            "Location"
        )
    )

    if not upload_url:
        raise HTTPException(
            status_code=502,
            detail="YouTube не вернул upload URL"
        )

    # --------------------------------------------------------
    # 2. ЗАГРУЖАЕМ MP4
    # --------------------------------------------------------

    with video_file.open(
        "rb"
    ) as video_stream:

        video_bytes = video_stream.read()

    async with httpx.AsyncClient(
        timeout=300.0
    ) as client:

        upload_response = await client.put(
            upload_url,

            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                ),
                "Content-Type": "video/mp4",
                "Content-Length": str(
                    file_size
                )
            },

            content=video_bytes
        )

    if upload_response.status_code not in (
        200,
        201
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "YouTube не принял "
                    "видео"
                ),
                "status": upload_response.status_code,
                "response": upload_response.text
            }
        )

    try:
        result = upload_response.json()

    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube вернул "
                "невалидный JSON"
            )
        ) from exc

    youtube_video_id = (
        result.get("id")
    )

    if not youtube_video_id:
        raise HTTPException(
            status_code=502,
            detail="YouTube не вернул ID видео"
        )

    return {
        "video_id": youtube_video_id,
        "url": (
            "https://www.youtube.com/watch?v="
            + youtube_video_id
        ),
        "title": title,
        "privacy_status": "private"
    }
# ============================================================
# РАСЧЁТ ФАКТИЧЕСКОЙ СТОИМОСТИ OPENAI
# ============================================================

def calculate_usage_cost(
    usage: Dict[str, Any]
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # 1. TEXT GENERATION
    # GPT-5.6 Luna
    # --------------------------------------------------------

    text_usage = (
        usage.get("text_generation")
        or {}
    )

    text_input_tokens = int(
        text_usage.get(
            "input_tokens",
            0
        )
        or 0
    )

    text_output_tokens = int(
        text_usage.get(
            "output_tokens",
            0
        )
        or 0
    )

    text_input_cost = (
        text_input_tokens
        / 1_000_000
        * 0.20
    )

    text_output_cost = (
        text_output_tokens
        / 1_000_000
        * 1.20
    )

    text_cost = (
        text_input_cost
        + text_output_cost
    )

    # --------------------------------------------------------
    # 2. IMAGE GENERATION
    # GPT-Image-2
    # --------------------------------------------------------

    image_usage = (
        usage.get("image_generation")
        or {}
    )

    image_input_tokens = int(
        image_usage.get(
            "total_input_tokens",
            0
        )
        or 0
    )

    image_output_tokens = int(
        image_usage.get(
            "total_output_tokens",
            0
        )
        or 0
    )

    image_input_cost = (
        image_input_tokens
        / 1_000_000
        * 2.50
    )

    image_output_cost = (
        image_output_tokens
        / 1_000_000
        * 15.00
    )

    image_cost = (
        image_input_cost
        + image_output_cost
    )

    # --------------------------------------------------------
    # 3. VISION REVIEW
    # --------------------------------------------------------

    vision_usage = (
        usage.get("vision_review")
        or {}
    )

    vision_input_tokens = int(
        vision_usage.get(
            "input_tokens",
            0
        )
        or 0
    )

    vision_output_tokens = int(
        vision_usage.get(
            "output_tokens",
            0
        )
        or 0
    )

    # Здесь цена зависит от модели Vision.
    # Пока используем те же ставки GPT-5.6 Luna,
    # если CAROUSEL_IMAGE_REVIEW_MODEL = gpt-5.6-luna.

    vision_input_cost = (
        vision_input_tokens
        / 1_000_000
        * 0.20
    )

    vision_output_cost = (
        vision_output_tokens
        / 1_000_000
        * 1.20
    )

    vision_cost = (
        vision_input_cost
        + vision_output_cost
    )

    # --------------------------------------------------------
    # 4. TOTAL
    # --------------------------------------------------------

    total_cost = (
        text_cost
        + image_cost
        + vision_cost
    )

    return {
        "text_generation": {
            "input_tokens": text_input_tokens,
            "output_tokens": text_output_tokens,
            "cost_usd": round(
                text_cost,
                6
            )
        },

        "image_generation": {
            "input_tokens": image_input_tokens,
            "output_tokens": image_output_tokens,
            "cost_usd": round(
                image_cost,
                6
            )
        },

        "vision_review": {
            "input_tokens": vision_input_tokens,
            "output_tokens": vision_output_tokens,
            "cost_usd": round(
                vision_cost,
                6
            )
        },

        "total_cost_usd": round(
            total_cost,
            6
        )
    }


# ============================================================
# ПРОМПТ
# ============================================================

def build_dynamic_prompt(
    selected_formats: List[AllowedFormat],
    language: str,
    page_text: str,
    tov: str,
    page_url: str,
    page_keywords: List[str]
):

    normalized_tov = (
        tov.strip()
        or (
            "Современный, дружелюбный, "
            "экспертный и понятный тон. "
            "Без канцелярита и пустых рекламных фраз."
        )
    )

    instructions = [
        f"Язык генерации: {language.upper()}."
    ]
    instructions.append(
        f"URL исходной страницы: {page_url}"
    )

    instructions.append(
        "Ключевые слова SEO: "
        + ", ".join(page_keywords)
    )

    json_schema = {}

    # ========================================================
    # POST
    # ========================================================
    if AllowedFormat.POST in selected_formats:

        instructions.append(
            "Сгенерируй текстовый пост. "
            "Выбери одну тему, веди от проблемы к решению и действию."
        )

        json_schema["post"] = {
            "title": "...",
            "text": "...",
            "visual_idea": "..."
        }

    # ========================================================
    # REELS
    # ========================================================
    if AllowedFormat.REELS in selected_formats:

        instructions.append(
            "Сгенерируй сценарий Reels с сильным hook, "
            "проблемой, решением и CTA. "
            "Timeline должен содержать последовательные целые секунды "
            "от начала ролика: 0, 5, 10, 15, "
            "speaker и подробный visual для каждой сцены. "
            "Не добавляй в visual текст, субтитры или логотипы."
        )

        json_schema["reels"] = {
            "hook": "...",
            "timeline": [
                {
                    "second": 0,
                    "speaker": "...",
                    "visual": "..."
                }
            ]
        }

    # ========================================================
    # CAROUSEL
    # ========================================================
    if AllowedFormat.CAROUSEL in selected_formats:

        instructions.append(
            """
============================================================
ДИНАМИЧЕСКАЯ AI-АРТ-ДИРЕКЦИЯ КАРУСЕЛИ
============================================================

Карусель создаётся НА ОСНОВЕ ИСХОДНОГО ТЕКСТА.

Не используй универсальные темы и сценарии,
которые подходят любой компании.

Не начинай с абстрактных формулировок вроде:

«Как выбрать решение»
«5 ошибок»
«Почему это важно»
«В современном мире»

если конкретная тема не следует из исходного материала.

Сначала проанализируй исходный материал.

Определи:

- компанию;
- продукт или услугу;
- аудиторию;
- конкретную проблему;
- причину проблемы;
- последствия;
- конкретное решение;
- механизм решения;
- реальные характеристики;
- доказательства;
- реальные отличия;
- следующий шаг.

Ничего не придумывай.

Если факта нет в источнике — его нельзя добавлять.

============================================================
ЛОГИКА
============================================================

ТЕМА
→ ПРОБЛЕМА
→ ПОСЛЕДСТВИЕ
→ РЕШЕНИЕ
→ КАК ЭТО РАБОТАЕТ
→ ДОКАЗАТЕЛЬСТВО / ОТЛИЧИЕ / ПОЛЕЗНЫЙ ФАКТ
→ CTA

ОДИН СЛАЙД = ОДНА СИЛЬНАЯ МЫСЛЬ.

Если конкретный этап нельзя подтвердить источником,
используй другую полезную мысль из источника.

Не выдумывай доказательства.

============================================================
ТЕКСТ
============================================================

Слайд 1:

- заголовок 5–8 слов;
- CAPS;
- конкретный;
- напрямую связан с темой;
- вызывает любопытство или напряжение;
- 1–2 коротких предложения текста.

Слайды 2+:

- одна сильная мысль;
- короткий конкретный заголовок;
- 1–2 коротких предложения;
- примерно 120–300 символов основного текста,
  если смысл позволяет.

Не используй искусственные фразы:

«В современном мире»
«Важно понимать»
«Уникальное решение»
«Инновационный подход»
«Это поможет вам»
«Наша команда»
«Мы предлагаем лучшее решение»

если они не следуют из исходного материала.

Не выдумывай:

- цифры;
- цены;
- скидки;
- гарантии;
- отзывы;
- клиентов;
- результаты;
- сертификаты;
- исследования;
- сроки;
- характеристики.

Не используй слово TOV.

============================================================
AI ART DIRECTION
============================================================

Перед созданием слайдов придумай уникальную визуальную
концепцию ИМЕННО ДЛЯ ЭТОЙ КАРУСЕЛИ.

Не существует универсального визуального шаблона.

Создай:

- creative_concept;
- visual_language;
- visual_style;
- visual_epoch;
- color_atmosphere;
- lighting_language;
- material_language;
- continuity_anchor;
- variation_strategy.

Вся карусель должна ощущаться одной визуальной серией.

Но каждый слайд должен иметь самостоятельную композицию.

Не повторяй:

- один и тот же ракурс;
- одну и ту же камеру;
- одинаковое положение объекта;
- одинаковую композицию;
- одинаковый product shot;
- одинаковые декоративные элементы.

Меняй визуальную подачу осмысленно.

============================================================
VISUAL PROMPT
============================================================

Для каждого слайда создай подробный visual_prompt.

Он обязан описывать:

1. главный объект;
2. действие или состояние;
3. окружение;
4. композицию;
5. освещение;
6. цветовую атмосферу;
7. визуальный стиль;
8. свободную область под текст.

Изображение не должно содержать:

- текста;
- надписей;
- логотипов;
- watermark;
- UI;
- подписей;
- случайных символов.

Визуал должен напрямую показывать смысл слайда,
а не быть декоративным фоном.

============================================================
LAYOUT
============================================================

Для каждого слайда самостоятельно определи:

- text_position;
- x;
- y;
- width;
- align;
- title_size;
- body_size;
- line_spacing;
- title_color;
- body_color;
- accent_color;
- accent_words;
- text_panel;
- shadow_opacity.

Поля x, y, width, title_size, body_size, line_spacing,
opacity, radius и padding должны быть ЧИСЛАМИ.

text_panel.enabled должен быть boolean true или false.

Не копируй одинаковые координаты между слайдами.

Layout должен соответствовать конкретной композиции
конкретного слайда.

============================================================
КОНВЕРСИОННОСТЬ
============================================================

Веди читателя:

«Это моя проблема»
→
«Теперь понятна причина»
→
«Вот последствия»
→
«Вот решение»
→
«Вот как оно работает»
→
«Вот конкретное основание обратить внимание»
→
«Вот что делать дальше».

Не превращай карусель в каталог.

Если в источнике несколько продуктов или услуг,
выбери одну конкретную тему.

CTA — одно действие.

Не создавай искусственную срочность.
"""
        )

        json_schema["carousel"] = {
            "title": (
                "Короткая подпись к публикации карусели. "
                "Не пересказывай все слайды."
            ),
            "art_direction": {
                "creative_concept": "Уникальная концепция именно этой карусели.",
                "visual_language": "Общий визуальный язык серии.",
                "visual_style": "Конкретный выбранный стиль.",
                "visual_epoch": "Визуальная эпоха и характер.",
                "color_atmosphere": "Общая цветовая атмосфера.",
                "lighting_language": "Общий характер света.",
                "material_language": "Материалы и фактуры.",
                "continuity_anchor": "Элемент, объединяющий серию.",
                "variation_strategy": "Как меняются ракурсы и композиции."
            },
            "slides": [
    {
        "number": 1,
        "type": "cover",
        "title": "Конкретный заголовок CAPS",
        "text": "Короткий текст",
        "visual_direction": {
            "visual_role": "hero",
            "main_subject": "Главный объект",
            "action_or_state": "Действие или состояние",
            "environment": "Окружение",
            "composition": "Уникальная композиция",
            "camera": "Ракурс и тип кадра",
            "lighting": "Освещение",
            "color_atmosphere": "Цветовая атмосфера",
            "visual_style": "Стиль",
            "text_safe_area": "Область под текст"
        },
        "visual_prompt": "Подробный prompt для генерации изображения",
        "layout": {
            "text_position": "left",
            "x": 0.10,
            "y": 0.10,
            "width": 0.50,
            "align": "left",
            "title_size": 92,
            "body_size": 44,
            "line_spacing": 14,
            "title_color": "#FFFFFF",
            "body_color": "#FFFFFF",
            "accent_color": "#FFFFFF",
            "accent_words": [],
            "text_panel": {
    "enabled": False,
    "color": "#000000",
    "opacity": 0.0,
    "radius": 28,
    "padding": 28
},
"shadow_opacity": 0.0
        }
    }
]
        }

    # ========================================================
    # SYSTEM PROMPT
    # ========================================================

    system_prompt = (
        "Ты — senior content strategist, "
        "direct-response copywriter и AI art director "
        "для социальных сетей.\n\n"

        "Превращай исходный материал в контент "
        "для выбранных форматов.\n\n"

        "Для карусели используй логику: "
        "ТЕМА → ПРОБЛЕМА → ПОСЛЕДСТВИЕ → РЕШЕНИЕ → "
        "МЕХАНИЗМ → ДОКАЗАТЕЛЬСТВО/ОТЛИЧИЕ → CTA.\n\n"

        "Карусель должна быть конкретной именно "
        "для текущего исходного материала.\n\n"

        "Не выдумывай факты, цифры, характеристики, "
        "цены, гарантии, клиентов, отзывы, сертификаты "
        "или результаты.\n\n"

        "Для каждого слайда создавай уникальную "
        "визуальную сцену и layout.\n\n"

        "Не используй универсальный дизайн-шаблон.\n\n"

        "Изображения не должны содержать текста, "
        "логотипов, watermarks или UI.\n\n"

        "Заголовки карусели: 5–8 слов, CAPS, "
        "конкретные и связанные с исходным материалом.\n\n"

        "Никогда не выводи служебные термины, "
        "названия переменных или слово TOV.\n\n"

        "Возвращай только валидный JSON без markdown."
    )

    # ========================================================
    # USER PROMPT
    # ========================================================

    user_prompt = (
        f"ЯЗЫК ГЕНЕРАЦИИ: {language.upper()}\n\n"

        f"TONE OF VOICE (стиль общения бренда):\n"
        f"{normalized_tov}\n\n"

        "ЦЕЛЕВАЯ СХЕМА JSON:\n"
        f"{json.dumps(json_schema, ensure_ascii=False, indent=2)}\n\n"

        "ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ:\n"
        + "\n".join(instructions)
        + "\n\n"

        "ИСХОДНЫЙ МАТЕРИАЛ:\n"
        "----------------------------------------\n"
        f"{page_text}\n"
        "----------------------------------------\n\n"

        "Сформируй контент строго на основе "
        "исходного материала."
    )

    return system_prompt, user_prompt


# ============================================================
# OPENAI TEXT GENERATION
# ============================================================

AI_MAX_ATTEMPTS = 3
AI_RETRY_DELAYS = (2, 8)

# Увеличено только время ожидания ответа от LLM:
# это важно для больших SEO-текстов и тяжёлых JSON-ответов.
AI_TIMEOUT = httpx.Timeout(
    connect=30.0,
    read=900.0,
    write=60.0,
    pool=30.0
)

async def call_ai_llm(
    provider: str,
    system_prompt: str,
    user_prompt: str
) -> dict:
    if (provider or "grok").strip().lower() != "grok":
        raise HTTPException(
            status_code=400,
            detail="Поддерживается только провайдер Grok"
        )

    api_key = os.getenv("XAI_API_KEY", "").strip()
    api_url = "https://api.x.ai/v1/chat/completions"
    model = os.getenv(
        "XAI_TEXT_MODEL",
        "grok-4.7"
    ).strip()
    provider_label = "Grok"

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=(
                f"API key для {provider_label} "
                "не задан в переменных окружения"
            )
        )

    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],
        "response_format": {
            "type": "json_object"
        },
        "max_tokens": 16000
    }

    last_error = None
    response = None

    async with httpx.AsyncClient(
        timeout=AI_TIMEOUT
    ) as client:
        for attempt in range(
            1,
            AI_MAX_ATTEMPTS + 1
        ):
            try:
                response = await client.post(
                    api_url,
                    headers={
                        "Authorization": (
                            f"Bearer {api_key}"
                        ),
                        "Content-Type": (
                            "application/json"
                        )
                    },
                    json=request_payload
                )
                break

            except (
                httpx.TimeoutException,
                httpx.NetworkError
            ) as e:
                last_error = e

                print(
                    f"AI API attempt "
                    f"{attempt}/{AI_MAX_ATTEMPTS} "
                    f"failed: {type(e).__name__}"
                )

                if attempt == AI_MAX_ATTEMPTS:
                    break

                await asyncio.sleep(
                    AI_RETRY_DELAYS[attempt - 1]
                )

    if response is None:
        error_name = (
            type(last_error).__name__
            if last_error
            else "UnknownError"
        )

        error_hint = (
            "Проверьте интернет и доступность API, "
            "уменьшите объём исходного текста или повторите позже. "
            "Подробная инструкция: TROUBLESHOOTING.md"
        )

        raise HTTPException(
            status_code=504,
            detail=(
                f"{provider_label} API не ответил "
                f"за отведённое время после "
                f"{AI_MAX_ATTEMPTS} попыток. "
                f"Тип ошибки: {error_name}. "
                f"{error_hint}"
            )
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Ошибка {provider_label} API: "
                f"{response.text}"
            )
        )

    try:
        result_data = response.json()
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail=(
                f"{provider_label} API вернул "
                "невалидный JSON"
            )
        )

    try:
        choice = result_data["choices"][0]
        message = choice["message"]

    except (
        KeyError,
        IndexError,
        TypeError
    ) as exc:

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    f"Неожиданный ответ "
                    f"{provider_label} API"
                ),
                "response": result_data
            }
        ) from exc

    finish_reason = choice.get(
        "finish_reason"
    )

    content = (
        message.get("content")
        or ""
    ).strip()

    refusal = (
        message.get("refusal")
        or ""
    ).strip()

    print(
        "AI FINISH REASON:",
        finish_reason
    )

    print(
        "AI REFUSAL:",
        refusal or "<none>"
    )

    print(
        "AI CONTENT LENGTH:",
        len(content)
    )

    print(
        "AI CONTENT START:",
        content[:1000]
    )

    # ========================================================
    # REFUSAL
    # ========================================================

    if refusal:
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    f"{provider_label} отказался "
                    "генерировать ответ"
                ),
                "refusal": refusal
            }
        )

    # ========================================================
    # RESPONSE TRUNCATED
    # ========================================================

    if finish_reason == "length":
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "AI не успел завершить JSON-ответ. "
                    "Увеличьте max_tokens."
                ),
                "finish_reason": finish_reason,
                "content_length": len(content)
            }
        )

    # ========================================================
    # CONTENT EMPTY
    # ========================================================

    if not content:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "AI вернул пустой content",
                "finish_reason": finish_reason,
                "message": message,
                "response": result_data
            }
        )

    # ========================================================
    # PARSE JSON
    # ========================================================

    try:
        parsed_content = json.loads(content)

    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "AI вернул некорректный JSON",
                "finish_reason": finish_reason,
                "content": content,
                "content_length": len(content)
            }
        ) from exc

    return {
        "content": parsed_content,
        "usage": result_data.get("usage") or {},
        "model": model,
    }

# ============================================================
# SEO API
# ============================================================

async def get_source_texts() -> dict:
    api_key = os.getenv(
        "ILY_API_KEY"
    )

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "ILY_API_KEY не задан "
                "в переменных окружения"
            )
        )

    api_url = (
        "https://seo.re-spond.com/"
        "api/ilya/texts/norled-lights.ru"
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                api_url,
                headers={
                    "X-API-Key": api_key
                },
                timeout=30.0
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось подключиться "
                f"к SEO API: {str(e)}"
            )
        )

    print("SEO API URL:", api_url)
    print("SEO API STATUS:", response.status_code)
    print(
        "SEO API CONTENT-TYPE:",
        response.headers.get("content-type")
    )
    print(
        "SEO API RESPONSE LENGTH:",
        len(response.content)
    )
    print(
        "SEO API RESPONSE START:",
        response.text[:500]
    )

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="texts_not_ready"
        )

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="Неверный API-ключ"
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "Ключ не имеет доступа "
                "к этому домену"
            )
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Ошибка SEO API",
                "status": response.status_code,
                "response": response.text
            }
        )

    try:
        data = response.json()

        print(
    "SEO API PAGES COUNT:",
    len(data.get("pages", []))
)

        if data.get("pages"):
            first_page = data["pages"][0]

            print(
                "SEO API FIRST PAGE KEYS:",
                list(first_page.keys())
            )

            print(
                "SEO API FIRST PAGE URL:",
                first_page.get("url")
            )

            print(
                "SEO API FIRST PAGE KEYWORDS:",
                first_page.get("keywords")
            )

        return data
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "SEO API вернул "
                    "невалидный JSON"
                ),
                "url": api_url,
                "status": response.status_code,
                "content_type": (
                    response.headers.get("content-type")
                ),
                "response_start": (
                    response.text[:500]
                ),
                "json_error": str(e)
            }
        )


# ============================================================
# THREADS HELPERS
# ============================================================

def get_threads_credentials():
    return _social_credentials("threads")

async def get_threads_credentials_auto_refresh():
    account = _selected_account.get()

    if not account or account["platform"] != "threads":
        raise HTTPException(
            status_code=400,
            detail="Сначала выберите подключённый аккаунт threads"
        )

    try:
        access_token = _token_cipher().decrypt(
            account["access_token_encrypted"].encode()
        ).decode()
    except InvalidToken as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать Threads token"
        ) from exc

    expires_at = account.get("expires_at")

    should_refresh = False

    if expires_at:
        try:
            expires_datetime = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )

            remaining_seconds = (
                expires_datetime - datetime.now(timezone.utc)
            ).total_seconds()

            print(
                "THREADS TOKEN REMAINING:",
                int(remaining_seconds),
                "seconds"
            )

            if remaining_seconds <= 7 * 24 * 60 * 60:
                should_refresh = True

        except (ValueError, TypeError):
            print(
                "THREADS TOKEN EXPIRES_AT: invalid"
            )
            should_refresh = True
    else:
        print(
            "THREADS TOKEN EXPIRES_AT: missing"
        )
        should_refresh = True

    if should_refresh:
        print(
            "THREADS TOKEN REFRESH: starting"
        )

        token_data = await _refresh_threads_token(
            access_token
        )

        new_access_token = token_data.get(
            "access_token"
        )

        if not new_access_token:
            raise HTTPException(
                status_code=502,
                detail="Threads не вернул новый access token"
            )

        new_expires_at = _expires_at(
            token_data.get("expires_in")
        )

        _store_social_account(
            account["user_id"],
            "threads",
            account["account_id"],
            account["account_name"],
            new_access_token,
            expires_at=new_expires_at,
            metadata={
                "login_type": "threads_login",
                "token_type": "long_lived"
            }
        )

        access_token = new_access_token

        print(
            "THREADS TOKEN REFRESH: success"
        )

        print(
            "THREADS TOKEN NEW EXPIRES_AT:",
            new_expires_at
        )

    return account["account_id"], access_token

def validate_public_media_url(
    media_url: str,
    media_label: str
):
    media_url = str(media_url or "").strip()

    if not media_url:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Отсутствует публичный URL для {media_label}"
            )
        )

    if not media_url.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Threads не сможет получить {media_label} "
                f"с URL: {media_url}. Нужен публичный HTTPS URL."
            )
        )


# ============================================================
# THREADS TEXT POST
# ============================================================

async def publish_to_threads(
    content: str
) -> dict:
    user_id, access_token = (
        get_threads_credentials()
    )

    content = str(content or "").strip()

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Текст публикации пуст"
        )

    async with httpx.AsyncClient() as client:
        create_response = await client.post(
            f"https://graph.threads.net/v1.0/"
            f"{user_id}/threads",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },
            data={
                "media_type": "TEXT",
                "text": content[:500]
            },
            timeout=30.0
        )

        if create_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "Ошибка создания "
                        "Threads публикации"
                    ),
                    "status": create_response.status_code,
                    "response": create_response.text
                }
            )

        try:
            creation_id = (
                create_response.json().get("id")
            )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail="Threads API вернул невалидный JSON"
            )

        if not creation_id:
            raise HTTPException(
                status_code=502,
                detail="Threads API не вернул creation_id"
            )

        await asyncio.sleep(2)

        publish_response = await client.post(
            f"https://graph.threads.net/v1.0/"
            f"{user_id}/threads_publish",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },
            data={
                "creation_id": creation_id
            },
            timeout=30.0
        )

        if publish_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка публикации в Threads",
                    "status": publish_response.status_code,
                    "response": publish_response.text
                }
            )

        try:
            post_id = (
                publish_response.json().get("id")
            )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail="Threads API вернул невалидный JSON"
            )

        return {
            "creation_id": creation_id,
            "post_id": post_id
        }


# ============================================================
# THREADS VIDEO
# ============================================================

async def publish_video_to_threads(
    video: Dict[str, Any],
    text: str = "",
    alt_text: str = "Видео"
) -> dict:
    user_id, access_token = (
    await get_threads_credentials_auto_refresh()
)
    print("THREADS USER ID:", user_id)
    print(
        "THREADS TOKEN:",
        "<set>" if access_token else "<missing>"
    )
    print(
        "THREADS TOKEN LENGTH:",
        len(access_token) if access_token else 0
    )
    video_url = str(
        video.get("url", "")
    ).strip()

    validate_public_media_url(
        video_url,
        "видео"
    )

    post_text = str(text or "").strip()[:500]
    alt_text = str(alt_text or "Видео").strip()

    async with httpx.AsyncClient() as client:
        # ----------------------------------------------------
        # 1. CREATE VIDEO CONTAINER
        # ----------------------------------------------------
        data = {
            "media_type": "VIDEO",
            "video_url": video_url,
            "alt_text": alt_text
        }

        if post_text:
            data["text"] = post_text

        create_response = await client.post(
            f"https://graph.threads.net/v1.0/"
            f"{user_id}/threads",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },
            data=data,
            timeout=60.0
        )

        if create_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка создания VIDEO container",
                    "status": create_response.status_code,
                    "video_url": video_url,
                    "response": create_response.text
                }
            )

        try:
            creation_id = (
                create_response.json().get("id")
            )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail="Threads API вернул невалидный JSON"
            )

        if not creation_id:
            raise HTTPException(
                status_code=502,
                detail="Threads API не вернул creation_id"
            )

        # ----------------------------------------------------
        # 2. WAIT FOR PROCESSING
        # ----------------------------------------------------
        max_attempts = 60

        for attempt in range(max_attempts):
            await asyncio.sleep(5)

            status_response = await client.get(
                f"https://graph.threads.net/v1.0/"
                f"{creation_id}",
                headers={
                    "Authorization": (
                        f"Bearer {access_token}"
                    )
                },
                params={
                    "fields": "status,error_message"
                },
                timeout=30.0
            )

            if status_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Ошибка проверки статуса "
                            "Threads video container"
                        ),
                        "status": status_response.status_code,
                        "response": status_response.text
                    }
                )

            try:
                status_data = status_response.json()
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Threads API вернул "
                        "невалидный JSON при polling"
                    )
                )

            container_status = status_data.get("status")
            error_message = status_data.get("error_message")

            print(
                f"THREADS VIDEO STATUS: {container_status} "
                f"({attempt + 1}/{max_attempts})"
            )

            if error_message:
                print(
                    f"THREADS VIDEO ERROR: {error_message}"
                )

            if container_status == "FINISHED":
                break

            if container_status in (
                "ERROR",
                "EXPIRED"
            ):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Threads не смог обработать видео"
                        ),
                        "status": container_status,
                        "error_message": error_message,
                        "response": status_data
                    }
                )
        else:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Threads слишком долго "
                    "обрабатывает видео"
                )
            )

        # ----------------------------------------------------
        # 3. PUBLISH
        # ----------------------------------------------------
        publish_response = await client.post(
            f"https://graph.threads.net/v1.0/"
            f"{user_id}/threads_publish",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },
            data={
                "creation_id": creation_id
            },
            timeout=60.0
        )

        if publish_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка публикации VIDEO в Threads",
                    "status": publish_response.status_code,
                    "creation_id": creation_id,
                    "response": publish_response.text
                }
            )

        try:
            publish_data = publish_response.json()
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail="Threads API вернул невалидный JSON при публикации"
            )

        return {
            "creation_id": creation_id,
            "post_id": publish_data.get("id"),
            "video_url": video_url
        }

async def refresh_threads_tokens_background():
    while True:
        try:
            with _db() as connection:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM social_accounts
                    WHERE platform = ?
                    """,
                    ("threads",)
                ).fetchall()

            for row in rows:
                account = dict(row)

                expires_at = account.get("expires_at")

                if not expires_at:
                    continue

                try:
                    expires_datetime = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    print(
                        "THREADS BACKGROUND REFRESH: "
                        "invalid expires_at for account",
                        account["account_id"]
                    )
                    continue

                remaining_seconds = (
                    expires_datetime - datetime.now(timezone.utc)
                ).total_seconds()

                print(
                    "THREADS BACKGROUND CHECK:",
                    account["account_id"],
                    "remaining:",
                    int(remaining_seconds),
                    "seconds"
                )

                if remaining_seconds > 7 * 24 * 60 * 60:
                    continue

                try:
                    access_token = _token_cipher().decrypt(
                        account["access_token_encrypted"].encode()
                    ).decode()
                except InvalidToken:
                    print(
                        "THREADS BACKGROUND REFRESH: "
                        "failed to decrypt token for",
                        account["account_id"]
                    )
                    continue

                print(
                    "THREADS BACKGROUND REFRESH: starting for",
                    account["account_id"]
                )

                try:
                    token_data = await _refresh_threads_token(
                        access_token
                    )

                    new_access_token = token_data.get(
                        "access_token"
                    )

                    if not new_access_token:
                        print(
                            "THREADS BACKGROUND REFRESH: "
                            "no new token"
                        )
                        continue

                    new_expires_at = _expires_at(
                        token_data.get("expires_in")
                    )

                    _store_social_account(
                        account["user_id"],
                        "threads",
                        account["account_id"],
                        account["account_name"],
                        new_access_token,
                        expires_at=new_expires_at,
                        metadata={
                            "login_type": "threads_login",
                            "token_type": "long_lived"
                        }
                    )

                    print(
                        "THREADS BACKGROUND REFRESH: success"
                    )

                    print(
                        "THREADS BACKGROUND NEW EXPIRES_AT:",
                        new_expires_at
                    )

                except Exception as exc:
                    print(
                        "THREADS BACKGROUND REFRESH ERROR:",
                        repr(exc)
                    )

        except Exception as exc:
            print(
                "THREADS BACKGROUND CHECK ERROR:",
                repr(exc)
            )

        await asyncio.sleep(24 * 60 * 60)

# ============================================================
# THREADS CAROUSEL
# ============================================================

async def publish_carousel_to_threads(
    carousel: Dict[str, Any]
) -> dict:
    user_id, access_token = (
        await get_threads_credentials_auto_refresh()
    )
    print("THREADS USER ID:", user_id)
    print(
        "THREADS TOKEN:",
        "<set>" if access_token else "<missing>"
    )
    print(
        "THREADS TOKEN LENGTH:",
        len(access_token) if access_token else 0
    )
    render = carousel.get("render")

    if not isinstance(render, dict):
        raise HTTPException(
            status_code=400,
            detail="У карусели отсутствует объект render"
        )

    slides = render.get("slides")

    if not isinstance(slides, list):
        raise HTTPException(
            status_code=400,
            detail="У карусели отсутствует массив render.slides"
        )

    slide_count = len(slides)

    if slide_count < 2:
        raise HTTPException(
            status_code=400,
            detail=(
                "Для Threads карусель должна "
                "содержать минимум 2 слайда"
            )
        )

    if slide_count > 20:
        raise HTTPException(
            status_code=400,
            detail=(
                "Threads поддерживает максимум "
                "20 элементов карусели"
            )
        )

    child_container_ids = []

    async with httpx.AsyncClient() as client:
        # ====================================================
        # 1. IMAGE CONTAINER FOR EACH SLIDE
        # ====================================================
        for index, slide in enumerate(slides, start=1):
            if not isinstance(slide, dict):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Слайд {index} "
                        "имеет неправильный формат"
                    )
                )

            image_url = str(
                slide.get("url", "")
            ).strip()

            validate_public_media_url(
                image_url,
                f"изображения слайда {index}"
            )

            alt_text = str(
                slide.get(
                    "title",
                    f"Слайд {index}"
                )
            ).strip()

            response = await client.post(
    f"https://graph.threads.net/v1.0/"
    f"{user_id}/threads",
    headers={
        "Authorization": (
            f"Bearer {access_token}"
        )
    },
    params={
        "media_type": "IMAGE",
        "image_url": image_url,
        "is_carousel_item": "true",
        "alt_text": alt_text
    },
    timeout=60.0
)

            if response.status_code not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            f"Ошибка создания IMAGE container "
                            f"для слайда {index}"
                        ),
                        "status": response.status_code,
                        "image_url": image_url,
                        "response": response.text
                    }
                )

            try:
                response_data = response.json()
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Threads API вернул невалидный JSON "
                        f"при создании слайда {index}"
                    )
                )

            child_id = response_data.get("id")

            if not child_id:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Threads API не вернул ID контейнера "
                        f"для слайда {index}"
                    )
                )

            child_container_ids.append(child_id)

        # ====================================================
        # 2. WAIT
        # ====================================================
        await asyncio.sleep(3)

        # ====================================================
        # 3. CREATE CAROUSEL CONTAINER
        # ====================================================
        carousel_text = str(
            carousel.get("title", "")
        ).strip()

        carousel_data = {
            "media_type": "CAROUSEL",
            "children": ",".join(
                child_container_ids
            )
        }

        if carousel_text:
            carousel_data["text"] = carousel_text[:500]

        carousel_response = await client.post(
    f"https://graph.threads.net/v1.0/"
    f"{user_id}/threads",
    headers={
        "Authorization": (
            f"Bearer {access_token}"
        )
    },
    params=carousel_data,
    timeout=60.0
)

        if carousel_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка создания CAROUSEL container",
                    "status": carousel_response.status_code,
                    "children": child_container_ids,
                    "response": carousel_response.text
                }
            )

        try:
            carousel_response_data = (
                carousel_response.json()
            )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Threads API вернул невалидный JSON "
                    "при создании CAROUSEL"
                )
            )

        carousel_creation_id = (
            carousel_response_data.get("id")
        )

        if not carousel_creation_id:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Threads API не вернул creation_id карусели"
                )
            )

        # ====================================================
        # 4. PUBLISH CAROUSEL
        # ====================================================
        await asyncio.sleep(2)

        publish_response = await client.post(
    f"https://graph.threads.net/v1.0/"
    f"{user_id}/threads_publish",
    headers={
        "Authorization": (
            f"Bearer {access_token}"
        )
    },
    params={
        "creation_id": carousel_creation_id
    },
    timeout=60.0
)

        if publish_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка публикации карусели в Threads",
                    "status": publish_response.status_code,
                    "creation_id": carousel_creation_id,
                    "response": publish_response.text
                }
            )

        try:
            publish_data = publish_response.json()
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Threads API вернул невалидный JSON "
                    "при публикации"
                )
            )

        post_id = publish_data.get("id")

        return {
            "creation_id": carousel_creation_id,
            "post_id": post_id,
            "child_container_ids": child_container_ids,
            "slides_count": slide_count
        }

# ============================================================
# INSTAGRAM HELPERS
# ============================================================

def get_instagram_credentials():
    return _social_credentials("instagram")

# ============================================================
# INSTAGRAM IMAGE POST
# ============================================================

async def publish_image_to_instagram(
    image_url: str,
    caption: str = ""
) -> dict:

    user_id, access_token = get_instagram_credentials()

    image_url = str(image_url or "").strip()

    if not image_url:
        raise HTTPException(
            status_code=400,
            detail="Отсутствует URL изображения для Instagram"
        )

    validate_public_media_url(
        image_url,
        "изображения Instagram"
    )

    caption = str(caption or "").strip()

    async with httpx.AsyncClient() as client:

        # ====================================================
        # 1. CREATE MEDIA CONTAINER
        # ====================================================

        create_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media",

            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },

            data={
                "image_url": image_url,
                "caption": caption
            },

            timeout=60.0
        )

        if create_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "Ошибка создания "
                        "Instagram media container"
                    ),
                    "status": create_response.status_code,
                    "response": create_response.text
                }
            )

        try:
            create_data = create_response.json()
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram API вернул "
                    "невалидный JSON"
                )
            )

        creation_id = create_data.get("id")

        if not creation_id:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram API не вернул "
                    "creation_id"
                )
            )

        # ====================================================
        # 2. PUBLISH
        # ====================================================

        publish_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media_publish",

            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },

            data={
                "creation_id": creation_id
            },

            timeout=60.0
        )

        if publish_response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "Ошибка публикации "
                        "изображения в Instagram"
                    ),
                    "status": publish_response.status_code,
                    "creation_id": creation_id,
                    "response": publish_response.text
                }
            )

        try:
            publish_data = publish_response.json()
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram API вернул "
                    "невалидный JSON при публикации"
                )
            )

        return {
            "creation_id": creation_id,
            "media_id": publish_data.get("id"),
            "image_url": image_url
        }

# ============================================================
# INSTAGRAM CAROUSEL
# ============================================================

async def publish_carousel_to_instagram(
    carousel: dict
) -> dict:

    user_id, access_token = get_instagram_credentials()

    slides = (
        carousel
        .get("render", {})
        .get("slides", [])
    )

    if not slides:
        raise HTTPException(
            status_code=400,
            detail="В карусели Instagram нет слайдов"
        )

    if len(slides) > 10:
        raise HTTPException(
            status_code=400,
            detail="Instagram поддерживает максимум 10 слайдов"
        )

    caption = str(
        carousel.get("caption", "")
        or carousel.get("text", "")
        or ""
    ).strip()

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    async with httpx.AsyncClient() as client:

        child_ids = []

        # ====================================================
        # 1. СОЗДАЁМ CONTAINER ДЛЯ КАЖДОГО СЛАЙДА
        # ====================================================

        for index, slide in enumerate(slides):

            image_url = str(
                slide.get("url", "")
            ).strip()

            if not image_url:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"У слайда {index + 1} "
                        "отсутствует URL изображения"
                    )
                )

            validate_public_media_url(
                image_url,
                f"слайда Instagram #{index + 1}"
            )

            response = await client.post(
                f"https://graph.instagram.com/v23.0/"
                f"{user_id}/media",
                headers=headers,
                data={
                    "image_url": image_url,
                    "is_carousel_item": "true"
                },
                timeout=60.0
            )

            if response.status_code not in (200, 201):

                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Ошибка создания "
                            f"Instagram container "
                            f"для слайда #{index + 1}"
                        ),
                        "status": response.status_code,
                        "response": response.text
                    }
                )

            try:
                data = response.json()
            except json.JSONDecodeError:

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Instagram API вернул "
                        "невалидный JSON"
                    )
                )

            creation_id = data.get("id")

            if not creation_id:

                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Instagram не вернул "
                        f"creation_id для слайда "
                        f"#{index + 1}"
                    )
                )

            child_ids.append(creation_id)

        # ====================================================
        # 2. СОЗДАЁМ CAROUSEL CONTAINER
        # ====================================================

        carousel_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media",
            headers=headers,
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption
            },
            timeout=60.0
        )

        if carousel_response.status_code not in (200, 201):

            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "Ошибка создания "
                        "Instagram carousel"
                    ),
                    "status": (
                        carousel_response.status_code
                    ),
                    "response": (
                        carousel_response.text
                    )
                }
            )

        try:
            carousel_data = carousel_response.json()
        except json.JSONDecodeError:

            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram API вернул "
                    "невалидный JSON "
                    "для carousel"
                )
            )

        carousel_id = carousel_data.get("id")

        if not carousel_id:

            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram не вернул "
                    "carousel creation_id"
                )
            )

        # ====================================================
        # 3. ЖДЁМ ГОТОВНОСТИ CAROUSEL
        # ====================================================

        max_attempts = 12
        wait_seconds = 2

        carousel_status = None

        for attempt in range(max_attempts):

            status_response = await client.get(
                f"https://graph.instagram.com/v23.0/"
                f"{carousel_id}",
                headers=headers,
                params={
                    "fields": "status_code"
                },
                timeout=30.0
            )

            if status_response.status_code != 200:

                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Ошибка проверки статуса "
                            "Instagram carousel"
                        ),
                        "status": (
                            status_response.status_code
                        ),
                        "response": (
                            status_response.text
                        )
                    }
                )

            try:
                status_data = status_response.json()
            except json.JSONDecodeError:

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Instagram API вернул "
                        "невалидный JSON "
                        "при проверке статуса carousel"
                    )
                )

            carousel_status = status_data.get(
                "status_code"
            )

            print(
                f"Instagram carousel "
                f"{carousel_id}: "
                f"status={carousel_status}, "
                f"attempt={attempt + 1}/{max_attempts}"
            )

            # Готово
            if carousel_status == "FINISHED":
                break

            # Instagram сообщил об ошибке
            if carousel_status == "ERROR":

                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Instagram не смог "
                            "подготовить carousel"
                        ),
                        "carousel_id": carousel_id,
                        "status_code": carousel_status
                    }
                )

            # Ещё обрабатывается
            await asyncio.sleep(wait_seconds)

        else:

            raise HTTPException(
                status_code=504,
                detail={
                    "error": (
                        "Instagram слишком долго "
                        "подготавливает carousel"
                    ),
                    "carousel_id": carousel_id,
                    "status_code": carousel_status
                }
            )

        # ====================================================
        # 4. ПУБЛИКУЕМ CAROUSEL
        # ====================================================

        publish_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media_publish",
            headers=headers,
            data={
                "creation_id": carousel_id
            },
            timeout=60.0
        )

        if publish_response.status_code not in (200, 201):

            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "Ошибка публикации "
                        "Instagram carousel"
                    ),
                    "status": (
                        publish_response.status_code
                    ),
                    "creation_id": carousel_id,
                    "response": (
                        publish_response.text
                    )
                }
            )

        try:
            publish_data = publish_response.json()
        except json.JSONDecodeError:

            raise HTTPException(
                status_code=502,
                detail=(
                    "Instagram API вернул "
                    "невалидный JSON "
                    "при публикации"
                )
            )

        return {
            "success": True,
            "carousel_creation_id": carousel_id,
            "media_id": publish_data.get("id"),
            "children": child_ids,
            "status": carousel_status
        }

# ============================================================
# FACEBOOK
# ============================================================

async def publish_to_facebook(
    text: str
) -> dict:
    page_id, page_token = _social_credentials("facebook")

    text = str(text or "").strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail="Текст публикации в Facebook пустой"
        )

    async with httpx.AsyncClient() as client:

        response = await client.post(
            f"https://graph.facebook.com/v26.0/"
            f"{page_id}/feed",
            headers={
                "Authorization": f"Bearer {page_token}"
            },
            data={
                "message": text
            },
            timeout=60.0
        )

        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Ошибка публикации в Facebook",
                    "status": response.status_code,
                    "response": response.text
                }
            )

        try:
            return response.json()

        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Facebook API вернул "
                    "невалидный JSON"
                )
            )

async def publish_video_to_facebook(
    video_path: Path,
    text: str = ""
) -> dict:

    page_id, page_token = _social_credentials("facebook")

    if not video_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Файл видео не найден: {video_path}"
        )

    if video_path.suffix.lower() != ".mp4":
        raise HTTPException(
            status_code=400,
            detail="Facebook ожидает MP4-видео"
        )

    video_size = video_path.stat().st_size

    if video_size <= 0:
        raise HTTPException(
            status_code=400,
            detail="Файл видео пуст"
        )

    text = str(text or "").strip()

    # ========================================================
    # FACEBOOK VIDEO UPLOAD
    # ========================================================

    async with httpx.AsyncClient(
        timeout=300.0
    ) as client:

        with video_path.open("rb") as video_file:

            response = await client.post(
                f"https://graph.facebook.com/v26.0/"
                f"{page_id}/videos",
                headers={
                    "Authorization": (
                        f"Bearer {page_token}"
                    )
                },
                data={
                    "description": text
                },
                files={
                    "source": (
                        video_path.name,
                        video_file,
                        "video/mp4"
                    )
                }
            )

    print(
        "FACEBOOK VIDEO STATUS:",
        response.status_code
    )

    print(
        "FACEBOOK VIDEO RESPONSE:",
        response.text
    )

    if response.status_code not in (200, 201):

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "Facebook не смог "
                    "загрузить видео"
                ),
                "status": response.status_code,
                "response": response.text
            }
        )

    try:
        result = response.json()

    except json.JSONDecodeError as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Facebook API вернул "
                "невалидный JSON"
            )
        ) from exc

    return result
# ============================================================
# SOURCE TEXTS
# ============================================================

@app.post("/api/v1/auth/register")
async def register(payload: CredentialsRequest):
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Укажите корректный email")
    try:
        with _db() as connection:
            cursor = connection.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, _password_hash(payload.password), _now())
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Пользователь уже зарегистрирован") from exc
    token = secrets.token_urlsafe(32)
    with _db() as connection:
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
            (_session_hash(token), user_id, _now())
        )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user_id,
        "email": email
    }


@app.post("/api/v1/auth/login")
async def login(payload: CredentialsRequest):
    with _db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (payload.email.strip().lower(),)
        ).fetchone()
    if not user or not _password_matches(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    token = secrets.token_urlsafe(32)
    with _db() as connection:
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
            (_session_hash(token), user["id"], _now())
        )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user["id"],
        "email": user["email"]
    }


@app.post("/api/v1/auth/logout")
async def logout(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Требуется Bearer-сессия RespondService")
    token = authorization[7:].strip()
    with _db() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (_session_hash(token),)
        )
    return {"status": "logged_out"}


@app.get("/api/v1/social-accounts")
async def social_accounts(authorization: str | None = Header(default=None)):
    user = _user_from_authorization(authorization)
    with _db() as connection:
        rows = connection.execute(
            "SELECT * FROM social_accounts WHERE user_id = ? ORDER BY platform, account_name",
            (user["id"],)
        ).fetchall()
    return {"accounts": [_account_view(row) for row in rows]}


@app.delete("/api/v1/social-accounts/{account_id}")
async def disconnect_social_account(
    account_id: int,
    authorization: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)
    with _db() as connection:
        result = connection.execute(
            "DELETE FROM social_accounts WHERE id = ? AND user_id = ?",
            (account_id, user["id"])
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Социальный аккаунт не найден")
    return {"status": "disconnected", "account_id": account_id}


@app.post("/api/v1/social-accounts/manual")
async def add_social_account(
    payload: SocialAccountRequest,
    authorization: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)
    platform = payload.platform.strip().lower()
    if platform not in {"instagram", "facebook", "threads"}:
        raise HTTPException(status_code=400, detail="Поддерживаются Instagram, Facebook и Threads")
    return _store_social_account(
        user["id"], platform, payload.account_id.strip(), payload.account_name.strip(),
        payload.access_token, payload.expires_at, payload.metadata
    )


def _oauth_redirect_uri() -> str:
    redirect_uri = os.getenv("META_OAUTH_REDIRECT_URI", "").strip()
    if redirect_uri:
        return redirect_uri
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL не настроен")
    return f"{public_base_url}/api/v1/oauth/meta/callback"

def _x_redirect_uri() -> str:
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL не настроен"
        )

    return f"{public_base_url}/api/v1/oauth/x/callback"

def _tiktok_redirect_uri() -> str:
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "").strip()
    if redirect_uri:
        return redirect_uri

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL не настроен"
        )

    return f"{public_base_url}/api/v1/oauth/tiktok/callback"

def _frontend_url(result: str, frontend_url: str | None = None) -> str:
    frontend_url = (frontend_url or os.getenv("FRONTEND_URL", "")).strip()
    if not frontend_url:
        raise HTTPException(status_code=500, detail="FRONTEND_URL не настроен")
    separator = "&" if "?" in frontend_url else "?"
    return f"{frontend_url}{separator}oauth={result}"


def _expires_at(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

# ============================================================
# YOUTUBE REFRESH TOKEN
# ============================================================

async def _refresh_youtube_access_token(
    account: dict
) -> str:

    encrypted_refresh_token = account.get(
        "refresh_token_encrypted"
    )

    if not encrypted_refresh_token:
        raise HTTPException(
            status_code=401,
            detail=(
                "У YouTube аккаунта отсутствует "
                "refresh token. "
                "Необходимо заново подключить YouTube."
            )
        )

    try:
        refresh_token = (
            _token_cipher()
            .decrypt(
                encrypted_refresh_token.encode()
            )
            .decode()
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось расшифровать "
                "YouTube refresh token"
            )
        ) from exc

    client_id, client_secret = (
        _youtube_credentials()
    )

    async with httpx.AsyncClient() as client:

        response = await client.post(
            "https://oauth2.googleapis.com/token",

            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded"
            },

            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            },

            timeout=30.0
        )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=401,
            detail={
                "error":
                    "YouTube access token не удалось обновить",
                "status":
                    response.status_code,
                "response":
                    response.text
            }
        )

    token_data = response.json()

    new_access_token = str(
        token_data.get(
            "access_token",
            ""
        )
    ).strip()

    if not new_access_token:

        raise HTTPException(
            status_code=401,
            detail=(
                "Google не вернул новый "
                "YouTube access token"
            )
        )

    expires_at = _expires_at(
        token_data.get(
            "expires_in"
        )
    )

    encrypted_access_token = (
        _token_cipher()
        .encrypt(
            new_access_token.encode()
        )
        .decode()
    )

    with _db() as connection:

        connection.execute(
            """
            UPDATE social_accounts

            SET
                access_token_encrypted = ?,
                expires_at = ?,
                updated_at = ?

            WHERE id = ?
            """,

            (
                encrypted_access_token,
                expires_at,
                _now(),
                account["id"]
            )
        )

    return new_access_token


def _oauth_config(platform: str) -> tuple[str, str, str]:
    app_id, app_secret = _oauth_credentials(platform)
    if not app_id or not app_secret:
        raise HTTPException(status_code=503, detail=f"OAuth credentials для {platform} ещё не настроены")

    configs = {
        "instagram": (
            "https://www.instagram.com/oauth/authorize",
            "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_messages,instagram_business_manage_comments",
            "instagram"
        ),
        "facebook": (
            "https://www.facebook.com/v26.0/dialog/oauth",
            "pages_show_list,pages_read_engagement,pages_manage_posts",
            "facebook"
        ),
        "threads": (
            "https://threads.net/oauth/authorize",
            "threads_basic,threads_content_publish",
            "threads"
        ),
                "x": (
            "https://x.com/i/oauth2/authorize",
            "tweet.read tweet.write users.read offline.access",
            "x"
        )
    }
    try:
        return configs[platform]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="Неизвестная Meta-платформа") from exc


def _oauth_credentials(platform: str) -> tuple[str, str]:
    prefix = platform.upper()
    return (
        os.getenv(f"{prefix}_APP_ID", "").strip()
        or os.getenv("META_APP_ID", "").strip(),
        os.getenv(f"{prefix}_APP_SECRET", "").strip()
        or os.getenv("META_APP_SECRET", "").strip()
    )


def _tiktok_credentials() -> tuple[str, str]:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()

    if not client_key or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="TikTok OAuth credentials ещё не настроены"
        )

    return client_key, client_secret

def _linkedin_credentials() -> tuple[str, str]:
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="LinkedIn OAuth credentials ещё не настроены"
        )

    return client_id, client_secret


def _linkedin_redirect_uri() -> str:
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI", "").strip()

    if redirect_uri:
        return redirect_uri

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL не настроен"
        )

    return f"{public_base_url}/api/v1/oauth/linkedin/callback"


# ========================================================
# LINKEDIN — ПУБЛИКАЦИЯ ТЕКСТОВОГО ПОСТА
# ========================================================

# ============================================================
# LINKEDIN VIDEO UPLOAD
# ============================================================

async def upload_video_to_linkedin(
    video_path: Path,
    access_token: str,
    author_urn: str
) -> str:

    if not video_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Файл видео не найден: {video_path}"
        )

    if video_path.suffix.lower() != ".mp4":
        raise HTTPException(
            status_code=400,
            detail="LinkedIn ожидает MP4-видео"
        )

    video_size = video_path.stat().st_size

    if video_size <= 0:
        raise HTTPException(
            status_code=400,
            detail="Файл видео пуст"
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": "202609",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json"
    }

    # --------------------------------------------------------
    # 1. INITIALIZE UPLOAD
    # --------------------------------------------------------

    async with httpx.AsyncClient(timeout=60.0) as client:

        response = await client.post(
            "https://api.linkedin.com/rest/videos?action=initializeUpload",
            headers=headers,
            json={
                "initializeUploadRequest": {
                    "owner": author_urn,
                    "fileSizeBytes": video_size,
                    "uploadCaptions": False,
                    "uploadThumbnail": False
                }
            }
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=(
                "LinkedIn video initializeUpload error: "
                f"{response.text}"
            )
        )

    data = response.json()

    value = data.get("value") or {}

    video_urn = value.get("video")

    upload_instructions = (
        value.get("uploadInstructions")
        or []
    )

    upload_token = value.get("uploadToken", "")

    if not video_urn:
        raise HTTPException(
            status_code=502,
            detail="LinkedIn не вернул video URN"
        )

    if not upload_instructions:
        raise HTTPException(
            status_code=502,
            detail="LinkedIn не вернул upload instructions"
        )

    # --------------------------------------------------------
    # 2. UPLOAD VIDEO PARTS
    # --------------------------------------------------------

    with video_path.open("rb") as video_file:
        video_bytes = video_file.read()

    uploaded_part_ids = []

    async with httpx.AsyncClient(timeout=300.0) as client:

        for instruction in upload_instructions:

            upload_url = instruction.get("uploadUrl")

            if not upload_url:
                raise HTTPException(
                    status_code=502,
                    detail="LinkedIn не вернул uploadUrl"
                )

            first_byte = int(
                instruction.get("firstByte", 0)
            )

            last_byte = int(
                instruction.get("lastByte", video_size - 1)
            )

            part = video_bytes[
                first_byte:last_byte + 1
            ]

            upload_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(part)),
                "Content-Range": (
                    f"bytes {first_byte}-"
                    f"{last_byte}/{video_size}"
                )
            }

            response = await client.put(
                upload_url,
                headers=upload_headers,
                content=part
            )

            if response.status_code not in (
                200,
                201,
                202
            ):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "LinkedIn не принял видео",
                        "status": response.status_code,
                        "response": response.text
                    }
                )

            etag = (
                response.headers.get("ETag")
                or response.headers.get("etag")
            )

            if etag:
                uploaded_part_ids.append(
                    etag.strip('"')
                )

    # --------------------------------------------------------
    # 3. FINALIZE UPLOAD
    # --------------------------------------------------------

    finalize_payload = {
        "finalizeUploadRequest": {
            "video": video_urn,
            "uploadToken": upload_token,
            "uploadedPartIds": uploaded_part_ids
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:

        response = await client.post(
            "https://api.linkedin.com/rest/videos?action=finalizeUpload",
            headers=headers,
            json=finalize_payload
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "error": "LinkedIn не смог завершить загрузку видео",
                "status": response.status_code,
                "response": response.text
            }
        )

    return video_urn

@app.post("/api/v1/publish-linkedin")
async def publish_linkedin(
    payload: PublishRequest,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(default=None, alias="X-Account-Id")
):
    # ========================================================
    # 1. ВЫБИРАЕМ LINKEDIN АККАУНТ
    # ========================================================

    _prepare_publish_account(
        authorization,
        account_header,
        "linkedin"
    )

    # ========================================================
    # 2. ПОЛУЧАЕМ ACCOUNT ID И ACCESS TOKEN
    # ========================================================

    account_id, access_token = _social_credentials("linkedin")

    content = payload.content

    commentary = ""
    is_reels = False

    # ========================================================
    # 3. ОПРЕДЕЛЯЕМ ТЕКСТ И ТИП КОНТЕНТА
    # ========================================================

    if isinstance(content, str):

        commentary = content.strip()

    elif isinstance(content, dict):

        # ====================================================
        # REELS / VIDEO
        # ====================================================

        reels = content.get("reels")

        if isinstance(reels, dict):

            is_reels = True

            commentary = str(
                reels.get("hook")
                or reels.get("caption")
                or reels.get("text")
                or reels.get("description")
                or ""
            ).strip()

        # ====================================================
        # CAROUSEL
        # ====================================================

        if not commentary:

            carousel = content.get("carousel")

            if isinstance(carousel, dict):

                commentary = str(
                    carousel.get("caption")
                    or carousel.get("text")
                    or carousel.get("description")
                    or ""
                ).strip()

                # Если caption нет —
                # собираем текст слайдов

                if not commentary:

                    slides = carousel.get("slides")

                    if isinstance(slides, list):

                        slide_texts = []

                        for slide in slides:

                            if not isinstance(slide, dict):
                                continue

                            slide_text = (
                                slide.get("text")
                                or slide.get("body")
                                or slide.get("content")
                                or slide.get("title")
                                or ""
                            )

                            slide_text = str(
                                slide_text
                            ).strip()

                            if slide_text:
                                slide_texts.append(
                                    slide_text
                                )

                        commentary = "\n\n".join(
                            slide_texts
                        )

        # ====================================================
        # ОБЩИЕ ПОЛЯ
        # ====================================================

        if not commentary:

            commentary = str(
                content.get("text")
                or content.get("caption")
                or content.get("post")
                or content.get("content")
                or ""
            ).strip()

    else:

        commentary = ""

    # ========================================================
    # 4. ПРОВЕРЯЕМ ТЕКСТ
    # ========================================================

    if not commentary:

        raise HTTPException(
            status_code=400,
            detail="Не найден текст для публикации в LinkedIn"
        )

    # ========================================================
    # 5. LINKEDIN PERSON URN
    # ========================================================

    author_urn = f"urn:li:person:{account_id}"

    # ========================================================
    # 6. ЕСЛИ REELS — ИЩЕМ ПОСЛЕДНИЙ MP4
    # ========================================================

    video_urn = None

    if is_reels:

        videos_dir = GENERATED_DIR / "videos"

        if not videos_dir.exists():

            raise HTTPException(
                status_code=404,
                detail=(
                    "Папка с сгенерированными "
                    "видео не найдена"
                )
            )

        video_files = [
            path
            for path in videos_dir.rglob("*.mp4")
            if path.is_file()
        ]

        if not video_files:

            raise HTTPException(
                status_code=404,
                detail=(
                    "В generated/videos "
                    "нет готовых MP4-видео"
                )
            )

        # Берём САМОЕ ПОСЛЕДНЕЕ видео
        latest_video_file = max(
            video_files,
            key=lambda path: path.stat().st_mtime
        )

        print(
            "LINKEDIN: используется последнее видео:",
            latest_video_file
        )

        # ====================================================
        # 7. ЗАГРУЖАЕМ ВИДЕО В LINKEDIN
        # ====================================================

        video_urn = await upload_video_to_linkedin(
            video_path=latest_video_file,
            access_token=access_token,
            author_urn=author_urn
        )

        print(
            "LINKEDIN VIDEO URN:",
            video_urn
        )

    # ========================================================
    # 8. СОЗДАЁМ POST
    # ========================================================

    post_data = {
        "author": author_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    # ========================================================
    # 9. ЕСЛИ ЭТО REELS — ПРИКРЕПЛЯЕМ VIDEO
    # ========================================================

    if video_urn:

        post_data["content"] = {
            "media": {
                "id": video_urn
            }
        }

    # ========================================================
    # 10. ПУБЛИКУЕМ В LINKEDIN
    # ========================================================

    async with httpx.AsyncClient(timeout=60.0) as client:

        response = await client.post(
            "https://api.linkedin.com/rest/posts",

            headers={
                "Authorization": f"Bearer {access_token}",
                "Linkedin-Version": "202609",
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json"
            },

            json=post_data
        )

    # ========================================================
    # 11. ОБРАБОТКА ОШИБКИ
    # ========================================================

    if response.status_code >= 400:

        raise HTTPException(
            status_code=response.status_code,
            detail=(
                "LinkedIn API error: "
                f"{response.text}"
            )
        )

    # ========================================================
    # 12. ОТВЕТ
    # ========================================================

    return {
        "status": "success",
        "platform": "linkedin",
        "type": "video" if video_urn else "text",
        "post_id": response.headers.get(
            "x-restli-id"
        ),
        "video_urn": video_urn
    }

# ========================================================
# YOUTUBE OAUTH
# ========================================================

def _youtube_credentials() -> tuple[str, str]:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="YouTube OAuth credentials ещё не настроены"
        )

    return client_id, client_secret


def _youtube_redirect_uri() -> str:
    redirect_uri = os.getenv("YOUTUBE_REDIRECT_URI", "").strip()

    if redirect_uri:
        return redirect_uri

    public_base_url = (
        os.getenv("PUBLIC_BASE_URL", "")
        .strip()
        .rstrip("/")
    )

    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL не настроен"
        )

    return (
        f"{public_base_url}"
        "/api/v1/oauth/youtube/callback"
    )

@app.post("/api/v1/oauth/linkedin/start")
async def start_linkedin_oauth(
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)

    client_id, _ = _linkedin_credentials()

    configured_frontend = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
    requested_frontend = (origin or "").strip().rstrip("/")

    allowed_local_origins = {
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    }

    if (
        requested_frontend not in allowed_local_origins
        and requested_frontend != configured_frontend
    ):
        requested_frontend = configured_frontend

    state = secrets.token_urlsafe(32)

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO oauth_states (
                state,
                user_id,
                platform,
                created_at,
                frontend_url
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state,
                user["id"],
                "linkedin",
                _now(),
                requested_frontend
            )
        )

    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _linkedin_redirect_uri(),
        "state": state,
        "scope": "openid profile email w_member_social"
    })

    authorization_url = (
        f"https://www.linkedin.com/oauth/v2/authorization?{query}"
    )

    print("LINKEDIN OAUTH URL:", authorization_url)

    return {
        "authorization_url": authorization_url
    }

async def _exchange_linkedin_code(code: str) -> dict:
    client_id, client_secret = _linkedin_credentials()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://www.linkedin.com/oauth/v2/accessToken",
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _linkedin_redirect_uri()
            },
            timeout=30.0
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"LinkedIn token exchange failed: {response.text}"
        )

    return response.json()

async def _exchange_youtube_code(code: str) -> dict:
    client_id, client_secret = _youtube_credentials()

    async with httpx.AsyncClient() as client:

        response = await client.post(
            "https://oauth2.googleapis.com/token",

            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },

            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _youtube_redirect_uri(),
                "grant_type": "authorization_code"
            },

            timeout=30.0
        )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube token exchange failed: "
                f"{response.text}"
            )
        )

    return response.json()


async def _get_youtube_channel(
    access_token: str
) -> dict:

    async with httpx.AsyncClient() as client:

        response = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",

            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },

            params={
                "part": "snippet",
                "mine": "true"
            },

            timeout=30.0
        )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube channel request failed: "
                f"{response.text}"
            )
        )

    data = response.json()

    items = data.get("items") or []

    if not items:

        raise HTTPException(
            status_code=502,
            detail="YouTube не вернул канал пользователя"
        )

    return items[0]

async def _get_linkedin_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=30.0
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"LinkedIn userinfo failed: {response.text}"
        )

    return response.json()

@app.post("/api/v1/oauth/meta/{platform}/start")
async def start_meta_oauth(
    platform: str,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)
    platform = platform.lower()
    authorize_url, scopes, _ = _oauth_config(platform)
    configured_frontend = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
    requested_frontend = (origin or "").strip().rstrip("/")
    allowed_local_origins = {
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    }
    if requested_frontend not in allowed_local_origins and requested_frontend != configured_frontend:
        requested_frontend = configured_frontend
    state = secrets.token_urlsafe(32)
    with _db() as connection:
        connection.execute(
            "INSERT INTO oauth_states (state, user_id, platform, created_at, frontend_url) VALUES (?, ?, ?, ?, ?)",
            (state, user["id"], platform, _now(), requested_frontend)
        )
    app_id, _ = _oauth_credentials(platform)
    query = urlencode({
        "client_id": app_id,
        "redirect_uri": _oauth_redirect_uri(),
        "state": state,
        "scope": scopes,
        "response_type": "code"
    })
    print("OAUTH URL:", f"{authorize_url}?{query}")
    return {"authorization_url": f"{authorize_url}?{query}"}

@app.post("/api/v1/oauth/tiktok/start")
async def start_tiktok_oauth(
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)

    client_key, _ = _tiktok_credentials()

    configured_frontend = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
    requested_frontend = (origin or "").strip().rstrip("/")

    allowed_local_origins = {
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    }

    if (
        requested_frontend not in allowed_local_origins
        and requested_frontend != configured_frontend
    ):
        requested_frontend = configured_frontend

    state = secrets.token_urlsafe(32)

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO oauth_states (
                state,
                user_id,
                platform,
                created_at,
                frontend_url
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state,
                user["id"],
                "tiktok",
                _now(),
                requested_frontend
            )
        )

    query = urlencode({
        "client_key": client_key,
        "scope": "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri": _tiktok_redirect_uri(),
        "state": state
    })

    authorization_url = (
        f"https://www.tiktok.com/v2/auth/authorize/?{query}"
    )

    debug_data = {
    "client_key_length": len(client_key),
    "client_key_prefix": client_key[:4],
    "redirect_uri": _tiktok_redirect_uri()
}

    print("TIKTOK OAUTH DEBUG:", debug_data)

    with open("tiktok_debug.txt", "a", encoding="utf-8") as f:
        f.write(str(debug_data) + "\n")

    return {
        "authorization_url": authorization_url
    }

async def _exchange_instagram_code(code: str) -> dict:
    app_id, app_secret = _oauth_credentials("instagram")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": _oauth_redirect_uri(),
                "code": code
            },
            timeout=30.0
        )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail={"error": "Instagram не выдал access token", "response": response.text})
        token_data = response.json()
        access_token = token_data.get("access_token")
        user_id = token_data.get("user_id")
        if not access_token or not user_id:
            raise HTTPException(status_code=502, detail="Instagram не вернул access_token или user_id")

        long_response = await client.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": access_token
            },
            timeout=30.0
        )
        if long_response.status_code == 200:
            long_data = long_response.json()
            access_token = long_data.get("access_token", access_token)
            token_data.update(long_data)
        return token_data | {"access_token": access_token}

async def _exchange_threads_token(
    access_token: str
) -> dict:
    app_id, app_secret = _oauth_credentials("threads")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": access_token
            },
            timeout=30.0
        )

        print(
            "THREADS LONG TOKEN STATUS:",
            response.status_code
        )
        print(
            "THREADS LONG TOKEN RESPONSE:",
            response.text
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Threads не смог обменять token",
                    "status": response.status_code,
                    "response": response.text
                }
            )

        token_data = response.json()

        new_access_token = token_data.get("access_token")

        if not new_access_token:
            raise HTTPException(
                status_code=502,
                detail="Threads не вернул новый access token"
            )

        return token_data | {
            "access_token": new_access_token
        }

async def _exchange_tiktok_code(code: str) -> dict:
    client_key, client_secret = _tiktok_credentials()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _tiktok_redirect_uri()
            },
            timeout=30.0
        )

    print(
        "TIKTOK TOKEN STATUS:",
        response.status_code
    )

    if response.status_code != 200:
        print(
            "TIKTOK TOKEN RESPONSE:",
            response.text
        )

        raise HTTPException(
            status_code=502,
            detail={
                "error": "TikTok не выдал access token",
                "status": response.status_code,
                "response": response.text
            }
        )

    try:
        token_data = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="TikTok вернул невалидный JSON"
        ) from exc

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    open_id = token_data.get("open_id")

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="TikTok не вернул access_token"
        )

    if not refresh_token:
        raise HTTPException(
            status_code=502,
            detail="TikTok не вернул refresh_token"
        )

    if not open_id:
        raise HTTPException(
            status_code=502,
            detail="TikTok не вернул open_id"
        )

    return token_data

async def _refresh_tiktok_access_token(
    account: dict
) -> str:

    encrypted_refresh_token = account.get(
        "refresh_token_encrypted"
    )

    if not encrypted_refresh_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "У TikTok аккаунта отсутствует "
                "refresh token"
            )
        )

    try:
        refresh_token = (
            _token_cipher()
            .decrypt(
                encrypted_refresh_token.encode()
            )
            .decode()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось расшифровать "
                "TikTok refresh token"
            )
        ) from exc

    client_id = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()

    if not client_id:
        raise HTTPException(
            status_code=500,
            detail="TIKTOK_CLIENT_KEY не задан"
        )

    if not client_secret:
        raise HTTPException(
            status_code=500,
            detail="TIKTOK_CLIENT_SECRET не задан"
        )

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            }
        )

    print(
        "TIKTOK REFRESH STATUS:",
        response.status_code
    )

    print(
        "TIKTOK REFRESH RESPONSE:",
        response.text
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "TikTok не смог обновить "
                    "access token"
                ),
                "status": response.status_code,
                "response": response.text
            }
        )

    try:
        token_data = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "TikTok вернул невалидный JSON "
                "при обновлении токена"
            )
        ) from exc

    if token_data.get("error"):
        raise HTTPException(
            status_code=502,
            detail={
                "error": "TikTok refresh token error",
                "response": token_data
            }
        )

    new_access_token = str(
        token_data.get("access_token", "")
    ).strip()

    if not new_access_token:
        raise HTTPException(
            status_code=502,
            detail=(
                "TikTok не вернул новый "
                "access_token"
            )
        )

    new_refresh_token = str(
        token_data.get("refresh_token", "")
    ).strip()

    # Если TikTok вернул новый refresh_token —
    # используем его. Иначе оставляем старый.
    if not new_refresh_token:
        new_refresh_token = refresh_token

    new_expires_at = _expires_at(
        token_data.get("expires_in")
    )

    new_refresh_expires_at = _expires_at(
        token_data.get("refresh_expires_in")
    )

    account_id = account.get("id")
    user_id = account.get("user_id")

    if not account_id or not user_id:
        raise HTTPException(
            status_code=500,
            detail=(
                "У TikTok аккаунта отсутствует "
                "id или user_id"
            )
        )

    _store_social_account(
        user_id,
        "tiktok",
        str(account.get("account_id", "")),
        str(
            account.get(
                "account_name",
                ""
            )
        ),
        new_access_token,
        refresh_token=new_refresh_token,
        expires_at=new_expires_at,
        metadata={
            "login_type": "tiktok_login",
            "token_type": token_data.get(
                "token_type",
                "Bearer"
            ),
            "scope": token_data.get(
                "scope",
                account.get("scope", "")
            ),
            "refresh_expires_at": (
                new_refresh_expires_at
                or account.get("refresh_expires_at")
            ),
            "avatar_url": account.get(
                "avatar_url",
                ""
            )
        }
    )

    # Обновляем выбранный аккаунт в памяти,
    # чтобы следующий запрос использовал новый токен.
    updated_account = dict(account)

    updated_account[
        "access_token_encrypted"
    ] = _token_cipher().encrypt(
        new_access_token.encode()
    ).decode()

    updated_account[
        "refresh_token_encrypted"
    ] = _token_cipher().encrypt(
        new_refresh_token.encode()
    ).decode()

    _selected_account.set(
        updated_account
    )

    print(
        "TIKTOK: access token успешно обновлён"
    )

    return new_access_token

async def _refresh_threads_token(
    access_token: str
) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://graph.threads.net/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": access_token
            },
            timeout=30.0
        )

    print(
        "THREADS REFRESH STATUS:",
        response.status_code
    )
    print(
        "THREADS REFRESH RESPONSE:",
        response.text
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Threads не смог обновить access token",
                "status": response.status_code,
                "response": response.text
            }
        )

    try:
        token_data = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="Threads вернул невалидный JSON при обновлении token"
        ) from exc

    new_access_token = token_data.get("access_token")

    if not new_access_token:
        raise HTTPException(
            status_code=502,
            detail="Threads не вернул новый access token"
        )

    return token_data

@app.get("/api/v1/debug/threads/refresh")
async def debug_refresh_threads():
    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM social_accounts
            WHERE platform = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ("threads",)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="В базе нет подключённого аккаунта threads"
        )

    account = dict(row)

    try:
        access_token = _token_cipher().decrypt(
            account["access_token_encrypted"].encode()
        ).decode()
    except InvalidToken as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать Threads token"
        ) from exc

    print(
        "DEBUG REFRESH THREADS USER ID:",
        account["account_id"]
    )
    print(
        "DEBUG REFRESH THREADS OLD TOKEN:",
        "<set>" if access_token else "<missing>"
    )
    print(
        "DEBUG REFRESH THREADS OLD TOKEN LENGTH:",
        len(access_token) if access_token else 0
    )

    token_data = await _refresh_threads_token(
        access_token
    )

    new_access_token = token_data["access_token"]

    expires_at = _expires_at(
        token_data.get("expires_in")
    )

    _store_social_account(
        account["user_id"],
        "threads",
        account["account_id"],
        account["account_name"],
        new_access_token,
        expires_at=expires_at,
        metadata={
            "login_type": "threads_login",
            "token_type": "long_lived"
        }
    )

    return {
        "status": "success",
        "user_id": account["account_id"],
        "token": "<updated>",
        "token_length": len(new_access_token),
        "expires_in": token_data.get("expires_in"),
        "expires_at": expires_at
    }

# ============================================================
# YOUTUBE OAUTH START
# ============================================================

@app.post("/api/v1/oauth/youtube/start")
async def start_youtube_oauth(
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None)
):
    user = _user_from_authorization(authorization)

    client_id, _ = _youtube_credentials()

    configured_frontend = (
        os.getenv("FRONTEND_URL", "")
        .strip()
        .rstrip("/")
    )

    requested_frontend = (
        (origin or "")
        .strip()
        .rstrip("/")
    )

    allowed_local_origins = {
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    }

    if (
        requested_frontend not in allowed_local_origins
        and requested_frontend != configured_frontend
    ):
        requested_frontend = configured_frontend

    # ========================================================
    # СОЗДАЁМ STATE
    # ========================================================

    state = secrets.token_urlsafe(32)

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO oauth_states (
                state,
                user_id,
                platform,
                created_at,
                frontend_url
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state,
                user["id"],
                "youtube",
                _now(),
                requested_frontend
            )
        )

    # ========================================================
    # GOOGLE OAUTH URL
    # ========================================================

    query = urlencode({
        "client_id": client_id,
        "redirect_uri": _youtube_redirect_uri(),
        "response_type": "code",
        "scope": (
            "https://www.googleapis.com/auth/youtube.upload"
        ),
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    })

    authorization_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + query
    )

    print(
        "YOUTUBE OAUTH URL:",
        authorization_url
    )

    return {
        "authorization_url": authorization_url
    }

@app.get("/api/v1/oauth/youtube/callback")
async def youtube_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None
):

    if error:

        message = (
            error_description
            or error
        )

        raise HTTPException(
            status_code=400,
            detail=f"YouTube OAuth: {message}"
        )

    if not code:

        raise HTTPException(
            status_code=400,
            detail="YouTube OAuth не вернул code"
        )

    if not state:

        raise HTTPException(
            status_code=400,
            detail="YouTube OAuth не вернул state"
        )

    # ==========================================
    # ПРОВЕРЯЕМ STATE
    # ==========================================

    with _db() as connection:

        row = connection.execute(
            """
            SELECT *
            FROM oauth_states
            WHERE state = ?
              AND platform = ?
            """,
            (
                state,
                "youtube"
            )
        ).fetchone()

        if not row:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Недействительный или "
                    "просроченный OAuth state"
                )
            )

        connection.execute(
            """
            DELETE FROM oauth_states
            WHERE state = ?
            """,
            (state,)
        )

    # ==========================================
    # ОБМЕН CODE НА TOKEN
    # ==========================================

    token_data = await _exchange_youtube_code(
        code
    )

    access_token = str(
        token_data.get("access_token", "")
    ).strip()

    if not access_token:

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube не вернул access_token"
            )
        )

    refresh_token = str(
        token_data.get("refresh_token", "")
    ).strip()

    # ==========================================
    # ПОЛУЧАЕМ YOUTUBE CHANNEL
    # ==========================================

    channel = await _get_youtube_channel(
        access_token
    )

    channel_id = str(
        channel.get("id", "")
    ).strip()

    if not channel_id:

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube не вернул идентификатор канала"
            )
        )

    snippet = channel.get(
        "snippet",
        {}
    )

    channel_title = str(
        snippet.get("title", "")
    ).strip()

    if not channel_title:

        channel_title = "YouTube channel"

    # ==========================================
    # TOKEN EXPIRATION
    # ==========================================

    expires_at = _expires_at(
        token_data.get("expires_in")
    )

    # ==========================================
    # СОХРАНЯЕМ АККАУНТ
    # ==========================================

    metadata = {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "thumbnail": (
            snippet
            .get("thumbnails", {})
            .get("default", {})
            .get("url")
        ),
        "scope": token_data.get("scope"),
        "token_type": token_data.get("token_type")
    }

    account = _store_social_account(
        user_id=row["user_id"],
        platform="youtube",
        account_id=channel_id,
        account_name=channel_title,
        access_token=access_token,
        refresh_token=(
            refresh_token
            if refresh_token
            else None
        ),
        expires_at=expires_at,
        metadata=metadata
    )

    # ==========================================
    # ВОЗВРАЩАЕМ ПОЛЬЗОВАТЕЛЯ В FRONTEND
    # ==========================================

    frontend_url = row["frontend_url"]

    return RedirectResponse(
        url=_frontend_url(
            "connected",
            frontend_url
        )
    )

@app.get("/api/v1/oauth/linkedin/callback")
async def linkedin_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None
):
    if error:
        message = error_description or error
        raise HTTPException(
            status_code=400,
            detail=f"LinkedIn OAuth: {message}"
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="LinkedIn OAuth не вернул code"
        )

    if not state:
        raise HTTPException(
            status_code=400,
            detail="LinkedIn OAuth не вернул state"
        )

    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM oauth_states
            WHERE state = ?
              AND platform = ?
            """,
            (state, "linkedin")
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=400,
                detail="Недействительный или просроченный OAuth state"
            )

        connection.execute(
            "DELETE FROM oauth_states WHERE state = ?",
            (state,)
        )

    token_data = await _exchange_linkedin_code(code)

    access_token = str(
        token_data.get("access_token", "")
    ).strip()

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="LinkedIn не вернул access_token"
        )

    userinfo = await _get_linkedin_userinfo(access_token)

    linkedin_user_id = str(
        userinfo.get("sub", "")
    ).strip()

    if not linkedin_user_id:
        raise HTTPException(
            status_code=502,
            detail="LinkedIn не вернул идентификатор пользователя"
        )

    account_name = str(
        userinfo.get("name", "")
    ).strip()

    if not account_name:
        account_name = "LinkedIn user"

    expires_at = _expires_at(
        token_data.get("expires_in")
    )

    metadata = {
        "email": userinfo.get("email"),
        "email_verified": userinfo.get("email_verified"),
        "picture": userinfo.get("picture"),
        "locale": userinfo.get("locale"),
        "scope": token_data.get("scope"),
        "token_type": token_data.get("token_type")
    }

    account = _store_social_account(
        user_id=row["user_id"],
        platform="linkedin",
        account_id=linkedin_user_id,
        account_name=account_name,
        access_token=access_token,
        expires_at=expires_at,
        metadata=metadata,
        refresh_token=token_data.get("refresh_token")
    )

    frontend_url = row["frontend_url"]

    return RedirectResponse(
        url=_frontend_url(
            "success",
            frontend_url
        )
    )


@app.get("/api/v1/oauth/meta/callback")
async def meta_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_reason: str | None = None,
    error_description: str | None = None,
    error_code: str | None = None
):
    if error:
        print(
            "OAuth denied:",
            error,
            error_reason or "",
            error_description or "",
            error_code or ""
        )
        return RedirectResponse(url=_frontend_url("denied"))
    if not code or not state:
        raise HTTPException(status_code=400, detail="Meta OAuth не вернул code или state")
    with _db() as connection:
        oauth_state = connection.execute(
            "SELECT * FROM oauth_states WHERE state = ?",
            (state,)
        ).fetchone()
        connection.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not oauth_state:
        raise HTTPException(status_code=400, detail="OAuth state недействителен или уже использован")
    try:
        state_created_at = datetime.fromisoformat(oauth_state["created_at"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="OAuth state имеет неверную дату") from exc
    if datetime.now(timezone.utc) - state_created_at > timedelta(minutes=10):
        raise HTTPException(status_code=400, detail="OAuth state истёк")
    _oauth_config(oauth_state["platform"])
    app_id, app_secret = _oauth_credentials(oauth_state["platform"])
    print(
    "THREADS CREDENTIALS:",
    oauth_state["platform"],
    app_id,
    "<set>" if app_secret else "<missing>",
    len(app_secret) if app_secret else 0
)
    async with httpx.AsyncClient() as client:
        platform = oauth_state["platform"]
        if platform == "instagram":
            token_data = await _exchange_instagram_code(code)
            profile_response = await client.get(
                "https://graph.instagram.com/me",
                params={
                    "fields": "id,user_id,username",
                    "access_token": token_data["access_token"]
                },
                timeout=30.0
            )
            print("THREADS PROFILE STATUS:", profile_response.status_code)
            print("THREADS PROFILE RESPONSE:", profile_response.text)
            if profile_response.status_code != 200:
                raise HTTPException(status_code=502, detail="Не удалось получить Instagram-профиль")
            profile = profile_response.json()
            instagram_user_id = str(profile.get("id") or "").strip()
            if not instagram_user_id:
                raise HTTPException(status_code=502, detail="Instagram-профиль не вернул id")
            _store_social_account(
                oauth_state["user_id"], "instagram", instagram_user_id,
                profile.get("username") or instagram_user_id, token_data["access_token"],
                expires_at=_expires_at(token_data.get("expires_in")),
                metadata={"login_type": "instagram_login"}
            )
        elif platform == "facebook":
            token_response = await client.get(
                "https://graph.facebook.com/v26.0/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "redirect_uri": _oauth_redirect_uri(),
                    "code": code
                },
                timeout=30.0
            )
            if token_response.status_code != 200:
                raise HTTPException(status_code=502, detail="Facebook не выдал access token")
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(status_code=502, detail="Facebook не вернул access token")
            threads_me_response = await client.get(
                "https://graph.threads.net/v1.0/me",
                headers={
                    "Authorization": f"Bearer {access_token}"
                },
                params={
                    "fields": "id,username"
                },
                timeout=30.0
            )

            print(
                "THREADS ME STATUS:",
                threads_me_response.status_code
            )
            print(
                "THREADS ME RESPONSE:",
                threads_me_response.text
            )
            accounts_response = await client.get(
                "https://graph.facebook.com/v26.0/me/accounts",
                params={"access_token": access_token}, timeout=30.0
            )
            accounts = accounts_response.json().get("data", []) if accounts_response.status_code == 200 else []
            if not accounts:
                raise HTTPException(status_code=400, detail="Meta не вернула доступные Facebook Pages")
            for page in accounts:
                _store_social_account(
                    oauth_state["user_id"], "facebook", page["id"], page.get("name", page["id"]),
                    page.get("access_token", access_token),
                    expires_at=_expires_at(page.get("expires_in") or token_data.get("expires_in")),
                    metadata={"page_id": page["id"], "login_type": "facebook_login"}
                )
        else:
            token_response = await client.post(
                "https://graph.threads.net/oauth/access_token",
                data={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": _oauth_redirect_uri(),
                    "code": code
                },
                timeout=30.0
            )

            if token_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "Threads не выдал access token",
                        "response": token_response.text
                    }
                )

            token_data = token_response.json()

            short_access_token = token_data.get("access_token")

            print(
                "THREADS SHORT TOKEN:",
                "<set>" if short_access_token else "<missing>"
            )

            if not short_access_token:
                raise HTTPException(
                    status_code=502,
                    detail="Threads не вернул access token"
                )

            long_token_data = await _exchange_threads_token(
                short_access_token
            )

            access_token = long_token_data.get(
                "access_token"
            )

            print(
                "THREADS LONG TOKEN:",
                "<set>" if access_token else "<missing>"
            )

            print(
                "THREADS LONG TOKEN DATA:",
                {
                    key: value
                    for key, value in long_token_data.items()
                    if key not in {"access_token"}
                }
            )

            if not access_token:
                raise HTTPException(
                    status_code=502,
                    detail="Threads не вернул long-lived access token"
                )

            threads_user_id = str(
                token_data.get("user_id") or ""
            ).strip()

            if not threads_user_id:
                raise HTTPException(
                    status_code=502,
                    detail="Threads не вернул user_id"
                )

            _store_social_account(
                oauth_state["user_id"],
                platform,
                threads_user_id,
                threads_user_id,
                access_token,
                expires_at=_expires_at(
                    long_token_data.get("expires_in")
                ),
                metadata={
                    "login_type": "threads_login",
                    "token_type": "long_lived"
                }
            )

            
    return RedirectResponse(
        url=_frontend_url("connected", oauth_state["frontend_url"])
    )

@app.get("/api/v1/oauth/tiktok/callback")
async def tiktok_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None
):
    if error:
        print(
            "TikTok OAuth denied:",
            error,
            error_description or ""
        )

        return RedirectResponse(
            url=_frontend_url("denied")
        )

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="TikTok OAuth не вернул code или state"
        )

    with _db() as connection:
        oauth_state = connection.execute(
            """
            SELECT *
            FROM oauth_states
            WHERE state = ?
            """,
            (state,)
        ).fetchone()

        connection.execute(
            "DELETE FROM oauth_states WHERE state = ?",
            (state,)
        )

    if not oauth_state:
        raise HTTPException(
            status_code=400,
            detail="TikTok OAuth state недействителен или уже использован"
        )

    if oauth_state["platform"] != "tiktok":
        raise HTTPException(
            status_code=400,
            detail="OAuth state предназначен для другой платформы"
        )

    try:
        state_created_at = datetime.fromisoformat(
            oauth_state["created_at"]
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="TikTok OAuth state имеет неверную дату"
        ) from exc

    if datetime.now(timezone.utc) - state_created_at > timedelta(minutes=10):
        raise HTTPException(
            status_code=400,
            detail="TikTok OAuth state истёк"
        )

    token_data = await _exchange_tiktok_code(code)

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    open_id = str(token_data["open_id"]).strip()

    async with httpx.AsyncClient() as client:
        profile_response = await client.get(
            "https://open.tiktokapis.com/v2/user/info/",
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            params={
                "fields": "open_id,display_name,avatar_url"
            },
            timeout=30.0
        )

    print(
        "TIKTOK PROFILE STATUS:",
        profile_response.status_code
    )

    if profile_response.status_code != 200:
        print(
            "TIKTOK PROFILE RESPONSE:",
            profile_response.text
        )

        raise HTTPException(
            status_code=502,
            detail="Не удалось получить TikTok-профиль"
        )

    try:
        profile_data = profile_response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="TikTok вернул невалидный JSON профиля"
        ) from exc

    profile = (
        profile_data
        .get("data", {})
        .get("user", {})
    )

    profile_open_id = str(
        profile.get("open_id") or open_id
    ).strip()

    display_name = str(
        profile.get("display_name")
        or profile_open_id
    ).strip()

    _store_social_account(
        oauth_state["user_id"],
        "tiktok",
        profile_open_id,
        display_name,
        access_token,
        refresh_token=refresh_token,
        expires_at=_expires_at(
            token_data.get("expires_in")
        ),
        metadata={
            "login_type": "tiktok_login",
            "token_type": token_data.get(
                "token_type",
                "Bearer"
            ),
            "scope": token_data.get("scope", ""),
            "refresh_expires_at": _expires_at(
                token_data.get("refresh_expires_in")
            ),
            "avatar_url": profile.get(
                "avatar_url",
                ""
            )
        }
    )

    return RedirectResponse(
        url=_frontend_url(
            "connected",
            oauth_state["frontend_url"]
        )
    )

@app.get("/api/v1/source-texts")
async def source_texts():
    data = await get_source_texts()

    return {
        "domain": data.get("domain"),
        "tov": data.get("tov", ""),
        "pages": data.get("pages", [])
    }


# ============================================================
# GENERATE CONTENT
# ============================================================

def build_mock_generated_content(
    selected_formats: List[AllowedFormat]
) -> Dict[str, Any]:
    generated_content: Dict[str, Any] = {}

    if AllowedFormat.POST in selected_formats:
        generated_content["post"] = {
            "title": "Тестовый пост RespondService",
            "text": "Это демо-текст для проверки генерации без обращения к AI.",
            "visual_idea": "Превью режима заглушки RespondService"
        }

    if AllowedFormat.REELS in selected_formats:
        generated_content["reels"] = {
            "hook": "Тестовый сценарий Reels",
            "timeline": [
                {
                    "second": 0,
                    "speaker": "Проверяем работу RespondService.",
                    "visual": "Вертикальная демонстрационная сцена с интерфейсом RespondService."
                },
                {
                    "second": 5,
                    "speaker": "Сценарий создан в режиме заглушки.",
                    "visual": "Финальная вертикальная сцена с результатом тестовой генерации."
                }
            ]
        }

    if AllowedFormat.CAROUSEL in selected_formats:
        generated_content["carousel"] = {
            "title": "Тестовая карусель RespondService",
            "caption": "Демо-карусель для проверки RespondService",
            "slides": [
                {
                    "number": 1,
                    "type": "cover",
                    "title": "ТЕСТ ЗАПУЩЕН",
                    "text": "Карусель создана без обращения к AI.",
                    "visual_prompt": "Экран RespondService с успешно завершённой тестовой генерацией.",
                    "text_position": "left"
                },
                {
                    "number": 2,
                    "type": "content",
                    "title": "СЛАЙДЫ СОЗДАНЫ",
                    "text": "Проверка рендера и передачи изображений.",
                    "visual_prompt": "Три тестовых слайда карусели RespondService, показанные как готовые изображения.",
                    "text_position": "right"
                },
                {
                    "number": 3,
                    "type": "final",
                    "title": "ЗАГЛУШКА РАБОТАЕТ",
                    "text": "Ответ сформирован локально для тестирования.",
                    "visual_prompt": "Финальный экран с отметкой о завершении демо-генерации RespondService.",
                    "text_position": "center"
                }
            ]
        }

    return generated_content

@app.post("/api/v1/generate")
async def generate_content(
    payload: GenerateRequest,
    request: Request
):
    metrics = {}

    source_data = await get_source_texts()
    source_pages = source_data.get("pages", [])

    selected_page = None

    for page in source_pages:
        if page.get("page_text") == payload.page_text:
            selected_page = page
            break

    if not selected_page:
        raise HTTPException(
            status_code=400,
            detail="Не удалось найти выбранную страницу в SEO API"
        )

    page_url = selected_page.get("url", "")
    page_keywords = selected_page.get("keywords", [])

    if not page_url:
        raise HTTPException(
            status_code=502,
            detail="У выбранной страницы отсутствует URL в SEO API"
        )

    if not isinstance(page_keywords, list):
        page_keywords = []

    system_p, user_p = build_dynamic_prompt(
        payload.selected_formats,
        payload.language,
        payload.page_text,
        payload.tov,
        page_url,
        page_keywords
    )

    # ========================================================
    # AI TEXT GENERATION
    # ========================================================

    ai_result = await call_ai_llm(
        payload.provider,
        system_p,
        user_p
    )

    generated_content = ai_result.get("content")

    if not isinstance(generated_content, dict):
        raise HTTPException(
            status_code=502,
            detail="AI не вернул корректный объект content"
        )

    text_usage = ai_result.get("usage") or {}

    public_base_url = None
    render_usage = {}

    # ========================================================
    # CAROUSEL RENDER
    # ========================================================

    carousel_usage = {
        "slides": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    vision_usage = {
        "reviews": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    # ========================================================
    # CAROUSEL RENDER
    # ========================================================
    if AllowedFormat.CAROUSEL in payload.selected_formats:
        carousel = generated_content.get("carousel")

        if not carousel:
            raise HTTPException(
                status_code=502,
                detail="AI не вернул объект carousel"
            )

        public_base_url = get_public_base_url(request)

        rendered = await render_carousel(
            carousel,
            public_base_url
        )

        generated_content["carousel"]["render"] = rendered

        # ----------------------------------------------------
        # USAGE FROM CAROUSEL
        # ----------------------------------------------------

        render_usage = rendered.get("usage")

        if isinstance(render_usage, dict):
            carousel_usage = render_usage.get(
                "image_generation",
                carousel_usage
            )

            vision_usage = render_usage.get(
                "vision_review",
                vision_usage
            )

    # ========================================================
    # REELS VIDEO RENDER
    # ========================================================

    if AllowedFormat.REELS in payload.selected_formats:
        reels = generated_content.get("reels")

        if not reels:
            raise HTTPException(
                status_code=502,
                detail="AI не вернул объект reels"
            )

        video_result = await generate_reels_video_fallback(
            reels,
            public_base_url or get_public_base_url(request)
        )

        generated_content["reels"]["video"] = video_result.get("video")
        generated_content["reels"]["video_status"] = video_result.get(
            "status"
        )

        if video_result.get("status") != "success":
            generated_content["reels"]["video_error"] = {
                "error": video_result.get("error"),
                "detail": video_result.get("detail")
            }

        # ========================================================
    # TOTAL USAGE
    # ========================================================

    text_input_tokens = int(
        text_usage.get(
            "input_tokens",
            0
        ) or 0
    )

    text_output_tokens = int(
        text_usage.get(
            "output_tokens",
            0
        ) or 0
    )

    image_input_tokens = int(
        carousel_usage.get(
            "total_input_tokens",
            0
        ) or 0
    )

    image_output_tokens = int(
        carousel_usage.get(
            "total_output_tokens",
            0
        ) or 0
    )

    vision_input_tokens = int(
        vision_usage.get(
            "input_tokens",
            0
        ) or 0
    )

    vision_output_tokens = int(
        vision_usage.get(
            "output_tokens",
            0
        ) or 0
    )

    usage = {
        "text_generation": {
            **text_usage
        },
        "image_generation": {
            **(
                render_usage.get(
                    "image_generation",
                    {}
                )
            )
        },
        "vision_review": {
            **(
                render_usage.get(
                    "vision_review",
                    {}
                )
            )
        }
    }

    usage_cost = calculate_usage_cost(
        usage
    )

    usage["cost"] = usage_cost

    metrics = {
        "input_tokens": (
            text_input_tokens
            + image_input_tokens
            + vision_input_tokens
        ),
        "output_tokens": (
            text_output_tokens
            + image_output_tokens
            + vision_output_tokens
        ),
        "cost_usd": usage_cost["total_cost_usd"]
    }

    return {
        "status": "success",
        "metrics": metrics,
        "usage": usage,
        "content": generated_content
    }
# ============================================================
# RENDER CAROUSEL MANUALLY
# ============================================================

@app.post("/api/v1/render-carousel")
async def render_carousel_endpoint(
    payload: Dict[str, Any],
    request: Request
):
    carousel = payload.get("carousel")

    if not carousel:
        raise HTTPException(
            status_code=400,
            detail="Не передан объект carousel"
        )

    public_base_url = get_public_base_url(request)

    rendered = await render_carousel(
        carousel,
        public_base_url
    )

    return {
        "status": "success",
        "render": rendered
    }


# ============================================================
# GENERATE REELS VIDEO MANUALLY
# ============================================================

@app.post("/api/v1/render-reels")
async def render_reels_endpoint(
    payload: Dict[str, Any],
    request: Request
):
    reels = payload.get("reels")

    if not reels:
        raise HTTPException(
            status_code=400,
            detail="Не передан объект reels"
        )

    raise HTTPException(
        status_code=503,
        detail=(
            "Генерация MP4 сейчас недоступна: OpenAI Videos API "
            "остановлена. Сценарий Reels можно сгенерировать отдельно."
        )
    )

@app.get("/api/v1/debug/threads")
async def debug_threads():
    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM social_accounts
            WHERE platform = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ("threads",)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="В базе нет подключённого аккаунта threads"
        )

    account = dict(row)

    try:
        access_token = _token_cipher().decrypt(
            account["access_token_encrypted"].encode()
        ).decode()
    except InvalidToken as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать Threads token"
        ) from exc

    user_id = account["account_id"]

    print("DEBUG THREADS USER ID:", user_id)
    print(
        "DEBUG THREADS TOKEN:",
        "<set>" if access_token else "<missing>"
    )
    print(
        "DEBUG THREADS TOKEN LENGTH:",
        len(access_token) if access_token else 0
    )

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://graph.threads.net/v1.0/me",
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            params={
                "fields": "id,username"
            },
            timeout=30.0
        )

    print("DEBUG THREADS ME STATUS:", response.status_code)
    print("DEBUG THREADS ME RESPONSE:", response.text)

    return {
        "status": response.status_code,
        "response": (
            response.json()
            if response.headers.get("content-type", "").startswith(
                "application/json"
            )
            else response.text
        )
    }

@app.get("/api/v1/debug/threads-token")
async def debug_threads_token():

    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM social_accounts
            WHERE platform = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ("threads",)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="В базе нет подключённого аккаунта threads"
        )

    account = dict(row)

    try:
        access_token = _token_cipher().decrypt(
            account["access_token_encrypted"].encode()
        ).decode()
    except InvalidToken as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать Threads token"
        ) from exc

    async with httpx.AsyncClient() as client:

        response = await client.get(
            "https://graph.threads.net/v1.0/debug_token",
            params={
                "input_token": access_token,
                "access_token": access_token
            },
            timeout=30.0
        )

    try:
        data = response.json()
    except Exception:
        data = {
            "raw_response": response.text
        }

    return {
        "status": response.status_code,
        "response": data
    }
# ============================================================
# PUBLISH TO THREADS
# ============================================================
@app.post("/api/v1/publish-instagram")
async def publish_instagram(
    request: PublishRequest,
    http_request: Request,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(
        default=None,
        alias="X-Account-Id"
    )
):
    _prepare_publish_account(
        authorization,
        account_header,
        "instagram"
    )

    content = request.content

    if not isinstance(content, dict):
        raise HTTPException(
            status_code=400,
            detail="Некорректный content"
        )

    # ========================================================
    # CAROUSEL
    # ========================================================

    if content.get("carousel"):

        result = await publish_carousel_to_instagram(
            content["carousel"]
        )

        return {
            "platform": "instagram",
            "format": "carousel",
            **result
        }

    # ========================================================
    # REELS
    # ========================================================

    reels = content.get("reels")

    if not isinstance(reels, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации в Instagram "
                "нужен формат Reels"
            )
        )

    # ========================================================
    # 1. НАХОДИМ ГОТОВОЕ MP4
    # ========================================================

    video = reels.get("video")

    if not isinstance(video, dict):

        videos_dir = (
            GENERATED_DIR
            / "videos"
        )

        if not videos_dir.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    "Папка generated/videos "
                    "не найдена"
                )
            )

        video_files = [
            path
            for path in videos_dir.rglob("*.mp4")
            if path.is_file()
        ]

        if not video_files:
            raise HTTPException(
                status_code=404,
                detail=(
                    "В generated/videos "
                    "нет готовых MP4-видео"
                )
            )

        latest_video_file = max(
            video_files,
            key=lambda path: path.stat().st_mtime
        )

        public_base_url = get_public_base_url(
            http_request
        )

        relative_path = (
            latest_video_file.relative_to(
                GENERATED_DIR
            )
        )

        video_url = (
            public_base_url.rstrip("/")
            + "/generated/"
            + relative_path.as_posix()
        )

        video = {
            "path": str(
                latest_video_file
            ),
            "url": video_url
        }

        reels["video"] = video

        print(
            "INSTAGRAM: используется готовое видео:",
            latest_video_file
        )

        print(
            "INSTAGRAM: публичный URL:",
            video_url
        )

    # ========================================================
    # 2. ПРОВЕРЯЕМ VIDEO URL
    # ========================================================

    video_url = str(
        video.get("url", "")
    ).strip()

    if not video_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "У видео отсутствует публичный URL"
            )
        )

    if not video_url.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Instagram требует публичный "
                "HTTPS URL видео"
            )
        )

    # ========================================================
    # 3. ПОЛУЧАЕМ INSTAGRAM CREDENTIALS
    # ========================================================

    user_id, access_token = (
        _social_credentials("instagram")
    )

    # ========================================================
    # 4. ТЕКСТ REELS
    # ========================================================

    caption = str(
        reels.get("hook", "")
    ).strip()

    caption = caption[:2200]

    # ========================================================
    # 5. СОЗДАЁМ REELS CONTAINER
    # ========================================================

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        create_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media",
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": access_token
            },
            timeout=60.0
        )

    print(
        "INSTAGRAM REELS CONTAINER STATUS:",
        create_response.status_code
    )

    print(
        "INSTAGRAM REELS CONTAINER RESPONSE:",
        create_response.text
    )

    if create_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "Instagram не смог "
                    "создать Reels container"
                ),
                "status": create_response.status_code,
                "response": create_response.text
            }
        )

    try:
        create_data = create_response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Instagram вернул "
                "невалидный JSON"
            )
        ) from exc

    creation_id = create_data.get("id")

    if not creation_id:
        raise HTTPException(
            status_code=502,
            detail=(
                "Instagram не вернул "
                "creation_id"
            )
        )

    # ========================================================
    # 6. ЖДЁМ ГОТОВНОСТИ CONTAINER
    # ========================================================

    container_status = None

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        for attempt in range(20):

            await asyncio.sleep(5)

            status_response = await client.get(
                f"https://graph.instagram.com/v23.0/"
                f"{creation_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": access_token
                },
                timeout=30.0
            )

            print(
                "INSTAGRAM REELS STATUS:",
                status_response.status_code,
                status_response.text
            )

            if status_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Instagram не смог "
                            "проверить статус Reels"
                        ),
                        "status": (
                            status_response.status_code
                        ),
                        "response": (
                            status_response.text
                        )
                    }
                )

            status_data = status_response.json()

            container_status = (
                status_data.get("status_code")
                or status_data.get("status")
            )

            if container_status == "FINISHED":
                break

            if container_status in (
                "ERROR",
                "EXPIRED"
            ):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": (
                            "Instagram не смог "
                            "обработать Reels"
                        ),
                        "status": container_status,
                        "response": status_data
                    }
                )

        else:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Instagram слишком долго "
                    "обрабатывает Reels"
                )
            )

    # ========================================================
    # 7. ПУБЛИКУЕМ REELS
    # ========================================================

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        publish_response = await client.post(
            f"https://graph.instagram.com/v23.0/"
            f"{user_id}/media_publish",
            params={
                "creation_id": creation_id,
                "access_token": access_token
            },
            timeout=60.0
        )

    print(
        "INSTAGRAM REELS PUBLISH STATUS:",
        publish_response.status_code
    )

    print(
        "INSTAGRAM REELS PUBLISH RESPONSE:",
        publish_response.text
    )

    if publish_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "Instagram не смог "
                    "опубликовать Reels"
                ),
                "status": (
                    publish_response.status_code
                ),
                "response": (
                    publish_response.text
                )
            }
        )

    try:
        publish_data = publish_response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Instagram вернул "
                "невалидный JSON публикации"
            )
        ) from exc

    media_id = publish_data.get("id")

    return {
        "status": "success",
        "platform": "instagram",
        "format": "reels",
        "creation_id": creation_id,
        "media_id": media_id,
        "video": {
            "path": video.get("path"),
            "url": video_url
        }
    }

    # ========================================================
    # CAROUSEL
    # ========================================================

    if content.get("carousel"):

        result = await publish_carousel_to_instagram(
            content["carousel"]
        )

        return {
            "platform": "instagram",
            "format": "carousel",
            **result
        }

    # ========================================================
    # REELS
    # ========================================================

    if content.get("reels"):

        raise HTTPException(
            status_code=400,
            detail=(
                "Публикация Reels в Instagram "
                "пока не подключена"
            )
        )

    raise HTTPException(
        status_code=400,
        detail=(
            "Не найден поддерживаемый формат "
            "для публикации в Instagram"
        )
    )

# ============================================================
# PUBLISH TO FACEBOOK
# ============================================================

@app.post("/api/v1/publish-facebook")
async def publish_facebook(
    request: PublishRequest,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(default=None, alias="X-Account-Id")
):
    _prepare_publish_account(authorization, account_header, "facebook")
    content = request.content

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Контент для Facebook отсутствует"
        )

    # ========================================================
    # CAROUSEL
    # ========================================================

    carousel = content.get("carousel")

    if carousel:
        caption = str(
            carousel.get("caption", "")
            or carousel.get("text", "")
            or ""
        ).strip()

        if not caption:
            raise HTTPException(
                status_code=400,
                detail="Текст публикации Facebook пустой"
            )

        result = await publish_to_facebook(
            caption
        )

        return {
            "platform": "facebook",
            "format": "text",
            **result
        }

    # ========================================================
    # REELS
    # ========================================================

    reels = content.get("reels")

    if reels:
        hook = str(
            reels.get("hook", "")
        ).strip()

        if not hook:
            raise HTTPException(
                status_code=400,
                detail="Текст публикации Facebook пустой"
            )

        # ========================================================
        # ИЩЕМ УЖЕ ГОТОВОЕ ПОСЛЕДНЕЕ MP4
        # ========================================================

        videos_dir = GENERATED_DIR / "videos"

        if not videos_dir.exists():
            raise HTTPException(
                status_code=404,
                detail="Папка generated/videos не найдена"
            )

        video_files = [
            path
            for path in videos_dir.rglob("*.mp4")
            if path.is_file()
        ]

        if not video_files:
            raise HTTPException(
                status_code=404,
                detail="В generated/videos нет готовых MP4-видео"
            )

        latest_video_file = max(
            video_files,
            key=lambda path: path.stat().st_mtime
        )

        print(
            "FACEBOOK: используется уже готовое видео:",
            latest_video_file
        )

        # ========================================================
        # ПУБЛИКУЕМ ВИДЕО
        # ========================================================

        result = await publish_video_to_facebook(
            video_path=latest_video_file,
            text=hook
        )

        return {
            "platform": "facebook",
            "format": "video",
            "facebook": result
        }

    raise HTTPException(
        status_code=400,
        detail=(
            "Не найден поддерживаемый формат "
            "для публикации в Facebook"
        )
    )

@app.post("/api/v1/debug/threads-text")
async def debug_threads_text():

    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM social_accounts
            WHERE platform = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ("threads",)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="В базе нет подключённого Threads аккаунта"
        )

    account = dict(row)

    _selected_account.set(account)

    result = await publish_to_threads(
        "Тест RespondService"
    )

    return {
        "status": "success",
        "type": "text",
        "threads": result
    }

@app.post("/api/v1/publish")
async def publish_content(
    payload: PublishRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(default=None, alias="X-Account-Id")
):
    _prepare_publish_account(authorization, account_header, "threads")
    content = payload.content

    # ========================================================
    # REELS / VIDEO
    # ========================================================
    reels = content.get("reels")

    if reels:
        video = reels.get("video")

        if not isinstance(video, dict):
            videos_dir = GENERATED_DIR / "videos"

            if not videos_dir.exists():
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Папка generated/videos "
                        "не найдена"
                    )
                )

            video_files = [
                path
                for path in videos_dir.rglob("*.mp4")
                if path.is_file()
            ]

            if not video_files:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "В generated/videos "
                        "нет готовых MP4-видео"
                    )
                )

            latest_video_file = max(
                video_files,
                key=lambda path: path.stat().st_mtime
            )

            public_base_url = get_public_base_url(request)

            relative_path = latest_video_file.relative_to(
                GENERATED_DIR
            )

            video_url = (
                public_base_url.rstrip("/")
                + "/generated/"
                + relative_path.as_posix()
            )

            video = {
                "path": str(latest_video_file),
                "url": video_url
            }

            reels["video"] = video

            print(
                "THREADS: используется уже готовое видео:",
                latest_video_file
            )
            print(
                "THREADS: публичный URL видео:",
                video_url
            )

        hook = str(
            reels.get("hook", "")
        ).strip()

        result = await publish_video_to_threads(
            video=video,
            text=hook,
            alt_text=(
                "Вертикальное видео для Threads"
            )
        )

        return {
            "status": "success",
            "type": "video",
            "threads": result
        }

    # ========================================================
    # CAROUSEL
    # ========================================================
    carousel = content.get("carousel")

    if not carousel:
        raise HTTPException(
            status_code=400,
            detail=(
                "В результате генерации "
                "отсутствует reels или carousel"
            )
        )

    render = carousel.get("render")

    if not isinstance(render, dict):
        public_base_url = get_public_base_url(request)

        render = await render_carousel(
            carousel,
            public_base_url
        )

        carousel["render"] = render

    slides = render.get("slides")

    if not isinstance(slides, list):
        raise HTTPException(
            status_code=400,
            detail=(
                "У carousel.render отсутствует "
                "массив slides"
            )
        )

    if len(slides) < 2:
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации в Threads "
                "нужно минимум 2 слайда"
            )
        )

    if len(slides) > 20:
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации в Threads "
                "можно передать максимум 20 слайдов"
            )
        )

    result = await publish_carousel_to_threads(
        carousel
    )

    return {
        "status": "success",
        "type": "carousel",
        "slides_count": len(slides),
        "threads": result
    }


@app.post("/api/v1/publish-tiktok")
async def publish_tiktok(
    payload: PublishRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(
        default=None,
        alias="X-Account-Id"
    )
):
    # ========================================================
    # ПРОВЕРЯЕМ ВЫБРАННЫЙ TIKTOK-АККАУНТ
    # ========================================================

    _prepare_publish_account(
        authorization,
        account_header,
        "tiktok"
    )

    account = _selected_account.get()

    if not account:
        raise HTTPException(
            status_code=404,
            detail="TikTok аккаунт не выбран"
        )

    encrypted_token = account.get(
        "access_token_encrypted"
    )

    if not encrypted_token:
        raise HTTPException(
            status_code=400,
            detail="У TikTok аккаунта отсутствует access token"
        )

    try:
        access_token = (
            _token_cipher()
            .decrypt(
                encrypted_token.encode()
            )
            .decode()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать TikTok access token"
        ) from exc

    # ========================================================
    # ПОЛУЧАЕМ REELS
    # ========================================================

    content = payload.content

    reels = content.get("reels")

    if not isinstance(reels, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации в TikTok "
                "нужен формат Reels"
            )
        )

    # ========================================================
    # ЕСЛИ ВИДЕО ЕЩЁ НЕ СОЗДАНО — СОЗДАЁМ
    # ========================================================

    video = reels.get("video")

# ========================================================
# ЕСЛИ В CONTENT НЕТ VIDEO — БЕРЁМ ПОСЛЕДНЕЕ ГОТОВОЕ MP4
# ========================================================

    if not isinstance(video, dict):

        videos_dir = GENERATED_DIR / "videos"

        if not videos_dir.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    "Папка generated/videos "
                    "не найдена"
                )
            )

        video_files = [
            path
            for path in videos_dir.rglob("*.mp4")
            if path.is_file()
        ]

        if not video_files:
            raise HTTPException(
                status_code=404,
                detail=(
                    "В generated/videos "
                    "нет готовых MP4-видео"
                )
            )

        latest_video_file = max(
            video_files,
            key=lambda path: path.stat().st_mtime
        )

        video = {
            "path": str(
                latest_video_file
            )
        }

        reels["video"] = video

        print(
            "TIKTOK: используется уже готовое видео:",
            latest_video_file
    )

    # ========================================================
    # ПУТЬ К MP4
    # ========================================================

    video_path = video.get("path")

    if not video_path:
        raise HTTPException(
            status_code=400,
            detail="У сгенерированного видео отсутствует path"
        )

    video_file = Path(video_path)

    if not video_file.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Файл видео не найден: "
                f"{video_file}"
            )
        )

    if video_file.suffix.lower() != ".mp4":
        raise HTTPException(
            status_code=400,
            detail="TikTok сейчас ожидает MP4-видео"
        )

    video_size = video_file.stat().st_size

    if video_size <= 0:
        raise HTTPException(
            status_code=400,
            detail="Файл видео пуст"
        )

    # ========================================================
    # ТЕКСТ TIKTOK
    # ========================================================

    title = str(
        reels.get("hook", "")
    ).strip()

    # TikTok допускает до 2200 UTF-16 символов.
    # Для нашего случая достаточно безопасно ограничить
    # обычной длиной строки.
    title = title[:2200]

    # ========================================================
    # 1. CREATOR INFO
    # ========================================================

    async with httpx.AsyncClient(
    timeout=60.0
) as client:

        creator_response = await client.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                ),
                "Content-Type": (
                    "application/json; charset=UTF-8"
                )
            }
        )

    # ========================================================
    # ЕСЛИ ACCESS TOKEN ПРОТУХ — ОБНОВЛЯЕМ
    # ========================================================

    if creator_response.status_code == 401:

        print(
            "TIKTOK: access token недействителен."
        )

        print(
            "TIKTOK: пытаемся обновить access token..."
        )

        access_token = await _refresh_tiktok_access_token(
            account
        )

        print(
            "TIKTOK: повторяем Creator Info "
            "с новым access token..."
        )

        async with httpx.AsyncClient(
            timeout=60.0
        ) as client:

            creator_response = await client.post(
                "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                headers={
                    "Authorization": (
                        f"Bearer {access_token}"
                    ),
                    "Content-Type": (
                        "application/json; charset=UTF-8"
                    )
                }
            )

    print(
        "TIKTOK CREATOR INFO STATUS:",
        creator_response.status_code
    )

    if creator_response.status_code != 200:
        print(
            "TIKTOK CREATOR INFO RESPONSE:",
            creator_response.text
        )

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "TikTok не вернул информацию "
                    "о creator"
                ),
                "status": creator_response.status_code,
                "response": creator_response.text
            }
        )

    try:
        creator_data = creator_response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="TikTok вернул невалидный JSON creator info"
        ) from exc

    if (
        creator_data.get("error", {}).get("code")
        != "ok"
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "error": "TikTok creator info error",
                "response": creator_data
            }
        )

    creator = creator_data.get(
        "data",
        {}
    )

    privacy_options = creator.get(
        "privacy_level_options",
        []
    )

    if not privacy_options:
        raise HTTPException(
            status_code=502,
            detail=(
                "TikTok не вернул доступные "
                "privacy_level"
            )
        )

    # В Sandbox/неаудированном приложении
    # безопаснее использовать SELF_ONLY.
    if "SELF_ONLY" in privacy_options:
        privacy_level = "SELF_ONLY"
    else:
        privacy_level = privacy_options[0]

    max_duration = creator.get(
        "max_video_post_duration_sec"
    )

    video_duration = video.get(
        "duration_seconds"
    )

    if (
        isinstance(max_duration, (int, float))
        and isinstance(video_duration, (int, float))
        and video_duration > max_duration
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Видео слишком длинное для TikTok: "
                f"{video_duration} сек. "
                f"Максимум: {max_duration} сек."
            )
        )

    # ========================================================
    # 2. ИНИЦИАЛИЗИРУЕМ DIRECT POST
    # ========================================================

    # TikTok разрешает chunk от 5 MB до 64 MB.
    # Для небольшого файла отправляем одним куском.
    max_chunk_size = 10 * 1024 * 1024

    if video_size <= max_chunk_size:
        chunk_size = video_size
        total_chunk_count = 1
    else:
        chunk_size = max_chunk_size
        total_chunk_count = (
            video_size + chunk_size - 1
        ) // chunk_size

    init_payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "is_aigc": True
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count
        }
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        init_response = await client.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                ),
                "Content-Type": (
                    "application/json; charset=UTF-8"
                )
            },
            json=init_payload
        )

    print(
        "TIKTOK VIDEO INIT STATUS:",
        init_response.status_code
    )

    if init_response.status_code != 200:
        print(
            "TIKTOK VIDEO INIT RESPONSE:",
            init_response.text
        )

        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "TikTok не смог "
                    "инициализировать публикацию"
                ),
                "status": init_response.status_code,
                "response": init_response.text
            }
        )

    try:
        init_data = init_response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="TikTok вернул невалидный JSON init"
        ) from exc

    if (
        init_data.get("error", {}).get("code")
        != "ok"
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "error": "TikTok init error",
                "response": init_data
            }
        )

    publish_data = init_data.get(
        "data",
        {}
    )

    publish_id = publish_data.get(
        "publish_id"
    )

    upload_url = publish_data.get(
        "upload_url"
    )

    if not publish_id:
        raise HTTPException(
            status_code=502,
            detail="TikTok не вернул publish_id"
        )

    if not upload_url:
        raise HTTPException(
            status_code=502,
            detail="TikTok не вернул upload_url"
        )

    # ========================================================
    # 3. ЗАГРУЖАЕМ MP4 В TIKTOK
    # ========================================================

    with video_file.open("rb") as video_stream:

        uploaded = 0

        async with httpx.AsyncClient(
            timeout=300.0
        ) as client:

            while uploaded < video_size:

                remaining = video_size - uploaded

                current_chunk_size = min(
                    chunk_size,
                    remaining
                )

                chunk = video_stream.read(
                    current_chunk_size
                )

                if not chunk:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Не удалось прочитать "
                            "часть MP4-файла"
                        )
                    )

                first_byte = uploaded

                last_byte = (
                    uploaded
                    + len(chunk)
                    - 1
                )

                upload_response = await client.put(
                    upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(
                            len(chunk)
                        ),
                        "Content-Range": (
                            f"bytes "
                            f"{first_byte}-"
                            f"{last_byte}/"
                            f"{video_size}"
                        )
                    },
                    content=chunk
                )

                print(
                    "TIKTOK UPLOAD STATUS:",
                    upload_response.status_code,
                    f"{first_byte}-{last_byte}"
                )

                if upload_response.status_code not in (
                    200,
                    201,
                    206
                ):
                    print(
                        "TIKTOK UPLOAD RESPONSE:",
                        upload_response.text
                    )

                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": (
                                "TikTok не принял "
                                "видео"
                            ),
                            "status": (
                                upload_response.status_code
                            ),
                            "response": (
                                upload_response.text
                            )
                        }
                    )

                uploaded += len(chunk)

        # ========================================================
    # 4. ПРОВЕРЯЕМ СТАТУС ПУБЛИКАЦИИ
    # ========================================================

    current_status = None
    status_data = {}

    # Даём TikTok немного времени начать обработку видео
    await asyncio.sleep(3)

    for attempt in range(10):

        async with httpx.AsyncClient(
            timeout=30.0
        ) as client:

            status_response = await client.post(
                "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                headers={
                    "Authorization": (
                        f"Bearer {access_token}"
                    ),
                    "Content-Type": (
                        "application/json; charset=UTF-8"
                    )
                },
                json={
                    "publish_id": publish_id
                }
            )

        print(
            f"TIKTOK PUBLISH STATUS [{attempt + 1}/10]:",
            status_response.status_code
        )

        try:
            status_data = status_response.json()
        except json.JSONDecodeError:
            status_data = {
                "raw": status_response.text
            }

        if status_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "TikTok не вернул статус "
                        "публикации"
                    ),
                    "publish_id": publish_id,
                    "status": status_response.status_code,
                    "response": status_data
                }
            )

        current_status = (
            status_data
            .get("data", {})
            .get("status")
        )

        print(
            "TIKTOK CURRENT STATUS:",
            current_status
        )

        # ----------------------------------------------------
        # Публикация завершена
        # ----------------------------------------------------

        if current_status == "PUBLISH_COMPLETE":
            print(
                "TIKTOK: публикация успешно завершена"
            )
            break

        # ----------------------------------------------------
        # TikTok сообщил об ошибке
        # ----------------------------------------------------

        if current_status in (
            "FAILED",
            "PUBLISH_FAILED"
        ):
            raise HTTPException(
                status_code=502,
                detail={
                    "error": (
                        "TikTok не смог опубликовать видео"
                    ),
                    "publish_id": publish_id,
                    "tiktok_status": current_status,
                    "response": status_data
                }
            )

        # ----------------------------------------------------
        # Видео ещё обрабатывается
        # ----------------------------------------------------

        if attempt < 9:
            print(
                "TIKTOK: видео ещё обрабатывается. "
                "Ждём 3 секунды..."
            )

            await asyncio.sleep(3)

    else:
        # 10 проверок закончились,
        # но окончательного статуса нет
        raise HTTPException(
            status_code=202,
            detail={
                "error": (
                    "TikTok принял видео, "
                    "но ещё не завершил публикацию"
                ),
                "publish_id": publish_id,
                "tiktok_status": current_status,
                "response": status_data
            }
        )

    # ========================================================
    # ГОТОВО
    # ========================================================

    return {
        "status": "success",
        "type": "video",
        "platform": "tiktok",
        "publish_id": publish_id,
        "tiktok_status": current_status,
        "privacy_level": privacy_level,
        "video": {
            "video_id": video.get(
                "video_id"
            ),
            "duration_seconds": video.get(
                "duration_seconds"
            ),
            "size_bytes": video_size
        }
    }

# ============================================================
# YOUTUBE PUBLISH
# ============================================================

@app.post("/api/v1/publish-youtube")
async def publish_youtube(
    payload: PublishRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    account_header: str | None = Header(
        default=None,
        alias="X-Account-Id"
    )
):
    # ========================================================
    # 1. ПРОВЕРЯЕМ ВЫБРАННЫЙ YOUTUBE-АККАУНТ
    # ========================================================

    _prepare_publish_account(
        authorization,
        account_header,
        "youtube"
    )

    account = _selected_account.get()

    if not account:
        raise HTTPException(
            status_code=404,
            detail="YouTube аккаунт не выбран"
        )

    # ========================================================
    # 2. ПОЛУЧАЕМ ЗАШИФРОВАННЫЙ ACCESS TOKEN
    # ========================================================

    encrypted_token = account.get(
    "access_token_encrypted"
    )

    if not encrypted_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "У YouTube аккаунта "
                "отсутствует access token"
            )
        )

    try:
        access_token = (
            _token_cipher()
            .decrypt(
                encrypted_token.encode()
            )
            .decode()
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось расшифровать "
                "YouTube access token"
            )
        ) from exc

    # ========================================================
    # 3. ПОЛУЧАЕМ CONTENT
    # ========================================================

    content = payload.content

    if not isinstance(content, dict):
        raise HTTPException(
            status_code=400,
            detail="Некорректный content"
        )

    # ========================================================
    # 4. ПОЛУЧАЕМ REELS
    # ========================================================

    reels = content.get("reels")

    if not isinstance(reels, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Для публикации в YouTube "
                "нужен формат Reels"
            )
        )

    # ========================================================
# 5. НАХОДИМ ПОСЛЕДНЕЕ СГЕНЕРИРОВАННОЕ ВИДЕО
# ========================================================

    video = reels.get("video")

    if not isinstance(video, dict):

        videos_dir = (
            GENERATED_DIR
            / "videos"
        )

        if not videos_dir.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    "Папка с сгенерированными "
                    "видео не найдена"
                )
            )

        video_files = [
            path
            for path in videos_dir.rglob("*.mp4")
            if path.is_file()
        ]

        if not video_files:
            raise HTTPException(
                status_code=404,
                detail=(
                    "В папке generated/videos "
                    "нет готовых MP4-видео"
                )
            )

        latest_video_file = max(
            video_files,
            key=lambda path: path.stat().st_mtime
        )

        video = {
            "path": str(
                latest_video_file
            )
        }

        reels["video"] = video

        print(
            "YOUTUBE: используется последнее видео:",
            latest_video_file
        )

    # ========================================================
    # 6. ПРОВЕРЯЕМ VIDEO PATH
    # ========================================================

    video_path = str(
        video.get("path", "")
    ).strip()

    if not video_path:
        raise HTTPException(
            status_code=400,
            detail=(
                "У сгенерированного видео "
                "отсутствует path"
            )
        )

    video_file = Path(video_path)

    if not video_file.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Файл видео не найден: "
                f"{video_file}"
            )
        )

    if video_file.suffix.lower() != ".mp4":
        raise HTTPException(
            status_code=400,
            detail="YouTube ожидает MP4-видео"
        )

    video_size = video_file.stat().st_size

    if video_size <= 0:
        raise HTTPException(
            status_code=400,
            detail="Файл видео пуст"
        )

    # ========================================================
    # 7. TITLE
    # ========================================================

    title = str(
        reels.get("hook", "")
    ).strip()

    if not title:
        title = "RespondService Video"

    title = title[:100]

    # ========================================================
    # 8. DESCRIPTION
    # ========================================================

    description_parts = []

    script = reels.get("script")

    if isinstance(script, str):
        script = script.strip()

        if script:
            description_parts.append(
                script
            )

    description = "\n\n".join(
        description_parts
    ).strip()


    # ========================================================
# 9. UPLOAD VIDEO TO YOUTUBE
# ========================================================

    try:

        result = await upload_video_to_youtube(
            video=video,
            access_token=access_token,
            title=title,
            description=description
        )

    except HTTPException as exc:

        # YouTube вернул 401 — access token недействителен
        if exc.status_code != 502:
            raise

        detail = exc.detail

        if not isinstance(detail, dict):
            raise

        if detail.get("status") != 401:
            raise

        print(
            "YOUTUBE: access token недействителен. "
            "Обновляем через refresh token..."
        )

        access_token = (
            await _refresh_youtube_access_token(
                account
            )
        )

        print(
            "YOUTUBE: access token успешно обновлён."
        )

        result = await upload_video_to_youtube(
            video=video,
            access_token=access_token,
            title=title,
            description=description
        )

    # ========================================================
    # 10. RESPONSE
    # ========================================================

    return {
        "status": "success",
        "type": "video",
        "platform": "youtube",
        "youtube": result,
        "video": {
            "video_id": video.get(
                "video_id"
            ),
            "duration_seconds": video.get(
                "duration_seconds"
            ),
            "size_bytes": video_size
        }
    }


# ============================================================
# ROOT
# ============================================================

@app.get("/tiktok-developers-site-verification")
async def tiktok_developers_site_verification():
    return PlainTextResponse(
        "tiktok-developers-site-verification=DgjER65He0yR6sRTCyeZt6ctgylJoi8b"
    )

@app.get("/terms")
async def terms_of_service():
    return HTMLResponse("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RespondService — Terms of Service</title>
</head>
<body>
    <main style="max-width: 800px; margin: 40px auto; padding: 20px; font-family: Arial, sans-serif; line-height: 1.6;">
        <h1>Terms of Service</h1>

        <p>Last updated: September 24, 2026</p>

        <h2>1. Service</h2>
        <p>
            RespondService is a social media management service that helps users
            create, manage and publish content to supported social media platforms.
        </p>

        <h2>2. User Accounts</h2>
        <p>
            Users are responsible for maintaining the security of their accounts
            and for all activity performed through their accounts.
        </p>

        <h2>3. Social Media Accounts</h2>
        <p>
            Users may connect supported social media accounts to RespondService.
            By connecting an account, the user authorizes RespondService to use
            the permissions granted through the relevant social platform's
            authorization process.
        </p>

        <h2>4. Content</h2>
        <p>
            Users are responsible for the content they create, upload or publish
            through RespondService and must comply with applicable laws and the
            terms of the relevant social media platforms.
        </p>

        <h2>5. Prohibited Use</h2>
        <p>
            Users must not use RespondService for unlawful activity, spam,
            impersonation, abuse, or content that violates the rules of
            connected social media platforms.
        </p>

        <h2>6. Third-Party Services</h2>
        <p>
            RespondService may interact with third-party services, including
            social media platforms. Their availability and functionality are
            controlled by those third parties.
        </p>

        <h2>7. Changes to the Service</h2>
        <p>
            We may modify, suspend or discontinue parts of the service when
            reasonably necessary.
        </p>

        <h2>8. Contact</h2>
        <p>
            For questions regarding these Terms of Service, please contact the
            RespondService operator through the contact information provided
            with the service.
        </p>
    </main>
</body>
</html>
    """)


@app.get("/privacy-policy")
async def privacy_policy():
    return HTMLResponse("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RespondService — Privacy Policy</title>
</head>
<body>
    <main style="max-width: 800px; margin: 40px auto; padding: 20px; font-family: Arial, sans-serif; line-height: 1.6;">
        <h1>Privacy Policy</h1>

        <p>Last updated: September 24, 2026</p>

        <h2>1. Information We Process</h2>
        <p>
            RespondService may process account information, authentication
            information, social media account identifiers, access tokens and
            content required to provide the service.
        </p>

        <h2>2. Social Media Connections</h2>
        <p>
            When a user connects a social media account, RespondService receives
            information and permissions provided by the relevant platform during
            the authorization process.
        </p>

        <h2>3. Use of Information</h2>
        <p>
            Information is used to authenticate users, connect authorized social
            media accounts, generate and manage content, and publish content
            according to the user's actions and granted permissions.
        </p>

        <h2>4. Access Tokens</h2>
        <p>
            Social media access credentials are stored using protected storage
            mechanisms and are used only to provide functionality authorized by
            the user.
        </p>

        <h2>5. Third-Party Services</h2>
        <p>
            RespondService communicates with third-party platforms, including
            social media services, when a user connects or uses those services.
            Those platforms process information according to their own privacy
            policies.
        </p>

        <h2>6. Data Retention</h2>
        <p>
            Information is retained only as reasonably necessary to provide the
            service and maintain connected accounts, unless a longer retention
            period is required by law.
        </p>

        <h2>7. Data Deletion</h2>
        <p>
            Users may request deletion of their RespondService account and
            associated data through the service operator.
        </p>

        <h2>8. Changes to This Policy</h2>
        <p>
            This Privacy Policy may be updated when the service or applicable
            requirements change.
        </p>

        <h2>9. Contact</h2>
        <p>
            For privacy-related questions or data deletion requests, please
            contact the RespondService operator through the contact information
            provided with the service.
        </p>
    </main>
</body>
</html>
    """)

@app.get("/tiktokDgjER65He0yR6sRTCyeZt6ctgylJoi8b.txt")
async def tiktok_verification_file():
    return PlainTextResponse(
        "tiktok-developers-site-verification=DgjER65He0yR6sRTCyeZt6ctgylJoi8b"
    )


@app.get("/")
def root():
    return {
        "message": (
            "SMM AI Backend is running. "
            "Go to /docs for API documentation."
        )
    }
