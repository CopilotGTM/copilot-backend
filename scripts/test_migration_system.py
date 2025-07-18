#!/usr/bin/env python3
"""
Comprehensive test script for the migration detection system.
Tests all scenarios and provides detailed feedback.
"""

import os
import sys
import subprocess
import time
import json
from pathlib import Path
from typing import Dict, Any, List

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class MigrationSystemTester:
    """Comprehensive tester for the migration detection system."""
    
    def __init__(self):
        self.project_root = PROJECT_ROOT
        self.test_results = []
        self.current_test = None
    
    def log_test(self, test_name: str, success: bool, message: str = ""):
        """Log test result."""
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if message:
            print(f"   {message}")
        
        self.test_results.append({
            "test": test_name,
            "success": success,
            "message": message
        })
    
    def run_command(self, command: List[str], capture_output: bool = True) -> Dict[str, Any]:
        """Run a command and return results."""
        try:
            result = subprocess.run(
                command,
                capture_output=capture_output,
                text=True,
                cwd=self.project_root
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": 1
            }
    
    def test_1_dry_run_no_changes(self):
        """Test 1: Dry run with no changes."""
        print("\n🧪 Test 1: Dry run with no changes")
        
        result = self.run_command([
            "python", "scripts/deploy_with_migrations.py", "--dry-run"
        ])
        
        success = result["success"] and "Skipping migrations" in result["stdout"]
        self.log_test(
            "Dry run with no changes",
            success,
            "Should detect no changes and skip migrations"
        )
    
    def test_2_force_migrations(self):
        """Test 2: Force migrations."""
        print("\n🧪 Test 2: Force migrations")
        
        result = self.run_command([
            "python", "scripts/deploy_with_migrations.py", "--force-migrations", "--dry-run"
        ])
        
        success = result["success"] and "Running migrations" in result["stdout"]
        self.log_test(
            "Force migrations flag",
            success,
            "Should force run migrations even when not needed"
        )
    
    def test_3_skip_migrations(self):
        """Test 3: Skip migrations."""
        print("\n🧪 Test 3: Skip migrations")
        
        result = self.run_command([
            "python", "scripts/deploy_with_migrations.py", "--skip-migrations", "--dry-run"
        ])
        
        success = result["success"] and "Skipping migrations" in result["stdout"]
        self.log_test(
            "Skip migrations flag",
            success,
            "Should skip migrations even when needed"
        )
    
    def test_4_master_migrations_script(self):
        """Test 4: Master migrations script."""
        print("\n🧪 Test 4: Master migrations script")
        
        result = self.run_command([
            "python", "scripts/run_master_migrations.py", "--help"
        ])
        
        success = result["success"]
        self.log_test(
            "Master migrations script",
            success,
            "Should show help without errors"
        )
    
    def test_5_org_migrations_script(self):
        """Test 5: Organization migrations script."""
        print("\n🧪 Test 6: Organization migrations script")
        
        result = self.run_command([
            "python", "scripts/run_org_migrations.py", "--help"
        ])
        
        success = result["success"]
        self.log_test(
            "Organization migrations script",
            success,
            "Should show help without errors"
        )
    
    def test_6_create_organization_script(self):
        """Test 6: Create organization script."""
        print("\n🧪 Test 6: Create organization script")
        
        result = self.run_command([
            "python", "scripts/create_organization.py", "--help"
        ])
        
        success = result["success"]
        self.log_test(
            "Create organization script",
            success,
            "Should show help without errors"
        )
    
    def test_7_python_imports(self):
        """Test 7: Python imports."""
        print("\n🧪 Test 7: Python imports")
        
        try:
            # Test migration router import
            import src.api.routers.admin.migrations
            migration_import = True
        except Exception as e:
            migration_import = False
            print(f"   Migration router import failed: {e}")
        
        try:
            # Test deployment script import
            import scripts.deploy_with_migrations
            deployment_import = True
        except Exception as e:
            deployment_import = False
            print(f"   Deployment script import failed: {e}")
        
        try:
            # Test organization management import
            from src.service.organization_management import organization_management_service
            org_import = True
        except Exception as e:
            org_import = False
            print(f"   Organization management import failed: {e}")
        
        success = migration_import and deployment_import and org_import
        self.log_test(
            "Python imports",
            success,
            "All key modules should import successfully"
        )
    
    def test_8_migration_files_exist(self):
        """Test 8: Migration files exist."""
        print("\n🧪 Test 8: Migration files exist")
        
        required_files = [
            "migrations/master_migrations/alembic.ini",
            "migrations/master_migrations/env.py",
            "migrations/master_migrations/script.py.mako",
            "migrations/master_migrations/versions/001_add_supabase_support_and_chat_timestamps.py",
            "migrations/master_migrations/versions/002_add_organization_schema_fields.py",
            "migrations/org_migrations/alembic.ini",
            "migrations/org_migrations/env.py",
            "migrations/org_migrations/script.py.mako",
            "migrations/org_migrations/versions/001_initial_org_tables.py",
        ]
        
        missing_files = []
        for file_path in required_files:
            if not (self.project_root / file_path).exists():
                missing_files.append(file_path)
        
        success = len(missing_files) == 0
        self.log_test(
            "Migration files exist",
            success,
            f"Missing files: {missing_files}" if missing_files else "All required files exist"
        )
    
    def test_9_script_permissions(self):
        """Test 9: Script permissions."""
        print("\n🧪 Test 9: Script permissions")
        
        scripts_to_check = [
            "scripts/deploy_with_migrations.py",
            "scripts/run_master_migrations.py",
            "scripts/run_org_migrations.py",
            "scripts/create_organization.py",
            "scripts/delete_organizations.py",
            "scripts/reset_alembic.py",
        ]
        
        non_executable = []
        for script_path in scripts_to_check:
            full_path = self.project_root / script_path
            if full_path.exists() and not os.access(full_path, os.X_OK):
                non_executable.append(script_path)
        
        success = len(non_executable) == 0
        self.log_test(
            "Script permissions",
            success,
            f"Non-executable scripts: {non_executable}" if non_executable else "All scripts are executable"
        )
    
    def test_10_database_connection(self):
        """Test 10: Database connection."""
        print("\n🧪 Test 10: Database connection")
        
        try:
            from src.infra.utils.database import get_master_session
            from sqlmodel import Session
            
            engine = get_master_session()
            session = Session(engine)
            
            # Test a simple query
            from sqlalchemy import text
            result = session.execute(text("SELECT 1"))
            result.fetchone()
            
            session.close()
            success = True
        except Exception as e:
            success = False
            print(f"   Database connection failed: {e}")
        
        self.log_test(
            "Database connection",
            success,
            "Should connect to database successfully"
        )
    
    def test_11_api_endpoints_exist(self):
        """Test 11: API endpoints exist."""
        print("\n🧪 Test 11: API endpoints exist")
        
        try:
            from src.api.fastapi_app import create_fastapi_app
            app = create_fastapi_app()
            
            # Get the underlying FastAPI app
            fastapi_app = app.other_asgi_app if hasattr(app, 'other_asgi_app') else app
            
            # Check if migration routes are included
            routes = [route.path for route in fastapi_app.routes]
            migration_routes = [
                "/admin/migrations/check",
                "/admin/migrations/run/master",
                "/admin/migrations/run/organizations",
                "/admin/migrations/run/all"
            ]
            
            missing_routes = []
            for route in migration_routes:
                if not any(route in r for r in routes):
                    missing_routes.append(route)
            
            success = len(missing_routes) == 0
            self.log_test(
                "API endpoints exist",
                success,
                f"Missing routes: {missing_routes}" if missing_routes else "All migration endpoints exist"
            )
        except Exception as e:
            self.log_test(
                "API endpoints exist",
                False,
                f"Failed to check API endpoints: {e}"
            )
    
    def run_all_tests(self):
        """Run all tests."""
        print("🚀 Starting Migration System Tests")
        print("=" * 50)
        
        # Run all tests
        self.test_1_dry_run_no_changes()
        self.test_2_force_migrations()
        self.test_3_skip_migrations()
        self.test_4_master_migrations_script()
        self.test_5_org_migrations_script()
        self.test_6_create_organization_script()
        self.test_7_python_imports()
        self.test_8_migration_files_exist()
        self.test_9_script_permissions()
        self.test_10_database_connection()
        self.test_11_api_endpoints_exist()
        
        # Print summary
        print("\n" + "=" * 50)
        print("📊 Test Summary")
        print("=" * 50)
        
        passed = sum(1 for result in self.test_results if result["success"])
        total = len(self.test_results)
        
        print(f"✅ Passed: {passed}/{total}")
        print(f"❌ Failed: {total - passed}/{total}")
        
        if passed == total:
            print("\n🎉 All tests passed! Your migration system is ready!")
        else:
            print("\n⚠️  Some tests failed. Please check the issues above.")
        
        # Print failed tests
        failed_tests = [result for result in self.test_results if not result["success"]]
        if failed_tests:
            print("\n❌ Failed Tests:")
            for test in failed_tests:
                print(f"   - {test['test']}: {test['message']}")
        
        return passed == total


def main():
    """Main test runner."""
    tester = MigrationSystemTester()
    success = tester.run_all_tests()
    
    if success:
        print("\n🚀 Ready to test with real scenarios!")
        print("\nNext steps:")
        print("1. Start your FastAPI server: uvicorn src.main:app --reload")
        print("2. Test API endpoints with curl or Postman")
        print("3. Test frontend integration")
        print("4. Test deployment scenarios")
        print("\nSee TESTING_GUIDE.md for detailed scenarios!")
    else:
        print("\n⚠️  Please fix the failed tests before proceeding.")
        sys.exit(1)


if __name__ == "__main__":
    main() 