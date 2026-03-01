---
name: competitive-analysis
description: Perform structured competitor comparison using internal knowledge, deal-specific signals, and external context to produce a decision-grade competitive brief. Use when the user asks to compare us vs a competitor, asks how we stack up against another vendor, wants competitive positioning for a deal, or asks why a deal may be losing to a competitor.
---

# Competitive Analysis

Run an evidence-first competitive analysis. Do not generate positioning from assumptions or generic
marketing claims.
Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If no specific competitor is named, ask which competitor to compare before proceeding.
- If the request is narrow ("pricing vs X", "why are we losing to Y"), analyze only that scope.
- If the request is broad ("competitive landscape", "compare us vs X"), run the full framework.
- If multiple competitors are named, compare each one separately and keep evidence isolated.

2. Gather evidence before analyzing.
- Internal positioning intelligence: battlecards, competitive notes, win/loss summaries, pricing
  comparison artifacts.
  [SearchInternalKnowledge, ListDealNotes]
- Deal-specific competitive signals: competitor mentions in transcripts, stakeholder quotes,
  objections, feature comparison requests, procurement references, and timing shifts tied to
  alternatives.
  [GetDealDetails, ListDealMeetings, GetMeetingTranscript, ListDealEmails, GetEmailDetails,
  ListDealChannelMessages]
- External context only when recency may materially change deal dynamics: funding, launches,
  pricing changes, customer wins, or repositioning.
  [SearchInternet, CrawlWebpage, ExtractNewsArticle]

Use the best available tools for each data signal. Bracket hints are examples, not hard
constraints.

3. Analyze and classify the competitive dynamic.
- Determine whether the competitor is a `Real threat`, `Procurement anchor`, `Price leverage
  tactic`, `Political preference`, or `Unknown`.
- Load `references/comparison-framework.md` for dimension calibration.
- Rate each dimension as `Stronger`, `Comparable`, `Weaker`, or `Unknown`.
- Every non-`Unknown` judgment must cite a concrete evidence source type: `KB`, `Transcript`,
  `Email`, `Notes`, `Channel`, or `External`.
- If evidence is missing or contradictory, use `Unknown` or downgrade conservatively.

4. Produce a structured output.
- Keep the response decision-oriented and tied to this deal's competitive pressure.
- Load `references/win-themes-guide.md` for evidence-backed positioning language.
- Load `references/output-format.md` for the required matrix, risk format, and language rules.

## Output contract

Use this structure and keep the section order:

1. Competitive Context Summary
2. Structured Comparison Matrix
3. Deal-Specific Competitive Risk Signals
4. Recommended Positioning Angles
5. Unknowns and Missing Evidence

Never invent features, pricing, or implementation claims.
Never expose internal IDs or UUIDs unless the user explicitly asks.
If evidence is thin, state the limitation clearly instead of filling gaps with narrative.

## Reference loading guide

- Use `references/comparison-framework.md` for dimension definitions and calibration.
- Use `references/win-themes-guide.md` for differentiation language tied to observed deal risk.
- Use `references/output-format.md` for the required section order, matrix columns, and risk
  formatting rules.
