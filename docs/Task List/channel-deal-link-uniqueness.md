# Task: Channel-to-Deal Link Uniqueness Verification

## Status: TODO

## Question

Can a messaging channel be linked to **multiple deals** or should it be restricted to **one deal only**?

## Current Behavior

- Current code allows the same channel to be linked to multiple deals
- No uniqueness constraint on `(channel_id)` in `messaging_deal_links` table
- Only constraint is `(channel_id, deal_id)` preventing duplicate links to the same deal

## Options

### Option A: One Channel → One Deal (Exclusive)

- A Teams channel can only be linked to a single deal
- If already linked to Deal A, cannot link to Deal B
- Error message: "This channel is already linked to deal '{deal_name}'"

**Pros:**

- Clear ownership - messages clearly belong to one deal context
- Simpler analytics and reporting
- No confusion about which deal a conversation relates to

**Cons:**

- Less flexible for shared channels (e.g., partner channels)
- May require unlinking from one deal before linking to another

### Option B: One Channel → Multiple Deals (Shared)

- A Teams channel can be linked to multiple deals
- Messages are synced once but available in context of all linked deals

**Pros:**

- Flexible for channels that span multiple deals
- No need to choose which deal a channel belongs to

**Cons:**

- Complexity in message attribution
- Unclear deal context for AI analysis

## Implementation (if Option A chosen)

```python
# In link_channel_to_deal(), before creating link:
existing_link_stmt = select(MessagingDealLink).where(
    MessagingDealLink.channel_id == channel.channel_id,
    MessagingDealLink.deal_id != deal_id,  # Linked to a DIFFERENT deal
)
existing_link = await self.db.execute(existing_link_stmt)
if existing_link.scalars().first():
    raise ValueError(
        f"Channel '{channel.channel_name}' is already linked to another deal. "
        "A channel can only be linked to one deal at a time."
    )
```

## Decision

[ ] Option A - Exclusive (one channel per deal)
[ ] Option B - Shared (channel can link to multiple deals)

## Notes

- Verify with product/business requirements
- Consider: Do sales teams typically have one channel per deal or shared channels?
