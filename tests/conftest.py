from __future__ import annotations

import json
from pathlib import Path

import pytest

from settlerec.generate import generate
from settlerec.runner import run_pipeline


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("settlerec-data")
    generate(path)
    return path


@pytest.fixture(scope="session")
def truth(data_dir) -> dict:
    return json.loads((data_dir / "truth.json").read_text())


@pytest.fixture(scope="session")
def con(data_dir):
    connection = run_pipeline(data_dir)
    yield connection
    connection.close()
