"""보도자료 파일에서 텍스트를 뽑아내는 모듈.

지원: PDF, DOCX, TXT/MD/CSV, HTML, HWP(5.x), HWPX, 이미지(png/jpg/webp)
텍스트 추출이 불가능한 파일(스캔 PDF, 이미지)은 (빈 텍스트, 원본 바이트)를 돌려주어
호출한 쪽에서 Gemini 비전으로 읽게 합니다.
"""
import io
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

SUPPORTED_EXTS = [
    "pdf", "docx", "txt", "md", "csv", "html", "htm",
    "hwp", "hwpx", "png", "jpg", "jpeg", "webp",
]

IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class ExtractResult:
    def __init__(self, text="", media=None, note=""):
        self.text = (text or "").strip()
        self.media = media  # (mime, bytes) 또는 None
        self.note = note    # 사용자에게 보여줄 안내문


def _decode_text_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- PDF
def _extract_pdf(data: bytes) -> ExtractResult:
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
            text = "\n".join(pages)
    except Exception:
        text = ""
    if len(text.strip()) < 30:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for p in reader.pages:
                t = p.extract_text()
                if t:
                    pages.append(t)
            alt = "\n".join(pages)
            if len(alt.strip()) > len(text.strip()):
                text = alt
        except Exception:
            pass
    if len(text.strip()) < 30:
        return ExtractResult(
            "", ("application/pdf", data),
            "글자를 읽을 수 없는 스캔본 PDF로 보입니다. 생성 시 AI가 직접 문서를 읽습니다.",
        )
    return ExtractResult(text)


# ---------------------------------------------------------------- DOCX
def _extract_docx(data: bytes) -> ExtractResult:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    lines = []
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            t = Paragraph(child, document).text.strip()
            if t:
                lines.append(t)
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                seen = []
                for cell in row.cells:
                    ct = cell.text.strip()
                    if ct and ct not in seen:  # 병합 셀 중복 제거
                        seen.append(ct)
                if seen:
                    lines.append(" | ".join(seen))
    return ExtractResult("\n".join(lines))


# ---------------------------------------------------------------- HWPX
def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _walk_hwpx(el, buf):
    name = _local(el.tag)
    if name == "t":
        buf.append(el.text or "")
    for ch in el:
        cname = _local(ch.tag)
        if name == "t" and cname in ("lineBreak", "tab"):
            buf.append(" ")
        else:
            _walk_hwpx(ch, buf)
        if name == "t":
            buf.append(ch.tail or "")
    if name == "p":
        buf.append("\n")


def _extract_hwpx(data: bytes) -> ExtractResult:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sections = [
            n for n in z.namelist()
            if re.match(r"Contents/section\d+\.xml$", n, re.I)
        ]
        sections.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for name in sections:
            try:
                root = ET.fromstring(z.read(name))
            except ET.ParseError:
                xml = z.read(name).decode("utf-8", errors="ignore")
                out.append(re.sub(r"<[^>]+>", " ", xml))
                continue
            buf = []
            _walk_hwpx(root, buf)
            out.append("".join(buf))
        if not out and "Preview/PrvText.txt" in z.namelist():
            out.append(_decode_text_bytes(z.read("Preview/PrvText.txt")))
    text = re.sub(r"[ \t]+", " ", "\n".join(out))
    text = re.sub(r"\n\s*\n+", "\n", text)
    return ExtractResult(text)


# ---------------------------------------------------------------- HWP 5.x
# 확장 컨트롤/인라인 컨트롤은 8 WCHAR(16바이트)를 차지한다.
_HWP_EXT_CTRL = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


def _decode_hwp_para(body: bytes) -> str:
    out = []
    n = len(body) // 2
    i = 0
    while i < n:
        code = struct.unpack_from("<H", body, i * 2)[0]
        if code < 32:
            if code in (10, 13):
                out.append("\n")
            elif code == 9:
                out.append(" ")
                i += 7
            elif code in _HWP_EXT_CTRL:
                i += 7
            elif code in (24, 30, 31):
                out.append(" " if code != 24 else "-")
            i += 1
            continue
        if 0xD800 <= code <= 0xDFFF:  # 서로게이트: 쌍으로 처리
            if i + 1 < n:
                low = struct.unpack_from("<H", body, (i + 1) * 2)[0]
                try:
                    out.append(struct.pack("<HH", code, low).decode("utf-16le"))
                except UnicodeDecodeError:
                    pass
                i += 2
                continue
        elif not (0xE000 <= code <= 0xF8FF):  # 사설 영역 글자는 제외
            out.append(chr(code))
        i += 1
    return "".join(out)


def parse_hwp_records(raw: bytes) -> str:
    """HWP BodyText 섹션(압축 해제 후)의 레코드에서 문단 텍스트를 모은다."""
    paragraphs = []
    i = 0
    total = len(raw)
    while i + 4 <= total:
        header = struct.unpack_from("<I", raw, i)[0]
        i += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if i + 4 > total:
                break
            size = struct.unpack_from("<I", raw, i)[0]
            i += 4
        body = raw[i:i + size]
        i += size
        if tag == 67:  # HWPTAG_PARA_TEXT
            t = _decode_hwp_para(body).strip()
            if t:
                paragraphs.append(t)
    return "\n".join(paragraphs)


def _extract_hwp(data: bytes) -> ExtractResult:
    import olefile

    if zipfile.is_zipfile(io.BytesIO(data)):  # 확장자만 hwp인 hwpx
        return _extract_hwpx(data)
    if not olefile.isOleFile(io.BytesIO(data)):
        raise ValueError("HWP 5.x 형식이 아닙니다(구버전 HWP 3.0이거나 손상된 파일).")

    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        compressed = bool(flags & 0x1)
        encrypted = bool(flags & 0x2)
        dist = bool(flags & 0x4)  # 배포용 문서

        if encrypted:
            raise ValueError("암호가 걸린 HWP 파일입니다. 암호를 해제하거나 본문을 붙여넣어 주세요.")

        sections = [
            e for e in ole.listdir()
            if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")
        ]
        sections.sort(key=lambda e: int(re.findall(r"\d+", e[1])[0]))

        texts = []
        if not dist:
            for entry in sections:
                raw = ole.openstream(entry).read()
                if compressed:
                    raw = zlib.decompress(raw, -15)
                texts.append(parse_hwp_records(raw))
        text = "\n".join(t for t in texts if t).strip()

        note = ""
        if not text and ole.exists("PrvText"):
            # 배포용 문서 등 본문을 열 수 없을 때: 미리보기 텍스트(앞부분만 제공됨)
            text = ole.openstream("PrvText").read().decode("utf-16le", errors="ignore").strip()
            note = "본문 전체를 읽지 못해 미리보기 텍스트(앞부분)만 가져왔습니다. 전체 내용을 확인해 주세요."
        return ExtractResult(text, note=note)
    finally:
        ole.close()


# ---------------------------------------------------------------- 기타
def _extract_html(data: bytes) -> ExtractResult:
    html = _decode_text_bytes(data)
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    import html as html_lib
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return ExtractResult(text)


def extract(file_name: str, data: bytes) -> ExtractResult:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext == "pdf":
        return _extract_pdf(data)
    if ext == "docx":
        return _extract_docx(data)
    if ext == "hwpx":
        return _extract_hwpx(data)
    if ext == "hwp":
        return _extract_hwp(data)
    if ext in ("txt", "md", "csv"):
        return ExtractResult(_decode_text_bytes(data))
    if ext in ("html", "htm"):
        return _extract_html(data)
    if ext in IMAGE_MIME:
        return ExtractResult(
            "", (IMAGE_MIME[ext], data),
            "이미지 파일입니다. 생성 시 AI가 이미지 속 글자를 직접 읽습니다.",
        )
    raise ValueError(f"지원하지 않는 형식입니다: .{ext}")
