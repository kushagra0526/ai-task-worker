from worker.agent import Guard
from worker.policy import ApprovalPolicy, Decision, Risk, classify_control, classify_http
from worker.secrets import SecretVault
from worker.tools import ToolResult, validate_args


def test_control_classification_is_generic():
    assert classify_control("Search", submits_form=True, form_method="get") is Risk.READ
    assert classify_control("Save bill", submits_form=True, form_method="post") is Risk.WRITE
    assert classify_control("Send", submits_form=True, form_method="post") is Risk.EXTERNAL
    assert classify_control("Delete vendor", submits_form=False, form_method=None) is Risk.EXTERNAL
    assert classify_control("Bills", submits_form=False, form_method=None) is Risk.READ


def test_http_classification():
    assert classify_http("GET") is Risk.READ
    assert classify_http("post") is Risk.WRITE
    assert classify_http("DELETE") is Risk.EXTERNAL


def test_policy_modes():
    assert ApprovalPolicy("guarded").decide(Risk.WRITE) is Decision.ALLOW
    assert ApprovalPolicy("guarded").decide(Risk.EXTERNAL) is Decision.ASK
    assert ApprovalPolicy("strict").decide(Risk.WRITE) is Decision.ASK
    assert ApprovalPolicy("read_only").decide(Risk.WRITE) is Decision.DENY
    assert ApprovalPolicy("read_only").decide(Risk.READ) is Decision.ALLOW


def test_secret_resolution_and_masking():
    vault = SecretVault.from_values({"erp_password": "hunter22"})
    assert vault.resolve("pw={{secret:erp_password}}") == "pw=hunter22"
    assert vault.mask("echo hunter22") == "echo {{secret:erp_password}}"
    try:
        vault.resolve("{{secret:nope}}")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_validate_args():
    schema = {"type": "object", "properties": {"ref": {"type": "string"}, "submit": {"type": "boolean"}},
              "required": ["ref"], "additionalProperties": False}
    assert validate_args(schema, {"ref": "e1"}) == []
    assert "missing required argument 'ref'" in validate_args(schema, {})
    assert validate_args(schema, {"ref": 3}) == ["argument 'ref' must be of type string"]
    assert validate_args(schema, {"ref": "e1", "x": 1}) == ["unexpected argument 'x'"]


def test_guard_detects_repetition_and_error_streaks():
    guard = Guard()
    same = ToolResult("nothing changed")
    assert guard.record("browser_click", {"ref": "e1"}, same) == []
    assert guard.record("browser_click", {"ref": "e1"}, same) == []
    assert "same browser_click call" in guard.record("browser_click", {"ref": "e1"}, same)[0]

    guard = Guard()
    err = ToolResult("boom", is_error=True)
    guard.record("a", {"i": 1}, err)
    guard.record("a", {"i": 2}, err)
    assert "3 tool calls in a row have failed" in guard.record("a", {"i": 3}, err)[0]
    guard.record("a", {"i": 4}, ToolResult("fine"))
    assert guard.error_streak == 0
