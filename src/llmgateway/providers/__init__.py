"""Provider adapters: the boundary between the gateway and each LLM API.

Each provider (Claude, Gemini, mock) gets an adapter that turns a unified
`ChatRequest` into a `ProviderResult` and maps provider failures to the
`ProviderError` classes in `base`. Everything above this package (retries,
fallback, cache, accounting) works only with those types, so it never needs
provider-specific rules.
"""
