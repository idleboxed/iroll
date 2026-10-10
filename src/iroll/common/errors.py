"""Expected build/preflight errors. SPDX-License-Identifier: BSD-3-Clause."""


class BuildError(Exception):
    """An actionable failure, without implicit retries or privilege escalation."""
