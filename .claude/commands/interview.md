Interview me in-depth about a feature I'm building to help clarify requirements and uncover edge cases.

## How to Use

The user should provide a spec file or describe the feature they're building. Read any referenced spec file first.

## Interview Process

Read the spec file (if provided via @filename) and interview me in detail using the AskUserQuestion tool about literally anything:
- Technical implementation details
- UI & UX considerations
- Edge cases and error handling
- Concerns and potential issues
- Tradeoffs between approaches
- Performance implications
- Security considerations
- User experience flows
- Data modeling decisions
- Integration points

**Important guidelines:**
- Make sure the questions are NOT obvious - dig deep into non-obvious implications
- Ask about things I might not have considered
- Challenge assumptions in the spec
- Explore edge cases thoroughly
- Be very in-depth and thorough

## Workflow

1. Read any referenced spec file
2. Ask probing questions using AskUserQuestion (one topic at a time)
3. Continue interviewing me continually until the spec feels complete
4. After the interview is complete, write/update the spec file with all the clarified details

## Example Usage

```
/interview @docs/specs/new-feature.md
```

Or just describe what you're building:

```
/interview "I want to build a notification system"
```
