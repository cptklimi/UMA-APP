#!/usr/bin/env python3
"""
Pre-push checks for UMA (index.html).

Catches the bug classes that have actually shipped to production:
  - a function referenced from an onclick/onchange attribute that
    doesn't exist (fails silently until the user taps it)
  - a function called in JS that was never defined
  - an icon class used in markup that has no CSS mask defined
  - a function defined twice, where the second silently wins
  - plain JS syntax errors

Usage:  python3 check-app.py [path/to/index.html]
Exit code 0 = safe to push, 1 = problems found.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

# Names that are legitimately available without being defined in this file.
KNOWN_GLOBALS = {
    # JS language / standard library
    "Array", "Boolean", "Date", "Error", "Function", "JSON", "Map", "Math",
    "Number", "Object", "Promise", "RegExp", "Set", "String", "Symbol",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "eval", "require",
    # Timers / browser
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "alert",
    "confirm", "prompt", "fetch", "atob", "btoa", "requestAnimationFrame",
    "FormData", "FileReader", "Blob", "File", "Image", "URL", "Event",
    "CustomEvent", "XMLHttpRequest", "AbortController", "Intl",
    "getComputedStyle", "matchMedia", "structuredClone", "queueMicrotask",
    # Feature-detected, guarded by `if (window.BarcodeDetector)`
    "BarcodeDetector",
    # Third-party libraries loaded from CDN
    "QRCode", "jsQR",
    # CSS functions, which appear inside JS template strings for inline styles
    "rgba", "rgb", "hsl", "hsla", "url", "var", "calc", "translate",
    "translateX", "translateY", "translate3d", "scale", "rotate", "blur",
    "linear", "cubic", "cubicBezier", "minmax", "repeat", "clamp",
    "linearGradient", "radialGradient", "drop", "brightness", "saturate",
    # Control-flow keywords that the call-site regex can trip over
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "await", "new", "delete", "void", "in", "of", "else", "do", "try",
    "throw", "case", "break", "continue", "instanceof", "yield",
}

# Event-handler attributes whose value is executed as JS.
HANDLER_ATTRS = (
    "onclick", "onchange", "oninput", "onsubmit", "onkeyup", "onkeydown",
    "onkeypress", "onfocus", "onblur", "onload", "onerror", "ontouchstart",
    "ontouchend", "onmousedown", "onmouseup", "onscroll",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def inline_scripts(html: str) -> str:
    """Concatenate inline <script> bodies, skipping those with a src."""
    blocks = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I
    )
    return "\n".join(blocks)


def defined_functions(js: str) -> set:
    """Every name this file makes callable as a global function."""
    names = set()
    names |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", js))
    names |= set(
        re.findall(
            r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"(?:async\s*)?(?:function\b|\()",
            js,
        )
    )
    names |= set(
        re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function", js)
    )
    return names


def declared_vars(js: str) -> set:
    """All var/let/const names, so we don't flag a variable being invoked."""
    found = set()
    for match in re.findall(r"\b(?:var|let|const)\s+([^;=\n]+?)\s*[=;\n]", js):
        for part in match.split(","):
            name = part.strip().split("[")[0].split(".")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                found.add(name)
    # Function parameters are locals too, and may be invoked as callbacks.
    for params in re.findall(r"\bfunction\s*[\w$]*\s*\(([^)]*)\)", js):
        for part in params.split(","):
            name = part.strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                found.add(name)
    return found


def check_syntax(js: str) -> list:
    """Run node --check over the extracted JS."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(js)
        tmp = handle.name
    try:
        result = subprocess.run(
            ["node", "--check", tmp], capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        return ["node not available, syntax check skipped"]
    except subprocess.TimeoutExpired:
        return ["syntax check timed out"]
    finally:
        Path(tmp).unlink(missing_ok=True)

    if result.returncode == 0:
        return []
    return [line for line in result.stderr.strip().splitlines() if line.strip()][:6]


def check_handler_attrs(html: str, defined: set) -> list:
    """Functions invoked from HTML event attributes must exist as globals."""
    problems = []
    attr_re = re.compile(
        r"\b(" + "|".join(HANDLER_ATTRS) + r")\s*=\s*([\"'])(.*?)\2",
        re.S | re.I,
    )
    for match in attr_re.finditer(html):
        attr, _, code = match.group(1), match.group(2), match.group(3)
        line = html[: match.start()].count("\n") + 1
        for name in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", code):
            if name in defined or name in KNOWN_GLOBALS:
                continue
            problems.append((line, attr, name, code.strip()[:70]))
    return problems


def strip_comments(js: str) -> str:
    """Blank out comments, preserving line count so numbers stay accurate.

    Prose in a comment like "// the Barcode Detector API (not on iOS)"
    otherwise looks exactly like a call to API().
    """
    out = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), js, flags=re.S)
    out = re.sub(r"(?<![:\\])//[^\n]*", "", out)
    return out


def check_js_calls(js: str, defined: set, variables: set) -> list:
    """Bare function calls in JS that resolve to nothing defined here."""
    js = strip_comments(js)
    suspects = {}
    for match in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", js):
        name = match.group(1)
        if name in defined or name in KNOWN_GLOBALS or name in variables:
            continue
        line = js[: match.start()].count("\n") + 1
        suspects.setdefault(name, []).append(line)
    return sorted(suspects.items())


def check_icons(html: str) -> list:
    """Icon classes used in markup but never given a CSS mask."""
    defined_icons = set(re.findall(r"\.(ti-[a-z0-9-]+)\s*\{", html))
    used = {}
    for match in re.finditer(r'class\s*=\s*"([^"]*)"', html):
        for cls in match.group(1).split():
            if cls.startswith("ti-"):
                line = html[: match.start()].count("\n") + 1
                used.setdefault(cls, []).append(line)
    return sorted(
        (name, lines) for name, lines in used.items() if name not in defined_icons
    ), len(defined_icons)


def check_duplicate_functions(js: str) -> list:
    """A top-level function declared twice: the later silently replaces it.

    Only unindented declarations count. A nested helper is scoped to its
    parent, so two functions may share a name harmlessly — flagging those
    produces warnings that are always wrong, and a check that cries wolf
    stops being read.
    """
    seen = {}
    for match in re.finditer(
        r"^function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M
    ):
        line = js[: match.start()].count("\n") + 1
        seen.setdefault(match.group(1), []).append(line)
    return sorted((n, ls) for n, ls in seen.items() if len(ls) > 1)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "index.html")
    if not path.exists():
        print(f"{RED}cannot find {path}{OFF}")
        return 1

    html = read(path)
    js = inline_scripts(html)
    defined = defined_functions(js)
    variables = declared_vars(js)

    print(f"\n{BOLD}Checking {path}{OFF}")
    print(f"{DIM}{len(html):,} bytes · {len(js):,} bytes of JS · "
          f"{len(defined)} functions defined{OFF}\n")

    failures = 0
    warnings = 0

    # 1. syntax
    syntax = check_syntax(js)
    if syntax:
        failures += 1
        print(f"{RED}FAIL{OFF}  JavaScript syntax error")
        for line in syntax:
            print(f"      {DIM}{line}{OFF}")
    else:
        print(f"{GREEN}ok{OFF}    JavaScript parses")

    # 2. handler attributes -> hard failure, these break on tap
    handlers = check_handler_attrs(html, defined)
    if handlers:
        failures += 1
        print(f"\n{RED}FAIL{OFF}  {len(handlers)} handler(s) call a function "
              f"that does not exist")
        print(f"      {DIM}these do nothing when tapped — no error shown{OFF}")
        for line, attr, name, snippet in handlers[:12]:
            print(f"      line {line}: {BOLD}{name}(){OFF} in {attr}")
            print(f"        {DIM}{snippet}{OFF}")
        if len(handlers) > 12:
            print(f"      {DIM}...and {len(handlers) - 12} more{OFF}")
    else:
        print(f"{GREEN}ok{OFF}    every onclick/onchange target exists")

    # 3. icons -> hard failure, renders as an empty box
    missing_icons, icon_count = check_icons(html)
    if missing_icons:
        failures += 1
        print(f"\n{RED}FAIL{OFF}  {len(missing_icons)} icon class(es) used "
              f"but not defined")
        print(f"      {DIM}these render as blank space{OFF}")
        for name, lines in missing_icons:
            where = ", ".join(str(n) for n in lines[:5])
            print(f"      {BOLD}{name}{OFF}  line {where}")
    else:
        print(f"{GREEN}ok{OFF}    all icons used are defined "
              f"{DIM}({icon_count} available){OFF}")

    # 4. bare JS calls -> warning, heuristic
    calls = check_js_calls(js, defined, variables)
    if calls:
        warnings += 1
        print(f"\n{YELLOW}warn{OFF}  {len(calls)} name(s) called but not "
              f"defined in this file")
        print(f"      {DIM}may be fine (library globals, locals the scanner "
              f"missed) — eyeball them{OFF}")
        for name, lines in calls[:15]:
            where = ", ".join(str(n) for n in lines[:4])
            print(f"      {name}()  {DIM}line {where}{OFF}")
        if len(calls) > 15:
            print(f"      {DIM}...and {len(calls) - 15} more{OFF}")
    else:
        print(f"{GREEN}ok{OFF}    no unresolved function calls in JS")

    # 5. duplicate definitions -> warning
    dupes = check_duplicate_functions(js)
    if dupes:
        warnings += 1
        print(f"\n{YELLOW}warn{OFF}  {len(dupes)} function(s) defined more "
              f"than once {DIM}(the last one wins){OFF}")
        for name, lines in dupes[:10]:
            print(f"      {name}()  {DIM}lines "
                  f"{', '.join(str(n) for n in lines)}{OFF}")
    else:
        print(f"{GREEN}ok{OFF}    no duplicate function definitions")

    print()
    if failures:
        print(f"{RED}{BOLD}{failures} blocking problem(s). Do not push.{OFF}\n")
        return 1
    if warnings:
        print(f"{YELLOW}Passed with {warnings} thing(s) worth a look.{OFF}\n")
        return 0
    print(f"{GREEN}{BOLD}All checks passed.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
