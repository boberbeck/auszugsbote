"""Winziger PDF-Erzeuger ohne Abhängigkeiten – nur für den Demo-Modus."""
from __future__ import annotations


def _esc(s: str) -> bytes:
    b = s.encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def simple_pdf(lines: list[tuple[int, str]], title: str = "Demo") -> bytes:
    """lines: Liste aus (Schriftgröße, Text). Eine A4-Seite, Helvetica, WinAnsi."""
    content = [b"BT", b"/F1 11 Tf", b"56 790 Td"]
    first = True
    for size, text in lines:
        if not first:
            content.append(f"0 -{int(size * 1.6)} Td".encode())
        content.append(f"/F1 {size} Tf".encode())
        content.append(b"(" + _esc(text) + b") Tj")
        first = False
    content.append(b"ET")
    stream = b"\n".join(content)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >>"
        b" /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Title (" + _esc(title) + b") /Producer (Auszugsbote Demo) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
