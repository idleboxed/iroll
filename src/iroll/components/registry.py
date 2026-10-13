"""Component recipes and immutable upstream pins; CLI core switches are generated here.

Adding a Make-based core requires one Component entry, not new orchestration code.
SPDX-License-Identifier: BSD-3-Clause
"""

from dataclasses import dataclass

from ..common import paths
from ..common.errors import BuildError

ALL_PLATFORMS = ("linux-x86_64", "rk3032")


@dataclass(frozen=True)
class Source:
    archive: str
    sha256: str
    url: str
    revision: str


@dataclass(frozen=True)
class Submodule:
    directory: str
    source: Source


@dataclass(frozen=True)
class JsonResources:
    directory: str


@dataclass(frozen=True)
class Component:
    name: str
    recipe: str
    artifact: str
    destination: str
    source: Source | None = None
    directory: str = "."
    makefile: str = "Makefile"
    platforms: tuple[str, ...] = ALL_PLATFORMS
    game_platforms: tuple[str, ...] = ()
    resources: tuple[JsonResources, ...] = ()
    unavailable_reason: str = ""
    submodules: tuple[Submodule, ...] = ()
    make_args: tuple[str, ...] = ()
    arm_make_args: tuple[str, ...] = ()
    host_tools: tuple[str, ...] = ()
    cli_selectable: bool = True
    default_selected: bool = True

    @property
    def core(self) -> bool:
        return bool(self.game_platforms)


COMPONENTS = (
    Component(
        "igui",
        "cargo",
        "igui",
        "igui/igui",
        directory="igui",
        resources=(
            JsonResources("locales"),
            JsonResources("controls"),
        ),
    ),
    Component("iman", "cargo", "iman", "iman/iman", directory="iman"),
    Component(
        "itrampoline", "trampoline", "itrampoline", "itrampoline",
        directory="itrampoline", platforms=("rk3032",), default_selected=False,
        unavailable_reason="The stock-menu trampoline targets RK3032 ARMv7 only",
    ),
    Component(
        "retro",
        "configure",
        "retroarch",
        "retroarch/retroarch",
        source=Source(
            "retroarch-sourceonly-1.22.2.tar.xz",
            "2a8b1713f7f4d2b53bad3e2297e48d78f5666098cf00d583d3e08f3c213f8aa6",
            "https://github.com/libretro/RetroArch/releases/download/v1.22.2/retroarch-sourceonly-1.22.2.tar.xz",
            "1.22.2",
        ),
    ),
    Component(
        "core-nestopia",
        "make",
        "nestopia_libretro.so",
        f"{paths.CORES_DIR}/nestopia_libretro.so",
        directory="libretro",
        game_platforms=("NES", "FDS"),
        source=Source(
            "nestopia-224713b257c8620513c1c9ca28dc7f8dea711e18.tar.gz",
            "48f32948ade239d4e912a4b993f6e70f2cd8f4471194275fc376f15a073397ef",
            "https://codeload.github.com/libretro/nestopia/tar.gz/224713b257c8620513c1c9ca28dc7f8dea711e18",
            "224713b257c8620513c1c9ca28dc7f8dea711e18",
        ),
    ),
    Component(
        "core-gambatte",
        "make",
        "gambatte_libretro.so",
        f"{paths.CORES_DIR}/gambatte_libretro.so",
        makefile="Makefile.libretro",
        game_platforms=("GB", "GBC"),
        source=Source(
            "gambatte-libretro-d9d6cd06382d1ced30de34d56d3609452323dab1.tar.gz",
            "bd39cd38662135d17e221a7fd34baf2908a40065ade16edf2e8d1168b21b921e",
            "https://codeload.github.com/libretro/gambatte-libretro/tar.gz/d9d6cd06382d1ced30de34d56d3609452323dab1",
            "d9d6cd06382d1ced30de34d56d3609452323dab1",
        ),
    ),
    Component(
        "core-snes9x2010",
        "make",
        "snes9x2010_libretro.so",
        f"{paths.CORES_DIR}/snes9x2010_libretro.so",
        makefile="Makefile.libretro",
        game_platforms=("SNES",),
        # Upstream enables libretro-common threads; glibc < 2.34 still needs libpthread.
        make_args=("LIBS=-lpthread",),
        source=Source(
            "snes9x2010-fe690dd321fa5a46b5234a2bde089d2518c62b0e.tar.gz",
            "7d2d2a21c2c00e6c5a83c8b51c18fccb6a9b3764584aa5ceb4b231e3a7c0ab6c",
            "https://codeload.github.com/libretro/snes9x2010/tar.gz/fe690dd321fa5a46b5234a2bde089d2518c62b0e",
            "fe690dd321fa5a46b5234a2bde089d2518c62b0e",
        ),
    ),
    Component(
        "core-gpsp",
        "make",
        "gpsp_libretro.so",
        f"{paths.CORES_DIR}/gpsp_libretro.so",
        makefile="Makefile",
        game_platforms=("GBA",),
        arm_make_args=("CPU_ARCH=arm", "HAVE_DYNAREC=1", "MMAP_JIT_CACHE=1"),
        source=Source(
            "gpsp-5819380c2ffb0900219d700a382ee68c464ebb99.tar.gz",
            "1dfc919966cd58ce1d8f2580041d9bffe2e313d9442260c713ed0286a048a733",
            "https://codeload.github.com/libretro/gpsp/tar.gz/5819380c2ffb0900219d700a382ee68c464ebb99",
            "5819380c2ffb0900219d700a382ee68c464ebb99",
        ),
    ),
    Component(
        "core-picodrive",
        "make",
        "picodrive_libretro.so",
        f"{paths.CORES_DIR}/picodrive_libretro.so",
        makefile="Makefile.libretro",
        game_platforms=("MD", "SMS", "GG", "SG1000", "SC3000", "SCD", "32X", "PICO"),
        # Cyclone generates ARM assembly with a native executable; do not inherit cross Make overrides.
        make_args=("MAKEOVERRIDES=", "CYCLONE_CC=cc", "CYCLONE_CXX=c++", "PLATFORM_TREMOR=1", "PLATFORM_ZLIB=1"),
        host_tools=("cc", "c++"),
        submodules=(
            Submodule(
                "cpu/cyclone",
                Source(
                    "cyclone68000-3ac7cf1bdeecb60e2414980e8dc72ff092f69769.tar.gz",
                    "e570e121721441c1bf8015a8bce8614b73954e2344b983b3650e591952f74a6c",
                    "https://codeload.github.com/irixxxx/cyclone68000/tar.gz/3ac7cf1bdeecb60e2414980e8dc72ff092f69769",
                    "3ac7cf1bdeecb60e2414980e8dc72ff092f69769",
                ),
            ),
            Submodule(
                "pico/cd/libchdr",
                Source(
                    "libchdr-e62ac5995b1c7ef65ece35293914843b8ee57d49.tar.gz",
                    "59b99005d3d6317a0d1e45e1c4abf0e551070dbf32c8fdc66ed5b4ae6d8ea702",
                    "https://codeload.github.com/irixxxx/libchdr/tar.gz/e62ac5995b1c7ef65ece35293914843b8ee57d49",
                    "e62ac5995b1c7ef65ece35293914843b8ee57d49",
                ),
            ),
            Submodule(
                "pico/sound/emu2413",
                Source(
                    "emu2413-a2dfc20ff507e4fd075cd325620bcea655e2c1f7.tar.gz",
                    "bd816079cfbf13c56a260c095d6b8e260c2f877e5b13f50d4e570b96e8b92830",
                    "https://codeload.github.com/digital-sound-antiques/emu2413/tar.gz/"
                    "a2dfc20ff507e4fd075cd325620bcea655e2c1f7",
                    "a2dfc20ff507e4fd075cd325620bcea655e2c1f7",
                ),
            ),
            Submodule(
                "platform/common/dr_libs",
                Source(
                    "dr_libs-dd762b861ecadf5ddd5fb03e9ca1db6707b54fbb.tar.gz",
                    "8f0be8a4aa27cd4ddaba41acbb845b1641964f2cc2f5a5f7b4b72f73368c1c12",
                    "https://codeload.github.com/mackron/dr_libs/tar.gz/dd762b861ecadf5ddd5fb03e9ca1db6707b54fbb",
                    "dd762b861ecadf5ddd5fb03e9ca1db6707b54fbb",
                ),
            ),
            Submodule(
                "platform/libpicofe",
                Source(
                    "libpicofe-9ed5822606dd7ff20a782a882e8fd611cb53ba88.tar.gz",
                    "458519aaa70916528e593f1f052aa45f704670fba094b409478a8197e6f076a7",
                    "https://codeload.github.com/irixxxx/libpicofe/tar.gz/9ed5822606dd7ff20a782a882e8fd611cb53ba88",
                    "9ed5822606dd7ff20a782a882e8fd611cb53ba88",
                ),
            ),
        ),
        source=Source(
            "picodrive-ab021146b70eef7ec0ac2afe06a94e9b4c16ef74.tar.gz",
            "68317ec58c501f2b57057db4c12bd5990ae2941e5f015334c85770e86d39523f",
            "https://codeload.github.com/libretro/picodrive/tar.gz/ab021146b70eef7ec0ac2afe06a94e9b4c16ef74",
            "ab021146b70eef7ec0ac2afe06a94e9b4c16ef74",
        ),
    ),
    Component(
        "core-pcsx-rearmed",
        "make",
        "pcsx_rearmed_libretro.so",
        f"{paths.CORES_DIR}/pcsx_rearmed_libretro.so",
        makefile="Makefile.libretro",
        game_platforms=("PSX",),
        make_args=("HAVE_PHYSICAL_CDROM=0",),
        arm_make_args=("DYNAREC=ari64", "BUILTIN_GPU=neon", "HAVE_NEON_ASM=1"),
        source=Source(
            "pcsx_rearmed-ff81ed17a15241d2f3730cdd7585b38e172532ca.tar.gz",
            "23e0006435e66503e1b862daaf8a47cb6bee120f4ca82482c08ec129668a35c5",
            "https://codeload.github.com/libretro/pcsx_rearmed/tar.gz/ff81ed17a15241d2f3730cdd7585b38e172532ca",
            "ff81ed17a15241d2f3730cdd7585b38e172532ca",
        ),
    ),
    Component(
        "core-mupen64plus-next",
        "make",
        "mupen64plus_next_libretro.so",
        f"{paths.CORES_DIR}/mupen64plus_next_libretro.so",
        platforms=("rk3032",),
        game_platforms=("N64",),
        unavailable_reason="The prepared N64 recipe targets ARMv7/GLES2 on rk3032 only",
        # Never let the build host select x86 dynarec or desktop OpenGL.
        arm_make_args=(
            "platform=armv-neon-hardfloat", "ARCH=arm", "WITH_DYNAREC=arm",
            "FORCE_GLES=1", "FORCE_GLES3=0", "LLE=0", "HAVE_PARALLEL_RSP=0", "HAVE_PARALLEL_RDP=0",
        ),
        host_tools=("awk", "strings", "tr"),
        cli_selectable=False,
        source=Source(
            "mupen64plus-libretro-nx-12edd2c74a517ff86dfa8cfc71ad75e4c10486d5.tar.gz",
            "c2ea1cd75632e859b0d7cbe6d1e75e90c556aecdd204019ecf13a841bf96a021",
            "https://codeload.github.com/libretro/mupen64plus-libretro-nx/tar.gz/"
            "12edd2c74a517ff86dfa8cfc71ad75e4c10486d5",
            "12edd2c74a517ff86dfa8cfc71ad75e4c10486d5",
        ),
    ),
)


def select(platform: str, names: list[str], *, cores: bool = False) -> tuple[Component, ...]:
    registry = {component.name: component for component in COMPONENTS}
    requested = set(names)
    unknown = requested - registry.keys()

    if unknown:
        raise BuildError(f"Unknown components: {", ".join(sorted(unknown))}")

    for name in requested:
        component = registry[name]

        if platform not in component.platforms:
            raise BuildError(f"{name} is unavailable for {platform}: {component.unavailable_reason}")

    if cores:
        requested.update(
            component.name for component in COMPONENTS if component.core and platform in component.platforms
        )

    if not names and not cores:
        requested.update(
            component.name for component in COMPONENTS
            if platform in component.platforms and component.default_selected
        )

    if not requested:
        raise BuildError("No components are available for this selection")

    return tuple(component for component in COMPONENTS if component.name in requested)
