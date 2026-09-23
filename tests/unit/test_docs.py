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


# Variables used only by docker-compose.yml (not application settings).
COMPOSE_VARIABLES = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST_PORT",
    "REDIS_HOST_PORT",
    "API_HOST_PORT",
    "SIMULATOR_HOST_PORT",
    "WORKER_METRICS_HOST_PORT",
}


def _env_example_keys() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def test_env_example_documents_exactly_the_settings_and_compose_variables() -> None:
    from app.config import Settings

    settings = {name.upper() for name in Settings.model_fields}

    assert _env_example_keys() == settings | COMPOSE_VARIABLES


def _readme_section(title: str) -> str:
    """The README text from `## title` up to the next `## ` heading."""
    readme = (ROOT / "README.md").read_text()
    start = readme.index(f"\n## {title}\n")
    end = readme.find("\n## ", start + 1)
    return readme[start : end if end != -1 else None]


def test_readme_configuration_table_lists_every_variable() -> None:
    section = _readme_section("Configuration")
    table = set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", section, re.MULTILINE))

    assert table == _env_example_keys()


def test_readme_lists_every_error_code() -> None:
    from app.domain.errors import ErrorCode

    section = _readme_section("Error codes")
    listed = set(re.findall(r"^\| `([A-Z_]+)` \|", section, re.MULTILINE))

    assert listed == {code.value for code in ErrorCode}


def test_readme_lists_every_make_target() -> None:
    makefile = (ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z][a-z-]*):.*##", makefile, re.MULTILINE))
    section = _readme_section("Make commands")
    documented = set(re.findall(r"^\| `make ([a-z-]+)", section, re.MULTILINE))

    assert documented == targets


@pytest.mark.parametrize("document", ["README.md", "CONTRIBUTING.md"])
def test_every_relative_link_in_the_readme_resolves(document: str) -> None:
    text = (ROOT / document).read_text()
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if not target.startswith(("http", "mailto")):
            assert (ROOT / target).exists(), f"{document}: broken link {target}"


def test_all_seven_adrs_are_present_and_indexed() -> None:
    adrs = sorted(path.name for path in (DOCS / "adr").glob("0*.md"))
    index = (DOCS / "architecture.md").read_text()

    assert [name[:4] for name in adrs] == [f"{n:04d}" for n in range(1, 8)]
    for name in adrs:
        assert f"adr/{name}" in index
