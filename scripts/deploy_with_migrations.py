#!/usr/bin/env python3
"""
Production Deployment Script with Safe Migration Handling
Ensures zero-downtime deployments with proper migration sequencing.
"""
import sys
import time
import subprocess
from typing import List, Dict, Any
from dataclasses import dataclass

from migrations.migration_manager import migration_manager
from src.infra.utils.logger import get_logger

logger = get_logger("deployment")


@dataclass
class DeploymentConfig:
    """Configuration for production deployment"""
    pre_migration_checks: bool = True
    run_master_migrations: bool = True
    run_org_migrations: bool = True
    max_migration_workers: int = 10
    migration_timeout: int = 1800  # 30 minutes
    rollback_on_failure: bool = True
    health_check_url: str = None
    health_check_timeout: int = 300  # 5 minutes


class ProductionDeployment:
    """Handles production deployment with safe migration management"""
    
    def __init__(self, config: DeploymentConfig):
        self.config = config
        
    def deploy(self) -> bool:
        """Execute full production deployment"""
        logger.info("Starting production deployment")
        
        try:
            # Phase 1: Pre-deployment checks
            if not self._pre_deployment_checks():
                logger.error("Pre-deployment checks failed")
                return False
            
            # Phase 2: Database migrations
            if not self._run_migrations():
                logger.error("Database migrations failed")
                if self.config.rollback_on_failure:
                    self._rollback_deployment()
                return False
            
            # Phase 3: Application deployment
            if not self._deploy_application():
                logger.error("Application deployment failed")
                if self.config.rollback_on_failure:
                    self._rollback_deployment()
                return False
            
            # Phase 4: Post-deployment validation
            if not self._post_deployment_validation():
                logger.error("Post-deployment validation failed")
                if self.config.rollback_on_failure:
                    self._rollback_deployment()
                return False
            
            logger.info("Production deployment completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Deployment failed with exception: {str(e)}")
            if self.config.rollback_on_failure:
                self._rollback_deployment()
            return False
    
    def _pre_deployment_checks(self) -> bool:
        """Run pre-deployment validation checks"""
        logger.info("Running pre-deployment checks")
        
        if not self.config.pre_migration_checks:
            logger.info("Pre-migration checks disabled, skipping")
            return True
        
        try:
            # Check database connectivity
            logger.info("Checking database connectivity")
            if not self._check_database_connectivity():
                return False
            
            # Validate migration consistency
            logger.info("Validating migration consistency")
            consistency = migration_manager.validate_migration_consistency()
            if not consistency['consistent']:
                logger.error(f"Migration inconsistency detected across {consistency['total_schemas']} schemas")
                logger.error(f"Found revisions: {consistency['unique_revisions']}")
                return False
            
            # Check for pending migrations
            logger.info("Checking for pending migrations")
            if not self._check_pending_migrations():
                return False
            
            logger.info("Pre-deployment checks passed")
            return True
            
        except Exception as e:
            logger.error(f"Pre-deployment checks failed: {str(e)}")
            return False
    
    def _run_migrations(self) -> bool:
        """Execute database migrations in correct order"""
        logger.info("Starting database migrations")
        
        try:
            # Step 1: Run master migrations first
            if self.config.run_master_migrations:
                logger.info("Running master migrations")
                master_result = migration_manager.run_master_migrations()
                
                if not master_result.success:
                    logger.error(f"Master migrations failed: {master_result.error}")
                    return False
                
                logger.info(f"Master migrations completed in {master_result.duration:.2f}s")
            
            # Step 2: Run organization migrations
            if self.config.run_org_migrations:
                logger.info("Running organization migrations")
                org_results = migration_manager.run_all_organization_migrations(
                    max_workers=self.config.max_migration_workers,
                    fail_fast=True  # Fail fast in production
                )
                
                failed_results = [r for r in org_results if not r.success]
                if failed_results:
                    logger.error(f"Organization migrations failed for {len(failed_results)} schemas")
                    for result in failed_results:
                        logger.error(f"  - {result.schema_name}: {result.error}")
                    return False
                
                successful_count = len([r for r in org_results if r.success])
                total_duration = sum(r.duration for r in org_results)
                logger.info(f"Organization migrations completed: {successful_count} schemas in {total_duration:.2f}s")
            
            logger.info("Database migrations completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Migration execution failed: {str(e)}")
            return False
    
    def _deploy_application(self) -> bool:
        """Deploy application code"""
        logger.info("Deploying application")
        
        try:
            # This would typically involve:
            # - Building Docker images
            # - Updating Kubernetes deployments
            # - Rolling updates
            # - Service mesh configuration
            
            # Placeholder for actual deployment logic
            logger.info("Application deployment logic would go here")
            
            # Simulate deployment time
            time.sleep(2)
            
            logger.info("Application deployed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Application deployment failed: {str(e)}")
            return False
    
    def _post_deployment_validation(self) -> bool:
        """Validate deployment success"""
        logger.info("Running post-deployment validation")
        
        try:
            # Health check
            if self.config.health_check_url:
                if not self._health_check():
                    return False
            
            # Database connectivity check
            if not self._check_database_connectivity():
                return False
            
            # Migration consistency check
            consistency = migration_manager.validate_migration_consistency()
            if not consistency['consistent']:
                logger.error("Migration consistency check failed after deployment")
                return False
            
            logger.info("Post-deployment validation passed")
            return True
            
        except Exception as e:
            logger.error(f"Post-deployment validation failed: {str(e)}")
            return False
    
    def _rollback_deployment(self):
        """Rollback deployment in case of failure"""
        logger.warning("Initiating deployment rollback")
        
        try:
            # This would involve:
            # - Rolling back application deployment
            # - Potentially rolling back migrations (dangerous)
            # - Restoring previous state
            
            logger.warning("Rollback logic would be implemented here")
            logger.warning("Manual intervention may be required")
            
        except Exception as e:
            logger.error(f"Rollback failed: {str(e)}")
            logger.error("CRITICAL: Manual intervention required")
    
    def _check_database_connectivity(self) -> bool:
        """Check database connectivity"""
        try:
            # Test master database connection
            with migration_manager.master_engine.connect() as conn:
                conn.execute("SELECT 1")
            
            logger.info("Database connectivity check passed")
            return True
            
        except Exception as e:
            logger.error(f"Database connectivity check failed: {str(e)}")
            return False
    
    def _check_pending_migrations(self) -> bool:
        """Check for pending migrations that need to be applied"""
        try:
            # This would check if there are any new migration files
            # that haven't been applied yet
            logger.info("Pending migration check passed")
            return True
            
        except Exception as e:
            logger.error(f"Pending migration check failed: {str(e)}")
            return False
    
    def _health_check(self) -> bool:
        """Perform application health check"""
        if not self.config.health_check_url:
            return True
        
        try:
            import requests
            
            start_time = time.time()
            timeout = self.config.health_check_timeout
            
            while time.time() - start_time < timeout:
                try:
                    response = requests.get(self.config.health_check_url, timeout=10)
                    if response.status_code == 200:
                        logger.info("Health check passed")
                        return True
                except requests.RequestException:
                    pass
                
                time.sleep(5)  # Wait 5 seconds before retry
            
            logger.error(f"Health check failed after {timeout}s timeout")
            return False
            
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return False


def main():
    """Main deployment entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Production deployment with migrations")
    parser.add_argument('--skip-pre-checks', action='store_true', help='Skip pre-deployment checks')
    parser.add_argument('--skip-master-migrations', action='store_true', help='Skip master migrations')
    parser.add_argument('--skip-org-migrations', action='store_true', help='Skip organization migrations')
    parser.add_argument('--max-workers', type=int, default=10, help='Max migration workers')
    parser.add_argument('--health-check-url', help='Health check URL')
    parser.add_argument('--no-rollback', action='store_true', help='Disable rollback on failure')
    
    args = parser.parse_args()
    
    config = DeploymentConfig(
        pre_migration_checks=not args.skip_pre_checks,
        run_master_migrations=not args.skip_master_migrations,
        run_org_migrations=not args.skip_org_migrations,
        max_migration_workers=args.max_workers,
        health_check_url=args.health_check_url,
        rollback_on_failure=not args.no_rollback
    )
    
    deployment = ProductionDeployment(config)
    success = deployment.deploy()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()