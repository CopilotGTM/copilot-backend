# Pipeline Review Health Signals

Use these signals to judge whether the pipeline shape is healthy, blocked, or decaying.

## Healthy distribution signals

- No single stage should hold more than `30%` of total deals unless the team is intentionally
  building early pipeline.
- Late-stage deals should represent `20-35%` of pipeline value in stable quarters.

## Bottleneck detection

- Flag a bottleneck when `>=30%` of deals sit in the same stage.
- The same stage must also have an average age above the stage benchmark.

## Velocity interpretation

- Increasing stage age plus low activity -> decay
- High activity plus aging -> possible complexity, not necessarily decay

## Unknown handling

- If stage age or activity data is missing, reduce confidence and mark the affected conclusion
  `Unknown`.
