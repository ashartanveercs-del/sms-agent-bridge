import io

from smsbridge.__main__ import main, read_env_file

DRY_RUN_ENV = {
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "AGENT_BACKEND": "echo",
    "DRY_RUN": "true",
    "PORT": "18080",
}


def test_inspect_names_the_character_that_forces_ucs2(capsys):
    assert main(["inspect", "Open Sat" + chr(0x2011) + "Sun until noon."]) == 0
    out = capsys.readouterr().out
    assert "UCS-2" in out and "GSM-7" in out
    assert "U+2011 NON-BREAKING HYPHEN" in out
    assert "Open Sat-Sun until noon." in out


def test_inspect_reads_from_stdin(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("**Hi** there"))
    assert main(["inspect", "-"]) == 0
    assert "Hi there" in capsys.readouterr().out


def test_inspect_reports_what_the_sanitiser_removed(capsys):
    main(["inspect", "Done.\nValueError: boom"])
    out = capsys.readouterr().out
    assert "dropped 1 line" in out
    assert "ValueError" not in out.split("part 1/1", 1)[1]


def test_inspect_strict_mode_makes_a_latin_reply_gsm7(capsys):
    main(["inspect", "--mode", "strict", "caf" + chr(0xE9) + " " + chr(0x1F600)])
    out = capsys.readouterr().out
    assert "outbound  GSM-7" in out


def test_env_files_are_parsed_like_docker_env_files(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("# comment\nA=1\nB = 'two'\n\nC=\"three\"\nNOEQUALS\nD=x=y\n",
                    encoding="utf-8")
    assert read_env_file(path) == {"A": "1", "B": "two", "C": "three", "D": "x=y"}


def test_serve_reports_configuration_errors_without_a_traceback(capsys, tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    assert main(["serve", "--env-file", str(empty)], environ={}) == 2
    err = capsys.readouterr().err
    assert "Missing required settings" in err
    assert "Traceback" not in err


def test_serve_runs_the_app_with_settings_from_the_env_file(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr("smsbridge.__main__.web.run_app",
                        lambda app, **kwargs: started.update(kwargs))
    path = tmp_path / "settings.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in DRY_RUN_ENV.items()), encoding="utf-8")

    assert main(["serve", "--env-file", str(path)], environ={}) == 0
    assert (started["host"], started["port"]) == ("127.0.0.1", 18080)


def test_the_process_environment_wins_over_the_env_file(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr("smsbridge.__main__.web.run_app",
                        lambda app, **kwargs: started.update(kwargs))
    path = tmp_path / "settings.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in DRY_RUN_ENV.items()), encoding="utf-8")

    main(["serve", "--env-file", str(path)], environ={"PORT": "19090"})
    assert started["port"] == 19090


def test_serve_is_the_default_command(monkeypatch):
    started = {}
    monkeypatch.setattr("smsbridge.__main__.web.run_app",
                        lambda app, **kwargs: started.update(kwargs))
    assert main([], environ=DRY_RUN_ENV) == 0
    assert started["port"] == 18080
