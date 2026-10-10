"""Synthetic sources, firmware, card metadata and tool responses; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import socket
from typing import NoReturn

import pytest



@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
