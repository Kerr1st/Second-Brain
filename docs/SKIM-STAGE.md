# Step 3.5: skim arrivals

Skim turns arriving source documents into bounded structured records that help Explorer allocate attention. It does not distill a final durable conclusion or replace four-seat discovery review.

The `skim` role resolves through controlled Bedrock routing. It receives source text, uses a structured schema, validates contents, and shares the run's metered spend tracker. Its sub-budget is the configured skim allowance limited by the run total. Disabling skim must leave a plain arrivals manifest, not silently claim skim evidence exists.

Store each skim with source identity, source version, model and prompt version. Failures in model invocation and storage are different outcomes. A partial run retains completed records, reports missing records and stops when its failure/spend threshold is reached. A paid malformed response still contributes cost. Skim records and clusters guide which bounded source packets are read; they are not authority to mutate captured sources.

The public `src.skim` interface separates orchestration from internal storage/clustering. Read code for current bounds and [Models](MODEL-BACKENDS.md) for routing and usage rules. Historical model experiments and their observed acceptance/cost figures are in the dated archive and are not current pricing or quality guarantees.
