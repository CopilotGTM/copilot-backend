"""
Placeholder for unified environment/config loader.

DEPRECATED: Use src.infra.config.configs instead.
This file is maintained for backward compatibility only.
"""

# Import from the actual config location for backward compatibility
from src.infra.config.configs import (
    app_config,
    database_config,
    nango_integration_config,
)


# Legacy settings object for backward compatibility
class Settings:
    """Deprecated: Use configs directly from src.infra.config.configs"""

    pass


settings = Settings()
