"""Safe handling of uploaded files for multimodal chat.

Nothing is written to disk. Each file is read into memory, identified by its
real content (not just its name), turned into text or an image data URL and
then dropped.
"""
import base64
import html
import io
import re
import zipfile

MAX_FILES = 6
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_DOC_CHARS = 12000
MAX_TOTAL_CHARS = 30000
MAX_LIBRARY_CHARS = 60000
MAX_PDF_PAGES = 40
MAX_XML_BYTES = 10 * 1024 * 1024
TEXT_EXTENSIONS = (".txt", ".md", ".csv")
PARA_RE = re.compile(rb"\x3c/w:p\x3e")
TAG_RE = re.compile(rb"\x3c[^\x3e]*\x3e")
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff(raw, name):
    """Return image/png etc, pdf, docx, text, or None for the real type."""
    for prefix, mime in MAGIC:
        if raw.startswith(prefix):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"%PDF-"):
        return "pdf"
    low = name.lower()
    if raw.startswith(b"PK") and low.endswith(".docx"):
        return "docx"
    if low.endswith(TEXT_EXTENSIONS) and b"\x00" not in raw[:2048]:
        return "text"
    return None


def docx_text(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_XML_BYTES:
            raise ValueError("document too large")
        xml = archive.read(info)
    xml = PARA_RE.sub(b"\n", xml)
    return html.unescape(TAG_RE.sub(b"", xml).decode("utf-8", "replace"))


def pdf_text(raw):
    from pypdf import PdfReader  # imported late so the app starts without it
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF")
    return "\n".join(
        (page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES]
    )


def extract(kind, raw):
    if kind == "pdf":
        return pdf_text(raw)
    if kind == "docx":
        return docx_text(raw)
    return raw.decode("utf-8", "replace")


def process_uploads(uploads):
    """Return images (data URLs), document text, file names and warnings."""
    out = {"images": [], "docs": "", "names": [], "warnings": [], "items": []}
    total = 0
    if len(uploads) > MAX_FILES:
        out["warnings"].append("Only the first 6 files were used.")
    for upload in uploads[:MAX_FILES]:
        name = re.sub(r"[^\w .()-]", "_", upload.filename or "file")[:80]
        raw = upload.read(MAX_FILE_BYTES + 1)
        kind = None
        if not raw:
            out["warnings"].append(name + ": the file is empty.")
        elif len(raw) > MAX_FILE_BYTES:
            out["warnings"].append(name + ": larger than 8 MB, skipped.")
        else:
            kind = sniff(raw, name)
            if kind is None:
                out["warnings"].append(name + ": unsupported file type.")
        if kind is None:
            continue
        if kind.startswith("image/"):
            if len(raw) > MAX_IMAGE_BYTES:
                out["warnings"].append(name + ": image larger than 4 MB.")
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            out["images"].append("data:" + kind + ";base64," + encoded)
            out["names"].append(name)
            continue
        try:
            text = extract(kind, raw).strip()
        except ImportError:
            out["warnings"].append(name + ": PDF support is not installed.")
            continue
        except Exception:  # parsers raise many different errors
            out["warnings"].append(name + ": could not read this file.")
            continue
        if text:
            out["items"].append(
                {"name": name, "kind": kind, "text": text[:MAX_LIBRARY_CHARS]}
            )
        room = MAX_TOTAL_CHARS - total
        if not text:
            out["warnings"].append(name + ": no readable text found.")
        elif room <= 0:
            out["warnings"].append(name + ": skipped, too much text.")
        else:
            text = text[:min(MAX_DOC_CHARS, room)]
            total += len(text)
            out["docs"] += "\n--- file: " + name + " ---\n" + text + "\n"
            out["names"].append(name)
    return out
