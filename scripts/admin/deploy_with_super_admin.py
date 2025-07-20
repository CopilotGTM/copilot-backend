#!/usr/bin/env python3
"""
Deployment Script with Super Admin Initialization

This script handles the complete deployment process including:
1. Database migrations
2. Super admin initialization
3. Organization setup (if needed)

Usage:
    python scripts/admin/deploy_with_super_admin.py
"""

import os
import sys
import subprocess
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from src.infra.utils.logger import get_logger
from scripts.admin.init_super_admin import execute_create_super_admin

logger = get_logger()


def run_command(command: str, description: str) -> bool:
    """
    Run a shell command and log the result
    
    Args:
        command: Command to run
        description: Description of what the command does
        
    Returns:
        True if successful, False otherwise
    """
    logger.info(f"Running: {description}")
    logger.debug(f"Command: {command}")
    
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            capture_output=True,
            text=True
        )
        logger.info(f"✅ {description} completed successfully")
        if result.stdout:
            logger.debug(f"Output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ {description} failed")
        logger.error(f"Error: {e.stderr}")
        return False


def check_environment_variables() -> bool:
    """
    Check if required environment variables are set
    
    Returns:
        True if all required variables are set, False otherwise
    """
    required_vars = [
        "SUPER_ADMIN_EMAIL",
        "DATABASE_HOST",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_NAME"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    logger.info("✅ All required environment variables are set")
    return True


def run_master_migrations() -> bool:
    """
    Run master database migrations
    
    Returns:
        True if successful, False otherwise
    """
    return run_command(
        "python scripts/run_master_migrations.py",
        "Master database migrations"
    )


# def initialize_super_admin() -> bool:
#     """
#     Initialize super admin account
    
#     Returns:
#         True if successful, False otherwise
#     """
#     return run_command(
#         "python scripts/admin/init_super_admin.py",
#         "Super admin initialization"
#     )


def main():
    """
    Main deployment function
    """
    logger.info("🚀 Starting deployment with super admin initialization...")
    
    # Load environment variables
    load_dotenv()
    
    # Check environment variables
    if not check_environment_variables():
        logger.error("❌ Environment check failed. Please set all required variables.")
        sys.exit(1)
    
    # Step 1: Run master migrations
    if not run_master_migrations():
        logger.error("❌ Master migrations failed")
        sys.exit(1)
    
    # Step 2: Initialize super admin
    execute_create_super_admin()
    
    logger.info("🎉 Deployment completed successfully!")

if __name__ == "__main__":
    main() 