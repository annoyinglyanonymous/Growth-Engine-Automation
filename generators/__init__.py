"""Per-stage campaign generators.

One module per stage of the Campaign Brief to Asset Pack flow:

    strategy -> angles -> concepts -> {meta_ads, email_sequence}

Each writes its own rows plus the knowledge_snapshot that produced them, so
stage 10 (version history) is a by-product rather than a feature to build.

Every module imports kb_context.build_context directly. They deliberately do
NOT go through the HTTP API: a generator running in the same process making a
request to itself adds a serialisation round trip, a second failure mode, and a
port dependency, for nothing.
"""
