# Salesforce Stage Status Fallback Implementation Plan

> **For Codex:** Execute this plan test-first and keep the lifecycle rule in the shared persistence path used by both bulk sync and CDC.

**Goal:** Classify an otherwise-open Salesforce opportunity as lost when its resolved stage metadata has `DefaultProbability = 0` and `ForecastCategory = Omitted`, without using the opportunity's own probability.

**Architecture:** Pipeline normalization converts the Salesforce stage metadata into `CRMStageState.LOST`. Deal resolution carries that state with the resolved stage, and the shared Salesforce deal writer applies it only when the opportunity flags did not already classify the deal as won or lost. Both initial sync and CDC already converge on this writer.

**Constraints:** `IsWon` and `IsClosed` remain authoritative. Do not add stage-name heuristics, per-deal probability logic, dependencies, or a separate webhook implementation.

### Task 1: Define the stage metadata fallback

- Add failing pipeline-normalizer tests for the exact `0 + Omitted` rule and a zero-only guard.
- Implement the predicate after Salesforce's authoritative won/closed flags.
- Run the focused pipeline tests.

### Task 2: Apply the fallback in the shared deal writer

- Add failing writer and stage-resolution tests.
- Carry `CRMDealStage.stage_state` through the existing stage mapping.
- Resolve effective deal status once for inserts and updates, preserving explicit won/lost.
- Run the focused deal tests.

### Task 3: Verify both ingestion paths

- Run tests covering single-opportunity CDC persistence and bulk chunk persistence.
- Run the Salesforce pipeline and deal test directories.
- Run changed-file checks and inspect the final diff.

