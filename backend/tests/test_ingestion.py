"""文档入库文本提取（load_file_text）：编码容错 + PDF 解析。

回归 2026-10-20 上传 500：
- txt/md 此前强制 utf-8，GBK 文档直接 UnicodeDecodeError -> 裸 500；
- PDF 此前依赖 langchain_community（生产 venv 未装），import 失败 -> 裸 500。
零外部依赖；pypdf 未安装（未选 loaders extra）时跳过 PDF 用例。
"""

from pathlib import Path

import pytest

from dmf_agent.rag.ingestion import load_file_text


def test_txt_utf8_and_md(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("GMV 是商品交易总额", encoding="utf-8")
    text, kind = load_file_text(p)
    assert "GMV" in text and kind == "txt"

    m = tmp_path / "b.md"
    m.write_text("# 标题\n正文", encoding="utf-8")
    text, kind = load_file_text(m)
    assert kind == "md" and "标题" in text


def test_txt_gbk_and_utf8_bom(tmp_path: Path):
    # GBK（中文 Windows 常见）此前会 500，现按 gb18030 回退解码
    p = tmp_path / "gbk.txt"
    p.write_bytes("GMV 是商品交易总额".encode("gbk"))
    text, _ = load_file_text(p)
    assert "商品交易总额" in text

    # 带 BOM 的 UTF-8
    b = tmp_path / "bom.txt"
    b.write_bytes("﻿GMV".encode("utf-8"))  # 含 BOM
    text, _ = load_file_text(b)
    assert "GMV" in text and not text.startswith("﻿")


def test_unsupported_suffix(tmp_path: Path):
    p = tmp_path / "a.docx"
    p.write_bytes(b"x")
    with pytest.raises(ValueError):
        load_file_text(p)


def _minimal_pdf(text: str = "Hello GMV") -> bytes:
    """构造 pypdf 可提取文本的最小单页 PDF。"""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len('BT /F1 24 Tf 72 720 Td (' + text + ') Tj ET')} >>\n"
        f"stream\nBT /F1 24 Tf 72 720 Td ({text}) Tj ET\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_extract(tmp_path: Path):
    pytest.importorskip("pypdf")  # 未装 loaders extra 时跳过
    p = tmp_path / "a.pdf"
    p.write_bytes(_minimal_pdf())
    text, kind = load_file_text(p)
    assert kind == "pdf" and "Hello GMV" in text


def test_pdf_no_text_raises(tmp_path: Path):
    pytest.importorskip("pypdf")
    # 无文本内容流的 PDF（空白页）应明确报错，而不是入库空文档
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        "<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    p = tmp_path / "blank.pdf"
    p.write_bytes(out)
    with pytest.raises(ValueError):
        load_file_text(p)
