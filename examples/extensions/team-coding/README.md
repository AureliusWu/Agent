# Team Coding Example

This package demonstrates the declarative Extension SDK. It contributes one professional Agent profile, one Skill, and one sandboxed tool alias. It does not execute third-party Python or frontend code.

Install it from the Extension panel with:

```text
examples/extensions/team-coding
```

The package declares every capability it can expose. Runtime calls still pass through the normal workspace sandbox, permission engine, audit log, and Verifier.
