"""Unit tests for repository discovery, language detection, symbols, and chunking."""

from __future__ import annotations

from pathlib import Path

from app.retrieval.chunking import chunk_source
from app.retrieval.discovery import discover_source_files, is_excluded
from app.retrieval.languages import Language, detect_language
from app.retrieval.symbols import extract_symbols, extract_symbols_from_path


def _lang(name: str) -> Language:
    language = detect_language(Path(name))
    assert language is not None
    return language


def test_detect_language_by_extension() -> None:
    assert detect_language(Path("a/b/app.py")) is not None
    assert detect_language(Path("a/b/app.py")).name == "python"  # type: ignore[union-attr]
    assert detect_language(Path("x.ts")).name == "typescript"  # type: ignore[union-attr]
    assert detect_language(Path("x.tsx")).name == "typescript"  # type: ignore[union-attr]
    assert detect_language(Path("x.jsx")).name == "javascript"  # type: ignore[union-attr]
    assert detect_language(Path("Main.java")).name == "java"  # type: ignore[union-attr]
    assert detect_language(Path("foo.go")).name == "go"  # type: ignore[union-attr]
    assert detect_language(Path("foo.UPPER")) is None
    assert detect_language(Path("notes.md")) is None


def test_supported_extensions_include_python_and_ts() -> None:
    from app.retrieval.languages import supported_extensions

    assert ".py" in supported_extensions()
    assert ".ts" in supported_extensions()
    assert ".java" in supported_extensions()
    assert ".md" not in supported_extensions()


def test_discover_source_files_skips_noise(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "app" / "helper.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (root / "venv").mkdir()
    (root / "venv" / "lib.py").write_text("x", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cache.pyc").write_text("x", encoding="utf-8")
    (root / "README.md").write_text("hi", encoding="utf-8")
    (root / "data.json").write_text("{}", encoding="utf-8")
    (root / "huge.py").write_text("x\n" * 600_000, encoding="utf-8")

    files = discover_source_files(root)

    relative = sorted(path.relative_to(root).as_posix() for path in files)
    assert relative == ["app/helper.ts", "app/main.py"]


def test_discover_source_files_respects_extra_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    (root / "generated").mkdir()
    (root / "generated" / "b.py").write_text("x", encoding="utf-8")

    files = discover_source_files(root, extra_excluded={"generated"})

    relative = [path.relative_to(root).as_posix() for path in files]
    assert relative == ["src/a.py"]


def test_is_excluded_matches_names_and_suffixes() -> None:
    assert is_excluded(Path(".venv"))
    assert is_excluded(Path("node_modules"))
    assert is_excluded(Path("package-lock.json"))
    assert is_excluded(Path("a.pyc"))
    assert not is_excluded(Path("main.py"))


def test_python_symbol_extraction() -> None:
    source = (
        "import os\n"
        "from app.util import parse\n"
        "class Service:\n"
        "    def run(self, value):\n"
        "        return parse(value)\n"
        "def helper():\n"
        "    return 1\n"
        "async def fetch():\n"
        "    return 2\n"
    )
    symbols = extract_symbols(source, _lang("service.py"))

    classes = [symbol for symbol in symbols if symbol.kind == "class"]
    methods = [symbol for symbol in symbols if symbol.kind == "method"]
    functions = [symbol for symbol in symbols if symbol.kind == "function"]
    imports = [symbol for symbol in symbols if symbol.kind == "import"]

    assert [symbol.name for symbol in classes] == ["Service"]
    assert classes[0].qualified_name == "Service"
    assert [symbol.name for symbol in methods] == ["run"]
    assert methods[0].qualified_name == "Service.run"
    assert [symbol.name for symbol in functions] == ["helper", "fetch"]
    assert {symbol.qualified_name for symbol in imports} == {"os", "app.util.parse"}
    assert classes[0].start_line == 3
    assert methods[0].start_line == 4


def test_python_symbol_extraction_handles_syntax_errors() -> None:
    symbols = extract_symbols("def broken(:\n", _lang("x.py"))
    assert symbols == []


def test_clike_symbol_extraction() -> None:
    source = (
        "import { helper } from './helper';\n"
        "export class Parser {\n"
        "  parse(input) {\n"
        "    return helper(input);\n"
        "  }\n"
        "}\n"
        "export function topLevel(x) {\n"
        "  return x;\n"
        "}\n"
        "const arrow = (a) => {\n"
        "  return a;\n"
        "};\n"
    )
    symbols = extract_symbols(source, _lang("parser.ts"))

    classes = [symbol for symbol in symbols if symbol.kind == "class"]
    functions = [symbol for symbol in symbols if symbol.kind == "function"]
    methods = [symbol for symbol in symbols if symbol.kind == "method"]

    assert [symbol.name for symbol in classes] == ["Parser"]
    assert classes[0].start_line == 2
    assert classes[0].end_line == 6
    assert [symbol.name for symbol in methods] == ["parse"]
    assert methods[0].end_line == 5
    assert [symbol.name for symbol in functions] == ["topLevel", "arrow"]


def test_extract_symbols_from_path(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")

    symbols = extract_symbols_from_path(path, detect_language(path))

    assert [symbol.name for symbol in symbols] == ["f"]


def test_chunk_source_respects_symbol_boundaries() -> None:
    source = (
        "import os\n"
        "import sys\n"
        "class A:\n"
        "    pass\n"
        "\n"
        "def long_function():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    return a\n"
    )
    symbols = extract_symbols(source, _lang("m.py"))

    chunks = chunk_source(source, symbols)

    assert len(chunks) >= 3
    first = chunks[0]
    assert first.start_line == 1
    assert first.end_line == 2
    assert first.content == "import os\nimport sys"
    by_line = {chunk.start_line: chunk for chunk in chunks}
    class_chunk = by_line[3]
    assert class_chunk.symbols[0].name == "A"
    assert class_chunk.end_line == 5


def test_chunk_source_splits_long_blocks_and_caps_size() -> None:
    lines = [f"line {index}" for index in range(120)]
    source = "\n".join(lines)

    chunks = chunk_source(source, symbols=[], max_lines=40, overlap=4)

    assert all(chunk.end_line - chunk.start_line + 1 <= 40 for chunk in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 120
    assert chunks[-1].start_line > 80


def test_chunk_source_drops_tiny_gaps() -> None:
    source = "def a():\n    return 1\n\n\n\n\ndef b():\n    return 2\n"
    symbols = extract_symbols(source, _lang("m.py"))

    chunks = chunk_source(source, symbols, min_lines=2)

    assert all(chunk.end_line - chunk.start_line + 1 >= 2 for chunk in chunks)


def test_chunk_source_indexes_symbol_less_file_whole() -> None:
    chunks = chunk_source("export const x = 1;\n", symbols=[])

    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 1
    assert "x" in chunks[0].content
    assert chunks[0].symbols == ()
