"""把本插件目录通过 GitHub REST API 提交到仓库。

为什么要用 API 而不是 git push：本机 github.com:443 连接会被重置，
但 api.github.com 可达且返回 200，因此走 Contents API 提交文件。

凭据通过环境变量 DSH_GH_TOKEN 传入（不落盘、不进入对话记录）。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
TOKEN = os.environ.get("DSH_GH_TOKEN", "").strip()

REPO = os.environ.get("DSH_GH_REPO", "").strip()  # owner/name
BRANCH = os.environ.get("DSH_GH_BRANCH", "").strip()  # 空则用仓库默认分支
SRC = os.environ.get("DSH_GH_SRC", "").strip()
AUTHOR_NAME = os.environ.get("DSH_GH_AUTHOR", "").strip()
AUTHOR_EMAIL = os.environ.get("DSH_GH_EMAIL", "").strip()
COMMIT_MSG = os.environ.get("DSH_GH_MESSAGE", "feat: 随机数生成 LLM 工具插件 v1.0.0")

EXCLUDE_DIRS = {"__pycache__", ".git"}


def die(msg: str, code: int = 1):
    print(f"[错误] {msg}")
    sys.exit(code)


def request(method: str, path: str, payload: dict | None = None, allow_404=False):
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "dsh-agent")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 404 and allow_404:
            return 404, None
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("message", body)
            if parsed.get("errors"):
                detail += f" | {parsed['errors']}"
        except Exception:  # noqa: BLE001
            pass
        die(f"{method} {url} -> HTTP {e.code}: {detail}")
    except Exception as e:  # noqa: BLE001
        die(f"{method} {url} -> 网络异常: {type(e).__name__}: {e}")


def collect_files(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    return sorted(out)


def main():
    if not TOKEN:
        die("未提供凭据（环境变量 DSH_GH_TOKEN 为空）")
    if not REPO or "/" not in REPO:
        die("未提供仓库（DSH_GH_REPO 需为 owner/name 形式）")
    if not SRC or not os.path.isdir(SRC):
        die(f"源目录不存在: {SRC!r}")

    # 1. 校验凭据
    status, user = request("GET", "/user")
    login = (user or {}).get("login")
    print(f"凭据校验通过: 已登录为 {login} (HTTP {status})")
    owner = REPO.split("/")[0]
    if login and login.lower() != owner.lower():
        print(f"[提示] 登录账号 {login} 与仓库 owner {owner} 不一致，将尝试按仓库 owner 提交")

    # 2. 确认仓库存在并取默认分支
    status, repo_info = request("GET", f"/repos/{REPO}")
    default_branch = BRANCH or (repo_info or {}).get("default_branch", "main")
    print(f"仓库确认: {REPO}（默认分支 {default_branch}，"
          f"可见性={'公开' if not (repo_info or {}).get('private') else '私有'}）")

    # 3. 逐个提交文件
    files = collect_files(SRC)
    print(f"待提交文件 {len(files)} 个")
    print()

    committed = 0
    for rel in files:
        full = os.path.join(SRC, rel.replace("/", os.sep))
        with open(full, "rb") as f:
            raw = f.read()
        content_b64 = base64.b64encode(raw).decode("ascii")

        # 已存在则需带 sha 才能更新
        _, existing = request(
            "GET", f"/repos/{REPO}/contents/{rel}?ref={default_branch}", allow_404=True
        )
        sha = (existing or {}).get("sha") if isinstance(existing, dict) else None

        payload = {
            "message": f"{COMMIT_MSG}\n\n{rel}",
            "content": content_b64,
            "branch": default_branch,
        }
        if sha:
            payload["sha"] = sha
        if AUTHOR_NAME and AUTHOR_EMAIL:
            payload["committer"] = {"name": AUTHOR_NAME, "email": AUTHOR_EMAIL}
            payload["author"] = {"name": AUTHOR_NAME, "email": AUTHOR_EMAIL}

        status, res = request("PUT", f"/repos/{REPO}/contents/{rel}", payload)
        action = "更新" if sha else "新增"
        print(f"  [{action}] {rel:<24} {len(raw):>6} 字节  HTTP {status}")
        committed += 1

    print()
    print(f"完成：{committed}/{len(files)} 个文件已提交到 {REPO}@{default_branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
