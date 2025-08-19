# Package initializer for Celery tasks

# Import all task modules to ensure they are registered with Celery
from . import organization
from . import technical_deck
from . import slide_outline
from . import image_processing

__all__ = ["organization", "technical_deck", "slide_outline", "image_processing"]
