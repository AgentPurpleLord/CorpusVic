# Project-wide Claude.md - CorpusVic

CorpusVic is a two-part tool:

1. A Victorian legislation PDF parser, which parses the PDF into structured data.
2. A public-facing website which displays that structured data.

## LLM Behavioural Guidelines

- Minimum code that solves the problem.
- No features beyond what is asked for.
- No abstractions for single-use code.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50 lines, re-write it.

## Project Background

Victorian legislation is published by the Office of the Chief Parliamentary Counsel on https://www.legislation.vic.gov.au/. The legislation is published in PDF format with recognised text (no OCR required).

AustLii and Jade.io have versions of the legislation in parsed format, but often run into various errors.

CorpusVic seeks to solve those errors by ensuring each piece of legislation is reviewed, and the parser is capable of being fixed by users reviewing the legislation. Thus, creating a system that can be continually improved.

## Architecture

- Python is the coding language.

## Coding Conventions

- Type hints on all function signatures — parameters and return types
- Use `from __future__ import annotations` at the top of every module
- Pydantic models for all API input/output — never pass raw dicts across boundaries
- Use `pathlib.Path` instead of `os.path`
- f-strings for string formatting (no .format() or % formatting)
- Docstrings on public functions using Google style

## Do NOT

- Do not use `import *`
- Do not use `objects.raw()` or raw SQL — use the repository layer
- Do not use `print()` — use the configured `logging` module
- Do not use `*` imports