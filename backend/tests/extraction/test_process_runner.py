from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from app.extraction import process_runner as process_runner_module
from app.extraction.process_runner import (
    ProcessCleanupError,
    ProcessIsolationError,
    ProcessRunner,
    ProcessRunnerError,
    _WindowsJob,
)


def _pid_is_active(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _force_kill_pid(pid: int) -> None:
    if not _pid_is_active(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, 9)


def _drain_thread_count() -> int:
    return sum(thread.name.startswith("process-stdout-") or thread.name.startswith("process-stderr-") for thread in threading.enumerate())


def test_process_runner_executes_argv_without_shell(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path)

    result = runner.run(
        [sys.executable, "-c", "print('ok')", "&&", "echo", "unsafe"],
        tmp_path,
        {},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False


def test_process_runner_rejects_cwd_outside_allowed_root(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path / "work")

    with pytest.raises(ProcessRunnerError, match="allowed work root"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, env={})


def test_process_runner_uses_an_explicit_environment_allowlist(tmp_path: Path) -> None:
    os.environ["UNRELATED_PARENT_SECRET"] = "must-not-leak"
    runner = ProcessRunner(allowed_work_root=tmp_path)
    code = "import os; print(os.getenv('OPENAI_MODEL')); print(os.getenv('UNRELATED_PARENT_SECRET'))"

    result = runner.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"OPENAI_MODEL": "fixture-model", "NOT_ALLOWED": "discard-me"},
    )

    assert result.stdout.splitlines() == ["fixture-model", "None"]


def test_process_runner_redacts_and_bounds_captured_output(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, max_output_chars=120)
    code = (
        "import sys; "
        "print('Authorization: Bearer top-secret-token'); "
        "print('api_key=sk-fixture-secret', file=sys.stderr); "
        "print('x' * 1000)"
    )

    result = runner.run([sys.executable, "-c", code], cwd=tmp_path, env={})

    captured = result.stdout + result.stderr
    assert "top-secret-token" not in captured
    assert "sk-fixture-secret" not in captured
    assert "[REDACTED]" in captured
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 120


def test_process_runner_redacts_serialized_authorization_headers(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path)
    code = (
        "import sys; "
        "print('{\"headers\": {\"Authorization\": \"Bearer json-secret-token\"}}'); "
        "print(\"{'aUtHoRiZaTiOn': 'Basic stderr-secret-token'}\", file=sys.stderr)"
    )

    result = runner.run([sys.executable, "-c", code], cwd=tmp_path, env={})

    assert "json-secret-token" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "stderr-secret-token" not in result.stderr
    assert "[REDACTED]" in result.stderr


def test_process_runner_streams_and_discards_output_beyond_byte_limit(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, max_output_bytes=4096)
    code = (
        "import os,sys; "
        "chunk=b'x'*65536; "
        "[(os.write(1,chunk),os.write(2,chunk)) for _ in range(48)]"
    )

    result = runner.run([sys.executable, "-c", code], tmp_path, {})

    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert len(result.stdout.encode("utf-8")) <= 4096
    assert len(result.stderr.encode("utf-8")) <= 4096


def test_process_runner_redacts_encoded_and_common_credentials(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, max_output_bytes=4096)
    secrets = [
        r'{\"Authorization\":\"Bearer escaped-token\"}',
        "client_secret=client-secret-value",
        "PASSWORD: password-value",
        "refresh_token=refresh-secret-value",
        "Bearer standalone-bearer-value",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signaturevalue",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    ]
    code = "import sys; print(" + repr("\n".join(secrets)) + "); print(" + repr("\n".join(secrets)) + ", file=sys.stderr)"

    result = runner.run([sys.executable, "-c", code], tmp_path, {})

    captured = result.stdout + result.stderr
    for secret in secrets:
        assert secret not in captured
    assert captured.count("[REDACTED]") >= len(secrets) * 2


def test_process_runner_confines_temp_and_home_to_workdir(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path)
    code = "import os; print('|'.join(os.environ.get(k,'') for k in ('TEMP','TMP','TMPDIR','HOME','USERPROFILE')))"

    result = runner.run([sys.executable, "-c", code], tmp_path, {})

    for value in result.stdout.strip().split("|"):
        assert Path(value).resolve().is_relative_to(tmp_path.resolve())


def test_process_runner_times_out_and_terminates_process(tmp_path: Path) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, timeout_seconds=0.2)

    started = time.monotonic()
    result = runner.run(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        cwd=tmp_path,
        env={},
    )

    assert time.monotonic() - started < 5
    assert result.timed_out is True
    assert result.exit_code != 0
    assert "started" in result.stdout


def test_timeout_is_bounded_when_descendant_holds_pipes(tmp_path: Path) -> None:
    runner = ProcessRunner(
        allowed_work_root=tmp_path,
        timeout_seconds=0.2,
        shutdown_grace_seconds=0.5,
    )
    child = "import time; print('descendant', flush=True); time.sleep(30)"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "print('parent', flush=True); time.sleep(30)"
    )

    started = time.monotonic()
    result = runner.run([sys.executable, "-c", parent], tmp_path, {})

    assert time.monotonic() - started < 3
    assert result.timed_out is True
    assert "parent" in result.stdout


def test_runner_reaps_descendants_and_drain_threads_after_normal_exit(tmp_path: Path) -> None:
    runner = ProcessRunner(
        allowed_work_root=tmp_path,
        timeout_seconds=3,
        shutdown_grace_seconds=1,
    )
    pid_file = tmp_path / "descendant.pid"
    child = "import time; time.sleep(30)"
    parent = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    before_threads = _drain_thread_count()

    result = runner.run([sys.executable, "-c", parent], tmp_path, {})
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 2
        while _pid_is_active(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result.exit_code == 0
        assert not _pid_is_active(child_pid)
        assert _drain_thread_count() == before_threads
    finally:
        _force_kill_pid(child_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting")
def test_windows_job_handle_is_closed(tmp_path: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    count_before = ctypes.c_ulong()
    count_after = ctypes.c_ulong()
    process = kernel32.GetCurrentProcess()
    assert kernel32.GetProcessHandleCount(process, ctypes.byref(count_before))

    result = ProcessRunner(allowed_work_root=tmp_path).run(
        [sys.executable, "-c", "print('ok')"], tmp_path, {}
    )

    assert result.exit_code == 0
    assert kernel32.GetProcessHandleCount(process, ctypes.byref(count_after))
    assert count_after.value <= count_before.value + 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object handshake")
def test_assign_failure_never_starts_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "target-started"
    import_marker = tmp_path / "malicious-imported"
    (tmp_path / "json.py").write_text(
        f"from pathlib import Path; Path({str(import_marker)!r}).write_text('json')",
        encoding="utf-8",
    )
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(import_marker)!r}).write_text('site')",
        encoding="utf-8",
    )
    before_threads = _drain_thread_count()

    def reject_assign(self: _WindowsJob, process: object) -> None:
        raise ProcessIsolationError("process isolation is unavailable")

    monkeypatch.setattr(_WindowsJob, "assign", reject_assign)
    runner = ProcessRunner(allowed_work_root=tmp_path, shutdown_grace_seconds=0.5)

    with pytest.raises(ProcessIsolationError, match="isolation"):
        runner.run(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
            tmp_path,
            {},
        )

    assert not marker.exists()
    assert not import_marker.exists()
    assert _drain_thread_count() == before_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows failed-start cleanup")
def test_assign_failure_cleans_up_when_process_kill_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_threads = _drain_thread_count()

    def reject_assign(self: _WindowsJob, process: subprocess.Popen[bytes]) -> None:
        monkeypatch.setattr(
            process,
            "kill",
            lambda: (_ for _ in ()).throw(OSError("kill failed")),
        )
        raise ProcessIsolationError("process isolation is unavailable")

    monkeypatch.setattr(_WindowsJob, "assign", reject_assign)

    with pytest.raises(ProcessCleanupError, match="cleanup could not be confirmed"):
        ProcessRunner(
            allowed_work_root=tmp_path,
            shutdown_grace_seconds=0.5,
        ).run([sys.executable, "-c", "pass"], tmp_path, {})

    assert not list(tmp_path.glob(".process-launch-*"))
    assert _drain_thread_count() == before_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows Job initialization")
def test_job_initialization_failure_removes_launcher_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _WindowsJob,
        "__init__",
        lambda self: (_ for _ in ()).throw(ProcessIsolationError("process isolation is unavailable")),
    )

    with pytest.raises(ProcessIsolationError):
        ProcessRunner(allowed_work_root=tmp_path).run(
            [sys.executable, "-c", "pass"], tmp_path, {}
        )

    assert not list(tmp_path.glob(".process-launch-*"))


def test_launcher_config_write_failure_removes_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path)

    def fail_write(directory: Path) -> Path:
        (directory / "partial").write_text("partial", encoding="utf-8")
        raise OSError("private")

    monkeypatch.setattr(runner, "_write_launcher_config", fail_write)
    with pytest.raises(ProcessRunnerError, match="could not be started"):
        runner.run([sys.executable, "-c", "pass"], tmp_path, {})

    assert not list(tmp_path.glob(".process-launch-*"))


def test_payload_write_failure_reports_unfinished_drainers_as_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, shutdown_grace_seconds=0.5)
    before_threads = _drain_thread_count()
    finish_drainers = runner._finish_drainers

    monkeypatch.setattr(
        runner,
        "_environment",
        lambda env, work_dir: (_ for _ in ()).throw(TypeError("payload failed")),
    )

    def finish_but_report_failure(process: object, drainers: object, deadline: float) -> bool:
        finish_drainers(process, drainers, deadline)
        return False

    monkeypatch.setattr(runner, "_finish_drainers", finish_but_report_failure)

    with pytest.raises(ProcessCleanupError, match="cleanup could not be confirmed"):
        runner.run([sys.executable, "-c", "pass"], tmp_path, {})

    assert not list(tmp_path.glob(".process-launch-*"))
    assert _drain_thread_count() == before_threads


@pytest.mark.parametrize("failure", [MemoryError("memory"), RecursionError("recursion")])
def test_payload_preparation_failures_use_fixed_error_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, shutdown_grace_seconds=0.5)
    before_threads = _drain_thread_count()
    monkeypatch.setattr(
        runner,
        "_environment",
        lambda env, work_dir: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(ProcessRunnerError, match="process could not be started") as caught:
        runner.run([sys.executable, "-c", "pass"], tmp_path, {})

    assert type(caught.value) is ProcessRunnerError
    assert caught.value.__cause__ is None
    assert not list(tmp_path.glob(".process-launch-*"))
    assert _drain_thread_count() == before_threads


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7)])
def test_payload_control_exceptions_are_rethrown_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, shutdown_grace_seconds=0.5)
    before_threads = _drain_thread_count()
    monkeypatch.setattr(
        runner,
        "_environment",
        lambda env, work_dir: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(type(failure)):
        runner.run([sys.executable, "-c", "pass"], tmp_path, {})

    assert not list(tmp_path.glob(".process-launch-*"))
    assert _drain_thread_count() == before_threads


def test_payload_cleanup_runs_every_step_when_cleanup_calls_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ProcessRunner(allowed_work_root=tmp_path, shutdown_grace_seconds=0.5)
    calls: list[str] = []
    terminate = runner._terminate_owned_group
    finish = runner._finish_drainers
    remove = runner._remove_private_tree
    monkeypatch.setattr(
        runner,
        "_environment",
        lambda env, work_dir: (_ for _ in ()).throw(MemoryError("payload")),
    )

    def terminate_then_raise(process: object, job: object) -> bool:
        calls.append("terminate")
        terminate(process, job)
        raise OSError("terminate cleanup")

    def finish_then_raise(process: object, drainers: object, deadline: float) -> bool:
        calls.append("finish")
        finish(process, drainers, deadline)
        raise OSError("drainer cleanup")

    def remove_launcher(path: Path) -> bool:
        calls.append("remove")
        return remove(path)

    monkeypatch.setattr(runner, "_terminate_owned_group", terminate_then_raise)
    monkeypatch.setattr(runner, "_finish_drainers", finish_then_raise)
    monkeypatch.setattr(runner, "_remove_private_tree", remove_launcher)

    with pytest.raises(ProcessCleanupError, match="cleanup could not be confirmed"):
        runner.run([sys.executable, "-c", "pass"], tmp_path, {})

    assert calls == ["terminate", "finish", "remove"]
    assert not list(tmp_path.glob(".process-launch-*"))


def test_launcher_config_close_failure_still_unlinks_temp_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_calls: list[int] = []
    unlink_calls: list[Path] = []
    real_close = process_runner_module.os.close
    real_unlink = Path.unlink

    def fail_first_close(descriptor: int) -> None:
        close_calls.append(descriptor)
        if len(close_calls) > 1:
            raise AssertionError("descriptor was closed twice")
        real_close(descriptor)
        raise OSError("close failed")

    def track_unlink(path: Path, *args: object, **kwargs: object) -> None:
        unlink_calls.append(path)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(process_runner_module.os, "close", fail_first_close)
    monkeypatch.setattr(Path, "unlink", track_unlink)

    with pytest.raises(OSError, match="close failed"):
        ProcessRunner._write_launcher_config(tmp_path)

    assert len(close_calls) == 1
    assert [path.name for path in unlink_calls] == ["config.tmp"]
    assert not (tmp_path / "config.tmp").exists()


def test_launcher_config_unlink_failure_does_not_skip_fd_close_or_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_calls: list[int] = []
    unlink_calls: list[Path] = []
    real_close = process_runner_module.os.close

    def fail_write(descriptor: int, payload: bytes) -> int:
        raise OSError("write failed")

    def track_close(descriptor: int) -> None:
        close_calls.append(descriptor)
        real_close(descriptor)

    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
        unlink_calls.append(path)
        raise OSError("unlink failed")

    monkeypatch.setattr(process_runner_module.os, "write", fail_write)
    monkeypatch.setattr(process_runner_module.os, "close", track_close)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(OSError, match="write failed"):
        ProcessRunner._write_launcher_config(tmp_path)

    assert len(close_calls) == 1
    assert [path.name for path in unlink_calls] == ["config.tmp"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object close failure")
def test_job_close_failure_is_fixed_cleanup_error_without_thread_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_threads = _drain_thread_count()
    monkeypatch.setattr(_WindowsJob, "close", lambda self: False)

    with pytest.raises(ProcessRunnerError, match="cleanup could not be confirmed"):
        ProcessRunner(
            allowed_work_root=tmp_path,
            shutdown_grace_seconds=0.5,
        ).run([sys.executable, "-c", "print('done')"], tmp_path, {})

    assert _drain_thread_count() == before_threads


def test_source_document_is_copied_into_isolated_workdir(tmp_path: Path) -> None:
    source_root = tmp_path / "readonly-source"
    source_root.mkdir()
    source = source_root / "book.md"
    source.write_text("fixture", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    runner = ProcessRunner(
        allowed_work_root=work_root,
        allowed_source_roots=(source_root,),
    )

    staged = runner.stage_source_document(source, cwd=work_root / "run")

    assert staged.parent.name == "inputs"
    assert staged.name == "book.md"
    assert staged.resolve().is_relative_to((work_root / "run").resolve())
    assert staged.read_text(encoding="utf-8") == "fixture"
    assert staged.resolve() != source.resolve()


def test_staging_rejects_links_and_copy_limits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("1234", encoding="utf-8")
    (source / "two.txt").write_text("5678", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    count_limited = ProcessRunner(
        allowed_work_root=work,
        allowed_source_roots=(source,),
        max_copy_files=1,
    )
    with pytest.raises(ProcessRunnerError, match="copy safety limit"):
        count_limited.stage_source_tree(source, cwd=work, name="tree")

    byte_limited = ProcessRunner(
        allowed_work_root=work,
        allowed_source_roots=(source,),
        max_copy_bytes=3,
    )
    with pytest.raises(ProcessRunnerError, match="copy safety limit"):
        byte_limited.stage_source_document(source / "one.txt", cwd=work, stage_name="doc")

    link = source / "linked.txt"
    try:
        link.symlink_to(source / "one.txt")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    normal = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source,))
    with pytest.raises(ProcessRunnerError, match="links"):
        normal.stage_source_document(link, cwd=work, stage_name="linked")


def test_stage_budget_is_shared_across_multiple_trees(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    first = sources / "first"
    second = sources / "second"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "one.txt").write_text("123", encoding="utf-8")
    (second / "two.txt").write_text("456", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(
        allowed_work_root=work,
        allowed_source_roots=(sources,),
        max_copy_files=1,
        max_copy_bytes=5,
    )
    budget = runner.new_stage_budget()

    runner.stage_source_tree(first, cwd=work, name="first", budget=budget)
    with pytest.raises(ProcessRunnerError, match="copy safety limit"):
        runner.stage_source_tree(second, cwd=work, name="second", budget=budget)

    assert not list(work.rglob("two.txt"))


def test_stage_copy_detects_source_growth_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "growing.bin"
    source.write_bytes(b"1234")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(
        allowed_work_root=work,
        allowed_source_roots=(source_root,),
        max_copy_bytes=6,
    )
    real_read = process_runner_module.os.read
    calls = 0

    def growing_read(fd: int, size: int) -> bytes:
        nonlocal calls
        data = real_read(fd, size)
        calls += 1
        if calls == 1:
            with source.open("ab") as handle:
                handle.write(b"5678")
        return data

    monkeypatch.setattr(process_runner_module.os, "read", growing_read)

    with pytest.raises(ProcessRunnerError, match="copy safety limit"):
        runner.stage_source_document(
            source,
            cwd=work,
            stage_name="growth",
            budget=runner.new_stage_budget(),
        )

    assert not list(work.rglob("growing.bin"))


def test_stage_rejects_simulated_source_directory_identity_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("data", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source,))
    original = runner._verify_directory_identities

    def reject_source(records: object) -> bool:
        if any(path == source.resolve() for path, _ in records):
            return False
        return original(records)

    monkeypatch.setattr(runner, "_verify_directory_identities", reject_source)
    with pytest.raises(ProcessRunnerError, match="identity"):
        runner.stage_source_tree(source, cwd=work, name="tree")

    assert not list(work.glob(".stage-*"))


def test_stage_rejects_simulated_target_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("data", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source,))
    original = runner._verify_directory_identities

    def reject_target(records: object) -> bool:
        if any(".stage-" in str(path) for path, _ in records):
            return False
        return original(records)

    monkeypatch.setattr(runner, "_verify_directory_identities", reject_target)
    with pytest.raises(ProcessRunnerError, match="identity"):
        runner.stage_source_tree(source, cwd=work, name="tree")

    assert not list(work.glob(".stage-*"))


def test_sealed_stage_modification_blocks_process_start(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "input.txt"
    source.write_text("original", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    marker = work / "started"
    runner = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source_root,))
    staged = runner.stage_source_document(source, cwd=work, stage_name="document")
    runner.seal_staging()
    staged.chmod(0o600)
    staged.write_text("tampered", encoding="utf-8")

    with pytest.raises(ProcessIsolationError, match="stage integrity"):
        runner.run(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
            work,
            {},
        )

    assert not marker.exists()


@pytest.mark.parametrize("failure", ["hash", "replace"])
def test_seal_failure_removes_entire_private_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "input.txt"
    source.write_text("original", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source_root,))
    runner.stage_source_document(source, cwd=work, stage_name="document")

    if failure == "hash":
        real_sha256 = process_runner_module.hashlib.sha256

        def fail_file_hash(data: bytes = b"") -> object:
            if not data:
                raise MemoryError("hash failed")
            return real_sha256(data)

        monkeypatch.setattr(process_runner_module.hashlib, "sha256", fail_file_hash)
    else:
        real_replace = process_runner_module.os.replace

        def fail_manifest_replace(source_path: object, destination_path: object) -> None:
            if Path(destination_path).name == ".stage-manifest.json":
                raise OSError("replace failed")
            real_replace(source_path, destination_path)

        monkeypatch.setattr(process_runner_module.os, "replace", fail_manifest_replace)

    with pytest.raises(ProcessIsolationError, match="stage integrity"):
        runner.seal_staging()

    assert not list(work.glob(".stage-*"))


def test_failed_stage_discard_retains_root_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "input.txt"
    source.write_text("original", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    runner = ProcessRunner(allowed_work_root=work, allowed_source_roots=(source_root,))
    runner.stage_source_document(source, cwd=work, stage_name="document")
    remove = runner._remove_private_tree
    monkeypatch.setattr(runner, "_remove_private_tree", lambda root: False)

    assert runner.discard_stage() is False

    monkeypatch.setattr(runner, "_remove_private_tree", remove)
    assert runner.discard_stage() is True
    assert not list(work.glob(".stage-*"))
