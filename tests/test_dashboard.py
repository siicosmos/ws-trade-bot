import re

from trader.dashboard import DASHBOARD_HTML


def _strip_complete_strings(line):
    code = re.sub(r"//.*", "", line)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "", code)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', "", code)
    return code


def test_dashboard_script_strings_terminated():
    scripts = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S)
    assert scripts
    for js in scripts:
        for line in js.split("\n"):
            code = _strip_complete_strings(line)
            assert '"' not in code, f"unterminated string: {line}"
            assert "'" not in code, f"unterminated string: {line}"
