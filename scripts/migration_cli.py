#!/usr/bin/env python3
"""
Production Migration CLI for Multi-Tenant Architecture
Provides safe migration commands for production environments.
"""
import click
import sys
from typing import Optional

try:
    from tabulate import tabulate
except ImportError:
    # Fallback if tabulate is not available
    def tabulate(data, headers=None, tablefmt="grid"):
        if not data:
            return "No data"
        
        # Simple table formatting fallback
        result = []
        if headers:
            result.append(" | ".join(headers))
            result.append("-" * len(" | ".join(headers)))
        
        for row in data:
            result.append(" | ".join(str(cell) for cell in row))
        
        return "\n".join(result)

from migrations.migration_manager import migration_manager
from src.infra.utils.logger import get_logger

logger = get_logger("migration_cli")


@click.group()
def cli():
    """Production Migration CLI for Multi-Tenant Architecture"""
    pass


@cli.command()
@click.option('--target', default='head', help='Target revision (default: head)')
@click.option('--dry-run', is_flag=True, help='Show what would be migrated without executing')
def migrate_master(target: str, dry_run: bool):
    """Run master database migrations"""
    if dry_run:
        click.echo(f"DRY RUN: Would migrate master to revision: {target}")
        return
    
    click.echo(f"Running master migrations to revision: {target}")
    result = migration_manager.run_master_migrations(target)
    
    if result.success:
        click.echo(f"✅ Master migration completed in {result.duration:.2f}s")
    else:
        click.echo(f"❌ Master migration failed: {result.error}")
        sys.exit(1)


@cli.command()
@click.option('--schema', help='Specific schema to migrate')
@click.option('--target', default='head', help='Target revision (default: head)')
@click.option('--max-workers', default=5, help='Maximum concurrent migrations')
@click.option('--fail-fast', is_flag=True, help='Stop on first failure')
@click.option('--dry-run', is_flag=True, help='Show what would be migrated without executing')
def migrate_orgs(schema: Optional[str], target: str, max_workers: int, fail_fast: bool, dry_run: bool):
    """Run organization migrations"""
    if dry_run:
        if schema:
            click.echo(f"DRY RUN: Would migrate schema {schema} to revision: {target}")
        else:
            schemas = migration_manager._get_all_organization_schemas()
            click.echo(f"DRY RUN: Would migrate {len(schemas)} schemas to revision: {target}")
            for s in schemas:
                click.echo(f"  - {s}")
        return
    
    if schema:
        # Migrate single schema
        click.echo(f"Migrating schema: {schema}")
        result = migration_manager.run_organization_migration(schema, target)
        
        if result.success:
            click.echo(f"✅ Migration completed for {schema} in {result.duration:.2f}s")
        else:
            click.echo(f"❌ Migration failed for {schema}: {result.error}")
            sys.exit(1)
    else:
        # Migrate all schemas
        click.echo("Migrating all organization schemas...")
        results = migration_manager.run_all_organization_migrations(
            target_revision=target,
            max_workers=max_workers,
            fail_fast=fail_fast
        )
        
        # Display results
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        click.echo(f"\n📊 Migration Summary:")
        click.echo(f"✅ Successful: {len(successful)}")
        click.echo(f"❌ Failed: {len(failed)}")
        
        if failed:
            click.echo("\n❌ Failed Migrations:")
            for result in failed:
                click.echo(f"  - {result.schema_name}: {result.error}")
            sys.exit(1)


@cli.command()
@click.argument('schema')
@click.argument('revision')
@click.option('--confirm', is_flag=True, help='Confirm rollback operation')
def rollback(schema: str, revision: str, confirm: bool):
    """Rollback organization schema to specific revision"""
    if not confirm:
        click.echo("⚠️  Rollback is a destructive operation!")
        click.echo(f"This will rollback schema '{schema}' to revision '{revision}'")
        if not click.confirm("Are you sure you want to continue?"):
            click.echo("Rollback cancelled")
            return
    
    click.echo(f"Rolling back schema {schema} to revision {revision}...")
    result = migration_manager.rollback_organization_migration(schema, revision)
    
    if result.success:
        click.echo(f"✅ Rollback completed for {schema} in {result.duration:.2f}s")
    else:
        click.echo(f"❌ Rollback failed for {schema}: {result.error}")
        sys.exit(1)


@cli.command()
def validate():
    """Validate migration consistency across all schemas"""
    click.echo("Validating migration consistency...")
    result = migration_manager.validate_migration_consistency()
    
    if result['consistent']:
        click.echo(f"✅ All {result['total_schemas']} schemas are consistent")
        if result['unique_revisions']:
            click.echo(f"Current revision: {result['unique_revisions'][0]}")
    else:
        click.echo(f"❌ Inconsistent migrations detected across {result['total_schemas']} schemas")
        click.echo(f"Found {len(result['unique_revisions'])} different revisions:")
        
        # Group schemas by revision
        revision_groups = {}
        for schema, revision in result['schemas'].items():
            if revision not in revision_groups:
                revision_groups[revision] = []
            revision_groups[revision].append(schema)
        
        for revision, schemas in revision_groups.items():
            click.echo(f"\nRevision {revision}:")
            for schema in schemas:
                click.echo(f"  - {schema}")


@cli.command()
def status():
    """Show migration status for all schemas"""
    click.echo("Getting migration status...")
    
    # Get all schemas and their revisions
    schemas = migration_manager._get_all_organization_schemas()
    if not schemas:
        click.echo("No organization schemas found")
        return
    
    table_data = []
    for schema in schemas:
        try:
            revision = migration_manager._get_current_revision(schema)
            table_data.append([schema, revision or "No migrations"])
        except Exception as e:
            table_data.append([schema, f"ERROR: {str(e)}"])
    
    click.echo(f"\n📋 Migration Status ({len(schemas)} schemas):")
    click.echo(tabulate(table_data, headers=["Schema", "Current Revision"], tablefmt="grid"))


@cli.command()
@click.argument('migration_name')
@click.option('--type', 'migration_type', type=click.Choice(['master', 'org']), default='org', 
              help='Migration type (default: org)')
def create_migration(migration_name: str, migration_type: str):
    """Create a new migration file"""
    from alembic.config import Config
    from alembic import command
    
    migration_path = f"migrations/{migration_type}_migrations"
    cfg = Config(f"{migration_path}/alembic.ini")
    
    click.echo(f"Creating {migration_type} migration: {migration_name}")
    
    try:
        command.revision(cfg, message=migration_name, autogenerate=True)
        click.echo(f"✅ Migration created successfully")
    except Exception as e:
        click.echo(f"❌ Failed to create migration: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    cli()