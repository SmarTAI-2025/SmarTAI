#!/usr/bin/env python3
"""Publish the already-tested candidate; never merge, approve, force-push or close.

Default/check mode performs reads only. --publish explicitly authorizes writes.
Run with the repository path and your normal authorized git/GitHub CLI setup.
No credential value is accepted or printed by this script.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "SmarTAI-2025/SmarTAI"
BRANCH = "fix/verified-integration-20260920"
DOCS = "docs/integration/20260920"
OLD_PRS = (40, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67)
MARKER = "<!-- smartai-integration-20260920:review -->"


class PublishError(RuntimeError):
    pass


class Commands:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(self, command: list[str], *, allow_nonzero: bool = False) -> tuple[int, str]:
        result = subprocess.run(command, cwd=self.repo, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode and not allow_nonzero:
            # stderr can include URLs/configuration; keep public failure output bounded.
            raise PublishError(f"{command[0]} {command[1] if len(command)>1 else ''} failed (exit {result.returncode}).")
        return result.returncode, result.stdout.strip()

    def git(self, *args: str) -> str:
        return self.run(["git", *args])[1]

    def gh_json(self, *args: str) -> Any:
        return json.loads(self.run(["gh", *args])[1] or "null")


def validate_local(cmd: Commands, manifest: dict[str, Any]) -> str:
    if manifest.get("repository") != REPOSITORY or manifest.get("branch") != BRANCH:
        raise PublishError("Manifest targets a different repository/branch.")
    if cmd.git("branch", "--show-current") != BRANCH:
        raise PublishError("Checkout the tested integration branch first.")
    if cmd.git("status", "--porcelain"):
        raise PublishError("Working tree is not clean. Do not publish uncommitted work.")
    origin = cmd.git("remote", "get-url", "origin").rstrip("/")
    allowed = {f"https://github.com/{REPOSITORY}.git", f"https://github.com/{REPOSITORY}",
               f"git@github.com:{REPOSITORY}.git", f"ssh://git@github.com/{REPOSITORY}.git"}
    if origin not in allowed:
        raise PublishError("origin is not the intended repository.")
    code = manifest["code_sha"]
    if len(code) != 40 or any(c not in "0123456789abcdef" for c in code):
        raise PublishError("Invalid tested code SHA.")
    cmd.git("merge-base", "--is-ancestor", code, "HEAD")
    rc, _ = cmd.run(["git", "diff", "--quiet", code, "HEAD", "--", ".",
                     f":(exclude){DOCS}/**", ":(exclude)README.md", ":(exclude)CLAUDE_HANDOFF.md"], allow_nonzero=True)
    if rc:
        raise PublishError("Non-documentation files differ from the tested code commit; retest first.")
    return cmd.git("rev-parse", "HEAD")


def validate_remote(cmd: Commands, manifest: dict[str, Any]) -> None:
    refs = cmd.git("ls-remote", "origin", "refs/heads/main").split()
    if not refs or refs[0] != manifest["base_sha"]:
        raise PublishError("Remote main changed. Integrate/retest/update evidence before publishing.")


def existing_pr(cmd: Commands) -> dict[str, Any] | None:
    rows = cmd.gh_json("pr", "list", "--repo", REPOSITORY, "--head", BRANCH,
                       "--state", "all", "--json", "number,url,state,baseRefName,headRefName")
    if len(rows) > 1:
        raise PublishError("Multiple PRs use this head; resolve explicitly.")
    if rows and (rows[0]["state"] != "OPEN" or rows[0]["baseRefName"] != "main"):
        raise PublishError("Existing PR is closed/merged or has a different base; do not silently change it.")
    return rows[0] if rows else None


def ensure_comment(cmd: Commands, number: int, marker: str, body: str) -> str:
    for page in range(1, 101):
        rows = cmd.gh_json("api", f"repos/{REPOSITORY}/issues/{number}/comments?per_page=100&page={page}")
        for row in rows:
            if marker in row.get("body", ""):
                return row["html_url"]
        if len(rows) < 100:
            break
    else:
        raise PublishError("Comment history exceeds bounded scan; refusing duplicate publication.")
    row = cmd.gh_json("api", "--method", "POST", f"repos/{REPOSITORY}/issues/{number}/comments",
                      "--raw-field", "body=" + marker + "\n\n" + body)
    return row["html_url"]


def publish(cmd: Commands, manifest: dict[str, Any], files: Path, state: dict[str, Any]) -> None:
    head = validate_local(cmd, manifest)
    validate_remote(cmd, manifest)
    permission = cmd.gh_json("api", f"repos/{REPOSITORY}")
    if not permission.get("permissions", {}).get("push", False):
        raise PublishError("The authenticated account lacks repository push permission.")
    current_user = cmd.gh_json("api", "user")["login"]
    old = existing_pr(cmd)
    remote = cmd.git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if remote and remote[0] != head:
        cmd.git("fetch", "origin", f"refs/heads/{BRANCH}")
        cmd.git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD")
    # Ordinary fast-forward/new-branch push only. Never --force or a main refspec.
    cmd.git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    state.update(push="complete", head_sha=head)
    check = cmd.git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not check or check[0] != head:
        raise PublishError("Remote branch does not match the candidate head.")
    if old is None:
        cmd.run(["gh", "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head", BRANCH,
                 "--title", "fix: consolidate verified auth, question, grading and storage repairs",
                 "--body-file", str(files / "PR_BODY.md")])
        old = existing_pr(cmd)
    if old is None:
        raise PublishError("PR creation was not confirmed by a read-back.")
    pr = cmd.gh_json("api", f"repos/{REPOSITORY}/pulls/{old['number']}")
    if pr["head"]["sha"] != head or pr["base"]["ref"] != "main":
        raise PublishError("PR head/base mismatch after creation.")
    state.update(pr="complete", pr_url=pr["html_url"], pr_number=pr["number"])
    state.setdefault("comments", {})
    review = (files / "REVIEW_COMMENT.md").read_text(encoding="utf-8")
    state["comments"][str(pr["number"])] = ensure_comment(cmd, pr["number"], MARKER + "\n<!-- candidate:" + head + " -->", review)
    for number in OLD_PRS:
        body = (f"项目负责人已接受当前大题处理单位与 BYOK 并发政策。对应代码和本轮实质／安全修复已整理到 {pr['html_url']}，"
                f"候选 head `{head}`，目标为 main。请审核新整合候选，不要先把本旧 PR 原样独立合入。"
                "整合 PR 尚未合并；原提交保留情况、#58 PDF 退步排除、#40/#59 替代关系及后续接口约定见新 PR 文档。"
                "待整合 PR 真正合入并核对 main 后，再处理仍 open 的旧 PR；本评论不是合并、关闭或人工批准。")
        marker = f"<!-- smartai-integration-20260920:supersedes:{number}:{head} -->"
        state["comments"][str(number)] = ensure_comment(cmd, number, marker, body)
    if current_user != "lyj00422":
        cmd.run(["gh", "pr", "edit", str(pr["number"]), "--repo", REPOSITORY,
                 "--add-reviewer", "lyj00422"])
        state["review_request"] = "lyj00422"
    else:
        state["review_request"] = "A different qualified non-author reviewer is required."
    state["status"] = "published_not_merged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--files", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publish", action="store_true", help="Explicitly enable remote writes (never merge).")
    parser.add_argument("--check", action="store_true", help="Read-only checks; the default.")
    args = parser.parse_args()
    if args.publish and args.check:
        parser.error("Choose --check or --publish, not both.")
    repo = args.repo.resolve()
    files = args.files.resolve()
    output = args.output or repo.parent / "delivery" / "publication-result.json"
    state: dict[str, Any] = {"status": "not_started", "push": "not_done", "pr": "not_done",
                             "merged": False, "approved": False, "comments": {}}
    try:
        manifest = json.loads((args.manifest or files / "manifest.json").read_text(encoding="utf-8"))
        cmd = Commands(repo)
        state["head_sha"] = validate_local(cmd, manifest)
        validate_remote(cmd, manifest)
        if not args.publish:
            state["status"] = "checks_passed_no_writes"
        else:
            publish(cmd, manifest, files, state)
        return_code = 0
    except (PublishError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        state["status"] = "blocked"
        state["safe_error"] = str(exc) if isinstance(exc, PublishError) else type(exc).__name__
        return_code = 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
