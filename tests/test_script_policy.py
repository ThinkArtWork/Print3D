from __future__ import annotations

from print3d.script_policy import check_script, errors, summarise

GOOD = """\
from build123d import *

PARAMS = {"width": 40.0, "depth": 30.0, "height": 10.0}

def build_part():
    return Box(PARAMS["width"], PARAMS["depth"], PARAMS["height"])

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


def test_good_script_passes() -> None:
    issues = check_script(GOOD)
    assert errors(issues) == []
    assert summarise(issues)["ok"] is True


def test_syntax_error_rejected() -> None:
    issues = check_script("from build123d import *\ndef build_part(:\n")
    assert summarise(issues)["ok"] is False
    assert "does not parse" in issues[0].message


def test_empty_and_oversized() -> None:
    assert summarise(check_script(""))["ok"] is False
    assert summarise(check_script("x = 1\n" * 20000))["ok"] is False


def test_banned_import() -> None:
    code = GOOD.replace("from build123d import *", "import subprocess\nfrom build123d import *")
    issues = errors(check_script(code))
    assert any("subprocess" in issue.message for issue in issues)


def test_banned_call() -> None:
    code = GOOD.replace("return Box(", "eval('1')\n    return Box(")
    assert any("eval" in issue.message for issue in errors(check_script(code)))


def test_banned_attribute_call() -> None:
    code = GOOD.replace("import build123d", "import build123d") + "\nos.system('ls')\n"
    assert any("os.system" in issue.message for issue in errors(check_script(code)))


def test_missing_contract_pieces() -> None:
    issues = errors(check_script("x = 1\n"))
    messages = " ".join(issue.message for issue in issues)
    assert "build_part" in messages
    assert "build123d" in messages


def test_open_is_a_warning() -> None:
    code = GOOD + "\nopen('/tmp/x', 'w')\n"
    summary = summarise(check_script(code))
    assert summary["ok"] is True
    assert any("open()" in w["message"] for w in summary["warnings"])
