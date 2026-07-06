"""osvoice — local, provider-agnostic voice-to-voice server for Apple Silicon.

Keep this module import-light: do not import submodules that pull mlx / torch at
package import time, so the pure-logic layers and tests stay dependency-light.
"""

__version__ = "0.1.0"
