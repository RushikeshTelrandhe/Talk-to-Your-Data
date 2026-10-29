"""Helpers for turning uploaded CSV files or remote dataset links into SQLite.

Everything here is intentionally dependency-free (stdlib + requests) so it works
with the existing requirements.txt.
"""

import csv
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import zipfile
from urllib.parse import parse_qs, unquote, urlparse

import requests

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_CSV_ROWS = 200_000
MAX_HF_ROWS = 5_000
MAX_ZIP_CSVS = 10

INT_RE = re.compile(r"^[+-]?\d+$")
REAL_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

_USER_AGENT = "TalkToYourData/1.0 (+local Flask app)"


def sanitize_identifier(value, fallback="column"):
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"c_{text}"
    return text[:60]


def _unique_identifiers(names):
    used = {}
    result = []
    for index, name in enumerate(names):
        base = sanitize_identifier(name, fallback=f"column_{index + 1}")
        count = used.get(base, 0)
        used[base] = count + 1
        result.append(base if count == 0 else f"{base}_{count + 1}")
    return result


def _unique_identifier(base, used):
    candidate = base
    while candidate in used:
        candidate = f"{base}_{len(used) + 1}"
    used.add(candidate)
    return candidate


def _decode_bytes(data):
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _split_csv(data):
    """Return (headers, rows, delimiter) for a CSV/TSV payload."""
    if b"\x00" in data[:8192]:
        raise ValueError("The file looks binary, not like a CSV.")

    text = _decode_bytes(data)
    if not text.strip():
        raise ValueError("The file is empty.")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise ValueError("The file is empty.")

    headers = [(header or "").strip() for header in raw_headers]
    if not any(headers):
        raise ValueError("Could not find a header row in the file.")

    width = len(headers)
    rows = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        rows.append(row)
        if len(rows) >= MAX_CSV_ROWS:
            break

    return headers, rows, dialect.delimiter


def _normalize_number(value):
    text = value.strip().replace(",", "").replace(" ", "")
    text = re.sub(r"^[\$€£₹]", "", text)
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return text


def _infer_type(rows, column_index):
    inspected = 0
    all_int = True
    all_real = True
    for row in rows:
        if column_index >= len(row):
            continue
        value = row[column_index].strip()
        if not value:
            continue
        inspected += 1
        number = _normalize_number(value)
        if all_int and not INT_RE.match(number):
            all_int = False
        if all_real and not REAL_RE.match(number):
            all_real = False
        if inspected >= 200:
            break

    if inspected == 0:
        return "TEXT"
    if all_int:
        return "INTEGER"
    if all_real:
        return "REAL"
    return "TEXT"


def _convert(value, column_type):
    text = (value or "").strip()
    if text == "" or text.lower() in ("null", "na", "n/a", "nan", "none"):
        return None
    if column_type in ("INTEGER", "REAL"):
        number = _normalize_number(text)
        try:
            return int(number) if column_type == "INTEGER" else float(number)
        except ValueError:
            return None
    return text


def load_payloads_into_db(db_path, payloads):
    """Create a new SQLite database and load each (table_name, csv_bytes) payload.

    Returns a list of table metadata dicts.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    connection = sqlite3.connect(db_path)
    tables = []
    used_names = set()
    try:
        for base_name, data in payloads:
            headers, rows, _ = _split_csv(data)
            original_names = [name or f"column_{i + 1}" for i, name in enumerate(headers)]
            column_names = _unique_identifiers(original_names)
            column_types = [_infer_type(rows, i) for i in range(len(headers))]

            table_name = _unique_identifier(
                sanitize_identifier(base_name, fallback="dataset"), used_names
            )
            columns_sql = ", ".join(
                f'"{name}" {col_type}'
                for name, col_type in zip(column_names, column_types)
            )
            connection.execute(f'CREATE TABLE "{table_name}" ({columns_sql})')

            if rows:
                placeholders = ", ".join("?" for _ in column_names)
                insert_sql = (
                    f'INSERT INTO "{table_name}" '
                    f'({", ".join(chr(34) + c + chr(34) for c in column_names)}) '
                    f"VALUES ({placeholders})"
                )
                converted = [
                    [_convert(cell, col_type) for cell, col_type in zip(row, column_types)]
                    for row in rows
                ]
                for start in range(0, len(converted), 5000):
                    connection.executemany(insert_sql, converted[start:start + 5000])

            tables.append(
                {
                    "table": table_name,
                    "columns": [
                        {"name": name, "type": col_type, "source_name": original}
                        for name, col_type, original in zip(
                            column_names, column_types, original_names
                        )
                    ],
                    "row_count": len(rows),
                }
            )

        connection.commit()
    finally:
        connection.close()

    if not tables:
        if os.path.exists(db_path):
            os.remove(db_path)
        raise ValueError("No usable CSV data was found.")

    return tables


def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// links are supported.")
    host = parsed.hostname
    if not host:
        raise ValueError("The link is missing a host name.")
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        raise ValueError("Local addresses are not allowed.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Could not resolve the host '{host}'.")

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("The link points to a private network address.")


def _download_bytes(url, headers=None, auth=None, timeout=120):
    request_headers = {"User-Agent": _USER_AGENT}
    if headers:
        request_headers.update(headers)

    with requests.get(
        url,
        headers=request_headers,
        auth=auth,
        stream=True,
        timeout=(15, timeout),
        allow_redirects=True,
    ) as response:
        if response.status_code >= 400:
            raise ValueError(_http_error(response))
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
            raise ValueError("The remote file is larger than 100 MB.")
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("The remote file is larger than 100 MB.")
            chunks.append(chunk)
        return b"".join(chunks), response.headers


def _http_error(response):
    if response.status_code == 401:
        return "The source requires authentication (HTTP 401)."
    if response.status_code == 403:
        return "Access denied by the source (HTTP 403). Make sure the file is public."
    if response.status_code == 404:
        return "The source was not found (HTTP 404)."
    return f"The source returned HTTP {response.status_code}."


def _csv_payloads(data, default_name):
    """Turn raw bytes (CSV or ZIP of CSVs) into (table_name, csv_bytes) tuples."""
    if not data:
        raise ValueError("The source returned an empty file.")

    if zipfile.is_zipfile(io.BytesIO(data)):
        payloads = []
        total_uncompressed = 0
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith((".csv", ".tsv"))
                and not info.filename.startswith("__MACOSX/")
                and not os.path.basename(info.filename).startswith(".")
            ]
            if not candidates:
                raise ValueError("The archive does not contain any CSV files.")
            for info in candidates[:MAX_ZIP_CSVS]:
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_DOWNLOAD_BYTES * 2:
                    raise ValueError("The archive is too large.")
                name = os.path.splitext(os.path.basename(info.filename))[0]
                payloads.append((name, archive.read(info)))
        if not payloads:
            raise ValueError("The archive does not contain any CSV files.")
        return payloads

    if b"\x00" in data[:8192]:
        raise ValueError("The source is not a CSV file.")

    return [(default_name, data)]


def _kaggle_credentials():
    username = os.environ.get("KAGGLE_USERNAME")
    key = os.environ.get("KAGGLE_KEY")
    if username and key:
        return username, key

    path = os.path.expanduser("~/.kaggle/kaggle.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                creds = json.load(handle)
            if creds.get("username") and creds.get("key"):
                return creds["username"], creds["key"]
        except (OSError, ValueError):
            pass
    return None


def _fetch_google(url):
    sheet = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if sheet:
        file_id = sheet.group(1)
        export_url = (
            f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
        )
        data, _ = _download_bytes(export_url)
        return "google_sheets", file_id, _csv_payloads(data, "google_sheet")

    match = re.search(r"/file/d/([A-Za-z0-9_-]+)", url) or re.search(
        r"[?&]id=([A-Za-z0-9_-]+)", url
    )
    if not match:
        raise ValueError("Could not find a Google Drive file id in that link.")
    file_id = match.group(1)
    base_url = f"https://drive.google.com/uc?export=download&id={file_id}"

    response = requests.get(
        base_url, headers={"User-Agent": _USER_AGENT}, timeout=60, stream=False
    )
    content_type = response.headers.get("Content-Type", "")
    content = response.content

    if response.status_code >= 400:
        raise ValueError(_http_error(response))
    length = response.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
        raise ValueError("The Google Drive file is larger than 100 MB.")

    if "text/html" in content_type:
        text = _decode_bytes(content)
        token_match = re.search(r"confirm=([0-9A-Za-z_-]+)", text) or re.search(
            r'name="uuid" value="([^"]+)"', text
        )
        if token_match:
            confirm_url = f"{base_url}&confirm={token_match.group(1)}"
            content, download_headers = _download_bytes(confirm_url)
            content_type = download_headers.get("Content-Type", "")
        else:
            raise ValueError(
                "Google Drive returned a web page instead of a file. "
                "Make sure the file is shared as 'Anyone with the link'."
            )

    if "text/html" in content_type:
        raise ValueError(
            "Google Drive returned a web page instead of a file. "
            "Make sure the file is shared as 'Anyone with the link'."
        )
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("The Google Drive file is larger than 100 MB.")

    return "google_drive", file_id, _csv_payloads(content, "google_drive")


def _cell_to_text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _fetch_huggingface(url):
    match = re.search(r"huggingface\.co/datasets/([^/?#]+/[^/?#]+)", url)
    if not match:
        raise ValueError("Could not find a Hugging Face dataset id in that link.")
    repo_id = match.group(1).removesuffix(".git")
    parts = repo_id.split("/")
    if len(parts) == 2 and parts[1] in ("viewer", "tree", "blob", "resolve"):
        repo_id = parts[0]

    headers = {}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    query = parse_qs(urlparse(url).query)
    config = (query.get("config") or [None])[0]
    split = (query.get("split") or [None])[0]

    splits_response = requests.get(
        "https://datasets-server.huggingface.co/splits",
        params={"dataset": repo_id},
        headers={"User-Agent": _USER_AGENT, **headers},
        timeout=60,
    )
    if splits_response.status_code == 401:
        raise ValueError(
            "This Hugging Face dataset is private. Set an HF_TOKEN environment variable."
        )
    if splits_response.status_code >= 400:
        raise ValueError(_http_error(splits_response))

    payload = splits_response.json()
    splits = payload.get("splits") or []
    if not splits:
        raise ValueError(
            payload.get("error")
            or "No viewer splits are available for this Hugging Face dataset."
        )

    chosen = None
    for entry in splits:
        if config and entry.get("config") != config:
            continue
        if split and entry.get("split") != split:
            continue
        chosen = entry
        break
    if chosen is None:
        chosen = splits[0]

    chosen_config = chosen.get("config")
    chosen_split = chosen.get("split")
    rows_url = "https://datasets-server.huggingface.co/rows"

    offset = 0
    headers_out = None
    output_rows = []
    while offset < MAX_HF_ROWS:
        page_response = requests.get(
            rows_url,
            params={
                "dataset": repo_id,
                "config": chosen_config,
                "split": chosen_split,
                "offset": offset,
                "length": 100,
            },
            headers={"User-Agent": _USER_AGENT, **headers},
            timeout=90,
        )
        if page_response.status_code >= 400:
            raise ValueError(_http_error(page_response))
        page = page_response.json()
        if headers_out is None:
            features = page.get("features") or []
            headers_out = [feature["name"] for feature in features]
            if not headers_out:
                raise ValueError(
                    page.get("error")
                    or "Could not read columns for this Hugging Face dataset."
                )
        page_rows = page.get("rows") or []
        if not page_rows:
            break
        for item in page_rows:
            row = item.get("row") or {}
            output_rows.append([_cell_to_text(row.get(name)) for name in headers_out])
            if len(output_rows) >= MAX_HF_ROWS:
                break
        offset += len(page_rows)
        total = page.get("num_rows_total") or 0
        if offset >= total or len(page_rows) < 100:
            break

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers_out)
    writer.writerows(output_rows)
    table_name = repo_id.split("/")[-1]
    return "huggingface", repo_id, [(table_name, buffer.getvalue().encode("utf-8"))]


def _fetch_kaggle(url):
    match = re.search(r"kaggle\.com/datasets/([^/?#]+)/([^/?#]+)", url)
    if not match:
        raise ValueError("Could not find a Kaggle dataset id in that link.")
    owner, slug = match.group(1), match.group(2)
    slug = slug.removesuffix(".zip")

    credentials = _kaggle_credentials()
    if not credentials:
        raise ValueError(
            "Kaggle downloads need API credentials. Set KAGGLE_USERNAME and "
            "KAGGLE_KEY, or place kaggle.json in ~/.kaggle/, then try again. "
            "You can also download the CSV from Kaggle and upload it here."
        )

    download_url = (
        f"https://www.kaggle.com/api/v1/datasets/download/{owner}/{slug}"
    )
    data, _ = _download_bytes(
        download_url, auth=credentials, timeout=300
    )
    return "kaggle", f"{owner}/{slug}", _csv_payloads(data, slug)


def _fetch_generic(url):
    data, headers = _download_bytes(url)
    content_type = headers.get("Content-Type", "")
    if "text/html" in content_type and not url.lower().endswith(".csv"):
        raise ValueError(
            "That link returned a web page, not a CSV. Use a direct download link."
        )
    path = urlparse(url).path
    name = os.path.splitext(os.path.basename(unquote(path)))[0] or "dataset"
    return "url", url, _csv_payloads(data, name)


def fetch_from_url(url):
    """Fetch a dataset from a supported link.

    Returns (source_type, source_ref, payloads, suggested_name).
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Please provide a dataset link.")
    if len(url) > 2000:
        raise ValueError("That link is too long.")
    validate_public_url(url)

    lowered = url.lower()
    if "drive.google.com" in lowered or "docs.google.com" in lowered:
        source_type, source_ref, payloads = _fetch_google(url)
    elif "kaggle.com" in lowered:
        source_type, source_ref, payloads = _fetch_kaggle(url)
    elif "huggingface.co" in lowered:
        source_type, source_ref, payloads = _fetch_huggingface(url)
    else:
        source_type, source_ref, payloads = _fetch_generic(url)

    suggested_name = source_ref
    if payloads:
        suggested_name = payloads[0][0] or suggested_name
    return source_type, source_ref, payloads, suggested_name
