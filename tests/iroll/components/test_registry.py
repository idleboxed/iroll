"""Public component selection; no compilers, network or hardware.

SPDX-License-Identifier: BSD-3-Clause
"""

import pytest

from iroll.common.errors import BuildError
from iroll.host.platforms import DEFAULT_PLATFORM
from iroll.components.registry import COMPONENTS, select


@pytest.mark.parametrize(
    ("platform", "names", "cores", "expected"),
    [
        pytest.param(
            DEFAULT_PLATFORM, [], False,
            {component.name for component in COMPONENTS if DEFAULT_PLATFORM in component.platforms},
            id="default-native-components",
        ),
        pytest.param("rk3032", [], False, {component.name for component in COMPONENTS}, id="default-arm-components"),
        pytest.param(DEFAULT_PLATFORM, ["iman", "iman"], False, {"iman"}, id="deduplicate-selection"),
        pytest.param(
            DEFAULT_PLATFORM, ["retro"], True,
            {"retro", *(
                component.name for component in COMPONENTS
                if component.core and DEFAULT_PLATFORM in component.platforms
            )},
            id="retroarch-and-native-cores",
        ),
        pytest.param(
            DEFAULT_PLATFORM, [], True,
            {component.name for component in COMPONENTS if component.core and DEFAULT_PLATFORM in component.platforms},
            id="native-cores-only",
        ),
        pytest.param(
            "rk3032", [], True,
            {component.name for component in COMPONENTS if component.core and "rk3032" in component.platforms},
            id="arm-cores-only",
        ),
    ],
)
def test_component_selection(platform: str, names: list[str], cores: bool, expected: set[str]) -> None:
    selected = select(platform, names, cores=cores)

    assert {component.name for component in selected} == expected


def test_unknown_component_selection_is_rejected() -> None:

    with pytest.raises(BuildError, match="Unknown components: missing"):
        select(DEFAULT_PLATFORM, ["missing"])


def test_n64_recipe_refuses_native_selection_and_keeps_verified_source_pin() -> None:
    core = next(component for component in COMPONENTS if component.name == "core-mupen64plus-next")

    with pytest.raises(BuildError, match="unavailable for linux-x86_64"):
        select(DEFAULT_PLATFORM, [core.name])

    assert core.cli_selectable is False
    assert core.source.revision == "12edd2c74a517ff86dfa8cfc71ad75e4c10486d5"
    assert core.source.sha256 == "c2ea1cd75632e859b0d7cbe6d1e75e90c556aecdd204019ecf13a841bf96a021"
    assert core.source.url.endswith(f"/{core.source.revision}")
    assert core.submodules == ()
