import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("H2H_ADVISOR_HOME", str(tmp_path))
    return tmp_path
