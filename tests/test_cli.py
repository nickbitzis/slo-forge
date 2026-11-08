from __future__ import annotations

import json

from conftest import EXAMPLES

from sloforge.cli import main


def test_validate_ok(capsys):
    assert main(["validate", str(EXAMPLES / "slos"), "--policy",
                 str(EXAMPLES / "policy.yaml")]) == 0
    assert "ok: 15 services, 22 SLOs" in capsys.readouterr().out


def test_validate_fails_with_paths(capsys):
    assert main(["validate", str(EXAMPLES / "invalid")]) == 1
    err = capsys.readouterr().err
    assert "slos[0].objective" in err and "5 error(s)" in err


def test_generate_and_check(tmp_path, capsys):
    assert main(["generate", str(EXAMPLES / "slos"), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "checkout-api.alerts.yaml").exists()
    assert main(["check-rules", str(tmp_path)]) == 0
    assert "checked 30 rule files, 0 error(s)" in capsys.readouterr().out


def test_check_rules_fails_on_bad_file(tmp_path):
    (tmp_path / "bad.yaml").write_text("groups:\n- name: g\n  rules:\n  - record: x\n"
                                       "    expr: rate(y)\n")
    assert main(["check-rules", str(tmp_path)]) == 1


def test_schema(capsys):
    assert main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "ServiceSpec"


def test_gate_unreachable(capsys):
    assert main(["gate", "cart-service", "--url", "http://127.0.0.1:9"]) == 2
