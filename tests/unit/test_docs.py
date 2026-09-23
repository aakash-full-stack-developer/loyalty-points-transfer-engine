"""The documentation stays true to the code: diagrams, cited tests and links cannot drift."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
DIAGRAMS = sorted((DOCS / "diagrams").glob("*.mmd"))


def _test_names() -> set[str]:
    pattern = re.compile(r"^(?:async )?def (test_\w+)", re.MULTILINE)
    return {
        name
        for path in (ROOT / "tests").rglob("test_*.py")
        for name in pattern.findall(path.read_text())
    }


def test_every_diagram_has_a_source_file() -> None:
    assert {path.stem for path in DIAGRAMS} == {
        "component",
        "transfer-success",
        "transfer-failure",
        "transfer-states",
        "database",
    }


@pytest.mark.parametrize("source", DIAGRAMS, ids=lambda path: path.stem)
def test_architecture_page_embeds_each_diagram_exactly(source: Path) -> None:
    page = (DOCS / "architecture.md").read_text()
    marker = f"<!-- diagram: {source.stem} -->\n```mermaid\n"
    assert marker in page, f"architecture.md has no block for {source.stem}"
    embedded = page.split(marker, 1)[1].split("\n```", 1)[0]
    assert embedded.strip() == source.read_text().strip(), (
        f"architecture.md and docs/diagrams/{source.name} differ; update both, "
        "then run `make diagrams`"
    )


@pytest.mark.parametrize("source", DIAGRAMS, ids=lambda path: path.stem)
def test_every_diagram_has_been_exported(source: Path) -> None:
    for extension in ("svg", "png"):
        assert (DOCS / "images" / f"{source.stem}.{extension}").exists(), "run `make diagrams`"


@pytest.mark.parametrize("document", ["failure-scenarios.md", "testing.md"])
def test_every_test_cited_in_the_docs_exists(document: str) -> None:
    cited = set(re.findall(r"`(test_\w+)`", (DOCS / document).read_text()))
    missing = cited - _test_names()

    assert cited, f"{document} cites no tests"
    assert not missing, f"{document} cites tests that do not exist: {sorted(missing)}"


def test_every_relative_link_in_the_architecture_docs_resolves() -> None:
    for document in [DOCS / "architecture.md", *sorted((DOCS / "adr").glob("*.md"))]:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", document.read_text()):
            if not target.startswith("http"):
                assert (document.parent / target).exists(), f"{document.name}: {target}"


def test_all_seven_adrs_are_present_and_indexed() -> None:
    adrs = sorted(path.name for path in (DOCS / "adr").glob("0*.md"))
    index = (DOCS / "architecture.md").read_text()

    assert [name[:4] for name in adrs] == [f"{n:04d}" for n in range(1, 8)]
    for name in adrs:
        assert f"adr/{name}" in index
