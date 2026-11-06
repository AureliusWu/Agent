# Shared contracts

Stable event, protocol, schema, and identifier contracts shared by `siyi/`, `kokoro/`, and `desktop/` live here. This boundary prevents circular dependencies between the execution core and identity services.

`model_credential_alias_v1.json` fixes non-secret alias derivation vectors shared by the backend and desktop. Legacy aliases hash the canonical credential destination. Versioned aliases hash its UTF-8 representation followed by a NUL, `siyi-model-credential-v1`, a NUL, and the lowercase generation nonce. No API key material is part of these vectors or of the configuration file.
