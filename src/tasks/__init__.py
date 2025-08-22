# Package initializer for Celery tasks

# Import all task modules to ensure they are registered with Celery
from . import organization
from . import technical_deck
from . import slide_outline
from . import image_processing
from . import knowledge_post_upload
from . import knowledge_document_extraction

__all__ = [
	"organization",
	"technical_deck",
	"slide_outline",
	"image_processing",
	"knowledge_post_upload",
	"knowledge_document_extraction",
]
