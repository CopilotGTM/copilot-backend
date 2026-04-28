# Backend Infra Monorepo Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `D:\CopilotGTM\copilot-infra` into `D:\CopilotGTM\copilot-backend` as a first-class `infra/` subtree while keeping backend application deployment and infrastructure deployment independently triggerable.

**Architecture:** `copilot-backend` becomes the canonical platform repository. Backend code, migrations, Dockerfiles, Helm chart, and app deployment pipelines stay at the repo root; Terraform, cloud app manifests, and infra deployment docs move under `infra/`. Azure DevOps pipeline triggers must be path-scoped so backend-only changes do not run Terraform, and infra-only changes do not build backend images.

**Tech Stack:** Git, Azure Repos, Azure DevOps Pipelines, Terraform 1.9.0, Helm, Kubernetes/AKS, Azure OpenAI, Azure Key Vault, Azure DevOps variable groups, Python/FastAPI backend.

---

## Current State

### Backend Repository

Repository: `D:\CopilotGTM\copilot-backend`

Remote:

```text
origin https://copilotgtm@dev.azure.com/copilotgtm/app-copilotgtm/_git/copilot-backend
```

Important current paths:

```text
src/
tests/
migrations/
charts/application/
.azure-pipelines/azure-backend-pipeline-dev.yml
.azure-pipelines/azure-backend-pipeline-prod.yml
Dockerfile
pyproject.toml
uv.lock
```

Current app deployment ownership:

- `.azure-pipelines/azure-backend-pipeline-dev.yml` builds backend images and deploys `charts/application` to dev AKS.
- `.azure-pipelines/azure-backend-pipeline-prod.yml` builds backend images and deploys `charts/application` to prod AKS.
- Backend app pipelines already use `StaticAgentPool` and `StaticAgentPool-prod`.

### Infrastructure Repository

Repository: `D:\CopilotGTM\copilot-infra`

Remote:

```text
origin https://copilotgtm@dev.azure.com/copilotgtm/app-copilotgtm/_git/copilot-app-infra
```

Important current paths:

```text
terraform/
.pipeline/azure-pipeline-dev.yml
.pipeline/azure-pipeline-prod.yml
.pipeline/templates/terraform-env-stage.yml
slack-app-manifest/
teams-app-manifest/
google-chat-app-manifest/
docs/
scripts/
```

Current infrastructure deployment ownership:

- `.pipeline/azure-pipeline-dev.yml` runs Terraform for `terraform/azure/dev`.
- `.pipeline/azure-pipeline-prod.yml` runs Terraform for `terraform/azure/prod`.
- `.pipeline/templates/terraform-env-stage.yml` checks out the repo and runs Terraform on a self-hosted VNet-reachable agent.
- `terraform/azure/dev/pipelines.tf` and `terraform/azure/prod/pipelines.tf` create Azure DevOps pipeline resources for backend, frontend, admin frontend, demo frontend, and Terraform.
- `terraform/azure/dev/terraform.tfvars` and `terraform/azure/prod/terraform.tfvars` currently reference:

```hcl
backend_repo_name   = "copilot-backend"
backend_yml_path    = ".azure-pipelines/azure-backend-pipeline-dev.yml" # dev
backend_yml_path    = ".azure-pipelines/azure-backend-pipeline-prod.yml" # prod
terraform_repo_name = "copilot-app-infra"
```

Current infra working tree warning:

```text
D:\CopilotGTM\copilot-infra is dirty on codex/add-azure-realtime-deployments.
```

The dirty files are Google Chat app manifest docs/config, Slack manifest scope/redirect changes, and `terraform/azure/local/provider.tf`. These must be committed or deliberately carried before the repo import so no work is lost.

## Target Structure

Target repository: `D:\CopilotGTM\copilot-backend`

```text
copilot-backend/
  src/
  tests/
  migrations/
  charts/
  .azure-pipelines/
    azure-backend-pipeline-dev.yml
    azure-backend-pipeline-prod.yml
    azure-infra-pipeline-dev.yml
    azure-infra-pipeline-prod.yml
    templates/
      terraform-env-stage.yml
  infra/
    terraform/
    manifests/
      google-chat-app/
      slack-app/
      teams-app/
    docs/
    scripts/
    README.md
```

The old infra repository paths map as follows:

| Old infra path | New backend repo path |
| --- | --- |
| `terraform/` | `infra/terraform/` |
| `.pipeline/azure-pipeline-dev.yml` | `.azure-pipelines/azure-infra-pipeline-dev.yml` |
| `.pipeline/azure-pipeline-prod.yml` | `.azure-pipelines/azure-infra-pipeline-prod.yml` |
| `.pipeline/templates/terraform-env-stage.yml` | `.azure-pipelines/templates/terraform-env-stage.yml` |
| `slack-app-manifest/` | `infra/manifests/slack-app/` |
| `teams-app-manifest/` | `infra/manifests/teams-app/` |
| `google-chat-app-manifest/` | `infra/manifests/google-chat-app/` |
| `docs/` | `infra/docs/` |
| `scripts/` | `infra/scripts/` |
| `README.md` | `infra/README.md` |
| `CLAUDE.md` | `infra/CLAUDE.md` |

## Non-Goals

- Do not merge frontend repositories into this repo in this migration.
- Do not change Terraform state keys such as `azure/dev/terraform.tfstate` or `azure/prod/terraform.tfstate`.
- Do not move backend Helm charts under `infra/`; `charts/application` remains owned by backend deployment.
- Do not remove the old infra Azure Repo until the monorepo pipelines have run successfully in dev and prod.
- Do not expose or print any secret values from `terraform.tfvars`, variable groups, Key Vault, or local environment variables.

## Task 1: Preserve Current Infra Work

**Files:**

- Read: `D:\CopilotGTM\copilot-infra`
- Modify: infra repo Git state only

- [ ] **Step 1: Inspect current infra changes**

Run:

```powershell
git -C D:\CopilotGTM\copilot-infra status --short --branch
git -C D:\CopilotGTM\copilot-infra diff --stat
git -C D:\CopilotGTM\copilot-infra diff --cached --stat
```

Expected:

```text
Branch is codex/add-azure-realtime-deployments.
Dirty files include google-chat-app-manifest, slack-app-manifest, and terraform/azure/local/provider.tf.
```

- [ ] **Step 2: Create a dedicated infra branch for the dirty work**

Run:

```powershell
git -C D:\CopilotGTM\copilot-infra switch -c codex/infra-manifest-and-local-provider-followup
```

Expected:

```text
Switched to a new branch 'codex/infra-manifest-and-local-provider-followup'
```

- [ ] **Step 3: Commit the existing infra edits**

Run:

```powershell
git -C D:\CopilotGTM\copilot-infra add google-chat-app-manifest slack-app-manifest terraform/azure/local/provider.tf
git -C D:\CopilotGTM\copilot-infra commit -m "docs: add chat app manifests and local provider cleanup"
```

Expected:

```text
Commit succeeds and git status becomes clean.
```

- [ ] **Step 4: Push the infra follow-up branch**

Run:

```powershell
git -C D:\CopilotGTM\copilot-infra push -u origin codex/infra-manifest-and-local-provider-followup
```

Expected:

```text
Branch is available on origin and can be imported after the main infra subtree import.
```

## Task 2: Create Backend Migration Branch

**Files:**

- Modify: `D:\CopilotGTM\copilot-backend` Git state only

- [ ] **Step 1: Verify backend main is clean**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend status --short --branch
git -C D:\CopilotGTM\copilot-backend rev-parse --abbrev-ref HEAD
```

Expected:

```text
## main...origin/main
main
```

- [ ] **Step 2: Create the migration branch**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend switch -c chore/backend-infra-monorepo
```

Expected:

```text
Switched to a new branch 'chore/backend-infra-monorepo'
```

- [ ] **Step 3: Add the infra repository as a temporary remote**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend remote add infra-source https://copilotgtm@dev.azure.com/copilotgtm/app-copilotgtm/_git/copilot-app-infra
git -C D:\CopilotGTM\copilot-backend fetch infra-source
```

Expected:

```text
infra-source/main and infra-source/codex/infra-manifest-and-local-provider-followup are available locally.
```

## Task 3: Import Infra Under `infra/`

**Files:**

- Create: `infra/**`

- [ ] **Step 1: Import infra main with history preserved**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend subtree add --prefix=infra infra-source main -m "chore: import infra repository under infra"
```

Expected:

```text
The backend repo now contains infra repo files under infra/.
Git history from the infra repo is retained through the subtree merge.
```

- [ ] **Step 2: Pull in the infra follow-up branch**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend subtree pull --prefix=infra infra-source codex/infra-manifest-and-local-provider-followup -m "chore: import infra manifest follow-up changes"
```

Expected:

```text
The Google Chat, Slack manifest, and local provider changes are present under infra/.
```

- [ ] **Step 3: Verify expected imported paths**

Run:

```powershell
Test-Path D:\CopilotGTM\copilot-backend\infra\terraform
Test-Path D:\CopilotGTM\copilot-backend\infra\.pipeline\azure-pipeline-dev.yml
Test-Path D:\CopilotGTM\copilot-backend\infra\slack-app-manifest
Test-Path D:\CopilotGTM\copilot-backend\infra\google-chat-app-manifest
```

Expected:

```text
True
True
True
True
```

## Task 4: Normalize Imported Paths

**Files:**

- Move: `infra/.pipeline/azure-pipeline-dev.yml` -> `.azure-pipelines/azure-infra-pipeline-dev.yml`
- Move: `infra/.pipeline/azure-pipeline-prod.yml` -> `.azure-pipelines/azure-infra-pipeline-prod.yml`
- Move: `infra/.pipeline/templates/terraform-env-stage.yml` -> `.azure-pipelines/templates/terraform-env-stage.yml`
- Move: `infra/slack-app-manifest/` -> `infra/manifests/slack-app/`
- Move: `infra/teams-app-manifest/` -> `infra/manifests/teams-app/`
- Move: `infra/google-chat-app-manifest/` -> `infra/manifests/google-chat-app/`
- Keep: `infra/terraform/`
- Keep: `infra/docs/`
- Keep: `infra/scripts/`

- [ ] **Step 1: Create target pipeline template directory**

Run:

```powershell
New-Item -ItemType Directory -Force D:\CopilotGTM\copilot-backend\.azure-pipelines\templates
```

Expected:

```text
.azure-pipelines/templates exists.
```

- [ ] **Step 2: Move infra pipeline YAML into backend pipeline directory**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend mv infra/.pipeline/azure-pipeline-dev.yml .azure-pipelines/azure-infra-pipeline-dev.yml
git -C D:\CopilotGTM\copilot-backend mv infra/.pipeline/azure-pipeline-prod.yml .azure-pipelines/azure-infra-pipeline-prod.yml
git -C D:\CopilotGTM\copilot-backend mv infra/.pipeline/templates/terraform-env-stage.yml .azure-pipelines/templates/terraform-env-stage.yml
```

Expected:

```text
Infra pipeline YAML lives beside backend pipeline YAML.
```

- [ ] **Step 3: Move app manifests into `infra/manifests`**

Run:

```powershell
New-Item -ItemType Directory -Force D:\CopilotGTM\copilot-backend\infra\manifests
git -C D:\CopilotGTM\copilot-backend mv infra/slack-app-manifest infra/manifests/slack-app
git -C D:\CopilotGTM\copilot-backend mv infra/teams-app-manifest infra/manifests/teams-app
git -C D:\CopilotGTM\copilot-backend mv infra/google-chat-app-manifest infra/manifests/google-chat-app
```

Expected:

```text
Slack, Teams, and Google Chat app manifests live under infra/manifests/.
```

- [ ] **Step 4: Remove empty imported pipeline directory**

Run:

```powershell
Remove-Item -LiteralPath D:\CopilotGTM\copilot-backend\infra\.pipeline -Recurse
```

Expected:

```text
infra/.pipeline no longer exists.
```

## Task 5: Update Infra Pipeline Paths

**Files:**

- Modify: `.azure-pipelines/azure-infra-pipeline-dev.yml`
- Modify: `.azure-pipelines/azure-infra-pipeline-prod.yml`
- Modify: `.azure-pipelines/templates/terraform-env-stage.yml`

- [ ] **Step 1: Update dev infra trigger paths**

In `.azure-pipelines/azure-infra-pipeline-dev.yml`, replace the old path block:

```yaml
paths:
  include:
    - terraform/azure/dev/**
    - terraform/azure/modules/**
    - .pipeline/**
```

with:

```yaml
paths:
  include:
    - infra/terraform/azure/dev/**
    - infra/terraform/azure/modules/**
    - .azure-pipelines/azure-infra-pipeline-dev.yml
    - .azure-pipelines/templates/terraform-env-stage.yml
```

Also change the template reference:

```yaml
- template: templates/terraform-env-stage.yml
```

to:

```yaml
- template: templates/terraform-env-stage.yml
```

The template line text is unchanged because `azure-infra-pipeline-dev.yml` now lives in `.azure-pipelines/` and `templates/` is its sibling.

- [ ] **Step 2: Update dev Terraform working directory**

In `.azure-pipelines/azure-infra-pipeline-dev.yml`, replace:

```yaml
tfWorkingDir: terraform/azure/dev
```

with:

```yaml
tfWorkingDir: infra/terraform/azure/dev
```

- [ ] **Step 3: Update prod infra trigger paths**

In `.azure-pipelines/azure-infra-pipeline-prod.yml`, replace:

```yaml
paths:
  include:
    - terraform/azure/prod/**
    - terraform/azure/modules/**
    - .pipeline/**
```

with:

```yaml
paths:
  include:
    - infra/terraform/azure/prod/**
    - infra/terraform/azure/modules/**
    - .azure-pipelines/azure-infra-pipeline-prod.yml
    - .azure-pipelines/templates/terraform-env-stage.yml
```

- [ ] **Step 4: Update prod Terraform working directory**

In `.azure-pipelines/azure-infra-pipeline-prod.yml`, replace:

```yaml
tfWorkingDir: terraform/azure/prod
```

with:

```yaml
tfWorkingDir: infra/terraform/azure/prod
```

- [ ] **Step 5: Verify template uses parameterized working directory**

Run:

```powershell
Select-String -Path D:\CopilotGTM\copilot-backend\.azure-pipelines\templates\terraform-env-stage.yml -Pattern 'workingDirectory|tfWorkingDir|terraform'
```

Expected:

```text
The template uses the tfWorkingDir parameter and does not hard-code terraform/azure/dev or terraform/azure/prod.
```

## Task 6: Update Backend App Pipeline Path Filters

**Files:**

- Modify: `.azure-pipelines/azure-backend-pipeline-dev.yml`
- Modify: `.azure-pipelines/azure-backend-pipeline-prod.yml`

- [ ] **Step 1: Add backend dev path filters**

In `.azure-pipelines/azure-backend-pipeline-dev.yml`, change:

```yaml
trigger:
  branches:
    include:
      - main
      - feature/*
      - hotfix/*
```

to:

```yaml
trigger:
  branches:
    include:
      - main
      - feature/*
      - hotfix/*
  paths:
    exclude:
      - infra/**
      - .azure-pipelines/azure-infra-pipeline-dev.yml
      - .azure-pipelines/azure-infra-pipeline-prod.yml
      - .azure-pipelines/templates/terraform-env-stage.yml
```

Also change:

```yaml
pr:
  branches:
    include:
      - main
```

to:

```yaml
pr:
  branches:
    include:
      - main
  paths:
    exclude:
      - infra/**
      - .azure-pipelines/azure-infra-pipeline-dev.yml
      - .azure-pipelines/azure-infra-pipeline-prod.yml
      - .azure-pipelines/templates/terraform-env-stage.yml
```

- [ ] **Step 2: Add backend prod path filters**

In `.azure-pipelines/azure-backend-pipeline-prod.yml`, change:

```yaml
trigger:
  branches:
    include:
      - release/*
```

to:

```yaml
trigger:
  branches:
    include:
      - release/*
  paths:
    exclude:
      - infra/**
      - .azure-pipelines/azure-infra-pipeline-dev.yml
      - .azure-pipelines/azure-infra-pipeline-prod.yml
      - .azure-pipelines/templates/terraform-env-stage.yml
```

Keep:

```yaml
pr: none
```

## Task 7: Update Terraform Repository and YAML References

**Files:**

- Modify: `infra/terraform/azure/dev/terraform.tfvars`
- Modify: `infra/terraform/azure/prod/terraform.tfvars`
- Review: `infra/terraform/azure/dev/variables.tf`
- Review: `infra/terraform/azure/prod/variables.tf`
- Review: `infra/terraform/azure/dev/pipelines.tf`
- Review: `infra/terraform/azure/prod/pipelines.tf`

- [ ] **Step 1: Update dev Terraform pipeline repo and path**

In `infra/terraform/azure/dev/terraform.tfvars`, replace:

```hcl
terraform_repo_name = "copilot-app-infra"
```

with:

```hcl
terraform_repo_name = "copilot-backend"
```

Replace the commented default hint:

```hcl
# terraform_yml_path       = ".pipeline/azure-pipeline-dev.yml" # Default (dev-specific)
```

with an active value:

```hcl
terraform_yml_path = ".azure-pipelines/azure-infra-pipeline-dev.yml"
```

- [ ] **Step 2: Update prod Terraform pipeline repo and path**

In `infra/terraform/azure/prod/terraform.tfvars`, replace:

```hcl
terraform_repo_name = "copilot-app-infra"
```

with:

```hcl
terraform_repo_name = "copilot-backend"
```

Replace the commented default hint:

```hcl
# terraform_yml_path       = ".pipeline/azure-pipeline-prod.yml" # Default (prod-specific)
```

with an active value:

```hcl
terraform_yml_path = ".azure-pipelines/azure-infra-pipeline-prod.yml"
```

- [ ] **Step 3: Keep backend pipeline repository references unchanged**

Confirm both files still contain:

```hcl
backend_repo_name = "copilot-backend"
backend_yml_path  = ".azure-pipelines/azure-backend-pipeline-dev.yml"
```

for dev and:

```hcl
backend_repo_name = "copilot-backend"
backend_yml_path  = ".azure-pipelines/azure-backend-pipeline-prod.yml"
```

for prod.

Expected:

```text
Backend app pipelines and infra Terraform pipelines all live in copilot-backend, but remain separate Azure DevOps pipeline resources.
```

- [ ] **Step 4: Check Terraform variable defaults**

Run:

```powershell
Select-String -Path D:\CopilotGTM\copilot-backend\infra\terraform\azure\dev\variables.tf,D:\CopilotGTM\copilot-backend\infra\terraform\azure\prod\variables.tf -Pattern 'terraform_yml_path|terraform_repo_name|backend_yml_path|backend_repo_name'
```

Expected:

```text
Variables still exist and do not require schema changes.
```

## Task 8: Update Infra Documentation Links

**Files:**

- Modify: `infra/README.md`
- Modify: `infra/docs/deployment-guide.md`
- Modify: `infra/docs/Technical Documentation/infrastructure-deployment-guide.md`
- Modify: `infra/docs/Technical Documentation/devops-production-pipeline-setup.md`
- Modify: docs that reference `.pipeline/`, `terraform/azure/`, or `copilot-infra`

- [ ] **Step 1: Find stale path references**

Run:

```powershell
rg -n "copilot-infra|copilot-app-infra|\\.pipeline/|terraform/azure|google-chat-app-manifest|slack-app-manifest|teams-app-manifest" D:\CopilotGTM\copilot-backend\infra D:\CopilotGTM\copilot-backend\docs
```

Expected:

```text
Matches are returned in imported infra docs and possibly backend docs.
```

- [ ] **Step 2: Update common path references**

Apply these replacements where they refer to repository-relative paths:

```text
copilot-infra/terraform/azure/dev -> copilot-backend/infra/terraform/azure/dev
copilot-infra/terraform/azure/prod -> copilot-backend/infra/terraform/azure/prod
terraform/azure/dev -> infra/terraform/azure/dev
terraform/azure/prod -> infra/terraform/azure/prod
terraform/azure/modules -> infra/terraform/azure/modules
.pipeline/azure-pipeline-dev.yml -> .azure-pipelines/azure-infra-pipeline-dev.yml
.pipeline/azure-pipeline-prod.yml -> .azure-pipelines/azure-infra-pipeline-prod.yml
.pipeline/templates/terraform-env-stage.yml -> .azure-pipelines/templates/terraform-env-stage.yml
google-chat-app-manifest -> infra/manifests/google-chat-app
slack-app-manifest -> infra/manifests/slack-app
teams-app-manifest -> infra/manifests/teams-app
```

- [ ] **Step 3: Preserve Terraform state key references**

Do not replace remote state keys such as:

```text
azure/dev/terraform.tfstate
azure/prod/terraform.tfstate
```

Expected:

```text
Docs describe the new repo layout but existing Terraform state keys remain unchanged.
```

## Task 9: Validate Secret Hygiene

**Files:**

- Review: `infra/terraform/**/terraform.tfvars`
- Review: `infra/manifests/**`
- Review: `.env*`

- [ ] **Step 1: Search for likely secret keys in imported infra**

Run:

```powershell
rg -n --hidden --glob '!**/.git/**' --glob '!**/.terraform/**' --glob '!**/.venv/**' "(password|secret|client_secret|api_key|apikey|private_key|token|pat)" D:\CopilotGTM\copilot-backend\infra
```

Expected:

```text
Only variable names, documentation, placeholders, and non-secret references are present.
```

- [ ] **Step 2: Inspect committed tfvars manually**

Run:

```powershell
Get-Content D:\CopilotGTM\copilot-backend\infra\terraform\azure\dev\terraform.tfvars
Get-Content D:\CopilotGTM\copilot-backend\infra\terraform\azure\prod\terraform.tfvars
```

Expected:

```text
No real passwords, PATs, API keys, private keys, or client secrets are committed.
```

- [ ] **Step 3: If a real secret is found**

Stop the migration and do not push. Rotate the leaked value, remove it from the commit, and only then continue.

## Task 10: Validate Terraform and YAML

**Files:**

- Validate: `infra/terraform/**`
- Validate: `.azure-pipelines/*.yml`
- Validate: `.azure-pipelines/templates/*.yml`

- [ ] **Step 1: Run Terraform format check**

Run:

```powershell
terraform -chdir=D:\CopilotGTM\copilot-backend\infra\terraform fmt -check -recursive
```

Expected:

```text
All Terraform files are formatted, or Terraform prints exact files that need formatting.
```

- [ ] **Step 2: Run Terraform init and validate for dev**

Run:

```powershell
terraform -chdir=D:\CopilotGTM\copilot-backend\infra\terraform\azure\dev init -input=false
terraform -chdir=D:\CopilotGTM\copilot-backend\infra\terraform\azure\dev validate
```

Expected:

```text
Terraform initializes and validates using the existing backend state configuration.
```

- [ ] **Step 3: Run Terraform init and validate for prod**

Run:

```powershell
terraform -chdir=D:\CopilotGTM\copilot-backend\infra\terraform\azure\prod init -input=false
terraform -chdir=D:\CopilotGTM\copilot-backend\infra\terraform\azure\prod validate
```

Expected:

```text
Terraform initializes and validates using the existing backend state configuration.
```

- [ ] **Step 4: Verify backend tests are not affected by the import**

Run:

```powershell
uv run pytest --collect-only -q
```

Expected:

```text
Pytest collection succeeds. Test count may vary with current main.
```

## Task 11: Commit and Push Migration

**Files:**

- Modify: many files due to subtree import and path rewrites

- [ ] **Step 1: Review final status**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend status --short
git -C D:\CopilotGTM\copilot-backend diff --stat
```

Expected:

```text
The diff contains the infra import, path moves, pipeline path changes, Terraform tfvars repo/path updates, and documentation updates.
```

- [ ] **Step 2: Commit migration**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend add -A
git -C D:\CopilotGTM\copilot-backend commit -m "chore: consolidate infra into backend repo"
```

Expected:

```text
Commit succeeds.
```

- [ ] **Step 3: Push migration branch**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend push -u origin chore/backend-infra-monorepo
```

Expected:

```text
Branch is pushed and ready for Azure DevOps PR creation.
```

## Task 12: Azure DevOps Cutover

**Files:**

- Azure DevOps pipeline resources managed by Terraform

- [ ] **Step 1: Create backend PR**

Create a PR from:

```text
chore/backend-infra-monorepo -> main
```

PR title:

```text
chore: consolidate infra into backend repo
```

PR description must include:

```markdown
## Summary
- Imports copilot-app-infra under infra/
- Moves Terraform pipeline YAML to .azure-pipelines/infra-*.yml
- Keeps backend app pipelines separate from infra pipelines through path filters
- Updates Terraform pipeline repository references to copilot-backend

## Validation
- terraform fmt -check -recursive
- terraform validate for dev
- terraform validate for prod
- uv run pytest --collect-only -q

## Cutover Notes
- Do not delete the old copilot-app-infra repo until dev and prod infra pipelines have run successfully from copilot-backend.
- Keep Terraform state keys unchanged.
```

- [ ] **Step 2: After merge, run dev Terraform plan/apply from new location**

Use Azure DevOps pipeline:

```text
.azure-pipelines/azure-infra-pipeline-dev.yml
```

Expected:

```text
The dev infra pipeline checks out copilot-backend, runs from infra/terraform/azure/dev, and updates the Terraform pipeline resource to point to copilot-backend.
```

- [ ] **Step 3: After dev succeeds, run prod Terraform plan/apply from release branch**

Use Azure DevOps pipeline:

```text
.azure-pipelines/azure-infra-pipeline-prod.yml
```

Expected:

```text
The prod infra pipeline checks out copilot-backend, runs from infra/terraform/azure/prod, and keeps the terraform-prod approval gate.
```

- [ ] **Step 4: Archive old infra repo only after successful cutover**

Do not delete `copilot-app-infra`. After both dev and prod infra pipelines run from the monorepo, mark the old repo read-only or archive it with a README pointing to:

```text
copilot-backend/infra
```

## Rollback Plan

If migration fails before PR merge:

```powershell
git -C D:\CopilotGTM\copilot-backend switch main
git -C D:\CopilotGTM\copilot-backend branch -D chore/backend-infra-monorepo
```

If migration fails after PR merge but before Terraform cutover:

```powershell
git -C D:\CopilotGTM\copilot-backend log --oneline --merges -1
git -C D:\CopilotGTM\copilot-backend revert HEAD
git -C D:\CopilotGTM\copilot-backend push origin main
```

This assumes the monorepo PR merge commit is still `HEAD` on `main`. If another PR has merged after it, first inspect `git log --oneline --merges -10`, identify the monorepo merge commit by the PR title `chore: consolidate infra into backend repo`, and revert that exact commit.

Keep using the old infra repository pipeline until the monorepo infra pipeline is fixed.

If Terraform updates pipeline resources to the new repo and that breaks:

1. Revert the Terraform `terraform_repo_name` and `terraform_yml_path` values back to the old infra repo in a hotfix.
2. Run the old infra pipeline from `copilot-app-infra`.
3. Confirm Azure DevOps pipeline resources point back to the old repo.
4. Reattempt the monorepo cutover with a fixed branch.

## Review Checklist

- [ ] `infra/terraform` exists and contains all old Terraform roots/modules.
- [ ] Backend app pipelines exclude `infra/**`.
- [ ] Infra pipelines include only `infra/terraform/**` and infra pipeline YAML paths.
- [ ] `terraform_repo_name` is `copilot-backend` in dev and prod tfvars.
- [ ] `terraform_yml_path` points to `.azure-pipelines/azure-infra-pipeline-dev.yml` and `.azure-pipelines/azure-infra-pipeline-prod.yml`.
- [ ] Terraform state keys are unchanged.
- [ ] No real secrets were imported.
- [ ] Old infra repo remains available until cutover is proven.
