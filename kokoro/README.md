# Kokoro identity services

`kokoro/` contains the bundled identity resource and is the destination for persona, long-term memory, affect, relationship, continuity, and context-provider modules. Code in this layer must not execute shell commands, mutate workspace files, call providers directly, or read credentials.
