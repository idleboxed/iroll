"""Build orchestration and explicit card initialization, never a launcher. SPDX-License-Identifier: BSD-3-Clause."""

import logging

__version__ = "0.6.0"
logging.getLogger(__name__).addHandler(logging.NullHandler())
