You are responding to code review feedback on a pull request that updates dependencies.

## Context

This pull request updates dependencies:

{{UPDATES}}

## Review feedback

{{COMMENTS}}

## Your task

1. Read every comment. For each one decide which of these it is:
   - **a request for a code change** -> make the change, minimally and idiomatically;
   - **a question** -> answer it in your summary (no code change needed);
   - **a disagreement or a "nit"** -> if the reviewer is right, make the change; if
     you disagree, explain why in your summary rather than silently ignoring it;
   - **something out of scope for a dependency bump** -> say so in the summary and
     do not make that change.
2. A comment may be attached to a file and line that no longer exists because the
   dependency update rewrote it. In that case, find the equivalent code in the
   current tree and apply the change there.
3. If a comment asks you to do something that would break the build or the tests,
   make the change anyway and verify the build still passes. If that is genuinely
   impossible, explain it in your summary.
4. **Do not** widen the scope of the pull request. Do not refactor unrelated code.

## Rules

- You cannot use git to commit, push, or reply on GitHub. The pipeline does that.
- Keep changes minimal and consistent with the surrounding code.
- Stay inside the repository directory.

## When you are done

Report back in exactly this format:

```
ADDRESSED: <count of comments addressed>
REPLIES: <for each comment that was a question or a disagreement, one line:
          "#<n>: <your reply>">
FILES: <comma-separated list of files you changed>
DECLINED: <for each comment you did not act on, one line: "#<n>: <reason>">
NOTES: <anything the human should know, or "none">
```
