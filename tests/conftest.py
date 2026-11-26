import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """No test reads or writes the real ~/.config or ~/.local (config, cache, notes)."""
    monkeypatch.setenv("H2H_ADVISOR_HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("H2H_ADVISOR_HOME", str(tmp_path))
    return tmp_path
