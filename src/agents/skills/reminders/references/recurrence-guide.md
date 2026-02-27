# Recurrence Guide

## Available recurrence options

| Value | Behavior |
|-------|----------|
| `none` | One-time only. Fires once and completes. |
| `daily` | Every day at the same time. |
| `weekdays` | Monday through Friday at the same time. Skips weekends. |
| `weekly` | Same day of the week, every week. |
| `biweekly` | Every two weeks on the same day. |
| `monthly` | Same day of the month. Handles variable month lengths (e.g., 31st → last day of short months). |

## Mapping user language to recurrence

| User says | Recurrence value |
|-----------|-----------------|
| "every day", "daily" | `daily` |
| "every weekday", "on weekdays", "Monday to Friday" | `weekdays` |
| "every week", "weekly", "every Monday" | `weekly` |
| "every two weeks", "biweekly", "every other week" | `biweekly` |
| "every month", "monthly", "on the 15th of every month" | `monthly` |
| No recurrence mentioned | `none` |

## Edge cases

- **Past time with recurrence**: the system automatically advances to the next valid occurrence. For example, "daily at 9 AM" when it's currently 10 AM will schedule for tomorrow at 9 AM.
- **Past time without recurrence**: returns an error — one-time reminders must be in the future.
- **Weekdays recurrence on a weekend**: advances to next Monday.
- **Monthly on the 31st**: in months with fewer days, fires on the last day of the month.
- **DST transitions**: handled automatically. A "daily at 9 AM" reminder stays at 9 AM local time even across DST changes.

## Stopping recurring reminders

- Recurring reminders run until explicitly cancelled via `CancelReminder`.
- There is no built-in end date parameter exposed to the agent. Users cancel when done.
- If the reminder fails 3 consecutive times, it is automatically marked as failed and stops executing.
