#!/usr/bin/env python3
"""
Script to manually submit a knowledge processing task for a specific source ID and organization ID.

This script connects to the Celery broker and submits a post_upload_process task
for the specified source and organization.
"""

import sys
from uuid import UUID
from src.workers.knowledge_worker.celery_app import app as knowledge_app
from src.infra.utils.logger import get_logger

logger = get_logger("submit_task")


def submit_knowledge_task(org_id: str, source_id: str):
    """
    Submit a knowledge processing task to the Celery queue.
    
    Args:
        org_id: Organization ID (UUID string)
        source_id: Source ID (UUID string)
    """
    try:
        # Validate UUIDs
        org_uuid = UUID(org_id)
        source_uuid = UUID(source_id)
                
        # Submit the task to Celery
        result = knowledge_app.send_task(
            "knowledge.post_upload_process",
            args=[str(org_uuid), str(source_uuid)],
            queue="knowledge_upload",
            routing_key="knowledge_upload",
        )
        
        
        return result
        
    except ValueError as e:
        logger.error(f"Invalid UUID format: {e}")
        print(f"❌ Error: Invalid UUID format - {e}")
        sys.exit(1)
        
    except Exception as e:
        logger.error(f"Failed to submit task: {e}")
        print(f"❌ Error: Failed to submit task - {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Default values from your request
    DEFAULT_ORG_ID = "a1683e9c-f3a0-4e30-909c-75862255ce6f"
    DEFAULT_SOURCE_ID = "10817181-1cbf-4b1f-8dba-7df237cf301d"
    
    # Check if command line arguments are provided
    if len(sys.argv) == 3:
        org_id = sys.argv[1]
        source_id = sys.argv[2]
    elif len(sys.argv) == 1:
        # Use default values
        org_id = DEFAULT_ORG_ID
        source_id = DEFAULT_SOURCE_ID
    else:
        print("Usage: python submit_task.py [org_id] [source_id]")
        print("Or run without arguments to use the default IDs:")
        print(f"  org_id: {DEFAULT_ORG_ID}")
        print(f"  source_id: {DEFAULT_SOURCE_ID}")
        sys.exit(1)
    
    print("🚀 Submitting knowledge processing task...")
    print("-" * 50)
    
    submit_knowledge_task(org_id, source_id)
