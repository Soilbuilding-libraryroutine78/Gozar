"""Root test configuration.

Makes the suite hermetic with respect to a developer's local ``.env``. Gozar's
``Settings`` loads a ``.env`` file by default for local-dev convenience, but the
tests must never pick up a developer's real configuration (e.g. an
``GOZAR_APP_ENV=production`` line that would make app-startup tests fail closed on a
missing database URL). Setting ``GOZAR_ENV_FILE`` to empty here -- before
``gozar.core.config`` is imported -- disables dotenv loading for the whole run, so
tests rely solely on explicit fixtures and process env.
"""

import os

os.environ["GOZAR_ENV_FILE"] = ""
