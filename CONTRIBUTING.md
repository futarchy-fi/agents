# Contributing to agents.futarchy.ai

## Testing Philosophy

We only write high-level, difficult tests. System-level invariants, conservation laws, round-trip properties, adversarial scenarios. These are the only tests that matter. If a test doesn't make you think hard about whether it should pass, it's not worth writing.

No unit tests for simple functions. No testing that constructors construct or getters get. No "does this function return what I just told it to return." That's busywork, not engineering. One high-level invariant test that fuzzes 1000 random inputs and verifies a system property catches more bugs than 50 unit tests ever will.

A good test encodes a belief about the system that isn't obvious from reading the code. If a test would pass just by reading the function it exercises, it's not worth writing.

### What makes a good test

**Test invariants** — properties that must hold regardless of input. These are the most valuable tests because they catch entire classes of bugs, not just specific cases.

**Test round-trips** — do something, undo it, verify you're back where you started. These catch asymmetries between paired operations.

**Test conservation laws** — things that must be preserved across operations. If something goes in, the same amount must come out somewhere.

**Test boundaries that matter in production** — not toy edge cases, but the real limits of the system. The values where math breaks, where precision runs out, where overflow lurks.

**Test end-to-end** — for servers, test the full request/response cycle. For engines, test the full operation lifecycle. For any system, test it the way it will actually be used. Integration bugs hide in the seams between components.

### What NOT to test

- Simple getters, constructors, or property accessors
- Obvious arithmetic
- Things the type system already guarantees
- Individual functions in isolation when a round-trip test covers them
- Anything where the test is just restating the implementation

### Tests come first

Write tests before the implementation exists, or at least before running them. Tests encode what the system SHOULD do, not what it DOES do. The workflow:

1. Write the full test suite based on the spec and invariants.
2. Do NOT run the tests while writing them.
3. Build the implementation.
4. Run the tests. Fix the implementation to make them pass.
5. If a test fails and the test is wrong, review it very carefully before changing it. A test is a contract — changing it means changing what we believe the system should do. That's a design decision, not a bug fix.

This discipline matters because running tests while writing them creates a feedback loop that optimizes for passing, not for correctness. You end up testing what the code does rather than what it should do.

### How to structure tests

Each test should read like a story: setup a scenario, do something interesting, verify a non-obvious property. Name tests after the property they verify, not the function they call.

Use randomized inputs (fuzzing) for invariant tests. If a property should hold for ALL inputs, test it with many random inputs, not three hand-picked ones.
