"""IMO numbers of vessels named on official RFMO IUU vessel lists.

These numbers are used only as *search seeds*: they tell the ingestion which
vessels to look up. Whether a vessel counts as IUU-listed in the evaluation is
decided separately and uniformly for every vessel by the Global Fishing Watch
IUU-list indicator, so a stray number picked up here cannot create a false label.

Each source below is the organisation's own public list. IOTC and CCSBT cross-list
vessels from the other tuna bodies, so together they cover most current listings.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

USER_AGENT = "Mozilla/5.0 (compatible; fisheries-patrol-planner)"


@dataclass(frozen=True)
class ListSource:
    organisation: str
    page_url: str
    # When set, the list itself is a document linked from the page.
    document_pattern: str | None = None


SOURCES = (
    ListSource(
        "IOTC",
        "https://iotc.org/iotc-iuu-list",
        r'href="([^"]*IUU[^"]*List[^"]*\.pdf)"',
    ),
    ListSource(
        "CCSBT",
        "https://www.ccsbt.org/sites/default/files/userfiles/file/docs_english/CCSBT%20IUU%20List.pdf",
    ),
    ListSource(
        "CCAMLR (non-contracting party list)",
        "https://www.ccamlr.org/en/compliance/non-contracting-party-iuu-vessel-list",
    ),
    ListSource(
        "CCAMLR (contracting party list)",
        "https://www.ccamlr.org/en/compliance/contracting-party-iuu-vessel-list",
    ),
)


def is_valid_imo(number: str) -> bool:
    """IMO ship numbers carry a check digit: digits 1-6 weighted 7..2, last digit of the sum."""
    if not re.fullmatch(r"\d{7}", number):
        return False
    digits = [int(c) for c in number]
    return sum(d * (7 - i) for i, d in enumerate(digits[:6])) % 10 == digits[6]


def extract_imo_numbers(text: str) -> list[str]:
    candidates = re.findall(r"(?<!\d)(\d{7})(?!\d)", text)
    return sorted({c for c in candidates if is_valid_imo(c)})


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def html_text(content: bytes) -> str:
    html = content.decode("utf-8", errors="ignore")
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"<[^>]+>", " ", html)


def fetch_seed_imos(session: requests.Session | None = None, log=print) -> list[dict]:
    """Download each official list and return one record per (IMO number, source)."""
    session = session or requests.Session()
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records: list[dict] = []
    for source in SOURCES:
        try:
            url, content = _download(session, source)
            is_pdf = content[:5] == b"%PDF-"
            numbers = extract_imo_numbers(pdf_text(content) if is_pdf else html_text(content))
        except Exception as error:  # one unreachable site must not stop the others
            log(f"  could not read the {source.organisation} list: {error}")
            continue
        log(f"  {source.organisation}: {len(numbers)} IMO numbers")
        records.extend(
            {"imo": n, "organisation": source.organisation, "url": url, "retrieved": retrieved}
            for n in numbers
        )
    return records


def _download(session: requests.Session, source: ListSource) -> tuple[str, bytes]:
    response = session.get(source.page_url, headers={"User-Agent": USER_AGENT}, timeout=90)
    response.raise_for_status()
    if not source.document_pattern:
        return source.page_url, response.content
    match = re.search(source.document_pattern, response.text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("list document link not found on the page")
    url = requests.compat.urljoin(source.page_url, match.group(1))
    document = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
    document.raise_for_status()
    return url, document.content
