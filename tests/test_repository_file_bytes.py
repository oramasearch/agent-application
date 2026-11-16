# SPDX-License-Identifier: MIT

import base64

from agent_application.agent import exact_file_text


def lake_escaped(text: str) -> str:
    """What Lake's `contents` answers today (lake#1238): each non-ASCII byte as literal \\xNN."""
    return "".join(
        c if ord(c) < 128 else "".join(f"\\x{b:02X}" for b in c.encode()) for c in text
    )


def test_contents_come_from_the_exact_bytes():
    text = 'st.title("👥 Customer Insights")\nst.caption("Rows · 10,000")\r\n'
    value = {"contents": lake_escaped(text), "content_base64": base64.b64encode(text.encode()).decode()}
    assert "\\xF0\\x9F\\x91\\xA5" in value["contents"]
    assert exact_file_text(value)["contents"] == text


def test_a_binary_file_has_no_contents():
    data = b"\x89PNG\r\n\x1a\n\xff\xfe"
    value = {"contents": "\\x89PNG", "content_base64": base64.b64encode(data).decode()}
    assert "contents" not in exact_file_text(value)


def test_an_answer_without_base64_is_left_alone():
    assert exact_file_text({"contents": "plain"}) == {"contents": "plain"}


def test_the_lake_client_reads_repository_files_exactly(monkeypatch):
    from agent_application.agent import Lake

    text = "# 👥 · café\n"
    answer = {"contents": lake_escaped(text), "content_base64": base64.b64encode(text.encode()).decode()}
    lake = Lake.__new__(Lake)
    monkeypatch.setattr(Lake, "call", lambda self, capability, arguments, **_: dict(answer))
    assert lake.read("repository_file", {"id": "r", "path": "app.py"})["contents"] == text
    assert lake.read("get_app", {"app_id": "a"}) == answer
