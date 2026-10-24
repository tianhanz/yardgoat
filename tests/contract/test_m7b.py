"""M7b: retry feedback from real verifiers, isolated stores and shell-only workers."""
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from test_contract import env, wait_exit  # noqa: F401
from test_m4 import finding, rig  # noqa: F401
from yardgoat import cli, runner
from yardgoat.store import TaskDir


HEADING = "## 上一次尝试的验收结果"


def submit(rig, tmp_path, script="echo retry-diagnostic; exit 1", **changes):
    verifier = tmp_path / "retry-verifier.sh"
    verifier.write_text(script + "\n", encoding="utf-8")
    options = {"review": None, "budget": {"max_attempts": 12, "max_wall_min": 1},
               "verifier": {"command": f"bash {shlex.quote(str(verifier))}",
                            "freeze": [str(verifier)]}, **changes}
    return rig.submit(**options)


def claim(rig):
    rig.dispatch()
    rc, result = cli.claim_card("work", "work-st-1")
    assert rc == 0 and result["task_id"] == "parent", result
    return result


def finish(rig, result, verdict="FAIL"):
    assert cli.finish_card("parent", result["run_id"], "work-st-1")[0] == 0
    checked = rig.call("close", "parent", "--as", "owner", rc={"FAIL": 1, "ERROR": 2, "PASS": 0}[verdict])
    assert checked["verdict"] == verdict


def prompt(run_id):
    return (TaskDir("parent").run_dir(run_id) / "prompt.md").read_text(encoding="utf-8")


def test_direct_retry_appends_feedback_after_card_and_answers_without_changing_frozen_files(rig, tmp_path, env):
    submit(rig, tmp_path, "echo stdout-detail; echo stderr-tail >&2; exit 1", adapter={"kind": "direct"})
    td = TaskDir("parent")
    immutable = [Path(td.spec()["card"]), *map(Path, td.spec()["verifier"]["freeze"]),
                 td.path / "spec.json", td.frozen_path]
    before = {p: p.read_bytes() for p in immutable}
    frozen = td.state()["frozen"], td.state()["content_hash"]
    rig.call("park", "parent", "--as", "owner", "--question", "Which behavior?")
    rig.call("answer", "parent", "--as", "owner", "--text", "Preserve output ordering.")
    rig.dispatch()
    wait_exit(env, "parent")
    assert HEADING not in prompt("r1")
    rig.call("close", "parent", "--as", "owner", rc=1)
    rig.dispatch()
    wait_exit(env, "parent")
    text = prompt("r2")
    assert HEADING in text
    assert text.index("write out/result.txt") < text.index("Preserve output ordering.") < text.index(HEADING)
    assert "r1" in text and "FAIL" in text
    assert "stdout-detail\n\n--- stderr ---\nstderr-tail\n" in text
    assert before == {p: p.read_bytes() for p in immutable}
    assert frozen == (td.state()["frozen"], td.state()["content_hash"])


def test_pool_claim_receives_feedback_from_owner_verify_output_only(rig, tmp_path):
    wd = submit(rig, tmp_path)
    first = claim(rig)
    finish(rig, first)
    for decoy in (wd / "verify.out", wd / "out/verify.out", wd / "out/result.txt"):
        decoy.write_text("WORKSPACE_DECOY\n")
    second = claim(rig)
    text = Path(second["prompt"]).read_text()
    assert HEADING in text and "retry-diagnostic" in text and "FAIL" in text
    assert "WORKSPACE_DECOY" not in text
    assert second["run_id"] == "r2" and "r1" in text


def test_long_output_keeps_last_4000_characters_and_names_full_log(rig, tmp_path):
    # Multibyte characters distinguish a character budget from a byte budget.
    output = "DISCARDED_PREFIX\n" + "验收细节🙂" * 1500 + "\nFINAL_DIAGNOSTIC\n"
    submit(rig, tmp_path, f"printf %s {shlex.quote(output)}; exit 1")
    finish(rig, claim(rig))
    claim(rig)
    text = prompt("r2")
    log = TaskDir("parent").run_dir("r1") / "verify.out"
    assert HEADING in text and str(log) in text
    assert "截断" in text
    assert output[-4000:] in text and output[-4001:] not in text
    assert "DISCARDED_PREFIX" not in text


def test_error_blocks_dispatch_and_claim_until_environment_fixed_and_reclosed(rig, tmp_path, capsys):
    wd = submit(rig, tmp_path, "if ! test -f out/verifier-ready; then "
                "echo verifier-environment-error >&2; exit 2; fi\necho checked-failure; exit 1")
    finish(rig, claim(rig), "ERROR")
    td = TaskDir("parent")
    assert cli.main(["dispatch", "parent", "--as", "owner"]) == 3
    assert "fix its environment and run `yardgoat close` again" in capsys.readouterr().err
    rc, result = cli.claim_card("work", "work-st-1")
    assert rc == 0 and result["result"] == "EMPTY"
    assert td.state()["runs"][-1]["state"] == "verify_error"
    assert len(td.state()["runs"]) == 1 and not td.run_dir("r2").exists()
    assert HEADING not in prompt("r1")
    assert "verifier-environment-error" in (td.run_dir("r1") / "verify.out").read_text()

    # Repair the environment without changing the frozen verifier, then re-close r1.
    (wd / "out/verifier-ready").touch()
    assert rig.call("close", "parent", "--as", "owner", rc=1)["verdict"] == "FAIL"
    second = claim(rig)
    assert second["run_id"] == "r2"
    text = prompt("r2")
    assert HEADING in text and "判定：FAIL" in text and "运行：r1" in text
    assert "checked-failure" in text and "verifier-environment-error" not in text
    assert "判定：ERROR" not in text


@pytest.mark.parametrize("stdout,stderr", [(None, None), (b"partial stdout\n", b"partial stderr\n")])
def test_verifier_timeout_records_diagnostic_before_error_verdict(rig, tmp_path, monkeypatch, stdout, stderr):
    submit(rig, tmp_path)
    result = claim(rig)
    assert cli.finish_card("parent", result["run_id"], "work-st-1")[0] == 0
    td = TaskDir("parent")

    def timeout(command, **kwargs):
        assert command == td.spec()["verifier"]["command"]
        assert kwargs["timeout"] == 1800
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout, stderr=stderr)

    # Exercise close's real error-recording path without waiting thirty minutes.
    with monkeypatch.context() as patch:
        patch.setattr(cli.subprocess, "run", timeout)
        assert rig.call("close", "parent", "--as", "owner", rc=2)["verdict"] == "ERROR"
    rd = td.run_dir(result["run_id"])
    log = (rd / "verify.out").read_text(encoding="utf-8")
    assert "timed out" in log and "1800" in log
    if stdout is None:
        assert "No output was received" in log
    else:
        assert stdout.decode() in log and stderr.decode() in log
    receipt = json.loads((rd / "receipt.json").read_text())
    assert receipt["verifier"]["result"] == "ERROR" and receipt["verifier"]["exit_code"] == 124
    assert receipt["status"] == "verifier_error"
    assert td.state()["runs"][-1]["state"] == "verify_error"
    assert td.state()["verdict"]["verdict"] == "ERROR"
    rig.call("dispatch", "parent", "--as", "owner", rc=3)


@pytest.mark.parametrize("script", ["exit 1", "printf ' \\t\\n'; exit 1"])
def test_empty_verifier_output_has_explanation_instead_of_empty_code_block(rig, tmp_path, script):
    submit(rig, tmp_path, script)
    finish(rig, claim(rig))
    claim(rig)
    text = prompt("r2")
    assert HEADING in text
    feedback = text.split(HEADING, 1)[1]
    log = TaskDir("parent").run_dir("r1") / "verify.out"
    assert not log.read_text().strip()
    assert "运行：r1" in feedback and "判定：FAIL" in feedback and str(log) in feedback
    assert "验收程序没有输出，只返回了非零退出码；完整日志见上面的路径" in feedback
    assert "```" not in feedback


def test_pass_after_failure_does_not_reuse_old_feedback_for_review_repair(rig, tmp_path):
    wd = submit(rig, tmp_path, "if test -f out/ready; then echo passed-detail; exit 0; fi\necho old-failure; exit 1",
                review={"pool": "audit", "path": "src"})
    finish(rig, claim(rig))
    (wd / "out/ready").touch()
    second = claim(rig)
    finish(rig, second, "PASS")
    rig.review(1, [finding()])
    rig.call("reconcile")
    rc, third = cli.claim_card("work", "work-st-1")
    assert rc == 0 and third["run_id"] == "r3"
    text = prompt("r3")
    assert HEADING not in text and "old-failure" not in text and "passed-detail" not in text
    assert "Lost update" in text


@pytest.mark.parametrize("outcome", ["launch_failed", "provider_refused"])
@pytest.mark.parametrize("earlier_failure", [False, True])
def test_infrastructure_failure_never_reuses_stale_verdict(rig, tmp_path, env, outcome, earlier_failure):
    event = shlex.quote(json.dumps({"type": "error", "error": {"message": "usage limit reached"}}))
    failure = "echo yardgoat-launch-failed: missing-source; exit 3" if outcome == "launch_failed" else f"echo {event}; exit 1"
    worker = f"if test -f out/fail-launch; then {failure}; fi"
    wd = submit(rig, tmp_path, adapter={"kind": "direct"},
                executor={"host": "shell", "command": worker})
    if earlier_failure:
        rig.dispatch()
        wait_exit(env, "parent")
        rig.call("close", "parent", "--as", "owner", rc=1)
    (wd / "out/fail-launch").touch()
    rig.dispatch()
    state = wait_exit(env, "parent")
    assert state["runs"][-1]["state"] == outcome
    assert not (TaskDir("parent").run_dir(state["runs"][-1]["run_id"]) / "verify.out").exists()
    (wd / "out/fail-launch").unlink()
    rig.dispatch()
    state = wait_exit(env, "parent")
    text = prompt(state["runs"][-1]["run_id"])
    assert HEADING not in text and "retry-diagnostic" not in text


def test_never_verified_run_and_workspace_log_supply_no_feedback(rig, tmp_path):
    wd = submit(rig, tmp_path)
    (wd / "verify.out").write_text("FORGED_VERIFICATION\n")
    first = claim(rig)
    assert cli.finish_card("parent", first["run_id"], "work-st-1")[0] == 0
    td = TaskDir("parent")
    assert HEADING not in prompt("r1")
    assert HEADING not in runner.prompt_text(td, td.spec())
    assert "FORGED_VERIFICATION" not in runner.prompt_text(td, td.spec())


def test_answering_a_question_preserves_latest_judgment_without_inventing_a_new_one(rig, tmp_path):
    wd = submit(rig, tmp_path)
    finish(rig, claim(rig))
    second = claim(rig)
    (wd / "out/BLOCKED.md").write_text("Which output format?\n")
    assert cli.finish_card("parent", second["run_id"], "work-st-1")[0] == 0
    assert TaskDir("parent").state()["status"] == "parked"
    rig.call("answer", "parent", "--as", "owner", "--text", "Use plain text.")
    claim(rig)
    text = prompt("r3")
    assert HEADING in text and "retry-diagnostic" in text and "r1" in text
    assert "Use plain text." in text and "r2" not in text


@pytest.mark.parametrize("count", [2, 10])
def test_consecutive_failures_include_only_latest_run_in_run_order(rig, tmp_path, count):
    submit(rig, tmp_path, 'echo "DIAGNOSTIC_${YARDGOAT_RUN_DIR##*/}_END"; exit 1')
    for number in range(1, count + 1):
        result = claim(rig)
        assert result["run_id"] == f"r{number}"
        finish(rig, result)
    claim(rig)
    text = prompt(f"r{count + 1}")
    assert text.count(HEADING) == 1
    assert f"DIAGNOSTIC_r{count}_END" in text
    for number in range(1, count):
        assert f"DIAGNOSTIC_r{number}_END" not in text


def test_missing_latest_verify_output_does_not_fall_back_to_old_or_workspace_files(rig, tmp_path):
    wd = submit(rig, tmp_path)
    for _ in range(2):
        finish(rig, claim(rig))
    (TaskDir("parent").run_dir("r2") / "verify.out").unlink()
    (wd / "verify.out").write_text("WORKSPACE_DECOY")
    claim(rig)
    text = prompt("r3")
    assert HEADING not in text and "retry-diagnostic" not in text and "WORKSPACE_DECOY" not in text


def test_review_automatic_retry_gets_feedback_but_child_goal_context_excludes_it(rig, tmp_path):
    wd = submit(rig, tmp_path, "if test -f out/ready; then exit 0; fi\necho RETRY_FAILURE_CONTEXT; exit 1",
                review={"pool": "audit", "path": "src"})
    first = claim(rig)
    assert cli.finish_card("parent", first["run_id"], "work-st-1")[0] == 0
    rig.call("reconcile")  # auto-close FAIL
    rig.call("reconcile")  # review policy queues retry
    rc, second = cli.claim_card("work", "work-st-1")
    assert rc == 0 and second["run_id"] == "r2"
    execution = prompt("r2")
    (wd / "out/ready").touch()
    finish(rig, second, "PASS")
    child = TaskDir("parent-review-1")
    card = Path(child.spec()["card"]).read_text()
    goal = TaskDir("parent").run_dir("r2") / "goal.md"
    assert "write out/result.txt" in card and f"({goal})" in card
    assert goal.read_text() in card and str(Path(second["prompt"])) not in card
    assert HEADING not in card and "RETRY_FAILURE_CONTEXT" not in card
    assert HEADING in execution and "RETRY_FAILURE_CONTEXT" in execution
    assert prompt("r2") == execution  # review never rewrites execution evidence


def test_review_legacy_run_without_goal_quotes_prompt_with_matching_provenance(rig, tmp_path):
    submit(rig, tmp_path, "exit 0", review={"pool": "audit", "path": "src"})
    first = claim(rig)
    rd = TaskDir("parent").run_dir(first["run_id"])
    (rd / "goal.md").unlink()
    execution_prompt = rd / "prompt.md"
    legacy_context = execution_prompt.read_text() + "\nLegacy captured execution context.\n"
    execution_prompt.write_text(legacy_context)
    finish(rig, first, "PASS")
    child = TaskDir("parent-review-1")
    card = Path(child.spec()["card"]).read_text()
    assert f"## Executed goal and answers: parent/r1 ({execution_prompt})" in card
    assert legacy_context in card and str(rd / "goal.md") not in card
    assert execution_prompt.read_text() == legacy_context
    assert not (rd / "goal.md").exists()


def test_file_pool_transports_same_retry_prompt_as_saved_run(rig, tmp_path):
    root = tmp_path / "file-queue"
    cfg_path = Path(os.environ["YARDGOAT_CONFIG"])
    cfg = json.loads(cfg_path.read_text())
    cfg["adapters"] = {"file-pool": {"root": str(root)}}
    cfg_path.write_text(json.dumps(cfg))
    submit(rig, tmp_path, adapter={"kind": "file-pool", "pool": "queue"})
    rig.dispatch()
    done = root / "queue/done"
    done.mkdir()
    (root / "queue/pending/parent-r1.md").rename(done / "parent-r1.md")
    rig.call("reconcile")
    rig.call("close", "parent", "--as", "owner", rc=1)
    rig.dispatch()
    queued = (root / "queue/pending/parent-r2.md").read_text()
    assert prompt("r2") in queued
    assert HEADING in queued and "retry-diagnostic" in queued
