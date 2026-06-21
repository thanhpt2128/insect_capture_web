# Performance Optimization Skill

Use this skill when optimizing Python or NumPy code.

## Workflow
1. Identify bottleneck first.
2. Add benchmark if possible.
3. Preserve output and dtype.
4. Avoid unnecessary array copies.
5. Compare before/after.
6. Add regression test.

## Rules
- Do not optimize by changing numerical meaning.
- Do not convert complex data to magnitude unless requested.
- Always document input/output shape.