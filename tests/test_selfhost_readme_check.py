from __future__ import annotations

from pathlib import Path

from tools.readme_check import check_bilingual, check_images, check_links, check_readme

REPO_ROOT = Path(__file__).resolve().parents[1]

_EN = "This is the English body."
_CN = "这是中文正文。"


def _doc(sections: list[tuple[str, str]]) -> str:
    parts = ["# capgate · 本地能力网关", "", "English intro.", "", "中文导语。"]
    for heading, body in sections:
        parts += ["", f"## {heading}", "", body]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- 双语覆盖


def test_bilingual_passes_when_both_languages_present():
    text = _doc([("How to read · 怎么读", f"{_EN}\n\n{_CN}")])
    assert check_bilingual(text) == []


def test_bilingual_flags_section_missing_english():
    text = _doc([("How to read · 怎么读", _CN)])
    problems = check_bilingual(text)
    assert len(problems) == 1
    assert problems[0]["message"] == "小节「How to read · 怎么读」正文缺英文"


def test_bilingual_flags_section_missing_chinese():
    text = _doc([("How to read · 怎么读", _EN)])
    problems = check_bilingual(text)
    assert len(problems) == 1
    assert problems[0]["message"] == "小节「How to read · 怎么读」正文缺中文"


def test_bilingual_flags_monolingual_heading():
    text = _doc([("How to read", f"{_EN}\n\n{_CN}")])
    problems = check_bilingual(text)
    assert any(p["message"].startswith("小节标题「How to read」缺中文") for p in problems)


def test_bilingual_reports_line_number():
    text = _doc([("How to read · 怎么读", _CN)])
    problems = check_bilingual(text)
    # 该小节标题那一行，1-based
    assert problems[0]["line"] >= 1
    assert problems[0]["file"] == "README.md"


# ---------------------------------------------------------------- 相对链接


def test_links_flags_broken_relative_link(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ok.md").write_text("# ok\n", encoding="utf-8")
    text = "# capgate\n\n[good](docs/ok.md)\n\n[broken](docs/missing.md)\n"
    problems = check_links(tmp_path, text)
    assert len(problems) == 1
    assert problems[0]["kind"] == "link"
    assert "docs/missing.md" in problems[0]["message"]
    assert problems[0]["line"] == 5


def test_links_ignore_external_and_anchor(tmp_path: Path):
    text = (
        "# capgate\n\n"
        "[ext](https://example.com)\n\n"
        "[anchor](#how-to-read)\n\n"
        "[mail](mailto:x@y.z)\n"
    )
    assert check_links(tmp_path, text) == []


# ---------------------------------------------------------------- 图片存在


def test_images_flags_missing_local_image(tmp_path: Path):
    text = "# capgate\n\n![missing](assets/missing.png)\n"
    problems = check_images(tmp_path, text)
    assert len(problems) == 1
    assert problems[0]["kind"] == "image"
    assert "assets/missing.png" in problems[0]["message"]


def test_images_finds_html_img_tag(tmp_path: Path):
    (tmp_path / "assets").mkdir(parents=True)
    (tmp_path / "assets" / "demo.svg").write_text("<svg/>\n", encoding="utf-8")
    text = '# capgate\n\n<img src="assets/demo.svg" width="880" />\n'
    assert check_images(tmp_path, text) == []


# ---------------------------------------------------------------- 汇总


def test_check_readme_passes_on_real_repo():
    report = check_readme(REPO_ROOT, run_demo_check=False)
    assert report["ok"], report["problems"]
    assert report["checks"]["demo"] == "skipped"


def test_check_readme_reports_missing_readme(tmp_path: Path):
    report = check_readme(tmp_path, run_demo_check=False)
    assert report["ok"] is False
    assert any("README.md 不存在" in p["message"] for p in report["problems"])
