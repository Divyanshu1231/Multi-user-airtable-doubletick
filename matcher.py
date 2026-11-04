import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
import requests

# Always load the .env located next to app.py/matcher.py.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


# ============================================================
# CONFIG
# ============================================================

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
)

# Do not hard-code the proxy URL. Netlify Secrets Scanning checks
# repository files for the configured environment-variable values.
DOUBLETICK_URL = os.getenv("DOUBLETICK_PROXY_URL", "").strip()
DOUBLETICK_PROXY_TOKEN = os.getenv("DOUBLETICK_PROXY_TOKEN", "").strip()

# Safe diagnostic: never print the actual token.
print("DoubleTick proxy URL loaded:", bool(DOUBLETICK_URL))
print("DoubleTick proxy token loaded:", bool(DOUBLETICK_PROXY_TOKEN))

SENT_LOG = Path("data/sent_log.json")
SENT_LOG.parent.mkdir(exist_ok=True)


# ============================================================
# NORMALIZATION
# ============================================================

def norm(value: Any) -> str:

    value = str(value or "").strip().lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value
    )

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def phone(value: Any) -> str:

    digits = re.sub(
        r"\D",
        "",
        str(value or "")
    )

    if len(digits) == 10:
        digits = "91" + digits

    return "+" + digits if digits else ""


# ============================================================
# LOCATION ALIASES
# ============================================================

# State matching is STRICT.
# The selected state must match the candidate's state.
# No cross-state recruitment pool is used.
STATE_MATCH_GROUPS = []

LOCATION_ALIASES = {
    "new delhi": "delhi",
    "delhi ncr": "delhi",
    "ncr": "delhi",
    "greater noida": "noida",
    "uae": "united arab emirates",
    "uk": "united kingdom",
    "usa": "united states",
}


# ============================================================
# SPECIALIZATION ALIASES
# ============================================================

SPECIALIZATION_ALIASES = {
    "cardiology": "cardiologist",
    "interventional cardiology": "interventional cardiologist",
    "interventional cardiologist": "interventional cardiologist",

    "orthopaedics": "orthopedic",
    "orthopaedic": "orthopedic",
    "orthopaedics surgery": "orthopedic",
    "orthopaedic surgery": "orthopedic",
    "orthopedic surgery": "orthopedic",

    "paediatrics": "pediatrician",
    "pediatrics": "pediatrician",

    "general practitioner": "gp",
    "general practice": "gp",

    "gynaecology": "gynecologist",
    "gynecology": "gynecologist",
}


def canonical_location(value):

    value = norm(value)

    return LOCATION_ALIASES.get(
        value,
        value
    )


KNOWN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "delhi", "jammu and kashmir", "ladakh", "puducherry", "chandigarh",
    "andaman and nicobar islands", "dadra and nagar haveli and daman and diu",
    "lakshadweep"
}

STATE_ALIASES = {
    "up": "uttar pradesh",
    "u p": "uttar pradesh",
    "mp": "madhya pradesh",
    "mh": "maharashtra",
    "wb": "west bengal",
    "jk": "jammu and kashmir",
    "jammu kashmir": "jammu and kashmir",
    "orissa": "odisha",
}

def canonical_state(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    value = norm(value)
    return STATE_ALIASES.get(value, value)


def extract_states_from_value(value):
    """Extract canonical states even when Airtable stores a combined location.
    Examples: 'Noida, Uttar Pradesh', 'Uttar Pradesh - India', or ['UP'].
    """
    found = set()
    for raw in state_values(value):
        c = canonical_state(raw)
        if c in KNOWN_STATES:
            found.add(c)
            continue
        padded = f" {c} "
        for state in KNOWN_STATES:
            if f" {state} " in padded:
                found.add(state)
    return found


def state_values(value):

    # Airtable multi-select fields are returned as a list.
    # Example:
    # ["Tamil Nadu", "Karnataka"]
    #
    # A candidate matches if ANY state in the list is selected.

    if value is None:
        return []

    if isinstance(value, list):
        values = []

        for item in value:
            if isinstance(item, dict):
                item = (
                    item.get("name")
                    or item.get("value")
                    or ""
                )

            item = str(item).strip()

            if item:
                values.append(item)

        return values

    if isinstance(value, dict):
        item = (
            value.get("name")
            or value.get("value")
            or ""
        )

        return [str(item).strip()] if str(item).strip() else []

    # Also tolerate comma/semicolon separated strings.
    raw = str(value).strip()

    if not raw:
        return []

    parts = re.split(
        r"[,;]",
        raw
    )

    return [
        part.strip()
        for part in parts
        if part.strip()
    ]


def candidate_has_selected_state(
    candidate_state,
    selected_state_keys
):

    candidate_states = {
        canonical_state(state)
        for state in state_values(candidate_state)
        if canonical_state(state)
    }

    return bool(
        candidate_states.intersection(
            selected_state_keys
        )
    )


def canonical_specialization(value):

    value = norm(value)

    return SPECIALIZATION_ALIASES.get(
        value,
        value
    )


def specialization_values(value):
    if value is None:
        return []

    if isinstance(value, list):
        raw_values = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("value") or ""
            raw_values.append(item)
    elif isinstance(value, dict):
        raw_values = [value.get("name") or value.get("value") or ""]
    else:
        raw_values = re.split(r"[,;/|]", str(value))

    return [
        canonical_specialization(item)
        for item in raw_values
        if canonical_specialization(item)
    ]


# ============================================================
# MATCHING
# ============================================================

def state_match_keys(value):
    """Return ONLY the actual canonical states in the value.

    Important: no state is expanded into another state's recruitment pool.
    Example:
      selected = ["Uttar Pradesh"] -> only Uttar Pradesh candidates match.
      selected = ["Maharashtra"]   -> only Maharashtra candidates match.
    """
    return {
        canonical_state(state)
        for state in state_values(value)
        if canonical_state(state)
    }


def location_match(
    candidate_location,
    vacancy_location
):

    candidate_states = state_match_keys(candidate_location)
    vacancy_states = state_match_keys(vacancy_location)

    if candidate_states and vacancy_states:
        if candidate_states.intersection(vacancy_states):
            return True

    # Fallback for non-state/location values.
    a = canonical_location(candidate_location)
    b = canonical_location(vacancy_location)

    if not a or not b:
        return False

    return a == b or a in b or b in a


def specialization_match(
    candidate_specialization,
    vacancy_specialization
):

    candidate_values = specialization_values(candidate_specialization)
    vacancy_values = specialization_values(vacancy_specialization)

    if not candidate_values or not vacancy_values:
        return False

    for a in candidate_values:
        for b in vacancy_values:
            if a == b or a in b or b in a:
                return True

    return False

    if not a or not b:
        return False

    return (
        a == b
        or a in b
        or b in a
    )


# ============================================================
# AIRTABLE HEADERS
# ============================================================

def airtable_headers(token):

    if not token:

        raise RuntimeError(
            "Airtable token is missing in .env"
        )

    return {
        "Authorization": f"Bearer {token}",
        "User-Agent":
            "SpaceConsultant-VacancyAutomation/6.0",
        "Accept": "application/json",
    }


# ============================================================
# EXTRACT AIRTABLE IDs
# ============================================================

def extract_airtable_ids(url):

    """
    Accept these Airtable URL formats:

    1. https://airtable.com/shrXXXXXXXX
    2. https://airtable.com/appXXXXXXXX/shrXXXXXXXX/tblXXXXXXXX
    3. https://airtable.com/appXXXXXXXX/tblXXXXXXXX/viwXXXXXXXX

    Base ID and Table ID are extracted automatically whenever
    they are present in the URL.

    For a shr-only URL, Base/Table are taken from .env because
    the URL itself does not contain them.
    """

    if not url:
        raise RuntimeError(
            "Airtable Candidate URL is empty."
        )

    clean_url = url.strip()
    clean_url = clean_url.split("?", 1)[0]
    clean_url = clean_url.split("#", 1)[0]

    base_match = re.search(
        r"(app[a-zA-Z0-9]+)",
        clean_url
    )

    table_match = re.search(
        r"(tbl[a-zA-Z0-9]+)",
        clean_url
    )

    share_match = re.search(
        r"(shr[a-zA-Z0-9]+)",
        clean_url
    )

    view_match = re.search(
        r"(viw[a-zA-Z0-9]+)",
        clean_url
    )

    return {
        "base_id":
            base_match.group(1)
            if base_match else "",

        "table_id":
            table_match.group(1)
            if table_match else "",

        "share_id":
            share_match.group(1)
            if share_match else "",

        "view_id":
            view_match.group(1)
            if view_match else "",
    }


def fetch_airtable_view(
    base_id,
    table_id,
    view_id,
    token
):

    if not base_id:

        raise RuntimeError(
            "Airtable Base ID could not be determined."
        )

    if not table_id:

        raise RuntimeError(
            "Airtable Table ID could not be determined."
        )

    if not view_id:

        raise RuntimeError(
            "Airtable View ID (viw...) could not be determined."
        )

    url = (
        f"https://api.airtable.com/v0/"
        f"{base_id}/{table_id}"
    )

    records = []

    offset = None

    while True:

        params = {
            "pageSize": 100,
            "view": view_id,
        }

        if offset:
            params["offset"] = offset

        response = requests.get(
            url,
            headers=airtable_headers(token),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:

            try:
                error = response.json()
            except Exception:
                error = response.text

            raise RuntimeError(
                f"Airtable API error "
                f"{response.status_code}: "
                f"{error}"
            )

        data = response.json()

        for record in data.get(
            "records",
            []
        ):

            fields = dict(
                record.get(
                    "fields",
                    {}
                )
            )

            fields["_record_id"] = record.get(
                "id"
            )

            records.append(fields)

        offset = data.get("offset")

        if not offset:
            break

    return records


# ============================================================
# AIRTABLE API - TABLE RECORDS (NO VIEW ID REQUIRED)
# ============================================================

def fetch_airtable_table(
    base_id,
    table_id,
    token
):

    if not base_id:
        raise RuntimeError(
            "Airtable Base ID is missing."
        )

    if not table_id:
        raise RuntimeError(
            "Airtable Table ID is missing."
        )

    url = (
        f"https://api.airtable.com/v0/"
        f"{base_id}/{table_id}"
    )

    records = []
    offset = None

    while True:

        params = {
            "pageSize": 100
        }

        if offset:
            params["offset"] = offset

        response = requests.get(
            url,
            headers=airtable_headers(token),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:

            try:
                error = response.json()
            except Exception:
                error = response.text

            raise RuntimeError(
                f"Airtable API error "
                f"{response.status_code}: "
                f"{error}"
            )

        data = response.json()

        for record in data.get("records", []):

            fields = dict(
                record.get("fields", {})
            )

            fields["_record_id"] = record.get("id")
            records.append(fields)

        offset = data.get("offset")

        if not offset:
            break

    return records


# ============================================================
# SENT LOG
# ============================================================

def load_sent_log():

    if not SENT_LOG.exists():
        return {}

    try:

        return json.loads(
            SENT_LOG.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return {}


def save_sent_log(data):

    SENT_LOG.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# CANDIDATE FIELDS
# ============================================================

def get_candidate_field(candidate, configured_name, default=""):

    """
    Airtable API field names are matched robustly:
    - exact match
    - case-insensitive match
    - whitespace-normalized match
    - punctuation-normalized match
    """

    configured = str(
        configured_name or ""
    ).strip()

    if configured in candidate:
        return candidate.get(
            configured,
            default
        )

    configured_norm = norm(configured)

    for key, value in candidate.items():

        if key == "_record_id":
            continue

        if norm(key) == configured_norm:
            return value

    return default



def candidate_fields(candidate):
    """Automatically detect common candidate columns from Airtable headers."""
    def auto(aliases):
        # configured override first
        for alias in aliases:
            if not alias:
                continue
            if alias in candidate:
                return candidate.get(alias, "")
        normalized = {norm(k): v for k, v in candidate.items() if k != "_record_id"}
        for alias in aliases:
            na = norm(alias)
            if na in normalized:
                return normalized[na]
        # fuzzy keyword fallback
        for key, value in candidate.items():
            if key == "_record_id":
                continue
            nk = norm(key)
            if any(norm(alias) in nk for alias in aliases if alias):
                return value
        return ""

    return {
        "name": auto([
            os.getenv("CANDIDATE_NAME_FIELD", ""),
            "Name of Candidates", "Candidate Name", "Candidate", "Full Name", "Name"
        ]),
        "phone": auto([
            os.getenv("CANDIDATE_PHONE_FIELD", ""),
            "Mobile No.", "Mobile Number", "Mobile", "Phone Number",
            "Phone", "WhatsApp Number", "WhatsApp", "Contact Number", "Contact"
        ]),
        "specialization": auto([
            os.getenv("CANDIDATE_SPECIALIZATION_FIELD", ""),
            "Specialization", "Specialisation", "Specialty", "Speciality",
            "Department", "Profession", "Job Title"
        ]),
        "state": auto([
            os.getenv("CANDIDATE_STATE_FIELD", ""),
            "Location State", "Candidate State", "Preferred State",
            "Preferred Location State", "Preferred Location (State)",
            "State", "Current State", "Location"
        ]),
        "city": auto([
            os.getenv("CANDIDATE_CITY_FIELD", ""),
            "Preferred Location(City)", "Preferred Location (City)",
            "City", "Location City", "Preferred City"
        ]),
        "location": auto([
            os.getenv("CANDIDATE_STATE_FIELD", ""),
            "Location State", "Candidate State", "Preferred State", "State",
            "Location"
        ]),
        "opt_in": auto([
            os.getenv("CANDIDATE_OPT_IN_FIELD", ""),
            "WhatsApp Opt-In", "WhatsApp Opt In", "Opt In", "Consent"
        ]),
    }



# ============================================================
# VACANCY FIELDS
# ============================================================

def vacancy_fields(vacancy):

    return {

        "specialization": vacancy.get(
            os.getenv(
                "VACANCY_SPECIALIZATION_FIELD",
                "Job Title/ Speciaisation( Group)"
            ),
            ""
        ),

        "state": vacancy.get(
            os.getenv(
                "VACANCY_STATE_FIELD",
                "Location(State)"
            ),
            ""
        ),

        "city": vacancy.get(
            os.getenv(
                "VACANCY_CITY_FIELD",
                "Location (City) & USP"
            ),
            ""
        ),

        "designation": vacancy.get(
            os.getenv(
                "VACANCY_DESIGNATION_FIELD",
                "Post/ Designation"
            ),
            ""
        ),

        "record_id": vacancy.get(
            "_record_id",
            ""
        ),
    }


# ============================================================
# CANDIDATE OPT-IN
# ============================================================

def candidate_allowed(candidate):

    if os.getenv(
        "REQUIRE_OPT_IN",
        "false"
    ).lower() != "true":

        return True

    fields = candidate_fields(candidate)

    return norm(
        fields["opt_in"]
    ) in {
        "yes",
        "true",
        "1",
        "checked",
    }


# ============================================================
# VACANCY AVAILABILITY
# ============================================================

def vacancy_available(vacancy):

    send_field = os.getenv(
        "VACANCY_SEND_FIELD",
        "Send WhatsApp"
    )

    status_field = os.getenv(
        "VACANCY_STATUS_FIELD",
        "WhatsApp Status"
    )

    if os.getenv(
        "REQUIRE_SEND_FLAG",
        "true"
    ).lower() == "true":

        send_value = norm(
            vacancy.get(
                send_field,
                ""
            )
        )

        allowed = {
            "yes",
            "true",
            "1",
            "send",
            "ready",
        }

        if send_value not in allowed:

            return False

    status = norm(
        vacancy.get(
            status_field,
            ""
        )
    )

    if not status:
        return True

    unavailable = {
        "sent",
        "completed",
        "closed",
        "cancelled",
    }

    return status not in unavailable


# ============================================================
# LOCATION MATCH
# ============================================================

def location_matches(
    candidate,
    vacancy
):
    """
    Vacancy-driven state matching. Delhi, Maharashtra and Uttar Pradesh
    are one shared recruitment pool; other states match their own state.
    City is not required for a match.
    """

    c = candidate_fields(candidate)
    v = vacancy_fields(vacancy)

    if not c["state"] or not v["state"]:
        return False

    return location_match(c["state"], v["state"])


# ============================================================
# SPECIALIZATION MATCH
# ============================================================

def specialization_matches(
    candidate,
    vacancy
):

    c = candidate_fields(candidate)
    v = vacancy_fields(vacancy)

    return specialization_match(
        c["specialization"],
        v["specialization"]
    )


def clean_airtable_value(value):

    """
    Airtable can return single-select/multi-select values
    as lists. Convert them to clean strings for WhatsApp
    template placeholders.
    """

    if value is None:
        return ""

    if isinstance(value, list):
        return ", ".join(
            str(item).strip()
            for item in value
            if str(item).strip()
        )

    return str(value).strip()


# ============================================================
# DOUBLETICK
# ============================================================

def doubletick_headers(api_key=""):
    """Headers expected by the Cloudflare DoubleTick proxy."""
    if not DOUBLETICK_PROXY_TOKEN:
        raise RuntimeError(
            "DOUBLETICK_PROXY_TOKEN is missing. "
            "Set it in .env before sending."
        )

    return {
        "X-Proxy-Secret": DOUBLETICK_PROXY_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "SpaceConsultant-CandidateSender/1.0",
    }


def _proxy_template_payload(candidate, template_name, language=None):
    """Build the payload expected by the existing Cloudflare proxy."""
    c = candidate_fields(candidate)

    sender = phone(
        os.getenv("DOUBLETICK_SENDER_NUMBER")
        or os.getenv("DOUBLETICK_WABA_NUMBER")
    )
    recipient = phone(c["phone"])

    if not sender:
        raise RuntimeError("DOUBLETICK_SENDER_NUMBER is missing.")
    if not recipient:
        raise RuntimeError("Candidate phone number is empty.")
    if not template_name or not template_name.strip():
        raise RuntimeError("DoubleTick template name is missing.")

    variables = [
        x.strip()
        for x in os.getenv("WHATSAPP_TEMPLATE_VARIABLES", "").split(",")
        if x.strip()
    ]

    values = {
        "doctor_name": clean_airtable_value(c["name"]),
        "name": clean_airtable_value(c["name"]),
        "specialization": clean_airtable_value(c["specialization"]),
        "city": clean_airtable_value(c["city"]),
        "state": clean_airtable_value(c["state"]),
        "designation": "",
    }

    payload = {
        "to": recipient,
        "from": sender,
        "templateName": template_name.strip(),
        "language": (language or os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")).strip(),
    }

    csv_variables = candidate.get("_template_variables") if isinstance(candidate, dict) else None
    if isinstance(csv_variables, list):
        payload["templateData"] = {
            "body": {"placeholders": [clean_airtable_value(v) for v in csv_variables]}
        }
    elif variables:
        payload["templateData"] = {
            "body": {"placeholders": [values.get(v, "") for v in variables]}
        }

    button_type = os.getenv(
        "WHATSAPP_TEMPLATE_BUTTON_TYPE", ""
    ).strip().upper()
    button_parameter = os.getenv(
        "WHATSAPP_TEMPLATE_BUTTON_PARAMETER", ""
    ).strip()

    if button_type:
        if button_type != "URL":
            raise RuntimeError(
                "WHATSAPP_TEMPLATE_BUTTON_TYPE must be URL."
            )
        if not button_parameter:
            raise RuntimeError(
                "WHATSAPP_TEMPLATE_BUTTON_PARAMETER is missing."
            )
        payload["templateData"] = payload.get("templateData", {})
        payload["templateData"]["buttons"] = [{
            "type": "URL",
            "parameter": button_parameter,
        }]

    return payload


def send_doubletick_candidate_message(candidate, template_name, language=None, header_media_url=''):
    """Send one candidate message through the existing Cloudflare proxy."""
    if not DOUBLETICK_URL:
        raise RuntimeError(
            "DOUBLETICK_PROXY_URL is missing. Set it in Netlify Environment Variables."
        )

    payload = _proxy_template_payload(candidate, template_name, language)
    media_url = str(header_media_url or (candidate.get("_template_header_media_url") if isinstance(candidate, dict) else "") or "").strip()
    if media_url:
        payload.setdefault("templateData", {})["header"] = {"type": "IMAGE", "mediaUrl": media_url}

    try:
        response = requests.post(
            DOUBLETICK_URL,
            headers=doubletick_headers(),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"DoubleTick proxy connection failed: {exc}"
        ) from exc

    raw = response.text or ""
    try:
        body = response.json()
    except Exception:
        body = {"raw": raw}

    if not response.ok:
        # Include the actual proxy response so the UI/terminal can show
        # whether the failure is 401/403/422/502/etc.
        raise RuntimeError(
            f"DoubleTick proxy HTTP {response.status_code}: {body}"
        )

    # Treat explicit provider/message failures as failures even on HTTP 200.
    if isinstance(body, dict):
        message_results = body.get("messages", [])
        if isinstance(message_results, list):
            failed_results = [
                item for item in message_results
                if isinstance(item, dict)
                and (
                    str(item.get("status", "")).upper() == "FAILED"
                    or item.get("errorMessage")
                    or item.get("error")
                )
            ]
            if failed_results:
                first = failed_results[0]
                raise RuntimeError(
                    "DoubleTick message failed: "
                    f"{first.get('errorMessage') or first.get('error') or first}"
                )

        if body.get("success") is False:
            raise RuntimeError(
                f"DoubleTick proxy returned success=false: {body}"
            )

    return body


# Backward-compatible function used by older app.py versions.
def send_doubletick_message(candidate, vacancy, template_name):
    return send_doubletick_candidate_message(candidate, template_name)


# ============================================================
# CANDIDATE URL RESOLUTION + STRICT VIEW READING
# ============================================================

def resolve_candidate_url(candidate_url):
    """Resolve the exact Airtable source represented by the URL.

    IMPORTANT:
    - /app.../tbl.../viw...  -> exact Airtable view
    - /app.../shr.../tbl...  -> exact shared view
    - A shared-view URL is NEVER allowed to fall back to the whole table.
      Doing that would bypass view filters such as the user's Nephro view.
    """
    if not candidate_url or not str(candidate_url).strip():
        raise RuntimeError("Candidate Airtable URL is required.")

    candidate_url = str(candidate_url).strip()
    ids = extract_airtable_ids(candidate_url)

    # Direct view URL: exact view is already in the URL.
    if ids.get("base_id") and ids.get("table_id") and ids.get("view_id"):
        return {
            **ids,
            "source_type": "direct_view",
            "csv_url": "",
        }

    # Shared view URL: recover the hidden viw... and/or the signed CSV URL.
    if ids.get("share_id"):
        try:
            response = requests.get(
                candidate_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/154 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
                timeout=min(REQUEST_TIMEOUT, 10),
                allow_redirects=True,
            )
            html = response.text if response.ok else ""
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not open the Airtable shared view: {exc}"
            ) from exc

        def first_match(patterns):
            for pattern in patterns:
                match = re.search(pattern, html, re.I)
                if match:
                    return match.group(1)
            return ""

        recovered_base = first_match([
            r'(?:applicationId|application_id|baseId|base_id)[^A-Za-z0-9]{0,80}(app[A-Za-z0-9]+)',
        ])
        recovered_table = first_match([
            r'(?:tableId|table_id)[^A-Za-z0-9]{0,80}(tbl[A-Za-z0-9]+)',
        ])
        recovered_view = first_match([
            r'(?:viewId|view_id|viewID|modelId|model_id)[^A-Za-z0-9]{0,80}(viw[A-Za-z0-9]+)',
            r'\b(viw[A-Za-z0-9]{8,})\b',
        ])

        # Airtable's shared-view page can contain a signed downloadCsv URL.
        csv_url = ""
        csv_match = re.search(
            r'https?:(?:\\/|/){2}[^"\']+downloadCsv[^"\']*',
            html,
            re.I,
        )
        if csv_match:
            csv_url = (
                csv_match.group(0)
                .replace("\\/", "/")
                .replace("\\u002f", "/")
                .replace("\\u003d", "=")
                .replace("\\u0026", "&")
                .replace("\\u0025", "%")
            )

        # Also recover an encoded downloadCsv URL when it is stored in JSON.
        if not csv_url:
            encoded_csv = re.search(
                r'(https?%3A(?:%2F|/){2}[^"\']+downloadCsv[^"\']*)',
                html,
                re.I,
            )
            if encoded_csv:
                from urllib.parse import unquote
                csv_url = unquote(encoded_csv.group(1))

        base_id = ids.get("base_id") or recovered_base
        table_id = ids.get("table_id") or recovered_table
        view_id = ids.get("view_id") or recovered_view

        return {
            "base_id": base_id,
            "table_id": table_id,
            "share_id": ids.get("share_id", ""),
            "view_id": view_id,
            "csv_url": csv_url,
            "source_type": "shared_view",
        }

    raise RuntimeError(
        "Invalid Candidate Airtable URL. Use either "
        "/app.../shr.../tbl... (shared view) or /app.../tbl.../viw... (direct view)."
    )


def parse_candidate_csv(text):
    import csv
    from io import StringIO

    # Airtable may prefix UTF-8 CSV with a BOM.
    text = text.lstrip("\\ufeff")
    rows = list(csv.DictReader(StringIO(text)))

    return [
        {**row, "_record_id": f"csv_{i + 1}"}
        for i, row in enumerate(rows)
    ]


def fetch_shared_candidate_csv(ids):
    """Read the records from the ORIGINAL shared view only.

    A shared-view CSV represents the original shared view, so a Nephro view
    remains Nephro here. We deliberately do not use the table API as fallback.
    """
    urls = []

    if ids.get("csv_url"):
        urls.append(ids["csv_url"])

    # Unsigned fallback can work for some publicly downloadable shared views.
    if ids.get("view_id") and ids.get("base_id"):
        urls.append(
            "https://airtable.com/v0.3/view/"
            f"{ids['view_id']}/downloadCsv"
            "?x-time-zone=Asia%2FKolkata"
            f"&x-airtable-application-id={ids['base_id']}"
        )

    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)

        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/154 Safari/537.36"
                    ),
                    "Accept": "text/csv,text/plain,*/*",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            content = response.text or ""
            if not response.ok:
                continue

            # Never parse an HTML login/error page as candidate data.
            first = content.lstrip()[:300].lower()
            if first.startswith("<!doctype html") or "<html" in first:
                continue

            records = parse_candidate_csv(content)
            if records:
                return records
        except requests.RequestException:
            continue

    return []


def fetch_airtable_view(base_id, table_id, view_id, token):
    """Read records from one exact Airtable view."""
    if not base_id:
        raise RuntimeError("Airtable Base ID could not be determined.")
    if not table_id:
        raise RuntimeError("Airtable Table ID could not be determined.")
    if not view_id:
        raise RuntimeError(
            "Airtable View ID could not be determined from the shared URL. "
            "The app will not read the whole table because that would bypass the selected view."
        )

    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    records = []
    offset = None

    while True:
        params = {
            "pageSize": 100,
            "view": view_id,
        }
        if offset:
            params["offset"] = offset

        response = requests.get(
            url,
            headers=airtable_headers(token),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:
            try:
                error = response.json()
            except Exception:
                error = response.text
            raise RuntimeError(
                f"Airtable API error {response.status_code}: {error}"
            )

        data = response.json()
        for record in data.get("records", []):
            fields = dict(record.get("fields", {}))
            fields["_record_id"] = record.get("id")
            records.append(fields)

        offset = data.get("offset")
        if not offset:
            break

    return records


def load_candidate_records_auto(candidate_url, token):
    """Load ONLY the candidate view represented by the supplied URL."""
    ids = resolve_candidate_url(candidate_url)

    # Shared view: first try the shared-view CSV. This avoids requiring the
    # PAT to have base access and, importantly, preserves the shared view.
    if ids.get("source_type") == "shared_view":
        records = fetch_shared_candidate_csv(ids)
        if records:
            return records, ids

        # If CSV was unavailable, use the exact recovered view through the API.
        if ids.get("base_id") and ids.get("table_id") and ids.get("view_id"):
            if not token:
                raise RuntimeError(
                    "Shared-view CSV could not be downloaded and AIRTABLE_TOKEN is missing."
                )
            records = fetch_airtable_view(
                ids["base_id"],
                ids["table_id"],
                ids["view_id"],
                token,
            )
            return records, ids

        # CRITICAL: never fall back to /base/table for a shared view.
        raise RuntimeError(
            "Could not read the exact Airtable shared view. "
            "The view ID could not be recovered and the shared-view CSV is unavailable. "
            "No whole-table fallback was used, so other specializations cannot leak into the results."
        )

    # Direct /app.../tbl.../viw... URL.
    if ids.get("source_type") == "direct_view":
        if not token:
            raise RuntimeError(
                "AIRTABLE_TOKEN is required for a direct Airtable view URL."
            )
        return fetch_airtable_view(
            ids["base_id"],
            ids["table_id"],
            ids["view_id"],
            token,
        ), ids

    raise RuntimeError("Unsupported Candidate Airtable URL.")


def candidate_matches_selected_states(candidate, selected_states):
    cf = candidate_fields(candidate)
    if not selected_states:
        return False

    EMPTY_STATE_KEY = "__empty_state__"
    selected_keys = set()
    for selected in selected_states:
        # norm() removes underscores, so recognize the UI sentinel after
        # normalization rather than comparing it to the raw sentinel string.
        if norm(selected).replace(" ", "_") == "empty_state":
            selected_keys.add(EMPTY_STATE_KEY)
        else:
            state_key = canonical_state(selected)
            if state_key:
                selected_keys.add(state_key)
    include_empty_state = EMPTY_STATE_KEY in selected_keys
    selected_keys.discard(EMPTY_STATE_KEY)
    if not selected_keys and not include_empty_state:
        return False

    # Check the configured state field first. If the view stores a combined
    # location such as "Noida, Uttar Pradesh", extract the state from it.
    values_to_check = [cf.get("state", ""), cf.get("location", ""), cf.get("city", "")]
    candidate_keys = set()
    for value in values_to_check:
        candidate_keys.update(extract_states_from_value(value))

    # Also support an Airtable field whose exact name contains 'state' even
    # when the automatic aliases did not identify it.
    for key, value in candidate.items():
        nk = norm(key)
        if "state" in nk:
            candidate_keys.update(extract_states_from_value(value))

    # “Empty / No state” means no recognizable state could be detected in any
    # of the candidate's state/location/city fields.
    if include_empty_state and not candidate_keys:
        return True
    return bool(candidate_keys.intersection(selected_keys))

def build_candidate_matches(records, selected_states):
    matches = []
    seen = set()

    for candidate in records:
        if not candidate_allowed(candidate):
            continue

        cf = candidate_fields(candidate)

        if not cf["phone"]:
            continue

        # Exact state matching only.
        if not candidate_matches_selected_states(candidate, selected_states):
            continue

        cid = str(
            candidate.get("_record_id")
            or phone(cf["phone"])
        )

        if cid in seen:
            continue
        seen.add(cid)

        matches.append({
            "match_id": cid,
            "candidate_id": cid,
            "candidate": clean_airtable_value(cf["name"]) or "Unnamed candidate",
            "phone": phone(cf["phone"]),
            "specialization": clean_airtable_value(cf["specialization"]),
            "candidate_state": clean_airtable_value(cf["state"]),
            "candidate_city": clean_airtable_value(cf["city"]),
        })

    return matches


def preview_candidates(candidate_url, selected_states):
    if not selected_states:
        raise RuntimeError("Please select at least one state.")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    records, ids = load_candidate_records_auto(candidate_url, token)
    matches = build_candidate_matches(records, selected_states)

    return {
        "candidates_read": len(records),
        "matches_found": len(matches),
        "selected_states": list(selected_states),
        "state_matching": "strict",
        "shared_state_pool": [],
        "candidate_source_detected": {
            "base_id": ids.get("base_id"),
            "table_id": ids.get("table_id"),
            "view_id": ids.get("view_id"),
            "share_id": ids.get("share_id"),
            "source_type": ids.get("source_type"),
        },
        "matches": matches[:2000],
    }


def send_selected_candidates(candidate_url, template_name, selected_states, selected_ids):
    if not selected_states:
        raise RuntimeError("Please select at least one state.")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    records, ids = load_candidate_records_auto(candidate_url, token)
    matches = build_candidate_matches(records, selected_states)
    wanted = {str(x) for x in selected_ids}

    sent = 0
    failed = 0
    details = []

    max_messages = int(os.getenv("MAX_MESSAGES_PER_RUN", "1000"))
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    by_id = {
        str(
            c.get("_record_id")
            or phone(candidate_fields(c)["phone"])
        ): c
        for c in records
    }

    for match in matches:
        if str(match["match_id"]) not in wanted:
            continue
        if sent + failed >= max_messages:
            break

        candidate = by_id.get(str(match["match_id"]))
        if not candidate:
            continue

        try:
            if dry_run:
                status = "DRY_RUN"
            else:
                send_doubletick_candidate_message(
                    candidate,
                    template_name,
                )
                status = "SENT"

            sent += 1
            details.append({**match, "status": status})

        except Exception as exc:
            failed += 1
            error_text = str(exc)
            print(
                f"[DoubleTick FAILED] {match.get('candidate')} "
                f"{match.get('phone')}: {error_text}",
                flush=True,
            )
            details.append({
                **match,
                "status": "FAILED",
                "error": error_text,
            })

    return {
        "selected": len(wanted),
        "sent": sent,
        "failed": failed,
        "details": details,
    }


# ============================================================
# MAIN AUTOMATION
# ============================================================


def run_automation(candidate_url, template_name, selected_states):
    """Backward-compatible candidate-only run.

    No vacancy Airtable is used anywhere in this flow.
    """
    preview = preview_candidates(candidate_url, selected_states)
    return {
        "flow": "candidate_state_matching_only",
        **preview,
    }

