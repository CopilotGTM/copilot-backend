# Azure DevOps Pipeline Module

This module provisions an Azure DevOps YAML pipeline resource pointing to a YAML file (e.g., `azure-pipelines.yml`) in a Git repo. It enables "pipelines as code," letting you manage the CI/CD pipeline lifecycle in Terraform—not just the YAML.

## Usage Example

```hcl
provider "azuredevops" {
  org_service_url        = "https://dev.azure.com/YOUR_ORG"
  personal_access_token  = var.azuredevops_pat  # Secure via TF_VAR or secrets manager
}

data "azuredevops_project" "project" {
  name = "YOUR_PROJECT"
}

data "azuredevops_git_repository" "repo" {
  project_id = data.azuredevops_project.project.id
  name       = "YOUR_REPO_NAME"
}

module "pipeline" {
  source        = "../modules/devops_pipeline"

  project_id    = data.azuredevops_project.project.id
  pipeline_name = "My App CI/CD"
  repo_id       = data.azuredevops_git_repository.repo.id
  branch_name   = "refs/heads/main"
  yml_path      = "azure-pipelines.yml"
  # pipeline_path can be omitted for root folder ("\\")
}
```

## Required Inputs

| Variable        | Source                       | Notes                                 |
|-----------------|-----------------------------|---------------------------------------|
| project_id      | DevOps project data/output   | See data block above                  |
| repo_id         | DevOps repo data/output      | See data block above                  |
| pipeline_name   | Manual, arbitrary            | e.g., "Backend CI"                    |
| branch_name     | Manual or static             | e.g., "refs/heads/main"               |
| yml_path        | Manual or static             | e.g., "azure-pipelines.yml"           |
| pipeline_path   | Usually "\\" for root        | Optional, default provided            |

### Provider Authentication

You must set up the `azuredevops` provider with your Azure DevOps org URL and a Personal Access Token (PAT) with permission to manage pipelines.  
**Never** hard-code PAT in code—use `TF_VAR_azuredevops_pat` or a secrets manager.

## Outputs

- `pipeline_id`   — Pipeline resource ID.
- `pipeline_name` — Name of the created pipeline.

## What You May Need to Provide

- Azure DevOps org URL and a PAT (using provider, not this module)
- The project and repo must exist prior to using the module (managed elsewhere or as data resources)
- The `azure-pipelines.yml` must exist in the specified repo/path

---

Edit variable mapping and data sources as needed for your environment or to align with Terraform outputs.